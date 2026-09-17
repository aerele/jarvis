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

	def _add_up(self, user: str, allow: str, for_value: str) -> str:
		name = _user_permission(user, allow, for_value)
		self.addCleanup(
			lambda: frappe.delete_doc("User Permission", name, force=True, ignore_permissions=True)
		)
		return name

	def _restrictions(self) -> dict:
		return get_my_access()["restrictions"]

	def _detail(self) -> list:
		return self._restrictions()["detail"]

	def _other_up_doctype(self):
		"""A (DocType, names) other than "User" that accepts a User Permission, else skipTest.
		Only the DISTINCT-DocType cap test needs a second allow doctype; the sampling guard
		is deterministic (below) and never depends on site data."""
		for dt in ("Country", "Currency", "Language", "DocType", "Role"):
			try:
				names = frappe.get_all(dt, pluck="name", limit=1)
			except Exception:
				continue
			if not names:
				continue
			try:  # confirm User Permission accepts this allow (probe, rolled back)
				probe = _user_permission(ROLED_USER, dt, names[0])
				frappe.delete_doc("User Permission", probe, force=True, ignore_permissions=True)
				frappe.db.commit()
				return dt, names[0]
			except Exception:
				continue
		self.skipTest("no second DocType accepts a User Permission on this site")

	def test_detail_carries_applies_to(self):
		self._as(ROLED_USER)
		name = self._add_up(ROLED_USER, "User", OTHER_USER)
		frappe.db.set_value("User Permission", name, "applicable_for", "Contact")
		self.assertEqual(self._detail()[0]["applies_to"], "Contact")

	def test_no_restrictions_is_empty(self):
		self._as(ROLED_USER)
		r = self._restrictions()
		self.assertEqual(r["by_doctype"], {})
		self.assertEqual(r["detail"], [])
		self.assertEqual(r["total"], 0)

	def test_restriction_surfaces_in_by_doctype_and_detail(self):
		self._as(ROLED_USER)
		self._add_up(ROLED_USER, "User", OTHER_USER)
		r = self._restrictions()
		self.assertEqual(r["total"], 1)
		self.assertEqual(r["by_doctype"]["User"]["count"], 1)
		self.assertIn(OTHER_USER, r["by_doctype"]["User"]["values"])
		self.assertFalse(r["by_doctype"]["User"]["values_truncated"])
		self.assertEqual(r["detail"][0]["doctype"], "User")
		self.assertEqual(r["detail"][0]["value"], OTHER_USER)
		self.assertFalse(r["detail_truncated"])

	def test_multiple_on_same_doctype_group_into_one_entry(self):
		self._as(ROLED_USER)
		self._add_up(ROLED_USER, "User", OTHER_USER)
		self._add_up(ROLED_USER, "User", SM_USER)
		g = self._restrictions()["by_doctype"]["User"]
		self.assertEqual(g["count"], 2)
		self.assertEqual(set(g["values"]), {OTHER_USER, SM_USER})
		self.assertEqual(self._restrictions()["total"], 2)

	def test_by_doctype_samples_and_flags_large_value_sets(self):
		# The whole point of R3: more values than the sample on one DocType report a count +
		# a flagged sample, never a wall. Deterministic (sample patched to 2, 3 real Users) so
		# this load-bearing guard always runs — never a data-dependent skip. (mutation target.)
		self._as(ROLED_USER)
		self._add_up(ROLED_USER, "User", OTHER_USER)
		self._add_up(ROLED_USER, "User", SM_USER)
		self._add_up(ROLED_USER, "User", "Administrator")
		with patch.object(gma_mod, "_RESTRICTION_SAMPLE", 2):
			g = self._restrictions()["by_doctype"]["User"]
		self.assertEqual(g["count"], 3)
		self.assertEqual(len(g["values"]), 2)
		self.assertTrue(g["values_truncated"])

	def test_by_doctype_caps_distinct_doctypes_but_reports_true_count(self):
		# Cap the NUMBER of DocType keys too (top by count) so a user restricted across many
		# DocTypes can't blow the model budget; doctype_count keeps the true total honest.
		self._as(ROLED_USER)
		dt2, val2 = self._other_up_doctype()
		self._add_up(ROLED_USER, "User", OTHER_USER)
		self._add_up(ROLED_USER, "User", SM_USER)  # "User" has the higher count -> survives the cap
		self._add_up(ROLED_USER, dt2, val2)
		with patch.object(gma_mod, "_MAX_RESTRICTION_DOCTYPES", 1):
			r = self._restrictions()
		self.assertEqual(len(r["by_doctype"]), 1)
		self.assertIn("User", r["by_doctype"])  # kept the top DocType by count
		self.assertTrue(r["doctypes_truncated"])
		self.assertEqual(r["doctype_count"], 2)  # true distinct-DocType total still reported
		self.assertEqual(r["total"], 3)  # and total rows honest

	def test_detail_is_capped_but_by_doctype_counts_stay_complete(self):
		self._as(ROLED_USER)
		self._add_up(ROLED_USER, "User", OTHER_USER)
		self._add_up(ROLED_USER, "User", SM_USER)
		with patch.object(gma_mod, "_MAX_DETAIL", 1):
			r = self._restrictions()
		self.assertEqual(len(r["detail"]), 1)
		self.assertTrue(r["detail_truncated"])
		self.assertEqual(r["by_doctype"]["User"]["count"], 2)  # counts complete past the detail cap
		self.assertEqual(r["total"], 2)

	def test_self_scoped_never_reports_another_users_restriction(self):
		# A User Permission on OTHER_USER must never appear when ROLED_USER asks.
		self._add_up(OTHER_USER, "User", ROLED_USER)
		self._as(ROLED_USER)
		r = self._restrictions()
		self.assertEqual(r["by_doctype"], {}, "another user's restriction leaked (by_doctype)")
		self.assertEqual(r["detail"], [], "another user's restriction leaked (detail)")

	def test_detail_keeps_per_row_tree_scope_precise(self):
		# Option B's payoff: a single grouped form would collapse mixed tree-scope; the
		# per-row detail must keep subtree vs node-only distinct.
		self._as(ROLED_USER)
		self._add_up(ROLED_USER, "User", OTHER_USER)  # subtree (hide_descendants 0)
		n2 = self._add_up(ROLED_USER, "User", SM_USER)
		frappe.db.set_value("User Permission", n2, "hide_descendants", 1)  # node-only
		with patch.object(gma_mod, "_is_tree_doctype", return_value=True):
			by_val = {d["value"]: d["includes_descendants"] for d in self._detail()}
		self.assertTrue(by_val[OTHER_USER])
		self.assertFalse(by_val[SM_USER])

	def test_hide_descendants_narrows_the_scope_to_the_node(self):
		self._as(ROLED_USER)
		name = self._add_up(ROLED_USER, "User", OTHER_USER)
		frappe.db.set_value("User Permission", name, "hide_descendants", 1)
		with patch.object(gma_mod, "_is_tree_doctype", return_value=True):
			self.assertFalse(self._detail()[0]["includes_descendants"])

	def test_non_tree_detail_omits_descendant_scope(self):
		# "User" is not a nested-set DocType, so the descendant flag is meaningless and absent.
		self._as(ROLED_USER)
		self._add_up(ROLED_USER, "User", OTHER_USER)
		self.assertNotIn("includes_descendants", self._detail()[0])

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

	def test_max_doctypes_at_the_cap_is_allowed(self):
		# D2: the per-call cap (now 50) is a boundary, not a wall one-below it.
		self._as("Administrator")
		dts = frappe.get_all("DocType", pluck="name", limit=gma_mod._MAX_DOCTYPES)
		self.assertEqual(len(dts), gma_mod._MAX_DOCTYPES)  # site has enough doctypes
		out = get_my_access(doctypes=dts)
		self.assertEqual(len(out["can"]), gma_mod._MAX_DOCTYPES)

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
