"""The values a person saves from a draft card's edit form: the call they make, and the
check it passes before anything is claimed (``draft_save``).

The contract for ``values`` (field-keyed; a child table is a list of row dicts):

- Create: the write is the draft's values with ``values`` over them. A key left out
  keeps the drafted value; a key sent as None or "" clears it (it is left out of the
  write, so the default or a blank applies). A table sent is the table's complete final
  rows: a row carrying ``_draft_row`` (the 0-based index of the drafted row it came
  from) is that drafted row with the sent cells over it, a cell sent as None or ""
  cleared; a row without it is a new row; a drafted row no sent row names is removed.
  A table left out keeps its drafted rows. A table is cleared with ``[]`` or None, never
  with a string: "" for a table is refused with a mark. ``values == {}`` is exactly the card.
- Update: the write is the draft's ``changes`` with ``values`` over them; None or ""
  sets the field blank (as update_doc does). A table sent is the table's complete final
  rows (``tools._child_rows.merge_child_rows``): a row's ``name`` says which saved row it
  is, and must be a row the record has for that table or one the draft named; a row
  without one is new; a saved row left out is removed. A table left out keeps the
  draft's own list. ``values == {}`` is exactly the card.

Only the keys sent are checked: what is carried over from the draft is written as a
plain Confirm would write it. A key sent is refused, never dropped, when the person may
not set it here: a protected or bookkeeping key (``create_doc.refused_field_names``), a
field the form cannot edit (an attachment, a secret, a layout field), a field the
doctype does not have, or a value above the permission level they can write (Frappe
would reset it without a word).

``edited_call``, ``errors`` and ``summary`` are pure (the site's answers come in through
a rules object), so their cases run without a site; ``validate`` reads it."""

from __future__ import annotations

TABLE, MULTI = "Table", "Table MultiSelect"
DRAFT_ROW = "_draft_row"
NOT_ROWS = "Send this table as a list of rows."
NOT_A_ROW = "This row is not on the record. Remove it, or add it as a new row."
KEEP_A_ROW = "Keep the saved rows you want, or clear the table and save before adding new rows."
NOT_A_DRAFTED_ROW = "This row does not match a row of the draft."
TWICE = "Another row is already this row of the draft."
CANT_SET = "This field can't be set."
CANT_EDIT = "This field can't be edited here."
_SHOWN = 3


def edited_call(tool: str, args: dict, values: dict, *, tables=frozenset()) -> dict:
	"""The sealed call with the person's values, by the contract above. ``tables``: the
	doctype's table fields (a create merges their rows by ``_draft_row``)."""
	if tool == "update_doc":
		return {**args, "changes": {**(args.get("changes") or {}), **values}}
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
	out = []
	for idx, row in enumerate(rows, start=1):
		for key, value in row.items():
			if is_update and key == "name":
				# The row's identity, not a value: empty means a new row.
				message = "" if not value or (isinstance(value, str) and value in known) else NOT_A_ROW
			elif not is_update and key == DRAFT_ROW:
				message = _draft_row_error(value, len(drafted), taken)
			else:
				message = rules.cell_error(table, key, value)
			if message:
				out.append(rules.mark(key, message, table=table, idx=idx))
	# update_doc refuses to replace saved rows it cannot match by name.
	if is_update and kind == TABLE and rows and not any(r.get("name") for r in rows):
		if rules.saved_rows(table):
			out.append(rules.mark(table, KEEP_A_ROW))
	return out


def _draft_row_error(value, count: int, taken: set) -> str:
	if not _is_index(value, count):
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

		self.meta = frappe.get_meta(doctype)
		self.doctype, self.name, self.user = doctype, name, user
		self.is_update = is_update
		self.prompt = not is_update and (self.meta.autoname or "").lower().startswith("prompt")
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
