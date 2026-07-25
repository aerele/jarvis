"""Phase-0 durable admission control (chat concurrency, WP-0).

The reported incident: when every in-flight chat slot is taken, a new send
becomes an invisible multi-minute RQ wait (or starves the site). Phase 0
kills that in the CURRENT transport - no pump, no WS/relay changes. A send
that cannot dispatch becomes a durable ``queued`` Jarvis Chat Turn with a
visible position and a cancel affordance; the legacy per-turn job path is
still the executor for admitted turns.

Design (binding: ../wp-d D2/D3 + PANEL-DISPOSITIONS OAR/SUX):

* MariaDB is the only authority. Every state change is a version-CAS
  (``WHERE state=X AND version=V``) whose affected-rows count is the success
  signal. Redis is never authoritative.
* The admission serializer takes ``SELECT ... FOR UPDATE`` on the per-shard
  ``Jarvis Relay Pump`` control row (OAR-1). Lock order (OAR-6, normative):
  control-row -> conversation -> turn -> message.
* Dual-signal active check (OAR-11): a conversation/shard is busy on a
  Turn-row ``dispatching`` OR a legacy fresh-``streaming`` assistant Message
  with no owning Turn row (the flag can flip mid-traffic, so legacy turns
  always coexist in Phase 0).
* Flag OFF (``jarvis_phase0_admission_enabled``, default off) => byte-
  identical legacy behavior: the gate is a cheap ``frappe.conf`` read that
  short-circuits before any Turn row or admission query. No schema traffic
  on the hot path.

Phase-0 simplifications (recorded for WP-1):

* ``relay_target_id`` is a single site-wide shard (``"default"``) - one
  gateway per bench. WP-1 derives real per-container ids; the schema keys on
  ``relay_target_id`` from day one so that is additive.
* ``dispatching`` covers the whole legacy job lifetime; the reservation IS
  the ``dispatching`` row itself, and ``reservation_expires_at`` guards only
  a crashed/lost enqueue.
"""

from __future__ import annotations

import json
import time

import frappe

from jarvis.chat.events import publish_to_user
from jarvis.permissions import require_jarvis_access

TURN = "Jarvis Chat Turn"
PUMP = "Jarvis Relay Pump"
MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"

FLAG = "jarvis_phase0_admission_enabled"

# One gateway per bench in Phase 0 -> one site-wide admission shard.
DEFAULT_RELAY_TARGET = "default"

# Container main-lane ceiling (fleet agents.defaults.maxConcurrent, default 4).
DEFAULT_MAX_INFLIGHT = 4

# Accept-time overload guard (SUX-5): reject (no row) past this queued depth.
MAX_QUEUE_DEPTH = 25

# Sweep age-out (SUX-5): a queued turn older than this is system-cancelled.
QUEUED_MAX_AGE_S = 15 * 60

# Durable transcript marker copy (SUXI-4).
USER_CANCEL_REASON = "You cancelled this message before it was answered."
AGE_OUT_REASON = "Waited too long in the queue and was cancelled. Please try again."

# Crashed-enqueue guard: a dispatching turn whose enqueue was lost is returned
# to the queue after this many seconds. Deliberately >> the worker turn timeout
# (720s) so a legitimately long live turn is never reclaimed.
RESERVE_TTL_S = 900

# Legacy fresh-streaming window (mirrors api._INFLIGHT_FRESH_SECONDS).
_INFLIGHT_FRESH_SECONDS = 180

_ACTIVE_STATES = ("queued", "dispatching")

# Every non-terminal in-flight state that consumes a shard credit. Phase-0 turns
# only ever occupy 'dispatching' among these, so the dual-signal counts below are
# BYTE-IDENTICAL when the pump is off (the pump-only states are empty). During
# pump/legacy coexistence (rollout + draining, OAR-11) they make a new Phase-0/
# legacy send see a pump-owned in-flight turn as busy/inflight.
_INFLIGHT_STATES = ("preparing", "ready", "dispatching", "streaming", "terminal_observed")
# Same set + 'queued' = "any turn on this conversation not yet terminal" — the
# single-flight scope (a pump 'queued' turn also blocks a second send on its conv).
_CONV_ACTIVE_STATES = ("queued",) + _INFLIGHT_STATES
# CDX-24: 'recovering' joins the PER-CONVERSATION single-flight set ONLY (never the
# credit sets above — a parked turn still holds NO shard credit). A kill-/drop-parked
# turn releases capacity but its conversation stays BLOCKED: the old gateway run may
# still be live and owned by turn_recovery, so a second send on that conversation must
# not dispatch a concurrent run until the parked turn reaches a genuine terminal
# (done/errored/cancelled). 'recovering' is a pump/watchdog-only state, so when the
# pump is OFF no such row exists and the single-flight check stays byte-identical.
_CONV_BLOCKING_STATES = _CONV_ACTIVE_STATES + ("recovering",)


def _in_sql(values) -> str:
	return ", ".join(f"'{v}'" for v in values)


# --------------------------------------------------------------------------- #
# Flag / shard helpers
# --------------------------------------------------------------------------- #


def admission_enabled() -> bool:
	"""Cheap conf read (no DB). The single gate that keeps flag-OFF behavior
	byte-identical: callers short-circuit to the legacy dispatch path."""
	return bool(frappe.conf.get(FLAG))


def turn_machine_enabled(from_db: bool = False, target: str | None = None) -> bool:
	"""True when a NEW turn must go through the durable turn machine
	(``accept_or_queue``): the Relay Pump is ACTIVE (it owns dispatch), OR Phase-0
	admission is on AND the pump is not configured. During pump DRAINING this is
	FALSE so new turns fall through to the pure-legacy ``_dispatch_turn`` path (no
	Turn row), coexisting with the draining pump's Turn-row turns via the dual
	signal (OAR-11). Inside ``accept_or_queue``, ``pump.pump_mode_active()`` then
	picks the pump branch vs the Phase-0 branch.

	``from_db=True`` (CDX-10): the pump predicates read the DB-authoritative shard
	``transport_mode`` ROW (caller holds the shard control-row FOR UPDATE) instead of the
	stale request-local conf. This is the FENCED re-check used at the two dispatch-deciding
	points (``_dispatch_turn``'s enqueue boundary, ``accept_or_queue`` under the lock). The
	Phase-0 flag (``jarvis_phase0_admission_enabled``) is not part of the cutover race, so it
	stays a conf read either way."""
	from jarvis.chat import pump

	if pump.pump_mode_active(from_db=from_db, target=target):
		return True
	return admission_enabled() and not pump.pump_configured(from_db=from_db, target=target)


def relay_target_id(conversation: str | None = None) -> str:
	"""The admission shard for a conversation. Phase 0: one site-wide shard."""
	return DEFAULT_RELAY_TARGET


def _max_inflight() -> int:
	try:
		v = int(frappe.conf.get("jarvis_site_max_inflight_turns") or 0)
	except (TypeError, ValueError):
		v = 0
	return v if v > 0 else DEFAULT_MAX_INFLIGHT


def _now() -> str:
	return frappe.utils.now()


def _fresh_cutoff() -> str:
	return frappe.utils.add_to_date(None, seconds=-_INFLIGHT_FRESH_SECONDS)


