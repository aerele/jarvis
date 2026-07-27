"""WP-1c — the Relay Pump reactor (the heart of the managed-relay transport).

One leased supervisor per ``(site, relay_target_id)`` drains every turn on a
container over ONE multiplexed WebSocket (WP-1b's :class:`RelayMux`), driving the
durable turn-state machine (WP-1a's ``turn_state`` CAS library) forward. This
module is the D6 lifecycle: bounded 60-120s hops on ``long``, hop = mini-takeover
(epoch bump + adopted-turn re-stamp EVERY hop), snapshot reconcile on every
start/reconnect, full-credit admission at promote under the shard lock,
idle-exit conditional release, DB-disconnect bounded-backoff parking, and the one
shared lease-loss exit.

Binding contract (read in order):
  * implementation/wp-d/D6-lifecycle-diagrams.md (AMENDED) — cold start, the
    idle-exit/send race (act-on-affected-rows, OAR-12), hop handoff with a FRESH
    job id, SIGTERM warm shutdown, the 240s scheduler backstop, DB-disconnect park.
  * implementation/wp-d/D3-cas-pseudocode.md (AMENDED) — shard-lock admission at
    promote (Race 2), the settlement transaction (Race 3), reservation lifecycle.
  * implementation/wp-d/D2-schema-transitions.md (AMENDED) — the transition table
    ``turn_state`` implements; the watchdog's PER-STATE actions (finalizing ⇒
    re-enqueue finalize only, R-13; PREPARE_DEADLINE_S=300; queued age-out).
  * Binding spec §10.4 (pump control jobs ride ``long`` UNCONDITIONALLY), §10.5
    (hop = mini-takeover), §10.6 (state-machine corrections), §8-E (sender-driven
    ``ensure_pump`` PRIMARY; fresh job id per hop — the frappe dedupe trap; the
    240s scheduler backstop).
  * spikes/S4-turn-queue-cache.md (why pump jobs → ``long``) + S2 (ack semantics).

HARD INVARIANTS:
  * EVERY turn mutation goes through ``turn_state`` (no raw CAS SQL here); every
    realtime publish goes through ``turn_state.publish_fenced`` AFTER a winning
    commit. A pump-owned CAS that affects 0 rows AND whose epoch no longer matches
    routes through the ONE shared ``lease_lost_exit`` (stop reading, no publishes,
    hop returns).
  * Pump control jobs (hops, watchdog-triggered starts) ALWAYS enqueue to
    ``long`` with an EXPLICIT ``timeout=180`` and a FRESH job id per hop (§10.4,
    S4 — a dead ``jarvis_chat`` queue can look provisioned for ~420-480s, longer
    than a hop; ``long`` always has a live consumer).
  * The commit-first REPEATABLE-READ shard-lock discipline at every control-row
    lock site: ``turn_state._lock_shard`` commits to start a fresh transaction so
    the ``SELECT ... FOR UPDATE`` is the first statement and the credit COUNT that
    follows takes its snapshot AFTER the prior holder committed. Canonical lock
    order (OAR-6): control(shard) -> conversation -> turn -> message.

DEADLOCK-REPLAY SAFETY (Frappe replays a deadlocked job up to 5x): the hop body
is replay-safe BY CONSTRUCTION. Every effect is either a ``turn_state`` CAS keyed
by ``state+version(+pump_epoch)`` or an idempotent effect keyed by
``(turn, effect)`` / the ``run_id`` PK, so a replayed slice re-applies nothing
already committed — a replayed delta CAS sees the advanced watermark and affects 0
rows; a replayed settlement sees ``finalizing`` and affects 0 rows; a replayed
promote sees ``reserved=1`` and affects 0 rows; a replayed seed collides on the
``run_id`` PK. The mux/WS is rebuilt fresh each hop, so a replay never doubles a
socket. Nothing outside a ``turn_state`` CAS or an idempotency-keyed effect is
mutated on the hot path.

SIGTERM / warm shutdown (D6 §4): a hop finishes its current slice, commits, and
releases the lease cleanly, all inside the supervisor ``stopwaitsecs`` budget
because a hop is at most 120s. The queue-timeout ladder is comfortable
everywhere: 180 (SIGALRM ``timeout=``) < 240 (v16 monitor kill) < 1560 (``long``
``stopwaitsecs``).

WP-1c is DRIVEN BY TESTS ONLY. Production callers (api.py / turn_handler.py /
actions_api.py) are wired in WP-1d/e. The two WP-1d seams (prepare dispatcher +
settlement/finalize invoker) are injectable via :class:`PumpDeps` and default to
minimal-but-correct in-module implementations so the pump is exercisable and
correct standalone; WP-1d replaces them with the full prepare/finalize jobs.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import frappe

from jarvis.chat import turn_state as ts
from jarvis.chat.relay_mux import LaneHandler, RelayMux
from jarvis.exceptions import OpenclawUnreachableError

TURN = "Jarvis Chat Turn"
PUMP = "Jarvis Relay Pump"
MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"

# --------------------------------------------------------------------------- #
# Constants (from the amended D6 / RULINGS-PA §10.4)
# --------------------------------------------------------------------------- #

# Pump control jobs ALWAYS ride `long` (S4 / R-19 / §10.4): guaranteed consumer.
PUMP_QUEUE = "long"

# Hop budget ladder (R-18 / §10.4). Soft budget = when to hand off; hard deadline
# = the last moment a slice may start; explicit RQ timeout = the SIGALRM ceiling.
SOFT_HOP_BUDGET_S = 90
HARD_HOP_DEADLINE_S = 120
HOP_TIMEOUT_S = 180

# Loop heartbeat + lease renew cadence (distinct signals, Amendment E). Both are
# written at most this often (a slice is ~sub-second, so we gate them).
HEARTBEAT_INTERVAL_S = 10

# How long a slice blocks on the mux for buffered frames before re-checking the
# lease / budget. Small so budgets + SIGTERM are honored promptly.
SLICE_BLOCK_S = 0.2

# chat.send ack window. Longer than the mux default so a busy accept path still
# acks; on expiry the mux raises the `ack-timeout` sentinel → park recovering.
ACK_TIMEOUT_S = 15.0

# DB-disconnect bounded backoff (D6 §6): after this many CONSECUTIVE slice-level
# operational errors the pump PARKS the affected turns and exits (never spins).
DB_BACKOFF_ATTEMPTS = 5
DB_BACKOFF_BASE_S = 0.2
DB_BACKOFF_CAP_S = 2.0

# Recovery budget: a turn stuck in `recovering` past this (from
# recovery_started_at) is driven to `errored` by the watchdog (D2 row 24).
RECOVERY_BUDGET_S = 600

# Pre-claim dispatch deadline (OARF-4): a turn that RESERVED a credit at promote
# but whose prepare never CLAIMED it (lost enqueue / `long` saturation / a crash
# before claim_preparing) is reclaimed back to `queued reserved=0` FOR RETRY at
# this short deadline (from enqueued_at), well under both the 900s reserve TTL and
# the 900s queued age-out. Reclaim (retry) WINS over age-out (cancel) for a
# reserved turn: a turn that was admitted + given a credit must not be cancelled
# for a starvation that was not its fault.
PREPARE_DISPATCH_DEADLINE_S = 120

# Bench-side delta batcher cadence (OARF-6 / D1 #28): the legacy
# `_AssistantContentBatcher` relocated INTO the lane handler — per-frame
# commit+publish becomes one commit+publish per N frames OR every INTERVAL ms
# (whichever trips first), coalescing openclaw's ~150ms cumulative mirrors. The
# first delta always flushes immediately (first-token latency, C1). C3
# (flush_gap_ms) measures the resulting cadence.
_DELTA_BATCH_SIZE = 10
_DELTA_BATCH_INTERVAL_MS = 250

# Admission safety reserve (§10.1 — heartbeats self-defer, so default 0).
SAFETY_RESERVE = 0

# CDX-11 conservative admission when the gateway snapshot (foreign-usage visibility)
# is degraded. A failed snapshot keeps the LAST-KNOWN foreign count for this long
# (never fail-open to zero); once the last-known is ALSO stale, visibility is UNKNOWN and
# admission FAILS CLOSED — ZERO new promotions until a snapshot succeeds again (the
# earlier cap-1 compromise is dropped: with an invisible foreign run any positive local
# admission can oversubscribe the container). In-flight turns are unaffected; a loud
# telemetry warning fires while held. Capacity is refreshed on the mid-hop cadence by
# the CDX-17 issue-and-poll snapshot (never a blocking re-snapshot).
GATEWAY_ACTIVE_TTL_S = 60
SNAPSHOT_REFRESH_S = 30
# Retained constant (referenced by tests/telemetry); the cap-1 reduced-admission path it
# once fed is removed — unknown visibility now fails fully closed (zero promotions).
CONSERVATIVE_CAP_SAFETY = 1

# Redis lease-mirror TTL (fast NO-OP path only; MariaDB is authoritative, R-17).
LEASE_MIRROR_TTL_S = 30

# CDX-1 transport-exit successor budget. A non-handoff loop exit (transport_closed /
# no_transport) with live work releases the lease (epoch-guarded) and enqueues an
# IMMEDIATE successor, up to this many consecutive transport retries. frappe.enqueue
# exposes no delayed/scheduled enqueue (no enqueue_in/at; the RQ job scheduler is not
# run by the bench workers), so the ruled '5s/15s/45s' time-delay is realized as a
# bounded fast-retry COUNT paced by the OpenclawSession connect timeout, then a
# fall-through to the watchdog-cron / sender ``ensure_pump`` revival for a sustained
# outage. This preserves the hard invariant (never exit successor-less with live
# work, within the budget) without a hot reconnect loop. See PUMP-RUNBOOK.md §CDX-1.
TRANSPORT_RETRY_MAX = 4

# Injection seams for timing (tests monkeypatch to remove real waits).
_monotonic: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep


# --------------------------------------------------------------------------- #
# Pump-mode routing flags (§10.9 — managed relay ONLY; self-host keeps legacy)
# --------------------------------------------------------------------------- #
#
# The Relay Pump is Jarvis's DEFAULT managed transport, so the per-site flag
# `jarvis_pump_enabled` (frappe.conf) reads ABSENCE as ON:
#   * UNSET / None (the default) OR any truthy value  = ON — the pump owns new-turn
#     dispatch. An under-provisioned site meets the pump BY DEFAULT (watch for the
#     `provision_warning` telemetry / §8-I error log — see PUMP-RUNBOOK.md §6).
#   * an EXPLICIT falsy value (0, "0", false, "off", "no", "")  = OFF / INERT — the
#     kill switch: byte-identical legacy dispatch, and ensure_pump + the watchdog are
#     a total no-op (OARF-1). ABSENCE is NEVER this opt-out.
#   * the sentinel 'draining'  = no NEW pump admissions (new turns fall through to
#     legacy) while the pump keeps draining its existing Turn rows to terminal.
# All three predicates are ANDed with `not selfhost.is_self_hosted()` (§10.9 — the
# default applies ONLY where the managed relay is the transport; self-host stays
# legacy even with the flag unset). Independent of `jarvis_phase0_admission_enabled`
# (pump ON implies admission semantics INSIDE the machine).
#
# `_pump_flag_explicit_off` is the ONE place the absent-vs-explicit-0 distinction is
# decided, so every predicate below shares it verbatim — the kill switch means the
# same thing to each (HARD invariant: absent and explicit-0 must never diverge in a
# way that weakens the kill switch).

# Explicit falsy conf values that DISABLE the pump (the kill switch). Note "0" is a
# truthy Python string, so this set — not bare truthiness — is what makes a string
# "0"/"false" count as off.
_PUMP_EXPLICIT_OFF_VALUES = frozenset({"0", "false", "no", "off", ""})


def _pump_flag_explicit_off(flag) -> bool:
	"""True ONLY for an EXPLICIT falsy value of ``jarvis_pump_enabled`` (the kill
	switch): ``0`` / ``"0"`` / ``False`` / ``"false"`` / ``"off"`` / ``"no"`` /
	``""``. An ABSENT flag (``None``) is the managed DEFAULT (pump ON) and is NEVER
	explicit-off — this is the ONE decision every predicate below shares verbatim."""
	if flag is None:
		return False
	return str(flag).strip().lower() in _PUMP_EXPLICIT_OFF_VALUES


def _pump_flag_draining(flag) -> bool:
	"""True ONLY for the explicit ``'draining'`` sentinel (unaffected by default-ON)."""
	return flag is not None and str(flag).strip().lower() == "draining"


# --------------------------------------------------------------------------- #
# CDX-10 — DB-AUTHORITATIVE transport mode (the fenced dispatch decision)
# --------------------------------------------------------------------------- #
#
# The site_config flag `jarvis_pump_enabled` is copied into `frappe.local.conf` ONCE at
# request initialization (frappe.init) and is NOT refreshed when an operator flips it
# mid-flight (update_site_config rewrites the JSON + clears caches but never rewrites an
# already-initialized request's local copy). So a request that chose a route, then paused
# on the shard lock across a cutover, would decide on a STALE conf snapshot. The fix: the
# per-shard `Jarvis Relay Pump.transport_mode` column IS the fenced decision value. Every
# dispatch-DECIDING read (accept_or_queue's pump branch, _dispatch_turn's enqueue-boundary
# re-check) reads that ROW under the shard control-row FOR UPDATE it already holds; the
# conf flag is the operator-facing MIRROR only. Cheap NON-deciding readers (watchdog gating,
# telemetry, the callers' fast entry hints) keep reading conf as before — the `from_db`
# path below is used ONLY at the two fenced points.

# transport_mode values.
_MODE_PUMP = "pump"
_MODE_DRAINING = "draining"
_MODE_LEGACY = "legacy"


def _config_transport_mode() -> str:
	"""Derive the transport_mode a shard SHOULD have from the site_config flag (the
	initial/reconcile source). Shares ``_pump_flag_explicit_off`` verbatim so absent-vs-
	explicit-0 never diverges from the conf predicates. Self-host is orthogonal (handled
	at the decision points by ANDing ``not is_self_hosted()``), so this maps only the flag."""
	flag = frappe.conf.get("jarvis_pump_enabled")
	if _pump_flag_explicit_off(flag):
		return _MODE_LEGACY
	if _pump_flag_draining(flag):
		return _MODE_DRAINING
	return _MODE_PUMP


def _row_transport_mode(target: str) -> str:
	"""Read the shard control row's ``transport_mode`` — the DB-authoritative decision.
	Callers at the fenced points already hold that row FOR UPDATE, so this non-locking
	read (in the same txn) sees the latest committed value + any own-txn write. An EMPTY
	value (a pre-migration / not-yet-reconciled row) falls back to the config-derived mode
	WITHOUT writing — that fallback is only ever hit before any cutover, when conf and the
	row agree anyway; a committed cutover always leaves the row non-empty."""
	tm = frappe.db.get_value(PUMP, target, "transport_mode")
	if not tm:
		return _config_transport_mode()
	return str(tm).strip().lower()


def reconcile_transport_mode(target: str) -> str:
	"""Boot/reconcile (ruling): derive the row's initial transport_mode from the config
	flag ONCE when it is empty (a fresh row, or a pre-migration one), and persist it so the
	ROW becomes authoritative thereafter. Idempotent — a non-empty row is returned as-is,
	never re-derived from conf (that would let the file re-decide). Ensures the row exists
	first. Best-effort; commits its own write. Called from ``ensure_pump``/``watchdog``."""
	from jarvis.chat import turn_state as _ts

	_ts._ensure_control_row(target)
	tm = frappe.db.get_value(PUMP, target, "transport_mode")
	if tm:
		return str(tm).strip().lower()
	derived = _config_transport_mode()
	try:
		frappe.db.set_value(PUMP, target, "transport_mode", derived, update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
	return derived


def set_transport_mode(target: str, mode: str) -> int:
	"""Deliberately change the shard's transport_mode under the shard control-row lock the
	CALLER already holds (pump_cutover_execute), advancing ``mode_epoch`` by 1 so the change
	is observable/orderable (CDX-10). No commit here — the caller commits the flip atomically
	(or rolls it back on a straggler/fault). Returns the new mode_epoch (best-effort read)."""
	frappe.db.sql(
		f"""UPDATE `tab{PUMP}` SET transport_mode=%(m)s, mode_epoch=mode_epoch+1
		WHERE relay_target_id=%(t)s""",
		{"m": mode, "t": target},
	)
	# CDX-21: drop the lifecycle gate's TTL cache so a same-process ensure_pump/watchdog sees the
	# deliberate change immediately rather than waiting out the 5s window.
	_LIFECYCLE_MODE_CACHE.pop(target, None)
	return int(frappe.db.get_value(PUMP, target, "mode_epoch") or 0)


def pump_mode_active(from_db: bool = False, target: str | None = None) -> bool:
	"""True when the Relay Pump owns NEW turn dispatch on this bench: the per-site
	``jarvis_pump_enabled`` flag is NOT an explicit-off kill switch AND not
	``'draining'``, AND the transport is managed relay. The pump is the DEFAULT
	transport, so an UNSET flag is ACTIVE — only an explicit ``0``/``false`` disables
	it (§10.9 — self-host turns keep the legacy worker-per-turn path regardless).
	Cheap conf read + one selfhost check.

	``from_db=True`` (CDX-10, the FENCED path): read ``transport_mode`` from the shard
	control ROW instead of the request-local conf snapshot — the caller MUST already hold
	that row FOR UPDATE. Used only at the two dispatch-deciding points; every other reader
	keeps the cheap conf read."""
	from jarvis import selfhost

	if from_db:
		return _row_transport_mode(target or "default") == _MODE_PUMP and not selfhost.is_self_hosted()
	flag = frappe.conf.get("jarvis_pump_enabled")
	if _pump_flag_explicit_off(flag) or _pump_flag_draining(flag):
		return False
	return not selfhost.is_self_hosted()


def pump_draining(from_db: bool = False, target: str | None = None) -> bool:
	"""True when the shard is DRAINING on a managed bench: NO new pump admissions (new
	turns fall through to the legacy path), while the pump keeps draining its existing
	Turn-row turns to terminal (OAR-11 coexistence). Draining is ALWAYS an explicit
	sentinel, so the default-ON inversion does not touch it. ``from_db=True`` reads the
	fenced ROW (caller holds the shard lock)."""
	from jarvis import selfhost

	if from_db:
		return _row_transport_mode(target or "default") == _MODE_DRAINING and not selfhost.is_self_hosted()
	flag = frappe.conf.get("jarvis_pump_enabled")
	if not _pump_flag_draining(flag):
		return False
	return not selfhost.is_self_hosted()


def pump_configured(from_db: bool = False, target: str | None = None) -> bool:
	"""True when the pump is on in ANY form (active OR draining) on a managed bench —
	i.e. NOT the explicit-off kill switch. The pump is the DEFAULT transport, so an
	UNSET flag IS configured; only an explicit falsy value (``0``/``"0"``/``false``)
	makes it INERT (OARF-1: ``ensure_pump`` + ``watchdog`` no-op). Once configured, the
	pump OWNS every ``Jarvis Chat Turn`` row, so Phase-0 admission's promote/sweep step
	back (they must never legacy-dispatch or reconcile a pump-owned Turn row) — the
	coexistence discriminator that keeps the two machines from fighting over the same
	rows. ``from_db=True`` (CDX-10) reads the fenced ROW (caller holds the shard lock);
	configured == transport_mode is NOT ``legacy``."""
	from jarvis import selfhost

	if from_db:
		return _row_transport_mode(target or "default") != _MODE_LEGACY and not selfhost.is_self_hosted()
	if _pump_flag_explicit_off(frappe.conf.get("jarvis_pump_enabled")):
		return False
	return not selfhost.is_self_hosted()


def transport_predicates_from_row(target: str) -> dict:
	"""Sweep note (contention): read the shard control ROW's ``transport_mode`` EXACTLY ONCE
	(the caller holds it FOR UPDATE) and derive the fenced predicates from that single value —
	instead of ``turn_machine_enabled(from_db)`` + ``pump_mode_active(from_db)`` each re-reading
	the row while the site-wide admission lock is held. ``is_self_hosted()`` is a static
	per-bench property. Returns ``pump_active`` / ``configured`` (== not the legacy kill switch)
	so ``accept_or_queue`` can compute BOTH ``pump_mode`` and ``machine_active`` from one read."""
	from jarvis import selfhost

	mode = _row_transport_mode(target)
	self_host = selfhost.is_self_hosted()
	return {
		"mode": mode,
		"pump_active": mode == _MODE_PUMP and not self_host,
		"configured": mode != _MODE_LEGACY and not self_host,
	}


# CDX-21 — the non-fenced lifecycle gates (ensure_pump / watchdog) read the AUTHORITATIVE row,
# never the site_config mirror. A tiny in-process TTL cache keeps ensure_pump (called after
# EVERY durable send) from adding a row read per turn; 5s bounds how late a committed cutover is
# observed by the lifecycle machinery (the fenced accept/dispatch decisions are ALWAYS exact —
# they read the row under the shard lock). This closes the split the review flagged: with the
# gate reading the row, a mirror that briefly disagrees can never make the pump go inert while
# the fenced accept still admits pump-owned turns.
DEFAULT_TARGET = "default"
_LIFECYCLE_MODE_CACHE: dict[str, tuple[str, float]] = {}
_LIFECYCLE_CACHE_TTL_S = 5.0


def _lifecycle_row_mode(target: str) -> str:
	import time as _t

	now = _t.monotonic()
	ent = _LIFECYCLE_MODE_CACHE.get(target)
	if ent and ent[1] > now:
		return ent[0]
	mode = _row_transport_mode(target)
	_LIFECYCLE_MODE_CACHE[target] = (mode, now + _LIFECYCLE_CACHE_TTL_S)
	return mode


def pump_lifecycle_configured(target: str) -> bool:
	"""CDX-21 gate for ensure_pump/watchdog: configured == the ROW is not the ``legacy`` kill
	switch (and not self-hosted). Row-authoritative (5s TTL read-through), REPLACING the old
	config-based ``pump_configured()`` so the lifecycle machinery can never disagree with the
	fenced accept. When the row is momentarily out of sync with the config mirror, the accept
	gate (row) and this gate (row) still agree — no strand."""
	from jarvis import selfhost

	return _lifecycle_row_mode(target) != _MODE_LEGACY and not selfhost.is_self_hosted()


def _kill_switch_engaged(target: str) -> bool:
	"""CDX-21 (Residual B) — the pump kill switch: the shard's AUTHORITATIVE row mode is
	``legacy``. Read THROUGH the 5s lifecycle cache (the same cache the lifecycle gates use, which
	codex accepted), so a mid-hop ``pump -> legacy`` change is observed within the cache TTL —
	giving the explicit stop bound the prompt requires: kill latency <= cache TTL + the current
	slice. ``draining`` is NOT the kill switch (the pump keeps draining its in-flight turns to
	terminal, only refusing NEW work — current semantics), so ONLY an explicit ``legacy`` row halts
	succession."""
	return _lifecycle_row_mode(target) == _MODE_LEGACY


def _halt_for_kill_switch(ctx: "PumpContext") -> None:
	"""CDX-21 (Residual B) — the row flipped to ``legacy`` (kill switch) mid-hop. Release the lease
	WITHOUT enqueuing a successor and PARK the shard's in-flight turns ``recovering`` so
	Phase-0/legacy takes over ownership per the coexistence rules: the legacy Message-row recovery
	cron (``turn_recovery.recover_pending_turns``) finalizes the parked turns from the gateway
	transcript, and a ``recovering`` Turn row holds no admission credit. Park BEFORE releasing so the
	recovering marks are written while this hop still holds the epoch. Epoch-guarded: a stale hop
	whose epoch a takeover already bumped releases nothing (``lease_handoff`` matches 0 rows) — the
	new owner, which will ITSELF re-check the row at its own hop start, drives the decision."""
	target = ctx.relay_target_id
	next_hop = ctx.hop_counter + 1
	holder = f"jarvis-pump::{ctx.site}::{target}::hop{next_hop}"
	_park_affected_recovering(ctx)
	if ts.lease_handoff(target, ctx.epoch, next_hop, holder):
		_clear_lease_mirror(target)
	_telemetry("kill_switch_release", target=target, hop=ctx.hop_counter, epoch=ctx.epoch)


def _mirror_value_for_mode(mode: str):
	"""The site_config ``jarvis_pump_enabled`` MIRROR value for a row transport_mode:
	pump -> remove the key (absence = managed default ON), draining -> 'draining', legacy -> 0."""
	return {_MODE_PUMP: "None", _MODE_DRAINING: "draining", _MODE_LEGACY: 0}.get(mode, "None")


def _set_mirror_mismatch(target: str, val: int) -> None:
	"""Raise/clear the durable operator signal that the config mirror diverged from the row.
	Best-effort; writes only on change so it never churns the row."""
	try:
		cur = frappe.db.get_value(PUMP, target, "mirror_mismatch")
		if int(cur or 0) != int(val):
			frappe.db.set_value(PUMP, target, "mirror_mismatch", int(val), update_modified=False)
			frappe.db.commit()
	except Exception:
		pass


def reconcile_config_mirror(target: str) -> bool:
	"""CDX-21 — the shard ROW's ``transport_mode`` is the operational truth; ``site_config`` is an
	asynchronously reconciled operator MIRROR. Each watchdog cycle, compare the file's config-
	derived mode to the row; if they disagree (a mirror write failed, or a crash cut between the
	row commit and the mirror write), idempotently REWRITE the file from the row, raise the
	mismatch flag, and emit a ``transport_mode_mismatch`` telemetry warning WHILE they differ.
	When they agree, clear the flag. Returns True when a mismatch was found/repaired. Never
	raises out — the mirror is cosmetic/operator-facing; the row already decides."""
	try:
		row_mode = _row_transport_mode(target)
		conf_mode = _config_transport_mode()
		if row_mode == conf_mode:
			_set_mirror_mismatch(target, 0)
			return False
		from frappe.installer import update_site_config

		_telemetry("transport_mode_mismatch", target=target, row_mode=row_mode, conf_mode=conf_mode)
		update_site_config("jarvis_pump_enabled", _mirror_value_for_mode(row_mode))
		_set_mirror_mismatch(target, 1)
		return True
	except Exception:
		frappe.log_error(title="pump.reconcile_config_mirror", message=frappe.get_traceback())
		return False


def mirror_config_from_row(target: str, mode: str) -> bool:
	"""CDX-21 — write the operator-facing config mirror AFTER the row is durably committed (the
	new ordering: row+epoch UPDATE, COMMIT, THEN this best-effort mirror). A failure here NEVER
	unwinds the committed row — it logs, raises the mismatch flag, and emits telemetry; the
	watchdog's ``reconcile_config_mirror`` repairs the file on its next cycle. Returns True on a
	mirror-write FAILURE (mismatch). Never raises."""
	from frappe.installer import update_site_config

	try:
		update_site_config("jarvis_pump_enabled", _mirror_value_for_mode(mode))
		_set_mirror_mismatch(target, 0)
		# Refresh the lifecycle cache immediately so a same-process reader sees the new mode
		# without waiting out the TTL (the row is already authoritative regardless).
		_LIFECYCLE_MODE_CACHE.pop(target, None)
		return False
	except Exception:
		_set_mirror_mismatch(target, 1)
		_telemetry("transport_mode_mismatch", target=target, mode=mode, phase="mirror_write_failed")
		frappe.log_error(
			title="pump mirror write failed (row is authoritative)", message=frappe.get_traceback()
		)
		return True


# Operational-error tuple for the DB-disconnect park path (frappe.db exposes the
# driver's exception classes; resolved lazily so import never needs a live DB).
def _operational_errors() -> tuple[type[BaseException], ...]:
	errs: list[type[BaseException]] = []
	for name in ("OperationalError", "InterfaceError"):
		exc = getattr(frappe.db, name, None) if getattr(frappe, "db", None) else None
		if isinstance(exc, type) and issubclass(exc, BaseException):
			errs.append(exc)
	return tuple(errs) or (Exception,)


# --------------------------------------------------------------------------- #
# Wake bus — RAW redis client with an EXPLICIT site key (RedisWrapper BRPOP trap)
# --------------------------------------------------------------------------- #
#
# frappe's RedisWrapper prefixes `lpush`/`rpop` via `make_key` but NOT the raw
# `brpop` command (HANDOFF §6 gotcha). To make LPUSH and the pop AGREE no matter
# which wrapper method is used, we go through `execute_command` (the un-wrapped
# redis.Redis path) with a fully-explicit, site-scoped key we build ourselves. A
# LOST wake entry costs at most one tick, never a turn — the pump scans `queued`
# rows on wake and on every watchdog tick regardless (§3 v1 amendment).


def _wake_key(target: str) -> str:
	db = (frappe.local.conf.get("db_name") if getattr(frappe, "local", None) else None) or frappe.local.site
	return f"{db}|jarvis:pump:commands:{target}"


def lpush_wake(target: str, run_id: str) -> None:
	"""Best-effort wake signal (called by accept_send AFTER its durable commit,
	§10.6). Raw client + explicit key so it pairs with :func:`drain_wake`."""
	try:
		frappe.cache().execute_command("LPUSH", _wake_key(target), run_id)
	except Exception:
		pass


def drain_wake(target: str, max_items: int = 512) -> list[str]:
	"""Drain the wake bus (RPOP loop, non-blocking) via the RAW client + explicit
	key. Returns the run_ids signalled (advisory only — the pump scans `queued`
	rows anyway, so a lost/duplicate wake is harmless)."""
	out: list[str] = []
	try:
		conn = frappe.cache()
		key = _wake_key(target)
		for _ in range(max_items):
			val = conn.execute_command("RPOP", key)
			if val is None:
				break
			out.append(val.decode() if isinstance(val, bytes) else str(val))
	except Exception:
		pass
	return out


# --------------------------------------------------------------------------- #
# Wake thread — the accept wake-bus as an EVENT-DRIVEN block interrupt (C1)
# --------------------------------------------------------------------------- #
#
# WP-2 Stage B found warm first-token ~+513ms over legacy: a slice's idle wait is
# ``mux.dispatch(block_s=SLICE_BLOCK_S)`` (an Event wait the mux sets on frame
# delivery, and now — WP-2 C1 fix — on a resolved ack), but NOTHING in the pump
# process watched the CROSS-PROCESS accept wake bus. accept_send LPUSHes the bus
# from the web worker AFTER its durable commit (§10.6), yet the running reactor was
# blocked in a slice that only re-scanned `queued` rows at the NEXT slice top — so an
# uncontended new turn waited up to the SLICE_BLOCK_S ceiling just to be promoted.
#
# This thread BLOCKS on the wake bus (BRPOP) and pokes the mux the instant an accept
# (or a prepare -> ready) LPUSHes, waking the slice IMMEDIATELY — no polling theatre.
# It runs ON ITS OWN thread (like the mux reader) so the block is truly event-driven;
# the slice then waits on whichever of {frame, ack, accept wake} fires first, all via
# the ONE mux ``_wake`` Event. The redis client + the fully-resolved site-scoped key
# are captured on the PUMP thread (frappe.local valid there) and used RAW in the child
# via ``execute_command`` — the exact un-wrapped path lpush_wake/drain_wake use, which
# needs no frappe.local (RedisWrapper does not override execute_command). Best-effort:
# any redis error just ends the thread — SLICE_BLOCK_S stays the fallback tick and
# _promote_queued scans `queued` rows every slice regardless (the wake is advisory,
# so a lost/duplicate/late wake is harmless, never a stuck turn).

# BRPOP block ceiling: integer secs (valid on every redis, incl. <6.0); a real LPUSH
# returns AT ONCE regardless, so this only bounds how often an idle thread re-checks
# the stop flag. Kept small so per-hop thread churn (kill/e2e phases) stays bounded.
WAKE_BRPOP_TIMEOUT_S = 1
_WAKE_STOP_SENTINEL = "__pump_wake_stop__"


class _WakeThread:
	"""Daemon thread that turns the accept wake bus into an immediate mux poke (C1)."""

	def __init__(self, target: str, mux: RelayMux):
		self._target = target
		self._mux = mux
		self._stop = threading.Event()
		self._thread: threading.Thread | None = None
		# Capture the client + explicit key HERE (pump thread: frappe.local is valid).
		try:
			self._conn = frappe.cache()
			self._key = _wake_key(target)
		except Exception:
			self._conn = None
			self._key = None

	def start(self) -> "_WakeThread":
		if self._conn is None or self._key is None:
			return self
		self._thread = threading.Thread(target=self._run, name=f"pump-wake::{self._target}", daemon=True)
		self._thread.start()
		return self

	def _run(self) -> None:
		while not self._stop.is_set():
			try:
				# Blocking pop: returns [key, value] the instant an accept LPUSHes, else
				# None after the ceiling. Raw conn + explicit key — no frappe.local.
				item = self._conn.execute_command("BRPOP", self._key, WAKE_BRPOP_TIMEOUT_S)
			except Exception:
				return  # redis unavailable — fall back to the SLICE_BLOCK_S ceiling
			if self._stop.is_set():
				return
			if item is None:
				continue
			# Drain any OTHER queued wake commands non-blocking (raw RPOP on the captured
			# conn+key — no frappe.local needed in this bare thread), then poke ONCE. The
			# slice re-scans `queued` rows regardless, so the drained ids are advisory.
			try:
				for _ in range(512):
					if self._conn.execute_command("RPOP", self._key) is None:
						break
			except Exception:
				pass
			self._mux.poke()

	def stop(self) -> None:
		self._stop.set()
		# Unblock a mid-wait BRPOP so the daemon exits promptly (bounds thread churn
		# across the many bounded hops the kill/e2e phases run). Best-effort.
		try:
			if self._key is not None:
				frappe.cache().execute_command("LPUSH", self._key, _WAKE_STOP_SENTINEL)
		except Exception:
			pass
		if self._thread is not None:
			self._thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# Redis lease mirror (fast NO-OP path ONLY — never proof of vacancy, R-17/D4-b)
# --------------------------------------------------------------------------- #


def _lease_mirror_key(target: str) -> str:
	return f"jarvis:pump:lease:{target}"


def _write_lease_mirror(target: str) -> None:
	try:
		frappe.cache().set_value(_lease_mirror_key(target), "1", expires_in_sec=LEASE_MIRROR_TTL_S)
	except Exception:
		pass


def _clear_lease_mirror(target: str) -> None:
	try:
		frappe.cache().delete_value(_lease_mirror_key(target))
	except Exception:
		pass


def _lease_mirror_live(target: str) -> bool:
	"""Fast NO-OP gate ONLY: a PRESENT+fresh mirror lets ``ensure_pump`` skip the
	DB read (the mirror TTL ~= the lease TTL, so this never no-ops past the point
	MariaDB would). A MISSING mirror is NEVER treated as proof of vacancy — the
	caller falls through to the authoritative MariaDB read (D4-b)."""
	try:
		return bool(frappe.cache().get_value(_lease_mirror_key(target), use_local_cache=False))
	except Exception:
		return False


# --------------------------------------------------------------------------- #
# CDX-11 — last-known foreign gateway usage (never fail-open to zero)
# --------------------------------------------------------------------------- #


def _gateway_active_key(target: str) -> str:
	return f"jarvis:pump:gateway_active:{target}"


def _write_last_known_gateway_active(target: str, foreign: int) -> None:
	"""CDX-11: cache the last SUCCESSFUL foreign-usage count with a short TTL so a
	subsequent snapshot failure can reuse recent truth instead of failing open."""
	try:
		frappe.cache().set_value(
			_gateway_active_key(target), str(int(foreign)), expires_in_sec=GATEWAY_ACTIVE_TTL_S
		)
	except Exception:
		pass


def _read_last_known_gateway_active(target: str) -> int | None:
	"""CDX-11: the last-known foreign-usage count if still within its TTL, else None
	(the observation aged out — treat foreign usage as genuinely unknown)."""
	try:
		v = frappe.cache().get_value(_gateway_active_key(target), use_local_cache=False)
		if v is None:
			return None
		return int(v.decode() if isinstance(v, bytes) else v)
	except Exception:
		return None


def _reconcile_gateway_active(ctx: "PumpContext", snap: dict | None = None) -> None:
	"""CDX-11: fold a snapshot result into the context's capacity view — NEVER
	fail-open. On a good snapshot: record foreign usage + cache it (TTL) + clear the
	conservative flag. On a failed snapshot: reuse the last-known count if it is still
	within TTL (KNOWN), else mark foreign usage UNKNOWN so ``_pump_usable_credit`` FAILS
	CLOSED (zero new promotions until a snapshot succeeds). Stamps ``last_snapshot_mono``
	for the mid-hop refresh cadence (CDX-17: called from ``_poll_snapshot`` on resolve)."""
	if snap is None:
		try:
			snap = ctx.deps.snapshot(ctx)
		except Exception:
			snap = {"snapshot_ok": False, "gateway_active": None, "active_session_keys": None}
	ctx.last_snapshot_mono = _monotonic()
	# Back-compat: a snapshot double without the CDX-11 key that returns an int is
	# treated as a successful observation.
	ok = snap.get("snapshot_ok")
	ga = snap.get("gateway_active")
	if ok is None:
		ok = ga is not None
	if ok and ga is not None:
		ctx.gateway_active = int(ga)
		ctx.gateway_active_known = True
		ctx.conservative_hold_done = False
		_write_last_known_gateway_active(ctx.relay_target_id, ctx.gateway_active)
		return
	# Snapshot failed — reuse a recent last-known observation if we have one.
	last = _read_last_known_gateway_active(ctx.relay_target_id)
	if last is not None:
		ctx.gateway_active = last
		ctx.gateway_active_known = True
		_telemetry("capacity_last_known", target=ctx.relay_target_id, gateway_active=last)
		return
	# No trustworthy data at all — FAIL CLOSED (zero new promotions until a snapshot
	# succeeds again; in-flight turns unaffected). CDX-11: never admit on unknown capacity.
	ctx.gateway_active_known = False
	_telemetry("capacity_unknown", target=ctx.relay_target_id)


# --------------------------------------------------------------------------- #
# Control-job queue routing (F1 — long-queue self-starvation fix)
# --------------------------------------------------------------------------- #
#
# Pump HOPS ride ``long`` UNCONDITIONALLY (PUMP_QUEUE, §10.4 / S4 — a dead
# ``jarvis_chat`` lane can look provisioned for ~420-480s, longer than a hop, so a
# hop must never be strandable). But the SHORT CONTROL jobs a hop enqueues and then
# DEPENDS ON — ``prepare`` (queued->preparing->ready) and ``finalize`` (enrichment)
# — must NOT share that same ``long`` queue when it has only one live worker: a 90s
# hop occupies the single ``long`` worker for its whole slice budget, so the
# ``prepare`` it just enqueued cannot run, the hop idle-exits before prepare
# finishes, and the turn strands in ``preparing``/``ready`` until the watchdog
# backstop revives it minutes later (the F1 dev-QA finding — 3-5 min strands with 1
# ``long`` worker, ~10s turns with 2). The ORIGINAL bug: these two seams hardcoded
# ``queue="long"`` and never consulted ``_turn_queue`` at all, so even a bench with
# a live ``jarvis_chat`` lane (isolated parallel turn workers) still landed
# prepare/finalize on ``long`` behind the hops.
#
# Routing (F1 ruling):
#   * a live ``jarvis_chat`` lane  -> ride it (isolated parallel workers, exactly as
#     the legacy turn path does via ``api._turn_queue``);
#   * else ``long`` when it has >= 2 live workers (a hop can occupy one while the
#     other runs prepare/finalize — no self-starvation);
#   * else ``short`` — the single-``long``-no-``jarvis_chat`` shape. ``short`` is
#     ALWAYS provisioned, its jobs are bounded, and prepare/finalize carry an
#     EXPLICIT ``timeout=HOP_TIMEOUT_S`` (180s) that fits comfortably under short's
#     300s queue envelope (a vision-heavy 4-page-PDF prepare is ~22s).


def _live_worker_count(queue_name: str) -> int:
	"""Number of RQ workers currently listening on ``queue_name`` (0 on any probe
	trouble). Same ``get_workers()``/``generate_qname`` path ``api._turn_queue``
	uses; best-effort — a probe hiccup must never break an enqueue, so the caller
	treats 0 as "not safely provisioned" and falls back to ``short``."""
	try:
		from frappe.utils.background_jobs import generate_qname, get_workers

		qname = generate_qname(queue_name)
		return sum(1 for w in get_workers() if qname in (w.queue_names() or []))
	except Exception:
		return 0


def _control_queue() -> str:
	"""RQ queue for the pump's CONTROL jobs (prepare + finalize) — see the block
	comment above. Never returns the shared ``long`` queue when ``long`` has fewer
	than 2 live workers (the F1 self-starvation shape); routes to ``short`` there."""
	try:
		from jarvis.chat.api import _turn_queue

		q = _turn_queue()
	except Exception:
		# _turn_queue itself is fully defensive, but if the import/probe blows up,
		# never share the hop queue on an unknown shape — the bounded `short` lane is
		# always a correct executor for these jobs.
		return "short"
	# A dedicated isolated lane (jarvis_chat, or a non-`long` override) never shares
	# a worker with the hops — ride it as the legacy turn path does.
	if q != PUMP_QUEUE:
		return q
	# It resolved to `long` (jarvis_chat absent, or overridden to long): only safe to
	# share with the hops when `long` has >= 2 live workers; otherwise use `short`.
	return PUMP_QUEUE if _live_worker_count(PUMP_QUEUE) >= 2 else "short"


def _pump_shape_starves() -> bool:
	"""True on the F1 self-starvation shape: no live ``jarvis_chat`` lane AND the
	shared ``long`` hop queue has fewer than 2 workers — so a hop and the
	prepare/finalize it depends on would fight over one worker. Drives the loud
	``ensure_pump`` provisioning warning (§8-I)."""
	return _control_queue() == "short"


def _warn_provisioning_if_starved() -> None:
	"""§8-I: emit ONE loud provisioning warning (telemetry line + error log) when the
	site is in the F1 self-starvation shape. Throttled to at most once / 5 min per
	site so the after-every-commit ``ensure_pump`` path never spams. Best-effort."""
	if not _pump_shape_starves():
		return
	try:
		site = frappe.local.site
		key = f"jarvis:pump:provision_warn:{site}"
		if frappe.cache().get_value(key):
			return
		frappe.cache().set_value(key, "1", expires_in_sec=300)
	except Exception:
		# If the throttle store is unavailable, still warn (better loud than silent).
		pass
	long_workers = _live_worker_count(PUMP_QUEUE)
	_telemetry("provision_warning", queue=PUMP_QUEUE, long_workers=long_workers, control_queue="short")
	try:
		frappe.log_error(
			title="pump.provisioning: single-long-no-jarvis_chat",
			message=(
				"Relay Pump provisioning WARNING: this site has no live `jarvis_chat` "
				f"worker lane and only {long_workers} live `long` worker(s). Pump hops "
				"ride `long`; with fewer than 2 `long` workers a 90s hop starves the "
				"prepare/finalize jobs it enqueues (fresh turns strand for minutes until "
				"the watchdog backstop). Control jobs are being routed to `short` as a "
				"mitigation, but the supported shapes are >=2 `long` workers OR a live "
				"`jarvis_chat` lane. See PUMP-RUNBOOK.md §6 (F1)."
			),
		)
	except Exception:
		pass


# --------------------------------------------------------------------------- #
# WP-1d seams + internal test seams (PumpDeps)
# --------------------------------------------------------------------------- #


def _default_dispatch_prepare(run_id: str, relay_target_id: str) -> None:
	"""WP-1d SEAM 1 — prepare dispatcher. Enqueue the short prepare job (D1 stages
	#16-#26 → queued->preparing->ready). WP-1d owns the job body + an
	ATTEMPT-SUFFIXED deduped job_id (OARF-8): the suffix is the turn's ``version``,
	which is STABLE across the same slice's re-offers of a still-`queued` reserved
	turn (dedupe + the idempotent claim CAS make a re-offer a no-op) but CHANGES on
	a genuine new attempt (a park -> recover_to_queued -> re-reserve bumps version).
	A bare fixed id would let a hard-killed prepare's stale STARTED registration
	silently no-op every re-enqueue for the job timeout (~180s) — the dedupe trap
	§10.4 fixed for hops. The default enqueues a conventionally-named job that does
	not exist yet, so it is a no-op-until-WP-1d in production and is ALWAYS replaced
	by tests.

	QUEUE (F1): routed via ``_control_queue`` — a live ``jarvis_chat`` lane else
	``long`` (>=2 workers) else ``short`` — NEVER the single-worker ``long`` the hops
	ride (which would self-starve; see the block comment above)."""
	try:
		frappe.enqueue(
			"jarvis.chat.prepare.run_prepare",
			queue=_control_queue(),
			timeout=HOP_TIMEOUT_S,
			job_id=f"jarvis-prepare::{run_id}::a{_attempt_suffix(run_id)}",
			deduplicate=True,
			run_id=run_id,
			relay_target_id=relay_target_id,
		)
	except Exception:
		frappe.log_error(title="pump.dispatch_prepare", message=frappe.get_traceback())


def _default_enqueue_finalize(run_id: str, relay_target_id: str) -> None:
	"""WP-1d SEAM 2b — finalize invoker. Enqueue the short finalize (enrichment)
	job. Used by settlement (D3 S6) AND by the watchdog for a `finalizing` turn
	(R-13: the watchdog's ONLY legal action there is re-enqueueing finalize).
	ATTEMPT-SUFFIXED deduped job_id (OARF-8): the suffix is the turn's ``version``
	so a watchdog re-enqueue of an in-flight finalize dedupes, while a genuine new
	attempt (finalize_done bumps version on the success flip) gets a fresh id — a
	bare fixed id would let a hard-killed finalize's stale STARTED registration
	no-op re-enqueues for the job timeout (the §10.4 dedupe trap).

	QUEUE (F1): routed via ``_control_queue`` (same rule as prepare) — NEVER the
	single-worker ``long`` the hops ride."""
	try:
		frappe.enqueue(
			"jarvis.chat.finalize.run_finalize",
			queue=_control_queue(),
			timeout=HOP_TIMEOUT_S,
			job_id=f"jarvis-finalize::{run_id}::a{_attempt_suffix(run_id)}",
			deduplicate=True,
			run_id=run_id,
			relay_target_id=relay_target_id,
		)
	except Exception:
		frappe.log_error(title="pump.enqueue_finalize", message=frappe.get_traceback())


def _default_invoke_settlement(
	run_id: str,
	*,
	relay_target_id: str,
	epoch: int,
	version: int,
	terminal_kind: str,
	terminal_payload: dict | None,
	assistant_message: str | None,
	owner: str | None,
	conversation: str,
	deps: "PumpDeps",
) -> None:
	"""WP-1d SEAM 2a — settlement/finalize invoker (D3 Race 3). Pump-invoked and
	EPOCH-fenced: the one critical transaction that writes the final projection,
	releases the conversation slot, fixes the owed-enrichment set, then (after the
	winning commit) publishes the fenced terminal ``run:end`` with
	``enrichment_pending`` (SUX-7) and enqueues finalize.

	WP-1d wires the FULL ownership-table settlement (``jarvis.chat.settlement``):
	final-text projection + slot release + the OAR-9 effect-ledger inserts in ONE
	epoch+version-fenced txn, then the authoritative fenced terminal event
	(``run:end`` w/ ``enrichment_pending`` SUX-7, or ``run:error`` preserving the
	Message.error classification SUX-11) AFTER commit, then ``enqueue_finalize``.
	A 0-rows LOSS raises ``LeaseLostExit`` (D3 S3) — the ``on_terminal`` wrapper
	converts it to the pump's shared exit. Lazy import so pump.py has no import
	cycle with settlement (settlement imports turn_state only)."""
	from jarvis.chat import settlement

	settlement.invoke_settlement(
		run_id,
		relay_target_id=relay_target_id,
		epoch=epoch,
		version=version,
		terminal_kind=terminal_kind,
		terminal_payload=terminal_payload,
		assistant_message=assistant_message,
		owner=owner,
		conversation=conversation,
		deps=deps,
	)


def _default_apply_tool(ctx: "PumpContext", rs: "_RunState", event: dict) -> None:
	"""CDX-5 + CDX-15 — the REAL, EPOCH+STATE+VERSION-FENCED tool-event applier (D1 rows
	#30-32). Two ownership classes:

	  * **built-in openclaw tools** (browser/canvas/image-gen — NOT ``jarvis__*``):
	    the pump OWNS the durable ``role=tool`` receipt. On ``start`` it inserts the
	    row (seq allocated UNDER the conversation lock, R-9) keyed idempotently by the
	    durable ``(conversation, tool_call_id)`` so a hop re-attach / replay never
	    doubles it; on ``end`` it updates ``tool_status``/``streaming=0``.
	  * **``jarvis__*`` callback-owned tools**: the out-of-band ``call_tool`` path
	    persists the receipt row (R-6, exactly-once by its own key) — the pump NEVER
	    owns that row here; it publishes the ``tool:start`` / ``tool:end`` lifecycle
	    ONLY (so the live activity indicator still animates), ``message_id=None``.

	CDX-15 (the fix): the durable tool insert/update commits ONLY together with a CAS
	proving the turn is STILL ``streaming`` under epoch E (``apply_tool_fenced`` —
	version+1 + watermark advance in the SAME txn as the row write). A 0-rows CAS means a
	takeover re-stamped the turn to E+1 or settled it past ``streaming`` — this stale
	pump then writes NOTHING and publishes NOTHING and routes §10.11 (epoch moved =>
	lease loss; intact => benign replay/version drift). This closes the CDX-15 defect
	where a resumed stale pump inserted a ``streaming=1`` tool row into an already-settled
	conversation. The watermark clause also makes a re-attach/replay idempotent.

	Runs on the pump thread inside ``mux.dispatch`` (on_tool flushed the batched deltas
	first, so the on-disk row order matches the frame order). A raise QUARANTINES the
	lane (precious); a ``LeaseLostExit`` is caught by on_tool and converted to the shared
	exit."""
	phase = event.get("phase")
	if phase not in ("start", "end"):
		return
	tool_name = event.get("tool_name")
	tool_call_id = event.get("tool_call_id")
	event_seq = event.get("event_seq")
	is_jarvis = (tool_name or "").startswith("jarvis__")
	conversation = rs.conversation
	owns_row = (not is_jarvis) and bool(tool_call_id)  # pump owns the durable receipt
	need_lock = owns_row and phase == "start"  # seq allocation under the conversation lock
	message_id = None
	committed = False
	try:
		if need_lock:
			# commit-first so the FOR UPDATE lock opens a fresh txn (rank-2 conversation
			# lock; canonical order control->conversation->turn->message — no shard lock is
			# held on the streaming path, so conversation-first is legal). on_tool already
			# flushed + committed any pending delta, so nothing durable is dropped here.
			frappe.db.commit()
			ts._lock_conversation(conversation)
		# CDX-15 fence: same-txn proof the turn is still streaming under epoch E.
		if not ts.apply_tool_fenced(rs.run_id, rs.version, ctx.epoch, event_seq):
			if _epoch_lost(ctx, rs.run_id):
				ts.lease_lost_exit(rs.run_id)  # rolls back + raises; on_tool -> ctx.lease_lost
			# Benign 0-rows (watermark dup / version drift): release the lock, apply
			# nothing, publish nothing, re-sync the version.
			frappe.db.rollback()
			rs.version = _resync_version(rs.run_id)
			return
		# CDX-18: the fence CAS has WON — this uncommitted txn already advanced the Turn
		# version + last_event_seq watermark. If the durable tool-row write or the atomic
		# commit below now raises, RelayMux._apply catches the (precious) exception and
		# invokes quarantine; _park_recovering -> _mark_recovering_mirror would then COMMIT
		# its recovery CAS, and that commit would ALSO flush this half-done txn (watermark
		# advanced, tool row missing => replay skips the precious event). Roll the partial
		# txn back and re-sync the in-memory version from the durable row BEFORE the
		# exception propagates, so quarantine/recovery starts from a FRESH transaction.
		try:
			rs.version += 1
			if owns_row:
				if phase == "start":
					message_id = _insert_tool_start_row(conversation, tool_call_id, tool_name)
				else:
					message_id = _update_tool_end_row(
						conversation, tool_call_id, event.get("status"), rs.run_id
					)
			frappe.db.commit()  # fence + durable tool row commit ATOMICALLY
			committed = True
		except Exception:
			frappe.db.rollback()
			rs.version = _resync_version(rs.run_id)
			raise
	finally:
		if need_lock:
			ts.reset_lock_tracking()
	# Fenced lifecycle publish AFTER the winning commit (P0-3 payload contract: run_id +
	# event_seq + pump_epoch so the client's run-scoped fence dedupes/blocks a stale
	# writer's straggler for this run — CDX-3).
	if committed and rs.owner:
		if phase == "start":
			ts.publish_fenced(
				rs.owner,
				"tool:start",
				conversation_id=conversation,
				run_id=rs.run_id,
				event_seq=event_seq,
				message_id=message_id,
				tool_name=tool_name,
				tool_title=event.get("title"),
				tool_call_id=tool_call_id,
				pump_epoch=ctx.epoch,
				relay_target_id=ctx.relay_target_id,
			)
		else:
			ts.publish_fenced(
				rs.owner,
				"tool:end",
				conversation_id=conversation,
				run_id=rs.run_id,
				event_seq=event_seq,
				message_id=message_id,
				tool_name=tool_name,
				tool_call_id=tool_call_id,
				status=event.get("status"),
				pump_epoch=ctx.epoch,
				relay_target_id=ctx.relay_target_id,
			)


def _insert_tool_start_row(conversation: str, tool_call_id: str, tool_name: str | None) -> str:
	"""CDX-5/CDX-15: insert (or reuse) the durable built-in ``role=tool`` receipt for a
	tool start. Runs INSIDE the caller's fenced txn — the conversation FOR UPDATE lock is
	already held (R-9 seq allocation) and there is NO commit here: the caller commits the
	fence CAS + this row ATOMICALLY (CDX-15). Idempotent on the durable
	``(conversation, tool_call_id)`` key (a re-attach/replay reuses the existing row)."""
	existing = frappe.db.get_value(
		MSG, {"conversation": conversation, "tool_call_id": tool_call_id, "role": "tool"}, "name"
	)
	if existing:
		return existing
	seq = (
		frappe.db.sql(f"SELECT MAX(seq) FROM `tab{MSG}` WHERE conversation=%(c)s", {"c": conversation})[0][0]
		or 0
	) + 1
	doc = frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": conversation,
			"seq": seq,
			"role": "tool",
			"content": f"calling {tool_name}…",
			"tool_name": tool_name,
			"tool_status": "running",
			"tool_call_id": tool_call_id,
			"streaming": 1,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _update_tool_end_row(conversation: str, tool_call_id: str, status: str | None, run_id: str) -> str | None:
	"""CDX-5/CDX-15: close the built-in tool receipt at ``end`` (tool_status +
	streaming=0) INSIDE the caller's fenced txn — NO commit here (the caller commits the
	fence + this update atomically). Idempotent by (conversation, tool_call_id). An
	``end`` with no matching ``start`` row is logged (an openclaw event-ordering
	regression) and returns None — matches legacy ``turn_handler`` orphan handling."""
	name = frappe.db.get_value(
		MSG, {"conversation": conversation, "tool_call_id": tool_call_id, "role": "tool"}, "name"
	)
	if not name:
		frappe.log_error(
			title="pump.apply_tool: orphan tool 'end'",
			message=f"conversation={conversation!r} run_id={run_id!r} tool_call_id={tool_call_id!r}",
		)
		return None
	frappe.db.set_value(
		MSG, name, {"tool_status": status or "completed", "streaming": 0}, update_modified=False
	)
	return name


_SNAPSHOT_FAILED = {"gateway_active": None, "active_session_keys": None, "snapshot_ok": False}


def _parse_snapshot_frame(ctx: "PumpContext", frame: dict) -> dict:
	"""Parse a ``sessions.list`` response frame into the reconcile inputs:

	  ``{"gateway_active": int, "active_session_keys": set[str] | None,
	     "snapshot_ok": bool}``

	``gateway_active`` = FOREIGN ``main`` runs (gateway sessions with ``hasActiveRun``
	not matched to a local in-flight turn) — added to admission inflight so the bench
	never over-admits past a run it did not start (§10.1). ``active_session_keys`` lets
	reconcile tell a genuinely-gone in-flight run from one still active. Any parse error
	=> the CDX-11 fail-CLOSED shape (``snapshot_ok=False``, foreign UNKNOWN, NOT zero)."""
	try:
		payload = frame.get("payload") or frame.get("result") or {}
		sessions = payload.get("sessions") or []
		active_keys = {s.get("key") for s in sessions if s.get("hasActiveRun") and s.get("key")}
		local_keys = _local_active_session_keys(ctx.relay_target_id)
		foreign = len([k for k in active_keys if k not in local_keys])
		return {"gateway_active": foreign, "active_session_keys": active_keys, "snapshot_ok": True}
	except Exception:
		return dict(_SNAPSHOT_FAILED)


class _SnapshotFuture:
	"""CDX-17: adapt a raw ``sessions.list`` RPC future into a POLLABLE snapshot future.
	``done`` proxies the RPC future (so the slice loop can check it without blocking);
	``result(timeout)`` parses the response frame into the reconcile dict and maps ANY
	failure/timeout to the fail-CLOSED shape — so a stalled control RPC can never block
	delta application or fail open."""

	__slots__ = ("_ctx", "_fut")

	def __init__(self, ctx: "PumpContext", fut) -> None:
		self._ctx = ctx
		self._fut = fut

	@property
	def done(self) -> bool:
		return bool(self._fut.done)

	def result(self, timeout: float = 0) -> dict:
		try:
			frame = self._fut.result(timeout)
		except Exception:
			return dict(_SNAPSHOT_FAILED)
		return _parse_snapshot_frame(self._ctx, frame)


def _default_issue_snapshot(ctx: "PumpContext"):
	"""CDX-17: ISSUE the ``sessions.list`` capacity-refresh RPC WITHOUT blocking and
	return a pollable :class:`_SnapshotFuture` (or ``None`` when there is no mux to issue
	on — the caller keeps its last-known view). The reader thread resolves the future;
	``_poll_snapshot`` folds it on a later slice. NEVER waits on ``.result`` here."""
	if ctx.mux is None:
		return None
	try:
		fut = ctx.mux.issue_rpc("sessions.list", {}, timeout_s=ACK_TIMEOUT_S)
	except Exception:
		return None
	return _SnapshotFuture(ctx, fut)


def _default_snapshot(ctx: "PumpContext") -> dict:
	"""Blocking snapshot for the COLD-START reconcile (``_reconcile_on_start``), where
	there is no active stream to freeze. Issues ``sessions.list`` and awaits the frame,
	then parses it (:func:`_parse_snapshot_frame`); any error => the CDX-11 fail-CLOSED
	shape. The MID-HOP refresh does NOT use this — it is issue-and-poll (CDX-17) so a
	stalled control RPC never blocks delta application."""
	try:
		fut = ctx.mux.issue_rpc("sessions.list", {}, timeout_s=ACK_TIMEOUT_S)
		frame = fut.result(ACK_TIMEOUT_S)
	except Exception:
		return dict(_SNAPSHOT_FAILED)
	return _parse_snapshot_frame(ctx, frame)


def _default_make_mux(relay_target_id: str, epoch: int) -> RelayMux:
	"""Construct + start a mux on a FRESH dedicated session for this shard
	(reconnect-per-hop; the pump owns connect, the mux is the I/O adapter). The
	gateway URL is resolved from Jarvis Settings ``agent_url``. Production
	(WP-1e) exercises this; tests inject a transport double."""
	from jarvis.chat.openclaw_client import OpenclawSession

	settings = frappe.get_cached_doc("Jarvis Settings")
	gateway_url = (
		(getattr(settings, "agent_url", "") or "").replace("http://", "ws://").replace("https://", "wss://")
	)
	session = OpenclawSession.connect(gateway_url)
	mux = RelayMux(session, relay_target_id, on_breaker=_on_poison_breaker)
	return mux.start()


def _default_enqueue_pump_job(
	*,
	method: str,
	queue: str,
	timeout: int,
	job_id: str,
	relay_target_id: str,
	hop_counter: int,
	transport_retry: int = 0,
) -> None:
	"""Internal seam — enqueue a hop / watchdog-start job. ALWAYS ``long`` with an
	EXPLICIT ``timeout`` and a FRESH ``job_id`` (§10.4). ``transport_retry`` carries
	the CDX-1 bounded transport-exit budget forward to the successor (0 on a clean
	handoff / cold start; the successor resets it once it drains successfully). Tests
	inject a recorder to assert the args without running the successor."""
	frappe.enqueue(
		method,
		queue=queue,
		timeout=timeout,
		job_id=job_id,
		relay_target_id=relay_target_id,
		hop_counter=hop_counter,
		transport_retry=transport_retry,
	)


@dataclass
class PumpDeps:
	"""Injectable seams. The two WP-1d contracts are ``dispatch_prepare`` (prepare
	dispatcher) and ``invoke_settlement`` + ``enqueue_finalize`` (settlement/
	finalize invoker); the rest are pump-internal test seams. All default to the
	``_default_*`` implementations above."""

	dispatch_prepare: Callable[[str, str], None] = _default_dispatch_prepare
	invoke_settlement: Callable[..., None] = _default_invoke_settlement
	enqueue_finalize: Callable[[str, str], None] = _default_enqueue_finalize
	apply_tool: Callable[..., None] = _default_apply_tool
	snapshot: Callable[["PumpContext"], dict] = _default_snapshot
	issue_snapshot: Callable[["PumpContext"], object] = _default_issue_snapshot
	make_mux: Callable[[str, int], RelayMux] = _default_make_mux
	enqueue_pump_job: Callable[..., None] = _default_enqueue_pump_job


def _default_deps() -> PumpDeps:
	return PumpDeps()


def _on_poison_breaker(target: str, count: int) -> None:
	"""Poison-rate circuit-breaker alarm (OAR-7): telemetry + the mux itself then
	refuses re-adopts (stop-readopting). We only record it here."""
	_telemetry("poison_breaker", target=target, count=count)


# --------------------------------------------------------------------------- #
# Per-run streaming state + the pump context
# --------------------------------------------------------------------------- #


@dataclass
class _RunState:
	"""Pump-thread-local bookkeeping for one in-flight run. ``version`` is tracked
	forward through the run's epoch-fenced CAS chain (confirm_dispatching ->
	mark_streaming -> apply_delta* -> mark_terminal_observed) so each CAS passes
	the exact current version without a re-read on the hot path; a 0-rows CAS
	re-reads to distinguish a benign version drift (concurrent cancel bumps
	version out-of-band) from a real epoch loss."""

	run_id: str
	conversation: str
	owner: str | None
	assistant_message: str | None
	session_key: str
	version: int
	gateway_run_id: str | None = None
	# --- C1/C3/C4 telemetry bookkeeping (WP-1e; observability only) --------- #
	accept_ms: int = 0  # epoch-ms of the turn's enqueued_at (C1 accept baseline)
	first_delta_done: bool = False  # first streamed delta published this run?
	last_publish_mono: float = 0.0  # monotonic of the last delta publish (C3 gap)
	# --- OARF-6 bench-side delta batcher (relocated _AssistantContentBatcher) - #
	# The latest cumulative mirror + the accumulated incremental delta since the
	# last flush; a flush = one apply_delta CAS + commit + publish for the whole
	# batch (N=10 events / 250ms cadence). None => nothing pending.
	pending_seq: int | None = None
	pending_text: str = ""
	pending_delta: str = ""
	events_since_flush: int = 0
	last_flush_mono: float = 0.0


@dataclass
class _PendingAck:
	"""OARF-5: an in-flight ``chat.send`` ack the reactor issued but does NOT block
	on. Polled each slice (``fut.done``); on our slice-level ``deadline`` the
	``ack-timeout`` sentinel path fires (park recovering), not a blocking
	``.result()`` — so a dispatch wave never stalls the streaming lanes."""

	fut: object
	deadline: float
	rs: _RunState
	drained_note_ids: list


@dataclass
class _PendingRecovery:
	"""OARF-5: an in-flight ``sessions.get`` recovery-tail RPC (missed-terminal
	snapshot recovery) the reactor issued without blocking. Polled each slice; on
	resolve the tail is windowed by the turn's watermark (OARF-2) and the turn is
	settled final/errored; on our deadline it re-attaches and waits."""

	fut: object
	deadline: float
	r: dict
	session_key: str
	min_seq: int
	max_seq: int | None


@dataclass
class _PendingSnapshot:
	"""CDX-17: an in-flight ``sessions.list`` capacity-refresh RPC the reactor issued
	WITHOUT blocking. Polled each slice like acks; on resolve/timeout it is folded into
	capacity (``_poll_snapshot`` -> ``_reconcile_gateway_active``). No slice ever blocks
	on it — while it is pending the pump serves with the last-known view (stale-while-
	refreshing, TTL-bounded); on timeout it folds the CDX-11 fail-closed path."""

	fut: object
	deadline: float


@dataclass
class PumpContext:
	relay_target_id: str
	epoch: int
	holder: str
	hop_counter: int
	site: str
	deps: PumpDeps
	mux: RelayMux | None = None
	gateway_active: int = 0
	# CDX-11: whether the last capacity read had trustworthy foreign-usage data.
	# False => admission FAILS CLOSED (zero new promotions until a snapshot succeeds).
	gateway_active_known: bool = True
	conservative_hold_done: bool = False  # retained field (CDX-11 hold telemetry); unused gate
	last_snapshot_mono: float = 0.0
	lease_lost: str | None = None
	soft_deadline: float = 0.0
	hard_deadline: float = 0.0
	last_heartbeat: float = 0.0
	runs: dict[str, _RunState] = field(default_factory=dict)
	peak_occupancy: int = 0  # max concurrent lanes this hop (C4 pump occupancy)
	# OARF-5: in-flight RPCs the reactor polls (never blocks on) each slice.
	pending_acks: dict[str, _PendingAck] = field(default_factory=dict)
	pending_recoveries: dict[str, _PendingRecovery] = field(default_factory=dict)
	# CDX-17: the in-flight mid-hop capacity-refresh RPC (issue-and-poll, never blocked on).
	pending_snapshot: _PendingSnapshot | None = None


# --------------------------------------------------------------------------- #
# The RQ job body — run_pump_hop
# --------------------------------------------------------------------------- #


def run_pump_hop(
	relay_target_id: str,
	*,
	hop_counter: int = 0,
	transport_retry: int = 0,
	deps: PumpDeps | None = None,
	soft_budget_s: float = SOFT_HOP_BUDGET_S,
	hard_deadline_s: float = HARD_HOP_DEADLINE_S,
	max_slices: int | None = None,
) -> dict:
	"""One bounded pump hop (the RQ job body; enqueued to ``long`` with an
	explicit ``timeout``). Lifecycle (D6):

	  1. LEASE ACQUIRE = mini-takeover (EVERY hop, §10.5/R-11): epoch bump +
	     adopted-turn re-stamp, via ``turn_state.lease_acquire``. A lost acquire
	     means another pump owns the shard — return without draining.
	  2. RECONCILE on start (Amendment D): snapshot ``sessions.list``, settle
	     settlement-owed turns from the row (R-12), re-attach in-flight lanes,
	     recover parked turns per origin.
	  3. DRAIN LOOP: call :func:`drain_slice` repeatedly until the soft budget
	     (90s). An idle-exit slice releases the lease and returns.
	  4. HANDOFF at soft budget: bump ``hop_counter``, enqueue the successor
	     ``jarvis-pump::<site>::<target>::hop<n+1>`` with an EXPLICIT
	     ``timeout=180`` to ``long`` ALWAYS (fresh id — frappe dedupe trap), exit.

	``LeaseLostExit`` anywhere ⇒ the ONE shared exit (stop reading, no publishes,
	hop returns). A persistent DB operational error ⇒ bounded backoff then park
	the affected turns ``recovering`` and exit (never spin, D6 §6). ``max_slices``
	/ ``soft_budget_s`` are test knobs; production uses the constants."""
	deps = deps or _default_deps()
	site = frappe.local.site
	holder = f"jarvis-pump::{site}::{relay_target_id}::hop{hop_counter}"

	hop_started_mono = _monotonic()
	won, epoch = ts.lease_acquire(relay_target_id, holder, hop_counter=hop_counter)
	if not won or epoch is None:
		return {"acquired": False, "relay_target_id": relay_target_id}
	_write_lease_mirror(relay_target_id)

	ctx = PumpContext(
		relay_target_id=relay_target_id,
		epoch=epoch,
		holder=holder,
		hop_counter=hop_counter,
		site=site,
		deps=deps,
	)
	now = _monotonic()
	ctx.soft_deadline = now + soft_budget_s
	ctx.hard_deadline = now + hard_deadline_s
	ctx.last_heartbeat = now

	# CDX-21 (Residual B): honour the kill switch at hop START (post-acquire, pre-drain). A hop
	# queued before an operator flipped the row to legacy must NOT begin draining — release the
	# lease WITHOUT succession and park any live turns, so Phase-0/legacy takes over (this IS the
	# "queued successor rechecks the row before processing" boundary). Bounds kill latency to
	# cache TTL + one slice. ``draining`` is not the kill switch (the pump keeps draining in-flight).
	if _kill_switch_engaged(relay_target_id):
		_halt_for_kill_switch(ctx)
		ts.reset_lock_tracking()
		return {"acquired": True, "exit": "kill_switch", "epoch": epoch}

	try:
		ctx.mux = deps.make_mux(relay_target_id, epoch)
	except Exception:
		# Could not build the transport this hop. CDX-1: do NOT exit successor-less —
		# release the lease (epoch-guarded) and enqueue a bounded-backoff successor if
		# the shard still has live work, so a make_mux failure cannot strand a turn
		# until the watchdog. Do not crash the job.
		frappe.log_error(title="pump.make_mux", message=frappe.get_traceback())
		_schedule_successor_on_exit(ctx, transport_retry=transport_retry)
		return {"acquired": True, "exit": "no_transport", "epoch": epoch}

	# C1 (WP-2 Stage B): watch the cross-process accept wake bus on its own thread so
	# an accept's LPUSH interrupts the slice's idle block AT ONCE (event-driven, not
	# the SLICE_BLOCK_S poll ceiling). Best-effort; stopped in the finally.
	wake = _WakeThread(relay_target_id, ctx.mux).start()

	outcome = "handoff"
	op_errors = 0
	try:
		_reconcile_on_start(ctx)
		slices = 0
		while _monotonic() < ctx.soft_deadline and _monotonic() < ctx.hard_deadline:
			try:
				result = drain_slice(ctx)
				op_errors = 0
			except ts.LeaseLostExit:
				outcome = "lease_lost"
				break
			except _operational_errors():
				op_errors += 1
				if op_errors >= DB_BACKOFF_ATTEMPTS:
					_park_affected_recovering(ctx)
					outcome = "db_disconnect"
					break
				_backoff_reconnect(op_errors)
				continue
			slices += 1
			if result == "idle_exit":
				outcome = "idle"
				break
			if result == "transport_closed":
				# D5 §5-d: socket died — end the hop. CDX-1: the exit branch below
				# releases the lease (epoch-guarded) and enqueues an explicit successor,
				# which re-acquires with a fresh socket and reconciles from durable state
				# (re-attach live lanes / snapshot-recover a missed terminal).
				outcome = "transport_closed"
				break
			if max_slices is not None and slices >= max_slices:
				outcome = "max_slices"
				break
		else:
			outcome = "handoff"
		if outcome == "handoff":
			_handoff(ctx)
		elif outcome == "transport_closed":
			# CDX-1: a mid-hop socket death must not exit successor-less — release the
			# lease (epoch-guarded) and enqueue a bounded-backoff successor so a live
			# turn is re-attached/snapshot-recovered by the next hop, not stranded.
			_schedule_successor_on_exit(ctx, transport_retry=transport_retry)
	except ts.LeaseLostExit:
		outcome = "lease_lost"
	finally:
		try:
			wake.stop()
		except Exception:
			pass
		try:
			if ctx.mux is not None:
				ctx.mux.stop()
		except Exception:
			pass
		ts.reset_lock_tracking()
		# C4 pump occupancy + hop_duration_ms (replaces the obsolete worker_hold in
		# pump mode: a hop is a shared drain, not a held worker-per-turn).
		_telemetry(
			"hop",
			target=relay_target_id,
			hop=hop_counter,
			epoch=epoch,
			exit=outcome,
			occupancy=ctx.peak_occupancy,
			duration_ms=round((_monotonic() - hop_started_mono) * 1000.0, 1),
		)
	return {"acquired": True, "exit": outcome, "epoch": epoch}


# --------------------------------------------------------------------------- #
# drain_slice — the reactor body (a standalone function, F/§8-E)
# --------------------------------------------------------------------------- #


def drain_slice(ctx: PumpContext) -> str:
	"""One reactor slice. Returns ``"idle_exit"`` (lease released — the hop exits)
	or ``"continue"``. Steps:

	  1. drain the wake bus (advisory);
	  2. promote queued turns (reserve credit at queued->preparing under the shard
	     lock, then dispatch prepare — the CREDIT gate for leaving `queued`);
	  3. dispatch ready turns (ready->dispatching stamping the epoch, mux
	     ``send_chat`` with the run_id idempotency key, ack -> dispatching->
	     streaming with gateway_run_id; a DEFINITE ok:false rejection ->
	     dispatching->errored + credit release; the ack-timeout sentinel -> park
	     recovering);
	  4. pump the mux reader (apply deltas/tools/terminals via the lane handlers);
	  5. propagate a lease loss signalled from a lane callback;
	  6. cancel-requested sweep (out-of-band ``chat.abort`` then the aborted
	     terminal + settle);
	  7. heartbeat + lease renew (0 rows ⇒ lease-loss exit);
	  8. idle-exit conditional release (0 rows ⇒ CONTINUE, OAR-12).

	Returns ``"transport_closed"`` when the mux socket died (D5 §5-d — only
	socket/lease/site-DB end the whole hop): the hop returns so the successor
	re-acquires with a FRESH socket and reconciles/re-attaches (or snapshot-recovers
	a missed terminal) from durable state, rather than spinning a dead socket to the
	soft budget.
	"""
	drain_wake(ctx.relay_target_id)

	# CDX-11 + CDX-17: issue -> POLL -> promote. ISSUE the due mid-hop capacity refresh
	# (non-blocking), then RESOLVE it BEFORE promotion so an already-resolved refresh that
	# raised the foreign count is folded before ANY cold admission (no stale-count admit),
	# and a due-but-unresolved refresh holds NEW cold promotions at ZERO for this slice
	# (_promote_queued gates on ctx.pending_snapshot). In-flight lanes keep applying deltas
	# regardless — the snapshot poll never blocks and mux.dispatch runs below.
	_maybe_refresh_capacity(ctx)
	_poll_snapshot(ctx)  # CDX-11: fold a resolved refresh (or fail-close a timed-out one) pre-promote
	_promote_queued(ctx)
	_dispatch_ready(ctx)  # OARF-5: issues chat.send acks, parks them (never blocks)
	_poll_pending(ctx)  # OARF-5: resolve done acks/recovery tails; ack-timeout deadlines

	if ctx.mux is not None:
		ctx.mux.dispatch(block_s=SLICE_BLOCK_S)
	if ctx.lease_lost:
		ts.lease_lost_exit(ctx.lease_lost)
	# D5 §5-d: a dead socket ends the hop (the reader's Closing already failed the
	# pending futures + fired on_closing per lane). Keeping the lease held, the
	# successor hop re-attaches from durable state with a fresh connection.
	if ctx.mux is not None and ctx.mux.is_closed():
		return "transport_closed"

	_cancel_sweep(ctx)

	_heartbeat_and_renew(ctx)

	if _idle_exit(ctx):
		return "idle_exit"
	return "continue"


# --------------------------------------------------------------------------- #
# Step 2 — promote (full-credit admission under the shard lock, D3 Race 2)
# --------------------------------------------------------------------------- #


def _maybe_refresh_capacity(ctx: PumpContext) -> None:
	"""CDX-17: on the mid-hop snapshot cadence, ISSUE the ``sessions.list`` capacity
	refresh WITHOUT blocking and park it for ``_poll_snapshot`` (issue-and-poll, exactly
	like acks/recovery tails). NO slice may block on this control RPC — while it is
	pending the pump serves with its last-known foreign count (stale-while-refreshing,
	TTL-bounded by ``_reconcile_gateway_active``). A skipped issue (no mux / issue failed)
	folds a failed snapshot so the CDX-11 fail-closed path engages instead of going stale
	forever. One in-flight refresh at a time (guarded by ``pending_snapshot``)."""
	if ctx.pending_snapshot is not None:
		return  # a refresh is already in flight — do not stack another
	if _monotonic() - ctx.last_snapshot_mono < SNAPSHOT_REFRESH_S:
		return  # not due yet
	try:
		fut = ctx.deps.issue_snapshot(ctx)
	except Exception:
		fut = None
	if fut is None:
		# Cannot issue (no mux / issue failed): fold a failed snapshot (CDX-11 TTL / fail
		# closed) and stamp the cadence so we retry next cycle rather than hot-looping.
		_reconcile_gateway_active(ctx, dict(_SNAPSHOT_FAILED))
		return
	ctx.pending_snapshot = _PendingSnapshot(fut=fut, deadline=_monotonic() + ACK_TIMEOUT_S)


def _poll_snapshot(ctx: PumpContext) -> None:
	"""CDX-17: resolve the parked capacity-refresh RPC WITHOUT blocking — ``fut.done``
	per slice, our slice-level deadline is the ack window. On resolve/timeout fold the
	result into capacity (``_reconcile_gateway_active`` — good snapshot => known + cache;
	failed/timed-out => last-known within TTL else the CDX-11 fail-closed UNKNOWN). While
	pending, do nothing (the pump keeps serving deltas with the last-known view)."""
	ps = ctx.pending_snapshot
	if ps is None:
		return
	if not ps.fut.done and _monotonic() <= ps.deadline:
		return  # still refreshing — never block delta application on it
	ctx.pending_snapshot = None
	try:
		snap = ps.fut.result(0)
	except Exception:
		snap = dict(_SNAPSHOT_FAILED)
	_reconcile_gateway_active(ctx, snap)


def _promote_queued(ctx: PumpContext) -> int:
	"""Reserve credit for eligible `queued` turns at the queued->preparing
	promotion point and enqueue prepare for them (D3 Race 2 / OAR-1/OAR-2).

	LOCK ORDER (OAR-6): control(shard) -> conversation -> turn -> message. We take
	ONLY the shard control row here (``turn_state._lock_shard``), which restates
	the commit-first REPEATABLE-READ discipline: it commits to start a fresh
	transaction so the ``SELECT ... FOR UPDATE`` is the first statement and the
	credit COUNT that follows takes its snapshot AFTER the prior holder committed
	(never a non-locking read before the lock). Both admission call sites (this
	one and accept_send) serialize the credit count+reserve on the SHARD, which is
	what closes the cross-conversation double-admit race (OAR-1). The prepare
	enqueue happens AFTER the commit that releases the lock.

	CDX-17: capacity is refreshed by the issue-and-poll snapshot (``_maybe_refresh_
	capacity`` + ``_poll_snapshot`` on the slice), NOT synchronously here — a stalled
	``sessions.list`` must never block this promotion pass or the delta application that
	follows it. Promote reads the last-known ``gateway_active``/``gateway_active_known``
	view (CDX-11: unknown => zero new promotions)."""
	target = ctx.relay_target_id
	to_prepare: list[str] = []
	if not ctx.gateway_active_known:
		# CDX-11: loud telemetry while admission is held fully closed on unknown capacity.
		_telemetry("capacity_conservative", target=target, held_closed=1)
	try:
		ts._lock_shard(target)  # commit-first; FOR UPDATE is the first statement
		active_convs = _pump_active_convs(target)
		promoted_convs: set[str] = set()

		# (a) queued turns that ALREADY hold a credit (reserve-on-send winners):
		#     dispatch prepare, no re-reserve (they are counted in local_res).
		for row in _pump_queued_reserved(target):
			conv = row["conversation"]
			if conv in active_convs or conv in promoted_convs:
				continue
			to_prepare.append(row["run_id"])
			promoted_convs.add(conv)

		# (b) cold queued turns (reserved=0): reserve under the credit ceiling.
		# CDX-11: a DUE capacity refresh that has not yet resolved (issued this slice or a
		# still-pending prior one) means the foreign count is UNCONFIRMED — hold NEW cold
		# promotions at ZERO for this slice rather than admit against a possibly-stale count
		# (drain_slice polls the refresh BEFORE this pass, so a resolved one is already
		# folded and pending_snapshot is None here). Reserved-already winners (step a) and
		# in-flight lanes are untouched; only new cold admission waits one slice. A timed-out
		# refresh clears pending_snapshot and engages the _pump_usable_credit fail-closed path.
		if ctx.pending_snapshot is not None:
			_telemetry("capacity_refresh_hold", target=target, cold_held=1)
		while ctx.pending_snapshot is None:
			usable = _pump_usable_credit(ctx)
			if usable <= 0:
				break
			candidate = _pick_next_cold(target, active_convs, promoted_convs)
			if candidate is None:
				break
			if ts.reserve_credit(candidate["run_id"]):
				to_prepare.append(candidate["run_id"])
				promoted_convs.add(candidate["conversation"])
			# else lost the row to a concurrent actor; loop re-reads.

		frappe.db.commit()  # releases the shard lock; reservations durable
	except Exception:
		frappe.db.rollback()
		ts.reset_lock_tracking()
		raise
	ts.reset_lock_tracking()

	for run_id in to_prepare:
		try:
			ctx.deps.dispatch_prepare(run_id, target)
		except Exception:
			frappe.log_error(title="pump.promote.dispatch_prepare", message=frappe.get_traceback())
		# C5 queue_wait_ms: how long this turn waited queued before promotion.
		_telemetry("promote", target=target, run_id=run_id, queue_wait_ms=_queue_wait_ms(run_id))
	if to_prepare:
		# SUX-2 in pump mode: a promotion shifts every remaining queued turn's
		# approximate position — republish queue:position (bounded fan-out). Reuses
		# Phase-0's publisher (it selects state='queued' rows, which the pump owns
		# once configured). Best-effort.
		try:
			from jarvis.chat import admission

			admission._publish_queue_positions(target)
		except Exception:
			pass
	return len(to_prepare)


def _pump_usable_credit(ctx: PumpContext) -> int:
	"""usable = hard_cap - inflight - safety_reserve (§9-D2). inflight = local
	reservations (reserved OR dispatching/streaming/terminal_observed, unexpired) +
	FOREIGN gateway active runs.

	CDX-11: the static hard cap is what ships (the design's "learned 60-75%
	utilization controller" is a documented follow-up, NOT implemented — so there is
	no ``min(hard, learned)`` theatre here). When gateway visibility is UNKNOWN (a
	degraded snapshot AND no last-known observation within TTL), admission FAILS CLOSED:
	ZERO new promotions until a snapshot succeeds again (the cap-1 compromise is dropped
	— with an invisible foreign run any positive local admission can oversubscribe the
	container). In-flight turns are unaffected (they already hold their reservation /
	stream; only NEW cold admissions are held). A loud telemetry warning fires while
	held (``_promote_queued``)."""
	from jarvis.chat import admission

	hard = admission._max_inflight()
	local_res = _pump_local_reservations(ctx.relay_target_id)
	if not ctx.gateway_active_known:
		# Fully fail-closed: never admit a NEW turn on top of an unseen foreign run.
		return 0
	inflight = local_res + max(0, int(ctx.gateway_active))
	return hard - inflight - SAFETY_RESERVE


def _pump_local_reservations(target: str) -> int:
	"""D3 Race 2 ``local_res``: turns consuming a credit on the shard — reserved
	(pre-dispatch) OR in-flight (dispatching/streaming/terminal_observed) — whose
	reservation has not expired. An expired UNCLAIMED reservation is excluded so
	its credit auto-reclaims on the next recompute (OAR-5)."""
	return int(
		frappe.db.sql(
			f"""SELECT COUNT(*) FROM `tab{TURN}`
			WHERE relay_target_id=%(t)s
			  AND ( reserved=1 OR state IN ('dispatching','streaming','terminal_observed') )
			  AND ( reservation_expires_at IS NULL OR reservation_expires_at > %(now)s )""",
			{"t": target, "now": ts._now()},
		)[0][0]
	)


def _pump_active_convs(target: str) -> set[str]:
	"""Conversations with a turn already in flight (single-flight scope). CDX-24:
	includes ``recovering`` — a parked turn's old gateway run may still be live and
	owned by turn_recovery, so the pump must NOT promote/reserve a second queued turn
	on that conversation until the parked turn settles (done/errored/cancelled), at
	which point the queued turn promotes on the next slice. Single-flight scope ONLY;
	credit accounting (``_pump_local_reservations``/``_shard_inflight``) is unchanged,
	so other conversations keep dispatching against the freed capacity."""
	rows = frappe.db.sql(
		f"""SELECT DISTINCT conversation FROM `tab{TURN}`
		WHERE relay_target_id=%(t)s
		  AND state IN ('preparing','ready','dispatching','streaming','terminal_observed','recovering')""",
		{"t": target},
	)
	return {r[0] for r in rows}


def _pump_queued_reserved(target: str) -> list[dict]:
	return frappe.db.sql(
		f"""SELECT run_id, conversation FROM `tab{TURN}`
		WHERE relay_target_id=%(t)s AND state='queued' AND reserved=1
		  AND ( reservation_expires_at IS NULL OR reservation_expires_at > %(now)s )
		ORDER BY CASE turn_class WHEN 'interactive' THEN 0 ELSE 1 END, enqueued_at ASC, run_id ASC""",
		{"t": target, "now": ts._now()},
		as_dict=True,
	)


def _pick_next_cold(target: str, active_convs: set[str], promoted_convs: set[str]) -> dict | None:
	"""Choose the next cold (reserved=0) queued turn to reserve — weighted classes
	with a background floor of 1 (SUX-4a: when background work is queued,
	interactive holds at most cap-1 credits), per-conversation single-flight."""
	rows = frappe.db.sql(
		f"""SELECT run_id, conversation, turn_class FROM `tab{TURN}`
		WHERE relay_target_id=%(t)s AND state='queued' AND reserved=0
		ORDER BY enqueued_at ASC, run_id ASC LIMIT 200""",
		{"t": target},
		as_dict=True,
	)

	def eligible(r: dict) -> bool:
		conv = r["conversation"]
		return conv not in active_convs and conv not in promoted_convs

	interactive = next((r for r in rows if r["turn_class"] == "interactive" and eligible(r)), None)
	background = next((r for r in rows if r["turn_class"] == "background" and eligible(r)), None)

	if interactive and background:
		from jarvis.chat import admission

		cap = admission._max_inflight()
		int_inflight = _turn_state_count(target, "interactive")
		if cap >= 2 and int_inflight >= cap - 1:
			return background
		return interactive
	return interactive or background


def _turn_state_count(target: str, turn_class: str) -> int:
	return int(
		frappe.db.sql(
			f"""SELECT COUNT(*) FROM `tab{TURN}`
			WHERE relay_target_id=%(t)s AND turn_class=%(k)s
			  AND state IN ('preparing','ready','dispatching','streaming','terminal_observed')""",
			{"t": target, "k": turn_class},
		)[0][0]
	)


# --------------------------------------------------------------------------- #
# Step 3 — dispatch ready turns (ready -> dispatching -> streaming)
# --------------------------------------------------------------------------- #


def _dispatch_ready(ctx: PumpContext) -> int:
	target = ctx.relay_target_id
	rows = frappe.db.sql(
		f"""SELECT run_id FROM `tab{TURN}`
		WHERE relay_target_id=%(t)s AND state='ready'
		ORDER BY ready_at ASC, run_id ASC LIMIT 50""",
		{"t": target},
		as_dict=True,
	)
	dispatched = 0
	for r in rows:
		if _dispatch_one(ctx, r["run_id"]):
			dispatched += 1
	return dispatched


def _dispatch_one(ctx: PumpContext, run_id: str) -> bool:
	"""ready -> dispatching (stamp epoch, CONFIRM-only) then ISSUE ``chat.send``
	WITHOUT blocking on its ack (OARF-5): the ack future is parked in
	``ctx.pending_acks`` and resolved by ``_poll_pending`` on a later slice, so a
	dispatch wave never stalls the streaming lanes' delta application. Returns True
	if the send was issued."""
	turn = _read_dispatch_row(run_id)
	if turn is None or turn["state"] != "ready":
		return False

	# ready -> dispatching: E is FIRST stamped on the turn here, but the CAS now proves
	# the SHARD control row still holds epoch E (confirm_dispatching's EXISTS clause,
	# CDX-2) so a stale pump that read this ready row before a takeover cannot win. On
	# 0 rows, disambiguate on the CONTROL epoch (the turn has no epoch yet): a moved
	# shard epoch => a takeover happened => shared lease-loss exit; an intact shard
	# epoch => an ordinary concurrent cancel/version drift => skip (§10.11).
	if not ts.confirm_dispatching(run_id, int(turn["version"]), ctx.epoch, ctx.relay_target_id):
		if _shard_epoch_lost(ctx):
			ts.lease_lost_exit(run_id)
		return False
	frappe.db.commit()

	owner = frappe.db.get_value(CONV, turn["conversation"], "owner")
	dispatch = _load_dispatch(turn)
	rs = _RunState(
		run_id=run_id,
		conversation=turn["conversation"],
		owner=owner,
		assistant_message=turn.get("assistant_message"),
		session_key=dispatch["session_key"],
		version=int(turn["version"]) + 1,
		gateway_run_id=None,
		accept_ms=_dt_to_ms(turn.get("enqueued_at")),
	)
	ctx.runs[run_id] = rs
	ctx.peak_occupancy = max(ctx.peak_occupancy, len(ctx.runs))

	# run:start is pump-owned + epoch-fenced (R-1): the browser's "running" signal
	# comes only from the writer that actually owns the stream. message_id is REQUIRED
	# by today's ChatView run:start consumer (it pins currentMsgId so Stop can pin the
	# reply even before the first token) — legacy always sends it.
	if owner:
		ts.publish_fenced(
			owner,
			"run:start",
			conversation_id=rs.conversation,
			run_id=run_id,
			message_id=rs.assistant_message,
			pump_epoch=ctx.epoch,
			relay_target_id=ctx.relay_target_id,
		)

	handler = _make_handler(ctx, rs)
	try:
		fut = ctx.mux.send_chat(
			rs.session_key,
			dispatch["message"],
			run_id,
			handler,
			timeout_s=ACK_TIMEOUT_S,
			start_seq=int(turn.get("last_event_seq") or 0),
			thinking=dispatch.get("thinking"),
			attachments=dispatch.get("attachments"),
		)
	except OpenclawUnreachableError as exc:
		# Immediate send failure (socket write failed) — resolve inline (rare).
		_handle_ack_failure(ctx, rs, exc)
		return False
	# Park the ack: polled by _poll_pending; on our deadline the ack-timeout
	# sentinel path fires (park recovering), never a blocking .result().
	ctx.pending_acks[run_id] = _PendingAck(
		fut=fut,
		deadline=_monotonic() + ACK_TIMEOUT_S,
		rs=rs,
		drained_note_ids=dispatch.get("drained_note_ids"),
	)
	return True


def _poll_pending(ctx: PumpContext) -> None:
	"""OARF-5: resolve every in-flight ack / recovery-tail / capacity-refresh RPC
	WITHOUT blocking — ``fut.done`` checks per slice, our slice-level deadline is the
	ack-timeout. A real epoch loss raised here (via ``lease_lost_exit``) propagates to
	the shared hop exit."""
	_poll_acks(ctx)
	_poll_recoveries(ctx)
	# NB: the capacity-refresh snapshot is polled in drain_slice BEFORE promote (CDX-11 —
	# issue -> poll -> promote), not here, so a resolved refresh folds ahead of admission.


def _poll_acks(ctx: PumpContext) -> None:
	for run_id in list(ctx.pending_acks):
		pa = ctx.pending_acks[run_id]
		done = pa.fut.done
		if not done and _monotonic() <= pa.deadline:
			continue  # still in flight, under the ack window — leave for a later slice
		ctx.pending_acks.pop(run_id, None)
		try:
			# done -> returns the frame or raises the stored exc; not-done + past our
			# deadline -> result(0) cleans up the mux map and raises the ack-timeout
			# sentinel (the timeout, not a blocking wait).
			ack = pa.fut.result(0)
		except OpenclawUnreachableError as exc:
			_handle_ack_failure(ctx, pa.rs, exc)
			continue
		_on_ack_success(ctx, pa, ack)


def _on_ack_success(ctx: PumpContext, pa: _PendingAck, ack: dict) -> None:
	"""Process a resolved ``chat.send`` ack: rekey to the gateway run id if it
	differs, then dispatching -> streaming (epoch-fenced). A real 0-rows epoch
	loss routes through the shared lease-loss exit."""
	rs = pa.rs
	payload = ack.get("payload") or ack.get("result") or {}
	gw = payload.get("runId") or rs.run_id
	if gw != rs.run_id and ctx.mux is not None:
		ctx.mux.rekey_run(rs.run_id, gw)
	rs.gateway_run_id = gw
	if ts.mark_streaming(rs.run_id, rs.version, ctx.epoch, gateway_run_id=gw):
		rs.version += 1
		frappe.db.commit()
		# R-2: the ack PROVES delivery, so clear EXACTLY the agent-correction notes
		# prepare folded into this prompt (id-keyed, idempotent) — never on an
		# ack-timeout. Best-effort.
		_clear_agent_notes_on_ack(rs.conversation, pa.drained_note_ids)
		return
	# 0 rows: distinguish a real epoch loss from a benign version drift (e.g. a
	# concurrent cancel bumped version out-of-band).
	if _epoch_lost(ctx, rs.run_id):
		ts.lease_lost_exit(rs.run_id)
	rs.version = _resync_version(rs.run_id)


def _handle_ack_failure(ctx: PumpContext, rs: _RunState, exc: OpenclawUnreachableError) -> bool:
	"""Ack did not resolve OK. The ``ack-timeout`` sentinel (also the Closing/WS
	drop sentinel, OAR-10) is AMBIGUOUS — the request was written, the peer may
	have accepted it — so park recovering. A DEFINITE pre-ack rejection (ok:false
	with a concrete code) is dispatching->errored + credit release (OAR-8)."""
	run_id = rs.run_id
	code = getattr(exc, "code", None)
	if code == "ack-timeout":
		_park_recovering(ctx, run_id, reason="ack-timeout")
		return False
	# Definite rejection.
	turn = ts.read_turn(run_id)
	if turn is None:
		return False
	err = str(exc)
	if ts.dispatch_errored(run_id, int(turn["version"]), ctx.epoch, error=err):
		# Mark the placeholder errored (streaming off + error) so a reload renders the
		# failure instead of a stuck spinner (matches legacy _mark_errored).
		if rs.assistant_message:
			try:
				ts._run_cas(
					f"UPDATE `tab{MSG}` SET streaming=0, error=%(e)s WHERE name=%(m)s",
					{"e": err[:1000], "m": rs.assistant_message},
				)
			except Exception:
				pass
		frappe.db.commit()
		if rs.owner:
			# SUX-11: a definite pre-ack rejection is a real error — publish run:error
			# with today's classification code + message_id (not a bare run:end).
			# SUXF-2: carry changed_data=False — this is a pre-ack rejection, the run
			# never started, so "No changes were made to your data" is honest (parity
			# with prepare._prepare_error + legacy turn_handler._publish_run_error).
			ts.publish_fenced(
				rs.owner,
				"run:error",
				conversation_id=rs.conversation,
				run_id=run_id,
				message_id=rs.assistant_message,
				error=err,
				code=_classify_error(err),
				changed_data=False,
				pump_epoch=ctx.epoch,
				relay_target_id=ctx.relay_target_id,
			)
		return False
	if _epoch_lost(ctx, run_id):
		ts.lease_lost_exit(run_id)
	return False


# --------------------------------------------------------------------------- #
# Lane handlers — wire the mux integrity classes to turn_state CAS + publishes
# --------------------------------------------------------------------------- #


def _flush_deltas(ctx: PumpContext, rs: _RunState) -> None:
	"""OARF-6: flush the batched cumulative mirror — ONE ``apply_delta`` CAS +
	commit + fenced ``assistant:delta`` publish for the whole accumulated batch.
	No-op when nothing is pending. Raises ``LeaseLostExit`` on a real epoch loss
	(the caller converts it to ``ctx.lease_lost``); a benign 0-rows (watermark dup
	/ version drift) re-syncs the version and returns."""
	if rs.pending_seq is None:
		return
	seq, text, delta = rs.pending_seq, rs.pending_text, rs.pending_delta
	rs.pending_seq = None
	rs.pending_delta = ""
	rs.events_since_flush = 0
	rs.last_flush_mono = _monotonic()
	won = ts.apply_delta(
		run_id=rs.run_id,
		version=rs.version,
		epoch=ctx.epoch,
		event_seq=seq,
		assistant_message=rs.assistant_message,
		content=text,
	)
	if won:
		rs.version += 1
		frappe.db.commit()
		if rs.owner:
			# SUX-1/SUX-6: the event name + payload MUST match what today's ChatView
			# already consumes — it renders on `assistant:delta` with {message_id,
			# text} (cumulative mirror). publish_fenced adds (turn_id, event_seq) so
			# the client dedupes a replayed frame. `delta` is the accumulated
			# incremental fragment since the last flush.
			ts.publish_fenced(
				rs.owner,
				"assistant:delta",
				conversation_id=rs.conversation,
				run_id=rs.run_id,
				event_seq=seq,
				message_id=rs.assistant_message,
				text=text,
				delta=delta,
				pump_epoch=ctx.epoch,
				relay_target_id=ctx.relay_target_id,
			)
			_emit_stream_telemetry(rs)
	else:
		# Benign (watermark dup / version drift) vs real epoch loss.
		if _epoch_lost(ctx, rs.run_id):
			ts.lease_lost_exit(rs.run_id)
		rs.version = _resync_version(rs.run_id)


def _flush_all_pending(ctx: PumpContext) -> None:
	"""OARF-6: flush every active lane's batched deltas (called at hop handoff so a
	sub-threshold tail batch is not left in memory across the hop boundary). A lease
	loss during a flush ends the hop via the shared exit. Best-effort otherwise."""
	for rs in list(ctx.runs.values()):
		try:
			_flush_deltas(ctx, rs)
		except ts.LeaseLostExit:
			ctx.lease_lost = rs.run_id
		except Exception:
			frappe.log_error(title="pump.flush_pending", message=frappe.get_traceback())


def _make_handler(ctx: PumpContext, rs: _RunState) -> LaneHandler:
	"""Build the per-turn lane callbacks (they run ON THE PUMP THREAD inside
	``mux.dispatch``). A ``LeaseLostExit`` raised inside a callback is CAUGHT and
	converted into ``ctx.lease_lost`` (the mux would otherwise treat a raise as
	poison and quarantine the lane); ``drain_slice`` raises the shared exit after
	``dispatch`` returns. Any OTHER exception propagates so the mux applies its
	integrity class (LOSSY delta -> drop+count+continue; PRECIOUS tool/terminal ->
	quarantine)."""

	def on_delta(event_seq: int, text: str, delta: str) -> None:
		# OARF-6: BATCH the cumulative mirror (relocated _AssistantContentBatcher).
		# Record the latest cumulative text + accumulate the incremental delta, then
		# flush (one apply_delta CAS + commit + publish for the whole batch) only
		# when the size (N=10) or time (250ms) threshold trips — the first delta
		# always flushes immediately (first-token latency, C1). openclaw already
		# coalesces at ~150ms, so this is bench-side de-duplication of commits+
		# publishes, not a change to what the user eventually sees (cumulative).
		if ctx.lease_lost:
			return
		try:
			rs.pending_seq = event_seq
			rs.pending_text = text
			rs.pending_delta = (rs.pending_delta + delta) if rs.pending_delta else delta
			rs.events_since_flush += 1
			if rs.events_since_flush >= _DELTA_BATCH_SIZE or (
				(_monotonic() - rs.last_flush_mono) * 1000.0 >= _DELTA_BATCH_INTERVAL_MS
			):
				_flush_deltas(ctx, rs)
		except ts.LeaseLostExit:
			ctx.lease_lost = rs.run_id

	def on_tool(event: dict) -> None:
		# PRECIOUS: a raise here quarantines the lane. The out-of-band receipt
		# writer (R-6) is WP-1d; the default is a no-op. Flush any batched deltas
		# FIRST so the on-disk mirror + realtime order matches the frame order
		# (ordering: flush before any non-delta event, verbatim legacy batcher).
		if ctx.lease_lost:
			return
		try:
			_flush_deltas(ctx, rs)
		except ts.LeaseLostExit:
			ctx.lease_lost = rs.run_id
			return
		ctx.deps.apply_tool(ctx, rs, event)

	def on_terminal(kind: str, payload: dict) -> None:
		if ctx.lease_lost:
			return
		try:
			# Flush the last batched cumulative mirror before the terminal so the
			# Message row holds the full streamed text if settlement carries no final.
			_flush_deltas(ctx, rs)
			won = ts.mark_terminal_observed(rs.run_id, rs.version, ctx.epoch, kind, payload)
			if not won:
				if _epoch_lost(ctx, rs.run_id):
					ts.lease_lost_exit(rs.run_id)
				return
			rs.version += 1
			frappe.db.commit()
			ctx.deps.invoke_settlement(
				rs.run_id,
				relay_target_id=ctx.relay_target_id,
				epoch=ctx.epoch,
				version=rs.version,
				terminal_kind=kind,
				terminal_payload=payload,
				assistant_message=rs.assistant_message,
				owner=rs.owner,
				conversation=rs.conversation,
				deps=ctx.deps,
			)
		except ts.LeaseLostExit:
			ctx.lease_lost = rs.run_id

	def on_quarantine(reason: str) -> None:
		# The mux fenced this lane off (precious fault / overflow). Flush any batched
		# deltas (so partial content persists), then park the turn toward recovering
		# (out of band, D5 §5-c).
		try:
			_flush_deltas(ctx, rs)
			_park_recovering(ctx, rs.run_id, reason=reason)
		except ts.LeaseLostExit:
			ctx.lease_lost = rs.run_id

	def on_closing(sentinel: str) -> None:
		# Transport lost — nothing durable to write here. on_closing runs on the mux
		# READER thread (fired from _begin_closing), which has NO frappe DB
		# connection, so it must NEVER touch the DB (no _flush_deltas). Any batched
		# in-memory deltas are re-derived by the next hop, which re-attaches from the
		# durable watermark (last_event_seq) and the cumulative mirror self-heals.
		return None

	return LaneHandler(
		on_delta=on_delta,
		on_tool=on_tool,
		on_terminal=on_terminal,
		on_quarantine=on_quarantine,
		on_closing=on_closing,
	)


# --------------------------------------------------------------------------- #
# Step 6 — cancel-requested sweep (out-of-band abort -> aborted terminal)
# --------------------------------------------------------------------------- #


def _cancel_sweep(ctx: PumpContext) -> int:
	"""For in-flight turns the web sender flagged ``cancel_requested`` (D2 row 17),
	drive the out-of-band ``chat.abort`` (the bus is never the only abort route,
	Amendment D), record the aborted terminal (D2 row 18), then settle to
	``cancelled`` (D2 row 19 via the settlement seam)."""
	target = ctx.relay_target_id
	rows = frappe.db.sql(
		f"""SELECT run_id, state, version, gateway_run_id, conversation, assistant_message
		FROM `tab{TURN}`
		WHERE relay_target_id=%(t)s AND cancel_requested=1
		  AND state IN ('dispatching','streaming') AND pump_epoch=%(e)s""",
		{"t": target, "e": ctx.epoch},
		as_dict=True,
	)
	swept = 0
	for r in rows:
		run_id = r["run_id"]
		rs = ctx.runs.get(run_id)
		session_key = (
			rs.session_key if rs else _load_dispatch(_read_dispatch_row(run_id) or {}).get("session_key", "")
		)
		# Out-of-band abort (best-effort). OARF-5: ISSUE the abort without blocking on
		# its ack — the abort result is never consulted (the terminal frame arrives
		# regardless and the mux cancels the future on stop), so a per-abort .result()
		# wait would only stall the reactor. We record the aborted terminal from the
		# row + settle below, independent of the abort ack.
		try:
			if ctx.mux is not None and session_key:
				ctx.mux.abort(session_key, r.get("gateway_run_id"), timeout_s=ACK_TIMEOUT_S)
		except Exception:
			pass
		if not ts.record_aborted_terminal(run_id, r["state"], int(r["version"]), ctx.epoch):
			if _epoch_lost(ctx, run_id):
				ts.lease_lost_exit(run_id)
			continue
		frappe.db.commit()
		owner = frappe.db.get_value(CONV, r["conversation"], "owner")
		try:
			ctx.deps.invoke_settlement(
				run_id,
				relay_target_id=target,
				epoch=ctx.epoch,
				version=int(r["version"]) + 1,
				terminal_kind="relay:error",
				terminal_payload={"aborted": True},
				assistant_message=r.get("assistant_message"),
				owner=owner,
				conversation=r["conversation"],
				deps=ctx.deps,
			)
		except ts.LeaseLostExit:
			ctx.lease_lost = run_id
			raise
		swept += 1
	return swept


# --------------------------------------------------------------------------- #
# Step 7 — heartbeat + lease renew
# --------------------------------------------------------------------------- #


def _heartbeat_and_renew(ctx: PumpContext) -> None:
	"""Write ``loop_heartbeat_ts`` (loop-liveness, DISTINCT from lease renewal) and
	renew the lease, at most once per HEARTBEAT_INTERVAL_S. A 0-rows renew/
	heartbeat means the epoch was lost to a takeover ⇒ shared lease-loss exit."""
	now = _monotonic()
	if now - ctx.last_heartbeat < HEARTBEAT_INTERVAL_S:
		return
	ctx.last_heartbeat = now
	if not ts.lease_heartbeat(ctx.relay_target_id, ctx.epoch):
		ts.lease_lost_exit()
	if not ts.lease_renew(ctx.relay_target_id, ctx.epoch, holder=ctx.holder):
		ts.lease_lost_exit()
	_write_lease_mirror(ctx.relay_target_id)


# --------------------------------------------------------------------------- #
# Step 8 — idle-exit conditional release (D6 §2, OAR-12)
# --------------------------------------------------------------------------- #


def _idle_exit(ctx: PumpContext) -> bool:
	"""Idle exit = ONE atomic conditional release (never check-then-release). The
	release affects 1 row ONLY if this pump still holds the epoch AND no
	nonterminal turn remains on the shard. Acting on the affected-rows count is
	NORMATIVE (OAR-12): 0 rows ⇒ work remains (a turn committed during exit) OR
	the epoch was lost ⇒ KEEP the lease and CONTINUE the drain loop. True ⇒
	released, the hop exits."""
	if ts.lease_release_if_idle(ctx.relay_target_id, ctx.epoch):
		_clear_lease_mirror(ctx.relay_target_id)
		return True
	return False


# --------------------------------------------------------------------------- #
# Reconcile on start (Amendment D) + recovery parking
# --------------------------------------------------------------------------- #


def _reconcile_on_start(ctx: PumpContext) -> None:
	"""Runs on EVERY hop start/reconnect (not just crashes). ``lease_acquire``
	already re-stamped adopted in-flight turns (dispatching/streaming/
	terminal_observed) to the new epoch and bumped their version (D4-c). Here we:
	  * pull the snapshot (foreign gateway active count for admission);
	  * SETTLE settlement-owed turns from the row (R-12 — zero gateway round-trips);
	  * RE-ATTACH in-flight lanes so the future-only broadcast resumes;
	  * recover parked (`recovering`) turns per origin (OAR-4)."""
	target = ctx.relay_target_id
	try:
		snap = ctx.deps.snapshot(ctx)
	except Exception:
		snap = {"snapshot_ok": False, "gateway_active": None, "active_session_keys": None}
	# CDX-11: fold the snapshot into capacity WITHOUT failing open — a failed snapshot
	# keeps the last-known foreign count (TTL) or goes conservative, never zero.
	_reconcile_gateway_active(ctx, snap)
	active_keys = snap.get("active_session_keys")

	rows = frappe.db.sql(
		f"""SELECT run_id, state, version, conversation, assistant_message,
		       last_event_seq, gateway_run_id, dispatching_at, recovery_started_at
		FROM `tab{TURN}`
		WHERE relay_target_id=%(t)s
		  AND state IN ('dispatching','streaming','terminal_observed','recovering')""",
		{"t": target},
		as_dict=True,
	)
	for r in rows:
		try:
			_reconcile_one(ctx, r, active_keys)
		except ts.LeaseLostExit:
			raise
		except Exception:
			frappe.log_error(title="pump.reconcile", message=frappe.get_traceback())


def _reconcile_one(ctx: PumpContext, r: dict, active_keys) -> None:
	run_id = r["run_id"]
	state = r["state"]

	if state == "terminal_observed":
		# Settlement owed (R-12). Settle from the row.
		owner = frappe.db.get_value(CONV, r["conversation"], "owner")
		ctx.deps.invoke_settlement(
			run_id,
			relay_target_id=ctx.relay_target_id,
			epoch=ctx.epoch,
			version=int(r["version"]),
			terminal_kind=frappe.db.get_value(TURN, run_id, "terminal_kind"),
			terminal_payload=_json_or_none(frappe.db.get_value(TURN, run_id, "terminal_payload")),
			assistant_message=r.get("assistant_message"),
			owner=owner,
			conversation=r["conversation"],
			deps=ctx.deps,
		)
		return

	if state == "recovering":
		if r.get("dispatching_at") is None:
			# Parked PRE-dispatch (OAR-4): back to queued for a FRESH prepare.
			ts.recover_to_queued(run_id, int(r["version"]))
			frappe.db.commit()
			return
		# Parked IN-flight: adopt (re-stamp epoch), then re-attach or snapshot-recover.
		if ts.recover_adopt(run_id, int(r["version"]), ctx.epoch, target_state="streaming"):
			frappe.db.commit()
			r = {**r, "version": int(r["version"]) + 1, "state": "streaming"}
			_reattach_or_recover(ctx, r, active_keys)
		return

	# dispatching / streaming — already re-stamped in place by lease_acquire.
	_reattach_or_recover(ctx, r, active_keys)


def _reattach_or_recover(ctx: PumpContext, r: dict, active_keys) -> None:
	"""For an adopted in-flight turn: if the gateway snapshot shows its session no
	longer has an active run, the terminal was MISSED during the disconnect — settle
	from the durable tail (D2 row 23, snapshot recovery, Amendment D). Otherwise
	re-attach the lane so the future-only broadcast resumes.

	OARF-2: ONLY a `streaming` turn that ACTUALLY streamed frames
	(``last_event_seq > 0``) is missed-terminal eligible — a bare dispatching turn
	adopted to streaming (acked, no observed output) is re-attached, never settled
	from a session-wide tail (which could hold a PRIOR turn's answer). OARF-5: the
	recovery tail RPC is ISSUED non-blocking and resolved by ``_poll_recoveries``,
	so a crash-reconcile of N gone turns does not serialize N×15s of blocking waits
	off the delta-draining critical path."""
	if r.get("state") == "streaming" and active_keys is not None and int(r.get("last_event_seq") or 0) > 0:
		session_key = _load_dispatch(_read_dispatch_row(r["run_id"]) or {}).get("session_key")
		if session_key and session_key not in active_keys:
			_issue_recovery_tail(ctx, r, session_key)
			return
	_reattach_lane(ctx, r)


def _issue_recovery_tail(ctx: PumpContext, r: dict, session_key: str) -> None:
	"""OARF-5: issue the missed-terminal ``sessions.get`` recovery-tail RPC WITHOUT
	blocking and park it (with the OARF-2 watermark window) for ``_poll_recoveries``.
	Falls back to re-attach when the mux is unavailable."""
	if ctx.mux is None:
		_reattach_lane(ctx, r)
		return
	min_seq, max_seq = _recovery_window(r)
	try:
		fut = ctx.mux.issue_rpc("sessions.get", {"key": session_key}, timeout_s=ACK_TIMEOUT_S)
	except Exception:
		_reattach_lane(ctx, r)
		return
	ctx.pending_recoveries[r["run_id"]] = _PendingRecovery(
		fut=fut,
		deadline=_monotonic() + ACK_TIMEOUT_S,
		r=r,
		session_key=session_key,
		min_seq=min_seq,
		max_seq=max_seq,
	)


def _poll_recoveries(ctx: PumpContext) -> None:
	"""OARF-5: resolve parked recovery-tail RPCs WITHOUT blocking. On resolve the
	tail is windowed by the turn's watermark and the turn is settled (OARF-2); on
	the deadline / an unavailable RPC the lane re-attaches and waits (never
	fabricates a settlement)."""
	for run_id in list(ctx.pending_recoveries):
		pr = ctx.pending_recoveries[run_id]
		if not pr.fut.done and _monotonic() <= pr.deadline:
			continue
		ctx.pending_recoveries.pop(run_id, None)
		try:
			frame = pr.fut.result(0)
		except Exception:
			# tail RPC timed out / unavailable — re-attach and keep waiting.
			_reattach_lane(ctx, pr.r)
			continue
		_resolve_recovery_tail(ctx, pr, frame)


def _recovery_window(r: dict) -> tuple[int, int | None]:
	"""OARF-2 recovery window for a turn: ``min_seq`` = the turn's
	``openclaw_seq_watermark`` (captured by prepare BEFORE this turn's chat.send —
	a transcript message at/below it predates this turn), ``max_seq`` = the next
	turn's watermark (a message above it belongs to a later turn). Identical bound
	to the legacy ``turn_recovery`` fix for the same 'recovered with the next
	question's reply' incident."""
	am = r.get("assistant_message")
	if not am:
		return 0, None
	row = frappe.db.get_value(MSG, am, ["openclaw_seq_watermark", "seq"], as_dict=True) or {}
	min_seq = int(row.get("openclaw_seq_watermark") or 0)
	max_seq = None
	if row.get("seq"):
		from jarvis.chat.turn_recovery import _next_turn_watermark

		max_seq = _next_turn_watermark(r["conversation"], int(row["seq"]))
	return min_seq, max_seq


def _resolve_recovery_tail(ctx: PumpContext, pr: _PendingRecovery, frame: dict) -> None:
	"""OARF-2: settle a missed-terminal turn from the WINDOWED durable tail. The
	newest assistant text WITHIN [min_seq, max_seq] wins; if the window is empty
	the run genuinely ended with NO output beyond the watermark, so settle
	``errored`` honestly — NEVER adopt content from before the watermark (a prior
	turn's answer), Amendment D 'never fabricate'."""
	payload = frame.get("payload") or frame.get("result") or {}
	messages = payload.get("messages") or []
	from jarvis.chat.turn_recovery import _latest_assistant_text

	text = _latest_assistant_text(messages, min_seq=pr.min_seq, max_seq=pr.max_seq)
	if text:
		_settle_recovered_final(ctx, pr.r, text)
	else:
		_settle_recovered_errored(ctx, pr.r)


def _settle_recovered_final(ctx: PumpContext, r: dict, text: str) -> None:
	"""Genuine missed-terminal recovery: advance streaming->terminal_observed under
	this epoch with the in-window durable text, mark ``was_recovered`` (SUX-6 — the
	client may do a visible replacement), and settle to final."""
	run_id = r["run_id"]
	v = int(r["version"])
	payload = {"text": text}
	if not ts.mark_terminal_observed(run_id, v, ctx.epoch, "relay:final", payload):
		if _epoch_lost(ctx, run_id):
			ts.lease_lost_exit(run_id)
		return
	try:
		frappe.db.set_value(TURN, run_id, "was_recovered", 1, update_modified=False)
		if r.get("assistant_message"):
			frappe.db.set_value(MSG, r["assistant_message"], "recovering", 0, update_modified=False)
	except Exception:
		pass
	frappe.db.commit()
	owner = frappe.db.get_value(CONV, r["conversation"], "owner")
	ctx.deps.invoke_settlement(
		run_id,
		relay_target_id=ctx.relay_target_id,
		epoch=ctx.epoch,
		version=v + 1,
		terminal_kind="relay:final",
		terminal_payload=payload,
		assistant_message=r.get("assistant_message"),
		owner=owner,
		conversation=r["conversation"],
		deps=ctx.deps,
	)
	_telemetry("snapshot_recover", run_id=run_id)


def _settle_recovered_errored(ctx: PumpContext, r: dict) -> None:
	"""OARF-2: the run ended (session gone) with NO durable output in this turn's
	watermark window. Settle ``errored`` with an honest user-visible reason +
	credit release — NEVER surface a prior turn's leftover answer. Routes through
	settlement's ``relay:error`` path (Message ``streaming=0`` + error + fenced
	``run:error``)."""
	run_id = r["run_id"]
	v = int(r["version"])
	payload = {"state": "error", "error": _STALLED_ERROR}
	if not ts.mark_terminal_observed(run_id, v, ctx.epoch, "relay:error", payload):
		if _epoch_lost(ctx, run_id):
			ts.lease_lost_exit(run_id)
		return
	frappe.db.commit()
	owner = frappe.db.get_value(CONV, r["conversation"], "owner")
	ctx.deps.invoke_settlement(
		run_id,
		relay_target_id=ctx.relay_target_id,
		epoch=ctx.epoch,
		version=v + 1,
		terminal_kind="relay:error",
		terminal_payload=payload,
		assistant_message=r.get("assistant_message"),
		owner=owner,
		conversation=r["conversation"],
		deps=ctx.deps,
	)
	_telemetry("snapshot_recover_empty", run_id=run_id)


def _reattach_lane(ctx: PumpContext, r: dict) -> None:
	"""Re-register a lane so the future-only broadcast resumes for an adopted
	in-flight turn (start_seq seeded from the durable ``last_event_seq`` so the
	watermark stays monotonic across the hop, WP-1B deviation 2). A breaker-open
	re-adopt is refused (register_run returns None) — stop-readopting (OAR-7)."""
	run_id = r["run_id"]
	owner = frappe.db.get_value(CONV, r["conversation"], "owner")
	dispatch = _load_dispatch(_read_dispatch_row(run_id) or {})
	rs = _RunState(
		run_id=run_id,
		conversation=r["conversation"],
		owner=owner,
		assistant_message=r.get("assistant_message"),
		session_key=dispatch.get("session_key", ""),
		version=int(r["version"]),
		gateway_run_id=r.get("gateway_run_id"),
		accept_ms=_dt_to_ms(frappe.db.get_value(TURN, run_id, "enqueued_at")),
		first_delta_done=True,  # a re-attach resumes mid-stream; first token already sent
	)
	ctx.runs[run_id] = rs
	ctx.peak_occupancy = max(ctx.peak_occupancy, len(ctx.runs))
	lane_key = r.get("gateway_run_id") or run_id
	if ctx.mux is not None:
		ctx.mux.register_run(
			lane_key,
			_make_handler(ctx, rs),
			session_key=rs.session_key,
			start_seq=int(r.get("last_event_seq") or 0),
			is_readopt=True,
		)


def _write_message_recovering(assistant_message: str | None) -> None:
	"""SUXF-1: mirror ``mark_recovering`` onto the assistant Message row
	(``recovering=1`` + ``recovery_started_at``, verbatim legacy
	``turn_handler._mark_recovering``) so a reload / second tab / focus resync can
	RECONSTRUCT the 'Reconnecting' banner + unlocked composer (ChatView keys its
	reload reconciliation on ``Message.recovering``), AND the legacy
	``turn_recovery`` cron backstop (``WHERE streaming=1 AND recovering=1``) can
	sweep the row. Best-effort; a mirror failure never breaks the committed CAS."""
	if not assistant_message:
		return
	try:
		frappe.db.set_value(
			MSG,
			assistant_message,
			{"recovering": 1, "recovery_started_at": frappe.utils.now_datetime()},
			update_modified=False,
		)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="pump.message_recovering", message=frappe.get_traceback())


def _mark_recovering_mirror(
	run_id: str,
	version: int,
	conversation: str,
	assistant_message: str | None,
	*,
	reason: str,
	require_deadline_passed: bool = False,
	require_prepare_deadline: bool = False,
	pump_epoch: int | None = None,
	relay_target_id: str | None = None,
) -> bool:
	"""SUXF-1: the ONE recovering-transition path every park uses. Runs
	``mark_recovering`` (Turn CAS), commits, MIRRORS the state onto the Message row
	(so reload can reconstruct the banner), then publishes the fenced
	``run:recovering`` AFTER commit (SUX-1 — the event ChatView's live banner
	consumes). Actor-fenced, so a benign 0-rows (already parked/moved) is NOT a
	lease loss. CDX-3: a pump-driven park passes ``pump_epoch``/``relay_target_id``
	so the banner event is epoch-fenced end-to-end; the watchdog (no epoch) omits
	them and the event stays legacy-compatible (the client bypasses the fence for an
	epoch-less event). Returns won/lost."""
	if not ts.mark_recovering(
		run_id,
		version,
		require_deadline_passed=require_deadline_passed,
		require_prepare_deadline=require_prepare_deadline,
		# CDX-24: pump-driven parks (they pass pump_epoch) fence the CAS on the epoch so a
		# stale hop parks 0 rows; the watchdog deadline park omits pump_epoch (None) and
		# stays actor-fenced, unchanged.
		epoch=pump_epoch,
	):
		return False
	frappe.db.commit()
	_write_message_recovering(assistant_message)
	owner = frappe.db.get_value(CONV, conversation, "owner")
	if owner:
		# message_id is what today's ChatView run:recovering consumer keys the banner
		# on (legacy always sends it) — send it for parity.
		ts.publish_fenced(
			owner,
			"run:recovering",
			conversation_id=conversation,
			run_id=run_id,
			message_id=assistant_message,
			reason=reason,
			pump_epoch=pump_epoch,
			relay_target_id=relay_target_id,
		)
	return True


def _settle_recover_errored(
	run_id: str,
	version: int,
	conversation: str,
	assistant_message: str | None,
	*,
	error: str | None = None,
) -> bool:
	"""SUXF-1: budget-exhausted ``recovering -> errored`` (D2 row 24) that reaches
	the SAME terminal user experience as a mid-stream ``relay:error`` — write the
	Message row terminal (``streaming=0`` + error) so a budget-exhausted turn never
	sits forever at ``streaming=1`` with an empty spinner and no backstop, then
	publish the fenced ``run:error`` with today's classification. No
	``changed_data`` (the interrupted run may have streamed/tool-called; we do NOT
	claim nothing changed). Returns won/lost."""
	err = error or _STALLED_ERROR
	if not ts.recover_errored(run_id, version, error=err):
		return False
	frappe.db.commit()
	if assistant_message:
		try:
			ts._run_cas(
				f"UPDATE `tab{MSG}` SET streaming=0, error=%(e)s WHERE name=%(m)s",
				{"e": err[:1000], "m": assistant_message},
			)
			frappe.db.commit()
		except Exception:
			frappe.log_error(title="pump.message_errored", message=frappe.get_traceback())
	owner = frappe.db.get_value(CONV, conversation, "owner")
	if owner:
		ts.publish_fenced(
			owner,
			"run:error",
			conversation_id=conversation,
			run_id=run_id,
			message_id=assistant_message,
			error=err,
			code=_classify_error(err),
		)
	return True


def _park_recovering(ctx: PumpContext, run_id: str, *, reason: str) -> None:
	"""Park a turn toward the durable recovery route (D2 row 20). Routes through
	``_mark_recovering_mirror`` so EVERY park (ack-timeout, mux quarantine) writes
	the Message-row mirror + publishes the fenced ``run:recovering`` (SUXF-1)."""
	turn = ts.read_turn(run_id)
	if turn is None:
		return
	_mark_recovering_mirror(
		run_id,
		int(turn["version"]),
		turn_conversation(run_id),
		turn.get("assistant_message"),
		reason=reason,
		pump_epoch=ctx.epoch,
		relay_target_id=ctx.relay_target_id,
	)
	_telemetry("park_recovering", run_id=run_id, reason=reason)


def _park_affected_recovering(ctx: PumpContext) -> None:
	"""DB-disconnect park (D6 §6): mark the shard's in-flight turns ``recovering``
	so the next hop re-attaches from durable state, then let the hop exit. Never
	spins. Best-effort — if the DB is truly down the marks fail and we simply
	exit; the watchdog/ensure_pump revives later. SUXF-1: each park writes the
	Message-row mirror + publishes ``run:recovering`` (via ``_mark_recovering_mirror``)
	so a reload during the disconnect reconstructs the banner rather than a plain
	locked composer."""
	target = ctx.relay_target_id
	try:
		rows = frappe.db.sql(
			f"""SELECT run_id, version, conversation, assistant_message FROM `tab{TURN}`
			WHERE relay_target_id=%(t)s
			  AND state IN ('preparing','ready','dispatching','streaming','terminal_observed')""",
			{"t": target},
			as_dict=True,
		)
	except Exception:
		return
	for r in rows:
		try:
			_mark_recovering_mirror(
				r["run_id"],
				int(r["version"]),
				r["conversation"],
				r.get("assistant_message"),
				reason="db-disconnect",
				pump_epoch=ctx.epoch,
				relay_target_id=ctx.relay_target_id,
			)
		except Exception:
			try:
				frappe.db.rollback()
			except Exception:
				pass


# --------------------------------------------------------------------------- #
# ensure_pump — the sender-driven PRIMARY start/recovery path (§8-E)
# --------------------------------------------------------------------------- #


def ensure_pump(relay_target_id: str, *, deps: PumpDeps | None = None) -> dict:
	"""Start the pump if it is not already leased. MariaDB-authoritative start
	decision (R-17 / §10.6): the Redis mirror may only gate the fast NO-OP path,
	never prove vacancy. Callable after EVERY durable commit (accept_send after
	its commit is the PRIMARY start/recovery path — effectively instant, no
	scheduler dependency). Enqueues hop0 to ``long`` with an EXPLICIT
	``timeout=180`` under a FRESH job id (the frappe dedupe trap).

	OARF-1: gated on ``pump_configured()`` (enabled OR draining) — flag-off is
	INERT. Without this gate the watchdog/sender path would auto-start the pump on
	an admission-on / pump-off site (a shipped Phase-0 mode), letting two engines
	own the same shard and defeating the flag-off safety + the rollback ladder."""
	deps = deps or _default_deps()
	target = relay_target_id
	site = frappe.local.site

	# CDX-21: gate on the AUTHORITATIVE row (5s TTL), not the site_config mirror, so a mirror
	# that momentarily disagrees with the row can never make the sender-driven start path go
	# inert while the fenced accept still admits pump-owned turns (the strand the review flagged).
	if not pump_lifecycle_configured(target):
		return {"enqueued": False, "reason": "not_configured"}

	# §8-I / F1: if this site is in the single-long-no-jarvis_chat shape, warn LOUDLY
	# (throttled). Emitted on the start path so it surfaces even when the pump is
	# already leased (below) — the shape is a standing provisioning problem, not a
	# per-hop one. Best-effort, never blocks the start decision.
	_warn_provisioning_if_starved()

	if _lease_mirror_live(target):
		return {"enqueued": False, "reason": "mirror_live"}

	ts._ensure_control_row(target)
	# CDX-10: derive the row's transport_mode from config ONCE when it is empty (a fresh /
	# pre-migration shard), so the ROW is authoritative before any turn is dispatched. A
	# non-empty row is left as-is (a committed cutover, not conf, decides thereafter).
	reconcile_transport_mode(target)
	row = frappe.db.get_value(PUMP, target, ["lease_expires_at", "hop_counter"], as_dict=True)
	now = ts._now()
	if row and row.get("lease_expires_at"):
		if frappe.utils.get_datetime(row["lease_expires_at"]) > frappe.utils.get_datetime(now):
			return {"enqueued": False, "reason": "lease_live"}

	hop = int((row.get("hop_counter") if row else 0) or 0) + 1
	job_id = f"jarvis-pump::{site}::{target}::hop{hop}"
	deps.enqueue_pump_job(
		method="jarvis.chat.pump.run_pump_hop",
		queue=PUMP_QUEUE,
		timeout=HOP_TIMEOUT_S,
		job_id=job_id,
		relay_target_id=target,
		hop_counter=hop,
	)
	return {"enqueued": True, "job_id": job_id, "hop_counter": hop}


def request_cancel_conversation(relay_or_conversation: str) -> bool:
	"""Web ``stop_run`` hook (pump mode): flag the conversation's in-flight pump turn
	for cancellation (D2 #17, actor-fenced) and wake the pump so its cancel sweep
	drives the out-of-band ``chat.abort`` + aborted terminal + settle-cancelled.
	Best-effort; returns True iff a turn was flagged. NOT terminal itself — the pump
	still owns the abort + settle."""
	conversation = relay_or_conversation
	row = frappe.db.get_value(
		TURN,
		{"conversation": conversation, "state": ["in", ("dispatching", "streaming")]},
		["run_id", "version", "relay_target_id"],
		as_dict=True,
	)
	if not row:
		return False
	if ts.request_cancel(row["run_id"], int(row["version"])):
		frappe.db.commit()
		ensure_pump(row["relay_target_id"])
		lpush_wake(row["relay_target_id"], row["run_id"])
		return True
	return False


def _handoff(ctx: PumpContext) -> None:
	"""Hop handoff (D6 §3, CDX-1): an EPOCH-GUARDED ATOMIC lease TRANSFER, then a
	fresh-id successor enqueue. A bare re-enqueue is NOT enough: the running hop
	renewed its lease to ~now+30s, so a successor enqueued while that lease is still
	valid would fail ``lease_acquire`` and exit successor-less — stranding any turn
	that outlives a hop. ``ts.lease_handoff`` proves this pump still holds epoch E,
	makes the lease immediately acquirable, advances ``hop_counter`` (fresh job id —
	the frappe self-chain trap), and commits, ALL in one epoch-fenced statement; a
	stale outgoing hop (a takeover already bumped the epoch) matches 0 rows and we
	enqueue NOTHING (the shared lease-loss exit). The lease + epoch (not the job id)
	enforce single-instance; the successor re-acquires and bumps the epoch, fencing
	this hop."""
	# CDX-21 (Residual B): the kill switch trumps a clean handoff. If the row flipped to legacy
	# mid-hop, do NOT transfer the lease to a fresh successor — release WITHOUT succession and park
	# live turns instead (``draining`` still hands off, finishing in-flight). This is the soft-budget
	# boundary of the <= cache-TTL + slice kill bound.
	if _kill_switch_engaged(ctx.relay_target_id):
		_halt_for_kill_switch(ctx)
		return
	# OARF-6: flush any sub-threshold batched deltas so a tail batch is not left in
	# memory across the hop boundary (the next hop would re-derive it from the
	# cumulative mirror, but flushing now keeps the streamed text current).
	_flush_all_pending(ctx)
	next_hop = ctx.hop_counter + 1
	job_id = f"jarvis-pump::{ctx.site}::{ctx.relay_target_id}::hop{next_hop}"
	if not ts.lease_handoff(ctx.relay_target_id, ctx.epoch, next_hop, job_id):
		# Stale outgoing hop — a takeover already owns the shard. Write no counter,
		# enqueue no successor; the current owner drives succession (shared exit).
		ts.lease_lost_exit(ctx.relay_target_id, rollback=False)
		return
	_clear_lease_mirror(ctx.relay_target_id)
	ctx.deps.enqueue_pump_job(
		method="jarvis.chat.pump.run_pump_hop",
		queue=PUMP_QUEUE,
		timeout=HOP_TIMEOUT_S,
		job_id=job_id,
		relay_target_id=ctx.relay_target_id,
		hop_counter=next_hop,
	)


def _shard_epoch_lost(ctx: PumpContext) -> bool:
	"""CDX-2: True iff the SHARD control row's ``pump_epoch`` no longer matches this
	pump's epoch. Distinct from ``_epoch_lost`` (which reads the TURN row): at the
	``ready -> dispatching`` CAS the turn has no epoch yet, so a stale-pump loss can
	only be proven on the control row."""
	e = frappe.db.get_value(PUMP, ctx.relay_target_id, "pump_epoch")
	return e is None or int(e) != ctx.epoch


def _shard_has_live_work(target: str) -> bool:
	"""True iff the shard still has a nonterminal turn (used to decide whether a
	transport-exit successor is owed, CDX-1)."""
	return bool(
		frappe.db.sql(
			f"""SELECT 1 FROM `tab{TURN}`
			WHERE relay_target_id=%(t)s AND state IN ({_in_list(ts.NONTERMINAL_STATES)}) LIMIT 1""",
			{"t": target},
		)
	)


def _schedule_successor_on_exit(ctx: PumpContext, *, transport_retry: int) -> None:
	"""CDX-1 transport-exit succession. A non-handoff loop exit (transport_closed /
	no_transport) must not leave live work successor-less. Epoch-guarded ATOMIC lease
	release (``ts.lease_handoff`` forces the lease immediately acquirable + advances
	``hop_counter``, proving we still hold epoch E), then — only when the shard still
	has live nonterminal work and the bounded fast-retry budget is not exhausted —
	enqueue an IMMEDIATE successor carrying an incremented ``transport_retry``.

	A stale outgoing hop (a takeover bumped the epoch) matches 0 rows in
	``lease_handoff`` and we do nothing (the new owner drives succession). Over the
	budget (``TRANSPORT_RETRY_MAX``) we leave the now-acquirable lease for the
	watchdog-cron / sender ``ensure_pump`` to revive — the documented backoff tail for
	a sustained gateway outage (see the ``TRANSPORT_RETRY_MAX`` note). Best-effort:
	never raises out of an exit path."""
	target = ctx.relay_target_id
	# CDX-21 (Residual B): the kill switch overrides transport-error succession. A transport exit
	# under a legacy row must NOT enqueue a successor — release WITHOUT succession and park live
	# turns (Phase-0/legacy owns them per coexistence).
	if _kill_switch_engaged(target):
		_halt_for_kill_switch(ctx)
		return
	next_hop = ctx.hop_counter + 1
	successor = f"jarvis-pump::{ctx.site}::{target}::hop{next_hop}"
	try:
		if not ts.lease_handoff(target, ctx.epoch, next_hop, successor):
			return  # stale — a takeover owns the shard; it drives succession
		_clear_lease_mirror(target)
		if not _shard_has_live_work(target):
			return  # idle — nothing to revive; the released lease simply stays vacant
		if transport_retry >= TRANSPORT_RETRY_MAX:
			# Bounded fast-retry budget spent: fall through to the watchdog / sender
			# ensure_pump (the lease is already acquirable). Documented backoff tail.
			_telemetry("transport_retry_capped", target=target, retry=transport_retry)
			return
		ctx.deps.enqueue_pump_job(
			method="jarvis.chat.pump.run_pump_hop",
			queue=PUMP_QUEUE,
			timeout=HOP_TIMEOUT_S,
			job_id=successor,
			relay_target_id=target,
			hop_counter=next_hop,
			transport_retry=transport_retry + 1,
		)
	except Exception:
		# A release/enqueue failure must never crash the exit path — the watchdog /
		# sender ensure_pump remains the final backstop.
		frappe.log_error(title="pump.schedule_successor", message=frappe.get_traceback())


# --------------------------------------------------------------------------- #
# watchdog — the 240s scheduler backstop (§8-E, D6 §5)
# --------------------------------------------------------------------------- #


def watchdog(deps: PumpDeps | None = None) -> dict:
	"""Last-resort backstop (hooks cron; the sender path is PRIMARY). Scans ALL
	nonterminal states across every shard and applies the per-state actions
	(amended D2), then ``ensure_pump`` for any shard with live work. The one gap
	the sender path cannot cover: a turn was committed, the pump died, and NO new
	send arrives to fire ``ensure_pump``. Never raises out (best-effort).

	OARF-1: gated on ``pump_configured()`` (enabled OR draining) — with the flag
	OFF the watchdog is a total no-op, so an admission-on / pump-off site's Phase-0
	Turn rows never spuriously start a pump hop (dual-engine ownership). The
	rollback ladder keeps the no-strand guarantee: ``enabled -> draining`` keeps
	the pump configured (watchdog still drains in-flight rows to terminal) and only
	then ``-> disabled``."""
	deps = deps or _default_deps()
	summary = {"aged_out": 0, "reclaimed": 0, "parked": 0, "finalize_requeued": 0, "errored": 0, "revived": 0}
	# CDX-21 (Residual A): the config key is site-wide, so reconcile the operator mirror FROM the
	# authoritative default control row EVERY cycle, OUTSIDE the open-work target loop — an IDLE
	# site whose mirror diverged (a failed command mirror write) otherwise stays divergent
	# indefinitely, because the target set below is derived only from open turns / effects and an
	# idle site has none. Idempotent no-op when the mirror already matches the row.
	try:
		reconcile_config_mirror(DEFAULT_TARGET)
	except Exception:
		frappe.log_error(title="pump.watchdog reconcile", message=frappe.get_traceback())
	try:
		targets = {
			r[0]
			for r in frappe.db.sql(
				f"""SELECT DISTINCT relay_target_id FROM `tab{TURN}`
				WHERE state IN ({_in_list(ts.NONTERMINAL_STATES)})"""
			)
		}
		# CDX-4: also scan shards that have a turn with an open (pending/stale-running)
		# EFFECT row regardless of that turn's terminal state — a crash after an
		# errored/cancelled settlement commit but before enqueue_finalize leaves the
		# owed effects on a TERMINAL turn that the nonterminal-only scan above misses.
		targets |= set(ts.shards_with_open_effects())
	except Exception:
		return summary
	for target in targets:
		try:
			# CDX-21: gate PER-SHARD on the AUTHORITATIVE row (5s TTL) — NOT the config mirror — so a
			# mirror that momentarily disagrees can never suppress a shard's pump owner while the
			# fenced accept still admits its turns. A shard whose row is the ``legacy`` kill switch is
			# skipped (do not revive — use the drain ladder).
			if not pump_lifecycle_configured(target):
				continue
			_watchdog_shard(target, deps, summary)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="pump.watchdog", message=frappe.get_traceback())
	return summary


