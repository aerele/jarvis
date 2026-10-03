"""Settings -> "Delete all chat history" keeps the chats that are waiting for a reply.

``clear_chat_history`` used to force-delete every conversation of the caller. Deleting
a conversation removes its ``Jarvis Chat Turn`` rows, and the pump reads the missing
row of a turn it is streaming as a lost lease: the hop ends ``lease_lost``, which
schedules no successor, so nothing drains the shard until the lease expires or the
watchdog runs. The first class drives a real hop through that; the second pins which
conversations are kept and which go.

Runs as the pump suite's fixture user with a per-test shard, so it never touches the
site-wide admission shard or anyone's real history.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import frappe

from jarvis.chat import admission, pump
from jarvis.chat import api as chat_api
from jarvis.chat import turn_state as ts
from jarvis.tests._pending_action_helpers import ensure_user
from jarvis.tests.harness import transcripts
from jarvis.tests.race_harness import other_connection, snapshot_isolation_on
from jarvis.tests.test_pump import CONV, MSG, TEST_USER, TURN, _PumpTestCase
from jarvis.tests.test_pump import _cleanup as _cleanup_conversations
from jarvis.tests.test_relay_mux import _DoubleGateway
from jarvis.tests.test_tool_reentry_guards import _InDispatch

BYSTANDER = "clear-history-bystander@example.com"
RUN = "Jarvis Macro Run"
FAILED_LOG = "jarvis.chat.clear_history.conversation_failed"


def setUpModule():
	from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile

	install_synthetic_runtime_profile()


def _result(*kept, deleted, failed=()):
	"""``kept``: left because a reply is in progress. ``failed``: not deleted because
	of an error."""
	return {
		"ok": True,
		"deleted": deleted,
		"skipped": len(kept),
		"kept": list(kept),
		"failed": len(failed),
		"failed_names": list(failed),
	}


def _logged(log, title) -> list[dict]:
	"""The ``frappe.log_error`` calls under ``title``, as kwargs."""
	return [c.kwargs for c in log.call_args_list if c.kwargs.get("title") == title]


class _HeldGateway(_DoubleGateway):
	"""The transport double, with a reply that stops part-way: the first frames are
	delivered, the rest wait for ``resume``. That leaves a turn ``streaming`` across
	slices, which is where another request can land."""

	FRAMES_BEFORE_THE_HOLD = 2

	def __init__(self):
		super().__init__()
		self.resume = threading.Event()
		self._events = 0

	def _push(self, frame):
		if isinstance(frame, dict) and frame.get("type") == "event":
			self._events += 1
			if self._events > self.FRAMES_BEFORE_THE_HOLD:
				self.resume.wait(timeout=20)
		super()._push(frame)

	def stop(self):
		self.resume.set()
		super().stop()


class TestDeleteAllDuringALiveReply(_PumpTestCase):
	def test_the_hop_keeps_running_and_the_reply_finishes(self):
		live = self._mk_conv()
		seed = self._mk_msg(live, content="hello")
		rid = f"clr_{frappe.generate_hash(length=8)}"
		self._mk_turn(live, rid, seed, "queued", version=0, reserved=0)
		idle = self._mk_conv()
		self._mk_msg(idle, content="an older chat")

		double = _HeldGateway()
		self._doubles.append(double)
		deps = self._deps(double=double, prepare=self._prepare_stub(live, double, "success"))
		seen = {}
		real_slice = pump.drain_slice

		def slice_with_the_user_pressing_delete_all(ctx):
			state = self._state(rid)
			if "out" not in seen and state == "streaming":
				# Mid-reply, between two slices: the owner presses "Delete all".
				seen["out"] = chat_api.clear_chat_history()
				double.resume.set()
			elif "out" in seen and state == "finalizing":
				ctx.soft_deadline = 0  # the reply is settled: let the hop hand off
			return real_slice(ctx)

		with patch.object(pump, "drain_slice", slice_with_the_user_pressing_delete_all):
			res = pump.run_pump_hop(self._target, deps=deps, soft_budget_s=999, max_slices=150)

		self.assertIn("out", seen, "the turn never reached streaming")
		self.assertEqual(
			(res["exit"], deps.enqueue_pump_job.count),
			("handoff", 1),
			"the hop must go on draining the shard and hand off to a successor",
		)
		self.assertEqual(seen["out"], _result(live, deleted=1))
		self.assertFalse(frappe.db.exists(CONV, idle))
		self.assertTrue(frappe.db.exists(CONV, live))
		self.assertEqual(self._state(rid), "finalizing", "the reply was settled")
		reply = frappe.db.get_value(MSG, self._val(rid, "assistant_message"), "content")
		self.assertEqual(reply, transcripts.get("success")["terminal"]["text"])


class _ChatsTestCase(_PumpTestCase):
	def setUp(self):
		super().setUp()
		self._n = 0

	def _chat(self, *states, streaming_reply=False) -> str:
		"""A conversation with one user message, a Turn in each of ``states`` and,
		optionally, a reply row still marked streaming."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="hello")
		if streaming_reply:
			self._mk_msg(conv, role="assistant", content="partial", streaming=1)
		for state in states:
			self._n += 1
			self._mk_turn(conv, f"clr_{self._n}_{frappe.generate_hash(length=6)}", seed, state)
		return conv

	def _rows(self, conv) -> tuple:
		return (
			bool(frappe.db.exists(CONV, conv)),
			frappe.db.count(MSG, {"conversation": conv}),
			frappe.db.count(TURN, {"conversation": conv}),
		)