def _ensure_control_row(target: str) -> None:
	"""Get-or-create the shard control row OUTSIDE the serializing lock so the
	FOR UPDATE below always has a row to lock."""
	if frappe.db.exists(PUMP, target):
		return
	try:
		from jarvis.chat import pump

		# CDX-10: stamp the DB-authoritative transport_mode from config at creation so a fresh
		# shard's fenced decision value is correct from the first dispatch (never left empty).
		doc = frappe.get_doc(
			{"doctype": PUMP, "relay_target_id": target, "transport_mode": pump._config_transport_mode()}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
	except frappe.DuplicateEntryError:
		frappe.db.rollback()


def _lock_shard(target: str) -> None:
	"""Serialize the credit read + admit decision across every sender and the
	promoter for this shard (OAR-1).

	Correctness under REPEATABLE READ: the inflight/queued COUNTs that follow
	MUST see data committed by whoever held this lock before us. InnoDB
	establishes a transaction's consistent-read snapshot at its first
	NON-locking read, so we commit here to start a FRESH transaction - making
	the ``SELECT ... FOR UPDATE`` the first statement. The lock then blocks
	until the prior holder commits, and the first non-locking COUNT after it
	takes its snapshot AFTER that commit (verified on patterntest under both
	READ COMMITTED and REPEATABLE READ). Callers commit their own durable work
	BEFORE entering admission, so this commit never drops pending state."""
	_ensure_control_row(target)
	frappe.db.commit()
	frappe.db.sql(
		f"SELECT name FROM `tab{PUMP}` WHERE name=%(t)s FOR UPDATE",
		{"t": target},
	)


def on_conversation_trash(doc, method=None) -> None:
	"""doc_events hook: cascade-delete a deleted conversation's Turn rows so a
	removed conversation never leaves ghost queued/dispatching rows in the
	admission shard. Best-effort - never blocks the trash."""
	try:
		frappe.db.delete(TURN, {"conversation": doc.name})
	except Exception:
		frappe.log_error(title="admission.on_conversation_trash", message=frappe.get_traceback())


def _lock_conversation(conversation: str) -> None:
	"""Second lock in the canonical order; defends per-conversation
	single-flight / seq (OAR-6)."""
	frappe.db.sql(
		f"SELECT name FROM `tab{CONV}` WHERE name=%(c)s FOR UPDATE",
		{"c": conversation},
	)


def _run_cas(sql: str, params: dict) -> int:
	"""Run a CAS UPDATE and return affected rows (read BEFORE commit, as
	turn_recovery._conditional_clear does - commit can reset the cursor)."""
	frappe.db.sql(sql, params)
	cursor = getattr(frappe.db, "_cursor", None)
	return int(cursor.rowcount) if cursor else 0


# --------------------------------------------------------------------------- #
# Dual-signal counting (OAR-11)
# --------------------------------------------------------------------------- #


def _turn_dispatching_count(target: str) -> int:
	# Byte-identical to `state='dispatching'` when the pump is off; counts pump
	# in-flight turns during coexistence (OAR-11).
	return frappe.db.sql(
		f"SELECT COUNT(*) FROM `tab{TURN}` WHERE relay_target_id=%(t)s AND state IN ({_in_sql(_INFLIGHT_STATES)})",
		{"t": target},
	)[0][0]


def _turn_dispatching_count_by_class(target: str, turn_class: str) -> int:
	return frappe.db.sql(
		f"""SELECT COUNT(*) FROM `tab{TURN}`
		WHERE relay_target_id=%(t)s AND state IN ({_in_sql(_INFLIGHT_STATES)}) AND turn_class=%(k)s""",
		{"t": target, "k": turn_class},
	)[0][0]


def _legacy_streaming_count(target: str) -> int:
	"""Conversations whose newest assistant Message is fresh-``streaming`` and
	have NO owning ``dispatching`` Turn row - turns that started before the flag
	flipped on. The ``NOT EXISTS`` de-dups only against a ``dispatching`` Turn
	(the row that actually OWNS a stream); a merely ``queued`` Turn owns no
	stream, so it must NOT hide a live legacy turn from the shard count (OARI-2 -
	previously ``state IN ('queued','dispatching')`` let the caller's own queued
	row zero out a coexisting legacy stream). Phase-0 shard is site-wide, so
	every conversation belongs to ``target``.

	Freshness is approximated from the assistant row's own ``modified`` (rather
	than the conversation's newest-of-any-role, as api._conversation_busy uses);
	a tool-heavy turn can look briefly stale, at worst under-counting by one
	during a flag flip - a transitional signal, documented."""
	return frappe.db.sql(
		f"""
		SELECT COUNT(*) FROM (
			SELECT m.conversation
			FROM `tab{MSG}` m
			INNER JOIN (
				SELECT conversation, MAX(seq) AS mseq
				FROM `tab{MSG}` WHERE role='assistant' GROUP BY conversation
			) latest ON latest.conversation = m.conversation AND latest.mseq = m.seq
			WHERE m.role='assistant' AND m.streaming=1 AND m.recovering=0
			  AND m.modified > %(fresh)s
			  AND NOT EXISTS (
					SELECT 1 FROM `tab{TURN}` t
					WHERE t.conversation = m.conversation
					  AND t.state IN ({_in_sql(_INFLIGHT_STATES)})
			  )
		) x
		""",
		{"fresh": _fresh_cutoff()},
	)[0][0]


def _shard_inflight(target: str) -> int:
	"""Dual-signal inflight = Turn-row dispatching + legacy fresh-streaming."""
	return _turn_dispatching_count(target) + _legacy_streaming_count(target)


def _shard_queued_depth(target: str) -> int:
	return frappe.db.sql(
		f"SELECT COUNT(*) FROM `tab{TURN}` WHERE relay_target_id=%(t)s AND state='queued'",
		{"t": target},
	)[0][0]


def shard_overloaded(conversation: str | None = None) -> bool:
	"""Cheap unlocked pre-check so send_message can reject BEFORE inserting the
	user Message (avoids an orphan row). The authoritative overload check runs
	under the shard lock inside accept_or_queue."""
	return _shard_queued_depth(relay_target_id(conversation)) >= MAX_QUEUE_DEPTH


def _conv_has_other_active_turn(conversation: str, run_id: str) -> bool:
	"""Any other non-terminal Turn row on this conversation (single-flight scope).
	Byte-identical to ('queued','dispatching') when the pump is off; sees a
	pump-owned in-flight turn during coexistence (OAR-11). CDX-24: also treats a
	sibling 'recovering' Turn as blocking — a parked turn's old gateway run may
	still be live, so the conversation stays single-flight until it settles."""
	return bool(
		frappe.db.sql(
			f"""SELECT 1 FROM `tab{TURN}`
			WHERE conversation=%(c)s AND name!=%(r)s AND state IN ({_in_sql(_CONV_BLOCKING_STATES)})
			LIMIT 1""",
			{"c": conversation, "r": run_id},
		)
	)


def _conv_legacy_busy(conversation: str) -> bool:
	"""Legacy single-flight signal: a fresh streaming assistant row on this
	conversation (a turn that started before the flag flipped on, or a mid-flight
	self-hop). At every call site the *current* turn has no assistant row of its
	own yet (accept: the queued row was just inserted; promote: the candidate is
	queued), and a Turn-owned ``dispatching`` turn is already caught by
	``_conv_has_other_active_turn``, so a plain ``_conversation_busy`` is the
	correct legacy probe.

	OARI-2: the previous body subtracted "our own Turn row" via
	``_conv_has_other_active_turn(conversation, run_id="")`` - but the ``""``
	sentinel made ``name != ''`` exclude nothing, so the inner query counted the
	just-inserted queued row and always returned True, making this guard return
	False (dead) at BOTH call sites. Dropping the subtraction restores the
	OAR-11 coexistence interlock.

	CDX-24: OR a fresh ``recovering=1`` legacy assistant Message with no owning
	Turn row (the legacy dual-signal mirror of the Turn-row recovering block) — a
	legacy turn parked recovering still has its old gateway run owned by
	turn_recovery, so its conversation must stay single-flight-blocked here even
	though ``_conversation_busy`` deliberately UNLOCKS the composer for it."""
	from jarvis.chat.api import _conversation_busy

	return _conversation_busy(conversation) or _conv_legacy_recovering_busy(conversation)


def _conv_legacy_recovering_busy(conversation: str) -> bool:
	"""CDX-24 legacy dual-signal: True when this conversation's newest assistant
	Message is ``streaming=1 AND recovering=1`` with NO owning blocking Turn row.
	Mirrors the Turn-row ``recovering`` single-flight block for the pure-legacy /
	coexistence case that ``api._conversation_busy`` intentionally treats as free
	(the composer is unlocked while recovering, but a NEW admission dispatch must
	not start a second run on the same session). turn_recovery resolves the parked
	run — ``_finalize``/``_error`` clear ``streaming`` — so the block lifts exactly
	when recovery proves the old run terminal (ceiling-bounded, never wall-clock).
	The ``NOT EXISTS`` de-dups against an owning Turn already caught by
	``_conv_has_other_active_turn`` (this signal is for the Turn-less legacy park)."""
	rows = frappe.db.sql(
		f"""SELECT streaming, recovering FROM `tab{MSG}`
		WHERE conversation=%(c)s AND role='assistant'
		ORDER BY seq DESC LIMIT 1""",
		{"c": conversation},
		as_dict=True,
	)
	if not rows or not (rows[0].get("streaming") and rows[0].get("recovering")):
		return False
	# De-dup against a Turn that OWNS a run (in-flight or recovering) — that case is
	# already caught by _conv_has_other_active_turn. A merely 'queued' Turn owns no run
	# (it is the candidate waiting BEHIND the recovering message), so it must NOT hide
	# the Turn-less legacy park from this signal (mirrors _legacy_streaming_count's
	# NOT EXISTS, which de-dups only against _INFLIGHT_STATES).
	return not bool(
		frappe.db.sql(
			f"""SELECT 1 FROM `tab{TURN}`
			WHERE conversation=%(c)s AND state IN ({_in_sql(_INFLIGHT_STATES + ("recovering",))}) LIMIT 1""",
			{"c": conversation},
		)
	)


# --------------------------------------------------------------------------- #
# Position / publishing
# --------------------------------------------------------------------------- #


def _queued_ordered(target: str) -> list[dict]:
	return frappe.db.sql(
		f"""
		SELECT t.run_id, t.conversation, t.seed_message, t.turn_class, c.owner
		FROM `tab{TURN}` t
		INNER JOIN `tab{CONV}` c ON c.name = t.conversation
		WHERE t.relay_target_id=%(t)s AND t.state='queued'
		ORDER BY CASE t.turn_class WHEN 'interactive' THEN 0 ELSE 1 END,
		         t.enqueued_at ASC, t.run_id ASC
		""",
		{"t": target},
		as_dict=True,
	)


def _position_of(run_id: str, target: str) -> int | None:
	"""1-based rank among queued turns of the shard (interactive ahead of
	background, then FIFO). None when the run is not queued."""
	for i, r in enumerate(_queued_ordered(target), start=1):
		if r["run_id"] == run_id:
			return i
	return None


def _publish_queue_positions(target: str) -> None:
	"""Push queue:position to every queued turn's owner (bounded fan-out,
	SUX-2). Position labeled approximate in the UI ('~N ahead')."""
	for i, r in enumerate(_queued_ordered(target), start=1):
		try:
			publish_to_user(
				r["owner"],
				{
					"kind": "queue:position",
					"conversation_id": r["conversation"],
					"run_id": r["run_id"],
					"message_id": r["seed_message"],
					"position": i,
				},
			)
		except Exception:
			pass


def publish_action_confirmed(conversation: str, owner: str | None = None, run_id: str | None = None) -> None:
	"""SUX-3: on a synchronous confirm write, tell the user 'change saved'
	immediately - decoupled from the continuation turn, which then renders the
	standard queued chip. Best-effort. Flag-gated so flag-OFF stays byte-
	identical (no extra realtime event)."""
	if not admission_enabled():
		return
	try:
		user = owner or frappe.db.get_value(CONV, conversation, "owner")
		if not user:
			return
		publish_to_user(
			user,
			{
				"kind": "action:confirmed",
				"conversation_id": conversation,
				"run_id": run_id or "",
				"message": "Change saved",
			},
		)
	except Exception:
		pass


# --------------------------------------------------------------------------- #
# Telemetry (feeds C1-C6 later; existing latency channel + log lines)
# --------------------------------------------------------------------------- #


def _telemetry(event: str, **fields) -> None:
	try:
		from jarvis.chat.latency import get_logger

		parts = " ".join(f"{k}={v}" for k, v in fields.items())
		get_logger().info("admission %s %s", event, parts)
	except Exception:
		pass


# --------------------------------------------------------------------------- #
# accept_or_queue - the one chokepoint (all four _dispatch_turn callers)
# --------------------------------------------------------------------------- #


def accept_or_queue(
	*,
	conversation: str,
	run_id: str,
	seed_message: str | None,
	turn_class: str = "interactive",
	dispatch,
	dispatch_payload: dict | None = None,
	seed_content: str | None = None,
	exempt_overload: bool = False,
) -> dict:
	"""Admit or durably queue one turn. Returns one of:

	  {"ok": True, "dispatched": True,  "run_id", "queued_position": None}
	  {"ok": True, "dispatched": False, "run_id", "queued_position": N}
	  {"ok": False, "overloaded": True, "reason": <friendly copy>}

	``seed_message`` is the already-committed user Message (send/retry/orphan/
	macro all insert it before dispatch today - OAR-3's retry/orphan reuse is
	automatic here since no caller asks admission to insert). ``seed_content``
	+ ``seed_message=None`` is the WP-1 insert branch, wired but unused in
	Phase-0's legacy integration.

	``exempt_overload`` (SUXI-2 ruling): a confirm continuation is the follow-up
	of an ALREADY-committed write - it must never be rejected by the accept-time
	overload guard, only queued. The front-door senders (send/retry) keep the
	depth-25 backpressure; ``enqueue_continuation`` passes True so a continuation
	always queues with a visible position, never silently drops.

	``dispatch`` is a zero-arg callable that runs the legacy ``_dispatch_turn``
	for this turn; it is invoked ONLY on the admit path and ONLY after the
	admission txn commits."""
	from jarvis.chat import pump

	target = relay_target_id(conversation)
	turn_class = turn_class if turn_class in ("interactive", "background") else "interactive"
	payload_json = json.dumps(dispatch_payload) if dispatch_payload else None

	_lock_shard(target)
	_lock_conversation(conversation)
	# CDX-10 (DB-authoritative): read the transport decision from the shard control ROW under
	# the lock we now hold — NOT the request-local conf, which frappe.init froze at request start
	# and update_site_config never refreshes, so it can be stale across a cutover. The SAME row
	# pump_cutover_execute flips under this lock, so no mode change can interleave between the
	# branch decision and the durable insert below.
	#
	# Sweep note (contention): read the row's transport_mode ONCE and derive BOTH decisions from
	# it, rather than turn_machine_enabled(from_db) + pump_mode_active(from_db) each re-reading
	# the row (three indexed reads) while the site-wide admission lock is held.
	_pred = pump.transport_predicates_from_row(target)
	pump_mode = _pred["pump_active"]
	machine_active = pump_mode or (admission_enabled() and not _pred["configured"])

	# OARI-11: everything below runs while holding the shard + conversation FOR
	# UPDATE locks. The designed early-returns (legacy fallback, overload reject,
	# duplicate replay) roll back/commit explicitly before returning; an UNEXPECTED
	# failure must also release the locks immediately (not linger until request/job
	# teardown), so the whole locked section is guarded - rollback + re-raise (never mask).
	try:
		if not machine_active:
			# CDX-10 (reverse direction): the world reverted to pure-legacy (kill switch back on
			# AND Phase-0 off) while this sender waited on the shard lock — its stale conf still said
			# machine-ON, which is why it entered accept_or_queue. Creating a machine Turn now would
			# STRAND it: with both the pump and Phase-0 off, nothing promotes a queued row (it would
			# age out ~15min later). Instead enqueue the legacy job under the lock we hold (so a
			# concurrent cutover scan sees it — same guarantee as _dispatch_turn's gate) and commit;
			# no Turn row is created. The seed user Message already exists (every caller inserts it
			# before dispatch), so nothing is orphaned.
			dispatch()
			frappe.db.commit()  # releases both locks; the legacy job is durably enqueued
			_telemetry("accept_legacy_fallback", run_id=run_id, target=target)
			return {
				"ok": True,
				"dispatched": True,
				"run_id": run_id,
				"queued_position": None,
				"legacy_fallback": True,
			}

		# Overload guard (SUX-5): authoritative check under the shard lock. No row.
		# A confirm continuation (exempt_overload) skips it - it always queues.
		if not exempt_overload and _shard_queued_depth(target) >= MAX_QUEUE_DEPTH:
			frappe.db.rollback()
			_telemetry("overload_reject", run_id=run_id, target=target)
			return {
				"ok": False,
				"overloaded": True,
				"reason": frappe._("The site is busy — please try again in a moment."),
			}

		# Seed the user Message if the caller delegated it (WP-1 branch; unused in
		# Phase-0 wiring because every legacy caller inserts before dispatch).
		if not seed_message:
			seq = (
				frappe.db.sql(
					f"SELECT MAX(seq) FROM `tab{MSG}` WHERE conversation=%(c)s", {"c": conversation}
				)[0][0]
				or 0
			) + 1
			msg = frappe.get_doc(
				{
					"doctype": MSG,
					"conversation": conversation,
					"seq": seq,
					"role": "user",
					"content": seed_content or "",
					"streaming": 0,
				}
			)
			msg.flags.ignore_permissions = True
			msg.insert()
			seed_message = msg.name

		# Insert the durable Turn row (idempotent on the run_id PK).
		try:
			turn = frappe.get_doc(
				{
					"doctype": TURN,
					"run_id": run_id,
					"conversation": conversation,
					"relay_target_id": target,
					"turn_class": turn_class,
					"state": "queued",
					"version": 0,
					"seed_message": seed_message,
					"dispatch_payload": payload_json,
					"enqueued_at": _now(),
				}
			)
			turn.flags.ignore_permissions = True
			turn.insert()
		except frappe.DuplicateEntryError:
			# Same send replayed (double-click / retry-after-ack): already accepted.
			frappe.db.rollback()
			return {
				"ok": True,
				"dispatched": False,
				"run_id": run_id,
				"duplicate": True,
				"queued_position": None,
			}

		if pump_mode:
			# Relay-Pump mode: the pump OWNS dispatch. Leave the turn 'queued'
			# (reserved=0, pump_epoch=0); the pump reserves the credit at the
			# queued->preparing PROMOTION point under the shard lock (D3 Race 2) and
			# drives prepare->dispatch->settle. NO Phase-0 admit CAS, NO legacy
			# dispatch(). A UI hint (will_dispatch) mirrors the pump's OWN promote
			# order WITHOUT the CAS — the pump is authoritative.
			#
			# F2 (will_dispatch race): the pump leaves EVERY accepted turn 'queued'
			# and only promotes it to an inflight state on a later slice, so an
			# occupier turn A can still be plain 'queued' (uncounted by
			# _shard_inflight, which counts only _INFLIGHT_STATES) at the instant a
			# second turn B is accepted. The OLD hint compared only
			# `_shard_inflight < cap`, so it wrongly told B's sender "dispatched" —
			# B's tab then sat on the "Working on it…" placeholder for the whole
			# queue wait (never rendering the queued chip), while a RELOAD read the
			# durable 'queued' row and showed the chip correctly (the reported split).
			# The fix uses B's RANK among the shard's queued turns (interactive-first
			# FIFO — the pump's own promote order): B dispatches immediately iff a
			# free credit reaches its position, i.e. inflight + position <= cap. This
			# never claims immediate dispatch when a still-'queued' occupier sits
			# ahead of B. `_position_of` sees B's own (uncommitted) queued insert in
			# this same transaction, and the shard lock (held until the commit below)
			# blocks any concurrent accept/promote, so the rank is stable.
			conv_busy = _conv_has_other_active_turn(conversation, run_id) or _conv_legacy_busy(conversation)
			position = _position_of(run_id, target)
			inflight = _shard_inflight(target)
			will_dispatch = (
				(not conv_busy) and position is not None and (inflight + position <= _max_inflight())
			)
			frappe.db.commit()  # releases both locks; row durable
		else:
			# Admit decision: dispatch iff nothing else is live on this conversation
			# (single-flight) AND the shard has a free credit.
			conv_busy = _conv_has_other_active_turn(conversation, run_id) or _conv_legacy_busy(conversation)
			inflight = _shard_inflight(target)
			queue_depth_at_accept = _shard_queued_depth(target)
			admit = (not conv_busy) and (inflight < _max_inflight())

			if admit:
				affected = _run_cas(
					f"""UPDATE `tab{TURN}`
					SET state='dispatching', reserved=1, dispatching_at=%(now)s,
					    reservation_expires_at=%(exp)s, version=version+1
					WHERE name=%(r)s AND state='queued' AND version=0""",
					{
						"r": run_id,
						"now": _now(),
						"exp": frappe.utils.add_to_date(None, seconds=RESERVE_TTL_S),
					},
				)
				if affected != 1:
					# Lost a race to a concurrent promoter (should not happen under the
					# shard lock); fall back to queued and let promotion pick it up.
					admit = False

			frappe.db.commit()  # releases both locks; row is durable
	except Exception:
		frappe.db.rollback()  # release the FOR UPDATE locks on an unexpected failure
		raise

	if pump_mode:
		# Wake the pump AFTER the durable commit (§8-E PRIMARY start path). ensure_pump
		# is MariaDB-authoritative; lpush_wake is a best-effort tick (the pump scans
		# queued rows on wake + every watchdog tick regardless).
		_telemetry("accept_pump", run_id=run_id, target=target, turn_class=turn_class)
		pump.ensure_pump(target)
		pump.lpush_wake(target, run_id)
		# Reuse the rank computed under the lock (the row is unchanged — the pump's
		# promote is serialized on the same shard lock we just released).
		pos = None if will_dispatch else position
		return {
			"ok": True,
			"dispatched": bool(will_dispatch),
			"run_id": run_id,
			"queued_position": pos,
			"pump": True,
		}

	_telemetry(
		"accept",
		run_id=run_id,
		target=target,
		turn_class=turn_class,
		admitted=int(admit),
		admission_queue_depth=queue_depth_at_accept,
	)

	if admit:
		# Enqueue the legacy turn job AFTER the durable commit (mirrors the
		# _dispatch_turn after_commit invariant).
		try:
			dispatch()
		except Exception:
			# CDX-9: enqueue failed after the admission commit — compensate so the
			# credit is not stranded until the reservation TTL, and return the turn as
			# durably queued (a sweep/promote will re-dispatch it) instead of a false
			# "dispatched".
			frappe.log_error(title="admission.accept dispatch", message=frappe.get_traceback())
			_compensate_failed_dispatch(run_id, target)
			pos = _position_of(run_id, target)
			return {
				"ok": True,
				"dispatched": False,
				"run_id": run_id,
				"queued_position": pos,
				"dispatch_failed": True,
			}
		return {"ok": True, "dispatched": True, "run_id": run_id, "queued_position": None}

	pos = _position_of(run_id, target)
	# A new queued turn changes nobody else's position, so no shard-wide
	# republish is needed here; the caller returns this turn's own position.
	return {"ok": True, "dispatched": False, "run_id": run_id, "queued_position": pos}


# --------------------------------------------------------------------------- #
# promote_next - weighted, background-floor, per-conversation single-flight
# --------------------------------------------------------------------------- #


def _cas_dispatch(run_id: str) -> bool:
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='dispatching', reserved=1, dispatching_at=%(now)s,
			    reservation_expires_at=%(exp)s, version=version+1
			WHERE name=%(r)s AND state='queued'""",
			{
				"r": run_id,
				"now": _now(),
				"exp": frappe.utils.add_to_date(None, seconds=RESERVE_TTL_S),
			},
		)
		== 1
	)


def _compensate_failed_dispatch(run_id: str, target: str) -> None:
	"""CDX-9: an enqueue (RQ/Redis) failure AFTER the admission/promotion commit leaves
	the row ``dispatching`` + ``reserved`` with no job behind it, stranding a shard
	credit until the ~900s reservation TTL + the periodic sweep. Compensate
	immediately: CAS the exact ``dispatching`` attempt back to ``queued``, CLEAR the
	reservation/expiry, and republish positions so a waiting sender sees the freed
	slot. Version-fenced on ``state='dispatching'`` so a turn that meanwhile started
	streaming (won't happen on the same run in Phase-0, but defensive) is untouched.
	Best-effort — never raises into the caller."""
	try:
		affected = _run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='queued', reserved=0, dispatching_at=NULL,
			    reservation_expires_at=NULL, version=version+1
			WHERE name=%(r)s AND state='dispatching'""",
			{"r": run_id},
		)
		frappe.db.commit()
		if affected == 1:
			try:
				_publish_queue_positions(target)
			except Exception:
				pass
	except Exception:
		try:
			frappe.db.rollback()
		except Exception:
			pass
		frappe.log_error(title="admission.compensate_failed_dispatch", message=frappe.get_traceback())


def _dispatch_promoted(row: dict) -> None:
	"""Re-dispatch a promoted queued turn via the legacy path, reconstructing
	attachments/context from the stored dispatch_payload."""
	from jarvis.chat import api

	kwargs = {
		"conversation_id": row["conversation"],
		"message_id": row["seed_message"],
		"run_id": row["run_id"],
		"enqueued_at_ms": int(time.time() * 1000),
	}
	extra = row.get("dispatch_payload")
	if extra:
		try:
			parsed = json.loads(extra)
			if isinstance(parsed, dict):
				if parsed.get("attachments"):
					kwargs["attachments"] = parsed["attachments"]
				if parsed.get("context"):
					kwargs["context"] = parsed["context"]
		except Exception:
			pass
	api._dispatch_turn(kwargs, interactive=(row.get("turn_class") == "interactive"))


def promote_next(target: str | None = None) -> int:
	"""Dispatch as many eligible queued turns as free credits allow. Weighted
	classes with a background floor of 1 (SUX-4a): when background turns are
	queued, interactive holds at most ``ceiling - 1`` credits. Per-conversation
	single-flight respected. Returns the number promoted. Best-effort - never
	raises into a terminal hook.

	OARI-4: flag-gated so a flag-OFF sweep never issues a FRESH dispatch. With
	the flag off, existing Turn rows still drain to terminal via the sweep's
	reconcile/age-out duties (which only settle/cancel existing rows), but no
	queued row is ever promoted onto a worker - disabling admission is a true
	kill switch for new dispatch, not just for new rows.

	WP-1d: once the Relay Pump is configured (active OR draining) it OWNS every
	Jarvis Chat Turn row and drives its own promote/reconcile via the pump
	watchdog; Phase-0 must NOT legacy-dispatch a pump-owned queued turn, so this
	steps back entirely.

	CDX-21 (Residual A): the step-back reads the AUTHORITATIVE shard ROW
	(``pump_lifecycle_configured``, the same 5s row cache the pump lifecycle gates use),
	NOT the ``site_config`` mirror. So a stale/failed mirror can never make Phase-0
	legacy-dispatch a queued row the row-authoritative pump already owns (row=pump/config=legacy),
	nor suppress Phase-0's own promotion when the row is the ``legacy`` kill switch
	(row=legacy/config=pump). The row decides ownership everywhere."""
	from jarvis.chat import pump

	if target is None:
		target = DEFAULT_RELAY_TARGET
	if pump.pump_lifecycle_configured(target):
		return 0
	if not admission_enabled():
		return 0
	promoted_rows: list[dict] = []
	try:
		_lock_shard(target)
		cap = _max_inflight()
		promoted_convs: set[str] = set()

		while _shard_inflight(target) < cap:
			candidate = _pick_next(target, cap, promoted_convs)
			if not candidate:
				break
			if _cas_dispatch(candidate["run_id"]):
				promoted_rows.append(candidate)
				promoted_convs.add(candidate["conversation"])
			# else lost the row; loop re-reads and tries the next candidate

		frappe.db.commit()  # release the shard lock before enqueueing
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="admission.promote_next", message=frappe.get_traceback())
		return 0

	for row in promoted_rows:
		try:
			wait_ms = _queue_wait_ms(row["run_id"])
			_dispatch_promoted(row)
			_telemetry("promote", run_id=row["run_id"], target=target, queue_wait_ms=wait_ms)
		except Exception:
			# CDX-9: enqueue failed after the promotion commit — compensate (CAS
			# dispatching->queued + release the credit) instead of only logging, so the
			# promoted-but-not-dispatched credit is not stranded until the sweep.
			frappe.log_error(title="admission.promote dispatch", message=frappe.get_traceback())
			_compensate_failed_dispatch(row["run_id"], target)

	# Positions shifted for everyone still queued.
	try:
		_publish_queue_positions(target)
	except Exception:
		pass
	return len(promoted_rows)


