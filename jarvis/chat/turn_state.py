"""WP-1a — the durable turn-state machine (Relay Pump foundation).

This module is the CAS primitive library for the full ``Jarvis Chat Turn`` state
machine (amended D2). It is a LIBRARY: nothing in a production path calls it yet
(WP-1c/d wire the pump, prepare, finalize, and the accept_send chokepoint around
it). WP-1a ships the migration + this library + its tests only.

Binding contract (read in order):
  * ../../../jarvis-chat-concurrency-design/implementation/wp-d/D2-schema-transitions.md
    (AMENDED) — the schema + the FULL transition table (§4), fence classes,
    canonical lock order (§3a), PREPARE_DEADLINE_S=300, the effect ledger (§1a)
    with FINALIZE_MAX_ATTEMPTS=3.
  * D3-cas-pseudocode.md (AMENDED) — settlement shape (Race 3), reserve lifecycle.
  * D4-fencing-timelines.md — the epoch-fence proofs these functions reproduce.
  * §8/§9/§10 of jarvis-chat-concurrency-architecture.md — the constitution.

HARD INVARIANTS (restated per-function in docstrings):
  * MariaDB is the ONLY authority. Every mutation is a single conditional
    ``UPDATE ... WHERE <guard>`` whose affected-rows count is the success signal
    (1 = won, 0 = lost/stale). No ORM ``save()`` on any transition (it
    read-modify-writes and cannot express a CAS).
  * ``version`` is bumped on EVERY transition (the actor fence).
  * ``pump_epoch=E`` is in the WHERE of EVERY pump-owned (epoch-fenced) row, so a
    stale pump's CAS affects 0 rows after a takeover re-stamp (D4).
  * A pump-owned CAS that affects 0 rows routes through the ONE shared
    ``lease_lost_exit`` (rollback + ``LeaseLostExit``): stop writing, stop
    publishing, let the hop die (D4 "why one shared exit matters").
  * Realtime publishes happen ONLY after a winning commit and are themselves
    fenced, carrying ``(turn_id, event_seq)`` so duplicates are idempotent.
  * Canonical lock order (OAR-6, D2 §3a): control(shard) -> conversation ->
    turn -> message. ``_lock_shard`` restates the commit-first REPEATABLE-READ
    discipline (the first statement after acquiring the lock is NEVER a
    non-locking read) and every call site uses ``assert_lock_order``.

Transaction discipline: the pure transition CAS functions do the writes and
RETURN won/lost WITHOUT committing — the caller (pump / prepare / finalize /
watchdog) owns the transaction so it can commit-then-publish on a win, or
rollback-then-``lease_lost_exit`` on a loss (e.g. settlement's S1 message write
rolls back with a lost S2 CAS). The standalone lease/idle operations manage
their own transaction and say so.
"""

from __future__ import annotations

import json
import threading

import frappe

from jarvis.chat.events import publish_to_user

TURN = "Jarvis Chat Turn"
PUMP = "Jarvis Relay Pump"
MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"
EFFECT = "Jarvis Turn Effect"

# --------------------------------------------------------------------------- #
# Constants (from the amended D2 / RULINGS-PA)
# --------------------------------------------------------------------------- #

# A stuck `preparing` turn past this instant is re-queued via the watchdog
# `preparing -> recovering -> queued` route, NOT a direct sweep (OAR-5).
PREPARE_DEADLINE_S = 300

# An enrichment effect that fails this many times is FORCE-DONE (skipped +
# logged) so a settled turn always reaches `done` (D2 §1a, OAR-9).
FINALIZE_MAX_ATTEMPTS = 3

# CDX-4: a `running` effect whose exclusive claim is older than this bound is
# reclaimable — the finalizer that claimed it crashed (a finalize job is bounded
# well under this by HOP_TIMEOUT_S=180). Until the bound elapses the claim is
# exclusive, so two concurrent finalizers can never run the same effect twice.
EFFECT_CLAIM_STALE_S = 300

# Per-turn soft deadline stamped at dispatch (openclaw_client.py TURN_TIMEOUT).
TURN_TIMEOUT_SECONDS = 600

# The unclaimed-reservation window (mirrors admission.RESERVE_TTL_S). Bounds ONLY
# a reserved-but-not-yet-CLAIMED queued turn (NULLed at the queued->preparing
# claim, OAR-5). Deliberately >> the worker turn timeout.
RESERVE_TTL_S = 900

# Lease TTL wall-clock (~30s; heartbeat renews on a shorter cadence, WP-1c).
LEASE_TTL_S = 30

# System age-out for a queued turn (SUX-5).
QUEUED_MAX_AGE_S = 15 * 60

# The full possible enrichment set (D2 §1a + R-4). Settlement inserts the SUBSET
# a given turn actually owes (its outcome decides the set, OAR-9).
EFFECT_NAMES = (
	"terminal_publish",
	"usage",
	"auto_title",
	"rich_outputs",
	"macro_advance",
	"telemetry_flush",
)

TERMINAL_STATES = ("done", "errored", "cancelled")
# Nonterminal states for the idle-release NOT EXISTS + watchdog scans.
NONTERMINAL_STATES = (
	"queued",
	"preparing",
	"ready",
	"dispatching",
	"streaming",
	"terminal_observed",
	"finalizing",
	"recovering",
)
# The epoch-owned in-flight states re-stamped on takeover (D4 c t3, §10.5).
EPOCH_INFLIGHT_STATES = ("dispatching", "streaming", "terminal_observed")


# --------------------------------------------------------------------------- #
# Shared lease-loss exit (Amendment B / D4)
# --------------------------------------------------------------------------- #


class LeaseLostExit(Exception):
	"""Raised by the ONE shared lease-loss exit when a pump-owned CAS affects 0
	rows — either a takeover bumped ``pump_epoch`` (E != current) or a concurrent
	actor advanced ``version``. Either way THIS pump has lost authority: it must
	stop draining, stop publishing, abandon the WS, and let the RQ hop return so
	the worker frees (D4 'why one shared exit matters')."""

	def __init__(self, run_id: str | None = None):
		self.run_id = run_id
		super().__init__(f"lease lost for turn {run_id!r}")


def lease_lost_exit(run_id: str | None = None, *, rollback: bool = True) -> None:
	"""The ONE shared lease-loss exit. Rolls back the uncommitted transaction (so
	a losing settlement's S1 message write is undone), publishes NOTHING, and
	raises ``LeaseLostExit``. Every pump-owned 0-rows branch funnels here — there
	is no second path where a stale pump can leak a write or a publish (D4)."""
	if rollback:
		try:
			frappe.db.rollback()
		except Exception:
			pass
	reset_lock_tracking()
	raise LeaseLostExit(run_id)


# --------------------------------------------------------------------------- #
# Canonical lock-order dev assertion (OAR-6, D2 §3a)
# --------------------------------------------------------------------------- #

_LOCK_RANK = {"shard": 1, "conversation": 2, "turn": 3, "message": 4}
_lock_state = threading.local()


class LockOrderError(Exception):
	"""Raised in dev mode when a writer acquires a lower-rank lock while holding a
	higher-rank one (an inversion that can deadlock a concurrent send, OAR-6)."""


def _dev_mode() -> bool:
	try:
		return bool(frappe.conf.get("developer_mode")) or bool(frappe.conf.get("jarvis_pump_lock_assert"))
	except Exception:
		return False


def _lock_stack() -> list[int]:
	stack = getattr(_lock_state, "stack", None)
	if stack is None:
		stack = []
		_lock_state.stack = stack
	return stack


