"""One gated, deadline-bounded IO path for the shared agent WebSocket (#773).

A shard has ONE socket. The relay reader reads it in short slices on its own
thread while the pump thread writes every RPC, some of them multi-megabyte
uploads. websocket-client offers no total deadline for a frame and OpenSSL does
not support concurrent SSL_read/SSL_write on one SSL object, so before #773 a
write inherited whatever timeout the last read had set (0.5s) and the two sides
could interleave TLS calls. This module owns every low-level call on that socket:

* ``_SocketIO`` is a gate: at most one OPERATION (a read, a write, the probe or the
  graceful close) touches the socket at a time. The condition variable is held
  only to change gate state, never across socket IO or another lock.
  Lock order: ``AgentSession._lock`` -> gate -> websocket-client ``lock``/
  ``readlock``.
* ``_DeadlineSocket`` is installed as ``ws.sock`` so every ``send``/``recv`` the
  library makes inside an operation gets the REMAINING time of that operation's
  absolute deadline. With stdlib sockets (plain or TLS: the pump's RQ thread
  path) continuous partial progress can therefore never stretch a write past its
  budget. A write is handed to the kernel at most 16 KB per call
  (``RAW_SEND_CHUNK``); the frame stays one WebSocket frame and nothing is ever
  retried after a failed send. Under gevent (the legacy pool only, which holds no
  lease) an SSL send may wait its whole timeout again on every WANT_WRITE inside
  one 16 KB call, so there the bound is per chunk, not proven for the whole frame.

Reads wait for readability OUTSIDE the gate, so an idle reader never holds the
socket from a writer. Only a reader whose descriptor is readable, or whose TLS
layer already holds decrypted bytes (``SSLSocket.pending()``, sampled inside an
operation), queues for the gate, and such a reader gets one turn between writes.

Outcomes a caller can rely on:

* ``SendNotStarted`` (code ``send-not-started``): no byte of THIS frame reached the
  socket. ``reason`` says why: ``closed`` (the transport is already dead), ``busy``
  (the lock and gate wait ran out), ``deadline`` (the write could not finish in its
  window) leave the transport as it was; ``local`` (a socket error before the first
  raw send of the frame) also aborts it, since that socket may be broken.
* ``SendUncertain`` (code ``ack-timeout``, the existing ambiguous sentinel): the
  write started and did not finish normally. The peer may hold the whole frame:
  CPython can raise from a signal handler after the kernel accepted the last byte
  (``sock_sendall`` runs ``PyErr_CheckSignals`` before returning). The transport is
  aborted; every later write is ``SendNotStarted``.
* Any other exception during a started write (an RQ job timeout, ``SystemExit``)
  aborts the transport and propagates unchanged.

Abort is a kernel ``shutdown(SHUT_RDWR)`` called through ``_socket.socket.shutdown``
on the C socket (the gevent socket's ``._sock`` under gevent). It never calls
``SSLSocket.shutdown`` or ``WebSocket.abort``: those clear ``_sslobj`` first, and a
concurrent writer would then send PLAINTEXT. The descriptor is closed exactly once,
only when the transport is dead and no operation is active, so no TLS call can
ever run on a closed or reused descriptor number.

Sockets without a real C socket underneath (test doubles, scripted sockets) run
in a compatibility mode: the same gate and error types, the library's own socket
calls, and no descriptor-lifetime guarantees.

Pure module: stdlib, websocket-client and ``jarvis.exceptions`` only (no Frappe),
so the websocket-client contract harness can load it under any library copy.
"""

from __future__ import annotations

import _socket
import errno
import json
import selectors
import socket
import threading
import time

import websocket

from jarvis.exceptions import AgentUnreachableError

