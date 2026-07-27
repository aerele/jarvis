"""WP-1b — tests for the one-reader WS multiplexer (`chat/relay_mux.py`).

Transport policy: the suite forbids REAL sockets (``jarvis/tests/__init__.py``
rebinds ``websocket.create_connection`` to protect a live tenant), and its
stated remedy is "mock the transport your code uses". So these tests drive the
mux through an in-process transport double at the exact ``_recv`` / ``_send``
seam ``OpenclawSession`` exposes (the same seam ``test_relay_consumer.py``
stubs), REPLAYING the WP-2 harness transcripts (``jarvis.tests.harness.
transcripts``) frame-for-frame. Every millisecond of reader-loop demux, lane
dispatch, integrity-class fault handling, quarantine, and the circuit breaker
is the REAL mux; only the socket is doubled. The real-socket
``fake_gateway.FakeGateway`` remains the deliberate e2e path (Stage A ran it
under ``JARVIS_ALLOW_REAL_NETWORK_IN_TESTS``); it is not used inside the guarded
suite.

Determinism comes from the double's frame ordering + waiting on the recording
callbacks' ``threading.Event``s, never fixed sleeps in the assertions.

R-21 note: the ack-before-frames protocol fact the demux relies on was
re-asserted against the REAL pinned 2026.6.8 image by Stage A's
``probe_real_gateway.py`` (output at
``implementation/wp-2/baseline/r21_real_gateway_probe.json``, ok=true); not
re-run here (read-only, no container churn).
"""

from __future__ import annotations

import itertools
import json
import queue
import threading
import time
from unittest.mock import MagicMock

from frappe.tests.utils import FrappeTestCase

from jarvis.chat.relay_mux import LaneHandler, RelayMux
from jarvis.exceptions import OpenclawUnreachableError
from jarvis.tests.harness import transcripts as _transcripts

# Player cadence (fast; deterministic). Pauses in transcripts are capped so a
# 600ms compaction pause does not slow the suite.
_CADENCE_S = 0.001
_PAUSE_CAP_S = 0.01


# --------------------------------------------------------------------------- #
# Recording lane handler
# --------------------------------------------------------------------------- #


class _Recorder:
	"""A lane callback set that records everything and signals completion. Poison
	is injected by making a specific callback raise — integrity-class behaviour
	is driven by the mux, not by the raise itself (OAR-7)."""

	def __init__(self, *, poison_delta_seq: int | None = None, poison_terminal: bool = False):
		self.deltas: list[tuple[int, str, str]] = []
		self.tools: list[dict] = []
		self.terminal: tuple[str, dict] | None = None
		self.quarantined: str | None = None
		self.closing: str | None = None
		self.poison_delta_seq = poison_delta_seq
		self.poison_terminal = poison_terminal
		self.done = threading.Event()  # set on terminal | quarantine | closing

	def handler(self) -> LaneHandler:
		return LaneHandler(
			on_delta=self._on_delta,
			on_tool=self._on_tool,
			on_terminal=self._on_terminal,
			on_quarantine=self._on_quarantine,
			on_closing=self._on_closing,
		)

	def _on_delta(self, seq, text, delta):
		if self.poison_delta_seq is not None and seq == self.poison_delta_seq:
			raise RuntimeError(f"poison delta seq={seq}")
		self.deltas.append((seq, text, delta))

	def _on_tool(self, ev):
		self.tools.append(ev)

	def _on_terminal(self, kind, payload):
		if self.poison_terminal:
			raise RuntimeError("poison terminal")
		self.terminal = (kind, payload)
		self.done.set()

	def _on_quarantine(self, reason):
		self.quarantined = reason
		self.done.set()

	def _on_closing(self, sentinel):
		self.closing = sentinel
		self.done.set()


def _agent_frame(run_id, session_key, stream, data):
	return {
		"type": "event",
		"event": "agent",
		"payload": {"runId": run_id, "sessionKey": session_key, "stream": stream, "data": data},
	}


def _chat_final_frame(run_id, session_key, text="hi"):
	content = [{"type": "text", "text": text}] if text else []
	return {
		"type": "event",
		"event": "chat",
		"payload": {
			"runId": run_id,
			"sessionKey": session_key,
			"state": "final",
			"message": {"content": content},
		},
	}


def _term_text(name: str) -> str:
	return _transcripts.get(name)["terminal"]["text"]


