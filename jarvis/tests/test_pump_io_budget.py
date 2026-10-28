"""#773: the pump's write path, its lease budget and what the user is told.

Driven like ``test_pump``: the real ``turn_state`` CAS library on the private test
database plus a transport. The deferral tests use ``test_relay_mux._DoubleGateway``
(frames only); the failure-routing tests use the real ``AgentSession`` and
``RelayMux`` over a local TLS socket pair, because only a real socket can show a
write that never started, or one that started and stalled.

What is checked is what a customer and the lease depend on:
  * a slice that cannot finish a write inside its lease writes NOTHING: no CAS, no
    ``run:start``, no RPC; the turn stays ``ready`` under the same key and is sent
    on the next slice after a forced renew;
  * a send that provably never started is a definite, honest error;
  * a send that started and failed is AMBIGUOUS: the turn parks ``recovering``, the
    user never reads "No changes were made", later turns stay ``ready``, and the
    successor hop adopts the turn under the same key without sending it again.
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import patch

import frappe

from jarvis.chat import pump, relay_mux
from jarvis.chat import turn_state as ts
from jarvis.chat.agent_client import AgentSession
from jarvis.chat.relay_mux import RelayMux
from jarvis.tests import _tls_pair as tp
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests.test_pump import PUMP, _PumpTestCase

MB = 1024 * 1024


def _wait_for(predicate, timeout_s: float) -> bool:
	end = time.monotonic() + timeout_s
	while time.monotonic() < end:
		if predicate():
			return True
		time.sleep(0.01)
	return bool(predicate())


def setUpModule():
	install_synthetic_runtime_profile()


class _PublishLog:
	def __init__(self):
		self.events: list[tuple[str, dict]] = []

	def __call__(self, user, event, **fields):
		self.events.append((event, fields))

	def for_run(self, run_id: str) -> list[tuple[str, dict]]:
		return [(e, f) for e, f in self.events if f.get("run_id") == run_id]

	def names(self, run_id: str) -> list[str]:
		return [e for e, _f in self.for_run(run_id)]


class _IoPumpCase(_PumpTestCase):
	"""Adds ready-turn seeding, write recording on the frame double, and a real
	socket-backed mux. Sockets close after the base class has stopped every mux."""

	def setUp(self):
		super().setUp()
		self._cert: tp.TempCert | None = None
		self._peers: list[tp.Peer] = []
		self._socks: list = []

	def tearDown(self):
		super().tearDown()
		for p in self._peers:
			p.stop()
		for s in self._socks:
			try:
				s.close()
			except OSError:
				pass
		if self._cert is not None:
			self._cert.cleanup()

	def _seed_ready(self, conv, rid, message="hi"):
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
		self._mk_turn(
			conv,
			rid,
			seed,
			"ready",
			version=2,
			reserved=1,
			assistant_message=amsg,
			ready_at=frappe.utils.now(),
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": message}),
		)
		return amsg

	def _settled(self, rid) -> bool:
		"""The run's terminal was observed (settlement may already have moved on)."""
		return self._state(rid) in ("terminal_observed", "finalizing", "done")

	def _row(self, rid) -> tuple:
		return (self._state(rid), int(self._val(rid, "version")), self._val(rid, "dispatch_payload"))

	def _recording_double(self):
		double = self._double()
		writes: list[dict] = []
		original = double._ws._on_send

		def record(payload):
			writes.append(json.loads(payload))
			original(payload)

		double._ws._on_send = record
		return double, writes

	def _socket_mux(self, *, mode="tls13", bufsize=None, on_message=None, **peer_kw):
		if self._cert is None:
			self._cert = tp.TempCert()
		cli, srv, _ = tp.make_pair(mode, self._cert, bufsize=bufsize)
		self._socks.append(srv)
		peer = tp.Peer(srv, on_message=on_message, **peer_kw)
		self._peers.append(peer)
		peer.start()
		sess = AgentSession(tp.client_ws(cli), None)
		return sess, peer

	def _socket_deps(self, sess):
		deps = self._deps()

		def factory(target, epoch):
			mux = RelayMux(sess, target).start()
			self._muxes.append(mux)
			return mux

		deps.make_mux = factory
		return deps


