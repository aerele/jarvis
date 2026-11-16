"""Which draft card is live: the same rule the phone app and the desktop app use.

The rule is written once in ``frontend/src/lib/liveDraft.js`` and proven by the shared case
file ``frontend/src/lib/liveDraftCases.json``; this module is the server's reader of it.

Rows are the conversation's messages in ``seq`` order, ignoring ``hidden`` and ``streaming``
rows. A draft row is an assistant row whose FIRST closed ``jarvis-action`` fence is broken or
parses to a non-email action whose verb is missing, ``create`` or ``update``. The live draft
is the last draft row, unless a later tool row is a decided write (a non-empty
``action_outcome``, or an old receipt: a ``create_doc`` / ``update_doc`` row whose
``tool_status`` is ``completed`` and whose ``tool_call_id`` is empty), a later tool row that is
a parked tool draft (``draft_card`` true: the agent's own ``create_doc`` / ``update_doc`` card,
Pending), more than
``MAX_USER_ROWS_AFTER_DRAFT`` user rows come after it, or more than ``MAX_ROWS_AFTER_DRAFT``
rows of any role come after it. The tool rows DIRECTLY after the draft row (up to the first
later row that is not a tool row) are the draft's own turn: the reply row takes its ``seq``
when the turn starts, so they do not count toward ``MAX_ROWS_AFTER_DRAFT``, though a decided
write among them still retires it.

``pick`` is pure (plain dict rows, no Frappe) so it can be evaluated without a site.
"""

from __future__ import annotations

import json
import re

MAX_USER_ROWS_AFTER_DRAFT = 5
# Both apps load only the newest page of a chat (80 rows of any role, tool rows included,
# widened back to the start of its oldest turn), so a draft with more later rows than this
# (its own turn's tool rows aside) is never live: its card is not missing from a loaded page
# while the server calls it live.
MAX_ROWS_AFTER_DRAFT = 60
# Pending cards read before the strict parse (a draft parks one card; a few more cover a batch).
PARKED_CARDS_READ = 20
# A receipt written before 2026-07-17 (``actions_api._append_receipt``) has no action_outcome
# and no tool_call_id: its completed create / update row is the write.
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
	"""A parked tool draft (``draft_card`` true) or a decided write after a typed draft retires
	it: the tool card is then the one card, and once it is decided its row has an
	``action_outcome`` and no ``draft_card``, so the old typed draft never comes back. A
	decided write is a receipt (a non-empty ``action_outcome``), or an old receipt. A draft saved before 2026-07-17 left a completed ``create_doc`` /
	``update_doc`` row with no outcome AND no ``tool_call_id``. An agent's tool call always
	carries its call id, and a call that only staged a gated card is still ``completed``
	(``api._call_tool`` records it): it never retires a draft this way, only through an
	outcome once the card is decided."""
	if row.get("role") != "tool":
		return False
	if row.get("draft_card") is True:
		return True
	outcome = row.get("action_outcome")
	if isinstance(outcome, str) and _trim(outcome) != "":
		return True
	return (
		row.get("tool_name") in _WRITE_TOOLS
		and row.get("tool_status") == "completed"
		and not row.get("tool_call_id")
	)


def pick(rows: list[dict], parked_draft: bool = False) -> dict | None:
	"""The live draft among ``rows`` (messages in ``seq`` order), or None.

	``parked_draft`` means the conversation has a Pending draft card (read from the Pending
	Actions, whether or not its tool row is among ``rows``): exactly one card is shown, so no
	typed draft is live then, wherever it sits in the row order.

	Returns ``{"message": name, "verb": "create"|"update", "doctype": str, "bad": bool}``.
	The newest draft row is chosen first and only then tested for retirement: an older
	draft never comes back. ``counted`` holds the rows that count toward
	``MAX_ROWS_AFTER_DRAFT``; ``tool_run`` the tool rows walked since the last row that was
	not one, which are the draft's own turn if the draft comes next."""
	if parked_draft:
		return None
	user_rows = counted = tool_run = 0
	for row in reversed(rows or []):
		if not _shown(row):
			continue
		role = row.get("role")
		draft = draft_of(row.get("content")) if role == "assistant" else None
		if draft:
			return {"message": row.get("name"), **draft}
		if retires_draft(row):
			return None
		if role == "tool":
			tool_run += 1
			continue
		counted += tool_run + 1  # a row that is not a tool row: the run after it counts
		tool_run = 0
		if role == "user":
			user_rows += 1
		# Every row before this one has too many rows after it to be the live draft.
		if user_rows > MAX_USER_ROWS_AFTER_DRAFT or counted > MAX_ROWS_AFTER_DRAFT:
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


