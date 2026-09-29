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
import uuid

import frappe
from frappe import _

from jarvis.chat import txn
from jarvis.chat.events import publish_to_user

CONV = "Jarvis Conversation"
CHAT_SESSION = "Jarvis Chat Session"
TURN_USAGE = "Jarvis Turn Usage"

COMPACT_LOCK_SECONDS = 300
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


def _is_fresh_lock(since) -> bool:
	"""True while ``since`` (a ``compacting_since`` value already read by the
	caller) still falls inside the lock window. Shared by ``is_compacting``
	and ``context_payload`` so a caller that already has the row need not
	re-read ``compacting_since`` a second time."""
	if not since:
		return False
	age = (frappe.utils.now_datetime() - frappe.utils.get_datetime(since)).total_seconds()
	return 0 <= age < COMPACT_LOCK_SECONDS


def is_compacting(conversation: str) -> bool:
	since = frappe.db.get_value(CONV, conversation, "compacting_since")
	return _is_fresh_lock(since)


def classify_notice(text: str) -> str:
	"""``compacted`` when the runtime's notice says it compacted, ``skipped``
	when it says there was nothing to compact, else ``failed`` (the runtime
	reported a failure, or the notice is missing or unexpected)."""
	t = (text or "").strip().lstrip("⚙️").strip().lower()
	if t.startswith("compacted"):
		return "compacted"
	if t.startswith("compaction skipped"):
		return "skipped"
	return "failed"


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


def _last_turn_usage(session_key: str) -> dict:
	"""The newest ``Jarvis Turn Usage`` row for this session, for the ring
	popover's "Last reply" / "Model" rows. Empty when the session has no
	recorded turn yet."""
	if not session_key:
		return {}
	rows = frappe.get_all(
		TURN_USAGE,
		filters={"session_key": session_key},
		fields=["tokens_in", "tokens_out", "model"],
		order_by="creation desc",
		limit=1,
	)
	return rows[0] if rows else {}


def context_payload(conversation: str) -> dict:
	"""What the context ring renders. Reads only the bench snapshot; never
	touches the gateway."""
	conv = frappe.db.get_value(CONV, conversation, ["session_key", "compacting_since"], as_dict=True) or {}
	session_key = conv.get("session_key") or ""
	row = _session_row(session_key)
	capacity = int(row.get("context_capacity") or 0)
	used = int(row.get("last_total_tokens") or 0)
	reserve = int(row.get("reserve_tokens") or 0) or DEFAULT_RESERVE_TOKENS
	pct = round(100 * used / capacity, 1) if capacity > 0 else 0.0
	auto_pct = round(100 * (capacity - reserve) / capacity, 1) if capacity > reserve > 0 else 0.0
	last_turn = _last_turn_usage(session_key)
	return {
		"used": used,
		"capacity": capacity,
		"pct": pct,
		"warn_pct": WARN_PCT,
		"auto_compact_pct": auto_pct,
		"route": row.get("budget_route") or "",
		"compaction_count": int(row.get("compaction_count") or 0),
		"last_compacted_at": row.get("last_compacted_at"),
		"compacting": _is_fresh_lock(conv.get("compacting_since")),
		"fresh": bool(row.get("last_usage_at")) and capacity > 0,
		"last_in": int(last_turn.get("tokens_in") or 0),
		"last_out": int(last_turn.get("tokens_out") or 0),
		"model": last_turn.get("model") or "",
	}


def _try_take_lock(conversation: str) -> bool:
	"""Atomic compare-and-set on compacting_since: wins only when the lock is
	empty or expired. This, not any caller's earlier read, is the sole
	authority on which of two near-simultaneous callers may proceed."""
	now = frappe.utils.now_datetime()
	frappe.db.sql(
		"""UPDATE `tabJarvis Conversation`
		SET compacting_since = %(now)s
		WHERE name = %(name)s
		  AND (compacting_since IS NULL OR compacting_since < %(expired)s)""",
		{
			"now": now,
			"name": conversation,
			"expired": frappe.utils.add_to_date(now, seconds=-COMPACT_LOCK_SECONDS),
		},
	)
	won = frappe.db.sql("SELECT ROW_COUNT()")[0][0] > 0
	frappe.db.commit()
	return won


