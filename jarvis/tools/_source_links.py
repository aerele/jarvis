"""Put back the source links of a document built from another one (admin-v2#782).

``get_creation_context`` runs the ERP's own mapper (``_source_mapper``) and gives the agent a
``mapped`` draft whose rows carry links to the source: a Purchase Invoice row's
``purchase_order`` / ``po_detail``, a Sales Invoice row's ``sales_order`` / ``so_detail``. Those
fields are read-only (often hidden) on the form, so an agent that writes the rows it can see
leaves them out, and the saved document is not linked: the source still reads as unbilled, and
its advances are not offered.

``create_doc(mapped_from=...)`` re-runs the same mapper and fills, on the values the agent sent,
only the SOURCE LINK fields it left empty:

- a Link to the source DocType (``purchase_order`` -> Purchase Order), or
- a field whose value names one of the source's own child rows (``po_detail``).

Nothing else is copied: rates, quantities, taxes and every other value stay exactly as the agent
(and the person, through the card) set them, and a value the agent did give is never replaced.
Rows are matched to the mapped rows by item (``item_code``) in order, else by position when both
lists have the same length; a row with no match gets nothing.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._source_mapper import mapped_values

_TABLE_TYPES = ("Table", "Table MultiSelect")


def restore_source_links(doctype: str, values: dict, mapped_from) -> dict:
	"""``values`` with the source's link fields filled where the agent left them empty."""
	source_doctype, source_name = _source(mapped_from)
	if not frappe.db.exists(source_doctype, source_name):
		raise InvalidArgumentError(f"mapped_from: {source_doctype} {source_name} does not exist")
	mapped, note = mapped_values(source_doctype, source_name, doctype)
	if not mapped:
		raise InvalidArgumentError(
			note or f"{doctype} cannot be built from {source_doctype} {source_name} on this site"
		)
	source_rows = _source_row_names(source_doctype, source_name)
	meta = frappe.get_meta(doctype)
	out = dict(values)
	_fill(meta, out, mapped, source_doctype, source_name, source_rows)
	for df in meta.fields:
		if df.fieldtype not in _TABLE_TYPES or not df.options:
			continue
		rows, mapped_rows = out.get(df.fieldname), mapped.get(df.fieldname)
		if not isinstance(rows, list) or not isinstance(mapped_rows, list) or not mapped_rows:
			continue
		child = frappe.get_meta(df.options)
		filled = []
		for row, match in zip(rows, _match(rows, mapped_rows), strict=True):
			if isinstance(row, dict) and match is not None:
				row = dict(row)
				_fill(child, row, match, source_doctype, source_name, source_rows)
			filled.append(row)
		out[df.fieldname] = filled
	return out


def _source(mapped_from) -> tuple[str, str]:
	if not isinstance(mapped_from, dict):
		raise InvalidArgumentError(
			"mapped_from must be {source_doctype, source_name} (get_creation_context's mapped_from)"
		)
	source_doctype, source_name = mapped_from.get("source_doctype"), mapped_from.get("source_name")
	if not (
		isinstance(source_doctype, str) and source_doctype and isinstance(source_name, str) and source_name
	):
		raise InvalidArgumentError("mapped_from needs source_doctype and source_name")
	return source_doctype, source_name


def _source_row_names(source_doctype: str, source_name: str) -> set[str]:
	"""The names of the source's child rows (what a ``po_detail`` / ``so_detail`` holds)."""
	names: set[str] = set()
	for df in frappe.get_meta(source_doctype).get_table_fields():
		names.update(
			frappe.get_all(
				df.options,
				filters={"parent": source_name, "parenttype": source_doctype, "parentfield": df.fieldname},
				pluck="name",
			)
		)
	return names


def _is_source_link(df, value, source_doctype: str, source_name: str, source_rows: set) -> bool:
	if df is None or value in (None, "") or isinstance(value, list | dict):
		return False
	if df.fieldtype == "Link" and df.options == source_doctype:
		return value == source_name
	return isinstance(value, str) and value in source_rows


def _fill(meta, target: dict, mapped: dict, source_doctype: str, source_name: str, source_rows: set) -> None:
	for key, value in mapped.items():
		if target.get(key) not in (None, ""):
			continue  # what the agent (or the person) set is kept
		if _is_source_link(meta.get_field(key), value, source_doctype, source_name, source_rows):
			target[key] = value


def _match(rows: list, mapped_rows: list) -> list:
	"""For each row, the mapped row it stands for (or None): by item in order, else by position
	when the two lists have the same length. Each mapped row is used once."""
	used: set[int] = set()
	out = []
	same_length = len(rows) == len(mapped_rows)
	for i, row in enumerate(rows):
		match = None
		item = row.get("item_code") if isinstance(row, dict) else None
		if item:
			for j, candidate in enumerate(mapped_rows):
				if j not in used and candidate.get("item_code") == item:
					match = j
					break
		elif same_length and i not in used:
			match = i
		if match is not None:
			used.add(match)
			out.append(mapped_rows[match])
		else:
			out.append(None)
	return out