_LIGHT = ["name", "seq", "role", "action_outcome", "tool_name", "tool_status", "tool_call_id"]


def load_rows(conversation: str) -> list[dict]:
	"""A subset of ``conversation``'s shown rows, oldest first, on which ``pick`` (and
	``tail_shows_its_own_card``) give the same answer as on all of them. Four reads, none
	in a loop, every one bounded:

	1. the newest ``MAX_ROWS_AFTER_DRAFT + 1`` shown rows that are not tool rows. Each such
	   row after a draft counts, so the live draft, if any, is among them;
	2. the content of those assistant rows that mention a block (primary key). The newest
	   draft row among them is the only one ``pick`` can return: the candidate;
	3. the tool rows after the first row that follows the candidate, oldest first, at most
	   one more than the bound leaves room for (they count: one more means ``pick`` stops at
	   the bound, and these rows are enough for it to);
	4. the tool rows of the candidate's own turn (directly after it, before that first row)
	   that could be a decided write: a set outcome, or a create / update call. Own-turn rows
	   do not count, so the rest of them change nothing and are not read.

	``get_all`` is used because every caller (``apply_action`` and the dismiss endpoint after
	their owner checks, the turn's own prompt assembly) acts on a conversation it already owns.
	The tool rows then get ``draft_card`` from one more read (``_stamp_draft_cards``), made only
	when one of them is Pending. Plain reads only: no lock is taken (prompt assembly calls this)."""
	import frappe

	shown = [["conversation", "=", conversation], ["hidden", "=", 0], ["streaming", "=", 0]]
	# 1. Index conv_seq (conversation, seq): a backward scan of this chat, stopping at the limit.
	rows = frappe.get_all(
		MSG,
		filters=[*shown, ["role", "!=", "tool"]],
		fields=_LIGHT,
		order_by="seq desc",
		limit=MAX_ROWS_AFTER_DRAFT + 1,
	)
	rows.reverse()
	names = [r["name"] for r in rows if r["role"] == "assistant"]
	# 2. Only if they mention a block (replies can be long).
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
	at = max(
		(i for i, r in enumerate(rows) if r["role"] == "assistant" and draft_of(r["content"])), default=-1
	)
	if at < 0:
		return rows
	draft, later = rows[at], rows[at + 1 :]
	tools = []
	if later:
		# 3. Index conv_seq: a forward range from that row, stopping at the limit.
		tools += frappe.get_all(  # same ownership argument as above
			MSG,
			filters=[*shown, ["role", "=", "tool"], ["seq", ">", later[0]["seq"]]],
			fields=_LIGHT,
			order_by="seq asc",
			limit=MAX_ROWS_AFTER_DRAFT - len(later) + 1,
		)
	# 4. Index conv_seq: the draft's own turn only. A superset of ``retires_draft`` (an outcome
	# that is set, any create / update call), decided in Python.
	own = [*shown, ["role", "=", "tool"], ["seq", ">", draft["seq"]]]
	if later:
		own.append(["seq", "<", later[0]["seq"]])
	tools += frappe.get_all(  # same ownership argument as above
		MSG,
		filters=own,
		or_filters=[["action_outcome", "is", "set"], ["tool_name", "in", list(_WRITE_TOOLS)]],
		fields=_LIGHT,
		order_by="seq asc",
	)
	for row in tools:
		row["content"] = None
	_stamp_draft_cards(conversation, tools)
	return sorted(rows + tools, key=lambda r: r["seq"])


