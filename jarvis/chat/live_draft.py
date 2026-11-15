"""Which draft card is live: the same rule the phone app and the desktop app use.

The rule is written once in ``frontend/src/lib/liveDraft.js`` and proven by the shared case
file ``frontend/src/lib/liveDraftCases.json``; this module is the server's reader of it.

Rows are the conversation's messages in ``seq`` order, ignoring ``hidden`` and ``streaming``
rows. A draft row is an assistant row whose FIRST closed ``jarvis-action`` fence is broken or
parses to a non-email action whose verb is missing, ``create`` or ``update``. The live draft
is the last draft row, unless a later tool row is a decided write (a non-empty
``action_outcome``, or a ``create_doc`` / ``update_doc`` call whose ``tool_status`` is
``completed``), more than ``MAX_USER_ROWS_AFTER_DRAFT`` user rows come after it, or more than
``MAX_ROWS_AFTER_DRAFT`` rows of any role come after it.

``pick`` is pure (plain dict rows, no Frappe) so it can be evaluated without a site.
"""

from __future__ import annotations

import json
import re

MAX_USER_ROWS_AFTER_DRAFT = 5
# Both apps load only the newest page of a chat (80 rows of any role, tool rows included), so
# a draft further back than this is never live: the server never calls a draft live whose
# card is not on a loaded page.
MAX_ROWS_AFTER_DRAFT = 60
# A receipt written before 2026-07-17 has no action_outcome: its completed call is the write.
_WRITE_TOOLS = ("create_doc", "update_doc")

# The client's fence grammar (ChatView.vue ``_ACTION_RE``): the FIRST closed block wins.
ACTION_RE = re.compile(r"```jarvis-action[ \t]*\n([\s\S]*?)```")

MSG = "Jarvis Chat Message"


