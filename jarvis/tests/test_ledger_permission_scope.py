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
from jarvis.tools._ledger_access import gl_dimension_doctypes, gl_dimension_restrictions
from jarvis.tools.get_balance_on import get_balance_on
from jarvis.tools.get_customer_outstanding import get_customer_outstanding
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
PROJECT_2 = "_JLS Project 2"
ACCOUNT_ROOT_PL = "_JLS Expenses"
ACCOUNT_PL = "_JLS Rent"
ACCOUNT_ROOT_BS = "_JLS Assets"
ACCOUNT_BS = "_JLS Cash"
COST_CENTER_OTHER = "_JLS Other Cost Center"
COST_CENTER_GROUP = "_JLS Group Cost Center"
COST_CENTER_CHILD_1 = "_JLS Group Child 1"
COST_CENTER_CHILD_2 = "_JLS Group Child 2"
USER_GL_PROJECT = "jls-gl-project@example.com"
USER_GL_COST_CENTER = "jls-gl-cost-center@example.com"
USER_GL_FREE = "jls-gl-free@example.com"
USER_GL_GROUP = "jls-gl-group@example.com"
USER_GL_GROUP_HIDDEN = "jls-gl-group-hidden@example.com"
USER_OUTSTANDING_PROJECT = "jls-outstanding-project@example.com"
# Accounts Manager reads Customer and GL Entry but neither Sales Order nor
# Delivery Note; Auditor + Sales Manager reads Sales Order but not Delivery Note.
USER_OUTSTANDING_NO_ORDERS = "jls-outstanding-no-orders@example.com"
USER_OUTSTANDING_NO_NOTES = "jls-outstanding-no-notes@example.com"


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


