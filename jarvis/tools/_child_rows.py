"""Apply a child-table payload to an existing document without losing data.

``doc.set(table, rows)`` deletes every existing row and re-inserts the payload,
so any field the payload does not carry (custom fields, ``so_detail`` links,
project, cost centre) is silently lost. Instead, rows are matched by their
``name``:

* a row with ``name`` updates that existing row, only the keys it carries;
* a row without ``name`` is a new row;
* existing rows the payload does not name are removed (the payload is the
  complete final set);
* no row names at all on a table that already has rows is refused, never
  guessed at by position; ``[]`` clears the table.
"""

from jarvis.exceptions import InvalidArgumentError

# Keys Frappe owns on a child row; a payload may not move or re-parent a row.
_ROW_SYSTEM_KEYS = frozenset(
	{
		"name",
		"idx",
		"parent",
		"parentfield",
		"parenttype",
		"doctype",
		"docstatus",
		"owner",
		"creation",
		"modified",
		"modified_by",
	}
)


def merge_child_rows(doc, fieldname: str, rows) -> None:
	"""Replace ``doc.<fieldname>`` with ``rows``, keeping existing rows by name."""
	if rows is None:  # null clears the table, as doc.set always did
		rows = []
	_check_rows(fieldname, rows)
	existing = {row.name: row for row in doc.get(fieldname) or []}
	names = [r["name"] for r in rows if r.get("name")]
	if existing and rows and not names:
		raise InvalidArgumentError(_nameless_message(doc, fieldname, len(existing)))
	unknown = [n for n in names if n not in existing]
	if unknown:
		raise InvalidArgumentError(
			f"'{fieldname}' has no row named {', '.join(map(str, unknown))} on this document"
		)
	repeated = sorted({n for n in names if names.count(n) > 1})
	if repeated:
		raise InvalidArgumentError(f"'{fieldname}' lists row {', '.join(repeated)} more than once")
	final = []
	for row in rows:
		values = row_values(row)
		if row.get("name"):
			child = existing[row["name"]]
			child.update(values)
			final.append(child)
		else:
			final.append(values)
	doc.set(fieldname, final)
	# Kept rows keep their old idx through append(); renumber so order is exact.
	for i, child in enumerate(doc.get(fieldname), 1):
		child.idx = i


def row_values(row: dict) -> dict:
	"""A payload row's writable values: no system keys and no ``__`` internals."""
	return {k: v for k, v in row.items() if k not in _ROW_SYSTEM_KEYS and not str(k).startswith("__")}


def _check_rows(fieldname: str, rows) -> None:
	if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
		raise InvalidArgumentError(f"'{fieldname}' must be a list of row objects")
	if any(r.get("name") and not isinstance(r["name"], str) for r in rows):
		raise InvalidArgumentError(f"'{fieldname}' row names must be text, as get_doc returns them")


def _nameless_message(doc, fieldname: str, count: int) -> str:
	df = doc.meta.get_field(fieldname)
	label = (df.label if df else None) or fieldname
	return (
		f"{label}: this change would replace the {count} saved row(s) without saying which is "
		f"which, so their other details could be lost. Nothing was changed; ask to redo it from "
		f"the saved rows. (Send each kept or changed row with its 'name' from get_doc; a row "
		f"without 'name' is added, a row left out is removed; to replace every row, send [] "
		f"first, then the new rows.)"
	)
