"""Build the render-ready "what will change" summary for a confirmation card (F9).

The write-safety gate (``jarvis.api._run_tool``) parks a gated write and shows the
user a card. Today that card renders a raw-JSON dump of the dry-run ``would`` doc
and a one-line ``_describe_call`` target string - it names the record but not what
the write does to it. ``build_card`` turns the tool + args + park-time preview into
a structured, human-readable summary the SPA renders like the model-authored draft
card (a field list for a create, a from->to diff for an update, a per-record
from->to diff for a bulk update, a verb line for submit/cancel/delete/amend,
to/subject/body for an email, method + args for run_method).

It is attached ONCE at park as ``preview["card"]`` (see the gate). Phase-1 F2 stores
the park-time preview in the token record and the resync endpoint returns it
verbatim, so the card rides the ``action:pending`` event, the record, and every
resync identically - it is never rebuilt at resync (which would let it diverge from
the stored preview and re-load the doc on each reconnect).

Safety: values come from the ALREADY perm-filtered ``would`` (create/update tools
call ``apply_fieldlevel_read_permissions`` before returning), and the update
``from`` values are read from a freshly-fetched doc that is permission-CHECKED
(``has_permission("read")``) and then perm-filtered - ``frappe.get_doc`` checks no
permission on its own unless passed a ``check_permission`` kwarg (which defaults to
None), and the fieldlevel filter is permlevel-only, so the explicit check is what
keeps an unreadable record's old values off the card. Long values are truncated,
rows capped, and obviously-secret ``run_method`` arg keys masked. The card carries
no material the owner would not already see in the post-confirm receipt.

Two keys ride every kind, both optional (readers tolerate their absence, and
cards parked before them have neither):

- ``warning`` = ``{"jobs": int, "rolled_back": bool}``, only when the create /
  update trial found background jobs the confirm will start (the real count, not
  the number of kinds) or a hook that rolled back part-way (R2-6). Built here, so
  the chat, the phone and the Approval Board read the same thing.
- a sensitive card (``preview["risk"] == "sensitive"``, set by the gate before it
  calls ``build_card``; R2-8 / R2-12) is shown in FULL: no clip, no row cap,
  passwords as "set" / "changed" / "unchanged", every hiding character as a
  ``⟦U+XXXX⟧`` marker (one final pass, ``_visible_all``), a long value marked
  ``multiline``, an update's long value with a line diff (``lines``) beside both
  full texts, child tables as ``from_table`` / ``to_table``, and a delete's whole
  record. ``too_large`` lets the gate refuse a card that cannot be shown whole
  instead of clipping it. The gate (``api._build_park_card``, shared with the File
  Box hold) adds ``risk`` / ``risk_line``.

Whatever a card reads from the CALL (a table's rows, a bulk update's values) it
reads as the write stores it (``_stored_args``: "yes" is 1, "1,000" is 1000), and a
full card's table shows the rows as they will be stored (``_resulting_rows``: a
kept row whole, a new row with Frappe's defaults, a secret as a word, no cell the
requester may not write); a table an update leaves as it is gets no row, except on
a guarded structure card, which shows its tables whole.

A verb card also says what the action does (``consequence``): submit / cancel /
amend / delete park a described card with no trial run (R2-2).

Field selection, doc reads, value formatting and no-op detection all live in
``jarvis.chat._record_summary``; this module owns card SHAPE only. The dependency
runs one way (confirm_card -> _record_summary) and must stay that way.

Every GATED write now has a card: phase 4 added the last five kinds (bulk_email,
share, assign, skill, wiki), so share_doc, assign_to, create_custom_skill,
update_wiki and a bulk mail-merge no longer fall back to a bare tool name.
``None`` is still returned for a shape without a bespoke card and for any token
minted before this existed - the SPA then falls back to the summary + raw-preview
rendering. Each new kind ALSO needs an entry in the frontends' ``CARD_KINDS``
whitelist (``frontend/src/lib/actionSummary.js``): ``pendingCardOf`` returns null
for a kind not in that set, so a kind added here alone renders as if it never
shipped.
"""

from __future__ import annotations

import json

import frappe

from jarvis.chat._record_summary import (
	_MAX_BODY,
	_MAX_BULK_BODY,
	_MAX_TABLES,
	_MAX_VAL,
	HIDDEN_BREAK_NOTE,
	SecretWord,
	fmt,
	fmt_full,
	has_hidden_break,
	is_long,
	is_secret,
	line_diff,
	same_value,
	secret_state,
	secret_word,
	summary_rows,
	table_rows,
	value_row,
	values_rows,
	visible,
	visible_char,
)

_MAX_ROWS = 20  # cap fields / diff rows / batch bullets / targets shown
# A sensitive card is never clipped; one larger than this is refused instead.
MAX_FULL_CARD_BYTES = 256 * 1024

_BULK_KEYS = ("names", "updates", "docs", "messages")

# tool -> present-tense verb for the "will <verb> this <doctype> <name>" card.
_VERB = {
	"submit_doc": "submit",
	"cancel_doc": "cancel",
	"delete_doc": "delete",
	"amend_doc": "amend",
	"apply_workflow_action": "apply",
}

# tool -> (one document, several) consequence line on a verb card. The card is
# described, not trial-run (R2-2), so it says what Confirm will do.
_CONSEQUENCE = {
	"submit_doc": (
		"Submitting posts its accounting and stock entries, if any, and locks the document.",
		"Submitting posts each document's accounting and stock entries, if any, and locks it.",
	),
	"cancel_doc": ("Cancelling reverses its entries.", "Cancelling reverses each document's entries."),
	"delete_doc": ("Deleting removes it permanently.", "Deleting removes them permanently."),
	"amend_doc": (
		"Amending creates a new draft from the cancelled document.",
		"Amending creates a new draft from each cancelled document.",
	),
}


