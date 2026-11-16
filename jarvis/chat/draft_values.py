"""The values a person saves from a draft card's edit form: the call they make, and the
check it passes before anything is claimed (``draft_save``).

The contract for ``values`` (field-keyed; a child table is a list of row dicts). One rule
for both verbs: send a table's COMPLETE final row list, and in each kept row send only
the cells the person changed. A row says which drafted (or saved) row it is by an
identity key, ``_draft_row`` or ``name``; it never carries both.

- Create: the write is the draft's values with ``values`` over them. A key left out
  keeps the drafted value; a key sent as None or "" clears it (it is left out of the
  write, so the default or a blank applies). A table sent is the table's complete final
  rows: a row carrying ``_draft_row`` (the 0-based index of the drafted row it came
  from) is that drafted row with the sent cells over it, a cell sent as None or ""
  cleared (left out of the row, so the default applies); a row without it is a new row;
  a drafted row no sent row names is removed. A table left out keeps its drafted rows. A
  table is cleared with ``[]`` or None, never with a string: "" for a table is refused
  with a mark. ``values == {}`` is exactly the card.
- Update: the write is the draft's ``changes`` with ``values`` over them; None or ""
  sets the field blank (as update_doc does). A table sent is the table's complete final
  rows (``tools._child_rows.merge_child_rows``); a saved row left out is removed. A row
  is one of three things:

  * a row with a ``name``, which must be a row the record has for that table or one the
    draft named. When the draft's own list for that table also names it, it is that
    drafted row with the sent cells over it, so a cell the draft set and the person did
    not touch is still written; a row the draft did not name is written as sent (the
    write merges it with the saved row);
  * a row with ``_draft_row`` and no ``name``: the draft ITSELF added that row (a drafted
    row with no ``name``), and ``_draft_row`` is its 0-based index in the draft's list
    for that table (the sealed ``changes[table]``, named rows counted). It is that
    drafted row with the sent cells over it, so a cell the person did not touch (a link
    or a datetime second the form cannot send) is still written. It is refused unless
    the index is a whole number that points at a drafted row with no ``name``, and no
    other sent row uses it. A drafted new row no sent row points at is not written;
  * a row with neither: a new row, written as sent.

  A cell sent as None or "" is the person emptying it: it is kept in the row, overrides
  the drafted cell, and the write stores it blank (``merge_child_rows`` sets it on the
  saved row; on a NEW row the key is present, so "" stays blank and the DocType default
  does not apply; a cell left out of a saved row keeps the SAVED value, which is why a
  cell must be sent to clear it). A table left out keeps the draft's own list.
  ``values == {}`` is exactly the card.

``_draft_row`` is the form's marker, never data: it is removed from every row before the
call is built, so it reaches neither the write, the stored args nor the receipt. A blank
(None or "") marker means no marker.

Only the keys sent are checked: what is carried over from the draft is written as a
plain Confirm would write it. A key sent is refused, never dropped, when the person may
not set it here: a protected or bookkeeping key (``create_doc.refused_field_names``), a
field the form cannot edit (an attachment, a secret, a layout field), a field the
doctype does not have, or a value above the permission level they can write (Frappe
would reset it without a word).

``edited_call``, ``errors`` and ``summary`` are pure (the site's answers come in through
a rules object), so their cases run without a site; ``validate`` reads it."""

from __future__ import annotations

import json

TABLE, MULTI = "Table", "Table MultiSelect"
DRAFT_ROW = "_draft_row"
NOT_ROWS = "This table needs a list of rows."
NOT_A_ROW = "This row is not on the record. Remove it, or add it as a new row."
KEEP_A_ROW = "Keep the saved rows you want, or clear the table and save before adding new rows."
NOT_A_DRAFTED_ROW = "This row does not match a row of the draft."
TWICE = "This row is listed twice."
CANT_SET = "This field can't be set."
CANT_EDIT = "This field can't be edited here."
_SHOWN = 3
# What the form may send back: rows in one child table, characters of JSON in all.
MAX_TABLE_ROWS = 500
MAX_VALUES_CHARS = 200_000