def assert_lock_order(kind: str) -> None:
	"""Canonical lock-order guard (OAR-6, D2 §3a): control(shard) -> conversation
	-> turn -> message. Call sites acquire row locks through the ``_lock_*``
	helpers, which call this. Acquiring a lower-rank lock while holding a
	higher-rank one is an inversion — raise in dev mode, log otherwise. A repeat
	of the SAME rank (reentrant re-lock) is allowed."""
	rank = _LOCK_RANK[kind]
	stack = _lock_stack()
	if stack and rank < stack[-1]:
		msg = (
			f"lock-order violation: acquiring {kind}(rank {rank}) while holding rank "
			f"{stack[-1]} (canonical: control->conversation->turn->message)"
		)
		if _dev_mode():
			raise LockOrderError(msg)
		try:
			frappe.log_error(title="turn_state.lock_order", message=msg)
		except Exception:
			pass
	stack.append(rank)


def reset_lock_tracking() -> None:
	"""Clear the lock-order tracking stack. Call after every commit/rollback — DB
	row locks release then, so the logical stack resets too."""
	_lock_stack().clear()


# --------------------------------------------------------------------------- #
# CAS + shard-lock plumbing
# --------------------------------------------------------------------------- #


def _now() -> str:
	return frappe.utils.now()


def _run_cas(sql: str, params: dict) -> int:
	"""Run a CAS UPDATE and return affected rows, read BEFORE any commit (commit
	can reset the cursor — the discipline jarvis.chat.admission._run_cas and
	turn_recovery._conditional_clear both follow)."""
	frappe.db.sql(sql, params)
	cursor = getattr(frappe.db, "_cursor", None)
	return int(cursor.rowcount) if cursor else 0


def _ensure_control_row(target: str) -> None:
	"""Get-or-create the shard control row OUTSIDE the serializing lock so the
	FOR UPDATE / conditional acquire below always has a row to work on (mirrors
	admission._ensure_control_row)."""
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
	"""Take the per-shard control row FOR UPDATE (OAR-1, canonical rank 1).

	Commit-first REPEATABLE-READ discipline — a HARD invariant, identical to
	``jarvis.chat.admission._lock_shard``: InnoDB pins a transaction's
	consistent-read snapshot at its FIRST non-locking read, so we commit here to
	start a FRESH transaction, making the ``SELECT ... FOR UPDATE`` the first
	statement. The lock blocks until the prior holder commits, and any non-locking
	read AFTER it takes its snapshot AFTER that commit (verified on patterntest
	under both READ COMMITTED and REPEATABLE READ). The first statement after
	acquiring the lock is NEVER a non-locking read. Callers commit their own
	durable work BEFORE entering, so this commit never drops pending state."""
	_ensure_control_row(target)
	frappe.db.commit()
	assert_lock_order("shard")
	frappe.db.sql(f"SELECT name FROM `tab{PUMP}` WHERE name=%(t)s FOR UPDATE", {"t": target})


def _lock_conversation(conversation: str) -> None:
	"""Second lock in the canonical order (rank 2); defends per-conversation
	single-flight / seq allocation (OAR-6)."""
	assert_lock_order("conversation")
	frappe.db.sql(f"SELECT name FROM `tab{CONV}` WHERE name=%(c)s FOR UPDATE", {"c": conversation})


def read_turn(run_id: str) -> dict | None:
	"""Read the fields a caller/test needs to compute the next CAS (state,
	version, epoch, watermark, reservation, recovery discriminators)."""
	return frappe.db.get_value(
		TURN,
		run_id,
		[
			"state",
			"version",
			"pump_epoch",
			"reserved",
			"last_event_seq",
			"recovering",
			"dispatching_at",
			"terminal_kind",
			"cancel_requested",
			"assistant_message",
		],
		as_dict=True,
	)


# --------------------------------------------------------------------------- #
# Reservation lifecycle helpers (D2 rows #11/#12, D3 Race 2)
# --------------------------------------------------------------------------- #


def reserve_credit(run_id: str, ttl_s: int = RESERVE_TTL_S) -> bool:
	"""Reservation lifecycle — RESERVE (D2 #11/#12, D3 Race 2), actor-fenced.
	Atomic conditional reserve of one admission credit on a queued, unreserved
	turn: ``reserved=1``, ``reservation_expires_at=now+ttl`` (bounds ONLY the
	unclaimed window). The caller MUST hold the shard control-row lock so the
	credit COUNT+reserve is one shard-serialized critical section (OAR-1). Returns
	won/lost from affected rows. No commit."""
	exp = frappe.utils.add_to_date(None, seconds=ttl_s)
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET reserved=1, reservation_expires_at=%(exp)s, version=version+1
			WHERE name=%(r)s AND state='queued' AND reserved=0""",
			{"r": run_id, "exp": exp},
		)
		== 1
	)


def release_reservation(run_id: str) -> bool:
	"""Reservation lifecycle — RELEASE (D2 §1 #11, OAR-5), unconditional clear of a
	turn's credit (``reserved=0``, expiry NULL) with a version bump. Used by the
	explicit error/recovery release paths; settlement releases the slot inside its
	own settlement CAS. No commit."""
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET reserved=0, reservation_expires_at=NULL, version=version+1
			WHERE name=%(r)s""",
			{"r": run_id},
		)
		== 1
	)


# --------------------------------------------------------------------------- #
# Transition functions — ONE per amended-D2 §4 row
# --------------------------------------------------------------------------- #


def seed_queued_turn(
	*,
	run_id: str,
	conversation: str,
	relay_target_id: str,
	seed_message: str,
	turn_class: str = "interactive",
	dispatch_payload: dict | None = None,
	deadline_at: str | None = None,
) -> bool:
	"""D2 row 1 (∅ -> queued), web, ACTOR-fenced. INSERT the durable Turn with the
	unique ``run_id`` PK — a duplicate send collides on the PK (idempotent). The
	caller MUST hold the shard + conversation locks (canonical order) and allocate
	the seed Message seq under the SAME conv lock (D3 Race 1). Returns True on a
	fresh insert, False on an idempotent duplicate. No commit (caller owns the
	txn). The full accept_send chokepoint (admission + wake) is WP-1c/d — this is
	only the state-machine seed primitive."""
	try:
		doc = frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": run_id,
				"conversation": conversation,
				"relay_target_id": relay_target_id,
				"turn_class": turn_class if turn_class in ("interactive", "background") else "interactive",
				"state": "queued",
				"version": 0,
				"pump_epoch": 0,
				"seed_message": seed_message,
				"dispatch_payload": json.dumps(dispatch_payload) if dispatch_payload else None,
				"enqueued_at": _now(),
				"deadline_at": deadline_at,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		return True
	except frappe.DuplicateEntryError:
		return False


def claim_preparing(run_id: str, version: int, assistant_message: str | None = None) -> bool:
	"""D2 row 2 (queued -> preparing), prep, ACTOR-fenced. CONFIRM + CLAIM the held
	credit: NULL ``reservation_expires_at`` (OAR-5, so a slow prepare is never
	reclaimed), stamp ``preparing_at``, optionally link the assistant placeholder,
	``version+1``. The guard requires a HELD credit (``reserved=1``) — no credit
	=> stays queued (OAR-2; the grant itself is the caller's shard-locked
	``reserve_credit``). Returns won/lost. No commit."""
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='preparing', reserved=1, reservation_expires_at=NULL,
			    preparing_at=%(now)s,
			    assistant_message=COALESCE(%(am)s, assistant_message),
			    version=version+1
			WHERE name=%(r)s AND state='queued' AND version=%(v)s AND reserved=1""",
			{"r": run_id, "now": _now(), "am": assistant_message, "v": version},
		)
		== 1
	)


def attach_placeholder(run_id: str, version: int, assistant_message: str) -> bool:
	"""CDX-7 — attach the assistant placeholder to the Turn through an ACTOR-fenced
	CAS proving the caller still holds the preparing claim (state='preparing' AND
	version=V, the claim version). Does NOT bump version (an intra-claim durable
	write, not a state transition — mark_ready is the transition). A prepare that
	paused past a watchdog reclaim / cancel (which bumped version) affects 0 rows here
	and must clean its orphan placeholder + abort, never attaching a streaming row to a
	re-queued/cancelled turn. Returns won/lost. No commit."""
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}` SET assistant_message=%(am)s
			WHERE name=%(r)s AND state='preparing' AND version=%(v)s""",
			{"am": assistant_message, "r": run_id, "v": version},
		)
		== 1
	)


