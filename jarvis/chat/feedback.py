"""Post-reply chat feedback: a thumbs up/down (with an optional note on a down)
on an assistant reply, forwarded to the admin fleet dashboard.

Direct-send, no local table: this bench keeps NO copy. ``submit_feedback``
derives every piece of metadata server-side (tenant is derived on the admin side
from the authenticated principal), then forwards it best-effort. A failed forward
is dropped silently - feedback is low-stakes, and a blocked tap is not acceptable.
The reply text is never sent; only the rating, refs, and a bounded optional note.

Only the caller's OWN assistant messages can be rated (ownership via the same
gate the rest of the chat surface uses, ``chat.api._get_owned_conversation``).
"""

from __future__ import annotations

import re

import frappe

from jarvis.chat.api import _get_owned_conversation
from jarvis.permissions import require_jarvis_access

MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"

_RATINGS = {"up", "down"}
_MAX_NOTE = 1000
#: The agent session UUID lives as the last segment of a composite session_key
#: like "agent:main:dashboard:<uuid>". Empty when absent/malformed - the rating
#: still records; only the admin-side deep-link is best-effort.
_SESSION_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


@frappe.whitelist()
def submit_feedback(message_id: str, rating: str, note: str | None = None) -> dict:
	"""Record a thumbs up/down on one assistant reply and forward it to admin.

	Args:
		message_id: the Jarvis Chat Message name of the assistant reply.
		rating: "up" or "down".
		note: optional free text, kept only on a "down".

	The rating commits on the first call (thumbs tap); a later call carrying the
	note folds onto the same admin row (upsert). Returns ``{"ok": True}`` even when
	the forward fails - the tap must never surface an error.
	"""
	require_jarvis_access()
	rating = (rating or "").strip()
	if rating not in _RATINGS:
		frappe.throw("rating must be 'up' or 'down'", frappe.ValidationError)

	# Raw db read bypasses field permlevel; we only need these four columns.
	msg = frappe.db.get_value(
		MSG, message_id, ["conversation", "role", "model", "reply_duration_ms"], as_dict=True
	)
	if not msg:
		frappe.throw("message not found", frappe.DoesNotExistError)
	if msg.role != "assistant":
		frappe.throw("can only rate assistant replies", frappe.ValidationError)

	# Ownership: the canonical chat gate (raises PermissionError for another user's
	# conversation, DoesNotExistError if it vanished). session_key is permlevel-1;
	# read it via db.get_value to bypass that, matching how the worker touches it.
	_get_owned_conversation(msg.conversation)
	session_key = frappe.db.get_value(CONV, msg.conversation, "session_key")

	payload = {
		"rating": rating,
		"message_ref": message_id,
		"conversation_ref": msg.conversation,
		"session_id": _session_uuid(session_key),
		"model": msg.model or "",
		"user_ref": frappe.session.user,
		"reply_duration_ms": msg.reply_duration_ms or 0,
		"note": (note or "").strip()[:_MAX_NOTE] if rating == "down" else "",
	}
	_forward(payload)
	return {"ok": True}


def _session_uuid(session_key: str | None) -> str:
	"""Bare agent session UUID from a composite session_key, or "" when absent."""
	tail = (session_key or "").rsplit(":", 1)[-1]
	return tail if _SESSION_UUID_RE.match(tail) else ""


def _forward(payload: dict) -> None:
	"""Best-effort forward to admin. NEVER raises to the caller: a lost rating is
	acceptable (low-stakes), a blocked or errored tap is not."""
	try:
		from jarvis import admin_client

		admin_client.push_chat_feedback(payload)
	except Exception:
		frappe.log_error(title="chat feedback forward failed")
