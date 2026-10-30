"""Read v15 source links that survive an upgrade to native vendor-invoice LCVs."""

import frappe

SOURCE_FIELD = "custom_jarvis_source_invoice"
TAX_DOCTYPE = "Landed Cost Taxes and Charges"
MAX_CLAIM_ROWS = 2000


def source_names(doc):
	meta = frappe.get_meta(TAX_DOCTYPE)
	if not meta.has_field(SOURCE_FIELD):
		return set()
	from jarvis.tools._lcv_source_tax import _DENIED, _guard_fields

	_guard_fields(meta, doc.doctype, extra_fields=(SOURCE_FIELD,))
	names = [row.get(SOURCE_FIELD) for row in doc.get("taxes") or [] if row.get(SOURCE_FIELD)]
	if any(not isinstance(name, str) or not name.strip() or len(name) > 140 for name in names):
		from jarvis.exceptions import PermissionDeniedError

		raise PermissionDeniedError(_DENIED)
	return set(names)


def claimed_cost(invoice, voucher, *, lock=False):
	"""Count legacy submitted claims once, using current reads on write paths.

	A native link for this invoice on the same voucher is authoritative. Custom
	links to other invoices still count, rather than dropping the whole parent.
	No related voucher names or amounts enter a tool response.
	"""
	meta = frappe.get_meta(TAX_DOCTYPE)
	field = meta.get_field(SOURCE_FIELD)
	if not field:
		return 0
	if (
		field.fieldtype != "Link"
		or field.options != "Purchase Invoice"
		or not frappe.db.has_column(TAX_DOCTYPE, SOURCE_FIELD)
	):
		return None
	from jarvis.tools._lcv_source_tax import _amount

	rows = frappe.db.sql(
		f"""select t.base_amount
		from `tabLanded Cost Taxes and Charges` t
		inner join `tabLanded Cost Voucher` p on p.name = t.parent
		where t.parenttype = 'Landed Cost Voucher' and t.parentfield = 'taxes'
		and p.docstatus = 1 and p.name != %(voucher)s
		and t.`{SOURCE_FIELD}` = %(invoice)s
		and not exists (
			select 1 from `tabLanded Cost Vendor Invoice` n
			where n.parent = p.name and n.parenttype = 'Landed Cost Voucher'
			and n.parentfield = 'vendor_invoices' and n.vendor_invoice = %(invoice)s
		)
		limit {MAX_CLAIM_ROWS + 1} {"for update" if lock else ""}""",
		{"invoice": invoice, "voucher": voucher or ""},
		as_dict=True,
	)
	if len(rows) > MAX_CLAIM_ROWS:
		return None
	amounts = [_amount(row.base_amount) for row in rows]
	if any(amount < 0 for amount in amounts):
		return None
	return sum(amounts)
