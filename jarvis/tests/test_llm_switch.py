"""Tests for jarvis.chat.llm_switch (jarvis#1425 follow-up, Task 4).

Holds a proxy<->direct switch until no reply is in flight (or a 5-minute cap),
tells every open chat, then applies. Covers: begin/try_apply/end lifecycle,
idempotent retarget of an already-applied switch, the cap, the reconcile poller
hook, the maintenance-hold surfacing (boot_payload + persist_from_connection
still reading only the operator mirror), the promote_next hold, and the
settle_turn / settle_conversation_dispatching triggers.

``jarvis.chat.llm_switch._inflight`` (not ``admission._shard_inflight``) is the
patch seam here: admission's own 180s freshness window under-counts a legacy
streaming reply mid a long tool call (traced against turn_handler's assistant
batcher - see llm_switch.py's module comment), so llm_switch widens that leg to
900s via its own ``_inflight()`` wrapper instead of reusing ``_shard_inflight``
directly. See task-4-report.md for the trace.

``frappe.flags.in_test`` is on for the whole suite (FrappeTestCase), so
``begin()``/``_enqueue`` always take the inline branch - no ``frappe.db.commit()``
is needed to observe their effects, mirroring how the existing
``_enqueue_pool_sync`` / ``_enqueue_handover`` tests rely on the same flag.
"""

import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import maintenance_notice
from jarvis.chat import admission, llm_switch


class _LlmSwitchTestCase(FrappeTestCase):
	def setUp(self):
		frappe.cache().delete_value(llm_switch.KEY)
		maintenance_notice.persist({"active": False})
		self._orig_status = frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "last_sync_status")

	def tearDown(self):
		frappe.cache().delete_value(llm_switch.KEY)
		maintenance_notice.persist({"active": False})
		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": self._orig_status or ""}, update_modified=False
		)


class TestBeginAndTryApply(_LlmSwitchTestCase):
	def test_begin_stores_record_broadcasts_and_applies_when_idle(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime") as mock_pub,
		):
			llm_switch.begin("jarvis.tests.fake_job", foo="bar")

		mock_pub.assert_called_once_with(llm_switch.EVENT, {"state": "switching"})
		mock_enqueue.assert_called_once()
		args, kwargs = mock_enqueue.call_args
		self.assertEqual(args[0], "jarvis.tests.fake_job")
		self.assertEqual(kwargs["foo"], "bar")
		self.assertEqual(kwargs["queue"], "long")
		self.assertTrue(kwargs["now"])
		self.assertFalse(kwargs["enqueue_after_commit"])
		rec = llm_switch.status()
		self.assertTrue(rec["applied"])

	def test_begin_waits_while_replies_in_flight(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=2),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.fake_job")
			mock_enqueue.assert_not_called()
			self.assertFalse(llm_switch.status()["applied"])

			with patch.object(llm_switch, "_inflight", return_value=0):
				applied = llm_switch.try_apply()

			self.assertTrue(applied)
			mock_enqueue.assert_called_once()
			self.assertTrue(llm_switch.status()["applied"])

	def test_cap_applies_after_deadline(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=1),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.fake_job")
			mock_enqueue.assert_not_called()

			rec = llm_switch.status()
			rec["deadline"] = time.time() - 1
			frappe.cache().set_value(llm_switch.KEY, rec, expires_in_sec=llm_switch.TTL_S)

			# inflight is STILL 1 (nobody drained) - only the past deadline should
			# force the apply through.
			applied = llm_switch.try_apply()

		self.assertTrue(applied)
		mock_enqueue.assert_called_once()

	def test_try_apply_runs_once(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.fake_job")
			mock_enqueue.assert_called_once()

			second = llm_switch.try_apply()

		self.assertFalse(second)
		mock_enqueue.assert_called_once()  # still just the one from begin()

	def test_begin_while_active_retargets_and_applies_now(self):
		"""Review Focus 3: an already-APPLIED switch retargeted to a new job gets no
		second banner and applies the new job immediately, even with replies still
		in flight - the container is already restarting for the old job, so there
		is nothing left to drain."""
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime") as mock_pub,
		):
			llm_switch.begin("jarvis.tests.job_a")
			mock_pub.assert_called_once()
			mock_enqueue.assert_called_once()
			mock_pub.reset_mock()
			mock_enqueue.reset_mock()

			with patch.object(llm_switch, "_inflight", return_value=5):
				llm_switch.begin("jarvis.tests.job_b", other="kw")

		mock_pub.assert_not_called()  # no second "switching" banner
		mock_enqueue.assert_called_once()
		args, kwargs = mock_enqueue.call_args
		self.assertEqual(args[0], "jarvis.tests.job_b")
		self.assertEqual(kwargs["other"], "kw")
		self.assertTrue(llm_switch.status()["applied"])