def store_dispatch_payload(run_id: str, version: int, payload_json: str) -> bool:
	"""CDX-7 — store the prepare->pump handoff ``dispatch_payload`` through an
	ACTOR-fenced CAS proving the caller still holds the preparing claim
	(state='preparing' AND version=V). Does NOT bump version (intra-claim write). A
	losing prepare discards its payload + aborts. Returns won/lost. No commit."""
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}` SET dispatch_payload=%(p)s
			WHERE name=%(r)s AND state='preparing' AND version=%(v)s""",
			{"p": payload_json, "r": run_id, "v": version},
		)
		== 1
	)


def mark_ready(run_id: str, version: int) -> bool:
	"""D2 row 3 (preparing -> ready), prep, ACTOR-fenced. The credit is already
	held; stamp ``ready_at``, ``version+1``. Returns won/lost. No commit."""
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='ready', ready_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='preparing' AND version=%(v)s""",
			{"r": run_id, "now": _now(), "v": version},
		)
		== 1
	)


def prepare_errored(run_id: str, version: int, error: str | None = None) -> bool:
	"""D2 row 4 (preparing -> errored), prep, ACTOR-fenced. Write error, RELEASE
	the reservation (``reserved=0``, else a credit leaks), ``done_at``,
	``version+1``. Returns won/lost. No commit."""
	now = _now()
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='errored', reserved=0, reservation_expires_at=NULL,
			    error=%(e)s, done_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='preparing' AND version=%(v)s""",
			{"r": run_id, "e": (error or "")[:1000], "now": now, "v": version},
		)
		== 1
	)


def confirm_dispatching(run_id: str, version: int, epoch: int, target: str) -> bool:
	"""D2 row 5 (ready -> dispatching), pump, EPOCH-fenced (CDX-2). ``pump_epoch=E``
	is FIRST *stamped on the turn* here, so the guard cannot fence on the turn's own
	``pump_epoch`` (it is not E yet) — instead it proves, IN THE SAME statement, that
	the SHARD control row still carries epoch E via an ``EXISTS`` on
	``Jarvis Relay Pump`` (different table => MariaDB-legal inside an UPDATE, verified
	on patterntest). This closes the stale-``ready``-pump window: a pump that read the
	``ready`` row at epoch E but paused past a takeover (which re-stamps only
	``EPOCH_INFLIGHT_STATES`` — ``ready`` is excluded, §10.5) affects 0 rows here,
	because the control row now holds E+1. A prior Python epoch check is NOT
	sufficient (the takeover can land between the check and this CAS); the proof must
	be in the mutation. CONFIRM-only: the credit was reserved at queued->preparing
	(OAR-2), so NO admission re-check. Stamp ``pump_epoch=E``, ``dispatching_at``,
	``deadline_at`` (= now + TURN_TIMEOUT_SECONDS, D2 #27), ``version+1``. On 0 rows
	the caller re-reads the CONTROL epoch: mismatch => shared lease-loss exit; intact
	=> ordinary state/version CAS loss (§10.11). Returns won/lost. No commit."""
	now = _now()
	deadline = frappe.utils.add_to_date(None, seconds=TURN_TIMEOUT_SECONDS)
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='dispatching', pump_epoch=%(e)s, dispatching_at=%(now)s,
			    deadline_at=%(dl)s, version=version+1
			WHERE name=%(r)s AND state='ready' AND version=%(v)s
			  AND EXISTS (
			        SELECT 1 FROM `tab{PUMP}` p
			        WHERE p.relay_target_id=%(t)s AND p.pump_epoch=%(e)s
			  )""",
			{"r": run_id, "e": epoch, "now": now, "dl": deadline, "v": version, "t": target},
		)
		== 1
	)


def mark_streaming(run_id: str, version: int, epoch: int, gateway_run_id: str | None = None) -> bool:
	"""D2 row 6 (dispatching -> streaming), pump, EPOCH-fenced. Record
	``gateway_run_id`` from the ack, ``first_event_at``, ``version+1``. Returns
	won/lost (0 => caller routes through ``lease_lost_exit``). No commit."""
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='streaming', gateway_run_id=COALESCE(%(g)s, gateway_run_id),
			    first_event_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='dispatching' AND version=%(v)s AND pump_epoch=%(e)s""",
			{"r": run_id, "g": gateway_run_id, "now": _now(), "v": version, "e": epoch},
		)
		== 1
	)


def dispatch_errored(run_id: str, version: int, epoch: int, error: str | None = None) -> bool:
	"""D2 row 7 (dispatching -> errored), pump, EPOCH-fenced (OAR-8). For a
	DEFINITE pre-ack rejection ONLY (chat.send returned ok:false with a concrete
	code — NOT the ack-timeout sentinel; that ambiguous case parks to recovering).
	Write error, RELEASE credit (``reserved=0``), ``finalizing_at``+``done_at``,
	``version+1``. Caller publishes the terminal AFTER commit. Returns won/lost
	(0 => lease-loss exit). No commit."""
	now = _now()
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='errored', reserved=0, reservation_expires_at=NULL,
			    error=%(e)s, finalizing_at=%(now)s, done_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='dispatching' AND version=%(v)s AND pump_epoch=%(ep)s""",
			{"r": run_id, "e": (error or "")[:1000], "now": now, "v": version, "ep": epoch},
		)
		== 1
	)


def apply_delta(
	run_id: str,
	version: int,
	epoch: int,
	event_seq: int,
	assistant_message: str | None,
	content: str,
) -> bool:
	"""D2 row 8 (streaming -> streaming delta), pump, EPOCH-fenced. The guard adds
	``event_seq > last_event_seq`` (Amendment B watermark) so a re-attached or
	duplicate frame is idempotent (affects 0 rows). Advance ``last_event_seq``,
	``version+1``; write the cumulative content mirror to the assistant Message.
	Caller publishes the delta AFTER commit, carrying ``(turn_id, event_seq)``.
	Keep the txn SHORT — no long/blocking work (MariaDB max_statement_time).
	Returns won/lost. No commit."""
	won = (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET last_event_seq=%(seq)s, version=version+1
			WHERE name=%(r)s AND state='streaming' AND version=%(v)s
			  AND pump_epoch=%(e)s AND last_event_seq < %(seq)s""",
			{"r": run_id, "seq": event_seq, "v": version, "e": epoch},
		)
		== 1
	)
	if won and assistant_message is not None:
		_run_cas(
			f"""UPDATE `tab{MSG}` SET content=%(c)s, streaming=1 WHERE name=%(m)s""",
			{"c": content, "m": assistant_message},
		)
	return won


def apply_tool_fenced(run_id: str, version: int, epoch: int, event_seq: int | None) -> bool:
	"""CDX-15 — the Turn-side fence for a DURABLE tool mutation, EPOCH+version+state
	fenced. A tool receipt insert/update is durable state, so — exactly like
	``apply_delta`` — it must commit ONLY together with a CAS proving the turn is STILL
	``streaming`` under epoch E (version+1, watermark advance IN THE SAME txn as the
	caller's tool-row write). The caller (``pump._default_apply_tool``) runs this FIRST,
	writes the tool row only on a win, and commits once; a 0-rows loss => the caller
	applies NOTHING and publishes NOTHING and routes §10.11 (a takeover re-stamped the
	turn to E+1 or settled it past ``streaming`` — the stale pump's write is fenced out).
	When the tool event carries an ``event_seq`` the watermark clause
	(``last_event_seq < seq``) also makes a re-attach/replayed frame idempotent (0 rows =
	benign dup); a tool event with no seq fences on state/version/epoch only. No commit."""
	if event_seq is None:
		return (
			_run_cas(
				f"""UPDATE `tab{TURN}` SET version=version+1
				WHERE name=%(r)s AND state='streaming' AND version=%(v)s AND pump_epoch=%(e)s""",
				{"r": run_id, "v": version, "e": epoch},
			)
			== 1
		)
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET last_event_seq=%(seq)s, version=version+1
			WHERE name=%(r)s AND state='streaming' AND version=%(v)s
			  AND pump_epoch=%(e)s AND last_event_seq < %(seq)s""",
			{"r": run_id, "seq": event_seq, "v": version, "e": epoch},
		)
		== 1
	)


def mark_terminal_observed(
	run_id: str,
	version: int,
	epoch: int,
	terminal_kind: str,
	terminal_payload=None,
) -> bool:
	"""D2 row 9 (streaming -> terminal_observed), pump, EPOCH-fenced. Record
	``terminal_kind`` + ``terminal_payload`` (so SETTLEMENT is recoverable from the
	row alone if the pump dies before settling, R-12) and ``terminal_observed_at``,
	``version+1``. Does NOT write the final projection, release the slot, or run
	enrichment (those are settlement). Returns won/lost (0 => lease-loss exit). No
	commit."""
	payload = _as_payload(terminal_payload)
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='terminal_observed', terminal_kind=%(k)s, terminal_payload=%(p)s,
			    terminal_observed_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='streaming' AND version=%(v)s AND pump_epoch=%(e)s""",
			{"r": run_id, "k": terminal_kind, "p": payload, "now": _now(), "v": version, "e": epoch},
		)
		== 1
	)


def settle_finalizing(
	run_id: str,
	version: int,
	epoch: int,
	*,
	assistant_message: str | None = None,
	final_text: str | None = None,
	required_effects=None,
) -> bool:
	"""D2 row 10 / D3 Race 3 (terminal_observed -> finalizing), settle,
	EPOCH-fenced. THE one critical settlement transaction:
	  (S1) write the final assistant text projection FIRST, in this same txn, so a
	       crash/loss after S1 but before commit rolls BOTH back together.
	  (S2) the settlement CAS: terminal_observed -> finalizing, RELEASE the
	       conversation slot (``reserved=0``), ``finalizing_at``, ``version+1``,
	       fenced by ``pump_epoch=E`` AND ``version=V``.
	  (S4b) INSERT this turn's required ``Jarvis Turn Effect`` rows (OAR-9 — the
	        owed-enrichment set fixed atomically at settlement).
	Returns won/lost. On 0 rows the caller MUST call ``lease_lost_exit`` (rollback
	undoes S1 + LeaseLostExit). On a win the caller commits, then publishes the
	terminal ``run:end`` fenced with ``enrichment_pending`` (SUX-7) and enqueues
	finalize. No commit here."""
	now = _now()
	# (S1) final projection FIRST — rolled back with the CAS if we lose.
	if assistant_message is not None and final_text is not None:
		_run_cas(
			f"""UPDATE `tab{MSG}` SET content=%(c)s, streaming=0 WHERE name=%(m)s""",
			{"c": final_text, "m": assistant_message},
		)
	# (S2) settlement CAS — epoch + version fenced.
	won = (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='finalizing', reserved=0, reservation_expires_at=NULL,
			    finalizing_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='terminal_observed' AND version=%(v)s AND pump_epoch=%(e)s""",
			{"r": run_id, "now": now, "v": version, "e": epoch},
		)
		== 1
	)
	if not won:
		return False
	# (S4b) required effect rows (OAR-9) — the owed-enrichment set, fixed here.
	insert_required_effects(run_id, required_effects or ())
	return True


def settle_errored(
	run_id: str,
	version: int,
	epoch: int,
	error: str | None = None,
	*,
	assistant_message: str | None = None,
	final_text: str | None = None,
) -> bool:
	"""D2 row 11 (terminal_observed -> errored), settle, EPOCH-fenced. The guard
	adds ``terminal_kind='relay:error'`` so a success can NEVER be converted to
	errored. Write ``error``, RELEASE slot (``reserved=0``),
	``finalizing_at``+``done_at`` (terminal — no enrichment loop for an errored
	terminal), ``version+1``. Returns won/lost (0 => lease-loss exit). No commit."""
	now = _now()
	if assistant_message is not None and final_text is not None:
		_run_cas(
			f"""UPDATE `tab{MSG}` SET content=%(c)s, streaming=0, error=%(e)s WHERE name=%(m)s""",
			{"c": final_text, "e": (error or "")[:1000], "m": assistant_message},
		)
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='errored', reserved=0, reservation_expires_at=NULL,
			    error=%(e)s, finalizing_at=%(now)s, done_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='terminal_observed' AND terminal_kind='relay:error'
			  AND version=%(v)s AND pump_epoch=%(ep)s""",
			{"r": run_id, "e": (error or "")[:1000], "now": now, "v": version, "ep": epoch},
		)
		== 1
	)


def finalize_done(run_id: str, version: int) -> bool:
	"""D2 row 12 (finalizing -> done), fin, ACTOR-fenced. Guard: state='finalizing'
	AND version=V AND EVERY required ``Jarvis Turn Effect`` row is ``done`` (a
	force-done row counts as done). Set ``done_at``, ``version+1``. The ``NOT
	EXISTS`` in the CAS makes the all-effects-done guard atomic. Because force-done
	guarantees every effect reaches done, a permanently-failing enrichment can
	NEVER strand a settled turn in ``finalizing`` (D2 §1a). Caller publishes
	``message:enriched`` (SUX-7) AFTER commit. Returns won/lost. No commit."""
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='done', done_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='finalizing' AND version=%(v)s
			  AND NOT EXISTS (
			        SELECT 1 FROM `tab{EFFECT}` e WHERE e.turn=%(r)s AND e.status!='done'
			  )""",
			{"r": run_id, "now": _now(), "v": version},
		)
		== 1
	)


# D2 row 13 (finalizing -> errored): the explicit NEVER row — enrichment failure
# NEVER errors a settled turn (D2 §1a; force-done guarantees `done` is reached).
# There is deliberately NO function for it; a settled turn cannot be un-settled.


def cancel_queued(run_id: str, version: int) -> bool:
	"""D2 row 14 (queued -> cancelled), web, ACTOR-fenced. Release reservation,
	``done_at``, ``version+1``. No gateway call (never dispatched). Returns
	won/lost. No commit."""
	now = _now()
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='cancelled', reserved=0, reservation_expires_at=NULL,
			    cancel_requested=1, done_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='queued' AND version=%(v)s""",
			{"r": run_id, "now": now, "v": version},
		)
		== 1
	)


def cancel_queued_max_age(run_id: str, version: int, reason: str) -> bool:
	"""D2 row 15 (queued -> cancelled, system max-age SUX-5), sys, ACTOR-fenced.
	The guard adds ``now > enqueued_at + QUEUED_MAX_AGE_S`` (=900s). Release
	reservation, record the user-visible ``reason`` (both ``cancel_reason`` and
	``error``), ``done_at``, ``version+1``. Caller publishes the terminal carrying
	the reason. Returns won/lost. No commit."""
	now = _now()
	cutoff = frappe.utils.add_to_date(None, seconds=-QUEUED_MAX_AGE_S)
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='cancelled', reserved=0, reservation_expires_at=NULL,
			    cancel_reason=%(reason)s, error=%(reason)s, done_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='queued' AND version=%(v)s
			  AND enqueued_at IS NOT NULL AND enqueued_at < %(cut)s""",
			{"r": run_id, "reason": reason, "now": now, "v": version, "cut": cutoff},
		)
		== 1
	)


def cancel_preparing_or_ready(run_id: str, version: int) -> bool:
	"""D2 row 16 (preparing|ready -> cancelled), web, ACTOR-fenced. Release
	reservation, ``done_at``, ``version+1``. No gateway abort (nothing in flight).
	Returns won/lost. No commit."""
	now = _now()
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='cancelled', reserved=0, reservation_expires_at=NULL,
			    cancel_requested=1, done_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state IN ('preparing','ready') AND version=%(v)s""",
			{"r": run_id, "now": now, "v": version},
		)
		== 1
	)


def request_cancel(run_id: str, version: int) -> bool:
	"""D2 row 17 (dispatching|streaming -> cancel intent), web, ACTOR-fenced. Set
	``cancel_requested=1``, ``version+1`` — NOT terminal (the in-flight run must be
	aborted first; the pump drives the out-of-band chat.abort, then records the
	aborted terminal). Returns won/lost. No commit."""
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}` SET cancel_requested=1, version=version+1
			WHERE name=%(r)s AND state IN ('dispatching','streaming') AND version=%(v)s""",
			{"r": run_id, "v": version},
		)
		== 1
	)


def record_aborted_terminal(run_id: str, state: str, version: int, epoch: int) -> bool:
	"""D2 row 18 (streaming|dispatching -> terminal_observed, aborted), pump,
	EPOCH-fenced. Guard: ``cancel_requested=1`` AND state=X AND version=V AND
	pump_epoch=E (the pump has issued the out-of-band chat.abort). Record
	``terminal_kind='relay:error'`` with an aborted marker in ``terminal_payload``,
	``terminal_observed_at``, ``version+1``. ``cancel_requested=1`` is what
	distinguishes this aborted terminal from a plain error terminal at
	``settle_cancelled`` time. Returns won/lost. No commit."""
	if state not in ("streaming", "dispatching"):
		raise ValueError(f"record_aborted_terminal: illegal from-state {state!r}")
	payload = json.dumps({"aborted": True})
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='terminal_observed', terminal_kind='relay:error',
			    terminal_payload=%(p)s, terminal_observed_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state=%(s)s AND cancel_requested=1
			  AND version=%(v)s AND pump_epoch=%(e)s""",
			{"r": run_id, "p": payload, "now": _now(), "s": state, "v": version, "e": epoch},
		)
		== 1
	)


