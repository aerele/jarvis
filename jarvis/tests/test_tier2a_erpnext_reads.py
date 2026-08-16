"""Validation + envelope tests for the Tier-2a ERPNext computed-read
tools: get_stock_balance, get_valuation_rate, scan_barcode,
get_balance_on, get_customer_outstanding, get_party_dashboard_info,
get_exchange_rate, get_fiscal_year, get_itemised_tax_breakup.

These tools wrap ERPNext helpers that depend on fully-configured
company / chart-of-accounts / item master / fiscal year records.
Seeding all of that for unit tests is heavy and brittle (the master-
data defaults differ across ERPNext releases). The tests here pin
the boundary contract - arg validation, envelope shape, perm gating -
by patching:

  - ``frappe.db.exists`` for the existence checks (so tests don't
    care whether the bench is seeded)
  - the underlying ERPNext helper itself (so we assert the wrapper
    forwards args + reshapes the return correctly)

The ERPNext computations themselves are tested upstream.
"""

from __future__ import annotations

import contextlib
from datetime import date as _date
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.get_balance_on import get_balance_on
from jarvis.tools.get_customer_outstanding import get_customer_outstanding
from jarvis.tools.get_exchange_rate import get_exchange_rate
from jarvis.tools.get_fiscal_year import get_fiscal_year
from jarvis.tools.get_itemised_tax_breakup import get_itemised_tax_breakup
from jarvis.tools.get_party_dashboard_info import get_party_dashboard_info
from jarvis.tools.get_stock_balance import get_stock_balance
from jarvis.tools.get_valuation_rate import get_valuation_rate
from jarvis.tools.scan_barcode import scan_barcode


def _all_exist():
	"""Patch ``frappe.db.exists`` to return True for every check.

	Used by happy-path tests where the test doesn't care whether the
	bench actually has the named master record - only whether the
	wrapper forwards correct args to the underlying helper.
	"""
	return patch("frappe.db.exists", return_value=True)


def _no_exists():
	"""Inverse of ``_all_exist`` - drives the InvalidArgumentError
	branches for "unknown <doctype>" without depending on the bench
	being unseeded."""
	return patch("frappe.db.exists", return_value=False)


def _allow_perm():
	"""Patch ``frappe.has_permission`` to return True so the wrappers
	don't bail at the perm gate during happy-path envelope tests."""
	return patch("frappe.has_permission", return_value=True)


# ---------------------------------------------------------------------
# get_stock_balance
# ---------------------------------------------------------------------


class TestGetStockBalance(FrappeTestCase):
	def test_rejects_empty_item_code(self):
		with self.assertRaises(InvalidArgumentError):
			get_stock_balance("", "_T-Warehouse")

	def test_rejects_empty_warehouse(self):
		with self.assertRaises(InvalidArgumentError):
			get_stock_balance("_T-Item", "")

	def test_rejects_unknown_item(self):
		with _no_exists(), self.assertRaises(InvalidArgumentError):
			get_stock_balance("Definitely-Not-An-Item", "_T-Warehouse")

	def test_returns_qty_envelope_without_rate(self):
		with (
			_all_exist(),
			patch(
				"erpnext.stock.utils.get_stock_balance",
				return_value=42.5,
			),
		):
			out = get_stock_balance("_T-Item", "_T-Warehouse")
		self.assertEqual(out, {"qty": 42.5})

	def test_returns_qty_and_rate_envelope_when_requested(self):
		with (
			_all_exist(),
			patch(
				"erpnext.stock.utils.get_stock_balance",
				return_value=(42.5, 12.75),
			),
		):
			out = get_stock_balance(
				"_T-Item",
				"_T-Warehouse",
				with_valuation_rate=True,
			)
		self.assertEqual(out, {"qty": 42.5, "rate": 12.75})


# ---------------------------------------------------------------------
# get_valuation_rate
# ---------------------------------------------------------------------


