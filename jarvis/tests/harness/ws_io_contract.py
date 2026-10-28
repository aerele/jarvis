"""#773 websocket-client contract harness for the product socket gate.

``jarvis.chat.socket_io`` relies on websocket-client internals: ``ws.sock`` is
reassignable, the library does its socket IO only through ``send``/``recv``/
``gettimeout``/``fileno``/``shutdown``/``close``/``_closed``, ``frame_buffer`` keeps a
partial frame across a timeout, and ``recv_data_frame(True)`` returns control
frames. CI installs only the newest websocket-client while ``pyproject`` allows
``>=1.6``, so this script runs the PRODUCT ``_SocketIO`` against any library copy,
plain and TLS 1.3, optionally under gevent's ``monkey.patch_all`` (the legacy pool
runs under the gevent realtime executor):

    python -I jarvis/tests/harness/ws_io_contract.py --out result.json \\
        [--ws-dir <dir holding the websocket package>] [--gevent <dir holding gevent>] \\
        [--expect-version 1.6.0]

No Frappe, no network, no site: local socket pairs only. Exits non-zero on any
failed check. Under gevent the write deadline is bounded per 16 KB send, not proven
as a strict whole-frame bound (the legacy pool holds no lease).
"""

from __future__ import annotations

import argparse
import os
import sys


def _args():
	p = argparse.ArgumentParser()
	p.add_argument("--out", required=True)
	p.add_argument("--ws-dir")
	p.add_argument("--gevent")
	p.add_argument("--expect-version")
	return p.parse_args()


ARGS = _args()
if ARGS.gevent:
	sys.path.insert(0, ARGS.gevent)
	from gevent import monkey

	monkey.patch_all()
if ARGS.ws_dir:
	sys.path.insert(0, ARGS.ws_dir)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(1 if ARGS.ws_dir else 0, REPO)

import json
import socket
import ssl
import threading
import time
import traceback

import websocket

from jarvis.chat import socket_io
from jarvis.chat.socket_io import SendNotStarted, SendUncertain, _SocketIO
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests import _tls_pair as tp

RESULT: dict = {
	"env": {
		"python": sys.version.split()[0],
		"openssl": ssl.OPENSSL_VERSION,
		"websocket_client": websocket.__version__,
		"websocket_path": os.path.dirname(websocket.__file__),
		"gevent": __import__("gevent").__version__ if ARGS.gevent else None,
		"socket_io": socket_io.__file__,
		"network": False,
	},
	"checks": [],
}
CERT = None
OPEN: list = []


def _pair(mode, **kw):
	cli, srv, rel = tp.make_pair(mode, CERT if mode != "plain" else None, **kw)
	OPEN.append(srv)
	return cli, srv, rel


def check(name, mode, fn):
	t0 = time.monotonic()
	try:
		detail = fn(mode) or {}
		ok = all(v for k, v in detail.items() if k.startswith("ok_"))
	except Exception:
		detail, ok = {"error": traceback.format_exc()[-1500:]}, False
	RESULT["checks"].append(
		{"name": name, "mode": mode, "ok": ok, "runtime_s": round(time.monotonic() - t0, 3), **detail}
	)


def _close(peer, io):
	try:
		io.close_graceful(timeout_s=1.0)
	finally:
		peer.stop()


def whole_frame_deadline(mode):
	cli, srv, _ = _pair(mode, bufsize=4096)
	peer = tp.Peer(srv, rate=100_000, chunk=4096)
	peer.start()
	io = _SocketIO(tp.client_ws(cli))
	payload = json.dumps({"blob": "x" * 65536})
	t0 = time.monotonic()
	err = None
	try:
		io.write(payload, lock=None, write_s=0.2)
	except SendUncertain as e:
		err = e.code
	elapsed = time.monotonic() - t0
	time.sleep(1.0)
	out = {
		"elapsed_s": round(elapsed, 3),
		"error_code": err,
		"ok_uncertain": err == "ack-timeout",
		"ok_bounded": elapsed <= 0.35,
		"ok_no_frame": peer.messages == [],
		"ok_dead": io.dead,
	}
	_close(peer, io)
	cli2, srv2, _ = _pair(mode, bufsize=4096)
	peer2 = tp.Peer(srv2, rate=100_000, chunk=4096)
	peer2.start()
	io2 = _SocketIO(tp.client_ws(cli2))
	io2.write(payload, lock=None, write_s=5.0)
	end = time.monotonic() + 5
	while not peer2.messages and time.monotonic() < end:
		time.sleep(0.01)
	out["ok_exact_with_budget"] = bool(peer2.messages) and tp.sha(peer2.messages[0][2]) == tp.sha(payload)
	_close(peer2, io2)
	return out


