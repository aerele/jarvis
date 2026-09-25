"""Regression tests for #595 - Incorrect Skill Promotion State Allows Duplicate
Requests and Blocks Approval After Rejection.

Root cause: ``request_skill_promotion`` only ever checked the SOURCE skill's own
``scope`` column, which never changes when a promotion materializes a SEPARATE
shared (Role/Org) row from it (``_materialize_promotion``). So a skill already
promoted to Org could be re-requested endlessly, and the duplicate's approval
failed late, inside ``_materialize_promotion``, with a confusing "already shared"
error - after a reviewer had already spent time on it.

The fix resolves the skill's REAL effective shared scope by lineage
(``_effective_shared_scope``, shared by the request-time and approval-time
guards) and enforces it at REQUEST time, plus rejects a second Pending request
for the same skill. A legitimate further widen (Role -> Org after an approved
Role promotion) must keep working, and a rejection must never block a later,
genuinely-fresh request. Mirrors ``test_part2_security.py``'s fixtures
(``_as``, ``_mk_skill``, reviewer/user roles) so this suite runs the same way a
real caller would - through the whitelisted endpoints, not ``ignore_permissions``.
"""

from __future__ import annotations

import contextlib

import frappe
from frappe.tests.utils import FrappeTestCase

SKILL = "Jarvis Custom Skill"
SKILL_PROMO = "Jarvis Skill Promotion Request"

REVIEWER = "sp595-reviewer@example.com"
REVIEWER_2 = "sp595-reviewer2@example.com"
USER_A = "sp595-usera@example.com"
PFX = "sp595"


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _ensure_user(email: str, roles: list[str]) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": PFX,
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.insert(ignore_permissions=True)
	if "System Manager" in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).remove_roles("System Manager")
	add = [r for r in roles if r not in set(frappe.get_roles(email))]
	if add:
		frappe.get_doc("User", email).add_roles(*add)
	return email


def _sweep():
	"""Committing cleanup: request/decide both commit, so rows survive the
	per-test rollback and must be hard-deleted between tests."""
	frappe.db.rollback()
	for n in frappe.get_all(SKILL, filters={"skill_name": ["like", f"{PFX}-%"]}, pluck="name"):
		frappe.delete_doc(SKILL, n, force=True, ignore_permissions=True)
	frappe.db.delete(SKILL_PROMO, {"owner": ["in", [REVIEWER, REVIEWER_2, USER_A]]})
	if frappe.db.table_exists("Jarvis Shared Skill Slug"):
		frappe.db.delete("Jarvis Shared Skill Slug", {"slug": ["like", f"{PFX}-%"]})
	frappe.db.commit()


def _mk_skill(owner: str, skill_name: str) -> "frappe.model.document.Document":
	"""Mint a plain private (User-scope) skill owned by ``owner``."""
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": SKILL,
				"skill_name": skill_name,
				"description": f"{skill_name} desc",
				"instructions": "body",
				"enabled": 1,
				"user_invocable": 1,
			}
		)
		doc.insert(ignore_permissions=True)
	return doc


class PromotionLifecycleBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_sweep()
		_ensure_user(REVIEWER, ["Jarvis User", "Jarvis Skill Reviewer"])
		_ensure_user(REVIEWER_2, ["Jarvis User", "Jarvis Skill Reviewer"])
		# "Sales User" so USER_A can name a real Role-scope target
		# (promotable_target_roles only ever offers the caller's OWN roles).
		_ensure_user(USER_A, ["Jarvis User", "Sales User"])

	def tearDown(self):
		frappe.set_user("Administrator")
		_sweep()
		super().tearDown()


class TestDuplicatePendingRequestRejected(PromotionLifecycleBase):
	def test_second_pending_request_for_same_skill_is_refused(self):
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-dup")
		with _as(USER_A):
			first = custom_skills_api.request_skill_promotion(skill.name, "Org")
			self.assertTrue(first["ok"])
			with self.assertRaises(frappe.ValidationError):
				custom_skills_api.request_skill_promotion(skill.name, "Org")
		# Only the one Pending request exists.
		rows = frappe.get_all(SKILL_PROMO, filters={"skill": skill.name})
		self.assertEqual(len(rows), 1)