class TestWhichChatsAreKept(_ChatsTestCase):
	def _age_messages(self, conv, *, seconds) -> None:
		"""Nothing was written to this chat for ``seconds``: a reply row still marked
		streaming is then a flag a dead worker left behind, not a reply."""
		then = frappe.utils.add_to_date(None, seconds=-seconds)
		frappe.db.sql(f"UPDATE `tab{MSG}` SET modified=%s WHERE conversation=%s", (then, conv))
		frappe.db.commit()

	def _macro_run_in(self, conv) -> str:
		"""A finished macro run that still links ``conv`` (run history outlives a chat)."""
		run = frappe.get_doc(
			{"doctype": RUN, "macro": "clr-no-such-macro", "conversation": conv, "status": "completed"}
		)
		run.flags.ignore_links = run.flags.ignore_mandatory = True
		run.insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(self._drop_run, run.name)
		return run.name

	@staticmethod
	def _drop_run(run) -> None:
		frappe.db.delete(RUN, {"name": run})
		frappe.db.commit()

	def test_with_no_reply_in_progress_everything_is_deleted(self):
		plain = self._chat()
		finished = self._chat(*ts.TERMINAL_STATES)
		self.assertEqual(chat_api.clear_chat_history(), _result(deleted=2))
		for conv in (plain, finished):
			self.assertEqual(self._rows(conv), (False, 0, 0))

	def test_with_no_chats_there_is_nothing_to_do(self):
		self.assertEqual(chat_api.clear_chat_history(), _result(deleted=0))

	def test_a_chat_with_a_turn_that_has_not_ended_is_kept_whole(self):
		for state in ts.NONTERMINAL_STATES:
			with self.subTest(state=state):
				waiting = self._chat("done", state)
				other = self._chat("done")
				self.assertEqual(chat_api.clear_chat_history(), _result(waiting, deleted=1))
				self.assertEqual(self._rows(waiting), (True, 1, 2))
				self.assertEqual(self._rows(other), (False, 0, 0))
				_cleanup_conversations()

	def test_a_kept_chat_can_be_deleted_once_its_reply_has_finished(self):
		conv = self._chat("streaming")
		self.assertEqual(chat_api.clear_chat_history()["skipped"], 1)
		frappe.db.set_value(TURN, {"conversation": conv}, "state", "done", update_modified=False)
		frappe.db.commit()
		self.assertEqual(chat_api.clear_chat_history(), _result(deleted=1))
		self.assertEqual(self._rows(conv), (False, 0, 0))

	def test_a_chat_without_turn_rows_is_kept_while_its_reply_row_is_streaming(self):
		# The pre-turn-machine shape: the only sign of a reply in progress is the
		# reply row itself.
		legacy = self._chat(streaming_reply=True)
		self.assertEqual(chat_api.clear_chat_history(), _result(legacy, deleted=0))
		self.assertEqual(self._rows(legacy), (True, 2, 0))

	def test_a_reply_still_streaming_keeps_a_chat_whose_turns_have_all_ended(self):
		# A site that is draining, or was switched back to the legacy transport, sends
		# without a Turn row. Such a chat has old ended Turns and a live reply row:
		# either sign of a reply keeps it, the Turn rows do not overrule the reply.
		conv = self._chat(*ts.TERMINAL_STATES, streaming_reply=True)
		self.assertEqual(chat_api.clear_chat_history(), _result(conv, deleted=0))
		self.assertEqual(self._rows(conv), (True, 2, 3))

	def test_a_streaming_flag_nothing_has_written_under_for_a_while_keeps_no_chat(self):
		# The age-out every send already goes by (``api._conversation_busy``): a
		# streaming flag on a chat that has been silent past the freshness window is
		# what a dead worker left, and must not make the chat undeletable.
		for states in ((), ("done",)):
			with self.subTest(turns=states):
				conv = self._chat(*states, streaming_reply=True)
				self._age_messages(conv, seconds=chat_api._INFLIGHT_FRESH_SECONDS + 60)
				self.assertEqual(chat_api.clear_chat_history(), _result(deleted=1))
				self.assertEqual(self._rows(conv), (False, 0, 0))

	def test_a_turn_that_starts_while_the_delete_is_under_way_keeps_its_chat(self):
		# Between the first look at a chat and its delete, a send lands in it. The
		# second look, under the conversation's row lock, sees the new Turn.
		conv = self._chat("done")
		seed = frappe.db.get_value(MSG, {"conversation": conv, "role": "user"}, "name")
		real_stop = chat_api._stop_macro_runs_in
		seen = []

		def stop_then_a_send_lands(conversations, **kw):
			real_stop(conversations, **kw)
			seen.extend(conversations)
			# Another request's send: the pump suite's own Turn fixture, committed on
			# a second connection.
			mine = frappe.local.db
			with other_connection() as other:
				frappe.local.db = other
				try:
					self._mk_turn(conv, "clr_late_send", seed, "queued", version=0)
				finally:
					frappe.local.db = mine

		with patch.object(chat_api, "_stop_macro_runs_in", side_effect=stop_then_a_send_lands):
			out = chat_api.clear_chat_history()
		self.assertEqual(seen, [conv])
		self.assertEqual(out, _result(conv, deleted=0))
		self.assertEqual(self._rows(conv), (True, 1, 2))

	def test_a_deleted_chat_lets_go_of_its_macro_runs_and_a_kept_chat_keeps_them(self):
		gone, kept = self._chat("done"), self._chat("streaming")
		run_of_gone, run_of_kept = self._macro_run_in(gone), self._macro_run_in(kept)
		self.assertEqual(chat_api.clear_chat_history(), _result(kept, deleted=1))
		# Run history survives its chat, with the link blanked rather than dangling.
		self.assertFalse(frappe.db.get_value(RUN, run_of_gone, "conversation"))
		self.assertEqual(frappe.db.get_value(RUN, run_of_kept, "conversation"), kept)

	def test_the_macro_run_link_is_blanked_by_name_and_only_when_there_is_one(self):
		# No statement here may scan (and so lock) the site's macro runs: the link is
		# read without a lock and written by primary key.
		self._macro_run_in(self._chat("done"))
		self._chat("done")
		writes = []
		real_sql = frappe.db.sql

		def sql(query, *args, **kw):
			if str(query).lstrip().upper().startswith("UPDATE") and RUN in str(query):
				writes.append(" ".join(str(query).split()))
			return real_sql(query, *args, **kw)

		with patch.object(frappe.db, "sql", side_effect=sql):
			self.assertEqual(chat_api.clear_chat_history(), _result(deleted=2))
		self.assertEqual(len(writes), 1, "one chat had a run, so one write")
		self.assertIn("WHERE name IN", writes[0])
		self.assertNotIn("WHERE conversation", writes[0])

	def test_one_chat_that_cannot_be_deleted_does_not_stop_the_others(self):
		bad, good, other = self._chat("done"), self._chat("done"), self._chat("done")
		real = chat_api._unlink_macro_runs

		def unlink(conversation):
			if conversation == bad:
				raise RuntimeError("boom")
			return real(conversation)

		with (
			patch.object(chat_api, "_unlink_macro_runs", side_effect=unlink),
			patch("frappe.log_error") as log,
		):
			out = chat_api.clear_chat_history()
		# Not deleted because of an error, which is not "kept because a reply is in
		# progress": it is counted, and named, apart.
		self.assertEqual(out, _result(deleted=2, failed=[bad]))
		for conv in (good, other):
			self.assertEqual(self._rows(conv), (False, 0, 0))
		# The failed one is whole: its messages were deleted before the failure, and
		# that was rolled back, not committed by the next chat's work.
		self.assertEqual(self._rows(bad), (True, 1, 1))
		(failure,) = _logged(log, FAILED_LOG)
		self.assertIn(f"conversation {bad}", failure["message"])
		self.assertIn("RuntimeError: boom", failure["message"])
		self.assertEqual(len(log.call_args_list), 1, "the failure's own row and no other")
		# Its row lock is let go at once (a send to it would be waiting on it).
		with other_connection() as elsewhere:
			elsewhere.sql("SET SESSION innodb_lock_wait_timeout=1")
			elsewhere.sql(f"SELECT name FROM `tab{CONV}` WHERE name=%s FOR UPDATE", bad)

	def test_a_delete_that_loses_a_write_race_is_run_again(self):
		conv = self._chat("done")
		real = chat_api._unlink_macro_runs
		calls = []

		def unlink(conversation):
			calls.append(conversation)
			if len(calls) == 1:
				raise frappe.QueryDeadlockError(1020, "Record has changed since last read")
			return real(conversation)

		with (
			patch.object(chat_api, "_unlink_macro_runs", side_effect=unlink),
			patch("frappe.log_error") as log,
		):
			self.assertEqual(chat_api.clear_chat_history(), _result(deleted=1))
		self.assertEqual(calls, [conv, conv])
		self.assertEqual(self._rows(conv), (False, 0, 0))
		log.assert_not_called()

	def test_keeping_a_chat_that_is_waiting_for_a_reply_writes_no_error_log_row(self):
		# Keeping a busy chat is what the endpoint is for. An Error Log row titled
		# ``jarvis.`` is forwarded to the control plane as an error, so the by-design
		# outcome writes none: the answer already says what was kept.
		streaming, queued = self._chat("done", "streaming"), self._chat("queued")
		legacy = self._chat(streaming_reply=True)
		self._chat("done")
		before = frappe.db.count("Error Log")
		with patch("frappe.log_error") as log:
			out = chat_api.clear_chat_history()
		self.assertEqual((out["deleted"], sorted(out["kept"])), (1, sorted([streaming, queued, legacy])))
		self.assertEqual((out["skipped"], out["failed"], out["failed_names"]), (3, 0, []))
		log.assert_not_called()
		self.assertEqual(frappe.db.count("Error Log"), before)

	def _clear_with_failing(self, failing) -> tuple[dict, list[dict], list[str]]:
		"""Delete all, with the delete of every chat in ``failing`` raising. Answers
		the response, the per-chat failure rows, and the chats a delete was tried on."""
		real = chat_api._delete_idle_conversation
		tried = []

		def delete(conversation):
			tried.append(conversation)
			if conversation in failing:
				raise RuntimeError("boom")
			return real(conversation)

		with (
			patch.object(chat_api, "_delete_idle_conversation", side_effect=delete),
			patch("frappe.log_error") as log,
		):
			out = chat_api.clear_chat_history()
		self.assertEqual(len(log.call_args_list), len(_logged(log, FAILED_LOG)))
		return out, _logged(log, FAILED_LOG), tried

	def _chats_in_delete_order(self, count) -> list[str]:
		for _ in range(count):
			self._chat("done")
		return frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name")

	def _start_a_reply_in(self, conv) -> None:
		seed = frappe.db.get_value(MSG, {"conversation": conv, "role": "user"}, "name")
		self._mk_turn(conv, f"clr_busy_{frappe.generate_hash(length=6)}", seed, "streaming")

	def test_three_failures_in_a_row_end_the_request_and_the_rest_are_reported_failed(self):
		# A fault common to every chat (a broken trash hook, a held lock) would
		# otherwise cost one Error Log row with a traceback, and up to a lock wait,
		# per chat. The busy chat past the stop is not looked at either: it is
		# reported as not deleted, never as "a reply is in progress".
		order = self._chats_in_delete_order(6)
		busy = order[4]
		self._start_a_reply_in(busy)
		out, failures, tried = self._clear_with_failing(set(order))
		self.assertEqual(tried, order[:3])
		self.assertEqual(len(failures), 3)
		self.assertEqual(out, _result(deleted=0, failed=order))
		for conv in order:
			self.assertTrue(frappe.db.exists(CONV, conv))

	def test_failures_that_are_not_in_a_row_do_not_end_the_request(self):
		# Only an unbroken run of failures reads as "everything is failing". Any
		# chat that is deleted, kept for its reply, or already gone starts the count
		# again.
		order = self._chats_in_delete_order(7)
		failing = {order[0], order[1], order[3], order[4], order[6]}
		out, failures, tried = self._clear_with_failing(failing)
		self.assertEqual(tried, order)
		self.assertEqual(len(failures), 5)
		self.assertEqual(out, _result(deleted=2, failed=[c for c in order if c in failing]))
		self.assertEqual(out["deleted"] + out["skipped"] + out["failed"], len(order))

	def test_a_kept_chat_between_failures_starts_the_count_again(self):
		order = self._chats_in_delete_order(5)
		busy = order[2]
		self._start_a_reply_in(busy)
		failing = set(order) - {busy}
		out, failures, tried = self._clear_with_failing(failing)
		self.assertEqual(tried, order)
		self.assertEqual(len(failures), 4)
		self.assertEqual(out, _result(busy, deleted=0, failed=[c for c in order if c != busy]))

	def test_nothing_is_logged_when_nothing_was_kept(self):
		self._chat("done")
		with patch("frappe.log_error") as log:
			self.assertEqual(chat_api.clear_chat_history(), _result(deleted=1))
		log.assert_not_called()

	def test_a_chat_someone_else_deleted_meanwhile_is_not_counted(self):
		# A second tab pressing "Delete all" at the same moment: each chat is counted
		# by the request that deleted it.
		conv, mine = self._chat("done"), self._chat("done")
		real_stop = chat_api._stop_macro_runs_in

		def stop_then_the_other_tab_deletes_it(conversations, **kw):
			real_stop(conversations, **kw)
			if conversations == [conv]:
				with other_connection() as other:
					for doctype in (TURN, MSG):
						other.sql(f"DELETE FROM `tab{doctype}` WHERE conversation=%s", conv)
					other.sql(f"DELETE FROM `tab{CONV}` WHERE name=%s", conv)
					other.commit()

		with patch.object(chat_api, "_stop_macro_runs_in", side_effect=stop_then_the_other_tab_deletes_it):
			self.assertEqual(chat_api.clear_chat_history(), _result(deleted=1))
		self.assertEqual(self._rows(mine), (False, 0, 0))

	def test_another_users_chats_are_never_touched(self):
		frappe.set_user("Administrator")
		ensure_user(BYSTANDER)
		frappe.set_user(BYSTANDER)
		try:
			theirs_idle = self._chat("done")
			theirs_live = self._chat("streaming")
			frappe.set_user(TEST_USER)
			mine = self._chat("done")
			self.assertEqual(chat_api.clear_chat_history(), _result(deleted=1))
			self.assertEqual(self._rows(mine), (False, 0, 0))
			self.assertEqual(self._rows(theirs_idle), (True, 1, 1))
			self.assertEqual(self._rows(theirs_live), (True, 1, 1))
		finally:
			frappe.set_user("Administrator")
			_cleanup_conversations(BYSTANDER)
			frappe.set_user(TEST_USER)