# --------------------------------------------------------------------------- #
# In-process transport double (mocks OpenclawSession at the _recv/_send seam)
# --------------------------------------------------------------------------- #


def _agent_payload(run_id, session_key, op, fr):
	if op == "assistant":
		return {
			"runId": run_id,
			"sessionKey": session_key,
			"stream": "assistant",
			"data": {"text": fr.get("text", ""), "delta": fr.get("delta", "")},
		}
	if op == "tool_start":
		return {
			"runId": run_id,
			"sessionKey": session_key,
			"stream": "item",
			"data": {
				"kind": "tool",
				"phase": "start",
				"name": fr.get("name"),
				"toolCallId": fr.get("call_id"),
				"title": fr.get("title"),
			},
		}
	if op == "tool_end":
		return {
			"runId": run_id,
			"sessionKey": session_key,
			"stream": "item",
			"data": {
				"kind": "tool",
				"phase": "end",
				"name": fr.get("name"),
				"toolCallId": fr.get("call_id"),
				"status": fr.get("status", "completed"),
			},
		}
	if op == "lifecycle_error":
		return {
			"runId": run_id,
			"sessionKey": session_key,
			"stream": "lifecycle",
			"data": {"phase": "error", "error": fr.get("error")},
		}
	return None


def _terminal_frame(run_id, session_key, terminal):
	kind = terminal.get("kind", "final")
	payload = {"runId": run_id, "sessionKey": session_key}
	if kind == "final":
		text = terminal.get("text")
		payload.update(
			{"state": "final", "message": {"content": [{"type": "text", "text": text}] if text else []}}
		)
	elif kind == "failed_final":
		payload.update({"state": "final", "message": {"content": [], "stopReason": "error"}})
	elif kind == "aborted":
		payload.update({"state": "aborted", "errorMessage": "aborted by user"})
	else:
		payload.update(
			{
				"state": terminal.get("state", "error"),
				"errorMessage": terminal.get("errorMessage", "run error"),
			}
		)
	return {"type": "event", "event": "chat", "payload": payload}


class _FakeWs:
	def __init__(self, on_send):
		self._on_send = on_send
		self.connected = True

	def send(self, payload):
		self._on_send(payload)