class TestAlreadySharedBlocksReRequest(PromotionLifecycleBase):
	def test_reapproved_skill_cannot_be_re_requested_at_same_scope(self):
		# #595 repro: approve once, then a second request at/below the now-shared
		# scope must be refused AT REQUEST TIME - not accepted and left to fail
		# later, confusingly, when a reviewer tries to approve it.
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-already-org")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			out = custom_skills_api.decide_skill_promotion(req["request"], 1)
		self.assertTrue(out["ok"])

		with _as(USER_A):
			with self.assertRaisesRegex(frappe.ValidationError, "already shared at Org"):
				custom_skills_api.request_skill_promotion(skill.name, "Org")

	def test_role_to_org_widen_after_approved_role_still_allowed(self):
		# Product decision: a FURTHER widen stays allowed. Role -> Org after an
		# approved Role promotion must succeed, and must widen the SAME shared row
		# in place (never a second shared copy of the same lineage).
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-widen")
		with _as(USER_A):
			req1 = custom_skills_api.request_skill_promotion(
				skill.name, "Role", target_role="Sales User", target_roles=["Sales User"]
			)
		with _as(REVIEWER):
			out1 = custom_skills_api.decide_skill_promotion(req1["request"], 1)
		self.assertTrue(out1["ok"])
		role_copy = out1["materialized"]
		self.assertEqual(frappe.db.get_value(SKILL, role_copy, "scope"), "Role")

		with _as(USER_A):
			req2 = custom_skills_api.request_skill_promotion(skill.name, "Org")
			self.assertTrue(req2["ok"])
		with _as(REVIEWER_2):
			out2 = custom_skills_api.decide_skill_promotion(req2["request"], 1)
		self.assertTrue(out2["ok"])
		self.assertEqual(out2["materialized"], role_copy, "must widen the SAME row, not insert a 2nd")
		self.assertEqual(frappe.db.get_value(SKILL, role_copy, "scope"), "Org")
		# Exactly one shared row for this lineage, ever.
		shared_rows = frappe.get_all(
			SKILL, filters={"skill_name": f"{PFX}-widen", "scope": ("in", ("Role", "Org"))}
		)
		self.assertEqual(len(shared_rows), 1)


class TestReRequestAfterRejection(PromotionLifecycleBase):
	def test_reject_then_request_then_approve_succeeds(self):
		# The other half of #595: a rejection (nothing ever approved) must never
		# block a later, genuinely-fresh request from being approved.
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-rerequest")
		with _as(USER_A):
			req1 = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			rejected = custom_skills_api.decide_skill_promotion(req1["request"], 0, note="not yet")
		self.assertEqual(rejected["status"], "Rejected")
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "scope"), "User")

		with _as(USER_A):
			req2 = custom_skills_api.request_skill_promotion(skill.name, "Org")
			self.assertTrue(req2["ok"])
		with _as(REVIEWER):
			approved = custom_skills_api.decide_skill_promotion(req2["request"], 1)
		self.assertTrue(approved["ok"])
		self.assertEqual(approved["status"], "Approved")


class TestMySkillPromotionEffectiveScope(PromotionLifecycleBase):
	def test_effective_scope_reflects_lineage_not_the_stale_source_column(self):
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-mine")
		with _as(USER_A):
			mine = custom_skills_api.my_skill_promotion(skill.name)
		self.assertEqual(mine["effective_scope"], "User")

		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			custom_skills_api.decide_skill_promotion(req["request"], 1)

		with _as(USER_A):
			mine = custom_skills_api.my_skill_promotion(skill.name)
		# The SOURCE skill's own scope column is still "User" (it is left intact),
		# but effective_scope must report the lineage's real state: "Org".
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "scope"), "User")
		self.assertEqual(mine["effective_scope"], "Org")
		self.assertEqual(mine["status"], "Approved")
