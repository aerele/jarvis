"""Explicit, owner-issued read grants; never Frappe document sharing."""

import frappe
from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisConversationShare(NotRenamable, Document):
	def before_insert(self):
		self._validate_owner_write()

	def validate(self):
		self._validate_owner_write()
		if not self.is_new():
			if any(
				self.has_value_changed(field)
				for field in ("conversation", "user", "shared_by", "seen_marker")
			):
				frappe.throw(
					frappe._("Share identities and seen markers cannot be edited."), frappe.PermissionError
				)
		elif self.seen_marker:
			frappe.throw(frappe._("A new share cannot have a seen marker."), frappe.PermissionError)

	def _validate_owner_write(self):
		from jarvis.chat.sharing import _validate_addition

		if not frappe.flags.get("jarvis_share_write"):
			frappe.throw(frappe._("Use the chat's sharing dialog to add recipients."), frappe.PermissionError)
		doc = frappe.get_doc("Jarvis Conversation", self.conversation)
		if not self.shared_by == doc.owner == frappe.session.user:
			frappe.throw(frappe._("Only the owner can share this chat."), frappe.PermissionError)
		_validate_addition(doc, self.user)


def on_doctype_update():
	frappe.db.add_unique(
		"Jarvis Conversation Share", ["conversation", "user"], constraint_name="conversation_user"
	)