class TestGetValuationRate(FrappeTestCase):
	def test_rejects_empty_item(self):
		with self.assertRaises(InvalidArgumentError):
			get_valuation_rate("", "_T-Co")

	def test_rejects_empty_company(self):
		with self.assertRaises(InvalidArgumentError):
			get_valuation_rate("_T-Item", "")

	def test_returns_rate_envelope_from_bare_number(self):
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"erpnext.stock.get_item_details.get_valuation_rate",
				return_value=99.5,
			),
		):
			out = get_valuation_rate("_T-Item", "_T-Co")
		self.assertEqual(out, {"rate": 99.5})

	def test_returns_rate_envelope_from_dict(self):
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"erpnext.stock.get_item_details.get_valuation_rate",
				return_value={"valuation_rate": 88.0},
			),
		):
			out = get_valuation_rate("_T-Item", "_T-Co")
		self.assertEqual(out, {"rate": 88.0})


# ---------------------------------------------------------------------
# scan_barcode
# ---------------------------------------------------------------------


class TestScanBarcode(FrappeTestCase):
	def test_rejects_empty(self):
		with self.assertRaises(InvalidArgumentError):
			scan_barcode("")

	def test_returns_dict_envelope_when_helper_returns_dict(self):
		with patch(
			"erpnext.stock.utils.scan_barcode",
			return_value={"item_code": "X", "barcode": "012"},
		):
			out = scan_barcode("012")
		self.assertEqual(out, {"item_code": "X", "barcode": "012"})


# ---------------------------------------------------------------------
# get_balance_on
# ---------------------------------------------------------------------


class TestGetBalanceOn(FrappeTestCase):
	def test_rejects_when_no_account_or_party(self):
		with self.assertRaises(InvalidArgumentError):
			get_balance_on()

	def test_returns_envelope_for_account_only(self):
		with (
			_all_exist(),
			patch(
				"erpnext.accounts.utils.get_balance_on",
				return_value=1500.0,
			),
		):
			out = get_balance_on(account="_T-Acct", date="2026-06-18")
		self.assertEqual(out["balance"], 1500.0)
		self.assertEqual(out["account"], "_T-Acct")
		self.assertEqual(out["date"], "2026-06-18")


# ---------------------------------------------------------------------
# get_customer_outstanding
# ---------------------------------------------------------------------


class TestGetCustomerOutstanding(FrappeTestCase):
	def test_rejects_empty_customer(self):
		with self.assertRaises(InvalidArgumentError):
			get_customer_outstanding("", "Any-Co")

	def test_returns_envelope(self):
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"erpnext.selling.doctype.customer.customer.get_customer_outstanding",
				return_value=2500.0,
			),
		):
			out = get_customer_outstanding("_T-Customer", "_T-Co")
		self.assertEqual(out["outstanding"], 2500.0)
		self.assertEqual(out["customer"], "_T-Customer")
		self.assertEqual(out["company"], "_T-Co")


# ---------------------------------------------------------------------
# get_party_dashboard_info
# ---------------------------------------------------------------------


class TestGetPartyDashboardInfo(FrappeTestCase):
	def test_rejects_invalid_party_type(self):
		with self.assertRaises(InvalidArgumentError):
			get_party_dashboard_info("Employee", "any")

	def test_rejects_empty_party(self):
		with self.assertRaises(InvalidArgumentError):
			get_party_dashboard_info("Customer", "")

	def test_returns_envelope(self):
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"erpnext.accounts.party.get_dashboard_info",
				return_value=[{"company": "X", "total_unpaid": 100}],
			),
		):
			out = get_party_dashboard_info("Customer", "_T-Customer")
		self.assertEqual(out["party_type"], "Customer")
		self.assertEqual(out["party"], "_T-Customer")
		self.assertEqual(out["dashboard"], [{"company": "X", "total_unpaid": 100}])


# ---------------------------------------------------------------------
# get_exchange_rate
# ---------------------------------------------------------------------


