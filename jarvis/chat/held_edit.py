"""Edit & create on a held File Box create (unified pending action PR-2d, D11).

The approver's values are a PATCH over the sealed values, one per doc. They can
fill or correct fields, never change what the row is about:

- locked: the dedup-key fields (tax id, party name) and fields that refer to
  another record of the same batch;
- refused: secret fields (shown masked), protected / unknown / layout fields,
  permlevel > 0 fields the approver can't write (never silently reset), ``name``
  unless the doctype is prompt-named, and a child row's keys; a child patch edits
  the proposed rows in place (no rows added).

``view`` is what the board shows the owner / a System Manager (the AC-U4
carve-out): the proposed values with secrets masked, plus the locked fields."""

from __future__ import annotations

from functools import partial

import frappe
from frappe import _

from jarvis.chat import held_parties
from jarvis.chat._record_summary import is_secret

EDIT_TOOLS = held_parties.CREATE_TOOLS
MASK = "••••••"
_LINKS = ("Link", "Dynamic Link")


def _identities(items: list[dict], index: int) -> set[str]:
	"""The names the OTHER records of the batch will be known by."""
	out = set()
	for j, other in enumerate(items):
		if j == index:
			continue
		field = held_parties.title_field(frappe.get_meta(other["doctype"]))
		for value in (other["values"].get(field) if field else None, other["values"].get("name")):
			if isinstance(value, str) and value.strip():
				out.add(value.strip())
	return out


def locked_fields(items: list[dict], index: int) -> dict[str, str]:
	"""``{key: reason}`` for doc ``index``; a child cell's key is ``table.idx.field``."""
	item = items[index]
	meta = frappe.get_meta(item["doctype"])
	locked = {}
	if held_parties.is_party_key(held_parties.item_key(item)):
		for field in (held_parties.tax_field(meta), held_parties.title_field(meta)):
			if field:
				locked[field] = _("It identifies this record: to change it, Skip or Use existing.")
	others = _identities(items, index)
	if not others:
		return locked
	reason = _("It refers to another record in this batch.")
	for df in meta.fields:
		value = item["values"].get(df.fieldname)
		if df.fieldtype in _LINKS and isinstance(value, str) and value.strip() in others:
			locked[df.fieldname] = reason
		if df.fieldtype == "Table" and isinstance(value, list):
			child = frappe.get_meta(df.options)
			for idx, row in enumerate(value, start=1):
				for cdf in child.fields:
					cell = row.get(cdf.fieldname) if isinstance(row, dict) else None
					if cdf.fieldtype in _LINKS and isinstance(cell, str) and cell.strip() in others:
						locked[f"{df.fieldname}.{idx}.{cdf.fieldname}"] = reason
	return locked


def _masked(meta, values: dict) -> tuple[dict, list[str]]:
	out, secret = {}, []
	for key, value in values.items():
		df = meta.get_field(key)
		if df and df.fieldtype == "Table" and isinstance(value, list):
			child = frappe.get_meta(df.options)
			out[key] = [_masked(child, r)[0] if isinstance(r, dict) else {} for r in value]
		elif is_secret(meta, key):
			out[key] = MASK
			secret.append(key)
		else:
			out[key] = value
	return out, secret


def view(items: list[dict]) -> list[dict]:
	"""Per doc: ``{doctype, values (secrets masked), secret, locked}``."""
	docs = []
	for index, item in enumerate(items):
		values, secret = _masked(frappe.get_meta(item["doctype"]), item["values"])
		docs.append(
			{
				"doctype": item["doctype"],
				"values": values,
				"secret": secret,
				"locked": locked_fields(items, index),
			}
		)
	return docs


def patches_of(values) -> list[dict]:
	"""The approver's per-doc patches: a list (one per doc) or a dict for doc 0."""
	if isinstance(values, str):
		try:
			values = frappe.parse_json(values)
		except ValueError:
			values = None  # malformed JSON: refused below, never a 500
	if isinstance(values, dict):
		values = [values]
	if not isinstance(values, list) or not all(v is None or isinstance(v, dict) for v in values):
		frappe.throw(_("values must be a list of field maps, one per record"))
	return [v or {} for v in values]


def _same(a, b) -> bool:
	return str("" if a is None else a).strip() == str("" if b is None else b).strip()


def _field_error(meta, field: str, value, sealed, locked: dict, key: str, *, prompt: bool) -> str:
	"""Why ``field`` can't take ``value`` in this patch, or ""."""
	from jarvis.chat.api import _NON_EDIT_FIELDTYPES
	from jarvis.tools.create_doc import PROTECTED_FIELDS

	if field == "name":
		return "" if prompt else _("The name is set automatically.")
	if field in PROTECTED_FIELDS:
		return _("This field can't be set.")
	if is_secret(meta, field):
		return _("Secret fields can't be edited here.")
	df = meta.get_field(field)
	if not df or df.fieldtype in _NON_EDIT_FIELDTYPES:
		return _("{0} has no field {1}.").format(_(meta.name), field)
	if key in locked and not _same(value, sealed):
		return locked[key]
	return ""


def _permlevel_error(meta, field: str, value, writable: list) -> str:
	df = meta.get_field(field)
	if df and df.permlevel and df.permlevel not in writable and value not in (None, "", []):
		return _("You can't set {0}: it needs higher access.").format(_(df.label or field))
	return ""