class _DoubleGateway:
	"""Stands in for a connected ``OpenclawSession``: exposes ``_recv`` (the
	sole read seam the mux owns), ``_lock`` (the REAL serialized-send lock),
	``_ws.send`` (parses the request and plays transcript frames back), and
	``close``. Frames flow through one thread-safe recv queue — so the mux's
	single-reader invariant is exercised end-to-end.
	"""

	def __init__(self):
		self._lock = threading.Lock()
		self._q: queue.Queue = queue.Queue()
		self._ws = _FakeWs(self._on_send)
		self._armed: dict[str, tuple[str, dict]] = {}
		self._sessions_get: dict[str, list] = {}  # session_key -> raw transcript messages
		self._sessions_list: list = []  # rows returned by sessions.list (foreign-usage probe)
		self._hang_sessions_list = False  # CDX-17: when True, sessions.list never responds
		self._closed = threading.Event()
		self._players: list[threading.Thread] = []

	# -- session seam ----------------------------------------------------- #

	def _recv(self, timeout_s: float):
		if timeout_s <= 0:
			return None
		try:
			item = self._q.get(timeout=timeout_s)
		except queue.Empty:
			return None
		if isinstance(item, BaseException):
			raise item
		return item

	def close(self):
		self._closed.set()

	# -- test-side control ------------------------------------------------ #

	def arm(self, run_id: str, transcript: str = "success", **overrides):
		self._armed[run_id] = (transcript, overrides)

	def arm_sessions_get(self, session_key: str, messages: list):
		"""Arm the raw transcript ``sessions.get`` returns for a session key (used by
		the pump's missed-terminal snapshot-recovery tail). Each message may carry an
		``__openclaw.seq`` so the pump's watermark windowing (OARF-2) is exercised."""
		self._sessions_get[session_key] = messages

	def arm_sessions_list(self, rows: list):
		"""Arm the rows ``sessions.list`` returns (the pump's foreign-usage capacity probe)."""
		self._sessions_list = rows

	def arm_sessions_list_hang(self):
		"""CDX-17: make ``sessions.list`` NEVER respond, simulating a stalled control RPC —
		the pump must poll its future without blocking a slice / delta application."""
		self._hang_sessions_list = True

	def stop(self):
		self._closed.set()
		for t in self._players:
			t.join(timeout=2)

	# -- request handling ------------------------------------------------- #

	def _push(self, frame):
		self._q.put(frame)

	def _on_send(self, payload: str):
		frame = json.loads(payload)
		method = frame.get("method")
		rid = frame.get("id")
		params = frame.get("params") or {}
		if method == "chat.send":
			self._handle_chat_send(rid, params)
			return
		if method == "sessions.get":
			key = params.get("key")
			self._push(
				{
					"type": "res",
					"id": rid,
					"ok": True,
					"payload": {"messages": self._sessions_get.get(key, [])},
				}
			)
			return
		if method == "sessions.list":
			if self._hang_sessions_list:
				return  # CDX-17: stalled control RPC — never respond
			self._push({"type": "res", "id": rid, "ok": True, "payload": {"sessions": self._sessions_list}})
			return
		# every other RPC (sessions.patch / sessions.create / chat.abort / ...)
		# gets a plain ok response.
		self._push({"type": "res", "id": rid, "ok": True, "payload": {}})

	def _handle_chat_send(self, rid: str, params: dict):
		run_id = params.get("idempotencyKey")
		session_key = params.get("sessionKey")
		name, overrides = self._armed.get(run_id, ("success", {}))
		transcript = _transcripts.get(name)
		ack_behavior = overrides.get("ack_behavior") or transcript.get("ack_behavior", "normal")

		if ack_behavior == "drop":
			# WS-drop MID-ACK: the socket dies with the ack outstanding. The
			# reader's next _recv raises → Closing fails the pending future.
			self._push(OpenclawUnreachableError("fake ack-drop", code=None))
			return
		if ack_behavior == "timeout":
			# Hold the ack past the caller's window (delivered late; the caller's
			# bounded future fires the ack-timeout sentinel first).
			threading.Timer(
				(transcript.get("ack_hold_s") or 3.0),
				lambda: self._push(
					{"type": "res", "id": rid, "ok": True, "payload": {"runId": run_id, "status": "started"}}
				),
			).start()
			return

		ack = dict(transcript.get("ack") or {"status": "started"})
		ack["runId"] = run_id
		self._push({"type": "res", "id": rid, "ok": True, "payload": ack})
		t = threading.Thread(
			target=self._play, args=(run_id, session_key, transcript, overrides), daemon=True
		)
		t.start()
		self._players.append(t)

	def _play(self, run_id, session_key, transcript, overrides):
		inject = overrides.get("inject") or transcript.get("inject") or {}
		drop_after = inject.get("drop_after_frame")
		frames = transcript.get("frames") or []
		for idx, fr in enumerate(frames):
			if self._closed.is_set():
				return
			op = fr.get("op")
			if op == "pause":
				time.sleep(min(fr.get("ms", 0) / 1000.0, _PAUSE_CAP_S))
				continue
			time.sleep(_CADENCE_S)
			payload = _agent_payload(run_id, session_key, op, fr)
			if payload is not None:
				self._push({"type": "event", "event": "agent", "payload": payload})
			if drop_after is not None and idx >= drop_after:
				self._push(OpenclawUnreachableError("fake mid-stream ws drop", code=None))
				return
		self._push(
			_terminal_frame(
				run_id, session_key, transcript.get("terminal") or {"kind": "final", "text": None}
			)
		)


# --------------------------------------------------------------------------- #
# White-box tests (no transport) — routing, stray counting, breaker
# --------------------------------------------------------------------------- #