class TestGetExchangeRate(FrappeTestCase):
	def test_rejects_empty(self):
		with self.assertRaises(InvalidArgumentError):
			get_exchange_rate("", "INR")

	def test_returns_envelope(self):
		with (
			_all_exist(),
			patch(
				"erpnext.setup.utils.get_exchange_rate",
				return_value=83.25,
			),
		):
			out = get_exchange_rate("USD", "INR", "2026-06-18")
		self.assertEqual(out["rate"], 83.25)
		self.assertEqual(out["from_currency"], "USD")
		self.assertEqual(out["to_currency"], "INR")
		self.assertEqual(out["transaction_date"], "2026-06-18")


# ---------------------------------------------------------------------
# get_fiscal_year
# ---------------------------------------------------------------------


class TestGetFiscalYear(FrappeTestCase):
	def test_rejects_when_neither_date_nor_fiscal_year(self):
		with self.assertRaises(InvalidArgumentError):
			get_fiscal_year()

	def test_returns_envelope(self):
		with (
			_all_exist(),
			patch(
				"erpnext.accounts.utils.get_fiscal_year",
				return_value=("FY26", _date(2025, 4, 1), _date(2026, 3, 31)),
			),
		):
			out = get_fiscal_year(date="2025-12-15")
		self.assertEqual(out["fiscal_year"], "FY26")
		self.assertEqual(out["year_start_date"], "2025-04-01")
		self.assertEqual(out["year_end_date"], "2026-03-31")


# ---------------------------------------------------------------------
# get_itemised_tax_breakup
# ---------------------------------------------------------------------


class TestGetItemisedTaxBreakup(FrappeTestCase):
	def test_rejects_unsupported_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			get_itemised_tax_breakup("Address", "x")

	def test_rejects_empty_name(self):
		with self.assertRaises(InvalidArgumentError):
			get_itemised_tax_breakup("Sales Invoice", "")

	def test_rejects_unknown_doc(self):
		with _no_exists(), self.assertRaises(InvalidArgumentError):
			get_itemised_tax_breakup("Sales Invoice", "SINV-not-a-real-doc")

	def test_returns_envelope(self):
		# The wrapper calls frappe.get_doc to load the source; rather
		# than seed a Sales Invoice (heavy + brittle across releases)
		# we patch get_doc to return a sentinel and assert the loaded doc
		# was handed to the compat shim.
		#
		# Patch the shim, NOT erpnext's get_itemised_tax: ERPNext 15 takes the
		# taxes child table and 16 takes the document, so a mock standing in for
		# the erpnext function pins one major's calling convention and passes on
		# both. That is how the 'SalesInvoice' object is not iterable bug reached
		# production. Which object each major receives is asserted for real in
		# tests/test_compat_frappe_versions.py.
		sentinel_doc = object()
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"frappe.get_doc",
				return_value=sentinel_doc,
			),
			patch(
				"jarvis.compat.itemised_tax",
				return_value={"_T-Item": {"VAT - X": 18.0}},
			) as git,
		):
			out = get_itemised_tax_breakup("Sales Invoice", "SINV-001")
		git.assert_called_once()
		self.assertIs(git.call_args.args[0], sentinel_doc)
		self.assertEqual(out["doctype"], "Sales Invoice")
		self.assertEqual(out["name"], "SINV-001")
		self.assertEqual(out["itemised_tax"], {"_T-Item": {"VAT - X": 18.0}})


# ---------------------------------------------------------------------
# Real permission-path regression tests (F13/F14/F15/F16 - see
# .superpowers/sdd/audit-findings.md). Unlike the envelope tests above
# (which patch frappe.has_permission/frappe.db.exists to pin the boundary
# contract without heavy master-data), these exercise real
# frappe.has_permission decisions against real users/roles/records so the
# permission checks the fix added are proven end to end, not just wired.
# ---------------------------------------------------------------------

