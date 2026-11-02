"""An explicit review grant, written only through the approval's owner gate."""

import hashlib

import frappe
from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisApprovalReviewer(NotRenamable, Document):
	def autoname(self):
		self.name = hashlib.sha256(f"{self.approval}\0{self.user}".encode()).hexdigest()[:24]

	def validate(self):
		self._server_only()

	def on_trash(self):
		self._server_only()

	def _server_only(self):
		if not self.flags.jarvis_server_write:
			frappe.throw("Review grants are server-managed. Use Tag for review.", frappe.PermissionError)

	def validate_share(self, _share):
		frappe.throw("Review grants cannot be shared.", frappe.PermissionError)