def settle_cancelled(run_id: str, version: int, epoch: int) -> bool:
	"""D2 row 19 (terminal_observed aborted -> cancelled), settle, EPOCH-fenced.
	Guard: state='terminal_observed' AND ``cancel_requested=1`` (the aborted
	marker) AND version=V AND pump_epoch=E. RELEASE slot (``reserved=0``),
	``done_at``, ``version+1``. No usage accrual for a cancelled turn. Returns
	won/lost (0 => lease-loss exit). No commit."""
	now = _now()
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='cancelled', reserved=0, reservation_expires_at=NULL,
			    done_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='terminal_observed' AND cancel_requested=1
			  AND version=%(v)s AND pump_epoch=%(e)s""",
			{"r": run_id, "now": now, "v": version, "e": epoch},
		)
		== 1
	)


def mark_recovering(
	run_id: str,
	version: int,
	*,
	require_prepare_deadline: bool = False,
	require_deadline_passed: bool = False,
	epoch: int | None = None,
) -> bool:
	"""D2 row 20 (any nonterminal -> recovering, EXCLUDES finalizing per R-13), wd,
	ACTOR-fenced (the watchdog is not a pump). Set ``recovering=1``,
	``recovery_started_at``, ``was_recovered=1``, ``version+1`` (and state=
	'recovering'). The base guard allows only
	``queued|preparing|ready|dispatching|streaming|terminal_observed`` — never
	``finalizing`` (a settled turn cannot be un-settled). The watchdog decides WHY
	(lease stale / reconcile-gone / deadline); the optional flags fold the
	row-level deadline predicates INTO the CAS:
	  * ``require_prepare_deadline`` adds state='preparing' AND
	    now > preparing_at + PREPARE_DEADLINE_S (=300, OAR-5).
	  * ``require_deadline_passed`` adds now > deadline_at.
	CDX-24 ``epoch`` (pump-driven parks ONLY): when given, ADD ``pump_epoch=%(e)s``
	so a delayed OLD pump whose lease epoch a takeover already bumped parks 0 rows
	(the takeover re-stamped its adopted in-flight turns to the new epoch — §10.11).
	Watchdog/recovery callers (not pumps) omit ``epoch`` and keep the actor-fenced
	form. A pre-dispatch turn (pump_epoch=0, no gateway run) therefore is NOT parked
	by an epoch-fenced pump park; reconcile-on-start / the watchdog reclaim it.
	Caller publishes ``run:recovering`` fenced AFTER commit (SUX-1). Returns
	won/lost. No commit."""
	now = _now()
	clauses = [
		"name=%(r)s",
		"state IN ('queued','preparing','ready','dispatching','streaming','terminal_observed')",
		"version=%(v)s",
	]
	params = {"r": run_id, "v": version, "now": now}
	if epoch is not None:
		clauses.append("pump_epoch=%(e)s")
		params["e"] = epoch
	if require_prepare_deadline:
		clauses.append("state='preparing' AND preparing_at IS NOT NULL AND preparing_at < %(pcut)s")
		params["pcut"] = frappe.utils.add_to_date(None, seconds=-PREPARE_DEADLINE_S)
	if require_deadline_passed:
		clauses.append("deadline_at IS NOT NULL AND deadline_at < %(now)s")
	where = " AND ".join(clauses)
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='recovering', recovering=1, recovery_started_at=%(now)s,
			    was_recovered=1, version=version+1
			WHERE {where}""",
			params,
		)
		== 1
	)


