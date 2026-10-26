"""Native form defaults that a server-side create otherwise never fetches."""

import math

import frappe
from frappe.utils import flt

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._doc_parts import _refuse_unreadable


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
	# A Link value must never become permission-agnostic database filters.
	if not isinstance(name, str) or not name.strip():
		raise PermissionDeniedError(message)
	if not frappe.db.exists("Purchase Invoice", name):
		raise PermissionDeniedError(message)
	mark = len(getattr(frappe.local, "message_log", None) or [])
	try:
		allowed = frappe.has_permission("Purchase Invoice", "read", doc=name)
	except frappe.DoesNotExistError:
		# A concurrent delete must not reveal existence through a native message.
		messages = getattr(frappe.local, "message_log", None)
		if messages is not None:
			del messages[mark:]
		allowed = False
	if not allowed:
		raise PermissionDeniedError(message)
	_refuse_unreadable("Purchase Invoice", ["base_total", "claimed_landed_cost_amount"])
	masked_fields = getattr(frappe.get_meta("Purchase Invoice"), "get_masked_fields", None)
	if masked_fields and any(
		field.fieldname in {"base_total", "claimed_landed_cost_amount"} for field in masked_fields()
	):
		raise PermissionDeniedError(message)