def build_card(tool: str, args, preview) -> dict | None:
	"""Structured, render-ready confirmation summary, or None to fall back to the
	SPA's summary + raw-preview rendering. Best-effort: never raises (a card is
	UX, not correctness), so a failure just yields None."""
	if not isinstance(args, dict):
		return None
	try:
		card = _card_of(tool, args, preview)
		if isinstance(card, dict):
			if _is_sensitive(preview):
				# Names, titles and targets too, not only the values: a record name
				# with a direction override reads as something else.
				card = _visible_all(card)
			_add_warning(card, preview)
		return card
	except Exception:
		frappe.log_error(title="build_card failed", message=frappe.get_traceback())
	return None


def too_large(card, args) -> bool:
	"""Whether a sensitive card (or, with none built, the call it stands for) is
	over ``MAX_FULL_CARD_BYTES``: the gate then refuses the write, because a
	sensitive card is never clipped (R2-8)."""
	shown = card if isinstance(card, dict) else args
	try:
		size = len(json.dumps(shown, default=str, ensure_ascii=False).encode())
	except Exception:
		return True
	return size > MAX_FULL_CARD_BYTES


def _is_sensitive(preview) -> bool:
	return isinstance(preview, dict) and preview.get("risk") == "sensitive"


# Keys whose string is a shown VALUE (a row, a diff side, a table cell): a value
# that is only whitespace would draw as an empty cell, so every character of it is
# marked. Not a line-diff line, whose indentation stays as typed.
_VALUE_KEYS = frozenset({"value", "from", "to", "cells"})


def _visible_all(value, key=None):
	"""``value`` with every string in it passed through ``visible`` (a sensitive
	card), and a whitespace-only shown value marked in full (``_VALUE_KEYS``)."""
	if isinstance(value, str):
		if key in _VALUE_KEYS and value and value.isspace():
			return "".join(visible_char(ch) for ch in value)
		return visible(value)
	if isinstance(value, dict):
		return {k: _visible_all(v, k) for k, v in value.items()}
	if isinstance(value, list):
		return [_visible_all(v, key) for v in value]
	return value


def _add_warning(card: dict, preview) -> None:
	"""The trial's findings (R2-6), only when there is one: the real number of
	background jobs Confirm will start (``will_queue_jobs``) and whether a hook
	rolled back part-way (``hook_rolled_back``)."""
	if not isinstance(preview, dict):
		return
	jobs = preview.get("will_queue_jobs")
	jobs = jobs if isinstance(jobs, int) and not isinstance(jobs, bool) and jobs > 0 else 0
	rolled_back = preview.get("hook_rolled_back") is True
	if jobs or rolled_back:
		card["warning"] = {"jobs": jobs, "rolled_back": rolled_back}


def _card_of(tool: str, args: dict, preview) -> dict | None:
	would = preview.get("would") if isinstance(preview, dict) else None
	full = _is_sensitive(preview)
	if tool in ("create_doc", "create_docs", "update_doc"):
		# Every value the card reads from the call is read as the write stores it.
		args = _stored_args(tool, args)
	bulk_key, bulk_items = _bulk(args)
	if tool in ("create_doc", "create_docs"):
		if bulk_key == "docs" or tool == "create_docs":
			return _batch_create_card(args, would, full)
		return _create_card(args, would, full)
	if tool == "update_doc" and not bulk_key:
		# A guarded structure card (a Workflow) shows its tables whole even when an
		# edit leaves them as they are: what is being switched on is the point.
		whole = isinstance(preview, dict) and preview.get("structural") is True
		return _update_card(args, would, full, whole)
	if tool == "update_doc" and bulk_key == "updates":
		return _bulk_update_card(args, bulk_items, full)
	if tool in _VERB:
		return _verb_card(tool, args, bulk_items, full)
	if tool == "share_doc":
		return _share_card(args, bulk_items if bulk_key == "names" else None)
	if tool == "assign_to":
		return _assign_card(args, bulk_items if bulk_key == "names" else None)
	if tool == "send_email":
		if args.get("messages") is not None:
			# Key off `is not None`, exactly as the tool does (send_email.py:54):
			# an explicit `messages: null` takes the SINGLE-email path and sends,
			# so `"messages" in args` here would drop that card to raw fallback
			# for a call that really does send one email.
			#
			# _bulk treats an EMPTY list as non-bulk (confirm_card.py:94), so a
			# bare `messages=[]` would otherwise reach _email_card and render a
			# plausible empty single-email card - while the tool raises "messages
			# must be a non-empty list" at confirm (send_email.py:107-109). A card
			# that describes a call that cannot run is a lying card; fall back to
			# raw instead. (`[]` is not None -> this branch -> bulk_key is None ->
			# None.)
			return _bulk_email_card(bulk_items) if bulk_key == "messages" else None
		return _email_card(args)
	if tool == "create_custom_skill":
		return _skill_card(args)
	if tool == "update_wiki":
		return _wiki_card(args)
	if tool == "run_method":
		return _method_card(args)
	if tool == "run_import":
		return _import_card(args, preview)
	return None


def _bulk(args: dict):
	"""(batch-key, items) for a bulk call, else (None, None). Keys are mutually
	exclusive per the tool contracts."""
	for k in _BULK_KEYS:
		v = args.get(k)
		if isinstance(v, list) and v:
			return k, v
	return None, None


def _meta(doctype):
	try:
		return frappe.get_meta(doctype) if doctype else None
	except Exception:
		return None


def _label(meta, fieldname: str) -> str:
	if meta:
		df = meta.get_field(fieldname)
		if df and df.label:
			return df.label
	return fieldname


