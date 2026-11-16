"""A parked ``create_doc`` / ``update_doc`` call, marked as a DRAFT card.

When the agent proposes ONE record, the card the server parks is a draft the person
can read as a form: ``card["draft"]`` carries the call's values by field name and a
title. It goes on the card BEFORE it is sealed and parked (``api._run_tool``), so the
seal's card hash covers it.

Dark behind the site flag ``jarvis_draft_cards`` (``enabled``, default off): with it
off nothing here changes a card.

``is_draft_call`` and ``payload_from`` are pure (the site's field lookups are passed
in), so their cases run without a site; ``payload`` and ``attach`` read the site.
"""

from __future__ import annotations

import json

from jarvis.chat import live_draft

_TOOLS = ("create_doc", "update_doc")
_TABLE_TYPES = ("Table", "Table MultiSelect")


def enabled() -> bool:
	"""The site flag. Off (0) unless ``jarvis_draft_cards`` is set in the site config."""
	import frappe

	return bool(frappe.utils.cint(frappe.conf.get("jarvis_draft_cards", 0)))


def _is_batch(args: dict, key: str) -> bool:
	"""A non-empty list under ``key``: the same test ``confirm_card._bulk`` uses."""
	value = args.get(key)
	return isinstance(value, list) and bool(value)


def is_draft_call(tool: str, args, *, must_card: bool) -> bool:
	"""A single-record ``create_doc`` / ``update_doc`` that is not ``must_card`` (a
	sensitive, structural or forced card keeps its own shape), whose doctype name is
	fit to show."""
	if must_card or tool not in _TOOLS or not isinstance(args, dict):
		return False
	if not live_draft.safe_doctype(args.get("doctype")):
		return False
	if tool == "create_doc":
		return not _is_batch(args, "docs") and isinstance(args.get("values"), dict)
	name = args.get("name")
	return (
		not _is_batch(args, "updates")
		and isinstance(name, str)
		and bool(name.strip())
		and isinstance(args.get("changes"), dict)
	)


def wanted(tool: str, args, *, must_card: bool) -> bool:
	"""Whether this park builds a draft card: the flag is on and the call is one."""
	return enabled() and is_draft_call(tool, args, must_card=must_card)


def is_pending_draft_row(row) -> bool:
	"""A tool row that is still awaiting its decision and whose parked card is a draft.

	Reads only the row's own (already parsed) ``pending_card``, so a history read adds no
	query per row. Once the card is decided the row carries an ``action_outcome`` and no
	card, and this is False."""
	card = row.get("pending_card")
	return (
		row.get("role") == "tool"
		and row.get("tool_status") == "pending"
		and isinstance(card, dict)
		and isinstance(card.get("draft"), dict)
	)


def payload_from(
	verb: str,
	doctype: str,
	name: str,
	values: dict,
	would,
	*,
	has_field,
	child_has,
	skip,
	cont: bool = False,
) -> dict:
	"""The ``draft`` payload from plain inputs: ``title`` ("New {doctype}" / "{doctype}
	{name}"; ``card["title"]`` keeps what the card always had), the call's ``values``,
	``ignored`` and ``dropped``.

	``has_field(key)``: whether ``key`` is a field of the form. ``child_has(key)``: for a
	table field, a predicate on its row keys, else None. ``skip``: bookkeeping keys that
	are never fields and never ignored (``_record_summary.row_bookkeeping_keys``).
	A key that is not a field is listed in ``ignored`` and left out of ``values`` (a
	child row's as "table.field"). A field the trial dropped (``would`` is a dict that
	lacks it: a permission filter) is left out of ``values`` as the card's rows do, and
	named in ``dropped``. A real field is judged first; a bookkeeping key that is no
	field is kept and never ignored."""
	ignored: list[str] = []
	dropped: list[str] = []
	out: dict = {}
	for key, value in values.items():
		if not has_field(key):
			if key in skip:
				out[key] = value
			else:
				ignored.append(key)
			continue
		if isinstance(would, dict) and key not in would:
			dropped.append(key)
			continue
		row_has = child_has(key) if isinstance(value, list) else None
		if row_has is not None:
			value = [_clean_row(key, row, row_has, skip, ignored) for row in value]
		out[key] = value
	return {
		"verb": verb,
		"doctype": doctype,
		"name": name,
		"title": title_of(verb, doctype, name),
		"values": out,
		"ignored": ignored,
		"dropped": dropped,
		"continue": bool(cont),
	}


