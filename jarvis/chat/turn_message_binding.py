"""Turn -> triggering-message binding (skill "Approve & run", design §3.3).

The park path of the write-confirmation gate (`_run_tool`) carries only the
`conversation` - it has no pointer to the user message that triggered the
currently-running turn (`run_id=""` at park). A later offer-gate must derive the
invoked skill from *that exact message*; "the latest ``hidden=0`` message" is
racy (a second browser tab commits a queued send row before admission, which a
table query would wrongly pick up) and mis-scoped (macro steps are ``hidden=0``).

This module owns the fix: at TURN START (`turn_handler.handle_chat_send`, before
the agent is dispatched and before any tool call can park) we bind the running
turn's triggering ``message_id`` under its ``conversation``. Because admission +
the pump enforce **one in-flight turn per conversation** (a concurrent human send
is QUEUED, its turn not started), a conversation-keyed binding written at turn
start reflects the turn that is *actually* running - immune to the second tab's
committed-but-queued message row. The offer-gate reads *that* message.

Storage: ``frappe.cache()`` (Redis), one key per conversation, mirroring the
per-target ephemeral-key idiom already used across ``jarvis/chat`` (e.g. the
pump's lease-mirror keys). Portability floor matches ``pending_confirm``: stays
compatible down to Frappe 15 / Redis 6.0 - only ``set_value``/``get_value`` with
``expires_in_sec``/``expires=True`` are used, no >=6.2-only command.

Scope note: this task is JUST the bind + reader + the ``handle_chat_send`` call
site. Staleness is bounded now by the per-turn overwrite + the TTL; task #41 adds
the explicit terminal clears (finalize / recovery / legacy advance chokepoints).
"""

from __future__ import annotations

import frappe

# TTL bounds the stale window if a terminal clear never runs (worker death, an
# abandoned turn). It must comfortably exceed the max duration of a single turn
# so a live turn's binding never expires mid-run: the whole-turn RQ envelope is
# 720s (``_AGENT_TURN_WORKER_TIMEOUT``, chat/api.py). 900s (matching
# ``pending_confirm``'s token TTL) clears that with headroom. Correctness does
# NOT rest on this value - a new turn overwrites the key at its own start; the
# TTL is only the backstop until task #41 lands explicit terminal clears.
_TTL_S = 900

_PREFIX = "jarvis:turn_msg:"


def _key(conversation: str) -> str:
	return _PREFIX + conversation


def bind_turn_message(conversation: str, message_id: str) -> None:
	"""Bind ``conversation -> message_id`` for the turn now starting.

	Called at turn start, before dispatch. A no-op on a missing argument (there
	is nothing meaningful to bind). Overwrites any prior binding for this
	conversation - correct, because only one turn is ever in flight per
	conversation, so the last writer is the running turn.
	"""
	if not conversation or not message_id:
		return
	frappe.cache().set_value(_key(conversation), message_id, expires_in_sec=_TTL_S)


def current_turn_message_id(conversation: str) -> str | None:
	"""Return the triggering ``message_id`` of the turn currently running in
	``conversation``, or ``None`` if nothing is bound (never set, or expired).

	``expires=True`` so the read reflects true Redis expiry and does not pollute
	the per-request local cache with the value.
	"""
	if not conversation:
		return None
	return frappe.cache().get_value(_key(conversation), expires=True)


# --------------------------------------------------------------------------- #
# The transport-independent run-cancel signal (skill "Approve & run", design §3.4)
# --------------------------------------------------------------------------- #
#
# ``stop_run``'s abort is turn-level + best-effort and never reaches the bench tool
# path (every cancel reader lives in the turn-settlement layer), so an in-flight
# skill auto-run chain would keep executing covered writes at the bench. The fix is
# a bench-visible cancel signal the auto-run branch (``jarvis.api._run_tool``) reads
# before EACH covered write, hard-stopping the chain within one write. It must work
# in BOTH pump and legacy transport, so it lives here as a bare Redis key set by
# ``stop_run``, not on any turn-machine row. A SHORT TTL: a cancel is only meaningful
# while a run is active - a stale key must not halt a fresh, re-approved run minutes
# later. It is also cleared explicitly the moment the gate consumes it.
_RUN_CANCEL_TTL_S = 120

_RUN_CANCEL_PREFIX = "jarvis:run_cancel:"


def _run_cancel_key(conversation: str) -> str:
	return _RUN_CANCEL_PREFIX + conversation


def request_run_cancel(conversation: str) -> None:
	"""Set the run-cancel signal for ``conversation`` (called by ``stop_run``,
	best-effort). A no-op on a missing conversation - there is nothing to halt."""
	if not conversation:
		return
	frappe.cache().set_value(_run_cancel_key(conversation), "1", expires_in_sec=_RUN_CANCEL_TTL_S)


