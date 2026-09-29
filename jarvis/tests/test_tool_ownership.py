import json

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.tool_ownership import record_tool_owner


class TestToolOwnership(FrappeTestCase):
	def test_binding_is_durable_idempotent_and_conversation_scoped(self):
		conversation = frappe.get_doc(
			{"doctype": "Jarvis Conversation", "title": "Ownership regression"}
		).insert(ignore_permissions=True)

		def message(role, seq):
			return frappe.get_doc(
				{
					"doctype": "Jarvis Chat Message",
					"conversation": conversation.name,
					"seq": seq,
					"role": role,
				}
			).insert(ignore_permissions=True)

		answer = message("assistant", 1)
		queued = message("user", 2)
		cancelled = message("assistant", 3)
		for call_id in ("call-1", "call-1", "call-2"):
			record_tool_owner(conversation.name, answer.name, call_id)
		record_tool_owner("another-conversation", answer.name, "wrong-conversation")
		record_tool_owner(conversation.name, queued.name, "not-an-answer")
		self.assertEqual(json.loads(answer.reload().tool_call_ids), ["call-1", "call-2"])
		self.assertFalse(cancelled.reload().tool_call_ids)
		self.assertFalse(queued.reload().tool_call_ids)