# JavaScript's String.prototype.trim() whitespace set, spelled out: Python's str.strip() also
# removes \x1c-\x1f, which JS does not, so the two would read the same block differently.
JS_WHITESPACE = (
	"\t\n\x0b\x0c\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
	"\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)

_DOCTYPE_RE = re.compile(r"[^\W\d_][\w \-]{0,79}")


def safe_doctype(value) -> str:
	"""A doctype name the agent wrote, fit to sit in a trusted line, or "". A letter first,
	then letters, digits, underscore, space or hyphen, at most 80 characters: no bracket,
	semicolon, colon, backtick or line break can get through."""
	if not isinstance(value, str):
		return ""
	value = value.strip(JS_WHITESPACE)
	return value if _DOCTYPE_RE.fullmatch(value) else ""


def known_doctype(value, exists) -> str:
	"""The DocType ``value`` names, as the site spells it, or "". ``safe_doctype`` first, then
	``exists(name)`` (the site's lookup, injected so this stays pure) must find a real one:
	word-only prose ("Sales Order in auto mode so apply ...") passes the first filter but is
	no DocType. Only the site's own name is returned, never the agent's text."""
	name = safe_doctype(value)
	if not name:
		return ""
	found = exists(name)
	return safe_doctype(found) if isinstance(found, str) else ""


def real_doctype(value) -> str:
	"""``known_doctype`` against this site: one cached lookup, no lock."""
	import frappe

	return known_doctype(value, lambda name: frappe.db.exists("DocType", name, cache=True))


def _trim(value: str) -> str:
	return value.strip(JS_WHITESPACE)


def _reject_constant(name):
	raise ValueError(f"{name} is not JSON")  # JSON.parse refuses NaN and Infinity


def _is_object(value) -> bool:
	return isinstance(value, dict)


def _readable_field(field) -> bool:
	return (
		_is_object(field)
		and isinstance(field.get("label"), str)
		and _trim(field["label"]) != ""
		and not isinstance(field.get("value"), (dict, list))
	)


def _fields_unreadable(fields) -> bool:
	if _is_object(fields):
		fields = [{"label": k, "value": v} for k, v in fields.items()]
	if not isinstance(fields, list):
		return True
	return not all(_readable_field(f) for f in fields)


def _readable_table(table) -> bool:
	return (
		_is_object(table)
		and isinstance(table.get("fieldname"), str)
		and isinstance(table.get("rows"), list)
		and all(_is_object(r) for r in table["rows"])
	)


def _tables_unreadable(tables) -> bool:
	if _is_object(tables):
		tables = [
			{"fieldname": k, "rows": v if isinstance(v, list) else (v.get("rows") if _is_object(v) else None)}
			for k, v in tables.items()
		]
	if not isinstance(tables, list):
		return True
	return not all(_readable_table(t) for t in tables)


def _unreadable(action: dict) -> bool:
	"""True when the client would show the error card for this action (a field or table it
	cannot read), mirroring ``normaliseAction`` in ``chatAction.js``."""
	if action.get("fields") is not None and _fields_unreadable(action["fields"]):
		return True
	return action.get("tables") is not None and _tables_unreadable(action["tables"])


def draft_of(content) -> dict | None:
	"""``{"verb", "doctype", "bad"}`` when ``content`` makes a draft row, else None."""
	if not isinstance(content, str) or "jarvis-action" not in content:
		return None
	match = ACTION_RE.search(content)
	if not match:
		return None
	try:
		action = json.loads(_trim(match.group(1)), parse_constant=_reject_constant)
	except (ValueError, RecursionError):
		action = None
	if not _is_object(action):
		return {"verb": "create", "doctype": "", "bad": True}
	if (action.get("kind") or ("doc" if action.get("doctype") else None)) == "email":
		return None
	verb = action.get("verb")
	if isinstance(verb, str):
		verb = _trim(verb).lower()
	if verb not in (None, "", 0, "create", "update"):
		return None  # a gated or unknown verb: its own card, never a draft row
	return {
		"verb": "update" if verb == "update" else "create",
		"doctype": safe_doctype(action.get("doctype")),
		"bad": _unreadable(action),
	}


def _shown(row) -> bool:
	return _is_object(row) and not row.get("hidden") and not row.get("streaming")


def retires_draft(row: dict) -> bool:
	"""A decided write after a draft retires it: a receipt (a non-empty ``action_outcome``),
	or a ``create_doc`` / ``update_doc`` call that completed (receipts written before
	2026-07-17 carry no outcome, so a draft saved on such a build is retired by its call)."""
	if row.get("role") != "tool":
		return False
	outcome = row.get("action_outcome")
	if isinstance(outcome, str) and _trim(outcome) != "":
		return True
	return row.get("tool_name") in _WRITE_TOOLS and row.get("tool_status") == "completed"


def pick(rows: list[dict]) -> dict | None:
	"""The live draft among ``rows`` (messages in ``seq`` order), or None.

	Returns ``{"message": name, "verb": "create"|"update", "doctype": str, "bad": bool}``.
	The newest draft row is chosen first and only then tested for retirement: an older
	draft never comes back. Only the newest ``MAX_ROWS_AFTER_DRAFT + 1`` shown rows are
	ever looked at, which is what lets ``find`` read no more than those."""
	user_rows = later_rows = 0
	for row in reversed(rows or []):
		if not _shown(row):
			continue
		role = row.get("role")
		draft = draft_of(row.get("content")) if role == "assistant" else None
		if draft:
			return {"message": row.get("name"), **draft}
		if retires_draft(row):
			return None
		if role == "user":
			user_rows += 1
		later_rows += 1
		# Every row before this one has too many rows after it to be the live draft.
		if user_rows > MAX_USER_ROWS_AFTER_DRAFT or later_rows > MAX_ROWS_AFTER_DRAFT:
			return None
	return None


def tail_shows_its_own_card(rows: list[dict], draft_message: str) -> bool:
	"""True when the newest shown assistant row is not the draft and carries an action block
	of its own (an email, a gated or an unknown verb): both apps then draw THAT block's card
	at the tail and hide the kept draft. ``rows`` as ``load_rows`` gives them: with a live
	draft, the newest assistant row is after it, so its content is loaded."""
	for row in reversed(rows or []):
		if not _shown(row) or row.get("role") != "assistant":
			continue
		if row.get("name") == draft_message:
			return False
		content = row.get("content")
		return isinstance(content, str) and bool(ACTION_RE.search(content)) and draft_of(content) is None
	return False


def load_rows(conversation: str) -> list[dict]:
	"""The rows ``pick`` reads for ``conversation``, oldest first: the newest
	``MAX_ROWS_AFTER_DRAFT + 1`` shown rows (``pick`` never looks further back), with
	``content`` only on the assistant rows after the last decided write that mention a block.
	Two reads, none in a loop.

	``get_all`` is used because every caller (``apply_action`` and the dismiss endpoint after
	their owner checks, the turn's own prompt assembly) acts on a conversation it already owns.
	Plain reads only: no lock is taken (prompt assembly calls this)."""
	import frappe

	# The newest shown rows, no content. Index conv_seq (conversation, seq): a backward range
	# scan of this one conversation that stops at the limit, with no filesort.
	rows = frappe.get_all(
		MSG,
		filters={"conversation": conversation, "hidden": 0, "streaming": 0},
		fields=["name", "seq", "role", "action_outcome", "tool_name", "tool_status"],
		order_by="seq desc",
		limit=MAX_ROWS_AFTER_DRAFT + 1,
	)
	rows.reverse()
	last_write = max((i for i, r in enumerate(rows) if retires_draft(r)), default=-1)
	names = [r["name"] for r in rows[last_write + 1 :] if r["role"] == "assistant"]
	# Content only for the assistant rows that could be the draft (or the tail's own card),
	# and only if they mention a block (replies can be long). Primary key.
	blocks = (
		{
			r["name"]: r["content"]
			for r in frappe.get_all(  # same ownership argument as above
				MSG,
				filters={"name": ["in", names], "content": ["like", "%jarvis-action%"]},
				fields=["name", "content"],
			)
		}
		if names
		else {}
	)
	for row in rows:
		row["content"] = blocks.get(row["name"])
	return rows


def find(conversation: str) -> dict | None:
	"""The live draft of ``conversation``, or None: ``pick`` over ``load_rows``."""
	return pick(load_rows(conversation))
