"""WP-1d — end-to-end pump-pipeline tests (accept -> prepare -> dispatch ->
settle -> finalize) + the PANEL matrix rows WP-1d owns.

These drive the REAL prepare / settlement / finalize jobs in-process against the
real ``turn_state`` CAS library + WP-1b's transport double (only the gateway
socket + the openclaw pool checkout are faked). The two enrichment side effects
that do real HTTP/gateway work (rich outputs, usage poll) are mocked at their
boundary so the LEDGER machinery (claim/complete/force-done/finalize_done) runs
for real.

Coverage:
  * full pipeline: accept(pump) -> promote+prepare(REAL) -> ready -> pump dispatch
    -> frames -> terminal -> REAL settlement -> REAL finalize -> done, asserting
    every D1 spot-check owner ran its effect exactly once;
  * PANEL 4 (retry/orphan through the chokepoint: no duplicate user row, seq
    monotonic under the conv lock);
  * PANEL 5 (kill during preparing -> recovering -> queued -> fresh prepare;
    session at-most-once, OAR-4);
  * PANEL 8 (unfinishable enrichment -> force-done at 3 -> turn reaches done ->
    watchdog stops);
  * PANEL 10 (draining coexistence: new turns go legacy, dual-signal sees pump
    in-flight, promote/sweep step back);
  * two-writer settlement (pump terminal + racing recovery, both orders, side
    effects exactly once);
  * SUX-11 error-payload contract preserved through Turn.error/terminal_payload;
  * usage (turn_id) idempotency under finalize replay.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from jarvis.chat import admission, finalize, prepare, pump, settlement
from jarvis.chat import turn_state as ts
from jarvis.tests.test_pump import TEST_USER, _PumpTestCase, _Recorder

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
EFFECT = "Jarvis Turn Effect"
SESSION = "Jarvis Chat Session"


class _FakeSess:
	"""In-process stand-in for a pooled OpenclawSession — the bootstrap RPCs
	(prepare) + the usage poll (finalize) with NO real socket."""

	def __init__(self):
		self._key = None
		self.model_patches: list = []
		self.created = 0

	def create_session(self, label: str = "x") -> str:
		self.created += 1
		self._key = "sess-" + frappe.generate_hash(length=8)
		return self._key

	def set_session_model(self, key, ref):
		self.model_patches.append((key, ref))

	def get_session_messages(self, key, limit=5):
		return []

	def list_sessions(self):
		return [
			{
				"key": self._key,
				"totalTokensFresh": True,
				"inputTokens": 120,
				"outputTokens": 30,
				"totalTokens": 900,
				"model": "test-model",
			}
		]

	def close(self):
		pass


class _PipelineCase(_PumpTestCase):
	def setUp(self):
		super().setUp()
		self._pubs: list = []
		# Capture every fenced publish (turn_state routes them through
		# events.publish_to_user, imported into turn_state at module load).
		self._pub_patch = patch.object(
			ts, "publish_to_user", lambda user, payload: self._pubs.append(payload)
		)
		self._pub_patch.start()

	def tearDown(self):
		self._pub_patch.stop()
		# Clean the Jarvis Chat Session rows prepare created for this test's convs
		# (owned by TEST_USER — the seed-message sender / chat_user).
		try:
			frappe.db.delete(SESSION, {"user": TEST_USER})
		except Exception:
			pass
		super().tearDown()

	def _acquire_fresh(self, holder="pipe"):
		"""Force the shard lease vacant, then acquire — so repeated acquires within
		one test (subtests) never fail on a still-live 30s lease."""
		frappe.db.set_value(
			"Jarvis Relay Pump",
			self._target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.commit()
		won, epoch = ts.lease_acquire(self._target, holder)
		self.assertTrue(won, "fresh lease acquire failed")
		return epoch

	# ---- pump-on context + gateway mock ---------------------------------- #

	@contextmanager
	def _pump_on(self, *, ensure=None):
		"""Force pump-mode active + configured and stub the wake so accept/prepare
		never enqueue a real hop."""
		with (
			patch.object(pump, "pump_mode_active", return_value=True),
			patch.object(pump, "pump_configured", return_value=True),
			patch.object(pump, "ensure_pump", ensure or (lambda *a, **k: {"enqueued": False})),
			patch.object(pump, "lpush_wake", lambda *a, **k: None),
		):
			yield

	@contextmanager
	def _gateway(self, fake: _FakeSess):
		@contextmanager
		def _co(url):
			yield fake

		with patch("jarvis.chat.openclaw_session_pool.checkout", _co):
			yield

	@contextmanager
	def _mock_enrichment(self):
		"""Mock the enrichment effect BOUNDARIES (real HTTP/gateway/macro) with
		recorders; the finalize ledger loop itself stays real. The USAGE effect is mocked
		at the whole-effect boundary (``finalize._effect_usage``) rather than only its
		inner ``record_turn_usage`` — since CDX-6 a no-session-key/unmapped turn RETRIES
		inside ``_effect_usage`` (never a silent no-op), so a lifecycle test that just
		needs a turn to reach ``done`` mocks the effect to a clean success that mirrors the
		real R-4 ``usage_recorded`` guard (the real usage retry/attribution behaviour is
		covered end-to-end by TestUsageHonesty / TestUsageIdempotency)."""
		recs = {
			"rich": _Recorder(),
			"macro": _Recorder(),
			"applearn": _Recorder(),
			"title": _Recorder(),
			"usage": _Recorder(),
		}

		def _usage_effect_noop(ctx):
			recs["usage"](ctx)
			# Mirror the real effect's at-most-once R-4 guard so lifecycle tests both reach
			# `done` AND satisfy the guard-set assertion, without a live gateway poll.
			ts._run_cas(
				"UPDATE `tabJarvis Chat Turn` SET usage_recorded=1 WHERE name=%(r)s AND usage_recorded=0",
				{"r": ctx.run_id},
			)

		with (
			patch("jarvis.chat.turn_handler.persist_rich_outputs", recs["rich"]),
			patch("jarvis.chat.macros.advance_after_turn", recs["macro"]),
			patch("jarvis.learning.app_analysis.on_turn_end", recs["applearn"]),
			patch("jarvis.chat.title.enqueue_autotitle", recs["title"]),
			# The runner dispatches through _RUNNERS (captured at import), so patch the dict
			# entry — patching the module attribute alone would not reach the runner.
			patch.dict("jarvis.chat.finalize._RUNNERS", {"usage": _usage_effect_noop}),
			patch("jarvis.chat.wiki.wiki_enabled", return_value=False),
		):
			yield recs

	def _effects(self, run_id):
		return {
			r["effect_name"]: r["status"]
			for r in frappe.get_all(EFFECT, filters={"turn": run_id}, fields=["effect_name", "status"])
		}

	def _pub_kinds(self):
		return [p.get("kind") for p in self._pubs]


# --------------------------------------------------------------------------- #
# 1. Full pipeline end-to-end
# --------------------------------------------------------------------------- #


class TestPumpPipelineE2E(_PipelineCase):
	def test_full_pipeline_accept_prepare_dispatch_settle_finalize_done(self):
		conv = self._mk_conv()  # no session_key
		seed = self._mk_msg(conv, content="hello")
		rid = "pmp_pipe"
		fake = _FakeSess()
		double = self._double()

		# 1. ACCEPT (pump mode): the send routes to the pump; the turn is queued,
		#    no legacy dispatch. (No user row inserted — seed already exists.)
		ensure_rec = _Recorder()
		with (
			self._pump_on(ensure=ensure_rec),
			patch.object(admission, "relay_target_id", lambda conversation=None: self._target),
		):
			adm = admission.accept_or_queue(
				conversation=conv, run_id=rid, seed_message=seed, dispatch=lambda: None
			)
		self.assertTrue(adm.get("pump"))
		self.assertEqual(self._state(rid), "queued")
		self.assertGreaterEqual(ensure_rec.count, 1, "accept woke the pump (§8-E PRIMARY)")

		# 2. PROMOTE (reserve credit under the shard lock) + PREPARE (REAL job).
		#    dispatch_prepare runs prepare.run_prepare in-process; the gateway pool
		#    checkout is faked so session bootstrap/model-patch/watermark are real DB
		#    writes without a socket.
		def real_prepare(run_id, target):
			with self._gateway(fake), self._pump_on():
				prepare.run_prepare(run_id, target)

		deps = self._deps(double=double, prepare=real_prepare)
		ctx = self._make_ctx(deps)
		pump._promote_queued(ctx)

		# prepare drove queued->preparing->ready; R-1: it inserted the assistant
		# placeholder and linked it; #22: it created the session (at-most-once).
		self.assertEqual(self._state(rid), "ready")
		amsg = self._val(rid, "assistant_message")
		self.assertIsNotNone(amsg, "R-1: prepare inserted+linked the assistant placeholder")
		self.assertEqual(int(frappe.db.get_value(MSG, amsg, "streaming")), 1)
		self.assertEqual(fake.created, 1, "session created exactly once")
		self.assertTrue(frappe.db.get_value(CONV, conv, "session_key"))
		dp = json.loads(self._val(rid, "dispatch_payload"))
		self.assertTrue(dp["session_key"] and dp["message"], "prepare->pump handoff payload written")

		# 3. DISPATCH + STREAM + TERMINAL + REAL SETTLEMENT.
		double.arm(rid, "success")
		self._pump_until(ctx, lambda: self._state(rid) in ("finalizing", "done"))
		self.assertEqual(self._state(rid), "finalizing", "REAL settlement released the slot")
		self.assertEqual(int(self._val(rid, "reserved")), 0)
		self.assertIsNotNone(self._val(rid, "gateway_run_id"))
		# The pump published the fenced run:start (R-1) and the settlement run:end.
		self.assertIn("run:start", self._pub_kinds())
		self.assertIn("run:end", self._pub_kinds())
		# Settlement fixed the owed-enrichment set (OAR-9).
		eff = self._effects(rid)
		self.assertEqual(set(eff), set(settlement.FINAL_EFFECTS))
		self.assertTrue(all(v == "pending" for v in eff.values()))

		# 4. REAL finalize off the ledger -> done + message:enriched (SUX-7).
		with self._gateway(fake), self._mock_enrichment() as recs:
			out = finalize.run_finalize(rid, self._target)
		self.assertTrue(out["done"])
		self.assertEqual(self._state(rid), "done")
		self.assertIn("message:enriched", self._pub_kinds())
		# Every owed effect reached done exactly once (D1 spot-check).
		self.assertTrue(all(v == "done" for v in self._effects(rid).values()))
		self.assertEqual(recs["rich"].count, 1, "rich_outputs owner ran once")
		self.assertEqual(recs["macro"].count, 1, "macro_advance owner ran once")
		self.assertEqual(recs["title"].count, 1, "auto_title owner ran once")
		self.assertEqual(recs["usage"].count, 1, "usage recorded once")
		self.assertEqual(int(self._val(rid, "usage_recorded")), 1, "R-4 (turn_id) guard set")

		# 5. Re-running finalize is a total no-op (idempotent).
		with self._gateway(fake), self._mock_enrichment() as recs2:
			out2 = finalize.run_finalize(rid, self._target)
		self.assertTrue(out2.get("already_done"))
		self.assertEqual(recs2["usage"].count, 0, "no effect re-runs after done")


# --------------------------------------------------------------------------- #
# 1b. F2 — the pump-mode accept `dispatched` hint reflects the real promote order
# --------------------------------------------------------------------------- #


class TestF2WillDispatchHint(_PipelineCase):
	"""F2: the SENDER tab must render the queued chip immediately from the accept
	response. That needs the pump-mode ``dispatched`` hint to reflect the pump's real
	promote order — the OLD hint compared only ``inflight < cap`` and so wrongly
	claimed immediate dispatch when a still-``queued`` occupier (uncounted by
	``_shard_inflight``) sat AHEAD of the new turn, leaving the sender on "Working on
	it…" for the whole wait while a reload showed the chip. The fix uses the turn's
	RANK among the shard's queued turns: dispatch iff ``inflight + position <= cap``."""

	def _accept(self, conv, rid, seed, cap):
		with (
			self._pump_on(),
			patch.object(admission, "relay_target_id", lambda conversation=None: self._target),
			patch.object(admission, "_max_inflight", lambda: cap),
		):
			return admission.accept_or_queue(
				conversation=conv, run_id=rid, seed_message=seed, dispatch=lambda: None
			)

	def test_idle_shard_dispatches(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="hi")
		adm = self._accept(conv, "f2_idle", seed, cap=1)
		self.assertTrue(adm["dispatched"], "an idle shard dispatches immediately")
		self.assertIsNone(adm["queued_position"])

	def test_queued_occupier_ahead_marks_new_turn_queued(self):
		# The exact F2 race: occupier A is plain 'queued' (pump has NOT promoted it to
		# an inflight state yet) so _shard_inflight()==0. cap=1. Accepting B in another
		# conversation must still report B QUEUED (position 2), not dispatched.
		conv_a = self._mk_conv()
		seed_a = self._mk_msg(conv_a, content="A")
		self._mk_turn(conv_a, "f2_occ_a", seed_a, "queued", version=0, reserved=0)
		conv_b = self._mk_conv()
		seed_b = self._mk_msg(conv_b, content="B")
		adm = self._accept(conv_b, "f2_occ_b", seed_b, cap=1)
		self.assertFalse(adm["dispatched"], "F2: a queued occupier ahead => the new turn is queued")
		self.assertEqual(adm["queued_position"], 2)
		self.assertEqual(self._state("f2_occ_b"), "queued")

	def test_streaming_occupier_marks_new_turn_queued(self):
		conv_a = self._mk_conv()
		seed_a = self._mk_msg(conv_a, content="A")
		amsg = self._mk_msg(conv_a, role="assistant", content="", streaming=1)
		self._mk_turn(conv_a, "f2_str_a", seed_a, "streaming", version=3, reserved=1, assistant_message=amsg)
		conv_b = self._mk_conv()
		seed_b = self._mk_msg(conv_b, content="B")
		adm = self._accept(conv_b, "f2_str_b", seed_b, cap=1)
		self.assertFalse(adm["dispatched"])
		self.assertEqual(adm["queued_position"], 1, "A is inflight; only B is queued")

	def test_second_credit_free_dispatches(self):
		# cap=2 with one queued occupier ahead: inflight(0)+position(2) <= 2 => dispatch.
		conv_a = self._mk_conv()
		seed_a = self._mk_msg(conv_a, content="A")
		self._mk_turn(conv_a, "f2_cap2_a", seed_a, "queued", version=0, reserved=0)
		conv_b = self._mk_conv()
		seed_b = self._mk_msg(conv_b, content="B")
		adm = self._accept(conv_b, "f2_cap2_b", seed_b, cap=2)
		self.assertTrue(adm["dispatched"], "cap=2 with one queued ahead still has a free credit for B")