def recover_to_queued(run_id: str, version: int) -> bool:
	"""D2 row 21 (recovering -> queued, FRESH prepare, parked PRE-dispatch, OAR-4),
	rec/wd, ACTOR-fenced. Guard: state='recovering' AND version=V AND
	``dispatching_at IS NULL`` (origin queued/preparing/ready). RELEASE credit
	(``reserved=0``), NULL ``reservation_expires_at``, clear ``recovering``, DROP
	stale prepare refs (``assistant_message``/``preparing_at``/``ready_at``) so it
	re-prepares from scratch (session at-most-once absorbs the leak, OAR-4),
	``version+1``. Returns won/lost. No commit."""
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='queued', recovering=0, reserved=0, reservation_expires_at=NULL,
			    assistant_message=NULL, preparing_at=NULL, ready_at=NULL, version=version+1
			WHERE name=%(r)s AND state='recovering' AND version=%(v)s AND dispatching_at IS NULL""",
			{"r": run_id, "v": version},
		)
		== 1
	)


def recover_adopt(run_id: str, version: int, new_epoch: int, target_state: str) -> bool:
	"""D2 row 22 (recovering -> dispatching|streaming, ADOPT, was IN flight, OAR-4),
	rec, EPOCH-fenced ON ADOPTION. Guard: state='recovering' AND version=V AND
	``dispatching_at IS NOT NULL``. RE-STAMP ``pump_epoch=E_new``, clear
	``recovering``, ``version+1``. The retry reuses the SAME ``run_id``/idempotency
	key (no new key, no re-prepare — the prompt is already assembled). Returns
	won/lost. No commit."""
	if target_state not in ("dispatching", "streaming"):
		raise ValueError(f"recover_adopt: illegal target_state {target_state!r}")
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state=%(ts)s, recovering=0, pump_epoch=%(e)s, version=version+1
			WHERE name=%(r)s AND state='recovering' AND version=%(v)s AND dispatching_at IS NOT NULL""",
			{"r": run_id, "ts": target_state, "e": new_epoch, "v": version},
		)
		== 1
	)