def _watchdog_shard(target: str, deps: PumpDeps, summary: dict) -> None:
	now = ts._now()
	# A stale lease is not parked here — ensure_pump (below) revives the pump,
	# which re-stamps + reconciles in-flight turns on start (D6 §5). The watchdog
	# parks only on a per-turn deadline / recovery budget.
	rows = frappe.db.sql(
		f"""SELECT run_id, state, version, reserved, reservation_expires_at, enqueued_at,
		       preparing_at, deadline_at, dispatching_at, recovery_started_at, conversation,
		       assistant_message, seed_message
		FROM `tab{TURN}`
		WHERE relay_target_id=%(t)s AND state IN ({_in_list(ts.NONTERMINAL_STATES)})""",
		{"t": target},
		as_dict=True,
	)

	live_work = False
	for r in rows:
		state = r["state"]
		v = int(r["version"])
		run_id = r["run_id"]
		conv = r["conversation"]
		am = r.get("assistant_message")

		if state == "queued":
			reserved = int(r.get("reserved") or 0)
			# (OARF-4) A turn that RESERVED a credit at promote but whose prepare
			# never CLAIMED it (still `queued` reserved=1) is reclaimed back to
			# `queued reserved=0` FOR RETRY at the short pre-claim dispatch deadline
			# (120s from when the reservation was made — claim_preparing NULLs the
			# expiry, so a still-`queued` reserved turn is one prepare never claimed),
			# checked BEFORE age-out so reclaim (retry) WINS over cancel for a
			# reserved turn. A turn that was admitted + given a credit must not be
			# cancelled for a starvation that was not its fault. recover_to_queued
			# drops the stale prepare refs so it re-prepares from scratch.
			if reserved and _reservation_stale(r.get("reservation_expires_at"), PREPARE_DISPATCH_DEADLINE_S):
				if ts.mark_recovering(run_id, v):
					frappe.db.commit()
					if ts.recover_to_queued(run_id, v + 1):
						frappe.db.commit()
						summary["reclaimed"] += 1
				live_work = True
				continue
			# Age-out (SUX-5): genuinely-waiting UNRESERVED queued turns only (a
			# reserved turn is reclaimed above, never cancelled by this path).
			if not reserved and _older_than(r.get("enqueued_at"), ts.QUEUED_MAX_AGE_S):
				if ts.cancel_queued_max_age(run_id, v, _AGE_OUT_REASON):
					frappe.db.commit()
					_publish_cancelled(r, _AGE_OUT_REASON)
					summary["aged_out"] += 1
					continue
			live_work = True

		elif state == "preparing":
			# PREPARE_DEADLINE_S=300 (OAR-5): recovering->queued (fresh prepare).
			# Pre-dispatch reclaim: recover_to_queued NULLs the assistant_message, so
			# no Message banner is owed (the turn simply re-queues).
			if ts.mark_recovering(run_id, v, require_prepare_deadline=True):
				frappe.db.commit()
				if ts.recover_to_queued(run_id, v + 1):
					frappe.db.commit()
					summary["reclaimed"] += 1
			live_work = True

		elif state in ("ready", "dispatching", "streaming", "terminal_observed"):
			# Deadline exceeded (per-turn soft deadline) -> park; budget exhausted
			# -> errored. Otherwise the revived pump adopts/settles (ensure_pump).
			# SUXF-1: the deadline park writes the Message mirror + publishes
			# run:recovering so a reload reconstructs the banner (not a plain locked
			# composer).
			if _recovery_budget_exhausted(r, now):
				if _settle_recover_errored(run_id, v, conv, am, error=_STALLED_ERROR):
					summary["errored"] += 1
			elif r.get("deadline_at") and _expired(r.get("deadline_at"), now):
				if _mark_recovering_mirror(
					run_id, v, conv, am, reason="deadline", require_deadline_passed=True
				):
					summary["parked"] += 1
			live_work = True

		elif state == "recovering":
			# SUXF-1: a budget-exhausted recovering turn reaches the SAME terminal UX
			# as a mid-stream error (Message streaming=0 + error + run:error) instead
			# of stranding the Message row at streaming=1 with no backstop.
			if _recovery_budget_exhausted(r, now):
				if _settle_recover_errored(run_id, v, conv, am):
					summary["errored"] += 1
			elif r.get("dispatching_at") is None:
				if ts.recover_to_queued(run_id, v):
					frappe.db.commit()
					summary["reclaimed"] += 1
					live_work = True
			else:
				live_work = True  # pump reconcile adopts

		elif state == "finalizing":
			# R-13: the watchdog's ONLY legal action on a settled turn is
			# re-enqueueing finalize (never re-enter recovering).
			deps.enqueue_finalize(run_id, target)
			summary["finalize_requeued"] += 1

	# CDX-4: independent EFFECT-ledger scan. Any turn on this shard with an open
	# (pending / stale-running) effect row — INCLUDING a terminal errored/cancelled
	# turn whose finalize enqueue was lost after the settlement commit — gets finalize
	# re-enqueued. Idempotent (the attempt-suffixed dedupe + the exclusive claim make a
	# concurrent healthy finalize a no-op), so this never fights a live finalizer.
	try:
		open_effect_turns = ts.turns_with_open_effects(target)
	except Exception:
		open_effect_turns = []
	requeued_finalize = {r["run_id"] for r in rows if r["state"] == "finalizing"}
	for run_id in open_effect_turns:
		if run_id in requeued_finalize:
			continue  # already re-enqueued by the finalizing branch above
		deps.enqueue_finalize(run_id, target)
		summary["finalize_requeued"] += 1

	if live_work:
		res = ensure_pump(target, deps=deps)
		if res.get("enqueued"):
			summary["revived"] += 1


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _read_dispatch_row(run_id: str) -> dict | None:
	return frappe.db.get_value(
		TURN,
		run_id,
		[
			"run_id",
			"state",
			"version",
			"conversation",
			"assistant_message",
			"last_event_seq",
			"dispatch_payload",
			"enqueued_at",
		],
		as_dict=True,
	)


