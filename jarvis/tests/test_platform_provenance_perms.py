"""PP-5 provenance ledger — permission scoping (panel blocker fix).

The ``Jarvis Agent Provenance Event`` ledger references ``installation``,
``reviewing_human``, ``initiating_human`` and ``detail`` (promotion sign-offs
and the PP-6 reviewer-capacity justifications). It previously granted the
``Jarvis User`` role ``if_owner`` read with NO ``permission_query_conditions`` /
``has_permission`` hook registered in ``hooks.py`` — the same anti-pattern the
security-review parts eliminated. Because the ledger writer inserts events with
``ignore_permissions`` (agents_api._append_provenance_event) the row ``owner`` is
whoever/whatever was in session at append time, so ``if_owner`` was neither a
reliable nor a sufficient tenant boundary: a cross-tenant / PII leak over
generic REST GET ``/api/resource/Jarvis Agent Provenance Event``.

The fix drops the ``Jarvis User`` read grant entirely from the doctype: the
ledger is now readable only by the oversight tier (System Manager, Jarvis Admin,
Administrator). No user-facing surface reads the ledger over generic REST today
(events are written server-side and the promotion / ceiling-raise APIs return
only the event name), so nothing legitimate depends on the dropped grant.

These tests assert the boundary directly: a plain Jarvis User can neither list
nor open a provenance event, while the oversight tier still can.
"""

from __future__ import annotations

import contextlib

import frappe
from frappe.tests.utils import FrappeTestCase

PROVENANCE = "Jarvis Agent Provenance Event"

USER_A = "ppprov-usera@example.com"
ADMIN_USER = "ppprov-admin@example.com"
PFX = "ppprov"


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _ensure_user(email: str, roles: list[str]) -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
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
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	# Force the exact role set (drop System Manager so the role gates are real).
	if "System Manager" in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).remove_roles("System Manager")
	have = set(frappe.get_roles(email))
	add = [r for r in roles if r not in have]
	if add:
		frappe.get_doc("User", email).add_roles(*add)
	strip = [
		r
		for r in ("Jarvis User", "Jarvis Skill Reviewer", "Jarvis Admin")
		if r not in roles and r in set(frappe.get_roles(email))
	]
	if strip:
		frappe.get_doc("User", email).remove_roles(*strip)
	return email


def _mk_event(**fields) -> str:
	doc = frappe.get_doc({"doctype": PROVENANCE, "event_type": "run_launched", **fields})
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name


class TestProvenanceLedgerScoping(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(USER_A, ["Jarvis User"])
		_ensure_user(ADMIN_USER, ["Jarvis User", "Jarvis Admin"])
		# An event whose owner is a *foreign* user, carrying reviewer sign-off PII.
		cls.event = _mk_event(
			reviewing_human=ADMIN_USER,
			initiating_human=ADMIN_USER,
			detail=f"{PFX} reviewer-capacity justification (sensitive)",
			event_type="agent_promoted_to_live",
		)
		# Re-home the row owner to USER_A to prove even if_owner would have leaked.
		frappe.db.set_value(PROVENANCE, cls.event, "owner", USER_A, update_modified=False)

	def test_jarvis_user_cannot_list_provenance_events(self):
		with _as(USER_A):
			self.assertFalse(
				frappe.has_permission(PROVENANCE, "read", user=USER_A),
				"Jarvis User must have NO read grant on the provenance ledger",
			)
			with self.assertRaises(frappe.PermissionError):
				frappe.get_list(PROVENANCE, filters={"detail": ["like", f"{PFX}%"]}, pluck="name")

	def test_jarvis_user_cannot_open_a_provenance_event(self):
		with _as(USER_A):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(PROVENANCE, self.event).check_permission("read")

	def test_admin_tier_still_reads_the_ledger(self):
		with _as(ADMIN_USER):
			self.assertTrue(frappe.has_permission(PROVENANCE, "read", user=ADMIN_USER))
			names = set(frappe.get_list(PROVENANCE, filters={"detail": ["like", f"{PFX}%"]}, pluck="name"))
		self.assertIn(self.event, names)

	def test_system_manager_reads_the_ledger(self):
		with _as("Administrator"):
			names = set(frappe.get_list(PROVENANCE, filters={"detail": ["like", f"{PFX}%"]}, pluck="name"))
		self.assertIn(self.event, names)
