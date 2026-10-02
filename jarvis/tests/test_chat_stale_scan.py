"""Tests for jarvis.chat.stale_scan.scan_and_mark_errored.

Runs as the fixture user ``TEST_USER`` so it never wipes Administrator's
chat history on a dev site.
"""

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
from jarvis.tests.test_chat_api import (
	TEST_USER,
	_cleanup_user_conversations,
	_ensure_test_user,
)
from jarvis.tests.test_pump import _PumpTestCase

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"


def setUpModule():
	# The pump-mode class below drives a real pump slice, which reads the runtime profile.
	install_synthetic_runtime_profile()


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
		from jarvis.chat import pump

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
		old = now_datetime() - timedelta(seconds=age_seconds)
		frappe.db.sql("UPDATE `tabJarvis Chat Message` SET creation=%s WHERE name=%s", (old, doc.name))
		frappe.db.commit()
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
		from jarvis.chat import pump, stale_scan

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
		from jarvis.chat import api, pump, stale_scan

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
		from jarvis.chat import admission, api, stale_scan

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
		from jarvis.chat import admission, api, stale_scan

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
	already has a ``Jarvis Chat Turn`` is the machine's: its watchdog bounds every
	non-terminal state, and a terminal Turn is a verdict the sweep must not overturn.

	Runs with the Relay Pump ACTIVE (the production transport), which the class above
	pins OFF. Nothing about the RQ probe is patched: in pump mode no ``jarvis-turn::``
	job is ever created, so the sweep's liveness probe really does read None."""

	def tearDown(self):
		# The sweep is site-wide, so a foreign orphan left on the shared bench could
		# have been re-dispatched onto this test's shard.
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

	def _age(self, message: str, seconds: int = 300) -> None:
		old = now_datetime() - timedelta(seconds=seconds)
		frappe.db.sql(f"UPDATE `tab{MSG}` SET creation=%s WHERE name=%s", (old, message))
		frappe.db.commit()

	def _turns(self, seed: str) -> list[str]:
		return frappe.get_all(TURN, filters={"seed_message": seed}, pluck="state")

	def _sweep(self) -> int:
		with self._pump_on():
			return stale_scan._sweep_orphan_turns(now_datetime())

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
		self._age(seed)

		self._sweep()

		self.assertEqual(self._turns(seed), ["queued"], "one Turn per seed: the queued turn is not an orphan")
		self.assertEqual(int(frappe.db.get_value(MSG, seed, "was_recovered") or 0), 0, "no strike used")

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
		self._age(seed)

		self._sweep()

		double = self._double()
		deps = self._deps(double=double, prepare=self._prepare_stub(conv, double, "success"))
		ctx = self._make_ctx(deps)
		self._pump_until(ctx, lambda: all(s in ("finalizing", "done") for s in self._turns(seed)))
		replies = frappe.db.count(MSG, {"conversation": conv, "role": "assistant"})
		self.assertEqual(replies, 1, "one seed, one reply")

	def test_a_turn_being_prepared_is_left_alone(self):
		# `preparing` is claimed BEFORE the assistant placeholder is attached, so a
		# prepare job that died in between leaves a live Turn with no reply row until
		# the watchdog reclaims it at 300 s: past the sweep's 180 s floor.
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		self._mk_turn(conv, "orph_prep", seed, "preparing", reserved=1, preparing_at=frappe.utils.now())
		self._age(seed)

		self._sweep()

		self.assertEqual(self._turns(seed), ["preparing"])
		self.assertEqual(int(frappe.db.get_value(MSG, seed, "was_recovered") or 0), 0, "no strike used")

	def test_a_message_the_machine_never_saw_still_heals_once(self):
		# The case the sweep exists for: the seed was committed but the Turn insert
		# that follows it never ran. It is re-dispatched into ONE Turn, and from then
		# on it belongs to the machine: a later scan neither re-dispatches it again
		# nor writes the "never started" error over a turn that is merely waiting.
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		self._age(seed)

		self._sweep()

		self.assertEqual(self._turns(seed), ["queued"], "healed into a Turn the pump owns")
		self.assertEqual(int(frappe.db.get_value(MSG, seed, "was_recovered") or 0), 1, "strike used")
		self.assertEqual(
			frappe.db.get_value(TURN, {"seed_message": seed}, "turn_class"), "background", "as before"
		)

		self.assertEqual(self._sweep(), 0, "second scan: nothing errored")
		self.assertEqual(self._turns(seed), ["queued"])
		self.assertEqual(frappe.db.count(MSG, {"conversation": conv, "role": "assistant"}), 0)

	def test_a_turn_the_queue_aged_out_is_not_run_again(self):
		# The pump's age-out cancels the Turn and tells the user to try again, but
		# writes no transcript row, so the seed still has no reply after it. That is a
		# verdict, not a lost dispatch: the message is neither re-run nor errored.
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		stale = frappe.utils.add_to_date(None, seconds=-(ts.QUEUED_MAX_AGE_S + 60))
		self._mk_turn(conv, "orph_aged", seed, "queued", enqueued_at=stale)
		self.assertTrue(ts.cancel_queued_max_age("orph_aged", 1, pump._AGE_OUT_REASON))
		frappe.db.commit()
		self._age(seed, seconds=ts.QUEUED_MAX_AGE_S + 60)

		self.assertEqual(self._sweep(), 0)

		self.assertEqual(self._turns(seed), ["cancelled"], "no second Turn")
		self.assertEqual(int(frappe.db.get_value(MSG, seed, "was_recovered") or 0), 0, "no strike used")
		self.assertEqual(frappe.db.count(MSG, {"conversation": conv, "role": "assistant"}), 0)

	def test_a_cancel_whose_marker_was_lost_is_not_run_again(self):
		# A user cancel normally leaves an assistant marker row, which already keeps
		# the seed out of the sweep. The marker write is best-effort, though; without
		# it the user's own cancel must still stand.
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		self._mk_turn(conv, "orph_cxl", seed, "cancelled", cancel_requested=1, done_at=frappe.utils.now())
		self._age(seed)

		self.assertEqual(self._sweep(), 0)

		self.assertEqual(self._turns(seed), ["cancelled"])
		self.assertEqual(frappe.db.count(MSG, {"conversation": conv, "role": "assistant"}), 0)


