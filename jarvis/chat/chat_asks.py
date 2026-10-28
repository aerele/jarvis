"""Chat-sourced approvals: surface a ```jarvis-ask``` question on the
Approval Board so an away user finds it (notify-approvals design, Part 2).

The agent's attended-chat ask mechanism is a fenced ```jarvis-ask``` JSON
block in the assistant reply (persona AGENTS.md contract); until now only
the SPA parsed it (ChatView.vue ``_ASK_RE``/``askOf``), so a user who left
the tab never learned the agent was waiting. This module is the server-side
port of that parser plus the row lifecycle:

- ``materialize_from_turn``: called by turn_handler after the final
  assistant persist — parse the LAST fence, create ONE Pending
  ``Jarvis Approval Request`` (source "Chat") owned by the conversation
  owner, and publish an ``approval:new`` jarvis:event to them.
- ``resolve_on_user_message``: called at user-message intake — the user
  replied in chat, so any Pending chat-sourced row for that conversation
  flips to "Answered" (decision "(answered in chat)"). This closes the
  double-answer race: the board never offers a decision the chat already
  consumed.

Both callers wrap these in try/except (turn_handler is hot-path; a bug
here must never break a turn). Rows with source NULL are File-Box rows
from before this field existed — code treats NULL as "File Box".
"""

from __future__ import annotations

import json
import re

import frappe

from jarvis.chat.events import publish_to_user

APPROVAL = "Jarvis Approval Request"
CONV = "Jarvis Conversation"

# Port of the client fence grammar (ChatView.vue:1457 ``_ASK_RE``):
# ```jarvis-ask, optional spaces/tabs, newline, non-greedy body, closing
# fence. The client matches the FIRST fence of the last assistant message;
# server-side the fence may appear mid-content with other prose, so we take
# the LAST fence in the final content (see ``parse_ask``).
_ASK_RE = re.compile(r"```jarvis-ask[ \t]*\n([\s\S]*?)```")

_FIELD_TYPES = ("date", "datetime", "field", "link", "select", "text")

# Types a model may put in ``kind`` instead of ``type`` (chatAsk.js ASK_KINDS).
_ASK_KINDS = ("single", "multi", "yesno", "boolean", *_FIELD_TYPES)

# Same identifier guards as the client parser (chatAsk.js DOCTYPE_RE/FIELDNAME_RE).
_DOCTYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]*")
_FIELDNAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# The board shows options as chips; a long Select list is answered in chat
# (where the real dropdown is) rather than truncated into a misleading chip row.
_BOARD_MAX_CHIPS = 8

_ANSWERED_DECISION = "(answered in chat)"


def humanize_fieldname(fieldname: str | None) -> str:
	"""Port of chatAsk.js ``humanizeFieldname``: ``custom_gstin_2`` -> ``Gstin 2``."""
	s = re.sub(r"_+", " ", re.sub(r"^custom_", "", str(fieldname or ""))).strip()
	return s[:1].upper() + s[1:]


def ask_label(question: dict) -> str:
	"""Board label of a parsed question: its text, else (a ``field`` question
	may carry none; the card labels itself from the field) the humanized
	fieldname."""
	return question["q"] or humanize_fieldname(question.get("fieldname"))


def _ask_text(q: dict) -> str:
	"""Port of chatAsk.js ``askText``: the first non-empty of the keys a model
	may use for the question text."""
	for key in ("q", "question", "label", "title", "prompt"):
		value = "" if q.get(key) is None else str(q[key]).strip()
		if value:
			return value
	return ""


def _nested_field(value):
	"""Port of chatAsk.js ``nestedField``: a ``{doctype, fieldname}`` dict naming at
	least one of the two, else None (a list or an empty dict is no field reference)."""
	if not isinstance(value, dict):
		return None
	return value if value.get("doctype") or value.get("fieldname") else None


def _ask_items(parsed):
	"""Port of chatAsk.js ``askItems``: a bare list, ``{questions: [...]}``, or
	one question object on its own (a list of one)."""
	if isinstance(parsed, list):
		return parsed
	if not isinstance(parsed, dict):
		return None
	if "questions" in parsed:
		return parsed["questions"]
	return [parsed] if _ask_text(parsed) or parsed.get("type") or parsed.get("kind") else None