def _pick_next(target: str, cap: int, promoted_convs: set[str]) -> dict | None:
	"""Choose the next queued turn to promote, honoring the background floor
	and per-conversation single-flight. Reads a small ordered batch and filters
	in Python (bounded work: at most a handful of credits per round)."""
	rows = frappe.db.sql(
		f"""
		SELECT t.run_id, t.conversation, t.seed_message, t.turn_class, t.dispatch_payload
		FROM `tab{TURN}` t
		WHERE t.relay_target_id=%(t)s AND t.state='queued'
		ORDER BY t.enqueued_at ASC, t.run_id ASC
		LIMIT 200
		""",
		{"t": target},
		as_dict=True,
	)

	def eligible(r: dict) -> bool:
		conv = r["conversation"]
		if conv in promoted_convs:
			return False
		if _conv_has_other_active_turn(conv, r["run_id"]):
			return False
		if _conv_legacy_busy(conv):
			return False
		return True

	interactive = next((r for r in rows if r["turn_class"] == "interactive" and eligible(r)), None)
	background = next((r for r in rows if r["turn_class"] == "background" and eligible(r)), None)

	if interactive and background:
		# Background floor: reserve at least one credit for background work when
		# any is queued - interactive may hold at most cap-1 credits. Only for
		# cap>=2: at cap 1 the floor "int_inflight >= cap-1" degenerates to ">= 0"
		# (always true) and would hand the SOLE credit to background over a
		# waiting interactive turn, inverting priority (OARI-8). With one credit
		# there is no room to reserve a floor, so interactive wins.
		int_inflight = _turn_dispatching_count_by_class(target, "interactive")
		if cap >= 2 and int_inflight >= cap - 1:
			return background
		return interactive
	return interactive or background


