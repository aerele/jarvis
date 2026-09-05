"""Per-conversation context meter and manual compaction.

Spec: docs/superpowers/specs/2026-09-05-chat-context-meter-compact-design.md

The runtime keeps the context snapshot on its sessions row (used = totalTokens,
capacity = contextTokens) and ``jarvis.chat.usage`` already mirrors it into
``Jarvis Chat Session`` after every completed turn. This module adds the read
payload for the UI, the compact job, and the per-conversation lock the send
path honours while a compaction is in flight.

Facts verified live 2026-09-05 (image 2026.6.8, e2e.localhost):
- the ONLY way to pass a "what to keep" hint is the runtime's text command
  ``/compact <hint>`` over chat.send; the sessions.compact RPC has no hint;
- the command path answers with one ``chat`` final event carrying a notice
  ("⚙️ Compacted (58k before) • ...", or "⚙️ Compaction skipped: ...") and
  emits no lifecycle frames;
- after compaction the row's totalTokens is null / not fresh until the NEXT
  turn, so this job never rewrites last_total_tokens or context_pct.
"""

from __future__ import annotations

import re

import frappe
from frappe import _

from jarvis.chat.events import publish_to_user

CONV = "Jarvis Conversation"
CHAT_SESSION = "Jarvis Chat Session"

COMPACT_LOCK_SECONDS = 240
HINT_MAX_CHARS = 500
WARN_PCT = 80
DEFAULT_RESERVE_TOKENS = 20000
COMPACT_JOB_TIMEOUT_S = 260
COMPACT_RPC_TIMEOUT_S = 200.0

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_hint(hint: str | None) -> str:
	"""Trim, strip control characters, collapse whitespace, cap the length.
	A hint starting with ``/`` would be read by the runtime as ANOTHER
	command, so it is refused outright."""
	text = _CONTROL_CHARS.sub("", str(hint or ""))
	text = " ".join(text.split())
	if text.startswith("/"):
		frappe.throw(_("The hint cannot start with a slash."), frappe.ValidationError)
	return text[:HINT_MAX_CHARS]


def is_compacting(conversation: str) -> bool:
	since = frappe.db.get_value(CONV, conversation, "compacting_since")
	if not since:
		return False
	age = (frappe.utils.now_datetime() - frappe.utils.get_datetime(since)).total_seconds()
	return 0 <= age < COMPACT_LOCK_SECONDS


def classify_notice(text: str) -> str:
	"""``compacted`` when the runtime's notice says it compacted, else
	``declined`` (skipped / failed / unavailable / anything unexpected)."""
	t = (text or "").strip().lstrip("⚙️").strip().lower()
	return "compacted" if t.startswith("compacted") else "declined"


def _session_row(session_key: str) -> dict:
	if not session_key:
		return {}
	return (
		frappe.db.get_value(
			CHAT_SESSION,
			{"session_key": session_key},
			[
				"last_total_tokens",
				"context_capacity",
				"context_pct",
				"last_usage_at",
				"budget_route",
				"reserve_tokens",
				"compaction_count",
				"last_compacted_at",
			],
			as_dict=True,
		)
		or {}
	)


def context_payload(conversation: str) -> dict:
	"""What the context pill renders. Reads only the bench snapshot; never
	touches the gateway."""
	conv = frappe.db.get_value(CONV, conversation, ["session_key", "compacting_since"], as_dict=True) or {}
	row = _session_row(conv.get("session_key") or "")
	capacity = int(row.get("context_capacity") or 0)
	used = int(row.get("last_total_tokens") or 0)
	reserve = int(row.get("reserve_tokens") or 0) or DEFAULT_RESERVE_TOKENS
	pct = round(100 * used / capacity, 1) if capacity > 0 else 0.0
	auto_pct = round(100 * (capacity - reserve) / capacity, 1) if capacity > reserve > 0 else 0.0
	return {
		"used": used,
		"capacity": capacity,
		"pct": pct,
		"warn_pct": WARN_PCT,
		"auto_compact_pct": auto_pct,
		"route": row.get("budget_route") or "",
		"compaction_count": int(row.get("compaction_count") or 0),
		"last_compacted_at": row.get("last_compacted_at"),
		"compacting": is_compacting(conversation),
		"fresh": bool(row.get("last_usage_at")) and capacity > 0,
	}


