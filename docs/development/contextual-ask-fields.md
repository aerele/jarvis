# Contextual field questions

Use a `jarvis-ask` field question when requesting a value for a document field.
A child-table field also names its parent DocType and Table field:

```json
{
  "q": "Item",
  "type": "field",
  "doctype": "Sales Invoice Item",
  "fieldname": "item_code",
  "parent": "Sales Invoice",
  "parentfield": "items"
}
```

The nested `field: {doctype, fieldname, parent, parentfield}` shape also works.
`get_creation_context` supplies `child_doctypes` with the same table keys as
`child_fields`, and explains this question shape in its returned instructions.

The metadata endpoint validates the parent/table relationship, checks the
viewer's read permissions and returns the configured and supported scripted Link
filters. The picker resolves these filters through the same helper as the draft
editor and searches using Frappe's permission-checked `search_link` endpoint.
An unsupported filter produces a typed-answer fallback with an explanation;
the picker never silently drops it.

For Sales Invoice `items.item_code`, the supported scripted filters require an
enabled sales Item with no variants. For Purchase Invoice, Purchase Order,
Purchase Receipt and Supplier Quotation, `items.item_code` requires an enabled
Item with **Allow Purchase** (`is_purchase_item`) and no variants. Both the
question picker and the draft editor's grid receive these filters. Use the
matching child DocType, for example `Purchase Invoice Item`, and parent in the
question above.

These scoped rules do not reproduce ERPNext's customer/supplier restrictions,
end-of-life checks or subcontracting-specific queries. Legacy old-flow
subcontracting drafts (`is_old_subcontracting_flow=1`) can use subcontracted-only
Items without Allow Purchase; this picker hides those Items. Use ERPNext Desk
for those exceptional drafts. Normal document validation remains authoritative.

A bare `{"q":"Item","type":"link","doctype":"Item"}` deliberately remains a
generic Item lookup. Do not use it for a missing sales or purchase line item.