class TestAWriteConflictInTheTrashHook(_ChatsTestCase):
	"""``admission.on_conversation_trash`` deletes the conversation's Turn rows inside
	the delete's transaction. When that statement loses a write race the server has
	already aborted the whole transaction, so the hook must not swallow it."""

	def test_a_turn_row_moved_after_the_second_look_is_replayed_not_half_deleted(self):
		# A real conflict: the snapshot check armed, and another connection commits
		# a write to the chat's (ended) Turn row once this request has looked at it
		# under the lock. With the conflict swallowed the conversation went, its
		# message and Turn row stayed for good, and the answer said "deleted".
		conv = self._chat("done")
		real = admission.reply_in_progress
		looks = []

		def look_then_the_turn_row_moves(conversation):
			out = real(conversation)
			looks.append(conversation)
			if len(looks) == 2:
				with other_connection() as other:
					other.sql(f"UPDATE `tab{TURN}` SET modified=NOW(6) WHERE conversation=%s", conv)
					other.commit()
			return out

		with (
			snapshot_isolation_on(),
			patch.object(admission, "reply_in_progress", side_effect=look_then_the_turn_row_moves),
			patch("frappe.log_error") as log,
		):
			out = chat_api.clear_chat_history()
		frappe.db.commit()
		self.assertEqual(looks, [conv] * 3, "first look, second look, and the replay's look")
		self.assertEqual(out, _result(deleted=1))
		self.assertEqual(self._rows(conv), (False, 0, 0))
		log.assert_not_called()

	def test_the_hook_lets_a_write_conflict_through(self):
		conv = frappe.get_doc(CONV, self._chat("done"))
		conflict = frappe.QueryDeadlockError(1020, "Record has changed since last read")
		with (
			patch.object(frappe.db, "delete", side_effect=conflict),
			patch("frappe.log_error") as log,
		):
			with self.assertRaises(frappe.QueryDeadlockError):
				admission.on_conversation_trash(conv)
		log.assert_not_called()

	def test_the_hook_still_logs_any_other_failure_and_lets_the_delete_go_on(self):
		conv = frappe.get_doc(CONV, self._chat("done"))
		with (
			patch.object(frappe.db, "delete", side_effect=RuntimeError("boom")),
			patch("frappe.log_error") as log,
		):
			admission.on_conversation_trash(conv)
		(row,) = log.call_args_list
		self.assertEqual(row.kwargs["title"], "admission.on_conversation_trash")


class TestStillRefusedInsideATool(_InDispatch):
	def test_delete_all_is_refused_inside_a_tool_dispatch(self):
		with patch.object(chat_api, "_delete_idle_conversation") as delete:
			err = self._raised_inside(chat_api.clear_chat_history)
		self.assertIsInstance(err, frappe.PermissionError)
		self.assertIn("inside a tool", str(err))
		delete.assert_not_called()
