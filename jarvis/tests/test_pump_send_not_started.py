"""#773 review (PR #2201): a turn whose ``chat.send`` provably never left after the
pump claimed it goes back to ``ready`` instead of failing.

The pump claims a turn (``ready -> dispatching``, a committed CAS), publishes
``run:start`` and only then writes the frame. If the write is refused before any
byte left (the shared socket stayed busy, the relay closed, or the claim itself
took so long that the write would outrun the lease), the customer must not see an
error for a request nothing was ever sent for. The turn returns to ``ready`` under
its original key, payload, credit and queue position, and the next slice (or the
successor hop) sends it exactly once.

What is proven here, against the private test database with the real turn CAS
library, real ``AgentSession``/``RelayMux`` over a local TLS socket pair, and other
workers on their OWN database connections (a web request's Stop, a takeover, a
row lock):

* the refusal causes: a real row lock delaying the claim past the write window,
  a relay lost inside the claim window, a socket error before the first raw send;
* Stop on both sides of the refusal, its ``run:end`` epoch, and that Stop without
  a run id (or for another run) keeps today's contract;
* a takeover before or after the refusal: the old pump writes nothing more;
* an unexplained version change: retried once, or parked when the gateway may
  have the run;
* a remote ``send-not-started`` code is NOT local proof (negative control);
* a refused ``chat.abort`` keeps the Stop until a hop can deliver it;
* shared chats: the preflight refusal, the post-claim refusal, a revoked grant,
  and an actor's or owner's Stop inside the claim window.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from unittest.mock import Mock, patch

import frappe

from jarvis.chat import empty_reply_recovery, pump
from jarvis.chat import turn_state as ts
from jarvis.chat.agent_client import SendNotStarted, SendUncertain
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests import _tls_pair as tp
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests.race_harness import as_job
from jarvis.tests.test_pump import PUMP, TARGET_PREFIX
from jarvis.tests.test_pump_io_budget import _IoPumpCase, _PublishLog, _wait_for

TURN = "Jarvis Chat Turn"
MSG = "Jarvis Chat Message"
EFFECT = "Jarvis Turn Effect"

# Every column the claim or its compensation may touch, plus the ones it must keep.
_ROW = [
	"state",
	"version",
	"ready_at",
	"reserved",
	"reservation_expires_at",
	"pump_epoch",
	"dispatching_at",
	"deadline_at",
	"collaboration_send_started_at",
	"collaboration_retry_allowed",
	"cancel_requested",
	"gateway_run_id",
	"first_event_at",
	"last_event_seq",
	"dispatch_payload",
	"assistant_message",
	"error",
]


def setUpModule():
	install_synthetic_runtime_profile()


class _Elsewhere(threading.Thread):
	"""Another process's work (a web request, a second pump) on its OWN thread and
	database connection. ``fn(worker)`` runs inside; its return value or exception
	is handed back by :func:`elsewhere`."""

	def __init__(self, fn, *, user: str | None = None):
		super().__init__(daemon=True)
		self.fn, self.user = fn, user
		self.site, self.sites_path = frappe.local.site, frappe.local.sites_path
		self.result = None
		self.error: BaseException | None = None
		self.ready = threading.Event()

	def run(self) -> None:
		frappe.init(site=self.site, sites_path=self.sites_path)
		frappe.connect()
		frappe.flags.in_test = True
		try:
			if self.user:
				frappe.set_user(self.user)
			self.result = self.fn(self)
			frappe.db.commit()
		except BaseException as e:
			self.error = e
		finally:
			try:
				frappe.db.rollback()
			finally:
				frappe.destroy()


def elsewhere(fn, *, user: str | None = None, timeout: float = 30.0):
	worker = _Elsewhere(fn, user=user)
	worker.start()
	worker.join(timeout)
	if worker.is_alive():
		raise AssertionError("the other worker did not finish")
	if worker.error is not None:
		raise worker.error
	return worker.result


def _takeover(target: str) -> int:
	"""A second pump takes the shard over (its lease looked expired): the real
	``lease_acquire``, which bumps the epoch and re-stamps in-flight turns."""

	def run(_w):
		frappe.db.set_value(
			PUMP,
			target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.commit()
		won, epoch = ts.lease_acquire(target, "takeover-holder")
		assert won, "premise: the takeover won the lease"
		return epoch

	return elsewhere(run)


def _row(rid) -> dict:
	return dict(frappe.db.get_value(TURN, rid, _ROW, as_dict=True))


def _in_window(published: _PublishLog, rid: str, action):
	"""``ts.publish_fenced`` that runs ``action`` once, right after the claim's
	``run:start`` (between the committed claim and the frame write)."""
	fired: list = []

	def publish(user, event, **fields):
		published(user, event, **fields)
		if event == "run:start" and fields.get("run_id") == rid and not fired:
			fired.append(1)
			action()

	return publish


class _UnsentCase(_IoPumpCase):
	def setUp(self):
		super().setUp()
		self._job = as_job()
		self._job.__enter__()
		self._switch = frappe.local.conf.get(empty_reply_recovery.SWITCH)
		frappe.local.conf[empty_reply_recovery.SWITCH] = 0
		self.woken: list = []
		for name in ("ensure_pump", "lpush_wake"):
			p = patch.object(pump, name, lambda *a, **_k: self.woken.append(a))
			p.start()
			self.addCleanup(p.stop)

	def tearDown(self):
		if self._switch is None:
			frappe.local.conf.pop(empty_reply_recovery.SWITCH, None)
		else:
			frappe.local.conf[empty_reply_recovery.SWITCH] = self._switch
		self._job.__exit__(None, None, None)
		super().tearDown()

	def _server(self):
		return self._socks[-1]

	def _drop_relay(self, peer, ctx) -> None:
		"""The gateway goes away: the peer stops and closes its side."""
		peer.stop()
		try:
			self._server().close()
		except OSError:
			pass
		self.assertTrue(_wait_for(ctx.mux.is_closed, 5), "premise: the reader saw the relay close")

	def _refuse(self, ctx) -> None:
		"""The mux refuses the write before any byte: no time left in its window."""
		ctx.mux.set_write_deadline(pump._monotonic())

	def _drain_until(self, ctx, predicate, slices=40):
		for _ in range(slices):
			outcome = pump.drain_slice(ctx)
			if predicate() or outcome in ("idle_exit", "transport_closed"):
				return outcome
		return "timeout"

	def assert_returned(self, rid, before: dict, ctx, *, epoch: int, peer=None, published=None):
		"""UNSENT-RETURN: the row is its pre-claim self apart from version (+2) and the
		server-only return marker; nothing reached the gateway; nobody was told it failed."""
		after = _row(rid)
		self.assertEqual(after["state"], "ready", f"{rid}: a never-sent request is not a failure")
		self.assertEqual(int(after["version"]), int(before["version"]) + 2)
		for f in _ROW:
			if f not in ("state", "version", "dispatch_payload"):
				self.assertEqual(after[f], before[f], f"{rid}: {f} must be its pre-claim value")
		stored = json.loads(after["dispatch_payload"])
		marker = stored.pop("unsent_return", None)
		self.assertEqual(stored, json.loads(before["dispatch_payload"]), "the request is unchanged")
		self.assertEqual(marker, {"version": int(before["version"]) + 2, "epoch": epoch})
		msg = frappe.db.get_value(MSG, before["assistant_message"], ["streaming", "error"], as_dict=True)
		self.assertEqual(int(msg.streaming), 1, "the reply keeps its live state")
		self.assertFalse(msg.error)
		self.assertNotIn(rid, ctx.runs)
		self.assertNotIn(rid, ctx.pending_acks)
		if ctx.mux is not None and not isinstance(ctx.mux, Mock):
			self.assertNotIn(rid, ctx.mux._runs, "no ghost lane")
			self.assertEqual(ctx.mux._pending, {}, "no ghost pending RPC")
		if published is not None:
			names = published.names(rid)
			self.assertNotIn("run:error", names)
			self.assertFalse(any("changed_data" in f for _e, f in published.for_run(rid)))
		if peer is not None:
			time.sleep(0.2)
			self.assertEqual(
				[s for s in tp.chat_sends(peer) if s["params"]["idempotencyKey"].startswith(rid)], []
			)


# --------------------------------------------------------------------------- #
# The refusal causes
# --------------------------------------------------------------------------- #


class TestClaimDelayedPastTheWriteWindow(_UnsentCase):
	"""The review's MAJOR, with production constants: the lease admits the write with
	0.3s to spare, then a web request holds the turn row locked for 7s, so the claim
	itself waits on a real InnoDB row lock. By the time the frame could go out it
	would outrun the lease, so the mux refuses it before any byte. Before: the turn
	``errored`` and the customer read ``run:error``."""

	def test_row_lock_stall_returns_the_turn_and_the_next_slice_sends_it_once(self):
		conv = self._mk_conv()
		rid = "pmp_nsl_lock"
		self._seed_ready(conv, rid, message="the BOE upload")
		before = _row(rid)
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=True, final_text="answer"))
		ctx = self._make_ctx(self._socket_deps(sess))
		ctx.last_snapshot_mono = pump._monotonic()  # no capacity refresh in these slices
		locked = threading.Event()

		def hold_row(_w):
			frappe.db.sql(f"SELECT name FROM `tab{TURN}` WHERE name=%s FOR UPDATE", (rid,))
			locked.set()
			time.sleep(7.0)

		holder = _Elsewhere(hold_row)
		holder.start()
		self.assertTrue(locked.wait(10))
		expires_before = frappe.db.get_value(PUMP, self._target, "lease_expires_at")
		ctx.lease_valid_until = pump._monotonic() + 23.3
		pump._sync_io_deadline(ctx)
		published = _PublishLog()
		with patch.object(ts, "publish_fenced", side_effect=published):
			pump.drain_slice(ctx)
		holder.join(10)
		self.assertIn("run:start", published.names(rid), "premise: the turn was claimed")
		self.assert_returned(rid, before, ctx, epoch=ctx.epoch, peer=peer, published=published)
		self.assertGreater(
			frappe.utils.get_datetime(frappe.db.get_value(PUMP, self._target, "lease_expires_at")),
			frappe.utils.get_datetime(expires_before),
			"the slice renews the lease at once",
		)
		self.assertGreater(ctx.lease_valid_until, pump._monotonic() + 25)
		self.assertFalse(ctx.mux.is_closed(), "a refusal before any byte keeps the transport")
		with patch.object(ts, "publish_fenced", side_effect=published):
			self._drain_until(ctx, lambda: self._settled(rid))
		self.assertTrue(self._settled(rid), self._state(rid))
		self.assertEqual([s["params"]["idempotencyKey"] for s in tp.chat_sends(peer)], [rid])
		self.assertNotIn("run:error", published.names(rid))


class TestRelayLostInTheClaimWindow(_UnsentCase):
	"""The gateway connection drops between the claim and the write (a gateway
	restart): the mux refuses as closed. The turn returns; the hop ends; the
	successor on a fresh connection sends it once with the same key. Before #773
	this race parked the turn ``recovering``; at 43c05eb85 it errored."""

	def test_successor_sends_the_returned_turn_once(self):
		conv = self._mk_conv()
		rid = "pmp_nsl_closed"
		self._seed_ready(conv, rid)
		before = _row(rid)
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))
		published = _PublishLog()
		hook = _in_window(published, rid, lambda: self._drop_relay(peer, ctx))
		with patch.object(ts, "publish_fenced", side_effect=hook):
			outcome = pump.drain_slice(ctx)
		self.assert_returned(rid, before, ctx, epoch=ctx.epoch, published=published)
		self.assertEqual(outcome, "transport_closed")
		self.assertEqual(tp.chat_sends(peer), [])

		pump._schedule_successor_on_exit(ctx, transport_retry=0)
		sess2, peer2 = self._socket_mux(on_message=tp.rpc_responder(chat_final=True))
		ctx2 = self._make_ctx(self._socket_deps(sess2), holder="hop-test-2", hop_counter=1)
		with patch.object(ts, "publish_fenced", side_effect=published):
			pump._reconcile_on_start(ctx2)
			self._drain_until(ctx2, lambda: self._settled(rid))
		self.assertTrue(self._settled(rid), self._state(rid))
		self.assertEqual([s["params"]["idempotencyKey"] for s in tp.chat_sends(peer2)], [rid])
		self.assertNotIn("run:error", published.names(rid))


class TestSocketErrorBeforeTheFirstRawSend(_UnsentCase):
	"""A socket error before the first raw send of the frame: nothing of it left,
	so the turn returns, but the socket may be broken, so the transport is closed
	AT ONCE: no second claim and no second ``run:start`` in this hop. One warning
	names the method and the error class only. Before: ambiguous, ``recovering``."""

	def test_turn_returns_and_the_hop_stops_claiming(self):
		conv1, conv2 = self._mk_conv(), self._mk_conv()
		first, second = "pmp_nsl_raw1", "pmp_nsl_raw2"
		self._seed_ready(conv1, first, message="secret prompt text")
		self._seed_ready(conv2, second)
		before1, before2 = _row(first), _row(second)
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))
		read_before = peer.bytes_read
		real_budget = sess._io._send_budget
		failed: list = []

		def budget():
			if not failed:
				failed.append(1)
				raise TimeoutError("timed out")
			return real_budget()

		records: list[logging.LogRecord] = []
		handler = logging.Handler()
		handler.emit = records.append
		mux_log = logging.getLogger("jarvis.chat.relay_mux")
		mux_log.addHandler(handler)
		published = _PublishLog()
		try:
			with (
				patch.object(sess._io, "_send_budget", side_effect=budget),
				patch.object(ts, "publish_fenced", side_effect=published),
			):
				pump._dispatch_ready(ctx)
		finally:
			mux_log.removeHandler(handler)
		self.assertNotIn("run:recovering", published.names(first), "nothing was sent: not ambiguous")
		self.assert_returned(first, before1, ctx, epoch=ctx.epoch, published=published)
		self.assertTrue(ctx.mux.is_closed(), "the hop knows at once that the socket is gone")
		self.assertEqual(_row(second), before2, "no second claim on a broken socket")
		self.assertEqual([e for e, _f in published.events], ["run:start"])
		time.sleep(0.2)
		self.assertEqual(peer.bytes_read, read_before, "no byte of the frame reached the gateway")
		self.assertEqual(ctx.mux.stats().get("writes_poisoned"), 1)
		warnings = [r for r in records if r.levelno >= logging.WARNING]
		self.assertEqual(len(warnings), 1)
		text = warnings[0].getMessage()
		self.assertIn("chat.send", text)
		self.assertIn("SendNotStarted", text)
		for secret in ("secret prompt text", f"sess-{first}", "timed out"):
			self.assertNotIn(secret, text)


# --------------------------------------------------------------------------- #
# Stop around the refusal (personal chats)
# --------------------------------------------------------------------------- #


class TestStopAroundTheRefusal(_UnsentCase):
	def _dispatch_returned(self, conv, rid, **seed):
		"""Seed, claim, refuse: the turn is back in ``ready``. Returns (ctx, peer, before)."""
		self._seed_ready(conv, rid, **seed)
		before = _row(rid)
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))
		published = _PublishLog()
		with patch.object(
			ts, "publish_fenced", side_effect=_in_window(published, rid, lambda: self._refuse(ctx))
		):
			pump._dispatch_ready(ctx)
		self.assert_returned(rid, before, ctx, epoch=ctx.epoch, published=published)
		ctx.mux.set_write_deadline(None)
		pump._heartbeat_and_renew(ctx, False)  # the slice ends: forced renew, next wave allowed
		self.assertFalse(ctx.io_deferred)
		return ctx, peer, before

	def test_stop_landing_in_the_claim_window_cancels_without_any_frame(self):
		"""Stop (a web request on its own connection) lands after the claim, before
		the refused write. Nothing was sent, so nothing is aborted: the turn settles
		``cancelled`` (credit released), the reply shows stopped, one ``run:end``
		stopped at the claim's epoch. Before: ``errored`` plus ``run:error``."""
		conv = self._mk_conv()
		rid = "pmp_nsl_stop1"
		amsg = self._seed_ready(conv, rid)
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))

		def stop_then_refuse():
			self.assertTrue(elsewhere(lambda _w: pump.request_cancel_conversation(conv, rid)))
			self._refuse(ctx)

		published = _PublishLog()
		with patch.object(ts, "publish_fenced", side_effect=_in_window(published, rid, stop_then_refuse)):
			pump._dispatch_ready(ctx)
		row = _row(rid)
		self.assertEqual(
			(row["state"], int(row["reserved"]), int(row["cancel_requested"])), ("cancelled", 0, 1)
		)
		msg = frappe.db.get_value(MSG, amsg, ["streaming", "stopped", "error"], as_dict=True)
		self.assertEqual((int(msg.streaming), int(msg.stopped)), (0, 1))
		self.assertFalse(msg.error)
		self.assertNotIn("run:error", published.names(rid))
		ends = [f for e, f in published.for_run(rid) if e == "run:end"]
		self.assertEqual(len(ends), 1)
		self.assertIs(ends[0].get("stopped"), True)
		self.assertEqual(ends[0].get("pump_epoch"), ctx.epoch)
		time.sleep(0.2)
		self.assertEqual([t.get("method") for t in peer.texts()], [], "no chat.send, no chat.abort")
		self.assertNotIn(rid, ctx.runs)
		self.assertEqual(ctx.mux._runs, {})

	def test_stop_after_the_return_cancels_by_run_id_only(self):
		"""Stop on the ready-again turn (re-send switch OFF). Today's contract stays for
		a Stop without a run id ("" from the PWA, None from older callers) and for a
		Stop naming another run: the turn stays ready. A Stop naming this run (the id
		the browser holds from ``run:start``, or restores on reload) cancels it, the
		reply shows stopped, and ``run:end`` carries the claim's epoch so every tab's
		fence takes it. Nothing is sent afterwards."""
		conv = self._mk_conv()
		rid = "pmp_nsl_stop2"
		ctx, peer, before = self._dispatch_returned(conv, rid)
		for other in (None, "", "pmp_some_other_run"):
			with self.subTest(run_id=other):
				self.assertFalse(pump.request_cancel_conversation(conv, other))
				self.assertEqual(self._state(rid), "ready")
		self.assertEqual(self.woken, [])
		published = _PublishLog()
		with patch.object(ts, "publish_fenced", side_effect=published):
			self.assertTrue(pump.request_cancel_conversation(conv, rid))
		row = _row(rid)
		self.assertEqual((row["state"], int(row["reserved"])), ("cancelled", 0))
		msg = frappe.db.get_value(MSG, before["assistant_message"], ["streaming", "stopped"], as_dict=True)
		self.assertEqual((int(msg.streaming), int(msg.stopped)), (0, 1))
		ends = [f for e, f in published.for_run(rid) if e == "run:end"]
		self.assertEqual(len(ends), 1)
		self.assertEqual(
			(ends[0]["message_id"], ends[0]["pump_epoch"], ends[0]["stopped"]),
			(before["assistant_message"], ctx.epoch, True),
		)
		self.assertIsInstance(ends[0]["pump_epoch"], int)
		self.assertEqual(self.woken, [(self._target,), (self._target, rid)])
		with patch.object(ts, "publish_fenced", side_effect=published):
			pump._dispatch_ready(ctx)
		time.sleep(0.2)
		self.assertEqual(tp.chat_sends(peer), [], "a stopped request is never sent")

	def test_the_gateway_key_form_names_the_returned_turn(self):
		conv = self._mk_conv()
		rid = "pmp_nsl_stop3"
		self._dispatch_returned(conv, rid)
		with patch.object(ts, "publish_fenced"):
			self.assertTrue(pump.request_cancel_conversation(conv, f"{rid}-r1"))
		self.assertEqual(self._state(rid), "cancelled")

	def test_a_lost_stop_against_a_redispatch_flags_the_dispatch_instead(self):
		"""Stop reads the ready-again turn, but the pump claims it again before Stop's
		cancel lands. Stop reads once more and flags the dispatch for the cancel sweep."""
		conv = self._mk_conv()
		rid = "pmp_nsl_stop4"
		self._dispatch_returned(conv, rid)
		real = ts.cancel_preparing_or_ready
		fired: list = []

		def reclaim_first(run_id, version):
			if not fired:
				fired.append(1)
				elsewhere(
					lambda _w: frappe.db.sql(
						f"UPDATE `tab{TURN}` SET state='dispatching', version=version+1 WHERE name=%s",
						(run_id,),
					)
				)
			return real(run_id, version)

		with (
			patch.object(ts, "cancel_preparing_or_ready", side_effect=reclaim_first),
			patch.object(ts, "publish_fenced"),
		):
			self.assertTrue(pump.request_cancel_conversation(conv, rid))
		row = _row(rid)
		self.assertEqual((row["state"], int(row["cancel_requested"])), ("dispatching", 1))

	def test_resent_turn_returned_at_a_later_epoch_ends_at_that_epoch(self):
		"""Re-send switch ON. A turn the empty-reply re-send put back (claimed first at
		E0) is claimed by a later hop at E > E0 and returned. Its restore keeps the
		first attempt's ``dispatching_at``/``deadline_at`` and E0; both markers coexist.
		Stop, with or without a run id, cancels it, and ``run:end`` carries E (the
		browser's fence sits at E since ``run:start``; E0 would be dropped). A returned
		re-sent turn that is not stopped goes out under its ``-r1`` key."""
		frappe.local.conf[empty_reply_recovery.SWITCH] = 1
		frappe.db.set_value(PUMP, self._target, "pump_epoch", 5, update_modified=False)
		frappe.db.commit()
		first_sent = frappe.utils.add_to_date(None, seconds=-40)
		resent = {"redispatch": 1, "redispatch_reason": empty_reply_recovery.REASON}
		for stop_with in (None, "named", "not stopped"):
			with self.subTest(stop_with=stop_with):
				# Each case is claimed by a fresh hop: the previous one's lease looks expired.
				frappe.db.set_value(
					PUMP, self._target, "lease_expires_at", frappe.utils.add_to_date(None, seconds=-1)
				)
				frappe.db.commit()
				conv = self._mk_conv()
				rid = f"pmp_nsl_rs_{(stop_with or 'none').replace(' ', '_')}"
				seed = self._mk_msg(conv)
				amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
				self._mk_turn(
					conv,
					rid,
					seed,
					"ready",
					version=6,
					pump_epoch=5,
					reserved=1,
					assistant_message=amsg,
					ready_at=frappe.utils.now(),
					dispatching_at=first_sent,
					deadline_at=frappe.utils.add_to_date(first_sent, seconds=600),
					dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi", **resent}),
				)
				before = _row(rid)
				sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
				ctx = self._make_ctx(self._socket_deps(sess))
				self.assertGreater(ctx.epoch, 5)
				published = _PublishLog()
				with patch.object(
					ts, "publish_fenced", side_effect=_in_window(published, rid, lambda: self._refuse(ctx))
				):
					pump._dispatch_ready(ctx)
				self.assert_returned(rid, before, ctx, epoch=ctx.epoch, published=published)
				self.assertTrue(empty_reply_recovery.resent_turn(rid))
				starts = [f for e, f in published.for_run(rid) if e == "run:start"]
				if stop_with == "not stopped":
					ctx.mux.set_write_deadline(None)
					pump._heartbeat_and_renew(ctx, False)  # the slice ends
					with patch.object(ts, "publish_fenced", side_effect=published):
						pump._dispatch_ready(ctx)
					self.assertTrue(_wait_for(lambda: tp.chat_sends(peer), 5))
					self.assertEqual(
						[s["params"]["idempotencyKey"] for s in tp.chat_sends(peer)], [f"{rid}-r1"]
					)
				else:
					with patch.object(ts, "publish_fenced", side_effect=published):
						self.assertTrue(pump.request_cancel_conversation(conv, rid if stop_with else None))
					self.assertEqual(self._state(rid), "cancelled")
					ends = [f for e, f in published.for_run(rid) if e == "run:end"]
					self.assertEqual(len(ends), 1)
					self.assertEqual(
						ends[0]["pump_epoch"], starts[0]["pump_epoch"], "the browser fence takes it"
					)


