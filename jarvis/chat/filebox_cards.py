"""A ``jarvis-action`` card in a File Box reply goes through the File Box write policy.

A File Box run should write through tools (``held_writes``), but the persona can
still end a turn with a create/update card - typically on a resume turn. That card
lived only in the chat: nothing reached the Approval Board, and a chat Confirm wrote
straight past the policy. At turn end the card now runs as the tool call it stands
for, as the conversation owner: a draft applies, a master is held on the board, and a
refused write (a Single such as GST Settings) becomes a question on the board. The
card is then replaced by a one-line note, so it can't also be confirmed in chat."""

from __future__ import annotations

import json
import re

import frappe

from jarvis._session import impersonate
from jarvis.chat.events import publish_to_user

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
AR = "Jarvis Approval Request"
# The client's fence grammar (ChatView.vue ``_ACTION_RE``): the FIRST block wins.
_ACTION_RE = re.compile(r"```jarvis-action[ \t]*\n([\s\S]*?)```")
_DONE_OPTIONS = ["Done", "Skip"]
_PROCESSING = "Sending this change to the Approval Board."
_GONE = "This change was already sent to the Approval Board. Look for it there."
_WAITING = "This change waits for an earlier approval; Jarvis picks it up once that is decided."


def parse_action(content: str | None) -> dict | None:
	"""The first ``jarvis-action`` block of ``content`` when it is a doc create/update."""
	m = _ACTION_RE.search(content or "")
	if not m:
		return None
	try:
		a = json.loads(m.group(1).strip())
	except ValueError:
		return None
	if not isinstance(a, dict) or (a.get("kind") or "doc") != "doc" or not a.get("doctype"):
		return None
	if (a.get("verb") or "create") not in ("create", "update"):
		return None
	return a


def _fieldname(meta, label: str) -> str | None:
	if label == "name" or meta.has_field(label):
		return label
	key = label.strip().lower()
	for df in meta.fields:
		if (df.label or "").strip().lower() == key or df.fieldname == key.replace(" ", "_"):
			return df.fieldname
	return None


def tool_call(a: dict) -> tuple[str, dict] | None:
	"""``(tool, args)`` for card ``a``; None when a field can't be matched to the doctype."""
	doctype = a["doctype"]
	if not frappe.db.exists("DocType", doctype):
		return None
	meta = frappe.get_meta(doctype)
	verb = a.get("verb") or "create"
	values = {}
	fields = a.get("fields") or []
	if isinstance(fields, dict):
		fields = [{"label": k, "value": v} for k, v in fields.items()]
	for f in fields:
		if not isinstance(f, dict) or not f.get("label"):
			continue
		value = f.get("value")
		if verb == "create" and (value is None or str(value).strip() == ""):
			continue  # a blank on a create card is "unknown", as the client treats it
		fieldname = _fieldname(meta, str(f["label"]))
		if not fieldname:
			return None
		values[fieldname] = value
	tables = a.get("tables") or []
	if isinstance(tables, dict):
		tables = [
			{"fieldname": k, "rows": v.get("rows") if isinstance(v, dict) else v} for k, v in tables.items()
		]
	for t in tables:
		rows = t.get("rows") if isinstance(t, dict) else None
		if not isinstance(rows, list) or not meta.has_field(t.get("fieldname") or ""):
			return None
		values[t["fieldname"]] = rows
	if verb == "update":
		if not a.get("name"):
			return None
		return "update_doc", {"doctype": doctype, "name": a["name"], "changes": values}
	return "create_doc", {"doctype": doctype, "values": values}


def _first_miss(message: str) -> bool:
	"""A missing-fields refusal that holds the records on a second identical call (the
	held write's, or the approval sheet's) - which is what a card with blanks means."""
	from jarvis.chat import held_sheets, held_writes

	return any(message.startswith(t.split(":")[0]) for t in (held_writes._FIRST_MISS, held_sheets._FIRST_TRY))


def _run(tool: str, args: dict, conversation: str) -> dict:
	from jarvis import api

	res = api._run_tool(tool, json.loads(json.dumps(args)), conversation=conversation)
	err = (res or {}).get("error") or {}
	if not res.get("ok") and _first_miss(str(err.get("message") or "")):
		res = api._run_tool(tool, json.loads(json.dumps(args)), conversation=conversation)
	return res


