"""Scheduler-driven recovery for managed chat turns that the bench abandoned.

A long turn can outrun the bench's WS cap, or its RQ worker can be hard-killed
(SIGTERM at the 720s job cap, OOM, crash, deploy). openclaw keeps running the
turn and persists the result regardless. So instead of falsely erroring, the
turn is left `streaming=1, recovering=1` and this job finalizes it from the
gateway's durable transcript.

Design notes (from the 2026-06-30 review):
- Content comes from the RAW transcript (sessions.get), never chat.history,
  which truncates assistant text at max_chars.
- Only the LATEST recovering row per conversation is snapshot-finalized, so the
  session-wide "newest assistant message" can never bleed onto an older row.
- There is an UNCONDITIONAL ceiling backstop: a row past the ceiling is errored
  even when the gateway is unreachable, so a managed turn can never be stranded
  at a perpetual spinner.
- Finalize/error are conditional (only act on a row still streaming=1 AND
  recovering=1), so overlapping cycles cannot double-deliver.
- is_run_active is read ONCE per cycle (sessions.list is expensive), not per row.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import frappe

from jarvis.chat.events import publish_to_user
from jarvis.chat.openclaw_client import OpenclawSession

MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"

# Absolute give-up: a row recovering longer than this is errored, gateway
# reachable or not. The unconditional backstop that replaces the old
# time-based stale_scan error path.
_RECOVERY_CEILING_MINUTES = 60

# Exact text stamped by the unconditional ceiling backstop below. Named so
# jarvis.diagnostics.chat_recovery_stats can count ceiling errors without
# duplicating (and risking drift from) the literal string.
CEILING_ERROR_MESSAGE = "Run did not finish within the recovery window."

# Spike-alarm thresholds (recovery_rate_watch): a sustained high recovered
# rate over the last 24h means the never-error machinery is quietly
# compensating for a sick gateway. Both must hold - the count guard (>=5)
# keeps a quiet bench (e.g. 1 recovered out of 2 turns = 50%) from alarming
# on noise.
_RATE_WATCH_MIN_RECOVERED = 5
_RATE_WATCH_MIN_RATE = 0.2
# Dedupe window: a sustained problem alarms roughly once a day, not once an
# hour, even though this is hooked hourly.
_RATE_WATCH_DEDUPE_HOURS = 20


@contextlib.contextmanager
def _recovery_connection(gateway_url: str) -> Iterator[OpenclawSession]:
	"""A dedicated short-lived connection for recovery. NEVER the shared pool
	(openclaw_session_pool is single-turn-exclusive and not concurrency-safe)."""
	sess = OpenclawSession.connect(gateway_url)
	try:
		yield sess
	finally:
		sess.close()


def _age_minutes(dt) -> float:
	if not dt:
		return 0.0
	delta = frappe.utils.now_datetime() - frappe.utils.get_datetime(dt)
	return delta.total_seconds() / 60.0


def _latest_assistant_text(messages: list, *, min_seq: int = 0, max_seq: int | None = None) -> str:
	"""Newest assistant message text from a raw transcript. Handles a
	plain-string content and the {type:"text", text} block list. Sorted by the
	transcript seq so the latest turn wins. Every text source is type-guarded.

	``min_seq`` is the transcript-seq watermark captured before this turn's
	chat.send: a message whose seq is <= min_seq predates this turn (or is a
	previous turn's reply left over from a run that died server-side with
	zero output), so it is skipped even if it is the newest assistant message
	in the transcript. A message with no seq (0) is treated as predating the
	turn whenever a watermark is in force (min_seq > 0).

	``max_seq`` bounds the window from ABOVE: the next turn's watermark
	(captured before ITS chat.send) marks where this turn's transcript slice
	ends. Without it, recovering an older parked row after a NEWER turn
	completed in the same conversation would steal the newer turn's answer
	(observed live 2026-07-03: the parked row recovered with the next
	question's reply). Messages with seq > max_seq belong to a later turn."""

	def seq(m):
		return ((m or {}).get("__openclaw") or {}).get("seq", 0)

	for m in sorted(messages or [], key=seq, reverse=True):
		if min_seq > 0 and seq(m) <= min_seq:
			continue
		if max_seq is not None and seq(m) > max_seq:
			continue
		if (m.get("role") or "").lower() != "assistant":
			continue
		c = m.get("content")
		if isinstance(c, str) and c.strip():
			return c
		if isinstance(c, list):
			parts = [
				b.get("text", "")
				for b in c
				if isinstance(b, dict)
				and b.get("type") == "text"
				and isinstance(b.get("text"), str)
				and b.get("text")
			]
			joined = "\n".join(p for p in parts if p.strip())
			if joined.strip():
				return joined
		t = m.get("text")
		if isinstance(t, str) and t.strip():
			return t
	return ""


def _conditional_clear(name: str, fields: dict) -> bool:
	"""Apply `fields` to a row ONLY if it is still streaming=1 AND recovering=1.
	Returns True if this call won the row (so the caller publishes), False if
	another cycle already finalized it. Idempotency guard for #8."""
	set_clause = ", ".join(f"`{k}` = %({k})s" for k in fields)
	params = dict(fields, name=name)
	frappe.db.sql(
		f"UPDATE `tab{MSG}` SET {set_clause} WHERE name = %(name)s AND streaming = 1 AND recovering = 1",
		params,
	)
	# Read rowcount BEFORE commit (commit can reset the cursor).
	cursor = getattr(frappe.db, "_cursor", None)
	won = bool(cursor and cursor.rowcount)
	frappe.db.commit()
	return won


def _advance_macro(conversation_id: str, *, errored: bool) -> None:
	"""Chaining hook for the macro engine (mirrors turn_handler._advance_macro;
	not imported from there to avoid a cycle risk between chat.turn_handler
	and chat.turn_recovery). A macro chain that ends its turn via park-and-
	recover (any post-ack interruption under the relay model) must still
	step/terminate the macro, or the chain stalls forever. Best-effort: a
	macro bug must never affect the recovery outcome."""
	try:
		from jarvis.chat import macros

		macros.advance_after_turn(conversation_id, errored=errored)
	except Exception:
		frappe.log_error(
			title="turn_recovery: macro advance hook failed",
			message=frappe.get_traceback(),
		)
	try:
		from jarvis.learning import app_analysis

		app_analysis.on_turn_end(conversation_id, errored=errored)
	except Exception:
		frappe.log_error(
			title="turn_recovery: app-learning turn hook failed",
			message=frappe.get_traceback(),
		)


def _finalize(row: dict, text: str) -> None:
	"""Authoritative completion (conditional + idempotent): overwrite content
	from the raw snapshot, clear the flags, then publish for any live viewer."""
	if not _conditional_clear(
		row["name"],
		{
			"content": text,
			"streaming": 0,
			"recovering": 0,
			"error": "",
			"was_recovered": 1,
		},
	):
		return  # another cycle already finalized this row
	conv, owner, name = row["conversation"], row["owner"], row["name"]
	# Phase-0 admission: a recovered turn is a terminal settlement of the
	# conversation's dispatching Turn row - close it (done) + promote the next
	# queued turn. Flag-gated + best-effort inside admission (self-host / flag-off
	# are unaffected). Keyed by conversation because recovery works off Message
	# rows and per-conversation single-flight makes the dispatching turn unique.
	_admission_settle_conv(conv, "done")
	publish_to_user(
		owner,
		{
			"kind": "assistant:delta",
			"conversation_id": conv,
			"message_id": name,
			"text": text,
			"run_id": "recovered",
		},
	)
	publish_to_user(
		owner,
		{
			"kind": "run:end",
			"conversation_id": conv,
			"message_id": name,
			"run_id": "recovered",
		},
	)
	_advance_macro(conv, errored=False)

	# Best-effort: a recovered long turn is exactly the kind that produced
	# charts / generated images, so it deserves the same rich-output
	# persistence as the worker's clean-exit path. Lazy import (codebase
	# pattern - avoids any import-cycle question between turn_recovery and
	# turn_handler).
	try:
		from jarvis.chat import turn_handler

		turn_handler.persist_rich_outputs(
			name,
			conv,
			row["owner"],
			"recovered",
			int(frappe.utils.get_datetime(row["creation"]).timestamp() * 1000) if row.get("creation") else 0,
		)
	except Exception:
		frappe.log_error(
			title="turn_recovery: rich-output persist failed",
			message=frappe.get_traceback(),
		)

	# A recovered turn completed like any other, so it earns the same post-
	# turn wiki nudge as the worker's clean exit. Fire-and-forget: all gates
	# re-check inside the short-queue job; a failure never affects recovery.
	# Cheap pre-gate mirrors the clean-exit path so wiki-off / self-host
	# deployments never spawn the per-turn job (owner is the only sender
	# identity a recovered turn carries).
	try:
		from jarvis import selfhost
		from jarvis.chat import wiki

		if not selfhost.is_self_hosted() and wiki.wiki_enabled():
			frappe.enqueue(
				"jarvis.chat.wiki.maybe_nudge",
				queue="short",
				conversation_id=conv,
				user=owner,
				run_id="recovered",
			)
	except Exception:
		frappe.log_error(
			title="turn_recovery: wiki nudge enqueue failed",
			message=frappe.get_traceback(),
		)


def _admission_settle_conv(conversation: str, state: str, error: str | None = None) -> None:
	"""Phase-0 admission recovery hook (flag-gated + best-effort)."""
	try:
		from jarvis.chat import admission

		admission.settle_conversation_dispatching(conversation, state, error=error)
	except Exception:
		frappe.log_error(title="admission recovery settle", message=frappe.get_traceback())


def _error(row: dict, message: str) -> None:
	if not _conditional_clear(
		row["name"],
		{
			"streaming": 0,
			"recovering": 0,
			"error": message,
			"was_recovered": 1,
		},
	):
		return
	_admission_settle_conv(row["conversation"], "errored", message)
	publish_to_user(
		row["owner"],
		{
			"kind": "run:error",
			"conversation_id": row["conversation"],
			"message_id": row["name"],
			"run_id": "recovered",
			"error": message,
		},
	)
	_advance_macro(row["conversation"], errored=True)


def _active_map(sess: OpenclawSession) -> dict:
	"""One sessions.list per cycle -> {session_key: hasActiveRun}. Replaces the
	per-row is_run_active call (#13)."""
	res = sess._request("sessions.list", {}, timeout_s=10)
	out = {}
	for s in (res.get("payload") or {}).get("sessions") or []:
		if s.get("key"):
			out[s["key"]] = bool(s.get("hasActiveRun"))
	return out


def recover_pending_turns(limit: int = 20) -> dict:
	"""Scheduler entry: finalize managed turns stuck in the recovering state."""
	from jarvis import selfhost

	if selfhost.is_self_hosted():
		return {"skipped": "self-hosted"}

	# Query rows FIRST (so we never load Settings on an empty bench, #14).
	# Ordered conversation, seq DESC so the first row per conversation is the
	# latest (used for the no-bleed dedup, #2).
	rows = frappe.db.sql(
		"""
		SELECT m.name, m.conversation, c.session_key, c.owner,
			   m.recovery_started_at, m.seq, m.openclaw_seq_watermark, m.creation
		FROM `tabJarvis Chat Message` m
		JOIN `tabJarvis Conversation` c ON c.name = m.conversation
		WHERE m.streaming = 1 AND m.recovering = 1
		  AND c.session_key IS NOT NULL AND c.session_key != ''
		ORDER BY m.conversation ASC, m.seq DESC
		LIMIT %(limit)s
		""",
		{"limit": limit},
		as_dict=True,
	)
	if not rows:
		return {"checked": 0}

	counts = {"finalized": 0, "active": 0, "errored": 0, "waiting": 0}

	# UNCONDITIONAL backstop FIRST (#3/#5): error anything past the ceiling,
	# gateway reachable or not, so a row is never stranded at a spinner.
	live = []
	for r in rows:
		if _age_minutes(r["recovery_started_at"]) > _RECOVERY_CEILING_MINUTES:
			_error(r, CEILING_ERROR_MESSAGE)
			counts["errored"] += 1
		else:
			live.append(r)
	if not live:
		return {"checked": len(rows), **counts}

	# Only the LATEST recovering row per conversation is eligible for snapshot
	# finalize (#2: never assign the session-wide newest answer to an older row).
	# Older recovering rows ride the ceiling backstop above.
	eligible = {}
	for r in live:  # seq DESC, so first seen per conversation is the latest
		eligible.setdefault(r["conversation"], r)
	eligible = list(eligible.values())

	settings = frappe.get_single("Jarvis Settings")
	gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
	if not gateway_url:
		return {"checked": len(rows), **counts, "skipped": "no gateway"}

	try:
		with _recovery_connection(gateway_url) as sess:
			active = _active_map(sess)  # one sessions.list per cycle (#13)
			for r in eligible:
				try:
					counts[_recover_one(sess, r, active)] += 1
				except Exception:
					frappe.log_error(
						title="turn_recovery: row failed",
						message=frappe.get_traceback(),
					)
	except Exception:
		# Gateway unreachable this cycle. The ceiling backstop already ran, so
		# nothing is stranded; non-expired rows simply retry next cycle.
		frappe.log_error(
			title="turn_recovery: connect failed",
			message=frappe.get_traceback(),
		)
		return {"checked": len(rows), **counts, "connect_failed": True}
	return {"checked": len(rows), **counts}


def _recover_one(sess: OpenclawSession, row: dict, active: dict) -> str:
	"""Drive one eligible (latest-per-conversation) recovering row. Returns
	'finalized' | 'active' | 'waiting'. Ceiling/error is handled by the caller
	and the unconditional backstop, not here."""
	session_key = row["session_key"]
	# Absent from sessions.list -> treat as not-active, but only finalize when
	# the transcript actually has content (#9: never finalize from a stale
	# snapshot just because a row vanished from the gateway's in-memory list).
	if active.get(session_key, False):
		return "active"
	# Raw transcript (sessions.get), NOT chat.history -> no max_chars truncation (#1).
	messages = sess.get_session_messages(session_key, limit=50)
	text = _latest_assistant_text(
		messages,
		min_seq=row.get("openclaw_seq_watermark") or 0,
		max_seq=_next_turn_watermark(row["conversation"], row["seq"]),
	)
	if text:
		_finalize(row, text)
		return "finalized"
	return "waiting"  # no output yet; the ceiling backstop bounds the wait


def _next_turn_watermark(conversation: str, seq: int) -> int | None:
	"""Upper bound for a parked row's transcript window: the smallest
	watermark among LATER assistant rows in the same conversation (each
	captured just before that turn's chat.send, so transcript messages
	past it belong to that later turn). None when there is no later turn
	(or none with a usable watermark) - then the window stays open-ended,
	which matches the common single-in-flight-turn case."""
	val = frappe.db.sql(
		"""
		SELECT MIN(openclaw_seq_watermark)
		FROM `tabJarvis Chat Message`
		WHERE conversation = %(conv)s AND role = 'assistant'
		  AND seq > %(seq)s AND openclaw_seq_watermark > 0
		""",
		{"conv": conversation, "seq": seq},
	)[0][0]
	return int(val) if val else None


def recover_now(conversation_id: str) -> str:
	"""In-worker immediate recovery for one conversation's latest recovering
	row, so the common case (run already finished when the stream was lost)
	does not wait for the cron. Dedicated connection - the pooled WS may be
	the very thing that just died. Idempotent vs the cron via
	_conditional_clear inside _finalize/_error."""
	from jarvis import selfhost

	if selfhost.is_self_hosted():
		return "skipped"
	rows = frappe.db.sql(
		"""
		SELECT m.name, m.conversation, c.session_key, c.owner,
			   m.recovery_started_at, m.seq, m.openclaw_seq_watermark, m.creation
		FROM `tabJarvis Chat Message` m
		JOIN `tabJarvis Conversation` c ON c.name = m.conversation
		WHERE m.streaming = 1 AND m.recovering = 1
		  AND m.conversation = %(conv)s
		  AND c.session_key IS NOT NULL AND c.session_key != ''
		ORDER BY m.seq DESC
		LIMIT 1
		""",
		{"conv": conversation_id},
		as_dict=True,
	)
	if not rows:
		return "skipped"
	row = rows[0]
	settings = frappe.get_single("Jarvis Settings")
	gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
	if not gateway_url:
		return "skipped"
	try:
		with _recovery_connection(gateway_url) as sess:
			active = {row["session_key"]: sess.is_run_active(row["session_key"])}
			return _recover_one(sess, row, active)
	except Exception:
		frappe.log_error(
			title="turn_recovery: recover_now failed",
			message=frappe.get_traceback(),
		)
		return "skipped"


def recovery_rate_watch() -> None:
	"""Hourly scheduler entry: spike alarm for the recovered-turn rate.

	Snapshot recovery is designed to be invisible to the user (never-error),
	which means a sick gateway that is forcing MOST turns through recovery
	would otherwise go unnoticed. If at least _RATE_WATCH_MIN_RECOVERED turns
	were recovered in the last 24h AND they are more than _RATE_WATCH_MIN_RATE
	of all assistant turns in that window, log an Error Log so operators see
	it - deduped against an existing Error Log with the same title in the
	last _RATE_WATCH_DEDUPE_HOURS hours, so a sustained problem alarms
	roughly once a day rather than every hour this cron fires."""
	from jarvis import selfhost

	if selfhost.is_self_hosted():
		return

	since_24h = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-24)
	row = frappe.db.sql(
		"""
		SELECT COUNT(*) AS total,
			   SUM(CASE WHEN was_recovered = 1 THEN 1 ELSE 0 END) AS recovered
		FROM `tabJarvis Chat Message`
		WHERE role = 'assistant' AND creation >= %(since)s
		""",
		{"since": since_24h},
		as_dict=True,
	)[0]
	total = row.total or 0
	recovered = row.recovered or 0
	rate = (recovered / total) if total else 0
	if recovered < _RATE_WATCH_MIN_RECOVERED or rate <= _RATE_WATCH_MIN_RATE:
		return

	title = "chat: high recovered-turn rate"
	since_dedupe = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-_RATE_WATCH_DEDUPE_HOURS)
	existing = frappe.db.sql(
		"""
		SELECT name FROM `tabError Log`
		WHERE method = %(title)s AND creation >= %(since)s
		LIMIT 1
		""",
		{"title": title, "since": since_dedupe},
	)
	if existing:
		return
	frappe.log_error(
		title=title,
		message=(
			f"Recovered {recovered}/{total} assistant turns ({rate:.0%}) in "
			"the last 24h via snapshot recovery. The never-error machinery "
			"is compensating for turns that never completed live - check "
			"gateway health."
		),
	)