class TestStopReadRacesTheReturn(_UnsentCase):
	"""(Independent CODE review, adopted from the root's native probe.) Stop reads the
	turn while it is ``dispatching``, then the pump's refused write returns it to
	``ready`` and commits BEFORE Stop's cancel lands. That cancel matches nothing.
	Stop must read the present once more and cancel the returned turn by its marker,
	or the user's Stop is lost and the next slice sends the request anyway.

	The Stop runs as a web request would: its own thread and connection, with the
	re-send switch set in ITS config. Snapshot isolation: the server default, and the
	session forced OFF (older servers report a lost CAS as 0 rows) and ON (11.6.2+:
	a snapshot conflict, which the replay already handled)."""

	def _race(self, rid, *, resend: int, snapshot: str | None):
		conv = self._mk_conv()
		amsg = self._seed_ready(conv, rid)
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))
		stop_read, returned = threading.Event(), threading.Event()
		real_cancel = ts.request_cancel
		box: dict = {}

		def held_cancel(run_id, version):
			stop_read.set()  # the real Stop unit has read `dispatching`
			if not returned.wait(15):
				raise AssertionError("the pump never returned the turn")
			return real_cancel(run_id, version)

		def stopping(_w):
			frappe.local.conf[empty_reply_recovery.SWITCH] = resend
			if snapshot is not None:
				frappe.db.sql(f"SET SESSION innodb_snapshot_isolation={snapshot}")
			box["snapshot"] = frappe.db.sql("SELECT @@session.innodb_snapshot_isolation")[0][0]
			return pump.request_cancel_conversation(conv, rid)

		def stop_then_refuse():
			box["worker"] = _Elsewhere(stopping)
			box["worker"].start()
			self.assertTrue(stop_read.wait(10), "premise: Stop read the claimed turn first")
			self._refuse(ctx)  # a real mux refusal: the pump returns the turn

		published = _PublishLog()
		with (
			patch.object(ts, "request_cancel", side_effect=held_cancel),
			patch.object(ts, "publish_fenced", side_effect=_in_window(published, rid, stop_then_refuse)),
		):
			try:
				pump._dispatch_ready(ctx)
				self.assertEqual(self._state(rid), "ready", "premise: the refusal returned the turn")
			finally:
				returned.set()
				if "worker" in box:
					box["worker"].join(20)
		worker = box["worker"]
		self.assertFalse(worker.is_alive())
		if worker.error is not None:
			raise worker.error
		self.assertTrue(worker.result, f"the Stop was lost (snapshot isolation {box['snapshot']})")
		frappe.db.rollback()  # read the present, not this thread's older snapshot
		row = _row(rid)
		self.assertEqual((row["state"], int(row["reserved"])), ("cancelled", 0))
		msg = frappe.db.get_value(MSG, amsg, ["streaming", "stopped"], as_dict=True)
		self.assertEqual((int(msg.streaming), int(msg.stopped)), (0, 1), "the reply shows stopped")
		self.assertNotIn("run:error", published.names(rid))
		ends = [f for e, f in published.for_run(rid) if e == "run:end"]
		self.assertEqual(len(ends), 1)
		self.assertEqual(
			(ends[0]["stopped"], ends[0]["pump_epoch"]), (True, ctx.epoch), "at the claim's epoch"
		)
		self.assertIsInstance(ends[0]["pump_epoch"], int)
		self.assertIn((self._target, rid), self.woken)
		ctx.mux.set_write_deadline(None)
		pump._heartbeat_and_renew(ctx, False)  # the slice ends; the next one may send
		with patch.object(ts, "publish_fenced"):
			pump._dispatch_ready(ctx)
		time.sleep(0.3)
		sent = [s["params"]["idempotencyKey"] for s in tp.chat_sends(peer)]
		self.assertNotIn(rid, sent, "a stopped request is never sent")

	def test_stop_is_kept_whatever_the_switch_and_snapshot_isolation(self):
		for resend in (0, 1):
			for snapshot in (None, "OFF", "ON"):
				with self.subTest(resend=resend, snapshot=snapshot or "server default"):
					# Each case is claimed by a fresh hop: the previous one's lease looks expired.
					frappe.db.set_value(
						PUMP, self._target, "lease_expires_at", frappe.utils.add_to_date(None, seconds=-1)
					)
					frappe.db.commit()
					self.woken.clear()
					self._race(
						f"pmp_nsl_race_{resend}_{(snapshot or 'srv').lower()}",
						resend=resend,
						snapshot=snapshot,
					)