def _queue_wait_ms(run_id: str) -> int:
	row = frappe.db.get_value(TURN, run_id, ["enqueued_at", "dispatching_at"], as_dict=True)
	if not row or not row.get("enqueued_at") or not row.get("dispatching_at"):
		return 0
	try:
		delta = frappe.utils.get_datetime(row["dispatching_at"]) - frappe.utils.get_datetime(
			row["enqueued_at"]
		)
		return int(delta.total_seconds() * 1000)
	except Exception:
		return 0


# --------------------------------------------------------------------------- #
# Terminal hooks (worker + recovery) - CAS + best-effort promote
# --------------------------------------------------------------------------- #


def settle_turn(run_id: str, terminal_state: str, error: str | None = None) -> None:
	"""Mark a dispatching Turn terminal (done/errored/cancelled) then promote.
	Called AFTER the legacy terminal commit points, OUTSIDE the brittle region.
	Guarded by the flag so flag-OFF is byte-identical. Best-effort: never
	breaks the turn."""
	if not admission_enabled():
		return
	try:
		affected = _run_cas(
			f"""UPDATE `tab{TURN}`
			SET state=%(s)s, reserved=0, cancel_requested=0, done_at=%(now)s,
			    error=%(err)s, version=version+1
			WHERE name=%(r)s AND state='dispatching'""",
			{"s": terminal_state, "now": _now(), "err": (error or "")[:1000], "r": run_id},
		)
		target = frappe.db.get_value(TURN, run_id, "relay_target_id") or DEFAULT_RELAY_TARGET
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="admission.settle_turn", message=frappe.get_traceback())
		return
	if affected == 1:
		promote_next(target)


