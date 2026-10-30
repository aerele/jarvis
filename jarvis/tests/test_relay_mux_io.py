"""#773: the relay multiplexer's write path on a real socket.

``RelayMux`` owns one ``AgentSession`` per shard. Its reader thread reads in 0.5s
slices while the pump thread writes every RPC on the same socket. These tests run
the real mux over a real ``AgentSession`` on a local socket pair (plain and TLS)
and check what the pump depends on: a large write after a quiet read arrives
byte-exact, a send that never started says so and leaves no lane or pending entry,
and a send that started but could not finish is reported with the ambiguous
``ack-timeout`` code, poisons the transport and never claims the frame was lost.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from jarvis.chat import relay_mux
from jarvis.chat.relay_mux import LaneHandler, RelayMux
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests import _tls_pair as tp
from jarvis.tests.test_ws_io_gate import MB, _SocketCase, _wait


def _handler() -> LaneHandler:
	return LaneHandler(on_delta=lambda *a, **k: None, on_tool=lambda *a: None, on_terminal=lambda *a: None)


class _MuxCase(_SocketCase):
	def setUp(self):
		super().setUp()
		self._muxes: list[RelayMux] = []

	def tearDown(self):
		for mux in self._muxes:
			try:
				mux.stop(timeout=3)
			except Exception:
				pass
		super().tearDown()

	def mux(self, mode: str, **kw):
		sess, peer, rel = self.session(mode, **kw)
		mux = RelayMux(sess, "773-io").start()
		self._muxes.append(mux)
		return mux, sess, peer, rel


class TestMuxLargeWriteAfterQuietRead(_MuxCase):
	"""AC-T1 on the mux: with the reader alive and the inbound side quiet, a 2 MiB
	``chat.send`` to a 1.5 MB/s peer must be delivered and acked. Before #773 it
	failed after ~0.57s with ``chat.send send failed: timed out`` and code None
	(D/transport-before-probe.json)."""

	def _run(self, mode: str) -> None:
		mux, _sess, peer, _ = self.mux(mode, rate=1.5 * MB, on_message=tp.rpc_responder(chat_final=False))
		time.sleep(0.6)  # the reader has finished at least one quiet slice
		message = "D" * (2 * MB)
		t0 = time.monotonic()
		try:
			fut = mux.send_chat("sess-1", message, "run-big", _handler(), timeout_s=15)
		except AgentUnreachableError as e:
			self.fail(f"{mode}: send failed after {time.monotonic() - t0:.2f}s, code={e.code}: {e}")
		ack = fut.result(15)
		self.assertTrue(ack.get("ok"))
		self.assertEqual(tp.sha(tp.chat_sends(peer)[0]["params"]["message"]), tp.sha(message))

	def test_plain(self):
		self._run("plain")

	def test_tls13(self):
		self._run("tls13")


class TestMuxSendNotStarted(_MuxCase):
	"""AC-M1: the send lock held by another writer for longer than the gate wait.
	Nothing of this frame reaches the socket, so the error is DEFINITE
	(``send-not-started``), the pending map and the lane are clean, and the transport
	stays healthy for the next write."""

	def test_lock_held_past_the_wait_is_not_started_and_leaves_no_ghosts(self):
		mux, sess, peer, _ = self.mux("tls13", on_message=tp.rpc_responder(chat_final=False))
		held = threading.Event()

		def hold_lock():
			with sess._lock:
				held.set()
				time.sleep(6.5)

		self.thread(hold_lock)
		self.assertTrue(held.wait(2))
		t0 = time.monotonic()
		raised = None
		try:
			mux.send_chat("sess-1", "hello", "run-wait", _handler(), timeout_s=10)
		except AgentUnreachableError as e:
			raised = e
		elapsed = time.monotonic() - t0
		self.assertIsNotNone(raised, f"no error: the send waited {elapsed:.2f}s for the lock and went out")
		self.assertEqual(raised.code, "send-not-started")
		self.assertLess(elapsed, 5.6, "the wait is bounded by one 5s budget")
		self.assertNotIn("run-wait", mux._runs, "ghost lane left behind")
		self.assertEqual(mux._pending, {}, "ghost pending RPC left behind")
		self.assertFalse(mux.is_closed(), "a send that never started must not poison the transport")
		self.assertEqual(tp.chat_sends(peer), [], "no byte of the refused frame may reach the peer")
		# The lock holder finishes; the next write goes out normally.
		self._threads[-1].join(10)
		fut = mux.issue_rpc("sessions.list", {}, timeout_s=5)
		self.assertTrue(fut.result(5).get("ok"))


class TestMuxSendUncertain(_MuxCase):
	"""AC-M2: a peer that stops reading mid-frame. The write started, so the
	outcome is AMBIGUOUS: the error carries the existing ``ack-timeout`` sentinel,
	the transport is poisoned at once, the lane and pending entry are removed, the
	next write is refused without touching the socket, and every other RPC already
	written fails promptly with the same ambiguous code (never a definite one)."""

	def test_stalled_peer_is_uncertain_poisons_and_fails_other_pending(self):
		mux, _sess, peer, _ = self.mux("tls13", bufsize=65536)
		peer.paused.set()  # the peer stops reading: nothing will drain the socket
		other = mux.issue_rpc("sessions.list", {}, timeout_s=30)
		t0 = time.monotonic()
		raised = None
		with patch.object(relay_mux, "IO_WRITE_TIMEOUT_S", 1.5, create=True):
			try:
				mux.send_chat("sess-1", "E" * (4 * MB), "run-stall", _handler(), timeout_s=30)
			except AgentUnreachableError as e:
				raised = e
		elapsed = time.monotonic() - t0
		self.assertIsNotNone(raised, "a stalled write must fail")
		self.assertEqual(
			raised.code,
			"ack-timeout",
			f"write-started failure must be ambiguous, got {raised.code!r}: {raised}",
		)
		self.assertGreater(elapsed, 1.2, "the write had its whole budget, not the reader's 0.5s slice")
		self.assertTrue(mux.is_closed(), "the transport is poisoned immediately")
		self.assertNotIn("run-stall", mux._runs, "ghost lane left behind")
		self.assertNotIn("run-stall", [f.method for f in mux._pending.values()])
		t1 = time.monotonic()
		with self.assertRaises(AgentUnreachableError) as cm:
			mux.issue_rpc("chat.abort", {"sessionKey": "sess-1"}, timeout_s=5)
		self.assertEqual(cm.exception.code, "send-not-started")
		# Instant, not a wait: a reintroduced 0.5s read hold would still fail this.
		self.assertLess(time.monotonic() - t1, 0.25)
		t2 = time.monotonic()
		with self.assertRaises(AgentUnreachableError) as cm:
			other.result(10)
		self.assertEqual(cm.exception.code, "ack-timeout")
		self.assertLess(
			time.monotonic() - t2, 3.0, "other written RPCs fail promptly, not at their own timeout"
		)


class TestMuxGracefulStop(_MuxCase):
	"""stop() is a graceful teardown: no lane is told the transport was lost, every
	in-flight RPC is cancelled, and the socket is closed once."""

	def test_stop_cancels_pending_without_on_closing(self):
		mux, _sess, peer, _ = self.mux("tls13")
		closings = []
		handler = LaneHandler(
			on_delta=lambda *a, **k: None,
			on_tool=lambda *a: None,
			on_terminal=lambda *a: None,
			on_closing=closings.append,
		)
		mux.register_run("run-live", handler, session_key="sess-1")
		fut = mux.issue_rpc("sessions.list", {}, timeout_s=30)
		self.assertTrue(_wait(lambda: peer.texts(), 5))
		mux.stop(timeout=3)
		with self.assertRaises(relay_mux.RpcCancelled):
			fut.result(1)
		self.assertEqual(closings, [])
		self.assertTrue(_wait(lambda: peer.closes >= 1 or peer.eof, 5), "the peer saw the close")
		mux.stop(timeout=3)  # idempotent


class TestMuxIdleThroughput(_MuxCase):
	"""AC-K2a: with the reader alive and NOTHING arriving, small RPCs (the pump's
	control writes and short chat sends, each after ~20ms of CAS/commit/publish)
	must not wait behind an idle read. Every write <= 0.10s, 50 of them <= 6s."""

	def _run(self, mode):
		mux, _sess, peer, _ = self.mux(mode)  # the peer never answers: inbound stays quiet
		time.sleep(0.6)
		durations = []
		t_all = time.monotonic()
		for i in range(50):
			time.sleep(0.02)
			t0 = time.monotonic()
			mux.issue_rpc("chat.abort", {"sessionKey": f"s{i}"}, timeout_s=30)
			durations.append(time.monotonic() - t0)
		total = time.monotonic() - t_all
		self.assertLessEqual(max(durations), 0.10, f"{mode}: slowest write {max(durations):.3f}s")
		self.assertLessEqual(total, 6.0)
		self.assertTrue(mux._reader.is_alive())
		self.assertTrue(_wait(lambda: len(peer.texts()) == 50, 5))

	def test_plain(self):
		self._run("plain")

	def test_tls13(self):
		self._run("tls13")


class TestMuxFairness(_MuxCase):
	"""AC-K2b: back-to-back 250 KB writes while the peer PINGs every 0.2s. A reader
	with data gets a turn between writes: every PING is answered within the longest
	write plus 0.15s, and no write follows a write while a ready reader waited."""

	def test_tls13(self):
		from jarvis.tests.test_ws_io_gate import _OpLog

		mux, sess, peer, _ = self.mux("tls13", rate=5 * MB, ping_every=0.2)
		log = _OpLog(sess._io)
		for i in range(20):
			mux.issue_rpc(
				"chat.send", {"sessionKey": "s", "message": "g" * 250_000, "idempotencyKey": f"r{i}"}
			)
		self.assertTrue(_wait(lambda: len(tp.chat_sends(peer)) == 20, 10))
		time.sleep(0.5)
		longest = max(log.durations("write"))
		pings = list(peer.pings)
		self.assertGreaterEqual(len(pings), 4)
		for p in pings[:-1]:
			answered = [q for q in peer.pongs if q >= p]
			self.assertTrue(answered, "a PING was never answered")
			self.assertLessEqual(
				answered[0] - p, longest + 0.15, f"PONG {answered[0] - p:.3f}s, longest write {longest:.3f}s"
			)
		for prev, op in zip(log.ops, log.ops[1:], strict=False):
			if prev["kind"] == op["kind"] == "write":
				self.assertEqual(op["readers"], 0, "a write followed a write while a ready reader waited")


class TestMuxWriteDeadline(_MuxCase):
	"""AC-M3: the pump's lease deadline on the mux. A write that could not finish by
	then is refused before any byte goes out, both at issuance and (exactly) once
	the writer holds the socket; the transport stays healthy."""

	def test_refused_at_issuance(self):
		mux, _sess, peer, _ = self.mux("tls13")
		mux.set_write_deadline(time.monotonic() + 1.0)
		with self.assertRaises(AgentUnreachableError) as cm:
			mux.issue_rpc("chat.abort", {"sessionKey": "s"}, timeout_s=5)
		self.assertEqual(cm.exception.code, "send-not-started")
		self.assertEqual(mux._pending, {})
		self.assertFalse(mux.is_closed())
		mux.set_write_deadline(None)
		mux.issue_rpc("chat.abort", {"sessionKey": "s"}, timeout_s=5)
		self.assertTrue(_wait(lambda: len(peer.texts()) == 1, 3))
		self.assertEqual(peer.texts()[0]["method"], "chat.abort")

	def test_refused_inside_the_gate(self):
		from jarvis.chat.agent_client import SendNotStarted

		_mux, sess, peer, _ = self.mux("tls13")
		with self.assertRaises(SendNotStarted):
			sess._write_frame('{"type":"req"}', write_s=10.0, not_after=time.monotonic() + 1.0)
		self.assertFalse(sess._io.dead)
		time.sleep(0.2)
		self.assertEqual(peer.messages, [])


class TestPoisonWarningNeverMasksTheOutcome(_MuxCase):
	"""(Independent CODE review.) The one poison warning is diagnostics only: a logging
	handler that raises must not replace what the caller is owed. Through the real mux
	and socket gate: a socket error before the first raw send stays ``SendNotStarted``
	(reason ``local``), and an RQ job timeout or ``SystemExit`` stays the SAME object;
	the transport is poisoned, no lane or pending RPC is left, and the warning was
	attempted without the request's text."""

	def _run(self, exc_for_budget, *, after_first_chunk: bool):
		mux, sess, _peer, _ = self.mux("tls13")
		real = sess._io._send_budget
		calls: list[int] = []

		def budget():
			calls.append(1)
			if len(calls) > (1 if after_first_chunk else 0):
				raise exc_for_budget
			return real()

		message = "secret request text " + "z" * 100_000
		with (
			patch.object(sess._io, "_send_budget", side_effect=budget),
			patch.object(relay_mux._logger, "warning", side_effect=RuntimeError("log handler broke")) as warn,
		):
			try:
				mux.send_chat("sess-1", message, "run-poison", _handler(), timeout_s=5)
			except BaseException as e:
				raised = e
			else:
				raised = None
		self.assertTrue(mux.is_closed(), "the transport is poisoned")
		self.assertNotIn("run-poison", mux._runs, "ghost lane left behind")
		self.assertEqual(mux._pending, {}, "ghost pending RPC left behind")
		self.assertEqual(warn.call_count, 1, "the warning was attempted once")
		self.assertNotIn("secret request text", " ".join(map(str, warn.call_args.args)))
		return raised

	def test_local_not_started_survives_a_failing_logger(self):
		from jarvis.chat.agent_client import SendNotStarted

		raised = self._run(TimeoutError("timed out"), after_first_chunk=False)
		self.assertIs(type(raised), SendNotStarted, repr(raised))
		self.assertEqual(raised.reason, "local")

	def test_job_timeout_and_system_exit_survive_a_failing_logger(self):
		from jarvis.tests.test_ws_io_gate import _JobTimeout

		for exc, after in ((_JobTimeout(), True), (SystemExit(3), False)):
			with self.subTest(exc=type(exc).__name__):
				self.assertIs(self._run(exc, after_first_chunk=after), exc)
