"""#773: the shared agent WebSocket must deliver a large frame after a short read.

One socket serves every turn on a shard: the relay reader reads it in 0.5s
slices, and every write goes out on the same socket. These tests run the real
``AgentSession`` over a local socketpair, in plain and real TLS (1.2/1.3), against
a peer that reads at a bounded rate, and check what the user would see: whether
a BOE-sized frame arrives byte-exact, how long it takes, and what a stalled
transport reports. No network, no site data: only the local socket pair.
"""

from __future__ import annotations

import json
import threading
import time

from frappe.tests.utils import FrappeTestCase

from jarvis.chat.agent_client import AgentSession
from jarvis.tests import _tls_pair as tp

MB = 1024 * 1024


class _SocketCase(FrappeTestCase):
	"""Owns every socket, peer and session a test opens, and tears them down in the
	safe order: session close, then peer threads joined, then sockets closed."""

	def setUp(self):
		self._cert: tp.TempCert | None = None
		self._sessions: list[AgentSession] = []
		self._peers: list[tp.Peer] = []
		self._socks: list = []
		self._relays: list = []
		self._threads: list[threading.Thread] = []

	def tearDown(self):
		for t in self._threads:
			t.join(15)
		for s in self._sessions:
			try:
				s.close()
			except Exception:
				pass
		for p in self._peers:
			p.stop()
		for r in self._relays:
			r.close()
		for s in self._socks:
			try:
				s.close()
			except OSError:
				pass
		if self._cert is not None:
			self._cert.cleanup()

	def cert(self) -> tp.TempCert:
		if self._cert is None:
			self._cert = tp.TempCert()
		return self._cert

	def session(
		self,
		mode: str,
		*,
		bufsize: int | None = None,
		relay: bool = False,
		zero_mask: bool = False,
		**peer_kw,
	):
		cert = self.cert() if mode != "plain" else None
		cli, srv, rel = tp.make_pair(mode, cert, bufsize=bufsize, relay=relay)
		self._socks.append(srv)
		if rel is not None:
			self._relays.append(rel)
		peer = tp.Peer(srv, **peer_kw)
		self._peers.append(peer)
		peer.start()
		sess = AgentSession(tp.client_ws(cli, zero_mask=zero_mask), None)
		self._sessions.append(sess)
		return sess, peer, rel

	def thread(self, target, *args) -> threading.Thread:
		t = threading.Thread(target=target, args=args, daemon=True)
		self._threads.append(t)
		t.start()
		return t


def _wait(predicate, timeout_s: float, step: float = 0.01) -> bool:
	end = time.monotonic() + timeout_s
	while time.monotonic() < end:
		if predicate():
			return True
		time.sleep(step)
	return predicate()


class TestLargeWriteAfterQuietRead(_SocketCase):
	"""AC-T1/AC-L1: a legacy session that just finished a quiet 0.5s read must still
	send a 1.5 MB frame to a peer that reads at 1.5 MB/s (about one second of
	transfer). Before #773 the write inherited the read's 0.5s timeout and failed."""

	def _quiet_read_then_large_send(self, mode: str) -> None:
		sess, peer, _ = self.session(mode, rate=1.5 * MB)
		self.assertIsNone(sess._recv(0.5), "premise: nothing to read")
		message = "B" * int(1.5 * MB)
		t0 = time.monotonic()
		try:
			sess._send("chat.send", {"sessionKey": "s", "message": message, "idempotencyKey": "run-1"})
		except Exception as e:  # report the failure the customer saw, not a traceback
			self.fail(
				f"{mode}: large send after a quiet read failed after {time.monotonic() - t0:.2f}s: {type(e).__name__}: {e}"
			)
		self.assertTrue(_wait(lambda: tp.chat_sends(peer), 10), f"{mode}: the peer never got the frame")
		got = tp.chat_sends(peer)[0]["params"]["message"]
		self.assertEqual(tp.sha(got), tp.sha(message), f"{mode}: frame not byte-exact")

	def test_plain(self):
		self._quiet_read_then_large_send("plain")

	def test_tls12(self):
		self._quiet_read_then_large_send("tls12")

	def test_tls13(self):
		self._quiet_read_then_large_send("tls13")


