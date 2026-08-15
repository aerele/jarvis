"""Dry-run a document create: full ERP pipeline, nothing persisted.

Runs the same ``doc.insert()`` as create_doc inside the preview sandbox and
rolls back, returning the resolved header, what the server filled, and which
integrity fields stayed empty - so the agent reviews master-data gaps (e.g. a
supplier with no linked Address) BEFORE creating.
"""

import frappe

from jarvis.tools._preview_sandbox import preview_sandbox
from jarvis.tools.create_doc import _set_title_from_title_field, _validate_create_args

# Header fieldtypes worth echoing back (child tables + layout/HTML excluded).
_HEADER_TYPES = {
	"Data",
	"Link",
	"Dynamic Link",
	"Select",
	"Autocomplete",
	"Date",
	"Datetime",
	"Time",
	"Currency",
	"Float",
	"Int",
	"Check",
	"Percent",
	"Small Text",
}

# Exact allow-list of writable integrity fields. Substring matching swept in
# read-only mirrors (contact_display/_mobile/_email), turning one missing
# contact link into four reported gaps.
_INTEGRITY_FIELDS = {
	"supplier_address",
	"customer_address",
	"company_address",
	"shipping_address",
	"shipping_address_name",
	"dispatch_address",
	"billing_address",
	"contact_person",
	"payment_terms_template",
	"due_date",
	"place_of_supply",
	"tax_category",
	"taxes_and_charges",
}

_TOTAL_FIELDS = ("net_total", "total_taxes_and_charges", "grand_total", "rounded_total")


def _norm(v):
	# The insert pipeline writes 0 where a bare new_doc holds None; that
	# difference is not "the server filled this field".
	return 0 if v in (None, 0, 0.0, False) else v


def preview_doc(doctype: str, values: dict) -> dict:
	"""Validate + resolve a would-be document without creating it.

	Same guards and values shape as ``create_doc``. Returns ``{valid,
	resolved, server_filled, empty_fields, items_count, totals}``; a rejected
	document returns ``{valid: false, error}`` instead of raising. Use before
	``create_doc`` on consequential documents (invoices, orders).
	"""
	_validate_create_args(doctype, values)

	doc = frappe.new_doc(doctype)
	for field, value in values.items():
		doc.set(field, value)
	_set_title_from_title_field(doc)

	try:
		with preview_sandbox():
			doc.insert()
	except Exception as e:
		frappe.clear_messages()
		return {"valid": False, "error": _error_text(e)}
	# Outside the try: a summarization bug must never mislabel a document
	# that validated cleanly. The in-memory doc survives the rollback.
	return _summarize(doc, values)


def _summarize(doc, caller_values: dict) -> dict:
	# server_filled = differs from a bare new_doc, so zero-defaults don't
	# masquerade as auto-fill.
	baseline = frappe.new_doc(doc.doctype)
	has_permlevel_read = doc.get_permlevel_access("read")
	resolved: dict = {}
	server_filled: list[str] = []
	empty_fields: list[str] = []
	for df in doc.meta.fields:
		if df.fieldtype not in _HEADER_TYPES:
			continue
		if df.permlevel and df.permlevel not in has_permlevel_read:
			# Caller can't read this field at its permlevel - never echo its
			# value (caller-supplied or server/hook-computed) back to them.
			continue
		name = df.fieldname
		val = doc.get(name)
		if val in (None, ""):
			if name in _INTEGRITY_FIELDS and not df.read_only:
				empty_fields.append(name)
			continue
		if name in _TOTAL_FIELDS or name.startswith("base_"):
			continue  # totals live in `totals`; base_* mirrors add no signal
		if name in caller_values:
			resolved[name] = val
		elif _norm(val) != _norm(baseline.get(name)):
			resolved[name] = val
			server_filled.append(name)
	# Same permlevel guard as the header loop above: a tenant can customize
	# any of these totals to permlevel>0, and its server-computed value must
	# not leak to a caller who can't read that permlevel.
	allowed_permlevels = (0, *has_permlevel_read)
	totals = {
		f: doc.get(f)
		for f in _TOTAL_FIELDS
		if doc.meta.has_field(f)
		and doc.get(f) is not None
		and doc.meta.get_field(f).permlevel in allowed_permlevels
	}
	return {
		"valid": True,
		"resolved": resolved,
		"server_filled": sorted(server_filled),
		"empty_fields": empty_fields,
		"items_count": len(doc.get("items") or []) if doc.meta.has_field("items") else None,
		"totals": totals,
		"note": (
			"dry run only - nothing was persisted or queued; use create_doc "
			"to create. Side effects fired directly inside hooks (inline "
			"HTTP calls) are not sandboxed."
		),
	}


def _error_text(e: Exception) -> str:
	from frappe.utils import strip_html_tags

	msg = str(e) or type(e).__name__
	try:
		return strip_html_tags(msg)
	except Exception:
		return msg
