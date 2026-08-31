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