class TestLargeWriteAfterRequestReply(_SocketCase):
	"""AC-L1 through the public request path: a small request answered, then a large
	``chat.send`` on the same pooled session."""

	def test_tls13_request_then_large_send(self):
		sess, peer, _ = self.session("tls13", rate=1.5 * MB, on_message=tp.rpc_responder(chat_final=False))
		frame = sess._request("sessions.list", {}, timeout_s=5)
		self.assertTrue(frame.get("ok"))
		self.assertIsNone(sess._recv(0.5))
		message = json.dumps({"blob": "C" * int(1.5 * MB)})
		try:
			sess._send("chat.send", {"sessionKey": "s", "message": message, "idempotencyKey": "run-2"})
		except Exception as e:
			self.fail(f"large send after a request failed: {type(e).__name__}: {e}")
		self.assertTrue(_wait(lambda: tp.chat_sends(peer), 10))
		self.assertEqual(tp.sha(tp.chat_sends(peer)[0]["params"]["message"]), tp.sha(message))


# --------------------------------------------------------------------------- #
# Instrumentation
# --------------------------------------------------------------------------- #


class _SpySock:
	"""Wraps the client's raw socket the way gevent wraps one (``._sock`` is the real
	C socket), so the product treats it as a real socket, and records every call
	with the gate's state at that moment. ``fail_after`` raises ``fail_with()`` from
	``send`` once that many bytes in total went through, AFTER the real send
	returned: an exception that arrives when the peer already has the whole frame."""

	def __init__(self, raw):
		self._sock = raw
		self.io = None
		self.calls: list[tuple[str, int, bool, bool, bool]] = []
		self.closes: list[bool] = []
		self.sent = 0
		self.fail_after: int | None = None
		self.fail_with = None

	def _note(self, name):
		io = self.io
		if io is not None:
			self.calls.append(
				(name, threading.get_ident(), io._dead, io._fd_closed, io._op_owner == threading.get_ident())
			)

	def send(self, data):
		self._note("send")
		n = self._sock.send(data)
		self.sent += n
		if self.fail_after is not None and self.sent >= self.fail_after:
			self.fail_after = None
			raise self.fail_with()
		return n

	def recv(self, n):
		self._note("recv")
		return self._sock.recv(n)

	def pending(self):
		self._note("pending")
		return self._sock.pending() if hasattr(self._sock, "pending") else 0

	def close(self):
		self.closes.append(bool(self.io and self.io._busy))
		self._sock.close()

	def settimeout(self, t):
		self._sock.settimeout(t)

	def gettimeout(self):
		return self._sock.gettimeout()

	def fileno(self):
		return self._sock.fileno()

	@property
	def _closed(self):
		return self._sock._closed

	def io_after_close(self) -> list:
		return [c for c in self.calls if c[0] in ("send", "recv") and ((c[2] and not c[4]) or c[3])]


class _OpLog:
	"""Records every gate operation: kind, start, end, and how many ready readers
	were queued when it started."""

	def __init__(self, io):
		self.ops: list[dict] = []
		self._io = io
		start, end = io._start_op, io._end_op

		def start_op(kind, deadline):
			self.ops.append(
				{"kind": kind, "start": time.monotonic(), "end": None, "readers": io._readers_waiting}
			)
			start(kind, deadline)

		def end_op():
			for op in reversed(self.ops):
				if op["end"] is None:
					op["end"] = time.monotonic()
					break
			end()

		io._start_op = start_op
		io._end_op = end_op

	def durations(self, kind: str) -> list[float]:
		return [op["end"] - op["start"] for op in self.ops if op["kind"] == kind and op["end"] is not None]


def _frame_len(payload: str) -> int:
	import websocket

	return len(websocket.ABNF.create_frame(payload, websocket.ABNF.OPCODE_TEXT).format())


class _SpyCase(_SocketCase):
	def spied_session(self, mode: str, *, bufsize=None, relay=False, zero_mask=False, **peer_kw):
		cert = self.cert() if mode != "plain" else None
		cli, srv, rel = tp.make_pair(mode, cert, bufsize=bufsize, relay=relay)
		self._socks.append(srv)
		if rel is not None:
			self._relays.append(rel)
		peer = tp.Peer(srv, **peer_kw)
		self._peers.append(peer)
		peer.start()
		spy = _SpySock(cli)
		sess = AgentSession(tp.client_ws(spy, zero_mask=zero_mask), None)
		spy.io = sess._io
		self._sessions.append(sess)
		return sess, peer, spy, cli, srv, rel


