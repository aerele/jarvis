"""Computed role-attachment tests (plan sections 4.1, 6.6).

``roles_for_doctype`` reads permission grants off ``get_all_perms``, which is
backed by ``DocPerm`` / ``Custom DocPerm`` rows keyed by role. We fabricate
DocPerm rows pointing at a fictional doctype name (no DDL, no real table needed)
plus a handful of test Roles, and assert the enumeration honors: desk-access
only, permlevel-0 read only, and the if_owner exclusion.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.learning.roles import roles_for_doctype, roles_for_user

DOCTYPE = "_JPL Perm Doctype"
ROLE_DESK = "_JPL Role Desk"
ROLE_PORTAL = "_JPL Role Portal"
ROLE_OWNER = "_JPL Role Owner"
ROLE_DISABLED = "_JPL Role Disabled"


class TestRolesForDoctype(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self._role(ROLE_DESK, desk_access=1, disabled=0)
		self._role(ROLE_PORTAL, desk_access=0, disabled=0)
		self._role(ROLE_OWNER, desk_access=1, disabled=0)
		self._role(ROLE_DISABLED, desk_access=1, disabled=1)
		# org-wide read (qualifies)
		self._docperm(ROLE_DESK, permlevel=0, read=1, if_owner=0)
		# portal role: readable but desk_access=0 (excluded)
		self._docperm(ROLE_PORTAL, permlevel=0, read=1, if_owner=0)
		# if_owner-only read (per-owner, not org-wide -> excluded)
		self._docperm(ROLE_OWNER, permlevel=0, read=1, if_owner=1)
		# disabled role with a valid grant (excluded)
		self._docperm(ROLE_DISABLED, permlevel=0, read=1, if_owner=0)

	def tearDown(self):
		frappe.db.delete("DocPerm", {"parent": DOCTYPE})
		for role in (ROLE_DESK, ROLE_PORTAL, ROLE_OWNER, ROLE_DISABLED):
			frappe.db.delete("Role", {"name": role})
		super().tearDown()

	def _role(self, name, desk_access, disabled):
		if frappe.db.exists("Role", name):
			frappe.db.set_value("Role", name, {"desk_access": desk_access, "disabled": disabled})
			return
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": name,
				"desk_access": desk_access,
				"disabled": disabled,
			}
		).insert(ignore_permissions=True)

	def _docperm(self, role, *, name_suffix="", **fields):
		d = frappe.new_doc("DocPerm")
		d.update({"role": role, **fields})
		d.parent = DOCTYPE
		d.parenttype = "DocType"
		d.parentfield = "permissions"
		d.name = f"_JPL-perm-{role}{name_suffix}".replace(" ", "-")
		d.flags.name_set = True
		d.db_insert()

	def test_only_desk_orgwide_read_role_qualifies(self):
		result = roles_for_doctype(DOCTYPE)
		self.assertEqual(result, [ROLE_DESK])

	def test_portal_role_excluded(self):
		self.assertNotIn(ROLE_PORTAL, roles_for_doctype(DOCTYPE))

	def test_if_owner_only_excluded(self):
		self.assertNotIn(ROLE_OWNER, roles_for_doctype(DOCTYPE))

	def test_disabled_role_excluded(self):
		self.assertNotIn(ROLE_DISABLED, roles_for_doctype(DOCTYPE))

	def test_permlevel1_read_does_not_qualify(self):
		# Give the OWNER role a permlevel-1 (non-if_owner) read too; it still must
		# not qualify - its only permlevel-0 read is if_owner.
		self._docperm(ROLE_OWNER, name_suffix="-pl1", permlevel=1, read=1, if_owner=0)
		self.assertNotIn(ROLE_OWNER, roles_for_doctype(DOCTYPE))

	def test_empty_doctype_returns_empty(self):
		self.assertEqual(roles_for_doctype(""), [])
		self.assertEqual(roles_for_doctype(None), [])


class TestRolesForUser(FrappeTestCase):
	def test_returns_set(self):
		roles = roles_for_user("Administrator")
		self.assertIsInstance(roles, set)
		self.assertIn("System Manager", roles)

	def test_missing_user_no_crash(self):
		self.assertIsInstance(roles_for_user("nobody-xyz@example.com"), set)