class TestReturnMarker(_UnsentCase):
	"""The Stop lookup trusts only a well-formed marker for the row's CURRENT
	version. Anything else keeps today's Stop exactly: no exception, the turn stays
	ready, and no read beyond the existing one."""

	def _ready_with(self, rid, payload):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
		self._mk_turn(
			conv,
			rid,
			seed,
			"ready",
			version=6,
			reserved=1,
			assistant_message=amsg,
			ready_at=frappe.utils.now(),
			dispatch_payload=payload,
		)
		return conv

	def test_invalid_markers_keep_todays_stop(self):
		base = {"session_key": "sess-x", "message": "hi"}
		cases = {
			"pmp_nsl_mk_null": None,
			"pmp_nsl_mk_text": "not json",
			"pmp_nsl_mk_list": json.dumps([1, 2]),
			"pmp_nsl_mk_none": json.dumps(base),
			"pmp_nsl_mk_nan": json.dumps({**base, "unsent_return": {"version": "x", "epoch": 3}}),
			"pmp_nsl_mk_noep": json.dumps({**base, "unsent_return": {"version": 6}}),
			"pmp_nsl_mk_stale": json.dumps({**base, "unsent_return": {"version": 5, "epoch": 3}}),
		}
		real = frappe.db.get_value
		for rid, payload in cases.items():
			with self.subTest(rid=rid):
				conv = self._ready_with(rid, payload)
				reads: list = []
				with patch.object(
					frappe.db, "get_value", side_effect=lambda *a, **k: (reads.append(a), real(*a, **k))[1]
				):
					self.assertFalse(pump.request_cancel_conversation(conv, rid))
				self.assertEqual(self._state(rid), "ready")
				self.assertEqual(len(reads), 1, reads)

	def test_a_current_marker_is_honoured(self):
		rid = "pmp_nsl_mk_ok"
		conv = self._ready_with(
			rid,
			json.dumps({"session_key": "s", "message": "hi", "unsent_return": {"version": 6, "epoch": 3}}),
		)
		published = _PublishLog()
		with patch.object(ts, "publish_fenced", side_effect=published):
			self.assertTrue(pump.request_cancel_conversation(conv, rid))
		self.assertEqual(self._state(rid), "cancelled")
		self.assertEqual([f["pump_epoch"] for e, f in published.for_run(rid) if e == "run:end"], [3])