def _create_card(args: dict, would, full: bool = False) -> dict:
	doctype = args.get("doctype")
	values = args.get("values") if isinstance(args.get("values"), dict) else {}
	meta = _meta(doctype)
	# Child tables FIRST: a list value rendered as a table must not also render as a
	# bare "Items · 1 row" text row below. The ``would`` guard is the same perm check
	# the scalar rows use - a permlevel-dropped table would otherwise show as a write
	# that will not happen.
	tables, table_keys = [], set()
	for key in list(values):
		if not full and len(tables) >= _MAX_TABLES:
			break
		value = values.get(key)
		if not isinstance(value, list) or not value:
			continue
		if isinstance(would, dict) and key not in would:
			continue  # perm-dropped from the resolved doc: the save will not write it
		t = table_rows(meta, key, _resulting_rows(meta, key, value, []) if full else value, full)
		if t:
			tables.append(t)
			table_keys.add(key)
	rows = []
	for key in list(values) if full else list(values)[: _MAX_ROWS * 2]:
		# Perm guard: only show a field that survived the resolved doc's
		# field-level read-permission filter.
		if isinstance(would, dict) and key not in would:
			continue
		if key in table_keys:
			continue  # rendered as a table below, not as a bare "N rows"
		val = would.get(key) if isinstance(would, dict) else values.get(key)
		if full and is_secret(meta, key):
			# The resolved doc holds the masked column: "set" / "not set" is judged on
			# what the call writes (never shown).
			val = values.get(key)
		# A sensitive card keeps a whitespace-only value (the card marks it); an
		# ordinary one drops it as before.
		if val is None or (not isinstance(val, list) and (str(val) if full else str(val).strip()) == ""):
			continue
		df = meta.get_field(key) if meta else None
		# A held create with missing fields has no dry-run doc: never echo a secret arg.
		rows.append(value_row(meta, key, _label(meta, key), val, df, None, full))
		if not full and len(rows) >= _MAX_ROWS:
			break
	if full:
		for label, _old, new in _code_cells(doctype, values, {}):
			row = value_row(None, "", label, new, None, None, True)
			rows.append({**row, "multiline": True})
	name = would.get("name") if isinstance(would, dict) else None
	return {"kind": "create", "doctype": doctype, "name": name, "rows": rows, "tables": tables}


# Code a record carries inside a child table: (table, the cell, what marks the cell
# as code or None for always, how its row is named). A full card's table scrolls
# sideways on the board and the phone, so each such cell ALSO gets a full-width
# row of its own, whole and wrapped (a Workflow transition's condition; Frappe
# 16: a state's update value marked as an expression). The table keeps its column.
_CODE_CELLS = {
	"Workflow": (
		("transitions", "condition", None, "Condition on {action}, from {state} to {next_state}"),
		("states", "update_value", "evaluate_as_expression", "Update value of {state} (an expression)"),
	)
}


def _code_cells(doctype, new: dict, old: dict) -> list[tuple[str, str, str]]:
	"""``(label, old text, new text)`` for every code cell (``_CODE_CELLS``) of the
	rows ``new`` writes, against the stored rows ``old`` (matched by row name; {} on
	a create). Rows that share a label say which row they are."""
	out = []
	for table, cell, marker, wording in _CODE_CELLS.get(doctype or "", ()):
		rows = new.get(table) if isinstance(new.get(table), list) else []
		stored = {r.get("name"): r for r in old.get(table) or [] if isinstance(r, dict)}
		found = []
		for i, row in enumerate(rows, 1):
			if not isinstance(row, dict):
				continue
			was = stored.get(row.get("name")) or {}
			texts = [
				str(r.get(cell) or "") if (marker is None or frappe.utils.cint(r.get(marker))) else ""
				for r in (was, row)
			]
			if any(texts):
				label = wording.format(**{k: row.get(k) or "" for k in ("action", "state", "next_state")})
				found.append((label, i, *texts))
		labels = [f[0] for f in found]
		for label, i, before, after in found:
			out.append((f"{label} (row {i})" if labels.count(label) > 1 else label, before, after))
	return out


def _code_diff_rows(doctype, changes: dict, old: dict) -> list[dict]:
	"""The code cells of an update as rows after the diff: a changed one as a from ->
	to block with its line diff, a kept one whole and marked unchanged."""
	out = []
	for label, before, after in _code_cells(doctype, changes, old):
		if before == after:
			text = fmt_full(after)
			out.append({"label": f"{label} (unchanged)", "from": text, "to": text, "multiline": True})
			continue
		row = {"label": label, "from": fmt_full(before), "to": fmt_full(after), "multiline": True}
		lines = line_diff(row["from"], row["to"], force=True)
		if lines:
			row["lines"] = lines
		if has_hidden_break(row["from"]) or has_hidden_break(row["to"]):
			row["note"] = HIDDEN_BREAK_NOTE
		out.append(row)
	return out


def _stored_args(tool: str, args: dict) -> dict:
	"""The call with its values as the write will STORE them
	(``_field_values.stored_values``), for the card only: the sealed call is left as
	it was sent, and Confirm runs the same reading on it, so what is drawn is what is
	written. A value the write would refuse (the park refuses it first) leaves that
	part as sent."""
	from jarvis.tools._field_values import stored_values

	def stored(doctype, values):
		if not isinstance(values, dict) or not isinstance(doctype, str):
			return values
		try:
			return stored_values(doctype, values)
		except Exception:
			frappe.clear_messages()
			return values

	out = dict(args)
	if tool in ("create_doc", "create_docs"):
		if isinstance(out.get("values"), dict):
			out["values"] = stored(out.get("doctype"), out["values"])
		if isinstance(out.get("docs"), list):
			out["docs"] = [
				{**d, "values": stored(d.get("doctype"), d.get("values"))} if isinstance(d, dict) else d
				for d in out["docs"]
			]
	elif tool == "update_doc":
		if isinstance(out.get("changes"), dict):
			out["changes"] = stored(out.get("doctype"), out["changes"])
		if isinstance(out.get("updates"), list):
			out["updates"] = [
				{**u, "changes": stored(out.get("doctype"), u.get("changes"))} if isinstance(u, dict) else u
				for u in out["updates"]
			]
	return out