def _stamp_draft_cards(conversation: str, tools: list[dict]) -> None:
	"""``draft_card`` on each tool row, as ``api._stamp_draft_cards`` gives it to the apps: true
	only for a row still Pending whose Pending Action card carries a ``draft``. The card lives on
	the ``Jarvis Pending Action`` row (``api._pending_action_cards`` hands it to the app), so one
	read of this chat's Pending chat cards among the rows' call ids supplies it: no query per row,
	none at all when no row is Pending. Plain read, no lock."""
	import frappe

	from jarvis.chat import draft_card

	tokens = [t["tool_call_id"] for t in tools if t.get("tool_status") == "pending" and t.get("tool_call_id")]
	cards = {}
	if tokens:
		cards = {
			r["name"]: r["card"]
			for r in frappe.get_all(  # same ownership argument as above; the card is read-only here
				"Jarvis Pending Action",
				filters={
					"name": ["in", tokens],
					"kind": "chat",
					"conversation": conversation,
					"status": "Pending",
				},
				fields=["name", "card"],
			)
		}
	for row in tools:
		row["pending_card"] = _card_of(cards.get(row.get("tool_call_id")))
		row["draft_card"] = draft_card.is_pending_draft_row(row)


def pending_draft_cards(*, conversations=None, owner: str | None = None, limit: int = 20) -> list[dict]:
	"""The Pending draft cards (the agent's own ``create_doc`` / ``update_doc`` call parked as a
	draft) of chat conversations, newest first: ``[{"token", "conversation", "created", "verb",
	"doctype", "title"}]``. ``conversations`` (a list) and ``owner`` narrow the read; give at least one.

	``verb`` is ``create`` or ``update``, ``doctype`` has passed ``real_doctype`` ("" when the
	agent named no real DocType) and ``title`` is built from those by ``draft_card.title_of``,
	never taken from the stored text. One plain read, no lock: prompt assembly, the board and the
	export all call this.

	The server's view can differ from the app's for one fenced-off card: the apps redact a card
	``pending_action_is_safe_to_render`` refuses, and that check locks, so it is not made here
	(accepted: such a card is already dead and settles on its own).

	``get_all``: every caller acts for the owner of these conversations (the turn's own prompt, a
	board or export of the caller's own chats, a macro run for its owner), and the Pending Action
	DocType grants no user read at all."""
	import frappe

	from jarvis.chat import draft_card

	# The ``like`` keeps the limit counting draft cards only (a newer plain pending card must not
	# hide one). It matches the canonical text of a top-level dict value (``_seal.json_text`` writes
	# the column: sorted keys, no spaces), so the string value "Draft" (LIKE ignores case) does not
	# match; the parse below is the real test, the text match only narrows the read.
	filters = {"kind": "chat", "status": "Pending", "card": ["like", '%"draft":{%']}
	if conversations is not None:
		if not conversations:
			return []
		filters["conversation"] = ["in", list(conversations)]
	if owner:
		filters["owner_user"] = owner
	rows = frappe.get_all(
		"Jarvis Pending Action",
		filters=filters,
		fields=["name", "conversation", "card", "creation"],
		order_by="creation desc",
		limit=limit,
	)
	out = []
	for row in rows:
		card = _card_of(row.get("card"))
		draft = card.get("draft") if card else None
		if not isinstance(draft, dict) or draft.get("verb") not in ("create", "update"):
			continue
		doctype = real_doctype(draft.get("doctype"))
		name = draft.get("name")
		out.append(
			{
				"token": row["name"],
				"conversation": row["conversation"],
				"created": str(row.get("creation") or ""),
				"verb": draft["verb"],
				"doctype": doctype,
				"title": draft_card.title_of(
					draft["verb"], doctype, name[:80] if isinstance(name, str) else ""
				)
				if doctype
				else "",
			}
		)
	return out


def _card_of(raw) -> dict | None:
	"""A stored card (JSON text, or already a dict) as a dict, or None."""
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			return None
	return raw if isinstance(raw, dict) else None


def find(conversation: str) -> dict | None:
	"""The live draft of ``conversation``, or None: ``pick`` over ``load_rows``, and None
	whenever a Pending draft card waits (one more plain read, whatever rows were loaded)."""
	return pick(load_rows(conversation), parked_draft=has_pending_draft(conversation))


def has_pending_draft(conversation: str) -> bool:
	"""Whether ``conversation`` has a Pending draft card: ``pending_draft_cards`` for it alone. It
	reads up to ``PARKED_CARDS_READ`` rows and any one that passes the strict parse counts: a row
	the text filter let through but the parse refused must not hide a real draft card."""
	return bool(pending_draft_cards(conversations=[conversation], limit=PARKED_CARDS_READ))
