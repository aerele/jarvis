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
# DocTypes whose write keeps today's one-tap confirmation card even with the flag on. The Dashboard
# Builder pane saves a dashboard with its own "Save dashboard" Approve in the pane (a draft card would
# send it to the full chat, away from the builder and its unsaved canvas), and a dashboard is
# configuration, not a record to review field by field.
_NOT_DRAFTED = frozenset({"jarvis dashboard"})  # casefolded: compared as the tool reads the name
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
	fit to show and is not one of ``_NOT_DRAFTED``."""
	if must_card or tool not in _TOOLS or not isinstance(args, dict):
		return False
	doctype = live_draft.safe_doctype(args.get("doctype"))
	if not doctype:
		return False
	# the tool takes the name stripped and any case (the DocType lookup is case-insensitive)
	if doctype.casefold() in _NOT_DRAFTED:
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


def wanted(tool: str, args, *, must_card: bool, offers_run: bool = False) -> bool:
	"""Whether this park builds a draft card: the flag is on and the call is one.

	``offers_run``: the park will offer "Approve & run" (the gate stamped an armed skill on it,
	``api._resolve_approve_run_offer``). Only the ordinary confirmation card has that control: no
	draft card does, so such a write keeps the ordinary card, as with the flag off. Such a card has
	no ``draft``, which is also why ``draft_save`` (it needs one) never edits it."""
	return enabled() and not offers_run and is_draft_call(tool, args, must_card=must_card)


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


# A DECIDED draft row loses its card (``persist_tool_receipt`` clears ``pending_card``) and its
# Pending Action is purged a week after it settles, so the row itself keeps a tiny marker in its
# stored ``tool_result``: ``{"draft_receipt": {"reason"?: ...}}``. It
# is written at settlement from the card that WAS a draft (``_settle._deliver``), never from the
# flag, and read back by ``decided_mark``. ``reason`` is only the two causes the receipt words
# differently: the record changed after the draft, or it is gone.
RECEIPT_KEY = "draft_receipt"
_CHANGED_REASONS = ("stale", "target_missing")


def with_receipt_mark(result, draft, reason_code: str | None):
	"""``result`` (the receipt's stored result) with the draft marker on it; unchanged when
	the settled card was not a draft. A missing result (a discard, a failure that never ran)
	becomes a result holding only the marker."""
	if not isinstance(draft, dict):
		return result
	mark = {}
	if reason_code in _CHANGED_REASONS:
		mark["reason"] = reason_code
	return {**(result if isinstance(result, dict) else {}), RECEIPT_KEY: mark}


def decided_mark(row) -> dict:
	"""The stamp a DECIDED tool row gets when it was a draft card, else ``{}``:
	``{"draft_receipt": True}`` and, for a record that changed or went missing after the
	draft, ``"draft_receipt_reason": "stale" | "target_missing"``. Reads only the row's own
	``tool_result`` (a JSON string or dict), so a history read adds no query per row."""
	raw = row.get("tool_result")
	if row.get("role") != "tool" or not row.get("action_outcome") or not raw:
		return {}
	if isinstance(raw, str):
		if RECEIPT_KEY not in raw:  # the common receipt: no parse
			return {}
		try:
			raw = json.loads(raw)
		except (ValueError, RecursionError):  # deep nesting must not 500 a history read
			return {}
	mark = raw.get(RECEIPT_KEY) if isinstance(raw, dict) else None
	if not isinstance(mark, dict):
		return {}
	out = {RECEIPT_KEY: True}
	if mark.get("reason") in _CHANGED_REASONS:
		out["draft_receipt_reason"] = mark["reason"]
	return out


def without_mark(raw):
	"""``raw`` (a stored ``tool_result``: a JSON string, a dict or anything else) without the
	draft marker, in the kind it came in, or None when nothing else is left (a discard row is
	empty again). The marker is for the apps only and must never reach the model or a share
	recipient. Anything that holds no marker is returned as it came (the same object or string),
	so with the feature off every payload is unchanged. Never raises."""
	if isinstance(raw, str):
		if RECEIPT_KEY not in raw:  # the common receipt: no parse
			return raw
		try:
			parsed = json.loads(raw)
		except (ValueError, RecursionError):
			return raw
		if not isinstance(parsed, dict) or RECEIPT_KEY not in parsed:
			return raw
		rest = {k: v for k, v in parsed.items() if k != RECEIPT_KEY}
		return json.dumps(rest, ensure_ascii=False) if rest else None
	if isinstance(raw, dict) and RECEIPT_KEY in raw:
		rest = {k: v for k, v in raw.items() if k != RECEIPT_KEY}
		return rest or None
	return raw


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
_CELL_CLIP = 80  # a cell on the sealed card is cut to this many characters (ending "...")
_FALLBACK_COLUMNS = 3  # a child with no list-view field and no key set shows its first data fields
_LINE_BREAKS = ("\n", "\r", "\u2028")


def _clip_cell(text: str) -> str:
	return text if len(text) <= _CELL_CLIP else text[: _CELL_CLIP - 3] + "..."


def row_diff(stored: list, final: list, cells) -> dict:
	"""The rows an update leaves, set against the rows stored, from plain inputs.

	``stored``: ``[(name, row)]`` of the saved rows. ``final``: ``[(name or None, row,
	changed)]`` of the rows the call lists, in its order (``update_doc`` takes a table list as
	the COMPLETE final set, ``tools._child_rows.merge_child_rows``); ``changed`` was decided
	by the caller on the RAW values, never on display text. ``cells(row)`` gives a row's
	display cells, and is called only for the rows that are listed.

	A row whose name is saved is ``changed`` or ``kept`` as ``changed`` says; a changed row
	carries ``was`` (the saved cells) and ``hidden_change`` when the shown cells are equal
	(a secret, a long text, a column beyond the cap). A row with no name is ``new``. A saved
	row the call leaves out is ``removed``. A name that is not saved is skipped: the trial
	refuses such a call before a card is built, and no status is invented for it.
	``reordered`` is True when the named rows are listed in another order than saved
	(``update_doc`` renumbers ``idx``).

	``counts`` cover every row. ``rows`` and ``removed`` hold at most ``MAX_CHANGE_ROWS``
	each, in order; when a list is over, the rows that change something win over the kept
	ones, and ``more`` says how many were left out."""
	saved = {name: row for name, row in stored if name}
	listed = {name for name, _row, _changed in final if name}
	entries: list[tuple] = []
	for name, row, changed in final:
		if not name:
			entries.append(("new", row, None))
		elif name in saved:
			entries.append(("changed" if changed else "kept", row, name))
	gone = [row for name, row in stored if name and name not in listed]
	counts = {s: sum(1 for e in entries if e[0] == s) for s in ("changed", "new", "kept")}
	counts["removed"] = len(gone)
	chosen = range(len(entries))
	if len(entries) > MAX_CHANGE_ROWS:
		# Changes first: a change must not be what the bound hides.
		order = sorted(chosen, key=lambda i: (entries[i][0] == "kept", i))
		chosen = sorted(order[:MAX_CHANGE_ROWS])
	rows = []
	for i in chosen:
		status, row, name = entries[i]
		out_row = {"status": status, "cells": cells(row)}
		if status == "changed":
			out_row["was"] = cells(saved[name])
			if out_row["was"] == out_row["cells"]:
				out_row["hidden_change"] = True
		rows.append(out_row)
	out = {
		"rows": rows,
		"removed": [{"cells": cells(r)} for r in gone[:MAX_CHANGE_ROWS]],
		"counts": counts,
	}
	more = {"rows": max(len(entries) - MAX_CHANGE_ROWS, 0), "removed": max(len(gone) - MAX_CHANGE_ROWS, 0)}
	if more["rows"] or more["removed"]:
		out["more"] = more
	named = [name for name, _row, _changed in final if name in saved]
	called = set(named)
	if named != [name for name, _row in stored if name in called]:
		out["reordered"] = True
	return out


def _differs(a, b, df) -> bool:
	"""Whether saving ``b`` over the stored ``a`` changes the cell: ``same_value``'s casts
	(dates, numbers), plus a Time compared as a time (a call sends "11:00", the row holds
	11:00:00).

	Kept beside ``same_value`` (jarvis/chat/_record_summary.py) on purpose: ``same_value`` has no
	Time cast, so it compares a Time with ``cstr`` and calls "11:00" against a stored 11:00:00 a
	CHANGE. Its other callers (the ordinary update card in confirm_card.py and the post-save
	check in pending_actions/_verify.py) show or verify that as a change today; teaching it the
	Time case would silently change those cards' diffs, so the Time case lives here only."""
	from jarvis.chat._record_summary import same_value

	if getattr(df, "fieldtype", None) == "Time":
		from frappe.utils.data import to_timedelta

		try:
			return (None if a in (None, "") else to_timedelta(a)) != (
				None if b in (None, "") else to_timedelta(b)
			)
		except Exception:
			return True
	return not same_value(a, b, df)


def _row_changed(
	child, was: dict, final: dict, sent: dict, columns: list, matched: bool = False, gaps: list | None = None
) -> bool:
	"""Whether the write changes the saved row ``was`` into ``final``, judged on RAW values
	for every field the call set and every shown column. When the trial's own row is
	``matched``, every value field of the child is compared too: validation may recalculate a
	field the call did not set and the card does not show (a base amount). A secret is judged
	on the value the call sends (the trial's row holds a mask of the same length, which would
	hide a new password), by what the write does with it (``secret_word``); one the call did not
	send is not compared. A stored field the trial row lacks and the call did not send is added to
	``gaps`` (the caller then calls the comparison not exhaustive). Bookkeeping columns (``idx``, ``modified`` ...) are not fields."""
	from frappe.model import no_value_fields

	from jarvis.chat._record_summary import is_secret, secret_word

	keys = [*sent, *columns]
	if matched:
		keys += [d.fieldname for d in child.fields if d.fieldname]
	for key in dict.fromkeys(keys):
		df = child.get_field(key)
		if df is None or df.fieldtype in no_value_fields:
			continue
		if is_secret(child, key):
			if key in sent and secret_word(child, key, sent[key], was.get(key), update=True) not in (
				"",
				"unchanged",
			):
				return True
			continue
		if key not in final and key not in sent:
			# Nothing is known about it after the write: the comparison is not exhaustive.
			if gaps is not None and key in was:
				gaps.append(key)
			continue
		if _differs(was.get(key), final[key] if key in final else sent.get(key), df):
			return True
	return False


def _row_columns(child, call_rows: list) -> list[str]:
	"""The fields the call set (first seen first), then the child DocType's list-view
	fields, capped at the create card's column limit: an edited field is shown whenever it
	can be. With neither, the first data fields of the child, so added and removed rows
	still say what they are."""
	from frappe.model import no_value_fields

	from jarvis.chat._record_summary import _MAX_COLS, is_secret

	set_keys: list[str] = []
	for row in call_rows:
		for key in row:
			df = child.get_field(key)
			if key in set_keys or df is None or df.fieldtype in no_value_fields:
				continue
			set_keys.append(key)
	listed = [d.fieldname for d in child.fields if d.in_list_view and d.fieldtype not in no_value_fields]
	columns = (set_keys + [k for k in listed if k not in set_keys])[:_MAX_COLS]
	if columns:
		return columns
	return [
		d.fieldname
		for d in child.fields
		if d.fieldname and d.fieldtype not in no_value_fields and not is_secret(child, d.fieldname)
	][:_FALLBACK_COLUMNS]


def _link_changes(child, call_rows: list, stored: list) -> dict | None:
	"""``row_changes`` of a Table MultiSelect: ``update_doc`` replaces the set of links.
	One column (the link field): a link kept, added (``new``) or dropped (``removed``)."""
	link = next((d for d in child.fields if d.fieldtype == "Link"), None)
	if link is None or not all(isinstance(r, dict) for r in call_rows):
		return None
	before = [r.get(link.fieldname) for r in stored if isinstance(r, dict) and r.get(link.fieldname)]
	now = [r.get(link.fieldname) for r in call_rows if r.get(link.fieldname)]
	# Count occurrences: of each link, as many as are saved are kept, the extra ones called are
	# new, the saved ones not called are removed. Each occurrence is its own "row".
	saved_n = {v: before.count(v) for v in before}
	seen: dict = {}
	final = []
	for v in now:
		i = seen[v] = seen.get(v, -1) + 1
		final.append(((v, i) if i < saved_n.get(v, 0) else None, v, False))
	stored_rows = [((v, i), v) for v in saved_n for i in range(saved_n[v])]
	out = row_diff(stored_rows, final, lambda v: [_clip_cell(str(v))])
	out.pop("reordered", None)  # a set has no order
	return {"columns": [link.label or link.fieldname], "fieldnames": [link.fieldname], **out}


def row_changes(meta, key: str, call_rows, trial_rows, stored) -> dict | None:
	"""``row_changes`` of table field ``key`` of an update draft card, or None for nothing to show.

	``call_rows``: the list the call sent (None clears the table, as ``[]`` does:
	``merge_child_rows``). ``trial_rows``: the trial's would-be rows of the table (or None).
	``stored``: the saved rows, which ``confirm_card._update_card`` has already read (the
	permission-checked, field-filtered record), so this makes NO query.

	A final row is the trial's row (it carries computed cells such as hours): a named row is
	found in the trial by its name, a new one by its position among the trial rows the call
	does not name. Without a usable trial (no dry run, other row count, an unsaved name) a
	named row is the saved row with the call's cells over it (the merge ``update_doc`` makes)
	and a new row is the call's cells alone; a cell it does not carry is empty. Whether a row
	changed is decided on raw values (``_row_changed``); cells are formatted (and clipped) only
	for the rows that are listed."""
	import frappe

	from jarvis.chat._record_summary import value_row
	from jarvis.tools._child_rows import row_values

	df = meta.get_field(key) if meta else None
	if df is None or df.fieldtype not in _TABLE_TYPES or not df.options:
		return None
	if call_rows is None:
		call_rows = []
	if not isinstance(call_rows, list) or not isinstance(stored, list):
		return None
	if not all(isinstance(r, dict) for r in call_rows):
		return None
	child = frappe.get_meta(df.options)
	if df.fieldtype == "Table MultiSelect":
		return _link_changes(child, call_rows, stored)
	# The write's order is ``idx``; the list position is only the tie-break (a stable sort).
	by_idx = sorted(
		(r for r in stored if isinstance(r, dict) and r.get("name")),
		key=lambda r: (
			not isinstance(r.get("idx"), int),
			r.get("idx") if isinstance(r.get("idx"), int) else 0,
		),
	)
	saved = {r["name"]: r for r in by_idx}
	called = {r["name"] for r in call_rows if r.get("name")}
	columns = _row_columns(child, [row_values(r) for r in call_rows])
	if not columns:
		return None

	def cells(row: dict) -> list[str]:
		return [
			_clip_cell(value_row(child, c, "", row[c], child.get_field(c))["value"]) if c in row else ""
			for c in columns
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
	# True only when every listed row was set against the trial's own row, so every value field
	# of it was compared (``_row_changed``). Otherwise only the call's keys could differ.
	# A saved row with no name cannot be matched or counted as removed.
	exhaustive = all(isinstance(r, dict) and r.get("name") for r in stored)
	for row in call_rows:
		name = row.get("name")
		sent = row_values(row)
		if name and name not in saved:
			exhaustive = False
			continue
		if name:
			got = by_name.get(name)
			row_final = got if got is not None else {**saved[name], **sent}
			exhaustive = exhaustive and got is not None
			gaps: list = []
			changed = _row_changed(child, saved[name], row_final, sent, columns, got is not None, gaps)
			exhaustive = exhaustive and not gaps
			final.append((name, row_final, changed))
		else:
			got = next(new_trial, None)
			exhaustive = exhaustive and got is not None
			final.append((None, got if got is not None else sent, False))
	out = row_diff(list(saved.items()), final, cells)
	if not exhaustive and not has_row_change({"row_changes": out}):
		# Nothing visible differs, but we could not compare every field: say so, never "unchanged".
		out["unverified"] = True
	return {
		"columns": [child.get_field(c).label or c for c in columns],
		"fieldnames": columns,
		**out,
	}


def has_row_change(entry: dict) -> bool:
	"""Whether a diff ``entry`` carries ``row_changes`` that change, add or remove a row, or
	reorder them."""
	rc = entry.get("row_changes")
	if not isinstance(rc, dict):
		return False
	counts = rc.get("counts") or {}
	return bool(counts.get("changed") or counts.get("new") or counts.get("removed") or rc.get("reordered"))


def _breaks(value) -> bool:
	return isinstance(value, str) and any(ch in value for ch in _LINE_BREAKS)


def decorate_update_entry(entry: dict, meta, key: str, call_value, from_val, to_val, trial_value) -> None:
	"""Add what only a DRAFT update card carries to the diff ``entry`` of field ``key``:
	``multiline`` when its RAW from / to value holds a line break (the display text is
	clipped), ``row_changes`` for a child table the call lists (or clears with None).
	``from_val``: the stored value (for a table, the saved rows). Added before the card is
	sealed. A failure leaves the entry as it was."""
	import frappe

	if _breaks(from_val) or _breaks(to_val):
		entry["multiline"] = True
	if not isinstance(call_value, list) and call_value is not None:
		return
	try:
		changes = row_changes(meta, key, call_value, trial_value, from_val)
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
			str(exc),
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
