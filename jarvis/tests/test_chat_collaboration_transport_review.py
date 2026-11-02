"""Regressions for the independent intermediate transport review."""

import uuid
from collections import defaultdict
from unittest.mock import Mock, patch

import frappe

from jarvis.chat import admission, pump
from jarvis.chat import collaboration as c
from jarvis.chat import turn_state as ts
from jarvis.chat.collaboration_bindings import insert_message, stamp
from jarvis.tests._share_helpers import OWNER, as_user
from jarvis.tests.test_chat_collaboration_rq import RQCase, setUpModule


class TestCollaborationTransportReview(RQCase):
	def test_definite_runtime_drift_survives_the_recovery_error_rollback(self):
		from jarvis.chat.collaboration_recovery import sweep

		self.gateway.fail_ack = True
		self.drive()
		self.gateway.entry["inheritedToolAllow"] = ["*"]
		sweep(conversation=self.conversation)
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Uncertain")
		self.assertEqual(len(self.gateway.sends), 1)

	def test_deadline_park_preserves_uncertainty_without_writing_a_wrong_reply(self):
		from jarvis.chat.collaboration_recovery import sweep

		self.gateway.fail_ack = True
		self.drive()
		seed_text = frappe.db.get_value(ts.MSG, self.turn.seed_message, "content")
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{
				"state": "streaming",
				"assistant_message": self.turn.seed_message,
				"deadline_at": frappe.utils.add_to_date(None, seconds=-1),
			},
		)
		frappe.db.commit()
		sweep(conversation=self.conversation)
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "recovering")
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Uncertain")
		self.assertEqual(frappe.db.get_value(ts.MSG, self.turn.seed_message, "content"), seed_text)

	def test_stop_during_recovery_aborts_the_actual_bound_run(self):
		from jarvis.chat import collaboration_api as api

		self.gateway.fail_ack = True
		self.drive()
		with as_user(self.bound.actor):
			result = api.stop_run(self.conversation, self.turn.name)
		self.assertTrue(result["ok"])
		self.assertEqual(self.gateway.aborts, [(self.bound.session_key, self.turn.name)])
		self.assertEqual(result["state"], "stopping")
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "recovering")
		self.assertTrue(frappe.db.get_value(ts.TURN, self.turn.name, "cancel_requested"))
		from jarvis.chat.collaboration_recovery import sweep
		from jarvis.tests._gateway_fixtures import transcript_message

		message = transcript_message("assistant", "Useful stopped answer", seq=1)
		message["stopReason"] = "aborted"
		self.gateway.history = [message]
		sweep(conversation=self.conversation)
		row = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertEqual(row.state, "cancelled")
		self.assertEqual(
			frappe.db.get_value(ts.MSG, row.assistant_message, "content"), "Useful stopped answer"
		)

	def test_mode_change_does_not_wait_for_transport_independent_finalizing(self):
		frappe.db.set_value(ts.TURN, self.turn.name, "state", "finalizing")
		frappe.db.commit()
		pump.set_transport_mode(self.target, "pump")
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(ts.PUMP, self.target, "transport_mode"), "pump")
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "collaboration_executor"), "rq")

	def test_owner_actor_can_stop_while_collaboration_rollout_is_off(self):
		from jarvis.chat import collaboration_api as api

		frappe.db.set_value(ts.TURN, self.turn.name, "state", "cancelled")
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex, actor=OWNER))
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{
				"relay_target_id": self.target,
				"collaboration_executor": "rq",
				"reserved": 1,
				"collaboration_send_started_at": None,
			},
		)
		frappe.db.commit()
		stops = []

		def stop():
			with patch.dict(frappe.conf, {"chat_collaboration_enabled": 0}), as_user(OWNER):
				stops.append(api.stop_run(self.conversation, self.turn.name))

		self.gateway.before_reply = stop
		self.drive()
		self.assertEqual(len(stops), 1)
		self.assertTrue(stops[0]["ok"])
		self.assertEqual(self.gateway.aborts, [(self.bound.session_key, self.turn.name)])
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")

	def test_shared_rq_completion_immediately_wakes_the_waiting_legacy_queue(self):
		with patch.object(admission, "promote_next", return_value=0) as promote:
			self.drive()
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "finalizing")
		promote.assert_called_once_with(self.target)

	def test_old_pump_watchdog_cannot_age_out_shared_pump_rows_or_publish_as_owner(self):
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{
				"state": "queued",
				"reserved": 0,
				"collaboration_executor": "pump",
				"enqueued_at": frappe.utils.add_to_date(None, seconds=-ts.QUEUED_MAX_AGE_S - 1),
			},
		)
		frappe.db.commit()
		with (
			patch.object(pump, "_publish_cancelled") as publish,
			patch.object(pump, "_write_age_out_marker") as marker,
		):
			pump._watchdog_shard(self.target, Mock(), defaultdict(int))
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "queued")
		publish.assert_not_called()
		marker.assert_not_called()

	def test_unscoped_recovery_reaches_expired_work_past_ten_fresh_queued_rows(self):
		from jarvis.chat.collaboration_recovery import sweep

		for _ in range(10):
			run = uuid.uuid4().hex
			seed = insert_message(
				self.bound, {"role": "user", "content": "future request", "hidden": 1}, run_id=run
			)
			doc = frappe.get_doc(
				{
					"doctype": ts.TURN,
					"run_id": run,
					"conversation": self.conversation,
					"relay_target_id": self.target,
					"seed_message": seed.name,
					"state": "queued",
					"enqueued_at": frappe.utils.add_to_date(None, minutes=-2),
					"collaboration_executor": "rq",
				}
			)
			stamp(doc, self.bound)
			doc.insert(ignore_permissions=True)
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{
				"enqueued_at": frappe.utils.add_to_date(None, minutes=-1),
				"reservation_expires_at": frappe.utils.add_to_date(None, seconds=-1),
			},
		)
		frappe.db.commit()
		with patch.dict(frappe.conf, {admission.FLAG: 0}):
			sweep(limit=1)
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")
		self.assertEqual(self.gateway.sends, [])