PERM_COMPANY_A = "_JPL Perm Company A"
PERM_COMPANY_B = "_JPL Perm Company B"
PERM_ITEM_GROUP = "_JPL Perm Test Item Group"
PERM_UOM = "_JPL Perm Nos"
PERM_ITEM = "_JPL Perm Test Item"
PERM_CUSTOMER = "_JPL Perm Test Customer"
PERM_WAREHOUSE = "_JPL Perm Test Warehouse"
ROLE_NO_GRANTS = "JPL Perm No Grants Role"
USER_NO_GRANTS = "jpl-perm-no-grants@example.com"
USER_ITEM_READER = "jpl-perm-item-reader@example.com"
# Sales User grants role-level Customer + Company read, but this user is
# additionally scoped by a Company User Permission to PERM_COMPANY_A only -
# exactly the "restricted via a Company User Permission" case the audit
# findings call out.
USER_COMPANY_SCOPED = "jpl-perm-company-scoped@example.com"
# ERPNext's stock "Auditor" role (GL Entry read, Company "select" only -
# not "read") combined with "Sales Manager" (Customer read, no Company
# permission of any kind) for party read - together, real stock roles
# that add up to GL/party read with NO Company read anywhere and no
# Company User Permission at all. This is exactly the over-block Fix A
# addresses: gating on Company-doctype read (rather than Company User
# Permission scope) would deny this combination outright.
USER_AUDITOR_LIKE = "jpl-perm-auditor-like@example.com"


def _ensure_role(name: str) -> None:
	if not frappe.db.exists("Role", name):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": name,
				"desk_access": 1,
				"is_custom": 1,
			}
		).insert(ignore_permissions=True)


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


def _ensure_item(name: str) -> None:
	if frappe.db.exists("Item", name):
		return
	if not frappe.db.exists("Item Group", PERM_ITEM_GROUP):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": PERM_ITEM_GROUP,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("UOM", PERM_UOM):
		frappe.get_doc({"doctype": "UOM", "uom_name": PERM_UOM}).insert(ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": name,
			"item_name": name,
			"item_group": PERM_ITEM_GROUP,
			"stock_uom": PERM_UOM,
		}
	).insert(ignore_permissions=True)


def _ensure_warehouse(name: str, company: str) -> str:
	existing = frappe.db.exists("Warehouse", {"warehouse_name": name, "company": company})
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": name,
			"company": company,
		}
	)
	# Upstream erpnext develop now validates a new Warehouse against the
	# company's inventory account (Warehouse.validate_inventory_account ->
	# get_warehouse_account), and this fixture's companies deliberately skip
	# chart-of-accounts creation - they exist for permission checks, not a
	# ledger. Use the escape flag upstream provides for exactly this case
	# (harmless on older erpnext that predates the validation).
	doc.flags.ignore_inventory_account_validation = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_customer(name: str) -> str:
	existing = frappe.db.exists("Customer", {"customer_name": name})
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": name,
			"customer_type": "Company",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


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


