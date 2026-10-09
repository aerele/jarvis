"""Declarative equivalents of the supported ERPNext Desk Link queries.

Desk installs these through form scripts, not DocField.link_filters. Keep the
scope explicit: a similarly named custom field must not acquire these rules.
Configured DocField filters travel separately and retain Desk's override order.
"""


def draft_link_query_filters(doctype, df, parent_doctype=None, parentfield=None):
	if df.fieldtype != "Link":
		return []
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
