"""Tests for the Jarvis app-access gate (``jarvis.permissions``).

Reaching Jarvis at all requires the ``Jarvis User`` role, with ``System
Manager`` always allowed and ``Administrator`` implicitly allowed. These
tests exercise the single source of truth (``has_jarvis_access`` /
``require_jarvis_access``) plus one representative gated chat endpoint
(``list_conversations``) so a dropped ``require_jarvis_access()`` call
surfaces here.

Hermetic: throwaway ``System User`` rows (one per role shape) are created
in ``setUp`` and deleted in ``tearDown``; the ``Jarvis User`` role is
seeded idempotently and dropped again only if this test created it.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.api import list_conversations
from jarvis.permissions import (
	JARVIS_ACCESS_ROLES,
	JARVIS_USER_ROLE,
	has_jarvis_access,
	require_jarvis_access,
)

JARVIS_ROLE = JARVIS_USER_ROLE

# One throwaway user per role shape under test.
USER_JARVIS = "jarvis-access-juser@example.test"
USER_SM = "jarvis-access-sm@example.test"
USER_NONE = "jarvis-access-none@example.test"


def _ensure_role() -> bool:
	"""Seed the ``Jarvis User`` role if absent. Returns True iff we created it
	(so tearDown only drops a role this test introduced)."""
	if frappe.db.exists("Role", JARVIS_ROLE):
		return False
	frappe.get_doc(
		{
			"doctype": "Role",
			"role_name": JARVIS_ROLE,
			"desk_access": 1,
			"is_custom": 1,
		}
	).insert(ignore_permissions=True)
	return True


def _ensure_user(email: str, roles: tuple[str, ...] = ()) -> None:
	"""Create a disposable enabled System User (idempotent) and attach ``roles``."""
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Jarvis",
				"last_name": "AccessTest",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	if roles:
		frappe.get_doc("User", email).add_roles(*roles)


def _delete_user(email: str) -> None:
	# Drop any conversations the user owns first, then the user row itself.
	for conv in frappe.get_all("Jarvis Conversation", filters={"owner": email}, pluck="name"):
		for msg in frappe.get_all("Jarvis Chat Message", filters={"conversation": conv}, pluck="name"):
			frappe.delete_doc("Jarvis Chat Message", msg, ignore_permissions=True, force=True)
		frappe.delete_doc("Jarvis Conversation", conv, ignore_permissions=True, force=True)
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, ignore_permissions=True, force=True)


class TestJarvisAccessGate(FrappeTestCase):
	def setUp(self):
		self._orig_user = frappe.session.user
		self._created_role = _ensure_role()
		_ensure_user(USER_JARVIS, (JARVIS_ROLE,))
		_ensure_user(USER_SM, ("System Manager",))
		_ensure_user(USER_NONE, ())
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user(self._orig_user)
		for email in (USER_JARVIS, USER_SM, USER_NONE):
			_delete_user(email)
		if self._created_role and frappe.db.exists("Role", JARVIS_ROLE):
			frappe.delete_doc("Role", JARVIS_ROLE, ignore_permissions=True, force=True)
		frappe.db.commit()

	# --- (a) has_jarvis_access ------------------------------------------- #

	def test_has_access_for_jarvis_user(self):
		self.assertTrue(has_jarvis_access(USER_JARVIS))

	def test_has_access_for_system_manager(self):
		self.assertTrue(has_jarvis_access(USER_SM))

	def test_has_access_for_administrator(self):
		# Administrator is explicitly allowed even though it holds neither role.
		self.assertTrue(has_jarvis_access("Administrator"))

	def test_no_access_for_roleless_user(self):
		# A plain enabled System User with neither Jarvis User nor System Manager.
		self.assertFalse(has_jarvis_access(USER_NONE))
		# Sanity: the fixture user really lacks both access roles.
		self.assertFalse(set(JARVIS_ACCESS_ROLES) & set(frappe.get_roles(USER_NONE)))

	def test_defaults_to_session_user(self):
		frappe.set_user(USER_JARVIS)
		self.assertTrue(has_jarvis_access())
		frappe.set_user(USER_NONE)
		self.assertFalse(has_jarvis_access())

	# --- (b) a gated chat endpoint --------------------------------------- #

	def test_gated_endpoint_blocks_roleless_user(self):
		frappe.set_user(USER_NONE)
		with self.assertRaises(frappe.PermissionError):
			list_conversations()

	def test_gated_endpoint_allows_jarvis_user(self):
		frappe.set_user(USER_JARVIS)
		# The prewarm side-effect is irrelevant to the gate; stub it so the test
		# asserts only that access is granted and the endpoint returns its rows.
		with patch("jarvis.chat.prewarm.enqueue_warm_if_due"):
			rows = list_conversations()
		self.assertIsInstance(rows, list)

	# --- (c) require_jarvis_access --------------------------------------- #

	def test_require_raises_for_roleless_user(self):
		frappe.set_user(USER_NONE)
		with self.assertRaises(frappe.PermissionError):
			require_jarvis_access()

	def test_require_passes_for_jarvis_user(self):
		frappe.set_user(USER_JARVIS)
		# Must not raise.
		require_jarvis_access()
