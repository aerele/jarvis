"""Deferred agent-correction notes queued on a conversation.

When a user DISCARDS a parked write, the agent's in-container session memory
still holds the stale ``pending_confirmation`` result for that call - and the
bench never replays transcript rows to the model, so the only channel to correct
it is a line folded into the NEXT outgoing turn's ``[Context: ...]`` bracket.
These notes are that channel:

  * ``append``  on discard (jarvis.chat.actions_api.dismiss_tool),
  * ``read``    at bracket-build (jarvis.chat.turn_handler.handle_chat_send),
  * ``clear``   after a delivered send - remove EXACTLY the entries this turn
                folded into its bracket, by id.

Stored as a JSON list of ``{"id", "text"}`` on
``Jarvis Conversation.pending_agent_notes`` (durable across a bench restart,
unlike a Redis key). ``append`` and ``clear`` take a brief row lock
(``get_value(..., for_update=True)`` held only until the immediate commit), and
the clear removes entries **by id**, not by position. Id-based removal is what
makes it airtight against overlapping turns: continuation turns dispatch through
``_enqueue_turn`` which does NOT take the single-flight ``_conversation_busy``
guard, so two rapid Confirm clicks can run two continuations concurrently; each
clears only the ids IT delivered, so a discard appended mid-window (a fresh id
neither turn drained) can never be dropped by the other turn's clear. The drain
(``read``) is an unlocked snapshot on purpose - a note appended after it has a
new id that no in-flight clear will match.
"""

from __future__ import annotations

import frappe

CONV = "Jarvis Conversation"
_FIELD = "pending_agent_notes"


def _parse(raw) -> list:
	"""Normalize the stored value to a list of ``{"id","text"}`` entries, dropping
	anything malformed - a corrupt value must never break a turn."""
	if not raw:
		return []
	try:
		items = frappe.parse_json(raw)
	except Exception:
		return []
	if not isinstance(items, list):
		return []
	out = []
	for it in items:
		if isinstance(it, dict) and it.get("id") and it.get("text") is not None:
			out.append({"id": str(it["id"]), "text": str(it["text"])})
	return out


def _locked(conversation: str) -> list:
	"""Read the queue under a row lock (held until the caller's commit)."""
	return _parse(frappe.db.get_value(CONV, conversation, _FIELD, for_update=True))


def _write(conversation: str, entries: list) -> None:
	frappe.db.set_value(
		CONV,
		conversation,
		_FIELD,
		frappe.as_json(entries) if entries else None,
		update_modified=False,
	)
	frappe.db.commit()


def read(conv) -> list:
	"""Unlocked snapshot of the queued entries ``[{"id","text"}, ...]`` for the
	bracket build. The caller folds the texts into the bracket and passes the ids
	to ``clear`` after a delivered send."""
	return _parse(getattr(conv, _FIELD, None))


def append(conversation: str, note: str) -> None:
	"""Append one note (with a fresh id) atomically under a row lock, so a discard
	landing while a turn streams is never lost to a racing append or a turn's
	end-of-turn clear."""
	entries = _locked(conversation)
	entries.append({"id": frappe.generate_hash(length=12), "text": note})
	_write(conversation, entries)


def clear(conversation: str, ids) -> None:
	"""Remove exactly the entries a turn delivered (by id), keeping any appended
	since AND any a concurrent overlapping turn also delivered but this one did
	not drain. Atomic under a row lock; called only after a delivered send, so a
	pre-ack failure leaves the queue intact for retry."""
	ids = set(ids or ())
	if not ids:
		return
	entries = _locked(conversation)
	remaining = [e for e in entries if e["id"] not in ids]
	# Always write (releasing the lock) even when nothing matched - a concurrent
	# turn may have cleared these ids first; that is the correct no-op, not a leak.
	_write(conversation, remaining)
