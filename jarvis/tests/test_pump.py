"""WP-1c — tests for jarvis.chat.pump (the Relay Pump reactor).

The pump is driven against the REAL ``turn_state`` CAS library on the real DB
(patterntest) plus WP-1b's transport double (``test_relay_mux._DoubleGateway``,
which replays the WP-2 harness transcripts frame-for-frame at the exact
``_recv``/``_send`` seam). Only the socket is doubled; every millisecond of the
mux reader-loop demux and every turn CAS is real. The two WP-1d seams (prepare
dispatcher + settlement/finalize invoker) and the internal enqueue seam are
injected so nothing hits RQ and each hand-off is asserted at the args level.

Coverage (the WP-1c acceptance set):
  * cold-start full lifecycle (queued -> prepare-stub -> ready -> dispatched ->
    streamed -> terminal_observed -> settlement invoked) over fake frames;
  * idle-exit vs concurrent-send race — BOTH D6 §2 orderings;
  * hop handoff (fresh job id, timeout=180, queue=long — asserted, successor NOT
    run);
  * takeover fencing (a second acquire re-stamps the epoch; the old context's
    write affects 0 rows);
  * watchdog per-state matrix (queued age-out, stale-preparing past 300s ->
    fresh prepare, finalizing re-enqueue-only, ensure_pump revive);
  * ack-timeout park vs DEFINITE-rejection errored (PANEL test 7);
  * WS drop w/ outstanding ack -> immediate sentinel -> park (PANEL test 9);
  * cancel flow end-to-end;
  * DB-disconnect bounded backoff -> park recovering -> exit hop;
  * credit lifecycle across promote/dispatch/settle under the shard lock
    (PANEL tests 2/3), and cross-conversation over-admit at promote (PANEL 1);
  * the raw-redis wake bus round-trip.

Each test uses a UNIQUE per-test ``relay_target_id`` so it never touches the
site-wide "default" admission shard the WP-0 suite counts.
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import pump
from jarvis.chat import turn_state as ts
from jarvis.chat.relay_mux import RelayMux
from jarvis.tests.test_relay_mux import _DoubleGateway

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
PUMP = "Jarvis Relay Pump"
EFFECT = "Jarvis Turn Effect"
TEST_USER = "jarvis-pump-test@example.com"
TARGET_PREFIX = "pmpx_"


def _ensure_test_user(user: str = TEST_USER) -> None:
	if frappe.db.exists("User", user):
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": user,
			"first_name": "Pump",
			"last_name": "Test",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.add_roles("System Manager", "Jarvis User")
	frappe.db.commit()


def _cleanup(user: str = TEST_USER) -> None:
	conv_names = frappe.get_all(CONV, filters={"owner": user}, pluck="name")
	for name in conv_names:
		turn_names = frappe.get_all(TURN, filters={"conversation": name}, pluck="name")
		if turn_names:
			frappe.db.delete(EFFECT, {"turn": ["in", turn_names]})
		frappe.db.delete(TURN, {"conversation": name})
		frappe.db.delete(MSG, {"conversation": name})
		frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
	frappe.db.delete(PUMP, {"relay_target_id": ["like", f"{TARGET_PREFIX}%"]})
	frappe.db.commit()


# --------------------------------------------------------------------------- #
# Enqueue / seam recorders
# --------------------------------------------------------------------------- #


class _Recorder:
	def __init__(self):
		self.calls: list = []

	def __call__(self, *args, **kwargs):
		self.calls.append((args, kwargs))

	@property
	def count(self):
		return len(self.calls)


class _ResolvedSnap:
	"""CDX-17: an already-resolved snapshot future double (``.done`` True, ``.result``
	returns a fixed reconcile dict) for driving the issue-and-poll capacity refresh in
	tests without a real mux."""

	def __init__(self, snap: dict):
		self._snap = snap

	@property
	def done(self) -> bool:
		return True

	def result(self, timeout: float = 0) -> dict:
		return self._snap


class _PumpTestCase(FrappeTestCase):
	def setUp(self):
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup()
		self._target = f"{TARGET_PREFIX}{frappe.generate_hash(length=10)}"
		ts._ensure_control_row(self._target)
		# CDX-21 (Residual B): the per-shard row is now the AUTHORITATIVE transport mode that the
		# kill-switch check (_kill_switch_engaged) and the lifecycle gates read. Stamp this test
		# shard explicitly to ``pump`` so the suite is independent of the ambient site config (an
		# empty row would otherwise fall back to conf, which on a flag-off site would spuriously
		# read as the kill switch and halt every hop). Kill-switch tests override it to ``legacy``.
		frappe.db.set_value(PUMP, self._target, "transport_mode", pump._MODE_PUMP, update_modified=False)
		frappe.db.commit()
		pump._LIFECYCLE_MODE_CACHE.pop(self._target, None)
		ts.reset_lock_tracking()
		self._muxes: list[RelayMux] = []
		self._doubles: list[_DoubleGateway] = []
		# Remove real waits from the DB-disconnect backoff path.
		self._sleep_patch = patch.object(pump, "_sleep", lambda *_a, **_k: None)
		self._sleep_patch.start()
		# OARF-9: activate the canonical lock-order assertion for the whole pump
		# suite so an inversion (control->conversation->turn->message) FAILS a test
		# instead of silently log_error-ing. patterntest sets neither developer_mode
		# nor this flag, so without it assert_lock_order is a no-op everywhere.
		self._lock_assert_prev = frappe.local.conf.get("jarvis_pump_lock_assert")
		frappe.local.conf["jarvis_pump_lock_assert"] = 1

	def tearDown(self):
		if self._lock_assert_prev is None:
			frappe.local.conf.pop("jarvis_pump_lock_assert", None)
		else:
			frappe.local.conf["jarvis_pump_lock_assert"] = self._lock_assert_prev
		self._sleep_patch.stop()
		for mux in self._muxes:
			try:
				mux.stop(timeout=3)
			except Exception:
				pass
		for d in self._doubles:
			try:
				d.stop()
			except Exception:
				pass
		ts.reset_lock_tracking()
		_cleanup()
		frappe.set_user(self._orig_user)

	# ---- fixtures --------------------------------------------------------- #

	def _mk_conv(self) -> str:
		doc = frappe.get_doc({"doctype": CONV, "title": "New chat", "status": "Active"})
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
		return doc.name

	def _next_seq(self, conv: str) -> int:
		return (
			frappe.db.sql(f"SELECT MAX(seq) FROM `tab{MSG}` WHERE conversation=%(c)s", {"c": conv})[0][0] or 0
		) + 1

	def _mk_msg(self, conv: str, role: str = "user", content: str = "hi", **extra) -> str:
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": self._next_seq(conv),
				"role": role,
				"content": content,
				**extra,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
		return doc.name

	def _mk_turn(self, conv, run_id, seed, state, *, version=1, pump_epoch=0, reserved=0, **extra) -> None:
		row = {
			"doctype": TURN,
			"run_id": run_id,
			"conversation": conv,
			"relay_target_id": self._target,
			"turn_class": "interactive",
			"state": state,
			"version": version,
			"pump_epoch": pump_epoch,
			"seed_message": seed,
			"reserved": reserved,
			"enqueued_at": frappe.utils.now(),
		}
		row.update(extra)
		frappe.get_doc(row).insert(ignore_permissions=True)
		frappe.db.commit()

	def _state(self, run_id) -> str:
		return frappe.db.get_value(TURN, run_id, "state")

	def _val(self, run_id, field):
		return frappe.db.get_value(TURN, run_id, field)

	# ---- deps + ctx helpers ---------------------------------------------- #

	def _double(self) -> _DoubleGateway:
		d = _DoubleGateway()
		self._doubles.append(d)
		return d

	def _deps(
		self, *, double=None, prepare=None, settlement=None, snapshot=None, issue_snapshot=None
	) -> pump.PumpDeps:
		d = pump.PumpDeps()
		d.dispatch_prepare = prepare or _Recorder()
		d.enqueue_finalize = _Recorder()
		d.enqueue_pump_job = _Recorder()
		d.apply_tool = _Recorder()
		d.snapshot = snapshot or (lambda ctx: {"gateway_active": 0, "active_session_keys": None})
		if issue_snapshot is not None:
			d.issue_snapshot = issue_snapshot
		if settlement is not None:
			d.invoke_settlement = settlement
		if double is not None:
			d.make_mux = self._make_mux_factory(double)
		return d

	def _make_mux_factory(self, double):
		def factory(target, epoch):
			mux = RelayMux(double, target).start()
			self._muxes.append(mux)
			return mux

		return factory

	def _make_ctx(self, deps, *, holder="hop-test", hop_counter=0, with_mux=True) -> pump.PumpContext:
		won, epoch = ts.lease_acquire(self._target, holder, hop_counter=hop_counter)
		self.assertTrue(won, "test ctx failed to acquire the lease")
		ctx = pump.PumpContext(
			relay_target_id=self._target,
			epoch=epoch,
			holder=holder,
			hop_counter=hop_counter,
			site=frappe.local.site,
			deps=deps,
		)
		now = pump._monotonic()
		ctx.soft_deadline = now + 999
		ctx.hard_deadline = now + 999
		ctx.last_heartbeat = now
		if with_mux:
			ctx.mux = deps.make_mux(self._target, epoch)
		return ctx

	def _prepare_stub(self, conv, double, transcript="success"):
		"""Synchronous prepare stub: queued -> preparing -> ready, writes the
		prepare->pump handoff payload (session_key + message) and arms the double.
		Stands in for WP-1d's prepare job."""

		def prep(run_id, target):
			row = ts.read_turn(run_id)
			v = int(row["version"])
			amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
			self.assertTrue(ts.claim_preparing(run_id, v, assistant_message=amsg))
			frappe.db.commit()
			v += 1
			frappe.db.set_value(
				TURN,
				run_id,
				"dispatch_payload",
				json.dumps({"session_key": f"sess-{run_id}", "message": "hi"}),
				update_modified=False,
			)
			frappe.db.commit()
			self.assertTrue(ts.mark_ready(run_id, v))
			frappe.db.commit()
			double.arm(run_id, transcript)

		return prep

	def _pump_until(self, ctx, predicate, *, max_slices=60):
		for _ in range(max_slices):
			outcome = pump.drain_slice(ctx)
			if predicate():
				return outcome
			if outcome == "idle_exit":
				return outcome
		return "timeout"


