"""Immutable collaboration identity ledger; never an end-user document."""

import re

import frappe
from frappe.model.document import Document
from frappe.utils import cint

from jarvis.chat import collaboration as c
from jarvis.permissions import NotRenamable


class JarvisChatBinding(NotRenamable, Document):
	def validate(self):
		c.guard_server_fields(self, c.IDENTITY_FIELDS)
		if not self.flags.jarvis_collaboration_write:
			c._deny()
		if self.is_new() and not frappe.db.exists("User", self.actor):
			c._deny()
		before = None if self.is_new() else self.get_doc_before_save()
		if before and before.state == "Retired" and self.state != "Retired":
			c._deny()
		if cint(frappe.conf.get("chat_collaboration_reader_floor")) < c.READER_FLOOR or not c.schema_ready():
			c._deny()
		if not c.is_collaborative(self.conversation) or c.NAMESPACE not in (self.session_key or ""):
			c._deny()
		if not re.fullmatch(r"[a-f0-9]{32}", self.logical_run_id or ""):
			c._deny()
		if cint(self.conversation_epoch) <= 0 or cint(self.conversation_epoch) != cint(
			frappe.db.get_value(c.CONVERSATION, self.conversation, "collaboration_epoch")
		):
			c._deny()
		if self.share:
			grant = frappe.db.get_value(
				c.SHARE, self.share, ["user", "conversation", "access_revision", "access_mode"], as_dict=True
			)
			if (
				not grant
				or grant.user != self.actor
				or grant.conversation != self.conversation
				or c.effective_mode(grant.access_mode) != "Chat"
				or cint(grant.access_revision) != cint(self.grant_revision)
			):
				c._deny()
		elif self.actor != frappe.db.get_value(c.CONVERSATION, self.conversation, "owner") or cint(
			self.grant_revision
		):
			c._deny()

	def on_trash(self):
		# Revocation retires authentication, not the identity/audit evidence.
		c._deny()
