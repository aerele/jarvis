"""Tests for the Jarvis Conversation DocType.

Schema, permissions (owner-only read), and the before_insert default for
last_active_at.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

DOCTYPE = "Jarvis Conversation"


class TestJarvisConversationDocType(FrappeTestCase):
	def test_doctype_exists(self):
		self.assertTrue(frappe.db.exists("DocType", DOCTYPE))

	def test_required_fields_exist(self):
		meta = frappe.get_meta(DOCTYPE)
		field_names = {f.fieldname for f in meta.fields}
		for required in ("title", "session_key", "last_active_at", "status"):
			self.assertIn(required, field_names, f"missing field: {required}")

	def test_session_key_is_unique(self):
		meta = frappe.get_meta(DOCTYPE)
		f = next(f for f in meta.fields if f.fieldname == "session_key")
		self.assertTrue(f.unique, "session_key must be unique")

	def test_status_select_options(self):
		meta = frappe.get_meta(DOCTYPE)
		f = next(f for f in meta.fields if f.fieldname == "status")
		self.assertEqual(f.fieldtype, "Select")
		options = (f.options or "").split("\n")
		self.assertIn("Active", options)
		self.assertIn("Archived", options)

	def test_owner_can_read_own_only(self):
		"""Permissions are owner-only and role-gated (security review TASK 7):
		read at permlevel 0 is granted only to "Jarvis User"/"System Manager"
		(NOT ``role: "All"``) and is ``if_owner`` scoped. The permlevel-1 row
		(session_key protection, TASK 9) is SM-only and correctly carries no
		``if_owner`` (permlevel rows are not owner-scoped)."""
		meta = frappe.get_meta(DOCTYPE)
		base_read_roles = {p.role for p in meta.permissions if p.read and (p.permlevel or 0) == 0}
		self.assertNotIn("All", base_read_roles, "conversation read must not be granted to 'All'")
		self.assertEqual(base_read_roles, {"Jarvis User", "System Manager"})
		for perm in meta.permissions:
			if perm.read and (perm.permlevel or 0) == 0:
				self.assertEqual(
					perm.if_owner,
					1,
					f"role {perm.role} has permlevel-0 read without if_owner",
				)

	def test_before_insert_sets_last_active_at(self):
		try:
			doc = frappe.get_doc(
				{
					"doctype": DOCTYPE,
					"title": "Test chat",
					"status": "Active",
				}
			)
			doc.insert(ignore_permissions=True)
			self.assertIsNotNone(doc.last_active_at)
		finally:
			frappe.delete_doc(DOCTYPE, doc.name, ignore_permissions=True, force=True)
			frappe.db.commit()
