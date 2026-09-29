"""Snapshot-isolation races in admission, prepare, stale_scan and turn_recovery
(W1, W2, P6, P7, P8), against the real engine.

Every test arms the real race (``race_harness``): a plain read opens the snapshot
the real code would hold, a second connection commits the competing write, then the
real unit under test runs. ``assert_fix_closes_race`` runs each scenario with the
defences in ``jarvis.chat.txn`` switched off (the race must bite: 1020, or - where
the unpatched code swallows the error - an outcome ``check`` rejects) and on (the
outcome must be right), so none of these can pass without the fix.

Sites covered here:
  * W1 ``admission.cancel_queued_turn`` - the Cancel button races the pump's own
    promote/claim/dispatch CASes on the same Turn row.
  * W2 ``admission.mark_cancel_requested`` + ``pump.request_cancel_conversation`` -
    the Stop button, called from ``chat.api.stop_run``.
  * P6 ``prepare.run_prepare``'s first CAS (``claim_preparing``).
  * P7 ``stale_scan.scan_and_mark_errored`` (+ its own per-row unit).
  * P8 ``turn_recovery.recover_pending_turns``'s ceiling-backstop loop.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from jarvis.chat import admission, prepare, pump, stale_scan, turn_recovery
from jarvis.chat import turn_state as ts
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests.race_harness import (
	as_job,
	assert_fix_closes_race,
	open_read_view,
	other_connection,
	snapshot_isolation_on,
)
from jarvis.tests.test_pump import CONV, MSG, TARGET_PREFIX, TURN, _PumpTestCase

PUMP = "Jarvis Relay Pump"
CEILING_MINUTES = turn_recovery._RECOVERY_CEILING_MINUTES


def setUpModule():
	install_synthetic_runtime_profile()


def _compete(sql: str, params: tuple) -> None:
	"""Commit ``sql`` from a second connection, the way another web request or the
	pump's own hop would."""
	with other_connection() as other:
		other.sql(sql, params)
		other.commit()


def _race_once(real, after=None, before=None):
	"""Wrap ``real`` so its FIRST call runs the race around it: ``before`` ahead of the
	call, ``after`` right after it. Later calls (a replay) pass straight through."""
	fired: list = []

	def wrapper(*args, **kwargs):
		first = not fired
		fired.append(1)
		if first and before:
			before()
		result = real(*args, **kwargs)
		if first and after:
			after()
		return result

	return wrapper


class _RaceCase(_PumpTestCase):
	def _new_shard(self) -> None:
		"""A fresh shard per scenario run: ``assert_fix_closes_race`` runs each scenario
		twice and the first run's row/lease state must not bleed into the second. Same
		setup as ``_PumpTestCase.setUp``; ``_cleanup`` (in tearDown) removes every
		``pmpx_`` shard."""
		self._target = f"{TARGET_PREFIX}{frappe.generate_hash(length=10)}"
		ts._ensure_control_row(self._target)
		frappe.db.set_value(PUMP, self._target, "transport_mode", pump._MODE_PUMP, update_modified=False)
		frappe.db.commit()
		pump._LIFECYCLE_MODE_CACHE.pop(self._target, None)
		ts.reset_lock_tracking()


# --------------------------------------------------------------------------- #
# W1 - admission.cancel_queued_turn (web)
# --------------------------------------------------------------------------- #


