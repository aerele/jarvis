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
enabled sales Item with no variants. This also applies to the draft editor's
grid. This scoped rule does not reproduce ERPNext's customer-specific or
end-of-life checks; the document's normal validation remains authoritative.

A bare `{"q":"Item","type":"link","doctype":"Item"}` deliberately remains a
generic Item lookup. Do not use it for a missing Sales Invoice line item.