def _load_dispatch(turn: dict) -> dict:
	"""The prepare->pump handoff contract: prepare (WP-1d) writes the assembled
	prompt + created session into ``dispatch_payload`` JSON for the pump to
	``chat.send``. Keys the pump reads: ``session_key`` (required), ``message``
	(assembled prompt; falls back to the seed message content), ``thinking``,
	``attachments``. Robust to a missing/partial payload."""
	payload: dict = {}
	raw = turn.get("dispatch_payload") if turn else None
	if raw:
		try:
			parsed = json.loads(raw)
			if isinstance(parsed, dict):
				payload = parsed
		except Exception:
			payload = {}
	message = payload.get("message")
	if not message and turn.get("run_id"):
		seed = frappe.db.get_value(TURN, turn["run_id"], "seed_message")
		if seed:
			message = frappe.db.get_value(MSG, seed, "content") or ""
	return {
		"session_key": payload.get("session_key") or "",
		"message": message or "",
		"thinking": payload.get("thinking"),
		"attachments": payload.get("attachments"),
		"drained_note_ids": payload.get("drained_note_ids") or [],
	}


def _clear_agent_notes_on_ack(conversation: str, drained_note_ids) -> None:
	"""R-2: clear the agent-correction notes prepare folded into this turn's prompt,
	by id (airtight against overlapping turns), once delivery is PROVEN at the ack.
	Best-effort — never breaks the stream."""
	if not drained_note_ids:
		return
	try:
		from jarvis.chat import agent_notes

		agent_notes.clear(conversation, drained_note_ids)
	except Exception:
		frappe.log_error(title="pump.agent_notes_clear", message=frappe.get_traceback())