class TestCancelQueuedTurnRace(_RaceCase):
	def test_cancel_replays_a_promote_race_and_never_500s(self):
		"""The pump's promote CAS (queued -> dispatching) lands on the same row between
		the endpoint's read and its own CAS. Without the fix the stale CAS raises 1020
		straight into the web request (the reported 500); with it the replay re-reads
		the present row and correctly reports "already started" - no exception either
		way, which is the point."""

		def scenario():
			self._new_shard()
			conv = self._mk_conv()
			seed = self._mk_msg(conv, content="Export my overdue sales invoices to an Excel file.")
			rid = f"pmpr_{frappe.generate_hash(length=8)}"
			self._mk_turn(conv, rid, seed, "queued", version=0, reserved=1)
			with snapshot_isolation_on():
				open_read_view()  # the endpoint's own prelude read, fixed early
				# The pump's promote CAS: queued -> dispatching, credit claimed.
				_compete(
					f"UPDATE `tab{TURN}` SET state='dispatching', reserved=1, version=version+1 WHERE name=%s",
					(rid,),
				)
				res = admission.cancel_queued_turn(rid)
			return rid, res

		def check(res):
			rid, out = res
			# The real state (post-race) is 'dispatching' - not cancellable from here;
			# the replay must see that FRESH state, not crash and not silently no-op.
			self.assertFalse(out["ok"])
			self.assertEqual(frappe.db.get_value(TURN, rid, "state"), "dispatching")

		assert_fix_closes_race(self, scenario, check)

	def test_preparing_ready_cleanup_replays_a_race_not_a_false_success(self):
		"""BLOCKER fix (full-diff review): the placeholder-cleanup CAS, run right
		after ``ts.cancel_preparing_or_ready`` wins (still uncommitted), used to
		swallow ANY exception including a write conflict - but a write conflict here
		means the server already aborted the WHOLE transaction, the cancel CAS
		included, so the swallow let the unit fall through to a no-op commit and
		report success while NOTHING landed (chip clears, credit never released).

		Races a competing write on the assistant-message row between
		``ts.cancel_preparing_or_ready`` returning (won, uncommitted) and the
		placeholder cleanup's own CAS on that same row."""

		def scenario():
			self._new_shard()
			conv = self._mk_conv()
			seed = self._mk_msg(conv)
			amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
			rid = f"pmpr_{frappe.generate_hash(length=8)}"
			self._mk_turn(conv, rid, seed, "preparing", version=3, reserved=1, assistant_message=amsg)

			real = ts.cancel_preparing_or_ready

			def race():
				# The Turn CAS just won (uncommitted); a concurrent writer (e.g. a
				# pump tool-event) touches the assistant placeholder row before the
				# endpoint's own cleanup CAS reaches it.
				_compete(f"UPDATE `tab{MSG}` SET content=%s WHERE name=%s", ("raced", amsg))

			fenced = _race_once(real, after=race)

			with (
				snapshot_isolation_on(),
				patch.object(ts, "cancel_preparing_or_ready", fenced),
				patch.object(pump, "ensure_pump"),
				patch.object(pump, "lpush_wake"),
			):
				open_read_view()  # the endpoint's own prelude read, fixed early
				res = admission.cancel_queued_turn(rid)
			return rid, amsg, res

		def check(res):
			rid, amsg, out = res
			self.assertTrue(out["ok"])
			self.assertEqual(out["path"], "preparing_ready")
			# The cancel must actually have landed: state cancelled, credit released.
			row = frappe.db.get_value(TURN, rid, ["state", "reserved"], as_dict=True)
			self.assertEqual(row.state, "cancelled")
			self.assertEqual(int(row.reserved or 0), 0)
			# The placeholder is cleaned (no stuck spinner) AND the competing write
			# survives (the replay's cleanup CAS only ever touches `streaming`).
			msg = frappe.db.get_value(MSG, amsg, ["streaming", "content"], as_dict=True)
			self.assertEqual(int(msg.streaming), 0)
			self.assertEqual(msg.content, "raced")

		assert_fix_closes_race(self, scenario, check)


# --------------------------------------------------------------------------- #
# W2 - admission.mark_cancel_requested + pump.request_cancel_conversation (Stop)
# --------------------------------------------------------------------------- #


