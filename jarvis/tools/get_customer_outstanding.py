"""Net outstanding receivable for a customer at a company.

Reproduces ``erpnext.selling.doctype.customer.customer.get_customer_outstanding``
(the Customer Credit Limit calculation): GL entries against the customer,
plus the invoiceable balance of open Sales Orders, plus unbilled Delivery
Note value (net of billed and returned quantity). ERPNext sums the Sales
Order and Delivery Note parts with raw SQL that ignores permissions, so here
only documents the caller can read (``frappe.get_list``: roles and User
Permissions) count towards those two parts. The GL part is computed as
ERPNext does, and an unrestricted caller gets the same number as ERPNext.

The Delivery Note arithmetic is the return-aware, quantity-based formula of
current ERPNext. On older ERPNext versions (before that change) ERPNext's own
figure is computed differently, so the number here can differ from that
version's ``get_customer_outstanding``.

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
from frappe.query_builder.functions import Coalesce, IfNull, Sum
from frappe.utils import flt

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)
from jarvis.tools._company_scope import assert_company_permitted
from jarvis.tools._ledger_access import assert_ledger_readable

# Delivery Note names priced per query, to keep the ``IN (...)`` list bounded.
_NAME_CHUNK = 500


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
	# Unbilled, unreturned Delivery Note value not created against a Sales Order /
	# Sales Invoice, for the Delivery Notes the caller can read (none when the user
	# has no read on the DocType, where get_list would throw). The arithmetic is
	# ERPNext's own (quantity based, net of billed and returned stock quantity),
	# restricted to the readable notes; see erpnext get_customer_outstanding.
	if not frappe.has_permission("Delivery Note", "read"):
		return 0.0
	names = frappe.get_list(
		"Delivery Note",
		filters={
			"customer": customer,
			"company": company,
			"docstatus": 1,
			"status": ["not in", ["Closed", "Stopped"]],
		},
		pluck="name",
		limit_page_length=0,
	)
	return sum(
		_unbilled_delivery_value(customer, company, names[i : i + _NAME_CHUNK])
		for i in range(0, len(names), _NAME_CHUNK)
	)


def _unbilled_delivery_value(customer: str, company: str, names: list[str]) -> float:
	dn = frappe.qb.DocType("Delivery Note")
	dn_item = frappe.qb.DocType("Delivery Note Item")
	si = frappe.qb.DocType("Sales Invoice")
	si_item = frappe.qb.DocType("Sales Invoice Item")

	returned_items = (
		frappe.qb.from_(dn_item)
		.join(dn)
		.on(dn.name == dn_item.parent)
		.select(dn_item.name, dn_item.dn_detail)
		.where(
			(dn.customer == customer) & (dn.company == company) & (dn.docstatus == 1) & (dn.is_return == 1)
		)
	).as_("returned_items")
	# Credits against a return delivery reduce the original delivery row's billed quantity.
	billed_dn_detail = Coalesce(returned_items.dn_detail, si_item.dn_detail)
	# Only rows of this chunk's notes can be looked up below, so aggregate just those
	# instead of every submitted invoice item on the site. The coalesced key is the
	# original delivery row even for a credit note against a return.
	chunk_rows = frappe.qb.from_(dn_item).select(dn_item.name).where(dn_item.parent.isin(names))
	billed = (
		frappe.qb.from_(si_item)
		.join(si)
		.on(si.name == si_item.parent)
		.left_join(returned_items)
		.on((returned_items.name == si_item.dn_detail) & (si.is_return == 1))
		.select(billed_dn_detail.as_("dn_detail"), Sum(si_item.stock_qty).as_("billed_qty"))
		.where(billed_dn_detail.isin(chunk_rows))
		.where(si_item.docstatus == 1)
		.where((si.is_return == 0) | (si.update_billed_amount_in_delivery_note == 1))
		.groupby(billed_dn_detail)
	)
	billed_qty = IfNull(billed.billed_qty, 0.0)
	returned_qty = IfNull(dn_item.returned_qty, 0.0)

	rows = (
		frappe.qb.from_(dn)
		.join(dn_item)
		.on(dn.name == dn_item.parent)
		.left_join(billed)
		.on(dn_item.name == billed.dn_detail)
		.select(
			Sum(
				(
					dn_item.base_net_amount
					* (dn_item.stock_qty - billed_qty - returned_qty)
					/ dn_item.stock_qty
				)
				/ dn.base_net_total
				* dn.base_grand_total
			)
		)
		.where(dn.name.isin(names) & (dn.base_net_total > 0))
		.where(dn_item.stock_qty > billed_qty + returned_qty)
		.where(Coalesce(dn_item.against_sales_order, "") == "")
		.where(Coalesce(dn_item.against_sales_invoice, "") == "")
		.run()
	)
	return flt(rows[0][0]) if rows and rows[0][0] is not None else 0.0