def _epoch_lost(ctx: PumpContext, run_id: str) -> bool:
	"""True iff the row's ``pump_epoch`` no longer matches this pump's epoch — the
	real lease-loss signal that distinguishes a takeover from a benign version
	drift on a 0-rows pump-owned CAS."""
	epoch = frappe.db.get_value(TURN, run_id, "pump_epoch")
	return epoch is None or int(epoch) != ctx.epoch


def _resync_version(run_id: str) -> int:
	return int(frappe.db.get_value(TURN, run_id, "version") or 0)


def _attempt_suffix(run_id: str) -> int:
	"""OARF-8 attempt suffix for prepare/finalize job ids: the turn's ``version``.
	Stable across a slice's re-offers of the same not-yet-advanced turn (so dedupe
	no-ops the re-offer), and monotonically distinct across genuine new attempts
	(reserve/claim/recover_to_queued/finalize_done all bump version), so a
	hard-killed job's stale STARTED registration never blocks the next attempt."""
	return int(frappe.db.get_value(TURN, run_id, "version") or 0)


def turn_conversation(run_id: str) -> str:
	return frappe.db.get_value(TURN, run_id, "conversation")


def _local_active_session_keys(target: str) -> set[str]:
	"""Session keys the bench has an in-flight local turn for (used to subtract
	local runs from the gateway snapshot so only FOREIGN runs count as inflight)."""
	rows = frappe.db.sql(
		f"""SELECT dispatch_payload FROM `tab{TURN}`
		WHERE relay_target_id=%(t)s
		  AND state IN ('dispatching','streaming','terminal_observed')""",
		{"t": target},
		as_dict=True,
	)
	keys: set[str] = set()
	for r in rows:
		raw = r.get("dispatch_payload")
		if not raw:
			continue
		try:
			parsed = json.loads(raw)
			if isinstance(parsed, dict) and parsed.get("session_key"):
				keys.add(parsed["session_key"])
		except Exception:
			pass
	return keys


