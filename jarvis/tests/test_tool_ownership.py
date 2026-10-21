import json

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.tool_ownership import bind_to_live_reply, record_tool_owner


def _conversation(title):
	return frappe.get_doc({"doctype": "Jarvis Conversation", "title": title}).insert(ignore_permissions=True)


def _message(conversation, role, seq):
	return frappe.get_doc(
		{
			"doctype": "Jarvis Chat Message",
			"conversation": conversation.name,
			"seq": seq,
			"role": role,
		}
	).insert(ignore_permissions=True)


class TestToolOwnership(FrappeTestCase):
	def test_binding_is_durable_idempotent_and_conversation_scoped(self):
		conversation = _conversation("Ownership regression")
		answer = _message(conversation, "assistant", 1)
		queued = _message(conversation, "user", 2)
		cancelled = _message(conversation, "assistant", 3)
		for call_id in ("call-1", "call-1", "call-2"):
			record_tool_owner(conversation.name, answer.name, call_id)
		record_tool_owner("another-conversation", answer.name, "wrong-conversation")
		record_tool_owner(conversation.name, queued.name, "not-an-answer")
		self.assertEqual(json.loads(answer.reload().tool_call_ids), ["call-1", "call-2"])
		self.assertFalse(cancelled.reload().tool_call_ids)
		self.assertFalse(queued.reload().tool_call_ids)


class TestBindToLiveReply(FrappeTestCase):
	"""A Claude CLI turn sends no tool events, so its tools reach the bench only through
	the plugin callback. The callback binds each one to the reply of the running turn,
	or "Reports consulted" never finds its reports once the chat reloads."""

	def _turn(self, state, conversation=None, seq=1):
		conversation = conversation or _conversation("Callback ownership")
		seed = _message(conversation, "user", seq)
		answer = _message(conversation, "assistant", seq + 1)
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Turn",
				"run_id": frappe.generate_hash(length=12),
				"conversation": conversation.name,
				"relay_target_id": "test-target",
				"turn_class": "interactive",
				"state": state,
				"seed_message": seed.name,
				"assistant_message": answer.name,
			}
		).insert(ignore_permissions=True)
		return conversation, answer

	def test_a_queued_follow_up_does_not_take_the_running_turn_s_tool(self):
		conversation, running = self._turn("streaming")
		_, queued = self._turn("queued", conversation, seq=3)
		bind_to_live_reply(conversation.name, "mcp-1")
		self.assertEqual(json.loads(running.reload().tool_call_ids), ["mcp-1"])
		self.assertFalse(queued.reload().tool_call_ids)

	def test_the_running_turn_s_reply_owns_a_callback_tool(self):
		conversation, answer = self._turn("streaming")
		bind_to_live_reply(conversation.name, "mcp-1")
		bind_to_live_reply(conversation.name, "mcp-1")
		self.assertEqual(json.loads(answer.reload().tool_call_ids), ["mcp-1"])

	def test_a_finished_turn_takes_no_new_tool(self):
		conversation, answer = self._turn("done")
		bind_to_live_reply(conversation.name, "mcp-1")
		self.assertFalse(answer.reload().tool_call_ids)

	def test_nothing_is_bound_without_a_turn_or_an_id(self):
		conversation, answer = self._turn("streaming")
		bind_to_live_reply(conversation.name, None)
		bind_to_live_reply(_conversation("No turn").name, "mcp-2")
		self.assertFalse(answer.reload().tool_call_ids)