# --------------------------------------------------------------------------- #
# 2. PANEL 4 — retry/orphan through the chokepoint (no dup user row, monotonic seq)
# --------------------------------------------------------------------------- #


class TestPanel4Chokepoint(_PipelineCase):
	def test_retry_and_orphan_reuse_seed_no_dup_user_row(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="first")
		before = frappe.db.count(MSG, {"conversation": conv, "role": "user"})
		with self._pump_on():
			# retry reuses the EXISTING user message (OAR-3) — no new user row.
			admission.accept_or_queue(
				conversation=conv, run_id="pmp_p4_retry", seed_message=seed, dispatch=lambda: None
			)
			# orphan re-dispatch (background) reuses it too.
			admission.accept_or_queue(
				conversation=conv,
				run_id="pmp_p4_orphan",
				seed_message=seed,
				turn_class="background",
				dispatch=lambda: None,
			)
		after = frappe.db.count(MSG, {"conversation": conv, "role": "user"})
		self.assertEqual(after, before, "retry/orphan inserted NO duplicate user row")

	def test_placeholder_seq_monotonic_under_conv_lock(self):
		conv = self._mk_conv()
		# A tool receipt sits at some seq; prepare's placeholder must not collide.
		self._mk_msg(conv, role="user", content="u1")
		self._mk_msg(conv, role="tool", content="t1")
		p1 = prepare._create_placeholder_locked(conv)
		self._mk_msg(conv, role="tool", content="t2")
		p2 = prepare._create_placeholder_locked(conv)
		seqs = [frappe.db.get_value(MSG, m, "seq") for m in (p1, p2)]
		self.assertTrue(seqs[0] < seqs[1], f"placeholder seq monotonic: {seqs}")
		# Unique + gap-free against everything on the conversation.
		all_seqs = frappe.get_all(MSG, filters={"conversation": conv}, pluck="seq")
		self.assertEqual(len(all_seqs), len(set(all_seqs)), "no seq collision")


# --------------------------------------------------------------------------- #
# 3. PANEL 5 — kill during preparing -> recovering -> queued -> fresh prepare
# --------------------------------------------------------------------------- #


class TestPanel5PrepareKill(_PipelineCase):
	def test_kill_during_preparing_recovers_and_session_at_most_once(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="hi")
		rid = "pmp_p5"
		self._mk_turn(conv, rid, seed, "queued", version=1, reserved=1)
		fake = _FakeSess()

		# First prepare runs (creates the session, sets conv.session_key), then the
		# pump "dies" — we simulate by parking the preparing turn to recovering and
		# recovering it back to queued for a FRESH prepare (OAR-4).
		with self._gateway(fake), self._pump_on():
			prepare.run_prepare(rid, self._target)
		self.assertEqual(self._state(rid), "ready")
		self.assertEqual(fake.created, 1)
		first_key = frappe.db.get_value(CONV, conv, "session_key")
		self.assertTrue(first_key)
		self.assertEqual(frappe.db.count(SESSION, {"session_key": first_key}), 1)

		# Simulate the kill: watchdog parks -> recovering, then recovers PRE-dispatch
		# (dispatching_at IS NULL) back to queued, dropping stale prepare refs.
		v = int(self._val(rid, "version"))
		self.assertTrue(ts.mark_recovering(rid, v))
		frappe.db.commit()
		self.assertTrue(ts.recover_to_queued(rid, v + 1))
		frappe.db.commit()
		self.assertEqual(self._state(rid), "queued")
		self.assertIsNone(self._val(rid, "assistant_message"), "stale prepare refs dropped")
		# recover_to_queued released the credit; re-grant it (the pump's promote does).
		self.assertTrue(ts.reserve_credit(rid))
		frappe.db.commit()

		# FRESH prepare — session at-most-once: it must REUSE the existing session,
		# never create a second (OAR-4 "session at-most-once absorbs the leak").
		with self._gateway(fake), self._pump_on():
			prepare.run_prepare(rid, self._target)
		self.assertEqual(self._state(rid), "ready")
		self.assertEqual(fake.created, 1, "no second session created on re-prepare")
		self.assertEqual(frappe.db.get_value(CONV, conv, "session_key"), first_key)
		self.assertEqual(frappe.db.count(SESSION, {"user": TEST_USER}), 1)