def _final_text(payload) -> str | None:
	payload = _coerce_payload(payload)
	if isinstance(payload, dict):
		return payload.get("text")
	return None


def _error_text(payload) -> str:
	payload = _coerce_payload(payload)
	if isinstance(payload, dict):
		return payload.get("error") or payload.get("state") or "The run ended with an error."
	return "The run ended with an error."


def _is_aborted_payload(terminal_kind, payload) -> bool:
	payload = _coerce_payload(payload)
	if isinstance(payload, dict):
		if payload.get("aborted") is True:
			return True
		if payload.get("state") == "aborted":
			return True
	return False


def _coerce_payload(payload):
	if isinstance(payload, str):
		try:
			return json.loads(payload)
		except Exception:
			return {"text": payload}
	return payload


def _json_or_none(raw):
	if not raw:
		return None
	try:
		return json.loads(raw)
	except Exception:
		return raw


def _in_list(values) -> str:
	return ",".join(f"'{v}'" for v in values)


def _older_than(dt, seconds: int) -> bool:
	if not dt:
		return False
	cutoff = frappe.utils.add_to_date(None, seconds=-seconds)
	return frappe.utils.get_datetime(dt) < frappe.utils.get_datetime(cutoff)


def _expired(dt, now) -> bool:
	if not dt:
		return False
	return frappe.utils.get_datetime(dt) < frappe.utils.get_datetime(now)