def quiet_slice_and_idle_writes(mode):
	cli, srv, _ = _pair(mode)
	peer = tp.Peer(srv)
	peer.start()
	io = _SocketIO(tp.client_ws(cli))
	t0 = time.monotonic()
	quiet = io.read(0.5)
	slice_s = time.monotonic() - t0
	stop = threading.Event()

	def reader():
		while not stop.is_set():
			try:
				io.read(0.5)
			except AgentUnreachableError:
				return

	t = threading.Thread(target=reader, daemon=True)
	t.start()
	time.sleep(0.6)
	lock = threading.Lock()
	durations = []
	for i in range(20):
		time.sleep(0.02)
		a = time.monotonic()
		io.write(json.dumps({"type": "req", "id": str(i), "method": "x"}), lock=lock)
		durations.append(time.monotonic() - a)
	stop.set()
	t.join(2)
	out = {
		"quiet_slice_s": round(slice_s, 3),
		"max_write_s": round(max(durations), 4),
		"ok_quiet_none": quiet is None,
		"ok_slice": 0.45 <= slice_s <= 0.7,
		"ok_idle_writes": max(durations) <= 0.10,
	}
	_close(peer, io)
	return out


def two_frames_one_record(mode):
	cli, srv, _ = _pair(mode)
	peer = tp.Peer(srv)
	peer.start()
	io = _SocketIO(tp.client_ws(cli))
	peer.send_raw(tp.server_frame(tp.OP_TEXT, '{"n":1}') + tp.server_frame(tp.OP_TEXT, '{"n":2}'))
	first = io.read(1.0)
	t1 = time.monotonic()
	second = io.read(1.0)
	gap = time.monotonic() - t1
	out = {
		"gap_s": round(gap, 4),
		"ok_both": (first or {}).get("n") == 1 and (second or {}).get("n") == 2,
		"ok_fast": gap <= 0.05,
	}
	_close(peer, io)
	return out


def trickled_partial_frame(mode):
	cli, srv, _ = _pair(mode)
	peer = tp.Peer(srv)
	peer.start()
	io = _SocketIO(tp.client_ws(cli))
	body = json.dumps({"blob": "t" * 60000})
	frame = tp.server_frame(tp.OP_TEXT, body)
	for i in range(0, len(frame), 3000):
		peer.send_raw(frame[i : i + 3000], delay=0.1 + i / 3000 * 0.12)
	got, longest = None, 0.0
	end = time.monotonic() + 6
	while got is None and time.monotonic() < end:
		a = time.monotonic()
		got = io.read(0.5)
		longest = max(longest, time.monotonic() - a)
	out = {
		"longest_read_s": round(longest, 3),
		"ok_intact": (got or {}).get("blob") == "t" * 60000,
		"ok_bounded_reads": longest <= 0.6 + 0.5,
	}  # readiness wait (0.5) + one read op (0.5)
	_close(peer, io)
	return out


def close_during_write(mode):
	cli, srv, _ = _pair(mode, bufsize=65536)
	peer = tp.Peer(srv)
	peer.paused.set()
	peer.start()
	io = _SocketIO(tp.client_ws(cli))
	box = {}

	def writer():
		try:
			io.write(json.dumps({"blob": "w" * 3_000_000}), lock=None, write_s=3.0)
		except BaseException as e:
			box["e"] = type(e).__name__

	w = threading.Thread(target=writer, daemon=True)
	w.start()
	time.sleep(0.3)
	violations = []
	stop = threading.Event()

	def poll():
		while not stop.is_set():
			if io._busy and io._fd_closed:
				violations.append(1)
			time.sleep(0.005)

	p = threading.Thread(target=poll, daemon=True)
	p.start()
	socket_io.IO_GATE_WAIT_S, saved = 0.3, socket_io.IO_GATE_WAIT_S
	try:
		io.close_graceful()
	finally:
		socket_io.IO_GATE_WAIT_S = saved
	w.join(5)
	stop.set()
	p.join(1)
	out = {
		"writer": box.get("e"),
		"ok_uncertain": box.get("e") == "SendUncertain",
		"ok_no_close_under_writer": not violations,
		"ok_closed_once": io._fd_closed,
	}
	peer.stop()
	return out


def peer_eof(mode):
	cli, srv, _ = _pair(mode)
	io = _SocketIO(tp.client_ws(cli))
	box = {}

	def reader():
		try:
			while True:
				io.read(0.5)
		except AgentUnreachableError:
			box["at"] = time.monotonic()

	t = threading.Thread(target=reader, daemon=True)
	t.start()
	time.sleep(0.3)
	closed = time.monotonic()
	srv.shutdown(socket.SHUT_RDWR)
	t.join(3)
	err = None
	try:
		io.write("after-eof", lock=None)
	except SendNotStarted as e:
		err = e.code
	out = {
		"ok_reader_exit": box.get("at", 99) - closed <= 0.6,
		"ok_next_not_started": err == "send-not-started",
	}
	io.close_graceful(timeout_s=0.5)
	return out