# --------------------------------------------------------------------------- #
# 4. PANEL 8 — unfinishable enrichment -> force-done at 3 -> turn reaches done
# --------------------------------------------------------------------------- #


class TestPanel8ForceDone(_PipelineCase):
	def test_unfinishable_effect_force_done_turn_reaches_done(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="ans", streaming=0)
		rid = "pmp_p8"
		# A settled (finalizing) turn owing exactly two effects: one that always
		# FAILS (macro_advance) + one that succeeds (telemetry_flush).
		self._mk_turn(conv, rid, seed, "finalizing", version=5, assistant_message=amsg)
		ts.insert_required_effects(rid, ("macro_advance", "telemetry_flush"))
		frappe.db.commit()

		calls = {"n": 0}

		def boom(*a, **k):
			calls["n"] += 1
			raise RuntimeError("enrichment permanently broken")

		# Drive finalize repeatedly (the watchdog's re-enqueue cadence). The failing
		# effect force-dones after FINALIZE_MAX_ATTEMPTS=3 so the turn ALWAYS reaches
		# done — a permanently-broken enrichment can never strand a settled turn.
		with patch("jarvis.chat.macros.advance_after_turn", boom):
			for _ in range(ts.FINALIZE_MAX_ATTEMPTS + 2):
				if self._state(rid) == "done":
					break
				finalize.run_finalize(rid, self._target)

		self.assertEqual(self._state(rid), "done", "turn reached done despite a broken effect")
		self.assertEqual(calls["n"], ts.FINALIZE_MAX_ATTEMPTS, "the effect was attempted exactly the budget")
		eff = self._effects(rid)
		self.assertEqual(eff["macro_advance"], "done", "force-done after the budget")
		self.assertEqual(eff["telemetry_flush"], "done")
		# Watchdog now STOPS re-enqueuing (state is terminal, not finalizing).
		fin_rec = _Recorder()
		wd_deps = pump.PumpDeps()
		wd_deps.enqueue_finalize = fin_rec
		wd_deps.enqueue_pump_job = _Recorder()
		# OARF-1: watchdog gated on pump_configured — run it as-if configured so the
		# assertion tests the R-13 "done turn -> no finalize re-enqueue" behaviour
		# (not the flag-off early return).
		with patch.object(pump, "pump_configured", return_value=True):
			pump.watchdog(deps=wd_deps)
		self.assertEqual(fin_rec.count, 0, "no finalize re-enqueue for a done turn")


# --------------------------------------------------------------------------- #
# 5. PANEL 10 — draining coexistence
# --------------------------------------------------------------------------- #