def _clean_row(table: str, row, row_has, skip, ignored: list) -> object:
	if not isinstance(row, dict):
		return row
	kept = {}
	for key, value in row.items():
		if row_has(key) or key in skip:
			kept[key] = value
			continue
		label = f"{table}.{key}"
		if label not in ignored:
			ignored.append(label)
	return kept


def _site_lookups(doctype: str):
	import frappe

	meta = frappe.get_meta(doctype)

	def has_field(key):
		return meta.get_field(key) is not None

	def child_has(key):
		df = meta.get_field(key)
		if df is None or df.fieldtype not in _TABLE_TYPES or not df.options:
			return None
		child = frappe.get_meta(df.options)
		return lambda k: child.get_field(k) is not None

	return has_field, child_has


def payload(tool: str, args: dict, would) -> dict:
	"""The ``draft`` payload of a draft call, read as the write will STORE it
	(``confirm_card._stored_args``) against the site's field list."""
	from jarvis.chat import _record_summary, confirm_card

	stored = confirm_card._stored_args(tool, args)
	doctype = live_draft.real_doctype(args.get("doctype")) or live_draft.safe_doctype(args.get("doctype"))
	has_field, child_has = _site_lookups(doctype)
	skip = _record_summary.row_bookkeeping_keys()
	cont = args.get("continue") is True
	if tool == "update_doc":
		return payload_from(
			"update",
			doctype,
			args["name"],
			stored["changes"],
			would,
			has_field=has_field,
			child_has=child_has,
			skip=skip,
			cont=cont,
		)
	import frappe

	prompt = live_draft.prompt_named(frappe.get_meta(doctype).autoname)
	name = stored["values"].get("name") if prompt else ""
	return payload_from(
		"create",
		doctype,
		name if isinstance(name, str) else "",
		stored["values"],
		would,
		has_field=has_field,
		child_has=child_has,
		skip=skip,
		cont=cont,
	)


def title_of(verb: str, doctype: str, name: str) -> str:
	""" "New {doctype}" for a create, "{doctype} {name}" for an update."""
	return f"New {doctype}" if verb == "create" else f"{doctype} {name}"


def attach(card: dict, tool: str, args: dict, would) -> bool:
	"""Put ``draft`` on ``card`` (before it is sealed); ``card["title"]`` is left alone.
	A create card carries no trial-allocated name: a prompt-named DocType keeps the name the call
	typed, any other gets none. False, and ``card`` untouched, when the doctype is not
	a real one on this site."""
	import frappe

	doctype = live_draft.real_doctype(args.get("doctype"))
	if not doctype:
		return False
	try:
		draft = payload(tool, args, would)
	except Exception:
		frappe.log_error(title="draft payload failed", message=frappe.get_traceback())
		return False
	card["draft"] = draft
	if draft["verb"] == "create":
		card["name"] = draft["name"] or None
	return True


MAX_CHANGE_ROWS = 50  # rows (and removed rows) a table's ``row_changes`` lists; the rest is counted