# --------------------------------------------------------------------------- #
# Ownership and unexplained changes
# --------------------------------------------------------------------------- #


class TestTakeoverAroundTheRefusal(_UnsentCase):
	def test_takeover_inside_the_window_exits_before_any_other_write(self):
		"""Another pump takes the shard over between the claim and the refused write.
		The old pump must notice FIRST (on a fresh snapshot: its own predates the
		takeover), write nothing over the new owner's row, and claim nothing more."""
		conv1, conv2 = self._mk_conv(), self._mk_conv()
		first, second = "pmp_nsl_tk1", "pmp_nsl_tk2"
		self._seed_ready(conv1, first)
		self._seed_ready(conv2, second)
		before2 = _row(second)
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))
		owners: dict = {}

		def takeover_then_refuse():
			frappe.db.get_value(PUMP, self._target, "pump_epoch")  # the pump's snapshot is now open
			owners["epoch"] = _takeover(self._target)
			owners["row"] = elsewhere(lambda _w: _row(first))
			self._refuse(ctx)

		order: list[str] = []
		real_lost, real_rollback = pump._shard_epoch_lost, frappe.db.rollback
		published = _PublishLog()
		with (
			patch.object(
				ts, "publish_fenced", side_effect=_in_window(published, first, takeover_then_refuse)
			),
			patch.object(
				pump, "_shard_epoch_lost", side_effect=lambda c: (order.append("owner"), real_lost(c))[1]
			),
			patch.object(
				frappe.db,
				"rollback",
				side_effect=lambda *a, **k: (order.append("rollback"), real_rollback(*a, **k))[1],
			),
		):
			with self.assertRaises(ts.LeaseLostExit):
				pump._dispatch_ready(ctx)
		self.assertEqual(_row(first), owners["row"], "the new owner's row is untouched")
		self.assertEqual(int(owners["row"]["pump_epoch"]), owners["epoch"])
		self.assertEqual(_row(second), before2)
		self.assertIn("owner", order)
		self.assertEqual(order[order.index("owner") - 1], "rollback", "a fresh snapshot comes first")
		self.assertNotIn("run:error", published.names(first))
		time.sleep(0.2)
		self.assertEqual(peer.texts(), [], "no frame from the old owner")

	def test_takeover_after_the_return_exits_at_the_forced_renew(self):
		"""The turn was returned; then another pump takes the shard over. The slice's
		forced renew (the return marks the slice deferred) finds the lease gone and
		exits before any later write. The new owner finds an ordinary ready turn."""
		conv = self._mk_conv()
		rid = "pmp_nsl_tk3"
		self._seed_ready(conv, rid)
		before = _row(rid)
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))
		published = _PublishLog()
		with patch.object(
			ts, "publish_fenced", side_effect=_in_window(published, rid, lambda: self._refuse(ctx))
		):
			pump._dispatch_ready(ctx)
		self.assert_returned(rid, before, ctx, epoch=ctx.epoch, published=published)
		self.assertTrue(ctx.io_deferred)
		returned = _row(rid)
		_takeover(self._target)
		with self.assertRaises(ts.LeaseLostExit):
			pump._heartbeat_and_renew(ctx, False)
		self.assertEqual(_row(rid), returned)
		self.assertEqual(peer.texts(), [])

	def test_takeover_before_the_stop_cancel_leaves_the_new_owners_row(self):
		"""Stop landed in the window, and a takeover lands just before the old pump's
		never-sent cancel: that cancel is fenced on the shard epoch, matches nothing,
		and the old pump exits."""
		conv = self._mk_conv()
		rid = "pmp_nsl_tk4"
		self._seed_ready(conv, rid)
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))

		def stop_then_refuse():
			self.assertTrue(elsewhere(lambda _w: pump.request_cancel_conversation(conv, rid)))
			self._refuse(ctx)

		owners: dict = {}
		real = ts.record_aborted_terminal

		def takeover_first(*args, **kwargs):
			if "row" not in owners:
				_takeover(self._target)
				owners["row"] = elsewhere(lambda _w: _row(rid))
			return real(*args, **kwargs)

		published = _PublishLog()
		with (
			patch.object(ts, "publish_fenced", side_effect=_in_window(published, rid, stop_then_refuse)),
			patch.object(ts, "record_aborted_terminal", side_effect=takeover_first),
		):
			with self.assertRaises(ts.LeaseLostExit):
				pump._dispatch_ready(ctx)
		self.assertIn("row", owners, "premise: the old pump reached its never-sent cancel")
		self.assertEqual(_row(rid), owners["row"])
		self.assertEqual(owners["row"]["state"], "dispatching")
		self.assertNotIn("run:error", published.names(rid))
		self.assertEqual(peer.texts(), [])


