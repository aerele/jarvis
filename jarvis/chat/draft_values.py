"""The values a person saves from a draft card's edit form, checked before anything is
claimed (``draft_save``).

``values`` maps field names to values; a child table is a list of row dicts. A key the
person may not set here is refused, never dropped: a protected or bookkeeping field, a
secret, a layout or display field, a field the doctype does not have, or a value above
the permission level they can write (Frappe would reset it without a word).

On an update a row's own ``name`` says which saved row it is: it must be a row the
record has for that table, or one the draft itself named. A row without ``name`` is a
new row, and a saved row left out of the list is removed: a table sent is the table's
complete final rows (``tools._child_rows.merge_child_rows``).

``edited_call``, ``errors`` and ``summary`` are pure (the site's answers come in
through a rules object), so their cases run without a site; ``validate`` reads it."""

from __future__ import annotations

TABLE, MULTI = "Table", "Table MultiSelect"
NOT_ROWS = "Send this table as a list of rows."
NOT_A_ROW = "This row is not on the record. Remove it, or add it as a new row."
KEEP_A_ROW = "Keep the saved rows you want, or clear the table and save before adding new rows."
_SHOWN = 3


def edited_call(tool: str, args: dict, values: dict, *, prompt_name: bool = False) -> dict:
	"""The sealed call with the person's values. An update writes them over the draft's
	own ``changes``: a table sent is that table's final rows, a table not sent keeps the
	draft's. A create writes exactly ``values``, plus the name the draft typed for a
	prompt-named doctype when the form sent none."""
	if tool == "update_doc":
		return {**args, "changes": {**(args.get("changes") or {}), **values}}
	sealed = args.get("values") if isinstance(args.get("values"), dict) else {}
	out = dict(values)
	if prompt_name and "name" not in out and sealed.get("name"):
		out["name"] = sealed["name"]
	return {**args, "values": out}


def errors(values: dict, rules, *, is_update: bool) -> list[dict]:
	"""Every refused key of ``values``, as the panel's field marks (``rules.mark``)."""
	out = []
	for field, value in values.items():
		kind = rules.table_kind(field)
		if kind:
			out.extend(_table_errors(field, kind, value, rules, is_update=is_update))
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
	"""``errors`` against the site for ``user``, who saves. ``name`` and ``base``: on an
	update, the record and the draft's own ``changes`` (rows it named are known rows)."""
	rules = _SiteRules(doctype, is_update=is_update, user=user, name=name, base=base or {})
	return errors(values, rules, is_update=is_update)


def _table_errors(table: str, kind: str, rows, rules, *, is_update: bool) -> list[dict]:
	if rows is None:
		return []  # clears the table, as update_doc does
	if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
		return [rules.mark(table, NOT_ROWS)]
	known = (rules.saved_rows(table) | rules.draft_rows(table)) if is_update else frozenset()
	out = []
	for idx, row in enumerate(rows, start=1):
		for key, value in row.items():
			if is_update and key == "name":
				# The row's identity, not a value: empty means a new row.
				message = "" if not value or (isinstance(value, str) and value in known) else NOT_A_ROW
			else:
				message = rules.cell_error(table, key, value)
			if message:
				out.append(rules.mark(key, message, table=table, idx=idx))
	# update_doc refuses to replace saved rows it cannot match by name.
	if is_update and kind == TABLE and rows and not any(r.get("name") for r in rows):
		if rules.saved_rows(table):
			out.append(rules.mark(table, KEEP_A_ROW))
	return out


class _SiteRules:
	"""The site's answers for ``errors``: field rules from ``held_edit`` (Edit & create's
	own), and on an update the record's saved row names, one read per table."""

	def __init__(self, doctype: str, *, is_update: bool, user: str, name: str, base: dict):
		import frappe

		self.meta = frappe.get_meta(doctype)
		self.doctype, self.name, self.base, self.user = doctype, name, base, user
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

		message = _field_error(self.meta, field, value, None, {}, field, prompt=self.prompt)
		if not message and self.levels is not None:
			message = _permlevel_error(self.meta, field, value, self.levels)
		return message

	def cell_error(self, table, field, value) -> str:
		from jarvis.chat.held_edit import _field_error, _permlevel_error

		child = self._child(table)
		message = _field_error(child, field, value, None, {}, field, prompt=False)
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

	def draft_rows(self, table) -> frozenset:
		rows = self.base.get(table)
		if not isinstance(rows, list):
			return frozenset()
		return frozenset(r["name"] for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str))

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


def _label(meta, field) -> str:
	from frappe import _

	df = meta.get_field(field)
	return _(df.label) if df and df.label else str(field)