def too_big(values: dict) -> bool:
	"""More rows in one child table, or more JSON, than the form is meant to send: checked
	before any other validation so a huge body costs nothing more."""
	if any(isinstance(v, list) and len(v) > MAX_TABLE_ROWS for v in values.values()):
		return True
	return len(json.dumps(values, default=str)) > MAX_VALUES_CHARS


def edited_call(tool: str, args: dict, values: dict, *, tables=frozenset()) -> dict:
	"""The sealed call with the person's values, by the contract above. ``tables``: the
	doctype's table fields (their rows are merged with the drafted ones, by ``_draft_row``
	on a create and by ``name`` or ``_draft_row`` on an update). Nothing in ``args`` is
	changed."""
	if tool == "update_doc":
		sealed = args.get("changes") if isinstance(args.get("changes"), dict) else {}
		changes = dict(sealed)
		for key, value in values.items():
			is_rows = key in tables and isinstance(value, list)
			changes[key] = _kept_update_rows(sealed.get(key), value) if is_rows else value
		return {**args, "changes": changes}
	sealed = args.get("values") if isinstance(args.get("values"), dict) else {}
	out = dict(sealed)
	for key, value in values.items():
		if _blank(value):
			out.pop(key, None)
		elif key in tables and isinstance(value, list):
			out[key] = _merged_rows(sealed.get(key), value)
		else:
			out[key] = value
	return {**args, "values": out}


def errors(values: dict, rules, *, is_update: bool, base: dict | None = None) -> list[dict]:
	"""Every refused key of ``values``, as the panel's field marks (``rules.mark``).
	``base``: the draft's own values (a create) or ``changes`` (an update)."""
	base = base or {}
	out = []
	for field, value in values.items():
		kind = rules.table_kind(field)
		if kind:
			out.extend(_table_errors(field, kind, value, rules, is_update=is_update, drafted=base.get(field)))
			continue
		message = rules.field_error(field, value)
		if message:
			out.append(rules.mark(field, message))
	return out


def summary(found: list[dict]) -> str:
	"""The banner line of a refused save: the first marks, then how many more."""
	parts = [f"{e['where']}: {e['message']}" for e in found[:_SHOWN]]
	if len(found) > _SHOWN:
		parts.append(f"and {len(found) - _SHOWN} more")
	return "; ".join(parts)


def validate(
	doctype: str, values: dict, *, is_update: bool, user: str, name: str = "", base: dict | None = None
) -> list[dict]:
	"""``errors`` against the site for ``user``, who saves. ``base``: the draft's values or
	``changes``; ``name``: the record an update changes."""
	rules = _SiteRules(doctype, is_update=is_update, user=user, name=name)
	return errors(values, rules, is_update=is_update, base=base)


def _blank(value) -> bool:
	return value is None or value == ""


def _is_index(value, count: int) -> bool:
	return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < count


def _merged_rows(drafted, sent: list) -> list:
	drafted = drafted if isinstance(drafted, list) else []
	out = []
	for row in sent:
		index = row.get(DRAFT_ROW)
		was = drafted[index] if _is_index(index, len(drafted)) and isinstance(drafted[index], dict) else {}
		cells = dict(was)  # carried over exactly as drafted, blank or not
		for key, value in row.items():
			if key == DRAFT_ROW:
				continue
			if _blank(value):
				cells.pop(key, None)  # only a cell the client SENT empty is cleared
			else:
				cells[key] = value
		out.append(cells)
	return out


