"""WP-1e — flag-matrix integration tests (Relay Pump).

The chat dispatch path has THREE independent per-site controls:

  * ``jarvis_phase0_admission_enabled`` — Phase-0 durable admission (WP-0);
  * ``jarvis_pump_enabled`` — the Relay Pump, Jarvis's DEFAULT managed transport:
    UNSET / any truthy = ON (the pump owns new-turn dispatch), an EXPLICIT falsy
    value (0 / "0" / false) = OFF / INERT (the kill switch — byte-identical legacy),
    the sentinel ``'draining'`` = no NEW pump admissions while the pump drains its
    existing Turn rows.

This suite drives the SAME send scenario (one user message on a conversation,
routed exactly as the api callers route it — ``turn_machine_enabled()`` →
``accept_or_queue`` else legacy ``_dispatch_turn``) through each cell of the
matrix and asserts the cell-appropriate behaviour end to end:

  (a) ABSENT (default)      -> PUMP mode owns dispatch (the default-ON cell): a
                               managed bench with the flag unset routes through the
                               pump — proven against the REAL flag readers;
  (b) EXPLICIT pump=0       -> inert / byte-identical legacy (the kill-switch
                               differential): the REAL reader reports the pump off,
                               ensure_pump + the watchdog no-op, no pump-owned writes;
  (c) admission ON + pump=0 -> Phase-0 exactly (admit CAS + legacy worker dispatch);
  (d) pump ACTIVE           -> the full machine (queued -> ... -> done), with the
                               admission semantics enforced INSIDE the machine;
  (e) pump 'draining'       -> NEW turns route pure-legacy (no Turn row), the
                               draining pump's in-flight Turn rows still complete,
                               and Phase-0 promote/sweep step back (dual signal);
  (f) rollback ladder       -> draining drains in-flight to ``done`` (no strand); a
                               HARD flip to explicit pump=0 is INERT (why the ladder
                               is mandatory — the OARF-1 gate now keys on explicit-0).

Most cells patch the flag-FUNCTION seam (deterministic, no durable state); the
default (a) + explicit-off (b/c) cells instead set the REAL ``frappe.conf`` flag so
they exercise the actual absent-vs-explicit-0 reader (the kill switch). The
transport is WP-1b's in-process double + the ``_FakeSess`` pool double (the pump
machinery, every CAS, prepare/settlement/finalize are REAL); the legacy
``_dispatch_turn`` is a recorder.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from jarvis.chat import admission, finalize, prepare, pump
from jarvis.chat import turn_state as ts
from jarvis.tests.test_pump import _Recorder
from jarvis.tests.test_pump_pipeline import _FakeSess, _PipelineCase

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"


class _FlagMatrixCase(_PipelineCase):
	def setUp(self):
		super().setUp()
		self._ensure_rec = _Recorder()
		# The default (a) + explicit-off (b/c) cells drive the REAL flag readers (no
		# function patch) and assert ``admission.admission_enabled()`` as a precondition.
		# That reads ``jarvis_phase0_admission_enabled`` from conf: patterntest's
		# site_config ships it =1, but a fresh CI ``test_site`` does not, so the
		# precondition failed there. Pin it ON in-conf here (these cells are
		# single-threaded, so a ``frappe.local.conf`` patch is sufficient) instead of
		# depending on ambient site config. The function-patched cells override this.
		_flag = patch.dict(frappe.local.conf, {admission.FLAG: 1})
		_flag.start()
		self.addCleanup(_flag.stop)

	@contextmanager
	def _flags(self, *, pump_active=False, draining=False, admission_on=True, configured=None):
		"""Patch the flag functions so turn_machine_enabled / accept_or_queue /
		promote_next / sweep all compute the target matrix cell deterministically.
		``configured`` defaults to (pump_active or draining). Also pins the shard to
		this test's target and stubs the pump wake so accept never enqueues a hop."""
		if configured is None:
			configured = pump_active or draining
		# CDX-21 sweep note: accept_or_queue derives pump_mode + machine_active from ONE row read
		# via transport_predicates_from_row — pin it to this cell too (patterntest's real row is
		# 'pump', which would otherwise flip the non-pump cells into the pump branch).
		mode = "pump" if pump_active else "draining" if draining else "legacy"
		with (
			patch.object(admission, "admission_enabled", return_value=admission_on),
			patch.object(pump, "pump_mode_active", return_value=pump_active),
			patch.object(pump, "pump_draining", return_value=draining),
			patch.object(pump, "pump_configured", return_value=configured),
			patch.object(
				pump,
				"transport_predicates_from_row",
				return_value={"mode": mode, "pump_active": pump_active, "configured": configured},
			),
			patch.object(admission, "relay_target_id", lambda conversation=None: self._target),
			patch.object(pump, "ensure_pump", self._ensure_rec),
			patch.object(pump, "lpush_wake", lambda *a, **k: None),
		):
			yield

	@contextmanager
	def _explicit_pump_off(self):
		"""Set the REAL kill switch (``jarvis_pump_enabled=0``) + pin the shard target,
		WITHOUT patching the flag functions — so ``pump_mode_active`` /
		``pump_configured`` / ``pump_draining`` / ``ensure_pump`` / ``watchdog`` all
		compute the explicit-off (inert) branch through the actual conf reader. This is
		the differential that proves absent (the new default = ON) ≠ explicit-0 (the
		kill switch). ``ensure_pump``/``lpush_wake`` are NOT stubbed: on the explicit-0
		Phase-0 path ``accept_or_queue`` never calls them, and the tests assert the REAL
		``ensure_pump`` no-op directly.

		CDX-10: the kill switch is a DELIBERATE mode change, so it updates BOTH the config
		mirror (for the cheap readers — ensure_pump/watchdog/promote/sweep) AND the
		DB-AUTHORITATIVE ``transport_mode`` ROW to ``legacy`` (the fenced value
		accept_or_queue reads under the shard lock). The shard row was created 'pump' by
		setUp under patterntest's ambient absent (default-ON) conf; the kill switch flips it
		to 'legacy', matching what pump_set_transport_mode does in production."""
		frappe.db.set_value(
			"Jarvis Relay Pump", self._target, "transport_mode", pump._MODE_LEGACY, update_modified=False
		)
		frappe.db.commit()
		with (
			patch.dict(frappe.local.conf, {"jarvis_pump_enabled": 0}),
			patch.object(admission, "relay_target_id", lambda conversation=None: self._target),
		):
			yield

	def _send(self, conv, rid, *, seed=None, turn_class="interactive"):
		"""The SAME send scenario the api callers run: route through the durable
		machine when enabled, else the legacy dispatch. Returns what happened."""
		seed = seed or self._mk_msg(conv, content="hello")
		legacy = _Recorder()
		if admission.turn_machine_enabled():
			res = admission.accept_or_queue(
				conversation=conv,
				run_id=rid,
				seed_message=seed,
				turn_class=turn_class,
				dispatch=legacy,
			)
			return {"route": "machine", "res": res, "legacy": legacy, "seed": seed}
		# api caller's else-branch: _dispatch_turn(...) straight to the legacy worker.
		legacy()
		return {"route": "legacy", "res": None, "legacy": legacy, "seed": seed}

	def _drive_machine_to_done(self, conv, rid, *, transcript="success"):
		"""Promote (reserve credit) -> REAL prepare (session bootstrap over the pool
		double) -> pump dispatch/stream/terminal -> REAL settlement -> REAL finalize
		-> done. Returns the FakeSess used."""
		fake = _FakeSess()
		double = self._double()

		def real_prepare(run_id, target):
			with self._gateway(fake), self._flags(pump_active=True):
				prepare.run_prepare(run_id, target)

		deps = self._deps(double=double, prepare=real_prepare)
		ctx = self._make_ctx(deps)
		pump._promote_queued(ctx)
		self.assertEqual(self._state(rid), "ready", "promote+prepare drove queued->ready")
		double.arm(rid, transcript)
		self._pump_until(ctx, lambda: self._state(rid) in ("finalizing", "done"))
		self.assertEqual(self._state(rid), "finalizing", "settlement released the slot")
		with self._gateway(fake), self._mock_enrichment():
			finalize.run_finalize(rid, self._target)
		self.assertEqual(self._state(rid), "done", "finalize reached done")
		return fake


