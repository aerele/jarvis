"""get_my_access: the calling user's OWN roles + record-level restrictions, in one
bounded call. Self-scoped — it never reports another user's access (no privilege
probing), and it drops the base All/Guest roles."""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.permissions import ensure_jarvis_user_role
from jarvis.tools.get_my_access import get_my_access

ROLED_USER = "jarvis-access-roled@example.com"
OTHER_USER = "jarvis-access-other@example.com"
SM_USER = "jarvis-access-sm@example.com"


def _ensure_user(email: str, first: str, roles: list[str]) -> None:
	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first,
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	doc = frappe.get_doc("User", email)
	missing = set(roles) - set(frappe.get_roles(email))
	if missing:
		doc.add_roles(*missing)
	frappe.db.commit()


def _user_permission(user: str, allow: str, for_value: str) -> str:
	name = (
		frappe.get_doc({"doctype": "User Permission", "user": user, "allow": allow, "for_value": for_value})
		.insert(ignore_permissions=True)
		.name
	)
	frappe.db.commit()
	return name


class TestGetMyAccess(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(ROLED_USER, "Roled", ["Jarvis User", "Accounts User"])
		_ensure_user(OTHER_USER, "Other", ["Jarvis User"])
		_ensure_user(SM_USER, "Sysman", ["Jarvis User", "System Manager"])

	def setUp(self):
		# These tests commit real User Permission rows; start each from a clean slate
		# for the test users so a prior test/run can't leak a baseline restriction.
		for u in (ROLED_USER, OTHER_USER, SM_USER):
			for n in frappe.get_all("User Permission", filters={"user": u}, pluck="name"):
				frappe.delete_doc("User Permission", n, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _as(self, user: str):
		frappe.set_user(user)
		self.addCleanup(lambda: frappe.set_user("Administrator"))

	def test_reports_the_calling_users_meaningful_roles(self):
		self._as(ROLED_USER)
		out = get_my_access()
		self.assertEqual(out["user"], ROLED_USER)
		self.assertEqual(out["full_name"], "Roled")
		self.assertIn("Accounts User", out["roles"])
		self.assertIn("Jarvis User", out["roles"])
		self.assertNotIn("All", out["roles"])
		self.assertNotIn("Guest", out["roles"])
		self.assertEqual(out["role_count"], len(out["roles"]))
		self.assertFalse(out["is_system_manager"])

	def test_system_manager_is_flagged(self):
		self._as(SM_USER)
		self.assertTrue(get_my_access()["is_system_manager"])

	def test_administrator_is_flagged_even_without_the_role_row(self):
		self._as("Administrator")
		self.assertTrue(get_my_access()["is_system_manager"])

	def test_restrictions_reflect_the_users_user_permissions(self):
		self._as(ROLED_USER)
		# Baseline: no restriction yet.
		self.assertEqual(get_my_access()["restrictions"], [])
		# Restrict this user to a single User record; it must surface.
		name = _user_permission(ROLED_USER, "User", OTHER_USER)
		self.addCleanup(
			lambda: frappe.delete_doc("User Permission", name, force=True, ignore_permissions=True)
		)
		out = get_my_access()
		self.assertEqual(out["restriction_count"], 1)
		self.assertEqual(out["restrictions"][0]["doctype"], "User")
		self.assertEqual(out["restrictions"][0]["value"], OTHER_USER)
		self.assertFalse(out["restrictions_truncated"])

	def test_self_scoped_never_reports_another_users_restriction(self):
		# A User Permission on OTHER_USER must never appear when ROLED_USER asks.
		name = _user_permission(OTHER_USER, "User", ROLED_USER)
		self.addCleanup(
			lambda: frappe.delete_doc("User Permission", name, force=True, ignore_permissions=True)
		)
		self._as(ROLED_USER)
		out = get_my_access()
		self.assertEqual(out["restrictions"], [], "another user's restriction leaked into my access")
