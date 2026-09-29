"""Shared conversation / non-admin-user test helpers.

Relocated from the retired ``test_auto_apply`` suite (Auto-Apply was removed in
the action-card overhaul) so the macro + skill suites - and the new Auto-Apply
removal tests - keep a single home for these fixtures.
"""

import frappe

from jarvis.permissions import ensure_jarvis_user_role

CONV = "Jarvis Conversation"

# A plain System User WITHOUT System Manager, used for the non-admin cases.
NON_ADMIN_USER = "jarvis-autoapply-plain@example.com"


def _ensure_non_admin_user() -> None:
	"""Create a plain Jarvis chat user: has the "Jarvis User" app-access role (as
	every real chat user does, granted at onboarding/migration) but NOT System
	Manager — so the gating tests still exercise the non-admin path while getting
	past the chat-API app-access gate. Idempotent."""
	ensure_jarvis_user_role()  # the test site may not have run after_migrate
	if frappe.db.exists("User", NON_ADMIN_USER):
		if "System Manager" in frappe.get_roles(NON_ADMIN_USER):
			frappe.get_doc("User", NON_ADMIN_USER).remove_roles("System Manager")
			frappe.db.commit()
		if "Jarvis User" not in frappe.get_roles(NON_ADMIN_USER):
			frappe.get_doc("User", NON_ADMIN_USER).add_roles("Jarvis User")
			frappe.db.commit()
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": NON_ADMIN_USER,
			"first_name": "Plain",
			"last_name": "User",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.add_roles("Jarvis User")
	frappe.db.commit()


def _make_conv(owner: str) -> str:
	"""Create a Jarvis Conversation owned by ``owner`` and return its name."""
	orig = frappe.session.user
	frappe.set_user(owner)
	try:
		doc = frappe.get_doc({"doctype": CONV, "title": "conv test"})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	finally:
		frappe.set_user(orig)