# --------------------------------------------------------------------------- #
# Whole-frame write deadline
# --------------------------------------------------------------------------- #


class TestWholeFrameDeadline(_SocketCase):
	"""AC-T10/F-1: the write budget is a TOTAL deadline. A peer that keeps draining
	slowly (continuous partial progress) must not stretch a 0.2s budget: the write
	fails as ambiguous within 0.2s plus scheduling slack, the transport is dead and
	the peer never sees a complete frame. With enough budget the same frame arrives
	byte-exact. Before #773 a plain socket gave each partial send a fresh timeout."""

	def _run(self, mode):
		from jarvis.chat.agent_client import SendUncertain

		payload = json.dumps({"blob": "x" * 65536})
		sess, peer, _ = self.session(mode, bufsize=4096, rate=100_000, chunk=4096)
		t0 = time.monotonic()
		with self.assertRaises(SendUncertain) as cm:
			sess._write_frame(payload, write_s=0.2)
		elapsed = time.monotonic() - t0
		self.assertEqual(cm.exception.code, "ack-timeout")
		self.assertLessEqual(elapsed, 0.35, f"{mode}: budget 0.2s overran to {elapsed:.3f}s")
		self.assertTrue(sess._io.dead)
		time.sleep(1.0)
		self.assertEqual(peer.messages, [], f"{mode}: the peer must not hold a complete frame")

		sess2, peer2, _ = self.session(mode, bufsize=4096, rate=100_000, chunk=4096)
		t1 = time.monotonic()
		sess2._write_frame(payload, write_s=5.0)
		self.assertLess(time.monotonic() - t1, 5.0)
		self.assertTrue(_wait(lambda: peer2.messages, 5))
		self.assertEqual(tp.sha(peer2.messages[0][2]), tp.sha(payload))

	def test_plain(self):
		self._run("plain")

	def test_tls12(self):
		self._run("tls12")

	def test_tls13(self):
		self._run("tls13")


class TestStalledWritePoisons(_SocketCase):
	"""AC-T2: a peer that stops reading. The write fails as ambiguous, the transport
	is dead and marked disconnected (the session pool evicts it), and the next write
	is refused as not started with ZERO new bytes on the wire."""

	def _run(self, mode):
		from jarvis.chat import agent_session_pool
		from jarvis.chat.agent_client import SendNotStarted, SendUncertain

		sess, peer, rel = self.session(mode, bufsize=65536, relay=True)
		peer.paused.set()
		t0 = time.monotonic()
		with self.assertRaises(SendUncertain):
			sess._write_frame(json.dumps({"blob": "s" * (4 * MB)}), write_s=1.0)
		self.assertLess(time.monotonic() - t0, 1.5)
		self.assertTrue(sess._io.dead)
		self.assertFalse(agent_session_pool._safe_connected(sess), "the pool must evict a poisoned session")
		time.sleep(0.2)
		wire = len(rel.c2s)
		t1 = time.monotonic()
		with self.assertRaises(SendNotStarted) as cm:
			sess._send("chat.abort", {"sessionKey": "s"})
		self.assertEqual(cm.exception.code, "send-not-started")
		# Instant, not a wait: a reintroduced 0.5s read hold would still fail this.
		self.assertLess(time.monotonic() - t1, 0.25)
		time.sleep(0.2)
		self.assertEqual(len(rel.c2s), wire, "a refused write put bytes on the wire")
		self.assertEqual(peer.messages, [])

	def test_plain(self):
		self._run("plain")

	def test_tls13(self):
		self._run("tls13")


# --------------------------------------------------------------------------- #
# One combined wait; partial inbound frames
# --------------------------------------------------------------------------- #


