"""Actual dispatched results settle under their immutable actor, including after revoke."""

import json
import uuid
from unittest.mock import Mock, patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import settlement
from jarvis.chat.collaboration_bindings import insert_message
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import OWNER, RECIPIENT


class TestCollaborationSettlement(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		self.reply = insert_message(self.bound, {"role": "assistant", "content": "partial", "streaming": 1})
		frappe.db.set_value(
			"Jarvis Chat Turn",
			self.turn.name,
			{
				"state": "terminal_observed",
				"assistant_message": self.reply.name,
				"terminal_kind": "relay:final",
				"pump_epoch": 12,
			},
		)
		frappe.db.commit()
		self.deps = Mock()

	def settle(self, **overrides):
		arguments = dict(
			relay_target_id="default",
			epoch=12,
			version=frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "version"),
			terminal_kind="relay:final",
			terminal_payload={"text": "Visible api_key=SECRET_SENTINEL"},
			assistant_message=self.reply.name,
			owner=OWNER,
			conversation=self.conversation,
			deps=self.deps,
		)
		arguments.update(overrides)
		settlement.invoke_settlement(self.turn.name, **arguments)

	def test_actual_final_uses_actor_and_safe_content_after_grant_revoked(self):
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		with patch("jarvis.chat.turn_state.publish_fenced") as publish:
			self.settle()
		turn = frappe.get_doc("Jarvis Chat Turn", self.turn.name)
		self.assertEqual(turn.state, "finalizing")
		self.assertFalse(turn.reserved)
		row = frappe.get_doc("Jarvis Chat Message", self.reply.name)
		self.assertFalse(row.streaming)
		self.assertEqual(row.actor, RECIPIENT)
		self.assertNotIn("SECRET_SENTINEL", row.content)
		self.assertEqual(publish.call_args.args[0], RECIPIENT)
		self.assertNotIn("SECRET_SENTINEL", json.dumps(publish.call_args.kwargs, default=str))
		self.deps.enqueue_finalize.assert_called_once_with(self.turn.name, "default")

	def test_duplicate_terminal_does_not_enqueue_or_settle_again(self):
		self.settle()
		first = frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "version")
		self.settle()
		self.assertEqual(frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "version"), first)
		self.deps.enqueue_finalize.assert_called_once()

	def test_undispatched_turn_cannot_claim_an_actual_result(self):
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "collaboration_send_started_at", None)
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.settle()
		self.assertEqual(frappe.db.get_value("Jarvis Chat Message", self.reply.name, "content"), "partial")

	def test_wrong_reply_cannot_receive_another_turns_terminal(self):
		with self.assertRaises(frappe.PermissionError):
			self.settle(assistant_message=self.message)
		self.assertEqual(
			frappe.db.get_value("Jarvis Chat Message", self.message, "content"), "Shared transcript sentinel"
		)

	def test_inconsistent_durable_reply_pointer_cannot_overwrite_a_different_actor(self):
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "assistant_message", self.message)
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.settle(assistant_message=self.message)
		self.assertEqual(
			frappe.db.get_value("Jarvis Chat Message", self.message, "content"), "Shared transcript sentinel"
		)

	def test_error_terminal_preserves_only_scrubbed_complete_shared_text(self):
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "terminal_kind", "relay:error")
		frappe.db.set_value("Jarvis Chat Message", self.reply.name, "content", "")
		frappe.db.commit()
		self.settle(
			terminal_kind="relay:error",
			terminal_payload={"text": "Useful answer api_key=SECRET_SENTINEL", "error": "connection lost"},
		)
		row = frappe.get_doc("Jarvis Chat Message", self.reply.name)
		self.assertIn("Useful answer", row.content)
		self.assertNotIn("SECRET_SENTINEL", row.content)
		self.assertFalse(row.streaming)
		self.assertEqual(row.error, "connection lost")
		self.assertTrue(
			frappe.db.exists("Jarvis Turn Effect", {"turn": self.turn.name, "effect_name": "usage"})
		)

	def test_aborted_terminal_preserves_only_scrubbed_complete_shared_text(self):
		frappe.db.set_value(
			"Jarvis Chat Turn", self.turn.name, {"terminal_kind": "relay:error", "cancel_requested": 1}
		)
		frappe.db.set_value("Jarvis Chat Message", self.reply.name, "content", "")
		frappe.db.commit()
		self.settle(
			terminal_kind="relay:error",
			terminal_payload={"text": "Completed portion api_key=SECRET_SENTINEL", "aborted": True},
		)
		row = frappe.get_doc("Jarvis Chat Message", self.reply.name)
		self.assertIn("Completed portion", row.content)
		self.assertNotIn("SECRET_SENTINEL", row.content)
		self.assertTrue(row.stopped)
		self.assertFalse(row.streaming)
		self.assertTrue(
			frappe.db.exists("Jarvis Turn Effect", {"turn": self.turn.name, "effect_name": "usage"})
		)