class TestStopCancelIntentRace(_RaceCase):
	def test_mark_cancel_requested_replays_a_version_race(self):
		"""admission.mark_cancel_requested's CAS is state-only (no version fence), but
		MariaDB's snapshot check fires on the ROW regardless of which field changed.
		Before the fix the bare except silently dropped the intent (no log); after,
		a lost race is replayed and lands, or - on genuine exhaustion - is logged."""

		def scenario():
			self._new_shard()
			conv = self._mk_conv()
			seed = self._mk_msg(conv)
			rid = f"pmpr_{frappe.generate_hash(length=8)}"
			self._mk_turn(conv, rid, seed, "dispatching", version=2, reserved=1)
			with patch.dict(frappe.local.conf, {admission.FLAG: 1}):
				with snapshot_isolation_on():
					open_read_view()
					_compete(f"UPDATE `tab{TURN}` SET version=version+1 WHERE name=%s", (rid,))
					admission.mark_cancel_requested(conv)
			return rid

		def check(rid):
			self.assertEqual(int(frappe.db.get_value(TURN, rid, "cancel_requested") or 0), 1)

		assert_fix_closes_race(self, scenario, check)

	def test_request_cancel_conversation_replays_a_version_bump_race(self):
		"""pump.request_cancel_conversation's CAS IS version-fenced
		(``ts.request_cancel``). A concurrent write (e.g. a tool event) bumps the
		Turn's version between the read and the CAS - exactly the Stop-mid-stream
		race that used to make settlement report an error instead of "stopped"."""

		def scenario():
			self._new_shard()
			conv = self._mk_conv()
			seed = self._mk_msg(conv, content="Export something")
			amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
			rid = f"pmpr_{frappe.generate_hash(length=8)}"
			self._mk_turn(conv, rid, seed, "streaming", version=4, assistant_message=amsg)
			with (
				snapshot_isolation_on(),
				patch.object(pump, "ensure_pump"),
				patch.object(pump, "lpush_wake"),
			):
				open_read_view()
				_compete(f"UPDATE `tab{TURN}` SET version=version+1 WHERE name=%s", (rid,))
				won = pump.request_cancel_conversation(conv)
			return rid, won

		def check(res):
			rid, won = res
			self.assertTrue(won)
			self.assertEqual(int(frappe.db.get_value(TURN, rid, "cancel_requested") or 0), 1)

		assert_fix_closes_race(self, scenario, check)


# --------------------------------------------------------------------------- #
# P6 - prepare.run_prepare's first CAS (claim_preparing)
# --------------------------------------------------------------------------- #


class TestPrepareClaimRace(_RaceCase):
	def test_a_lost_claim_race_never_crashes_the_job(self):
		"""A competing writer (a user cancel, or the watchdog reclaiming the credit)
		commits on the Turn row between run_prepare's first read and its claim CAS.
		Without the fix that raises 1020 straight out of the job (breaking its "never
		raises out" docstring); with it, fresh_snapshot() ahead of the claim closes
		the exposure and the job resolves to the SAME outcome the real (post-race)
		state dictates - here, the row was cancelled, so run_prepare skips cleanly."""

		def scenario():
			self._new_shard()
			conv = self._mk_conv()
			seed = self._mk_msg(conv)
			rid = f"pmpr_{frappe.generate_hash(length=8)}"
			self._mk_turn(conv, rid, seed, "queued", version=0, reserved=1)
			with snapshot_isolation_on(), as_job():
				open_read_view()  # run_prepare's own first read, fixed early
				# A genuine concurrent cancel: queued -> cancelled, credit released.
				_compete(
					f"UPDATE `tab{TURN}` SET state='cancelled', reserved=0, version=version+1 WHERE name=%s",
					(rid,),
				)
				result = prepare.run_prepare(rid, relay_target_id=self._target)
			return rid, result

		def check(res):
			rid, result = res
			self.assertEqual(result, {"ok": True, "skipped": "cancelled"})
			self.assertEqual(frappe.db.get_value(TURN, rid, "state"), "cancelled")

		assert_fix_closes_race(self, scenario, check)


# --------------------------------------------------------------------------- #
# P7 - stale_scan.scan_and_mark_errored (per-row units)
# --------------------------------------------------------------------------- #