class TestCombinedWaitAndPartialInbound(_SocketCase):
	"""AC-T6c/F-3 and F-5. The send lock is held for 3s, then a reader holds the
	socket on a partially received frame: the writer gets ONE 5s budget across
	both waits (refused at ~5.0s, not 6+), no byte goes out, and the transport is
	not poisoned. The partial inbound frame is kept across the read's timeout and
	arrives intact once the rest comes."""

	def test_tls13(self):
		from jarvis.chat.agent_client import SendNotStarted

		sess, peer, _ = self.session("tls13")
		body = json.dumps({"type": "event", "event": "x", "payload": {"blob": "p" * 60000}})
		frame = tp.server_frame(tp.OP_TEXT, body)
		got: list = []
		stop = threading.Event()

		def reader():
			while not stop.is_set():
				try:
					f = sess._recv(3.3)
				except Exception:
					return
				if f:
					got.append(f)

		def hold_lock():
			with sess._lock:
				time.sleep(3.0)

		self.thread(hold_lock)
		self.thread(reader)
		peer.send_raw(frame[:1000], delay=2.7)
		t0 = time.monotonic()
		with self.assertRaises(SendNotStarted):
			sess._write_frame(json.dumps({"type": "req", "id": "w", "method": "x"}), wait_s=5.0)
		elapsed = time.monotonic() - t0
		self.assertGreater(elapsed, 4.8)
		self.assertLess(elapsed, 5.3, f"one shared wait budget, got {elapsed:.2f}s")
		self.assertFalse(sess._io.dead, "a refused write must not poison the transport")
		self.assertEqual(peer.texts(), [], "no byte of the refused frame")
		peer.send_raw(frame[1000:], delay=1.5)
		self.assertTrue(_wait(lambda: got, 8), "the partial frame was lost")
		self.assertEqual(got[0]["payload"]["blob"], "p" * 60000)
		stop.set()


class TestTrickledInboundFairness(_SocketCase):
	"""F-5: a 60 KB inbound frame trickles in over ~2.4s while the pump writes 12
	small frames. Every read operation is bounded by its slice, every writer waits
	at most about one read slice, all writes arrive, and the frame is intact."""

	def _run(self, mode):
		sess, peer, _ = self.session(mode)
		log = _OpLog(sess._io)
		body = json.dumps({"type": "event", "event": "x", "payload": {"blob": "t" * 60000}})
		frame = tp.server_frame(tp.OP_TEXT, body)
		for i in range(0, len(frame), 3000):
			peer.send_raw(frame[i : i + 3000], delay=0.1 + i / 3000 * 0.12)
		got: list = []
		stop = threading.Event()

		def reader():
			while not stop.is_set():
				try:
					f = sess._recv(0.5)
				except Exception:
					return
				if f:
					got.append(f)

		self.thread(reader)
		waits = []
		for k in range(12):
			t0 = time.monotonic()
			sess._write_frame(json.dumps({"type": "req", "id": f"k{k}", "method": "ping"}))
			waits.append(time.monotonic() - t0)
			time.sleep(0.15)
		self.assertTrue(_wait(lambda: got, 5))
		stop.set()
		self.assertEqual(got[0]["payload"]["blob"], "t" * 60000, "frame not intact")
		self.assertLessEqual(max(log.durations("read")), 0.6, "a read op outlived its slice")
		self.assertLessEqual(max(waits), 0.65, f"a writer waited {max(waits):.3f}s")
		self.assertTrue(_wait(lambda: len(peer.texts()) == 12, 3))

	def test_plain(self):
		self._run("plain")

	def test_tls13(self):
		self._run("tls13")


# --------------------------------------------------------------------------- #
# Reads: buffered TLS bytes, no spin, EOF, control replies
# --------------------------------------------------------------------------- #


class TestBufferedFrames(_SocketCase):
	"""AC-K2c: two frames in ONE TLS record, then silence. The second frame is
	already decrypted inside TLS, so the descriptor is not readable: the reader
	must take it from TLS's buffer at once (<= 0.05s), not wait for the socket."""

	def _run(self, mode):
		sess, peer, _ = self.session(mode)
		a = tp.server_frame(tp.OP_TEXT, json.dumps({"type": "event", "n": 1}))
		b = tp.server_frame(tp.OP_TEXT, json.dumps({"type": "event", "n": 2}))
		peer.send_raw(a + b)
		first = sess._recv(1.0)
		t1 = time.monotonic()
		second = sess._recv(1.0)
		gap = time.monotonic() - t1
		self.assertEqual((first or {}).get("n"), 1)
		self.assertEqual((second or {}).get("n"), 2, f"{mode}: second frame not returned")
		self.assertLessEqual(gap, 0.05, f"{mode}: second frame took {gap:.3f}s")

	def test_plain(self):
		self._run("plain")

	def test_tls13(self):
		self._run("tls13")


