"""Tests for jarvis.chat.stale_scan.scan_and_mark_errored.

Runs as the fixture user ``TEST_USER`` so it never wipes Administrator's
chat history on a dev site.
"""

from collections import defaultdict
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from jarvis.chat import admission, api, pump, stale_scan
from jarvis.chat import turn_state as ts
from jarvis.chat.api import create_conversation
from jarvis.chat.stale_scan import scan_and_mark_errored
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests.race_harness import open_read_view, other_connection
from jarvis.tests.test_chat_api import (
	TEST_USER,
	_cleanup_user_conversations,
	_ensure_test_user,
)
from jarvis.tests.test_pump import _PumpTestCase, _Recorder

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"


def setUpModule():
	# The pump-mode class below drives a real pump slice, which reads the runtime profile.
	install_synthetic_runtime_profile()


def _age(message: str, seconds: int = 300) -> None:
	"""Backdate a message's creation so it sits inside the orphan sweep's window."""
	old = now_datetime() - timedelta(seconds=seconds)
	frappe.db.sql(f"UPDATE `tab{MSG}` SET creation=%s WHERE name=%s", (old, message))
	frappe.db.commit()


class TestScanAndMarkErrored(FrappeTestCase):
	def setUp(self):
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv = create_conversation()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _insert_stale_streaming_message(self, age_seconds: int) -> str:
		"""Insert an assistant message with streaming=1 and the given age."""
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": self.conv,
				"seq": 1,
				"role": "assistant",
				"content": "partial",
				"streaming": 1,
			}
		)
		doc.insert(ignore_permissions=True)
		# Forcibly age the creation timestamp (use Frappe's local-time helper
		# to match how scan_and_mark_errored computes the cutoff)
		old = now_datetime() - timedelta(seconds=age_seconds)
		frappe.db.sql(
			"UPDATE `tabJarvis Chat Message` SET creation = %s WHERE name = %s",
			(old, doc.name),
		)
		frappe.db.commit()
		return doc.name

	def test_marks_old_streaming_messages_errored(self):
		name = self._insert_stale_streaming_message(age_seconds=300)
		with patch("jarvis.chat.stale_scan.publish_to_user") as pub:
			scan_and_mark_errored()
		doc = frappe.get_doc(MSG, name)
		self.assertEqual(doc.streaming, 0)
		self.assertIn("abandoned", doc.error.lower())
		pub.assert_called_once()
		self.assertEqual(pub.call_args.args[1]["kind"], "run:error")

	def test_leaves_fresh_streaming_messages_alone(self):
		name = self._insert_stale_streaming_message(age_seconds=30)
		scan_and_mark_errored()
		doc = frappe.get_doc(MSG, name)
		self.assertEqual(doc.streaming, 1)
		self.assertIsNone(doc.error)

	def test_leaves_completed_messages_alone(self):
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": self.conv,
				"seq": 1,
				"role": "assistant",
				"content": "done",
				"streaming": 0,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		scan_and_mark_errored()
		doc.reload()
		self.assertEqual(doc.streaming, 0)
		self.assertIsNone(doc.error)

	def test_promotes_managed_streaming_to_recovering(self):
		# A stale streaming row whose conversation has a gateway session_key is
		# recoverable: promoted to recovering (handed to turn_recovery), NOT
		# errored. Covers a worker hard-killed before it could mark recovering.
		# Past the 720s managed cap so it is definitely orphaned, not live.
		frappe.db.set_value(CONV, self.conv, "session_key", "sk_managed")
		frappe.db.commit()
		name = self._insert_stale_streaming_message(age_seconds=800)
		with (
			patch("jarvis.chat.stale_scan.publish_to_user") as pub,
		):
			scan_and_mark_errored()
		doc = frappe.get_doc(MSG, name)
		self.assertEqual(doc.streaming, 1)  # spinner stays up
		self.assertEqual(doc.recovering, 1)  # handed to turn_recovery
		self.assertIsNone(doc.error)
		pub.assert_not_called()  # no false run:error