class TestDeferralBeforeAnySideEffect(_IoPumpCase):
	"""AC-P1: a slice whose lease cannot cover one more full write (wait 5 + write 10
	+ slack 1 inside the lease minus margin and tail reserve) must write nothing at
	all. Before #773 the pump dispatched regardless of lease age."""

	def test_unadmitted_slice_has_no_effect_then_next_slice_sends_original_keys(self):
		conv = self._mk_conv()
		rids = ["pmp_def1", "pmp_def2", "pmp_def3"]
		for rid in rids:
			self._seed_ready(conv, rid)
		double, writes = self._recording_double()
		ctx = self._make_ctx(self._deps(double=double))
		before = {rid: self._row(rid) for rid in rids}
		expires_before = frappe.db.get_value(PUMP, self._target, "lease_expires_at")
		# The lease was granted ~20s ago: 10s of local validity remain, less than
		# one write needs.
		ctx.lease_valid_until = pump._monotonic() + 10.0
		published = _PublishLog()
		time.sleep(0.05)
		with patch.object(ts, "publish_fenced", side_effect=published):
			pump.drain_slice(ctx)
		self.assertEqual(writes, [], "nothing may be written while the lease cannot cover the write")
		for rid in rids:
			self.assertEqual(self._row(rid), before[rid], f"{rid} changed without a write")
			self.assertEqual(published.for_run(rid), [], f"{rid} published without a write")
		expires_after = frappe.db.get_value(PUMP, self._target, "lease_expires_at")
		self.assertGreater(
			frappe.utils.get_datetime(expires_after),
			frappe.utils.get_datetime(expires_before),
			"the deferral forces a renew even inside the 10s heartbeat interval",
		)
		with patch.object(ts, "publish_fenced", side_effect=published):
			pump.drain_slice(ctx)
		keys = [w["params"]["idempotencyKey"] for w in writes if w.get("method") == "chat.send"]
		self.assertEqual(
			sorted(keys), rids, "the next slice sends every deferred turn once, with its own key"
		)
		for rid in rids:
			self.assertNotEqual(self._state(rid), "ready")


class TestSendNotStartedAfterClaim(_IoPumpCase):
	"""AC-P3: the send lock stays busy past the 5s wait after the turn was claimed.
	No byte was written, so the definite error is honest: ``errored``, ``run:error``
	with ``changed_data=False`` and code ``unreachable``. No ghost run or lane is
	left, the transport is not poisoned, and the next turn of the wave goes out."""

	def test_not_started_errors_honestly_and_the_wave_continues(self):
		conv = self._mk_conv()
		first, second = "pmp_ns1", "pmp_ns2"
		self._seed_ready(conv, first)
		self._seed_ready(conv, second)
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))
		held = threading.Event()

		def hold_lock():
			with sess._lock:
				held.set()
				time.sleep(6.5)

		holder = threading.Thread(target=hold_lock, daemon=True)
		holder.start()
		self.assertTrue(held.wait(2))
		published = _PublishLog()
		with patch.object(ts, "publish_fenced", side_effect=published):
			pump._dispatch_ready(ctx)
		holder.join(10)
		self.assertEqual(self._state(first), "errored", "a send that never started is a definite error")
		errors = [f for e, f in published.for_run(first) if e == "run:error"]
		self.assertEqual(len(errors), 1)
		self.assertIs(errors[0].get("changed_data"), False)
		self.assertEqual(errors[0].get("code"), "unreachable")
		self.assertNotIn(first, ctx.runs, "ghost run left in the pump context")
		self.assertNotIn(first, ctx.mux._runs, "ghost lane left in the mux")
		self.assertFalse(ctx.mux.is_closed())
		self.assertEqual(self._state(second), "dispatching", "the rest of the wave is unaffected")
		self.assertTrue(_wait_for(lambda: tp.chat_sends(peer), 5))
		self.assertEqual([f["params"]["idempotencyKey"] for f in tp.chat_sends(peer)], [second])


