"""#773 AC-P9/F-11: a whole pump hop over a REAL TLS socket, production constants.

``run_pump_hop`` drives a real ``AgentSession`` + ``RelayMux`` over a local TLS 1.3
socket pair to a gateway peer that reads at 1 MB/s, acks every ``chat.send`` and
answers with a final whose text is the SHA-256 of the message it received. Sixteen
2 MB turns make more than 32s of real write IO in one hop: longer than the 30s
lease, three heartbeat intervals. Meanwhile a second thread, on its OWN database
connection, tries to take the lease every second (it must never win), and checks
the lease's database expiry at the end of every write.

Only settlement is doubled (it records the final and marks the turn done); the
lease, admission, deferral, renew, dispatch and ack paths are the real ones.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from unittest.mock import patch

import frappe

from jarvis.chat import pump
from jarvis.chat import turn_state as ts
from jarvis.tests import _tls_pair as tp
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests.test_pump import PUMP
from jarvis.tests.test_pump_io_budget import MB, _IoPumpCase

TURN = "Jarvis Chat Turn"


def setUpModule():
	install_synthetic_runtime_profile()


def _sha_responder(*, stall_after_first: bool = False):
	"""Gateway double: ack each chat.send, answer with a final carrying the sha of
	the message received; optionally stop reading after the first chat.send."""

	def respond(peer: tp.Peer, raw: bytes) -> None:
		frame = json.loads(raw)
		if frame.get("type") != "req":
			return
		params = frame.get("params") or {}
		if frame.get("method") == "chat.send":
			run_id = params["idempotencyKey"]
			peer.send_json(
				{
					"type": "res",
					"id": frame["id"],
					"ok": True,
					"payload": {"runId": run_id, "status": "started"},
				}
			)
			peer.send_json(
				{
					"type": "event",
					"event": "chat",
					"payload": {
						"runId": run_id,
						"sessionKey": params.get("sessionKey"),
						"state": "final",
						"message": {"content": [{"type": "text", "text": tp.sha(params["message"])}]},
					},
				}
			)
			if stall_after_first:
				peer.paused.set()
			return
		payload = {"sessions": []} if frame.get("method") == "sessions.list" else {}
		peer.send_json({"type": "res", "id": frame["id"], "ok": True, "payload": payload})

	return respond


class _Competitor(threading.Thread):
	"""Another worker on its own database connection. Once the hop holds the lease
	(``armed``), it tries ``lease_acquire`` every second; and at every write end it
	reads the lease's database expiry and compares it with the wall clock of that
	write's END. The writer waits for that answer before going on, so a later,
	legitimate release can never be mistaken for an expiry at the write."""

	def __init__(self, site: str, sites_path: str, target: str):
		super().__init__(daemon=True)
		self.site, self.sites_path, self.target = site, sites_path, target
		self.armed = threading.Event()
		self.write_ends: queue.Queue = queue.Queue()
		self.halt = threading.Event()
		self.attempts = 0
		self.win_times: list[float] = []
		self.probes: list[bool] = []
		self.error: str | None = None

	def probe(self, write_end) -> None:
		"""Called on the writing thread at a write's end; returns once probed."""
		done = threading.Event()
		self.write_ends.put((write_end, done))
		if not done.wait(10):
			self.probes.append(False)

	def run(self) -> None:
		frappe.init(site=self.site, sites_path=self.sites_path)
		frappe.connect()
		frappe.flags.in_test = True
		try:
			next_try = None
			while not self.halt.is_set() or not self.write_ends.empty():
				try:
					write_end, done = self.write_ends.get(timeout=0.05)
					frappe.db.commit()
					expiry = frappe.db.get_value(PUMP, self.target, "lease_expires_at")
					self.probes.append(frappe.utils.get_datetime(expiry) > write_end)
					done.set()
				except queue.Empty:
					pass
				if self.halt.is_set() or not self.armed.is_set():
					continue
				if next_try is None:
					next_try = time.monotonic()
				if time.monotonic() >= next_try:
					next_try += 1.0
					self.attempts += 1
					won, _epoch = ts.lease_acquire(self.target, "competitor")
					if won:
						self.win_times.append(time.monotonic())
					frappe.db.commit()
		except Exception as e:
			self.error = f"{type(e).__name__}: {e}"
		finally:
			frappe.db.rollback()
			frappe.destroy()


