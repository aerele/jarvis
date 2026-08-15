"""Jarvis Chat Message DocType controller.

One row per user message, assistant response, or tool call within a
conversation. The 'streaming' flag is True while an assistant message is
mid-stream; the worker flips it to False on lifecycle end (or sets error
on lifecycle error). Tool rows carry tool_name, tool_args, tool_result,
tool_status fields populated by jarvis.api.call_tool when invoked from a
chat session.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class JarvisChatMessage(Document):
	def validate(self):
		self._validate_conversation_owner()

	def _validate_conversation_owner(self):
		"""Cross-link ownership guard (security review TASK 2).

		A chat message must belong to a conversation the acting user owns.
		``if_owner`` on the message row's OWN owner does not prevent injecting a
		row into another user's conversation (the row owner is trivially the
		creator on insert; ``api.get_conversation`` treats the CONVERSATION owner
		as the authority, so an injected row would surface in the victim's
		transcript). The ``has_permission`` hook
		(``jarvis.chat.chat_permissions.has_message_permission``) is the primary
		ORM control; this validate is defense-in-depth on every write path.

		Server context: the worker, tool dispatch (``impersonate(owner)``),
		scheduler and approval-resume writers insert assistant/tool/system rows
		with ``ignore_permissions=True`` (as the impersonated owner or
		Administrator) — those are trusted and skipped. Administrator bypasses
		perms entirely.
		"""
		if self.flags.ignore_permissions or frappe.session.user == "Administrator":
			return
		if not self.conversation:
			return
		owner = frappe.db.get_value("Jarvis Conversation", self.conversation, "owner")
		if owner and owner != frappe.session.user:
			frappe.throw(
				_("You can only post messages to your own conversations."),
				frappe.PermissionError,
			)