def start_compaction(conversation: str, user: str, hint: str) -> dict:
	"""Take the lock and enqueue the job. Callers have already checked the
	conversation is idle (api.compact_conversation)."""
	frappe.db.set_value(
		CONV, conversation, "compacting_since", frappe.utils.now_datetime(), update_modified=False
	)
	frappe.db.commit()
	frappe.enqueue(
		method="jarvis.chat.compaction.run_compact",
		queue="long",
		timeout=COMPACT_JOB_TIMEOUT_S,
		at_front=True,
		job_id=f"jarvis-compact::{conversation}",
		conversation=conversation,
		user=user,
		hint=hint,
	)
	return {"ok": True, "queued": True}


def write_compaction_result(session_key: str, row: dict | None) -> None:
	"""Stamp the compaction on the session snapshot. Does NOT touch
	last_total_tokens / context_pct: the runtime row is stale until the next
	turn (verified live)."""
	from jarvis.chat import usage

	usage._write_budget_fields(session_key, row)
	frappe.db.sql(
		"""UPDATE `tabJarvis Chat Session`
		SET compaction_count = GREATEST(IFNULL(compaction_count, 0), 1),
			last_compacted_at = %(now)s
		WHERE session_key = %(key)s""",
		{"now": frappe.utils.now_datetime(), "key": session_key},
	)


def _clear_lock(conversation: str) -> None:
	frappe.db.set_value(CONV, conversation, "compacting_since", None, update_modified=False)
	frappe.db.commit()


def run_compact(conversation: str, user: str, hint: str = "") -> None:
	"""RQ entry point. One pooled gateway session for the whole operation."""
	from jarvis.chat import agent_session_pool, usage
	from jarvis.chat.agent_client import AgentUnreachableError

	conv = frappe.db.get_value(CONV, conversation, ["session_key"], as_dict=True) or {}
	session_key = conv.get("session_key") or ""
	settings = frappe.get_single("Jarvis Settings")
	gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
	reason: str | None = None
	before = 0
	capacity = 0
	count = 0
	try:
		with agent_session_pool.checkout(gateway_url) as sess:
			rows = [r for r in (sess.list_sessions() or []) if r.get("key") == session_key]
			row = rows[0] if rows else {}
			before = int(row.get("totalTokens") or 0)
			capacity = int(row.get("contextTokens") or 0)
			res = sess.compact_session(session_key, hint or None)
			outcome = classify_notice(res.get("text") or "") if res.get("state") == "final" else "declined"
			if outcome != "compacted":
				reason = "runtime_declined"
				frappe.logger("jarvis.chat").info(
					"compact declined conv=%s state=%s text=%s",
					conversation,
					res.get("state"),
					res.get("text"),
				)
			else:
				rows = [r for r in (sess.list_sessions() or []) if r.get("key") == session_key]
				after_row = rows[0] if rows else None
				write_compaction_result(session_key, after_row)
				frappe.db.commit()
				count = int(
					frappe.db.get_value(CHAT_SESSION, {"session_key": session_key}, "compaction_count") or 0
				)
	except AgentUnreachableError as e:
		reason = "timeout" if getattr(e, "code", None) == "compact-timeout" else "gateway_unreachable"
	except Exception:
		frappe.log_error(title="chat: compact job failed", message=frappe.get_traceback())
		reason = "unknown"
	finally:
		_clear_lock(conversation)

	if reason:
		publish_to_user(
			user, {"kind": "context:compact_failed", "conversation_id": conversation, "reason": reason}
		)
		return
	publish_to_user(
		user,
		{
			"kind": "context:compacted",
			"conversation_id": conversation,
			"before": before,
			"capacity": capacity,
			"compaction_count": count,
		},
	)