class TestRelayMuxWhiteBox(FrappeTestCase):
	def _mux(self, **kw):
		return RelayMux(MagicMock(), "wb-target", **kw)

	def test_fresh_send_pre_registration_routes_early_frames(self):
		"""OAR-15: a run pre-registered under run_id routes frames immediately,
		with NO ack processing needed — the run map is keyed at issuance."""
		mux = self._mux()
		rec = _Recorder()
		mux.register_run("r1", rec.handler(), session_key="s1")  # pre-register, no ack
		mux._classify(_agent_frame("r1", "s1", "assistant", {"text": "Hello", "delta": "Hello"}))
		mux._classify(_agent_frame("r1", "s1", "assistant", {"text": "Hello there", "delta": " there"}))
		mux._classify(_chat_final_frame("r1", "s1", "Hello there"))
		mux.dispatch()
		self.assertEqual([d[0] for d in rec.deltas], [1, 2])
		self.assertEqual(rec.deltas[-1][1], "Hello there")
		self.assertIsNotNone(rec.terminal)
		self.assertEqual(rec.terminal[0], "relay:final")
		self.assertEqual(mux.stats()["stray_frames"], 0)

	def test_late_and_unknown_frames_counted_never_crash(self):
		mux = self._mux()
		mux.register_run("known", _Recorder().handler(), session_key="sk")
		before = mux.stats()["stray_frames"]
		mux._classify(
			_agent_frame("UNKNOWN", "sk", "assistant", {"text": "x", "delta": "x"})
		)  # unknown runId
		mux._classify({"type": "res", "id": "nope", "ok": True, "payload": {}})  # unknown req id
		mux._classify(
			{"type": "event", "event": "agent", "payload": {"stream": "assistant", "data": {}}}
		)  # no runId
		mux._classify({"type": "weird", "blob": 1})  # unknown frame type
		mux._classify(
			{"type": "event", "event": "mystery", "payload": {"runId": "known"}}
		)  # unknown event kind
		self.assertEqual(mux.stats()["stray_frames"], before + 5)
		# a KNOWN-run agent frame still routes (no stray) — proves it never crashed.
		mux._classify(_agent_frame("known", "sk", "assistant", {"text": "ok", "delta": "ok"}))
		self.assertEqual(mux.stats()["stray_frames"], before + 5)
		self.assertEqual(mux.dispatch(), 1)

	def test_poison_rate_circuit_breaker_fires_at_threshold(self):
		fired: list[tuple[str, int]] = []
		mux = self._mux(on_breaker=lambda t, c: fired.append((t, c)), breaker_threshold=2)
		for i in range(3):  # threshold=2 → the 3rd quarantine (count>threshold) opens it
			rec = _Recorder(poison_terminal=True)
			mux.register_run(f"r{i}", rec.handler(), session_key=f"s{i}")
			mux._classify(_chat_final_frame(f"r{i}", f"s{i}"))
			mux.dispatch()
			self.assertEqual(rec.quarantined, "precious_fault")
		self.assertTrue(mux.is_breaker_open())
		self.assertEqual(len(fired), 1)
		self.assertEqual(fired[0][0], "wb-target")
		self.assertGreater(fired[0][1], 2)
		self.assertEqual(mux.stats()["lanes_quarantined"], 3)
		# with the breaker OPEN a RE-ADOPT is refused (stop-readopting signal).
		self.assertIsNone(mux.register_run("readopt", LaneHandler(), is_readopt=True))
		self.assertIsNotNone(mux.register_run("fresh", LaneHandler()))  # fresh send still allowed

	def test_rekey_run_retry_attach(self):
		mux = self._mux()
		rec = _Recorder()
		mux.register_run("idem-key", rec.handler(), session_key="s1")
		self.assertTrue(mux.rekey_run("idem-key", "gw-run-77"))
		mux._classify(_agent_frame("gw-run-77", "s1", "assistant", {"text": "a", "delta": "a"}))
		mux.dispatch()
		self.assertEqual(len(rec.deltas), 1)
		self.assertFalse(mux.rekey_run("idem-key", "x"))  # old key gone

	def test_precious_overflow_quarantines_lane(self):
		"""A wedged consumer whose lane fills with precious frames quarantines
		rather than losing a precious fact (D5 §4 overflow-precious)."""
		from jarvis.chat import relay_mux as rm

		mux = self._mux()
		rec = _Recorder()
		lane = mux.register_run("r", rec.handler(), session_key="s")
		for _ in range(rm.LANE_QUEUE_MAX):
			mux._classify(
				_agent_frame(
					"r", "s", "item", {"kind": "tool", "phase": "start", "name": "t", "toolCallId": "c"}
				)
			)
		self.assertEqual(lane.state, "active")
		mux._classify(
			_agent_frame(
				"r", "s", "item", {"kind": "tool", "phase": "start", "name": "t", "toolCallId": "c2"}
			)
		)
		self.assertEqual(lane.state, "quarantined")
		mux.dispatch()  # delivers the deferred quarantine notification
		self.assertEqual(rec.quarantined, "precious_overflow")

	def test_lossy_overflow_drops_oldest_delta(self):
		from jarvis.chat import relay_mux as rm

		mux = self._mux()
		rec = _Recorder()
		lane = mux.register_run("r", rec.handler(), session_key="s")
		for i in range(rm.LANE_QUEUE_MAX + 5):  # 5 over the bound
			mux._classify(_agent_frame("r", "s", "assistant", {"text": f"t{i}", "delta": f"d{i}"}))
		self.assertEqual(lane.state, "active")  # never quarantines on lossy
		self.assertGreaterEqual(mux.stats()["deltas_dropped"], 5)

	def test_lane_fairness_hot_lane_does_not_starve_cold(self):
		# CDX-13: a backed-up hot lane must not monopolise one dispatch — the per-lane
		# quantum + round-robin service a cold lane within the same bounded call.
		from jarvis.chat import relay_mux as rm

		mux = self._mux()
		hot, cold = _Recorder(), _Recorder()
		mux.register_run("hot", hot.handler(), session_key="sh")
		mux.register_run("cold", cold.handler(), session_key="sc")
		for i in range(rm.LANE_QUANTUM * 3):  # hot backlog >> one quantum
			mux._classify(_agent_frame("hot", "sh", "assistant", {"text": f"h{i}", "delta": f"h{i}"}))
		mux._classify(_agent_frame("cold", "sc", "assistant", {"text": "c", "delta": "c"}))
		# One bounded dispatch: without the quantum this would apply only hot events and
		# never reach the cold lane; the round-robin quantum services cold within budget.
		applied = mux.dispatch(max_events=rm.LANE_QUANTUM + 1)
		self.assertEqual(applied, rm.LANE_QUANTUM + 1)
		self.assertEqual(len(cold.deltas), 1, "cold lane serviced within the budget (not starved)")
		self.assertEqual(len(hot.deltas), rm.LANE_QUANTUM, "hot lane bounded to its per-pass quantum")
		# Residue remains -> the wake is re-armed so the next dispatch continues at once.
		self.assertTrue(mux._has_work())
		self.assertTrue(mux._wake.is_set())

	def test_dispatch_round_robin_drains_all_within_default_budget(self):
		# CDX-13: fairness must not LOSE events — a moderate backlog fully drains via
		# round-robin passes within one default-budget dispatch.
		from jarvis.chat import relay_mux as rm

		mux = self._mux()
		rec = _Recorder()
		mux.register_run("r", rec.handler(), session_key="s")
		total = rm.LANE_QUANTUM * 2 + 5
		for i in range(total):
			mux._classify(_agent_frame("r", "s", "assistant", {"text": f"t{i}", "delta": f"t{i}"}))
		self.assertEqual(mux.dispatch(), total, "all events drained in one call (round-robin passes)")
		self.assertFalse(mux._has_work())


