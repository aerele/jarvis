"""Retry only a proven unsent request, using the same actor, binding and key."""

from unittest.mock import patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import turn_state as ts
from jarvis.tests._share_helpers import OUTSIDER, OWNER, RECIPIENT, as_user
from jarvis.tests.test_chat_collaboration_rq import RQCase, setUpModule


class TestCollaborationRetry(RQCase):
	def setUp(self):
		super().setUp()
		setting = patch.dict(frappe.conf, {"jarvis_phase0_admission_enabled": 1})
		setting.start()
		self.addCleanup(setting.stop)

	def failed_unsent(self):
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{
				"state": "cancelled",
				"reserved": 0,
				"error": "Request did not start",
				"collaboration_retry_allowed": 1,
			},
		)
		frappe.db.commit()

	def retry(self, actor=RECIPIENT):
		from jarvis.chat.collaboration_api import retry_run

		with as_user(actor):
			return retry_run(self.conversation, self.turn.name)

	def test_unsent_retry_reuses_identity_and_seed_and_double_click_queues_once(self):
		self.failed_unsent()
		before = int(frappe.db.get_value(ts.TURN, self.turn.name, "version"))
		with patch("jarvis.chat.collaboration_ownership.wake_queued"):
			first, second = self.retry(), self.retry()
		self.assertTrue(first["ok"])
		self.assertTrue(second["duplicate"])
		row = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertEqual(
			(row.state, row.seed_message, row.actor, row.collaboration_binding),
			("queued", self.turn.seed_message, RECIPIENT, self.bound.name),
		)
		self.assertEqual(int(row.version), before + 1)
		self.assertEqual(frappe.db.count(ts.TURN, {"conversation": self.conversation}), 1)
		self.assertEqual(
			frappe.db.get_value(c.BINDING, self.bound.name, "session_key"), self.bound.session_key
		)
		self.assertEqual(self.gateway.sends, [])

	def test_owner_and_outsider_cannot_retry_as_recipient(self):
		self.failed_unsent()
		for actor in (OWNER, OUTSIDER):
			with self.assertRaises(frappe.PermissionError):
				self.retry(actor)
			frappe.db.rollback()
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")

	def test_lost_ack_request_is_never_replayed(self):
		self.gateway.fail_ack = True
		self.drive()
		with self.assertRaises(frappe.ValidationError):
			self.retry()
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "recovering")
		self.assertEqual(len(self.gateway.sends), 1)

	def test_regrant_cannot_revive_old_retry_identity(self):
		self.failed_unsent()
		frappe.db.set_value(c.SHARE, self.share, {"access_revision": 4, "access_mode": "Chat"})
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.retry()
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")

	def test_retry_never_hands_an_existing_run_to_a_different_executor(self):
		self.failed_unsent()
		frappe.db.set_value(ts.PUMP, self.target, "transport_mode", "pump")
		frappe.db.commit()
		with self.assertRaises(frappe.ValidationError):
			self.retry()
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "collaboration_executor"), "rq")
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")

	def test_owner_moderation_cannot_be_undone_by_recipient_retry(self):
		from jarvis.chat.collaboration_api import stop_run

		with as_user(OWNER):
			stop_run(self.conversation, self.turn.name)
		with (
			patch("jarvis.chat.collaboration_ownership.wake_queued"),
			self.assertRaises(frappe.ValidationError),
		):
			self.retry()
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")

	def test_disable_then_reenable_cannot_revive_a_fenced_request(self):
		frappe.db.set_value("User", RECIPIENT, "enabled", 0)
		frappe.db.commit()
		self.drive()
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")
		frappe.db.set_value("User", RECIPIENT, "enabled", 1)
		frappe.db.commit()
		with (
			patch("jarvis.chat.collaboration_ownership.wake_queued"),
			self.assertRaises(frappe.ValidationError),
		):
			self.retry()
		self.assertEqual(self.gateway.sends, [])

	def test_sharing_off_then_on_cannot_revive_a_fenced_request(self):
		frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 0)
		frappe.db.commit()
		self.drive()
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")
		frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 1)
		frappe.db.commit()
		with (
			patch("jarvis.chat.collaboration_ownership.wake_queued"),
			self.assertRaises(frappe.ValidationError),
		):
			self.retry()
		self.assertEqual(self.gateway.sends, [])

	def test_actor_can_retry_their_own_unsent_stop_and_the_request_runs_once(self):
		from jarvis.chat.collaboration_api import stop_run

		with as_user(RECIPIENT):
			stop_run(self.conversation, self.turn.name)
		with patch("jarvis.chat.collaboration_ownership.wake_queued"):
			self.assertTrue(self.retry()["ok"])
		from jarvis.chat import admission

		with (
			patch.object(admission, "_dispatch_promoted"),
			patch.object(admission, "_legacy_streaming_count", return_value=0),
		):
			self.assertEqual(admission.promote_next(self.target), 1)
		self.drive()
		self.assertEqual(len(self.gateway.sends), 1)
		self.assertEqual(self.gateway.sends[0][2], self.turn.name)
