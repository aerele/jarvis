"""Tests for the "Approve & run" schema fields + their admin guards.

Skill "Approve & run the plan" (design doc D-CONTROL, §3.4): an admin arms a
Jarvis Custom Skill (``allow_approve_run``); an approved run then stamps
``Jarvis Conversation.skill_autorun`` (+ the sliding ``skill_autorun_at``
timestamp) on its own conversation, which the write-confirmation gate reads
to run the explicit ``_SKILL_AUTORUN_COVERED`` allowlist uncarded. This
module covers only the schema + the admin guards on both flags - the LOAD-
BEARING backstop that a plain owner cannot self-grant either bypass through a
generic ``doc.save()``. Mirrors ``test_macro_skip_confirmation.py`` exactly.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.permissions import ensure_jarvis_user_role
from jarvis.tests.test_auto_apply import (
	NON_ADMIN_USER,
	_ensure_non_admin_user,
	_make_conv,
)

CONV = "Jarvis Conversation"
SKILL = "Jarvis Custom Skill"

# A Jarvis Admin who is NOT a System Manager - proves the guard admits the
# Jarvis Admin tier specifically, not only System Manager (edge-case review).
ADMIN_USER = "jarvis-approve-run-admin@example.com"


def _ensure_admin_user() -> None:
	"""A Jarvis Admin (+ Jarvis User so it can own/write a skill), NOT System Manager."""
	ensure_jarvis_user_role()
	if not frappe.db.exists("User", ADMIN_USER):
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": ADMIN_USER,
				"first_name": "Approve",
				"last_name": "Admin",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		)
		doc.insert(ignore_permissions=True)
	user = frappe.get_doc("User", ADMIN_USER)
	roles = set(frappe.get_roles(ADMIN_USER))
	if "System Manager" in roles:
		user.remove_roles("System Manager")
	for role in ("Jarvis Admin", "Jarvis User"):
		if role not in roles:
			user.add_roles(role)  # add_roles auto-vivifies a missing Role row
	frappe.db.commit()


def _make_skill(owner: str, *, armed: bool = False, name: str = "approve-run-test") -> str:
	"""Create a User-scope Jarvis Custom Skill owned by ``owner``; return its name.
	``armed`` stamps allow_approve_run via a raw db write (bypassing the guard),
	simulating an already admin-armed skill."""
	orig = frappe.session.user
	frappe.set_user(owner)
	try:
		doc = frappe.get_doc(
			{
				"doctype": SKILL,
				"skill_name": name,
				"description": "approve & run test skill",
				"instructions": "do the thing",
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		if armed:
			frappe.db.set_value(SKILL, doc.name, "allow_approve_run", 1, update_modified=False)
			frappe.db.commit()
		return doc.name
	finally:
		frappe.set_user(orig)


class TestConversationSkillAutorunGuard(FrappeTestCase):
	"""The LOAD-BEARING guard: the gate will read Jarvis Conversation.skill_autorun,
	which is owner-writable with no permlevel. A non-admin owner must not flip it
	0 -> 1 through a generic save; only a Jarvis Admin / System Manager may."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_non_admin_user()
		_ensure_admin_user()

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)
		# _make_conv commits, so FrappeTestCase's class-rollback won't undo it.
		for owner in (NON_ADMIN_USER, ADMIN_USER):
			for name in frappe.get_all(CONV, filters={"owner": owner}, pluck="name"):
				frappe.delete_doc(CONV, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_non_admin_owner_save_cannot_enable(self):
		conv = _make_conv(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.skill_autorun = 1
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)

	def test_jarvis_admin_save_can_enable(self):
		conv = _make_conv(ADMIN_USER)
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.skill_autorun = 1
		doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun")), 1)

	def test_non_admin_owner_save_can_disable(self):
		conv = _make_conv(NON_ADMIN_USER)
		# Server stamped it on (the sanctioned approve_and_run raw path), then the
		# owner saves again with it off - always free.
		frappe.db.set_value(CONV, conv, "skill_autorun", 1, update_modified=False)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.skill_autorun = 0
		doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)

	def test_raw_stamp_bypasses_the_guard(self):
		"""The sanctioned approve_and_run path (raw db.set_value) sets the flag on a
		non-admin owner's conversation without tripping the controller guard."""
		conv = _make_conv(NON_ADMIN_USER)
		frappe.db.set_value(CONV, conv, "skill_autorun", 1, update_modified=False)
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun")), 1)