class TestIdleAndEof(_SpyCase):
	"""AC-K2d/F-7: a quiet socket costs at most a dozen reader iterations in 5s (no
	spin), and an EOF while the reader waits outside the gate surfaces within one
	slice as transport loss, with no socket call after the transport is dead; the
	next write is a definite not-started."""

	def _run(self, mode):
		from jarvis.chat.agent_client import SendNotStarted
		from jarvis.exceptions import AgentUnreachableError

		sess, peer, spy, _cli, srv, _ = self.spied_session(mode)
		t0 = time.monotonic()
		iterations = 0
		while time.monotonic() - t0 < 5.0:
			self.assertIsNone(sess._recv(0.5))
			iterations += 1
		self.assertLessEqual(iterations, 12, f"{mode}: {iterations} iterations in 5s")
		box: dict = {}

		def reader():
			try:
				while True:
					sess._recv(0.5)
			except AgentUnreachableError as e:
				box["at"] = time.monotonic()
				box["e"] = e

		t = self.thread(reader)
		time.sleep(0.3)
		peer.stop()
		closed_at = time.monotonic()
		try:
			srv.shutdown(2)
		except OSError:
			pass
		t.join(2)
		self.assertIn("at", box, f"{mode}: reader did not see the EOF")
		self.assertLessEqual(box["at"] - closed_at, 0.6)
		self.assertEqual(spy.io_after_close(), [], f"{mode}: socket call after the transport died")
		with self.assertRaises(SendNotStarted):
			sess._send("chat.send", {"message": "after eof"})

	def test_plain(self):
		self._run("plain")

	def test_tls13(self):
		self._run("tls13")


class TestControlReplies(_SocketCase):
	"""F-6: the library's automatic PONG runs inside a read with one absolute budget
	of its own (CONTROL_REPLY_S). A PONG that has to wait past the read's slice for
	buffer space still goes out; a PONG that cannot be written poisons the transport
	instead of passing for a quiet slice."""

	def test_pong_outlives_its_read_slice(self):
		sess, peer, _ = self.session("plain", bufsize=4096)
		peer.paused.set()
		time.sleep(0.05)  # the peer thread has finished its last recv
		tp.fill_send_buffer(sess._io._raw)
		peer.send_frame(tp.OP_PING, b"x")
		time.sleep(0.1)
		threading.Timer(0.3, peer.paused.clear).start()  # the peer drains again in 0.3s
		t0 = time.monotonic()
		self.assertIsNone(sess._recv(0.05))
		self.assertGreater(time.monotonic() - t0, 0.2, "premise: the PONG had to wait for buffer space")
		self.assertFalse(sess._io.dead, "a PONG that finishes inside its own budget is healthy")

	def test_pong_write_failure_poisons(self):
		from jarvis.chat.agent_client import SendNotStarted
		from jarvis.exceptions import AgentUnreachableError

		sess, peer, _ = self.session("plain", bufsize=4096)
		peer.paused.set()
		time.sleep(0.05)  # the peer thread has finished its last recv
		tp.fill_send_buffer(sess._io._raw)  # the peer reads nothing: no PONG can fit
		peer.send_frame(tp.OP_PING, b"x")
		time.sleep(0.1)
		t0 = time.monotonic()
		with self.assertRaises(AgentUnreachableError):
			sess._recv(0.5)
		elapsed = time.monotonic() - t0
		self.assertGreater(elapsed, 0.8)
		self.assertLess(elapsed, 1.6, "one control-reply budget, not a per-send floor")
		self.assertTrue(sess._io.dead)
		with self.assertRaises(SendNotStarted):
			sess._send("chat.send", {"message": "after"})


# --------------------------------------------------------------------------- #
# Abort: never plaintext
# --------------------------------------------------------------------------- #