class TestPanel10Draining(_PipelineCase):
	def test_draining_new_turns_legacy_dual_signal_sees_pump(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
		rid = "pmp_p10"
		# An in-flight PUMP turn (streaming) that is draining.
		self._mk_turn(
			conv, rid, seed, "streaming", version=4, pump_epoch=1, reserved=1, assistant_message=amsg
		)
		frappe.db.commit()

		with (
			patch.object(pump, "pump_mode_active", return_value=False),
			patch.object(pump, "pump_draining", return_value=True),
			patch.object(pump, "pump_configured", return_value=True),
			patch.object(admission, "admission_enabled", return_value=True),
		):
			# 1. No NEW pump admissions: new turns fall through to the LEGACY path.
			self.assertFalse(
				admission.turn_machine_enabled(),
				"draining routes NEW turns to legacy (no Turn row)",
			)
			# 2. Dual-signal (OAR-11) COVERS pump-mode: the draining pump's streaming
			#    turn counts as shard inflight AND as a busy conversation, so a new
			#    legacy send can't double-admit onto it.
			self.assertGreaterEqual(
				admission._shard_inflight(self._target), 1, "pump streaming counts as inflight"
			)
			self.assertTrue(
				admission._conv_has_other_active_turn(conv, "other"),
				"pump streaming turn makes the conversation busy",
			)
			# 3. Phase-0 promote/sweep STEP BACK (they must never touch pump rows).
			with patch("jarvis.chat.admission._dispatch_promoted") as disp:
				self.assertEqual(admission.promote_next(self._target), 0)
				disp.assert_not_called()
			self.assertEqual(
				admission.sweep(), {"reclaimed": 0, "reconciled": 0, "aged_out": 0, "promoted": 0}
			)
			# The pump turn was NOT touched by Phase-0.
			self.assertEqual(self._state(rid), "streaming")


# --------------------------------------------------------------------------- #
# 5b. OARF-2 — snapshot recovery is bounded by the turn's watermark
# --------------------------------------------------------------------------- #


class TestSnapshotRecoveryWindow(_PipelineCase):
	"""OARF-2: missed-terminal snapshot recovery must window the durable tail by
	the turn's ``openclaw_seq_watermark`` (captured by prepare BEFORE this turn's
	send). A run that ended with NO output beyond the watermark must NEVER adopt a
	PRIOR turn's answer — it settles ``errored`` honestly (Amendment D: never
	fabricate)."""

	def _seed_streaming_gone(self, conv, rid, *, watermark, last_event_seq=3):
		seed = self._mk_msg(conv, content="second question")
		amsg = self._mk_msg(
			conv, role="assistant", content="partial", streaming=1, openclaw_seq_watermark=watermark
		)
		epoch = self._acquire_fresh("snaprec")
		self._mk_turn(
			conv,
			rid,
			seed,
			"streaming",
			version=6,
			pump_epoch=epoch,
			reserved=1,
			assistant_message=amsg,
			last_event_seq=last_event_seq,
			gateway_run_id=rid,
			dispatching_at=frappe.utils.now(),
			dispatch_payload=json.dumps({"session_key": "sess-rec"}),
		)
		return amsg, epoch

	def _ctx_for(self, double, epoch):
		# A ctx whose snapshot reports the session as GONE (not in active_session_keys)
		# so reconcile treats the terminal as missed and pulls the durable tail.
		deps = self._deps(
			double=double, snapshot=lambda ctx: {"gateway_active": 0, "active_session_keys": set()}
		)
		ctx = pump.PumpContext(
			relay_target_id=self._target,
			epoch=epoch,
			holder="snaprec",
			hop_counter=0,
			site=frappe.local.site,
			deps=deps,
		)
		now = pump._monotonic()
		ctx.soft_deadline = now + 999
		ctx.hard_deadline = now + 999
		ctx.last_heartbeat = now
		ctx.mux = deps.make_mux(self._target, epoch)
		return ctx

	def test_two_turn_recovery_does_not_surface_prior_answer(self):
		"""MANDATORY (OARF-2 regression): a two-turn conversation whose SECOND turn
		died pre-output. The session transcript still holds turn 1's 'PRIOR ANSWER'
		@seq5; the second turn's watermark is 5 (captured before its send). Recovery
		must NOT settle the second turn with turn 1's answer — it settles errored."""
		conv = self._mk_conv()
		rid = "pmp_rec_prior"
		amsg, epoch = self._seed_streaming_gone(conv, rid, watermark=5)
		double = self._double()
		# Durable transcript: only turn 1's answer @seq5 (<= watermark) + the user's
		# second question @seq6. NO assistant message for the current (second) turn.
		double.arm_sessions_get(
			"sess-rec",
			[
				{"role": "assistant", "content": "PRIOR ANSWER", "__openclaw": {"seq": 5}},
				{"role": "user", "content": "second question", "__openclaw": {"seq": 6}},
			],
		)
		ctx = self._ctx_for(double, epoch)
		self._pubs.clear()

		pump._reconcile_on_start(ctx)  # issues the recovery-tail RPC (non-blocking)
		self._pump_until(ctx, lambda: self._state(rid) in ("errored", "finalizing", "done"))

		# The second turn settled ERRORED — NEVER final with turn 1's answer.
		self.assertEqual(self._state(rid), "errored", "no in-window output -> honest errored")
		row = frappe.db.get_value(MSG, amsg, ["content", "error", "streaming"], as_dict=True)
		self.assertNotEqual(row["content"], "PRIOR ANSWER", "must NOT adopt the prior turn's answer")
		self.assertTrue(row["error"], "an honest user-visible error reason is set")
		self.assertEqual(int(row["streaming"]), 0, "spinner cleared (no stuck bubble)")
		self.assertEqual(int(self._val(rid, "reserved")), 0, "credit released")
		self.assertNotIn("PRIOR ANSWER", [p.get("text") for p in self._pubs])
		self.assertIn("run:error", self._pub_kinds())
		self.assertNotIn("run:end", self._pub_kinds())

	def test_in_window_answer_recovers_as_final_marked_recovered(self):
		"""Positive: when the durable tail DOES hold an answer WITHIN the window
		(seq 7 > watermark 5), recovery settles it as a final AND marks
		was_recovered (OARF-7: a genuine recovery, unlike a clean-hop re-stamp)."""
		conv = self._mk_conv()
		rid = "pmp_rec_final"
		amsg, epoch = self._seed_streaming_gone(conv, rid, watermark=5)
		double = self._double()
		double.arm_sessions_get(
			"sess-rec",
			[
				{"role": "assistant", "content": "PRIOR ANSWER", "__openclaw": {"seq": 5}},
				{"role": "user", "content": "second question", "__openclaw": {"seq": 6}},
				{"role": "assistant", "content": "RECOVERED ANSWER", "__openclaw": {"seq": 7}},
			],
		)
		ctx = self._ctx_for(double, epoch)
		self._pubs.clear()

		pump._reconcile_on_start(ctx)
		self._pump_until(ctx, lambda: self._state(rid) in ("errored", "finalizing", "done"))

		self.assertEqual(self._state(rid), "finalizing", "in-window answer settles final")
		self.assertEqual(frappe.db.get_value(MSG, amsg, "content"), "RECOVERED ANSWER")
		self.assertEqual(int(self._val(rid, "was_recovered")), 1, "genuine recovery flags was_recovered")
		self.assertIn("run:end", self._pub_kinds())
		end = next(p for p in self._pubs if p.get("kind") == "run:end")
		self.assertTrue(end.get("was_recovered"), "run:end carries was_recovered for a real recovery")


# --------------------------------------------------------------------------- #
# 6. Two-writer settlement — exactly-once under a racing recovery (both orders)
# --------------------------------------------------------------------------- #


class TestTwoWriterSettlement(_PipelineCase):
	def _seed_terminal_observed(self, conv, rid, epoch):
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
		self._mk_turn(
			conv,
			rid,
			seed,
			"terminal_observed",
			version=6,
			pump_epoch=epoch,
			reserved=1,
			assistant_message=amsg,
			terminal_kind="relay:final",
			terminal_payload=json.dumps({"text": "final answer"}),
			terminal_observed_at=frappe.utils.now(),
			dispatching_at=frappe.utils.now(),
		)
		return amsg

	def _settle(self, rid, conv, epoch, deps):
		settlement.invoke_settlement(
			rid,
			relay_target_id=self._target,
			epoch=epoch,
			version=7,
			terminal_kind="relay:final",
			terminal_payload={"text": "final answer"},
			assistant_message=None,
			owner=self._orig_user,
			conversation=conv,
			deps=deps,
		)

	def test_two_settlement_writers_side_effects_exactly_once(self):
		# Two invocations of settlement race on the SAME terminal_observed turn (the
		# pump's on_terminal AND the reconcile/recovery path). The epoch+version CAS
		# + the state guard make exactly ONE win; the loser is a no-op. Side effects
		# (final projection, slot release, effect-ledger rows, run:end) apply once.
		for order in ("terminal_first", "recovery_first"):
			with self.subTest(order=order):
				_cleanup_rid = f"pmp_2w_{order}"
				conv = self._mk_conv()
				epoch = self._acquire_fresh("twowriter")
				amsg = self._seed_terminal_observed(conv, _cleanup_rid, epoch)
				fin_rec = _Recorder()
				deps = pump.PumpDeps()
				deps.enqueue_finalize = fin_rec
				self._pubs.clear()

				self._settle(_cleanup_rid, conv, epoch, deps)  # winner
				# Second writer (either order maps to the same idempotent call).
				try:
					self._settle(_cleanup_rid, conv, epoch, deps)  # loser -> no-op
				except ts.LeaseLostExit:
					pass

				self.assertEqual(self._state(_cleanup_rid), "finalizing")
				self.assertEqual(int(self._val(_cleanup_rid, "reserved")), 0)
				# final projection written once.
				self.assertEqual(frappe.db.get_value(MSG, amsg, "content"), "final answer")
				# effect rows inserted exactly once (composite PK idempotent).
				self.assertEqual(len(self._effects(_cleanup_rid)), len(settlement.FINAL_EFFECTS))
				# exactly one run:end published, one finalize enqueued.
				self.assertEqual(self._pub_kinds().count("run:end"), 1)
				self.assertEqual(fin_rec.count, 1)

	def test_settlement_vs_watchdog_benign_loss_does_not_kill_hop(self):
		"""OARF-3 / §10.11 (the requested THIRD writer order): settlement races a
		DIFFERENT writer (the watchdog moving a stuck terminal_observed turn to
		recovering). A benign 0-rows loss with the epoch INTACT must NOT
		``lease_lost_exit`` — that would kill the hop and stall the whole shard for
		an ordinary optimistic-concurrency loss."""
		conv = self._mk_conv()
		rid = "pmp_2w_wd"
		epoch = self._acquire_fresh("wd_race")
		amsg = self._seed_terminal_observed(conv, rid, epoch)  # v6, pump_epoch=epoch
		# The watchdog (a DIFFERENT writer) moves the terminal_observed turn to
		# recovering: version -> 7, epoch UNTOUCHED.
		self.assertTrue(ts.mark_recovering(rid, 6))
		frappe.db.commit()
		self.assertEqual(self._state(rid), "recovering")
		# Faithful TOCTOU: settlement had read the row as terminal_observed@v6 BEFORE
		# the watchdog moved it, so its CAS now 0-rows with the epoch intact.
		stale = {
			"state": "terminal_observed",
			"version": 6,
			"pump_epoch": epoch,
			"reserved": 1,
			"assistant_message": amsg,
			"terminal_kind": "relay:final",
			"cancel_requested": 0,
			"last_event_seq": 3,
			"recovering": 0,
			"dispatching_at": frappe.utils.now(),
		}
		deps = pump.PumpDeps()
		deps.enqueue_finalize = _Recorder()
		self._pubs.clear()
		with patch.object(ts, "read_turn", return_value=stale):
			# Epoch intact -> a clean no-op (rollback + return), NEVER LeaseLostExit.
			settlement.invoke_settlement(
				rid,
				relay_target_id=self._target,
				epoch=epoch,
				version=6,
				terminal_kind="relay:final",
				terminal_payload={"text": "final answer"},
				assistant_message=amsg,
				owner=self._orig_user,
				conversation=conv,
				deps=deps,
			)
		# The hop survives; the watchdog still owns the (recovering) turn; the S1
		# message write was rolled back; no finalize/run:end leaked.
		self.assertEqual(self._state(rid), "recovering", "the concurrent actor still owns the turn")
		self.assertEqual(frappe.db.get_value(MSG, amsg, "content"), "partial", "S1 write rolled back")
		self.assertEqual(deps.enqueue_finalize.count, 0)
		self.assertNotIn("run:end", self._pub_kinds())

	def test_settlement_epoch_mismatch_raises_lease_lost(self):
		"""OARF-3 / §10.11: when the 0-rows loss is a genuine TAKEOVER (epoch no
		longer matches) settlement DOES route through the shared lease-loss exit."""
		conv = self._mk_conv()
		rid = "pmp_2w_takeover"
		epoch = self._acquire_fresh("takeover_race")
		self._seed_terminal_observed(conv, rid, epoch)
		# A takeover re-stamps the turn to a new epoch (expire the lease first).
		frappe.db.set_value(
			"Jarvis Relay Pump",
			self._target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.commit()
		won, new_epoch = ts.lease_acquire(self._target, "taker")
		self.assertTrue(won)
		self.assertGreater(new_epoch, epoch)
		self.assertEqual(int(self._val(rid, "pump_epoch")), new_epoch, "turn re-stamped by the takeover")
		deps = pump.PumpDeps()
		deps.enqueue_finalize = _Recorder()
		with self.assertRaises(ts.LeaseLostExit):
			settlement.invoke_settlement(
				rid,
				relay_target_id=self._target,
				epoch=epoch,
				version=6,
				terminal_kind="relay:final",
				terminal_payload={"text": "final answer"},
				assistant_message=None,
				owner=self._orig_user,
				conversation=conv,
				deps=deps,
			)
		self.assertEqual(deps.enqueue_finalize.count, 0)


# --------------------------------------------------------------------------- #
# 7. SUX-11 — error-payload contract preserved
# --------------------------------------------------------------------------- #


class TestSux11ErrorContract(_PipelineCase):
	def test_error_terminal_preserves_classification(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="streamed so far", streaming=1)
		rid = "pmp_sux11"
		won, epoch = ts.lease_acquire(self._target, "sux11")
		self._mk_turn(
			conv,
			rid,
			seed,
			"terminal_observed",
			version=3,
			pump_epoch=epoch,
			reserved=1,
			assistant_message=amsg,
			terminal_kind="relay:error",
			terminal_payload=json.dumps({"state": "error", "error": "provider quota exceeded"}),
			terminal_observed_at=frappe.utils.now(),
		)
		fin_rec = _Recorder()
		deps = pump.PumpDeps()
		deps.enqueue_finalize = fin_rec
		self._pubs.clear()

		settlement.invoke_settlement(
			rid,
			relay_target_id=self._target,
			epoch=epoch,
			version=4,
			terminal_kind="relay:error",
			terminal_payload={"state": "error", "error": "provider quota exceeded"},
			assistant_message=None,
			owner=self._orig_user,
			conversation=conv,
			deps=deps,
		)
		self.assertEqual(self._state(rid), "errored")
		# The Message.error is set + streaming cleared; the streamed content is
		# PRESERVED (not overwritten with the error), matching legacy _mark_errored.
		row = frappe.db.get_value(MSG, amsg, ["error", "streaming", "content"], as_dict=True)
		self.assertEqual(row["error"], "provider quota exceeded")
		self.assertEqual(int(row["streaming"]), 0)
		self.assertEqual(row["content"], "streamed so far")
		# The run:error event carries today's classification code (SUX-11).
		err_pub = next(p for p in self._pubs if p.get("kind") == "run:error")
		self.assertEqual(err_pub["error"], "provider quota exceeded")
		self.assertEqual(err_pub["code"], "provider", "quota -> 'provider' headline (ERROR_HEADLINES)")
		# Turn.error mirrors it too.
		self.assertEqual(self._val(rid, "error"), "provider quota exceeded")


# --------------------------------------------------------------------------- #
# 8. Usage (turn_id) idempotency under finalize replay
# --------------------------------------------------------------------------- #


class TestUsageIdempotency(_PipelineCase):
	def test_usage_recorded_once_under_replay(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="ans", streaming=0)
		rid = "pmp_usage"
		frappe.db.set_value(CONV, conv, "session_key", "sess-usage-x")
		self._mk_turn(conv, rid, seed, "finalizing", version=5, assistant_message=amsg)
		ts.insert_required_effects(rid, ("usage",))
		frappe.db.commit()
		fake = _FakeSess()
		fake._key = "sess-usage-x"
		rec = _Recorder()

		# Two finalize passes (a replay) — record_turn_usage must fire AT MOST ONCE
		# (the usage_recorded (turn_id) CAS guard, R-4). The soft cap is never
		# double-counted.
		with self._gateway(fake), patch("jarvis.chat.usage.record_turn_usage", rec):
			finalize.run_finalize(rid, self._target)
			finalize.run_finalize(rid, self._target)

		self.assertEqual(rec.count, 1, "usage recorded exactly once across the replay")
		self.assertEqual(int(self._val(rid, "usage_recorded")), 1)
		self.assertEqual(self._state(rid), "done")

	def test_usage_guard_rolls_back_on_poll_failure_and_retries(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="ans", streaming=0)
		rid = "pmp_usage_fail"
		frappe.db.set_value(CONV, conv, "session_key", "sess-usage-y")
		self._mk_turn(conv, rid, seed, "finalizing", version=5, assistant_message=amsg)
		ts.insert_required_effects(rid, ("usage",))
		frappe.db.commit()
		fake = _FakeSess()
		fake._key = "sess-usage-y"

		# A transient poll failure must ROLL BACK the guard so usage retries next
		# cycle (never a silent permanent loss on a hiccup).
		with (
			self._gateway(fake),
			patch("jarvis.chat.usage.fetch_fresh_session_row", side_effect=RuntimeError("gateway hiccup")),
		):
			finalize.run_finalize(rid, self._target)
		self.assertEqual(int(self._val(rid, "usage_recorded")), 0, "guard rolled back on failure")
		self.assertEqual(self._effects(rid)["usage"], "pending", "usage effect stays pending for retry")

		# The retry succeeds and records once.
		rec = _Recorder()
		with self._gateway(fake), patch("jarvis.chat.usage.record_turn_usage", rec):
			finalize.run_finalize(rid, self._target)
		self.assertEqual(rec.count, 1)
		self.assertEqual(self._state(rid), "done")


# --------------------------------------------------------------------------- #
# 9. SUXF-2 — dispatch-time definite rejection carries changed_data=False
# --------------------------------------------------------------------------- #


class TestSuxf2AckFailureContract(_PipelineCase):
	def test_definite_rejection_carries_changed_data_false(self):
		"""SUXF-2: the pump's own pre-ack definite-rejection surface
		(_handle_ack_failure) must publish run:error with changed_data=False — the
		run never started, so 'No changes were made to your data' is honest (parity
		with prepare._prepare_error + legacy)."""
		from jarvis.tests.test_pump import _RejectGateway

		conv = self._mk_conv()
		rid = "pmp_suxf2"
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
		double = _RejectGateway()
		self._doubles.append(double)
		deps = self._deps(double=double)
		ctx = self._make_ctx(deps)
		self._pubs.clear()
		pump._dispatch_ready(ctx)  # OARF-5: non-blocking issue
		self._pump_until(ctx, lambda: self._state(rid) in ("errored", "recovering"))
		self.assertEqual(self._state(rid), "errored")
		err = next(p for p in self._pubs if p.get("kind") == "run:error")
		self.assertIs(err.get("changed_data"), False, "SUXF-2: pre-ack rejection => changed_data False")


# --------------------------------------------------------------------------- #
# 10. SUXF-1 — every recovering/errored transition mirrors the Message row
# --------------------------------------------------------------------------- #


class TestSuxf1RecoveringMirror(_PipelineCase):
	def test_watchdog_deadline_park_writes_message_mirror_and_publishes(self):
		conv = self._mk_conv()
		rid = "pmp_suxf1_park"
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
		epoch = self._acquire_fresh("suxf1a")
		self._mk_turn(
			conv,
			rid,
			seed,
			"streaming",
			version=4,
			pump_epoch=epoch,
			reserved=1,
			assistant_message=amsg,
			dispatching_at=frappe.utils.now(),
			deadline_at=frappe.utils.add_to_date(None, seconds=-5),  # soft deadline passed
		)
		self._pubs.clear()
		wd_deps = pump.PumpDeps()
		wd_deps.enqueue_pump_job = _Recorder()
		with patch.object(pump, "pump_configured", return_value=True):
			pump.watchdog(deps=wd_deps)
		self.assertEqual(self._state(rid), "recovering")
		# SUXF-1: the Message row mirrors the recovering state so a reload / 2nd tab
		# reconstructs the 'Reconnecting' banner (not a plain locked composer).
		row = frappe.db.get_value(MSG, amsg, ["recovering", "recovery_started_at"], as_dict=True)
		self.assertEqual(int(row["recovering"]), 1)
		self.assertIsNotNone(row["recovery_started_at"])
		self.assertIn("run:recovering", self._pub_kinds())

	def test_watchdog_budget_exhausted_errors_message_and_publishes(self):
		conv = self._mk_conv()
		rid = "pmp_suxf1_err"
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1, recovering=1)
		epoch = self._acquire_fresh("suxf1b")
		self._mk_turn(
			conv,
			rid,
			seed,
			"recovering",
			version=4,
			pump_epoch=epoch,
			reserved=1,
			assistant_message=amsg,
			recovering=1,
			dispatching_at=frappe.utils.now(),
			recovery_started_at=frappe.utils.add_to_date(None, seconds=-(pump.RECOVERY_BUDGET_S + 60)),
		)
		self._pubs.clear()
		wd_deps = pump.PumpDeps()
		wd_deps.enqueue_pump_job = _Recorder()
		with patch.object(pump, "pump_configured", return_value=True):
			pump.watchdog(deps=wd_deps)
		self.assertEqual(self._state(rid), "errored")
		# SUXF-1: a budget-exhausted turn reaches the terminal error UX — the Message
		# never sits forever at streaming=1 with an empty spinner and no backstop.
		row = frappe.db.get_value(MSG, amsg, ["streaming", "error"], as_dict=True)
		self.assertEqual(int(row["streaming"]), 0)
		self.assertTrue(row["error"])
		self.assertIn("run:error", self._pub_kinds())
		self.assertEqual(int(self._val(rid, "reserved")), 0, "credit released")

	def test_db_disconnect_park_writes_mirror_and_publishes(self):
		conv = self._mk_conv()
		rid = "pmp_suxf1_db"
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
		epoch = self._acquire_fresh("suxf1c")
		self._mk_turn(
			conv,
			rid,
			seed,
			"streaming",
			version=4,
			pump_epoch=epoch,
			reserved=1,
			assistant_message=amsg,
			dispatching_at=frappe.utils.now(),
		)
		ctx = pump.PumpContext(
			relay_target_id=self._target,
			epoch=epoch,
			holder="db",
			hop_counter=0,
			site=frappe.local.site,
			deps=self._deps(),
		)
		self._pubs.clear()
		pump._park_affected_recovering(ctx)
		self.assertEqual(self._state(rid), "recovering")
		self.assertEqual(int(frappe.db.get_value(MSG, amsg, "recovering") or 0), 1)
		self.assertIn("run:recovering", self._pub_kinds())


# --------------------------------------------------------------------------- #
# 11. OARF-6 — the bench-side delta batcher relocated into the lane handler
# --------------------------------------------------------------------------- #


class TestDeltaBatcher(_PipelineCase):
	def test_delta_publishes_are_batched_not_per_frame(self):
		"""OARF-6: the 'success' transcript streams many cumulative delta frames at a
		1ms cadence (< the 250ms batch interval), so the relocated batcher must
		coalesce them into FEW commits+publishes (N=10/250ms) — a bounded gap count,
		not one publish per frame."""
		import math

		from jarvis.tests.harness import transcripts as _t

		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="hello")
		rid = "pmp_batch"
		fake = _FakeSess()
		double = self._double()

		def real_prepare(run_id, target):
			with self._gateway(fake), self._pump_on():
				prepare.run_prepare(run_id, target)

		self._mk_turn(conv, rid, seed, "queued", version=0, reserved=0)
		deps = self._deps(double=double, prepare=real_prepare)
		ctx = self._make_ctx(deps)
		pump._promote_queued(ctx)
		double.arm(rid, "success")
		self._pubs.clear()
		self._pump_until(ctx, lambda: self._state(rid) in ("finalizing", "done"))

		deltas = [p for p in self._pubs if p.get("kind") == "assistant:delta"]
		frames = [f for f in _t.get("success")["frames"] if f.get("op") == "assistant"]
		self.assertGreater(len(frames), 15, "the success transcript streams many delta frames")
		self.assertGreaterEqual(len(deltas), 1, "at least one delta published (first token flushes now)")
		self.assertLess(len(deltas), len(frames), "batcher coalesced deltas (fewer publishes than frames)")
		# Cadence bound (size-triggered under the fast cadence): ceil(frames/N) + a
		# small slop for the immediate first-token flush + the terminal tail flush.
		self.assertLessEqual(
			len(deltas),
			math.ceil(len(frames) / pump._DELTA_BATCH_SIZE) + 3,
			"publish count within the N=10 batch cadence",
		)