class TestSendUncertainMidWave(_IoPumpCase):
	"""AC-P4/AC-K6: turn 2 of 4 stalls mid-frame. Its write started, so the peer may
	hold the whole frame: the turn parks ``recovering`` (banner), never errors with
	"No changes were made", turns 3 and 4 stay ``ready``, and the hop ends. The
	successor adopts turn 2 under its SAME key without sending it again, and sends
	3 and 4 once each.

	Why a socket error after the write started is not "not delivered": CPython's
	``sock_sendall`` (Modules/socketmodule.c, v3.14.0) runs ``PyErr_CheckSignals()``
	after the last chunk is accepted and before it returns, so a signal handler's
	exception (``TimeoutError`` included) can surface after the kernel took every
	byte; and the eval loop can run a handler after any send returns. Only a send
	that never started is a definite failure."""

	def _drain_until(self, ctx, predicate, slices=40):
		for _ in range(slices):
			outcome = pump.drain_slice(ctx)
			if predicate() or outcome in ("idle_exit", "transport_closed"):
				return outcome
		return "timeout"

	def test_stalled_second_turn_parks_and_successor_adopts_same_key(self):
		conv = self._mk_conv()
		rids = ["pmp_unc1", "pmp_unc2", "pmp_unc3", "pmp_unc4"]
		for rid in rids:
			self._seed_ready(conv, rid, message="F" * MB if rid == "pmp_unc2" else "hi")
		before = {rid: self._row(rid) for rid in rids}
		base = tp.rpc_responder(chat_final=False)

		def ack_first_then_stop_reading(peer, raw):
			base(peer, raw)
			if json.loads(raw).get("method") == "chat.send":
				peer.paused.set()

		sess, peer = self._socket_mux(bufsize=65536, on_message=ack_first_then_stop_reading)
		deps = self._socket_deps(sess)
		ctx = self._make_ctx(deps)
		published = _PublishLog()
		with (
			patch.object(ts, "publish_fenced", side_effect=published),
			patch.object(relay_mux, "IO_WRITE_TIMEOUT_S", 1.5, create=True),
		):
			outcome = self._drain_until(ctx, lambda: self._state("pmp_unc2") != "ready")
			if outcome not in ("transport_closed",):
				outcome = self._drain_until(ctx, lambda: False, slices=3)
		self.assertEqual(self._state("pmp_unc2"), "recovering", "a write that started is ambiguous")
		self.assertNotIn(
			"run:error", published.names("pmp_unc2"), "never a definite error after the write started"
		)
		self.assertIn("run:recovering", published.names("pmp_unc2"))
		self.assertFalse(
			any("changed_data" in f for _e, f in published.for_run("pmp_unc2")),
			"never claim nothing changed",
		)
		for rid in ("pmp_unc3", "pmp_unc4"):
			self.assertEqual(self._row(rid), before[rid], f"{rid} must stay ready with its key and version")
		self.assertEqual(outcome, "transport_closed")
		self.assertNotIn("pmp_unc2", ctx.runs)

		# The successor hop on a fresh connection.
		pump._schedule_successor_on_exit(ctx, transport_retry=0)
		sess2, peer2 = self._socket_mux(on_message=tp.rpc_responder(chat_final=True))
		ctx2 = self._make_ctx(self._socket_deps(sess2), holder="hop-test-2", hop_counter=1)
		pump._reconcile_on_start(ctx2)
		self.assertEqual(self._state("pmp_unc2"), "streaming", "the successor adopts the parked turn")
		self.assertIn("pmp_unc2", ctx2.mux._runs, "its lane is registered under the SAME send key")
		self._drain_until(ctx2, lambda: all(self._settled(r) for r in ("pmp_unc3", "pmp_unc4")))
		self.assertTrue(all(self._settled(r) for r in ("pmp_unc3", "pmp_unc4")))
		keys2 = [f["params"]["idempotencyKey"] for f in tp.chat_sends(peer2)]
		self.assertEqual(sorted(keys2), ["pmp_unc3", "pmp_unc4"], "turn 2 is never sent again")
		self.assertEqual([f["params"]["idempotencyKey"] for f in tp.chat_sends(peer)], ["pmp_unc1"])
		# The gateway's answer for turn 2 arrives on the adopted lane.
		peer2.send_json(
			{
				"type": "event",
				"event": "chat",
				"payload": {
					"runId": "pmp_unc2",
					"sessionKey": "sess-pmp_unc2",
					"state": "final",
					"message": {"content": [{"type": "text", "text": "adopted"}]},
				},
			}
		)
		self._drain_until(ctx2, lambda: self._settled("pmp_unc2"))
		self.assertTrue(self._settled("pmp_unc2"), self._state("pmp_unc2"))