def _tls_records_only(wire: bytes) -> bool:
	i = 0
	while i + 5 <= len(wire):
		if wire[i] not in (0x14, 0x15, 0x16, 0x17):
			return False
		i += 5 + int.from_bytes(wire[i + 3 : i + 5], "big")
	return True


class TestAbortNeverLeaksPlaintext(_SocketCase):
	"""AC-T5/F-8: abort from a third thread while a writer and a reader are active.
	``SSLSocket.shutdown`` (what ``WebSocket.abort`` calls) clears ``_sslobj`` BEFORE
	the kernel shutdown, and a writer in that gap sends PLAINTEXT. The negative
	control widens that gap and shows the detector finds the leak; the product's
	abort, under the same widened gap, leaves only TLS records on the wire, keeps
	``_sslobj``, and wakes both threads within 0.5s."""

	def _drive(self, abort):
		import socket as _sock

		sess, peer, rel = self.session("tls13", relay=True, zero_mask=True)
		cli = sess._io._raw
		box: dict = {"n": 0}

		def writer():
			try:
				while True:
					sess._write_frame(json.dumps({"m": "PLAINTEXT-MARKER-773-" + "x" * 2000}))
					box["n"] += 1
			except BaseException as e:
				box["w"] = (time.monotonic(), type(e).__name__)

		def reader():
			try:
				while True:
					sess._recv(0.5)
			except BaseException as e:
				box["r"] = (time.monotonic(), type(e).__name__)

		real = _sock.socket.shutdown if "shutdown" in _sock.socket.__dict__ else None

		def slow_shutdown(self, how):
			time.sleep(0.3)  # widen the window between `_sslobj = None` and the kernel call
			import _socket

			return _socket.socket.shutdown(self, how)

		w, r = self.thread(writer), self.thread(reader)
		time.sleep(0.4)
		from unittest.mock import patch

		with patch.object(_sock.socket, "shutdown", slow_shutdown, create=real is None):
			t0 = time.monotonic()
			abort(sess, cli)
			w.join(3)
			r.join(3)
		time.sleep(0.3)
		wire = bytes(rel.c2s)
		return {
			"t0": t0,
			"frames": box["n"],
			"writer": box.get("w"),
			"reader": box.get("r"),
			"plaintext": b"PLAINTEXT-MARKER" in wire,
			"tls_only": _tls_records_only(wire),
			"sslobj": cli._sslobj is not None,
		}

	def test_negative_control_sslsocket_shutdown_leaks(self):
		import socket as _sock

		out = self._drive(lambda sess, cli: cli.shutdown(_sock.SHUT_RDWR))
		self.assertGreater(out["frames"], 0)
		self.assertTrue(
			out["plaintext"] or not out["tls_only"], "the detector must see the unsafe path's leak"
		)

	def test_product_abort_keeps_tls(self):
		out = self._drive(lambda sess, cli: sess.abort_transport())
		self.assertGreater(out["frames"], 0)
		self.assertFalse(out["plaintext"], "plaintext reached the wire")
		self.assertTrue(out["tls_only"], "a non-TLS byte reached the wire")
		self.assertTrue(out["sslobj"], "_sslobj must survive the abort")
		self.assertLess(out["writer"][0] - out["t0"], 0.5)
		self.assertLess(out["reader"][0] - out["t0"], 0.5)


# --------------------------------------------------------------------------- #
# Exception after the last byte
# --------------------------------------------------------------------------- #


class _JobTimeout(Exception):
	"""Stands in for RQ's JobTimeoutException, raised by a signal handler."""


