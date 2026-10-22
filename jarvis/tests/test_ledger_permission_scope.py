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
from jarvis.tools._ledger_access import gl_dimension_restrictions
from jarvis.tools.get_balance_on import get_balance_on
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

# Accounts User reads GL Entry; each user below is narrowed by one GL
# dimension User Permission (or none).
CUSTOMER = "_JLS Customer"
PROJECT = "_JLS Project"
ACCOUNT_ROOT_PL = "_JLS Expenses"
ACCOUNT_PL = "_JLS Rent"
ACCOUNT_ROOT_BS = "_JLS Assets"
ACCOUNT_BS = "_JLS Cash"
COST_CENTER_OTHER = "_JLS Other Cost Center"
USER_GL_PROJECT = "jls-gl-project@example.com"
USER_GL_COST_CENTER = "jls-gl-cost-center@example.com"
USER_GL_FREE = "jls-gl-free@example.com"


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


def _ensure_account(name: str, company: str, **kwargs) -> str:
	account = frappe.db.exists("Account", {"account_name": name, "company": company})
	if account:
		return account
	doc = frappe.get_doc({"doctype": "Account", "account_name": name, "company": company, **kwargs})
	# Roots have no parent; the fixture companies carry no chart of accounts.
	doc.insert(ignore_permissions=True, ignore_mandatory=True)
	return doc.name