# --------------------------------------------------------------------------- #
# Lease loss, cancel, cold start, capacity refresh, throughput
# --------------------------------------------------------------------------- #


class TestLeaseLoss(_IoPumpCase):
	"""AC-P2: a lost lease never writes. A takeover bumped the shard epoch and the
	local bound has passed: nothing is written and the slice-end renew exits. At hop
	start, a renew that finds the lease gone (a slow connect) exits before ANY
	gateway write, the cold snapshot included."""

	def test_passed_bound_and_moved_epoch_write_nothing(self):
		conv = self._mk_conv()
		self._seed_ready(conv, "pmp_lost1")
		double, writes = self._recording_double()
		ctx = self._make_ctx(self._deps(double=double))
		frappe.db.set_value(PUMP, self._target, "pump_epoch", ctx.epoch + 1, update_modified=False)
		frappe.db.commit()
		ctx.lease_valid_until = pump._monotonic() - 1.0
		with self.assertRaises(ts.LeaseLostExit):
			pump.drain_slice(ctx)
		self.assertEqual(writes, [])
		self.assertEqual(self._state("pmp_lost1"), "ready")

	def test_hop_start_renew_failure_exits_before_any_write(self):
		conv = self._mk_conv()
		self._seed_ready(conv, "pmp_lost2")
		double, writes = self._recording_double()
		deps = self._deps(double=double)
		deps.snapshot = pump._default_snapshot  # a cold snapshot would write sessions.list
		with patch.object(ts, "lease_renew", return_value=False):
			result = pump.run_pump_hop(self._target, deps=deps, max_slices=3)
		self.assertEqual(result["exit"], "lease_lost")
		self.assertEqual(writes, [], "no gateway write under a lease that may be gone")
		self.assertEqual(self._state("pmp_lost2"), "ready")