class TestExceptionAfterLastByte(_SpyCase):
	"""AC-T7/AC-K6: an exception raised AFTER the kernel accepted the final byte of
	the frame (CPython's ``sock_sendall`` checks signals after the last chunk, so a
	``TimeoutError`` or an RQ job timeout can surface there). The peer HAS the whole
	frame, so the outcome must be ambiguous: a socket error becomes ``SendUncertain``
	(``ack-timeout``), a non-socket exception propagates as the same type. Either way
	the transport is aborted."""

	def _run(self, mode, exc_type):
		from jarvis.chat.agent_client import SendUncertain

		sess, peer, spy, *_ = self.spied_session(mode)
		payload = json.dumps({"blob": "z" * 200_000})
		spy.fail_after = _frame_len(payload)
		spy.fail_with = exc_type
		expected = SendUncertain if issubclass(exc_type, OSError) else exc_type
		with self.assertRaises(expected) as cm:
			sess._write_frame(payload)
		if expected is SendUncertain:
			self.assertEqual(cm.exception.code, "ack-timeout")
		self.assertIs(type(cm.exception), expected)
		self.assertTrue(sess._io.dead)
		self.assertTrue(_wait(lambda: peer.messages, 5), "the peer should hold the full frame")
		self.assertEqual(tp.sha(peer.messages[0][2]), tp.sha(payload))

	def test_socket_error_plain(self):
		self._run("plain", TimeoutError)

	def test_socket_error_tls13(self):
		self._run("tls13", TimeoutError)

	def test_job_timeout_plain(self):
		self._run("plain", _JobTimeout)

	def test_job_timeout_tls13(self):
		self._run("tls13", _JobTimeout)


# --------------------------------------------------------------------------- #
# Descriptor lifetime
# --------------------------------------------------------------------------- #


def _assert_no_theft(case, n: int = 20) -> None:
	"""Open ``n`` fresh socket pairs (they reuse the freed descriptor numbers) and
	check each one's bytes reach its own peer: an old reader on a reused number
	would steal them."""
	import socket as _sock

	pairs = [_sock.socketpair() for _ in range(n)]
	try:
		for i, (a, _b) in enumerate(pairs):
			a.sendall(b"mine-%d" % i)
		time.sleep(0.6)
		for i, (_a, b) in enumerate(pairs):
			b.settimeout(1.0)
			case.assertEqual(b.recv(64), b"mine-%d" % i, "a stale reader consumed a reused descriptor")
	finally:
		for a, b in pairs:
			a.close()
			b.close()


class TestDescriptorLifetime(_SpyCase):
	"""AC-K3/T8/T12/T13/F-2: the descriptor closes exactly once, never while any
	operation is running, and no socket call ever runs after it closed."""

	def test_owner_close_while_reader_waits_outside_the_gate(self):
		from jarvis.exceptions import AgentUnreachableError

		sess, peer, spy, *_ = self.spied_session("tls13")
		box: dict = {}

		def reader():
			try:
				while True:
					sess._recv(0.5)
			except AgentUnreachableError:
				box["at"] = time.monotonic()

		t = self.thread(reader)
		time.sleep(0.3)
		t0 = time.monotonic()
		sess.close()
		sess.close()  # idempotent
		t.join(2)
		self.assertLessEqual(box.get("at", 99) - t0, 1.0, "the reader did not exit")
		self.assertEqual(spy.closes, [False], "closed exactly once, with no operation running")
		self.assertEqual(spy.io_after_close(), [])
		self.assertTrue(_wait(lambda: peer.closes >= 1, 2), "graceful close sends CLOSE")
		_assert_no_theft(self)

	def test_close_while_a_writer_holds_the_socket(self):
		from unittest.mock import patch

		from jarvis.chat import socket_io

		sess, peer, spy, *_ = self.spied_session("tls13", bufsize=65536)
		peer.paused.set()
		box: dict = {}

		def writer():
			try:
				sess._write_frame(json.dumps({"blob": "w" * (4 * MB)}), write_s=8.0)
			except BaseException as e:
				box["e"] = (time.monotonic(), type(e).__name__)

		t = self.thread(writer)
		time.sleep(0.3)
		violations = []
		stop = threading.Event()

		def poll():
			while not stop.is_set():
				if sess._io._busy and sess._io._fd_closed:
					violations.append(time.monotonic())
				time.sleep(0.01)

		self.thread(poll)
		with patch.object(socket_io, "IO_GATE_WAIT_S", 0.5):
			t0 = time.monotonic()
			sess.close()
		self.assertLess(time.monotonic() - t0, 1.0, "close must not wait out a stalled writer")
		t.join(3)
		stop.set()
		self.assertEqual(box.get("e", (0, ""))[1], "SendUncertain")
		self.assertEqual(violations, [], "the descriptor closed under an active writer")
		self.assertEqual(spy.closes, [False])
		self.assertEqual(spy.io_after_close(), [])

	def test_mux_stop_twice_with_a_live_reader(self):
		from jarvis.chat.relay_mux import RelayMux

		sess, peer, spy, *_ = self.spied_session("tls13")
		mux = RelayMux(sess, "773-lifetime").start()
		time.sleep(0.3)
		mux.stop(timeout=3)
		mux.stop(timeout=3)
		self.assertFalse(mux._reader.is_alive())
		self.assertEqual(spy.closes, [False])
		self.assertEqual(spy.io_after_close(), [])
		_assert_no_theft(self)