# --------------------------------------------------------------------------- #
# 11. CDX-5 — the PRODUCTION tool applier (no recorder injection)
# --------------------------------------------------------------------------- #


class TestToolApplierEquivalence(_PipelineCase):
	"""CDX-5 equivalence RE-RUN: the tool-heavy transcript through the pump with the
	DEFAULT (production) ``apply_tool`` — NOT the recorder the Stage-B evidence used.
	Built-in openclaw tools (browser) must produce their durable role=tool receipt +
	tool-end update + start/end publishes; ``jarvis__*`` callback-owned tools must
	publish lifecycle ONLY (no pump-owned receipt row)."""

	def test_production_applier_tool_heavy_equivalence(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="how much do we owe?")
		rid = "pmp_toolheavy"
		self._mk_turn(conv, rid, seed, "queued", version=0, reserved=0)
		double = self._double()
		deps = self._deps(double=double, prepare=self._prepare_stub(conv, double, "tool-heavy"))
		# The whole point of CDX-5: use the REAL applier, not a recorder.
		deps.apply_tool = pump.PumpDeps().apply_tool
		ctx = self._make_ctx(deps)
		self._pubs.clear()
		self._pump_until(ctx, lambda: self._state(rid) in ("finalizing", "done"))

		# Built-in tool (browser, t3): durable receipt row inserted + closed.
		row = frappe.db.get_value(
			MSG,
			{"conversation": conv, "tool_call_id": "t3", "role": "tool"},
			["name", "tool_name", "tool_status", "streaming"],
			as_dict=True,
		)
		self.assertIsNotNone(row, "built-in browser tool got a durable role=tool receipt")
		self.assertEqual(row["tool_name"], "browser")
		self.assertEqual(row["tool_status"], "completed")
		self.assertEqual(int(row["streaming"]), 0, "tool-end closed the receipt")

		# jarvis__* tools (t1/t2): NO pump-owned receipt row (callback owns it, R-6).
		self.assertFalse(
			frappe.db.exists(MSG, {"conversation": conv, "tool_call_id": "t1", "role": "tool"}),
			"jarvis__* tool receipt is NOT pump-owned",
		)
		self.assertFalse(frappe.db.exists(MSG, {"conversation": conv, "tool_call_id": "t2", "role": "tool"}))

		# Lifecycle publishes fired for ALL three tools (3 start + 3 end), epoch-fenced.
		starts = [p for p in self._pubs if p.get("kind") == "tool:start"]
		ends = [p for p in self._pubs if p.get("kind") == "tool:end"]
		self.assertEqual(len(starts), 3, "a tool:start per tool")
		self.assertEqual(len(ends), 3, "a tool:end per tool")
		for p in starts + ends:
			# CDX-3: EVERY pump tool event (built-in AND jarvis__*) carries the run-scoped
			# fence keys — run_id + pump_epoch + event_seq — so the client can fence a stale
			# writer's tool straggler even though jarvis__* events have no message_id.
			self.assertEqual(p.get("pump_epoch"), ctx.epoch, "tool events epoch-fenced (P0-3 contract)")
			self.assertEqual(p.get("run_id"), rid, "tool events carry run_id (CDX-3 run-scoped fence)")
			self.assertIsNotNone(p.get("event_seq"), "tool events carry event_seq (CDX-3)")
		# The built-in tool's publishes carry the receipt message_id; jarvis__ ones None.
		bstart = next(p for p in starts if p.get("tool_call_id") == "t3")
		self.assertEqual(bstart.get("message_id"), row["name"])
		jstart = next(p for p in starts if p.get("tool_call_id") == "t1")
		self.assertIsNone(jstart.get("message_id"))

	def test_builtin_tool_row_idempotent_on_replay(self):
		# CDX-5: a re-applied start (hop re-attach / replayed frame) reuses the durable
		# (conversation, tool_call_id) row instead of doubling it.
		conv = self._mk_conv()
		rid = "pmp_toolidem"
		self._mk_msg(conv, content="x")
		self._mk_turn(conv, rid, self._mk_msg(conv), "streaming", version=3, pump_epoch=1)
		ctx = self._make_ctx(self._deps(), with_mux=False)
		ctx.epoch = int(frappe.db.get_value("Jarvis Relay Pump", self._target, "pump_epoch"))
		rs = pump._RunState(
			run_id=rid,
			conversation=conv,
			owner=TEST_USER,
			assistant_message=None,
			session_key="s",
			version=int(frappe.db.get_value(TURN, rid, "version")),
		)
		ev = {"event_seq": 1, "phase": "start", "tool_name": "browser", "tool_call_id": "tz", "title": "t"}
		pump._default_apply_tool(ctx, rs, ev)
		pump._default_apply_tool(ctx, rs, ev)  # replay
		rows = frappe.get_all(MSG, filters={"conversation": conv, "tool_call_id": "tz", "role": "tool"})
		self.assertEqual(len(rows), 1, "replayed tool start reuses the durable row (idempotent)")

	def test_stale_pump_tool_application_after_takeover_writes_nothing(self):
		# CDX-15: pause A immediately before its tool application; B takes over (epoch E+1)
		# and SETTLES the turn; A resumes and applies a tool-start. The fence CAS affects
		# 0 rows, so A inserts NO durable tool row into the already-settled conversation
		# and publishes NOTHING — the LeaseLostExit routes A through the shared exit.
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="q")
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
		rid = "pmp_tool_takeover"
		# A owns a streaming turn. _make_ctx acquires the lease (epoch EA) and re-stamps
		# this turn to EA — so A is the legit owner at EA with the turn's current version.
		self._mk_turn(conv, rid, seed, "streaming", version=5, reserved=1, assistant_message=amsg)
		frappe.db.commit()
		ctx_a = self._make_ctx(self._deps(), with_mux=False)
		EA = ctx_a.epoch
		self.assertEqual(int(frappe.db.get_value(TURN, rid, "pump_epoch")), EA, "turn owned by A at EA")
		rs_a = pump._RunState(
			run_id=rid,
			conversation=conv,
			owner=TEST_USER,
			assistant_message=amsg,
			session_key="s",
			version=int(frappe.db.get_value(TURN, rid, "version")),
		)
		# --- A pauses here. Make the lease stale so B can take over (epoch EA+1, re-stamps
		# the turn) and SETTLES it. ---
		frappe.db.set_value(
			"Jarvis Relay Pump",
			self._target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.commit()
		won, e2 = ts.lease_acquire(self._target, "hopB")
		self.assertTrue(won)
		self.assertEqual(e2, EA + 1)
		vB = int(frappe.db.get_value(TURN, rid, "version"))
		self.assertTrue(ts.mark_terminal_observed(rid, vB, e2, "relay:final", {"text": "final"}))
		frappe.db.commit()
		self.assertTrue(ts.settle_finalizing(rid, vB + 1, e2, required_effects=("terminal_publish",)))
		frappe.db.commit()
		self.assertEqual(self._state(rid), "finalizing")
		# --- A resumes and applies the buffered tool-start. It must write/publish nothing. ---
		self._pubs.clear()
		ev = {
			"event_seq": 9,
			"phase": "start",
			"tool_name": "browser",
			"tool_call_id": "tk_stale",
			"title": "t",
		}
		with self.assertRaises(ts.LeaseLostExit):
			pump._default_apply_tool(ctx_a, rs_a, ev)
		self.assertFalse(
			frappe.db.exists(MSG, {"conversation": conv, "tool_call_id": "tk_stale", "role": "tool"}),
			"stale pump inserted NO durable tool row into the settled conversation",
		)
		self.assertEqual(
			[p for p in self._pubs if p.get("kind") in ("tool:start", "tool:end")],
			[],
			"stale pump published NO tool lifecycle event",
		)

	def test_tool_apply_failure_after_fence_rolls_back_before_quarantine(self):
		# CDX-18: an exception AFTER the apply_tool_fenced CAS wins (the durable tool-row write
		# or the atomic commit raises) must roll the partial txn back BEFORE it propagates to
		# the mux, else quarantine's recovery CAS would COMMIT this half-done txn (Turn version
		# + watermark advanced, tool row MISSING => replay skips the precious event). Inject a
		# failure right after the fence CAS and assert codex's four conditions.
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="q")
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
		rid = "pmp_tool_cdx18"
		self._mk_turn(
			conv, rid, seed, "streaming", version=5, reserved=1, last_event_seq=3, assistant_message=amsg
		)
		frappe.db.commit()
		ctx = self._make_ctx(self._deps(), with_mux=False)  # acquires the lease + re-stamps the turn to E
		V = int(frappe.db.get_value(TURN, rid, "version"))
		W = int(frappe.db.get_value(TURN, rid, "last_event_seq") or 0)
		rs = pump._RunState(
			run_id=rid,
			conversation=conv,
			owner=TEST_USER,
			assistant_message=amsg,
			session_key="s",
			version=V,
		)
		self._pubs.clear()
		ev = {
			"event_seq": 9,
			"phase": "start",
			"tool_name": "browser",
			"tool_call_id": "tk_boom",
			"title": "t",
		}
		# The fence CAS wins first; the durable row insert then raises (disk full / lock wait).
		with patch.object(pump, "_insert_tool_start_row", side_effect=RuntimeError("disk full")):
			with self.assertRaises(RuntimeError):
				pump._default_apply_tool(ctx, rs, ev)
		# (1) Turn version + watermark are back at their pre-event values (the CAS rolled back).
		self.assertEqual(
			int(frappe.db.get_value(TURN, rid, "version")), V, "Turn version restored — no leaked advance"
		)
		self.assertEqual(
			int(frappe.db.get_value(TURN, rid, "last_event_seq") or 0),
			W,
			"watermark restored — no leaked advance",
		)
		self.assertEqual(rs.version, V, "in-memory rs.version re-synced from the durable row after rollback")
		# (2) No durable tool row was written.
		self.assertFalse(
			frappe.db.exists(MSG, {"conversation": conv, "tool_call_id": "tk_boom", "role": "tool"}),
			"no tool receipt row inserted on the failed apply",
		)
		# (3) No lifecycle event published.
		self.assertEqual(
			[p for p in self._pubs if p.get("kind") in ("tool:start", "tool:end")],
			[],
			"no tool lifecycle event published on the failed apply",
		)
		# (4) Quarantine/recovery proceeds from a FRESH transaction: _park_recovering marks the
		#     turn recovering against the CLEAN pre-event version (V -> V+1), NOT a leaked V+1.
		pump._park_recovering(ctx, rid, reason="precious_fault")
		self.assertEqual(self._state(rid), "recovering", "recovery proceeded from a fresh, consistent txn")
		self.assertEqual(
			int(frappe.db.get_value(TURN, rid, "version")),
			V + 1,
			"recovery CAS bumped the CLEAN version exactly once",
		)


