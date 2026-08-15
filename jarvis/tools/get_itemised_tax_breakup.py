"""Per-line-item tax breakup for a Sales / Purchase Invoice.

Wraps ``erpnext.controllers.taxes_and_totals.get_itemised_tax``. That
function takes a Document object; we accept (doctype, name), load the
doc with permission checks, then delegate. Returns the same per-item
tax map the standard ERPNext print formats render in their tax tables.

Useful when the agent is asked to explain "why is the tax on this
invoice $X?" - the LLM consistently miscomputes by re-applying tax
rates to the wrong base when it tries to derive from the invoice
fields alone.
"""

from __future__ import annotations

import frappe

from jarvis import compat
from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)
from jarvis.tools import require_doctype_and_name

_VALID_DOCTYPES = (
	"Sales Invoice",
	"Purchase Invoice",
	"Sales Order",
	"Purchase Order",
	"Delivery Note",
	"Purchase Receipt",
	"Quotation",
	"Supplier Quotation",
)


def get_itemised_tax_breakup(doctype: str, name: str) -> dict:
	"""Return ``{doctype, name, itemised_tax}`` where ``itemised_tax``
	is a map keyed by item code (or item row index) to the per-line
	tax account contributions.
	"""
	require_doctype_and_name(doctype, name)
	if doctype not in _VALID_DOCTYPES:
		raise InvalidArgumentError(
			f"doctype must be one of {list(_VALID_DOCTYPES)}",
		)
	if not frappe.db.exists(doctype, name):
		raise InvalidArgumentError(f"unknown {doctype}: {name}")
	if not frappe.has_permission(doctype, "read", doc=name):
		raise PermissionDeniedError(f"no read permission on {doctype} {name}")

	doc = frappe.get_doc(doctype, name)
	# ERPNext 15 wants the taxes child table here, ERPNext 16 wants the parent
	# doc. Passing `doc` unconditionally was a TypeError ('SalesInvoice' object
	# is not iterable) on every ERPNext 15 call.
	itemised_tax = compat.itemised_tax(doc, with_tax_account=True)
	return {
		"doctype": doctype,
		"name": name,
		"itemised_tax": itemised_tax,
	}