def _kept_update_rows(drafted, sent: list) -> list:
	"""An update's sent rows over the drafted ones: a row of the same ``name``, or the
	drafted new row a nameless row's ``_draft_row`` points at. A sent cell wins whatever it
	holds (None and "" too: the write blanks it); a row the draft did not name is passed on
	as sent. The marker never survives."""
	drafted = drafted if isinstance(drafted, list) else []
	was = {r["name"]: r for r in drafted if isinstance(r, dict) and _row_name(r)}
	out = []
	for row in sent:
		if not isinstance(row, dict):
			out.append(row)
			continue
		base = was.get(_row_name(row)) if _row_name(row) else _drafted_new_row(drafted, row)
		merged = {**(base or {}), **row}
		merged.pop(DRAFT_ROW, None)
		out.append(merged)
	return out


def _drafted_new_row(drafted: list, row: dict) -> dict | None:
	"""The drafted row without a ``name`` that a sent row without one points at, or None."""
	index = row.get(DRAFT_ROW)
	if row.get("name") or not _is_index(index, len(drafted)):
		return None
	entry = drafted[index]
	return entry if isinstance(entry, dict) and not entry.get("name") else None


def _row_name(row: dict) -> str:
	name = row.get("name")
	return name if isinstance(name, str) else ""


def _table_errors(table: str, kind: str, rows, rules, *, is_update: bool, drafted) -> list[dict]:
	if rows is None:
		return []  # clears the table
	if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
		return [rules.mark(table, NOT_ROWS)]
	drafted = drafted if isinstance(drafted, list) else []
	known = frozenset()
	if is_update:
		named = {r.get("name") for r in drafted if isinstance(r, dict)}
		known = rules.saved_rows(table) | {n for n in named if isinstance(n, str)}
	taken: set[int] = set()
	named_rows: set[str] = set()
	out = []
	for idx, row in enumerate(rows, start=1):
		for key, value in row.items():
			if is_update and key == "name":
				# The row's identity, not a value: empty means a new row.
				message = "" if not value or (isinstance(value, str) and value in known) else NOT_A_ROW
				if not message and value:
					# The same saved row twice: the write's own refusal never reaches the form.
					message = TWICE if value in named_rows else ""
					named_rows.add(value)
			elif key == DRAFT_ROW:
				# None or "" is a client's way of writing "no marker": a new row.
				message = "" if _blank(value) else _draft_row_error(value, drafted, taken, row, is_update)
			else:
				message = rules.cell_error(table, key, value)
			if message:
				out.append(rules.mark(key, message, table=table, idx=idx))
	# update_doc refuses to replace saved rows it cannot match by name.
	if is_update and kind == TABLE and rows and not any(r.get("name") for r in rows):
		if rules.saved_rows(table):
			out.append(rules.mark(table, KEEP_A_ROW))
	return out


def _draft_row_error(value, drafted: list, taken: set, row: dict, is_update: bool) -> str:
	"""Why ``_draft_row`` ``value`` is refused on ``row``, or "". On an update it may only
	point at a drafted row that has no ``name`` (the draft's own new row), and only from a
	row that has none either."""
	if not _is_index(value, len(drafted)):
		return NOT_A_DRAFTED_ROW
	if is_update:
		entry = drafted[value]
		if row.get("name") or not isinstance(entry, dict) or entry.get("name"):
			return NOT_A_DRAFTED_ROW
	if value in taken:
		return TWICE
	taken.add(value)
	return ""


