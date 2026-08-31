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