def settle_conversation_dispatching(conversation: str, terminal_state: str, error: str | None = None) -> None:
	"""Recovery-path settle: close the (single-flight) dispatching Turn on this
	conversation. turn_recovery works off Message rows and has no run_id, so we
	settle by conversation - per-conversation single-flight makes this
	unambiguous. Best-effort + flag-gated."""
	if not admission_enabled():
		return
	try:
		run_id = frappe.db.get_value(TURN, {"conversation": conversation, "state": "dispatching"}, "name")
	except Exception:
		run_id = None
	if run_id:
		settle_turn(run_id, terminal_state, error=error)


def mark_cancel_requested(conversation: str) -> None:
	"""stop_run hook: record cancel intent on the dispatching Turn (D2's
	dispatching->cancel-intent transition). NOT terminal - the legacy worker's
	aborted-terminal settles + promotes. Flag-gated, best-effort."""
	if not admission_enabled():
		return
	try:
		run_id = frappe.db.get_value(TURN, {"conversation": conversation, "state": "dispatching"}, "name")
		if run_id:
			_run_cas(
				f"""UPDATE `tab{TURN}` SET cancel_requested=1, version=version+1
				WHERE name=%(r)s AND state='dispatching'""",
				{"r": run_id},
			)
			frappe.db.commit()
	except Exception:
		frappe.db.rollback()


# --------------------------------------------------------------------------- #
# Web endpoints - cancel + poll position
# --------------------------------------------------------------------------- #


def _assert_owner(conversation: str) -> str:
	owner = frappe.db.get_value(CONV, conversation, "owner")
	if owner is None:
		raise frappe.DoesNotExistError(f"conversation {conversation!r} not found")
	if owner != frappe.session.user and "System Manager" not in frappe.get_roles():
		raise frappe.PermissionError("not your conversation")
	return owner


@frappe.whitelist()
def cancel_queued_turn(run_id: str) -> dict:
	"""Owner-checked cancel of a pre-dispatch turn, ROUTED BY STATE (CDX-8):

	  * ``queued``            -> ``turn_state.cancel_queued`` (version-fenced): CAS to
	                            ``cancelled`` CLEARING ``reserved`` + ``reservation_
	                            expires_at`` atomically. A reserved-but-unclaimed queued
	                            turn (the pump reserved a credit before prepare) would
	                            otherwise leak that credit until the ~900s reservation
	                            expiry — the reported leak.
	  * ``preparing``/``ready`` -> ``turn_state.cancel_preparing_or_ready`` (version-
	                            fenced): CAS to ``cancelled`` (also clearing the
	                            reservation) + clean the assistant placeholder so no
	                            stuck spinner. The pump owns dispatch but nothing is in
	                            flight yet, so no gateway abort is needed; the losing
	                            prepare's ``mark_ready`` then affects 0 rows.

	Returns ``{"ok": True, "run_id", "path": "queued"|"preparing_ready"}`` so the
	caller/UI knows which path won; ``{"ok": False, ...}`` when the turn already
	advanced (e.g. dispatched) — the UI then KEEPS its chip until the server confirms
	(no optimistic clear on failure)."""
	require_jarvis_access()
	row = frappe.db.get_value(
		TURN,
		run_id,
		["conversation", "state", "version", "relay_target_id", "seed_message", "assistant_message"],
		as_dict=True,
	)
	if not row:
		return {"ok": False, "reason": frappe._("This turn no longer exists.")}
	owner = _assert_owner(row["conversation"])
	from jarvis.chat import turn_state as ts

	state = row["state"]
	version = int(row["version"] or 0)
	path = None
	if state == "queued":
		if ts.cancel_queued(run_id, version):
			path = "queued"
	elif state in ("preparing", "ready"):
		if ts.cancel_preparing_or_ready(run_id, version):
			path = "preparing_ready"
			# Clean the assistant placeholder prepare attached so a reload never shows
			# a stuck spinner for a cancelled turn (credit already released by the CAS).
			if row.get("assistant_message"):
				try:
					_run_cas(
						f"UPDATE `tab{MSG}` SET streaming=0 WHERE name=%(m)s",
						{"m": row["assistant_message"]},
					)
				except Exception:
					pass
	frappe.db.commit()
	if path is None:
		return {"ok": False, "reason": frappe._("This turn already started or was cancelled.")}
	try:
		publish_to_user(
			owner,
			{
				"kind": "turn:cancelled",
				"conversation_id": row["conversation"],
				"run_id": run_id,
				"message_id": row["seed_message"],
				"reason": USER_CANCEL_REASON,
			},
		)
	except Exception:
		pass
	# SUXI-4: durable transcript marker so a later reload shows the send was
	# cancelled (not silently dropped).
	_write_cancel_marker(row["conversation"], USER_CANCEL_REASON)
	target = row["relay_target_id"] or DEFAULT_RELAY_TARGET
	# The freed credit lets the next queued turn run. In pump mode wake the pump to
	# re-promote (promote_next is a no-op there); else best-effort legacy promote.
	# CDX-21 (Residual A): the pump-vs-legacy wake decision reads the AUTHORITATIVE shard ROW
	# (``pump_lifecycle_configured``), NOT the config mirror — so a stale mirror can never route the
	# credit-freed wake to the wrong owner.
	from jarvis.chat import pump

	if pump.pump_lifecycle_configured(target):
		try:
			pump.ensure_pump(target)
			pump.lpush_wake(target, run_id)
			_publish_queue_positions(target)
		except Exception:
			pass
	else:
		promote_next(target)
	return {"ok": True, "run_id": run_id, "path": path}


@frappe.whitelist()
def queue_position(run_id: str) -> dict:
	"""Poll-on-focus fallback for the queued chip. Owner-checked. Returns the
	current state and (when queued) the approximate position."""
	require_jarvis_access()
	row = frappe.db.get_value(TURN, run_id, ["conversation", "state", "relay_target_id"], as_dict=True)
	if not row:
		return {"ok": False, "reason": frappe._("This turn no longer exists.")}
	_assert_owner(row["conversation"])
	pos = (
		_position_of(run_id, row["relay_target_id"] or DEFAULT_RELAY_TARGET)
		if row["state"] == "queued"
		else None
	)
	return {"ok": True, "run_id": run_id, "state": row["state"], "position": pos}