def is_run_cancel_requested(conversation: str) -> bool:
	"""True iff a run cancel is currently requested for ``conversation``. Read by
	the auto-run cancel-gate before each covered write. ``expires=True`` so the read
	honours the short TTL and does not pin the value in the per-request local cache."""
	if not conversation:
		return False
	return bool(frappe.cache().get_value(_run_cancel_key(conversation), expires=True))


def clear_run_cancel(conversation: str) -> None:
	"""Drop the run-cancel signal - called the instant the gate consumes it (so a
	single Halt refuses exactly one covered write, not every later re-approved run)."""
	if not conversation:
		return
	frappe.cache().delete_value(_run_cancel_key(conversation))


# --------------------------------------------------------------------------- #
# Ending an approved skill run (skill "Approve & run", design §3.4 "Clear")
# --------------------------------------------------------------------------- #
#
# The auto-run branch (jarvis.api._run_tool) already clears ``skill_autorun`` on a
# failed covered write (hard-stop-on-error) and on a Halt (the cancel-gate). The
# REMAINING clears - a turn's terminal settlement, a new top-level message, and a
# dismiss of the paused card - live here so every clear site drops the SAME run
# state through one implementation.

_CONV = "Jarvis Conversation"


def clear_skill_autorun(conversation: str) -> None:
	"""Best-effort: END an approved skill run on ``conversation``.

	Drops ``skill_autorun`` (+ its sliding ``skill_autorun_at`` timestamp) and cleans
	up this conversation's ephemeral run state - the turn->message binding and any
	run-cancel signal (a stale cancel must not halt a later re-approved run). Committed
	immediately (mirroring the flag's ``db_set``+commit pattern) so the ended state
	survives a worker death; idempotent (a 0->0 clear is a no-op). NEVER raises: an
	end-of-run cleanup must not break its caller (a terminal settlement, a new send,
	or a dismiss)."""
	if not conversation:
		return
	try:
		frappe.db.set_value(
			_CONV,
			conversation,
			{"skill_autorun": 0, "skill_autorun_at": None, "skill_autorun_skill": None},
			update_modified=False,
		)
		frappe.db.commit()
	except Exception:
		try:
			frappe.log_error(title="clear_skill_autorun failed", message=frappe.get_traceback())
		except Exception:
			pass
	# The ephemeral redis run-state for this conversation is now stale - drop both keys.
	try:
		frappe.cache().delete_value(_key(conversation))
	except Exception:
		pass
	clear_run_cancel(conversation)


def _has_pending_card(owner: str | None, conversation: str) -> bool:
	"""True iff a pending confirmation card is STRICTLY bound to ``conversation``.

	Mirrors the gate's strict-conversation filter (``jarvis.api._run_tool``): a
	conversation-less token (a rare session-resolution miss) surfaces under any
	filter, so re-filter to ``record.conversation == conversation`` - it must never
	count as this conversation's pause. Lazy import (no import cycle: pending_confirm
	does not import this module)."""
	if not owner:
		return False
	from jarvis.chat import pending_confirm

	return any(
		t.get("conversation") == conversation
		for t in pending_confirm.list_for_owner(owner, conversation=conversation, strict=True)
	)


def on_terminal_turn(conversation: str) -> None:
	"""Terminal-turn hook (design §3.4 "Clear"): at a turn's terminal settlement, END
	an approved skill run UNLESS it is merely PAUSED on a parked card.

	Installed at ALL THREE terminal chokepoints (``turn_handler._advance_macro``,
	``finalize._effect_macro_advance``, ``turn_recovery._advance_macro``) because the
	Relay Pump - the DEFAULT transport - settles turns through ``finalize``, NOT
	``turn_handler`` (correctness-C1): a clear living only in ``turn_handler`` would
	never fire on the default path.

	Predicate: the run ends iff there is NO pending confirmation card strictly bound
	to this conversation. A pending card means a covered write's sibling destructive /
	create_custom_skill / bulk-light step PARKED - a legitimate PAUSE that resumes on
	confirm - so the flag is KEPT (the resume auto-runs the rest). A read-only no-op
	on a conversation not in an approved run. Best-effort: never raises into the
	terminal settlement path (a lookup failure KEEPS the flag - safer than ending a
	live/paused run; the TTL + reaper are the backstop)."""
	if not conversation:
		return
	try:
		row = frappe.db.get_value(_CONV, conversation, ["owner", "skill_autorun"], as_dict=True)
		if not row or not row.get("skill_autorun"):
			return  # not an approved run - nothing to end
		if _has_pending_card(row.get("owner"), conversation):
			return  # PAUSED on a parked card - keep the flag so the resume auto-runs
		clear_skill_autorun(conversation)
	except Exception:
		try:
			frappe.log_error(
				title="on_terminal_turn skill_autorun clear failed", message=frappe.get_traceback()
			)
		except Exception:
			pass
