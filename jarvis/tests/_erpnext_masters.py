"""ERPNext masters the J2a tests save against, created when missing.

The CI site is a fresh ERPNext install that never ran the setup wizard, so it has
no Item Group, UOM, Customer Group, Territory, Price List, Fiscal Year, Stock Entry Type or
Warehouse Type "Transit" (a Standard chart Company needs that last one). Every
helper here is "if not exists, insert", under its own ``_J2A`` name where the name
is ours to pick, so it is a no-op on a populated site. Nothing commits: the
calling test class rolls back, and ``clear_cache_after_rollback`` drops the
defaults and fiscal-year caches the inserts filled.
"""

from __future__ import annotations

import datetime

import frappe
from frappe.utils import getdate

COMPANY = "_J2A Failure Kind Co"
ABBR = "J2AFK"
ITEM_GROUP = "_J2A Item Group"
UOM = "_J2A Nos"
CUSTOMER_GROUP = "_J2A Customer Group"
TERRITORY = "_J2A Territory"
SELLING_PRICE_LIST = "_J2A Selling"


def clear_cache_after_rollback(test_class) -> None:
	"""Company / Fiscal Year inserts fill the defaults and fiscal-year caches, which
	the class rollback does not undo. Class cleanups run last-in first-out, so this
	runs just before the rollback ``FrappeTestCase.setUpClass`` registered."""
	test_class.addClassCleanup(frappe.clear_cache)


def ensure_ledger_company() -> str:
	"""A real Company with the Standard chart, so its ledger, cost center and default
	warehouses (Stores among them) exist.

	The country has no regional setup in ERPNext or HRMS. The first Indian company on
	a site makes HRMS add payroll Custom Fields, and that ALTER TABLE commits the open
	transaction, leaking this company past the class rollback. INR stays the currency, as
	before; the country decides only which regional setup runs."""
	if frappe.db.exists("Company", COMPANY):
		return COMPANY
	if not frappe.db.exists("Warehouse Type", "Transit"):
		frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": COMPANY,
			"abbr": ABBR,
			"default_currency": "INR",
			"country": "Maldives",
			"create_chart_of_accounts_based_on": "Standard Template",
			"chart_of_accounts": "Standard",
		}
	).insert(ignore_permissions=True)
	return COMPANY


def ensure_item(code: str, *, is_stock_item: int, valuation_rate: float | None = None) -> str:
	if frappe.db.exists("Item", code):
		return code
	ensure_item_group_and_uom()
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_group": ITEM_GROUP,
			"stock_uom": UOM,
			"is_stock_item": is_stock_item,
			"valuation_rate": valuation_rate,
		}
	).insert(ignore_permissions=True)
	return code


def ensure_item_group_and_uom() -> None:
	if not frappe.db.exists("Item Group", ITEM_GROUP):
		frappe.get_doc({"doctype": "Item Group", "item_group_name": ITEM_GROUP, "is_group": 0}).insert(
			ignore_permissions=True
		)
	if not frappe.db.exists("UOM", UOM):
		frappe.get_doc({"doctype": "UOM", "uom_name": UOM}).insert(ignore_permissions=True)


def ensure_customer(name: str, *, credit_limit: float | None = None) -> str:
	existing = frappe.db.get_value("Customer", {"customer_name": name})
	if existing:
		return existing
	if not frappe.db.exists("Customer Group", CUSTOMER_GROUP):
		frappe.get_doc(
			{"doctype": "Customer Group", "customer_group_name": CUSTOMER_GROUP, "is_group": 0}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Territory", TERRITORY):
		frappe.get_doc({"doctype": "Territory", "territory_name": TERRITORY, "is_group": 0}).insert(
			ignore_permissions=True
		)
	doc = {
		"doctype": "Customer",
		"customer_name": name,
		"customer_group": CUSTOMER_GROUP,
		"territory": TERRITORY,
	}
	if credit_limit is not None:
		doc["credit_limits"] = [{"company": ensure_ledger_company(), "credit_limit": credit_limit}]
	return frappe.get_doc(doc).insert(ignore_permissions=True).name


def ensure_selling_price_list() -> str:
	if not frappe.db.exists("Price List", SELLING_PRICE_LIST):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": SELLING_PRICE_LIST,
				"currency": "INR",
				"selling": 1,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	return SELLING_PRICE_LIST


def ensure_credit_controller(email: str) -> str:
	"""An enabled user holding the role ERPNext asks to extend a credit limit: the
	Accounts Settings credit controller role, or Sales Master Manager when unset."""
	role = frappe.db.get_single_value("Accounts Settings", "credit_controller") or "Sales Master Manager"
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "J2A",
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	if not user.enabled:
		user.enabled = 1
		user.save(ignore_permissions=True)
	if role not in frappe.get_roles(email):
		user.add_roles(role)
	return email


def ensure_stock_entry_type(purpose: str) -> str:
	"""The setup wizard creates one Stock Entry Type per purpose, named after it."""
	if not frappe.db.exists("Stock Entry Type", purpose):
		frappe.get_doc({"doctype": "Stock Entry Type", "name": purpose, "purpose": purpose}).insert(
			ignore_permissions=True
		)
	return purpose


def ensure_fiscal_years(*dates) -> None:
	"""An active Fiscal Year that applies to COMPANY covering each date, found the way
	ERPNext finds one (enabled, and for every company or for this one). A missing one
	is the calendar year scoped to COMPANY alone, which never overlaps the site's own
	years: ERPNext flags an overlap only between two global years or two that share a
	company."""
	company = ensure_ledger_company()
	for day in map(getdate, dates):
		if _active_fiscal_year_covers(day, company):
			continue
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": f"_J2A FY {day.year}",
				"year_start_date": datetime.date(day.year, 1, 1),
				"year_end_date": datetime.date(day.year, 12, 31),
				"companies": [{"company": company}],
			}
		).insert(ignore_permissions=True)


def _active_fiscal_year_covers(day: datetime.date, company: str) -> bool:
	return bool(
		frappe.db.sql(
			"""select fy.name from `tabFiscal Year` fy
			where fy.disabled = 0 and fy.year_start_date <= %(day)s and fy.year_end_date >= %(day)s
			and (
				not exists (select 1 from `tabFiscal Year Company` c where c.parent = fy.name)
				or exists (
					select 1 from `tabFiscal Year Company` c
					where c.parent = fy.name and c.company = %(company)s
				)
			)
			limit 1""",
			{"day": day, "company": company},
		)
	)
