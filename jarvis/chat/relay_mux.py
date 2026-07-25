"""WP-1b — the one-reader WebSocket multiplexer (Relay Pump transport core).

This module is the I/O adapter the pump reactor (D6) drives. It owns exactly
ONE ``OpenclawSession`` (one WS per ``relay_target_id``) and is the SOLE caller
of that socket's ``_recv`` — the single-reader constraint the pool warns about
(``openclaw_session_pool.py:21-38``: two readers "would steal frames between
turns") is not relaxed, it is INVERTED: one reader serves every turn on the
container by DEMUXing frames instead of discarding non-matching ones.

Binding contract (read in order):
  * ../../../jarvis-chat-concurrency-design/implementation/wp-d/D5-mux-protocol.md
    (AMENDED) — the reader-loop state diagram, pending-RPC map, run map with
    fresh-send pre-registration (OAR-15), serialized send, bounded cancellable
    futures + fail-all-on-Closing (OAR-10), integrity-class quarantine (delta
    faults drop+count+continue, precious faults quarantine ONE lane, OAR-7),
    per-shard poison-rate circuit breaker, stray-frame counter (Amendment I).
  * implementation/spikes/S2-gateway-semantics.md — ack at accept
    {runId,status}; broadcast agent/chat frames keyed by runId.
  * jarvis/chat/openclaw_client.py — the frame classifiers this module adapts
    around (``_is_response_frame``, ``_build_request_frame``, ``parse_event``,
    ``_chat_final_text``/``_chat_final_failed``, the ``ack-timeout`` sentinel,
    the serialized-send ``_lock`` idiom).

HARD INVARIANTS (this component is TRANSPORT-PURE):
  * NO ``frappe.db`` writes, NO ``turn_state`` calls inside this file — the mux
    routes frames and invokes PUMP-PROVIDED callbacks; every DB CAS lives in
    those callbacks (pump side). This is what keeps the mux unit-testable
    against the fake gateway alone, and it is why a mux that keeps reading
    after lease loss is still harmless: its callbacks' epoch+version CAS writes
    (pump side) affect 0 rows (D5 non-goal "the mux is not the fence").
  * The reader NEVER blocks on a lane (it only enqueues into a bounded per-lane
    queue with an explicit overflow policy), NEVER invokes a lane callback, and
    NEVER sends. Lane application is caller-driven via ``dispatch`` so it runs
    on the pump's thread (which owns the DB connection).
  * The send path is serialized by the session's one ``_lock`` (reused here) so
    the reader loop and any RPC issuance never interleave partial frames.
  * No second reader ever touches ``_recv`` (R-15): one thread owns the socket
    read; RPC futures are resolved on that SAME reader loop.
  * Only THREE causes end a hop (D5 §2/§5): socket failure, lease loss, or
    site-DB failure. THIS module only observes the socket cause directly (the
    reader's ``_recv`` raises) → ``Closing``. Lease loss and site-DB failure
    are pump-side signals; the pump stops driving / ``stop()``s the mux.

Threading model (works under both the gevent realtime executor and RQ threads —
the waits use ``threading`` primitives the realtime process monkey-patches into
cooperative waits, exactly as ``openclaw_session_pool`` does):
  * READER thread (mux-owned, no DB): the sole ``_recv`` caller. Classifies each
    frame and either resolves a pending-RPC future, routes an event to its lane
    queue, or increments the stray counter. Never blocks, never calls a lane
    callback.
  * PUMP thread (the caller, has the DB): issues RPCs (``issue_rpc`` /
    ``send_chat`` / ``abort`` / ``set_session_model`` → a bounded cancellable
    future it awaits) and drains lane queues by calling ``dispatch``, which
    invokes the lane callbacks with integrity-class fault handling.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from jarvis.chat.events import parse_event
from jarvis.chat.openclaw_client import (
	FAILED_FINAL_ERROR,
	OpenclawSession,
	_build_request_frame,
	_chat_final_failed,
	_chat_final_text,
)
from jarvis.exceptions import OpenclawUnreachableError

_logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

# How long the reader blocks in one ``_recv`` before waking to re-check the
# stop/close flags. Small so a graceful ``stop()`` returns promptly; a warm
# socket returns on the first frame regardless.
READER_SOFT_TIMEOUT_S = 0.5

# Bound on a single lane's pending-application queue. Deltas arrive at ~150ms
# cumulative cadence, so 512 buffered frames is ~76s of un-applied stream — far
# beyond any healthy apply lag. The bound exists so a pathologically slow apply
# consumer can NEVER make the reader block or grow memory unboundedly:
#   * LOSSY overflow (a delta) → drop the OLDEST buffered delta and count it;
#     the next cumulative mirror supersedes the dropped frame (self-healing).
#   * PRECIOUS overflow (a tool/terminal frame that cannot be dropped) →
#     QUARANTINE the lane (park it for snapshot recovery) rather than lose a
#     precious fact. Precious frames are rare, so this only fires under a truly
#     wedged consumer.
LANE_QUEUE_MAX = 512

# CDX-13 lane-fairness: one ``dispatch`` drains at most LANE_QUANTUM events from a
# single lane per round-robin pass, and at most DISPATCH_EVENT_BUDGET events overall
# per call. A backed-up hot lane can therefore no longer monopolise the callback/DB/
# publish work for hundreds of events before a cold lane is serviced — the passes
# interleave the lanes, and any residue re-arms the wake so the next dispatch
# continues at once (latency isolation between conversations on the single reader).
LANE_QUANTUM = 32
DISPATCH_EVENT_BUDGET = 512

# Default RPC ack window (mirrors CONNECT_TIMEOUT_SECONDS) when a caller does
# not pass its own timeout_s.
DEFAULT_RPC_TIMEOUT_S = 10.0

# Per-shard poison-rate circuit breaker (OAR-7). When more than THRESHOLD lanes
# quarantine within WINDOW seconds the breaker OPENS: it fires the alarm
# callback once and (for callers that ask) refuses to re-adopt a run, so a
# systematic delta/terminal-parse skew cannot loop every credit through the
# recovery budget.
POISON_BREAKER_THRESHOLD = 5
POISON_BREAKER_WINDOW_S = 60.0

# Integrity classes (D5 §4/§9 integrity-class law).
LOSSY = "lossy"  # cumulative-mirror deltas — a dropped frame self-heals
PRECIOUS = "precious"  # terminal + tool frames — must be applied exactly once


# --------------------------------------------------------------------------- #
# Bounded cancellable RPC future (§3 "bounded cancellable future")
# --------------------------------------------------------------------------- #


class RpcCancelled(Exception):
	"""Raised by ``PendingRpc.result`` when a takeover/shutdown cancelled the
	in-flight RPC (``stop()``) — distinct from an ``ack-timeout`` so the caller
	knows the abandonment was deliberate, not a false gateway error."""


class PendingRpc:
	"""One outstanding RPC. Registered in the pending-RPC map at issuance and
	resolved ON THE READER LOOP when the matching response frame arrives — a
	non-blocking resolve, so the reader keeps draining broadcast frames for
	OTHER turns while this RPC is outstanding (§3). Bounded (``result`` waits at
	most ``timeout_s``, then raises the ``ack-timeout`` sentinel so the pump
	parks for snapshot recovery instead of reporting a false error) and
	cancellable (a takeover/shutdown abandons it without leaking)."""

	def __init__(self, method: str, req_id: str, timeout_s: float):
		self.method = method
		self.req_id = req_id
		self.timeout_s = timeout_s
		self._event = threading.Event()
		self._result: dict | None = None
		self._exc: BaseException | None = None
		self._on_settle: Callable[[str], None] | None = None

	# -- reader-side resolution (called on the reader loop) --------------- #

	def set_result(self, frame: dict) -> None:
		if self._event.is_set():
			return
		self._result = frame
		self._event.set()

	def set_exception(self, exc: BaseException) -> None:
		if self._event.is_set():
			return
		self._exc = exc
		self._event.set()

	def cancel(self) -> None:
		self.set_exception(RpcCancelled(f"{self.method} cancelled"))

	# -- caller-side await ------------------------------------------------ #

	def result(self, timeout: float | None = None) -> dict:
		"""Block until resolved, then return the response frame or raise. On the
		bounded timeout raise the ``ack-timeout`` sentinel (ambiguous outcome:
		the request frame was written, the peer may have accepted it — the pump
		parks for recovery, never a false error)."""
		wait_s = self.timeout_s if timeout is None else timeout
		if not self._event.wait(wait_s):
			# Remove ourselves so a late resolve doesn't linger in the map.
			if self._on_settle is not None:
				self._on_settle(self.req_id)
			raise OpenclawUnreachableError(f"{self.method} timed out", code="ack-timeout")
		if self._exc is not None:
			raise self._exc
		return self._result  # type: ignore[return-value]

	@property
	def done(self) -> bool:
		return self._event.is_set()


# --------------------------------------------------------------------------- #
# Lane-event + per-turn lane
# --------------------------------------------------------------------------- #


@dataclass
class _LaneEvent:
	"""A decoded, class-tagged frame waiting to be applied to a lane."""

	cls: str  # LOSSY | PRECIOUS
	kind: str  # "delta" | "tool" | "terminal"
	event_seq: int
	data: dict


@dataclass
class LaneHandler:
	"""The PUMP-PROVIDED callback set for one turn's lane — the contract WP-1c
	wires to ``turn_state``. All callbacks are invoked ON THE PUMP THREAD (inside
	``dispatch``), never on the reader loop, so the DB CAS they perform runs on a
	thread that owns a frappe connection. All are optional (default no-op).

	Callback signatures (see WP-1B report for the full contract):
	  * ``on_delta(event_seq: int, text: str, delta: str)`` — a cumulative-mirror
	    assistant delta (LOSSY). ``text`` is the full cumulative text; ``delta``
	    is the incremental fragment. A raise ⇒ drop+count+CONTINUE the SAME lane
	    (OAR-7): the next mirror supersedes it.
	  * ``on_tool(event: dict)`` — a tool start/end frame (PRECIOUS). ``event``
	    has ``event_seq, phase, tool_name, tool_call_id, status, title``. A raise
	    ⇒ QUARANTINE this lane only.
	  * ``on_terminal(kind: str, payload: dict)`` — the run's terminal chat frame
	    (PRECIOUS, applied exactly once). ``kind`` ∈ {``relay:final``,
	    ``relay:error``}; ``payload`` = {``text``} for a final, or {``state``,
	    ``error``} for an error/aborted/failed_final. A raise ⇒ QUARANTINE.
	  * ``on_quarantine(reason: str)`` — the mux fenced this lane off (precious
	    fault, precious overflow). The pump parks the turn toward ``recovering``.
	  * ``on_closing(sentinel: str)`` — the socket died: this lane lost its
	    transport. The pump re-attaches from durable state on the next hop.
	"""

	on_delta: Callable[[int, str, str], None] | None = None
	on_tool: Callable[[dict], None] | None = None
	on_terminal: Callable[[str, dict], None] | None = None
	on_quarantine: Callable[[str], None] | None = None
	on_closing: Callable[[str], None] | None = None


class _Lane:
	"""One per-turn lane — the unit of quarantine (D5 §5). Holds the turn's
	bounded pending-application queue, its watermark cursor (the last event_seq
	the mux ASSIGNED — monotonic, seeded from the pump-supplied ``start_seq`` so
	the DB watermark stays monotonic across a re-attach), and its callbacks."""

	def __init__(self, run_id: str, handler: LaneHandler, *, session_key: str, start_seq: int):
		self.run_id = run_id
		self.session_key = session_key
		self.handler = handler
		self.lock = threading.Lock()
		self.queue: deque[_LaneEvent] = deque()
		self.watermark = int(start_seq)
		self.state = "active"  # active | quarantined | terminal | closed
		self.poison_count = 0

	def next_seq(self) -> int:
		self.watermark += 1
		return self.watermark

	def offer(self, ev: _LaneEvent) -> str:
		"""Reader-side, non-blocking enqueue. Returns:
		* ``"ok"``        — buffered.
		* ``"dropped"``   — LOSSY overflow: an old delta was dropped (count it).
		* ``"quarantine"``— PRECIOUS overflow: the lane must be quarantined.
		* ``"closed"``    — lane is no longer active (drop, treat as stray)."""
		with self.lock:
			if self.state != "active":
				return "closed"
			if len(self.queue) < LANE_QUEUE_MAX:
				self.queue.append(ev)
				return "ok"
			# Full.
			if ev.cls == LOSSY:
				# Drop the OLDEST lossy delta to make room (self-healing mirror).
				for i, old in enumerate(self.queue):
					if old.cls == LOSSY:
						del self.queue[i]
						self.queue.append(ev)
						return "dropped"
				# No lossy frame to evict (queue is all precious): drop incoming.
				return "dropped"
			# Precious frame cannot be dropped and cannot fit → quarantine.
			self.state = "quarantined"
			return "quarantine"

	def drain(self, max_events: int | None = None) -> list[_LaneEvent]:
		"""Pump-side: pop up to ``max_events`` currently-buffered events (all when
		None) under the lane lock, then release it BEFORE the caller applies them (so a
		slow callback never blocks the reader's ``offer``). CDX-13: the bounded pop is
		the per-lane quantum that keeps a hot lane from starving the others in one
		``dispatch`` pass."""
		with self.lock:
			if not self.queue:
				return []
			if max_events is None or max_events >= len(self.queue):
				events = list(self.queue)
				self.queue.clear()
				return events
			return [self.queue.popleft() for _ in range(max_events)]


# --------------------------------------------------------------------------- #
# The multiplexer
# --------------------------------------------------------------------------- #


class RelayMux:
	"""One-reader WS multiplexer for one ``relay_target_id``.

	Construct it with an already-connected ``OpenclawSession`` (the pump owns
	connect/reconnect — the mux is the I/O adapter on a given socket). Call
	``start()`` to spawn the reader; the pump then registers runs, issues RPCs,
	and calls ``dispatch()`` to apply buffered frames.
	"""

	def __init__(
		self,
		session: OpenclawSession,
		relay_target_id: str,
		*,
		on_breaker: Callable[[str, int], None] | None = None,
		breaker_threshold: int = POISON_BREAKER_THRESHOLD,
		breaker_window_s: float = POISON_BREAKER_WINDOW_S,
	):
		self._session = session
		self.relay_target_id = relay_target_id

		# pending-RPC map: req_id -> PendingRpc
		self._pending: dict[str, PendingRpc] = {}
		self._pending_lock = threading.Lock()

		# run map: gateway_run_id -> _Lane  (+ deferred-quarantine handoff list)
		self._runs: dict[str, _Lane] = {}
		self._quarantine_pending: list[tuple[_Lane, str]] = []
		self._map_lock = threading.Lock()

		# reader-loop lifecycle
		self._reader: threading.Thread | None = None
		self._stopping = threading.Event()  # graceful stop requested
		self._closed = threading.Event()  # socket dead / torn down
		self._wake = threading.Event()  # reader poked dispatch: work available

		# circuit breaker
		self._on_breaker = on_breaker
		self._breaker_threshold = breaker_threshold
		self._breaker_window_s = breaker_window_s
		self._quarantine_times: deque[float] = deque()
		self._breaker_open = False
		self._breaker_lock = threading.Lock()

		# telemetry counters (Amendment I)
		self._counter_lock = threading.Lock()
		self._stray_frames = 0
		self._deltas_dropped = 0
		self._lanes_quarantined = 0
		self._rpc_failed_on_close = 0
		self._chat_delta_ignored = 0

	# -- lifecycle ---------------------------------------------------------- #

	def start(self) -> RelayMux:
		"""Spawn the single reader thread. Idempotent-ish: start once per mux."""
		if self._reader is not None:
			raise RuntimeError("RelayMux.start() called twice")
		self._reader = threading.Thread(
			target=self._read_loop, name=f"relay-mux-{self.relay_target_id}", daemon=True
		)
		self._reader.start()
		return self

	def stop(self, *, timeout: float = 5.0) -> None:
		"""Graceful teardown (hop end / takeover). Stops the reader, CANCELS every
		in-flight RPC (so no awaiter dead-waits), and closes the socket. Does NOT
		fire ``on_closing`` — a graceful stop is not transport loss; the durable
		turn state persists and the next hop re-attaches."""
		self._stopping.set()
		self._wake.set()
		if self._reader is not None:
			self._reader.join(timeout=timeout)
		self._cancel_all_pending()
		self._closed.set()
		try:
			self._session.close()
		except Exception:
			pass

	def is_closed(self) -> bool:
		return self._closed.is_set()

	def is_breaker_open(self) -> bool:
		with self._breaker_lock:
			return self._breaker_open

	def poke(self) -> None:
		"""C1 (WP-2 Stage B): wake a slice blocked in ``dispatch(block_s>0)`` from
		OUTSIDE the reader loop. The pump's wake-bus thread calls this the instant an
		accept LPUSHes the cross-process wake bus, so the slice's idle block waits on
		whichever of {a delivered frame, a resolved ack, an accept wake} fires first —
		no polling theatre. Setting the same ``_wake`` Event the reader uses keeps the
		block single-sourced; a spurious poke costs at most one no-op drain."""
		self._wake.set()

	# -- reader loop (the sole _recv owner, R-15) --------------------------- #

	def _read_loop(self) -> None:
		while not self._stopping.is_set():
			try:
				frame = self._session._recv(READER_SOFT_TIMEOUT_S)
			except OpenclawUnreachableError as exc:
				# Socket failure — the ONLY hop-ending cause this module observes
				# directly (§2/§5). Everything else routes back to reading.
				self._begin_closing(exc)
				return
			except Exception as exc:  # pragma: no cover - defensive
				self._begin_closing(OpenclawUnreachableError(f"reader loop error: {exc}"))
				return
			if frame is None:
				# soft timeout / non-JSON noise (swallowed by _recv) — never crash
				continue
			try:
				self._classify(frame)
			except Exception:  # pragma: no cover - a classify bug must not kill the hop
				self._bump("stray_frames")
				_logger.debug("relay_mux: classify raised on frame; counted as stray", exc_info=True)
		# graceful stop path: reader observed the stop flag
		self._closed.set()

	def _classify(self, frame: dict) -> None:
		ftype = frame.get("type")
		if ftype == "res":
			self._resolve_response(frame)
			return
		if ftype == "event":
			self._route_event(frame)
			return
		# Unknown frame type — late/unknown/noise (D5 rule 4). Count, never crash.
		self._bump("stray_frames")

	def _resolve_response(self, frame: dict) -> None:
		req_id = frame.get("id")
		with self._pending_lock:
			fut = self._pending.pop(req_id, None)
		if fut is None:
			# response to an unknown/already-settled request id → stray.
			self._bump("stray_frames")
			return
		if not frame.get("ok"):
			# Preserve today's structured error classification (code + details)
			# so the issuing pump path classifies stale-pairing etc. as it does
			# now (openclaw_client._request:1023-1030).
			err = frame.get("error") or {}
			details = err.get("details")
			fut.set_exception(
				OpenclawUnreachableError(
					f"{fut.method} rejected: {err.get('code', '?')}: {err.get('message', '')}",
					code=err.get("code"),
					details=details if isinstance(details, dict) else None,
				)
			)
			# C1 (WP-2 Stage B): a resolved RPC is a delivery too — poke the block so a
			# slice waiting in dispatch() wakes AT ONCE (see set_result path below).
			self._wake.set()
			return
		fut.set_result(frame)
		# C1 (WP-2 Stage B): the chat.send ack resolves HERE, on the reader loop. Set
		# the same wake Event a delivered frame sets so the pump's slice returns from
		# its idle block immediately and _poll_acks runs mark_streaming this slice —
		# instead of dead-blocking to the SLICE_BLOCK_S poll ceiling before the turn
		# can leave `dispatching` and stream its first token.
		self._wake.set()

	def _route_event(self, frame: dict) -> None:
		event = frame.get("event")
		payload = frame.get("payload") or {}
		run_id = payload.get("runId")
		if not run_id:
			self._bump("stray_frames")
			return
		with self._map_lock:
			lane = self._runs.get(run_id)
		if lane is None:
			# Unknown/late/foreign runId (drop-and-count is the safe fallback,
			# R-16 / OAR-15): no frame of OUR fresh run can precede its ack, and
			# a genuinely-unknown id is a late or foreign frame.
			self._bump("stray_frames")
			return
		# Defensive sessionKey cross-check (runId is globally unique, so this only
		# guards against a foreign frame that happens to reuse an id).
		sk = payload.get("sessionKey")
		if event == "chat" and lane.session_key and sk and sk != lane.session_key:
			self._bump("stray_frames")
			return
		if event == "agent":
			self._route_agent(lane, payload)
		elif event == "chat":
			self._route_terminal(lane, payload)
		else:
			self._bump("stray_frames")

	def _route_agent(self, lane: _Lane, payload: dict) -> None:
		parsed = parse_event(payload)
		if parsed is None or parsed.get("kind") == "lifecycle":
			# lifecycle frames are dropped on the managed relay path (mirror of
			# relay_turn_events); not a stray — it's an expected known-run drop.
			return
		kind = parsed.get("kind")
		if kind == "assistant":
			seq = lane.next_seq()
			ev = _LaneEvent(
				cls=LOSSY,
				kind="delta",
				event_seq=seq,
				data={"text": parsed.get("text", ""), "delta": parsed.get("delta", "")},
			)
		elif kind == "tool":
			seq = lane.next_seq()
			ev = _LaneEvent(
				cls=PRECIOUS,
				kind="tool",
				event_seq=seq,
				data={
					"phase": parsed.get("phase"),
					"tool_name": parsed.get("tool_name"),
					"tool_call_id": parsed.get("tool_call_id"),
					"status": parsed.get("status"),
					"title": parsed.get("tool_title"),
				},
			)
		else:  # pragma: no cover - parse_event only yields the three kinds
			return
		self._offer(lane, ev)

	def _route_terminal(self, lane: _Lane, payload: dict) -> None:
		state = payload.get("state")
		if state == "final":
			text = _chat_final_text(payload)
			if _chat_final_failed(payload, text):
				term_kind, term_payload = (
					"relay:error",
					{"state": "failed_final", "error": FAILED_FINAL_ERROR},
				)
			else:
				term_kind, term_payload = "relay:final", {"text": text}
		elif state in ("error", "aborted"):
			term_kind = "relay:error"
			term_payload = {"state": state, "error": payload.get("errorMessage") or state}
		else:
			# chat state=delta (the 150ms cumulative mirror) and unknown states
			# are ignored — the tokens ride the `agent` frames (mirror of
			# relay_turn_events:779). Known run, expected drop — not stray.
			self._bump("chat_delta_ignored")
			return
		seq = lane.next_seq()
		ev = _LaneEvent(
			cls=PRECIOUS,
			kind="terminal",
			event_seq=seq,
			data={"terminal_kind": term_kind, "payload": term_payload},
		)
		self._offer(lane, ev)

	def _offer(self, lane: _Lane, ev: _LaneEvent) -> None:
		status = lane.offer(ev)
		if status == "ok":
			self._wake.set()
		elif status == "dropped":
			self._bump("deltas_dropped")
			self._wake.set()
		elif status == "quarantine":
			# PRECIOUS overflow (D5 §4 overflow-on-precious). Hand off to the pump
			# thread — the reader never invokes a lane callback.
			self._defer_quarantine(lane, "precious_overflow")
		# "closed": lane no longer active; drop silently (its frames are stale).

	# -- send path (serialized by the session lock) ------------------------- #

	def issue_rpc(self, method: str, params: dict, *, timeout_s: float = DEFAULT_RPC_TIMEOUT_S) -> PendingRpc:
		"""Issue one RPC and return its bounded cancellable future. Registers the
		future in the pending-RPC map BEFORE the frame is written (so a fast
		response can never arrive before the map knows about it), then sends under
		the serialized send lock. The reader resolves the future when the response
		arrives; the caller awaits ``future.result(timeout)``."""
		if self._closed.is_set() or self._stopping.is_set():
			raise OpenclawUnreachableError(f"{method} on a closed mux", code="ack-timeout")
		payload, req_id = _build_request_frame(method, params)
		fut = PendingRpc(method, req_id, timeout_s)
		fut._on_settle = self._drop_pending
		with self._pending_lock:
			self._pending[req_id] = fut
		try:
			# Serialized send: the session's one _lock guards every socket write,
			# so the reader loop and this issuance never interleave partial frames.
			with self._session._lock:
				self._session._ws.send(payload)
		except Exception as exc:
			self._drop_pending(req_id)
			raise OpenclawUnreachableError(f"{method} send failed: {exc}") from exc
		return fut

	def send_chat(
		self,
		session_key: str,
		message: str,
		run_id: str,
		handler: LaneHandler,
		*,
		timeout_s: float = DEFAULT_RPC_TIMEOUT_S,
		start_seq: int = 0,
		thinking: str | None = None,
		deliver: bool = False,
		attachments: list[dict] | None = None,
	) -> PendingRpc:
		"""FRESH-send fast path with early-frame routing (OAR-15). PRE-REGISTERS
		the run map under ``run_id == idempotencyKey`` at ISSUANCE (before the
		ack), so any frame arriving in the issuance→ack window routes to its lane
		immediately. Returns the ack future.

		Retry-attach (an ack of ``status:"in_flight"`` returning an EXISTING
		run's id) is NOT handled here: the caller inspects the ack and calls
		``rekey_run(run_id, ack_run_id)`` when the returned id differs."""
		self.register_run(run_id, handler, session_key=session_key, start_seq=start_seq)
		params: dict = {
			"sessionKey": session_key,
			"message": message,
			"idempotencyKey": run_id,
			"deliver": deliver,
		}
		if thinking:
			params["thinking"] = thinking
		if attachments:
			params["attachments"] = attachments
		return self.issue_rpc("chat.send", params, timeout_s=timeout_s)

	def set_session_model(
		self, session_key: str, model_ref: str, *, timeout_s: float = DEFAULT_RPC_TIMEOUT_S
	) -> PendingRpc:
		"""``sessions.patch`` — per-conversation model override, issued mid-stream
		without stealing any lane's frames."""
		return self.issue_rpc("sessions.patch", {"key": session_key, "model": model_ref}, timeout_s=timeout_s)

	def abort(
		self, session_key: str, run_id: str | None = None, *, timeout_s: float = DEFAULT_RPC_TIMEOUT_S
	) -> PendingRpc:
		"""``chat.abort`` — the mux ISSUES the abort RPC when the pump asks, but
		abort authority does not live in the reader loop (D5 non-goal: the direct
		out-of-band abort route stays primary)."""
		params: dict = {"sessionKey": session_key}
		if run_id:
			params["runId"] = run_id
		return self.issue_rpc("chat.abort", params, timeout_s=timeout_s)

	# -- run map management ------------------------------------------------- #

	def register_run(
		self,
		run_id: str,
		handler: LaneHandler,
		*,
		session_key: str = "",
		start_seq: int = 0,
		is_readopt: bool = False,
	) -> _Lane | None:
		"""Create + install a lane in the run map. Used both for FRESH sends
		(pre-registration under ``run_id``, OAR-15) and for reconnect rebuild /
		retry-attach (the pump feeds the lane spec). When the poison-rate breaker
		is OPEN and this is a RE-ADOPT, the mux REFUSES (returns ``None``) rather
		than loop the run through the recovery budget again (OAR-7)."""
		if is_readopt and self.is_breaker_open():
			return None
		lane = _Lane(run_id, handler, session_key=session_key, start_seq=start_seq)
		with self._map_lock:
			self._runs[run_id] = lane
		return lane

	def rekey_run(self, from_key: str, to_key: str) -> bool:
		"""Retry-attach re-key (§6 / R-20): move a lane from its fresh
		``idempotencyKey`` to the EXISTING gateway run id returned by an
		``in_flight`` ack. Returns True on success."""
		if from_key == to_key:
			return True
		with self._map_lock:
			lane = self._runs.pop(from_key, None)
			if lane is None:
				return False
			lane.run_id = to_key
			self._runs[to_key] = lane
		return True

	def unregister_run(self, run_id: str) -> None:
		with self._map_lock:
			self._runs.pop(run_id, None)

	# -- application (pump-thread; the mux invokes the pump callbacks here) -- #

	def dispatch(self, *, block_s: float = 0.0, max_events: int | None = None) -> int:
		"""Drain buffered lane events and apply them via the pump-provided
		callbacks, with integrity-class fault handling. MUST be called on the
		pump thread (the one with the DB connection). Returns the number of
		events applied.

		``block_s`` > 0 waits up to that long for the reader to signal work before
		draining (so a driver loop need not busy-spin). Deferred quarantine
		notifications (from precious-overflow on the reader) are delivered here
		too, on this thread.

		CDX-13 — FAIR draining: round-robin passes over the lanes, each pass taking at
		most ``LANE_QUANTUM`` events from a lane, capped at ``max_events`` (default
		``DISPATCH_EVENT_BUDGET``) events overall per call. A hot lane can no longer
		drain its whole 512-event backlog before a cold lane is serviced. Any residue
		(budget tripped with work remaining) RE-ARMS the wake so the next dispatch
		continues immediately — no event is stranded, just fairly deferred."""
		budget = max_events if max_events is not None else DISPATCH_EVENT_BUDGET
		if block_s > 0 and not self._has_work():
			self._wake.wait(block_s)
		self._wake.clear()

		self._flush_deferred_quarantines()

		with self._map_lock:
			lanes = list(self._runs.values())

		applied = 0
		# Round-robin passes: interleave the lanes a quantum at a time until nothing
		# remains or the per-call budget is spent (fairness on the single reader).
		while applied < budget:
			progressed = False
			for lane in lanes:
				if applied >= budget:
					break
				take = min(LANE_QUANTUM, budget - applied)
				drained = lane.drain(take)
				if not drained:
					continue
				progressed = True
				for ev in drained:
					self._apply(lane, ev)
					applied += 1
					if applied >= budget:
						break
			if not progressed:
				break

		# CDX-13: if the budget was spent with buffered events still pending, re-arm the
		# wake so the driver's next dispatch drains the residue at once (never stalls).
		if applied >= budget and self._has_work():
			self._wake.set()
		return applied

	def _apply(self, lane: _Lane, ev: _LaneEvent) -> None:
		h = lane.handler
		try:
			if ev.kind == "delta":
				if h.on_delta is not None:
					h.on_delta(ev.event_seq, ev.data["text"], ev.data["delta"])
			elif ev.kind == "tool":
				if h.on_tool is not None:
					h.on_tool({"event_seq": ev.event_seq, **ev.data})
			elif ev.kind == "terminal":
				if h.on_terminal is not None:
					h.on_terminal(ev.data["terminal_kind"], ev.data["payload"])
				# The terminal is the last frame of a healthy run — retire the
				# lane so subsequent frames for this id count as stray.
				self._retire_lane(lane)
		except Exception:
			lane.poison_count += 1
			if ev.cls == LOSSY:
				# Poison DELTA (OAR-7): DROP the frame, count it, CONTINUE the
				# SAME lane. The next cumulative mirror supersedes it; a lossy
				# fault NEVER parks the turn.
				self._bump("deltas_dropped")
				_logger.debug(
					"relay_mux: poison delta on run=%s seq=%s dropped+continued",
					lane.run_id,
					ev.event_seq,
					exc_info=True,
				)
			else:
				# Poison PRECIOUS (terminal/tool): QUARANTINE this ONE lane; its
				# neighbours keep streaming on the same socket (D5 §5).
				self._quarantine(lane, "precious_fault")

	# -- quarantine + circuit breaker (OAR-7) ------------------------------- #

	def _defer_quarantine(self, lane: _Lane, reason: str) -> None:
		"""Reader-side: mark the lane quarantined and hand the callback to the
		pump thread (the reader never invokes a lane callback)."""
		with self._map_lock:
			self._runs.pop(lane.run_id, None)
			self._quarantine_pending.append((lane, reason))
		self._wake.set()

	def _flush_deferred_quarantines(self) -> None:
		with self._map_lock:
			pending = self._quarantine_pending
			self._quarantine_pending = []
		for lane, reason in pending:
			self._notify_quarantine(lane, reason)

	def _quarantine(self, lane: _Lane, reason: str) -> None:
		"""Pump-side: fence one lane off, remove it from the run map, fire its
		``on_quarantine`` callback, and record it against the poison-rate
		breaker. Called inline for an apply fault; deferred-list for a
		reader-side overflow."""
		with lane.lock:
			if lane.state in ("terminal", "closed"):
				return
			lane.state = "quarantined"
		with self._map_lock:
			self._runs.pop(lane.run_id, None)
		self._notify_quarantine(lane, reason)

	def _notify_quarantine(self, lane: _Lane, reason: str) -> None:
		self._bump("lanes_quarantined")
		self._record_quarantine()
		if lane.handler.on_quarantine is not None:
			try:
				lane.handler.on_quarantine(reason)
			except Exception:
				_logger.debug("relay_mux: on_quarantine callback raised", exc_info=True)

	def _record_quarantine(self) -> None:
		fire = False
		count = 0
		with self._breaker_lock:
			now = time.monotonic()
			self._quarantine_times.append(now)
			while self._quarantine_times and now - self._quarantine_times[0] > self._breaker_window_s:
				self._quarantine_times.popleft()
			count = len(self._quarantine_times)
			if not self._breaker_open and count > self._breaker_threshold:
				self._breaker_open = True
				fire = True
		if fire and self._on_breaker is not None:
			try:
				self._on_breaker(self.relay_target_id, count)
			except Exception:
				_logger.debug("relay_mux: on_breaker callback raised", exc_info=True)

	def _retire_lane(self, lane: _Lane) -> None:
		with lane.lock:
			lane.state = "terminal"
		with self._map_lock:
			self._runs.pop(lane.run_id, None)

	# -- Closing (transport loss, OAR-10) ----------------------------------- #

	def _begin_closing(self, exc: BaseException) -> None:
		"""Socket died. BEFORE anything reconnects (the pump's job, not the
		mux's): fail ALL pending-RPC futures with the transport sentinel — an
		orphaned ``chat.send`` ack would otherwise dead-wait its full timeout,
		leaving the ``dispatching`` turn stuck; failing immediately lets each
		awaiter resolve and the turn park deterministically (OAR-10). Then notify
		every active lane via ``on_closing`` so the pump re-attaches from durable
		state on the next hop. Does NOT reconnect."""
		if self._closed.is_set():
			return
		detail = str(exc)
		sentinel = OpenclawUnreachableError(f"relay transport lost: {detail}", code="ack-timeout")
		# 1. fail ALL pending RPC futures (immediately, no dead-wait).
		with self._pending_lock:
			futs = list(self._pending.values())
			self._pending.clear()
		for fut in futs:
			fut.set_exception(sentinel)
		with self._counter_lock:
			self._rpc_failed_on_close += len(futs)
		# 2. notify every active lane; the run map holds no truth the DB lacks.
		with self._map_lock:
			lanes = list(self._runs.values())
			self._runs.clear()
			self._quarantine_pending = []
		for lane in lanes:
			if lane.handler.on_closing is not None:
				try:
					lane.handler.on_closing("transport")
				except Exception:
					_logger.debug("relay_mux: on_closing callback raised", exc_info=True)
		self._closed.set()
		self._wake.set()
		try:
			self._session.close()
		except Exception:
			pass

	def _cancel_all_pending(self) -> None:
		with self._pending_lock:
			futs = list(self._pending.values())
			self._pending.clear()
		for fut in futs:
			fut.cancel()

	def _drop_pending(self, req_id: str) -> None:
		with self._pending_lock:
			self._pending.pop(req_id, None)

	# -- telemetry ---------------------------------------------------------- #

	def _bump(self, name: str, n: int = 1) -> None:
		with self._counter_lock:
			setattr(self, f"_{name}", getattr(self, f"_{name}") + n)

	def _has_work(self) -> bool:
		with self._map_lock:
			if self._quarantine_pending:
				return True
			for lane in self._runs.values():
				if lane.queue:
					return True
		return False

	def stats(self) -> dict:
		"""Telemetry snapshot (Amendment I). ``stray_frames`` settles the demoted
		"N workers discard everyone else's frames" question; ``deltas_dropped``
		and ``lanes_quarantined`` expose the poison surface."""
		with self._counter_lock:
			return {
				"stray_frames": self._stray_frames,
				"deltas_dropped": self._deltas_dropped,
				"lanes_quarantined": self._lanes_quarantined,
				"rpc_failed_on_close": self._rpc_failed_on_close,
				"chat_delta_ignored": self._chat_delta_ignored,
				"breaker_open": self.is_breaker_open(),
				"active_runs": len(self._runs),
			}