def parse_ask(content: str | None) -> list[dict]:
	"""Normalized questions ``[{q, type, options, doctype, fieldname}]`` from
	the LAST ```jarvis-ask``` fence in ``content``, or ``[]``.

	Mirrors chatAsk.js ``parseAsk`` exactly: JSON body is a list,
	``{questions: [...]}`` or one question object; the text is the first of
	``q``/``question``/``label``/``title``/``prompt``, the type is ``type`` else
	a known ``kind``, and a nested ``field: {doctype, fieldname}`` makes a
	``field`` question; at most 6 questions; ``type`` normalized
	("boolean" -> "yesno", unknown -> "single"); at most 8 string options
	(200 for ``select``, which with none degrades to ``text``); a ``field``
	question needs a plain-identifier doctype and fieldname (else it degrades
	to a ``text`` question labelled by the humanized fieldname) and may have an
	empty ``q``; any other question is kept only when it has text AND (it is
	yesno / a field type, or it carries options). Malformed JSON -> ``[]``.
	"""
	matches = _ASK_RE.findall(content or "")
	if not matches:
		return []
	try:
		parsed = json.loads(matches[-1].strip())
	except Exception:
		return []
	raw = _ask_items(parsed)
	if not isinstance(raw, list):
		return []
	out: list[dict] = []
	for q in raw[:6]:
		if not isinstance(q, dict):
			continue
		nested = _nested_field(q.get("field"))
		qtype = q.get("kind") if q.get("type") is None and q.get("kind") in _ASK_KINDS else q.get("type")
		if nested:
			qtype = "field"
		if qtype == "boolean":
			qtype = "yesno"
		if qtype not in ("single", "multi", "yesno", *_FIELD_TYPES):
			qtype = "single"
		cap = 200 if qtype == "select" else 8
		options = q.get("options")
		options = [str(o) for o in options[:cap]] if isinstance(options, list) else []
		if qtype == "select" and not options:
			qtype = "text"
		text = _ask_text(q)
		if qtype == "field":
			source = nested or q
			doctype = str(source.get("doctype") or "").strip()
			fieldname = str(source.get("fieldname") or "").strip()
			if _DOCTYPE_RE.fullmatch(doctype) and _FIELDNAME_RE.fullmatch(fieldname):
				out.append(
					{"q": text, "type": qtype, "options": [], "doctype": doctype, "fieldname": fieldname}
				)
				continue
			# a bad identifier degrades to a typed answer instead of vanishing
			text = text or humanize_fieldname(fieldname)
			qtype, options = "text", []
		if not text:
			continue
		if qtype not in ("yesno", *_FIELD_TYPES) and not options:
			continue
		doctype = str(q.get("doctype") or q.get("link") or "").strip() if qtype == "link" else ""
		out.append({"q": text, "type": qtype, "options": options, "doctype": doctype, "fieldname": ""})
	return out


def question_excerpt(content: str | None, limit: int = 140) -> str:
	"""Short single-line excerpt of the question living in ``content``, for
	the board's awaiting-reply lane. Fence present and parseable -> the
	question text(s); else the tail of the prose up to its last "?" (the
	question sits at the END of a reply, so keep the tail, not the head)."""
	content = content or ""
	questions = parse_ask(content) if "```jarvis-ask" in content else []
	if questions:
		text = re.sub(r"\s+", " ", " ".join(ask_label(q) for q in questions)).strip()
		return text[:limit]
	# a malformed/unparseable fence must not leak raw JSON into the excerpt —
	# drop fence blocks before falling back to the prose tail
	content = re.sub(r"```jarvis-ask.*?(?:```|$)", " ", content, flags=re.S)
	tail = content[-200:]
	pos = tail.rfind("?")
	seg = tail[: pos + 1] if pos >= 0 else content
	text = re.sub(r"\s+", " ", seg).strip()
	return text[-limit:].lstrip()


