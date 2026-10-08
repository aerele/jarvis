"""The pump sends a turn again ONCE when the agent runtime ends it with its plain empty
reply ("Agent couldn't generate a response. Please try again.") and nothing ran.

Off unless site config ``jarvis_empty_reply_resend`` is an on value; while off,
``resend_decision`` returns None and writes nothing. It reads the terminal first and
the database only for a plain empty reply. The breaker reads the settled Turn rows:
``BREAKER_FAILURES`` turns of one relay target sent again that got a second empty
reply in ``BREAKER_WINDOW_S`` stop the re-send there. docs/chat-errors.md lists the
log lines."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import NamedTuple

import frappe

from jarvis.chat import turn_state, txn
from jarvis.chat.error_taxonomy import classify_error_text

TURN = "Jarvis Chat Turn"
MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"

# ``redispatch_reason`` of this re-send in the stored dispatch payload.
REASON = "empty_reply"

# The runtime's empty-reply text alone, narrower than the UI rule on purpose: text around
# it (``other``) is a drift signal and is never sent again. Group 1 is the "Please try
# again." of the plain form.
EMPTY_REPLY_RE = re.compile(
	r"^\W*agent couldn.?t generate a response\.?(?:\s*(please try again\.?))?\s*$", re.I
)
# ``variant`` reads this many characters; a re-send needs the whole text within it.
MAX_CHARS = 300
EMPTY_REPLY_CODES = ("empty-reply", "empty-reply-tools")

SWITCH = "jarvis_empty_reply_resend"
_ON_VALUES = frozenset({"1", "true", "on", "yes"})

# The only ``origin`` of a message a person typed in a chat (api.send_message).
_HUMAN_ORIGINS = ("human",)

BREAKER_FAILURES = 3
BREAKER_WINDOW_S = 600
BREAKER_ALERT = "jarvis.empty_reply.breaker_open"
# The chat rule looks at the turn before this one only when it is this recent.
CHAT_WINDOW_S = 24 * 3600


class Decision(NamedTuple):
	resend: bool
	reason: str


def variant(err_text, code: str | None = None) -> str:
	"""``tools`` for the empty-reply-tools code; else ``plain`` (the sentence and "Please
	try again."), ``bare`` (the sentence alone) or ``other`` (more text around it). Reads
	the first ``MAX_CHARS`` characters only (B1's empty_reply line uses it)."""
	if code == "empty-reply-tools":
		return "tools"
	match = EMPTY_REPLY_RE.match((err_text or "")[:MAX_CHARS])
	if not match:
		return "other"
	return "plain" if match.group(1) else "bare"


def switch_on() -> bool:
	"""Off unless site config sets an on value (opt-in)."""
	flag = frappe.conf.get(SWITCH)
	return flag is not None and str(flag).strip().lower() in _ON_VALUES


def switch_state() -> str:
	"""``on`` or ``off``, for the runbook probe (``bench execute``)."""
	return "on" if switch_on() else "off"


def stored_dispatch(raw) -> dict | None:
	"""A Turn's stored dispatch payload, or None when it is missing or not a JSON object."""
	if not raw:
		return None
	try:
		parsed = json.loads(raw)
	except Exception:
		return None
	return parsed if isinstance(parsed, dict) else None


def resent_turn(run_id: str, conversation: str | None = None) -> bool:
	"""The Turn ``run_id`` (of ``conversation``, when given) was sent again for an empty reply.
	Reads the marker key only, not the whole payload."""
	rows = frappe.db.sql(
		"""SELECT JSON_VALUE(dispatch_payload, '$.redispatch_reason') FROM `tabJarvis Chat Turn`
		WHERE name=%(r)s AND (%(c)s IS NULL OR conversation=%(c)s)""",
		{"r": run_id, "c": conversation or None},
	)
	return bool(rows) and rows[0][0] == REASON


def resend_decision(
	*,
	kind: str,
	payload,
	run_id: str,
	conversation: str,
	owner: str | None,
	assistant_message: str | None,
	relay_target_id: str,
	sent_again: bool,
	saw_step: Callable[[], bool],
) -> Decision | None:
	"""None when the switch is off or the terminal is not an empty reply; else a
	``Decision``. ``sent_again``: this run is already the second attempt. ``saw_step()``:
	the attempt showed a step line. Before the first database read it ends the pump's
	transaction (``txn.fresh_snapshot``), so a tool receipt committed since the slice
	began is seen. The pump adds ``used`` (a stored marker), ``no_payload``, ``cas_lost``
	and ``error``."""
	if not isinstance(payload, dict) or kind != "relay:error" or payload.get("state") != "error":
		return None
	if (payload.get("text") or "").strip() or not switch_on():
		return None
	err = payload.get("error") or ""
	code = classify_error_text(err)
	if code not in EMPTY_REPLY_CODES:
		return None
	if len(err) > MAX_CHARS or variant(err, code) != "plain":
		return Decision(False, "variant")
	if sent_again:
		return Decision(False, "used")
	if saw_step():
		return Decision(False, "steps")
	txn.fresh_snapshot()
	turn = frappe.db.get_value(TURN, run_id, ["turn_class", "seed_message", "creation"], as_dict=True) or {}
	if turn.get("turn_class") != "interactive":
		return Decision(False, "class")
	seed = turn.get("seed_message")
	if not seed or frappe.db.get_value(MSG, seed, "origin") not in _HUMAN_ORIGINS:
		return Decision(False, "origin")
	if reason := _chat_mode(conversation):
		return Decision(False, reason)
	if _breaker_open(relay_target_id):
		return Decision(False, "breaker")
	if tool := _tool_after(conversation, assistant_message):
		return Decision(False, f"tool:{_token(tool)}")
	from jarvis.chat import pending_confirm

	if pending_confirm.has_live_card(owner, conversation):
		return Decision(False, "card")
	if _retried(conversation, run_id, seed):
		return Decision(False, "retried")
	if _chat_failing(conversation, run_id, turn.get("creation")):
		return Decision(False, "chat")
	return Decision(True, "plain")


def _chat_mode(conversation: str) -> str | None:
	"""``armed`` (an armed chat or a live macro run) or ``file_box`` (a File Box chat)."""
	conv = frappe.db.get_value(CONV, conversation, ["skip_confirmation", "file_box"], as_dict=True) or {}
	if conv.get("skip_confirmation"):
		return "armed"
	if conv.get("file_box"):
		return "file_box"
	from jarvis.chat.macros import _LIVE_RUN_STATUSES

	# A queued run has not sent its first step yet; it still owns the chat.
	live = ("queued", *_LIVE_RUN_STATUSES)
	if frappe.db.exists("Jarvis Macro Run", {"conversation": conversation, "status": ["in", live]}):
		return "armed"
	return None


def _breaker_open(target: str) -> bool:
	"""``BREAKER_FAILURES`` turns of this relay target sent again got a second empty reply
	in the last ``BREAKER_WINDOW_S`` (settled Turn rows, so a replay never counts twice).
	A read error counts as open. Each refusal writes a WARNING."""
	since = frappe.utils.add_to_date(None, seconds=-BREAKER_WINDOW_S)
	try:
		count = _breaker_count(target, since)
	except Exception:
		_log("warning", "empty_reply breaker=unreadable target=%s", target, exc_info=True)
		return True
	if count < BREAKER_FAILURES:
		return False
	_log("warning", "empty_reply breaker=open target=%s count=%s", target, count)
	_alert_breaker(target, count, since)
	return True


def _breaker_count(target: str, since) -> int:
	"""Reads the payloads only when enough rows of the window have an empty-reply error;
	a count below ``BREAKER_FAILURES`` can include turns not sent again."""
	rows = frappe.db.sql(
		"""SELECT name, error FROM `tabJarvis Chat Turn`
		WHERE state='errored' AND relay_target_id=%(t)s AND finalizing_at > %(since)s""",
		{"t": target, "since": since},
	)
	names = [name for name, error in rows if classify_error_text(error) in EMPTY_REPLY_CODES]
	if len(names) < BREAKER_FAILURES:
		return len(names)
	return frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabJarvis Chat Turn`
		WHERE name IN %(names)s AND JSON_VALUE(dispatch_payload, '$.redispatch_reason')=%(reason)s""",
		{"names": tuple(names), "reason": REASON},
	)[0][0]


def _alert_breaker(target: str, count: int, since) -> None:
	"""At most one Error Log per relay target per ``BREAKER_WINDOW_S``. Never raises."""
	try:
		if not frappe.db.exists(
			"Error Log", {"method": BREAKER_ALERT, "reference_name": target, "creation": [">", since]}
		):
			frappe.log_error(
				title=BREAKER_ALERT,
				message=f"{count} turns sent again got a second empty reply in {BREAKER_WINDOW_S // 60} "
				"minutes: the re-send is off on this relay target until they are older.",
				reference_doctype="Jarvis Relay Pump",
				reference_name=target,
			)
	except Exception:
		_log("warning", "empty_reply breaker_alert_failed target=%s", target, exc_info=True)


def log_decision(decision: str, reason: str, *, run_id: str, conversation: str, target: str) -> None:
	"""``empty_reply decision=resent|skip reason=.. run_id=.. conversation=.. target=..``.
	``reason="error"`` is a WARNING with the traceback (call it inside the ``except``)."""
	_log(
		"warning" if reason == "error" else "info",
		"empty_reply decision=%s reason=%s run_id=%s conversation=%s target=%s",
		decision,
		reason,
		run_id,
		conversation,
		target,
		exc_info=reason == "error",
	)


def _log(level: str, fmt: str, *args, exc_info: bool = False) -> None:
	"""One line in the latency log. Never raises."""
	try:
		from jarvis.chat.latency import get_logger

		getattr(get_logger(), level)(fmt, *args, exc_info=exc_info)
	except Exception:
		pass


def _token(name: str) -> str:
	"""A tool name the agent runtime chose, safe in one log line."""
	return re.sub(r"[^\w.:-]", "_", name)[:64]


def _tool_after(conversation: str, assistant_message: str | None) -> str | None:
	"""A tool row written after this turn's reply placeholder, except the memory hook's
	recall (written with no call id). No placeholder: every tool row counts."""
	seq = frappe.db.get_value(MSG, assistant_message, "seq") if assistant_message else None
	rows = frappe.db.sql(
		"""SELECT IFNULL(tool_name, '') FROM `tabJarvis Chat Message`
		WHERE conversation=%(c)s AND role='tool' AND seq > %(s)s
		  AND NOT (IFNULL(tool_name, '')='recall' AND tool_call_id IS NULL)
		LIMIT 1""",
		{"c": conversation, "s": seq or 0},
	)
	return (rows[0][0] or "unknown") if rows else None


def _retried(conversation: str, run_id: str, seed: str) -> bool:
	"""Another attempt at this user message has not finished, or one ended with the
	plain empty reply (once per user message)."""
	rows = frappe.db.sql(
		"""SELECT state, error FROM `tabJarvis Chat Turn`
		WHERE conversation=%(c)s AND seed_message=%(s)s AND name != %(r)s""",
		{"c": conversation, "s": seed, "r": run_id},
	)
	return any(
		state in turn_state.NONTERMINAL_STATES or (state == "errored" and variant(error) == "plain")
		for state, error in rows
	)


def _chat_failing(conversation: str, run_id: str, created) -> bool:
	"""The last finished turn before this one in the chat, in the last ``CHAT_WINDOW_S``,
	ended with an empty reply: the chat fails every time, so each message is not sent twice."""
	since = frappe.utils.add_to_date(None, seconds=-CHAT_WINDOW_S)
	rows = frappe.db.sql(
		"""SELECT error FROM `tabJarvis Chat Turn`
		WHERE conversation=%(c)s AND name != %(r)s AND creation < %(at)s AND creation > %(since)s
		  AND state IN ('done', 'errored', 'cancelled')
		ORDER BY creation DESC LIMIT 1""",
		{"c": conversation, "r": run_id, "at": created, "since": since},
	)
	return bool(rows) and classify_error_text(rows[0][0]) in EMPTY_REPLY_CODES


def note_outcome(
	*, run_id: str, conversation: str, state: str | None, relay_target_id: str, code: str = ""
) -> None:
	"""``empty_reply outcome=ok|failed|stopped code=.. run_id=.. conversation=.. target=..``
	for a turn sent again for an empty reply, once settlement settled it (``finalizing``,
	``errored`` or ``cancelled``). ``code`` is the error code the user saw (``failed``
	only). A replayed finalize can write the line again. Never raises."""
	outcome = {"errored": "failed", "cancelled": "stopped"}.get(state, "ok")
	_log(
		"info",
		"empty_reply outcome=%s code=%s run_id=%s conversation=%s target=%s",
		outcome,
		code if outcome == "failed" else "",
		run_id,
		conversation,
		relay_target_id,
	)
