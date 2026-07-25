"""Tests for jarvis.chat.turn_state - the WP-1a durable turn-state machine.

Covers the WP-1a acceptance set:

* Every amended-D2 transition: the legal actor wins ONCE, a replay affects 0
  rows, and every illegal actor/state (and, for pump rows, wrong-epoch /
  wrong-version) combination is rejected.
* The D4 fencing timelines as DB-level tests:
    (a) lease expiry WITHOUT takeover still commits (epoch is not a clock);
    (c) a delayed old writer with a stale epoch affects 0 rows after a takeover
        re-stamp;
    (d) dual acquisition - exactly one pump wins (threaded, real DB locks).
* GLM's two-writer finalize shape: two actors race the SAME settlement CAS -
  exactly one wins, both sequential and threaded orders; the loser's S1 message
  write rolls back with it (S1+S2 atomicity).
* The recovering split by dispatching_at: NULL -> queued (fresh prepare, credit
  released, prepare refs dropped); NOT NULL -> adopt (epoch re-stamp).
* Effect-ledger idempotency + force-done at FINALIZE_MAX_ATTEMPTS=3 + the
  finalize_done all-effects-done guard.
* The canonical lock-order dev assertion (OAR-6).
* R-14 isolation-level assertion: the settlement CAS and dual-acquisition run
  under BOTH READ COMMITTED and REPEATABLE READ (set per-connection).

Each test uses a UNIQUE per-test relay_target_id so it never touches the
site-wide "default" admission shard the WP-0 suite counts. Tests clean up their
own Turn / Turn Effect / Relay Pump / Conversation rows.
"""

import threading
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import turn_state as ts

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
PUMP = "Jarvis Relay Pump"
EFFECT = "Jarvis Turn Effect"
TEST_USER = "jarvis-turnstate-test@example.com"
TARGET_PREFIX = "tsx_"


def _ensure_test_user(user: str = TEST_USER) -> None:
	if frappe.db.exists("User", user):
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": user,
			"first_name": "TurnState",
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
	# Drop this suite's per-test control rows (never the site-wide "default").
	frappe.db.delete(PUMP, {"relay_target_id": ["like", f"{TARGET_PREFIX}%"]})
	frappe.db.commit()


