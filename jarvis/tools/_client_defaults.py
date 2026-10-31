"""Native form link fetches that server-side document writes do not run."""

import math

import frappe
from frappe.utils import flt

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._doc_parts import _refuse_unreadable

LCV_RECEIPT_SUMMARY_FIELDS = ("supplier", "posting_date", "grand_total")
LCV_RECEIPT_FIELDS = ("receipt_document_type", "receipt_document", *LCV_RECEIPT_SUMMARY_FIELDS)
_RECEIPT_SOURCE_FIELDS = {
	"Purchase Receipt": ("supplier", "posting_date", "base_grand_total"),
	"Purchase Invoice": ("supplier", "posting_date", "base_grand_total"),
	"Subcontracting Receipt": ("supplier", "posting_date", "total"),
	"Stock Entry": ("total_incoming_value",),
}


def landed_cost_receipt_details(doc):
	"""Fetch source-owned receipt summaries, including stale caller-supplied zeros.

	The supplier, date and company-currency total are read-only in Desk. They are
	not the editable partial cost claims in vendor_invoices.
	"""
	if doc.doctype != "Landed Cost Voucher" or doc.docstatus != 0:
		return
	field = doc.meta.get_field("purchase_receipts")
	if not field:
		return
	child = frappe.get_meta(field.options)
	type_field = child.get_field("receipt_document_type")
	allowed = set((type_field.options or "").splitlines()) if type_field else set()
	native = callable(getattr(type(doc), "get_receipt_document_details", None))
	cache = {}
	for row in doc.get("purchase_receipts") or []:
		doctype, name = row.receipt_document_type, row.receipt_document
		if (
			(doctype is not None and not isinstance(doctype, str))
			or (name is not None and not isinstance(name, str))
			or (
				doctype
				and doctype.strip()
				and (doctype not in _RECEIPT_SOURCE_FIELDS or doctype not in allowed)
			)
		):
			raise InvalidArgumentError(f"Receipt row {row.idx} needs a supported document type and name.")
		details = {}
		if doctype and doctype.strip() and name and name.strip():
			key = (doctype, name)
			if key not in cache:
				cache[key] = _receipt_details(doc, doctype, name, native, row.idx)
			details = cache[key]
		# Let normal mandatory checks and collect-mode metadata handle blank links.
		for target in LCV_RECEIPT_SUMMARY_FIELDS:
			if child.has_field(target):
				row.set(target, details.get(target))


def _receipt_details(doc, doctype, name, native, index):
	"""Return fetched summaries, or clear them for unsupported legacy fetch types.

	An empty result deliberately replaces existing summary values with None.
	"""
	# v15 has only PR/PI add_fetch handlers. Do not guess a mapping for other
	# receipt types when a different installed version has no native handler.
	if not native and doctype not in ("Purchase Receipt", "Purchase Invoice"):
		return {}
	message = f"Cannot read the source details for receipt row {index}. Use a receipt you can read."
	fields = _RECEIPT_SOURCE_FIELDS[doctype]
	_require_source_read(doctype, name, fields, message)
	mark = len(getattr(frappe.local, "message_log", None) or [])
	try:
		if native:
			details = doc.get_receipt_document_details(doctype, name)
		else:
			source = frappe.db.get_value(doctype, name, list(fields), as_dict=True)
			details = (
				{
					"supplier": source.supplier,
					"posting_date": source.posting_date,
					"grand_total": source.base_grand_total,
				}
				if source
				else None
			)
	except frappe.DoesNotExistError:
		_clear_new_messages(mark)
		raise PermissionDeniedError(message) from None
	if not isinstance(details, dict):
		_clear_new_messages(mark)
		raise PermissionDeniedError(message)
	return details


def landed_cost_vendor_amounts(doc, values):
	"""Mirror the LCV vendor link's native fetch, only for omitted input amounts.

	Explicit amounts remain caller-owned, including zero. This is a create default,
	not a reservation of costs or a change to ERPNext's claim/submission rules.
	"""
	if doc.doctype != "Landed Cost Voucher":
		return
	method = getattr(type(doc), "get_vendor_invoice_amount", None)
	field = doc.meta.get_field("vendor_invoices")
	if not callable(method) or not field:
		return  # older ERPNext versions do not have this form behavior
	pending = [
		(idx, row)
		for idx, (row, proposed) in enumerate(
			zip(doc.get("vendor_invoices") or [], values.get("vendor_invoices") or [], strict=True), start=1
		)
		if row.vendor_invoice not in (None, "") and "amount" not in proposed
	]
	if not pending or not frappe.get_meta(field.options).has_field("amount"):
		return
	if not doc.company:
		raise InvalidArgumentError("company is required to fetch vendor invoice amounts")
	for idx, row in pending:
		_require_invoice_read(row.vendor_invoice)
		result = doc.get_vendor_invoice_amount(row.vendor_invoice)
		amount = flt(result.get("amount")) if isinstance(result, dict) and "amount" in result else 0
		if not math.isfinite(amount) or amount <= 0:
			raise InvalidArgumentError(
				f"Cannot derive an unclaimed vendor amount for row {idx}; "
				"supply its amount explicitly or remove the row."
			)
		row.amount = amount


def _require_invoice_read(name):
	message = "Cannot read the vendor invoice amounts; supply an amount explicitly or use a readable invoice."
	_require_source_read("Purchase Invoice", name, ("base_total", "claimed_landed_cost_amount"), message)


def _clear_new_messages(mark):
	messages = getattr(frappe.local, "message_log", None)
	if messages is not None:
		del messages[mark:]


def _require_source_read(doctype, name, fields, message):
	# A Link value must never become permission-agnostic database filters.
	if not isinstance(name, str) or not name.strip():
		raise PermissionDeniedError(message)
	if not frappe.db.exists(doctype, name):
		raise PermissionDeniedError(message)
	mark = len(getattr(frappe.local, "message_log", None) or [])
	try:
		allowed = frappe.has_permission(doctype, "read", doc=name)
	except frappe.DoesNotExistError:
		# A concurrent delete must not reveal existence through a native message.
		_clear_new_messages(mark)
		allowed = False
	if not allowed:
		raise PermissionDeniedError(message)
	_refuse_unreadable(doctype, fields)
	masked_fields = getattr(frappe.get_meta(doctype), "get_masked_fields", None)
	if masked_fields and any(field.fieldname in fields for field in masked_fields()):
		raise PermissionDeniedError(message)
