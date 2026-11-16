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

	prompt = frappe.get_meta(doctype).autoname == "Prompt"
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