class CrossCompanyPermTestCase(FrappeTestCase):
	"""Shared fixture for the F13/F14/F15/F16 permission-path tests."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# Roles/users are cheap, harmless to leave committed (reused
		# idempotently across runs, mirrors test_permlevel_leak.py).
		_ensure_role(ROLE_NO_GRANTS)
		_ensure_user(USER_NO_GRANTS, roles=(ROLE_NO_GRANTS,))
		_ensure_user(USER_ITEM_READER, roles=("Stock User",))
		_ensure_user(USER_COMPANY_SCOPED, roles=("Sales User",))
		_ensure_user(USER_AUDITOR_LIKE, roles=("Auditor", "Sales Manager"))
		frappe.db.commit()
		# Company/Item/Customer/User Permission are NOT committed - unlike
		# roles/users, a leftover Company row changes production inference
		# elsewhere (e.g. the agent scope resolver's "single Company on
		# the site" fallback), so these must vanish via FrappeTestCase's
		# automatic per-class rollback rather than persist across test
		# modules. They're still visible within this class's own
		# transaction (same DB connection), which is all these tests need.
		_ensure_company(PERM_COMPANY_A, "JPLA")
		_ensure_company(PERM_COMPANY_B, "JPLB")
		_ensure_item(PERM_ITEM)
		cls.customer = _ensure_customer(PERM_CUSTOMER)
		cls.warehouse = _ensure_warehouse(PERM_WAREHOUSE, PERM_COMPANY_A)
		_ensure_user_permission(USER_COMPANY_SCOPED, "Company", PERM_COMPANY_A)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()


class TestScanBarcodePermissionGate(CrossCompanyPermTestCase):
	"""F16: scan_barcode must gate on Item / Warehouse read permission -
	the underlying erpnext helper performs none itself."""

	def test_restricted_user_denied(self):
		with (
			_as(USER_NO_GRANTS),
			patch(
				"erpnext.stock.utils.scan_barcode",
				return_value={"item_code": PERM_ITEM, "barcode": "012"},
			),
			self.assertRaises(frappe.PermissionError),
		):
			scan_barcode("012")

	def test_item_reader_still_succeeds(self):
		with (
			_as(USER_ITEM_READER),
			patch(
				"erpnext.stock.utils.scan_barcode",
				return_value={"item_code": PERM_ITEM, "barcode": "012"},
			),
		):
			out = scan_barcode("012")
		self.assertEqual(out["item_code"], PERM_ITEM)

	def test_restricted_user_denied_for_warehouse_match(self):
		# The erpnext helper's Warehouse branch (scanning a warehouse name
		# itself, e.g. a bin-location barcode) returns {"warehouse": ...}
		# with no permission check of its own - this must be gated too.
		with (
			_as(USER_NO_GRANTS),
			patch(
				"erpnext.stock.utils.scan_barcode",
				return_value={"warehouse": self.warehouse},
			),
			self.assertRaises(frappe.PermissionError),
		):
			scan_barcode("WH-012")

	def test_warehouse_reader_still_succeeds_for_warehouse_match(self):
		# Stock User has role-level Warehouse read.
		with (
			_as(USER_ITEM_READER),
			patch(
				"erpnext.stock.utils.scan_barcode",
				return_value={"warehouse": self.warehouse},
			),
		):
			out = scan_barcode("WH-012")
		self.assertEqual(out["warehouse"], self.warehouse)


class TestGetCustomerOutstandingCompanyPermission(CrossCompanyPermTestCase):
	"""F13: company must be permission-checked, not just the customer."""

	def test_company_scoped_user_denied_for_other_company(self):
		with (
			_as(USER_COMPANY_SCOPED),
			patch(
				"erpnext.selling.doctype.customer.customer.get_customer_outstanding",
				return_value=2500.0,
			),
			self.assertRaises(PermissionDeniedError),
		):
			get_customer_outstanding(self.customer, PERM_COMPANY_B)

	def test_company_scoped_user_allowed_for_own_company(self):
		with (
			_as(USER_COMPANY_SCOPED),
			patch(
				"erpnext.selling.doctype.customer.customer.get_customer_outstanding",
				return_value=2500.0,
			),
		):
			out = get_customer_outstanding(self.customer, PERM_COMPANY_A)
		self.assertEqual(out["outstanding"], 2500.0)


class TestGetPartyDashboardInfoCompanyStrip(CrossCompanyPermTestCase):
	"""F14: dashboard entries for companies the caller can't read must be
	stripped, not just gated on the party itself."""

	def test_company_scoped_user_only_sees_own_company_entry(self):
		with (
			_as(USER_COMPANY_SCOPED),
			patch(
				"erpnext.accounts.party.get_dashboard_info",
				return_value=[
					{"company": PERM_COMPANY_A, "total_unpaid": 100},
					{"company": PERM_COMPANY_B, "total_unpaid": 999},
				],
			),
		):
			out = get_party_dashboard_info("Customer", self.customer)
		companies = [d["company"] for d in out["dashboard"]]
		self.assertEqual(companies, [PERM_COMPANY_A])

	def test_unrestricted_user_sees_every_company_entry(self):
		# No Company User Permission at all -> role-level Company read
		# (granted broadly to most ERP roles) is unrestricted, so both
		# entries must survive the strip.
		with (
			_as("Administrator"),
			patch(
				"erpnext.accounts.party.get_dashboard_info",
				return_value=[
					{"company": PERM_COMPANY_A, "total_unpaid": 100},
					{"company": PERM_COMPANY_B, "total_unpaid": 999},
				],
			),
		):
			out = get_party_dashboard_info("Customer", self.customer)
		companies = {d["company"] for d in out["dashboard"]}
		self.assertEqual(companies, {PERM_COMPANY_A, PERM_COMPANY_B})


class TestGetBalanceOnCompanyPermission(CrossCompanyPermTestCase):
	"""F15: an explicit company must be permission-checked, and party-only
	mode (which otherwise sums across every company) must not silently
	blend in companies a restricted caller can't read."""

	def test_company_scoped_user_denied_for_other_company(self):
		with _as(USER_COMPANY_SCOPED), self.assertRaises(PermissionDeniedError):
			get_balance_on(account=None, party_type="Customer", party=self.customer, company=PERM_COMPANY_B)

	def test_company_scoped_user_allowed_for_own_company(self):
		with (
			_as(USER_COMPANY_SCOPED),
			patch(
				"erpnext.accounts.utils.get_balance_on",
				return_value=500.0,
			),
		):
			out = get_balance_on(party_type="Customer", party=self.customer, company=PERM_COMPANY_A)
		self.assertEqual(out["balance"], 500.0)

	def test_company_scoped_user_must_pass_company_in_party_only_mode(self):
		with _as(USER_COMPANY_SCOPED), self.assertRaises(InvalidArgumentError):
			get_balance_on(party_type="Customer", party=self.customer)

	def test_unrestricted_user_party_only_mode_still_works(self):
		with (
			_as("Administrator"),
			patch(
				"erpnext.accounts.utils.get_balance_on",
				return_value=750.0,
			),
		):
			out = get_balance_on(party_type="Customer", party=self.customer)
		self.assertEqual(out["balance"], 750.0)


