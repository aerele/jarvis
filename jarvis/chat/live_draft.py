"""Which draft card is live: the same rule the phone app and the desktop app use.

The rule is written once in ``frontend/src/lib/liveDraft.js`` and proven by the shared case
file ``frontend/src/lib/liveDraftCases.json``; this module is the server's reader of it.

Rows are the conversation's messages in ``seq`` order, ignoring ``hidden`` and ``streaming``
rows. A draft row is an assistant row whose FIRST closed ``jarvis-action`` fence is broken or
parses to a non-email action whose verb is missing, ``create`` or ``update``. The live draft
is the last draft row, unless a later tool row carries a non-empty ``action_outcome`` or more
than ``MAX_USER_ROWS_AFTER_DRAFT`` user rows come after it.

``pick`` is pure (plain dict rows, no Frappe) so it can be evaluated without a site.
"""

from __future__ import annotations

import json
import re

MAX_USER_ROWS_AFTER_DRAFT = 5
# ``find`` reads only the rows newer than the 6th-newest user row (a draft older than that has
# more than MAX_USER_ROWS_AFTER_DRAFT user rows after it, so it is retired). This caps that
# slice; when it is hit the answer is "no draft" (retired is the safe direction).
HARD_LIMIT = 2000

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


def pick(rows: list[dict]) -> dict | None:
	"""The live draft among ``rows`` (messages in ``seq`` order), or None.

	Returns ``{"message": name, "verb": "create"|"update", "doctype": str, "bad": bool}``.
	The newest draft row is chosen first and only then tested for retirement: an older
	draft never comes back."""
	user_rows = 0
	for row in reversed(rows or []):
		if not _is_object(row) or row.get("hidden") or row.get("streaming"):
			continue
		role = row.get("role")
		draft = draft_of(row.get("content")) if role == "assistant" else None
		if draft:
			if user_rows > MAX_USER_ROWS_AFTER_DRAFT:
				return None
			return {"message": row.get("name"), **draft}
		if role == "tool" and str(row.get("action_outcome") or "").strip():
			return None
		if role == "user":
			user_rows += 1
	return None


def _retires(row: dict) -> bool:
	return (
		row["role"] == "tool"
		and not row.get("hidden")
		and not row.get("streaming")
		and bool(str(row.get("action_outcome") or "").strip())
	)


def find(conversation: str) -> dict | None:
	"""The live draft of ``conversation``, or None. Shaped like the rule: only the rows newer
	than the 6th-newest user row can hold it, and only the assistant rows newer than the last
	receipt can be it. Three reads, none in a loop.

	``get_all`` is used because the callers (the dismiss endpoint after its owner check, the
	turn's own prompt assembly) already act on a conversation this user owns."""
	import frappe

	# Cutoff. Index role_conv_seq (role, conversation, seq) serves this ordered read.
	users = frappe.get_all(
		MSG,
		filters={"conversation": conversation, "role": "user", "hidden": 0, "streaming": 0},
		fields=["seq"],
		order_by="seq desc",
		limit=MAX_USER_ROWS_AFTER_DRAFT + 1,
	)
	cutoff = users[MAX_USER_ROWS_AFTER_DRAFT]["seq"] if len(users) > MAX_USER_ROWS_AFTER_DRAFT else None
	filters = {"conversation": conversation}
	if cutoff is not None:
		filters["seq"] = [">", cutoff]
	# Light rows (no content). Index on conversation, then the seq range within it.
	rows = frappe.get_all(
		MSG,
		filters=filters,
		fields=["name", "seq", "role", "action_outcome", "hidden", "streaming"],
		order_by="seq asc",
		limit=HARD_LIMIT + 1,
	)
	if len(rows) > HARD_LIMIT:
		return None
	last_receipt = max((i for i, r in enumerate(rows) if _retires(r)), default=-1)
	names = [r["name"] for r in rows[last_receipt + 1 :] if r["role"] == "assistant"]
	if not names:
		return None
	# Content only for the assistant rows that could be the draft, and only if they mention a
	# block (replies can be long). Primary key.
	blocks = {
		r["name"]: r["content"]
		for r in frappe.get_all(  # same ownership argument as above
			MSG,
			filters={"name": ["in", names], "content": ["like", "%jarvis-action%"]},
			fields=["name", "content"],
		)
	}
	for row in rows:
		row["content"] = blocks.get(row["name"])
	return pick(rows)