def row_diff(stored: list, final: list) -> dict:
	"""The rows an update leaves, set against the rows stored, from plain inputs.

	``stored``: ``[(name, cells)]`` of the saved rows. ``final``: ``[(name or None, cells)]``
	of the rows the call lists, in its order (``update_doc`` takes a table list as the COMPLETE
	final set, ``tools._child_rows.merge_child_rows``). ``cells`` are already formatted text,
	one per column, the same columns on every row.

	A row whose name is saved is ``changed`` when a cell differs from the saved one (it then
	carries ``was``, the saved cells), else ``kept``. A row with no name is ``new``. A saved
	row the call leaves out is ``removed``. A name that is not saved is skipped: the trial
	refuses such a call before a card is built, and no status is invented for it.

	``counts`` always cover every row. ``rows`` and ``removed`` hold at most
	``MAX_CHANGE_ROWS`` each, in order; when a list is over, the rows that change something
	win over the kept ones, and ``more`` says how many were left out."""
	saved = {name: cells for name, cells in stored if name}
	listed = {name for name, _cells in final if name}
	rows: list[dict] = []
	for name, cells in final:
		if not name:
			rows.append({"status": "new", "cells": cells})
		elif name in saved:
			was = saved[name]
			if was == cells:
				rows.append({"status": "kept", "cells": cells})
			else:
				rows.append({"status": "changed", "cells": cells, "was": was})
	removed = [{"cells": cells} for name, cells in stored if name and name not in listed]
	counts = {status: sum(1 for r in rows if r["status"] == status) for status in ("changed", "new", "kept")}
	counts["removed"] = len(removed)
	out = {"rows": rows, "removed": removed[:MAX_CHANGE_ROWS], "counts": counts}
	if len(rows) > MAX_CHANGE_ROWS:
		# Changes first: a change must not be what the bound hides.
		order = sorted(range(len(rows)), key=lambda i: (rows[i]["status"] == "kept", i))
		out["rows"] = [rows[i] for i in sorted(order[:MAX_CHANGE_ROWS])]
	more = {"rows": max(len(rows) - MAX_CHANGE_ROWS, 0), "removed": max(len(removed) - MAX_CHANGE_ROWS, 0)}
	if more["rows"] or more["removed"]:
		out["more"] = more
	return out


def _row_columns(child, call_rows: list) -> list[str]:
	"""The child DocType's list-view fields, then the other fields the call set, in the order
	``_record_summary.table_rows`` uses for a create card (capped at its column limit)."""
	from jarvis.chat._record_summary import _MAX_COLS

	set_keys: list[str] = []
	for row in call_rows:
		for key in row:
			df = child.get_field(key)
			if key in set_keys or df is None or df.fieldtype in _TABLE_TYPES:
				continue
			set_keys.append(key)
	listed = [d.fieldname for d in child.fields if d.in_list_view and d.fieldtype not in _TABLE_TYPES]
	return (listed + [k for k in set_keys if k not in listed])[:_MAX_COLS]


