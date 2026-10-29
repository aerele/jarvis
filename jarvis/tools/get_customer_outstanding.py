"""Net outstanding receivable for a customer at a company.

Reproduces ``erpnext.selling.doctype.customer.customer.get_customer_outstanding``
(the Customer Credit Limit calculation): GL entries against the customer,
plus the invoiceable balance of open Sales Orders, plus unbilled Delivery
Note value. ERPNext sums the Sales Order and Delivery Note parts with raw SQL
that ignores permissions, so here only documents the caller can read
(``frappe.get_list``: roles and User Permissions) count towards those two
parts. The GL part is computed as ERPNext does, and an unrestricted caller
gets the same number as ERPNext.

Customer read perm is enforced before delegating, and the company is
gated by Company User Permission scope (not Company-doctype read - see
``jarvis.tools._company_scope``), so a user who can't read the customer
- or who is restricted to a different company - can't probe an
outstanding balance outside what they can see. The underlying helper
itself applies no company-level permission filter.

Customer read alone is not enough, though: the figure is a ledger sum
(plus unbilled Sales Order value), and Stock / Sales Users read customers
without any ledger access. So it also needs GL Entry read or the Accounts
Receivable report (``jarvis.tools._ledger_access``).
"""

from __future__ import annotations

import frappe
from frappe.query_builder.functions import Coalesce, Sum
from frappe.utils import flt

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)
from jarvis.tools._company_scope import assert_company_permitted
from jarvis.tools._ledger_access import assert_ledger_readable


def get_customer_outstanding(
	customer: str,
	company: str,
	cost_center: str | None = None,
	ignore_outstanding_sales_order: bool = False,
) -> dict:
	"""Return ``{outstanding, customer, company}`` where ``outstanding``
	is the net receivable from ``customer`` at ``company``. If
	``ignore_outstanding_sales_order=True``, exclude open Sales Orders
	from the calculation."""
	if not customer:
		raise InvalidArgumentError("customer is required")
	if not company:
		raise InvalidArgumentError("company is required")
	if not frappe.db.exists("Customer", customer):
		raise InvalidArgumentError(f"unknown Customer: {customer}")
	if not frappe.db.exists("Company", company):
		raise InvalidArgumentError(f"unknown Company: {company}")
	if not frappe.has_permission("Customer", "read", doc=customer):
		raise PermissionDeniedError(f"no read permission on Customer {customer}")
	assert_ledger_readable("Customer")
	assert_company_permitted(company)

	if cost_center and not frappe.db.exists("Cost Center", cost_center):
		raise InvalidArgumentError(f"unknown Cost Center: {cost_center}")

	outstanding = _gl_outstanding(customer, company, cost_center)
	if not ignore_outstanding_sales_order:
		outstanding += _sales_order_outstanding(customer, company)
	outstanding += _delivery_note_outstanding(customer, company)
	return {
		"outstanding": float(outstanding or 0),
		"customer": customer,
		"company": company,
	}


def _gl_outstanding(customer: str, company: str, cost_center: str | None) -> float:
	# Same query as ERPNext: ledger sum, not a document list, so no per-row read filter.
	gle = frappe.qb.DocType("GL Entry")
	query = (
		frappe.qb.from_(gle)
		.select(Sum(gle.debit) - Sum(gle.credit))
		.where(
			(gle.party_type == "Customer")
			& (gle.is_cancelled == 0)
			& (gle.party == customer)
			& (gle.company == company)
		)
	)
	if cost_center:
		lft, rgt = frappe.get_cached_value("Cost Center", cost_center, ["lft", "rgt"])
		cc = frappe.qb.DocType("Cost Center")
		query = query.where(
			gle.cost_center.isin(frappe.qb.from_(cc).select(cc.name).where((cc.lft >= lft) & (cc.rgt <= rgt)))
		)
	rows = query.run()
	return flt(rows[0][0]) if rows else 0.0