def _reservation_stale(reservation_expires_at, deadline_s: int) -> bool:
	"""OARF-4: True iff a reservation was made more than ``deadline_s`` ago.
	``reserve_credit`` stamps ``reservation_expires_at = made_at + RESERVE_TTL_S``,
	so ``made_at`` is more than ``deadline_s`` in the past exactly when
	``reservation_expires_at < now + (RESERVE_TTL_S - deadline_s)``."""
	if not reservation_expires_at:
		return False
	cutoff = frappe.utils.add_to_date(None, seconds=(ts.RESERVE_TTL_S - deadline_s))
	return frappe.utils.get_datetime(reservation_expires_at) < frappe.utils.get_datetime(cutoff)


def _recovery_budget_exhausted(r: dict, now) -> bool:
	started = r.get("recovery_started_at")
	if not started:
		return False
	cutoff = frappe.utils.add_to_date(None, seconds=-RECOVERY_BUDGET_S)
	return frappe.utils.get_datetime(started) < frappe.utils.get_datetime(cutoff)


def _backoff_reconnect(attempt: int) -> None:
	"""Bounded backoff + reconnect attempt (D6 §6). Rolls back the failed txn,
	sleeps (capped), and probes the connection. Never spins: the caller counts
	consecutive failures and parks after DB_BACKOFF_ATTEMPTS."""
	try:
		frappe.db.rollback()
	except Exception:
		pass
	_sleep(min(DB_BACKOFF_BASE_S * (2 ** (attempt - 1)), DB_BACKOFF_CAP_S))
	try:
		frappe.connect()
	except Exception:
		pass