def _resulting_rows(meta, key, rows: list, stored, doc=None) -> list:
	"""The rows of child table ``key`` as a write STORES them, for a full card.

	``update_doc`` keeps a saved row sent by its ``name`` and lays only the cells the
	call gives over it; a row without a name is added as given; a saved row the call
	does not name is removed (``tools._child_rows.merge_child_rows``). The card used
	to show the call's own rows, so a kept row sent by its name alone drew as a blank
	row: on an access card, a role that looks removed.

	Mirrors that merge on the card's own perm-filtered copy of the stored rows
	(``stored``) instead of reading the trial's resolved rows: a guarded structure
	card has no trial, and resolved rows carry masked secrets. On top of the merge:

	- a secret cell says what happens to it (``_secret_word``), never its value;
	- a cell above the requester's permission level is not drawn as written: Frappe
	  puts back the stored value of a kept row and the default of a new one
	  (document.py ``validate_higher_perm_levels``; ``doc``: the stored parent, whose
	  permissions decide; not on a create, where Frappe skips the rows).

	A Table MultiSelect is replaced as a set, so its rows are the call's."""
	from jarvis.tools._child_rows import row_values

	df = meta.get_field(key) if meta else None
	if df is None or df.fieldtype != "Table":
		return rows
	saved = {r.get("name"): r for r in stored or [] if isinstance(r, dict) and r.get("name")}
	try:
		child = frappe.get_meta(df.options)
	except Exception:
		child = None
	locked = _unwritable_cells(child, doc)
	secrets = [f.fieldname for f in (child.fields if child else []) if is_secret(child, f.fieldname)]
	out = []
	for row in rows:
		if not isinstance(row, dict):
			out.append(row)
			continue
		was = saved.get(row.get("name"))
		# A new row starts from Frappe's own defaults (a Select's first option, a
		# Check that defaults to on): what it stores in the cells the call leaves out.
		before = row_values(was) if was is not None else _row_defaults(child)
		given = {k: v for k, v in row_values(row).items() if k not in locked}
		merged = {**before, **given}
		for fieldname in secrets:
			word = _secret_word(child, fieldname, given, before, kept=was is not None)
			if word is None:
				merged.pop(fieldname, None)
			else:
				merged[fieldname] = word
		out.append(merged)
	return out


def _row_defaults(child) -> dict:
	"""The cells Frappe fills in on a new row of ``child`` by itself, where that is
	something to see (not an empty text or a zero)."""
	if child is None:
		return {}
	try:
		fresh = frappe.new_doc(child.name, as_dict=True)
	except Exception:
		return {}
	from frappe.model import no_value_fields

	fields = [f.fieldname for f in child.fields if f.fieldtype not in no_value_fields]
	return {k: fresh.get(k) for k in fields if fresh.get(k) not in (None, "", 0, 0.0)}


def _unwritable_cells(child, doc) -> set:
	"""The value fields of a child row the requester may not write: above permlevel
	0, at a level the parent's permissions do not give them."""
	if child is None or doc is None or frappe.session.user == "Administrator":
		return set()
	from frappe.model import display_fieldtypes

	high = [f for f in child.get_high_permlevel_fields() if f.fieldtype not in display_fieldtypes]
	if not high:
		return set()
	try:
		writable = set(doc.get_permlevel_access("write"))
	except Exception:
		return {f.fieldname for f in high}
	return {f.fieldname for f in high if f.permlevel not in writable}


def _secret_word(child, fieldname: str, given: dict, before: dict, *, kept: bool):
	"""What the card says for a secret cell of a row, or None for nothing: "set" for
	one a new row carries or a kept row keeps, "changed" / "cleared" for one the call
	replaces or empties, "unchanged" for Frappe's own mask sent back: the same rule
	as a top-level secret (``_record_summary.secret_word``)."""
	stored = before.get(fieldname)
	if fieldname not in given:
		return SecretWord("set") if kept and stored not in (None, "") else None
	word = secret_word(child, fieldname, given[fieldname], stored if kept else None, update=kept)
	return SecretWord(word) if word else None


def _same_rows(meta, key, rows, stored) -> bool:
	"""Whether the rows a write leaves (``_resulting_rows``) are the stored rows: a
	table sent whole by its row names, nothing changed. Compared cell by cell as the
	save would store them; a secret that stays is the same."""
	from jarvis.tools._child_rows import row_values

	df = meta.get_field(key) if meta else None
	if df is None or df.fieldtype != "Table" or not isinstance(rows, list) or not isinstance(stored, list):
		return False
	if len(rows) != len(stored):
		return False
	try:
		child = frappe.get_meta(df.options)
	except Exception:
		return False
	for row, was in zip(rows, stored, strict=True):
		if not isinstance(row, dict) or not isinstance(was, dict):
			return False
		before = row_values(was)
		for fieldname in {*row, *before}:
			new, old = row.get(fieldname), before.get(fieldname)
			if isinstance(new, SecretWord):
				if new not in ("set", "unchanged"):
					return False
			elif not same_value(old, new, child.get_field(fieldname)):
				return False
	return True


def _diff_row(meta, key, from_val, to_val, doc, full: bool) -> dict:
	"""One from -> to row. A secret is "[hidden]" both sides, or on a sensitive
	card "set" / "changed" / "unchanged". On a sensitive card a child table comes
	as ``from_table`` / ``to_table`` (every row and column), and a long value is
	``multiline`` with a ``lines`` diff, plus a ``note`` when it holds a hidden
	line break (lone carriage return, U+2028 ...) or when only its CRLF / LF line
	endings change."""
	df = meta.get_field(key) if meta else None
	label = _label(meta, key)
	if is_secret(meta, key):
		if full:
			return {
				"label": label,
				"from": secret_state(from_val, stored=True),
				"to": secret_word(meta, key, to_val, from_val, update=True),
			}
		return {"label": label, "from": "[hidden]", "to": "[hidden]"}
	if not full:
		return {"label": label, "from": fmt(from_val, df, doc), "to": fmt(to_val, df)}
	row = {"label": label, "from": fmt_full(from_val, df, doc), "to": fmt_full(to_val, df)}
	if df is not None and df.fieldtype in ("Table", "Table MultiSelect"):
		# A child table (webhook headers, roles) in full, not "N rows": the from / to
		# strings are the row counts and each non-empty side is a table (the
		# clients show an empty side as "none").
		for side, value in (("from", from_val), ("to", to_val)):
			rows = value if isinstance(value, list) else []
			row[side] = fmt(rows)
			table = table_rows(meta, key, rows, True, stored=side == "from")
			if table:
				row[side + "_table"] = table
		return row
	breaks = has_hidden_break(row["from"]) or has_hidden_break(row["to"])
	if is_long(row["from"]) or is_long(row["to"]):
		row["multiline"] = True
	# A hidden break forces the line diff even on a one-line value: "x" -> "x\r..."
	# must show as a change, not as two strings that draw alike.
	lines = line_diff(row["from"], row["to"], force=breaks)
	if lines:
		row["lines"] = lines
	if breaks:
		row["note"] = HIDDEN_BREAK_NOTE
	elif row["from"] != row["to"] and _crlf_as_lf(row["from"]) == _crlf_as_lf(row["to"]):
		row["note"] = "Only the line endings change."
	return row


