"""Shared RQ claims survive unrelated pump and enqueue lifecycle mutations."""

import uuid
from unittest.mock import patch

import frappe

from jarvis.chat import admission, pump
from jarvis.chat import turn_state as ts
from jarvis.tests._collaboration_committed import CommittedCollaborationCase


class TestCollaborationOwnership(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		self.target = "collaboration-ownership-" + uuid.uuid4().hex[:12]
		ts._ensure_control_row(self.target)
		frappe.db.set_value(ts.PUMP, self.target, "transport_mode", "pump")
		frappe.db.set_value(ts.TURN, self.turn.name, {"relay_target_id": self.target, "reserved": 1})
		if frappe.db.has_column(ts.TURN, "collaboration_executor"):
			frappe.db.set_value(ts.TURN, self.turn.name, "collaboration_executor", "rq")
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.delete(ts.PUMP, {"name": self.target})
		frappe.db.commit()
		super().tearDown()

	def current(self):
		return frappe.db.get_value(
			ts.TURN, self.turn.name, ["state", "version", "pump_epoch", "reserved"], as_dict=True
		)

	def test_pump_takeover_does_not_restamp_or_mutate_rq_turn(self):
		before = self.current()
		won, epoch = ts.lease_acquire(self.target, "unrelated-pump")
		self.assertTrue(won)
		self.assertGreater(epoch, 0)
		self.assertEqual(self.current(), before)

	def test_wedge_takeover_does_not_restamp_or_mutate_rq_turn(self):
		frappe.db.set_value(
			ts.PUMP,
			self.target,
			{
				"pump_epoch": 4,
				"lease_expires_at": frappe.utils.add_to_date(None, seconds=60),
				"loop_heartbeat_ts": frappe.utils.add_to_date(None, seconds=-600),
			},
		)
		frappe.db.commit()
		before = self.current()
		self.assertTrue(ts.force_expire_wedged(self.target, 4, 60))
		self.assertEqual(self.current(), before)

	def test_idle_pump_can_release_but_rq_still_consumes_capacity(self):
		frappe.db.set_value(ts.PUMP, self.target, "pump_epoch", 4)
		frappe.db.commit()
		# The historical legacy signal is site-wide, including unrelated
		# fixture conversations. This target still owns exactly one RQ credit.
		self.assertEqual(admission._turn_dispatching_count(self.target), 1)
		before = admission._shard_inflight(self.target)
		self.assertGreaterEqual(before, 1)
		self.assertTrue(ts.lease_release_if_idle(self.target, 4))
		self.assertEqual(admission._turn_dispatching_count(self.target), 1)
		self.assertEqual(admission._shard_inflight(self.target), before)

	def test_transport_change_refuses_even_when_rq_job_disappeared(self):
		before = self.current()
		with self.assertRaises(frappe.ValidationError):
			pump.set_transport_mode(self.target, "legacy")
		frappe.db.rollback()
		self.assertEqual(self.current(), before)
		self.assertEqual(frappe.db.get_value(ts.PUMP, self.target, "transport_mode"), "pump")

	def test_same_transport_is_idempotent_with_shared_work(self):
		pump.set_transport_mode(self.target, "pump")
		self.assertEqual(frappe.db.get_value(ts.PUMP, self.target, "transport_mode"), "pump")

	def test_enqueue_error_cannot_requeue_a_preparing_or_sent_shared_request(self):
		before = self.current()
		admission._compensate_failed_dispatch(self.turn.name, self.target, version=int(before.version))
		self.assertEqual(self.current(), before)
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{"collaboration_send_started_at": None, "preparing_at": frappe.utils.now_datetime()},
		)
		frappe.db.commit()
		before = self.current()
		admission._compensate_failed_dispatch(self.turn.name, self.target, version=int(before.version))
		self.assertEqual(self.current(), before)

	def test_rq_publish_uses_actor_fence_without_a_pump_lease_or_epoch_payload(self):
		frappe.db.set_value(ts.PUMP, self.target, "pump_epoch", 19)
		frappe.db.commit()
		# Fence routing here; the realtime suite separately exercises the queued,
		# committed projection fan-out rather than publishing producer payloads.
		with patch("jarvis.chat.turn_state.publish_to_user") as publish:
			ts.publish_fenced(
				self.bound.actor,
				"run:start",
				conversation_id=self.conversation,
				run_id=self.turn.name,
				pump_epoch=0,
				relay_target_id=self.target,
			)
		publish.assert_called_once()
		self.assertNotIn("pump_epoch", publish.call_args.args[1])
		self.assertEqual(publish.call_args.args[0], self.bound.actor)

	def test_old_phase_zero_settle_does_not_finish_shared_request(self):
		before = self.current()
		with patch.dict(frappe.conf, {admission.FLAG: 1}), patch.object(admission, "_try_llm_switch_apply"):
			admission.settle_turn(self.turn.name, "done")
		self.assertEqual(self.current(), before)

	def test_old_phase_zero_cancel_does_not_change_shared_version(self):
		before = self.current()
		with patch.dict(frappe.conf, {admission.FLAG: 1}):
			admission.mark_cancel_requested(self.conversation, self.turn.name)
		self.assertEqual(self.current(), before)

	def _queued_for_legacy(self):
		frappe.db.set_value(ts.PUMP, self.target, "transport_mode", "legacy")
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{
				"state": "queued",
				"reserved": 0,
				"enqueued_at": frappe.utils.now_datetime(),
				"dispatching_at": None,
				"collaboration_send_started_at": None,
			},
		)
		frappe.db.commit()

	def test_flag_off_phase_zero_never_promotes_shared_requests(self):
		self._queued_for_legacy()
		with (
			patch.dict(frappe.conf, {admission.FLAG: 1, "chat_collaboration_enabled": 0}),
			patch.object(admission, "_dispatch_promoted") as dispatch,
			patch.object(admission, "_legacy_streaming_count", return_value=0),
		):
			admission.promote_next(self.target)
		self.assertEqual(self.current().state, "queued")
		dispatch.assert_not_called()

	def test_revoked_request_is_cancelled_at_dequeue_without_worker_dispatch(self):
		self._queued_for_legacy()
		frappe.db.delete("Jarvis Conversation Share", {"name": self.share})
		frappe.db.commit()
		with (
			patch.dict(frappe.conf, {admission.FLAG: 1}),
			patch.object(admission, "_dispatch_promoted") as dispatch,
			patch.object(admission, "_legacy_streaming_count", return_value=0),
		):
			admission.promote_next(self.target)
		self.assertEqual(self.current().state, "cancelled")
		self.assertFalse(self.current().reserved)
		dispatch.assert_not_called()