# One wait budget shared by the session send lock and the gate.
IO_GATE_WAIT_S = 5.0
# Whole-frame write budget. An IO deadline (the pump's lease must cover it), not a
# payload cap: a 5 MB frame needs about 4 Mbit/s to fit.
IO_WRITE_TIMEOUT_S = 10.0
# One absolute budget for the library's automatic PONG/CLOSE reply inside a read.
CONTROL_REPLY_S = 1.0
# Graceful close: CLOSE frame out and the peer's CLOSE back, as websocket-client.
CLOSE_TIMEOUT_S = 3.0
# At most this many bytes per raw send call (under gevent this caps the bytes one
# call can wait on, not the frame's total time).
RAW_SEND_CHUNK = 16384
# A reader with data waits at most this long for a turn before re-checking its loop.
READY_READER_WAIT_S = IO_WRITE_TIMEOUT_S + 1.0

_SOCKET_ERRORS = (OSError, websocket.WebSocketException)


class SendNotStarted(AgentUnreachableError):
	"""No byte of this frame was handed to the socket. Raised only locally (never
	built from a gateway reply), so its TYPE is the proof; ``reason`` is one of
	``closed``, ``busy``, ``deadline`` or ``local`` (see the module docstring)."""

	def __init__(self, message: str, *, reason: str = "closed"):
		super().__init__(message, code="send-not-started")
		self.reason = reason


class SendUncertain(AgentUnreachableError):
	"""The frame write started and failed; the peer may hold the whole frame."""

	def __init__(self, message: str):
		super().__init__(message, code="ack-timeout")


def _kernel_target(raw):
	"""The C socket whose descriptor a kernel shutdown acts on: a stdlib socket or
	SSLSocket is one; a gevent socket wraps one in ``._sock``. None for doubles."""
	if isinstance(raw, _socket.socket):
		return raw
	inner = getattr(raw, "_sock", None)
	return inner if isinstance(inner, _socket.socket) else None


class _DeadlineSocket:
	"""Stands in for the raw socket as ``ws.sock``. Only the thread running the
	current gate operation may send or receive through it; anything else gets
	EBADF. ``close`` and ``shutdown`` never touch the descriptor directly: they ask
	the gate, which closes it once no operation is running."""

	def __init__(self, raw, io: _SocketIO):
		self._raw = raw
		self._io = io

	def fileno(self):
		return self._raw.fileno()

	def gettimeout(self):
		remaining = self._io._remaining()
		return remaining if remaining is not None else self._raw.gettimeout()

	def settimeout(self, _timeout) -> None:
		# The operation's absolute deadline governs every call.
		return None

	@property
	def _closed(self):
		return getattr(self._raw, "_closed", False)

	def send(self, data) -> int:
		self._io._check_owner()  # outside its operation: EBADF, and nothing was sent
		try:
			view = memoryview(data if not isinstance(data, str) else data.encode("utf-8"))
			total = len(view)
			sent = 0
			while sent < total:
				self._raw.settimeout(self._io._send_budget())
				# From here on the peer may hold bytes of this operation's frame.
				self._io._raw_send_started = True
				sent += self._raw.send(view[sent : sent + RAW_SEND_CHUNK])
			return total
		except BaseException:
			# A failed send may have been partial: the stream is unusable. Never retried.
			self._io.abort()
			raise

	def recv(self, bufsize: int):
		self._raw.settimeout(self._io._recv_budget())
		return self._raw.recv(bufsize)

	def shutdown(self, _how) -> None:
		self._io.kernel_shutdown()

	def close(self) -> None:
		self._io.request_close()

	def __getattr__(self, name):
		return getattr(self._raw, name)