def _ensure_cost_center(name: str, company: str) -> str:
	existing = frappe.db.exists("Cost Center", {"cost_center_name": name, "company": company})
	if existing:
		return existing
	root = frappe.db.get_value("Cost Center", {"company": company, "is_group": 1}, order_by="lft")
	doc = frappe.get_doc(
		{"doctype": "Cost Center", "cost_center_name": name, "company": company, "parent_cost_center": root}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_customer(name: str) -> str:
	existing = frappe.db.exists("Customer", {"customer_name": name})
	if existing:
		return existing
	return (
		frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_type": "Company"})
		.insert(ignore_permissions=True)
		.name
	)


def _ensure_project(name: str, company: str) -> str:
	existing = frappe.db.exists("Project", {"project_name": name})
	if existing:
		return existing
	return (
		frappe.get_doc({"doctype": "Project", "project_name": name, "company": company})
		.insert(ignore_permissions=True)
		.name
	)


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
		for email in (USER_GL_PROJECT, USER_GL_COST_CENTER, USER_GL_FREE):
			_ensure_user(email, ("Accounts User",))
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

		root_pl = _ensure_account(ACCOUNT_ROOT_PL, COMPANY_A, is_group=1, root_type="Expense")
		cls.account_pl = _ensure_account(ACCOUNT_PL, COMPANY_A, parent_account=root_pl, root_type="Expense")
		root_bs = _ensure_account(ACCOUNT_ROOT_BS, COMPANY_A, is_group=1, root_type="Asset")
		cls.account_bs = _ensure_account(ACCOUNT_BS, COMPANY_A, parent_account=root_bs, root_type="Asset")
		cls.cost_center = frappe.db.get_value("Cost Center", {"company": COMPANY_A, "is_group": 0})
		cls.cost_center_other = _ensure_cost_center(COST_CENTER_OTHER, COMPANY_A)
		cls.project = _ensure_project(PROJECT, COMPANY_A)
		cls.customer = _ensure_customer(CUSTOMER)
		_ensure_user_permission(USER_GL_PROJECT, "Project", cls.project)
		_ensure_user_permission(USER_GL_COST_CENTER, "Cost Center", cls.cost_center)

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


BALANCE_HELPER = "erpnext.accounts.utils.get_balance_on"


class TestGetBalanceOnGlDimensionScope(LedgerScopeTestCase):
	def test_preconditions_users_read_ledger(self):
		# Without GL Entry read the refusals below could pass for the wrong reason.
		for user in (USER_GL_PROJECT, USER_GL_COST_CENTER, USER_GL_FREE):
			self.assertTrue(frappe.has_permission("GL Entry", "read", user=user))
		self.assertEqual(frappe.db.get_value("Account", self.account_pl, "report_type"), "Profit and Loss")
		self.assertEqual(frappe.db.get_value("Account", self.account_bs, "report_type"), "Balance Sheet")

	def test_project_restricted_user_refused(self):
		with _as(USER_GL_PROJECT), patch(BALANCE_HELPER, return_value=10.0) as helper:
			with self.assertRaises(PermissionDeniedError):
				get_balance_on(account=self.account_pl, cost_center=self.cost_center)
			with self.assertRaises(PermissionDeniedError):
				get_balance_on(account=self.account_bs)
		helper.assert_not_called()

	def test_cost_center_restricted_user_refused_without_cost_center(self):
		with _as(USER_GL_COST_CENTER), patch(BALANCE_HELPER, return_value=10.0) as helper:
			with self.assertRaises(PermissionDeniedError):
				get_balance_on(account=self.account_pl)
		helper.assert_not_called()

	def test_cost_center_restricted_user_refused_for_other_cost_center(self):
		with _as(USER_GL_COST_CENTER), patch(BALANCE_HELPER, return_value=10.0) as helper:
			with self.assertRaises(PermissionDeniedError):
				get_balance_on(account=self.account_pl, cost_center=self.cost_center_other)
		helper.assert_not_called()

	def test_cost_center_restricted_user_refused_where_cost_center_is_ignored(self):
		# ERPNext applies cost_center to Profit and Loss accounts only, so a
		# Balance Sheet account or a party balance would still sum every row.
		with _as(USER_GL_COST_CENTER), patch(BALANCE_HELPER, return_value=10.0) as helper:
			with self.assertRaises(PermissionDeniedError):
				get_balance_on(account=self.account_bs, cost_center=self.cost_center)
			with self.assertRaises(PermissionDeniedError):
				get_balance_on(party_type="Customer", party=self.customer, cost_center=self.cost_center)
		helper.assert_not_called()

	def test_cost_center_restricted_user_allowed_for_permitted_cost_center(self):
		with _as(USER_GL_COST_CENTER), patch(BALANCE_HELPER, return_value=10.0) as helper:
			out = get_balance_on(account=self.account_pl, cost_center=self.cost_center)
		self.assertEqual(out["balance"], 10.0)
		self.assertEqual(helper.call_args.kwargs["cost_center"], self.cost_center)

	def test_unrestricted_user_unchanged(self):
		with _as(USER_GL_FREE), patch(BALANCE_HELPER, return_value=10.0) as helper:
			out = get_balance_on(account=self.account_bs)
			get_balance_on(account=self.account_pl, cost_center=self.cost_center_other)
		self.assertEqual(out["balance"], 10.0)
		self.assertEqual(helper.call_count, 2)

	def test_restrictions_cover_project_cost_center_and_branch(self):
		perms = frappe._dict(
			{
				"Cost Center": [frappe._dict(doc="CC-1", applicable_for=None)],
				"Project": [frappe._dict(doc="P-1", applicable_for="GL Entry")],
				"Branch": [frappe._dict(doc="B-1", applicable_for=None)],
				"Customer": [frappe._dict(doc="C-1", applicable_for=None)],
			}
		)
		with patch("jarvis.tools._ledger_access.get_user_permissions", return_value=perms):
			self.assertEqual(
				gl_dimension_restrictions("anyone@example.com"),
				{"Cost Center": {"CC-1"}, "Project": {"P-1"}, "Branch": {"B-1"}},
			)

	def test_restriction_for_another_doctype_does_not_count(self):
		perms = frappe._dict({"Project": [frappe._dict(doc="P-1", applicable_for="Task")]})
		with patch("jarvis.tools._ledger_access.get_user_permissions", return_value=perms):
			self.assertEqual(gl_dimension_restrictions("anyone@example.com"), {})

	def test_branch_restricted_user_refused(self):
		perms = frappe._dict({"Branch": [frappe._dict(doc="B-1", applicable_for=None)]})
		with (
			_as(USER_GL_FREE),
			patch("jarvis.tools._ledger_access.get_user_permissions", return_value=perms),
			patch(BALANCE_HELPER, return_value=10.0) as helper,
			self.assertRaises(PermissionDeniedError),
		):
			get_balance_on(account=self.account_pl, cost_center=self.cost_center)
		helper.assert_not_called()
