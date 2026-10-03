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

from jarvis.chat import api as chat_api
from jarvis.chat import pump
from jarvis.chat import turn_state as ts
from jarvis.tests._pending_action_helpers import ensure_user
from jarvis.tests.harness import transcripts
from jarvis.tests.race_harness import other_connection
from jarvis.tests.test_pump import CONV, MSG, TEST_USER, TURN, _PumpTestCase
from jarvis.tests.test_pump import _cleanup as _cleanup_conversations
from jarvis.tests.test_relay_mux import _DoubleGateway
from jarvis.tests.test_tool_reentry_guards import _InDispatch

BYSTANDER = "clear-history-bystander@example.com"


def setUpModule():
	from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile

	install_synthetic_runtime_profile()


def _result(*kept, deleted):
	return {"ok": True, "deleted": deleted, "skipped": len(kept), "kept": list(kept)}


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


class TestWhichChatsAreKept(_PumpTestCase):
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

	def test_turn_rows_decide_when_there_are_any(self):
		# Every Turn has ended: a `streaming` flag left on a reply row does not keep
		# the chat (the turn machine is the authority once it has seen the chat).
		conv = self._chat("done", streaming_reply=True)
		self.assertEqual(chat_api.clear_chat_history(), _result(deleted=1))
		self.assertEqual(self._rows(conv), (False, 0, 0))

	def test_a_turn_that_starts_while_the_delete_is_under_way_keeps_its_chat(self):
		# Between the first look at a chat and its delete, a send lands in it. The
		# second look, under the conversation's row lock, sees the new Turn.
		conv = self._chat("done")
		seed = frappe.db.get_value(MSG, {"conversation": conv, "role": "user"}, "name")
		row = frappe.db.get_value(TURN, {"conversation": conv}, "*", as_dict=True)
		real_stop = chat_api._stop_macro_runs_in
		seen = []

		def stop_then_a_send_lands(conversations):
			real_stop(conversations)
			seen.extend(conversations)
			with other_connection() as other:
				other.sql(
					f"""INSERT INTO `tab{TURN}`
						(name, run_id, conversation, relay_target_id, turn_class, state, version,
						 seed_message, owner, creation, modified)
					VALUES (%(n)s, %(n)s, %(c)s, %(t)s, 'interactive', 'queued', 0, %(s)s, %(o)s, NOW(), NOW())""",
					{"n": "clr_late_send", "c": conv, "t": row.relay_target_id, "s": seed, "o": TEST_USER},
				)
				other.commit()

		with patch.object(chat_api, "_stop_macro_runs_in", side_effect=stop_then_a_send_lands):
			out = chat_api.clear_chat_history()
		self.assertEqual(seen, [conv])
		self.assertEqual(out, _result(conv, deleted=0))
		self.assertEqual(self._rows(conv), (True, 1, 2))

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


class TestStillRefusedInsideATool(_InDispatch):
	def test_delete_all_is_refused_inside_a_tool_dispatch(self):
		with patch.object(chat_api, "_delete_conversation_unless_replying") as delete:
			err = self._raised_inside(chat_api.clear_chat_history)
		self.assertIsInstance(err, frappe.PermissionError)
		self.assertIn("inside a tool", str(err))
		delete.assert_not_called()