_AGE_OUT_REASON = "Waited too long in the queue and was cancelled. Please try again."
_STALLED_ERROR = "The response was interrupted and could not be recovered."


def _publish_cancelled(r: dict, reason: str) -> None:
	try:
		owner = frappe.db.get_value(CONV, r["conversation"], "owner")
		if owner:
			ts.publish_fenced(
				owner, "turn:cancelled", conversation_id=r["conversation"], run_id=r["run_id"], reason=reason
			)
	except Exception:
		pass


def _telemetry(event: str, **fields) -> None:
	try:
		from jarvis.chat.latency import get_logger

		parts = " ".join(f"{k}={v}" for k, v in fields.items())
		get_logger().info("pump %s %s", event, parts)
	except Exception:
		pass


def _classify_error(err_text: str) -> str:
	"""Preserve today's Message.error headline classification (SUX-11) for the
	pump's own error publishes (definite pre-ack rejection)."""
	try:
		from jarvis.chat.turn_handler import _classify_error as _ce

		return _ce(err_text)
	except Exception:
		return "internal"


def _dt_to_ms(dt) -> int:
	"""Datetime (or str) -> epoch milliseconds; 0 when absent/unparseable. Used to
	seed the C1 accept baseline from the turn's enqueued_at."""
	if not dt:
		return 0
	try:
		return int(frappe.utils.get_datetime(dt).timestamp() * 1000)
	except Exception:
		return 0