def _crlf_as_lf(text: str) -> str:
	return text.replace("\r\n", "\n")


def _update_card(args: dict, would, full: bool = False, whole: bool = False) -> dict:
	doctype = args.get("doctype")
	name = args.get("name")
	changes = args.get("changes") if isinstance(args.get("changes"), dict) else {}
	meta = _meta(doctype)
	# OLD values from the current doc (the park dry-run rolled back, so the DB is
	# back to the pre-update state), permission-CHECKED then perm-filtered so
	# neither an unreadable record's nor a restricted field's old value ever leaks.
	# NEW values from ``would`` (already perm-filtered + normalized by the tool).
	old = {}
	doc = None  # must be bound: fmt(..., doc=doc) below needs it for currency/link titles
	try:
		doc = frappe.get_doc(doctype, name)
		# get_doc checks NO permission unless passed a check_permission kwarg
		# (document.py:141-145 -> :336-349), which defaults to None; and the
		# fieldlevel filter below is permlevel-only. Without this, a user who cannot
		# read the record sees its old values on the card.
		if not doc.has_permission("read"):
			raise frappe.PermissionError
		doc.apply_fieldlevel_read_permissions()
		old = doc.as_dict()
	except Exception:
		frappe.clear_messages()  # get_doc's throw leaves an entry that leaks into the turn
		old, doc = {}, None
	diff = []
	for key in list(changes) if full else list(changes)[: _MAX_ROWS * 2]:
		if isinstance(would, dict) and key not in would:
			continue  # not perm-visible in the resolved doc
		to_val = would.get(key) if isinstance(would, dict) else changes.get(key)
		if full and (is_secret(meta, key) or isinstance(changes.get(key), list)):
			# The resolved doc holds the masked column (one "*" per character), so a
			# new password of the same length would compare equal and vanish: judge
			# what the call sets. Only "set" / "changed" is ever shown. A child table
			# is built from the call too (the resolved rows carry masked secrets and
			# bookkeeping), as the rows the save will STORE (``_resulting_rows``).
			to_val = changes.get(key)
		from_val = old.get(key)
		if full and isinstance(to_val, list):
			to_val = _resulting_rows(meta, key, to_val, from_val, doc)
			if not whole and _same_rows(meta, key, to_val, from_val):
				continue  # every row kept as it is: nothing to show
		df = meta.get_field(key) if meta else None
		if same_value(from_val, to_val, df):
			continue  # the save would not change the stored value
		# Never a password / secret value (_diff_row masks it).
		diff.append(_diff_row(meta, key, from_val, to_val, doc, full))
		if not full and len(diff) >= _MAX_ROWS:
			break
	if full:
		diff.extend(_code_diff_rows(doctype, changes, old))
	title = ""
	if doc is not None and meta:
		try:
			tf = meta.get_title_field()
			if tf and tf != "name" and hasattr(doc, tf):
				title = fmt(doc.get(tf), meta.get_field(tf), doc)
		except Exception:
			title = ""
	return {"kind": "update", "doctype": doctype, "name": name, "title": title, "diff": diff}


def _bulk_update_card(args: dict, updates, full: bool = False) -> dict | None:
	"""Per-record from->to diff for a batch ``update_doc(updates=[{name, changes}])``.

	Each rendered record's OLD values come from its current (post-rollback) doc,
	permission-CHECKED then perm-filtered so a restricted field never leaks; the
	NEW values are the caller's requested ``changes``. No-op fields are dropped
	(comparing the values the SAVE would store, never their display forms - see
	``same_value``: fmt_money renders 100.005 and 100.001 identically, so a display
	compare would drop a real change from the card), permlevel-restricted
	fields are skipped (the confirmed save would silently drop them - mirrors
	_update_card's ``would`` guard), and secret / Password values are masked. Only
	the first ``_MAX_ROWS`` records are rendered - each costs one doc read - and the
	rest ride ``extra`` (the raw payload under Details still lists every name).
	``varying`` flags a heterogeneous batch (the requested changes differ across
	records). Each record also carries the changed field labels for its row."""
	doctype = args.get("doctype")
	meta = _meta(doctype)
	first = updates[0].get("changes") if isinstance(updates[0], dict) else None
	varying = any(isinstance(u, dict) and u.get("changes") != first for u in updates)
	records = []
	for u in updates if full else updates[:_MAX_ROWS]:
		if not isinstance(u, dict):
			continue
		name = u.get("name")
		changes = u.get("changes") if isinstance(u.get("changes"), dict) else {}
		# OLD from the current doc (the park dry-run rolled back), perm-filtered so a
		# restricted field's old value never leaks - same guard as _update_card.
		old, loaded = {}, False
		try:
			doc = frappe.get_doc(doctype, name)
			# get_doc checks NO permission (document.py:141-145 -> :336-349 defaults
			# check_permission to None) and the fieldlevel filter is permlevel-only.
			if not doc.has_permission("read"):
				raise frappe.PermissionError
			doc.apply_fieldlevel_read_permissions()
			old = doc.as_dict()
			loaded = True
		except Exception:
			frappe.clear_messages()
			old = {}
		diff, fields = [], []
		# over-scan so no-ops don't eat slots (a sensitive card scans and shows all)
		for key in list(changes) if full else list(changes)[: _MAX_ROWS * 2]:
			# A field the user cannot read at their permlevel is delattr'd from the
			# perm-filtered doc; the confirmed save silently skips it too, so don't
			# show a phantom change (mirrors _update_card's ``key not in would``).
			if loaded and key not in old:
				continue
			cdf = meta.get_field(key) if meta else None
			to_val = changes.get(key)
			if full and isinstance(to_val, list):
				# As it will be stored.
				to_val = _resulting_rows(meta, key, to_val, old.get(key), doc if loaded else None)
				if _same_rows(meta, key, to_val, old.get(key)):
					continue
			if same_value(old.get(key), to_val, cdf):
				continue  # the save would not change the stored value
			# Never a password / secret value (_diff_row masks it).
			row = _diff_row(meta, key, old.get(key), to_val, doc if loaded else None, full)
			fields.append(row["label"])
			diff.append(row)
			if not full and len(diff) >= _MAX_ROWS:
				break
		row_title = ""
		if loaded and meta:
			try:
				tf = meta.get_title_field()
				if tf and tf != "name" and hasattr(doc, tf):
					row_title = fmt(doc.get(tf), meta.get_field(tf), doc)
			except Exception:
				row_title = ""
		records.append({"name": name, "title": row_title, "fields": fields, "diff": diff})
	if not records:
		return None
	return {
		"kind": "bulk_update",
		"doctype": doctype,
		"count": len(updates),
		"records": records,
		"extra": max(0, len(updates) - len(records)),
		"varying": varying,
	}