# --------------------------------------------------------------------------- #
# (a) ABSENT (default) — the pump is the DEFAULT transport, so unset ⇒ PUMP mode
# --------------------------------------------------------------------------- #


class TestDefaultAbsentIsPump(_FlagMatrixCase):
	def test_absent_flag_defaults_to_pump_mode(self):
		"""Default-ON inversion (new default cell): with ``jarvis_pump_enabled`` ABSENT
		(patterntest's real conf) on a managed bench, the Relay Pump is the DEFAULT
		transport. The REAL flag readers (no function patch) report the pump ACTIVE +
		configured (not draining), ``turn_machine_enabled`` picks the machine, and a new
		send is pump-owned — left ``queued`` (the pump promotes later), NO Phase-0 admit
		CAS, NO legacy dispatch."""
		self.assertIsNone(frappe.conf.get("jarvis_pump_enabled"), "precondition: flag absent")
		self.assertTrue(admission.admission_enabled(), "precondition: admission on (patterntest)")
		# Real readers: absent ⇒ ON on a managed bench (no function patch).
		self.assertTrue(pump.pump_mode_active(), "absent ⇒ pump active (default-ON)")
		self.assertTrue(pump.pump_configured(), "absent ⇒ pump configured (default-ON)")
		self.assertFalse(pump.pump_draining(), "absent is NOT draining")
		self.assertTrue(admission.turn_machine_enabled(), "absent ⇒ machine route (pump)")

		conv = self._mk_conv()
		rid = "fm_default"
		# Keep the REAL readers; only pin the shard + stub the pump wake so the send is
		# deterministic and enqueues no real hop.
		with (
			patch.object(admission, "relay_target_id", lambda conversation=None: self._target),
			patch.object(pump, "ensure_pump", self._ensure_rec),
			patch.object(pump, "lpush_wake", lambda *a, **k: None),
		):
			r = self._send(conv, rid)
		self.assertEqual(r["route"], "machine")
		self.assertTrue(r["res"].get("pump"), "absent flag ⇒ the pump branch owns dispatch")
		self.assertEqual(self._state(rid), "queued", "pump-accept leaves the turn queued")
		self.assertEqual(int(self._val(rid, "reserved")), 0, "no admit CAS on the pump path")
		self.assertEqual(r["legacy"].count, 0, "the pump path NEVER calls the legacy dispatch")
		self.assertGreaterEqual(self._ensure_rec.count, 1, "accept woke the pump (§8-E PRIMARY)")


