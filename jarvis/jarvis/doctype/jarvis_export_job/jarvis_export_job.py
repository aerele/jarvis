import frappe
from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisExportJob(NotRenamable, Document):
	def validate(self):
		if not self.flags.jarvis_server_write:
			frappe.throw("Export jobs are managed by the export service.", frappe.PermissionError)


def get_permission_query_conditions(user=None):
	user = user or frappe.session.user
	if user != "Guest" and (user == "Administrator" or "System Manager" in frappe.get_roles(user)):
		return ""
	return "`tabJarvis Export Job`.owner = " + frappe.db.escape(user) if user != "Guest" else "1=0"


def has_permission(doc, user=None, ptype=None, permission_type=None):
	user = user or frappe.session.user
	if (ptype or permission_type) not in (None, "read", "select") or user == "Guest":
		return False
	return bool(doc.owner == user or user == "Administrator" or "System Manager" in frappe.get_roles(user))