class TestNativeHopHeartbeat(_IoPumpCase):
	def _run_hop(self, *, turns: int, stall: bool, bufsize=None):
		conv = self._mk_conv()
		rids = [f"pmp_e2e{i:02d}" for i in range(turns)]
		messages = {}
		for i, rid in enumerate(rids):
			messages[rid] = f"turn {i} " + chr(ord("a") + i % 26) * (2 * MB)
			self._seed_ready(conv, rid, message=messages[rid])
		sess, peer = self._socket_mux(
			bufsize=bufsize, on_message=_sha_responder(stall_after_first=stall), rate=1 * MB
		)
		writes: list[dict] = []
		competitor = _Competitor(frappe.local.site, frappe.local.sites_path, self._target)
		real_write = sess._write_frame

		def write_frame(payload, **kw):
			rec = {"method": kw.get("method"), "start": time.monotonic(), "not_after": kw.get("not_after")}
			try:
				return real_write(payload, **kw)
			finally:
				rec["end"] = time.monotonic()
				writes.append(rec)
				competitor.probe(frappe.utils.now_datetime())

		sess._write_frame = write_frame
		finals: dict[str, str] = {}

		def settle(run_id, **kw):
			finals[run_id] = (kw.get("terminal_payload") or {}).get("text")
			frappe.db.sql(
				f"UPDATE `tab{TURN}` SET state='done', reserved=0, version=version+1 "
				"WHERE run_id=%(r)s AND state='terminal_observed'",
				{"r": run_id},
			)
			frappe.db.commit()

		deps = self._socket_deps(sess)
		deps.invoke_settlement = settle
		deps.snapshot = pump._default_snapshot
		renews: list[dict] = []
		real_renew = pump._heartbeat_and_renew

		def heartbeat_and_renew(ctx, progressed):
			now = pump._monotonic()
			due = now - ctx.last_heartbeat >= pump.HEARTBEAT_INTERVAL_S
			if due or ctx.io_deferred:
				renews.append({"at": now, "forced": ctx.io_deferred and not due})
			return real_renew(ctx, progressed)

		released: list[float] = []
		real_acquire, real_release, real_handoff = (
			ts.lease_acquire,
			ts.lease_release_if_idle,
			ts.lease_handoff,
		)
		hop_thread = threading.get_ident()

		def acquire(*a, **k):
			won = real_acquire(*a, **k)
			if threading.get_ident() == hop_thread and won[0]:
				competitor.armed.set()  # contention starts once the hop holds the lease
			return won

		def stamp(real):
			def call(*a, **k):
				# The boundary is the moment before the release: a competitor that
				# wins right after the release commits is legitimate.
				at = time.monotonic()
				won = real(*a, **k)
				if won:
					released.append(at)
				return won

			return call

		competitor.start()
		t0 = time.monotonic()
		try:
			with (
				patch.object(pump, "_heartbeat_and_renew", heartbeat_and_renew),
				patch.object(ts, "lease_acquire", acquire),
				patch.object(ts, "lease_release_if_idle", stamp(real_release)),
				patch.object(ts, "lease_handoff", stamp(real_handoff)),
			):
				result = pump.run_pump_hop(self._target, deps=deps)
			released.append(time.monotonic())
		finally:
			competitor.halt.set()
			competitor.join(15)
		return {
			"result": result,
			"elapsed": time.monotonic() - t0,
			"rids": rids,
			"messages": messages,
			"writes": writes,
			"finals": finals,
			"renews": renews,
			"competitor": competitor,
			"released": released[0],
			"peer": peer,
			"deps": deps,
		}

	def _assert_lease_held(self, run):
		competitor = run["competitor"]
		self.assertIsNone(competitor.error)
		early = [t for t in competitor.win_times if t < run["released"]]
		self.assertEqual(early, [], "another worker took the lease while the hop still held it")
		self.assertGreaterEqual(competitor.attempts, 5)
		self.assertEqual(len(competitor.probes), len(run["writes"]), "every write end was probed")
		self.assertTrue(all(competitor.probes), "the DB lease had expired at a write end")
		for w in run["writes"]:
			self.assertIsNotNone(w["not_after"], f"{w['method']} ran without a lease deadline")
			self.assertLessEqual(w["end"], w["not_after"], f"{w['method']} ended past its lease deadline")

	def test_sixteen_large_turns_outlast_the_lease_and_keep_it(self):
		run = self._run_hop(turns=16, stall=False)
		self.assertEqual(run["result"]["exit"], "idle", run["result"])
		self._assert_lease_held(run)
		sends = [w for w in run["writes"] if w["method"] == "chat.send"]
		self.assertEqual(len(sends), 16, "exactly one chat.send per turn")
		io = sum(w["end"] - w["start"] for w in sends)
		self.assertGreaterEqual(io, 32.0, f"premise: aggregate write IO {io:.1f}s must exceed the 30s lease")
		self.assertTrue(any(r["forced"] for r in run["renews"]), "a deferral must force a renew")
		keys = [f["params"]["idempotencyKey"] for f in tp.chat_sends(run["peer"])]
		self.assertEqual(sorted(keys), run["rids"], "each turn delivered exactly once")
		for f in tp.chat_sends(run["peer"]):
			rid = f["params"]["idempotencyKey"]
			self.assertEqual(
				tp.sha(f["params"]["message"]), tp.sha(run["messages"][rid]), f"{rid} not byte-exact"
			)
		self.assertEqual(
			run["finals"], {rid: tp.sha(m) for rid, m in run["messages"].items()}, "every final came back"
		)
		self.assertLess(run["elapsed"], 60.0)

	def test_stalled_peer_parks_the_turn_and_hands_off(self):
		run = self._run_hop(turns=4, stall=True, bufsize=65536)
		self.assertEqual(run["result"]["exit"], "transport_closed", run["result"])
		self._assert_lease_held(run)
		rids = run["rids"]
		self.assertEqual(self._state(rids[1]), "recovering", "the stalled write is ambiguous")
		self.assertNotEqual(self._state(rids[0]), "ready")
		for rid in rids[2:]:
			self.assertEqual(self._state(rid), "ready", "later turns are never claimed")
		self.assertFalse(self._val(rids[1], "error"), "never a definite error after the write started")
		self.assertGreaterEqual(run["deps"].enqueue_pump_job.count, 1, "a successor hop is enqueued")
		sends = [w for w in run["writes"] if w["method"] == "chat.send"]
		self.assertEqual(len(sends), 2, "the wave stops at the poisoned transport")
		self.assertGreater(sends[1]["end"] - sends[1]["start"], 9.0, "the stalled write had its full budget")
		self.assertLess(run["elapsed"], 60.0)
