"""Unit tests for ``jarvis.tools._company_scope`` - the Company User
Permission scoping helper shared by ``get_customer_outstanding``,
``get_balance_on``, and ``get_party_dashboard_info``.

Exercises real ``get_user_permissions`` decisions (not a mocked one)
against a real ``User Permission`` row, so the fix (scope by Company User
Permission, not Company-doctype read) is proven end to end.
"""

from __future__ import annotations

import contextlib

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools._company_scope import assert_company_permitted, is_company_permitted

COMPANY_A = "_JPL Scope Company A"
COMPANY_B = "_JPL Scope Company B"
USER_SCOPED = "jpl-scope-company-a@example.com"


def _ensure_company(name: str, abbr: str) -> None:
	if frappe.db.exists("Company", name):
		return
	# Skip default chart-of-accounts / warehouse / tax-template creation -
	# this fixture only needs a Company row for permission checks, not a
	# functioning ledger, and CI sites may be missing the fixtures those
	# hooks depend on (e.g. Warehouse Type "Transit").
	frappe.local.flags.ignore_chart_of_accounts = True
	try:
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": name,
				"abbr": abbr,
				"default_currency": "INR",
				"country": "India",
			}
		).insert(ignore_permissions=True)
	finally:
		frappe.local.flags.ignore_chart_of_accounts = False


def _ensure_user(email: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	if "System Manager" in frappe.get_roles(email):
		user.remove_roles("System Manager")


def _ensure_user_permission(user: str, allow: str, for_value: str) -> None:
	if frappe.db.exists("User Permission", {"user": user, "allow": allow, "for_value": for_value}):
		return
	frappe.get_doc(
		{
			"doctype": "User Permission",
			"user": user,
			"allow": allow,
			"for_value": for_value,
		}
	).insert(ignore_permissions=True)


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


class TestCompanyScope(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# User is cheap, harmless to leave committed (reused idempotently
		# across runs, mirrors test_tier2a_erpnext_reads.py).
		_ensure_user(USER_SCOPED)
		frappe.db.commit()
		# Company/User Permission are NOT committed - vanish via
		# FrappeTestCase's automatic per-class rollback rather than
		# persist across test modules (mirrors CrossCompanyPermTestCase in
		# test_tier2a_erpnext_reads.py).
		_ensure_company(COMPANY_A, "JPLSCA")
		_ensure_company(COMPANY_B, "JPLSCB")
		_ensure_user_permission(USER_SCOPED, "Company", COMPANY_A)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_unrestricted_user_is_permitted_for_any_company(self):
		# No Company User Permission at all -> not company-restricted.
		with _as("Administrator"):
			self.assertTrue(is_company_permitted(COMPANY_A))
			self.assertTrue(is_company_permitted(COMPANY_B))
			assert_company_permitted(COMPANY_A)  # must not raise
			assert_company_permitted(COMPANY_B)  # must not raise

	def test_scoped_user_permitted_for_own_company(self):
		with _as(USER_SCOPED):
			self.assertTrue(is_company_permitted(COMPANY_A))
			assert_company_permitted(COMPANY_A)  # must not raise

	def test_scoped_user_denied_for_other_company(self):
		with _as(USER_SCOPED):
			self.assertFalse(is_company_permitted(COMPANY_B))
			with self.assertRaises(PermissionDeniedError):
				assert_company_permitted(COMPANY_B)

	def test_is_company_permitted_explicit_user_arg(self):
		# is_company_permitted takes an explicit `user` kwarg independent
		# of the current session (assert_company_permitted always checks
		# the session user).
		with _as("Administrator"):
			self.assertFalse(is_company_permitted(COMPANY_B, user=USER_SCOPED))
			self.assertTrue(is_company_permitted(COMPANY_A, user=USER_SCOPED))