class TestReturnTelemetryOutcome(_UnsentCase):
	"""The ``send_returned`` line says what happened: a database error while undoing
	the claim (the hop's crash path takes it) is ``error``; only a lost shard is
	``lease_lost``."""

	def _returned_outcomes(self, rid, action, *, expect, **patches):
		conv = self._mk_conv()
		self._seed_ready(conv, rid)
		sess, _peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))
		telemetry: list = []

		def refuse_after():
			action(ctx)
			self._refuse(ctx)

		with (
			patch.object(ts, "publish_fenced", side_effect=_in_window(_PublishLog(), rid, refuse_after)),
			patch.object(pump, "_telemetry", side_effect=lambda event, **f: telemetry.append((event, f))),
		):
			with self.assertRaises(expect):
				if patches:
					with patch.object(ts, "unclaim_unsent_dispatch", **patches):
						pump._dispatch_ready(ctx)
				else:
					pump._dispatch_ready(ctx)
		return [f["outcome"] for e, f in telemetry if e == "send_returned"]

	def test_database_error_is_not_reported_as_a_lost_lease(self):
		outcomes = self._returned_outcomes(
			"pmp_nsl_tel_db",
			lambda ctx: None,
			expect=frappe.QueryTimeoutError,
			side_effect=frappe.QueryTimeoutError("Lock wait timeout exceeded"),
		)
		self.assertEqual(outcomes, ["error"])

	def test_takeover_is_reported_as_a_lost_lease(self):
		outcomes = self._returned_outcomes(
			"pmp_nsl_tel_tk", lambda ctx: _takeover(self._target), expect=ts.LeaseLostExit
		)
		self.assertEqual(outcomes, ["lease_lost"])