# --------------------------------------------------------------------------- #
# 1. Cold start — full lifecycle over fake frames
# --------------------------------------------------------------------------- #


class TestColdStart(_PumpTestCase):
	def test_cold_start_full_lifecycle(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="hello")
		rid = "pmp_cold"
		# A cold queued turn (loser / no held credit) — the pump grants the credit.
		self._mk_turn(conv, rid, seed, "queued", version=0, reserved=0)

		double = self._double()
		settle_calls = []

		def settlement(run_id, **kw):
			settle_calls.append((run_id, dict(kw)))
			pump._default_invoke_settlement(run_id, **kw)

		deps = self._deps(
			double=double, prepare=self._prepare_stub(conv, double, "success"), settlement=settlement
		)
		ctx = self._make_ctx(deps)

		self._pump_until(ctx, lambda: bool(settle_calls))

		self.assertEqual(len(settle_calls), 1, "settlement seam invoked exactly once")
		self.assertEqual(settle_calls[0][0], rid)
		self.assertEqual(settle_calls[0][1]["epoch"], ctx.epoch)
		# The machine ran all the way: reserved -> preparing -> ready -> dispatching
		# -> streaming -> terminal_observed -> finalizing (slot released).
		self.assertEqual(self._state(rid), "finalizing")
		self.assertEqual(int(self._val(rid, "reserved")), 0, "slot released at settlement")
		self.assertIsNotNone(self._val(rid, "gateway_run_id"))
		self.assertIsNotNone(self._val(rid, "terminal_observed_at"))
		# Deltas were applied to the assistant placeholder (cumulative mirror).
		amsg = self._val(rid, "assistant_message")
		self.assertTrue((frappe.db.get_value(MSG, amsg, "content") or "").strip())
		# finalize was enqueued by settlement (D3 S6).
		self.assertGreaterEqual(deps.enqueue_finalize.count, 1)


# --------------------------------------------------------------------------- #
# 2. Idle-exit vs concurrent-send race — both D6 §2 orderings
# --------------------------------------------------------------------------- #


