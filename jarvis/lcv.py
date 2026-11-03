"""Persist service-invoice provenance on ERPNext versions without native links."""

import frappe

SOURCE_INVOICE = "custom_jarvis_source_invoice"
TAX_DOCTYPE = "Landed Cost Taxes and Charges"


def source_link_available():
	if not frappe.db.exists("DocType", TAX_DOCTYPE):
		return False
	field = frappe.get_meta(TAX_DOCTYPE).get_field(SOURCE_INVOICE)
	return bool(
		field
		and field.fieldtype == "Link"
		and field.options == "Purchase Invoice"
		and not field.no_copy
		and not field.allow_on_submit
		and not field.hidden
	)


def setup_source_link():
	"""Install/migrate only. Keep provenance when Jarvis is removed or rolled back."""
	if not frappe.db.exists("DocType", "Landed Cost Voucher"):
		return
	if frappe.get_meta("Landed Cost Voucher").has_field("vendor_invoices"):
		return
	field = frappe.get_meta(TAX_DOCTYPE).get_field(SOURCE_INVOICE)
	if field:
		if not source_link_available():
			frappe.logger("lcv_source_tax").warning(
				"Source service invoice field is incompatible; setup left the existing field unchanged."
			)
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			TAX_DOCTYPE: [
				{
					"fieldname": SOURCE_INVOICE,
					"label": "Source Service Invoice",
					"fieldtype": "Link",
					"options": "Purchase Invoice",
					"insert_after": "expense_account",
					"description": "Booked service cost checked by Jarvis. Keep this link when editing its charge.",
					"search_index": 1,
					"no_copy": 0,
					"allow_on_submit": 0,
					"print_hide": 1,
				}
			]
		},
		update=False,
	)