class TestUnexplainedVersionChange(_UnsentCase):
	"""No known writer bumps a claimed row's version without a cancel or an epoch
	change, so this is defensive. With no sign the gateway has the run, the return
	is retried once on the fresh version (its marker then names that version, and
	Stop honours it); with a sign, the turn parks ``recovering`` and is never put
	back to ready."""

	def _drift(self, rid, **cols):
		def run(_w):
			sets = ", ".join(["version=version+1", *[f"{k}=%({k})s" for k in cols]])
			frappe.db.sql(f"UPDATE `tab{TURN}` SET {sets} WHERE name=%(r)s", {"r": rid, **cols})

		elsewhere(run)

	def test_without_gateway_evidence_the_return_is_retried_once(self):
		conv = self._mk_conv()
		rid = "pmp_nsl_drift1"
		self._seed_ready(conv, rid)
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))

		def drift_then_refuse():
			self._drift(rid)
			self._refuse(ctx)

		published = _PublishLog()
		with patch.object(ts, "publish_fenced", side_effect=_in_window(published, rid, drift_then_refuse)):
			pump._dispatch_ready(ctx)
		row = _row(rid)
		self.assertEqual(row["state"], "ready")
		self.assertNotIn("run:error", published.names(rid))
		self.assertEqual(
			json.loads(row["dispatch_payload"])["unsent_return"],
			{"version": int(row["version"]), "epoch": ctx.epoch},
		)
		self.assertNotIn(rid, ctx.runs)
		self.assertEqual(ctx.mux._runs, {})
		with patch.object(ts, "publish_fenced"):
			self.assertTrue(pump.request_cancel_conversation(conv, rid))
		self.assertEqual(self._state(rid), "cancelled")

	def test_with_gateway_evidence_the_turn_parks_and_is_never_ready(self):
		conv = self._mk_conv()
		rid = "pmp_nsl_drift2"
		self._seed_ready(conv, rid)
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))

		def drift_then_refuse():
			self._drift(rid, gateway_run_id=rid)
			self._refuse(ctx)

		published = _PublishLog()
		with patch.object(ts, "publish_fenced", side_effect=_in_window(published, rid, drift_then_refuse)):
			pump._dispatch_ready(ctx)
		self.assertEqual(self._state(rid), "recovering")
		self.assertIn("run:recovering", published.names(rid))
		self.assertNotIn("run:error", published.names(rid))
		self.assertNotIn(rid, ctx.runs)
		self.assertEqual(ctx.mux._runs, {})


class TestRemoteCodeIsNotProof(_UnsentCase):
	"""Negative control: a gateway that answers ``chat.send`` with ok:false and the
	code ``send-not-started`` has received the frame. Only the local exception TYPE
	is proof, so this keeps today's definite path: ``errored``, never ``ready``."""

	def test_remote_send_not_started_code_keeps_the_definite_error(self):
		conv = self._mk_conv()
		rid = "pmp_nsl_remote"
		self._seed_ready(conv, rid)

		def reject(peer, raw):
			frame = json.loads(raw)
			if frame.get("method") == "chat.send":
				peer.send_json(
					{
						"type": "res",
						"id": frame["id"],
						"ok": False,
						"error": {"code": "send-not-started", "message": "rejected"},
					}
				)

		sess, peer = self._socket_mux(on_message=reject)
		ctx = self._make_ctx(self._socket_deps(sess))
		published = _PublishLog()
		with patch.object(ts, "publish_fenced", side_effect=published):
			self.assertEqual(pump._dispatch_ready(ctx), 1)
			self.assertTrue(_wait_for(lambda: ctx.pending_acks[rid].fut.done, 5))
			pump._poll_pending(ctx)
		self.assertEqual(self._state(rid), "errored")
		self.assertNotIn("unsent_return", json.loads(self._val(rid, "dispatch_payload")))
		self.assertIn("run:error", published.names(rid))


