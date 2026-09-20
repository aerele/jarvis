import frappe
from frappe.tests.utils import FrappeTestCase

# The audit sink is metadata-only BY CONSTRUCTION: these are the only fields it
# may ever carry, and none of the content-adjacent ones may appear.
_META_ONLY = {
	"actor",
	"actor_name",
	"tool",
	"outcome",
	"provenance",
	"provenance_name",
	"ref_doctype",
	"ref_name",
	"bulk_count",
	"conversation",
	"model",
	"at",
}
_FORBIDDEN = {"tool_args", "tool_result", "content", "error"}


def _row(**kw):
	d = {
		"doctype": "Jarvis Agent Write",
		"actor": "Administrator",
		"tool": "create_doc",
		"outcome": "applied",
		"provenance": "chat",
		"at": frappe.utils.now(),
	}
	d.update(kw)
	return frappe.get_doc(d).insert(ignore_permissions=True).name


def _ensure_jarvis_admin():
	"""A realistic tamper actor: a Jarvis Admin who is NOT a superuser."""
	if not frappe.db.exists("User", "mgr@example.com"):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": "mgr@example.com",
				"first_name": "Mgr",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		u.add_roles("Jarvis Admin")
	return "mgr@example.com"


class TestAgentWriteDoctype(FrappeTestCase):
	def test_schema_is_metadata_only(self):
		fields = {f.fieldname for f in frappe.get_meta("Jarvis Agent Write").fields}
		self.assertTrue(fields <= _META_ONLY, msg=f"unexpected fields: {fields - _META_ONLY}")
		self.assertFalse(_FORBIDDEN & fields)  # content can never be stored

	def test_append_only_jarvis_admin_cannot_delete(self):
		name = _row()
		frappe.set_user(_ensure_jarvis_admin())  # the realistic tamper actor, not Guest
		try:
			with self.assertRaises(frappe.PermissionError):
				frappe.delete_doc("Jarvis Agent Write", name, ignore_permissions=False)
		finally:
			frappe.set_user("Administrator")