class TestStaleScanRowRace(_RaceCase):
	def _stale_msg(self, conv: str, *, age_seconds: int = 300) -> str:
		amsg = self._mk_msg(conv, role="assistant", content="partial", streaming=1)
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-age_seconds)
		frappe.db.sql(f"UPDATE `tab{MSG}` SET creation=%s WHERE name=%s", (old, amsg))
		frappe.db.commit()
		return amsg

	def test_a_conflict_on_one_row_does_not_stop_the_rest_of_the_scan(self):
		"""Two stale streaming rows in the same fresh conversation (no session_key, so
		both take the "genuinely unrecoverable" error branch). A competing write lands
		on row 1 between the scan's one snapshot read and its own per-row write; row 2
		must still be marked, and row 1 must not crash the whole cycle.

		``_sweep_orphan_turns`` is patched to a no-op: on a live site with real orphan
		user messages it commits between the scan's SELECT and this loop (the
		admission shard lock it can reach is commit-first), which drops the very
		snapshot this test needs to stay open - nondeterministic, unrelated to the
		loop under test here (``_scan_one_row``; the orphan-sweep defence has its own
		test below, ``TestOrphanSweepRowRace``)."""

		def scenario():
			conv = self._mk_conv()
			mine = {self._stale_msg(conv), self._stale_msg(conv)}
			real_row = stale_scan._scan_one_row
			seen: list = []

			def one_row(r, *args, **kwargs):
				# Only this test's rows reach the real per-row unit: the scan's SELECT sees
				# every stale row on the site, and a live row must neither be touched by a test
				# nor decide the race. The SELECT has no ORDER BY, so the competing write goes
				# to whichever of ours runs FIRST, after that SELECT opened the snapshot.
				if r["name"] not in mine:
					return 0
				if not seen:
					_compete(f"UPDATE `tab{MSG}` SET content=%s WHERE name=%s", ("raced", r["name"]))
				seen.append(r["name"])
				return real_row(r, *args, **kwargs)

			with (
				snapshot_isolation_on(),
				as_job(),
				patch.object(stale_scan, "_sweep_orphan_turns", return_value=0),
				patch.object(stale_scan, "_scan_one_row", one_row),
			):
				errored = stale_scan.scan_and_mark_errored()
			return mine, errored, seen

		def check(res):
			mine, errored, seen = res
			self.assertEqual(errored, 2)
			self.assertEqual(
				sorted(seen), sorted(mine), "both rows reached, the raced one did not stop the scan"
			)
			for m in mine:
				row = frappe.db.get_value(MSG, m, ["streaming", "error"], as_dict=True)
				self.assertEqual(int(row.streaming), 0)
				self.assertIn("abandoned", (row.error or "").lower())

		assert_fix_closes_race(self, scenario, check)