@frappe.whitelist()
def active_turn_for_conversation(conversation: str) -> dict:
	"""SUXI-1 server-truth resync for the queued chip. Owner-checked. Returns the
	conversation's own QUEUED turn (the one the chip represents), if any, so a
	client that lost ``queuedTurn`` (reload, conversation switch, second tab, WS
	reconnect) can rebuild it from server truth - the same pattern as
	``resyncPendingConfirmations``. Conversations are owner-scoped, so this turn
	IS the caller's own. A ``dispatching`` turn is deliberately NOT returned here:
	loadConversation's existing streaming resume already shows its "Thinking…"
	state from the assistant placeholder; the chip is only for a not-yet-running
	turn. Returns ``{"ok": True, "active": None}`` when nothing is pending.
	The earliest-enqueued pending turn (position closest to 1) is reported.

	SUXF-3: the pump adds a ``preparing``/``ready`` window between ``queued`` and
	the stream (prompt assembly + session bootstrap). Those states are returned too
	(with ``state`` so the chip reads "Starting…" via TURN_STATE_COPY) so a reload /
	focus resync during that window keeps the chip + composer lock instead of going
	silent and re-enabling an empty composer. Position is reported only for a still
	``queued`` turn (meaningless once a credit is held)."""
	require_jarvis_access()
	_assert_owner(conversation)
	row = frappe.db.get_value(
		TURN,
		{"conversation": conversation, "state": ["in", ("queued", "preparing", "ready")]},
		["run_id", "seed_message", "relay_target_id", "state"],
		as_dict=True,
		order_by="enqueued_at asc",
	)
	if not row:
		return {"ok": True, "active": None}
	pos = (
		_position_of(row["run_id"], row["relay_target_id"] or DEFAULT_RELAY_TARGET)
		if row["state"] == "queued"
		else None
	)
	return {
		"ok": True,
		"active": {
			"run_id": row["run_id"],
			"state": row["state"],
			"message_id": row["seed_message"],
			"position": pos,
		},
	}


# --------------------------------------------------------------------------- #
# Cutover preflight (CDX-10) — first-deploy legacy-overlap detector
# --------------------------------------------------------------------------- #


def _legacy_turn_jobs() -> tuple[int, list[str], bool]:
	"""CDX-10: scan every RQ queue (queued + started registries) for THIS site's
	legacy ``jarvis-turn::*`` jobs. Frappe namespaces a job id as
	``"<site>||<job_id-with-colons-as-pipes>"`` (``create_job_id``), so a legacy
	``jarvis-turn::<msg>::a<n>`` id becomes ``<site>||jarvis-turn|<msg>|a<n>`` — we
	match that prefix.

	Returns ``(count, job_ids[:20], scan_ok)``. CDX-10 fail-CLOSED: ANY scan exception
	(the outer ``get_queues`` OR any per-queue/registry probe) sets ``scan_ok=False`` so
	the preflight cannot report ``clear=True`` on an incomplete scan (a swallowed probe
	error must never green-light a cutover that could overlap an invisible legacy job)."""
	prefix = f"{frappe.local.site}||jarvis-turn|"
	found: set[str] = set()
	scan_ok = True
	try:
		from frappe.utils.background_jobs import get_queues
		from rq.registry import StartedJobRegistry

		for q in get_queues():
			ids: list = []
			try:
				ids += list(q.get_job_ids() or [])
			except Exception:
				scan_ok = False
			try:
				ids += list(StartedJobRegistry(queue=q).get_job_ids() or [])
			except Exception:
				scan_ok = False
			for jid in ids:
				if isinstance(jid, bytes):
					jid = jid.decode()
				if isinstance(jid, str) and jid.startswith(prefix):
					found.add(jid)
	except Exception:
		scan_ok = False
		frappe.log_error(title="admission.pump_cutover_preflight scan", message=frappe.get_traceback())
	ordered = sorted(found)
	return len(ordered), ordered[:20], scan_ok


@frappe.whitelist()
def pump_cutover_preflight(relay_target: str | None = None) -> dict:
	"""CDX-10 — the MANDATORY first-deploy cutover preflight. Before removing the
	explicit ``jarvis_pump_enabled=0`` kill switch to enter the default-ON pump, run
	``bench --site <site> execute jarvis.chat.admission.pump_cutover_preflight`` to
	detect INVISIBLE legacy activity that a fresh pump start could overlap (violating
	per-conversation ordering / the container cap):

	  * queued OR started legacy ``jarvis-turn::*`` RQ jobs — a pre-upgrade job that has
	    no Turn row AND no assistant placeholder yet (it creates the placeholder only
	    after the worker starts), so neither the pump reconcile nor the dual signal can
	    see it;
	  * fresh legacy streaming assistant messages with no owning Turn row (a legacy
	    turn already mid-stream).

	Returns a verdict + counts. ``clear=True`` (verdict ``"clear"``) ⇒ no invisible
	legacy activity — safe to unset the kill switch and enter default-ON. ``clear=
	False`` ⇒ either ``"drain_first"`` (legacy activity present — drain the listed jobs /
	let the streams finish, PUMP-RUNBOOK §2) or ``"scan_failed"`` (CDX-10 fail-CLOSED: a
	queue/registry probe raised, so the scan is INCOMPLETE and cannot be trusted to say
	clear — resolve the RQ/redis fault and re-run). System-Manager gated (so ``bench
	execute`` as Administrator works)."""
	frappe.only_for("System Manager")
	target = relay_target or DEFAULT_RELAY_TARGET
	legacy_jobs, job_ids, scan_ok = _legacy_turn_jobs()
	# CDX-10 fail-CLOSED: the streaming scan can also fault; treat its failure as unknown.
	try:
		legacy_streaming = _legacy_streaming_count(target)
	except Exception:
		scan_ok = False
		legacy_streaming = -1
		frappe.log_error(
			title="admission.pump_cutover_preflight streaming scan", message=frappe.get_traceback()
		)
	clear = scan_ok and legacy_jobs == 0 and legacy_streaming == 0
	if not scan_ok:
		verdict = "scan_failed"
	elif clear:
		verdict = "clear"
	else:
		verdict = "drain_first"
	result = {
		"ok": True,
		"clear": clear,
		"verdict": verdict,
		"scan_ok": scan_ok,
		"legacy_jobs": legacy_jobs,
		"legacy_streaming": legacy_streaming,
		"job_ids": job_ids,
		"relay_target": target,
	}
	if not scan_ok:
		result["error"] = "cutover preflight scan incomplete (RQ/redis probe failed) — failing closed"
	_telemetry(
		"cutover_preflight",
		target=target,
		legacy_jobs=legacy_jobs,
		legacy_streaming=legacy_streaming,
		scan_ok=int(scan_ok),
	)
	return result


@frappe.whitelist()
def pump_cutover_execute(relay_target: str | None = None) -> dict:
	"""CDX-10/CDX-21 — ONE cutover PASS that encodes the safe protocol (the loop is the
	operator's; this method is one pass). It closes the TOCTOU window the read-only
	preflight left open (a legacy job arriving AFTER ``clear=True`` and BEFORE the flip):

	  1. **Preflight.** If NOT clear (``drain_first`` / ``scan_failed``) => return
	     ``done=False`` with the verdict; the operator drains / fixes the fault and
	     re-runs. The mode is left untouched.
	  2. **Flip the ROW** (``transport_mode`` -> ``pump``, ``mode_epoch``+1) under the lock,
	     uncommitted. No config write here (CDX-21) — the operator mirror is written only
	     after the row commits.
	  3. **IMMEDIATE re-check.** If a legacy ``jarvis-turn::*`` job appeared in the tiny
	     flip-vs-recheck window (or the recheck scan faulted), ROLL BACK (undoes the
	     uncommitted row flip) and return ``done=False, verdict="retry"`` so the operator
	     drains the straggler and loops. Only when the re-check is STILL clear is the row flip
	     committed (``done=True``), and THEN the ``jarvis_pump_enabled`` config mirror is
	     written best-effort (a mirror failure never unwinds the committed row; the watchdog
	     repairs it — CDX-21).

	Restart is not required (the row is the decider; ``ensure_pump`` is the primary
	start path). System-Manager gated. Run:
	``bench --site <site> execute jarvis.chat.admission.pump_cutover_execute`` — repeat
	until it returns ``done=True``."""
	frappe.only_for("System Manager")
	target = relay_target or DEFAULT_RELAY_TARGET

	from jarvis.chat import pump
	from jarvis.chat import turn_state as _ts

	# CDX-10 — the REAL serialization point. Hold the per-shard control row FOR UPDATE across
	# the ENTIRE scan -> flip -> recheck pass (no commit until the end), so this pass is mutually
	# exclusive with every dispatch-deciding read that takes the SAME lock: the legacy path's
	# enqueue-boundary re-check (_dispatch_turn) and accept_or_queue's under-lock decision. The
	# FLIP is DB-AUTHORITATIVE: it UPDATEs the shard control row's ``transport_mode`` to ``pump``
	# (mode_epoch+1), which IS the fenced value every dispatch decision reads under this lock.
	# CDX-21: the ``jarvis_pump_enabled`` config mirror is NOT written under the lock — it is
	# written best-effort ONLY AFTER the row flip commits, so the two stores are never claimed to
	# update atomically and a mirror failure can't diverge from a not-yet-committed row. This
	# closes the round-3 race in BOTH directions: a legacy sender either (a) enqueued+committed
	# BEFORE we take the lock — visible to the scan, so we do NOT flip; or (b) blocks on this lock
	# until we finish — then reads the ROW (=pump) and does NOT enqueue an invisible legacy job.
	# The row UPDATE does NOT db-commit until the end, so the FOR UPDATE is held unbroken.
	try:
		_ts._lock_shard(target)  # commit-first; the FOR UPDATE is the first statement

		# (1) Preflight scan UNDER the lock.
		pre = pump_cutover_preflight(target)
		if not pre["clear"]:
			# Not safe to cut over — leave the mode + kill switch exactly as they are; release the lock.
			frappe.db.rollback()
			return {
				"ok": True,
				"done": False,
				"action": "blocked",
				"verdict": pre["verdict"],
				"preflight": pre,
			}

		# (2) FLIP the DB-authoritative transport_mode to pump (mode_epoch+1) — the fenced decision
		# value. Still locked, no commit, and NO config write yet (CDX-21): the operator mirror is
		# written only AFTER the row is durably committed, so a mirror failure can never diverge
		# from a not-yet-committed row.
		mode_epoch = pump.set_transport_mode(target, pump._MODE_PUMP)

		# (3) IMMEDIATE re-check for a straggler that raced into the window — still locked.
		post = pump_cutover_preflight(target)
		if not post["clear"]:
			# ROLL BACK (undoes the uncommitted transport_mode flip — the row reverts to its
			# last-committed mode) and release the lock. Nothing was mirrored, so there is nothing
			# to restore. The pump never admitted here; the lock kept every dispatch-deciding read
			# serialized behind this re-check, and none observed the (uncommitted) pump mode.
			frappe.db.rollback()
			_telemetry("cutover_execute", target=target, done=0, verdict="retry")
			return {"ok": True, "done": False, "action": "reverted", "verdict": "retry", "preflight": post}

		frappe.db.commit()  # (4) commit the gate: the ROW flip is durable, the lock is released
	except Exception:
		# Any fault mid-pass: roll back (undoing the uncommitted transport_mode flip), releasing
		# the lock. Nothing was mirrored, so there is nothing to restore.
		frappe.db.rollback()
		raise
	finally:
		_ts.reset_lock_tracking()

	# (5) Best-effort config MIRROR — a failure here NEVER unwinds the committed row (CDX-21); the
	# watchdog repairs the file from the row on its next cycle.
	mirror_mismatch = pump.mirror_config_from_row(target, pump._MODE_PUMP)
	_telemetry(
		"cutover_execute",
		target=target,
		done=1,
		verdict="cutover",
		mode_epoch=mode_epoch,
		mirror_mismatch=int(mirror_mismatch),
	)
	return {
		"ok": True,
		"done": True,
		"action": "cutover",
		"verdict": "cutover",
		"mode_epoch": mode_epoch,
		"mirror_mismatch": mirror_mismatch,
		"preflight": post,
	}