class TestIdleExitRace(_PumpTestCase):
	def test_commit_before_release_keeps_lease_and_continues(self):
		"""Ordering A: a queued turn committed before the conditional release makes
		it affect 0 rows -> the pump KEEPS the lease and CONTINUES (OAR-12)."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		ctx = self._make_ctx(self._deps(), with_mux=False)
		self._mk_turn(conv, "pmp_idleA", seed, "queued", version=0)
		self.assertFalse(pump._idle_exit(ctx), "must NOT release while a queued turn exists")
		# lease still held by this epoch.
		self.assertTrue(ts.lease_renew(self._target, ctx.epoch))

	def test_release_before_commit_then_ensure_pump_revives(self):
		"""Ordering B: no work -> the conditional release commits (lease vacant) ->
		a later sender's ensure_pump sees the vacant lease (MariaDB-authoritative)
		and enqueues a fresh hop."""
		ctx = self._make_ctx(self._deps(), with_mux=False)
		self.assertTrue(pump._idle_exit(ctx), "must release when the shard is idle")
		# Now a sender commits a new turn and calls ensure_pump.
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		self._mk_turn(conv, "pmp_idleB", seed, "queued", version=0)
		rec = _Recorder()
		deps = pump.PumpDeps()
		deps.enqueue_pump_job = rec
		# ensure_pump is gated on pump_configured (OARF-1) — it only revives when the
		# pump is enabled/draining.
		with patch.object(pump, "pump_configured", return_value=True):
			res = pump.ensure_pump(self._target, deps=deps)
		self.assertTrue(res["enqueued"])
		self.assertEqual(rec.count, 1)
		_, kwargs = rec.calls[0]
		self.assertEqual(kwargs["queue"], "long")
		self.assertEqual(kwargs["timeout"], 180)
		self.assertEqual(kwargs["relay_target_id"], self._target)
		self.assertIn("hop", kwargs["job_id"])

	def test_ensure_pump_noop_when_lease_live(self):
		# A live lease -> ensure_pump is a MariaDB-authoritative NO-OP.
		won, epoch = ts.lease_acquire(self._target, "live-holder")
		self.assertTrue(won)
		pump._clear_lease_mirror(self._target)  # force the DB path, not the fast mirror
		rec = _Recorder()
		deps = pump.PumpDeps()
		deps.enqueue_pump_job = rec
		with patch.object(pump, "pump_configured", return_value=True):
			res = pump.ensure_pump(self._target, deps=deps)
		self.assertFalse(res["enqueued"])
		self.assertEqual(res["reason"], "lease_live")
		self.assertEqual(rec.count, 0)


# --------------------------------------------------------------------------- #
# 3. Hop handoff — fresh job id, timeout=180, queue=long (assert args)
# --------------------------------------------------------------------------- #


class TestHopHandoff(_PumpTestCase):
	def test_handoff_enqueues_fresh_successor(self):
		rec = _Recorder()
		deps = pump.PumpDeps()
		deps.enqueue_pump_job = rec
		ctx = self._make_ctx(deps, hop_counter=4, with_mux=False)
		pump._handoff(ctx)
		self.assertEqual(rec.count, 1, "exactly one successor enqueued (not run)")
		_, kwargs = rec.calls[0]
		self.assertEqual(kwargs["method"], "jarvis.chat.pump.run_pump_hop")
		self.assertEqual(kwargs["queue"], "long")  # §10.4: ALWAYS long
		self.assertEqual(kwargs["timeout"], 180)  # explicit hop timeout
		self.assertEqual(kwargs["hop_counter"], 5)  # incremented
		self.assertEqual(kwargs["job_id"], f"jarvis-pump::{frappe.local.site}::{self._target}::hop5")
		# control row hop_counter bumped so the next hop mints a fresh id.
		self.assertEqual(int(frappe.db.get_value(PUMP, self._target, "hop_counter")), 5)


# --------------------------------------------------------------------------- #
# 4. Takeover fencing — the old context's write affects 0 rows
# --------------------------------------------------------------------------- #


class TestTakeoverFencing(_PumpTestCase):
	def test_old_epoch_write_affects_zero_rows(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
		rid = "pmp_fence"
		# A streaming turn owned by epoch 1.
		frappe.db.set_value(PUMP, self._target, "pump_epoch", 1, update_modified=False)
		frappe.db.set_value(
			PUMP,
			self._target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.commit()
		self._mk_turn(
			conv,
			rid,
			seed,
			"streaming",
			version=5,
			pump_epoch=1,
			reserved=1,
			assistant_message=amsg,
			dispatching_at=frappe.utils.now(),
		)
		old_ctx = pump.PumpContext(
			relay_target_id=self._target,
			epoch=1,
			holder="old",
			hop_counter=0,
			site=frappe.local.site,
			deps=self._deps(),
		)

		# A second hop takes over (mini-takeover): epoch -> 2, re-stamps the turn.
		won, new_epoch = ts.lease_acquire(self._target, "new-hop")
		self.assertTrue(won)
		self.assertEqual(new_epoch, 2)
		self.assertEqual(int(self._val(rid, "pump_epoch")), 2, "adopted turn re-stamped to E+1")

		# The OLD context's epoch-fenced write now affects 0 rows.
		self.assertFalse(
			ts.apply_delta(rid, version=6, epoch=1, event_seq=1, assistant_message=amsg, content="stale"),
			"stale-epoch delta must affect 0 rows",
		)
		self.assertTrue(pump._epoch_lost(old_ctx, rid), "the pump recognises the epoch loss")
		# The new epoch owns it and can still write.
		self.assertTrue(
			ts.apply_delta(
				rid,
				version=int(self._val(rid, "version")),
				epoch=2,
				event_seq=1,
				assistant_message=amsg,
				content="fresh",
			)
		)

	def test_dual_acquire_exactly_one_wins_threaded(self):
		"""Reuse WP-1a's threaded dual-acquire shape at the pump layer: two racers,
		exactly one becomes the leader, the epoch advances once."""
		frappe.db.set_value(
			PUMP,
			self._target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.set_value(PUMP, self._target, "pump_epoch", 0, update_modified=False)
		frappe.db.commit()
		site = frappe.local.site
		target = self._target
		barrier = threading.Barrier(2)
		results: dict[str, tuple] = {}
		errors: list = []

		def worker(name):
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user(TEST_USER)
			try:
				barrier.wait(timeout=10)
				results[name] = ts.lease_acquire(target, f"hop-{name}")
			except Exception as e:  # pragma: no cover
				errors.append(e)
			finally:
				try:
					frappe.db.commit()
				finally:
					frappe.destroy()

		threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=20)
		self.assertEqual(errors, [])
		wins = [n for n, (won, _e) in results.items() if won]
		self.assertEqual(len(wins), 1, f"exactly one leader: {results}")
		self.assertEqual(int(frappe.db.get_value(PUMP, target, "pump_epoch")), 1)


# --------------------------------------------------------------------------- #
# 5. Watchdog per-state matrix
# --------------------------------------------------------------------------- #


class TestWatchdog(_PumpTestCase):
	def test_per_state_actions(self):
		conv = self._mk_conv()
		# queued age-out (older than QUEUED_MAX_AGE_S).
		s1 = self._mk_msg(conv)
		self._mk_turn(conv, "pmp_wd_queued", s1, "queued", version=0)
		frappe.db.set_value(
			TURN,
			"pmp_wd_queued",
			"enqueued_at",
			frappe.utils.add_to_date(None, seconds=-(ts.QUEUED_MAX_AGE_S + 60)),
			update_modified=False,
		)
		# stale preparing (past PREPARE_DEADLINE_S) -> fresh prepare (queued).
		s2 = self._mk_msg(conv)
		self._mk_turn(conv, "pmp_wd_prep", s2, "preparing", version=1, reserved=1)
		frappe.db.set_value(
			TURN,
			"pmp_wd_prep",
			"preparing_at",
			frappe.utils.add_to_date(None, seconds=-(ts.PREPARE_DEADLINE_S + 60)),
			update_modified=False,
		)
		# finalizing -> re-enqueue finalize ONLY (R-13), never recovering.
		s3 = self._mk_msg(conv)
		self._mk_turn(conv, "pmp_wd_fin", s3, "finalizing", version=3)
		frappe.db.commit()

		fin_rec = _Recorder()
		pump_rec = _Recorder()
		deps = pump.PumpDeps()
		deps.enqueue_finalize = fin_rec
		deps.enqueue_pump_job = pump_rec

		# OARF-1: the watchdog is gated on pump_configured — it only runs when the
		# pump is enabled/draining. (The queued age-out row here is UNRESERVED, so
		# OARF-4's reserved-reclaim does not pre-empt the age-out.)
		with patch.object(pump, "pump_configured", return_value=True):
			summary = pump.watchdog(deps=deps)

		self.assertEqual(self._state("pmp_wd_queued"), "cancelled", "queued age-out")
		self.assertEqual(summary["aged_out"], 1)
		self.assertEqual(self._state("pmp_wd_prep"), "queued", "stale preparing -> fresh prepare")
		self.assertEqual(
			int(self._val("pmp_wd_prep", "reserved")), 0, "credit released on fresh-prepare park"
		)
		self.assertGreaterEqual(summary["reclaimed"], 1)
		# finalizing untouched (R-13) but its finalize was re-enqueued.
		self.assertEqual(self._state("pmp_wd_fin"), "finalizing")
		self.assertEqual(fin_rec.count, 1)
		self.assertEqual(summary["finalize_requeued"], 1)
		# live work remained (the re-queued turn) -> ensure_pump revived the shard.
		self.assertGreaterEqual(pump_rec.count, 1)


# --------------------------------------------------------------------------- #
# 6. Ack-timeout park vs definite-rejection errored (PANEL 7) + WS drop (PANEL 9)
# --------------------------------------------------------------------------- #


class _RejectGateway:
	"""Minimal transport double whose chat.send is DEFINITIVELY rejected (ok:false
	with a concrete code) — the OAR-8 dispatching->errored path."""

	def __init__(self, code="policy_denied"):
		self._lock = threading.Lock()
		self._code = code
		import queue as _q

		self._q = _q.Queue()
		self._ws = _RejectWs(self._on_send)
		self._closed = threading.Event()

	def _recv(self, timeout_s):
		if timeout_s <= 0:
			return None
		try:
			return self._q.get(timeout=timeout_s)
		except Exception:
			return None

	def close(self):
		self._closed.set()

	def stop(self):
		self._closed.set()

	def _on_send(self, payload):
		frame = json.loads(payload)
		self._q.put(
			{
				"type": "res",
				"id": frame.get("id"),
				"ok": False,
				"error": {"code": self._code, "message": "nope"},
			}
		)


class _RejectWs:
	def __init__(self, on_send):
		self._on_send = on_send
		self.connected = True

	def send(self, payload):
		self._on_send(payload)


class TestDispatchOutcomes(_PumpTestCase):
	def _seed_ready(self, conv, rid):
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
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		return amsg

	def test_ack_timeout_parks_recovering(self):
		conv = self._mk_conv()
		rid = "pmp_ackto"
		self._seed_ready(conv, rid)
		double = self._double()
		double.arm(rid, "ack-timeout")  # gateway holds the ack past the window
		deps = self._deps(double=double)
		ctx = self._make_ctx(deps)
		# OARF-5: dispatch ISSUES the ack (non-blocking) + parks it; the ack-timeout
		# fires on a LATER poll when our slice-level deadline passes (not a blocking
		# .result()). Drive slices until the park lands.
		with patch.object(pump, "ACK_TIMEOUT_S", 0.3):
			t0 = time.monotonic()
			pump._dispatch_ready(ctx)
			self.assertLess(time.monotonic() - t0, 1.0, "dispatch did NOT block on the ack (OARF-5)")
			self._pump_until(ctx, lambda: self._state(rid) == "recovering")
		# Ambiguous outcome -> parked recovering (NOT errored).
		self.assertEqual(self._state(rid), "recovering")
		self.assertEqual(int(self._val(rid, "recovering")), 1)
		# SUXF-1: the park mirrored the Message row so a reload reconstructs the banner.
		amsg = self._val(rid, "assistant_message")
		self.assertEqual(int(frappe.db.get_value(MSG, amsg, "recovering") or 0), 1)

	def test_ws_drop_outstanding_ack_immediate_sentinel_parks(self):
		"""PANEL 9: a WS drop while a chat.send ack is outstanding fails the future
		immediately (OAR-10 sentinel) -> the pump parks recovering, no dead-wait."""
		conv = self._mk_conv()
		rid = "pmp_wsdrop"
		self._seed_ready(conv, rid)
		double = self._double()
		double.arm(rid, "success", ack_behavior="drop")
		deps = self._deps(double=double)
		ctx = self._make_ctx(deps)
		t0 = time.monotonic()
		pump._dispatch_ready(ctx)  # OARF-5: non-blocking issue
		# The Closing sentinel fails the pending future immediately; a poll resolves it.
		self._pump_until(ctx, lambda: self._state(rid) == "recovering")
		self.assertLess(time.monotonic() - t0, 5.0, "did not dead-wait the ack")
		self.assertEqual(self._state(rid), "recovering")

	def test_definite_rejection_errors_and_releases_credit(self):
		"""PANEL 7: a DEFINITE ok:false rejection -> dispatching->errored + credit
		release (OAR-8), distinct from the ack-timeout park."""
		conv = self._mk_conv()
		rid = "pmp_reject"
		self._seed_ready(conv, rid)
		double = _RejectGateway()
		self._doubles.append(double)
		deps = self._deps(double=double)
		ctx = self._make_ctx(deps)
		pump._dispatch_ready(ctx)  # OARF-5: non-blocking issue
		self._pump_until(ctx, lambda: self._state(rid) in ("errored", "recovering"))
		self.assertEqual(self._state(rid), "errored")
		self.assertEqual(int(self._val(rid, "reserved")), 0, "credit released on definite rejection")
		self.assertTrue(self._val(rid, "error") or "")


# --------------------------------------------------------------------------- #
# 7. Cancel flow end-to-end
# --------------------------------------------------------------------------- #


class TestCancelFlow(_PumpTestCase):
	def test_cancel_requested_aborts_and_settles_cancelled(self):
		conv = self._mk_conv()
		rid = "pmp_cancel"
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
		double = self._double()
		deps = self._deps(double=double)
		ctx = self._make_ctx(deps)
		# A streaming turn owned by THIS pump's epoch (so the abort CAS is fenced).
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
		# The web sender requests cancellation (D2 row 17).
		self.assertTrue(ts.request_cancel(rid, 4))
		frappe.db.commit()

		swept = pump._cancel_sweep(ctx)
		self.assertEqual(swept, 1)
		self.assertEqual(self._state(rid), "cancelled")
		self.assertEqual(int(self._val(rid, "reserved")), 0, "slot released on cancel")


# --------------------------------------------------------------------------- #
# 8. DB-disconnect -> bounded backoff -> park recovering -> exit hop
# --------------------------------------------------------------------------- #


class TestDBDisconnect(_PumpTestCase):
	def test_db_disconnect_parks_and_exits(self):
		conv = self._mk_conv()
		rid = "pmp_dbdrop"
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
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		double = self._double()
		deps = self._deps(double=double)

		op_error = frappe.db.OperationalError("(2013, 'Lost connection to MySQL server')")
		# The dispatch CAS raises a DB operational error persistently; the pump
		# must bound the backoff, park the affected in-flight turns, and exit (never spin).
		with (
			patch.object(ts, "confirm_dispatching", side_effect=op_error),
			patch.object(pump, "_backoff_reconnect", lambda *_a, **_k: None),
		):
			res = pump.run_pump_hop(self._target, deps=deps, soft_budget_s=999)

		self.assertEqual(res["exit"], "db_disconnect")
		# CDX-24: the DB-disconnect park is now epoch-fenced. This turn is PRE-dispatch
		# (`ready`, pump_epoch never stamped — lease_acquire re-stamps only in-flight
		# dispatching/streaming turns), so it carries no gateway run and the epoch-fenced
		# park affects 0 rows: it stays `ready` and the next healthy hop's `_dispatch_ready`
		# re-dispatches it. The in-flight (epoch-stamped) park-on-disconnect is covered by
		# test_pump_pipeline.test_db_disconnect_park_writes_mirror_and_publishes.
		self.assertEqual(self._state(rid), "ready", "pre-dispatch turn is NOT epoch-fence-parked")

	def test_stale_pump_park_affects_zero_rows(self):
		"""CDX-24: after a takeover adopted the turn to the NEXT epoch (re-stamping its
		pump_epoch), a delayed OLD pump's epoch-fenced park matches 0 rows — no park, no
		Message-row mirror, no run:recovering publish."""
		conv = self._mk_conv()
		rid = "pmp_stale_park"
		ctx = self._make_ctx(self._deps(), with_mux=False)  # epoch E, lease held
		amsg = self._inflight_turn(ctx, rid, conv)  # pump_epoch=E, assistant_message=amsg
		# A takeover adopts the turn to the NEXT epoch; ctx is now STALE (E < E+1).
		frappe.db.set_value(TURN, rid, "pump_epoch", ctx.epoch + 1, update_modified=False)
		frappe.db.commit()
		captured = []
		with patch.object(ts, "publish_to_user", side_effect=lambda u, p: captured.append(p)):
			pump._park_affected_recovering(ctx)
		self.assertEqual(self._state(rid), "streaming", "stale pump parks 0 rows")
		self.assertEqual(int(self._val(rid, "recovering") or 0), 0, "no recovering flag set")
		self.assertEqual(int(frappe.db.get_value(MSG, amsg, "recovering") or 0), 0, "no Message mirror")
		self.assertEqual(
			[p for p in captured if p.get("kind") == "run:recovering"], [], "no run:recovering publish"
		)

	def test_current_epoch_park_recovers_inflight(self):
		"""CDX-24 companion: the CURRENT pump's epoch-fenced park DOES recover its own
		in-flight (epoch-stamped) turn — the fence blocks only STALE hops."""
		conv = self._mk_conv()
		rid = "pmp_live_park"
		ctx = self._make_ctx(self._deps(), with_mux=False)
		self._inflight_turn(ctx, rid, conv)  # pump_epoch=ctx.epoch
		with patch.object(ts, "publish_to_user", side_effect=lambda u, p: None):
			pump._park_affected_recovering(ctx)
		self.assertEqual(self._state(rid), "recovering", "current pump parks its in-flight turn")

	def _inflight_turn(self, ctx, rid, conv):
		"""A streaming turn owned by ctx.epoch (mirror of the kill-switch suite's
		helper, local to this class)."""
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
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
			last_event_seq=2,
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		return amsg


# --------------------------------------------------------------------------- #
# 9. Credit lifecycle (PANEL 2/3) + cross-conversation over-admit (PANEL 1)
# --------------------------------------------------------------------------- #


class TestCreditLifecycle(_PumpTestCase):
	def test_promote_dispatch_settle_credit(self):
		conv = self._mk_conv()
		rid = "pmp_credit"
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
		self._mk_turn(conv, rid, seed, "queued", version=0, reserved=0)
		prep_rec = _Recorder()
		deps = self._deps(prepare=prep_rec)
		ctx = self._make_ctx(deps, with_mux=False)

		self.assertEqual(pump._pump_local_reservations(self._target), 0)
		# PROMOTE: reserve credit under the shard lock -> local_res = 1.
		self.assertEqual(pump._promote_queued(ctx), 1)
		self.assertEqual(int(self._val(rid, "reserved")), 1)
		self.assertEqual(pump._pump_local_reservations(self._target), 1)
		self.assertEqual(prep_rec.count, 1, "prepare dispatched after the shard-lock commit")

		# DISPATCH (drive the prepare + dispatch transitions with turn_state) —
		# a dispatching/streaming turn keeps consuming the credit.
		v = int(self._val(rid, "version"))
		self.assertTrue(ts.claim_preparing(rid, v, assistant_message=amsg))
		self.assertTrue(ts.mark_ready(rid, v + 1))
		frappe.db.commit()
		self.assertTrue(ts.confirm_dispatching(rid, v + 2, ctx.epoch, self._target))
		self.assertTrue(ts.mark_streaming(rid, v + 3, ctx.epoch, gateway_run_id=rid))
		frappe.db.commit()
		self.assertEqual(pump._pump_local_reservations(self._target), 1, "in-flight still holds the credit")

		# TERMINAL + SETTLE (default seam) -> slot released -> local_res = 0.
		self.assertTrue(ts.mark_terminal_observed(rid, v + 4, ctx.epoch, "relay:final", {"text": "done"}))
		frappe.db.commit()
		ctx.deps.invoke_settlement(
			rid,
			relay_target_id=self._target,
			epoch=ctx.epoch,
			version=v + 5,
			terminal_kind="relay:final",
			terminal_payload={"text": "done"},
			assistant_message=amsg,
			owner=frappe.session.user,
			conversation=conv,
			deps=ctx.deps,
		)
		self.assertEqual(self._state(rid), "finalizing")
		self.assertEqual(pump._pump_local_reservations(self._target), 0, "credit released at settlement")

	def test_cross_conversation_over_admit_at_promote(self):
		"""PANEL 1: two queued turns on DIFFERENT conversations of the SAME shard
		with cap=1 -> the shard-locked promote reserves EXACTLY ONE (the credit
		count+reserve serialize on the shard, OAR-1)."""
		conv_a = self._mk_conv()
		conv_b = self._mk_conv()
		sa = self._mk_msg(conv_a)
		sb = self._mk_msg(conv_b)
		self._mk_turn(conv_a, "pmp_oa", sa, "queued", version=0, reserved=0)
		self._mk_turn(conv_b, "pmp_ob", sb, "queued", version=0, reserved=0)
		prep_rec = _Recorder()
		deps = self._deps(prepare=prep_rec)
		ctx = self._make_ctx(deps, with_mux=False)

		with patch("jarvis.chat.admission._max_inflight", return_value=1):
			promoted = pump._promote_queued(ctx)

		self.assertEqual(promoted, 1, "exactly one credit granted under cap=1")
		reserved = frappe.db.sql(
			f"SELECT COUNT(*) FROM `tab{TURN}` WHERE relay_target_id=%(t)s AND reserved=1",
			{"t": self._target},
		)[0][0]
		self.assertEqual(reserved, 1, "no cross-conversation over-admit")
		self.assertEqual(prep_rec.count, 1)

	def test_reserve_on_send_winner_dispatches_prepare_without_re_reserve(self):
		"""A queued turn that ALREADY holds a credit (reserve-on-send winner) is
		promoted for prepare without consuming a NEW credit."""
		conv = self._mk_conv()
		rid = "pmp_held"
		seed = self._mk_msg(conv)
		self._mk_turn(conv, rid, seed, "queued", version=1, reserved=1)
		prep_rec = _Recorder()
		deps = self._deps(prepare=prep_rec)
		ctx = self._make_ctx(deps, with_mux=False)
		before = pump._pump_local_reservations(self._target)
		self.assertEqual(pump._promote_queued(ctx), 1)
		self.assertEqual(prep_rec.count, 1)
		self.assertEqual(int(self._val(rid, "reserved")), 1)
		self.assertEqual(pump._pump_local_reservations(self._target), before, "no new credit consumed")


# --------------------------------------------------------------------------- #
# 10. Wake bus round-trip (raw redis, explicit site key)
# --------------------------------------------------------------------------- #


class TestWakeBus(_PumpTestCase):
	def test_lpush_then_drain_roundtrip(self):
		pump.lpush_wake(self._target, "run-1")
		pump.lpush_wake(self._target, "run-2")
		drained = pump.drain_wake(self._target)
		self.assertEqual(set(drained), {"run-1", "run-2"})
		# Draining again yields nothing (the bus was emptied).
		self.assertEqual(pump.drain_wake(self._target), [])


# --------------------------------------------------------------------------- #
# 11. OARF-4 — reserved-but-unclaimed reclaim (retry, never age-out cancel)
# --------------------------------------------------------------------------- #


class TestWatchdogReservationReclaim(_PumpTestCase):
	def test_reserved_unclaimed_reclaimed_for_retry_not_cancelled(self):
		"""OARF-4: a turn that RESERVED a credit at promote but whose prepare never
		CLAIMED it (still `queued` reserved=1, reservation > 120s old) is reclaimed
		back to `queued reserved=0` FOR RETRY — even when it is ALSO past the 900s
		age-out (reclaim WINS over cancel for a reserved turn)."""
		conv = self._mk_conv()
		rid = "pmp_oarf4"
		seed = self._mk_msg(conv)
		self._mk_turn(conv, rid, seed, "queued", version=1, reserved=1)
		frappe.db.set_value(
			TURN,
			rid,
			{
				# reservation made > PREPARE_DISPATCH_DEADLINE_S ago (stale, unclaimed):
				"reservation_expires_at": frappe.utils.add_to_date(
					None, seconds=(ts.RESERVE_TTL_S - pump.PREPARE_DISPATCH_DEADLINE_S - 30)
				),
				# ALSO past the 900s age-out — prove reclaim pre-empts cancel.
				"enqueued_at": frappe.utils.add_to_date(None, seconds=-(ts.QUEUED_MAX_AGE_S + 120)),
			},
			update_modified=False,
		)
		frappe.db.commit()
		wd_deps = pump.PumpDeps()
		wd_deps.enqueue_pump_job = _Recorder()
		with patch.object(pump, "pump_configured", return_value=True):
			summary = pump.watchdog(deps=wd_deps)
		self.assertEqual(self._state(rid), "queued", "reserved-unclaimed turn RETRIED, not cancelled")
		self.assertEqual(int(self._val(rid, "reserved")), 0, "credit released for retry")
		self.assertGreaterEqual(summary["reclaimed"], 1)
		self.assertEqual(summary["aged_out"], 0, "reclaim wins over age-out for a reserved turn")

	def test_unreserved_queued_still_ages_out(self):
		"""The 15-min age-out still cancels a genuinely-waiting UNRESERVED queued turn
		(never given a credit) — OARF-4 only spares RESERVED turns."""
		conv = self._mk_conv()
		rid = "pmp_oarf4b"
		seed = self._mk_msg(conv)
		self._mk_turn(conv, rid, seed, "queued", version=0, reserved=0)
		frappe.db.set_value(
			TURN,
			rid,
			"enqueued_at",
			frappe.utils.add_to_date(None, seconds=-(ts.QUEUED_MAX_AGE_S + 120)),
			update_modified=False,
		)
		frappe.db.commit()
		wd_deps = pump.PumpDeps()
		wd_deps.enqueue_pump_job = _Recorder()
		with patch.object(pump, "pump_configured", return_value=True):
			summary = pump.watchdog(deps=wd_deps)
		self.assertEqual(self._state(rid), "cancelled", "unreserved queued turn still ages out")
		self.assertGreaterEqual(summary["aged_out"], 1)


# --------------------------------------------------------------------------- #
# 12. OARF-8 — prepare/finalize enqueues use attempt-suffixed job ids
# --------------------------------------------------------------------------- #


class TestAttemptSuffixJobIds(_PumpTestCase):
	def test_prepare_finalize_ids_carry_attempt_suffix(self):
		conv = self._mk_conv()
		rid = "pmp_jobid"
		seed = self._mk_msg(conv)
		self._mk_turn(conv, rid, seed, "queued", version=3, reserved=1)
		captured: list = []

		def fake_enqueue(method, **kw):
			captured.append(kw.get("job_id"))

		with patch("frappe.enqueue", fake_enqueue):
			pump._default_dispatch_prepare(rid, self._target)
			pump._default_enqueue_finalize(rid, self._target)
		self.assertEqual(captured[0], f"jarvis-prepare::{rid}::a3", "prepare id carries ::a<version>")
		self.assertEqual(captured[1], f"jarvis-finalize::{rid}::a3", "finalize id carries ::a<version>")

		# A genuine new attempt (version bumped) mints a DIFFERENT id, so a dead
		# STARTED registration of the old id can never no-op the re-enqueue.
		frappe.db.set_value(TURN, rid, "version", 7, update_modified=False)
		frappe.db.commit()
		captured.clear()
		with patch("frappe.enqueue", fake_enqueue):
			pump._default_dispatch_prepare(rid, self._target)
		self.assertEqual(captured[0], f"jarvis-prepare::{rid}::a7", "new attempt -> fresh id")


# --------------------------------------------------------------------------- #
# 12b. F1 — control-job (prepare/finalize) queue routing per bench shape
# --------------------------------------------------------------------------- #


class TestControlQueueRouting(_PumpTestCase):
	"""F1 (long-queue self-starvation): the pump's CONTROL jobs (prepare + finalize)
	must never share the single-worker ``long`` queue the 90s hops ride. Routing per
	bench shape: a live ``jarvis_chat`` lane -> ride it; else ``long`` with >=2
	workers; else ``short``. Hops stay on ``long`` unconditionally (asserted in the
	handoff test)."""

	def test_jarvis_chat_lane_live_rides_it(self):
		with patch("jarvis.chat.api._turn_queue", lambda: "jarvis_chat"):
			self.assertEqual(pump._control_queue(), "jarvis_chat")
			self.assertFalse(pump._pump_shape_starves())

	def test_long_with_two_workers_uses_long(self):
		with (
			patch("jarvis.chat.api._turn_queue", lambda: "long"),
			patch.object(pump, "_live_worker_count", lambda q: 2),
		):
			self.assertEqual(pump._control_queue(), "long")
			self.assertFalse(pump._pump_shape_starves())

	def test_single_long_no_jarvis_chat_uses_short(self):
		with (
			patch("jarvis.chat.api._turn_queue", lambda: "long"),
			patch.object(pump, "_live_worker_count", lambda q: 1),
		):
			self.assertEqual(pump._control_queue(), "short")
			self.assertTrue(pump._pump_shape_starves())

	def test_prepare_finalize_route_to_short_on_starve_shape(self):
		"""The F1 scenario as a queue-name assertion on the enqueue calls: on the
		single-``long``-no-``jarvis_chat`` shape, BOTH prepare and finalize route to
		``short`` — never the ``long`` queue the hops occupy (which self-starves)."""
		conv = self._mk_conv()
		rid = "pmp_route_short"
		seed = self._mk_msg(conv)
		self._mk_turn(conv, rid, seed, "queued", version=2, reserved=1)
		captured: list = []

		def fake_enqueue(method, **kw):
			captured.append((method, kw.get("queue"), kw.get("timeout")))

		with (
			patch("jarvis.chat.api._turn_queue", lambda: "long"),
			patch.object(pump, "_live_worker_count", lambda q: 1),
			patch("frappe.enqueue", fake_enqueue),
		):
			pump._default_dispatch_prepare(rid, self._target)
			pump._default_enqueue_finalize(rid, self._target)
		self.assertEqual(captured[0], ("jarvis.chat.prepare.run_prepare", "short", pump.HOP_TIMEOUT_S))
		self.assertEqual(captured[1], ("jarvis.chat.finalize.run_finalize", "short", pump.HOP_TIMEOUT_S))

	def test_prepare_finalize_ride_jarvis_chat_when_live(self):
		"""When a ``jarvis_chat`` lane is live, prepare/finalize ride it (isolated
		parallel workers), NOT ``long`` or ``short``."""
		conv = self._mk_conv()
		rid = "pmp_route_jc"
		seed = self._mk_msg(conv)
		self._mk_turn(conv, rid, seed, "queued", version=2, reserved=1)
		captured: list = []

		def fake_enqueue(method, **kw):
			captured.append((method, kw.get("queue")))

		with (
			patch("jarvis.chat.api._turn_queue", lambda: "jarvis_chat"),
			patch("frappe.enqueue", fake_enqueue),
		):
			pump._default_dispatch_prepare(rid, self._target)
			pump._default_enqueue_finalize(rid, self._target)
		self.assertEqual(captured[0], ("jarvis.chat.prepare.run_prepare", "jarvis_chat"))
		self.assertEqual(captured[1], ("jarvis.chat.finalize.run_finalize", "jarvis_chat"))

	def test_provision_warning_emitted_once_then_throttled(self):
		"""§8-I: the starve shape emits ONE loud provisioning warning, then throttles
		(so the after-every-commit ensure_pump path never spams)."""
		key = f"jarvis:pump:provision_warn:{frappe.local.site}"
		frappe.cache().delete_value(key)
		events: list = []
		try:
			with (
				patch("jarvis.chat.api._turn_queue", lambda: "long"),
				patch.object(pump, "_live_worker_count", lambda q: 1),
				patch.object(pump, "_telemetry", lambda ev, **kw: events.append(ev)),
				patch("frappe.log_error", lambda *a, **k: None),
			):
				pump._warn_provisioning_if_starved()
				pump._warn_provisioning_if_starved()  # throttled — a no-op
			self.assertEqual(events.count("provision_warning"), 1, "warned once, then throttled")
		finally:
			frappe.cache().delete_value(key)

	def test_no_provision_warning_when_shape_healthy(self):
		events: list = []
		with (
			patch("jarvis.chat.api._turn_queue", lambda: "jarvis_chat"),
			patch.object(pump, "_telemetry", lambda ev, **kw: events.append(ev)),
		):
			pump._warn_provisioning_if_starved()
		self.assertNotIn("provision_warning", events)


# --------------------------------------------------------------------------- #
# 13. OARF-9 — the canonical lock-order assertion fires under the test config
# --------------------------------------------------------------------------- #


class TestLockOrderAssertion(_PumpTestCase):
	def test_lock_order_violation_raises_under_test_config(self):
		# setUp activates jarvis_pump_lock_assert, so _dev_mode() is True and an
		# inversion RAISES instead of silently log_error-ing (OARF-9).
		self.assertTrue(ts._dev_mode(), "lock-assert config active for the suite")
		ts.reset_lock_tracking()
		try:
			ts.assert_lock_order("turn")  # rank 3
			with self.assertRaises(ts.LockOrderError):
				ts.assert_lock_order("shard")  # rank 1 while holding rank 3 -> inversion
		finally:
			ts.reset_lock_tracking()

	def test_canonical_order_does_not_raise(self):
		ts.reset_lock_tracking()
		try:
			ts.assert_lock_order("shard")
			ts.assert_lock_order("conversation")
			ts.assert_lock_order("turn")
			ts.assert_lock_order("message")  # canonical order -> no raise
		finally:
			ts.reset_lock_tracking()


# --------------------------------------------------------------------------- #
# 14. OARF-5 — the reactor does not block on RPC futures inside a slice
# --------------------------------------------------------------------------- #


class TestNonBlockingReactor(_PumpTestCase):
	def test_dispatch_wave_does_not_block_on_slow_acks(self):
		"""OARF-5: dispatching N ready turns whose gateway HOLDS the ack must NOT
		block the reactor per-ack (the old fut.result(15s) wave). _dispatch_ready
		issues every send and returns fast; the acks are parked for polling."""
		double = self._double()
		rids = []
		for i in range(3):
			conv = self._mk_conv()
			rid = f"pmp_wave_{i}"
			rids.append(rid)
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
				dispatch_payload=json.dumps({"session_key": f"s-{rid}", "message": "hi"}),
			)
			double.arm(rid, "ack-timeout")  # the gateway holds every ack
		deps = self._deps(double=double)
		ctx = self._make_ctx(deps)
		t0 = time.monotonic()
		with patch.object(pump, "ACK_TIMEOUT_S", 5.0):
			n = pump._dispatch_ready(ctx)
		elapsed = time.monotonic() - t0
		self.assertEqual(n, 3, "all three sends issued")
		self.assertLess(elapsed, 2.0, "OARF-5: the wave did NOT block ~N*ACK_TIMEOUT on acks")
		self.assertEqual(len(ctx.pending_acks), 3, "acks parked for polling, not awaited inline")
		for rid in rids:
			self.assertEqual(self._state(rid), "dispatching")


# --------------------------------------------------------------------------- #
# 15. CDX-1 — clean handoff / transport-exit succession (the fencing fix)
# --------------------------------------------------------------------------- #


class TestCleanHandoffSuccession(_PumpTestCase):
	"""CDX-1: a hop must make its successor IMMEDIATELY able to acquire the lease.
	The old code enqueued the successor while the predecessor's freshly-renewed lease
	was still valid, so the successor's ``lease_acquire`` affected 0 rows and exited
	successor-less — any turn that outlived a hop stranded. These tests exercise the
	REAL handoff (the atomic epoch-guarded transfer force-expires the lease), never a
	manual lease expiry."""

	def _streaming_turn(self, ctx, rid, conv):
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
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
			last_event_seq=2,
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		return amsg

	def test_clean_handoff_makes_lease_acquirable_and_turn_continues(self):
		conv = self._mk_conv()
		rid = "pmp_ho_a"
		ctx = self._make_ctx(self._deps(), hop_counter=3, with_mux=False)  # acquires -> epoch E
		E = ctx.epoch
		self._streaming_turn(ctx, rid, conv)
		# The running hop renews its lease to ~now+30s (the exact condition the OLD
		# code stranded on) — a naive successor could not acquire.
		self.assertTrue(ts.lease_renew(self._target, E))
		pump._clear_lease_mirror(self._target)
		won_pre, _ = ts.lease_acquire(self._target, "premature")
		self.assertFalse(won_pre, "a valid renewed lease must block a naive successor acquire")
		# The clean handoff: atomic epoch-guarded transfer, THEN enqueue.
		rec = _Recorder()
		ctx.deps.enqueue_pump_job = rec
		pump._handoff(ctx)
		self.assertEqual(rec.count, 1, "exactly one successor enqueued")
		self.assertEqual(rec.calls[0][1]["hop_counter"], 4)
		# The lease is now immediately acquirable DESPITE the fresh renewal (the fix).
		won, e2 = ts.lease_acquire(self._target, "successor")
		self.assertTrue(won, "successor MUST acquire immediately after a clean handoff")
		self.assertEqual(e2, E + 1)
		# The streaming turn was adopted (re-stamped) to E+1 by the acquire, and it
		# continues to terminal under the new epoch (the SAME turn survives the hop).
		self.assertEqual(int(self._val(rid, "pump_epoch")), E + 1)
		v = int(self._val(rid, "version"))
		self.assertTrue(ts.mark_terminal_observed(rid, v, E + 1, "relay:final", {"text": "done"}))
		frappe.db.commit()
		self.assertEqual(self._state(rid), "terminal_observed")

	def test_stale_handoff_after_takeover_is_noop(self):
		"""CDX-1 (d): a stale outgoing hop whose epoch a takeover already bumped must
		transfer NOTHING — no hop_counter write, no successor enqueue (shared exit)."""
		conv = self._mk_conv()
		rid = "pmp_ho_d"
		ctx = self._make_ctx(self._deps(), hop_counter=7, with_mux=False)  # epoch E
		E = ctx.epoch
		self._streaming_turn(ctx, rid, conv)
		# A takeover lapses + re-acquires: epoch -> E+1 (ctx is now the stale owner).
		frappe.db.set_value(
			PUMP,
			self._target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.commit()
		pump._clear_lease_mirror(self._target)
		won, e2 = ts.lease_acquire(self._target, "new-owner")
		self.assertTrue(won)
		self.assertEqual(e2, E + 1)
		hop_before = int(frappe.db.get_value(PUMP, self._target, "hop_counter"))
		rec = _Recorder()
		ctx.deps.enqueue_pump_job = rec
		with self.assertRaises(ts.LeaseLostExit):
			pump._handoff(ctx)
		self.assertEqual(rec.count, 0, "stale handoff enqueues nothing")
		self.assertEqual(
			int(frappe.db.get_value(PUMP, self._target, "hop_counter")),
			hop_before,
			"stale handoff does not bump hop_counter",
		)

	def test_transport_closed_schedules_acquirable_successor(self):
		"""CDX-1 (b): a transport-exit with live work releases the lease (epoch-guarded)
		and enqueues an acquirable successor with an incremented transport_retry."""
		conv = self._mk_conv()
		rid = "pmp_ho_b"
		ctx = self._make_ctx(self._deps(), hop_counter=2, with_mux=False)  # epoch E
		E = ctx.epoch
		self._streaming_turn(ctx, rid, conv)  # live work
		self.assertTrue(ts.lease_renew(self._target, E))  # valid lease
		rec = _Recorder()
		ctx.deps.enqueue_pump_job = rec
		pump._schedule_successor_on_exit(ctx, transport_retry=0)
		self.assertEqual(rec.count, 1, "transport exit with live work enqueues a successor")
		_, kw = rec.calls[0]
		self.assertEqual(kw["transport_retry"], 1)
		self.assertEqual(kw["hop_counter"], 3)
		won, e2 = ts.lease_acquire(self._target, "succ")
		self.assertTrue(won, "successor can acquire (lease force-released)")
		self.assertEqual(e2, E + 1)

	def test_transport_exit_no_live_work_no_successor(self):
		ctx = self._make_ctx(self._deps(), hop_counter=1, with_mux=False)  # epoch E, no turns
		rec = _Recorder()
		ctx.deps.enqueue_pump_job = rec
		pump._schedule_successor_on_exit(ctx, transport_retry=0)
		self.assertEqual(rec.count, 0, "no live work -> no successor owed")

	def test_transport_retry_budget_caps_then_defers_to_watchdog(self):
		conv = self._mk_conv()
		rid = "pmp_ho_cap"
		ctx = self._make_ctx(self._deps(), hop_counter=1, with_mux=False)
		self._streaming_turn(ctx, rid, conv)
		rec = _Recorder()
		ctx.deps.enqueue_pump_job = rec
		pump._schedule_successor_on_exit(ctx, transport_retry=pump.TRANSPORT_RETRY_MAX)
		self.assertEqual(rec.count, 0, "over the retry budget -> no enqueue (defer to watchdog)")
		# The lease is still released so the watchdog / sender ensure_pump can revive it.
		won, _ = ts.lease_acquire(self._target, "succ")
		self.assertTrue(won)

	def test_no_transport_make_mux_failure_enqueues_successor(self):
		"""CDX-1 (c): a make_mux failure with live work must not exit successor-less."""
		conv = self._mk_conv()
		rid = "pmp_ho_c"
		seed = self._mk_msg(conv)
		self._mk_turn(conv, rid, seed, "queued", version=0, reserved=0)  # live work
		rec = _Recorder()
		deps = pump.PumpDeps()
		deps.enqueue_pump_job = rec
		deps.dispatch_prepare = _Recorder()
		deps.enqueue_finalize = _Recorder()

		def boom(target, epoch):
			raise RuntimeError("no socket")

		deps.make_mux = boom
		res = pump.run_pump_hop(self._target, deps=deps, soft_budget_s=999)
		self.assertEqual(res["exit"], "no_transport")
		self.assertEqual(rec.count, 1, "make_mux failure with live work enqueues a successor")
		self.assertEqual(rec.calls[0][1]["transport_retry"], 1)
		won, _ = ts.lease_acquire(self._target, "succ")
		self.assertTrue(won, "successor can acquire after the no_transport release")


# --------------------------------------------------------------------------- #
# 15b. CDX-21 (Residual B) — the pump kill switch halts succession mid-hop
# --------------------------------------------------------------------------- #


class TestKillSwitchSuccession(_PumpTestCase):
	"""CDX-21 (Residual B): a mid-hop ``pump -> legacy`` row flip (the kill switch) must release the
	lease WITHOUT enqueuing a successor and park live turns ``recovering``, at hop START, at the
	soft-budget ``_handoff``, and at a transport-error successor decision. ``draining`` still hands
	off (finish in-flight). Bound: kill latency <= cache TTL + one slice."""

	def _streaming_turn(self, ctx, rid, conv):
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
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
			last_event_seq=2,
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		return amsg

	def _kill(self):
		"""Flip the shard row to the legacy kill switch (pops the lifecycle cache)."""
		pump.set_transport_mode(self._target, pump._MODE_LEGACY)
		frappe.db.commit()

	def test_handoff_under_legacy_releases_without_successor_and_parks(self):
		conv = self._mk_conv()
		rid = "pmp_kill_ho"
		ctx = self._make_ctx(self._deps(), hop_counter=3, with_mux=False)  # epoch E, pump-mode row
		self.assertTrue(ts.lease_renew(self._target, ctx.epoch))  # a valid, freshly-renewed lease
		pump._clear_lease_mirror(self._target)
		self._streaming_turn(ctx, rid, conv)
		self._kill()  # operator flips row -> legacy mid-hop
		rec = _Recorder()
		ctx.deps.enqueue_pump_job = rec
		pump._handoff(ctx)
		self.assertEqual(rec.count, 0, "kill switch: the handoff enqueues NO successor")
		self.assertEqual(self._state(rid), "recovering", "live turn parked for legacy recovery")
		# The lease is released (acquirable) so a later revive (if the row flips back) can start.
		won, _ = ts.lease_acquire(self._target, "later")
		self.assertTrue(won, "kill-switch release makes the lease acquirable")

	def test_hop_start_under_legacy_halts_before_draining(self):
		conv = self._mk_conv()
		rid = "pmp_kill_start"
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="p", streaming=1)
		# A live streaming turn adopted on acquire; row is legacy BEFORE the hop runs (a successor
		# queued before the operator flipped the switch).
		self._mk_turn(
			conv,
			rid,
			seed,
			"streaming",
			version=3,
			reserved=1,
			assistant_message=amsg,
			gateway_run_id=rid,
			dispatching_at=frappe.utils.now(),
			dispatch_payload=json.dumps({"session_key": f"s-{rid}", "message": "hi"}),
		)
		self._kill()
		rec = _Recorder()
		deps = self._deps()
		deps.enqueue_pump_job = rec

		def boom(target, epoch):  # a legacy hop must never even build the transport
			raise AssertionError("make_mux must not be called under the kill switch")

		deps.make_mux = boom
		res = pump.run_pump_hop(self._target, deps=deps, soft_budget_s=999)
		self.assertEqual(res["exit"], "kill_switch")
		self.assertEqual(rec.count, 0, "hop-start kill: no successor enqueued")
		self.assertEqual(self._state(rid), "recovering")

	def test_transport_exit_under_legacy_enqueues_no_successor(self):
		conv = self._mk_conv()
		rid = "pmp_kill_tx"
		ctx = self._make_ctx(self._deps(), hop_counter=2, with_mux=False)
		self.assertTrue(ts.lease_renew(self._target, ctx.epoch))
		self._streaming_turn(ctx, rid, conv)  # live work
		self._kill()
		rec = _Recorder()
		ctx.deps.enqueue_pump_job = rec
		pump._schedule_successor_on_exit(ctx, transport_retry=0)
		self.assertEqual(rec.count, 0, "kill switch overrides transport-retry succession")
		self.assertEqual(self._state(rid), "recovering")

	def test_draining_still_hands_off(self):
		"""``draining`` is NOT the kill switch — the pump keeps draining in-flight (normal handoff)."""
		conv = self._mk_conv()
		rid = "pmp_drain_ho"
		ctx = self._make_ctx(self._deps(), hop_counter=5, with_mux=False)
		self.assertTrue(ts.lease_renew(self._target, ctx.epoch))
		pump._clear_lease_mirror(self._target)
		self._streaming_turn(ctx, rid, conv)
		pump.set_transport_mode(self._target, pump._MODE_DRAINING)
		frappe.db.commit()
		rec = _Recorder()
		ctx.deps.enqueue_pump_job = rec
		pump._handoff(ctx)
		self.assertEqual(rec.count, 1, "draining still hands off to finish in-flight work")
		self.assertEqual(self._state(rid), "streaming", "draining does not park live turns")


# --------------------------------------------------------------------------- #
# 15c. CDX-21 (Residual A) — the watchdog reconciles the default mirror every cycle
# --------------------------------------------------------------------------- #


class TestWatchdogMirrorReconcile(_PumpTestCase):
	def test_reconciles_default_mirror_with_no_open_turns(self):
		"""Ruling #3: the OLD watchdog derived its target set from open turns/effects and reconciled
		the mirror INSIDE that loop, so an IDLE site never healed a divergent mirror. The fix
		reconciles the default control row's mirror EVERY cycle, outside the loop."""
		# No nonterminal turns exist for this fresh suite target; assert reconcile still runs once.
		with patch.object(pump, "reconcile_config_mirror") as rc:
			pump.watchdog(deps=self._deps())
		rc.assert_called_once_with(pump.DEFAULT_TARGET)

	def test_idle_site_mirror_divergence_healed_in_one_cycle(self):
		# Induce a divergence on the DEFAULT shard: row says draining, config says pump. No open
		# turns anywhere -> the fix still repairs the file from the row in one watchdog pass.
		ts._ensure_control_row(pump.DEFAULT_TARGET)
		prev = frappe.db.get_value(PUMP, pump.DEFAULT_TARGET, "transport_mode")
		frappe.db.set_value(
			PUMP, pump.DEFAULT_TARGET, "transport_mode", pump._MODE_DRAINING, update_modified=False
		)
		frappe.db.commit()
		pump._LIFECYCLE_MODE_CACHE.pop(pump.DEFAULT_TARGET, None)

		def _restore():
			frappe.db.set_value(PUMP, pump.DEFAULT_TARGET, "transport_mode", prev, update_modified=False)
			frappe.db.set_value(PUMP, pump.DEFAULT_TARGET, "mirror_mismatch", 0, update_modified=False)
			frappe.db.commit()
			pump._LIFECYCLE_MODE_CACHE.pop(pump.DEFAULT_TARGET, None)

		self.addCleanup(_restore)
		with (
			patch.object(pump, "_config_transport_mode", return_value=pump._MODE_PUMP),
			patch("frappe.installer.update_site_config") as usc,
		):
			pump.watchdog(deps=self._deps())
		usc.assert_called_once_with("jarvis_pump_enabled", pump._mirror_value_for_mode(pump._MODE_DRAINING))