# --------------------------------------------------------------------------- #
# 12. CDX-6 — usage honesty (no permanent 'recorded' on stale/missing data)
# --------------------------------------------------------------------------- #


class TestUsageHonesty(_PipelineCase):
	def _finalizing_usage_turn(self, rid, session_key):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="ans", streaming=0)
		frappe.db.set_value(CONV, conv, "session_key", session_key)
		self._mk_turn(conv, rid, seed, "finalizing", version=5, assistant_message=amsg)
		ts.insert_required_effects(rid, ("usage",))
		frappe.db.commit()

	def test_stale_row_does_not_mark_recorded(self):
		# CDX-6: a fresh=False row is a `retry` — the usage effect must NOT commit the
		# guard; it stays pending for a later cycle (never a silent permanent loss).
		self._finalizing_usage_turn("pmp_usage_stale", "sess-stale")
		fake = _FakeSess()
		fake._key = "sess-stale"
		stale_row = {"key": "sess-stale", "totalTokensFresh": False, "inputTokens": 10, "outputTokens": 5}
		with self._gateway(fake), patch("jarvis.chat.usage.fetch_fresh_session_row", return_value=stale_row):
			finalize.run_finalize("pmp_usage_stale", self._target)
		self.assertEqual(int(self._val("pmp_usage_stale", "usage_recorded")), 0, "guard NOT set on stale")
		self.assertEqual(self._effects("pmp_usage_stale")["usage"], "pending", "usage stays pending")

	def test_no_row_does_not_mark_recorded(self):
		# CDX-6: no row at all (None) is a `retry`.
		self._finalizing_usage_turn("pmp_usage_none", "sess-none")
		fake = _FakeSess()
		fake._key = "sess-none"
		with self._gateway(fake), patch("jarvis.chat.usage.fetch_fresh_session_row", return_value=None):
			finalize.run_finalize("pmp_usage_none", self._target)
		self.assertEqual(int(self._val("pmp_usage_none", "usage_recorded")), 0)
		self.assertEqual(self._effects("pmp_usage_none")["usage"], "pending")

	def test_delayed_fresh_row_records_on_retry(self):
		# CDX-6: stale on the first cycle (pending), fresh on the next (recorded once).
		self._finalizing_usage_turn("pmp_usage_delay", "sess-delay")
		fake = _FakeSess()
		fake._key = "sess-delay"
		stale = {"key": "sess-delay", "totalTokensFresh": False}
		fresh = {"key": "sess-delay", "totalTokensFresh": True, "inputTokens": 40, "outputTokens": 10}
		with self._gateway(fake), patch("jarvis.chat.usage.fetch_fresh_session_row", return_value=stale):
			finalize.run_finalize("pmp_usage_delay", self._target)
		self.assertEqual(self._effects("pmp_usage_delay")["usage"], "pending")
		rec = _Recorder()
		with (
			self._gateway(fake),
			patch("jarvis.chat.usage.fetch_fresh_session_row", return_value=fresh),
			patch("jarvis.chat.usage.record_turn_usage", rec),
		):
			finalize.run_finalize("pmp_usage_delay", self._target)
		self.assertEqual(rec.count, 1, "recorded exactly once on the fresh retry")
		self.assertEqual(int(self._val("pmp_usage_delay", "usage_recorded")), 1)
		self.assertEqual(self._state("pmp_usage_delay"), "done")

	def test_no_session_key_does_not_mark_recorded(self):
		# CDX-6: a SUCCESSFUL turn with NO session key is unattributed real usage, NOT
		# legitimate zero — it must NOT permanently mark usage recorded; it retries (and
		# the force-done budget logs the undercount if the key never materializes).
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="ans", streaming=0)
		# Deliberately DO NOT set conv.session_key and no dispatch_payload session_key.
		self._mk_turn(conv, "pmp_usage_nokey", seed, "finalizing", version=5, assistant_message=amsg)
		ts.insert_required_effects("pmp_usage_nokey", ("usage",))
		frappe.db.commit()
		finalize.run_finalize("pmp_usage_nokey", self._target)
		self.assertEqual(
			int(self._val("pmp_usage_nokey", "usage_recorded")), 0, "no-session-key does NOT mark recorded"
		)
		self.assertEqual(self._effects("pmp_usage_nokey")["usage"], "pending", "usage stays pending to retry")

	def test_unmapped_user_positive_delta_retries(self):
		# CDX-6: a FRESH POSITIVE token delta whose session_key has no `Jarvis Chat
		# Session` user mapping is unattributed real usage => USAGE_RETRY, never VALID_ZERO.
		from jarvis.chat import usage as _usage

		row = {"key": "sess-unmapped", "totalTokensFresh": True, "inputTokens": 30, "outputTokens": 12}
		# Ensure there is no mapping row for this session key.
		frappe.db.delete(SESSION, {"session_key": "sess-unmapped"})
		frappe.db.commit()
		self.assertEqual(
			_usage.record_turn_usage("sess-unmapped", row),
			_usage.USAGE_RETRY,
			"positive delta + unmapped user => RETRY (not VALID_ZERO)",
		)