# --------------------------------------------------------------------------- #
# fully-disabled config (pump OFF + admission OFF) — pure legacy, no Turn row
# --------------------------------------------------------------------------- #


class TestBothOff(_FlagMatrixCase):
	def test_both_off_pure_legacy_no_turn_row(self):
		conv = self._mk_conv()
		rid = "fm_a"
		with self._flags(pump_active=False, draining=False, admission_on=False, configured=False):
			self.assertFalse(admission.turn_machine_enabled())
			r = self._send(conv, rid)
		self.assertEqual(r["route"], "legacy", "both OFF routes to the legacy _dispatch_turn")
		self.assertEqual(r["legacy"].count, 1, "legacy dispatch fired exactly once")
		self.assertFalse(frappe.db.exists(TURN, rid), "no durable Turn row created (byte-identical)")


# --------------------------------------------------------------------------- #
# (b/c) EXPLICIT pump=0 + admission ON — the kill switch is inert = Phase 0 exactly
# --------------------------------------------------------------------------- #


class TestAdmissionOnly(_FlagMatrixCase):
	def test_admission_only_is_phase0(self):
		conv = self._mk_conv()
		rid = "fm_b"
		with self._flags(pump_active=False, draining=False, admission_on=True, configured=False):
			self.assertTrue(admission.turn_machine_enabled())
			r = self._send(conv, rid)
		self.assertEqual(r["route"], "machine")
		self.assertTrue(r["res"]["dispatched"], "first turn admits with a free credit (Phase 0)")
		self.assertFalse(r["res"].get("pump"), "NOT the pump branch")
		self.assertEqual(self._state(rid), "dispatching", "Phase-0 admit CAS queued->dispatching")
		self.assertEqual(r["legacy"].count, 1, "Phase-0 dispatches via the LEGACY worker after commit")
		self.assertEqual(int(self._val(rid, "reserved")), 1, "the dispatching row IS the reservation")

	def test_explicit_zero_kill_switch_inert_over_phase0_rows(self):
		"""OARF-1 regression guard, re-based on the default-ON inversion (cells b/c):
		an EXPLICIT ``jarvis_pump_enabled=0`` is the kill switch. Driven through the
		REAL conf reader (NOT the function seam), the pump is inert on every predicate,
		so an admission-on site is EXACTLY Phase-0 (admit CAS + legacy worker) and the
		pump machinery writes nothing — ``ensure_pump`` no-ops (``not_configured``) and
		the watchdog starts NO hop even over a live Phase-0 Turn row (dual-engine
		ownership would defeat the kill switch + rollback ladder). ABSENCE is now the
		DEFAULT (pump ON), so the off case MUST set the flag explicitly — the OARF-1
		gate keys on explicit-0, not on absence. The symmetric positive case (pump
		configured) DOES revive the shard, proving the gate is the only thing holding
		it back."""
		conv = self._mk_conv()
		rid = "fm_b_wd"
		with self._explicit_pump_off():
			# Real readers: explicit-0 ⇒ inert on every predicate.
			self.assertFalse(pump.pump_mode_active(), "explicit-0 ⇒ pump inactive")
			self.assertFalse(pump.pump_configured(), "explicit-0 ⇒ pump NOT configured")
			self.assertFalse(pump.pump_draining(), "explicit-0 is NOT draining")
			# admission ON + pump explicit-0 ⇒ Phase-0 route (cell c).
			self.assertTrue(admission.turn_machine_enabled(), "admission on + pump off ⇒ Phase-0")
			r = self._send(conv, rid)
			self.assertEqual(r["route"], "machine")
			self.assertFalse(r["res"].get("pump"), "explicit-0: NOT the pump branch")
			self.assertEqual(self._state(rid), "dispatching", "Phase-0 admit CAS queued->dispatching")
			self.assertEqual(r["legacy"].count, 1, "Phase-0 dispatches via the LEGACY worker")
			self.assertEqual(int(self._val(rid, "reserved")), 1, "the dispatching row IS the reservation")

			# ensure_pump no-ops on the real explicit-0 reader (the kill switch).
			self.assertEqual(
				pump.ensure_pump(self._target),
				{"enqueued": False, "reason": "not_configured"},
				"explicit-0 ensure_pump is a total no-op",
			)
			# The watchdog starts NO hop even with the live Phase-0 dispatching row.
			wd_off = pump.PumpDeps()
			wd_off.enqueue_pump_job = _Recorder()
			summary_off = pump.watchdog(deps=wd_off)
			self.assertEqual(wd_off.enqueue_pump_job.count, 0, "explicit-0 watchdog enqueues NO hop")
			self.assertEqual(summary_off["revived"], 0)
			self.assertEqual(self._state(rid), "dispatching", "the Phase-0 row is untouched")

		# Symmetric positive: flip the ROW back to configured (pump) -> the watchdog DOES revive
		# live work, proving the row is the ONLY thing holding it back (CDX-21: the gate reads the
		# row, not the config). (No pump ever ran this test, so the shard control row's lease is
		# vacant — the MariaDB revive path.)
		frappe.db.set_value(
			"Jarvis Relay Pump", self._target, "transport_mode", pump._MODE_PUMP, update_modified=False
		)
		frappe.db.commit()
		pump._LIFECYCLE_MODE_CACHE.pop(self._target, None)  # bypass the 5s gate cache
		wd_on = pump.PumpDeps()
		wd_on.enqueue_pump_job = _Recorder()
		with patch.object(admission, "relay_target_id", lambda conversation=None: self._target):
			summary_on = pump.watchdog(deps=wd_on)
		self.assertGreaterEqual(
			wd_on.enqueue_pump_job.count, 1, "configured (row) watchdog revives the shard"
		)
		self.assertGreaterEqual(summary_on["revived"], 1)