# --------------------------------------------------------------------------- #
# 16. CDX-2 — ready -> dispatching is epoch-fenced in the mutation
# --------------------------------------------------------------------------- #


class TestReadyDispatchEpochFence(_PumpTestCase):
	def test_stale_pump_cannot_confirm_dispatching_after_takeover(self):
		"""CDX-2: a pump that read a `ready` row at epoch E but paused past a takeover
		(which does NOT re-stamp `ready`) must lose the ready->dispatching CAS, because
		it proves the SHARD control row still holds E in the same statement."""
		conv = self._mk_conv()
		rid = "pmp_cdx2"
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
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		# Pump A acquires epoch E and reads the ready row (version 2).
		won, E = ts.lease_acquire(self._target, "hopA")
		self.assertTrue(won)
		# A takeover between A's read and A's CAS: pump B acquires E+1. `ready` is NOT in
		# EPOCH_INFLIGHT_STATES so the row is not re-stamped — the CDX-2 window.
		frappe.db.set_value(
			PUMP,
			self._target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.commit()
		pump._clear_lease_mirror(self._target)
		wonB, E2 = ts.lease_acquire(self._target, "hopB")
		self.assertTrue(wonB)
		self.assertEqual(E2, E + 1)
		# Stale pump A loses; the ready row is untouched.
		self.assertFalse(
			ts.confirm_dispatching(rid, 2, E, self._target),
			"a stale-epoch pump cannot confirm_dispatching (CDX-2)",
		)
		self.assertEqual(self._state(rid), "ready")
		# Fresh pump B wins.
		self.assertTrue(ts.confirm_dispatching(rid, 2, E2, self._target))
		frappe.db.commit()
		self.assertEqual(self._state(rid), "dispatching")
		self.assertEqual(int(self._val(rid, "pump_epoch")), E2)

	def test_dispatch_one_stale_shard_epoch_routes_to_lease_loss(self):
		"""CDX-2: _dispatch_one's 0-rows branch re-reads the CONTROL epoch — a moved
		shard epoch (a takeover) routes through the shared lease-loss exit."""
		conv = self._mk_conv()
		rid = "pmp_cdx2b"
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
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		double = self._double()
		ctx = self._make_ctx(self._deps(double=double))  # epoch E
		# A takeover bumps the shard epoch AFTER ctx acquired (ctx is now stale).
		frappe.db.set_value(PUMP, self._target, "pump_epoch", ctx.epoch + 3, update_modified=False)
		frappe.db.commit()
		with self.assertRaises(ts.LeaseLostExit):
			pump._dispatch_one(ctx, rid)
		self.assertEqual(self._state(rid), "ready", "no dispatch by the stale pump")


# --------------------------------------------------------------------------- #
# 17. CDX-3 — publish fencing (server belt + payload shape)
# --------------------------------------------------------------------------- #


class TestPublishFencing(_PumpTestCase):
	def test_stale_pump_publish_belt_skips_after_takeover(self):
		won, E = ts.lease_acquire(self._target, "hopA")
		self.assertTrue(won)
		captured = []
		with patch.object(ts, "publish_to_user", side_effect=lambda u, p: captured.append(p)):
			ts.publish_fenced(
				"u@x",
				"assistant:delta",
				conversation_id="c",
				run_id="r",
				event_seq=1,
				pump_epoch=E,
				relay_target_id=self._target,
				text="hi",
			)
			self.assertEqual(len(captured), 1)
			self.assertEqual(captured[0]["pump_epoch"], E)
			self.assertEqual(captured[0]["event_seq"], 1)
			# A takeover bumps the shard epoch.
			frappe.db.set_value(PUMP, self._target, "pump_epoch", E + 1, update_modified=False)
			frappe.db.commit()
			# The stale pump's publish is belt-skipped server-side.
			ts.publish_fenced(
				"u@x",
				"assistant:delta",
				conversation_id="c",
				run_id="r",
				event_seq=2,
				pump_epoch=E,
				relay_target_id=self._target,
				text="stale",
			)
			self.assertEqual(len(captured), 1, "stale-epoch publish belt-skipped")
			# The fresh owner publishes fine.
			ts.publish_fenced(
				"u@x",
				"assistant:delta",
				conversation_id="c",
				run_id="r",
				event_seq=3,
				pump_epoch=E + 1,
				relay_target_id=self._target,
				text="fresh",
			)
			self.assertEqual(len(captured), 2)

	def test_pump_lifecycle_events_carry_epoch_and_seq(self):
		"""CDX-3 payload shape: run:start / assistant:delta / run:end all carry
		pump_epoch (and assistant:delta an event_seq) so the client can fence a stale
		writer end-to-end."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="hello")
		rid = "pmp_cdx3_shape"
		self._mk_turn(conv, rid, seed, "queued", version=0, reserved=0)
		double = self._double()
		captured = []
		deps = self._deps(double=double, prepare=self._prepare_stub(conv, double, "success"))
		ctx = self._make_ctx(deps)
		with patch.object(ts, "publish_to_user", side_effect=lambda u, p: captured.append(p)):
			self._pump_until(ctx, lambda: any(p.get("kind") == "run:end" for p in captured))
		by_kind = {}
		for p in captured:
			by_kind.setdefault(p.get("kind"), p)
		self.assertIn("run:start", by_kind)
		self.assertEqual(by_kind["run:start"]["pump_epoch"], ctx.epoch)
		self.assertIn("assistant:delta", by_kind)
		self.assertEqual(by_kind["assistant:delta"]["pump_epoch"], ctx.epoch)
		self.assertIsNotNone(by_kind["assistant:delta"].get("event_seq"))
		self.assertIn("run:end", by_kind)
		self.assertEqual(by_kind["run:end"]["pump_epoch"], ctx.epoch)


# --------------------------------------------------------------------------- #
# CDX-4 — watchdog EFFECT scan (recover a lost enqueue_finalize)
# --------------------------------------------------------------------------- #


class TestWatchdogEffectScan(_PumpTestCase):
	def test_terminal_turn_with_open_effects_reenqueues_finalize(self):
		# CDX-4: a crash after an errored/cancelled settlement commit but before
		# enqueue_finalize leaves owed effects on a TERMINAL turn — invisible to the
		# nonterminal-only scan. The independent effect scan re-enqueues finalize.
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		rid = "pmp_wd_openeff"
		self._mk_turn(conv, rid, seed, "errored", version=6)
		ts.insert_required_effects(rid, ("terminal_publish", "macro_advance"))
		frappe.db.commit()
		fin_rec = _Recorder()
		deps = pump.PumpDeps()
		deps.enqueue_finalize = fin_rec
		deps.enqueue_pump_job = _Recorder()
		with patch.object(pump, "pump_configured", return_value=True):
			pump.watchdog(deps=deps)
		enqueued = [c[0][0] for c in fin_rec.calls]
		self.assertIn(rid, enqueued, "terminal turn with open effects gets finalize re-enqueued")

	def test_finalizing_turn_reenqueued_exactly_once(self):
		# The finalizing branch and the effect scan must not BOTH re-enqueue the same
		# turn (the effect scan skips turns the finalizing branch already handled).
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		rid = "pmp_wd_fin1"
		self._mk_turn(conv, rid, seed, "finalizing", version=6)
		ts.insert_required_effects(rid, ("terminal_publish",))
		frappe.db.commit()
		fin_rec = _Recorder()
		deps = pump.PumpDeps()
		deps.enqueue_finalize = fin_rec
		deps.enqueue_pump_job = _Recorder()
		with patch.object(pump, "pump_configured", return_value=True):
			pump.watchdog(deps=deps)
		enqueued = [c[0][0] for c in fin_rec.calls]
		self.assertEqual(enqueued.count(rid), 1, "finalizing turn re-enqueued exactly once")

	def test_live_running_effect_not_reenqueued(self):
		# A turn whose only open effect is a LIVE (non-stale) running claim is NOT
		# re-enqueued — its finalizer owns it.
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		rid = "pmp_wd_live"
		self._mk_turn(conv, rid, seed, "errored", version=6)
		ts.insert_required_effects(rid, ("macro_advance",))
		frappe.db.commit()
		o, tok = ts.claim_effect(rid, "macro_advance")  # live running claim
		frappe.db.commit()
		self.assertEqual(o, "attempt")
		fin_rec = _Recorder()
		deps = pump.PumpDeps()
		deps.enqueue_finalize = fin_rec
		deps.enqueue_pump_job = _Recorder()
		with patch.object(pump, "pump_configured", return_value=True):
			pump.watchdog(deps=deps)
		self.assertNotIn(rid, [c[0][0] for c in fin_rec.calls], "live claim excluded from the scan")


# --------------------------------------------------------------------------- #
# CDX-11 — conservative admission when gateway visibility is degraded
# --------------------------------------------------------------------------- #


class TestConservativeAdmission(_PumpTestCase):
	def _queue_cold(self, n):
		rids = []
		for i in range(n):
			c = self._mk_conv()
			s = self._mk_msg(c)
			rid = f"pmp_cons_{i}_{frappe.generate_hash(length=4)}"
			self._mk_turn(c, rid, s, "queued", version=0, reserved=0)
			rids.append(rid)
		return rids

	def test_snapshot_failure_admits_zero_until_snapshot_succeeds(self):
		# CDX-11 (fail CLOSED): unknown gateway visibility (failed snapshot AND no
		# TTL-valid last-known) => ZERO new promotions, EVERY cycle, until a snapshot
		# succeeds. The old cap-1 compromise is dropped — with an invisible foreign run
		# any positive local admission can oversubscribe the container.
		frappe.cache().delete_value(pump._gateway_active_key(self._target))
		deps = self._deps(
			snapshot=lambda c: {"snapshot_ok": False, "gateway_active": None, "active_session_keys": None}
		)
		ctx = self._make_ctx(deps, with_mux=False)
		pump._reconcile_gateway_active(ctx)
		self.assertFalse(ctx.gateway_active_known, "snapshot failure w/ no last-known => UNKNOWN, not zero")
		self._queue_cold(4)
		# Every batch while unknown admits ZERO (never the full cap, never cap-1).
		self.assertEqual(pump._promote_queued(ctx), 0, "batch 1: fail closed, zero promotions")
		self.assertEqual(pump._promote_queued(ctx), 0, "batch 2: STILL zero — no cap-1 compromise")
		# Once a snapshot SUCCEEDS (foreign=0), admission resumes at the real cap.
		ctx.deps.snapshot = lambda c: {"snapshot_ok": True, "gateway_active": 0, "active_session_keys": set()}
		pump._reconcile_gateway_active(ctx)
		self.assertTrue(ctx.gateway_active_known)
		from jarvis.chat import admission

		self.assertEqual(pump._promote_queued(ctx), admission._max_inflight(), "full cap once visible again")

	def test_snapshot_failure_reuses_last_known_within_ttl(self):
		# CDX-11: a failed snapshot with a RECENT last-known observation reuses it
		# (KNOWN, not conservative) instead of holding.
		pump._write_last_known_gateway_active(self._target, 1)
		deps = self._deps(
			snapshot=lambda c: {"snapshot_ok": False, "gateway_active": None, "active_session_keys": None}
		)
		ctx = self._make_ctx(deps, with_mux=False)
		pump._reconcile_gateway_active(ctx)
		self.assertTrue(ctx.gateway_active_known, "recent last-known is trusted")
		self.assertEqual(ctx.gateway_active, 1)

	def test_foreign_run_after_hop_start_refreshed_before_promote(self):
		# CDX-11 + CDX-17: a foreign run that begins AFTER the hop-start snapshot is caught
		# by the mid-hop capacity refresh (issue-and-poll) before over-admitting — the
		# refresh RESOLVES on a poll slice, never a blocking re-snapshot inside promote.
		foreign = {"n": 0}
		deps = self._deps(
			issue_snapshot=lambda c: _ResolvedSnap(
				{"snapshot_ok": True, "gateway_active": foreign["n"], "active_session_keys": set()}
			)
		)
		ctx = self._make_ctx(deps, with_mux=False)
		pump._reconcile_gateway_active(
			ctx, {"snapshot_ok": True, "gateway_active": 0, "active_session_keys": set()}
		)  # hop start: 0 foreign
		self.assertEqual(ctx.gateway_active, 0)
		foreign["n"] = 4  # a foreign run fills the whole cap mid-hop
		ctx.last_snapshot_mono -= pump.SNAPSHOT_REFRESH_S + 1  # force the refresh cadence
		self._queue_cold(1)
		# Issue + poll the mid-hop refresh (what drain_slice does around promote).
		pump._maybe_refresh_capacity(ctx)
		self.assertIsNotNone(ctx.pending_snapshot, "refresh issued (non-blocking)")
		pump._poll_snapshot(ctx)
		self.assertIsNone(ctx.pending_snapshot, "resolved refresh folded into capacity")
		self.assertEqual(ctx.gateway_active, 4, "capacity refreshed before the batch")
		self.assertEqual(pump._promote_queued(ctx), 0, "no local admission while foreign usage fills the cap")

	def test_stalled_snapshot_rpc_does_not_delay_delta_application(self):
		# CDX-17: a stalled sessions.list capacity RPC must NOT block a slice / delta
		# application. Drive frames while the control RPC hangs and prove the slice does
		# not wait ~ACK_TIMEOUT_S and the streaming delta still lands.
		# NB (CDX-11 interaction): a RESERVED (reserve-on-send winner) turn is used here.
		# CDX-11 now holds NEW COLD promotions at zero while a capacity refresh is unresolved
		# (a permanently-hung gateway can never confirm the foreign count), so a cold turn
		# would be correctly held for the whole hang. A reserved turn already holds its credit
		# and promotes via _promote_queued step (a) — ungated — which is exactly the in-flight
		# lane CDX-17 protects: its deltas must keep applying while the control RPC hangs.
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="hello")
		rid = "pmp_snap_stall"
		self._mk_turn(conv, rid, seed, "queued", version=0, reserved=1)
		double = self._double()
		double.arm_sessions_list_hang()  # the control RPC never responds
		deps = self._deps(double=double, prepare=self._prepare_stub(conv, double, "success"))
		ctx = self._make_ctx(deps)  # real mux -> default issue_snapshot uses it
		# Force the mid-hop refresh cadence so the FIRST slice issues the (hanging) RPC.
		ctx.last_snapshot_mono -= pump.SNAPSHOT_REFRESH_S + 1
		t0 = time.monotonic()
		# A LARGE ack window: if ANY slice blocked on the control RPC it would wait ~30s;
		# a non-blocking issue-and-poll finishes the turn in a couple of seconds with the
		# snapshot STILL pending. Both the wall time AND the still-pending future prove it.
		with patch.object(pump, "ACK_TIMEOUT_S", 30.0):
			outcome = self._pump_until(ctx, lambda: self._state(rid) in ("finalizing", "done"), max_slices=60)
		elapsed = time.monotonic() - t0
		self.assertIn(self._state(rid), ("finalizing", "done"), f"turn progressed (outcome={outcome})")
		self.assertLess(elapsed, 15.0, "CDX-17: a hanging control RPC did NOT block delta application")
		self.assertIsNotNone(ctx.pending_snapshot, "the capacity RPC is still in flight (issue-and-poll)")
		self.assertFalse(
			ctx.pending_snapshot.fut.done, "the control RPC genuinely hung — no slice waited on it"
		)
		# The delta content landed on the assistant placeholder despite the stalled RPC.
		amsg = self._val(rid, "assistant_message")
		self.assertTrue((frappe.db.get_value(MSG, amsg, "content") or "").strip(), "deltas applied")

	def test_drain_slice_folds_resolved_refresh_before_promotion(self):
		# CDX-11 (a) — at the drain_slice level (NOT a manually reordered helper): a DUE
		# capacity refresh that has already RESOLVED with a RAISED foreign count is folded
		# BEFORE promotion (drain_slice runs issue -> POLL -> promote), so a cold turn is never
		# admitted against the stale (pre-refresh) count. Hop start = 0 foreign (which WOULD
		# admit); the resolved refresh reports the cap full => the cold turn must be HELD.
		from jarvis.chat import admission

		high = admission._max_inflight() + pump.SAFETY_RESERVE + 5
		deps = self._deps(
			issue_snapshot=lambda c: _ResolvedSnap(
				{"snapshot_ok": True, "gateway_active": high, "active_session_keys": set()}
			)
		)
		ctx = self._make_ctx(deps, with_mux=False)
		pump._reconcile_gateway_active(
			ctx, {"snapshot_ok": True, "gateway_active": 0, "active_session_keys": set()}
		)
		self.assertEqual(ctx.gateway_active, 0, "hop start: cap free (the stale count WOULD admit)")
		(cold,) = self._queue_cold(1)
		ctx.last_snapshot_mono -= pump.SNAPSHOT_REFRESH_S + 1  # force the refresh cadence
		pump.drain_slice(ctx)
		self.assertIsNone(ctx.pending_snapshot, "resolved refresh folded (poll ran BEFORE promote)")
		self.assertEqual(ctx.gateway_active, high, "the RAISED foreign count was folded before promotion")
		self.assertEqual(
			self._state(cold), "queued", "CDX-11: no cold promotion against the stale zero count"
		)
		self.assertEqual(
			int(self._val(cold, "reserved") or 0), 0, "cold turn not reserved against the stale count"
		)

	def test_drain_slice_holds_cold_promotions_while_snapshot_stalls_but_deltas_flow(self):
		# CDX-11 (b) — at the drain_slice level: a permanently STALLED capacity refresh holds
		# NEW cold promotions at ZERO (the foreign count is unconfirmed) while in-flight lanes
		# keep applying deltas. A reserved (reserve-on-send winner) turn streams to terminal via
		# the UNGATED step (a); a cold turn on another conversation stays 'queued' throughout.
		convA = self._mk_conv()
		seedA = self._mk_msg(convA, content="hi")
		ridA = "pmp_cdx11_stream"
		self._mk_turn(convA, ridA, seedA, "queued", version=0, reserved=1)  # reserve-on-send winner
		convB = self._mk_conv()
		seedB = self._mk_msg(convB, content="later")
		ridB = "pmp_cdx11_cold"
		self._mk_turn(convB, ridB, seedB, "queued", version=0, reserved=0)  # cold
		double = self._double()
		double.arm_sessions_list_hang()  # the capacity RPC never responds
		deps = self._deps(double=double, prepare=self._prepare_stub(convA, double, "success"))
		ctx = self._make_ctx(deps)  # real mux
		ctx.last_snapshot_mono -= pump.SNAPSHOT_REFRESH_S + 1  # force the (hanging) refresh cadence
		with patch.object(pump, "ACK_TIMEOUT_S", 30.0):
			outcome = self._pump_until(
				ctx, lambda: self._state(ridA) in ("finalizing", "done"), max_slices=60
			)
		self.assertIn(
			self._state(ridA),
			("finalizing", "done"),
			f"reserved lane streamed to terminal (outcome={outcome})",
		)
		self.assertIsNotNone(ctx.pending_snapshot, "the capacity refresh is STILL stalled (permanent hang)")
		self.assertEqual(
			self._state(ridB), "queued", "CDX-11: the cold turn was HELD while the refresh stalled"
		)
		self.assertEqual(
			int(self._val(ridB, "reserved") or 0), 0, "cold turn never reserved while unconfirmed"
		)
		# Deltas continued: the in-flight (reserved) lane's placeholder got the streamed content.
		amsgA = self._val(ridA, "assistant_message")
		self.assertTrue(
			(frappe.db.get_value(MSG, amsgA, "content") or "").strip(), "deltas applied to the in-flight lane"
		)