class TestOrphanSweepLegacyTransport(_PumpTestCase):
	"""Turn machine OFF (pump kill switch on, Phase-0 admission off): there are no
	Turn rows, so the sweep behaves exactly as it did: first strike re-dispatches the
	legacy job, second strike surfaces the error."""

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
		old = now_datetime() - timedelta(seconds=300)
		frappe.db.sql(f"UPDATE `tab{MSG}` SET creation=%s WHERE name=%s", (old, seed))
		frappe.db.commit()
		self.assertFalse(admission.turn_machine_enabled())

		with (
			patch.object(api, "_dispatch_turn", return_value=None) as dispatch,
			patch("jarvis.chat.stale_scan.publish_to_user") as pub,
		):
			self.assertEqual(stale_scan._sweep_orphan_turns(now_datetime()), 0)
			ours = [c for c in dispatch.call_args_list if c.args[0]["message_id"] == seed]
			self.assertEqual(len(ours), 1, "first strike: one legacy re-dispatch")
			self.assertEqual(ours[0].kwargs, {"interactive": False, "cutover_gate": True})
			self.assertEqual(int(frappe.db.get_value(MSG, seed, "was_recovered") or 0), 1)
			self.assertEqual(frappe.db.count(TURN, {"seed_message": seed}), 0, "legacy makes no Turn")

			self.assertGreaterEqual(stale_scan._sweep_orphan_turns(now_datetime()), 1)
			ours = [c for c in dispatch.call_args_list if c.args[0]["message_id"] == seed]
			self.assertEqual(len(ours), 1, "second strike does not re-dispatch")
		err = frappe.get_all(MSG, filters={"conversation": conv, "role": "assistant"}, pluck="error")
		self.assertEqual(err, [stale_scan._ORPHAN_ERR])
		self.assertTrue(any(c.args[1].get("conversation_id") == conv for c in pub.call_args_list))