def _card_text(a: dict) -> str:
	lines = [f"{a.get('verb') or 'create'} {a['doctype']} {a.get('name') or ''}".strip()]
	for f in a.get("fields") or []:
		if isinstance(f, dict) and f.get("label"):
			lines.append(f"- {f['label']}: {f.get('value')}")
	for t in a.get("tables") or []:
		if isinstance(t, dict) and isinstance(t.get("rows"), list):
			lines.append(f"- {t.get('fieldname')}: {len(t['rows'])} row(s)")
	return "\n".join(lines)


def _ask(conversation: str, owner: str, a: dict, reason: str) -> str:
	"""File the card as a board question: a human handles the change, then answers."""
	doctype = a["doctype"]
	question = (
		f"Jarvis couldn't make this change to {doctype}: handle it yourself, then answer Done (or Skip)."
	)
	doc = frappe.get_doc(
		{
			"doctype": AR,
			"title": f"Change {doctype}"[:100],
			"status": "Pending",
			"source": "File Box",
			"conversation": conversation,
			"document_type": doctype,
			"ref_doctype": doctype,
			"ref_name": a.get("name") or "",
			"question": question,
			"context_md": f"{_card_text(a)}\n\nWhy: {reason}"[:10000],
			"options": json.dumps(_DONE_OPTIONS),
		}
	).insert(ignore_permissions=True)
	if doc.owner != owner:
		frappe.db.set_value(AR, doc.name, "owner", owner, update_modified=False)
	return doc.name


def _note(tool: str | None, res: dict, asked: str | None) -> str:
	if asked:
		return "This change is waiting for you on the Approval Board."
	if not res.get("ok"):
		return _WAITING
	data = res.get("data") or {}
	if data.get("held_for") == "approval_board" or not data.get("name"):
		return "This change is waiting for approval on the Approval Board."
	verb = "Updated" if tool == "update_doc" else "Created"
	return f"{verb} {data.get('doctype') or ''} {data['name']}.".replace("  ", " ")


def _outcome(conversation: str, owner: str, a: dict, call) -> tuple[dict, str | None]:
	"""Run the card as its tool call; a refusal a human must handle becomes a question.
	A write waiting on an earlier approval is left to that approval's resume."""
	try:
		res = _run(*call, conversation) if call else _unreadable()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="jarvis.file_box.card_park_failed", message=frappe.get_traceback())
		res = {"ok": False, "error": {"message": "The change could not be checked."}}
	err = res.get("error") or {}
	if res.get("ok") or err.get("code") == "ApprovalPendingError":
		return res, None
	return res, _ask(conversation, owner, a, str(err.get("message") or ""))


def _park(conversation: str, message: str, content: str, a: dict, call=None, fence: str | None = None):
	"""Claim the card (a compare-and-set on the message, so the turn end and a chat
	Confirm never both write it), run it, and leave the note. None when already claimed.
	If even filing the question fails, the card is put back and the error re-raised."""
	owner = frappe.db.get_value(CONV, conversation, "owner")
	claimed = _sub(content, _PROCESSING, fence)
	if not owner or not _swap(message, content, claimed):
		return None
	try:
		with impersonate(owner):
			call = call or tool_call(a)
			res, asked = _outcome(conversation, owner, a, call)
	except Exception:
		frappe.db.rollback()
		_swap(message, claimed, content)
		raise
	note = _note(call[0] if call else None, res, asked)
	_swap(message, claimed, _sub(content, note, fence))
	if asked:
		publish_to_user(owner, {"kind": "approval:new", "conversation_id": conversation, "name": asked})
	return note


def park_from_turn(conversation: str, message: str | None) -> str | None:
	"""Turn end: route the card in File Box reply ``message`` through the File Box
	policy and replace it with a note. The note, or None when there was no card."""
	if not message or not frappe.db.get_value(CONV, conversation, "file_box"):
		return None
	content = frappe.db.get_value(MSG, message, "content") or ""
	m = _ACTION_RE.search(content)
	a = parse_action(m.group(0)) if m else None
	return _park(conversation, message, content, a, fence=m.group(0)) if a else None