def row_changes(meta, key: str, call_rows, trial_rows, stored) -> dict | None:
	"""``row_changes`` of table field ``key`` of an update draft card, or None for nothing to show.

	``call_rows``: the list the call sent. ``trial_rows``: the trial's would-be rows of the
	table (or None). ``stored``: the saved rows, which ``confirm_card._update_card`` has
	already read (the permission-checked, field-filtered record), so this makes NO query.

	A final row is the trial's row (it carries computed cells such as hours): a named row is
	found in the trial by its name, a new one by its position among the trial rows the call
	does not name. Without a usable trial (no dry run, other row count, an unsaved name) a
	named row is the saved row with the call's cells over it (the merge ``update_doc`` makes)
	and a new row is the call's cells alone; a cell it does not carry is empty. A cell that
	differs from the saved one only in type (a time as text, a float as text) is not a change:
	``same_value`` decides, and the saved text is shown."""
	import frappe

	from jarvis.chat._record_summary import same_value, value_row
	from jarvis.tools._child_rows import row_values

	df = meta.get_field(key) if meta else None
	if df is None or df.fieldtype != "Table" or not df.options:
		return None
	if not isinstance(call_rows, list) or not isinstance(stored, list):
		return None
	if not all(isinstance(r, dict) for r in call_rows):
		return None
	child = frappe.get_meta(df.options)
	saved = {r["name"]: r for r in stored if isinstance(r, dict) and r.get("name")}
	called = {r["name"] for r in call_rows if r.get("name")}
	columns = _row_columns(child, [row_values(r) for r in call_rows])
	if not columns:
		return None

	def cells(row: dict) -> list[str]:
		return [
			value_row(child, c, "", row[c], child.get_field(c))["value"] if c in row else "" for c in columns
		]

	usable = (
		isinstance(trial_rows, list)
		and len(trial_rows) == len(call_rows)
		and all(isinstance(r, dict) for r in trial_rows)
		and called <= set(saved)
	)
	by_name = {r.get("name"): r for r in trial_rows} if usable else {}
	fresh = [r for r in trial_rows if r.get("name") not in called] if usable else []
	if len(fresh) != sum(1 for r in call_rows if not r.get("name")):
		fresh = []
	new_trial = iter(fresh)
	final = []
	for row in call_rows:
		name = row.get("name")
		if name and name not in saved:
			continue
		if name:
			got = by_name.get(name)
			values = got if got is not None else {**saved[name], **row_values(row)}
			text = cells(values)
			was = cells(saved[name])
			# A difference of type only (a time sent as text) is no change.
			for i, c in enumerate(columns):
				if text[i] != was[i] and same_value(saved[name].get(c), values.get(c), child.get_field(c)):
					text[i] = was[i]
			final.append((name, text))
		else:
			got = next(new_trial, None)
			final.append((None, cells(got if got is not None else row_values(row))))
	out = row_diff([(n, cells(r)) for n, r in saved.items()], final)
	return {
		"columns": [child.get_field(c).label or c for c in columns],
		"fieldnames": columns,
		**out,
	}


def decorate_update_entry(entry: dict, meta, key: str, call_value, trial_value, stored) -> None:
	"""Add what only a DRAFT update card carries to the diff ``entry`` of field ``key``:
	``multiline`` when its text holds a line break, ``row_changes`` for a child table the
	call lists. Added before the card is sealed. A failure leaves the entry as it was."""
	import frappe

	if "\n" in str(entry.get("to", "")) or "\n" in str(entry.get("from", "")):
		entry["multiline"] = True
	if not isinstance(call_value, list):
		return
	try:
		changes = row_changes(meta, key, call_value, trial_value, stored)
	except Exception:
		frappe.log_error(title="draft row changes failed", message=frappe.get_traceback())
		return
	if changes:
		entry["row_changes"] = changes


SHORT_NOTE = "A review card is on the user's screen. Write one short line and stop. Nothing is saved until they choose."
# The model's result for a parked draft stays this small (``len(json.dumps(data))``, where
# a non-ASCII character costs 6): it is the whole point of not sending the would-be
# document back.
SHORT_LIMIT = 600
_MAX_NAMES = 10
_NAME_CHARS = 40
HOOK_WARNING = "A hook was rolled back in the trial, so the saved record may differ from this draft."


def _clip(text: str, keep: int) -> str:
	"""``text`` cut to its first ``keep`` characters and ended with "..."; whole when it fits."""
	return text if keep >= len(text) else text[: max(keep, 0)] + "..."


def _warnings_of(preview) -> list[str]:
	"""The trial's two notes for the model, which the short result must not lose: the
	jobs a confirm will start (``will_queue_note``, the existing sentence) and a hook that
	rolled back (``hook_rolled_back``, a flag only; its long note would not fit)."""
	if not isinstance(preview, dict):
		return []
	out = []
	if preview.get("will_queue_note"):
		out.append(str(preview["will_queue_note"]))
	if preview.get("hook_rolled_back") is True:
		out.append(HOOK_WARNING)
	return out


