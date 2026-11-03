"""#773 review CODE-F1: a writer that leaves the gate without starting must wake a
ready reader it was holding back.

A reader with data waits while a writer is queued and the last operation was a
read (the writer's turn). If that writer then leaves WITHOUT writing (refused at
its lease deadline, or interrupted by an RQ job-timeout signal while waiting),
the reader must be woken at once: otherwise it sleeps until its own wait limit
(READY_READER_WAIT_S, 11s in production) with data ready and the socket idle,
and every lane on the shard stops receiving (deltas, finals, acks, PONGs).

These tests drive the real ``_SocketIO`` over a local AF_UNIX pair. The wake order
after the previous operation ends is normally up to the scheduler; to make the
failing interleaving deterministic, the writer's wake-up is delayed by 50ms so the
reader always re-checks first (and goes back to sleep behind the queued writer).
Each case runs in both arrival orders, ten times.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from unittest.mock import patch

import websocket
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import socket_io
from jarvis.chat.socket_io import SendNotStarted, _SocketIO
from jarvis.tests import _tls_pair as tp

TRIALS = 10
# Short enough that a stuck reader shows up quickly, long enough to tell "woken at
# once" from "woke at its own limit".
READER_LIMIT_S = 2.0


class _JobTimeout(Exception):
	"""Stands in for RQ's JobTimeoutException raised by its SIGALRM handler."""


class _WriterWakesLate(threading.Condition):
	"""The gate's condition variable, except that the registered writer thread, on
	its first wake-up, gives the lock back for ``delay_s`` before continuing (and,
	with ``interrupt``, raises instead of continuing, as a signal handler would).
	The lock semantics are unchanged; only the scheduling is pinned."""

	def __init__(self, delay_s: float = 0.05):
		super().__init__()
		self.delay_s = delay_s
		self.writer: int | None = None
		self.interrupt = False
		self.delayed = threading.Event()

	def wait(self, timeout=None):
		woke = super().wait(timeout)
		if threading.get_ident() == self.writer and not self.delayed.is_set():
			self.delayed.set()
			self.release()
			try:
				time.sleep(self.delay_s)
			finally:
				self.acquire()
			if self.interrupt:
				raise _JobTimeout("job exceeded its timeout")
		return woke


class TestRefusedWriterWakesReadyReader(FrappeTestCase):
	def _trial(self, *, reader_first: bool, interrupt: bool) -> dict:
		cli, srv = socket.socketpair()
		try:
			ws = websocket.WebSocket(enable_multithread=True)
			ws.sock, ws.connected = cli, True
			io = _SocketIO(ws)
			cv = io._cv = _WriterWakesLate()
			cv.interrupt = interrupt
			out: dict = {}
			# A read operation is running (the reader's previous read).
			with cv:
				io._start_op("read", time.monotonic() + 5)

			def writer():
				cv.writer = threading.get_ident()
				try:
					# Refused at the gate's lease backstop once the read op ends: its 10s
					# write budget does not fit before not_after.
					io.write(
						json.dumps({"type": "req", "id": "w"}),
						lock=None,
						write_s=10.0,
						not_after=time.monotonic() + 9.0,
						method="chat.send",
					)
					out["writer"] = "sent"
				except (SendNotStarted, _JobTimeout) as e:
					out["writer"] = type(e).__name__
				out["writer_left"] = time.monotonic()

			def reader():
				frame = io.read(0.5)
				out["reader_back"] = time.monotonic()
				out["frame"] = frame

			srv.sendall(tp.server_frame(tp.OP_TEXT, json.dumps({"type": "event", "n": 1})))
			first, second = (reader, writer) if reader_first else (writer, reader)
			threads = [threading.Thread(target=first, daemon=True)]
			threads[0].start()
			time.sleep(0.05)
			threads.append(threading.Thread(target=second, daemon=True))
			threads[1].start()
			end = time.monotonic() + 2
			while time.monotonic() < end:
				with cv:
					if io._readers_waiting == 1 and io._writers_waiting == 1:
						break
				time.sleep(0.005)
			with cv:
				out["queued"] = (io._readers_waiting, io._writers_waiting)
			io._end_op()  # the previous read ends: the writer's turn, not the reader's
			for t in threads:
				t.join(READER_LIMIT_S + 3)
			return out
		finally:
			cli.close()
			srv.close()

	def _run(self, *, interrupt: bool) -> None:
		expected = "_JobTimeout" if interrupt else "SendNotStarted"
		with patch.object(socket_io, "READY_READER_WAIT_S", READER_LIMIT_S):
			for i in range(TRIALS * 2):
				reader_first = i % 2 == 0
				out = self._trial(reader_first=reader_first, interrupt=interrupt)
				label = f"trial {i} reader_first={reader_first}"
				self.assertEqual(out["queued"], (1, 1), f"{label}: premise, both queued")
				self.assertEqual(out.get("writer"), expected, label)
				self.assertEqual((out.get("frame") or {}).get("n"), 1, f"{label}: the ready frame")
				late = out["reader_back"] - out["writer_left"]
				self.assertLessEqual(
					late, 0.2, f"{label}: the ready reader stayed asleep {late:.2f}s after the writer left"
				)

	def test_writer_refused_at_its_deadline(self):
		self._run(interrupt=False)

	def test_writer_interrupted_while_waiting(self):
		self._run(interrupt=True)
