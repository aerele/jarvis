"""FakeGateway — a local WS server speaking the openclaw protocol subset the
bench uses, with deterministic transcript playback and configurable faults.

It is a REAL ``websockets`` server on ``127.0.0.1:<port>`` (reachable by the
real ``jarvis.chat.openclaw_client.OpenclawSession`` over a real socket), so
the whole bench transport + relay path is exercised — no frame mocking.

Protocol subset (grounded in spike S2 + the transport client):

  handshake        connect.challenge event -> connect req -> hello-ok res
  sessions.create  -> {"key": ...}
  sessions.patch   -> ok            (per-conversation model override)
  sessions.get     -> {"messages": [...]}   (seq watermark / recovery tail)
  chat.history     -> {"messages": [...]}   (recovery snapshot)
  sessions.list    -> {"sessions": [{"key","hasActiveRun"}...]}
  sessions.messages.subscribe -> ok
  chat.send        -> ACK {"runId","status"} BEFORE lane admission (S2 (a)),
                      then broadcast agent frames + a terminal chat event
  chat.abort       -> ok, and the in-flight run terminates "aborted"

Lane semantics (S2 (a)+(d)): the ack fires at ACCEPT time, before the run is
enqueued. Playback then goes per-session FIFO -> a global ``main`` lane capped
at ``max_concurrent`` (default 4). The gap between ack and the first streamed
frame is the C4 dwell proxy; it grows when > max_concurrent runs contend.

Config knobs (per gateway, overridable per-run via ``arm``):
  cadence_ms          spacing between streamed frames (token cadence)
  ack_delay_ms        normal ack latency
  ack_timeout_hold_ms hold before responding when ack_behavior == "timeout"
  max_concurrent      global main-lane cap (openclaw default 4)
  lane_sim            enable the FIFO->lane admission gating (dwell)
  lane_dwell_ms       extra artificial dwell added while holding a lane slot
                      (simulate a busy container even below the cap)

Everything is deterministic given the same transcripts, cadence and arm order.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass, field

import websockets

from jarvis.tests.harness import transcripts as _transcripts


@dataclass
class RunTimeline:
	run_id: str
	session_key: str = ""
	transcript: str = ""
	ack_ts: float | None = None  # monotonic; ack sent (accept time)
	lane_admit_ts: float | None = None  # monotonic; admitted to main lane
	first_frame_ts: float | None = None  # monotonic; first streamed frame
	terminal_ts: float | None = None
	terminal_kind: str = ""
	frames_sent: int = 0
	aborted: bool = False

	def dwell_ms(self) -> float | None:
		if self.ack_ts is None or self.lane_admit_ts is None:
			return None
		return (self.lane_admit_ts - self.ack_ts) * 1000.0

	def ack_to_first_frame_ms(self) -> float | None:
		if self.ack_ts is None or self.first_frame_ts is None:
			return None
		return (self.first_frame_ts - self.ack_ts) * 1000.0


class FakeGateway:
	def __init__(
		self,
		*,
		cadence_ms: float = 25.0,
		ack_delay_ms: float = 2.0,
		ack_timeout_hold_ms: float = 2000.0,
		max_concurrent: int = 4,
		lane_sim: bool = True,
		lane_dwell_ms: float = 0.0,
		host: str = "127.0.0.1",
	):
		self.cadence_ms = cadence_ms
		self.ack_delay_ms = ack_delay_ms
		self.ack_timeout_hold_ms = ack_timeout_hold_ms
		self.max_concurrent = max_concurrent
		self.lane_sim = lane_sim
		self.lane_dwell_ms = lane_dwell_ms
		self.host = host

		self.port: int | None = None
		self._loop: asyncio.AbstractEventLoop | None = None
		self._thread: threading.Thread | None = None
		self._ready = threading.Event()
		self._stop = None  # asyncio.Future set in loop

		# cross-thread state
		self._state_lock = threading.Lock()
		self._armed: dict[str, dict] = {}  # run_id -> {transcript, overrides}
		self._timelines: dict[str, RunTimeline] = {}
		self._sessions: dict[str, dict] = {}  # session_key -> {has_active_run}

		# in-loop lane state (created lazily inside the loop)
		self._lane_sema: asyncio.Semaphore | None = None
		self._session_locks: dict[str, asyncio.Lock] = {}
		self._abort_events: dict[str, asyncio.Event] = {}
		self._active_main = 0  # observed concurrent lane occupancy (for assertions)
		self._max_observed_main = 0

	# ---- lifecycle --------------------------------------------------------

	@property
	def ws_url(self) -> str:
		return f"ws://{self.host}:{self.port}"

	def start(self, timeout: float = 5.0) -> "FakeGateway":
		self._thread = threading.Thread(target=self._run, name="fake-gateway", daemon=True)
		self._thread.start()
		if not self._ready.wait(timeout):
			raise RuntimeError("FakeGateway failed to start")
		return self

	def stop(self) -> None:
		if self._loop and self._stop and not self._stop.done():
			self._loop.call_soon_threadsafe(self._stop.set_result, True)
		if self._thread:
			self._thread.join(timeout=5)

	def _run(self) -> None:
		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)
		self._loop = loop
		loop.run_until_complete(self._serve())

	async def _serve(self) -> None:
		self._lane_sema = asyncio.Semaphore(self.max_concurrent)
		self._stop = self._loop.create_future()
		server = await websockets.serve(self._handler, self.host, 0, max_size=8 * 1024 * 1024)
		self.port = server.sockets[0].getsockname()[1]
		self._ready.set()
		try:
			await self._stop
		finally:
			server.close()
			await server.wait_closed()

	async def _handler(self, ws):
		send_lock = asyncio.Lock()

		async def send_json(obj: dict) -> None:
			async with send_lock:
				await ws.send(json.dumps(obj))

		# 1. challenge
		await send_json(
			{"type": "event", "event": "connect.challenge", "payload": {"nonce": uuid.uuid4().hex}}
		)

		try:
			async for raw in ws:
				try:
					frame = json.loads(raw)
				except json.JSONDecodeError:
					continue
				if frame.get("type") != "req":
					continue
				await self._dispatch(ws, send_json, frame)
		except websockets.ConnectionClosed:
			return

	async def _dispatch(self, ws, send_json, frame: dict) -> None:
		method = frame.get("method")
		rid = frame.get("id")
		params = frame.get("params") or {}

		if method == "connect":
			await send_json({"type": "res", "id": rid, "ok": True, "payload": {}})
			return
		if method == "sessions.create":
			key = f"sess-{uuid.uuid4().hex[:12]}"
			with self._state_lock:
				self._sessions[key] = {"has_active_run": False}
			await send_json({"type": "res", "id": rid, "ok": True, "payload": {"key": key}})
			return
		if method in ("sessions.patch", "sessions.messages.subscribe", "sessions.delete"):
			await send_json({"type": "res", "id": rid, "ok": True, "payload": {}})
			return
		if method in ("sessions.get", "chat.history"):
			key = params.get("key") or params.get("sessionKey")
			await send_json(
				{"type": "res", "id": rid, "ok": True, "payload": {"messages": self._history_for(key)}}
			)
			return
		if method == "sessions.list":
			with self._state_lock:
				rows = [{"key": k, "hasActiveRun": v["has_active_run"]} for k, v in self._sessions.items()]
			await send_json({"type": "res", "id": rid, "ok": True, "payload": {"sessions": rows}})
			return
		if method == "chat.abort":
			sk = params.get("sessionKey")
			run_id = params.get("runId")
			self._signal_abort(sk, run_id)
			await send_json({"type": "res", "id": rid, "ok": True, "payload": {}})
			return
		if method == "chat.send":
			await self._handle_chat_send(ws, send_json, rid, params)
			return
		# default ok for anything unmodelled
		await send_json({"type": "res", "id": rid, "ok": True, "payload": {}})

	# ---- chat.send: ack (pre-lane) then playback --------------------------

	async def _handle_chat_send(self, ws, send_json, rid: str, params: dict) -> None:
		run_id = params.get("idempotencyKey")
		session_key = params.get("sessionKey")
		armed = self._get_armed(run_id)
		tname = armed["transcript"]
		transcript = _transcripts.get(tname)
		overrides = armed.get("overrides") or {}
		ack_behavior = overrides.get("ack_behavior") or transcript.get("ack_behavior", "normal")

		tl = RunTimeline(run_id=run_id, session_key=session_key, transcript=tname)
		with self._state_lock:
			self._timelines[run_id] = tl
			if session_key in self._sessions:
				self._sessions[session_key]["has_active_run"] = True

		if ack_behavior == "drop":
			# WS-drop MID-ACK: close the socket on receiving chat.send WITHOUT
			# acking. The client's pending chat.send future must fail immediately
			# via the mux Closing path (fail-all-on-Closing, OAR-10), not
			# dead-wait its own ack timeout. Added for the WP-1b mux tests.
			tl.terminal_kind = "ack-drop"
			tl.terminal_ts = time.monotonic()
			self._clear_active(session_key)
			try:
				await ws.close(code=1011, reason="fake-gateway ack-drop")
			except websockets.ConnectionClosed:
				pass
			return

		if ack_behavior == "timeout":
			# Hold the ack past the client window; the bench parks. Still finish
			# server-side (the transcript "completed" but no one is listening on
			# the ack — the run would be recoverable from the durable tail).
			await asyncio.sleep(self.ack_timeout_hold_ms / 1000.0)

		ack_payload = dict(transcript.get("ack") or {"status": "started"})
		ack_payload["runId"] = run_id
		tl.ack_ts = time.monotonic()
		try:
			await send_json({"type": "res", "id": rid, "ok": True, "payload": ack_payload})
		except websockets.ConnectionClosed:
			return

		if ack_behavior == "timeout":
			# The client already gave up; mark terminal for bookkeeping, no stream.
			tl.terminal_kind = "ack-timeout(server-late-ack)"
			tl.terminal_ts = time.monotonic()
			self._clear_active(session_key)
			return

		# Playback runs as its own task so the read loop keeps serving
		# chat.abort while the stream is in flight.
		self._loop.create_task(self._playback(ws, send_json, tl, transcript, overrides))

	async def _playback(self, ws, send_json, tl: RunTimeline, transcript: dict, overrides: dict) -> None:
		session_key = tl.session_key
		run_id = tl.run_id
		cadence = overrides.get("cadence_ms", self.cadence_ms) / 1000.0
		lane_dwell = overrides.get("lane_dwell_ms", self.lane_dwell_ms) / 1000.0
		abort_ev = self._get_abort_event(run_id)

		# per-session FIFO -> global main lane (S2). Ack already fired above.
		sess_lock = self._get_session_lock(session_key)
		try:
			if self.lane_sim:
				async with sess_lock:
					async with self._lane_sema:
						self._enter_main()
						tl.lane_admit_ts = time.monotonic()
						if lane_dwell:
							await asyncio.sleep(lane_dwell)
						await self._stream_frames(ws, send_json, tl, transcript, overrides, cadence, abort_ev)
						self._exit_main()
			else:
				tl.lane_admit_ts = time.monotonic()
				await self._stream_frames(ws, send_json, tl, transcript, overrides, cadence, abort_ev)
		except websockets.ConnectionClosed:
			tl.terminal_kind = tl.terminal_kind or "ws-closed"
			tl.terminal_ts = time.monotonic()
		finally:
			self._clear_active(session_key)

	async def _stream_frames(self, ws, send_json, tl, transcript, overrides, cadence, abort_ev) -> None:
		run_id = tl.run_id
		inject = overrides.get("inject") or transcript.get("inject") or {}
		drop_after = inject.get("drop_after_frame")
		frames = transcript.get("frames") or []

		for idx, fr in enumerate(frames):
			if abort_ev.is_set():
				await self._emit_terminal(send_json, tl, {"kind": "aborted"})
				return
			op = fr.get("op")
			if op == "pause":
				await asyncio.sleep(fr.get("ms", 0) / 1000.0)
				continue

			await asyncio.sleep(cadence)
			payload = self._frame_payload(run_id, op, fr)
			if payload is None:
				continue
			if tl.first_frame_ts is None:
				tl.first_frame_ts = time.monotonic()
			await send_json({"type": "event", "event": "agent", "payload": payload})
			tl.frames_sent += 1

			if drop_after is not None and idx >= drop_after:
				# WS-drop injection: close the socket mid-stream.
				tl.terminal_kind = "ws-drop"
				tl.terminal_ts = time.monotonic()
				await ws.close(code=1011, reason="fake-gateway injected drop")
				return

		# normal terminal
		if abort_ev.is_set():
			await self._emit_terminal(send_json, tl, {"kind": "aborted"})
			return
		await self._emit_terminal(
			send_json, tl, transcript.get("terminal") or {"kind": "final", "text": None}
		)

	def _frame_payload(self, run_id: str, op: str, fr: dict) -> dict | None:
		if op == "assistant":
			return {
				"runId": run_id,
				"stream": "assistant",
				"data": {"text": fr.get("text", ""), "delta": fr.get("delta", "")},
			}
		if op == "tool_start":
			return {
				"runId": run_id,
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
				"stream": "lifecycle",
				"data": {"phase": "error", "error": fr.get("error")},
			}
		return None

	async def _emit_terminal(self, send_json, tl: RunTimeline, terminal: dict) -> None:
		kind = terminal.get("kind", "final")
		run_id = tl.run_id
		session_key = tl.session_key
		payload = {"runId": run_id, "sessionKey": session_key}
		if kind == "final":
			text = terminal.get("text")
			content = [{"type": "text", "text": text}] if text else []
			payload.update({"state": "final", "message": {"content": content}})
		elif kind == "failed_final":
			payload.update({"state": "final", "message": {"content": [], "stopReason": "error"}})
		elif kind == "aborted":
			tl.aborted = True
			payload.update({"state": "aborted", "errorMessage": "aborted by user"})
		else:  # error
			payload.update(
				{
					"state": terminal.get("state", "error"),
					"errorMessage": terminal.get("errorMessage", "run error"),
				}
			)
		tl.terminal_kind = kind
		tl.terminal_ts = time.monotonic()
		try:
			await send_json({"type": "event", "event": "chat", "payload": payload})
		except websockets.ConnectionClosed:
			pass

	# ---- helpers ----------------------------------------------------------

	def _history_for(self, session_key: str | None) -> list:
		# Used for the pre-send seq watermark (empty tail => watermark 0) and,
		# for the "recovered" fixture, the post-drop recovery snapshot: if a run
		# on this session was armed with inject.recover_via == "history", the
		# durable transcript still holds the complete answer, so surface it as a
		# role=assistant tail message the way sessions.get / chat.history would
		# (openclaw stamps __openclaw:{seq,id}). Stage-B recovery probes read it.
		if not session_key:
			return []
		with self._state_lock:
			runs = [tl for tl in self._timelines.values() if tl.session_key == session_key]
			armed = dict(self._armed)
		seq = 1
		tail: list = []
		for tl in runs:
			inject = (armed.get(tl.run_id) or {}).get("overrides", {}).get("inject")
			if not inject:
				inject = (
					_transcripts.get(tl.transcript).get("inject")
					if tl.transcript in _transcripts.TRANSCRIPTS
					else None
				)
			if inject and inject.get("recover_via") == "history" and inject.get("final_text"):
				tail.append(
					{
						"role": "assistant",
						"content": inject["final_text"],
						"__openclaw": {"seq": seq, "id": f"rec-{tl.run_id}"},
					}
				)
				seq += 1
		return tail

	def _get_armed(self, run_id: str) -> dict:
		with self._state_lock:
			return self._armed.get(run_id) or {"transcript": "success", "overrides": {}}

	def _get_abort_event(self, run_id: str) -> asyncio.Event:
		ev = self._abort_events.get(run_id)
		if ev is None:
			ev = asyncio.Event()
			self._abort_events[run_id] = ev
		return ev

	def _get_session_lock(self, session_key: str) -> asyncio.Lock:
		lock = self._session_locks.get(session_key)
		if lock is None:
			lock = asyncio.Lock()
			self._session_locks[session_key] = lock
		return lock

	def _signal_abort(self, session_key: str | None, run_id: str | None) -> None:
		# Called from the read loop (in-loop). Set the abort event(s).
		if run_id and run_id in self._abort_events:
			self._abort_events[run_id].set()
			return
		# abort-all-on-session: signal every armed run on this session
		for rid, tl in list(self._timelines.items()):
			if tl.session_key == session_key and tl.terminal_ts is None:
				self._get_abort_event(rid).set()

	def _enter_main(self) -> None:
		self._active_main += 1
		self._max_observed_main = max(self._max_observed_main, self._active_main)

	def _exit_main(self) -> None:
		self._active_main -= 1

	def _clear_active(self, session_key: str | None) -> None:
		with self._state_lock:
			if session_key in self._sessions:
				self._sessions[session_key]["has_active_run"] = False

	# ---- public arm / introspection (thread-safe) -------------------------

	def arm(self, run_id: str, transcript: str = "success", **overrides) -> None:
		"""Register the transcript (and per-run overrides) to play for run_id."""
		with self._state_lock:
			self._armed[run_id] = {"transcript": transcript, "overrides": overrides}

	def timeline(self, run_id: str) -> RunTimeline | None:
		with self._state_lock:
			return self._timelines.get(run_id)

	def max_observed_main(self) -> int:
		return self._max_observed_main