# --------------------------------------------------------------------------- #
# A refused chat.abort keeps the Stop
# --------------------------------------------------------------------------- #


class TestRefusedAbortKeepsTheStop(_UnsentCase):
	"""The cancel sweep asks the gateway to stop a running reply. If that
	``chat.abort`` was refused (never sent) or may not have arrived, the gateway may
	still be running the reply and its tools: nothing is recorded locally and the
	Stop stays pending until a hop delivers it. Before: recorded ``cancelled`` while
	the gateway kept running."""

	def _streaming(self, ctx, rid):
		conv = self._mk_conv()
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
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		self.assertTrue(ts.request_cancel(rid, 4))
		frappe.db.commit()
		return _row(rid)

	def _assert_pending(self, rid, before):
		row = _row(rid)
		self.assertEqual(row["state"], "streaming", "nothing recorded locally")
		self.assertEqual(int(row["cancel_requested"]), 1, "the Stop is kept")
		self.assertEqual(int(row["version"]), int(before["version"]))

	def test_busy_socket_refusal_retries_on_the_next_slice(self):
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))
		rid = "pmp_nsl_ab1"
		before = self._streaming(ctx, rid)
		held = threading.Event()

		def hold_lock():
			with sess._lock:
				held.set()
				time.sleep(6.0)

		holder = threading.Thread(target=hold_lock, daemon=True)
		holder.start()
		self.assertTrue(held.wait(2))
		with patch.object(ts, "publish_fenced"):
			self.assertEqual(pump._cancel_sweep(ctx), 0)
		self._assert_pending(rid, before)
		self.assertTrue(ctx.io_deferred)
		self.assertEqual([t.get("method") for t in peer.texts()], [])
		holder.join(10)
		with patch.object(ts, "publish_fenced"):
			self.assertEqual(pump._cancel_sweep(ctx), 1)
		self.assertTrue(_wait_for(lambda: peer.texts(), 5))
		self.assertEqual([t.get("method") for t in peer.texts()], ["chat.abort"])
		self.assertEqual(self._state(rid), "cancelled")

	def test_uncertain_abort_is_sent_again_by_the_successor(self):
		sess, peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))
		rid = "pmp_nsl_ab2"
		before = self._streaming(ctx, rid)
		with (
			patch.object(
				ctx.mux, "abort", side_effect=SendUncertain("chat.abort may not have been delivered")
			),
			patch.object(ts, "publish_fenced"),
		):
			pump._cancel_sweep(ctx)
		self._assert_pending(rid, before)
		pump._schedule_successor_on_exit(ctx, transport_retry=0)
		sess2, peer2 = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx2 = self._make_ctx(self._socket_deps(sess2), holder="hop-test-2", hop_counter=1)
		with patch.object(ts, "publish_fenced"):
			self.assertEqual(pump._cancel_sweep(ctx2), 1)
		self.assertTrue(_wait_for(lambda: peer2.texts(), 5))
		self.assertEqual([t.get("method") for t in peer2.texts()], ["chat.abort"])
		self.assertEqual(tp.chat_sends(peer) + tp.chat_sends(peer2), [])
		self.assertEqual(self._state(rid), "cancelled")

	def test_unexpected_error_reaches_the_hop_crash_path(self):
		sess, _peer = self._socket_mux(on_message=tp.rpc_responder(chat_final=False))
		ctx = self._make_ctx(self._socket_deps(sess))
		rid = "pmp_nsl_ab3"
		before = self._streaming(ctx, rid)
		with (
			patch.object(ctx.mux, "abort", side_effect=RuntimeError("bug")),
			patch.object(ts, "publish_fenced"),
		):
			with self.assertRaises(RuntimeError):
				pump._cancel_sweep(ctx)
		self._assert_pending(rid, before)


# --------------------------------------------------------------------------- #
# Shared chats
# --------------------------------------------------------------------------- #


class _Future:
	done = False

	def __init__(self, response):
		self.response = response

	def result(self, timeout=0):
		if not self.done:
			raise AssertionError("A reactor must not block on an unready policy RPC")
		return self.response