class _SiteRules:
	"""The site's answers for ``errors``: field rules from ``held_edit`` (Edit & create's
	own), and on an update the record's saved row names, one read per table."""

	def __init__(self, doctype: str, *, is_update: bool, user: str, name: str):
		import frappe

		from jarvis.chat import live_draft

		self.meta = frappe.get_meta(doctype)
		self.doctype, self.name, self.user = doctype, name, user
		self.is_update = is_update
		self.prompt = not is_update and live_draft.prompt_named(self.meta.autoname)
		# Frappe never resets a value Administrator sets.
		self.levels = None if user == "Administrator" else self.meta.get_permlevel_access("write", user=user)
		self._saved: dict[str, frozenset] = {}
		self._readable: bool | None = None

	def table_kind(self, field) -> str | None:
		df = self.meta.get_field(field)
		return df.fieldtype if df and df.fieldtype in (TABLE, MULTI) and df.options else None

	def field_error(self, field, value) -> str:
		from jarvis.chat.held_edit import _field_error, _permlevel_error

		if self._refused(field):
			return CANT_SET
		message = _not_editable(self.meta, field) or _field_error(
			self.meta, field, value, None, {}, field, prompt=self.prompt
		)
		if not message and self.levels is not None:
			message = _permlevel_error(self.meta, field, value, self.levels)
		return message

	def cell_error(self, table, field, value) -> str:
		from jarvis.chat.held_edit import _field_error, _permlevel_error

		child = self._child(table)
		message = _not_editable(child, field) or _field_error(
			child, field, value, None, {}, field, prompt=False
		)
		# Saving an existing record, Frappe also resets row cells above the writer's level.
		if not message and self.is_update and self.levels is not None:
			message = _permlevel_error(child, field, value, self.levels)
		return message

	def saved_rows(self, table) -> frozenset:
		if not (self.is_update and self.name):
			return frozenset()
		if table not in self._saved:
			self._saved[table] = self._read_rows(table)
		return self._saved[table]

	def mark(self, field, message: str, *, table=None, idx=None) -> dict:
		"""One mark as the panel reads it (``panel_fields.mark_entry``), ``invalid`` so it
		is drawn as a refused value, never as a missing one."""
		from frappe import _

		if not table:
			label = _label(self.meta, field)
			return {"fieldname": field, "label": label, "where": label, "message": _(message), "invalid": 1}
		label = _label(self._child(table), field)
		return {
			"fieldname": field,
			"label": label,
			"parentfield": table,
			"idx": idx,
			"where": _("{0} row {1}: {2}").format(_label(self.meta, table), idx, label),
			"message": _(message),
			"invalid": 1,
		}

	def _refused(self, field) -> bool:
		"""A key the write tool refuses itself (protected, ``amended_from``, ``_``-names)."""
		from jarvis.tools import update_doc
		from jarvis.tools.create_doc import PROTECTED_FIELDS, refused_field_names

		protected = update_doc.PROTECTED_FIELDS if self.is_update else PROTECTED_FIELDS
		return bool(refused_field_names([field], protected))

	def _child(self, table):
		import frappe

		return frappe.get_meta(self.meta.get_field(table).options)

	def _read_rows(self, table) -> frozenset:
		import frappe

		if self._readable is None:
			self._readable = bool(frappe.has_permission(self.doctype, "read", doc=self.name, user=self.user))
		if not self._readable:
			return frozenset()
		# get_all: the person's read permission on the parent record is checked just above.
		names = frappe.get_all(
			self.meta.get_field(table).options,
			filters={"parent": self.name, "parenttype": self.doctype, "parentfield": table},
			pluck="name",
		)
		return frozenset(names)


def _not_editable(meta, field) -> str:
	"""A real field the form cannot edit (an attachment, a layout field): never "no such
	field". The person can neither set nor clear it here; a drafted value is kept."""
	from jarvis.chat.api import _NON_EDIT_FIELDTYPES

	df = meta.get_field(field)
	return CANT_EDIT if df and df.fieldtype in _NON_EDIT_FIELDTYPES else ""


def _label(meta, field) -> str:
	from frappe import _

	df = meta.get_field(field)
	return _(df.label) if df and df.label else str(field)


def reply_follows(status: str, settled: bool, final_draft: bool) -> bool:
	"""Whether the agent answers in the chat because this row ended. Only an Executed or
	Failed row's settle queues a continuation turn, and a saved final draft queues none (the
	agent hears of it on its next turn). A Discarded / Superseded row leaves a passive note
	and a Cancelled one says nothing: the agent was (or was not) told, but nothing is coming."""
	return bool(settled) and status in ("Executed", "Failed") and not final_draft
