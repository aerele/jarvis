from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.resolve_links import _resolve_one, resolve_links


class TestResolveLinks(FrappeTestCase):
	"""ToDo (header Link fields: role -> Role, allocated_to -> User) and User
	(child table roles -> Has Role.role -> Role) are core doctypes, so no
	ERPNext fixtures are needed."""

	def _link(self, res, field, row=None):
		for lk in res["links"]:
			if lk["field"] == field and lk.get("row") == row:
				return lk
		return None

	def test_exact_header_link(self):
		res = resolve_links("ToDo", {"allocated_to": "Administrator"})
		lk = self._link(res, "allocated_to")
		self.assertEqual(lk["status"], "exact")
		self.assertEqual(lk["target_doctype"], "User")

	def test_missing_header_link(self):
		res = resolve_links("ToDo", {"allocated_to": "ghost-not-a-user@invalid.example"})
		lk = self._link(res, "allocated_to")
		self.assertEqual(lk["status"], "missing")
		self.assertEqual(lk["candidates"], [])

	def test_candidates_header_link(self):
		# "System" is not a Role, but "System Manager" like-matches it.
		res = resolve_links("ToDo", {"role": "System"})
		lk = self._link(res, "role")
		self.assertEqual(lk["status"], "candidates")
		self.assertIn("System Manager", [c["name"] for c in lk["candidates"]])

	def test_child_row_links(self):
		res = resolve_links(
			"User",
			{"roles": [{"role": "System Manager"}, {"role": "No Such Role XYZ"}]},
		)
		row0 = self._link(res, "role", row=0)
		row1 = self._link(res, "role", row=1)
		self.assertEqual(row0["table"], "roles")
		self.assertEqual(row0["status"], "exact")
		self.assertEqual(row1["status"], "missing")

	def test_unchecked_when_no_read_permission(self):
		with patch("jarvis.tools.resolve_links.frappe.has_permission", return_value=False):
			res = resolve_links("ToDo", {"allocated_to": "Administrator"})
		self.assertEqual(self._link(res, "allocated_to")["status"], "unchecked")

	def test_exact_check_is_permission_scoped(self):
		"""A record that exists but is not readable by the caller (ToDo is
		read-scoped to allocated_to/assigned_by via get_permission_query_conditions)
		must not be reported "exact", even though the coarse doctype-level read
		gate passes for the "All" role. Otherwise the exact check becomes an
		existence oracle over records the user cannot read."""
		other_todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "resolve_links permission-scope probe",
				"allocated_to": "Administrator",
			}
		).insert(ignore_permissions=True)

		uname = "resolve_links_perm_test@example.com"
		if not frappe.db.exists("User", uname):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": uname,
					"first_name": "PermTest",
					"send_welcome_email": 0,
					"roles": [{"role": "Desk User"}],
				}
			).insert(ignore_permissions=True)

		orig_user = frappe.session.user
		try:
			frappe.set_user(uname)
			# Sanity: the coarse doctype-level gate passes (role "All" grants
			# read on ToDo) -- the leak can only be caught downstream, by the
			# exact-check query itself being permission-scoped.
			self.assertTrue(frappe.has_permission("ToDo", ptype="read"))
			rec = _resolve_one("allocated_to", "ToDo", other_todo.name, 5)
		finally:
			frappe.set_user(orig_user)

		self.assertNotEqual(rec["status"], "exact")

	def test_empty_values_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			resolve_links("ToDo", {})

	def test_unknown_doctype_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			resolve_links("No Such Doctype 999", {"x": "y"})