class TestOrphanRedispatchOverload(FrappeTestCase):
	"""CDX-19 (residual) — when the orphan sweep's re-dispatch hits a FULL admission queue, the
	momentary overload must NOT consume the one healing strike: was_recovered is reset to 0 so a
	later scan retries, and no spurious second-strike error is surfaced. Pump is pinned OFF so
	the re-dispatch takes the deterministic phase-0 accept path (no pump start side effects)."""

	TURN = "Jarvis Chat Turn"

	def setUp(self):
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv = create_conversation()
		self._pump_patches = [
			patch.dict(frappe.local.conf, {"jarvis_phase0_admission_enabled": 1}),
			patch.object(pump, "pump_mode_active", return_value=False),
			patch.object(pump, "pump_configured", return_value=False),
			patch.object(pump, "pump_draining", return_value=False),
			patch.object(
				pump,
				"transport_predicates_from_row",
				return_value={"mode": "legacy", "pump_active": False, "configured": False},
			),
		]
		for p in self._pump_patches:
			p.start()

	def tearDown(self):
		for p in self._pump_patches:
			p.stop()
		frappe.db.delete(self.TURN, {"conversation": self.conv})
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _mk_orphan(self, age_seconds: int = 300) -> str:
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": self.conv,
				"seq": 1,
				"role": "user",
				"content": "hi",
				"streaming": 0,
			}
		)
		doc.insert(ignore_permissions=True)
		_age(doc.name, age_seconds)
		return doc.name

	def _queued_job(self):
		job = MagicMock()
		job.origin = "bench:long"
		job.kwargs = {}
		return job

	def test_a_stale_registry_leaves_a_queued_orphan_alone(self):
		# Workers alive but invisible in the RQ registry (bare or empty after a
		# heartbeat gap or a queue-Redis restart): the queued job is draining toward
		# them, so the sweep must not cancel and re-dispatch it.
		uid = self._mk_orphan()
		job = self._queued_job()
		with (
			patch("frappe.utils.background_jobs.get_job_status", return_value="queued"),
			patch("frappe.utils.background_jobs.get_job", return_value=job),
			patch("frappe.utils.background_jobs.get_workers", return_value=[]),
			patch.object(pump, "_fresh_heartbeat_count", return_value=2),
		):
			stale_scan._sweep_orphan_turns(now_datetime())
		job.cancel.assert_not_called()
		self.assertEqual(int(frappe.db.get_value(MSG, uid, "was_recovered") or 0), 0, "no heal")
		self.assertEqual(frappe.db.count(self.TURN, {"conversation": self.conv}), 0, "no replacement Turn")

	def test_a_truly_empty_registry_still_heals_a_queued_orphan(self):
		# Nobody heartbeats: the queue really is dead, the existing heal path stands.
		uid = self._mk_orphan()
		job = self._queued_job()
		with (
			patch("frappe.utils.background_jobs.get_job_status", return_value="queued"),
			patch("frappe.utils.background_jobs.get_job", return_value=job),
			patch("frappe.utils.background_jobs.get_workers", return_value=[]) as registry,
			patch.object(pump, "_fresh_heartbeat_count", return_value=0),
			patch.object(api, "_dispatch_turn", side_effect=lambda *a, **k: None),
		):
			stale_scan._sweep_orphan_turns(now_datetime())
		# One registry snapshot per sweep: the staleness check and the queue map
		# must not read Redis twice and disagree with each other.
		self.assertEqual(registry.call_count, 1)
		job.cancel.assert_called_once()
		self.assertEqual(
			int(frappe.db.get_value(MSG, uid, "was_recovered") or 0), 1, "heal consumed the strike"
		)

	def test_overload_does_not_consume_healing_strike(self):
		uid = self._mk_orphan()
		with (
			patch("frappe.utils.background_jobs.get_job_status", return_value=None),
			patch("frappe.utils.background_jobs.get_workers", return_value=[]),
			patch.object(admission, "MAX_QUEUE_DEPTH", 0),
			patch.object(api, "_dispatch_turn", side_effect=lambda *a, **k: None),
		):
			stale_scan._sweep_orphan_turns(now_datetime())
		self.assertEqual(int(frappe.db.get_value(MSG, uid, "was_recovered") or 0), 0, "strike reset")
		self.assertEqual(
			frappe.db.count(MSG, {"conversation": self.conv, "role": "assistant"}),
			0,
			"no second-strike error",
		)
		self.assertEqual(frappe.db.count(self.TURN, {"conversation": self.conv}), 0, "no replacement Turn")

	def test_rescan_heals_once_capacity_frees(self):
		uid = self._mk_orphan()
		with (
			patch("frappe.utils.background_jobs.get_job_status", return_value=None),
			patch("frappe.utils.background_jobs.get_workers", return_value=[]),
			patch.object(admission, "MAX_QUEUE_DEPTH", 0),
			patch.object(api, "_dispatch_turn", side_effect=lambda *a, **k: None),
		):
			stale_scan._sweep_orphan_turns(now_datetime())
		self.assertEqual(int(frappe.db.get_value(MSG, uid, "was_recovered") or 0), 0)
		# Depth frees: the next scan re-dispatches the orphan into a durable Turn and consumes the strike.
		with (
			patch("frappe.utils.background_jobs.get_job_status", return_value=None),
			patch("frappe.utils.background_jobs.get_workers", return_value=[]),
			patch.object(admission, "_max_inflight", return_value=4),
			patch.object(api, "_dispatch_turn", side_effect=lambda *a, **k: None),
		):
			stale_scan._sweep_orphan_turns(now_datetime())
		self.assertEqual(
			int(frappe.db.get_value(MSG, uid, "was_recovered") or 0), 1, "heal consumed the strike"
		)
		self.assertEqual(
			frappe.db.count(self.TURN, {"conversation": self.conv}), 1, "a replacement Turn exists"
		)
		self.assertEqual(
			frappe.db.count(MSG, {"conversation": self.conv, "role": "assistant"}), 0, "no error surfaced"
		)