class TestSharedSendNotStarted(CommittedCollaborationCase):
	"""Shared chats: the actor-bound preflight (``sessions.patch``) and the claim.
	The mux is a double here; its refusals are the REAL ``SendNotStarted`` type the
	transport raises (proven on real sockets above), so what is checked is the
	shared claim, its compensation, the grant checks and the shared Stop."""

	def setUp(self):
		super().setUp()
		from jarvis.chat.collaboration_bindings import insert_message
		from jarvis.chat.collaboration_runtime import RuntimePolicy, provision
		from jarvis.tests._share_helpers import RECIPIENT
		from jarvis.tests.test_chat_collaboration_runtime import Gateway

		self._job = as_job()
		self._job.__enter__()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		self.reply = insert_message(self.bound, {"role": "assistant", "content": "", "streaming": 1})
		# Real preparation creates the bound placeholder before ready; nothing was sent yet.
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{"assistant_message": self.reply.name, "collaboration_send_started_at": None},
		)
		self.gateway = Gateway()
		self.policy = RuntimePolicy("/workspace/skills", "provider/actor-model")
		self.physical = provision(self.gateway, self.bound, self.policy)
		self.target = f"{TARGET_PREFIX}{uuid.uuid4().hex[:10]}"
		ts._ensure_control_row(self.target)
		payload = {
			"session_key": self.bound.session_key,
			"message": "actor request",
			"chat_user": RECIPIENT,
			"collaboration_runtime": {
				"skills_root": self.policy.skills_root,
				"model_ref": self.policy.model_ref,
				"physical_id": self.physical,
				"context_tokens": 8192,
				"runtime_reserve": 1024,
			},
		}
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{
				"state": "ready",
				"relay_target_id": self.target,
				"reserved": 1,
				"assistant_message": self.reply.name,
				"collaboration_send_started_at": None,
				"dispatch_payload": json.dumps(payload),
			},
		)
		frappe.db.commit()
		won, epoch = ts.lease_acquire(self.target, "shared-test-holder")
		self.assertTrue(won)
		self.context = pump.PumpContext(
			self.target, epoch, "shared-test-holder", 0, frappe.local.site, pump.PumpDeps()
		)
		self.mux = self.context.mux = Mock()
		self.mux.issue_rpc.side_effect = self._issue
		self.mux.send_chat.return_value = _Future({"payload": {"runId": self.turn.name}})
		self.published = _PublishLog()
		self.publishing = patch.object(ts, "publish_fenced", side_effect=self._publish)
		self.publishing.start()
		self.wakes: list = []
		self.waking = patch(
			"jarvis.chat.collaboration_ownership.wake_queued", side_effect=lambda t: self.wakes.append(t)
		)
		self.waking.start()

	def tearDown(self):
		self.waking.stop()
		self.publishing.stop()
		self._job.__exit__(None, None, None)
		frappe.db.delete(ts.PUMP, {"name": self.target})
		frappe.db.commit()
		super().tearDown()

	def _publish(self, user, event, **fields):
		self.published(user, event, **fields)
		self.published.events[-1][1]["user"] = user

	def _issue(self, method, params, **kwargs):
		self.preflight = _Future(self.gateway._request(method, params, **kwargs))
		return self.preflight

	def _preflight_done(self):
		self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		self.mux.send_chat.assert_not_called()
		self.preflight.done = True

	def _shared_row(self):
		return _row(self.turn.name)

	def _attempt(self):
		"""One dispatch attempt as the slices would make it: a fresh preflight that went
		out is answered, then the dispatch runs again."""
		issued = self.mux.issue_rpc.call_count
		result = pump._dispatch_one(self.context, self.turn.name)
		if self.mux.issue_rpc.call_count > issued:
			self.preflight.done = True
			result = pump._dispatch_one(self.context, self.turn.name)
		return result

	def test_post_claim_refusal_returns_the_turn_and_the_retry_rechecks_access(self):
		from jarvis.chat import collaboration as c

		self._preflight_done()
		before = self._shared_row()
		binding_state = frappe.db.get_value(c.BINDING, self.bound.name, "state")
		self.mux.send_chat.side_effect = SendNotStarted(
			"chat.send was not sent: no time left in this delivery window"
		)
		self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		after = self._shared_row()
		self.assertEqual(after["state"], "ready", "a never-sent shared request is not a failure")
		self.assertEqual(int(after["version"]), int(before["version"]) + 2)
		self.assertIsNone(after["collaboration_send_started_at"], "nothing was sent")
		for f in ("reserved", "ready_at", "pump_epoch", "dispatching_at", "collaboration_retry_allowed"):
			self.assertEqual(after[f], before[f], f)
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), binding_state)
		self.assertNotIn("run:error", self.published.names(self.turn.name))
		self.assertNotIn(self.turn.name, self.context.runs)
		self.assertTrue(self.context.io_deferred)

		self.mux.send_chat.side_effect = None
		patches = self.mux.issue_rpc.call_count
		self.assertTrue(self._attempt())
		self.assertEqual(self.mux.issue_rpc.call_count, patches + 1, "the retry checks access again")
		self.assertEqual(
			self.mux.issue_rpc.call_args.args[1],
			{"key": self.bound.session_key, "expectedSessionId": self.physical},
		)
		self.assertEqual(self.mux.send_chat.call_count, 2)
		self.assertEqual(self.mux.send_chat.call_args.kwargs["verified_session_id"], self.physical)
		self.assertEqual(self.mux.send_chat.call_args.args[2], self.turn.name, "the same key")

	def test_refused_preflight_leaves_the_turn_ready(self):
		before = self._shared_row()
		self.mux.issue_rpc.side_effect = SendNotStarted(
			"sessions.patch was not sent: the relay connection is closed"
		)
		self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		self.assertEqual(self._shared_row(), before, "nothing was sent: nothing changes")
		self.assertEqual(self.published.events, [])
		self.assertTrue(self.context.io_deferred)
		self.mux.issue_rpc.side_effect = self._issue
		self._preflight_done()
		self.assertTrue(pump._dispatch_one(self.context, self.turn.name))
		self.mux.send_chat.assert_called_once()

	def test_remote_send_not_started_code_on_the_preflight_is_not_proof(self):
		self._preflight_done()
		self.preflight.result = Mock(
			side_effect=AgentUnreachableError("sessions.patch rejected", code="send-not-started")
		)
		self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		self.assertEqual(self._shared_row()["state"], "cancelled", "today's refusal path")
		self.mux.send_chat.assert_not_called()

	def test_grant_revoked_while_returned_is_refused_on_the_retry(self):
		from jarvis.chat import collaboration as c

		self._preflight_done()
		self.mux.send_chat.side_effect = SendNotStarted(
			"chat.send was not sent: the relay connection is closed"
		)
		self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		self.assertEqual(self._shared_row()["state"], "ready")
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		self.mux.send_chat.side_effect = None
		self.context.io_deferred = False
		self.assertFalse(self._attempt())
		self.assertEqual(self._shared_row()["state"], "cancelled")
		self.assertEqual(self.mux.send_chat.call_count, 1, "only the refused attempt; nothing sent")

	def _stop_in_window(self, user):
		from jarvis.chat import collaboration_control

		stopped: dict = {}
		rollout = dict(self.rollout.values)

		def stop_then_refuse(*_a, **_k):
			def run(_w):
				# The same shared-chat rollout the fixture switches on for this site.
				frappe.local.conf.update(rollout)
				with patch(
					"jarvis.chat.agent_session_pool.checkout", side_effect=AgentUnreachableError("down")
				):
					with patch.object(frappe, "log_error"):
						stopped["answer"] = collaboration_control.stop(self.conversation, self.turn.name)
				frappe.db.commit()
				stopped["row"] = _row(self.turn.name)

			elsewhere(run, user=user)
			raise SendNotStarted("chat.send was not sent: no time left in this delivery window")

		self._preflight_done()
		self.mux.send_chat.side_effect = stop_then_refuse
		self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		return stopped

	def _assert_stopped_unsent(self, stopped):
		from jarvis.tests._share_helpers import RECIPIENT

		self.assertEqual(stopped["answer"]["state"], "stopping", "premise: Stop took the in-flight branch")
		row = self._shared_row()
		self.assertEqual(
			(row["state"], int(row["cancel_requested"]), int(row["reserved"])), ("cancelled", 1, 0)
		)
		self.assertIsNone(row["collaboration_send_started_at"], "nothing was sent")
		self.assertIsNone(row["reservation_expires_at"])
		self.assertEqual(row["collaboration_retry_allowed"], stopped["row"]["collaboration_retry_allowed"])
		msg = frappe.db.get_value(MSG, self.reply.name, ["streaming", "stopped"], as_dict=True)
		self.assertEqual((int(msg.streaming), int(msg.stopped)), (0, 1))
		events = self.published.for_run(self.turn.name)
		self.assertNotIn("run:error", [e for e, _f in events])
		ends = [f for e, f in events if e == "run:end"]
		self.assertEqual(len(ends), 1)
		self.assertEqual(
			(ends[0]["user"], ends[0]["message_id"], ends[0]["pump_epoch"], ends[0]["stopped"]),
			(RECIPIENT, self.reply.name, self.context.epoch, True),
		)
		self.assertEqual(frappe.get_all(EFFECT, {"turn": self.turn.name}), [], "no usage effects")
		self.mux.abort.assert_not_called()
		self.assertEqual(self.wakes, [self.target])

	def test_actor_stop_in_the_claim_window(self):
		from jarvis.tests._share_helpers import RECIPIENT

		self._assert_stopped_unsent(self._stop_in_window(RECIPIENT))

	def test_owner_stop_in_the_claim_window(self):
		from jarvis.tests._share_helpers import OWNER

		self._assert_stopped_unsent(self._stop_in_window(OWNER))