class TestOutsideOperationGuard(_SpyCase):
	"""F-10: the socket can only be used from inside a gate operation. A direct call
	is refused (EBADF, nothing on the wire), and the normal flows (a request, a mux
	RPC, the graceful close) make every socket call from the operation's thread."""

	def test_direct_use_is_refused_and_flows_stay_inside_operations(self):
		import errno

		from jarvis.chat.relay_mux import RelayMux

		sess, peer, spy, *_ = self.spied_session("tls13", on_message=tp.rpc_responder(chat_final=False))
		with self.assertRaises(OSError) as cm:
			sess._ws.sock.send(b"stray")
		self.assertEqual(cm.exception.errno, errno.EBADF)
		self.assertTrue(sess._request("sessions.list", {}, timeout_s=5).get("ok"))
		mux = RelayMux(sess, "773-guard").start()
		self.assertTrue(mux.issue_rpc("sessions.list", {}, timeout_s=5).result(5).get("ok"))
		mux.stop(timeout=3)
		outside = [c for c in spy.calls if c[0] in ("send", "recv") and not c[4]]
		self.assertEqual(outside, [], "a socket call ran outside its operation")
		self.assertFalse(any(b"stray" in m[2] for m in peer.messages))


class TestLegacyWriteFailure(_SocketCase):
	"""AC-L2: a legacy (pooled, single-flow) session whose write stalls reports the
	ambiguous ``ack-timeout`` (turn_handler's existing park-for-recovery branch),
	is marked disconnected so the pool evicts it, and refuses the next send."""

	def test_tls13(self):
		from jarvis.chat import agent_session_pool
		from jarvis.chat.agent_client import SendNotStarted
		from jarvis.exceptions import AgentUnreachableError

		sess, peer, _ = self.session("tls13", bufsize=65536)
		peer.paused.set()
		with self.assertRaises(AgentUnreachableError) as cm:
			sess._write_frame(json.dumps({"blob": "l" * (4 * MB)}), write_s=1.0, method="chat.send")
		self.assertEqual(cm.exception.code, "ack-timeout")
		self.assertFalse(agent_session_pool._safe_connected(sess))
		with self.assertRaises(SendNotStarted):
			sess._send("chat.send", {"message": "again"})


class TestWebsocketClientContract(FrappeTestCase):
	"""AC-T9/F-9: the socket gate depends on websocket-client internals. The harness
	(``jarvis/tests/harness/ws_io_contract.py``) runs the product ``_SocketIO`` in a
	subprocess against the INSTALLED library; the gevent variant runs where gevent
	is installed (the CI image) and is skipped otherwise. The 1.6.0 floor runs
	manually with an isolated copy (``--ws-dir``)."""

	def _run(self, *extra):
		import os
		import subprocess
		import sys
		import tempfile

		harness = os.path.join(os.path.dirname(__file__), "harness", "ws_io_contract.py")
		with tempfile.TemporaryDirectory(prefix="jarvis-773-contract-") as d:
			out = os.path.join(d, "result.json")
			proc = subprocess.run(
				[sys.executable, "-I", harness, "--out", out, *extra],
				capture_output=True,
				text=True,
				timeout=180,
			)
			result = json.load(open(out)) if os.path.exists(out) else {"stderr": proc.stderr[-2000:]}
		failed = [c for c in result.get("checks", []) if not c.get("ok")]
		self.assertEqual(proc.returncode, 0, json.dumps(failed or result, indent=1)[:4000])

	def test_installed_library(self):
		self._run()

	def test_installed_library_under_gevent(self):
		import importlib.util
		import os

		spec = importlib.util.find_spec("gevent")
		if spec is None:
			self.skipTest("gevent is not installed in this environment")
		self._run("--gevent", os.path.dirname(os.path.dirname(spec.origin)))