def recover_to_terminal_observed(
	run_id: str,
	version: int,
	new_epoch: int,
	terminal_kind: str,
	terminal_payload=None,
) -> bool:
	"""D2 row 23 (recovering -> terminal_observed, missed terminal), rec,
	EPOCH-fenced ON ADOPTION. Guard: state='recovering' AND version=V AND
	``dispatching_at IS NOT NULL`` (snapshot reconciliation shows the run actually
	finished). Record ``terminal_kind``/``terminal_payload`` FROM THE SNAPSHOT,
	re-stamp ``pump_epoch=E_new`` (adoption), clear ``recovering``,
	``terminal_observed_at``, ``version+1``. Never fabricates final text not in the
	snapshot. Returns won/lost. No commit."""
	payload = _as_payload(terminal_payload)
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='terminal_observed', recovering=0, pump_epoch=%(e)s,
			    terminal_kind=%(k)s, terminal_payload=%(p)s,
			    terminal_observed_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='recovering' AND version=%(v)s AND dispatching_at IS NOT NULL""",
			{"r": run_id, "e": new_epoch, "k": terminal_kind, "p": payload, "now": _now(), "v": version},
		)
		== 1
	)


def recover_errored(run_id: str, version: int, error: str | None = None) -> bool:
	"""D2 row 24 (recovering -> errored, recovery budget exhausted), rec/wd,
	ACTOR-fenced. Write ``error``, release credit (``reserved=0``), ``done_at``,
	clear ``recovering``, ``version+1``. The caller decides budget exhaustion
	(``recovery_started_at`` age). Returns won/lost. No commit."""
	now = _now()
	return (
		_run_cas(
			f"""UPDATE `tab{TURN}`
			SET state='errored', recovering=0, reserved=0, reservation_expires_at=NULL,
			    error=%(e)s, done_at=%(now)s, version=version+1
			WHERE name=%(r)s AND state='recovering' AND version=%(v)s""",
			{"r": run_id, "e": (error or "")[:1000], "now": now, "v": version},
		)
		== 1
	)


# --------------------------------------------------------------------------- #
# Effect ledger API (D2 §1a, OAR-9)
# --------------------------------------------------------------------------- #


def _effect_name(run_id: str, effect_name: str) -> str:
	"""The composite doc name = the UNIQUE (turn, effect_name) key."""
	return f"{run_id}::{effect_name}"


def insert_required_effects(run_id: str, effect_names) -> int:
	"""Effect ledger — INSERT the required rows at settlement (OAR-9), idempotently:
	the composite name PK (``turn::effect_name``) makes a duplicate a no-op. This
	fixes the owed-enrichment set atomically the instant the slot is released.
	Returns the count freshly inserted. No commit (runs inside the settlement
	txn)."""
	inserted = 0
	for name in effect_names or ():
		try:
			doc = frappe.get_doc(
				{"doctype": EFFECT, "turn": run_id, "effect_name": name, "status": "pending", "attempts": 0}
			)
			doc.flags.ignore_permissions = True
			doc.insert()
			inserted += 1
		except frappe.DuplicateEntryError:
			pass
	return inserted


def claim_effect(run_id: str, effect_name: str) -> tuple[str, str | None]:
	"""Effect ledger — EXCLUSIVELY CLAIM one effect for a finalize attempt (D2 §1a,
	CDX-4). The claim is ONE atomic CAS (``pending``/stale-``running`` -> ``running``)
	that stamps a fresh ``claim_token`` + ``claimed_at`` AND increments ``attempts``,
	with the affected-rows count as the success signal — so two concurrent finalizers
	can never both run the same effect (only one wins the CAS; the other sees a live
	``running`` claim). Returns ``(outcome, token)``:
	  * ``('done', None)``       — already done (or no such required row); skip.
	  * ``('attempt', token)``   — EXCLUSIVELY claimed; ``attempts`` incremented, the
	                               row is now ``running`` under ``token``. The caller
	                               runs the effect then ``complete_effect(.., token)``
	                               on success, or ``release_effect(.., token)`` on
	                               failure (back to pending for the next cycle).
	  * ``('busy', None)``       — another finalizer holds a LIVE (non-stale) claim;
	                               skip this cycle (its owner will complete/release it).
	                               CDX-16: returned REGARDLESS of attempt count — a live
	                               third-attempt claim can NEVER be force-completed out
	                               from under the finalizer executing its side effect.
	  * ``('force_done', None)`` — ``attempts >= FINALIZE_MAX_ATTEMPTS`` on a PENDING or
	                               stale-``running`` row; the effect is FORCE-DONE
	                               (status=done, logged) and skipped, so the turn always
	                               reaches ``done`` (OAR-9).
	A ``running`` row whose ``claimed_at`` is older than ``EFFECT_CLAIM_STALE_S`` (its
	finalizer crashed) is RECLAIMABLE by the claim CAS. No commit (caller owns the txn;
	it commits after the claim so the attempt increment survives a mid-effect crash).

	CDX-16 ordering: the claim CAS wins ONLY on a ``pending``/stale-``running`` row
	UNDER budget, so a LIVE running claim affects 0 rows here. On 0 rows the disambig is
	the ``force_done_effect`` CAS (itself guarded to pending/stale-running only): it wins
	=> budget-exhausted force-done; it 0-rows => a live claim is running => BUSY. Both
	the read-then-force-done TOCTOU and the attempt-vs-liveness order defect are closed
	because liveness is proved INSIDE each mutation, never by a prior Python read."""
	name = _effect_name(run_id, effect_name)
	row = frappe.db.get_value(EFFECT, name, ["status"], as_dict=True)
	if not row or row["status"] == "done":
		return ("done", None)
	token = frappe.generate_hash(length=16)
	stale_cut = frappe.utils.add_to_date(None, seconds=-EFFECT_CLAIM_STALE_S)
	won = (
		_run_cas(
			f"""UPDATE `tab{EFFECT}`
			SET status='running', claim_token=%(tok)s, claimed_at=%(now)s, attempts=attempts+1
			WHERE name=%(n)s AND attempts < %(max)s
			  AND ( status='pending'
			        OR (status='running' AND (claimed_at IS NULL OR claimed_at < %(stale)s)) )""",
			{"n": name, "tok": token, "now": _now(), "max": FINALIZE_MAX_ATTEMPTS, "stale": stale_cut},
		)
		== 1
	)
	if won:
		return ("attempt", token)
	# 0 rows. CDX-16: force-done ONLY a pending/stale-running row at/over budget; a LIVE
	# running claim (another finalizer's, any attempt count) 0-rows the force-done CAS
	# and is reported BUSY so its running side effect is never marked done from under it.
	if force_done_effect(run_id, effect_name):
		return ("force_done", None)
	return ("busy", None)


def complete_effect(run_id: str, effect_name: str, token: str | None = None) -> bool:
	"""Effect ledger — mark a CLAIMED effect ``done`` inside its own idempotent txn
	(D2 §1a, CDX-4). Fenced on the claim ``token``: a finalizer whose ``running`` claim
	was already reclaimed (it stalled past ``EFFECT_CLAIM_STALE_S`` and another
	finalizer took the effect) affects 0 rows here and does NOT overwrite the new
	owner's work. When ``token`` is None the guard degrades to ``status='running'``
	(any live claim), used by legacy callers/tests. Returns won/lost. No commit."""
	name = _effect_name(run_id, effect_name)
	if token is not None:
		return (
			_run_cas(
				f"""UPDATE `tab{EFFECT}` SET status='done', applied_at=%(now)s
				WHERE name=%(n)s AND status='running' AND claim_token=%(tok)s""",
				{"n": name, "now": _now(), "tok": token},
			)
			== 1
		)
	return (
		_run_cas(
			f"""UPDATE `tab{EFFECT}` SET status='done', applied_at=%(now)s
			WHERE name=%(n)s AND status='running'""",
			{"n": name, "now": _now()},
		)
		== 1
	)


def release_effect(run_id: str, effect_name: str, token: str | None = None) -> bool:
	"""Effect ledger — RELEASE a claimed effect back to ``pending`` after a FAILED
	attempt (CDX-4), so the next finalize cycle retries it (the attempt increment
	stays, driving the force-done budget). Fenced on the claim ``token`` so a stalled
	finalizer whose claim was reclaimed cannot un-claim the new owner's ``running``
	row. Returns won/lost. No commit."""
	name = _effect_name(run_id, effect_name)
	if token is not None:
		return (
			_run_cas(
				f"""UPDATE `tab{EFFECT}` SET status='pending', claim_token=NULL
				WHERE name=%(n)s AND status='running' AND claim_token=%(tok)s""",
				{"n": name, "tok": token},
			)
			== 1
		)
	return (
		_run_cas(
			f"""UPDATE `tab{EFFECT}` SET status='pending', claim_token=NULL
			WHERE name=%(n)s AND status='running'""",
			{"n": name},
		)
		== 1
	)


def force_done_effect(run_id: str, effect_name: str) -> bool:
	"""Effect ledger — FORCE an effect ``done`` after the attempt budget (OAR-9):
	skipped + logged, never retried forever, so a permanently-failing enrichment
	can never strand a settled turn.

	CDX-16: the CAS applies ONLY to a ``pending`` or STALE-``running`` row (its
	finalizer crashed) — a LIVE (non-stale) ``running`` claim affects 0 rows and is
	NEVER force-completed while its finalizer may still be executing the side effect.
	Returns won/lost (0 rows => a live claim exists, no-op). No commit. Only logs on an
	ACTUAL force-done (a win)."""
	name = _effect_name(run_id, effect_name)
	stale_cut = frappe.utils.add_to_date(None, seconds=-EFFECT_CLAIM_STALE_S)
	won = (
		_run_cas(
			f"""UPDATE `tab{EFFECT}` SET status='done', applied_at=%(now)s
			WHERE name=%(n)s AND status!='done'
			  AND ( status='pending'
			        OR (status='running' AND (claimed_at IS NULL OR claimed_at < %(stale)s)) )""",
			{"n": name, "now": _now(), "stale": stale_cut},
		)
		== 1
	)
	if won:
		try:
			frappe.log_error(
				title="turn_state.force_done_effect",
				message=f"turn={run_id} effect={effect_name} force-done after {FINALIZE_MAX_ATTEMPTS} attempts",
			)
		except Exception:
			pass
	return won


def all_required_effects_done(run_id: str) -> bool:
	"""True iff no required effect row for the turn is still pending/running (the D2
	row 12 finalize guard, mirrored in Python for the caller's pre-check)."""
	return not frappe.db.sql(
		f"""SELECT 1 FROM `tab{EFFECT}` WHERE turn=%(r)s AND status!='done' LIMIT 1""",
		{"r": run_id},
	)


def shards_with_open_effects() -> list[str]:
	"""CDX-4 — every shard that has a turn with a still-open (``pending``/``running``)
	effect row, INDEPENDENT of that turn's terminal state. Lets the pump watchdog
	recover a crash-after-settlement-commit-before-``enqueue_finalize`` on an
	errored/cancelled (terminal) turn whose owed effects would otherwise be invisible
	to a nonterminal-only scan."""
	rows = frappe.db.sql(
		f"""SELECT DISTINCT t.relay_target_id
		FROM `tab{EFFECT}` e INNER JOIN `tab{TURN}` t ON t.run_id = e.turn
		WHERE e.status != 'done'"""
	)
	return [r[0] for r in rows if r[0]]


def turns_with_open_effects(target: str) -> list[str]:
	"""CDX-4 — every turn on ``target`` with a still-open (``pending``/stale-
	``running``) effect row. The watchdog re-enqueues finalize for each so a lost/
	crashed ``enqueue_finalize`` (settlement committed but finalize never ran) is
	recovered regardless of the turn's state (finalizing OR errored/cancelled). A
	live (non-stale) ``running`` claim is EXCLUDED — its finalizer owns it — so the
	watchdog never fights a healthy in-flight finalize."""
	stale_cut = frappe.utils.add_to_date(None, seconds=-EFFECT_CLAIM_STALE_S)
	rows = frappe.db.sql(
		f"""SELECT DISTINCT e.turn
		FROM `tab{EFFECT}` e INNER JOIN `tab{TURN}` t ON t.run_id = e.turn
		WHERE t.relay_target_id=%(t)s
		  AND ( e.status='pending'
		        OR (e.status='running' AND (e.claimed_at IS NULL OR e.claimed_at < %(stale)s)) )""",
		{"t": target, "stale": stale_cut},
	)
	return [r[0] for r in rows]


# --------------------------------------------------------------------------- #
# Lease acquire / renew / heartbeat / conditional idle-release (D2 §3, D4, R-17)
# --------------------------------------------------------------------------- #


def lease_acquire(target: str, holder: str, hop_counter: int | None = None) -> tuple[bool, int | None]:
	"""Lease ACQUIRE / takeover / self-hop (D4 c/d, §10.5, R-11) — one uniform
	adoption path for cold starts, crashes, takeovers, and clean hops. In ONE
	transaction:
	  1. Conditionally ``pump_epoch = pump_epoch + 1`` + set
	     ``lease_holder``/``lease_expires_at`` ONLY when the lease is vacant
	     (``lease_expires_at IS NULL``) or stale (``< now``). This IS the
	     acquisition CAS D4(d) relies on: exactly one racer's UPDATE sees the stale
	     predicate and bumps; the others block on the control-row lock, then
	     re-evaluate against the freshened lease and affect 0 rows.
	  2. On win, RE-STAMP adopted in-flight turns (dispatching/streaming/
	     terminal_observed) to the new epoch and bump their version (D4 c t3), so a
	     delayed old pump's cached-E CAS affects 0 rows and exits.
	  3. Optionally set ``hop_counter`` (a fresh hop mints a fresh job id).
	Commit-first REPEATABLE-READ discipline: the ``frappe.db.commit()`` below
	starts a FRESH transaction so the acquire UPDATE is the first statement and no
	stale snapshot precedes it; the acquire UPDATE's WHERE re-evaluates the
	predicate on the locked control row at write time (correct under both READ
	COMMITTED and REPEATABLE READ, D4 Q2). Returns (won, new_epoch). Manages its
	own transaction (a standalone pump lifecycle op)."""
	_ensure_control_row(target)
	frappe.db.commit()
	now = _now()
	exp = frappe.utils.add_to_date(None, seconds=LEASE_TTL_S)
	hop_set = ", hop_counter=%(hop)s" if hop_counter is not None else ""
	params = {"t": target, "h": holder, "exp": exp, "now": now}
	if hop_counter is not None:
		params["hop"] = hop_counter
	won = (
		_run_cas(
			f"""UPDATE `tab{PUMP}`
			SET pump_epoch=pump_epoch+1, lease_holder=%(h)s, lease_expires_at=%(exp)s{hop_set}
			WHERE relay_target_id=%(t)s
			  AND (lease_expires_at IS NULL OR lease_expires_at < %(now)s)""",
			params,
		)
		== 1
	)
	if not won:
		frappe.db.rollback()
		return (False, None)
	epoch = int(frappe.db.get_value(PUMP, target, "pump_epoch") or 0)
	# Re-stamp adopted in-flight turns (D4 c t3, §10.5): bounded batch (<= ceiling).
	# OARF-7: the clean-hop re-stamp does NOT set was_recovered — a turn that merely
	# spans a hop boundary (hops are 60-120s, turns can run to 600s) was NOT
	# recovered. was_recovered is set ONLY on a GENUINE recovery transition
	# (mark_recovering when a turn is parked; _settle_recovered_final on a real
	# snapshot recovery), so the flag keeps meaning "the answer was reconstructed"
	# for support/telemetry and the SUX-6 SR announcement.
	inflight = "','".join(EPOCH_INFLIGHT_STATES)
	_run_cas(
		f"""UPDATE `tab{TURN}`
		SET pump_epoch=%(e)s, version=version+1
		WHERE relay_target_id=%(t)s AND state IN ('{inflight}')""",
		{"e": epoch, "t": target},
	)
	frappe.db.commit()
	return (True, epoch)


def lease_handoff(target: str, epoch: int, next_hop: int, successor_holder: str) -> bool:
	"""Clean-hop ATOMIC lease TRANSFER (CDX-1, D6 §3). A normal hop reaching its soft
	budget must make its successor IMMEDIATELY able to acquire — a bare re-enqueue is
	not enough, because the running hop renewed its lease to ~now+30s, so the
	successor's ``lease_acquire`` (``lease_expires_at IS NULL OR < now``) would find a
	still-future lease, affect 0 rows, and exit successor-less (the stranded-turn
	defect). Under the control-row epoch, in ONE statement, this:
	  * PROVES this pump still holds epoch E (``pump_epoch=%(e)s`` in the WHERE) — a
	    stale outgoing hop whose epoch a takeover already bumped matches 0 rows, so the
	    caller writes NO counter and enqueues NO successor (it must not resurrect a
	    shard a newer owner holds);
	  * makes the lease immediately acquirable (``lease_expires_at`` set 1s in the
	    PAST — unambiguously expired for the successor's ``< now`` acquire predicate,
	    the same convention ``lease_release_if_idle`` uses);
	  * advances ``hop_counter`` (a fresh hop mints a fresh job id — the frappe dedupe
	    trap) — this counter write is now epoch-guarded (CDX-1: a stale hop must not
	    bump it);
	  * stamps ``lease_holder`` = the successor job id (a transfer HINT for
	    observability — the successor's epoch bump on acquire is the real fence, not
	    this value).
	The caller COMMITS via this function, THEN enqueues the fresh successor id.
	Returns won/lost. Commits its own transaction (a standalone lifecycle op)."""
	frappe.db.commit()
	past = frappe.utils.add_to_date(None, seconds=-1)
	won = (
		_run_cas(
			f"""UPDATE `tab{PUMP}`
			SET lease_expires_at=%(past)s, hop_counter=%(hop)s, lease_holder=%(h)s
			WHERE relay_target_id=%(t)s AND pump_epoch=%(e)s""",
			{"past": past, "hop": next_hop, "h": successor_holder, "t": target, "e": epoch},
		)
		== 1
	)
	frappe.db.commit()
	return won


def lease_renew(target: str, epoch: int, holder: str | None = None) -> bool:
	"""Extend the lease TTL while THIS pump still holds the epoch (D2 §3). CAS on
	``pump_epoch=E`` — a pump that lost the epoch to a takeover renews 0 rows and
	must exit. Returns won/lost. Commits (a standalone heartbeat op)."""
	frappe.db.commit()
	exp = frappe.utils.add_to_date(None, seconds=LEASE_TTL_S)
	sets = "lease_expires_at=%(exp)s"
	params = {"exp": exp, "t": target, "e": epoch}
	if holder is not None:
		sets += ", lease_holder=%(h)s"
		params["h"] = holder
	won = (
		_run_cas(
			f"""UPDATE `tab{PUMP}` SET {sets} WHERE relay_target_id=%(t)s AND pump_epoch=%(e)s""",
			params,
		)
		== 1
	)
	frappe.db.commit()
	return won


def lease_heartbeat(target: str, epoch: int) -> bool:
	"""Write ``loop_heartbeat_ts`` (loop-WRITTEN liveness, DISTINCT from lease
	renewal, Amendment E) while THIS pump holds the epoch — catches the
	live-but-wedged pump the lease alone cannot. Returns won/lost. Commits."""
	frappe.db.commit()
	won = (
		_run_cas(
			f"""UPDATE `tab{PUMP}` SET loop_heartbeat_ts=%(now)s
			WHERE relay_target_id=%(t)s AND pump_epoch=%(e)s""",
			{"now": _now(), "t": target, "e": epoch},
		)
		== 1
	)
	frappe.db.commit()
	return won


def lease_release_if_idle(target: str, epoch: int) -> bool:
	"""Idle exit = ONE ATOMIC conditional release (R-17, OAR-12), never
	check-then-release. Release the lease ONLY if THIS pump holds the epoch AND no
	nonterminal turn remains on the shard. Returns:
	  * ``True``  — released; the pump exits.
	  * ``False`` — 0 rows (work remains OR the epoch was lost); the caller MUST
	    CONTINUE the drain loop (OAR-12, normative), never treat it as released.
	Commit-first REPEATABLE-READ discipline: the ``NOT EXISTS`` subquery must see
	turns committed by concurrent senders, so we commit first to start a fresh
	transaction whose read view is established at this statement. Commits."""
	frappe.db.commit()
	nonterm = "','".join(NONTERMINAL_STATES)
	released = (
		_run_cas(
			f"""UPDATE `tab{PUMP}`
			SET lease_holder=NULL, lease_expires_at=%(past)s
			WHERE relay_target_id=%(t)s AND pump_epoch=%(e)s
			  AND NOT EXISTS (
			        SELECT 1 FROM `tab{TURN}`
			        WHERE relay_target_id=%(t)s AND state IN ('{nonterm}')
			  )""",
			{"t": target, "e": epoch, "past": frappe.utils.add_to_date(None, seconds=-1)},
		)
		== 1
	)
	frappe.db.commit()
	return released


# --------------------------------------------------------------------------- #
# Fenced publish (Amendment B / D4 e)
# --------------------------------------------------------------------------- #


def publish_fenced(
	user: str,
	kind: str,
	*,
	conversation_id: str,
	run_id: str,
	event_seq: int | None = None,
	pump_epoch: int | None = None,
	relay_target_id: str | None = None,
	**extra,
) -> None:
	"""Fenced realtime publish (Amendment B, D4 e, CDX-3). Call ONLY after the
	winning CAS has COMMITTED — a stale pump raises ``LeaseLostExit`` before reaching
	here and so never publishes on the happy path. Every pump-owned payload also
	carries ``(turn_id, event_seq, pump_epoch)`` so a stale writer's late frame is
	fenced END-TO-END at the client: the client keeps the greatest accepted
	``(turn_id, epoch, seq)`` and ignores a lower-epoch event, an equal-epoch
	lower/equal-seq frame, or ANY event that arrives after a terminal at a higher
	epoch. Events published WITHOUT a ``pump_epoch`` (legacy transport, prepare/
	finalize actor-fenced events) bypass the client epoch fence unchanged.

	BELT (CDX-3): when ``pump_epoch`` + ``relay_target_id`` are given, re-read the
	shard's CURRENT control epoch and SKIP the publish when this caller's epoch is
	already stale. This is a cheap PK read that suppresses the common stale-writer
	case server-side, but it CANNOT close the final race — a takeover can land
	between this check and the socket write — which is exactly why the client epoch
	fence above is the real guarantee. Best-effort (a publish failure never breaks
	the committed transition)."""
	try:
		if pump_epoch is not None and relay_target_id is not None:
			current = frappe.db.get_value(PUMP, relay_target_id, "pump_epoch")
			if current is None or int(current) != int(pump_epoch):
				return  # stale writer — belt-skip (the client fence is the real guarantee)
		payload = {"kind": kind, "conversation_id": conversation_id, "run_id": run_id, "turn_id": run_id}
		if event_seq is not None:
			payload["event_seq"] = event_seq
		if pump_epoch is not None:
			payload["pump_epoch"] = pump_epoch
		payload.update(extra)
		publish_to_user(user, payload)
	except Exception:
		pass


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _as_payload(value) -> str | None:
	"""Normalize a terminal payload to a JSON string column value."""
	if value is None or isinstance(value, str):
		return value
	try:
		return json.dumps(value)
	except (TypeError, ValueError):
		return str(value)
