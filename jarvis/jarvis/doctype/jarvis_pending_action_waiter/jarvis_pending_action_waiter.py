"""Jarvis Pending Action Waiter: a conversation waiting on a held pending action.

Server-written only, like its parent: rows arrive with the parent's flagged
insert and change through raw conditional UPDATEs in
``jarvis.chat.pending_actions``, so any direct ORM write or delete is refused."""

import frappe
from frappe.model.document import Document

from jarvis.jarvis.doctype.jarvis_pending_action.jarvis_pending_action import refuse_orm_write


class JarvisPendingActionWaiter(Document):
	def validate(self):
		refuse_orm_write(self)

	def on_trash(self):
		refuse_orm_write(self, deleting=True)


def on_doctype_update():
	"""One waiter row per (pending action, conversation)."""
	frappe.db.add_unique(
		"Jarvis Pending Action Waiter", ["parent", "conversation"], constraint_name="parent_conversation"
	)
