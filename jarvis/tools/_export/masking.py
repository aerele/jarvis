"""Frappe 16 field masking for a spec export.

``get_list`` masks a ``mask=1`` field (phone, email, date ...) for a user without
the "mask" permission, but only inside its own result handling
(``DatabaseQuery.mask_fields``). A spec export runs a raw query-builder SELECT, so
it never passes through that step and would write the real values into the file.
This applies the same rule with Frappe's own helpers: a plain masked column is
masked value by value, exactly as ``get_list`` would; a computed column over a
masked field is refused, because its result (``upper(phone)``, ``max(phone)``)
would carry the real value in a form no helper can mask.

Frappe 15 has no masking (no ``mask_field_value``, no ``Meta.get_masked_fields``),
so there every function here is a no-op. Detected by the helper's presence, not by
version. Administrator is never masked (``get_masked_fields`` answers empty).

A Frappe 16 build before July 2026 masks a child table by its own rules, not its
parent's, because its ``get_masked_fields`` takes no ``parenttype``. There the
lookup follows that build (``compat.masked_fields_of``), as its ``get_list`` does."""

import frappe

from jarvis import compat
from jarvis.exceptions import InvalidArgumentError

try:
	from frappe.model.utils.mask import mask_field_value
except ImportError:  # Frappe 15: nothing to mask
	mask_field_value = None


def masked_fields(tables: dict, child_parents: dict) -> dict[str, dict]:
	"""``{alias: {fieldname: DocField}}`` of the fields masked for the session user,
	only for aliases that have any. A child table takes its masking from the parent
	its rows are scoped to, as ``get_list(parent_doctype=...)`` does."""
	if mask_field_value is None:
		return {}
	out = {}
	for alias, doctype in tables.items():
		fields = compat.masked_fields_of(frappe.get_meta(doctype), child_parents.get(alias))
		if fields:
			out[alias] = {df.fieldname: df for df in fields}
	return out


def refuse_masked_expressions(columns: list, query, masked: dict) -> None:
	"""Raise before the SELECT runs when a computed column reads a masked field.
	The references come from the built query's own select terms, so every shape
	(an aggregate, an expression, a CASE with a predicate) is covered alike."""
	if not masked:
		return
	for column, term in zip(columns, query._selects, strict=True):
		if column.alias is not None:  # a plain column is masked after the fetch
			continue
		for ref in term.fields_():
			alias = getattr(ref.table, "alias", None)
			if ref.name in masked.get(alias, {}):
				raise InvalidArgumentError(
					f"column {column.key!r} computes over {alias}.{ref.name}, which is masked for "
					f"you (you lack the mask permission), and a computed value would show it "
					f"unmasked. Select {alias}.{ref.name} on its own to get it masked as in a "
					f"list, or drop {column.key!r}."
				)


def mask_rows(columns: list, rows: list, masked: dict) -> None:
	"""Mask, in place, every plain column whose field is masked for the user."""
	for column in columns:
		field = masked.get(column.alias, {}).get(column.key)
		if field is None:
			continue
		for row in rows:
			row[column.key] = mask_field_value(field, row.get(column.key))
