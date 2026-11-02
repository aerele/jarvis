"""Jarvis Chat Session DocType controller.

Maps a single agent session key to the Frappe user who initiated the
session. Rows are inserted at session-create time (chat UI's
``_ensure_session_key`` or ``demo.py``). When a tool call arrives carrying
``X-Jarvis-Session``, ``jarvis.api.call_tool`` resolves the user via this
table and dispatches under ``frappe.set_user(user)``.
"""

import frappe
from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisChatSession(NotRenamable, Document):
	# Legacy rows retain their field constraints. Collaborative auth rows also
	# require an immutable, matching identity ledger entry.
	def validate(self):
		from jarvis.chat import collaboration as c

		c.guard_server_fields(self, ("collaboration_binding",))
		before = None if self.is_new() else self.get_doc_before_save()
		if (
			self.get("collaboration_binding")
			or (before and before.get("collaboration_binding"))
			or c.NAMESPACE in (self.session_key or "")
		):
			if not self.flags.jarvis_collaboration_write:
				c._deny()
			if before and (self.has_value_changed("user") or self.has_value_changed("session_key")):
				c._deny()
			binding = frappe.db.get_value(
				c.BINDING, self.get("collaboration_binding"), ["actor", "session_key", "state"], as_dict=True
			)
			if (
				not binding
				or binding.state != "Active"
				or binding.actor != self.user
				or binding.session_key != self.session_key
			):
				c._deny()

	def on_trash(self):
		from jarvis.chat import collaboration as c

		if c.is_collaborative_session(self.session_key):
			c._deny()

	def before_insert(self):
		if not self.created_at:
			self.created_at = frappe.utils.now()
