"""Tests for admin-armed macro "skip confirmation".

An admin arms a Jarvis Macro (``skip_confirmation``); its runs then execute the
covered gated writes without a confirmation card. The single source of truth the
write-confirmation gate reads is ``Jarvis Conversation.skip_confirmation``, stamped
onto an armed macro's run conversation. This module covers the admin guards on both
flags (this file), the gate branch, the run stamp + human-inert block, the
clear-on-terminal, and D5 stop-and-report.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.permissions import ensure_jarvis_user_role
from jarvis.tests.test_auto_apply import (
	NON_ADMIN_USER,
	_ensure_non_admin_user,
	_make_conv,
)
from jarvis.tests.test_chat_api import TEST_USER, _ensure_test_user

CONV = "Jarvis Conversation"
MACRO = "Jarvis Macro"

# A Jarvis Admin who is NOT a System Manager - proves the guard admits the
# Jarvis Admin tier specifically, not only System Manager (edge-case review).
ADMIN_USER = "jarvis-skip-admin@example.com"


def _ensure_admin_user() -> None:
	"""A Jarvis Admin (+ Jarvis User so it can own/write a macro), NOT System Manager."""
	ensure_jarvis_user_role()
	if not frappe.db.exists("User", ADMIN_USER):
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": ADMIN_USER,
				"first_name": "Skip",
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


def _make_macro(owner: str, *, armed: bool = False, name: str = "skip-conf test macro") -> str:
	"""Create a Jarvis Macro owned by ``owner`` with one step; return its name.
	``armed`` stamps skip_confirmation via a raw db write (bypassing the guard),
	simulating an already-armed macro."""
	orig = frappe.session.user
	frappe.set_user(owner)
	try:
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": name,
				"steps": [{"prompt": "do the thing"}],
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		if armed:
			frappe.db.set_value(MACRO, doc.name, "skip_confirmation", 1, update_modified=False)
			frappe.db.commit()
		return doc.name
	finally:
		frappe.set_user(orig)


class TestConversationSkipConfirmationGuard(FrappeTestCase):
	"""The LOAD-BEARING guard: the gate reads Jarvis Conversation.skip_confirmation,
	which is owner-writable with no permlevel. A non-admin owner must not flip it
	0 -> 1 through a generic save; only a Jarvis Admin / System Manager may."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()
		_ensure_non_admin_user()
		_ensure_admin_user()

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)

	def test_non_admin_owner_save_cannot_enable(self):
		conv = _make_conv(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.skip_confirmation = 1
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation") or 0), 0)

	def test_jarvis_admin_save_can_enable(self):
		conv = _make_conv(ADMIN_USER)
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.skip_confirmation = 1
		doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation")), 1)

	def test_non_admin_owner_save_can_disable(self):
		conv = _make_conv(NON_ADMIN_USER)
		# Server stamped it on (the run_macro raw path), then the owner disarms.
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.skip_confirmation = 0
		doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation") or 0), 0)

	def test_raw_stamp_bypasses_the_guard(self):
		"""The sanctioned run_macro path (raw db.set_value) sets the flag on a
		non-admin owner's conversation without tripping the controller guard."""
		conv = _make_conv(NON_ADMIN_USER)
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation")), 1)


class TestMacroSkipConfirmationGuard(FrappeTestCase):
	"""Arming a macro (Jarvis Macro.skip_confirmation 0 -> 1) requires admin;
	editing steps while armed keeps it armed (D6 trust model)."""

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
			for name in frappe.get_all(MACRO, filters={"owner": owner}, pluck="name"):
				frappe.delete_doc(MACRO, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_non_admin_owner_cannot_arm(self):
		macro = _make_macro(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(MACRO, macro)
		doc.skip_confirmation = 1
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(int(frappe.db.get_value(MACRO, macro, "skip_confirmation") or 0), 0)

	def test_jarvis_admin_can_arm_own_macro(self):
		macro = _make_macro(ADMIN_USER)
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(MACRO, macro)
		doc.skip_confirmation = 1
		doc.save()
		self.assertEqual(int(frappe.db.get_value(MACRO, macro, "skip_confirmation")), 1)

	def test_editing_steps_keeps_arm_for_non_admin_owner(self):
		"""D6 trust model: an admin armed the macro; its NON-admin owner can later
		edit the steps (a no-op 1 -> 1 transition on skip_confirmation) without admin
		rights and without disarming - arming trusts the owner for future steps too."""
		macro = _make_macro(NON_ADMIN_USER, armed=True)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(MACRO, macro)
		doc.steps[0].prompt = "do a different thing"
		doc.save()  # must not raise; arm persists
		self.assertEqual(int(frappe.db.get_value(MACRO, macro, "skip_confirmation")), 1)

	def test_owner_can_disarm(self):
		macro = _make_macro(ADMIN_USER, armed=True)
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(MACRO, macro)
		doc.skip_confirmation = 0
		doc.save()
		self.assertEqual(int(frappe.db.get_value(MACRO, macro, "skip_confirmation") or 0), 0)
