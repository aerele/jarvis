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
- ``ask_lead_in``: the prose just above that fence (what the agent said it was doing),
  stored as plain text in the row's ``context_md`` for chat asks (never File Box), shown on
  the board, and quoted back as DATA in the decision message that resumes the chat, so the
  decision names the task it belongs to (jarvis-admin-v2#793).
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
import traceback

import frappe

from jarvis.chat.events import publish_to_user
from jarvis.chat.latency import get_logger
from jarvis.chat.safe_text import LEAD_IN_CAP, safe_line

APPROVAL = "Jarvis Approval Request"
CONV = "Jarvis Conversation"
GRANT = "Jarvis Approval Reviewer"

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
			parent = str(source.get("parent") or "").strip()
			parentfield = str(source.get("parentfield") or "").strip()
			if (
				_DOCTYPE_RE.fullmatch(doctype)
				and _FIELDNAME_RE.fullmatch(fieldname)
				and (not parent or _DOCTYPE_RE.fullmatch(parent))
				and (not parentfield or (parent and _FIELDNAME_RE.fullmatch(parentfield)))
			):
				out.append(
					{
						"q": text,
						"type": qtype,
						"options": [],
						"doctype": doctype,
						"fieldname": fieldname,
						**({"parent": parent} if parent else {}),
						**({"parentfield": parentfield} if parentfield else {}),
					}
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


# The "why" stored with a chat ask (jarvis-admin-v2#793): what the agent said just above its
# question, so a decision can name the task it belongs to.
_LEAD_IN_SHORT = 120  # a nearest paragraph this short ("A few quick questions:") names no task
# Markdown the board and the prompt would show as stray characters: [text](url) and
# ![alt](url) keep their text; **x**, __x__, *x* and _x_ lose their markers only when they wrap
# a word (so snake_case_names, "a * b * c" and 2*3*4 survive); a leading "#" before a letter is
# a heading marker ("#42" is not).
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_STRONG = re.compile(r"(?<![\w*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])|(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")
_MD_EM = re.compile(
	r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])|(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])"
)
_MD_HEADING = re.compile(r"^#{1,6}[ \t]*(?=[^\W\d_])")
# Lines that are structure, not prose: table rows, list items, headings, horizontal rules.
_NON_PROSE_LINE = re.compile(r"^\s*(?:\||#{1,6}\s|[-*+]\s|\d+[.)]\s|(?:-{3,}|\*{3,}|_{3,})\s*$)")


_FENCE_MARKERS = ("```", "~~~")


def _strip_markdown(text: str) -> str:
	"""``text`` without link syntax, emphasis markers and a leading heading marker."""
	text = _MD_LINK.sub(r"\1", text)
	for pattern in (_MD_STRONG, _MD_EM):
		text = pattern.sub(lambda m: m.group(1) or m.group(2), text)
	return _MD_HEADING.sub("", text)


def _prose_paragraphs(prose: str) -> list[str]:
	"""Blank-line separated prose paragraphs of ``prose``, whitespace collapsed. Fenced code
	(a ``` or ~~~ fence, closed or not; a one-line ```x``` is inline) and table/list/heading lines are passed over, not stopped at."""
	paras: list[str] = []
	block: list[str] = []

	def flush():
		text = " ".join(" ".join(block).split())
		# Clip to the tail BEFORE the markdown regexes: on a long unbroken paragraph of unmatched
		# markers (" **a" * N, a run of "[") they are quadratic and would stall the turn.
		# ask_lead_in only ever uses the last LEAD_IN_CAP chars of a paragraph, so keep twice
		# that. A clip that lands mid-word is fine: _tail_fit and safe_line cut again later.
		text = _strip_markdown(text[-2 * LEAD_IN_CAP :])
		if text:
			paras.append(text)
		block.clear()

	fence = ""  # the marker that opened the current fenced block, "" outside one
	for line in prose.splitlines():
		stripped = line.lstrip()
		marker = next((m for m in _FENCE_MARKERS if stripped.startswith(m)), "")
		# A line holding two or more markers (```x```) opens and closes in one line: inline,
		# so it never toggles the fence state.
		inline = bool(marker) and stripped.count(marker) >= 2
		if fence:
			if marker == fence and not inline:
				fence = ""
			continue
		if marker:
			flush()
			if not inline:
				fence = marker
		elif not line.strip():
			flush()
		elif not _NON_PROSE_LINE.match(line):
			block.append(line.strip())
	flush()
	return paras


def _tail_fit(text: str, room: int) -> str:
	"""The END of ``text`` in at most ``room`` chars (the part nearest the ask), starting on a
	word boundary and led by an ellipsis."""
	tail = text[len(text) - (room - 1) :]
	if not text[len(text) - room].isspace():
		space = tail.find(" ")
		if 0 <= space <= (room - 1) // 2:
			tail = tail[space + 1 :]
	return "…" + tail.lstrip()