def materialize_from_turn(conversation: str, assistant_content: str) -> str | None:
	"""Create ONE Pending chat-sourced approval row from the final assistant
	content's ```jarvis-ask``` fence, and tell the conversation owner.

	Returns the new row name, or None when skipped (no/malformed fence, an
	open chat ask already exists, or the conversation is gone). Called by
	turn_handler after the final assistant persist — the caller wraps it in
	try/except, and the fast no-fence path costs one substring check.
	"""
	if "```jarvis-ask" not in (assistant_content or ""):
		return None
	questions = parse_ask(assistant_content)
	if not questions:
		return None
	# Dedupe: one open chat ask per conversation. A re-asked/refined question
	# supersedes the old one in chat anyway, and resolve_on_user_message
	# flips the old row the moment the user replies.
	if frappe.db.exists(APPROVAL, {"conversation": conversation, "status": "Pending", "source": "Chat"}):
		return None
	conversation_meta = frappe.db.get_value(CONV, conversation, ["owner", "origin_page"], as_dict=True)
	if not conversation_meta:
		return None
	owner = conversation_meta.owner
	# One decision field per row: a single question carries its options as
	# board chips; a multi-question block becomes a free-form row (the board
	# routes those to "Answer in chat").
	question_text = "\n".join(ask_label(q) for q in questions)
	options = questions[0]["options"] if len(questions) == 1 else []
	# A field question has no options (the board cannot draw its real control:
	# it takes a typed answer in chat); a long Select list is answered in chat
	# too, not cut to a partial chip row.
	if len(options) > _BOARD_MAX_CHIPS:
		options = []
	doc = frappe.get_doc(
		{
			"doctype": APPROVAL,
			"title": ask_label(questions[0])[:100],
			"status": "Pending",
			"source": "Chat",
			"conversation": conversation,
			"question": question_text,
			"options": json.dumps(options) if options else "",
		}
	)
	doc.insert(ignore_permissions=True)
	# The board scopes visibility by the LINKED CONVERSATION's owner, but the
	# row itself should read as the customer's (if_owner Desk perms + audit).
	# v16's set_user_and_timestamp overwrites owner with the session user on
	# insert, so stamp it AFTER — the same idiom the controller uses for the
	# trace Comment's owner.
	if doc.owner != owner:
		frappe.db.set_value(APPROVAL, doc.name, "owner", owner, update_modified=False)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist before realtime publish
	publish_to_user(
		owner,
		{
			"kind": "approval:new",
			"conversation_id": conversation,
			"origin_page": conversation_meta.origin_page or "",
			"name": doc.name,
			"question": question_text,
		},
	)
	return doc.name


def resolve_on_user_message(conversation: str) -> None:
	"""The user sent a message in ``conversation``: any Pending chat-sourced
	approval there is now answered in chat — flip it to "Answered" so the
	board row never goes stale or double-answered. One indexed UPDATE
	(conversation is search_index'd); decided rows are untouched, so a
	board decide() that resumes the chat never re-flips its own row. A File Box
	sheet's question is answered with its sheet, never here."""
	from jarvis.jarvis.doctype.jarvis_approval_request.jarvis_approval_request import sheet_ready

	open_filter = {"conversation": conversation, "status": "Pending", "source": "Chat"}
	if sheet_ready():
		open_filter["sheet"] = ["is", "not set"]
	if not frappe.db.exists(APPROVAL, open_filter):
		return
	# Lock exactly the Pending rows that this answer wins. A simultaneous
	# reviewer decision cannot be announced as answered or resumed twice.
	a = frappe.qb.DocType(APPROVAL)
	where = (a.conversation == conversation) & (a.status == "Pending") & (a.source == "Chat")
	if sheet_ready():
		from frappe.query_builder.functions import Coalesce

		where &= Coalesce(a.sheet, "") == ""
	rows = frappe.qb.from_(a).select(a.name).where(where).for_update().run(pluck=True)
	if not rows:
		return
	frappe.db.sql(
		"""update `tabJarvis Approval Request`
		set status='Answered', decision=%(decision)s,
			decided_by=%(user)s, decided_at=%(now)s
		where conversation=%(conversation)s
		and status='Pending' and source='Chat'"""
		+ (" and IFNULL(sheet, '')=''" if sheet_ready() else ""),
		{
			"decision": _ANSWERED_DECISION,
			"user": frappe.session.user,
			"now": frappe.utils.now_datetime(),
			"conversation": conversation,
		},
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist before realtime publish
	# The board badge counted it: tell the owner's shell to re-count.
	owner = frappe.db.get_value(CONV, conversation, "owner")
	if owner:
		publish_to_user(owner, {"kind": "action:settled", "conversation_id": conversation})

	from jarvis.chat.approval_reviewers import notify_settled

	for name in rows:
		notify_settled(frappe.get_doc(APPROVAL, name), include_requester=False)