class TestAuditorLikeCompanyScope(CrossCompanyPermTestCase):
	"""Fix A: company scoping must be by Company User Permission, not
	Company-doctype read - so a real, non-Administrator user with GL/party
	read but NO Company read anywhere (like ERPNext's stock "Auditor"
	role) and no Company User Permission is allowed, not denied. Using
	Administrator wouldn't catch a regression here (Administrator bypasses
	frappe.has_permission entirely), so USER_AUDITOR_LIKE genuinely lacks
	Company read via any of its roles - see the constant's docstring."""

	def test_get_customer_outstanding_allowed_without_company_read(self):
		with (
			_as(USER_AUDITOR_LIKE),
			patch(
				"erpnext.selling.doctype.customer.customer.get_customer_outstanding",
				return_value=42.0,
			),
		):
			out = get_customer_outstanding(self.customer, PERM_COMPANY_A)
		self.assertEqual(out["outstanding"], 42.0)

	def test_get_party_dashboard_info_keeps_entry_without_company_read(self):
		with (
			_as(USER_AUDITOR_LIKE),
			patch(
				"erpnext.accounts.party.get_dashboard_info",
				return_value=[{"company": PERM_COMPANY_A, "total_unpaid": 10}],
			),
		):
			out = get_party_dashboard_info("Customer", self.customer)
		self.assertEqual([d["company"] for d in out["dashboard"]], [PERM_COMPANY_A])

	def test_get_balance_on_allowed_without_company_read(self):
		with (
			_as(USER_AUDITOR_LIKE),
			patch(
				"erpnext.accounts.utils.get_balance_on",
				return_value=333.0,
			),
		):
			out = get_balance_on(party_type="Customer", party=self.customer, company=PERM_COMPANY_A)
		self.assertEqual(out["balance"], 333.0)