@frappe.whitelist()
def pump_set_transport_mode(mode: str, relay_target: str | None = None) -> dict:
	"""CDX-10/CDX-21 — the operator command for a DELIBERATE transport-mode change OTHER than the
	forward cutover: the kill switch (``legacy``), the rollback ladder's drain step
	(``draining``), or re-enabling (``pump``). ONE operational source of truth (CDX-21): it
	updates the DB-AUTHORITATIVE ``transport_mode`` ROW under the shard control-row FOR UPDATE
	(``mode_epoch``+1) — the fenced value EVERY decision reads, fenced accept AND the lifecycle
	gates (ensure_pump/watchdog) alike — COMMITS it (the row is now the durable truth), and ONLY
	THEN best-effort mirrors ``jarvis_pump_enabled`` in site_config for the operator's eye.

	CDX-21 ordering (no cross-resource atomicity is claimed): row+epoch UPDATE, COMMIT, THEN the
	mirror. A mirror-write failure NEVER unwinds the committed row — it logs, raises a mismatch
	flag + ``transport_mode_mismatch`` telemetry, and the watchdog repairs the file on its next
	cycle from the row. Because ensure_pump/watchdog now gate on the ROW too, no combination of
	row/mirror can admit pump work while suppressing its pump owner.

	Editing ``site_config`` by hand is NOT sufficient: the row decides, so a raw config edit alone
	is reconciled AWAY by the watchdog. Always change the mode through this command (or
	``pump_cutover_execute`` for the forward cutover). System-Manager gated. Run:
	``bench --site <site> execute jarvis.chat.admission.pump_set_transport_mode --kwargs '{"mode":"legacy"}'``."""
	frappe.only_for("System Manager")

	from jarvis.chat import pump
	from jarvis.chat import turn_state as _ts

	mode = (mode or "").strip().lower()
	if mode not in (pump._MODE_PUMP, pump._MODE_DRAINING, pump._MODE_LEGACY):
		raise frappe.ValidationError(
			frappe._("mode must be one of pump/draining/legacy, got {0}").format(repr(mode))
		)
	target = relay_target or DEFAULT_RELAY_TARGET
	# (1) UPDATE the row + mode_epoch under the lock, then (2) COMMIT — the ROW is now the durable
	# operational truth for every reader (fenced + lifecycle). The lock releases on this commit.
	try:
		_ts._lock_shard(target)  # commit-first; the FOR UPDATE is the first statement
		mode_epoch = pump.set_transport_mode(target, mode)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise
	finally:
		_ts.reset_lock_tracking()
	# (3) Best-effort config MIRROR — a failure here NEVER unwinds the committed row (CDX-21).
	mirror_mismatch = pump.mirror_config_from_row(target, mode)
	_telemetry(
		"set_transport_mode",
		target=target,
		mode=mode,
		mode_epoch=mode_epoch,
		mirror_mismatch=int(mirror_mismatch),
	)
	return {
		"ok": True,
		"mode": mode,
		"mode_epoch": mode_epoch,
		"relay_target": target,
		"mirror_mismatch": mirror_mismatch,
	}


def _write_cancel_marker(conversation: str, reason: str) -> None:
	"""SUXI-4: leave a durable assistant marker Message when a queued turn is
	cancelled (user-initiated or system age-out) so a later reload shows WHY the
	send has no reply - indistinguishable otherwise from a silently dropped send.
	Reuses the error-card pattern (``error`` field) so the transcript renders it
	as a card with a Retry affordance. seq is allocated under the conversation
	FOR UPDATE lock (R-9 discipline) so it never collides with a concurrent
	writer on the same conversation. Best-effort - never blocks the cancel."""
	try:
		_lock_conversation(conversation)
		seq = (
			frappe.db.sql(f"SELECT MAX(seq) FROM `tab{MSG}` WHERE conversation=%(c)s", {"c": conversation})[
				0
			][0]
			or 0
		) + 1
		marker = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conversation,
				"seq": seq,
				"role": "assistant",
				"content": "",
				"streaming": 0,
				"error": reason,
			}
		)
		marker.flags.ignore_permissions = True
		marker.insert()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="admission._write_cancel_marker", message=frappe.get_traceback())


# --------------------------------------------------------------------------- #
# Sweep (scheduler backstop) - reservation expiry, age-out, Turn/Message reconcile
# --------------------------------------------------------------------------- #


def sweep() -> dict:
	"""Backstop tick (hooks cron). Runs whenever any non-terminal Turn rows
	exist (so a flag flip-off still gets its rows reconciled), else a cheap
	no-op. Three duties:

	  1. Reservation expiry: a dispatching turn whose enqueue was lost (expired
	     reservation, no live assistant activity of ITS OWN) returns to queued;
	     a stopped turn (cancel_requested) is cancelled, not re-queued (OARI-6).
	  2. Turn-vs-Message reconcile: a dispatching turn whose OWN assistant
	     Message (seq > seed.seq) already settled/errored is closed to match
	     Message truth; an orphaned turn whose own placeholder is a stale
	     streaming row past a lost worker (reservation expired) is closed errored
	     to free its credit (OARI-1/OARI-3). The legacy transport is the executor.
	  3. Age-out: a queued turn older than QUEUED_MAX_AGE_S is system-cancelled
	     with a recorded reason.

	Then re-promote and republish positions. Never raises.

	Flag-OFF drain semantics (OARI-4): the three settle/reclaim/age-out duties
	still run so residual rows from a flag flip-off DRAIN to terminal, but
	``promote_next`` is flag-gated so NO fresh dispatch is issued while the flag
	is off. Disabling admission therefore stops all new dispatch immediately;
	the leftover rows settle themselves out. accept_or_queue is never reached
	with the flag off (all four callers gate on ``admission_enabled`` first), so
	no NEW rows are created - the flag-OFF hot path stays byte-identical.

	WP-1d: once the Relay Pump is configured (active OR draining) it OWNS every
	Jarvis Chat Turn row and reconciles them via the pump watchdog; Phase-0's
	Turn<->Message reconcile / reservation-reclaim would fight the pump over the
	same rows, so the sweep steps back entirely (new draining-window turns are pure
	legacy with no Turn row, so nothing here is owed).

	CDX-21 (Residual A): the step-back reads the AUTHORITATIVE shard ROW
	(``pump_lifecycle_configured``), NOT the ``site_config`` mirror — so a stale/failed mirror can
	never make the sweep reconcile a row the row-authoritative pump owns (row=pump/config=legacy ⇒
	step back), nor abandon Phase-0's own rows when the row is the ``legacy`` kill switch
	(row=legacy/config=pump ⇒ the sweep runs). Phase-0 is one site-wide shard, so the decision reads
	the default control row."""
	summary = {"reclaimed": 0, "reconciled": 0, "aged_out": 0, "promoted": 0}
	from jarvis.chat import pump

	if pump.pump_lifecycle_configured(DEFAULT_RELAY_TARGET):
		return summary
	try:
		open_count = frappe.db.sql(
			f"SELECT COUNT(*) FROM `tab{TURN}` WHERE state IN ('queued','dispatching')"
		)[0][0]
	except Exception:
		return summary
	if not open_count:
		return summary

	targets: set[str] = set()
	try:
		summary["reconciled"] += _sweep_reconcile(targets)
		summary["reclaimed"] += _sweep_reservations(targets)
		summary["aged_out"] += _sweep_age_out(targets)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="admission.sweep", message=frappe.get_traceback())

	for target in targets or {DEFAULT_RELAY_TARGET}:
		try:
			summary["promoted"] += promote_next(target)
		except Exception:
			pass
	return summary


