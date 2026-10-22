"""A trusted writer preserves actor identity without granting ERP permissions."""

import frappe

from jarvis.chat import collaboration as c
from jarvis.tests._collaboration_helpers import CollaborationCase
from jarvis.tests._share_helpers import OWNER, RECIPIENT, as_user, make_chat


class TestCollaborationTurnContext(CollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn()

	def scope(self, run=None):
		from jarvis.chat.collaboration_turn import turn_context

		return turn_context(self.bound, run or self.turn.name)

	def tool_message(self, **kwargs):
		doc = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": self.conversation,
				"role": "tool",
				"content": "actual receipt",
				"seq": 10,
				**kwargs,
			}
		)
		doc.flags.jarvis_server_write = True
		return doc

	def test_scope_runs_as_immutable_recipient_and_restores_ambient_identity(self):
		with as_user(OWNER):
			with self.scope():
				self.assertEqual(frappe.session.user, RECIPIENT)
			self.assertEqual(frappe.session.user, OWNER)

	def test_scope_resets_after_an_exception(self):
		from jarvis.chat.collaboration_turn import current_turn

		with as_user(OWNER):
			with self.assertRaises(RuntimeError):
				with self.scope():
					raise RuntimeError("planted failure")
			self.assertIsNone(current_turn())
			self.assertEqual(frappe.session.user, OWNER)

	def test_trusted_receipt_gets_same_actor_binding_and_child_run(self):
		with self.scope():
			doc = self.tool_message()
			doc.insert(ignore_permissions=True)
		self.assertEqual(
			(doc.owner, doc.actor, doc.collaboration_binding, doc.collaboration_run_id),
			(RECIPIENT, RECIPIENT, self.bound.name, self.turn.name),
		)

	def test_generic_insert_still_fails_even_inside_a_trusted_turn_scope(self):
		with self.scope(), self.assertRaises(frappe.PermissionError):
			self.tool_message().insert()

	def test_explicit_actor_mismatch_is_not_silently_rewritten(self):
		with self.scope(), self.assertRaises(frappe.PermissionError):
			self.tool_message(actor=OWNER).insert(ignore_permissions=True)

	def test_wrong_run_cannot_enter_a_turn_scope(self):
		with self.assertRaises(frappe.PermissionError):
			with self.scope("b" * 32):
				self.fail("wrong run entered")

	def test_revocation_does_not_erase_dispatched_output_identity(self):
		frappe.db.delete(c.SHARE, {"name": self.bound.share})
		with self.scope():
			doc = self.tool_message()
			doc.insert(ignore_permissions=True)
		self.assertEqual(doc.actor, RECIPIENT)

	def test_revocation_still_prevents_new_user_work_inside_the_scope(self):
		frappe.db.delete(c.SHARE, {"name": self.bound.share})
		with self.scope(), self.assertRaises(frappe.PermissionError):
			self.tool_message(role="user").insert(ignore_permissions=True)

	def test_shared_scope_cannot_write_into_a_different_private_conversation(self):
		other, _ = make_chat()
		with self.scope(), self.assertRaises(frappe.PermissionError):
			self.tool_message(conversation=other).insert(ignore_permissions=True)
