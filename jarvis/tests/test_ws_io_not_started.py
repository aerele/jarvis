"""#773 review (PR #2201): which failures prove that NO byte of a frame left.

Only a socket error raised before the first raw send of a write is proof that the
peer holds nothing of that frame: it is ``SendNotStarted`` (reason ``local``). The
socket may be broken, so the transport is dead and every later write is refused
too (reason ``closed``), with no byte on the wire. Once a raw send started, a socket
error stays ``SendUncertain`` and any other exception propagates as the same object,
both with the transport aborted. Real local plain and TLS 1.3 socket pairs; the wire
is read through a relay, so "no byte" is measured, not assumed.
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import patch

import websocket

from jarvis.chat import socket_io
from jarvis.chat.agent_client import AgentSession, SendNotStarted, SendUncertain
from jarvis.tests.test_ws_io_gate import _JobTimeout, _SocketCase, _SpyCase, _wait


class TestFailureBeforeTheFirstRawSend(_SpyCase):
	"""The write's first budget check fails (its whole budget is already spent)
	before any raw send: nothing of the frame reached the socket."""

	def _run(self, mode):
		sess, peer, _spy, _cli, _srv, rel = self.spied_session(mode, relay=True)
		time.sleep(0.2)
		wire = len(rel.c2s)
		with patch.object(sess._io, "_send_budget", side_effect=TimeoutError("timed out")):
			with self.assertRaises(SendNotStarted, msg=f"{mode}: no raw send ran, so nothing was sent") as cm:
				sess._write_frame(json.dumps({"blob": "q" * 50_000}), method="chat.send")
		self.assertEqual(cm.exception.code, "send-not-started")
		self.assertEqual(getattr(cm.exception, "reason", None), "local")
		self.assertTrue(sess._io.dead, "a socket that failed may be broken: it is not used again")
		with self.assertRaises(SendNotStarted) as again:
			sess._send("chat.abort", {"sessionKey": "s"})
		self.assertEqual(getattr(again.exception, "reason", None), "closed")
		time.sleep(0.3)
		self.assertEqual(len(rel.c2s), wire, f"{mode}: a refused write put bytes on the wire")
		self.assertEqual(peer.messages, [])

	def test_plain(self):
		self._run("plain")

	def test_tls13(self):
		self._run("tls13")


class TestFailureAfterARawSend(_SpyCase):
	"""The budget fails on the SECOND chunk: 16 KB already went to the kernel, so
	the peer may hold part of the frame. Unchanged: ambiguous, transport aborted."""

	def _run(self, mode):
		sess, _peer, _spy, _cli, _srv, rel = self.spied_session(mode, relay=True)
		real = sess._io._send_budget
		calls: list[int] = []

		def budget():
			calls.append(1)
			if len(calls) > 1:
				raise TimeoutError("timed out")
			return real()

		with patch.object(sess._io, "_send_budget", side_effect=budget):
			with self.assertRaises(SendUncertain) as cm:
				sess._write_frame(json.dumps({"blob": "u" * 200_000}), method="chat.send")
		self.assertEqual(cm.exception.code, "ack-timeout")
		self.assertTrue(sess._io.dead)
		self.assertTrue(_wait(lambda: len(rel.c2s) > 0, 2), f"{mode}: premise: the first chunk went out")

	def test_plain(self):
		self._run("plain")

	def test_tls13(self):
		self._run("tls13")


class TestOtherExceptionsKeepTheirObject(_SpyCase):
	"""An RQ job timeout (an ``Exception`` subclass raised by a signal handler) or
	``SystemExit`` before or after the first raw send: the SAME object propagates,
	never wrapped as a send outcome, and the transport is aborted."""

	def _run(self, mode, exc, *, after_first_chunk):
		sess, _peer, _spy, *_ = self.spied_session(mode)
		real = sess._io._send_budget
		calls: list[int] = []

		def budget():
			calls.append(1)
			if len(calls) > (1 if after_first_chunk else 0):
				raise exc
			return real()

		with patch.object(sess._io, "_send_budget", side_effect=budget):
			with self.assertRaises(type(exc)) as cm:
				sess._write_frame(json.dumps({"blob": "j" * 200_000}), method="chat.send")
		self.assertIs(cm.exception, exc)
		self.assertTrue(sess._io.dead)

	def test_job_timeout_before_and_after_plain(self):
		self._run("plain", _JobTimeout(), after_first_chunk=False)
		self._run("plain", _JobTimeout(), after_first_chunk=True)

	def test_system_exit_before_and_after_tls13(self):
		self._run("tls13", SystemExit(3), after_first_chunk=False)
		self._run("tls13", SystemExit(3), after_first_chunk=True)


class _ScriptedWs:
	"""A websocket double with no real socket underneath: the compatibility mode."""

	sock = None
	connected = True

	def settimeout(self, _t):
		return None

	def recv(self):
		raise websocket.WebSocketTimeoutException("timed out")

	def send(self, _payload):
		return None

	def close(self, **_kw):
		return None


class TestCompatibilityReadIsBounded(_SocketCase):
	"""A scripted session whose gate is held by another operation: a read waits at
	most its ready-reader bound, then returns None, as the real-socket read does.
	Before, it waited on the condition with no timeout at all."""

	def test_read_returns_while_another_operation_holds_the_gate(self):
		sess = AgentSession(_ScriptedWs(), None)
		io = sess._io
		with io._cv:
			io._start_op("write", time.monotonic() + 60)
		out: dict = {}

		def reader():
			t0 = time.monotonic()
			out["frame"] = sess._recv(0.1)
			out["elapsed"] = time.monotonic() - t0

		with patch.object(socket_io, "READY_READER_WAIT_S", 0.3):
			t = threading.Thread(target=reader, daemon=True)
			t.start()
			t.join(3)
			blocked = t.is_alive()
		io._end_op()
		t.join(3)
		self.assertFalse(blocked, "the read was still waiting for the gate after 3s")
		self.assertIsNone(out.get("frame"))
		self.assertLess(out["elapsed"], 0.3 + 0.5)