# --------------------------------------------------------------------------- #
# 13. CDX-12 — terminal_publish backstop
# --------------------------------------------------------------------------- #


class TestTerminalPublishBackstop(_PipelineCase):
	def test_suppressed_settlement_terminal_delivered_by_finalize(self):
		# CDX-12: settlement's terminal publish is LOST (belt-skip / socket hiccup) but
		# the DB settled; finalize's terminal_publish effect re-delivers run:end so the
		# client can clear its spinner from durable truth.
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="final answer", streaming=0)
		rid = "pmp_termpub"
		self._mk_turn(
			conv,
			rid,
			seed,
			"finalizing",
			version=5,
			pump_epoch=1,
			last_event_seq=7,
			assistant_message=amsg,
			terminal_kind="relay:final",
			terminal_payload=json.dumps({"text": "final answer"}),
		)
		# The owed set includes terminal_publish (CDX-12).
		ts.insert_required_effects(rid, settlement.FINAL_EFFECTS)
		frappe.db.commit()
		self.assertIn("terminal_publish", self._effects(rid))
		self._pubs.clear()
		with self._mock_enrichment():
			finalize.run_finalize(rid, self._target)
		ends = [p for p in self._pubs if p.get("kind") == "run:end"]
		self.assertGreaterEqual(len(ends), 1, "finalize re-published the terminal run:end")
		self.assertEqual(ends[0].get("run_id"), rid)
		# CDX-12: the re-publish carries a STABLE terminal identity (run_id + epoch +
		# terminal seq = the durable watermark) so the client one-shot fence dedupes it.
		self.assertEqual(ends[0].get("pump_epoch"), 1)
		self.assertEqual(ends[0].get("event_seq"), 7, "terminal seq = the durable watermark")
		self.assertEqual(self._effects(rid)["terminal_publish"], "done")

	def test_settlement_and_finalize_terminals_share_identity(self):
		# CDX-12: settlement's authoritative terminal AND finalize's backstop re-publish
		# carry the SAME (run_id, pump_epoch, event_seq) so the client one-shot fence
		# recognises the backstop as a duplicate of the settled terminal (no repeated
		# announcement / reload) — while still clearing a spinner on a genuine miss.
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="hello", streaming=1)
		rid = "pmp_term_identity"
		E = self._acquire_fresh("hopT")
		self._mk_turn(
			conv,
			rid,
			seed,
			"terminal_observed",
			version=4,
			pump_epoch=E,
			last_event_seq=11,
			assistant_message=amsg,
			terminal_kind="relay:final",
			terminal_payload=json.dumps({"text": "hello"}),
		)
		frappe.db.commit()
		self._pubs.clear()
		# Settlement emits the authoritative terminal.
		settlement.invoke_settlement(
			rid,
			relay_target_id=self._target,
			epoch=E,
			version=4,
			terminal_kind="relay:final",
			terminal_payload={"text": "hello"},
			assistant_message=amsg,
			owner=TEST_USER,
			conversation=conv,
			deps=self._deps(),
		)
		settle_end = next(p for p in self._pubs if p.get("kind") == "run:end")
		# Finalize's backstop re-publish.
		self._pubs.clear()
		with self._mock_enrichment():
			finalize.run_finalize(rid, self._target)
		fin_end = next(p for p in self._pubs if p.get("kind") == "run:end")
		self.assertEqual(
			(settle_end.get("run_id"), settle_end.get("pump_epoch"), settle_end.get("event_seq")),
			(fin_end.get("run_id"), fin_end.get("pump_epoch"), fin_end.get("event_seq")),
			"settlement + finalize terminals share a stable identity (one-shot dedup)",
		)
		self.assertEqual(settle_end.get("event_seq"), 11)

	def test_completed_pump_turn_persists_real_duration(self):
		# CDX-20 / F4: the pump projects the assistant row via raw SQL (and the streaming batcher
		# writes deltas with update_modified=False), so `modified` never advanced past the
		# placeholder insert — a reload's duration read 0.0s. Settlement now stamps a dedicated
		# `reply_duration_ms` on ONE baseline SHARED with the live timer: run:start (dispatching_at,
		# stamped immediately before run:start is published) -> settlement (now). It must NOT rewrite
		# the `creation` audit field, and must anchor on dispatching_at (run:start ~15s ago), NOT
		# first_event_at (the chat.send ACK ~12s ago, which undercounts the span the user watched).
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="hi", streaming=1)
		creation_before = frappe.db.get_value(MSG, amsg, "creation")
		rid = "pmp_duration"
		E = self._acquire_fresh("hopDur")
		self._mk_turn(
			conv,
			rid,
			seed,
			"terminal_observed",
			version=4,
			pump_epoch=E,
			last_event_seq=7,
			assistant_message=amsg,
			terminal_kind="relay:final",
			terminal_payload=json.dumps({"text": "hi"}),
			# run:start (dispatching_at) ~15s ago; first token (first_event_at) ~12s ago. The live
			# timer measured run:start->run:end, so the persisted value must anchor on dispatching_at.
			dispatching_at=frappe.utils.add_to_date(None, seconds=-15),
			first_event_at=frappe.utils.add_to_date(None, seconds=-12),
		)
		frappe.db.commit()
		settlement.invoke_settlement(
			rid,
			relay_target_id=self._target,
			epoch=E,
			version=4,
			terminal_kind="relay:final",
			terminal_payload={"text": "hi"},
			assistant_message=amsg,
			owner=TEST_USER,
			conversation=conv,
			deps=self._deps(),
		)
		row = frappe.db.get_value(MSG, amsg, ["creation", "reply_duration_ms"], as_dict=True)
		# CDX-20: the `creation` audit field is UNTOUCHED (no longer repurposed as a metric).
		self.assertEqual(row.creation, creation_before, "creation (the audit field) must not be rewritten")
		# Live-definition equality within tolerance: the persisted span == now - dispatching_at
		# (run:start->settlement) ≈ 15s. This distinguishes the correct dispatching_at anchor from
		# the round-3 first_event_at anchor, which would read ~12s.
		self.assertIsNotNone(row.reply_duration_ms, "a dedicated reply_duration_ms is persisted")
		secs = row.reply_duration_ms / 1000
		self.assertGreaterEqual(
			secs,
			14,
			f"duration anchors on dispatching_at (run:start ~15s), not first_event_at (~12s); got {secs}s",
		)
		self.assertLess(
			secs, 30, "duration stays within the UI's sane window (test-timing tolerance around 15s)"
		)

	def test_terminal_publish_for_errored_turn_republishes_run_error(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="", streaming=0, error="boom")
		rid = "pmp_termpub_err"
		self._mk_turn(
			conv,
			rid,
			seed,
			"errored",
			version=6,
			pump_epoch=1,
			assistant_message=amsg,
			terminal_kind="relay:error",
			terminal_payload=json.dumps({"state": "error", "error": "boom"}),
			error="boom",
		)
		ts.insert_required_effects(rid, settlement.TERMINAL_EFFECTS)
		frappe.db.commit()
		self._pubs.clear()
		with self._mock_enrichment():
			finalize.run_finalize(rid, self._target)
		errs = [p for p in self._pubs if p.get("kind") == "run:error"]
		self.assertGreaterEqual(len(errs), 1, "errored terminal re-published as run:error")
		self.assertEqual(self._effects(rid)["terminal_publish"], "done")