def finalize_while_waiting(mode):
	cli, srv, _ = _pair(mode)
	peer = tp.Peer(srv)
	peer.start()
	io = _SocketIO(tp.client_ws(cli))
	box = {}

	def reader():
		try:
			while True:
				io.read(0.5)
		except AgentUnreachableError:
			box["at"] = time.monotonic()

	t = threading.Thread(target=reader, daemon=True)
	t.start()
	time.sleep(0.3)
	t0 = time.monotonic()
	io.close_graceful()
	t.join(2)
	pairs = [socket.socketpair() for _ in range(20)]
	for i, (a, _b) in enumerate(pairs):
		a.sendall(b"mine-%d" % i)
	time.sleep(0.6)
	theft = 0
	for i, (_a, b) in enumerate(pairs):
		b.settimeout(1.0)
		theft += b.recv(64) != b"mine-%d" % i
	for a, b in pairs:
		a.close()
		b.close()
	peer.stop()
	return {
		"ok_reader_exit": box.get("at", 99) - t0 <= 1.0,
		"ok_closed": io._fd_closed,
		"ok_no_theft": theft == 0,
	}


def abort_no_plaintext(mode):
	cli, srv, rel = _pair(mode, relay=True)
	peer = tp.Peer(srv)
	peer.start()
	ws = tp.client_ws(cli, zero_mask=True)
	io = _SocketIO(ws)
	box = {"n": 0}

	def writer():
		try:
			while True:
				io.write(json.dumps({"m": "PLAINTEXT-MARKER-773-" + "x" * 2000}), lock=None)
				box["n"] += 1
		except BaseException as e:
			box["w"] = time.monotonic()
			box["we"] = type(e).__name__

	def reader():
		try:
			while True:
				io.read(0.5)
		except BaseException:
			box["r"] = time.monotonic()

	w, r = threading.Thread(target=writer, daemon=True), threading.Thread(target=reader, daemon=True)
	w.start()
	r.start()
	time.sleep(0.4)
	t0 = time.monotonic()
	io.abort()
	w.join(3)
	r.join(3)
	time.sleep(0.3)
	wire = bytes(rel.c2s)
	i, records = 0, True
	while i + 5 <= len(wire):
		if wire[i] not in (0x14, 0x15, 0x16, 0x17):
			records = False
			break
		i += 5 + int.from_bytes(wire[i + 3 : i + 5], "big")
	out = {
		"frames": box["n"],
		"ok_no_plaintext": b"PLAINTEXT-MARKER" not in wire,
		"ok_tls_records": records,
		"ok_sslobj_kept": getattr(cli, "_sslobj", None) is not None,
		"ok_writer_woke": box.get("w", 99) - t0 < 0.5,
		"ok_reader_woke": box.get("r", 99) - t0 < 0.5,
	}
	io.close_graceful(timeout_s=0.5)
	peer.stop()
	rel.close()
	return out


def main() -> int:
	global CERT
	if ARGS.expect_version and websocket.__version__ != ARGS.expect_version:
		RESULT["checks"].append({"name": "version", "ok": False, "found": websocket.__version__})
	CERT = tp.TempCert()
	t0 = time.monotonic()
	try:
		for mode in ("plain", "tls13"):
			check("whole_frame_deadline", mode, whole_frame_deadline)
			check("quiet_slice_and_idle_writes", mode, quiet_slice_and_idle_writes)
			check("two_frames_one_record", mode, two_frames_one_record)
			check("trickled_partial_frame", mode, trickled_partial_frame)
			check("close_during_write", mode, close_during_write)
			check("peer_eof", mode, peer_eof)
			check("finalize_while_waiting", mode, finalize_while_waiting)
		check("abort_no_plaintext", "tls13", abort_no_plaintext)
	finally:
		for s in OPEN:
			try:
				s.close()
			except OSError:
				pass
		CERT.cleanup()
	RESULT["runtime_s"] = round(time.monotonic() - t0, 2)
	RESULT["ok"] = all(c["ok"] for c in RESULT["checks"])
	RESULT["cert_dir_removed"] = not os.path.exists(CERT.dir)
	with open(ARGS.out, "w") as f:
		json.dump(RESULT, f, indent=1)
	return 0 if RESULT["ok"] else 1


if __name__ == "__main__":
	sys.exit(main())