def short_result(tool: str, draft: dict, preview=None) -> dict:
	"""What the MODEL gets back for a parked draft: the call's identity, the fields the
	card left out, the trial's warnings, and a note. No ``preview``: the person reads the
	card, the model does not need the would-be document.

	The result WITHOUT ``warnings`` is at most ``SHORT_LIMIT`` characters of JSON for any
	input: ``tool`` and ``verb`` are clipped to 40 and 20 characters, each field list is
	capped at 10 names of 40 characters up front (``more_ignored`` / ``more_dropped`` count
	the rest), and if it is still too long names leave ``dropped_fields`` first, then
	``ignored_fields`` (the model needs those to correct itself), then the record name and
	the doctype are shortened. The fixed note is never cut. ``warnings`` are added last,
	whole and outside that budget: a clipped warning loses its instruction."""
	warnings = _warnings_of(preview)
	ignored = [str(n)[:_NAME_CHARS] for n in (draft.get("ignored") or [])]
	dropped = [str(n)[:_NAME_CHARS] for n in (draft.get("dropped") or [])]
	more = {
		"more_ignored": max(len(ignored) - _MAX_NAMES, 0),
		"more_dropped": max(len(dropped) - _MAX_NAMES, 0),
	}
	ignored, dropped = ignored[:_MAX_NAMES], dropped[:_MAX_NAMES]
	name, doctype = str(draft.get("name") or ""), str(draft.get("doctype") or "")
	tool, verb = str(tool)[:40], str(draft.get("verb"))[:20]
	keep = {"name": len(name), "doctype": len(doctype)}

	def build() -> dict:
		data = {
			"status": "pending_confirmation",
			"tool": tool,
			"draft": {
				"verb": verb,
				"doctype": _clip(doctype, keep["doctype"]),
				"name": _clip(name, keep["name"]),
			},
			"ignored_fields": ignored,
		}
		if more["more_ignored"]:
			data["more_ignored"] = more["more_ignored"]
		if dropped:
			data["dropped_fields"] = dropped
		if more["more_dropped"]:
			data["more_dropped"] = more["more_dropped"]
		data["note"] = SHORT_NOTE
		return data

	while True:
		data = build()
		excess = len(json.dumps(data)) - SHORT_LIMIT
		if excess <= 0:
			break
		if dropped:
			dropped.pop()
			more["more_dropped"] += 1
		elif ignored:
			ignored.pop()
			more["more_ignored"] += 1
		else:
			# Each step takes off at least one character and about the excess in bytes.
			step = max(1, excess // 6)
			for field in ("name", "doctype"):
				if keep[field] > 0:
					keep[field] = max(keep[field] - step, 0)
					break
			else:
				break
	if warnings:
		data["warnings"] = warnings
	return {"ok": True, "data": data}


def refusal(exc, tool: str, args: dict) -> list[dict] | None:
	"""The fields a refused draft call needs, as ``[{fieldname, label, where, parentfield?,
	idx?}]`` with ``where`` "main" or "row": the edit panel's own mandatory marks
	(``panel_fields.missing_marks``) for a trial that failed with ``MandatoryError``.
	None for any other failure, for a call that is not a draft call, and when the marks
	are empty or ones the form cannot fill: the refusal then keeps today's shape."""
	import frappe

	if not isinstance(exc, frappe.MandatoryError):
		return None
	from jarvis.chat.panel_fields import missing_marks

	doctype = live_draft.real_doctype(args.get("doctype"))
	if not doctype:
		return None
	update = tool == "update_doc"
	try:
		marks = missing_marks(
			"update" if update else "create",
			doctype,
			args["name"] if update else "",
			(args["changes"] if update else args["values"]) or {},
		)
	except Exception:
		return None
	if not marks:
		return None
	out = []
	for mark in marks:
		entry = {"fieldname": mark["fieldname"], "label": mark["label"]}
		if mark.get("parentfield"):
			entry.update(where="row", parentfield=mark["parentfield"], idx=mark.get("idx"))
		else:
			entry["where"] = "main"
		out.append(entry)
	return out
