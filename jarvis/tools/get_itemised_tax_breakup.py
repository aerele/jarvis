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

# The item fields the breakup reveals: ``taxable_amount`` is each line's net
# amount (divided by the readable qty it is the rate), and ``tax_amount`` /
# ``tax_rate`` reconstruct it too.
_REVEALED_ITEM_FIELDS = ("net_amount", "amount", "rate")


def _item_amounts_readable(doc) -> bool:
	"""True when the caller may read every item amount field the breakup reveals,
	at that field's permlevel (child fields follow the parent's DocPerms)."""
	if frappe.session.user == "Administrator":
		return True
	table_df = doc.meta.get_field("items")
	if not table_df or not table_df.options:
		return True
	child_meta = frappe.get_meta(table_df.options)
	levels = set(child_meta.get_permlevel_access(permission_type="read", parenttype=doc.doctype))
	for fieldname in _REVEALED_ITEM_FIELDS:
		df = child_meta.get_field(fieldname)
		if df and df.permlevel and df.permlevel not in levels:
			return False
	return True


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

	# Load once, then check permission on the loaded object (has_permission with a
	# name string would lazy-load the parent row this get_doc already fetches).
	# The full doc - with its tax/item child tables - is needed by compat below.
	doc = frappe.get_doc(doctype, name)
	if not frappe.has_permission(doctype, "read", doc=doc):
		raise PermissionDeniedError(f"no read permission on {doctype} {name}")
	# Document read is not enough: the breakup is computed from the item amounts,
	# so a user who cannot see them at their permlevel must not get it either.
	if not _item_amounts_readable(doc):
		raise PermissionDeniedError(
			f"no read permission on the item amounts of {doctype} {name}, which the tax breakup reveals"
		)

	# ERPNext 15 wants the taxes child table here, ERPNext 16 wants the parent
	# doc. Passing `doc` unconditionally was a TypeError ('SalesInvoice' object
	# is not iterable) on every ERPNext 15 call.
	itemised_tax = compat.itemised_tax(doc, with_tax_account=True)
	return {
		"doctype": doctype,
		"name": name,
		"itemised_tax": itemised_tax,
	}