class TestOrphanSweepLeavesTurnMachineSeedsAlone(_PumpTestCase):
	"""The orphan sweep heals a user message the turn machine never saw. A seed that
	already has a ``Jarvis Chat Turn`` is the machine's: a non-terminal Turn is the
	machine's to bound, and a terminal Turn is a verdict the sweep must not overturn.

	Runs with the Relay Pump ACTIVE (the production transport), which the class above
	pins OFF. Nothing about the RQ probe is patched: in pump mode no ``jarvis-turn::``
	job is ever created, so the sweep's liveness probe really does read None.

	The sweep is site-wide and this is a shared site, so every assertion here is on
	THIS test's seed and conversation, never on the sweep's return value."""

	def tearDown(self):
		# A foreign orphan left on the shared site by another module is swept too, and
		# while ``_pump_on`` is active its re-dispatch lands on this test's shard. Those
		# Turns are removed here. What is NOT undone: the strike (``was_recovered``) or
		# the error row the sweep wrote on that foreign message itself.
		frappe.db.delete(TURN, {"relay_target_id": self._target})
		frappe.db.commit()
		super().tearDown()

	@contextmanager
	def _pump_on(self):
		"""Pump active and configured; the wake is stubbed so no real hop is enqueued.
		The shard row itself already says ``pump`` (the base setUp stamps it), so the
		fenced decision inside ``accept_or_queue`` is the real one."""
		with (
			patch.object(pump, "pump_mode_active", return_value=True),
			patch.object(pump, "pump_configured", return_value=True),
			patch.object(pump, "ensure_pump", lambda *a, **k: {"enqueued": False}),
			patch.object(pump, "lpush_wake", lambda *a, **k: None),
			patch.object(admission, "relay_target_id", lambda conversation=None: self._target),
		):
			yield

	def _turns(self, seed: str) -> list[str]:
		return sorted(frappe.get_all(TURN, filters={"seed_message": seed}, pluck="state"))

	def _strikes(self, seed: str) -> int:
		return int(frappe.db.get_value(MSG, seed, "was_recovered") or 0)

	def _replies(self, conv: str) -> list:
		"""The ``error`` of every assistant row in the conversation, in transcript order."""
		return frappe.get_all(
			MSG, filters={"conversation": conv, "role": "assistant"}, order_by="seq asc", pluck="error"
		)

	def _sweep(self) -> None:
		with self._pump_on():
			stale_scan._sweep_orphan_turns(now_datetime())

	def _watchdog_tick(self) -> dict:
		"""One watchdog pass over THIS test's shard only (``pump.watchdog`` walks every
		shard on the site)."""
		deps = pump.PumpDeps()
		deps.enqueue_finalize = _Recorder()
		deps.enqueue_pump_job = _Recorder()
		summary = defaultdict(int)
		with self._pump_on():
			pump._watchdog_shard(self._target, deps, summary)
		return summary

	def _mk_aged_out_candidate(self, run_id: str) -> tuple[str, str]:
		"""A message whose Turn has waited, unreserved, past the queue's maximum age."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		stale = frappe.utils.add_to_date(None, seconds=-(ts.QUEUED_MAX_AGE_S + 60))
		self._mk_turn(conv, run_id, seed, "queued", enqueued_at=stale)
		_age(seed, seconds=ts.QUEUED_MAX_AGE_S + 60)
		return conv, seed

	def test_a_turn_waiting_in_the_queue_is_not_dispatched_a_second_time(self):
		# The reported duplicate, through the real send path for an internal turn:
		# the seed is committed, the pump accepts it as `queued`, and it waits (no
		# credit) long enough for the sweep's 180 s floor to pass.
		conv = self._mk_conv()
		# A conversation that already has its gateway session, so the enqueue does not
		# open a socket to mint one.
		frappe.db.set_value(CONV, conv, "session_key", "sk-orphan-sweep", update_modified=False)
		frappe.db.commit()
		with self._pump_on():
			out = api._enqueue_turn(conv, "post the month-end journal", origin="macro")
		seed = out["message_id"]
		self.assertEqual(self._turns(seed), ["queued"], "the pump accepted the turn and left it queued")
		_age(seed)

		self._sweep()

		self.assertEqual(self._turns(seed), ["queued"], "one Turn per seed: the queued turn is not an orphan")
		self.assertEqual(self._strikes(seed), 0, "no strike used")

	def test_the_message_is_answered_once(self):
		# What the user saw: the pump is single-flight per conversation, so the two
		# Turns on one seed ran one after the other and the same message was answered
		# twice (on an armed macro conversation, its writes happened twice).
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="post the month-end journal")
		with self._pump_on():
			admission.accept_or_queue(
				conversation=conv, run_id="orph_first", seed_message=seed, dispatch=None
			)
		_age(seed)

		self._sweep()

		double = self._double()
		deps = self._deps(double=double, prepare=self._prepare_stub(conv, double, "success"))
		ctx = self._make_ctx(deps)
		self._pump_until(ctx, lambda: all(s in ("finalizing", "done") for s in self._turns(seed)))
		self.assertEqual(len(self._replies(conv)), 1, "one seed, one reply")

	def test_a_turn_being_prepared_is_left_alone(self):
		# `preparing` is claimed BEFORE the assistant placeholder is attached, so a
		# prepare job that died in between leaves a live Turn with no reply row until
		# the watchdog reclaims it at 300 s: past the sweep's 180 s floor.
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		self._mk_turn(conv, "orph_prep", seed, "preparing", reserved=1, preparing_at=frappe.utils.now())
		_age(seed)

		self._sweep()

		self.assertEqual(self._turns(seed), ["preparing"])
		self.assertEqual(self._strikes(seed), 0, "no strike used")
		self.assertEqual(self._replies(conv), [], "no error over a live turn")

	def test_a_turn_past_preparing_is_left_alone(self):
		# `ready` and `dispatching` normally have their placeholder, which already
		# keeps the seed out of the sweep. Pinned WITHOUT one, so it is the Turn itself
		# that excludes the seed (a placeholder insert that was lost must not turn a
		# turn that is about to run, or is running, into a second Turn).
		for state in ("ready", "dispatching"):
			with self.subTest(state=state):
				conv = self._mk_conv()
				seed = self._mk_msg(conv)
				self._mk_turn(conv, f"orph_{state}", seed, state, reserved=1)
				_age(seed)

				self._sweep()

				self.assertEqual(self._turns(seed), [state])
				self.assertEqual(self._strikes(seed), 0, "no strike used")
				self.assertEqual(self._replies(conv), [], "no error over a live turn")

	def test_a_retried_message_with_several_turns_is_left_alone(self):
		# Retry reuses the seed, so one message can carry several Turns: the one that
		# ended and the retry that is now waiting. ANY Turn excludes the seed; there is
		# no "latest Turn" logic to get wrong. (A real retry also has the error row it
		# was pressed on; pinned without it so the Turn predicate is what decides.)
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		self._mk_turn(conv, "orph_retry_1", seed, "errored", done_at=frappe.utils.now())
		self._mk_turn(conv, "orph_retry_2", seed, "queued")
		_age(seed)

		self._sweep()

		self.assertEqual(self._turns(seed), ["errored", "queued"], "no third Turn")
		self.assertEqual(self._strikes(seed), 0, "no strike used")
		self.assertEqual(self._replies(conv), [], "no error over the waiting retry")

	def test_a_message_the_machine_never_saw_still_heals_once(self):
		# The case the sweep exists for: the seed was committed but the Turn insert
		# that follows it never ran. It is re-dispatched into ONE Turn, and from then
		# on it belongs to the machine: a later scan neither re-dispatches it again
		# nor writes the "never started" error over a turn that is merely waiting.
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		_age(seed)

		self._sweep()

		self.assertEqual(self._turns(seed), ["queued"], "healed into a Turn the pump owns")
		self.assertEqual(self._strikes(seed), 1, "strike used")
		self.assertEqual(
			frappe.db.get_value(TURN, {"seed_message": seed}, "turn_class"), "background", "as before"
		)

		self._sweep()

		self.assertEqual(self._turns(seed), ["queued"], "second scan: no second Turn")
		self.assertEqual(self._replies(conv), [], "second scan: nothing errored")

	def test_a_message_the_queue_aged_out_has_a_reply_row_saying_why(self):
		# The pump's age-out cancels the Turn and tells the user to try again. That
		# notice is a live event only, so the age-out also writes the durable cancel
		# marker: after a reload the message has a reply row carrying the reason. The
		# sweep then has two reasons to leave the seed alone (a Turn, and a reply row),
		# and neither runs it again nor writes its own error under the marker.
		conv, seed = self._mk_aged_out_candidate("orph_aged")

		summary = self._watchdog_tick()

		self.assertEqual(summary["aged_out"], 1)
		self.assertEqual(self._turns(seed), ["cancelled"])
		marker = frappe.get_all(
			MSG,
			filters={"conversation": conv, "role": "assistant"},
			fields=["seq", "error", "content", "streaming"],
		)
		self.assertEqual([m.error for m in marker], [pump._AGE_OUT_REASON], "one reply row, with the reason")
		self.assertGreater(marker[0].seq, frappe.db.get_value(MSG, seed, "seq"), "after the message")
		self.assertEqual((marker[0].content or "", int(marker[0].streaming or 0)), ("", 0))

		self._sweep()

		self.assertEqual(self._turns(seed), ["cancelled"], "no second Turn")
		self.assertEqual(self._strikes(seed), 0, "no strike used")
		self.assertEqual(self._replies(conv), [pump._AGE_OUT_REASON], "the marker is the only reply row")

	def test_an_age_out_whose_marker_write_failed_is_still_cancelled_and_not_run_again(self):
		# The marker is best-effort and is written AFTER the cancel is committed: a
		# failed write must not undo the cancel or stop the watchdog. What is left is a
		# terminal Turn with no reply row, which the sweep neither re-runs nor errors.
		conv, seed = self._mk_aged_out_candidate("orph_aged_nomark")

		with patch.object(admission, "_write_cancel_marker", side_effect=RuntimeError("marker write failed")):
			summary = self._watchdog_tick()

		self.assertEqual(summary["aged_out"], 1, "the cancel stands")
		self.assertEqual(self._turns(seed), ["cancelled"])
		self.assertEqual(self._replies(conv), [])

		self._sweep()

		self.assertEqual(self._turns(seed), ["cancelled"], "no second Turn")
		self.assertEqual(self._strikes(seed), 0, "no strike used")
		self.assertEqual(self._replies(conv), [])

	def _other_writer_appends(self, conv: str) -> int:
		"""Another writer commits one more row at the end of the conversation, from a
		real second connection, so this connection's open read view cannot see it.
		Returns the seq it took."""
		with other_connection() as other:
			seq = (
				other.sql(f"SELECT MAX(seq) FROM `tab{MSG}` WHERE conversation=%s", (conv,))[0][0] or 0
			) + 1
			now = frappe.utils.now()
			other.sql(
				f"""INSERT INTO `tab{MSG}` (name, creation, modified, owner, modified_by,
					conversation, seq, role, content) VALUES (%s, %s, %s, %s, %s, %s, %s, 'user', 'again')""",
				(frappe.generate_hash(length=10), now, now, TEST_USER, TEST_USER, conv, seq),
			)
			other.commit()
		return seq

	def _seqs(self, conv: str) -> list[tuple]:
		frappe.db.commit()  # this helper's own read must not be answered from an old view
		return [
			(m.seq, m.role)
			for m in frappe.get_all(
				MSG, filters={"conversation": conv}, fields=["seq", "role"], order_by="seq asc, creation asc"
			)
		]

	def test_the_age_out_marker_takes_a_seq_after_a_row_committed_during_the_tick(self):
		# The watchdog's live notice reads the conversation, which under REPEATABLE READ
		# opens the read view. A row another writer commits after that is invisible to a
		# later plain MAX(seq), so the marker used to take that row's seq. The marker
		# must allocate from a fresh view.
		conv, _seed = self._mk_aged_out_candidate("orph_aged_seq")
		real_publish = pump._publish_cancelled
		taken = []

		def publish_then_a_concurrent_write(r, reason):
			real_publish(r, reason)
			taken.append(self._other_writer_appends(conv))

		with patch.object(pump, "_publish_cancelled", publish_then_a_concurrent_write):
			summary = self._watchdog_tick()

		self.assertEqual(summary["aged_out"], 1)
		self.assertEqual(taken, [2], "the other writer took the next seq")
		self.assertEqual(
			self._seqs(conv), [(1, "user"), (2, "user"), (3, "assistant")], "no two rows share a seq"
		)

	def test_the_cancel_marker_does_not_allocate_its_seq_from_the_callers_read_view(self):
		# The helper itself, as its other two callers reach it (user cancel, Phase-0
		# age-out): after a commit and then some read on this connection.
		conv = self._mk_conv()
		self._mk_msg(conv)
		open_read_view()
		self.assertEqual(self._other_writer_appends(conv), 2)

		admission._write_cancel_marker(conv, admission.USER_CANCEL_REASON)

		self.assertEqual(
			self._seqs(conv), [(1, "user"), (2, "user"), (3, "assistant")], "no two rows share a seq"
		)

	def test_a_cancel_whose_marker_was_lost_is_not_run_again(self):
		# A user cancel normally leaves an assistant marker row, which already keeps
		# the seed out of the sweep. The marker write is best-effort, though; without
		# it the user's own cancel must still stand.
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		self._mk_turn(conv, "orph_cxl", seed, "cancelled", cancel_requested=1, done_at=frappe.utils.now())
		_age(seed)

		self._sweep()

		self.assertEqual(self._turns(seed), ["cancelled"])
		self.assertEqual(self._strikes(seed), 0, "no strike used")
		self.assertEqual(self._replies(conv), [])


class TestOrphanSweepLegacyTransport(_PumpTestCase):
	"""Turn machine OFF (pump not active, not configured, not draining; Phase-0
	admission off): there are no Turn rows, so the sweep behaves exactly as it did:
	first strike re-dispatches the legacy job, second strike surfaces the error."""

	def setUp(self):
		super().setUp()
		self._patches = [
			patch.dict(frappe.local.conf, {"jarvis_phase0_admission_enabled": 0}),
			patch.object(pump, "pump_mode_active", return_value=False),
			patch.object(pump, "pump_configured", return_value=False),
			patch.object(pump, "pump_draining", return_value=False),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()
		super().tearDown()

	def test_first_strike_redispatches_and_second_strike_errors(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		_age(seed)
		self.assertFalse(admission.turn_machine_enabled())

		with (
			patch.object(api, "_dispatch_turn", return_value=None) as dispatch,
			patch("jarvis.chat.stale_scan.publish_to_user") as pub,
		):
			stale_scan._sweep_orphan_turns(now_datetime())
			ours = [c for c in dispatch.call_args_list if c.args[0]["message_id"] == seed]
			self.assertEqual(len(ours), 1, "first strike: one legacy re-dispatch")
			self.assertEqual(ours[0].kwargs, {"interactive": False, "cutover_gate": True})
			self.assertEqual(int(frappe.db.get_value(MSG, seed, "was_recovered") or 0), 1)
			self.assertEqual(frappe.db.count(TURN, {"seed_message": seed}), 0, "legacy makes no Turn")
			self.assertEqual(
				frappe.db.count(MSG, {"conversation": conv, "role": "assistant"}), 0, "no error yet"
			)

			stale_scan._sweep_orphan_turns(now_datetime())
			ours = [c for c in dispatch.call_args_list if c.args[0]["message_id"] == seed]
			self.assertEqual(len(ours), 1, "second strike does not re-dispatch")
		err = frappe.get_all(MSG, filters={"conversation": conv, "role": "assistant"}, pluck="error")
		self.assertEqual(err, [stale_scan._ORPHAN_ERR])
		self.assertTrue(any(c.args[1].get("conversation_id") == conv for c in pub.call_args_list))