def _batch_create_card(args: dict, would, full: bool = False) -> dict | None:
	"""Per-record proposed content for a batch create.

	Values come from ``args.docs[i].values``, NOT from ``would``: the sandbox inserted
	those rows and rolled them back, so ``would.created[i]["name"]`` points at nothing
	and cannot be read. For a create the args are also the MORE truthful source -
	Frappe skips the child-table permlevel reset on new records
	(document.py:1326-1328), so a permlevel-restricted child field the caller set IS
	written, while ``would`` is read-filtered and would under-report it.

	``notes`` is NOT rendered: it is a tool argument the MODEL writes
	(create_doc.py:87 -> :134), and on the card it reads as system truth. The card is
	the human's independent check on the agent; the agent can say what it likes in
	chat, where the claim is attributed to it.
	"""
	if not isinstance(would, dict) or not isinstance(would.get("created"), list):
		return None
	created = would["created"]
	docs = args.get("docs") if isinstance(args.get("docs"), list) else []
	rows = [
		{"doctype": d.get("doctype"), "name": d.get("name")}
		for d in (created if full else created[:_MAX_ROWS])
		if isinstance(d, dict)
	]
	records = []
	# zip, never filter-then-index: filtering non-dicts out of `created` first would
	# pair docs[i] with the WRONG created entry from the first bad row onwards.
	# strict=False is the DELIBERATE choice: unequal lengths only happen for a
	# direct caller or a stale preview, and the card must degrade to a partial
	# render rather than raise (build_card is best-effort; a raise costs the
	# whole card). Through the gate the lists are always equal and aligned.
	pairs = list(zip(created, docs, strict=False))
	for made, req in pairs if full else pairs[:_MAX_ROWS]:
		if not isinstance(made, dict) or not isinstance(req, dict):
			continue
		doctype = req.get("doctype") or made.get("doctype")
		values = req.get("values") if isinstance(req.get("values"), dict) else {}
		meta = _meta(doctype)  # PER ITEM: a batch can mix doctypes
		# Tables FIRST, then EVERYTHING not rendered as a table goes through
		# values_rows - including lists table_rows REJECTED (unknown doctype -> meta
		# is None -> table_rows returns None; a list on a non-Table field) and the
		# _MAX_TABLES overflow, which the spec promises degrades to "N rows". Those
		# fall to fmt's list branch. A proposed key must NEVER vanish: splitting on
		# isinstance(v, list) up front drops every list table_rows rejects, so a
		# human approves a create without seeing its line items - the exact defect
		# this redesign exists to kill. Mirrors _create_card's table_keys pattern
		# (confirm_card.py:121-141), which already solved this.
		tables, table_keys = [], set()
		for key, value in values.items():
			if not full and len(tables) >= _MAX_TABLES:
				break
			if not isinstance(value, list) or not value:
				continue
			t = table_rows(meta, key, _resulting_rows(meta, key, value, []) if full else value, full)
			if t:
				tables.append(t)
				table_keys.add(key)
		body = values_rows(meta, {k: v for k, v in values.items() if k not in table_keys}, full=full)
		records.append(
			{
				"doctype": doctype,
				"name": made.get("name"),
				"rows": body["rows"],
				"extra": body["extra"],
				"tables": tables,
			}
		)
	return {
		"kind": "batch_create",
		"count": len(created),
		"rows": rows,
		"extra": max(0, len(created) - len(rows)),
		"records": records,
	}


def _target_name(item):
	if isinstance(item, dict):
		return item.get("name") or item.get("recipients") or item.get("doctype")
	return item


def _verb_records(doctype, names, full: bool = False) -> list[dict]:
	"""A summary per target, capped. ``summary_rows`` returns None for a record that
	is MISSING or that the caller cannot READ - both degrade to name-only, and they
	must stay indistinguishable so the card is not an existence oracle. The title
	only ever comes from summary_rows' permission-checked path.
	"""
	out = []
	for name in names if full else names[:_MAX_ROWS]:
		summary = summary_rows(doctype, name, full) if doctype and name else None
		record = {
			"name": name,
			"title": summary["title"] if summary else "",
			"rows": summary["rows"] if summary else [],
		}
		if summary and summary.get("tables"):
			record["tables"] = summary["tables"]  # a sensitive delete's child tables
		out.append(record)
	return out


