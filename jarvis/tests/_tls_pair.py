"""Local socket fixtures for the #773 transport tests (no network, no site, no Frappe).

Everything here runs over ``socket.socketpair()`` (AF_UNIX), optionally wrapped in
real TLS with a throwaway self-signed certificate, so the tests exercise the actual
kernel buffers, the actual OpenSSL record layer and the actual websocket-client
framing that production uses, without opening a port.

* ``TempCert``: an EC certificate in a fresh temporary directory, deleted by
  ``cleanup()``.
* ``make_pair``: a connected ``(client, server, relay)`` triple, plain or TLS
  1.2/1.3, with optional small kernel buffers and an optional ``Relay`` that keeps
  every byte the client puts on the wire.
* ``client_ws``: a ``websocket.WebSocket`` bound to the client end, as
  ``create_connection`` leaves it after the upgrade.
* ``Peer``: the server end as one thread. It parses masked client frames, can read
  slowly (``rate``), stop reading (``paused``), send scheduled server frames, PING,
  and answer requests through ``on_message``.

Teardown rule: stop and JOIN every thread that touches a socket BEFORE closing that
socket. A closed descriptor number is reused at once by the next socket, and a
thread still reading the old number would read someone else's bytes.

Importable without Frappe: ``jarvis/tests/harness/ws_io_contract.py`` loads it
under an isolated websocket-client copy.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import socket
import ssl
import struct
import tempfile
import threading
import time
from collections.abc import Callable

import websocket

OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MODES = ("plain", "tls12", "tls13")


class TempCert:
	"""A self-signed localhost certificate, valid for one hour."""

	def __init__(self):
		from cryptography import x509
		from cryptography.hazmat.primitives import hashes, serialization
		from cryptography.hazmat.primitives.asymmetric import ec
		from cryptography.x509.oid import NameOID

		key = ec.generate_private_key(ec.SECP256R1())
		name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
		now = datetime.datetime.now(datetime.timezone.utc)
		cert = (
			x509.CertificateBuilder()
			.subject_name(name)
			.issuer_name(name)
			.public_key(key.public_key())
			.serial_number(x509.random_serial_number())
			.not_valid_before(now - datetime.timedelta(minutes=1))
			.not_valid_after(now + datetime.timedelta(hours=1))
			.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
			.sign(key, hashes.SHA256())
		)
		self.dir = tempfile.mkdtemp(prefix="jarvis-773-cert-")
		self.cert = os.path.join(self.dir, "cert.pem")
		self.key = os.path.join(self.dir, "key.pem")
		with open(self.cert, "wb") as f:
			f.write(cert.public_bytes(serialization.Encoding.PEM))
		with open(self.key, "wb") as f:
			f.write(
				key.private_bytes(
					serialization.Encoding.PEM,
					serialization.PrivateFormat.PKCS8,
					serialization.NoEncryption(),
				)
			)

	def cleanup(self) -> None:
		shutil.rmtree(self.dir, ignore_errors=True)


class Relay:
	"""Forward raw bytes between the client's pair and the server's pair, keeping a
	copy of everything the client sent."""

	def __init__(self, client_side: socket.socket, server_side: socket.socket):
		self.c2s = bytearray()
		self._a, self._b = client_side, server_side
		self._stop = threading.Event()
		self._threads = [
			threading.Thread(target=self._pump, args=(self._a, self._b, True), daemon=True),
			threading.Thread(target=self._pump, args=(self._b, self._a, False), daemon=True),
		]
		for t in self._threads:
			t.start()

	def _pump(self, src, dst, record):
		src.settimeout(0.05)
		while not self._stop.is_set():
			try:
				data = src.recv(65536)
			except TimeoutError:
				continue
			except OSError:
				break
			if not data:
				break
			if record:
				self.c2s += data
			try:
				dst.sendall(data)
			except OSError:
				break
		# Carry the EOF across, as a real path would.
		try:
			dst.shutdown(socket.SHUT_WR)
		except OSError:
			pass

	def close(self) -> None:
		self._stop.set()
		for t in self._threads:
			t.join(5)
		self._a.close()
		self._b.close()


def _bufsize(sock: socket.socket, size: int | None) -> None:
	if size:
		sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, size)
		sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, size)


def make_pair(mode: str, cert: TempCert | None = None, *, bufsize: int | None = None, relay: bool = False):
	"""Return ``(client, server, relay_or_None)``. ``mode`` is plain, tls12 or tls13."""
	client, server = socket.socketpair()
	rel = None
	if relay:
		relay_client_side, relay_server_side = socket.socketpair()
		rel_server_end = server
		server = relay_server_side
		rel = Relay(rel_server_end, relay_client_side)
		# client <-> rel_server_end | relay | relay_client_side <-> server
	for s in (client, server):
		_bufsize(s, bufsize)
	if mode == "plain":
		return client, server, rel
	if cert is None:
		raise ValueError("TLS pairs need a TempCert")
	version = ssl.TLSVersion.TLSv1_2 if mode == "tls12" else ssl.TLSVersion.TLSv1_3
	sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
	sctx.load_cert_chain(cert.cert, cert.key)
	sctx.minimum_version = sctx.maximum_version = version
	cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
	cctx.load_verify_locations(cert.cert)
	out: dict = {}
	t = threading.Thread(target=lambda: out.update(s=sctx.wrap_socket(server, server_side=True)), daemon=True)
	t.start()
	tls_client = cctx.wrap_socket(client, server_hostname="localhost")
	t.join(10)
	tls_server = out["s"]
	# One application round trip, as the HTTP upgrade does in production: it makes
	# the client process the TLS 1.3 session tickets the server sent after the
	# handshake, so the WebSocket stream starts clean.
	tls_server.sendall(b"R")
	assert tls_client.recv(1) == b"R"
	return tls_client, tls_server, rel


def fill_send_buffer(sock) -> None:
	"""Fill ``sock``'s kernel send buffer (the peer must not be reading), down to
	single bytes so not even a control frame fits."""
	sock.setblocking(False)
	try:
		for size in (4096, 512, 64, 8, 1):
			try:
				while True:
					sock.send(b"\0" * size)
			except BlockingIOError:
				continue
	finally:
		sock.setblocking(True)


def client_ws(sock, *, zero_mask: bool = False) -> websocket.WebSocket:
	"""A connected client ``WebSocket`` on ``sock`` (what ``create_connection`` returns
	after the HTTP upgrade). ``zero_mask`` makes payload bytes visible on the wire."""
	ws = websocket.WebSocket(enable_multithread=True)
	ws.sock = sock
	ws.connected = True
	if zero_mask:
		ws.set_mask_key(lambda n: b"\x00" * n)
	return ws


def server_frame(opcode: int, payload: bytes | str) -> bytes:
	"""An unmasked, final server->client frame."""
	if isinstance(payload, str):
		payload = payload.encode()
	n = len(payload)
	if n < 126:
		head = bytes([0x80 | opcode, n])
	elif n < 65536:
		head = bytes([0x80 | opcode, 126]) + struct.pack("!H", n)
	else:
		head = bytes([0x80 | opcode, 127]) + struct.pack("!Q", n)
	return head + payload


def _unmask(mask: bytes, data: bytes) -> bytes:
	n = len(data)
	if not n:
		return b""
	key = (mask * (n // 4 + 1))[:n]
	return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


def sha(data: bytes | str) -> str:
	if isinstance(data, str):
		data = data.encode()
	return hashlib.sha256(data).hexdigest()


class Peer(threading.Thread):
	"""Server side of one WebSocket, on one thread.

	``messages`` holds ``(monotonic, opcode, payload)`` for every complete client data
	frame; ``pongs`` the arrival times of client PONGs. ``on_message(peer, payload)``
	runs on this thread for each text frame (use it to answer requests). ``rate``
	throttles reads to that many bytes per second; ``paused.set()`` stops reading
	altogether (outbound frames are still sent)."""

	def __init__(
		self,
		sock,
		*,
		rate: float | None = None,
		chunk: int = 16384,
		on_message: Callable[[Peer, bytes], None] | None = None,
		ping_every: float | None = None,
	):
		super().__init__(daemon=True)
		self.sock = sock
		self.rate = rate
		self.chunk = chunk
		self.on_message = on_message
		self.ping_every = ping_every
		self.paused = threading.Event()
		self._halt = threading.Event()
		self._out_lock = threading.Lock()
		self._out: list[tuple[float, bytes]] = []
		self._buf = bytearray()
		self.messages: list[tuple[float, int, bytes]] = []
		self.pongs: list[float] = []
		self.pings: list[float] = []
		self.closes = 0
		self.eof = False
		self.error: str | None = None
		self.bytes_read = 0
		self.handler_errors: list[str] = []

	# -- outbound ------------------------------------------------------------ #

	def send_raw(self, data: bytes, delay: float = 0.0) -> None:
		with self._out_lock:
			self._out.append((time.monotonic() + delay, data))
			self._out.sort(key=lambda item: item[0])

	def send_frame(self, opcode: int, payload: bytes | str, delay: float = 0.0) -> None:
		self.send_raw(server_frame(opcode, payload), delay)

	def send_json(self, obj: dict, delay: float = 0.0) -> None:
		self.send_frame(OP_TEXT, json.dumps(obj), delay)

	def _flush_due(self) -> bool:
		now = time.monotonic()
		with self._out_lock:
			due = [d for t, d in self._out if t <= now]
			self._out = [(t, d) for t, d in self._out if t > now]
		if not due:
			return True
		try:
			self.sock.settimeout(5.0)
			self.sock.sendall(b"".join(due))
		except (OSError, ssl.SSLError) as e:
			self.error = f"send: {type(e).__name__}: {e}"
			return False
		finally:
			try:
				self.sock.settimeout(0.01)
			except OSError:
				pass
		return True

	# -- loop ------------------------------------------------------------------ #

	def stop(self) -> None:
		self._halt.set()
		self.join(10)

	def run(self) -> None:
		self.sock.settimeout(0.01)
		next_ping = time.monotonic() + (self.ping_every or 0)
		while not self._halt.is_set():
			if not self._flush_due():
				return
			if self.ping_every and time.monotonic() >= next_ping:
				next_ping += self.ping_every
				self.pings.append(time.monotonic())
				self.send_frame(OP_PING, b"p%d" % len(self.pings))
				continue
			if self.paused.is_set():
				time.sleep(0.005)
				continue
			try:
				data = self.sock.recv(self.chunk)
			except (TimeoutError, ssl.SSLWantReadError):
				continue
			except (ssl.SSLError, OSError) as e:
				self.error = f"recv: {type(e).__name__}: {e}"
				return
			if not data:
				self.eof = True
				return
			self.bytes_read += len(data)
			self._buf += data
			self._parse()
			if self.rate:
				time.sleep(len(data) / self.rate)

	def _parse(self) -> None:
		while len(self._buf) >= 2:
			b = self._buf
			opcode, masked, n, i = b[0] & 0x0F, b[1] & 0x80, b[1] & 0x7F, 2
			if n == 126:
				if len(b) < 4:
					return
				n, i = struct.unpack("!H", bytes(b[2:4]))[0], 4
			elif n == 127:
				if len(b) < 10:
					return
				n, i = struct.unpack("!Q", bytes(b[2:10]))[0], 10
			mask = b""
			if masked:
				if len(b) < i + 4:
					return
				mask, i = bytes(b[i : i + 4]), i + 4
			if len(b) < i + n:
				return
			payload = bytes(b[i : i + n])
			del self._buf[: i + n]
			if masked:
				payload = _unmask(mask, payload)
			self._on_frame(opcode, payload)

	def _on_frame(self, opcode: int, payload: bytes) -> None:
		now = time.monotonic()
		if opcode == OP_PONG:
			self.pongs.append(now)
		elif opcode == OP_CLOSE:
			self.closes += 1
			self.send_frame(OP_CLOSE, payload[:2] or struct.pack("!H", 1000))
		elif opcode in (OP_TEXT, OP_BINARY):
			self.messages.append((now, opcode, payload))
			if self.on_message is not None and opcode == OP_TEXT:
				try:
					self.on_message(self, payload)
				except Exception as e:  # a broken test handler must be visible, not fatal
					self.handler_errors.append(f"{type(e).__name__}: {e}")

	def texts(self) -> list[dict]:
		out = []
		for _t, op, payload in self.messages:
			if op == OP_TEXT:
				try:
					out.append(json.loads(payload))
				except ValueError:
					continue
		return out


def rpc_responder(
	*, chat_final: bool = True, final_text: str = "done", hold: Callable[[dict], bool] | None = None
):
	"""``on_message`` that answers like the gateway: every request gets an ``ok``
	response; ``chat.send`` acks ``{runId: idempotencyKey, status: started}`` and,
	with ``chat_final``, then streams the run's final chat event. ``hold(frame)``
	returning True leaves that request unanswered."""

	def respond(peer: Peer, raw: bytes) -> None:
		frame = json.loads(raw)
		if frame.get("type") != "req":
			return
		if hold is not None and hold(frame):
			return
		method = frame.get("method")
		params = frame.get("params") or {}
		if method == "chat.send":
			run_id = params.get("idempotencyKey")
			peer.send_json(
				{
					"type": "res",
					"id": frame["id"],
					"ok": True,
					"payload": {"runId": run_id, "status": "started"},
				}
			)
			if chat_final:
				peer.send_json(
					{
						"type": "event",
						"event": "chat",
						"payload": {
							"runId": run_id,
							"sessionKey": params.get("sessionKey"),
							"state": "final",
							"message": {"content": [{"type": "text", "text": final_text}]},
						},
					}
				)
			return
		if method == "sessions.list":
			peer.send_json({"type": "res", "id": frame["id"], "ok": True, "payload": {"sessions": []}})
			return
		peer.send_json({"type": "res", "id": frame["id"], "ok": True, "payload": {}})

	return respond


def chat_sends(peer: Peer) -> list[dict]:
	return [f for f in peer.texts() if f.get("method") == "chat.send"]