class _SocketIO:
	"""The gate and deadline owner for one WebSocket (see the module docstring).

	Invariant I-FD: the raw descriptor is closed only by ``_finalize``, which runs
	only when ``_dead`` is True and no operation is active; an operation starts only
	while ``_dead`` is False, checked under the condition variable. Kernel shutdown
	and finalize both hold ``_fd_lock`` and check ``_fd_closed``.

	Writers are not counted as holders: every write runs inside an operation, and
	finalize waits for the operation to end."""

	def __init__(self, ws):
		self._ws = ws
		raw = getattr(ws, "sock", None)
		self._raw = raw if _kernel_target(raw) is not None else None
		self._cv = threading.Condition()
		self._fd_lock = threading.Lock()
		self._busy = False
		self._op_kind: str | None = None
		self._op_deadline: float | None = None
		self._op_owner: int | None = None
		self._ctrl_deadline: float | None = None
		# Did the current operation call a raw send yet? Without a real socket nothing
		# proves it did not, so compatibility mode counts every operation as started.
		self._raw_send_started = False
		self._writers_waiting = 0
		self._readers_waiting = 0
		self._reader_turn = False
		self._dead = False
		self._close_requested = False
		self._fd_closed = False
		self._ssl_buffered = False
		if self._raw is not None:
			ws.sock = _DeadlineSocket(self._raw, self)
			# The handshake may have left decrypted bytes inside TLS: sample them in a
			# brief operation instead of a blind first blocking read.
			with self._cv:
				self._start_op("probe", time.monotonic())
			try:
				self._ssl_buffered = self._pending()
			finally:
				self._end_op()

	# -- state ------------------------------------------------------------- #

	@property
	def dead(self) -> bool:
		with self._cv:
			return self._dead

	def _start_op(self, kind: str, deadline: float) -> None:
		# Caller holds self._cv.
		self._busy = True
		self._op_kind = kind
		self._op_deadline = deadline
		self._op_owner = threading.get_ident()
		self._ctrl_deadline = None
		self._raw_send_started = self._raw is None

	def _end_op(self) -> None:
		with self._cv:
			if self._op_kind == "write":
				self._reader_turn = True
			self._busy = False
			self._op_kind = None
			self._op_deadline = None
			self._op_owner = None
			self._ctrl_deadline = None
			finalize = self._close_requested
			self._cv.notify_all()
		if finalize:
			self._finalize()

	def _check_owner(self) -> float:
		deadline = self._op_deadline
		if deadline is None or self._op_owner != threading.get_ident():
			raise OSError(errno.EBADF, "agent socket used outside its gated operation")
		return deadline

	def _remaining(self) -> float | None:
		deadline = self._op_deadline
		if deadline is None or self._op_owner != threading.get_ident():
			return None
		return max(deadline - time.monotonic(), 0.001)

	def _send_budget(self) -> float:
		deadline = self._check_owner()
		if self._op_kind == "read":
			# An automatic PONG/CLOSE reply inside a read: one absolute budget for the
			# whole reply, never re-floored per partial send.
			if self._ctrl_deadline is None:
				self._ctrl_deadline = max(deadline, time.monotonic() + CONTROL_REPLY_S)
			deadline = self._ctrl_deadline
		left = deadline - time.monotonic()
		if left <= 0:
			raise TimeoutError("timed out")
		return left

	def _recv_budget(self) -> float:
		left = self._check_owner() - time.monotonic()
		if left <= 0:
			raise TimeoutError("timed out")
		return left

	def _pending(self) -> bool:
		try:
			return self._raw.pending() > 0
		except Exception:
			return False

	def _raise_if_dead(self) -> None:
		if self.dead:
			raise AgentUnreachableError("agent WS closed: transport aborted")

	# -- lifecycle ----------------------------------------------------------- #

	def kernel_shutdown(self) -> None:
		target = _kernel_target(self._raw)
		if target is None:
			return
		with self._fd_lock:
			if self._fd_closed:
				return
			try:
				_socket.socket.shutdown(target, socket.SHUT_RDWR)
			except OSError:
				pass

	def abort(self) -> None:
		"""Poison the transport without waiting for any operation: mark it dead, wake
		every waiter, and shut the kernel socket down so a blocked reader or writer
		returns at once. The descriptor itself is closed later by ``_finalize``."""
		with self._cv:
			self._dead = True
			self._cv.notify_all()
		self.kernel_shutdown()
		try:
			self._ws.connected = False
		except Exception:
			pass

	def request_close(self) -> None:
		with self._cv:
			self._close_requested = True
			self._dead = True
			self._cv.notify_all()
			now = not self._busy
		if now:
			self._finalize()

	def _finalize(self) -> None:
		if self._raw is None:
			return
		with self._fd_lock:
			if self._fd_closed:
				return
			self._fd_closed = True
			try:
				self._raw.close()
			except OSError:
				pass

	def close_graceful(self, timeout_s: float = CLOSE_TIMEOUT_S) -> None:
		"""Owner close: send CLOSE and wait for the peer's within ``timeout_s`` when the
		gate is free within ``IO_GATE_WAIT_S``; otherwise abort. Idempotent. The
		descriptor closes when the close operation (or the operation running when
		the gate could not be had) ends."""
		if self._raw is None:
			with self._cv:
				if self._close_requested:
					return
				self._close_requested = self._dead = True
				self._cv.notify_all()
			try:
				self._ws.close()
			except Exception:
				pass
			return
		acquired = False
		with self._cv:
			if self._close_requested:
				return
			wait_end = time.monotonic() + IO_GATE_WAIT_S
			while not self._dead and self._busy:
				left = wait_end - time.monotonic()
				if left <= 0:
					break
				self._cv.wait(left)
			if not self._dead and not self._busy:
				self._start_op("close", time.monotonic() + timeout_s)
				acquired = True
		if not acquired:
			self.abort()
			self.request_close()
			return
		try:
			self._ws.close(timeout=timeout_s)
		except Exception:
			pass
		finally:
			with self._cv:
				self._close_requested = self._dead = True
			try:
				self._ws.connected = False
			except Exception:
				pass
			self._end_op()

	# -- read ---------------------------------------------------------------- #

	def _wait_readable(self, timeout_s: float) -> bool:
		"""Readiness wait with no gate and no TLS call. Any error (a closed
		descriptor gives fd -1) returns True: the dead check under the gate
		decides before any socket IO."""
		try:
			sel = selectors.DefaultSelector()
			try:
				sel.register(self._raw, selectors.EVENT_READ)
				return bool(sel.select(timeout_s))
			finally:
				sel.close()
		except (OSError, ValueError):
			return True

	def read(self, timeout_s: float) -> dict | None:
		"""One frame as parsed JSON, or None on a quiet slice, a control frame or
		non-JSON noise. Raises ``AgentUnreachableError`` on transport loss, after
		marking the transport dead."""
		if timeout_s <= 0:
			return None
		if self._raw is None:
			return self._read_compat(timeout_s)
		self._raise_if_dead()
		if not self._ssl_buffered and not self._wait_readable(timeout_s):
			self._raise_if_dead()
			return None
		wait_end = time.monotonic() + max(timeout_s, READY_READER_WAIT_S)
		with self._cv:
			self._readers_waiting += 1
			try:
				while not self._dead and (self._busy or (self._writers_waiting and not self._reader_turn)):
					left = wait_end - time.monotonic()
					if left <= 0:
						break
					self._cv.wait(left)
			finally:
				self._readers_waiting -= 1
			if self._dead:
				raise AgentUnreachableError("agent WS closed: transport aborted")
			if self._busy:
				return None
			self._start_op("read", time.monotonic() + timeout_s)
			self._reader_turn = False
		try:
			raw = self._read_frame()
		except websocket.WebSocketTimeoutException as e:
			if self.dead:
				# The library's PONG/CLOSE reply failed inside this read.
				raise AgentUnreachableError(f"agent WS closed: {e}") from e
			return None
		except websocket.WebSocketConnectionClosedException as e:
			self.abort()
			raise AgentUnreachableError(f"agent WS closed: {e}") from e
		except websocket.WebSocketException as e:
			self.abort()
			raise AgentUnreachableError(f"agent WS error: {e}") from e
		except OSError as e:
			self.abort()
			raise AgentUnreachableError(f"agent WS closed: {e}") from e
		except BaseException:
			self.abort()
			raise
		finally:
			self._ssl_buffered = self._pending()
			self._end_op()
		return _parse(raw)

	def _read_frame(self):
		"""One data frame, or "" for a control frame, so a PING never keeps the
		operation waiting for the next frame."""
		ws = self._ws
		with ws.readlock:
			opcode, frame = ws.recv_data_frame(True)
		if opcode == websocket.ABNF.OPCODE_TEXT:
			data = frame.data
			return data.decode("utf-8") if isinstance(data, bytes) else data
		if opcode == websocket.ABNF.OPCODE_BINARY:
			return frame.data
		return ""

	def _read_compat(self, timeout_s: float) -> dict | None:
		self._raise_if_dead()
		wait_end = time.monotonic() + max(timeout_s, READY_READER_WAIT_S)
		with self._cv:
			while not self._dead and self._busy:
				left = wait_end - time.monotonic()
				if left <= 0:
					break
				self._cv.wait(left)
			if self._dead:
				raise AgentUnreachableError("agent WS closed: transport aborted")
			if self._busy:
				return None
			self._start_op("read", time.monotonic() + timeout_s)
		ws = self._ws
		try:
			ws.settimeout(timeout_s)
			raw = ws.recv()
		except websocket.WebSocketTimeoutException:
			return None
		except websocket.WebSocketConnectionClosedException as e:
			self.abort()
			raise AgentUnreachableError(f"agent WS closed: {e}") from e
		except websocket.WebSocketException as e:
			self.abort()
			raise AgentUnreachableError(f"agent WS error: {e}") from e
		except OSError as e:
			self.abort()
			raise AgentUnreachableError(f"agent WS closed: {e}") from e
		finally:
			self._end_op()
		return _parse(raw)

	# -- write --------------------------------------------------------------- #

	def write(
		self,
		payload: str,
		*,
		lock: threading.Lock | None,
		wait_s: float = IO_GATE_WAIT_S,
		write_s: float = IO_WRITE_TIMEOUT_S,
		not_after: float | None = None,
		method: str = "frame",
	) -> None:
		"""Write one whole frame within ``write_s``, after waiting at most ``wait_s``
		in total for ``lock`` and the gate. ``not_after`` (monotonic) refuses a write
		that could not finish by then. See the module docstring for the outcomes."""
		if self.dead:
			raise SendNotStarted(f"{method} was not sent: the connection to the assistant is closed")
		wait_end = time.monotonic() + wait_s
		if lock is not None and not lock.acquire(timeout=max(0.0, wait_end - time.monotonic())):
			raise SendNotStarted(
				f"{method} was not sent: the connection to the assistant stayed busy for {wait_s:g}s",
				reason="busy",
			)
		try:
			with self._cv:
				self._writers_waiting += 1
				try:
					while not self._dead and (
						self._busy or (self._reader_turn and self._readers_waiting > 0)
					):
						left = wait_end - time.monotonic()
						if left <= 0:
							raise SendNotStarted(
								f"{method} was not sent: the connection to the assistant stayed busy for {wait_s:g}s",
								reason="busy",
							)
						self._cv.wait(left)
					if self._dead:
						raise SendNotStarted(
							f"{method} was not sent: the connection to the assistant is closed"
						)
					now = time.monotonic()
					if not_after is not None and now + write_s > not_after:
						raise SendNotStarted(
							f"{method} was not sent: no time left in this delivery window", reason="deadline"
						)
					self._start_op("write", now + write_s)
				finally:
					self._writers_waiting -= 1
					# A reader with data may be waiting only because this writer was
					# queued: wake it whether or not this writer goes on to write
					# (refused at its deadline, or interrupted while waiting).
					self._cv.notify_all()
			try:
				if self._raw is None:
					self._ws.settimeout(write_s)
				self._ws.send(payload)
			except _SOCKET_ERRORS as e:
				# Either way this socket is not used again.
				self.abort()
				if not self._raw_send_started:
					# Failed before any raw send of this frame (a spent budget, a library
					# check): the peer holds none of it.
					raise SendNotStarted(f"{method} was not sent: {type(e).__name__}", reason="local") from e
				raise SendUncertain(f"{method} may not have been delivered: {e}") from e
			except BaseException:
				self.abort()
				raise
			finally:
				self._end_op()
		finally:
			if lock is not None:
				lock.release()


def _parse(raw) -> dict | None:
	if not raw:
		return None
	try:
		return json.loads(raw)
	except json.JSONDecodeError:
		return None
