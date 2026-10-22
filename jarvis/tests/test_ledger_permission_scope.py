"""Permission-scope tests for the stock and ledger tools:
get_stock_balance, get_valuation_rate, get_balance_on and
get_customer_outstanding.

Real users, roles and User Permissions against real records (the ERPNext
helpers are patched where a test only needs to know whether they were
reached). Company, Item, Warehouse and User Permission rows are not
committed, so FrappeTestCase's per-class rollback removes them.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools.get_stock_balance import get_stock_balance
from jarvis.tools.get_valuation_rate import get_valuation_rate

COMPANY_A = "_JLS Company A"
COMPANY_B = "_JLS Company B"
ITEM_GROUP = "_JLS Item Group"
UOM = "_JLS Nos"
ITEM_1 = "_JLS Item 1"
ITEM_2 = "_JLS Item 2"
WH_A1 = "_JLS Warehouse A1"
WH_A2 = "_JLS Warehouse A2"
WH_B = "_JLS Warehouse B"

# Stock User reads Item and Warehouse by role; each user below is then
# narrowed by exactly one User Permission.
USER_COMPANY_A = "jls-company-a@example.com"
USER_WAREHOUSE_A1 = "jls-warehouse-a1@example.com"
USER_ITEM_1 = "jls-item-1@example.com"
USER_UNRESTRICTED = "jls-unrestricted@example.com"


def _ensure_user(email: str, roles: tuple) -> None:
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
	missing = [r for r in roles if r not in frappe.get_roles(email)]
	if missing:
		user.add_roles(*missing)


def _ensure_company(name: str, abbr: str) -> None:
	if frappe.db.exists("Company", name):
		return
	# No chart of accounts and no perpetual inventory: the fixture only needs
	# a Company row for permission checks (same as test_tier2a_erpnext_reads).
	frappe.local.flags.ignore_chart_of_accounts = True
	try:
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": name,
				"abbr": abbr,
				"default_currency": "INR",
				"country": "India",
				"enable_perpetual_inventory": 0,
			}
		).insert(ignore_permissions=True)
		if frappe.db.get_value("Company", name, "enable_perpetual_inventory"):
			frappe.db.set_value("Company", name, "enable_perpetual_inventory", 0)
	finally:
		frappe.local.flags.ignore_chart_of_accounts = False


def _ensure_item(name: str) -> None:
	if frappe.db.exists("Item", name):
		return
	if not frappe.db.exists("Item Group", ITEM_GROUP):
		frappe.get_doc({"doctype": "Item Group", "item_group_name": ITEM_GROUP, "is_group": 0}).insert(
			ignore_permissions=True
		)
	if not frappe.db.exists("UOM", UOM):
		frappe.get_doc({"doctype": "UOM", "uom_name": UOM}).insert(ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": name,
			"item_name": name,
			"item_group": ITEM_GROUP,
			"stock_uom": UOM,
		}
	).insert(ignore_permissions=True)


def _ensure_warehouse(name: str, company: str) -> str:
	existing = frappe.db.exists("Warehouse", {"warehouse_name": name, "company": company})
	if existing:
		return existing
	doc = frappe.get_doc({"doctype": "Warehouse", "warehouse_name": name, "company": company})
	# The fixture companies have no chart of accounts (see _ensure_company).
	doc.flags.ignore_inventory_account_validation = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_user_permission(user: str, allow: str, for_value: str) -> None:
	if frappe.db.exists("User Permission", {"user": user, "allow": allow, "for_value": for_value}):
		return
	frappe.get_doc(
		{"doctype": "User Permission", "user": user, "allow": allow, "for_value": for_value}
	).insert(ignore_permissions=True)


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


class LedgerScopeTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# Users are cheap and reused idempotently; everything else rolls back.
		for email in (USER_COMPANY_A, USER_WAREHOUSE_A1, USER_ITEM_1, USER_UNRESTRICTED):
			_ensure_user(email, ("Stock User",))
		frappe.db.commit()
		_ensure_company(COMPANY_A, "JLSA")
		_ensure_company(COMPANY_B, "JLSB")
		_ensure_item(ITEM_1)
		_ensure_item(ITEM_2)
		cls.wh_a1 = _ensure_warehouse(WH_A1, COMPANY_A)
		cls.wh_a2 = _ensure_warehouse(WH_A2, COMPANY_A)
		cls.wh_b = _ensure_warehouse(WH_B, COMPANY_B)
		_ensure_user_permission(USER_COMPANY_A, "Company", COMPANY_A)
		_ensure_user_permission(USER_WAREHOUSE_A1, "Warehouse", cls.wh_a1)
		_ensure_user_permission(USER_ITEM_1, "Item", ITEM_1)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()


STOCK_HELPER = "erpnext.stock.utils.get_stock_balance"
VALUATION_HELPER = "erpnext.stock.get_item_details.get_valuation_rate"


class TestGetStockBalanceScope(LedgerScopeTestCase):
	def test_company_scoped_user_denied_for_other_company_warehouse(self):
		with _as(USER_COMPANY_A), patch(STOCK_HELPER, return_value=5.0) as helper:
			with self.assertRaises(PermissionDeniedError):
				get_stock_balance(ITEM_1, self.wh_b)
		helper.assert_not_called()

	def test_company_scoped_user_allowed_for_own_company_warehouse(self):
		with _as(USER_COMPANY_A), patch(STOCK_HELPER, return_value=5.0):
			self.assertEqual(get_stock_balance(ITEM_1, self.wh_a1), {"qty": 5.0})

	def test_warehouse_user_permission_denies_other_warehouse(self):
		with _as(USER_WAREHOUSE_A1), patch(STOCK_HELPER, return_value=5.0) as helper:
			with self.assertRaises(PermissionDeniedError):
				get_stock_balance(ITEM_1, self.wh_a2)
		helper.assert_not_called()

	def test_warehouse_user_permission_allows_permitted_warehouse(self):
		with _as(USER_WAREHOUSE_A1), patch(STOCK_HELPER, return_value=5.0):
			self.assertEqual(get_stock_balance(ITEM_1, self.wh_a1), {"qty": 5.0})

	def test_item_user_permission_denies_other_item(self):
		# ERPNext's own check is doctype-level, so an Item User Permission
		# was not applied before the wrapper checked the Item document.
		with _as(USER_ITEM_1), patch(STOCK_HELPER, return_value=5.0) as helper:
			with self.assertRaises(PermissionDeniedError):
				get_stock_balance(ITEM_2, self.wh_a1)
		helper.assert_not_called()

	def test_unrestricted_user_reads_any_warehouse(self):
		with _as(USER_UNRESTRICTED), patch(STOCK_HELPER, return_value=5.0):
			self.assertEqual(get_stock_balance(ITEM_2, self.wh_b), {"qty": 5.0})


class TestGetValuationRateScope(LedgerScopeTestCase):
	def test_company_scoped_user_denied_for_other_company(self):
		with _as(USER_COMPANY_A), patch(VALUATION_HELPER, return_value=7.0) as helper:
			with self.assertRaises(PermissionDeniedError):
				get_valuation_rate(ITEM_1, COMPANY_B)
		helper.assert_not_called()

	def test_company_scoped_user_denied_for_other_company_warehouse(self):
		with _as(USER_COMPANY_A), patch(VALUATION_HELPER, return_value=7.0) as helper:
			with self.assertRaises(PermissionDeniedError):
				get_valuation_rate(ITEM_1, COMPANY_A, self.wh_b)
		helper.assert_not_called()

	def test_company_scoped_user_allowed_for_own_company(self):
		with _as(USER_COMPANY_A), patch(VALUATION_HELPER, return_value=7.0):
			self.assertEqual(get_valuation_rate(ITEM_1, COMPANY_A), {"rate": 7.0})
			self.assertEqual(get_valuation_rate(ITEM_1, COMPANY_A, self.wh_a1), {"rate": 7.0})

	def test_warehouse_user_permission_denies_other_warehouse(self):
		with _as(USER_WAREHOUSE_A1), patch(VALUATION_HELPER, return_value=7.0) as helper:
			with self.assertRaises(PermissionDeniedError):
				get_valuation_rate(ITEM_1, COMPANY_A, self.wh_a2)
		helper.assert_not_called()

	def test_unrestricted_user_reads_any_company(self):
		with _as(USER_UNRESTRICTED), patch(VALUATION_HELPER, return_value=7.0):
			self.assertEqual(get_valuation_rate(ITEM_1, COMPANY_B, self.wh_b), {"rate": 7.0})

	def test_warehouse_restricted_user_must_name_a_warehouse(self):
		with _as(USER_WAREHOUSE_A1), patch(VALUATION_HELPER, return_value=7.0) as helper:
			with self.assertRaises(PermissionDeniedError):
				get_valuation_rate(ITEM_1, COMPANY_A)
		helper.assert_not_called()

	def test_warehouse_restricted_user_allowed_with_permitted_warehouse(self):
		with _as(USER_WAREHOUSE_A1), patch(VALUATION_HELPER, return_value=7.0):
			self.assertEqual(get_valuation_rate(ITEM_1, COMPANY_A, self.wh_a1), {"rate": 7.0})

	def test_unrestricted_user_needs_no_warehouse(self):
		with _as(USER_UNRESTRICTED), patch(VALUATION_HELPER, return_value=7.0):
			self.assertEqual(get_valuation_rate(ITEM_1, COMPANY_A), {"rate": 7.0})

	def test_company_scope_alone_does_not_require_a_warehouse(self):
		with _as(USER_COMPANY_A), patch(VALUATION_HELPER, return_value=7.0):
			self.assertEqual(get_valuation_rate(ITEM_1, COMPANY_A), {"rate": 7.0})