def _queue_wait_ms(run_id: str) -> int:
	"""C5 queue_wait_ms at PROMOTION (enqueued_at -> now): how long a turn waited
	`queued` before the pump reserved its credit. Measured at promote (before
	``dispatching_at`` exists), so it is now-based, not dispatching-based."""
	enq = frappe.db.get_value(TURN, run_id, "enqueued_at")
	if not enq:
		return 0
	try:
		delta = frappe.utils.now_datetime() - frappe.utils.get_datetime(enq)
		return max(0, int(delta.total_seconds() * 1000))
	except Exception:
		return 0


def _emit_stream_telemetry(rs: "_RunState") -> None:
	"""Best-effort C1/C3/C4 streaming series, emitted around a winning delta
	publish (observability only — never affects the CAS/commit). first_token_ms =
	accept -> first delta publish (C1); dwell proxy = dispatching_at ->
	first_event_at (C4); flush_gap_ms = inter-publish gap (C3)."""
	try:
		now_mono = _monotonic()
		if not rs.first_delta_done:
			rs.first_delta_done = True
			now_ms = int(time.time() * 1000)
			if rs.accept_ms:
				_telemetry("first_token_ms", run_id=rs.run_id, ms=max(0, now_ms - rs.accept_ms))
			# Stamp first_event_at once (dwell baseline) + emit the dispatch->first-event
			# dwell proxy from the durable timestamps.
			row = (
				frappe.db.get_value(TURN, rs.run_id, ["dispatching_at", "first_event_at"], as_dict=True) or {}
			)
			if not row.get("first_event_at"):
				try:
					frappe.db.set_value(
						TURN, rs.run_id, "first_event_at", frappe.utils.now(), update_modified=False
					)
				except Exception:
					pass
			if row.get("dispatching_at"):
				try:
					dwell = frappe.utils.now_datetime() - frappe.utils.get_datetime(row["dispatching_at"])
					_telemetry("dwell_ms", run_id=rs.run_id, ms=max(0, int(dwell.total_seconds() * 1000)))
				except Exception:
					pass
		elif rs.last_publish_mono:
			_telemetry(
				"flush_gap_ms", run_id=rs.run_id, ms=round((now_mono - rs.last_publish_mono) * 1000.0, 1)
			)
		rs.last_publish_mono = now_mono
	except Exception:
		pass