def _sales_order_outstanding(customer: str, company: str) -> float:
	# Unbilled share of each open Sales Order the caller can read. get_list
	# throws when the user has no read on the DocType at all: count nothing.
	if not frappe.has_permission("Sales Order", "read"):
		return 0.0
	orders = frappe.get_list(
		"Sales Order",
		filters={
			"customer": customer,
			"company": company,
			"docstatus": 1,
			"per_billed": ["<", 100],
			"status": ["!=", "Closed"],
		},
		fields=["base_grand_total", "per_billed"],
		limit_page_length=0,
	)
	return sum(flt(o.base_grand_total) * (100 - flt(o.per_billed)) / 100 for o in orders)


def _delivery_note_outstanding(customer: str, company: str) -> float:
	# Unbilled, unreturned stock quantity of each Delivery Note row not created
	# against a Sales Order / Sales Invoice, valued as ERPNext does (row net
	# amount share of the note's grand total), for the Delivery Notes the caller
	# can read (none when the user has no read on the DocType, where get_list
	# would throw).
	if not frappe.has_permission("Delivery Note", "read"):
		return 0.0
	notes = frappe.get_list(
		"Delivery Note",
		filters={
			"customer": customer,
			"company": company,
			"docstatus": 1,
			"base_net_total": [">", 0],
			"status": ["not in", ["Closed", "Stopped"]],
		},
		fields=["name", "base_net_total", "base_grand_total"],
		limit_page_length=0,
	)
	if not notes:
		return 0.0
	totals = {n.name: n for n in notes}

	dn_item = frappe.qb.DocType("Delivery Note Item")
	items = (
		frappe.qb.from_(dn_item)
		.select(
			dn_item.name, dn_item.parent, dn_item.base_net_amount, dn_item.stock_qty, dn_item.returned_qty
		)
		.where(
			dn_item.parent.isin(list(totals))
			& (Coalesce(dn_item.against_sales_order, "") == "")
			& (Coalesce(dn_item.against_sales_invoice, "") == "")
		)
		.run(as_dict=True)
	)
	if not items:
		return 0.0

	billed = _billed_stock_qty(customer, company, [i.name for i in items])
	outstanding = 0.0
	for item in items:
		open_qty = flt(item.stock_qty) - flt(billed.get(item.name)) - flt(item.returned_qty)
		if open_qty > 0 and flt(item.stock_qty):
			note = totals[item.parent]
			share = flt(item.base_net_amount) * open_qty / flt(item.stock_qty) / note.base_net_total
			outstanding += share * note.base_grand_total
	return outstanding


def _billed_stock_qty(customer: str, company: str, dn_details: list[str]) -> dict:
	# Invoiced stock quantity per Delivery Note row, as ERPNext counts it: credit
	# notes count only when they update the delivery's billed amount, and a credit
	# against a return delivery row counts against the original row.
	dn = frappe.qb.DocType("Delivery Note")
	dn_item = frappe.qb.DocType("Delivery Note Item")
	si = frappe.qb.DocType("Sales Invoice")
	si_item = frappe.qb.DocType("Sales Invoice Item")
	returned = (
		frappe.qb.from_(dn_item)
		.join(dn)
		.on(dn.name == dn_item.parent)
		.select(dn_item.name, dn_item.dn_detail)
		.where(
			(dn.customer == customer) & (dn.company == company) & (dn.docstatus == 1) & (dn.is_return == 1)
		)
	).as_("returned")
	row = Coalesce(returned.dn_detail, si_item.dn_detail)
	return dict(
		frappe.qb.from_(si_item)
		.join(si)
		.on(si.name == si_item.parent)
		.left_join(returned)
		.on((returned.name == si_item.dn_detail) & (si.is_return == 1))
		.select(row, Sum(si_item.stock_qty))
		.where(
			(si_item.docstatus == 1)
			& ((si.is_return == 0) | (si.update_billed_amount_in_delivery_note == 1))
			& row.isin(dn_details)
		)
		.groupby(row)
		.run()
	)