class TestOrphanSweepRowRace(_RaceCase):
	"""P7's other defence: ``stale_scan._sweep_orphan_turns``'s per-row
	``fresh_snapshot() + catch/log/skip`` around ``_heal_or_error_orphan`` (not a
	replay - see the code comment for why: a redispatch and ``job.cancel()`` are
	not safe to blindly re-run after a server-side abort)."""

	def _mk_orphan_user_msg(self, conv: str, *, age_seconds: int = 300) -> str:
		"""A 'user' message with no later assistant reply, aged into
		``_sweep_orphan_turns``'s window (``ORPHAN_MIN_AGE_SECONDS`` .. ``_MAX``)."""
		uid = self._mk_msg(conv, role="user", content="hi")
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-age_seconds)
		frappe.db.sql(f"UPDATE `tab{MSG}` SET creation=%s WHERE name=%s", (old, uid))
		frappe.db.commit()
		return uid

	def test_a_conflict_on_one_orphan_row_does_not_stop_the_sweep(self):
		"""Two orphaned user messages (no assistant reply, no queued/started RQ job -
		the plain "heal via redispatch" first-strike path). A competing write lands on
		row A's message between ``fresh_snapshot()`` and ``_heal_or_error_orphan``'s
		own write (armed by racing ``_heal_or_error_orphan`` itself, the way the pump
		suite races ``apply_tool_fenced``): fix-off raises 1020 out of the whole
		sweep; fix-on catches it, logs exactly once, rolls back, and still heals row
		B. Row A's write is NOT retried (the module's own catch/log/skip choice), so
		it stays unhealed this cycle - the next scan cycle picks it up again."""

		def scenario():
			conv_a = self._mk_conv()
			uid_a = self._mk_orphan_user_msg(conv_a)
			conv_b = self._mk_conv()
			uid_b = self._mk_orphan_user_msg(conv_b)

			real_heal = stale_scan._heal_or_error_orphan
			fired: list = []

			def racing(r, orig_attachments, orig_context):
				# Only this test's two rows reach the real function: the sweep's SELECT sees every
				# orphan on the site, and a live one must not be healed or errored by a test.
				if r["name"] not in (uid_a, uid_b):
					return False
				# Only row A races, and only on its first (and only, per scenario run)
				# visit: fresh_snapshot() already committed for this row by the time
				# the caller reaches this call, so open_read_view() here fixes the
				# FRESH transaction's snapshot before the competing write lands.
				if not fired and r["name"] == uid_a:
					fired.append(1)
					open_read_view()
					_compete(f"UPDATE `tab{MSG}` SET content=%s WHERE name=%s", ("raced", uid_a))
				return real_heal(r, orig_attachments, orig_context)

			with (
				snapshot_isolation_on(),
				as_job(),
				patch("frappe.utils.background_jobs.get_job_status", return_value=None),
				patch("frappe.utils.background_jobs.get_workers", return_value=[]),
				patch("jarvis.chat.api._redispatch_orphan", return_value={"ok": True, "dispatched": False}),
				patch.object(stale_scan, "_heal_or_error_orphan", racing),
				# Patched wholesale (not the specific logger instance) so this does not
				# depend on frappe.logger's internal per-name caching: every call in
				# this scope shares one MagicMock, so .return_value.warning collects
				# every warning() call regardless of how many times frappe.logger(name)
				# itself is invoked.
				patch("frappe.logger") as logger_fn,
			):
				errored = stale_scan._sweep_orphan_turns(frappe.utils.now_datetime())
			return uid_a, uid_b, errored, logger_fn

		def check(res):
			uid_a, uid_b, errored, logger_fn = res
			# Row A lost the race this cycle: caught, logged, skipped - NOT retried,
			# so its healing write never landed.
			self.assertEqual(int(frappe.db.get_value(MSG, uid_a, "was_recovered") or 0), 0)
			# Row B is unaffected: healed normally despite A's conflict.
			self.assertEqual(int(frappe.db.get_value(MSG, uid_b, "was_recovered") or 0), 1)
			self.assertEqual(logger_fn.return_value.warning.call_count, 1)

		assert_fix_closes_race(self, scenario, check)


# --------------------------------------------------------------------------- #
# P8 - turn_recovery.recover_pending_turns ceiling-backstop loop
# --------------------------------------------------------------------------- #


class TestRecoveryCeilingRace(_RaceCase):
	def test_a_conflict_on_the_ceiling_write_is_replayed_not_crashed(self):
		"""A row past the recovery ceiling races a competing writer touching the SAME
		Message row between the cycle's one SELECT and the ceiling backstop's own
		terminal write. The eligible loop below already has a per-row try/log; this
		is the same shape for the ceiling loop, which had none."""

		def scenario():
			conv = self._mk_conv()
			# Unique per run: the scenario runs twice and session_key is a unique column.
			frappe.db.set_value(CONV, conv, "session_key", f"sk_ceiling_{frappe.generate_hash(length=8)}")
			amsg = self._mk_msg(conv, role="assistant", content="", streaming=1, recovering=1)
			old = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-(CEILING_MINUTES + 5))
			frappe.db.set_value(MSG, amsg, "recovery_started_at", old, update_modified=False)
			frappe.db.commit()
			with snapshot_isolation_on(), as_job():
				open_read_view()  # the cycle's own SELECT, fixed early
				_compete(f"UPDATE `tab{MSG}` SET content=%s WHERE name=%s", ("raced", amsg))
				result = turn_recovery.recover_pending_turns()
			return amsg, result

		def check(res):
			amsg, result = res
			self.assertGreaterEqual(result.get("errored") or 0, 1)
			row = frappe.db.get_value(MSG, amsg, ["streaming", "recovering", "error"], as_dict=True)
			self.assertEqual(int(row.streaming), 0)
			self.assertEqual(int(row.recovering), 0)
			self.assertEqual(row.error, turn_recovery.CEILING_ERROR_MESSAGE)

		assert_fix_closes_race(self, scenario, check)
