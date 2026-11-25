"""Declarative equivalents of the supported ERPNext Desk Link queries.

Desk installs these through form scripts, not DocField.link_filters. Keep the
scope explicit: a similarly named custom field must not acquire these rules.
Configured DocField filters travel separately and retain Desk's override order.

Item rules cover ordinary purchasing, not supplier restrictions, end-of-life or
subcontracting-specific queries. Legacy old-flow subcontracting can use items
without Allow Purchase; choose those in ERPNext Desk instead of this picker.
"""

_ITEM_CONTEXTS = {
	"Sales Invoice Item": ("Sales Invoice", "is_sales_item"),
	"Purchase Invoice Item": ("Purchase Invoice", "is_purchase_item"),
	"Purchase Order Item": ("Purchase Order", "is_purchase_item"),
	"Purchase Receipt Item": ("Purchase Receipt", "is_purchase_item"),
	"Supplier Quotation Item": ("Supplier Quotation", "is_purchase_item"),
}


# erpnext/accounts/doctype/payment_entry/payment_entry.js: references set_query.
# Type: Desk narrows it by the payment's party type (Customer: Sales Order, Sales Invoice,
# Journal Entry, Dunning; Supplier: Purchase Order, Purchase Invoice, Journal Entry). A static
# filter cannot branch on party_type, so the picker offers their union; the save still refuses
# a type that does not fit the party (PaymentEntry.validate_reference_documents).
_PAYMENT_REFERENCE_TYPES = [
	"Sales Order",
	"Sales Invoice",
	"Purchase Order",
	"Purchase Invoice",
	"Journal Entry",
	"Dunning",
]
# Name: submitted documents of the payment's company, and for these the payment's party in the
# document's own party field (Desk sets filters[party_type.lower()] = party).
_PAYMENT_REFERENCE_PARTY_FIELD = {
	"Sales Order": "customer",
	"Sales Invoice": "customer",
	"Dunning": "customer",
	"Purchase Order": "supplier",
	"Purchase Invoice": "supplier",
}


def _payment_reference_filters(df):
	if df.fieldname == "reference_doctype" and df.fieldtype == "Link" and df.options == "DocType":
		return [["DocType", "name", "in", list(_PAYMENT_REFERENCE_TYPES)]]
	if (
		df.fieldname == "reference_name"
		and df.fieldtype == "Dynamic Link"
		and df.options == "reference_doctype"
	):
		# One set per DocType the Name can point at; the card applies the current Type's.
		out = []
		for target in _PAYMENT_REFERENCE_TYPES:
			out += [[target, "docstatus", "=", 1], [target, "company", "=", "eval:parent.company"]]
			if target in _PAYMENT_REFERENCE_PARTY_FIELD:
				out.append([target, _PAYMENT_REFERENCE_PARTY_FIELD[target], "=", "eval:parent.party"])
		return out
	return []


def draft_link_query_filters(doctype, df, parent_doctype=None, parentfield=None):
	if (
		doctype == "Payment Entry Reference"
		and parent_doctype == "Payment Entry"
		and parentfield == "references"
	):
		return _payment_reference_filters(df)
	if df.fieldtype != "Link":
		return []
	# ERPNext sales_common.js and buying.js: canonical items/item_code queries.
	item_parent, eligibility = _ITEM_CONTEXTS.get(doctype, (None, None))
	if (
		item_parent
		and parent_doctype == item_parent
		and parentfield == "items"
		and df.fieldname == "item_code"
		and df.options == "Item"
	):
		return [
			["Item", eligibility, "=", 1],
			["Item", "disabled", "=", 0],
			["Item", "has_variants", "=", 0],
		]
	# erpnext/accounts/doctype/purchase_invoice/purchase_invoice.js: credit_to.get_query
	if doctype == "Purchase Invoice" and df.fieldname == "credit_to" and df.options == "Account":
		return [
			["Account", "account_type", "=", "Payable"],
			["Account", "is_group", "=", 0],
			["Account", "company", "=", "eval:doc.company"],
		]
	# erpnext/stock/doctype/material_request/material_request.js: onload set_query
	if df.options == "Warehouse":
		if doctype == "Material Request" and df.fieldname in {"set_warehouse", "set_from_warehouse"}:
			company = "eval:doc.company"
		elif (
			doctype == "Material Request Item"
			and parent_doctype == "Material Request"
			and parentfield == "items"
			and df.fieldname == "warehouse"
		):
			company = "eval:parent.company"
		else:
			return []
		return [["Warehouse", "company", "=", company], ["Warehouse", "is_group", "=", 0]]
	return []
