"""Approval reviewer choices are narrower than generic skill sharing."""

import unittest

import frappe

from jarvis.chat import approvals_api, custom_skills_api
from jarvis.permissions import (
	JARVIS_ADMIN_ROLE,
	JARVIS_ADMIN_ROLES,
	JARVIS_SKILL_REVIEWER_ROLE,
	ensure_jarvis_admin_role,
	ensure_jarvis_user_role,
)
from jarvis.tests._pending_action_helpers import as_user

PREFIX = "j757-"
USERS = {
	"caller": ("Z Requester", ["Jarvis User"]),
	"admin": ("A Eligible admin", [JARVIS_ADMIN_ROLE]),
	"manager": ("B Eligible manager", ["System Manager"]),
	"both": ("C Eligible both", [JARVIS_ADMIN_ROLE, "System Manager"]),
	"plain": ("D Ordinary user", ["Jarvis User"]),
	"skill": ("E Skill reviewer", ["Jarvis User", JARVIS_SKILL_REVIEWER_ROLE]),
	"disabled": ("F Disabled admin", [JARVIS_ADMIN_ROLE]),
	"website": ("G Website admin", [JARVIS_ADMIN_ROLE]),
	"outsider": ("H No Jarvis access", []),
}


def email(key):
	return f"{PREFIX}{key}@example.com"


class TestApprovalReviewers(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert frappe.local.site in ("test_jarvis", "jarvis-ci.test")
		cls.original_user = frappe.session.user
		cls.created = []
		cls.addClassCleanup(cls.cleanup_users)
		frappe.set_user("Administrator")
		ensure_jarvis_user_role()
		ensure_jarvis_admin_role()
		if not frappe.db.exists("Role", JARVIS_SKILL_REVIEWER_ROLE):
			frappe.get_doc(
				{"doctype": "Role", "role_name": JARVIS_SKILL_REVIEWER_ROLE, "desk_access": 1}
			).insert(ignore_permissions=True)
		for key, (label, roles) in USERS.items():
			assert not frappe.db.exists("User", email(key)), "stale reviewer test fixture"
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email(key),
					"first_name": label,
					"user_type": "System User",
					"send_welcome_email": 0,
					"roles": [{"role": r} for r in roles],
				}
			).insert(ignore_permissions=True)
			cls.created.append(email(key))
		frappe.db.set_value("User", email("disabled"), "enabled", 0)
		# Legacy/corrupt combinations must not bypass the explicit user-type gate.
		frappe.db.set_value("User", email("website"), "user_type", "Website User")
		frappe.db.set_value("User", email("outsider"), "user_type", "System User")
		frappe.db.commit()
		for user in cls.created:
			frappe.clear_cache(user=user)

	@classmethod
	def cleanup_users(cls):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for user in reversed(cls.created):
			if frappe.db.exists("User", user):
				frappe.delete_doc("User", user, ignore_permissions=True, force=True)
		frappe.db.commit()
		frappe.set_user(cls.original_user)

	def test_only_enabled_system_admins_are_returned_once_in_stable_order(self):
		with as_user(email("caller")):
			rows = approvals_api.list_approval_reviewers()
		ours = [r for r in rows if r["name"].startswith(PREFIX)]
		self.assertEqual([r["name"] for r in ours], [email("admin"), email("manager"), email("both")])
		self.assertEqual(len({r["name"] for r in rows}), len(rows))
		for row in rows:
			self.assertEqual(set(row), {"name", "full_name"})

	def test_caller_and_builtin_accounts_are_excluded(self):
		with as_user(email("admin")):
			rows = approvals_api.list_approval_reviewers()
		names = {r["name"] for r in rows}
		self.assertFalse(names & {email("admin"), "Administrator", "Guest"})
		self.assertIn(email("manager"), names)

	def test_missing_display_names_sort_by_the_label_shown_in_the_picker(self):
		for missing in (None, ""):
			with self.subTest(full_name=missing):
				frappe.db.set_value("User", email("both"), "full_name", missing)
				with as_user(email("caller")):
					rows = approvals_api.list_approval_reviewers()
				ours = [r["name"] for r in rows if r["name"].startswith(PREFIX)]
				self.assertEqual(ours, [email("admin"), email("manager"), email("both")])

	def test_empty_roster_when_only_the_caller_has_an_admin_role(self):
		frappe.db.savepoint("j757_empty")
		try:
			# Uncommitted test-only state: other connections never lose their roles.
			frappe.db.delete(
				"Has Role",
				{"parenttype": "User", "role": ["in", JARVIS_ADMIN_ROLES], "parent": ["!=", email("admin")]},
			)
			with as_user(email("admin")):
				self.assertEqual(approvals_api.list_approval_reviewers(), [])
		finally:
			frappe.db.rollback(save_point="j757_empty")

	def test_guest_and_non_jarvis_callers_are_refused(self):
		for user in ("Guest", email("outsider")):
			with self.subTest(user=user), as_user(user), self.assertRaises(frappe.PermissionError):
				approvals_api.list_approval_reviewers()

	def test_generic_skill_roster_keeps_plain_users_and_skill_reviewers(self):
		with as_user(email("caller")):
			names = {r["name"] for r in custom_skills_api.list_shareable_users()}
		self.assertTrue({email("plain"), email("skill")} <= names)
