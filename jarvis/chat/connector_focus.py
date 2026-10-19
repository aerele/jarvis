"""Connector focus: the composer pill that binds one chat turn to one connector.

The pill arrives as ``context.focus_connector`` (``{key, label}``, allow-listed
and length-capped in ``jarvis.chat.api.send_message``). The prompt line alone
was not enough (the agent stepped outside the focus for an ERP question), so
the focus is also stored for the running turn and enforced at the plugin's
tool gate (``jarvis.api._dispatch_from_session``): while it is set, only
``list_connector_actions`` and ``call_connector`` for the focused key run.

Storage mirrors ``turn_message_binding``: one Redis key per conversation,
set or deleted at turn start on both dispatch paths (legacy
``handle_chat_send`` and the pump's ``prepare``). Admission keeps one turn in
flight per conversation, so the value is the running turn's, and a turn
without focus deletes it: focus never reaches the next turn. There is no
terminal clear on purpose: a pump settlement effect can run after the next
turn's prepare and would wipe that turn's focus. Between turns only a person's
card confirm runs a tool, and that path is not gated. The TTL drops an
abandoned key. Redis, not a Conversation column: the worker holds its
transaction open for the whole turn, so a DB flag would need an explicit
commit to reach the tool-call request.
"""

from __future__ import annotations

import json
import re

import frappe

from jarvis._responses import err

# Outlives the 720 s whole-turn RQ envelope, like turn_message_binding._TTL_S.
_TTL_S = 900
_PREFIX = "jarvis:connector_focus:"
ALLOWED_TOOLS = frozenset({"list_connector_actions", "call_connector"})
REFUSED_CODE = "ConnectorFocusError"


def focus_of(context) -> dict | None:
	"""The sanitized ``{key, label}`` of the turn's connector focus, or None.
	Both are client text bound for a trusted prompt line, so they are kept to
	one line with no backticks or square brackets."""
	if not isinstance(context, dict):
		return None
	raw = context.get("focus_connector")
	if not isinstance(raw, dict):
		return None
	key, label = one_line(raw.get("key")), one_line(raw.get("label"))
	if not key or not label:
		return None
	return {"key": key, "label": label}


def bind_turn(conversation: str, focus: dict | None) -> None:
	"""Store ``focus`` for the turn now starting, or drop any stale one."""
	if not conversation:
		return
	if focus:
		frappe.cache().set_value(_key(conversation), json.dumps(focus), expires_in_sec=_TTL_S)
	else:
		clear(conversation)


def current(conversation: str | None) -> dict | None:
	"""The focus of the turn running in ``conversation``, or None."""
	if not conversation:
		return None
	raw = frappe.cache().get_value(_key(conversation), expires=True)
	try:
		focus = json.loads(raw) if raw else None
	except (TypeError, ValueError):
		return None
	return focus if isinstance(focus, dict) and focus.get("key") else None


def clear(conversation: str | None) -> None:
	if conversation:
		frappe.cache().delete_value(_key(conversation))


def refusal(conversation: str | None, tool: str, args) -> dict | None:
	"""The error envelope for a tool call the turn's focus forbids, or None.

	No focus means no restriction. Under focus only the two connector tools
	run, and a ``connector`` argument, when given, must name the focused key."""
	focus = current(conversation)
	if not focus or _permitted(tool, args, focus["key"]):
		return None
	frappe.logger("jarvis.connector_focus").info(
		f"refused tool={tool} conversation={conversation} focus={focus['key']}"
	)
	return err(
		REFUSED_CODE,
		f"This chat is focused on {focus.get('label') or focus['key']}. "
		"Clear the focus to use ERP or other tools.",
	)


def _permitted(tool: str, args, key: str) -> bool:
	if tool not in ALLOWED_TOOLS:
		return False
	named = args.get("connector") if isinstance(args, dict) else None
	return not named or str(named).strip() == key


def one_line(value) -> str:
	"""``value`` as one prompt-safe line: no newlines, backticks or brackets."""
	text = re.sub(r"\s+", " ", str(value or "")).strip()
	return text.replace("`", "'").replace("[", "(").replace("]", ")")


def _key(conversation: str) -> str:
	return _PREFIX + conversation