def _unshown_errors(meta, sealed: dict, patch: dict, exec_user: str, fail) -> None:
	"""S3, approving for someone else: the approver's rights never reach a proposed
	value the board's form can't show (it renders editable fields and each table's
	form columns), nor one ``exec_user`` couldn't set. Their own patch is theirs."""
	from frappe.model import child_table_fields, default_fields

	from jarvis.chat.actions_api import _child_columns
	from jarvis.chat.api import _NON_EDIT_FIELDTYPES

	unshown = _("The board can't show this proposed value: Skip, or create the record in Desk.")
	standard = {*default_fields, *child_table_fields}
	writable = meta.get_permlevel_access("write", user=exec_user)
	for field, value in sealed.items():
		if field in standard or value in (None, "", []):
			continue
		df = meta.get_field(field)
		if df and df.fieldtype == "Table":
			# a patched row cell is the approver's; its other hidden cells are not
			rows_patch = patch.get(field) if isinstance(patch.get(field), list) else []
			shown = {c["fieldname"] for c in _child_columns(df.options)}
			for idx, row in enumerate(value if isinstance(value, list) else [], start=1):
				cells = rows_patch[idx - 1] if idx <= len(rows_patch) else None
				mine = cells if isinstance(cells, dict) else {}
				for cfield, cvalue in (row if isinstance(row, dict) else {}).items():
					if cfield in shown or cfield in standard or cfield in mine or cvalue in (None, "", []):
						continue
					fail(cfield, unshown, parentfield=field, idx=idx)
		elif field in patch:
			continue
		elif not df or df.fieldtype in _NON_EDIT_FIELDTYPES:
			fail(field, unshown)
		elif df.permlevel and df.permlevel not in writable:
			fail(
				field,
				_("{0} needs access the person who dropped the file doesn't have.").format(
					_(df.label or field)
				),
			)


def apply_patches(
	items: list[dict], patches: list[dict], approver: str, exec_user: str | None = None
) -> tuple[list[dict], list[dict]]:
	"""``(merged_items, errors)``; ``errors`` = ``[{doc_index, fieldname, parentfield?,
	idx?, message}]``, empty when the patches may be applied. ``exec_user``: whose
	values these are, when not the approver's own (S3)."""
	errors = []
	if len(patches) > len(items):
		errors.append({"doc_index": len(items), "fieldname": "", "message": _("Unknown record.")})
	merged = [
		patch_one(items, index, patches[index] if index < len(patches) else {}, approver, errors, exec_user)
		for index in range(len(items))
	]
	return merged, errors


def patch_one(
	items: list[dict], index: int, patch: dict, user: str, errors: list, exec_user: str | None = None
) -> dict:
	"""Doc ``index`` with ``patch`` applied (``apply_patches``' rules), fitted to the
	permlevel access of ``user`` (who creates it); its refusals are appended to
	``errors``."""
	item = items[index]
	meta = frappe.get_meta(item["doctype"])
	locked = locked_fields(items, index)
	prompt = (meta.autoname or "").lower().startswith("prompt")
	writable = meta.get_permlevel_access("write", user=user)
	values = dict(item["values"])
	fail = partial(_fail, errors, index)
	for field, value in patch.items():
		df = meta.get_field(field)
		if df and df.fieldtype == "Table":
			values[field] = _patch_rows(meta, df, item["values"].get(field), value, locked, fail)
			continue
		message = _field_error(meta, field, value, item["values"].get(field), locked, field, prompt=prompt)
		if message:
			fail(field, message)
		else:
			values[field] = value
	# Frappe resets a new doc's main fields the creator can't write (never a
	# child row's, never for Administrator): refuse instead of losing the value.
	if user != "Administrator":
		for field, value in values.items():
			message = _permlevel_error(meta, field, value, writable)
			if message:
				fail(field, message)
	if exec_user and exec_user != user:
		_unshown_errors(meta, item["values"], patch, exec_user, fail)
	return {**item, "values": values}


def _fail(errors: list, index: int, field: str, message: str, **where) -> None:
	errors.append({"doc_index": index, "fieldname": field, "message": message, **where})


def _patch_rows(meta, df, sealed, patch, locked: dict, fail) -> list:
	"""A table patch edits the proposed rows in place, by position."""
	rows = [dict(r) if isinstance(r, dict) else {} for r in (sealed or [])]
	if not isinstance(patch, list) or len(patch) > len(rows):
		fail(df.fieldname, _("Rows can't be added or removed here: use the full form in Desk."))
		return rows
	child = frappe.get_meta(df.options)
	for idx, cells in enumerate(patch, start=1):
		if cells is not None and not isinstance(cells, dict):
			fail(df.fieldname, _("Each row must be a map of fields."), parentfield=df.fieldname, idx=idx)
			continue
		for cfield, cvalue in (cells or {}).items():
			# A row's name / parent* / idx / docstatus / owner: never (protected, no prompt).
			key = f"{df.fieldname}.{idx}.{cfield}"
			message = _field_error(
				child, cfield, cvalue, rows[idx - 1].get(cfield), locked, key, prompt=False
			)
			if message:
				fail(cfield, message, parentfield=df.fieldname, idx=idx)
			else:
				rows[idx - 1][cfield] = cvalue
	return rows


def call_args(tool: str, args: dict, items: list[dict]) -> dict:
	"""The sealed call rebuilt with the merged values (every other key kept)."""
	if tool == "create_docs" or isinstance(args.get("docs"), list):
		return {**args, "docs": [{"doctype": i["doctype"], "values": i["values"]} for i in items]}
	return {**args, "doctype": items[0]["doctype"], "values": items[0]["values"]}


def created_refs(tool: str, args: dict, result) -> list[tuple[str, str]]:
	"""``[(doctype, name)]`` the create made, in order."""
	data = result.get("data") if isinstance(result, dict) else None
	if not isinstance(data, dict):
		return []
	if isinstance(data.get("created"), list):
		return [(c.get("doctype"), c.get("name")) for c in data["created"] if isinstance(c, dict)]
	return [(args.get("doctype"), data.get("name"))] if data.get("name") else []