def _sweep_reconcile(targets: set[str]) -> int:
	"""Close dispatching turns whose OWN assistant Message (seq > seed.seq)
	already settled/errored, or whose own placeholder is a stale streaming row
	past a lost worker. Message truth wins.

	OARI-1: the reconcile is scoped to THIS turn's output (an assistant row
	strictly after the turn's seed user row). Keying on the conversation's newest
	assistant of ANY turn silently CLOSED a just-sent later turn as 'done' the
	moment a sweep landed while its own placeholder had not been written yet -
	losing a real user send with no reply and no error, and (in the placeholder
	window) over-promoting into same-session double-occupancy. When the turn has
	produced NOTHING of its own, reconcile leaves it: a lost enqueue is reclaimed
	by _sweep_reservations, a live turn mid-placeholder is untouched.

	OARI-3: an orphaned dispatching turn whose only OWN assistant is a stale
	streaming placeholder (worker provably dead - reservation past the 900s TTL,
	well beyond the 720s worker cap) is closed errored so a post-deploy batch of
	orphans frees its shard credits instead of pinning them for extra cycles."""
	rows = frappe.db.sql(
		f"""
		SELECT t.run_id, t.conversation, t.relay_target_id, t.seed_message,
		       t.reservation_expires_at
		FROM `tab{TURN}` t
		WHERE t.state='dispatching'
		""",
		as_dict=True,
	)
	now = _now()
	fresh = _fresh_cutoff()
	closed = 0
	for r in rows:
		# THIS turn's own latest assistant (strictly after its seed user row).
		latest = frappe.db.sql(
			f"""SELECT streaming, recovering, error, modified FROM `tab{MSG}`
			WHERE conversation=%(c)s AND role='assistant'
			  AND seq > (SELECT seq FROM `tab{MSG}` WHERE name=%(seed)s)
			ORDER BY seq DESC LIMIT 1""",
			{"c": r["conversation"], "seed": r["seed_message"]},
			as_dict=True,
		)
		if not latest:
			# Nothing of its OWN yet - a PRIOR turn's assistant is NOT this turn.
			# Do NOT close it (that was the OARI-1 silent-loss bug); leave it for
			# reservation-reclaim or a live placeholder to arrive.
			continue
		a = latest[0]
		if a.get("recovering"):
			continue  # parked for background recovery - recovery finalize settles it
		if a.get("streaming"):
			# Own placeholder still streaming. Close ONLY a provably-dead orphan
			# (reservation expired AND the placeholder has gone stale past the
			# freshness window); a live or not-yet-expired turn is left alone so a
			# live long turn is never false-closed (OARI-3).
			exp = r.get("reservation_expires_at")
			expired = bool(exp) and frappe.utils.get_datetime(exp) < frappe.utils.get_datetime(now)
			mod = a.get("modified")
			stale = bool(mod) and frappe.utils.get_datetime(mod) < frappe.utils.get_datetime(fresh)
			if expired and stale:
				state = "errored"
			else:
				continue
		else:
			# Settled or errored assistant row but the Turn is still dispatching.
			state = "errored" if a.get("error") else "done"
		affected = _run_cas(
			f"""UPDATE `tab{TURN}` SET state=%(s)s, reserved=0, done_at=%(now)s,
			was_recovered=1, version=version+1 WHERE run_id=%(r)s AND state='dispatching'""",
			{"s": state, "now": now, "r": r["run_id"]},
		)
		if affected == 1:
			closed += 1
			targets.add(r["relay_target_id"] or DEFAULT_RELAY_TARGET)
	if closed:
		frappe.db.commit()
	return closed


def _sweep_reservations(targets: set[str]) -> int:
	"""Reclaim dispatching turns whose reservation expired AND that never
	produced assistant activity OF THEIR OWN (seq > seed.seq) - a lost/crashed
	enqueue - returning them to queued for a fresh promotion.

	OARI-1: scoped to this turn's output (seq > seed.seq). The old ``role=
	'assistant' LIMIT 1`` matched ANY assistant row on the conversation, so on a
	multi-turn conversation a lost enqueue was NEVER reclaimed (an earlier turn's
	assistant made ``has_assistant`` true) - the reclaim path the brief promises
	only worked on first-turn conversations.

	OARI-6: a stopped turn (``cancel_requested``) whose worker died before making
	a placeholder is CANCELLED (terminal), never re-dispatched - a turn the user
	explicitly stopped must not run again."""
	rows = frappe.db.sql(
		f"""
		SELECT t.run_id, t.conversation, t.relay_target_id, t.seed_message,
		       t.cancel_requested
		FROM `tab{TURN}` t
		WHERE t.state='dispatching' AND t.reservation_expires_at IS NOT NULL
		  AND t.reservation_expires_at < %(now)s
		""",
		{"now": _now()},
		as_dict=True,
	)
	reclaimed = 0
	for r in rows:
		has_own_assistant = frappe.db.sql(
			f"""SELECT 1 FROM `tab{MSG}`
			WHERE conversation=%(c)s AND role='assistant'
			  AND seq > (SELECT seq FROM `tab{MSG}` WHERE name=%(seed)s) LIMIT 1""",
			{"c": r["conversation"], "seed": r["seed_message"]},
		)
		if has_own_assistant:
			continue  # reconcile owns this case
		if int(r.get("cancel_requested") or 0):
			# OARI-6: stopped-then-crashed - cancel it, never re-dispatch.
			affected = _run_cas(
				f"""UPDATE `tab{TURN}` SET state='cancelled', reserved=0, done_at=%(now)s,
				was_recovered=1, version=version+1
				WHERE run_id=%(r)s AND state='dispatching'
				  AND reservation_expires_at IS NOT NULL AND reservation_expires_at < %(now)s""",
				{"r": r["run_id"], "now": _now()},
			)
			if affected == 1:
				reclaimed += 1
				targets.add(r["relay_target_id"] or DEFAULT_RELAY_TARGET)
			continue
		affected = _run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='queued', reserved=0, dispatching_at=NULL,
			    reservation_expires_at=NULL, was_recovered=1, version=version+1
			WHERE run_id=%(r)s AND state='dispatching'
			  AND reservation_expires_at IS NOT NULL AND reservation_expires_at < %(now)s""",
			{"r": r["run_id"], "now": _now()},
		)
		if affected == 1:
			reclaimed += 1
			targets.add(r["relay_target_id"] or DEFAULT_RELAY_TARGET)
	if reclaimed:
		frappe.db.commit()
	return reclaimed


def _sweep_age_out(targets: set[str]) -> int:
	"""System-cancel queued turns older than QUEUED_MAX_AGE_S with a recorded
	reason + publish (SUX-5)."""
	cutoff = frappe.utils.add_to_date(None, seconds=-QUEUED_MAX_AGE_S)
	rows = frappe.db.sql(
		f"""
		SELECT t.run_id, t.conversation, t.relay_target_id, t.seed_message, c.owner
		FROM `tab{TURN}` t INNER JOIN `tab{CONV}` c ON c.name = t.conversation
		WHERE t.state='queued' AND t.enqueued_at IS NOT NULL AND t.enqueued_at < %(cut)s
		""",
		{"cut": cutoff},
		as_dict=True,
	)
	reason = AGE_OUT_REASON
	cancelled = 0
	for r in rows:
		affected = _run_cas(
			f"""UPDATE `tab{TURN}` SET state='cancelled', cancel_reason=%(reason)s,
			done_at=%(now)s, version=version+1 WHERE run_id=%(r)s AND state='queued'""",
			{"reason": reason, "now": _now(), "r": r["run_id"]},
		)
		if affected != 1:
			continue
		# Make the cancel durable BEFORE the side-effects: _write_cancel_marker
		# runs its own txn (and rolls back on failure), which would otherwise
		# undo an uncommitted CAS.
		frappe.db.commit()
		cancelled += 1
		targets.add(r["relay_target_id"] or DEFAULT_RELAY_TARGET)
		try:
			publish_to_user(
				r["owner"],
				{
					"kind": "turn:cancelled",
					"conversation_id": r["conversation"],
					"run_id": r["run_id"],
					"message_id": r["seed_message"],
					"reason": reason,
				},
			)
		except Exception:
			pass
		# SUXI-4: durable transcript marker (survives the next-morning reload).
		_write_cancel_marker(r["conversation"], reason)
	return cancelled
