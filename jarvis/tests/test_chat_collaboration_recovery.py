"""Lost jobs and unknown delivery recover without owner sessions or replay."""

from unittest.mock import patch

import frappe

from jarvis.chat import admission
from jarvis.chat import collaboration as c
from jarvis.chat import turn_state as ts
from jarvis.tests._gateway_fixtures import transcript_message
from jarvis.tests.race_harness import as_job
from jarvis.tests.test_chat_collaboration_rq import RQCase, setUpModule


class TestCollaborationRecovery(RQCase):
	def sweep(self):
		from jarvis.chat.collaboration_recovery import sweep

		with as_job():
			return sweep(conversation=self.conversation)

	def current(self):
		return frappe.get_doc(ts.TURN, self.turn.name)

	def test_lost_unclaimed_rq_job_expires_without_any_model_or_unbound_message(self):
		frappe.db.set_value(
			ts.TURN, self.turn.name, "reservation_expires_at", frappe.utils.add_to_date(None, seconds=-1)
		)
		frappe.db.commit()
		before = frappe.db.count(ts.MSG, {"conversation": self.conversation})
		self.sweep()
		self.assertEqual(self.current().state, "cancelled")
		self.assertFalse(self.current().reserved)
		self.assertEqual(self.gateway.calls, [])
		self.assertEqual(frappe.db.count(ts.MSG, {"conversation": self.conversation}), before)

	def test_dead_unsent_preparation_drains_with_both_rollout_and_admission_off(self):
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{
				"preparing_at": frappe.utils.add_to_date(None, seconds=-ts.PREPARE_DEADLINE_S - 1),
				"reservation_expires_at": None,
			},
		)
		frappe.db.set_value(ts.PUMP, self.target, "transport_mode", "pump")
		frappe.db.commit()
		with patch.dict(frappe.conf, {"chat_collaboration_enabled": 0, admission.FLAG: 0}):
			self.sweep()
		self.assertEqual(self.current().state, "cancelled")
		self.assertFalse(self.current().reserved)
		self.assertTrue(frappe.db.exists(ts.MSG, self.turn.seed_message))
		self.assertEqual(self.gateway.sends, [])

	def test_queued_shared_request_ages_out_when_admission_is_disabled(self):
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{
				"state": "queued",
				"reserved": 0,
				"dispatching_at": None,
				"enqueued_at": frappe.utils.add_to_date(None, seconds=-ts.QUEUED_MAX_AGE_S - 1),
			},
		)
		frappe.db.commit()
		before = frappe.db.count(ts.MSG, {"conversation": self.conversation})
		with patch.dict(frappe.conf, {"chat_collaboration_enabled": 0, admission.FLAG: 0}):
			self.sweep()
		self.assertEqual(self.current().state, "cancelled")
		self.assertEqual(frappe.db.count(ts.MSG, {"conversation": self.conversation}), before)
		self.assertEqual(self.gateway.sends, [])

	def unknown_delivery(self):
		self.gateway.fail_ack = True
		self.drive()
		self.assertEqual(self.current().state, "recovering")

	def test_unknown_send_recovers_only_its_verified_session_result_after_revoke(self):
		self.unknown_delivery()
		self.gateway.history = [transcript_message("assistant", "Recovered api_key=SECRET_SENTINEL", seq=1)]
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		with patch.dict(frappe.conf, {"chat_collaboration_enabled": 0}):
			self.sweep()
		turn = self.current()
		self.assertEqual(turn.state, "finalizing")
		self.assertEqual(len(self.gateway.sends), 1)
		text = frappe.db.get_value(ts.MSG, turn.assistant_message, "content")
		self.assertIn("Recovered", text)
		self.assertNotIn("SECRET_SENTINEL", text)

	def test_unknown_send_never_steals_a_different_runs_transcript(self):
		self.unknown_delivery()
		self.gateway.last_run = "different-child"
		self.gateway.history = [transcript_message("assistant", "FOREIGN_RESULT", seq=1)]
		self.sweep()
		turn = self.current()
		self.assertEqual(turn.state, "recovering")
		self.assertNotIn("FOREIGN_RESULT", frappe.db.get_value(ts.MSG, turn.assistant_message, "content"))
		self.assertEqual(len(self.gateway.sends), 1)

	def test_exhausted_recovery_releases_credit_and_keeps_uncertainty_evidence(self):
		self.unknown_delivery()
		frappe.db.set_value(
			ts.TURN, self.turn.name, "recovery_started_at", frappe.utils.add_to_date(None, hours=-1)
		)
		frappe.db.commit()
		self.sweep()
		turn = self.current()
		self.assertEqual(turn.state, "errored")
		self.assertFalse(turn.reserved)
		self.assertIn("unknown", turn.error.lower())
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Uncertain")
		self.assertTrue(frappe.db.exists(c.SESSION, {"collaboration_binding": self.bound.name}))
		self.assertEqual(len(self.gateway.sends), 1)

	def test_budget_expiry_checks_verified_result_before_declaring_unknown(self):
		self.unknown_delivery()
		self.gateway.history = [transcript_message("assistant", "Recovered actual result", seq=1)]
		frappe.db.set_value(
			ts.TURN, self.turn.name, "recovery_started_at", frappe.utils.add_to_date(None, hours=-1)
		)
		frappe.db.commit()
		self.sweep()
		self.assertEqual(self.current().state, "finalizing")
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Active")
		self.assertEqual(len(self.gateway.sends), 1)

	def test_lost_pump_prepare_reservation_is_reclaimed_without_losing_seed(self):
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{
				"state": "queued",
				"reserved": 1,
				"collaboration_executor": "pump",
				"dispatching_at": None,
				"enqueued_at": frappe.utils.now_datetime(),
				"reservation_expires_at": frappe.utils.add_to_date(None, seconds=-1),
			},
		)
		frappe.db.set_value(ts.PUMP, self.target, "transport_mode", "pump")
		frappe.db.commit()
		before = int(self.current().version)
		with patch("jarvis.chat.pump.ensure_pump"), patch("jarvis.chat.pump.lpush_wake"):
			self.sweep()
		row = self.current()
		self.assertEqual((row.state, row.reserved, int(row.version)), ("queued", 0, before + 1))
		self.assertTrue(frappe.db.exists(ts.MSG, row.seed_message))
		self.assertEqual(self.gateway.sends, [])

	def test_flag_off_pump_leaves_shared_queued_request_for_resume_or_age_out(self):
		from jarvis.chat import pump

		frappe.db.set_value(
			ts.TURN, self.turn.name, {"state": "queued", "reserved": 0, "collaboration_executor": "pump"}
		)
		frappe.db.commit()
		with patch.dict(frappe.conf, {"chat_collaboration_enabled": 0}):
			self.assertIsNone(pump._pick_next_cold(self.target, set(), set()))
		self.assertEqual(self.current().state, "queued")
		self.assertEqual(self.gateway.sends, [])