# --------------------------------------------------------------------------- #
# (c) pump ON — the full machine + admission semantics inside it
# --------------------------------------------------------------------------- #


class TestPumpFullMachine(_FlagMatrixCase):
	def test_pump_on_full_machine_queued_to_done(self):
		conv = self._mk_conv()
		rid = "fm_c1"
		with self._flags(pump_active=True):
			r = self._send(conv, rid)
		self.assertEqual(r["route"], "machine")
		self.assertTrue(r["res"].get("pump"), "pump branch owns dispatch")
		# The turn is durably QUEUED regardless of the `dispatched` UI hint — the pump
		# (not accept) is authoritative; accept never runs a Phase-0 admit CAS here.
		self.assertEqual(self._state(rid), "queued", "pump-accept leaves the turn queued (pump promotes)")
		self.assertEqual(int(self._val(rid, "reserved")), 0, "no admit CAS on the pump path")
		self.assertEqual(r["legacy"].count, 0, "the pump path NEVER calls the legacy dispatch")
		self.assertGreaterEqual(self._ensure_rec.count, 1, "accept woke the pump (§8-E PRIMARY)")

		# Full lifecycle through the durable machine.
		self._drive_machine_to_done(conv, rid)
		# SUX-1 frontend-compat publishes fired with the shapes ChatView consumes.
		kinds = self._pub_kinds()
		self.assertIn("run:start", kinds)
		self.assertIn("assistant:delta", kinds)  # NOT run:delta — ChatView renders assistant:delta
		self.assertIn("run:end", kinds)
		self.assertIn("message:enriched", kinds)
		delta = next(p for p in self._pubs if p.get("kind") == "assistant:delta")
		self.assertTrue(delta.get("message_id"), "assistant:delta carries message_id (ChatView needs it)")
		self.assertIn("event_seq", delta, "assistant:delta carries event_seq (client dedupe guard)")

	def test_pump_admission_single_flight_inside_the_machine(self):
		"""Admission semantics hold INSIDE the machine: a second send on the SAME
		conversation while the first is in-flight stays queued (single-flight); it
		promotes only once the conversation frees."""
		conv = self._mk_conv()
		with self._flags(pump_active=True):
			self._send(conv, "fm_c2a")
			r2 = self._send(conv, "fm_c2b")
		self.assertEqual(self._state("fm_c2a"), "queued")
		self.assertEqual(self._state("fm_c2b"), "queued")
		self.assertIsNotNone(r2["res"]["queued_position"], "the second same-conv send reports a position")

		# Promote: single-flight admits only ONE turn of the conversation. The prepare
		# seam is a recorder (turns stay `queued`), so the single-flight proof is that
		# exactly ONE dispatch_prepare fired AND exactly ONE turn holds a reservation.
		prep_rec = _Recorder()
		deps = self._deps(prepare=prep_rec)
		ctx = self._make_ctx(deps, with_mux=False)
		pump._promote_queued(ctx)
		self.assertEqual(prep_rec.count, 1, "single-flight: only one turn of the conversation promoted")
		reserved = [r for r in ("fm_c2a", "fm_c2b") if int(self._val(r, "reserved"))]
		self.assertEqual(len(reserved), 1, "exactly one of the two same-conv turns reserved a credit")


