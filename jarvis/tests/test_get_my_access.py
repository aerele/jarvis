"""get_my_access: the calling user's OWN roles + record-level restrictions, in one
bounded call. Self-scoped — it never reports another user's access (no privilege
probing), and it drops the base All/Guest roles."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.permissions import ensure_jarvis_user_role
from jarvis.tools import get_my_access as gma_mod
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

	def _only_restriction(self):
		out = get_my_access()
		self.assertEqual(len(out["restrictions"]), 1)
		return out["restrictions"][0]

	def test_tree_doctype_permission_reports_descendant_scope(self):
		# On a tree DocType, a node permission covers the subtree by default — the
		# response must say so, else "node only" and "node + subtree" look identical.
		self._as(ROLED_USER)
		name = _user_permission(ROLED_USER, "User", OTHER_USER)  # hide_descendants defaults to 0
		self.addCleanup(
			lambda: frappe.delete_doc("User Permission", name, force=True, ignore_permissions=True)
		)
		with patch.object(gma_mod, "_is_tree_doctype", return_value=True):
			self.assertTrue(self._only_restriction()["includes_descendants"])

	def test_hide_descendants_narrows_the_scope_to_the_node(self):
		self._as(ROLED_USER)
		name = _user_permission(ROLED_USER, "User", OTHER_USER)
		frappe.db.set_value("User Permission", name, "hide_descendants", 1)
		self.addCleanup(
			lambda: frappe.delete_doc("User Permission", name, force=True, ignore_permissions=True)
		)
		with patch.object(gma_mod, "_is_tree_doctype", return_value=True):
			self.assertFalse(self._only_restriction()["includes_descendants"])

	def test_non_tree_permission_omits_descendant_scope(self):
		# "User" is not a nested-set DocType, so the descendant flag is meaningless and absent.
		self._as(ROLED_USER)
		name = _user_permission(ROLED_USER, "User", OTHER_USER)
		self.addCleanup(
			lambda: frappe.delete_doc("User Permission", name, force=True, ignore_permissions=True)
		)
		self.assertNotIn("includes_descendants", self._only_restriction())

	# --- optional capability projection ("can I do X?") ---

	def test_no_doctypes_arg_omits_can(self):
		self._as(ROLED_USER)
		self.assertNotIn("can", get_my_access())

	def test_can_reports_doctype_actions(self):
		# Administrator can do everything, so the projection is deterministic here.
		self._as("Administrator")
		out = get_my_access(doctypes=["User"])
		self.assertIn("can", out)
		user_caps = out["can"]["User"]
		self.assertEqual(set(user_caps), {"create", "read", "write", "delete"})  # User is not submittable
		self.assertTrue(all(user_caps.values()))  # admin -> all True

	def test_submittable_doctype_reports_submit_and_cancel(self):
		subm = frappe.get_all("DocType", filters={"is_submittable": 1}, pluck="name", limit=1)
		if not subm:
			self.skipTest("no submittable DocType on this site")
		self._as("Administrator")
		caps = get_my_access(doctypes=[subm[0]])["can"][subm[0]]
		self.assertIn("submit", caps)
		self.assertIn("cancel", caps)

	def test_capabilities_reflect_the_users_roles(self):
		# Not hardcoded True: a plain Jarvis user cannot create User records; the admin can.
		self._as(OTHER_USER)  # only the Jarvis User role
		self.assertFalse(get_my_access(doctypes=["User"])["can"]["User"]["create"])
		frappe.set_user("Administrator")  # _as already queued the restore cleanup
		self.assertTrue(get_my_access(doctypes=["User"])["can"]["User"]["create"])

	def test_unknown_doctype_raises(self):
		self._as(ROLED_USER)
		with self.assertRaises(InvalidArgumentError):
			get_my_access(doctypes=["No Such DocType ZZZ"])

	def test_too_many_doctypes_raises(self):
		self._as(ROLED_USER)
		with self.assertRaises(InvalidArgumentError):
			get_my_access(doctypes=["User"] * (gma_mod._MAX_DOCTYPES + 1))

	def test_non_list_doctypes_raises(self):
		self._as(ROLED_USER)
		with self.assertRaises(InvalidArgumentError):
			get_my_access(doctypes="User")

	# --- per-record, User-Permission-aware projection ("can I do X to THIS record?") ---

	def test_no_docs_arg_omits_can_doc(self):
		self._as(ROLED_USER)
		self.assertNotIn("can_doc", get_my_access())

	def test_can_doc_reports_record_actions(self):
		# Administrator can act on any record; "User" is not submittable -> no submit/cancel.
		self._as("Administrator")
		out = get_my_access(docs=[{"doctype": "User", "name": "Administrator"}])
		self.assertIn("can_doc", out)
		entry = out["can_doc"][0]
		self.assertEqual(entry["doctype"], "User")
		self.assertEqual(entry["name"], "Administrator")
		self.assertEqual(set(entry["can"]), {"read", "write", "delete"})  # no create; not submittable
		self.assertTrue(all(entry["can"].values()))

	def test_can_doc_is_user_permission_aware(self):
		# The record-level answer MUST go through has_permission WITH the doc — per
		# frappe.permissions that is exactly what applies User Permissions / DocShare /
		# owner rules (a doc-less check is role-only). Pin that the doc is passed.
		self._as(ROLED_USER)
		with patch.object(gma_mod.frappe, "has_permission", return_value=True) as hp:
			get_my_access(docs=[{"doctype": "User", "name": "Administrator"}])
		self.assertTrue(
			any(c.kwargs.get("doc") == "Administrator" for c in hp.call_args_list),
			"record capability check must pass doc= so User Permissions are applied",
		)

	def test_unknown_doc_record_raises(self):
		self._as(ROLED_USER)
		with self.assertRaises(InvalidArgumentError):
			get_my_access(docs=[{"doctype": "User", "name": "no-such-user-zzz@example.com"}])

	def test_docs_entry_must_be_an_object(self):
		self._as(ROLED_USER)
		with self.assertRaises(InvalidArgumentError):
			get_my_access(docs=["User"])

	def test_too_many_docs_raises(self):
		self._as(ROLED_USER)
		with self.assertRaises(InvalidArgumentError):
			get_my_access(docs=[{"doctype": "User", "name": "Administrator"}] * (gma_mod._MAX_DOCTYPES + 1))