class TestCancelDeferral(_IoPumpCase):
	"""AC-P5: without lease time for the ``chat.abort`` write, a Stop is not recorded
	locally (the gateway was never asked to stop): ``cancel_requested`` stays set.
	The next slice with lease time sends the abort and settles the turn."""

	def test_unadmitted_cancel_waits_for_the_next_slice(self):
		conv = self._mk_conv()
		rid = "pmp_cdef"
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
		double, writes = self._recording_double()
		ctx = self._make_ctx(self._deps(double=double))
		self._mk_turn(
			conv,
			rid,
			seed,
			"streaming",
			version=4,
			pump_epoch=ctx.epoch,
			reserved=1,
			assistant_message=amsg,
			gateway_run_id=rid,
			dispatching_at=frappe.utils.now(),
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		self.assertTrue(ts.request_cancel(rid, 4))
		frappe.db.commit()
		ctx.lease_valid_until = pump._monotonic() + 10.0
		self.assertEqual(pump._cancel_sweep(ctx), 0)
		self.assertEqual(writes, [])
		self.assertEqual(self._state(rid), "streaming")
		self.assertEqual(int(self._val(rid, "cancel_requested")), 1)
		self.assertTrue(ctx.io_deferred)
		ctx.lease_valid_until = pump._monotonic() + 30.0
		self.assertEqual(pump._cancel_sweep(ctx), 1)
		self.assertEqual([w["method"] for w in writes], ["chat.abort"])
		self.assertEqual(self._state(rid), "cancelled")


class TestColdStart(_IoPumpCase):
	"""AC-P6: the cold-start snapshot is a lease-admitted write with a bounded wait;
	the reconcile renews (R1) after it and before its row loop; a missed-terminal
	recovery tail without lease time falls back to re-attaching the lane."""

	def test_snapshot_without_lease_time_writes_nothing(self):
		double, writes = self._recording_double()
		ctx = self._make_ctx(self._deps(double=double))
		ctx.lease_valid_until = pump._monotonic() + 10.0
		snap = pump._default_snapshot(ctx)
		self.assertFalse(snap["snapshot_ok"])
		self.assertEqual(writes, [])

	def test_snapshot_wait_is_bounded(self):
		double, writes = self._recording_double()
		double.arm_sessions_list_hang()
		ctx = self._make_ctx(self._deps(double=double))
		ctx.lease_valid_until = pump._monotonic() + 30.0
		t0 = time.monotonic()
		with patch.object(pump, "ACK_TIMEOUT_S", 0.5):
			snap = pump._default_snapshot(ctx)
		self.assertLess(time.monotonic() - t0, 1.5)
		self.assertFalse(snap["snapshot_ok"])
		self.assertEqual([w["method"] for w in writes], ["sessions.list"])

	def test_renew_precedes_the_row_loop(self):
		conv = self._mk_conv()
		rid = "pmp_r1"
		seed = self._mk_msg(conv)
		double, _writes = self._recording_double()
		ctx = self._make_ctx(self._deps(double=double))
		self._mk_turn(
			conv,
			rid,
			seed,
			"streaming",
			version=3,
			pump_epoch=ctx.epoch,
			reserved=1,
			gateway_run_id=rid,
			dispatching_at=frappe.utils.now(),
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		order: list[str] = []
		real_renew, real_one = ts.lease_renew, pump._reconcile_one
		ctx.lease_valid_until = pump._monotonic() + 12.0
		with (
			patch.object(
				ts, "lease_renew", side_effect=lambda *a, **k: order.append("renew") or real_renew(*a, **k)
			),
			patch.object(
				pump, "_reconcile_one", side_effect=lambda *a, **k: order.append("row") or real_one(*a, **k)
			),
		):
			pump._reconcile_on_start(ctx)
		self.assertEqual(order[:2], ["renew", "row"])
		self.assertGreater(ctx.lease_valid_until, pump._monotonic() + 25)

	def test_unadmitted_recovery_tail_reattaches(self):
		conv = self._mk_conv()
		rid = "pmp_tail"
		seed = self._mk_msg(conv)
		double, writes = self._recording_double()
		ctx = self._make_ctx(self._deps(double=double))
		self._mk_turn(
			conv,
			rid,
			seed,
			"streaming",
			version=3,
			pump_epoch=ctx.epoch,
			reserved=1,
			last_event_seq=4,
			gateway_run_id=rid,
			dispatching_at=frappe.utils.now(),
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		ctx.lease_valid_until = pump._monotonic() + 10.0
		r = frappe.db.get_value(
			"Jarvis Chat Turn",
			rid,
			[
				"run_id",
				"state",
				"version",
				"conversation",
				"assistant_message",
				"last_event_seq",
				"gateway_run_id",
			],
			as_dict=True,
		)
		pump._reattach_or_recover(ctx, r, set())
		self.assertEqual(writes, [], "no sessions.get without lease time")
		self.assertEqual(ctx.pending_recoveries, {})
		self.assertIn(rid, ctx.mux._runs, "the lane is re-attached instead")


class TestCapacityRefreshDeferral(_IoPumpCase):
	"""AC-P7: a due capacity refresh without lease time neither folds a failed
	snapshot (that would fail capacity closed) nor stamps its cadence."""

	def test_deferred_refresh_has_no_effect(self):
		double, writes = self._recording_double()
		ctx = self._make_ctx(self._deps(double=double))
		ctx.last_snapshot_mono = 0.0
		known = ctx.gateway_active_known
		ctx.lease_valid_until = pump._monotonic() + 10.0
		pump._maybe_refresh_capacity(ctx)
		self.assertEqual(writes, [])
		self.assertIsNone(ctx.pending_snapshot)
		self.assertEqual(ctx.last_snapshot_mono, 0.0)
		self.assertEqual(ctx.gateway_active_known, known)
		ctx.lease_valid_until = pump._monotonic() + 30.0
		pump._maybe_refresh_capacity(ctx)
		self.assertEqual([w["method"] for w in writes], ["sessions.list"])
		self.assertIsNotNone(ctx.pending_snapshot)


class TestWaveThroughput(_IoPumpCase):
	"""AC-P8: on a fresh lease, a full wave of 50 small ready turns goes out in ONE
	slice; with an 8s-old lease (too old to admit a write) it goes out within two
	slices: one deferral, one forced renew."""

	def _seed_wave(self):
		conv = self._mk_conv()
		rids = [f"pmp_w{i:02d}" for i in range(50)]
		for rid in rids:
			self._seed_ready(conv, rid)
		return rids

	def _sent(self, writes):
		return sorted(w["params"]["idempotencyKey"] for w in writes if w.get("method") == "chat.send")

	def test_fresh_lease_sends_the_wave_in_one_slice(self):
		rids = self._seed_wave()
		double, writes = self._recording_double()
		ctx = self._make_ctx(self._deps(double=double))
		ctx.lease_valid_until = pump._monotonic() + 30.0
		self.assertEqual(pump._dispatch_ready(ctx), 50)
		self.assertEqual(self._sent(writes), rids)
		self.assertFalse(ctx.io_deferred)

	def test_old_lease_sends_the_wave_within_two_slices(self):
		rids = self._seed_wave()
		double, writes = self._recording_double()
		ctx = self._make_ctx(self._deps(double=double))
		ctx.lease_valid_until = pump._monotonic() + 22.0  # acquired 8s ago
		pump.drain_slice(ctx)
		self.assertEqual(self._sent(writes), [])
		pump.drain_slice(ctx)
		self.assertEqual(self._sent(writes), rids)


# --------------------------------------------------------------------------- #
# Ack window (legacy parity)
# --------------------------------------------------------------------------- #


class TestAckWindow(_IoPumpCase):
	"""AC-R6/AC-K7: the ``chat.send`` ack window starts AFTER the whole frame was
	written, and its length follows the payload the way the legacy path sizes it
	(30s base, +30s per vision part, ceiling 180s). A payload stored before the key
	existed keeps the 15s window."""

	def _dispatch_with(self, rid, extra: dict):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
		self._mk_turn(
			conv,
			rid,
			seed,
			"ready",
			version=2,
			reserved=1,
			assistant_message=amsg,
			ready_at=frappe.utils.now(),
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi", **extra}),
		)
		double, _writes = self._recording_double()
		double.arm(rid, "ack-timeout")  # the ack never comes inside the test
		ended: list[float] = []
		send = double._ws._on_send

		def slow_send(payload):
			time.sleep(1.0)  # a 1s frame write
			send(payload)
			ended.append(pump._monotonic())

		double._ws._on_send = slow_send
		ctx = self._make_ctx(self._deps(double=double))
		self.assertEqual(pump._dispatch_ready(ctx), 1)
		return ctx.pending_acks[rid].deadline - ended[-1]

	def test_window_starts_after_the_write_and_old_payloads_keep_15s(self):
		window = self._dispatch_with("pmp_ack_old", {})
		self.assertGreaterEqual(window, pump.ACK_TIMEOUT_S - 0.05)
		self.assertLessEqual(window, pump.ACK_TIMEOUT_S + 0.05)

	def test_stored_window_is_used(self):
		from jarvis.chat import turn_handler

		stored = turn_handler._ack_timeout_s(3, 0)
		self.assertEqual(stored, 120.0)
		window = self._dispatch_with("pmp_ack_new", {"ack_timeout_s": stored})
		self.assertAlmostEqual(window, 120.0, delta=0.05)

	def test_formula_matches_legacy(self):
		from jarvis.chat import turn_handler

		self.assertEqual([turn_handler._ack_timeout_s(n, 0) for n in (0, 3, 6)], [30.0, 120.0, 180.0])
		self.assertEqual(pump._ack_window_s({}), 15.0)
		self.assertEqual(pump._ack_window_s({"ack_timeout_s": None}), 15.0)
		self.assertEqual(pump._ack_window_s({"ack_timeout_s": 180.0}), 180.0)


from jarvis.tests import test_pump_pipeline as _pipeline


class TestPrepareStoresAckWindow(_pipeline._PipelineCase):
	"""AC-K7: prepare stores the size-based window in the dispatch payload."""

	def test_payload_carries_the_legacy_window(self):
		from jarvis.chat import prepare, turn_handler

		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="hi")
		rid = "pmp_ack_prep"
		self._mk_turn(conv, rid, seed, "queued", version=1, reserved=1, dispatch_payload=json.dumps({}))
		with self._gateway(_pipeline._FakeSess()), self._pump_on():
			prepare.run_prepare(rid, self._target)
		self.assertEqual(self._state(rid), "ready")
		payload = json.loads(self._val(rid, "dispatch_payload"))
		self.assertEqual(payload["ack_timeout_s"], turn_handler._ack_timeout_s(0, 0))


# --------------------------------------------------------------------------- #
# Shared chats
# --------------------------------------------------------------------------- #

from jarvis.tests import test_chat_collaboration_pump as _collab


class TestSharedDispatchDeferral(_collab.TestCollaborationPump):
	"""F-13 (shared chats): the preflight ``sessions.patch`` keeps its parameters; a
	slice without lease time issues nothing and claims nothing; a verified
	preflight survives a deferral and is used by the next slice (no second patch);
	an immediate send failure leaves no run behind. Only the tests defined here run
	(see ``load_tests``); the parent's own suite runs in its module."""

	def test_preflight_params_unchanged_and_kept_across_a_deferral(self):
		self.start_check()
		method, params = self.mux.issue_rpc.call_args.args[:2]
		self.assertEqual(method, "sessions.patch")
		self.assertEqual(params, {"key": self.bound.session_key, "expectedSessionId": "physical-1"})
		version = int(frappe.db.get_value(ts.TURN, self.turn.name, "version"))
		self.context.lease_valid_until = pump._monotonic() + 10.0
		self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		self.mux.send_chat.assert_not_called()
		self.assertIn(self.turn.name, self.context.shared_preflights)
		self.assertTrue(self.context.io_deferred)
		row = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertEqual((row.state, int(row.version)), ("ready", version))
		self.assertFalse(row.collaboration_send_started_at)
		self.context.lease_valid_until = pump._monotonic() + 30.0
		patches = self.mux.issue_rpc.call_count
		self.assertTrue(pump._dispatch_one(self.context, self.turn.name))
		self.assertEqual(self.mux.issue_rpc.call_count, patches, "the kept preflight is used")
		self.assertEqual(self.mux.send_chat.call_args.kwargs["verified_session_id"], "physical-1")

	def test_no_preflight_without_lease_time(self):
		self.context.lease_valid_until = pump._monotonic() + 10.0
		self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		self.mux.issue_rpc.assert_not_called()
		self.assertNotIn(self.turn.name, self.context.shared_preflights)
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "ready")

	def test_immediate_uncertain_send_parks_and_drops_the_run(self):
		from jarvis.chat.agent_client import SendUncertain

		self.start_check()
		self.mux.send_chat.side_effect = SendUncertain("chat.send may not have been delivered: timed out")
		with patch("frappe.publish_realtime"):
			self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "recovering")
		self.assertNotIn(self.turn.name, self.context.runs)


def load_tests(loader, standard_tests, pattern):
	"""Run only the tests each class here defines itself: the classes reusing another
	suite's fixtures (``TestSharedDispatchDeferral``, ``TestPrepareStoresAckWindow``)
	must not re-run the inherited ones."""
	import unittest

	suite = unittest.TestSuite()

	def walk(tests):
		for t in tests:
			if isinstance(t, unittest.TestSuite):
				walk(t)
			elif t._testMethodName in type(t).__dict__:
				suite.addTest(t)

	walk(standard_tests)
	return suite