def _verb_card(tool: str, args: dict, bulk_items, full: bool = False) -> dict:
	verb = _VERB[tool]
	doctype = args.get("doctype")
	action = args.get("action") or ""  # apply_workflow_action only
	if bulk_items:
		shown = bulk_items if full else bulk_items[:_MAX_ROWS]
		targets = [t for t in (_target_name(x) for x in shown) if t]
		card = {
			"kind": "verb",
			"verb": verb,
			"action": action,
			"doctype": doctype,
			"count": len(bulk_items),
			"targets": targets,
			"extra": max(0, len(bulk_items) - len(targets)),
			"records": _verb_records(doctype, targets, full),
		}
	else:
		targets = [args["name"]] if args.get("name") else []
		card = {
			"kind": "verb",
			"verb": verb,
			"action": action,
			"doctype": doctype,
			"count": 1,
			"targets": targets,
			"extra": 0,
			"records": _verb_records(doctype, targets, full),
		}
	if tool in _CONSEQUENCE:
		one, many = _CONSEQUENCE[tool]
		card["consequence"] = many if card["count"] > 1 else one
	return card


# (key, label, the TOOL'S SIGNATURE DEFAULT). read defaults True (share_doc.py:38);
# everything else False. The default is half the effective value.
_SHARE_FLAGS = (
	("read", "Read", True),
	("write", "Write", False),
	("submit", "Submit", False),
	("share", "Share", False),
)


def _flag_on(args: dict, key: str, default: bool) -> bool:
	"""The value the TOOL will act on, not the value the model typed.

	``bool(args[key])`` when the key is PRESENT - mirroring share_doc's
	``int(bool(...))`` (share_doc.py:92-94), where bool("false") is True, so the
	string "false" GRANTS. The signature default when the key is ABSENT - share_doc
	defaults read=True (share_doc.py:38), so a call that never mentions `read` still
	grants it. Presence, not ``.get()``: absent and explicit-null take different
	branches in the tool.
	"""
	return bool(args[key]) if key in args else default


def _share_card(args: dict, bulk_items) -> dict:
	"""Grantee + permission flags + target summaries.

	Before this, share_doc's card was "share_doc doctype=X name=Y": read-for-one-user
	and everyone+write+share rendered IDENTICALLY - and those grants are the exact
	reason share_doc was pulled into the gate.
	"""
	doctype = args.get("doctype")
	everyone = _flag_on(args, "everyone", False)
	targets = [t for t in (bulk_items or [args.get("name")]) if t][:_MAX_ROWS]
	total = len(bulk_items) if bulk_items else 1
	return {
		"kind": "share",
		"doctype": doctype,
		"grantee": "Everyone" if everyone else fmt(args.get("user") or ""),
		"everyone": everyone,
		"flags": [
			{"label": label, "on": _flag_on(args, key, default)} for key, label, default in _SHARE_FLAGS
		],
		"notify": _flag_on(args, "notify", False),
		"count": total,
		"records": _verb_records(doctype, targets),
		"extra": max(0, total - len(targets)),
	}


def _assign_card(args: dict, bulk_items) -> dict:
	"""Assignee + the description that gets EMAILED to them + target summaries.

	``notify`` defaults True (assign_to.py:40) so an absent arg still sends mail -
	but an EXPLICIT notify=None reaches int(bool(None)) -> 0 and sends none
	(assign_to.py:84). _flag_on distinguishes them; ``.get()`` truthiness would not.
	"""
	doctype = args.get("doctype")
	targets = [t for t in (bulk_items or [args.get("name")]) if t][:_MAX_ROWS]
	total = len(bulk_items) if bulk_items else 1
	return {
		"kind": "assign",
		"doctype": doctype,
		"assignee": fmt(args.get("user") or ""),
		"description": fmt(args.get("description") or "", limit=_MAX_BODY),
		"priority": fmt(args.get("priority") or ""),
		"date": fmt(args.get("date") or ""),
		"notify": _flag_on(args, "notify", True),
		"count": total,
		"records": _verb_records(doctype, targets),
		"extra": max(0, total - len(targets)),
	}


def _recips(value) -> str:
	if isinstance(value, list):
		return ", ".join(str(x) for x in value)
	return "" if value is None else str(value)


def _email_attachment_names(attachments):
	from jarvis.tools.send_email import resolve_email_attachments

	return [fmt(f.file_name) for f in resolve_email_attachments(attachments)]


def _email_card(args: dict) -> dict | None:
	to = args.get("recipients") or args.get("to") or ""
	return {
		"kind": "email",
		"to": fmt(_recips(to)),
		"subject": fmt(args.get("subject") or ""),
		"cc": fmt(_recips(args.get("cc") or "")),
		"bcc": fmt(_recips(args.get("bcc") or "")),
		"print_format": fmt(args.get("print_format") or ""),
		"attachments": _email_attachment_names(args.get("attachments")),
		"body": fmt(args.get("content") or args.get("message") or "", limit=_MAX_BODY),
	}


def _bulk_email_card(messages: list) -> dict | None:
	"""A mail-merge: every message has its OWN recipient, subject and body
	(send_email.py:19-24). The old card returned None on the reasoning that "the
	count is clearer than one body" - true for ONE message to many people, which is
	the SINGLE call's ``recipients`` list, not this shape. send_email is _DESTRUCTIVE
	and always parks; showing the least of any gated tool for the one thing that
	cannot be recalled was the worst gap in the system.
	"""
	shown = []
	for m in messages[:_MAX_ROWS]:
		if not isinstance(m, dict):
			continue
		shown.append(
			{
				"name": fmt(m.get("name") or ""),
				"recipients": fmt(_recips(m.get("recipients") or "")),
				# The batch honours per-message cc/bcc (send_email.py:119). Without these
				# a merge that bcc's a third party on every message renders identical to
				# one that does not - hidden recipients on the one irreversible tool.
				"cc": fmt(_recips(m.get("cc") or "")),
				"bcc": fmt(_recips(m.get("bcc") or "")),
				"subject": fmt(m.get("subject") or ""),
				"attachments": _email_attachment_names(m.get("attachments")),
				"body": fmt(m.get("content") or "", limit=_MAX_BULK_BODY),
			}
		)
	if not shown:
		return None
	return {
		"kind": "bulk_email",
		"count": len(messages),
		"messages": shown,
		"extra": max(0, len(messages) - len(shown)),
	}


