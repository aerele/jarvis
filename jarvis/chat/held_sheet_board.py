"""File Box sheets on the board (T3, S1b): the counts the lane, badge and File Box
lines read (never the card), the records view and its lazy candidates."""

from __future__ import annotations

import frappe

from jarvis.chat import held_edit
from jarvis.chat.held_sheets import category, json_dict
from jarvis.chat.pending_actions import _seal
from jarvis.chat.pending_actions._store import EXECUTING, PENDING

# --------------------------------------------------------------------------- #
# Counts (the board and File Box lines read these, never the card)
# --------------------------------------------------------------------------- #
_SECTIONS = {"party": 0, "address": 1, "item": 2, "other": 3}


def counts(records: list[dict]) -> dict:
	"""``{doctype: records}``."""
	out: dict = {}
	for r in records:
		out[r["doctype"]] = out.get(r["doctype"], 0) + 1
	return out


def _noun(doctype: str, n: int) -> str:
	word = doctype.lower()
	if n == 1:
		return word
	if word.endswith(("s", "x", "ch", "sh")):
		return word + "es"
	if word.endswith("y") and word[-2:-1] not in ("a", "e", "i", "o", "u"):
		return word[:-1] + "ies"
	return word + "s"


def counts_line(by_doctype, questions=0) -> str:
	""" "1 supplier · 100 items · 2 questions": parties, addresses, items, the rest."""
	by_doctype = json_dict(by_doctype)
	order = sorted(by_doctype, key=lambda dt: (_SECTIONS[category(dt)], dt))
	parts = [f"{by_doctype[dt]} {_noun(dt, by_doctype[dt])}" for dt in order if by_doctype[dt]]
	questions = frappe.utils.cint(questions)
	if questions:
		parts.append(f"{questions} question{'' if questions == 1 else 's'}")
	return " · ".join(parts)


# --------------------------------------------------------------------------- #
# The board
# --------------------------------------------------------------------------- #
CANDIDATE_RECORDS = 25


def _questions(sheet: str) -> list[dict]:
	from jarvis.chat.approvals_api import _parse_options

	rows = frappe.db.sql(
		"SELECT name, title, question, options, context_md, routing, status, decision"
		" FROM `tabJarvis Approval Request` WHERE sheet=%(s)s ORDER BY creation, name",
		{"s": sheet},
		as_dict=True,
	)
	return [
		{
			"name": r.name,
			"title": r.title or "",
			"question": r.question or "",
			"options": _parse_options(r.options),
			"context_md": r.context_md or "",
			"routing": r.routing or "",
			"status": r.status,
			"decision": r.decision or "",
		}
		for r in rows
	]


def _fix(value) -> dict | None:
	"""A record's ``{field, label, message}`` fix (a legacy text entry has no field)."""
	if isinstance(value, dict):
		return {
			"field": value.get("field") or None,
			"label": value.get("label") or None,
			"message": str(value.get("message") or ""),
		}
	return {"field": None, "label": None, "message": str(value)} if value else None


def _sealed_records(row) -> list[dict] | None:
	try:
		return _seal.unseal_call(row)["args"]["records"]
	except _seal.SealError:
		return None


def detail(row, me: str) -> dict:
	"""The board's sheet view (owner / System Manager): per record its section, masked
	values, locked fields, dependencies and flags; the linked questions; the counts
	and ``card_sha256`` an apply echoes; the last apply's errors and decisions (they
	survive a bounce), its progress while it runs, and the outcome once applied.
	Never the sealed call."""
	from jarvis.chat import approvals_api
	from jarvis.chat.filebox import _source_file
	from jarvis.chat.pending_actions import _sheet
	from jarvis.chat.pending_actions._store import REASON_TEXT

	pending = row.status == PENDING
	shown = json_dict(row.card).get("records") or []
	items = _sealed_records(row) if pending else None
	docs = held_edit.view(items) if items is not None and len(items) == len(shown) else None
	missing: dict = {}
	for entry in approvals_api._needs_input(row) if pending else []:
		missing.setdefault(entry["doc_index"], []).append(entry)
	fix = json_dict(row.needs_fix)
	records = []
	for pos, r in enumerate(shown):
		view = (
			docs[pos] if docs is not None else {"values": r.get("values") or {}, "secret": [], "locked": {}}
		)
		records.append(
			{
				**{
					k: r.get(k) for k in ("index", "doctype", "op", "name", "title", "category", "depends_on")
				},
				"values": view["values"],
				"secret": view["secret"],
				"locked": view["locked"],
				"needs_input": missing.get(r.get("index"), []),
				"needs_fix": _fix(fix.get(str(r.get("index")))),
			}
		)
	source = _source_file(row.conversation) if row.conversation else None
	code = row.reason_code or ""
	return {
		"name": row.name,
		"kind": row.kind,
		"status": row.status,
		"summary": row.summary or "",
		"file_name": (source and source.file_name) or "",
		"created_at": str(row.creation),
		"age": approvals_api._age_s(row.creation),
		"for_user": approvals_api._for_user(row.owner_user, me),
		"collecting": int(row.collecting or 0),
		"can_act": int(pending and not row.collecting and docs is not None),
		"record_count": int(row.record_count or 0),
		"question_count": int(row.question_count or 0),
		"counts": json_dict(row.sheet_counts),
		"counts_line": counts_line(row.sheet_counts, row.question_count),
		"card_sha256": _seal.card_sha256(row),
		"records": records,
		"questions": _questions(row.name),
		"errors": json_dict(row.get("apply_errors")) if pending else {},
		"decisions": _decisions(row) if pending else {},
		"progress": _sheet.progress(row.name) if row.status == EXECUTING else None,
		"outcome": {k: v for k, v in json_dict(row.sheet_outcome).items() if k != "keys"},
		"reason_code": code,
		"reason": REASON_TEXT.get(code, ""),
	}


def _decisions(row) -> dict:
	"""The last apply's decisions, to fill the sheet again after a bounce."""
	plan = json_dict(row.get("apply_request"))
	return {k: plan[k] for k in ("records", "answers") if k in plan}


def status(row) -> dict:
	"""The poll while a sheet lists or applies: its state, never its records or an unseal."""
	from jarvis.chat.pending_actions import _sheet

	return {
		"name": row.name,
		"status": row.status,
		"collecting": int(row.collecting or 0),
		"progress": _sheet.progress(row.name) if row.status == EXECUTING else None,
		"card_sha256": _seal.card_sha256(row),
		"record_count": int(row.record_count or 0),
		"counts_line": counts_line(row.sheet_counts, row.question_count),
	}


def candidates(row, indexes=None) -> dict:
	"""``{index: [{name, title, gstin}]}``: existing records like each create record
	(its name, tax id or title), read as the sheet's ``exec_user``; at most
	``CANDIDATE_RECORDS`` records a call. Lazy: the detail never computes them."""
	from jarvis.chat import approvals_api

	records = _sealed_records(row) if row.status == PENDING and not row.collecting else None
	if not records:
		return {}
	wanted = range(len(records)) if indexes is None else indexes
	picked = [
		i
		for i in dict.fromkeys(i for i in wanted if type(i) is int)  # never a list, dict or bool
		if 0 <= i < len(records) and records[i]["op"] == "create"
	][:CANDIDATE_RECORDS]
	return {str(i): approvals_api._candidates_as(row.exec_user, records[i]) for i in picked}