class TestCustomSkillAllowApproveRunGuard(FrappeTestCase):
	"""Arming a skill (Jarvis Custom Skill.allow_approve_run 0 -> 1) requires
	admin (design doc D-CONTROL); editing other fields while armed keeps it
	armed - same D6 trust-persists-across-edits idiom the macro uses."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_non_admin_user()
		_ensure_admin_user()

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)
		for owner in (NON_ADMIN_USER, ADMIN_USER):
			for name in frappe.get_all(SKILL, filters={"owner": owner}, pluck="name"):
				frappe.delete_doc(SKILL, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_non_admin_owner_cannot_enable(self):
		skill = _make_skill(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(SKILL, skill)
		doc.allow_approve_run = 1
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(int(frappe.db.get_value(SKILL, skill, "allow_approve_run") or 0), 0)

	def test_jarvis_admin_can_enable_own_skill(self):
		skill = _make_skill(ADMIN_USER)
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(SKILL, skill)
		doc.allow_approve_run = 1
		doc.save()
		self.assertEqual(int(frappe.db.get_value(SKILL, skill, "allow_approve_run")), 1)

	def test_owner_can_disable(self):
		skill = _make_skill(ADMIN_USER, armed=True)
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(SKILL, skill)
		doc.allow_approve_run = 0
		doc.save()
		self.assertEqual(int(frappe.db.get_value(SKILL, skill, "allow_approve_run") or 0), 0)

	def test_editing_content_keeps_arm_for_non_admin_owner(self):
		"""D6 trust model: an admin armed the skill; its NON-admin owner can later
		edit its instructions (a no-op 1 -> 1 transition on allow_approve_run)
		without admin rights and without disarming - arming trusts the owner for
		future edits too."""
		skill = _make_skill(NON_ADMIN_USER, armed=True)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(SKILL, skill)
		doc.instructions = "do a different thing"
		doc.save()  # must not raise; arm persists
		self.assertEqual(int(frappe.db.get_value(SKILL, skill, "allow_approve_run")), 1)


class TestApproveAndRunFieldShapes(FrappeTestCase):
	"""Field-presence sanity: the three schema fields exist with the shape the
	design doc + downstream lifecycle code depend on (§3.4: hidden/no_copy,
	db_set-only fields on the conversation; a listable/filterable admin toggle
	on the skill)."""

	def test_conversation_skill_autorun_field(self):
		df = frappe.get_meta(CONV).get_field("skill_autorun")
		self.assertIsNotNone(df)
		self.assertEqual(df.fieldtype, "Check")
		self.assertEqual(int(df.hidden or 0), 1)
		self.assertEqual(int(df.no_copy or 0), 1)
		self.assertEqual(df.default, "0")

	def test_conversation_skill_autorun_at_field(self):
		df = frappe.get_meta(CONV).get_field("skill_autorun_at")
		self.assertIsNotNone(df)
		self.assertEqual(df.fieldtype, "Datetime")
		self.assertEqual(int(df.hidden or 0), 1)
		self.assertEqual(int(df.no_copy or 0), 1)

	def test_custom_skill_allow_approve_run_field(self):
		df = frappe.get_meta(SKILL).get_field("allow_approve_run")
		self.assertIsNotNone(df)
		self.assertEqual(df.fieldtype, "Check")
		self.assertEqual(df.default, "0")
		self.assertEqual(int(df.in_list_view or 0), 1)
		self.assertEqual(int(df.in_standard_filter or 0), 1)