class TestEnd(_LlmSwitchTestCase):
	def test_end_clears_and_broadcasts_and_promotes(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue"),
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.fake_job")
		self.assertIsNotNone(llm_switch.status())

		with (
			patch("frappe.publish_realtime") as mock_pub,
			patch.object(admission, "promote_next") as mock_promote,
		):
			llm_switch.end("ok: moved")

		self.assertIsNone(llm_switch.status())
		mock_pub.assert_called_once_with(llm_switch.EVENT, {"state": "done", "outcome": "ok: moved"})
		mock_promote.assert_called_once()

	def test_end_is_a_noop_without_an_active_record(self):
		with (
			patch("frappe.publish_realtime") as mock_pub,
			patch.object(admission, "promote_next") as mock_promote,
		):
			llm_switch.end("ok: moved")

		mock_pub.assert_not_called()
		mock_promote.assert_not_called()


class TestReconcile(_LlmSwitchTestCase):
	def _applied_record(self):
		rec = {
			"started_at": time.time(),
			"deadline": time.time() + llm_switch.CAP_S,
			"job": "jarvis.tests.fake_job",
			"job_kwargs": {},
			"applied": True,
		}
		frappe.cache().set_value(llm_switch.KEY, rec, expires_in_sec=llm_switch.TTL_S)

	def test_status_self_clears_after_terminal_sync_status(self):
		for status in ("ok: applied", "failed: fleet unreachable"):
			with self.subTest(status=status):
				self._applied_record()
				frappe.db.set_single_value(
					"Jarvis Settings", {"last_sync_status": status}, update_modified=False
				)
				with patch.object(llm_switch, "end") as mock_end:
					llm_switch.reconcile()
				mock_end.assert_called_once_with(status)

	def test_reconcile_leaves_pending_switch_alone(self):
		self._applied_record()
		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": "pending: provisioning container"}, update_modified=False
		)
		with patch.object(llm_switch, "end") as mock_end:
			llm_switch.reconcile()
		mock_end.assert_not_called()
		self.assertIsNotNone(llm_switch.status())


class TestBootPayload(_LlmSwitchTestCase):
	def test_boot_payload_reports_switch(self):
		with patch.object(llm_switch, "is_active", return_value=True):
			payload = maintenance_notice.boot_payload()
		self.assertEqual(payload, {"active": True, "message": llm_switch.MESSAGE})

	def test_operator_mirror_wins_over_switch(self):
		maintenance_notice.persist({"active": True, "message": "up"})
		with patch.object(llm_switch, "is_active", return_value=True):
			payload = maintenance_notice.boot_payload()
		self.assertEqual(payload, {"active": True, "message": "up"})

	def test_boot_payload_inactive_when_neither_holds(self):
		with patch.object(llm_switch, "is_active", return_value=False):
			payload = maintenance_notice.boot_payload()
		self.assertEqual(payload, {"active": False, "message": ""})

	def test_persist_from_connection_reads_mirror_not_switch(self):
		"""persist_from_connection must read only the operator mirror - a switch is
		bench-local and the control plane has no notion of it. Patching
		boot_payload to explode proves the old-CP branch no longer calls it."""
		with patch(
			"jarvis.maintenance_notice.boot_payload", side_effect=AssertionError("must not call boot_payload")
		):
			maintenance_notice.persist_from_connection({})  # no marker -> old-CP clear branch
		self.assertFalse(maintenance_notice._mirror_payload()["active"])


class TestInflightWidenedWindow(FrappeTestCase):
	"""Direct coverage of the documented deviation from the brief: llm_switch does
	NOT reuse admission._shard_inflight as-is (it under-counts a legacy streaming
	reply mid a long tool call - see llm_switch.py's module comment and
	task-4-report.md). It calls the two admission primitives itself and widens the
	legacy leg's freshness cutoff to 900s instead of admission's own 180s."""

	def test_inflight_widens_the_legacy_freshness_window(self):
		with (
			patch.object(admission, "_turn_dispatching_count", return_value=0) as mock_turn,
			patch.object(admission, "_legacy_streaming_count", return_value=0) as mock_legacy,
		):
			llm_switch._inflight()

		mock_turn.assert_called_once_with(admission.DEFAULT_RELAY_TARGET)
		mock_legacy.assert_called_once()
		args, kwargs = mock_legacy.call_args
		self.assertEqual(args[0], admission.DEFAULT_RELAY_TARGET)
		self.assertIn("fresh_cutoff", kwargs)
		# Wider window -> an EARLIER cutoff than admission's own 180s default.
		self.assertLess(
			frappe.utils.get_datetime(kwargs["fresh_cutoff"]),
			frappe.utils.get_datetime(admission._fresh_cutoff()),
		)


class TestPromoteNextHold(FrappeTestCase):
	def test_promote_next_holds_during_switch(self):
		with patch.object(llm_switch, "is_active", return_value=True):
			result = admission.promote_next()
		self.assertEqual(result, 0)


class TestSettleTriggersTryApply(FrappeTestCase):
	def test_settle_turn_triggers_try_apply(self):
		with (
			patch.object(admission, "admission_enabled", return_value=False),
			patch.object(llm_switch, "try_apply") as mock_try_apply,
		):
			admission.settle_turn("no-such-run-id", "done")
		mock_try_apply.assert_called_once()

	def test_settle_conversation_dispatching_triggers_try_apply(self):
		with (
			patch.object(admission, "admission_enabled", return_value=False),
			patch.object(llm_switch, "try_apply") as mock_try_apply,
		):
			admission.settle_conversation_dispatching("no-such-conversation", "done")
		mock_try_apply.assert_called_once()
