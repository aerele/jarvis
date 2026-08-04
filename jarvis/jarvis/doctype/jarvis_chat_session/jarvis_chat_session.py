"""Jarvis Chat Session DocType controller.

Maps a single agent session key to the Frappe user who initiated the
session. Rows are inserted at session-create time (chat UI's
``_ensure_session_key`` or ``demo.py``). When a tool call arrives carrying
``X-Jarvis-Session``, ``jarvis.api.call_tool`` resolves the user via this
table and dispatches under ``frappe.set_user(user)``.
"""

import frappe
from frappe.model.document import Document


class JarvisChatSession(Document):
	# Minimal controller - all validation is handled by field constraints.
	# session_key is unique+mandatory; user is a mandatory Link to User.

	def before_insert(self):
		if not self.created_at:
			self.created_at = frappe.utils.now()