# --------------------------------------------------------------------------- #
# Reader-loop tests (real reader thread + in-process transport double)
# --------------------------------------------------------------------------- #


class TestRelayMuxReaderLoop(FrappeTestCase):
	def setUp(self):
		self._doubles: list[_DoubleGateway] = []
		self._muxes: list[RelayMux] = []
		self._drivers: list[tuple[threading.Event, threading.Thread]] = []

	def tearDown(self):
		for stop, t in self._drivers:
			stop.set()
		for stop, t in self._drivers:
			t.join(timeout=3)
		for mux in self._muxes:
			try:
				mux.stop(timeout=3)
			except Exception:
				pass
		for d in self._doubles:
			try:
				d.stop()
			except Exception:
				pass

	def _start(self, *, drive: bool = True, **kw) -> tuple[_DoubleGateway, RelayMux]:
		d = _DoubleGateway()
		self._doubles.append(d)
		mux = RelayMux(d, "rl-target", **kw).start()
		self._muxes.append(mux)
		if drive:
			self._spawn_driver(mux)
		return d, mux

	def _spawn_driver(self, mux: RelayMux) -> None:
		stop = threading.Event()

		def loop():
			while not stop.is_set():
				try:
					mux.dispatch(block_s=0.05)
				except Exception:
					pass
			try:
				mux.dispatch()
			except Exception:
				pass

		t = threading.Thread(target=loop, name="mux-test-driver", daemon=True)
		t.start()
		self._drivers.append((stop, t))

	# -- tests ---------------------------------------------------------------

	def test_demux_four_concurrent_runs(self):
		d, mux = self._start()
		plan = [
			("r0", "s0", "success"),
			("r1", "s1", "tool-heavy"),
			("r2", "s2", "overflow-compaction"),
			("r3", "s3", "confirmation-card"),
		]
		recs = {}
		for run_id, sk, name in plan:
			d.arm(run_id, name)
			rec = _Recorder()
			recs[run_id] = (rec, name)
			fut = mux.send_chat(sk, "hi", run_id, rec.handler(), timeout_s=10.0)
			ack = fut.result(10.0)
			self.assertEqual((ack.get("payload") or {}).get("runId"), run_id)
		for run_id, (rec, name) in recs.items():
			self.assertTrue(rec.done.wait(20), f"{run_id} ({name}) did not complete")
			self.assertEqual(rec.terminal[0], "relay:final", run_id)
			self.assertEqual(rec.terminal[1].get("text"), _term_text(name), run_id)  # right lane's own answer
			seqs = [dd[0] for dd in rec.deltas]
			self.assertTrue(
				all(b > a for a, b in itertools.pairwise(seqs)), run_id
			)  # per-lane order preserved
			self.assertEqual(rec.deltas[-1][1], _term_text(name), run_id)
		self.assertTrue(len(recs["r1"][0].tools) >= 6)  # tool-heavy exercised precious tool path
		self.assertEqual(mux.stats()["stray_frames"], 0)

	def test_rpc_resolved_mid_stream_without_stealing_frames(self):
		d, mux = self._start()
		d.arm("run-a", "success")
		rec = _Recorder()
		send_fut = mux.send_chat("sess-a", "hi", "run-a", rec.handler(), timeout_s=10.0)
		self.assertEqual((send_fut.result(10.0).get("payload") or {}).get("runId"), "run-a")
		patch_fut = mux.set_session_model("sess-a", "acme/model-x", timeout_s=10.0)  # RPC mid-stream
		self.assertTrue(patch_fut.result(10.0).get("ok"))
		self.assertTrue(rec.done.wait(20), "run did not complete after mid-stream RPC")
		self.assertEqual(rec.terminal[0], "relay:final")
		self.assertEqual(rec.terminal[1].get("text"), _term_text("success"))  # RPC stole nothing
		self.assertEqual(mux.stats()["stray_frames"], 0)

	def test_fresh_send_streams_without_awaiting_ack(self):
		"""OAR-15 end-to-end: pre-registration routes the whole stream even if
		the caller never consumes the ack future."""
		d, mux = self._start()
		d.arm("run-x", "success")
		rec = _Recorder()
		mux.send_chat("sess-x", "hi", "run-x", rec.handler(), timeout_s=10.0)  # ack ignored
		self.assertTrue(rec.done.wait(20), "stream did not route without ack processing")
		self.assertEqual(rec.terminal[1].get("text"), _term_text("success"))
		self.assertEqual(mux.stats()["stray_frames"], 0)

	def test_poison_delta_drops_and_continues(self):
		d, mux = self._start()
		d.arm("run-pd", "success")
		rec = _Recorder(poison_delta_seq=3)  # the 3rd delta callback raises
		mux.send_chat("sess-pd", "hi", "run-pd", rec.handler(), timeout_s=10.0).result(10.0)
		self.assertTrue(rec.done.wait(20), "turn did not complete after poison delta")
		self.assertEqual(rec.terminal[0], "relay:final")  # lane CONTINUED and completed
		self.assertNotIn(3, [dd[0] for dd in rec.deltas])  # seq 3 dropped
		self.assertGreaterEqual(mux.stats()["deltas_dropped"], 1)
		self.assertEqual(mux.stats()["lanes_quarantined"], 0)  # a lossy fault never quarantines

	def test_poison_terminal_quarantines_one_lane_neighbour_finishes(self):
		d, mux = self._start()
		d.arm("run-bad", "success")
		d.arm("run-good", "tool-heavy")
		bad = _Recorder(poison_terminal=True)
		good = _Recorder()
		mux.send_chat("sess-bad", "hi", "run-bad", bad.handler(), timeout_s=10.0).result(10.0)
		mux.send_chat("sess-good", "hi", "run-good", good.handler(), timeout_s=10.0).result(10.0)
		self.assertTrue(bad.done.wait(20))
		self.assertTrue(good.done.wait(20))
		self.assertEqual(bad.quarantined, "precious_fault")  # ONE lane fenced off
		self.assertIsNone(bad.terminal)
		self.assertEqual(good.terminal[0], "relay:final")  # neighbour finished
		self.assertEqual(good.terminal[1].get("text"), _term_text("tool-heavy"))
		self.assertEqual(mux.stats()["lanes_quarantined"], 1)

	def test_ws_drop_mid_ack_fails_pending_future_immediately(self):
		"""OAR-10: a WS drop while a chat.send ack is outstanding fails the future
		immediately with the transport sentinel — no full-timeout dead-wait."""
		d, mux = self._start()
		d.arm("run-drop", "success", ack_behavior="drop")
		rec = _Recorder()
		fut = mux.send_chat("sess-drop", "hi", "run-drop", rec.handler(), timeout_s=30.0)
		t0 = time.monotonic()
		with self.assertRaises(OpenclawUnreachableError) as cm:
			fut.result(30.0)
		self.assertLess(time.monotonic() - t0, 5.0, "future dead-waited instead of failing on Closing")
		self.assertEqual(cm.exception.code, "ack-timeout")
		self.assertTrue(rec.done.wait(5))
		self.assertEqual(rec.closing, "transport")  # lane notified of transport loss
		self.assertTrue(mux.is_closed())

	def test_ws_drop_mid_stream_closes_notifies_and_rebuilds(self):
		d, mux = self._start()
		d.arm("run-ws", "ws-drop")  # closes the socket mid-stream
		rec = _Recorder()
		mux.send_chat("sess-ws", "hi", "run-ws", rec.handler(), timeout_s=10.0).result(10.0)
		self.assertTrue(rec.done.wait(20), "lane not notified on mid-stream drop")
		self.assertEqual(rec.closing, "transport")
		self.assertTrue(mux.is_closed())
		# REBUILD (the pump's job, simulated): a fresh transport + mux re-attaches
		# from caller-provided lane specs and streams again on the same target.
		d2, mux2 = self._start()
		d2.arm("run-rebuilt", "success")
		rec2 = _Recorder()
		mux2.send_chat("sess-ws", "hi", "run-rebuilt", rec2.handler(), timeout_s=10.0).result(10.0)
		self.assertTrue(rec2.done.wait(20), "rebuilt transport did not stream")
		self.assertEqual(rec2.terminal[0], "relay:final")
		self.assertEqual(rec2.terminal[1].get("text"), _term_text("success"))

	def test_ack_timeout_sentinel_path(self):
		"""A held ack past the caller's window resolves the future with the
		ack-timeout sentinel (park-for-recovery), not a false error."""
		d, mux = self._start()
		d.arm("run-to", "ack-timeout")  # gateway holds the ack
		rec = _Recorder()
		fut = mux.send_chat("sess-to", "hi", "run-to", rec.handler(), timeout_s=0.4)
		t0 = time.monotonic()
		with self.assertRaises(OpenclawUnreachableError) as cm:
			fut.result()  # the future's own 0.4s bound
		self.assertEqual(cm.exception.code, "ack-timeout")
		self.assertLess(time.monotonic() - t0, 2.0)

	def test_circuit_breaker_fires_over_transport(self):
		fired: list[tuple[str, int]] = []
		d, mux = self._start(on_breaker=lambda t, c: fired.append((t, c)), breaker_threshold=2)
		recs = []
		for i in range(3):
			d.arm(f"run-p{i}", "success")
			rec = _Recorder(poison_terminal=True)
			recs.append(rec)
			mux.send_chat(f"sess-p{i}", "hi", f"run-p{i}", rec.handler(), timeout_s=10.0).result(10.0)
		for rec in recs:
			self.assertTrue(rec.done.wait(20))
			self.assertEqual(rec.quarantined, "precious_fault")
		self.assertTrue(mux.is_breaker_open())
		self.assertEqual(len(fired), 1)

	def test_abort_rpc_issued_and_resolved(self):
		d, mux = self._start()
		d.arm("run-ab", "abort")
		rec = _Recorder()
		mux.send_chat("sess-ab", "hi", "run-ab", rec.handler(), timeout_s=10.0).result(10.0)
		# the mux issues the abort RPC when asked; authority is out-of-band.
		self.assertTrue(mux.abort("sess-ab", "run-ab", timeout_s=10.0).result(10.0).get("ok"))
		self.assertTrue(rec.done.wait(20))
		# the abort transcript ends aborted → relay:error terminal.
		self.assertEqual(rec.terminal[0], "relay:error")
		self.assertEqual(rec.terminal[1].get("state"), "aborted")