def _ensure_cost_center(name: str, company: str, parent: str | None = None, is_group: int = 0) -> str:
	existing = frappe.db.exists("Cost Center", {"cost_center_name": name, "company": company})
	if existing:
		return existing
	root = frappe.db.get_value("Cost Center", {"company": company, "is_group": 1}, order_by="lft")
	doc = frappe.get_doc(
		{
			"doctype": "Cost Center",
			"cost_center_name": name,
			"company": company,
			"parent_cost_center": parent or root,
			"is_group": is_group,
		}
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


def _ensure_user_permission(user: str, allow: str, for_value: str, hide_descendants: int = 0) -> None:
	if frappe.db.exists("User Permission", {"user": user, "allow": allow, "for_value": for_value}):
		return
	frappe.get_doc(
		{
			"doctype": "User Permission",
			"user": user,
			"allow": allow,
			"for_value": for_value,
			"hide_descendants": hide_descendants,
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


class LedgerScopeTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# Users are cheap and reused idempotently; everything else rolls back.
		for email in (USER_COMPANY_A, USER_WAREHOUSE_A1, USER_ITEM_1, USER_UNRESTRICTED):
			_ensure_user(email, ("Stock User",))
		for email in (
			USER_GL_PROJECT,
			USER_GL_COST_CENTER,
			USER_GL_FREE,
			USER_GL_GROUP,
			USER_GL_GROUP_HIDDEN,
			USER_OUTSTANDING_PROJECT,
		):
			_ensure_user(email, ("Accounts User",))
		_ensure_user(USER_OUTSTANDING_NO_ORDERS, ("Accounts Manager",))
		_ensure_user(USER_OUTSTANDING_NO_NOTES, ("Auditor", "Sales Manager"))
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
		cls.cc_group = _ensure_cost_center(COST_CENTER_GROUP, COMPANY_A, is_group=1)
		cls.cc_child_1 = _ensure_cost_center(COST_CENTER_CHILD_1, COMPANY_A, parent=cls.cc_group)
		cls.cc_child_2 = _ensure_cost_center(COST_CENTER_CHILD_2, COMPANY_A, parent=cls.cc_group)
		# Group permitted with its descendants (Frappe's default) vs. the group row alone.
		_ensure_user_permission(USER_GL_GROUP, "Cost Center", cls.cc_group)
		_ensure_user_permission(USER_GL_GROUP_HIDDEN, "Cost Center", cls.cc_group, hide_descendants=1)

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
ACCOUNTING_DIMENSIONS = (
	"erpnext.accounts.doctype.accounting_dimension.accounting_dimension.get_accounting_dimensions"
)


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

	def test_group_cost_center_allowed_when_whole_subtree_permitted(self):
		with _as(USER_GL_GROUP), patch(BALANCE_HELPER, return_value=10.0) as helper:
			get_balance_on(account=self.account_pl, cost_center=self.cc_group)
			get_balance_on(account=self.account_pl, cost_center=self.cc_child_1)
		self.assertEqual(helper.call_args_list[0].kwargs["cost_center"], self.cc_group)

	def test_group_cost_center_refused_when_descendants_not_permitted(self):
		# Permission on the group row only (hide_descendants): ERPNext would
		# still sum the whole subtree for the group.
		with _as(USER_GL_GROUP_HIDDEN), patch(BALANCE_HELPER, return_value=10.0) as helper:
			with self.assertRaises(PermissionDeniedError):
				get_balance_on(account=self.account_pl, cost_center=self.cc_group)
		helper.assert_not_called()

	def test_restrictions_use_accounting_dimensions_and_gl_branch_link(self):
		perms = frappe._dict(
			{
				"Cost Center": [frappe._dict(doc="CC-1", applicable_for=None)],
				"Project": [frappe._dict(doc="P-1", applicable_for="GL Entry")],
				"Branch": [frappe._dict(doc="B-1", applicable_for=None)],
				"Department": [frappe._dict(doc="D-1", applicable_for=None)],
				"Customer": [frappe._dict(doc="C-1", applicable_for=None)],
			}
		)
		dimensions = [frappe._dict(document_type="Department"), frappe._dict(document_type="Branch")]
		with (
			patch("jarvis.tools._ledger_access.get_user_permissions", return_value=perms),
			patch(ACCOUNTING_DIMENSIONS, return_value=dimensions),
		):
			self.assertEqual(
				gl_dimension_restrictions("anyone@example.com"),
				{
					"Cost Center": {"CC-1"},
					"Project": {"P-1"},
					"Branch": {"B-1"},
					"Department": {"D-1"},
				},
			)

	def test_branch_counts_only_where_gl_entry_links_it(self):
		on_site = any(f.options == "Branch" for f in frappe.get_meta("GL Entry").get_link_fields())
		with patch(ACCOUNTING_DIMENSIONS, return_value=[]):
			doctypes = gl_dimension_doctypes()
		self.assertEqual(doctypes - {"Branch"}, {"Cost Center", "Project"})
		self.assertEqual("Branch" in doctypes, on_site)
		with patch(ACCOUNTING_DIMENSIONS, return_value=[frappe._dict(document_type="Branch")]):
			self.assertIn("Branch", gl_dimension_doctypes())

	def test_restriction_for_another_doctype_does_not_count(self):
		perms = frappe._dict({"Project": [frappe._dict(doc="P-1", applicable_for="Task")]})
		with patch("jarvis.tools._ledger_access.get_user_permissions", return_value=perms):
			self.assertEqual(gl_dimension_restrictions("anyone@example.com"), {})

	def test_accounting_dimension_restricted_user_refused(self):
		perms = frappe._dict({"Department": [frappe._dict(doc="D-1", applicable_for=None)]})
		with (
			_as(USER_GL_FREE),
			patch("jarvis.tools._ledger_access.get_user_permissions", return_value=perms),
			patch(ACCOUNTING_DIMENSIONS, return_value=[frappe._dict(document_type="Department")]),
			patch(BALANCE_HELPER, return_value=10.0) as helper,
			self.assertRaises(PermissionDeniedError),
		):
			get_balance_on(account=self.account_pl, cost_center=self.cc_group)
		helper.assert_not_called()

	def test_accounting_dimension_with_cost_center_restriction_refused(self):
		# A Cost Center restriction does not excuse another dimension's.
		perms = frappe._dict(
			{
				"Cost Center": [frappe._dict(doc=self.cost_center, applicable_for=None)],
				"Department": [frappe._dict(doc="D-1", applicable_for=None)],
			}
		)
		with (
			_as(USER_GL_FREE),
			patch("jarvis.tools._ledger_access.get_user_permissions", return_value=perms),
			patch(ACCOUNTING_DIMENSIONS, return_value=[frappe._dict(document_type="Department")]),
			patch(BALANCE_HELPER, return_value=10.0) as helper,
			self.assertRaises(PermissionDeniedError),
		):
			get_balance_on(account=self.account_pl, cost_center=self.cost_center)
		helper.assert_not_called()

	def test_branch_restricted_user_refused(self):
		perms = frappe._dict({"Branch": [frappe._dict(doc="B-1", applicable_for=None)]})
		with (
			_as(USER_GL_FREE),
			patch("jarvis.tools._ledger_access.get_user_permissions", return_value=perms),
			patch(ACCOUNTING_DIMENSIONS, return_value=[frappe._dict(document_type="Branch")]),
			patch(BALANCE_HELPER, return_value=10.0) as helper,
			self.assertRaises(PermissionDeniedError),
		):
			get_balance_on(account=self.account_pl, cost_center=self.cost_center)
		helper.assert_not_called()


class TestGetCustomerOutstandingReadableDocuments(LedgerScopeTestCase):
	"""The Sales Order and Delivery Note parts count only documents the
	caller can read; an unrestricted caller gets ERPNext's own number.

	Documents are written with db_insert (no submit flow, so no items,
	accounts or stock ledger are needed); amounts:

	  GL            debit 5000 / credit 1000 (cost center A), debit 300 (cost center B)
	  SO-1  P1      1000 x 75% unbilled = 750
	  SO-2  P2       400 x 100% unbilled = 400
	  SO closed / fully billed: excluded
	  DN-1  P1      600 item (6 qty), 2 qty billed -> 4 / 6 x 600 / 600 x 708 = 472
	  DN-2  P2      300 item (3 qty), 1 qty returned -> 2 / 3 x 300 / 300 x 354 = 236
	  DN item against a Sales Order: excluded

	ERPNext prices a Delivery Note by quantity (net of billed and returned stock
	quantity), so the rows carry stock_qty / base_net_amount, the billing
	Sales Invoice exists, and a row with quantity but no billing is not zero.
	"""

	GL_TOTAL = 4300.0
	GL_COST_CENTER_A = 4000.0
	SO_P1 = 750.0
	SO_ALL = 1150.0
	DN_P1 = 472.0
	DN_ALL = 708.0

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.customer = _ensure_customer(CUSTOMER)
		cls.cc_a = frappe.db.get_value("Cost Center", {"company": COMPANY_A, "is_group": 0})
		cls.cc_b = _ensure_cost_center(COST_CENTER_OTHER, COMPANY_A)
		cls.p1 = _ensure_project(PROJECT, COMPANY_A)
		cls.p2 = _ensure_project(PROJECT_2, COMPANY_A)
		_ensure_user_permission(USER_OUTSTANDING_PROJECT, "Project", cls.p1)
		cls._insert_ledger_and_documents()

	@classmethod
	def _insert(cls, doctype: str, name: str, **values) -> None:
		frappe.get_doc({"doctype": doctype, "name": name, **values}).db_insert()

	@classmethod
	def _insert_ledger_and_documents(cls):
		gl = {
			"party_type": "Customer",
			"party": cls.customer,
			"company": COMPANY_A,
			"is_cancelled": 0,
			"docstatus": 1,
			"posting_date": frappe.utils.today(),
		}
		cls._insert("GL Entry", "_JLS-GLE-1", debit=5000, credit=0, cost_center=cls.cc_a, **gl)
		cls._insert("GL Entry", "_JLS-GLE-2", debit=0, credit=1000, cost_center=cls.cc_a, **gl)
		cls._insert("GL Entry", "_JLS-GLE-3", debit=300, credit=0, cost_center=cls.cc_b, **gl)
		cls._insert(
			"GL Entry", "_JLS-GLE-4", debit=999, credit=0, cost_center=cls.cc_a, **{**gl, "is_cancelled": 1}
		)

		so = {"customer": cls.customer, "company": COMPANY_A, "docstatus": 1}
		cls._insert(
			"Sales Order",
			"_JLS-SO-1",
			base_grand_total=1000,
			per_billed=25,
			status="To Bill",
			project=cls.p1,
			**so,
		)
		cls._insert(
			"Sales Order",
			"_JLS-SO-2",
			base_grand_total=400,
			per_billed=0,
			status="To Deliver and Bill",
			project=cls.p2,
			**so,
		)
		cls._insert(
			"Sales Order",
			"_JLS-SO-3",
			base_grand_total=900,
			per_billed=0,
			status="Closed",
			project=cls.p1,
			**so,
		)
		cls._insert(
			"Sales Order",
			"_JLS-SO-4",
			base_grand_total=900,
			per_billed=100,
			status="Completed",
			project=cls.p1,
			**so,
		)

		dn = {"customer": cls.customer, "company": COMPANY_A, "docstatus": 1, "status": "To Bill"}
		cls._insert(
			"Delivery Note", "_JLS-DN-1", base_net_total=600, base_grand_total=708, project=cls.p1, **dn
		)
		cls._insert(
			"Delivery Note", "_JLS-DN-2", base_net_total=300, base_grand_total=354, project=cls.p2, **dn
		)
		cls._insert(
			"Delivery Note",
			"_JLS-DN-3",
			base_net_total=500,
			base_grand_total=590,
			project=cls.p1,
			**{**dn, "status": "Closed"},
		)

		def dn_item(name, parent, amount, stock_qty, **extra):
			cls._insert(
				"Delivery Note Item",
				name,
				parent=parent,
				parenttype="Delivery Note",
				parentfield="items",
				amount=amount,
				base_net_amount=amount,
				stock_qty=stock_qty,
				docstatus=1,
				**extra,
			)

		dn_item("_JLS-DNI-1", "_JLS-DN-1", 600, 6)
		dn_item("_JLS-DNI-2", "_JLS-DN-2", 300, 3, returned_qty=1)
		dn_item("_JLS-DNI-3", "_JLS-DN-1", 250, 5, against_sales_order="_JLS-SO-1")
		dn_item("_JLS-DNI-4", "_JLS-DN-3", 500, 5)
		cls._insert(
			"Sales Invoice",
			"_JLS-SI-1",
			customer=cls.customer,
			company=COMPANY_A,
			docstatus=1,
			is_return=0,
		)
		cls._insert(
			"Sales Invoice Item",
			"_JLS-SII-1",
			parent="_JLS-SI-1",
			parenttype="Sales Invoice",
			parentfield="items",
			dn_detail="_JLS-DNI-1",
			amount=200,
			stock_qty=2,
			docstatus=1,
		)

	def _erpnext(self, **kwargs) -> float:
		from erpnext.selling.doctype.customer.customer import get_customer_outstanding as erpnext_gco

		return erpnext_gco(customer=self.customer, company=COMPANY_A, **kwargs)

	def test_preconditions_user_reads_customer_ledger_and_only_p1_documents(self):
		user = USER_OUTSTANDING_PROJECT
		self.assertTrue(frappe.has_permission("Customer", "read", doc=self.customer, user=user))
		self.assertTrue(frappe.has_permission("GL Entry", "read", user=user))
		for doctype, readable, hidden in (
			("Sales Order", "_JLS-SO-1", "_JLS-SO-2"),
			("Delivery Note", "_JLS-DN-1", "_JLS-DN-2"),
		):
			self.assertTrue(frappe.has_permission(doctype, "read", doc=readable, user=user))
			self.assertFalse(frappe.has_permission(doctype, "read", doc=hidden, user=user))

	def test_unrestricted_user_matches_erpnext(self):
		with _as("Administrator"):
			for kwargs in (
				{},
				{"ignore_outstanding_sales_order": True},
				{"cost_center": self.cc_a},
				{"cost_center": self.cc_a, "ignore_outstanding_sales_order": True},
			):
				got = get_customer_outstanding(self.customer, COMPANY_A, **kwargs)["outstanding"]
				self.assertAlmostEqual(got, self._erpnext(**kwargs), places=4, msg=str(kwargs))

	def test_unrestricted_user_total(self):
		with _as("Administrator"):
			out = get_customer_outstanding(self.customer, COMPANY_A)
		self.assertAlmostEqual(out["outstanding"], self.GL_TOTAL + self.SO_ALL + self.DN_ALL, places=4)

	def test_restricted_user_counts_only_readable_orders_and_notes(self):
		with _as(USER_OUTSTANDING_PROJECT):
			out = get_customer_outstanding(self.customer, COMPANY_A)
		self.assertAlmostEqual(out["outstanding"], self.GL_TOTAL + self.SO_P1 + self.DN_P1, places=4)

	def test_restricted_user_ignore_sales_order_and_cost_center_still_work(self):
		with _as(USER_OUTSTANDING_PROJECT):
			no_so = get_customer_outstanding(self.customer, COMPANY_A, ignore_outstanding_sales_order=True)[
				"outstanding"
			]
			cc_a = get_customer_outstanding(self.customer, COMPANY_A, cost_center=self.cc_a)["outstanding"]
		self.assertAlmostEqual(no_so, self.GL_TOTAL + self.DN_P1, places=4)
		self.assertAlmostEqual(cc_a, self.GL_COST_CENTER_A + self.SO_P1 + self.DN_P1, places=4)

	def test_user_without_order_or_note_read_gets_the_gl_outstanding(self):
		user = USER_OUTSTANDING_NO_ORDERS
		self.assertTrue(frappe.has_permission("Customer", "read", doc=self.customer, user=user))
		self.assertTrue(frappe.has_permission("GL Entry", "read", user=user))
		self.assertFalse(frappe.has_permission("Sales Order", "read", user=user))
		self.assertFalse(frappe.has_permission("Delivery Note", "read", user=user))
		with _as(user):
			out = get_customer_outstanding(self.customer, COMPANY_A)
		self.assertAlmostEqual(out["outstanding"], self.GL_TOTAL, places=4)

	def test_user_without_note_read_still_gets_sales_orders(self):
		user = USER_OUTSTANDING_NO_NOTES
		self.assertTrue(frappe.has_permission("Sales Order", "read", user=user))
		self.assertFalse(frappe.has_permission("Delivery Note", "read", user=user))
		with _as(user):
			out = get_customer_outstanding(self.customer, COMPANY_A)
		self.assertAlmostEqual(out["outstanding"], self.GL_TOTAL + self.SO_ALL, places=4)