# --------------------------------------------------------------------------- #
# 14. CDX-7 — prepare writes are actor-fenced (no orphan on a lost claim)
# --------------------------------------------------------------------------- #


class TestPrepareActorFencing(_PipelineCase):
	def test_stale_prepare_after_reclaim_leaves_no_orphan(self):
		# CDX-7: pause a prepare AFTER it creates its placeholder; a watchdog reclaim
		# bumps the version (preparing->recovering->queued); the stale prepare then
		# resumes, LOSES the actor-fenced attach, and cleans its orphan placeholder —
		# the turn is left cleanly queued with assistant_message NULL and no dangling
		# streaming row.
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="hi")
		rid = "pmp_prep_fence"
		self._mk_turn(conv, rid, seed, "queued", version=1, reserved=1)
		fake = _FakeSess()

		created = {}
		orig = prepare._create_placeholder_locked

		def _capture_then_reclaim(conversation):
			# Create the placeholder exactly as prepare would...
			name = orig(conversation)
			created["msg"] = name
			# ...then simulate a watchdog reclaim landing in the pause window: bump the
			# turn's version out from under this prepare (preparing->recovering->queued).
			row = ts.read_turn(rid)
			self.assertTrue(ts.mark_recovering(rid, int(row["version"])))
			frappe.db.commit()
			self.assertTrue(ts.recover_to_queued(rid, int(row["version"]) + 1))
			frappe.db.commit()
			return name

		with (
			self._gateway(fake),
			self._pump_on(),
			patch.object(prepare, "_create_placeholder_locked", _capture_then_reclaim),
		):
			# claim_preparing runs FIRST (version 1->2); then our patched placeholder
			# creator reclaims (2->recovering->queued, version 3+); the attach CAS at
			# version 2 then affects 0 rows and prepare cleans its orphan.
			res = prepare.run_prepare(rid, self._target)

		self.assertEqual(res.get("skipped"), "attach_lost", "the stale prepare lost the actor-fenced attach")
		# The orphan placeholder was cleaned (deleted).
		self.assertFalse(frappe.db.exists(MSG, created["msg"]), "orphan placeholder deleted")
		# The turn is cleanly re-queued with NO stale prepare refs.
		self.assertEqual(self._state(rid), "queued")
		self.assertIsNone(self._val(rid, "assistant_message"), "no orphan placeholder attached")