# --------------------------------------------------------------------------- #
# (d) pump 'draining' — new turns legacy, in-flight pump turns complete
# --------------------------------------------------------------------------- #


class TestDraining(_FlagMatrixCase):
	def test_draining_new_legacy_inflight_completes_phase0_steps_back(self):
		conv = self._mk_conv()
		# An in-flight PUMP turn (terminal_observed = settlement-owed) that is draining.
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
		rid_inflight = "fm_d_inflight"
		deps = self._deps(snapshot=lambda ctx: {"gateway_active": 0, "active_session_keys": None})
		ctx = self._make_ctx(deps, with_mux=False)
		self._mk_turn(
			conv,
			rid_inflight,
			seed,
			"terminal_observed",
			version=6,
			pump_epoch=ctx.epoch,
			reserved=1,
			assistant_message=amsg,
			terminal_kind="relay:final",
			terminal_payload=json.dumps({"text": "final answer"}),
			terminal_observed_at=frappe.utils.now(),
			dispatching_at=frappe.utils.now(),
		)

		with self._flags(pump_active=False, draining=True, admission_on=True, configured=True):
			# 1. NEW turns fall through to PURE LEGACY (no Turn row).
			self.assertFalse(admission.turn_machine_enabled(), "draining routes new turns legacy")
			conv2 = self._mk_conv()
			r = self._send(conv2, "fm_d_new")
			self.assertEqual(r["route"], "legacy")
			self.assertFalse(frappe.db.exists(TURN, "fm_d_new"), "draining-window new turn has NO Turn row")

			# 2. Dual signal (OAR-11) covers the draining pump's in-flight turn.
			self.assertGreaterEqual(
				admission._shard_inflight(self._target), 1, "pump in-flight counts as shard inflight"
			)

			# 3. Phase-0 promote/sweep STEP BACK (pump owns the rows).
			with patch("jarvis.chat.admission._dispatch_promoted") as disp:
				self.assertEqual(admission.promote_next(self._target), 0)
				disp.assert_not_called()
			self.assertEqual(
				admission.sweep(), {"reclaimed": 0, "reconciled": 0, "aged_out": 0, "promoted": 0}
			)

		# 4. The draining pump's in-flight turn still COMPLETES (reconcile settles it
		#    from the row, then finalize) — the pump machinery is flag-agnostic.
		pump._reconcile_on_start(ctx)
		self.assertEqual(self._state(rid_inflight), "finalizing", "draining in-flight turn settled")
		with self._mock_enrichment():
			finalize.run_finalize(rid_inflight, self._target)
		self.assertEqual(self._state(rid_inflight), "done", "draining in-flight turn drained to done")