def _skill_card(args: dict) -> dict:
	"""Persistent agent instructions - the card was the bare tool name.

	``scope`` is the EFFECTIVE value: create_custom_skill computes a `requested`
	scope and then hardcodes "scope": "User" (create_custom_skill.py:44-57), so
	echoing args.scope would claim a bench-wide skill while creating a private one.
	The instructions body gets _MAX_BODY, not the 200-char scalar cap: approving
	text you structurally cannot read is theatre.
	"""
	ui = args.get("user_invocable")
	if isinstance(ui, str):
		ui = ui.strip().lower() in ("1", "true", "yes", "on")
	return {
		"kind": "skill",
		"skill_name": fmt(args.get("skill_name") or ""),
		"scope": "User (private)",  # the tool caps it regardless of the request
		"user_invocable": bool(1 if ui is None else ui),
		"description": fmt(args.get("description") or "", limit=_MAX_VAL),
		"instructions": fmt(args.get("instructions") or "", limit=_MAX_BODY),
	}


def _wiki_card(args: dict) -> dict:
	"""``replace_body_md`` is a FULL REWRITE and says so; ``append_md`` adds a
	section. The tool rejects both together (update_wiki.py:3-4), so mode is
	unambiguous. A diff against the current body is the better card - deferred, it
	needs the current body loaded (spec open question 3).
	"""
	replace = args.get("replace_body_md")
	append = args.get("append_md")
	# MIRROR THE TOOL'S PRECEDENCE EXACTLY. update_wiki.py:143-146 is
	#   if append_md and str(append_md).strip(): ...append...
	#   elif replace_body_md is not None: doc.body_md = str(replace_body_md)
	# and the new-page path (:165) is str(replace_body_md or append_md or "") -
	# APPEND WINS whenever it is non-blank, including over replace_body_md="", which
	# is falsy and so sails through the both-args guard (:96, also truthiness).
	# Checking replace first inverts this: update_wiki(replace_body_md="",
	# append_md="<injected>") would render an EMPTY ERASE card while the tool APPENDS
	# the payload - a phantom action and a hidden real one in the same shape.
	#
	# `is not None` on replace is still right for the lone case: replace_body_md=""
	# sets body_md = str("") - a full ERASE - and must not be mistaken for a metadata
	# edit.
	if append and str(append).strip():
		mode = "append"
		body_src = append
	elif replace is not None:
		mode = "replace"
		body_src = replace
	else:
		mode = "meta"  # the tool no-ops a blank append (update_wiki.py:143)
		body_src = ""
	ref = ""
	if args.get("ref_doctype") and args.get("ref_name"):
		ref = f"{args['ref_doctype']} {args['ref_name']}"
	return {
		"kind": "wiki",
		"slug": fmt((args.get("slug") or "").strip().lower()),  # the tool strips/lowers (:93)
		"title": fmt((args.get("title") or "")[:140]),  # the tool truncates to 140 (:134)
		"scope": fmt((args.get("scope") or "Org").capitalize()),  # the tool capitalizes (:104)
		"page_type": fmt(args.get("page_type") or ""),
		"ref": fmt(ref),
		# `summary` is PERSISTED (update_wiki.py:141-142, :164) - a call setting only
		# summary would otherwise render as an empty "meta" card and the human would
		# approve stored text they never saw.
		"summary": fmt(args.get("summary") or "", limit=_MAX_VAL),
		"mode": mode,
		"body": fmt(body_src, limit=_MAX_BODY),  # the body the TOOL will write
	}


def _method_card(args: dict) -> dict:
	inner = args.get("args") if isinstance(args.get("args"), dict) else {}
	shown = {}
	for k, v in list(inner.items())[:_MAX_ROWS]:
		# No meta for a run_method arg bag, so this is the key-name check only.
		shown[str(k)] = "[hidden]" if is_secret(None, k) else fmt(v)
	return {"kind": "method", "method": fmt(args.get("method") or ""), "args": shown}


_MAX_SAMPLE_COLS = 8  # columns rendered in the import sample table (20 is unusable)


def _import_card(args: dict, preview) -> dict | None:
	"""Rich confirmation card for run_import, rendered from the read-only import preview
	the park stashed at ``preview["import"]`` (already secret-masked, permission-checked).
	Shows WHAT is imported - target doctype, record vs row count, the file, the sample
	rows, the column mapping, advisory warnings - not a function path. Returns None on a
	malformed preview so build_card's caller shows the described-intent floor (the park
	always attaches a ``note``), never a blank card."""
	imp = preview.get("import") if isinstance(preview, dict) else None
	if not isinstance(imp, dict):
		return None

	columns = imp.get("columns") or []
	mapped = [
		(f"{c.get('header')} -> {c.get('label')}" if c.get("label") else c.get("header"))
		for c in columns
		if c.get("mapped")
	]
	unmapped = [c.get("header") for c in columns if not c.get("mapped")]

	sample = imp.get("sample") if isinstance(imp.get("sample"), dict) else {}
	sample_cols = sample.get("columns") or []
	sample_rows = sample.get("rows") or []
	col_cap = min(len(sample_cols), _MAX_SAMPLE_COLS)
	shown_cols = sample_cols[:col_cap]
	shown_rows = [{"cells": (r.get("cells") or [])[:col_cap]} for r in sample_rows]

	advisory = [
		w.get("message") for w in (imp.get("warnings") or {}).get("advisory") or [] if w.get("message")
	]

	return {
		"kind": "import",
		"doctype": imp.get("doctype"),
		"import_type": imp.get("import_type"),
		"file": (imp.get("file") or {}).get("name"),
		"total_rows": imp.get("total_rows"),
		"total_records": imp.get("total_records"),
		# Only claim "will submit" when the target is actually submittable (else the card
		# lies: submit_after_import silently no-ops on a non-submittable doctype).
		"submit_after_import": bool(args.get("submit_after_import")) and bool(imp.get("submittable")),
		"columns": {"mapped": mapped[:_MAX_ROWS], "unmapped": unmapped[:_MAX_ROWS]},
		"sample": {
			"columns": shown_cols,
			"rows": shown_rows,
			"extra_cols": max(0, len(sample_cols) - col_cap),
		},
		"advisory": advisory[:_MAX_ROWS],
	}
