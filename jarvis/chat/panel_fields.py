"""Which draft-panel fields a failed save should mark (R2-9, round 2).

The panel keeps itself open for any failure it can fix, with the problem fields
marked: a required value left empty (``missing``) and a value the field does not
accept (``invalid``: an option the Select does not list, a link to a record that
does not exist, a value the field type check refuses). Every mark is worked out
from the draft itself, not parsed from the error, so a save that failed on one
problem still marks the others (a missing value AND a bad link are both marked).

Only top-level values are checked for ``invalid``; a child row's bad value stays in
the message (the panel's grid has no per-cell marks). A missing value the panel
cannot fill (a table, a secret, permlevel above 0) makes ``missing`` None: the panel
cannot fix that save."""

from __future__ import annotations

import frappe
from frappe import _

from jarvis.exceptions import InvalidFieldValueError


def invalid_marks(verb: str, doctype: str, name: str, values: dict) -> list[dict]:
	"""Marks for top-level values the fields refuse."""
	doc = _draft_doc(verb, doctype, name, values)
	return [] if doc is None else _invalid(doc.meta, doctype, values)


def missing_marks(verb: str, doctype: str, name: str, values: dict) -> list[dict] | None:
	"""Marks for required values still empty (None: one the panel cannot fill). Only
	for a "Value missing" failure: Frappe checks links before the controller fills its
	own required fields, so after any other failure the draft would show gaps that
	are not the person's to fill."""
	doc = _draft_doc(verb, doctype, name, values)
	return [] if doc is None else _missing(doc, verb)


def mark_entry(meta, missing: dict) -> dict:
	"""One mark as the panel reads it: ``{fieldname, label, where[, parentfield, idx]}``."""
	parentfield = missing.get("parentfield")
	if not parentfield:
		label = _(meta.get_label(missing["fieldname"]))
		return {"fieldname": missing["fieldname"], "label": label, "where": label}
	label = _(frappe.get_meta(meta.get_field(parentfield).options).get_label(missing["fieldname"]))
	return {
		"fieldname": missing["fieldname"],
		"label": label,
		"parentfield": parentfield,
		"idx": missing.get("idx"),
		"where": _("{0} row {1}: {2}").format(_(meta.get_label(parentfield)), missing.get("idx"), label),
	}


def _draft_doc(verb: str, doctype: str, name: str, values: dict):
	"""The document as the panel would have saved it, never written."""
	try:
		if verb == "update":
			if not (name and frappe.db.exists(doctype, name)):
				return None
			doc = frappe.get_doc(doctype, name)
			doc.update({k: v for k, v in (values or {}).items() if not isinstance(v, list)})
			return doc
		doc = frappe.new_doc(doctype)
		doc.update(values or {})
		return doc
	except Exception:
		return None


def _invalid(meta, doctype: str, values: dict) -> list[dict]:
	marks = []
	for fieldname, value in (values or {}).items():
		df = meta.get_field(fieldname)
		if df is None or isinstance(value, list | dict) or value in (None, ""):
			continue
		if _refused(df, doctype, fieldname, value):
			marks.append({**mark_entry(meta, {"fieldname": fieldname}), "invalid": 1})
	return marks


def _refused(df, doctype: str, fieldname: str, value) -> bool:
	from jarvis.tools._field_values import check_values

	try:
		check_values(doctype, {fieldname: value})
	except InvalidFieldValueError:
		return True
	except Exception:
		return False
	if df.fieldtype == "Select" and df.options:
		# Frappe's own option check, which the type check leaves to it for ``status``.
		return str(value).strip() not in [o.strip() for o in df.options.split("\n")]
	if df.fieldtype == "Link" and df.options:
		try:
			return not _link_exists(df.options, value)
		except Exception:
			return False  # an unmarked field, never a crash on top of a known failure
	return False


def _link_exists(doctype: str, value) -> bool:
	return bool(frappe.db.exists(doctype, value))


def _missing(doc, verb: str) -> list[dict] | None:
	"""Required values still empty, as the panel can mark them (None: one it cannot).
	A create is run in a rollback sandbox with mandatory checks off, so values its
	controller fills (a Sales Order's currency) are not asked for; when that run fails
	on something else, what it filled before failing still counts."""
	from jarvis.chat.held_writes import missing_fields

	if verb == "create":
		_fill_in_sandbox(doc)
	found = missing_fields(doc, 0)
	return None if found is None else [mark_entry(doc.meta, m) for m in _skip_fetched(doc, found)]


def _skip_fetched(doc, found: list[dict]) -> list[dict]:
	"""Drop values a field fetches from another one that is set (``fetch_from``, a
	Sales Order's customer_name from its customer): the save fills them. Values a
	controller sets in code (a row's item_name from its item_code) cannot be told
	apart this cheaply; the sandbox run above usually fills them, and child-row marks
	stay in the message (the panel marks only top-level fields)."""
	kept = []
	for entry in found:
		row = doc
		if entry.get("parentfield"):
			rows = doc.get(entry["parentfield"]) or []
			idx = int(entry.get("idx") or 0)
			row = rows[idx - 1] if 0 < idx <= len(rows) else None
		df = row.meta.get_field(entry["fieldname"]) if row is not None else None
		source = (df.fetch_from or "").split(".")[0] if df is not None else ""
		if source and row.get(source):
			continue
		kept.append(entry)
	return kept


def _fill_in_sandbox(doc) -> None:
	from jarvis.tools._preview_sandbox import preview_sandbox

	frappe.local.jarvis_dispatch_depth = getattr(frappe.local, "jarvis_dispatch_depth", 0) + 1
	try:
		with preview_sandbox():
			doc.flags.ignore_mandatory = True
			doc.insert()
	except Exception:
		pass
	finally:
		frappe.local.jarvis_dispatch_depth -= 1
		frappe.clear_messages()