class _TurnStateTestCase(FrappeTestCase):
	def setUp(self):
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup()
		self._target = f"{TARGET_PREFIX}{frappe.generate_hash(length=10)}"
		ts._ensure_control_row(self._target)
		ts.reset_lock_tracking()

	def tearDown(self):
		ts.reset_lock_tracking()
		_cleanup()
		frappe.set_user(self._orig_user)

	# ---- helpers ---------------------------------------------------------- #

	def _mk_conv(self) -> str:
		doc = frappe.get_doc({"doctype": CONV, "title": "New chat", "status": "Active"})
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
		return doc.name

	def _mk_msg(self, conv: str, seq: int, role: str = "user", content: str = "hi", **extra) -> str:
		doc = frappe.get_doc(
			{"doctype": MSG, "conversation": conv, "seq": seq, "role": role, "content": content, **extra}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
		return doc.name

	def _mk_turn(
		self,
		conv: str,
		run_id: str,
		seed: str,
		state: str,
		*,
		version: int = 1,
		pump_epoch: int = 0,
		reserved: int = 0,
		**extra,
	) -> None:
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

	def _v(self, run_id: str) -> int:
		return int(frappe.db.get_value(TURN, run_id, "version"))

	def _state(self, run_id: str) -> str:
		return frappe.db.get_value(TURN, run_id, "state")


# --------------------------------------------------------------------------- #
# The happy-path linear machine: legal actor wins once, replay affects 0.
# --------------------------------------------------------------------------- #


class TestLinearPathWinAndReplay(_TurnStateTestCase):
	def test_full_success_path(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		amsg = self._mk_msg(conv, 2, role="assistant", content="", streaming=1)
		rid = "ts_happy"
		# Seed at queued with a HELD credit (reserve-on-send winner), version 0.
		self._mk_turn(conv, rid, seed, "queued", version=0, reserved=1, assistant_message=amsg)
		E = 7

		# queued -> preparing (prep, actor). Claim NULLs the expiry.
		self.assertTrue(ts.claim_preparing(rid, 0, assistant_message=amsg))
		frappe.db.commit()
		self.assertEqual(self._state(rid), "preparing")
		self.assertEqual(self._v(rid), 1)
		self.assertIsNone(frappe.db.get_value(TURN, rid, "reservation_expires_at"))
		self.assertFalse(ts.claim_preparing(rid, 0), "replay at stale version affects 0")
		frappe.db.rollback()

		# preparing -> ready
		self.assertTrue(ts.mark_ready(rid, 1))
		frappe.db.commit()
		self.assertEqual(self._state(rid), "ready")
		self.assertFalse(ts.mark_ready(rid, 1))
		frappe.db.rollback()

		# ready -> dispatching (pump; E FIRST stamped on the turn, but the CAS now proves
		# the SHARD control row still holds epoch E, CDX-2). Put the control row at E so
		# the EXISTS clause passes for this legal owner.
		frappe.db.set_value(PUMP, self._target, "pump_epoch", E, update_modified=False)
		frappe.db.commit()
		self.assertTrue(ts.confirm_dispatching(rid, 2, E, self._target))
		frappe.db.commit()
		row = frappe.db.get_value(TURN, rid, ["state", "pump_epoch", "deadline_at"], as_dict=True)
		self.assertEqual(row["state"], "dispatching")
		self.assertEqual(row["pump_epoch"], E)
		self.assertIsNotNone(row["deadline_at"])
		self.assertFalse(ts.confirm_dispatching(rid, 2, E, self._target))
		frappe.db.rollback()

		# dispatching -> streaming (pump, epoch-fenced)
		self.assertTrue(ts.mark_streaming(rid, 3, E, gateway_run_id="gw-123"))
		frappe.db.commit()
		self.assertEqual(self._state(rid), "streaming")
		self.assertEqual(frappe.db.get_value(TURN, rid, "gateway_run_id"), "gw-123")
		self.assertFalse(ts.mark_streaming(rid, 3, E))
		frappe.db.rollback()

		# streaming -> streaming (delta, epoch-fenced, watermark guard)
		self.assertTrue(ts.apply_delta(rid, 4, E, 1, amsg, "hello"))
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(TURN, rid, "last_event_seq"), 1)
		self.assertEqual(frappe.db.get_value(MSG, amsg, "content"), "hello")
		# a duplicate/re-attached frame at the same seq affects 0 (watermark).
		self.assertFalse(ts.apply_delta(rid, 5, E, 1, amsg, "hello"), "duplicate seq is idempotent")
		frappe.db.rollback()
		# next cumulative frame advances.
		self.assertTrue(ts.apply_delta(rid, 5, E, 2, amsg, "hello world"))
		frappe.db.commit()

		# streaming -> terminal_observed (pump, epoch-fenced)
		self.assertTrue(ts.mark_terminal_observed(rid, 6, E, "relay:final", {"text": "hello world"}))
		frappe.db.commit()
		self.assertEqual(self._state(rid), "terminal_observed")
		self.assertEqual(frappe.db.get_value(TURN, rid, "terminal_kind"), "relay:final")
		self.assertFalse(ts.mark_terminal_observed(rid, 6, E, "relay:final"))
		frappe.db.rollback()

		# terminal_observed -> finalizing (settle, epoch-fenced) + effect rows
		self.assertTrue(
			ts.settle_finalizing(
				rid,
				7,
				E,
				assistant_message=amsg,
				final_text="hello world",
				required_effects=("terminal_publish", "auto_title"),
			)
		)
		frappe.db.commit()
		row = frappe.db.get_value(TURN, rid, ["state", "reserved", "finalizing_at"], as_dict=True)
		self.assertEqual(row["state"], "finalizing")
		self.assertEqual(row["reserved"], 0, "slot released at settlement")
		self.assertIsNotNone(row["finalizing_at"])
		self.assertEqual(frappe.db.get_value(MSG, amsg, "streaming"), 0, "final projection not streaming")
		self.assertEqual(frappe.db.count(EFFECT, {"turn": rid}), 2, "required effects inserted atomically")
		self.assertFalse(ts.settle_finalizing(rid, 7, E), "replay settlement affects 0")
		frappe.db.rollback()

		# finalizing -> done is BLOCKED until every effect is done.
		self.assertFalse(ts.finalize_done(rid, 8), "finalize blocked while effects pending")
		frappe.db.rollback()
		# CDX-4: claim (pending->running) then complete (running->done) with the token.
		o1, t1 = ts.claim_effect(rid, "terminal_publish")
		self.assertEqual(o1, "attempt")
		self.assertTrue(ts.complete_effect(rid, "terminal_publish", t1))
		o2, t2 = ts.claim_effect(rid, "auto_title")
		self.assertEqual(o2, "attempt")
		self.assertTrue(ts.complete_effect(rid, "auto_title", t2))
		frappe.db.commit()
		self.assertTrue(ts.finalize_done(rid, 8))
		frappe.db.commit()
		self.assertEqual(self._state(rid), "done")
		self.assertIsNotNone(frappe.db.get_value(TURN, rid, "done_at"))
		self.assertFalse(ts.finalize_done(rid, 8))
		frappe.db.rollback()


# --------------------------------------------------------------------------- #
# Error, cancel, abort branches.
# --------------------------------------------------------------------------- #


class TestErrorAndCancelBranches(_TurnStateTestCase):
	def test_prepare_errored_releases_credit(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		self._mk_turn(conv, "ts_pe", seed, "preparing", version=3, reserved=1)
		self.assertTrue(ts.prepare_errored("ts_pe", 3, "boom"))
		frappe.db.commit()
		row = frappe.db.get_value(TURN, "ts_pe", ["state", "reserved", "error"], as_dict=True)
		self.assertEqual(row["state"], "errored")
		self.assertEqual(row["reserved"], 0, "reservation released, credit not leaked")
		self.assertEqual(row["error"], "boom")
		self.assertFalse(ts.prepare_errored("ts_pe", 3))
		frappe.db.rollback()

	def test_dispatch_errored_epoch_fenced(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		self._mk_turn(
			conv,
			"ts_de",
			seed,
			"dispatching",
			version=4,
			pump_epoch=9,
			reserved=1,
			dispatching_at=frappe.utils.now(),
		)
		# wrong epoch loses.
		self.assertFalse(ts.dispatch_errored("ts_de", 4, 8, "rej"), "stale epoch affects 0")
		frappe.db.rollback()
		self.assertTrue(ts.dispatch_errored("ts_de", 4, 9, "definite rejection"))
		frappe.db.commit()
		row = frappe.db.get_value(TURN, "ts_de", ["state", "reserved", "done_at"], as_dict=True)
		self.assertEqual(row["state"], "errored")
		self.assertEqual(row["reserved"], 0)
		self.assertIsNotNone(row["done_at"])

	def test_settle_errored_only_on_relay_error(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		amsg = self._mk_msg(conv, 2, role="assistant", content="partial", streaming=1)
		# A SUCCESS terminal must NOT be convertible to errored.
		self._mk_turn(
			conv,
			"ts_seok",
			seed,
			"terminal_observed",
			version=6,
			pump_epoch=2,
			reserved=1,
			terminal_kind="relay:final",
			assistant_message=amsg,
		)
		self.assertFalse(ts.settle_errored("ts_seok", 6, 2, "no"), "cannot error a success")
		frappe.db.rollback()
		# An error terminal settles to errored.
		self._mk_turn(
			conv,
			"ts_seerr",
			self._mk_msg(conv, 3),
			"terminal_observed",
			version=6,
			pump_epoch=2,
			reserved=1,
			terminal_kind="relay:error",
		)
		self.assertTrue(ts.settle_errored("ts_seerr", 6, 2, "model failed"))
		frappe.db.commit()
		self.assertEqual(self._state("ts_seerr"), "errored")
		self.assertEqual(frappe.db.get_value(TURN, "ts_seerr", "reserved"), 0)

	def test_cancel_queued_by_web(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		self._mk_turn(conv, "ts_cq", seed, "queued", version=0, reserved=1)
		self.assertTrue(ts.cancel_queued("ts_cq", 0))
		frappe.db.commit()
		row = frappe.db.get_value(TURN, "ts_cq", ["state", "reserved"], as_dict=True)
		self.assertEqual(row["state"], "cancelled")
		self.assertEqual(row["reserved"], 0)
		self.assertFalse(ts.cancel_queued("ts_cq", 0))
		frappe.db.rollback()

	def test_cancel_queued_max_age_guard(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		# Fresh queued row: the age guard rejects it.
		self._mk_turn(conv, "ts_age", seed, "queued", version=0)
		self.assertFalse(ts.cancel_queued_max_age("ts_age", 0, "waited too long"), "fresh row not aged out")
		frappe.db.rollback()
		# Age it past QUEUED_MAX_AGE_S.
		old = frappe.utils.add_to_date(None, seconds=-(ts.QUEUED_MAX_AGE_S + 60))
		frappe.db.set_value(TURN, "ts_age", "enqueued_at", old, update_modified=False)
		frappe.db.commit()
		self.assertTrue(ts.cancel_queued_max_age("ts_age", 0, "Waited too long in the queue."))
		frappe.db.commit()
		row = frappe.db.get_value(TURN, "ts_age", ["state", "cancel_reason", "error"], as_dict=True)
		self.assertEqual(row["state"], "cancelled")
		self.assertEqual(row["cancel_reason"], "Waited too long in the queue.")
		self.assertEqual(row["error"], "Waited too long in the queue.")

	def test_cancel_preparing_or_ready(self):
		conv = self._mk_conv()
		for i, st in enumerate(("preparing", "ready")):
			rid = f"ts_cpr_{st}"
			self._mk_turn(conv, rid, self._mk_msg(conv, 10 + i), st, version=2, reserved=1)
			self.assertTrue(ts.cancel_preparing_or_ready(rid, 2))
			frappe.db.commit()
			self.assertEqual(self._state(rid), "cancelled")
			self.assertEqual(frappe.db.get_value(TURN, rid, "reserved"), 0)

	def test_cancel_abort_pair(self):
		"""dispatching/streaming -> cancel intent -> aborted terminal -> cancelled."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		E = 4
		self._mk_turn(
			conv,
			"ts_ab",
			seed,
			"streaming",
			version=5,
			pump_epoch=E,
			reserved=1,
			dispatching_at=frappe.utils.now(),
		)
		# web: request cancel (NOT terminal).
		self.assertTrue(ts.request_cancel("ts_ab", 5))
		frappe.db.commit()
		self.assertEqual(self._state("ts_ab"), "streaming")
		self.assertEqual(frappe.db.get_value(TURN, "ts_ab", "cancel_requested"), 1)
		# pump: record the aborted terminal (epoch-fenced; needs cancel_requested).
		self.assertTrue(ts.record_aborted_terminal("ts_ab", "streaming", 6, E))
		frappe.db.commit()
		self.assertEqual(self._state("ts_ab"), "terminal_observed")
		self.assertEqual(frappe.db.get_value(TURN, "ts_ab", "terminal_kind"), "relay:error")
		# settle: aborted terminal_observed -> cancelled.
		self.assertTrue(ts.settle_cancelled("ts_ab", 7, E))
		frappe.db.commit()
		row = frappe.db.get_value(TURN, "ts_ab", ["state", "reserved"], as_dict=True)
		self.assertEqual(row["state"], "cancelled")
		self.assertEqual(row["reserved"], 0)

	def test_record_aborted_terminal_requires_cancel_requested(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		E = 4
		# cancel NOT requested -> the abort record affects 0.
		self._mk_turn(
			conv, "ts_nab", seed, "streaming", version=5, pump_epoch=E, dispatching_at=frappe.utils.now()
		)
		self.assertFalse(ts.record_aborted_terminal("ts_nab", "streaming", 5, E))
		frappe.db.rollback()

	def test_request_cancel_illegal_from_state(self):
		with self.assertRaises(ValueError):
			ts.record_aborted_terminal("x", "queued", 1, 1)


# --------------------------------------------------------------------------- #
# Illegal actor/state (+ wrong epoch/version) rejection matrix.
# --------------------------------------------------------------------------- #


class TestIllegalCombinationsRejected(_TurnStateTestCase):
	def test_actor_transitions_reject_wrong_state_and_version(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		# A turn parked at 'streaming'; every non-matching actor CAS must be 0.
		self._mk_turn(
			conv, "ts_bad", seed, "streaming", version=5, pump_epoch=3, dispatching_at=frappe.utils.now()
		)
		cases = [
			("claim_preparing", lambda: ts.claim_preparing("ts_bad", 5)),
			("mark_ready", lambda: ts.mark_ready("ts_bad", 5)),
			("prepare_errored", lambda: ts.prepare_errored("ts_bad", 5)),
			(
				"confirm_dispatching(wrong state)",
				lambda: ts.confirm_dispatching("ts_bad", 5, 3, self._target),
			),
			("finalize_done(wrong state)", lambda: ts.finalize_done("ts_bad", 5)),
			("cancel_queued(wrong state)", lambda: ts.cancel_queued("ts_bad", 5)),
			("cancel_preparing_or_ready(wrong state)", lambda: ts.cancel_preparing_or_ready("ts_bad", 5)),
			("recover_to_queued(wrong state)", lambda: ts.recover_to_queued("ts_bad", 5)),
			("recover_errored(wrong state)", lambda: ts.recover_errored("ts_bad", 5)),
		]
		for label, fn in cases:
			self.assertFalse(fn(), f"{label} on a streaming turn must affect 0 rows")
			frappe.db.rollback()

	def test_wrong_version_rejected(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		self._mk_turn(conv, "ts_wv", seed, "queued", version=2, reserved=1)
		self.assertFalse(ts.claim_preparing("ts_wv", 1), "stale version affects 0")
		frappe.db.rollback()
		self.assertTrue(ts.claim_preparing("ts_wv", 2))
		frappe.db.rollback()

	def test_epoch_fenced_transitions_reject_wrong_epoch(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		amsg = self._mk_msg(conv, 2, role="assistant", content="", streaming=1)
		E = 5
		# streaming turn owned by epoch 5; a stale pump at epoch 4 loses every write.
		self._mk_turn(
			conv,
			"ts_ep",
			seed,
			"streaming",
			version=8,
			pump_epoch=E,
			reserved=1,
			dispatching_at=frappe.utils.now(),
			assistant_message=amsg,
		)
		self.assertFalse(ts.apply_delta("ts_ep", 8, 4, 1, amsg, "x"), "delta at stale epoch")
		frappe.db.rollback()
		self.assertFalse(ts.mark_terminal_observed("ts_ep", 8, 4, "relay:final"), "terminal at stale epoch")
		frappe.db.rollback()
		# terminal_observed variant for the settle CAS.
		self._mk_turn(
			conv, "ts_ep2", self._mk_msg(conv, 3), "terminal_observed", version=8, pump_epoch=E, reserved=1
		)
		self.assertFalse(ts.settle_finalizing("ts_ep2", 8, 4), "settle at stale epoch")
		frappe.db.rollback()
		# The right epoch wins (proves the guard is epoch, not just state).
		self.assertTrue(ts.mark_terminal_observed("ts_ep", 8, E, "relay:final"))
		frappe.db.rollback()

	def test_finalizing_never_reenters_recovering(self):
		"""R-13: mark_recovering excludes finalizing (a settled turn cannot be
		un-settled)."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		self._mk_turn(conv, "ts_fin", seed, "finalizing", version=7)
		self.assertFalse(ts.mark_recovering("ts_fin", 7), "finalizing is not a recoverable state")
		frappe.db.rollback()
		# ...and terminal states are equally excluded.
		self._mk_turn(conv, "ts_done", self._mk_msg(conv, 2), "done", version=9)
		self.assertFalse(ts.mark_recovering("ts_done", 9))
		frappe.db.rollback()


# --------------------------------------------------------------------------- #
# D4 fencing timelines as DB-level tests.
# --------------------------------------------------------------------------- #


class TestFencingTimelines(_TurnStateTestCase):
	def test_d4a_expiry_without_takeover_still_commits(self):
		"""D4 (a): the lease TTL lapses but NO takeover has run, so pump_epoch is
		unchanged and the settlement CAS still wins (the epoch is not a clock)."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		E = 3
		self._mk_turn(conv, "ts_d4a", seed, "terminal_observed", version=7, pump_epoch=E, reserved=1)
		# Simulate an expired lease with NO takeover: epoch still E on the row.
		frappe.db.set_value(
			PUMP,
			self._target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.commit()
		self.assertTrue(ts.settle_finalizing("ts_d4a", 7, E, required_effects=("terminal_publish",)))
		frappe.db.commit()
		self.assertEqual(self._state("ts_d4a"), "finalizing")

	def test_d4c_delayed_old_writer_stale_after_takeover(self):
		"""D4 (c): P_old streaming at epoch E stalls; P_new takes over
		(lease_acquire bumps epoch to E+1 and RE-STAMPS the turn); P_old's cached-E
		delta CAS then affects 0 rows (neither version nor epoch match)."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		amsg = self._mk_msg(conv, 2, role="assistant", content="", streaming=1)
		E = 4
		self._mk_turn(
			conv,
			"ts_d4c",
			seed,
			"streaming",
			version=12,
			pump_epoch=E,
			reserved=1,
			dispatching_at=frappe.utils.now(),
			assistant_message=amsg,
		)
		# Make the lease look stale so lease_acquire (the takeover) can win.
		frappe.db.set_value(
			PUMP,
			self._target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.set_value(PUMP, self._target, "pump_epoch", E, update_modified=False)
		frappe.db.commit()
		won, new_epoch = ts.lease_acquire(self._target, "hop-new")
		self.assertTrue(won)
		self.assertEqual(new_epoch, E + 1)
		row = frappe.db.get_value(TURN, "ts_d4c", ["pump_epoch", "version", "was_recovered"], as_dict=True)
		self.assertEqual(row["pump_epoch"], E + 1, "turn re-stamped to the new epoch")
		self.assertEqual(row["version"], 13, "re-stamp bumped version")
		# OARF-7: a CLEAN-hop re-stamp does NOT set was_recovered — a turn that merely
		# spans a hop boundary was not recovered; only a genuine recovery transition
		# (mark_recovering / snapshot recovery) sets the flag.
		self.assertEqual(row["was_recovered"], 0)
		# P_old wakes with cached (version=12, epoch=E) and tries a delta: 0 rows.
		self.assertFalse(ts.apply_delta("ts_d4c", 12, E, 5, amsg, "stale"), "stale writer affects 0")
		frappe.db.rollback()
		# P_new, owning it at E+1, applies the next frame.
		self.assertTrue(ts.apply_delta("ts_d4c", 13, E + 1, 5, amsg, "fresh"))
		frappe.db.commit()

	def test_apply_tool_fenced_stale_after_takeover_affects_zero(self):
		"""CDX-15: the tool-application fence — a stale pump's ``apply_tool_fenced`` (its
		cached version/epoch) must affect 0 rows after a takeover re-stamped the turn, and
		a settled turn (state past ``streaming``) must reject the tool CAS entirely, so a
		durable tool row can never be written into an already-settled conversation."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		amsg = self._mk_msg(conv, 2, role="assistant", content="", streaming=1)
		E = 4
		self._mk_turn(
			conv,
			"ts_tool_fence",
			seed,
			"streaming",
			version=12,
			pump_epoch=E,
			reserved=1,
			assistant_message=amsg,
		)
		frappe.db.set_value(
			PUMP,
			self._target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.set_value(PUMP, self._target, "pump_epoch", E, update_modified=False)
		frappe.db.commit()
		# Takeover: epoch -> E+1, turn re-stamped.
		won, new_epoch = ts.lease_acquire(self._target, "hop-new")
		self.assertTrue(won)
		self.assertEqual(new_epoch, E + 1)
		# The stale pump's tool CAS (cached version=12, epoch=E) affects 0 rows.
		self.assertFalse(
			ts.apply_tool_fenced("ts_tool_fence", 12, E, 5), "stale-epoch tool application affects 0 rows"
		)
		frappe.db.rollback()
		# The fresh owner (E+1, re-stamped version 13) wins and advances the watermark.
		self.assertTrue(ts.apply_tool_fenced("ts_tool_fence", 13, E + 1, 5))
		frappe.db.commit()
		self.assertEqual(int(frappe.db.get_value(TURN, "ts_tool_fence", "last_event_seq")), 5)
		# Once the turn settles past streaming, even the current owner cannot apply a tool.
		self.assertTrue(
			ts.mark_terminal_observed("ts_tool_fence", 14, E + 1, "relay:final", {"text": "done"})
		)
		frappe.db.commit()
		self.assertFalse(
			ts.apply_tool_fenced("ts_tool_fence", 15, E + 1, 6), "no tool application after streaming ends"
		)
		frappe.db.rollback()

	def test_d4d_dual_acquisition_exactly_one_wins(self):
		"""D4 (d): two pumps race lease_acquire on the same stale lease; exactly one
		bumps the epoch, the other re-evaluates the freshened predicate and gets 0
		rows (threaded, real separate DB connections)."""
		self._run_dual_acquire()

	def _run_dual_acquire(self, isolation: str | None = None):
		target = self._target
		frappe.db.set_value(
			PUMP,
			target,
			"lease_expires_at",
			frappe.utils.add_to_date(None, seconds=-5),
			update_modified=False,
		)
		frappe.db.set_value(PUMP, target, "pump_epoch", 0, update_modified=False)
		frappe.db.commit()
		site = frappe.local.site
		barrier = threading.Barrier(2)
		results: dict[str, tuple] = {}
		errors: list = []

		def worker(name: str):
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user(TEST_USER)
			try:
				if isolation:
					frappe.db.sql(f"SET SESSION TRANSACTION ISOLATION LEVEL {isolation}")
				barrier.wait(timeout=10)
				results[name] = ts.lease_acquire(target, f"hop-{name}")
			except Exception as e:  # pragma: no cover
				errors.append(e)
			finally:
				try:
					frappe.db.commit()
				finally:
					frappe.destroy()

		t1 = threading.Thread(target=worker, args=("A",))
		t2 = threading.Thread(target=worker, args=("B",))
		t1.start()
		t2.start()
		t1.join(timeout=20)
		t2.join(timeout=20)
		self.assertEqual(errors, [], f"worker error: {errors}")
		wins = [name for name, (won, _e) in results.items() if won]
		self.assertEqual(len(wins), 1, f"exactly one pump acquires (iso={isolation}): {results}")
		final_epoch = int(frappe.db.get_value(PUMP, target, "pump_epoch"))
		self.assertEqual(final_epoch, 1, "epoch advanced exactly once")


# --------------------------------------------------------------------------- #
# GLM two-writer finalize: exactly one wins, both orders + S1/S2 atomicity.
# --------------------------------------------------------------------------- #


class TestTwoWriterFinalize(_TurnStateTestCase):
	def test_sequential_second_settler_loses(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		amsg = self._mk_msg(conv, 2, role="assistant", content="partial", streaming=1)
		E = 2
		self._mk_turn(
			conv,
			"ts_2w",
			seed,
			"terminal_observed",
			version=6,
			pump_epoch=E,
			reserved=1,
			assistant_message=amsg,
		)
		self.assertTrue(
			ts.settle_finalizing(
				"ts_2w",
				6,
				E,
				assistant_message=amsg,
				final_text="final",
				required_effects=("terminal_publish",),
			)
		)
		frappe.db.commit()
		# The second writer (pump terminal frame vs receipt) uses the SAME (v,E): loses.
		self.assertFalse(ts.settle_finalizing("ts_2w", 6, E, assistant_message=amsg, final_text="OVERWRITE"))
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value(MSG, amsg, "content"), "final", "loser did not overwrite")
		self.assertEqual(self._v("ts_2w"), 7, "version bumped exactly once")
		self.assertEqual(frappe.db.count(EFFECT, {"turn": "ts_2w"}), 1, "effects inserted once (idempotent)")

	def test_lost_settlement_rolls_back_s1_write(self):
		"""S1+S2 atomicity: a stale-epoch settlement writes S1 (message) then loses
		S2; the caller's rollback undoes S1 too (no half-write escapes)."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		amsg = self._mk_msg(conv, 2, role="assistant", content="ORIGINAL", streaming=1)
		self._mk_turn(
			conv,
			"ts_s1",
			seed,
			"terminal_observed",
			version=6,
			pump_epoch=5,
			reserved=1,
			assistant_message=amsg,
		)
		# Stale epoch (4 != 5): S1 writes, S2 CAS affects 0, function returns False.
		self.assertFalse(ts.settle_finalizing("ts_s1", 6, 4, assistant_message=amsg, final_text="LEAKED"))
		# The caller's lease-loss exit rolls back; the S1 write must not persist.
		frappe.db.rollback()
		self.assertEqual(
			frappe.db.get_value(MSG, amsg, "content"), "ORIGINAL", "S1 rolled back with the lost CAS"
		)

	def test_threaded_two_writers_exactly_one_wins(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		amsg = self._mk_msg(conv, 2, role="assistant", content="partial", streaming=1)
		E = 2
		self._mk_turn(
			conv,
			"ts_2wt",
			seed,
			"terminal_observed",
			version=6,
			pump_epoch=E,
			reserved=1,
			assistant_message=amsg,
		)
		site = frappe.local.site
		barrier = threading.Barrier(2)
		results: dict[str, bool] = {}
		errors: list = []

		def worker(name: str):
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user(TEST_USER)
			try:
				barrier.wait(timeout=10)
				won = ts.settle_finalizing(
					"ts_2wt",
					6,
					E,
					assistant_message=amsg,
					final_text=f"by-{name}",
					required_effects=("terminal_publish",),
				)
				results[name] = won
				if won:
					frappe.db.commit()
				else:
					frappe.db.rollback()
			except Exception as e:  # pragma: no cover
				errors.append(e)
			finally:
				try:
					frappe.db.commit()
				finally:
					frappe.destroy()

		t1 = threading.Thread(target=worker, args=("A",))
		t2 = threading.Thread(target=worker, args=("B",))
		t1.start()
		t2.start()
		t1.join(timeout=20)
		t2.join(timeout=20)
		self.assertEqual(errors, [], f"worker error: {errors}")
		self.assertEqual(sorted(results.values()), [False, True], f"exactly one settler wins: {results}")
		self.assertEqual(self._state("ts_2wt"), "finalizing")
		self.assertEqual(self._v("ts_2wt"), 7, "version bumped exactly once")
		self.assertEqual(frappe.db.count(EFFECT, {"turn": "ts_2wt"}), 1, "effect inserted exactly once")


# --------------------------------------------------------------------------- #
# The recovering split by dispatching_at (OAR-4).
# --------------------------------------------------------------------------- #


class TestRecoveringSplit(_TurnStateTestCase):
	def test_pre_dispatch_park_returns_to_queued_and_releases_credit(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		amsg = self._mk_msg(conv, 2, role="assistant", content="half", streaming=1)
		# Parked from preparing (dispatching_at NULL); holds a credit + prepare refs.
		self._mk_turn(
			conv,
			"ts_rq",
			seed,
			"recovering",
			version=5,
			reserved=1,
			recovering=1,
			assistant_message=amsg,
			preparing_at=frappe.utils.now(),
			ready_at=frappe.utils.now(),
			dispatching_at=None,
		)
		# adopt must NOT fire (dispatching_at IS NULL).
		self.assertFalse(ts.recover_adopt("ts_rq", 5, 9, "streaming"), "cannot adopt a never-dispatched turn")
		frappe.db.rollback()
		# fresh-prepare park wins.
		self.assertTrue(ts.recover_to_queued("ts_rq", 5))
		frappe.db.commit()
		row = frappe.db.get_value(
			TURN,
			"ts_rq",
			["state", "reserved", "recovering", "assistant_message", "preparing_at", "ready_at"],
			as_dict=True,
		)
		self.assertEqual(row["state"], "queued")
		self.assertEqual(row["reserved"], 0, "credit released for a fresh prepare")
		self.assertEqual(row["recovering"], 0)
		self.assertIsNone(row["assistant_message"], "stale prepare refs dropped")
		self.assertIsNone(row["preparing_at"])
		self.assertIsNone(row["ready_at"])

	def test_in_flight_park_adopts_with_epoch_restamp(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		# Parked from streaming (dispatching_at NOT NULL).
		self._mk_turn(
			conv,
			"ts_ad",
			seed,
			"recovering",
			version=11,
			pump_epoch=3,
			reserved=1,
			recovering=1,
			dispatching_at=frappe.utils.now(),
		)
		# fresh-prepare must NOT fire (dispatching_at IS NOT NULL).
		self.assertFalse(ts.recover_to_queued("ts_ad", 11), "cannot fresh-prepare an in-flight turn")
		frappe.db.rollback()
		# adopt re-stamps the new epoch.
		self.assertTrue(ts.recover_adopt("ts_ad", 11, 8, "streaming"))
		frappe.db.commit()
		row = frappe.db.get_value(TURN, "ts_ad", ["state", "pump_epoch", "recovering"], as_dict=True)
		self.assertEqual(row["state"], "streaming")
		self.assertEqual(row["pump_epoch"], 8, "adopted turn re-stamped to the new epoch")
		self.assertEqual(row["recovering"], 0)

	def test_recover_to_terminal_observed_missed_terminal(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		self._mk_turn(
			conv,
			"ts_rt",
			seed,
			"recovering",
			version=11,
			pump_epoch=3,
			reserved=1,
			recovering=1,
			dispatching_at=frappe.utils.now(),
		)
		self.assertTrue(ts.recover_to_terminal_observed("ts_rt", 11, 8, "relay:final", {"text": "done"}))
		frappe.db.commit()
		row = frappe.db.get_value(TURN, "ts_rt", ["state", "pump_epoch", "terminal_kind"], as_dict=True)
		self.assertEqual(row["state"], "terminal_observed")
		self.assertEqual(row["pump_epoch"], 8)
		self.assertEqual(row["terminal_kind"], "relay:final")

	def test_recover_errored_budget_exhausted(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		self._mk_turn(conv, "ts_rerr", seed, "recovering", version=11, reserved=1, recovering=1)
		self.assertTrue(ts.recover_errored("ts_rerr", 11, "recovery budget exhausted"))
		frappe.db.commit()
		row = frappe.db.get_value(TURN, "ts_rerr", ["state", "reserved", "recovering"], as_dict=True)
		self.assertEqual(row["state"], "errored")
		self.assertEqual(row["reserved"], 0)
		self.assertEqual(row["recovering"], 0)

	def test_mark_recovering_prepare_deadline_guard(self):
		"""OAR-5: a preparing turn is only parked to recovering AFTER
		PREPARE_DEADLINE_S; a fresh preparing turn is left alone."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		self._mk_turn(
			conv, "ts_pd", seed, "preparing", version=3, reserved=1, preparing_at=frappe.utils.now()
		)
		self.assertFalse(
			ts.mark_recovering("ts_pd", 3, require_prepare_deadline=True), "fresh prepare not parked"
		)
		frappe.db.rollback()
		old = frappe.utils.add_to_date(None, seconds=-(ts.PREPARE_DEADLINE_S + 30))
		frappe.db.set_value(TURN, "ts_pd", "preparing_at", old, update_modified=False)
		frappe.db.commit()
		self.assertTrue(ts.mark_recovering("ts_pd", 3, require_prepare_deadline=True))
		frappe.db.commit()
		row = frappe.db.get_value(TURN, "ts_pd", ["state", "recovering", "was_recovered"], as_dict=True)
		self.assertEqual(row["state"], "recovering")
		self.assertEqual(row["recovering"], 1)
		self.assertEqual(row["was_recovered"], 1)

	def test_mark_recovering_general_and_deadline(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		# General watchdog park of a streaming turn (lease stale / reconcile-gone).
		self._mk_turn(
			conv, "ts_mr", seed, "streaming", version=8, pump_epoch=2, dispatching_at=frappe.utils.now()
		)
		self.assertTrue(ts.mark_recovering("ts_mr", 8))
		frappe.db.commit()
		self.assertEqual(self._state("ts_mr"), "recovering")
		# deadline-passed guard.
		self._mk_turn(
			conv,
			"ts_mrd",
			self._mk_msg(conv, 2),
			"dispatching",
			version=4,
			pump_epoch=2,
			dispatching_at=frappe.utils.now(),
			deadline_at=frappe.utils.add_to_date(None, seconds=5),
		)
		self.assertFalse(
			ts.mark_recovering("ts_mrd", 4, require_deadline_passed=True), "deadline not yet passed"
		)
		frappe.db.rollback()
		frappe.db.set_value(
			TURN, "ts_mrd", "deadline_at", frappe.utils.add_to_date(None, seconds=-5), update_modified=False
		)
		frappe.db.commit()
		self.assertTrue(ts.mark_recovering("ts_mrd", 4, require_deadline_passed=True))
		frappe.db.commit()
		self.assertEqual(self._state("ts_mrd"), "recovering")


# --------------------------------------------------------------------------- #
# Effect ledger: idempotency + force-done at 3 + all-done guard.
# --------------------------------------------------------------------------- #


class TestEffectLedger(_TurnStateTestCase):
	def _mk_finalizing(self, effects):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		rid = f"ts_eff_{frappe.generate_hash(length=6)}"
		self._mk_turn(conv, rid, seed, "finalizing", version=7)
		ts.insert_required_effects(rid, effects)
		frappe.db.commit()
		return rid

	def test_insert_required_effects_idempotent(self):
		rid = self._mk_finalizing(("terminal_publish", "auto_title"))
		self.assertEqual(frappe.db.count(EFFECT, {"turn": rid}), 2)
		# A duplicate settlement replay inserts nothing new (composite name PK).
		again = ts.insert_required_effects(rid, ("terminal_publish", "auto_title", "usage"))
		frappe.db.commit()
		self.assertEqual(again, 1, "only the genuinely new 'usage' row is inserted")
		self.assertEqual(frappe.db.count(EFFECT, {"turn": rid}), 3)

	def test_claim_complete_and_all_done(self):
		rid = self._mk_finalizing(("terminal_publish", "auto_title"))
		self.assertFalse(ts.all_required_effects_done(rid))
		# CDX-4: claim returns (outcome, token); complete/release fence on the token.
		o, tok = ts.claim_effect(rid, "terminal_publish")
		self.assertEqual(o, "attempt")
		self.assertEqual(frappe.db.get_value(EFFECT, f"{rid}::terminal_publish", "status"), "running")
		self.assertTrue(ts.complete_effect(rid, "terminal_publish", tok))
		frappe.db.commit()
		self.assertEqual(ts.claim_effect(rid, "terminal_publish")[0], "done", "a done effect is skipped")
		self.assertFalse(ts.all_required_effects_done(rid), "auto_title still pending")
		o2, tok2 = ts.claim_effect(rid, "auto_title")
		self.assertEqual(o2, "attempt")
		self.assertTrue(ts.complete_effect(rid, "auto_title", tok2))
		frappe.db.commit()
		self.assertTrue(ts.all_required_effects_done(rid))
		# finalize_done now succeeds.
		self.assertTrue(ts.finalize_done(rid, 7))
		frappe.db.commit()
		self.assertEqual(self._state(rid), "done")

	def test_exclusive_claim_blocks_second_finalizer(self):
		# CDX-4: a live claim is EXCLUSIVE — a second finalizer sees 'busy', never
		# runs the same effect twice (until the claim goes stale).
		rid = self._mk_finalizing(("rich_outputs",))
		o1, tok1 = ts.claim_effect(rid, "rich_outputs")
		self.assertEqual(o1, "attempt")
		frappe.db.commit()
		o2, tok2 = ts.claim_effect(rid, "rich_outputs")
		self.assertEqual(o2, "busy", "a live (non-stale) claim is not re-claimable")
		self.assertIsNone(tok2)
		# The loser cannot complete the winner's claim (token fence).
		self.assertFalse(ts.complete_effect(rid, "rich_outputs", "not-the-token"))
		self.assertTrue(ts.complete_effect(rid, "rich_outputs", tok1))
		frappe.db.commit()
		self.assertTrue(ts.all_required_effects_done(rid))

	def test_release_returns_to_pending_for_retry(self):
		# CDX-4: a FAILED attempt releases running->pending (attempts kept) so the next
		# cycle re-claims and retries — never a silent permanent loss.
		rid = self._mk_finalizing(("usage",))
		o, tok = ts.claim_effect(rid, "usage")
		self.assertEqual(o, "attempt")
		self.assertTrue(ts.release_effect(rid, "usage", tok))
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(EFFECT, f"{rid}::usage", "status"), "pending")
		self.assertEqual(int(frappe.db.get_value(EFFECT, f"{rid}::usage", "attempts")), 1, "attempt kept")
		# A fresh claim retries it.
		o2, tok2 = ts.claim_effect(rid, "usage")
		self.assertEqual(o2, "attempt")
		self.assertTrue(ts.complete_effect(rid, "usage", tok2))

	def test_force_done_after_max_attempts(self):
		rid = self._mk_finalizing(("rich_outputs",))
		# Three failing attempts: claim (running) then RELEASE (running->pending, keeps
		# the attempt increment) to simulate a failing effect each cycle.
		for i in range(ts.FINALIZE_MAX_ATTEMPTS):
			o, tok = ts.claim_effect(rid, "rich_outputs")
			self.assertEqual(o, "attempt", f"attempt {i + 1}")
			self.assertTrue(ts.release_effect(rid, "rich_outputs", tok))
			frappe.db.commit()  # attempt persisted; effect back to pending (simulated failure)
		self.assertEqual(
			int(frappe.db.get_value(EFFECT, f"{rid}::rich_outputs", "attempts")), ts.FINALIZE_MAX_ATTEMPTS
		)
		self.assertFalse(ts.all_required_effects_done(rid), "still pending before force-done")
		# The next claim FORCE-DONES it (budget exhausted) so the turn can finish.
		self.assertEqual(ts.claim_effect(rid, "rich_outputs")[0], "force_done")
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(EFFECT, f"{rid}::rich_outputs", "status"), "done")
		self.assertTrue(ts.all_required_effects_done(rid), "force-done unblocks finalize")
		self.assertTrue(ts.finalize_done(rid, 7), "a permanently-failing effect never strands the turn")
		frappe.db.commit()
		self.assertEqual(self._state(rid), "done")

	def test_live_third_attempt_claim_is_busy_not_force_done(self):
		# CDX-16: A holds a LIVE (non-stale) attempt-3 running claim while executing its
		# side effect; a second finalizer B must see BUSY and must NOT force-done it
		# (force-done applies only to pending / stale-running rows). The attempt-vs-
		# liveness order defect + the read-then-force-done TOCTOU are both closed.
		rid = self._mk_finalizing(("rich_outputs",))
		# Two failed attempts -> attempts=2, back to pending.
		for _ in range(2):
			o, tok = ts.claim_effect(rid, "rich_outputs")
			self.assertEqual(o, "attempt")
			self.assertTrue(ts.release_effect(rid, "rich_outputs", tok))
			frappe.db.commit()
		# A claims attempt 3 -> LIVE running, attempts=3, token A.
		oA, tokA = ts.claim_effect(rid, "rich_outputs")
		self.assertEqual(oA, "attempt")
		frappe.db.commit()
		self.assertEqual(int(frappe.db.get_value(EFFECT, f"{rid}::rich_outputs", "attempts")), 3)
		self.assertEqual(frappe.db.get_value(EFFECT, f"{rid}::rich_outputs", "status"), "running")
		# B (another finalizer) must NEITHER claim NOR force-done the live attempt-3 row.
		oB, tokB = ts.claim_effect(rid, "rich_outputs")
		self.assertEqual(oB, "busy", "a LIVE 3rd-attempt claim is BUSY, never force-done")
		self.assertIsNone(tokB)
		self.assertEqual(
			frappe.db.get_value(EFFECT, f"{rid}::rich_outputs", "status"),
			"running",
			"still running under A — not marked done from under it",
		)
		# force_done_effect directly is a no-op on the live claim (affected-rows CAS).
		self.assertFalse(ts.force_done_effect(rid, "rich_outputs"), "force-done no-ops a live claim")
		self.assertEqual(frappe.db.get_value(EFFECT, f"{rid}::rich_outputs", "status"), "running")
		# A completes its effect under its own token -> done (token fence intact).
		self.assertTrue(ts.complete_effect(rid, "rich_outputs", tokA))
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(EFFECT, f"{rid}::rich_outputs", "status"), "done")

	def test_stale_running_claim_is_reclaimable(self):
		# CDX-4: a running effect whose claimed_at is older than the stale bound (its
		# finalizer crashed) is RECLAIMABLE by a fresh claim.
		rid = self._mk_finalizing(("rich_outputs",))
		o1, tok1 = ts.claim_effect(rid, "rich_outputs")
		self.assertEqual(o1, "attempt")
		frappe.db.commit()
		# Backdate the claim past the stale bound (simulate the finalizer having died).
		stale = frappe.utils.add_to_date(None, seconds=-(ts.EFFECT_CLAIM_STALE_S + 5))
		frappe.db.set_value(EFFECT, f"{rid}::rich_outputs", "claimed_at", stale, update_modified=False)
		frappe.db.commit()
		o2, tok2 = ts.claim_effect(rid, "rich_outputs")
		self.assertEqual(o2, "attempt", "a stale running claim is reclaimable")
		self.assertNotEqual(tok1, tok2)
		self.assertTrue(ts.complete_effect(rid, "rich_outputs", tok2))

	def test_watchdog_effect_scan_finds_open_effect_turns(self):
		# CDX-4: turns_with_open_effects surfaces a TERMINAL (errored) turn whose owed
		# effects are still open, independent of the nonterminal-turn scan.
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		rid = f"ts_openeff_{frappe.generate_hash(length=6)}"
		self._mk_turn(conv, rid, seed, "errored", version=9)
		ts.insert_required_effects(rid, ("terminal_publish", "macro_advance"))
		frappe.db.commit()
		self.assertIn(rid, ts.turns_with_open_effects(self._target))
		self.assertIn(self._target, ts.shards_with_open_effects())
		# A live (non-stale) running claim is EXCLUDED (its finalizer owns it).
		o, tok = ts.claim_effect(rid, "terminal_publish")
		ts.complete_effect(rid, "terminal_publish", tok)
		o2, tok2 = ts.claim_effect(rid, "macro_advance")  # leave it running/live
		frappe.db.commit()
		self.assertNotIn(rid, ts.turns_with_open_effects(self._target), "live claim excluded")

	def test_claim_unknown_effect_is_done(self):
		rid = self._mk_finalizing(("terminal_publish",))
		self.assertEqual(
			ts.claim_effect(rid, "not_required")[0], "done", "no such required row => nothing owed"
		)


# --------------------------------------------------------------------------- #
# Lease lifecycle: acquire / renew / heartbeat / conditional idle-release.
# --------------------------------------------------------------------------- #


class TestLeaseLifecycle(_TurnStateTestCase):
	def test_acquire_then_renew_and_heartbeat(self):
		won, epoch = ts.lease_acquire(self._target, "hop-0", hop_counter=1)
		self.assertTrue(won)
		self.assertEqual(epoch, 1)
		self.assertEqual(int(frappe.db.get_value(PUMP, self._target, "hop_counter")), 1)
		# renew succeeds while the epoch matches; a stale epoch renews 0.
		self.assertTrue(ts.lease_renew(self._target, epoch, holder="hop-0"))
		self.assertFalse(ts.lease_renew(self._target, epoch + 5), "stale epoch cannot renew")
		# heartbeat writes loop_heartbeat_ts under the same epoch fence.
		self.assertTrue(ts.lease_heartbeat(self._target, epoch))
		self.assertIsNotNone(frappe.db.get_value(PUMP, self._target, "loop_heartbeat_ts"))
		self.assertFalse(ts.lease_heartbeat(self._target, epoch + 5))

	def test_idle_release_blocked_by_work_then_releases(self):
		won, epoch = ts.lease_acquire(self._target, "hop-0")
		self.assertTrue(won)
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		# A nonterminal turn on the shard blocks the atomic idle release.
		self._mk_turn(
			conv, "ts_work", seed, "streaming", version=3, pump_epoch=epoch, dispatching_at=frappe.utils.now()
		)
		self.assertFalse(
			ts.lease_release_if_idle(self._target, epoch), "0 rows: work remains -> caller CONTINUES the loop"
		)
		# Settle the work to a terminal state; now the shard is idle and it releases.
		frappe.db.set_value(TURN, "ts_work", "state", "done", update_modified=False)
		frappe.db.commit()
		self.assertTrue(ts.lease_release_if_idle(self._target, epoch), "idle -> released")

	def test_idle_release_wrong_epoch_does_not_release(self):
		won, epoch = ts.lease_acquire(self._target, "hop-0")
		self.assertTrue(won)
		self.assertFalse(
			ts.lease_release_if_idle(self._target, epoch + 5), "a pump that lost the epoch releases 0 rows"
		)

	def test_acquire_fails_on_a_fresh_lease(self):
		won, epoch = ts.lease_acquire(self._target, "hop-0")
		self.assertTrue(won)
		# A second acquirer against the (now fresh) lease fails.
		won2, epoch2 = ts.lease_acquire(self._target, "hop-other")
		self.assertFalse(won2)
		self.assertIsNone(epoch2)


# --------------------------------------------------------------------------- #
# Canonical lock-order dev assertion (OAR-6).
# --------------------------------------------------------------------------- #


class TestLockOrderAssertion(_TurnStateTestCase):
	def test_correct_order_and_reentrant_ok(self):
		ts.reset_lock_tracking()
		ts.assert_lock_order("shard")
		ts.assert_lock_order("conversation")
		ts.assert_lock_order("conversation")  # reentrant same-rank is allowed
		ts.assert_lock_order("turn")
		ts.assert_lock_order("message")
		ts.reset_lock_tracking()

	def test_inversion_raises_in_dev_mode(self):
		ts.reset_lock_tracking()
		with patch.dict(frappe.local.conf, {"jarvis_pump_lock_assert": 1}):
			ts.assert_lock_order("turn")
			with self.assertRaises(ts.LockOrderError):
				ts.assert_lock_order("shard")  # rank 1 after rank 3 = inversion
		ts.reset_lock_tracking()

	def test_inversion_only_logs_when_not_dev(self):
		ts.reset_lock_tracking()
		with patch.dict(frappe.local.conf, {"developer_mode": 0, "jarvis_pump_lock_assert": 0}):
			with patch.object(frappe, "log_error") as logged:
				ts.assert_lock_order("turn")
				ts.assert_lock_order("shard")  # inversion: logs, does not raise
				self.assertTrue(logged.called)
		ts.reset_lock_tracking()


# --------------------------------------------------------------------------- #
# Fenced publish carries (turn_id, event_seq).
# --------------------------------------------------------------------------- #


class TestFencedPublish(_TurnStateTestCase):
	def test_publish_fenced_payload(self):
		captured = []
		with patch.object(
			ts, "publish_to_user", side_effect=lambda user, payload: captured.append((user, payload))
		):
			ts.publish_fenced(
				"u@x", "run:recovering", conversation_id="c1", run_id="r1", event_seq=42, extra_field="v"
			)
		self.assertEqual(len(captured), 1)
		user, payload = captured[0]
		self.assertEqual(user, "u@x")
		self.assertEqual(payload["kind"], "run:recovering")
		self.assertEqual(payload["turn_id"], "r1")
		self.assertEqual(payload["run_id"], "r1")
		self.assertEqual(payload["event_seq"], 42)
		self.assertEqual(payload["extra_field"], "v")

	def test_publish_fenced_swallows_errors(self):
		with patch.object(ts, "publish_to_user", side_effect=RuntimeError("socket down")):
			# Best-effort: a publish failure never propagates.
			ts.publish_fenced("u@x", "run:end", conversation_id="c1", run_id="r1")

	def test_publish_fenced_carries_pump_epoch(self):
		"""CDX-3: a pump-owned payload carries pump_epoch (+ event_seq) so the client
		fences a stale writer end-to-end."""
		captured = []
		with patch.object(ts, "publish_to_user", side_effect=lambda u, p: captured.append(p)):
			ts.publish_fenced(
				"u@x", "assistant:delta", conversation_id="c1", run_id="r1", event_seq=3, pump_epoch=9
			)
		self.assertEqual(captured[0]["pump_epoch"], 9)
		self.assertEqual(captured[0]["event_seq"], 3)

	def test_publish_fenced_belt_skips_stale_epoch(self):
		"""CDX-3 belt: with pump_epoch + relay_target_id, a publish whose epoch is no
		longer the shard's current epoch is skipped server-side."""
		won, E = ts.lease_acquire(self._target, "owner")
		self.assertTrue(won)
		captured = []
		with patch.object(ts, "publish_to_user", side_effect=lambda u, p: captured.append(p)):
			ts.publish_fenced(
				"u@x", "run:end", conversation_id="c", run_id="r", pump_epoch=E, relay_target_id=self._target
			)
			self.assertEqual(len(captured), 1)
			frappe.db.set_value(PUMP, self._target, "pump_epoch", E + 5, update_modified=False)
			frappe.db.commit()
			ts.publish_fenced(
				"u@x", "run:end", conversation_id="c", run_id="r", pump_epoch=E, relay_target_id=self._target
			)
			self.assertEqual(len(captured), 1, "belt-skips a stale-epoch publish")


class TestLeaseHandoff(_TurnStateTestCase):
	def test_lease_handoff_transfers_and_is_epoch_guarded(self):
		"""CDX-1: the clean-hop atomic transfer makes the lease immediately acquirable
		(even against a freshly-renewed lease), advances hop_counter, and a stale
		outgoing hop (mismatched epoch) transfers nothing."""
		won, E = ts.lease_acquire(self._target, "hopA")
		self.assertTrue(won)
		self.assertTrue(ts.lease_renew(self._target, E))  # a valid ~now+30s lease
		self.assertTrue(ts.lease_handoff(self._target, E, 5, "succ-job"))
		row = frappe.db.get_value(
			PUMP, self._target, ["lease_expires_at", "hop_counter", "lease_holder"], as_dict=True
		)
		self.assertEqual(int(row["hop_counter"]), 5)
		self.assertEqual(row["lease_holder"], "succ-job")
		self.assertLess(
			frappe.utils.get_datetime(row["lease_expires_at"]),
			frappe.utils.get_datetime(ts._now()),
			"lease made immediately acquirable",
		)
		# A successor acquires at once, epoch E+1.
		won2, E2 = ts.lease_acquire(self._target, "succ")
		self.assertTrue(won2)
		self.assertEqual(E2, E + 1)
		# A stale outgoing hop (still at epoch E) transfers NOTHING and bumps no counter.
		self.assertFalse(ts.lease_handoff(self._target, E, 6, "succ2"))
		self.assertEqual(
			int(frappe.db.get_value(PUMP, self._target, "hop_counter")),
			5,
			"stale handoff does not bump the counter",
		)


# --------------------------------------------------------------------------- #
# R-14: key CAS tests under BOTH READ COMMITTED and REPEATABLE READ.
# --------------------------------------------------------------------------- #


class TestIsolationLevels(_TurnStateTestCase):
	def _run_settlement_under(self, isolation: str):
		frappe.db.commit()
		frappe.db.sql(f"SET SESSION TRANSACTION ISOLATION LEVEL {isolation}")
		try:
			conv = self._mk_conv()
			seed = self._mk_msg(conv, 1)
			amsg = self._mk_msg(conv, 2, role="assistant", content="p", streaming=1)
			rid = f"ts_iso_{frappe.generate_hash(length=6)}"
			E = 3
			self._mk_turn(
				conv,
				rid,
				seed,
				"terminal_observed",
				version=6,
				pump_epoch=E,
				reserved=1,
				assistant_message=amsg,
			)
			# Winner settles.
			self.assertTrue(
				ts.settle_finalizing(
					rid,
					6,
					E,
					assistant_message=amsg,
					final_text="final",
					required_effects=("terminal_publish",),
				)
			)
			frappe.db.commit()
			self.assertEqual(self._state(rid), "finalizing")
			# Stale writer (bumped version) loses under this isolation level.
			self.assertFalse(ts.settle_finalizing(rid, 6, E), f"stale settle loses under {isolation}")
			frappe.db.rollback()
		finally:
			frappe.db.commit()
			frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			frappe.db.commit()

	def test_settlement_under_read_committed(self):
		self._run_settlement_under("READ COMMITTED")

	def test_settlement_under_repeatable_read(self):
		self._run_settlement_under("REPEATABLE READ")

	def test_dual_acquisition_under_read_committed(self):
		TestFencingTimelines._run_dual_acquire(self, isolation="READ COMMITTED")

	def test_dual_acquisition_under_repeatable_read(self):
		TestFencingTimelines._run_dual_acquire(self, isolation="REPEATABLE READ")