# --------------------------------------------------------------------------- #
# (e) pump ON -> OFF flip mid-turn — no strands (watchdog completes it)
# --------------------------------------------------------------------------- #


class TestFlipOffMidTurn(_FlagMatrixCase):
	def test_rollback_ladder_draining_drains_inflight_no_strands(self):
		"""Rollback ladder (enabled -> DRAINING -> disabled): flipping to `draining`
		routes NEW turns pure-legacy while the pump keeps draining its in-flight
		rows to terminal. OARF-1: the no-strand guarantee now rides `draining`
		(pump_configured() TRUE), NOT a hard flip to fully-off — with the flag fully
		OFF the watchdog is inert (proven below), which is exactly why the runbook
		mandates the ladder."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
		rid = "fm_e"
		deps = self._deps(snapshot=lambda ctx: {"gateway_active": 0, "active_session_keys": None})
		ctx = self._make_ctx(deps, with_mux=False)
		# A pump turn caught in-flight (terminal observed, settlement owed) exactly at
		# the moment the operator begins the rollback.
		self._mk_turn(
			conv,
			rid,
			seed,
			"terminal_observed",
			version=6,
			pump_epoch=ctx.epoch,
			reserved=1,
			assistant_message=amsg,
			terminal_kind="relay:final",
			terminal_payload=json.dumps({"text": "final answer"}),
			terminal_observed_at=frappe.utils.now(),
			dispatching_at=frappe.utils.now(),
		)

		# Expire the lease + clear the mirror so ensure_pump takes the MariaDB revive
		# path (a dead pump left the lease vacant).
		frappe.db.set_value(
			"Jarvis Relay Pump",
			self._target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.commit()
		pump._clear_lease_mirror(self._target)

		# STEP 1 of the ladder — `draining` (pump_configured TRUE): new turns go
		# pure-legacy and Phase-0 promote steps back (pump owns the row).
		with self._flags(pump_active=False, draining=True, admission_on=True, configured=True):
			self.assertFalse(admission.turn_machine_enabled(), "draining routes new turns legacy")
			self.assertEqual(admission.promote_next(self._target), 0)

		# The draining watchdog (pump_configured TRUE) still revives the shard so the
		# in-flight turn drains. Real ensure_pump here (only pump_configured patched)
		# so the enqueue lands in wd_deps' recorder.
		wd_deps = pump.PumpDeps()
		wd_deps.enqueue_pump_job = _Recorder()
		with patch.object(pump, "pump_configured", return_value=True):
			summary = pump.watchdog(deps=wd_deps)
		self.assertGreaterEqual(
			wd_deps.enqueue_pump_job.count, 1, "draining watchdog revives the in-flight shard"
		)
		self.assertGreaterEqual(summary["revived"], 1)

		# The revived hop's reconcile settles the owed terminal from the row, then
		# finalize drains it to done — no strand.
		pump._reconcile_on_start(ctx)
		self.assertEqual(self._state(rid), "finalizing", "reconcile settled the owed terminal")
		with self._mock_enrichment():
			finalize.run_finalize(rid, self._target)
		self.assertEqual(self._state(rid), "done", "drained in-flight turn reached done — no strand")

	def test_hard_flip_fully_off_watchdog_is_inert(self):
		"""OARF-1 / CDX-21: a HARD flip to the ``legacy`` kill switch makes the watchdog INERT —
		it does NOT revive even an in-flight pump turn. Post default-ON inversion, "fully off" is
		the EXPLICIT kill switch, and post CDX-21 the watchdog gate reads the AUTHORITATIVE ROW
		(``transport_mode='legacy'``), not the config mirror — so this test flips the ROW to legacy
		(what ``pump_set_transport_mode('legacy')`` commits) rather than only the config. This is
		why the rollback ladder (enabled -> draining -> disabled) is mandatory: draining is what
		keeps the no-strand guarantee, not a hard flip to fully-off."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
		rid = "fm_e_hard"
		deps = self._deps(snapshot=lambda ctx: {"gateway_active": 0, "active_session_keys": None})
		ctx = self._make_ctx(deps, with_mux=False)
		self._mk_turn(
			conv,
			rid,
			seed,
			"terminal_observed",
			version=6,
			pump_epoch=ctx.epoch,
			reserved=1,
			assistant_message=amsg,
			terminal_kind="relay:final",
			terminal_payload=json.dumps({"text": "final answer"}),
			terminal_observed_at=frappe.utils.now(),
			dispatching_at=frappe.utils.now(),
		)
		frappe.db.set_value(
			"Jarvis Relay Pump",
			self._target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.commit()
		pump._clear_lease_mirror(self._target)

		# Fully OFF via the EXPLICIT kill switch — CDX-21: flip the ROW to legacy (what the
		# operator command commits). The per-shard watchdog gate reads that row and skips the
		# shard; NO revive. Config is mirrored 0 too for completeness.
		frappe.db.set_value(
			"Jarvis Relay Pump", self._target, "transport_mode", pump._MODE_LEGACY, update_modified=False
		)
		frappe.db.commit()
		pump._LIFECYCLE_MODE_CACHE.pop(self._target, None)
		wd_deps = pump.PumpDeps()
		wd_deps.enqueue_pump_job = _Recorder()
		with (
			patch.dict(frappe.local.conf, {"jarvis_pump_enabled": 0}),
			patch.object(admission, "relay_target_id", lambda conversation=None: self._target),
		):
			self.assertFalse(pump.pump_lifecycle_configured(self._target), "row=legacy ⇒ NOT configured")
			summary = pump.watchdog(deps=wd_deps)
		self.assertEqual(wd_deps.enqueue_pump_job.count, 0, "legacy-row watchdog starts NO hop")
		self.assertEqual(summary["revived"], 0)
		self.assertEqual(self._state(rid), "terminal_observed", "the turn is left as-is (use the ladder)")