def start_compaction(conversation: str, user: str, hint: str) -> dict:
	"""Take the lock via compare-and-set and enqueue the job. Callers have
	already checked the conversation looks idle (api.compact_conversation),
	but that earlier check is only an optimization - the CAS here is what
	actually serializes two near-simultaneous callers."""
	if not _try_take_lock(conversation):
		return {"ok": False, "reason": "already_compacting"}
	# ``jarvis_compact_queue`` (site config) lets a sidecar deployment route the
	# job to a dedicated worker; production stays on the shared long queue.
	frappe.enqueue(
		method="jarvis.chat.compaction.run_compact",
		queue=frappe.conf.get("jarvis_compact_queue") or "long",
		timeout=COMPACT_JOB_TIMEOUT_S,
		at_front=True,
		job_id=f"jarvis-compact::{conversation}::{uuid.uuid4().hex[:8]}",
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
	"""Release the CAS lock start_compaction took (compacting_since). Called from
	run_compact's finally, so it runs on whatever snapshot the job's transaction opened
	at its first read, AFTER up to COMPACT_RPC_TIMEOUT_S (200 s) of RPC time (and, on the
	success path, after the write_compaction_result write already refreshed it - but on
	the declined/failed/exception paths nothing has refreshed it yet). A competing write
	to the SAME conversation row in that window (autotitle, another compact attempt's own
	CAS) must not turn "clear the lock" into "leave it stuck" for the full
	COMPACT_LOCK_SECONDS (300 s)."""

	def _clear() -> None:
		frappe.db.set_value(CONV, conversation, "compacting_since", None, update_modified=False)
		frappe.db.commit()

	txn.fresh_snapshot()
	try:
		txn.replay_on_conflict(_clear, label=f"chat: compact clear lock {conversation}")
	except Exception as e:
		if not txn.is_write_conflict(e):
			raise
		# Both attempts lost the race (or a write already pending made replay unsafe):
		# the lock stays set until COMPACT_LOCK_SECONDS elapses - a bounded, customer-
		# visible "still compacting" state, not silent data loss, so report it rather
		# than crash run_compact's finally over a lock that self-heals.
		frappe.log_error(title="chat: compact lock clear lost a write race", message=frappe.get_traceback())


def _gateway_session_row(sess, session_key: str) -> dict:
	"""The gateway's own row for this session, or {} if it has none. Shared by
	the before and after reads in run_compact - both want the same lookup."""
	rows = [r for r in (sess.list_sessions() or []) if r.get("key") == session_key]
	return rows[0] if rows else {}


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
			row = _gateway_session_row(sess, session_key)
			before = int(row.get("totalTokens") or 0)
			capacity = int(row.get("contextTokens") or 0)
			res = sess.compact_session(session_key, hint or None, timeout_s=COMPACT_RPC_TIMEOUT_S)
			# The RPC above can run up to COMPACT_RPC_TIMEOUT_S (200 s) on the transaction's
			# ORIGINAL snapshot (taken at the job's first read, before the RPC). Drop it now,
			# before any write to the shared session row, so usage.refresh_session_snapshots
			# (admin sync) or another writer that committed to this row during the wait does
			# not make the write below fail with 1020.
			txn.fresh_snapshot()
			outcome = classify_notice(res.get("text") or "") if res.get("state") == "final" else "failed"
			if outcome != "compacted":
				reason = "runtime_declined" if outcome == "skipped" else "runtime_failed"
				frappe.logger("jarvis.chat").warning(
					"compact declined conv=%s state=%s text=%s",
					conversation,
					res.get("state"),
					res.get("text"),
				)
			else:
				after_row = _gateway_session_row(sess, session_key) or None

				def _write() -> None:
					write_compaction_result(session_key, after_row)
					frappe.db.commit()

				# write_compaction_result's two UPDATEs are both absolute/GREATEST (budget
				# fields set from `after_row`, compaction_count only moves forward) - safe to
				# rerun whole on a lost race, so a replay is correct here, not just a retry.
				txn.replay_on_conflict(_write, label=f"chat: compact write {conversation}")
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