def ask_lead_in(content: str | None) -> str:
	"""The prose just above the LAST ```jarvis-ask``` fence of ``content`` as one safe line of at
	most ~400 chars, or ``""``.

	Walks back from the fence over blank-line paragraphs, passing over tables, lists, headings
	and fences. It takes whole paragraphs nearest-first while they fit; when the nearest is
	under ~120 chars (a generic "a few quick questions" line) it always adds the next
	paragraph, keeping that paragraph's end to fit; a nearest paragraph over the cap is taken
	capped, keeping its end. Original order is kept."""
	content = content or ""
	fences = list(_ASK_RE.finditer(content))
	if fences:
		prose = content[: fences[-1].start()]
	else:
		cut = content.rfind("```jarvis-ask")
		prose = content[:cut] if cut >= 0 else content
	paras = _prose_paragraphs(prose)
	if not paras:
		return ""
	near = paras[-1]
	if len(near) > LEAD_IN_CAP:
		chosen = [_tail_fit(near, LEAD_IN_CAP)]
	else:
		chosen = [near]
		used = len(near)
		short = len(near) < _LEAD_IN_SHORT
		for para in reversed(paras[:-1]):
			room = LEAD_IN_CAP - used - 1
			if len(para) <= room:
				chosen.insert(0, para)
				used += len(para) + 1
			elif short and len(chosen) == 1:
				chosen.insert(0, _tail_fit(para, room))
				break
			else:
				break
	out = safe_line(" ".join(chosen), LEAD_IN_CAP)
	# punctuation or markup alone ("...", "***") names no task
	return out if any(ch.isalnum() for ch in out) else ""


def _log_lead_in_failure(conversation: str, exc: Exception) -> None:
	"""Record a lead-in failure for on-call without its content, and never raise."""
	try:
		get_logger().warning(f"chat ask lead_in failed: {type(exc).__name__}")
	except Exception:
		pass
	try:
		frappe.log_error(
			title="jarvis.chat.ask_lead_in_failed",
			message=(
				f"conversation: {conversation}\nexception: {type(exc).__name__}\n"
				"Traceback (frames only, no exception message):\n"
				+ "".join(traceback.format_tb(exc.__traceback__))
			),
		)
	except Exception:
		pass


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
	conversation_meta = frappe.db.get_value(
		CONV, conversation, ["owner", "origin_page", "file_box"], as_dict=True
	)
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
	# The lead-in is garnish: nothing it raises may cost the customer the ask card. Only
	# Exception is caught, so an rq job timeout (BaseException) still reaches the worker. What
	# is logged is the conversation name, the exception class and the stack frames: never the
	# exception message and never the agent's text.
	lead_in = ""
	if not conversation_meta.file_box:
		try:
			lead_in = ask_lead_in(assistant_content)
		except Exception as exc:
			_log_lead_in_failure(conversation, exc)
	doc = frappe.get_doc(
		{
			"doctype": APPROVAL,
			"title": ask_label(questions[0])[:100],
			"status": "Pending",
			"source": "Chat",
			"conversation": conversation,
			"question": question_text,
			"options": json.dumps(options) if options else "",
			# A File Box ask is routed by its own flow; only a chat ask carries the "why".
			"context_md": lead_in,
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
			"source": "Chat",
			"conversation_id": conversation,
			"origin_page": conversation_meta.origin_page or "",
			"name": doc.name,
			"question": question_text,
		},
	)
	# Last, and guarded: a logging fault must never cost the ask the customer was just sent.
	# Constant metadata only (whether the lead-in was captured, never its text). get_logger()
	# re-pins INFO on every call; frappe.logger defaults to ERROR and would drop this line.
	try:
		state = "skipped" if conversation_meta.file_box else ("yes" if doc.context_md else "no")
		get_logger().info(f"chat ask {doc.name} lead_in={state}")
	except Exception:
		pass
	return doc.name


def resolve_on_user_message(conversation: str, seed_message: str | None = None) -> None:
	"""The user sent a message in ``conversation``: any Pending chat-sourced
	approval there is now answered in chat — flip it to "Answered" so the
	board row never goes stale or double-answered. One indexed UPDATE
	(conversation is search_index'd); decided rows are untouched, so a
	board decide() that resumes the chat never re-flips its own row. A File Box
	sheet's question is answered with its sheet, never here.

	``seed_message`` is the turn's triggering message. When it is a board answer
	(one decided question sent to the chat), another Pending question that has
	its own tagged reviewers stays open for them; the requester's own messages
	and untagged questions settle as before."""
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
	if (
		seed_message
		and frappe.db.get_value("Jarvis Chat Message", seed_message, "origin") == "board_answer"
		and frappe.db.table_exists(GRANT)
	):
		from pypika.terms import ExistsCriterion

		g = frappe.qb.DocType(GRANT)
		where &= ~ExistsCriterion(frappe.qb.from_(g).select(1).where(g.approval == a.name))
	rows = frappe.qb.from_(a).select(a.name).where(where).for_update().run(pluck=True)
	if not rows:
		return
	frappe.db.sql(
		"""update `tabJarvis Approval Request`
		set status='Answered', decision=%(decision)s,
			decided_by=%(user)s, decided_at=%(now)s
		where name in %(names)s
		and status='Pending' and source='Chat'""",
		{
			"decision": _ANSWERED_DECISION,
			"user": frappe.session.user,
			"now": frappe.utils.now_datetime(),
			"names": tuple(rows),
		},
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist before realtime publish
	from jarvis.chat.approval_reviewers import log_bounded, notify_settled

	# The board badge counted it: tell the owner's shell to re-count. Best effort: a
	# failed delivery never skips the reviewers below (the log names the kind only).
	try:
		owner = frappe.db.get_value(CONV, conversation, "owner")
		if owner:
			publish_to_user(owner, {"kind": "action:settled", "conversation_id": conversation})
	except Exception:
		log_bounded("jarvis.approval.notify_failed", "action:settled")
	for name in rows:
		try:
			doc = frappe.get_doc(APPROVAL, name)
		except Exception:
			continue
		notify_settled(doc, include_requester=False)