def _js(v) -> str:
	"""``v`` as JavaScript's ``String()`` renders it after a JSON round trip (``null`` as
	"", as the PWA stringifies it), so every client's copy of a card compares equal."""
	if v is None:
		return ""
	if isinstance(v, bool):
		return "true" if v else "false"
	if isinstance(v, float) and v.is_integer():
		return str(int(v))
	if isinstance(v, (dict, list)):
		return json.dumps(_whole(v), sort_keys=True, separators=(",", ":"))
	return str(v)


def _whole(v):
	"""A JSON value with whole-number floats as ints, as a JavaScript round trip leaves it."""
	if isinstance(v, float) and v.is_integer():
		return int(v)
	if isinstance(v, dict):
		return {k: _whole(x) for k, x in v.items()}
	if isinstance(v, list):
		return [_whole(x) for x in v]
	return v


def _ident(a: dict) -> str:
	"""A card's identity, the same however a client normalised it (``fields`` or
	``tables`` as a map or a list, ``kind`` inferred, values stringified)."""
	fields = a.get("fields") or []
	if isinstance(fields, dict):
		fields = [{"label": k, "value": v} for k, v in fields.items()]
	tables = a.get("tables") or []
	if isinstance(tables, dict):
		tables = [
			{"fieldname": k, "rows": v.get("rows") if isinstance(v, dict) else v} for k, v in tables.items()
		]
	pairs = sorted(
		(str(f["label"]), _js(f.get("value"))) for f in fields if isinstance(f, dict) and f.get("label")
	)
	rows = sorted(
		(str(t.get("fieldname")), _js(t.get("rows")))
		for t in tables
		if isinstance(t, dict) and t.get("fieldname")
	)
	return json.dumps([a.get("doctype"), a.get("verb") or "create", a.get("name") or "", pairs, rows])


def _find(conversation: str, doctype: str, card: dict | None, message: str | None):
	"""``(message, content, card, fence)`` of the confirmed card still in the chat, or None.
	Matched by identity, so a stale screen can never confirm a different card."""
	filters = {"conversation": conversation, "role": "assistant", "content": ["like", "%```jarvis-action%"]}
	if message:
		filters["name"] = message
	rows = frappe.get_all(MSG, filters=filters, fields=["name", "content"], order_by="seq desc", limit=20)
	want = _ident(card) if isinstance(card, dict) else None
	for row in rows:
		for m in _ACTION_RE.finditer(row.content or ""):
			a = parse_action(m.group(0))
			if not a or a["doctype"] != doctype:
				continue
			if want is None or _ident(a) == want:
				return row.name, row.content, a, m.group(0)
		if want is None:
			return None  # a client that sends no card: only the newest card's message
	return None


def park_confirmed(
	conversation: str,
	verb: str,
	doctype: str,
	name: str,
	values: dict,
	card: dict | None = None,
	message: str | None = None,
) -> dict:
	"""A chat Confirm in a File Box conversation: the confirmed values go through the File
	Box policy instead of straight to the write, and that exact card becomes its note."""
	found = _find(conversation, doctype, card, message)
	if not found:
		return {"ok": False, "error": {"code": "FileBoxRefusedError", "message": _GONE}}
	msg, content, a, fence = found
	a = {**a, "name": name or a.get("name")}
	call = (
		("update_doc", {"doctype": doctype, "name": a["name"], "changes": values})
		if verb == "update"
		else ("create_doc", {"doctype": doctype, "values": values})
	)
	note = _park(conversation, msg, content, a, call, fence)
	if note is None:
		return {"ok": False, "error": {"code": "FileBoxRefusedError", "message": _GONE}}
	return {"ok": True, "verb": verb, "name": "", "note": note}


def _unreadable() -> dict:
	return {"ok": False, "error": {"message": "The card's fields don't match this doctype."}}


def _sub(content: str, note: str, fence: str | None = None) -> str:
	if fence:
		return content.replace(fence, note)  # an identical copy is the same change
	return _ACTION_RE.sub(lambda _m: note, content, count=1)


def _swap(message: str, old: str, new: str) -> bool:
	"""Compare-and-set the message content; True when this call changed it."""
	frappe.db.sql(
		"UPDATE `tabJarvis Chat Message` SET content=%(new)s WHERE name=%(m)s AND content=%(old)s",
		{"new": new, "m": message, "old": old},
	)
	from jarvis.chat.pending_actions._store import rowcount

	changed = bool(rowcount())
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist swap result
	return changed
