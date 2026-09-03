"""Scheduled-macro dispatch: identity, caps and failure handling.

Covers the three defects the hourly ``run_due_macros`` cron shipped with:

* **#469** — it gated only on ``has_jarvis_access``, which returns True for
  ``Administrator`` and never reads ``User.enabled``, so an unattended turn could
  bind to a fully perm-bypassing identity or to an offboarded employee.
* **#468** — macro steps never passed the entitlement gate (``validate_can_send``)
  and had no run budget of any kind, so they drained the owner's quota without
  ever being refused by it.
* **#471** — a failed run advanced its schedule anyway, recorded nothing the owner
  could see, and could strand a run in ``running`` forever (no reaper).

Nothing here reaches a live gateway. Tests about the SCHEDULER's own decisions stub
``macros.run_macro`` wholesale and assert on which macros it dispatched; tests about
the #468 gate, which lives INSIDE ``run_macro``, stub only the turn dispatch
(``api._enqueue_turn``) so the real gate executes, and assert on the run rows it
did or did not leave behind. ``run_due_macros`` sweeps every due macro on the site,
so assertions are always scoped to this suite's own rows.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from jarvis.chat import macro_scheduler, macros

MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"

PFX = "msched"
OWNER_OK = "msched-owner@example.com"
OWNER_OFF = "msched-disabled@example.com"


def _ensure_user(email: str, *, enabled: int = 1) -> str:
	from jarvis.permissions import JARVIS_USER_ROLE, ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": PFX,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	if JARVIS_USER_ROLE not in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).add_roles(JARVIS_USER_ROLE)
	# enabled is set LAST and with a raw write: User.validate refuses some edits on
	# a disabled row, and add_roles on a disabled user is a no-op.
	frappe.db.set_value("User", email, {"enabled": enabled, "user_type": "System User"})
	return email


def _mk_macro(owner: str, tag: str, *, due: bool = True, enabled: int = 1, steps: int = 1):
	doc = frappe.get_doc(
		{
			"doctype": MACRO,
			"macro_name": f"{PFX}-{tag}",
			"enabled": enabled,
			"schedule_enabled": 1,
			"schedule_frequency": "daily",
			"steps": [{"prompt": f"step {i}"} for i in range(1, steps + 1)],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.set_value(MACRO, doc.name, "owner", owner, update_modified=False)
	frappe.db.set_value(
		MACRO,
		doc.name,
		"next_run_at",
		add_to_date(now_datetime(), hours=-1) if due else add_to_date(now_datetime(), days=1),
		update_modified=False,
	)
	frappe.db.commit()
	return doc


def _purge() -> None:
	"""Drop this suite's macros + their runs. Several paths under test COMMIT, so
	the per-test rollback does not clean up after them, and leaked macros count
	against ``MAX_MACROS_PER_OWNER`` (25) until every later insert throws."""
	for n in frappe.get_all(MACRO, filters={"macro_name": ["like", f"{PFX}-%"]}, pluck="name"):
		for run in frappe.get_all(RUN, filters={"macro": n}, pluck="name"):
			frappe.delete_doc(RUN, run, force=True, ignore_permissions=True)
		frappe.delete_doc(MACRO, n, force=True, ignore_permissions=True)
	for n in frappe.get_all("Notification Log", filters={"subject": ["like", f"%{PFX}-%"]}, pluck="name"):
		frappe.delete_doc("Notification Log", n, force=True, ignore_permissions=True)
	frappe.db.commit()


def _runs_for(macro_name: str) -> list:
	return frappe.get_all(
		RUN,
		filters={"macro": macro_name},
		fields=["name", "status", "error", "owner", "trigger"],
		order_by="creation asc",
	)


class MacroSchedulerBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(OWNER_OK, enabled=1)
		_ensure_user(OWNER_OFF, enabled=0)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		_purge()

	def tearDown(self):
		frappe.db.rollback()
		_purge()
		super().tearDown()

	def _run_due(self):
		"""Run the cron with dispatch stubbed; returns the macro names it dispatched."""
		with patch("jarvis.chat.macros.run_macro", return_value={"ok": True}) as mock_run:
			macro_scheduler.run_due_macros()
		return [c.args[0] for c in mock_run.call_args_list]


# --------------------------------------------------------------------------- #
# #469 — the unattended-identity guard
# --------------------------------------------------------------------------- #
class TestScheduledMacroIdentity(MacroSchedulerBase):
	def test_administrator_owned_macro_is_refused(self):
		m = _mk_macro("Administrator", "admin-owned")
		self.assertNotIn(m.name, self._run_due(), "scheduler bound an unattended turn to Administrator")

	def test_disabled_owner_macro_is_refused(self):
		m = _mk_macro(OWNER_OFF, "disabled-owner")
		# The premise of the defect: the OLD gate still says this identity is fine.
		from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

		self.assertTrue(has_jarvis_access(OWNER_OFF), "premise changed: has_jarvis_access now filters")
		self.assertFalse(is_valid_unattended_owner(OWNER_OFF))
		self.assertNotIn(m.name, self._run_due(), "offboarding did not revoke scheduled ERP access")

	def test_enabled_jarvis_owner_still_runs(self):
		m = _mk_macro(OWNER_OK, "good-owner")
		self.assertIn(m.name, self._run_due(), "the guard over-reached and refused a legitimate owner")

	def test_refused_macro_does_not_busy_refire(self):
		m = _mk_macro("Administrator", "admin-advance")
		self._run_due()
		nxt = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertGreater(nxt, now_datetime(), "a refused macro stayed due and would re-fire hourly")

	def test_refusal_is_recorded_as_a_failed_run(self):
		m = _mk_macro(OWNER_OFF, "disabled-recorded")
		self._run_due()
		runs = _runs_for(m.name)
		self.assertEqual([r.status for r in runs], ["failed"])
		self.assertEqual(runs[0].owner, OWNER_OFF, "the failed row is not visible to the owner")
		self.assertIn("unattended", runs[0].error)


# --------------------------------------------------------------------------- #
# #471 — failures are durable, honest, and terminalized
# --------------------------------------------------------------------------- #
class TestScheduledMacroFailures(MacroSchedulerBase):
	def test_dispatch_failure_does_not_advance_the_schedule(self):
		m = _mk_macro(OWNER_OK, "raises")
		before = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		after = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertEqual(after, before, "a failed run advanced its schedule and lost the slot")
		self.assertGreater(now_datetime(), after, "the missed slot is no longer due, so it cannot retry")

	def test_dispatch_failure_is_visible_as_failed_not_successful(self):
		m = _mk_macro(OWNER_OK, "raises-visible")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		runs = _runs_for(m.name)
		self.assertEqual([r.status for r in runs], ["failed"], "the failure left no owner-visible trace")
		self.assertEqual(runs[0].trigger, "scheduled")
		self.assertEqual(runs[0].owner, OWNER_OK)
		# The UI reads last_run_at as "last run"; a run that never happened must not
		# stamp it (the "actively misleading" limb of #471).
		self.assertFalse(
			frappe.db.get_value(MACRO, m.name, "last_run_at"),
			"last_run_at was stamped for a run that never executed",
		)

	def test_dispatch_failure_notifies_the_owner(self):
		_mk_macro(OWNER_OK, "raises-notify")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		notes = frappe.get_all(
			"Notification Log",
			filters={"for_user": OWNER_OK, "subject": ["like", f"%{PFX}-raises-notify%"]},
			pluck="name",
		)
		self.assertTrue(notes, "the owner got no signal at all")

	def test_dispatch_raising_after_the_run_row_exists_leaves_no_orphan(self):
		# run_macro COMMITS the conversation and the run row, then dispatches. When the
		# dispatch raises (gateway down, _ensure_session_key throws) no turn exists, so
		# the chaining hook that terminalizes a run never fires. Without the fix that
		# row sat `running` for three hours until the sweep, AND the scheduler recorded
		# a second failed row, showing two failures for one missed slot.
		m = _mk_macro(OWNER_OK, "orphan", steps=1)
		before = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		with patch("jarvis.chat.api._enqueue_turn", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		runs = _runs_for(m.name)
		self.assertEqual(len(runs), 1, f"expected exactly one run row for one slot, got {runs}")
		self.assertEqual(runs[0].status, "failed", "the run row was left stranded in `running`")
		self.assertIn("dispatch", runs[0].error.lower())
		# It is the row that carries the conversation, not a bare scheduler record.
		self.assertTrue(frappe.db.get_value(RUN, runs[0].name, "conversation"))
		# Transient: the slot stays due so the next tick retries it.
		after = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertEqual(after, before, "a dispatch failure cost the macro its slot")

	def test_notify_owner_never_escapes_and_aborts_the_sweep(self):
		# The scheduler's per-macro loop has no outer guard, so an escape from
		# notify_owner would abort the sweep for every REMAINING due macro.
		with patch("jarvis.permissions.is_valid_unattended_owner", side_effect=RuntimeError("db blip")):
			macros.notify_owner(OWNER_OK, subject="x", body="y")  # must not raise

	def test_successful_run_stamps_last_run_at(self):
		m = _mk_macro(OWNER_OK, "success")
		self._run_due()
		self.assertTrue(frappe.db.get_value(MACRO, m.name, "last_run_at"))
		self.assertEqual(_runs_for(m.name), [], "a successful dispatch wrote a spurious failed row")

	def test_disabled_macro_consumes_the_slot_without_claiming_a_run(self):
		m = _mk_macro(OWNER_OK, "switched-off", enabled=0)
		self.assertNotIn(m.name, self._run_due(), "a disabled macro was dispatched")
		nxt = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertGreater(nxt, now_datetime(), "a disabled macro stayed due and re-fires hourly forever")
		self.assertFalse(
			frappe.db.get_value(MACRO, m.name, "last_run_at"),
			"a disabled macro stamped last_run_at, so the UI claims it ran",
		)
		self.assertEqual(_runs_for(m.name), [], "a deliberately disabled macro is not a failure")


# --------------------------------------------------------------------------- #
# #468 — the entitlement gate and the unattended step budget
# --------------------------------------------------------------------------- #
class TestScheduledMacroCaps(MacroSchedulerBase):
	def _run_due_gated(self):
		"""Run the cron with only the TURN dispatch stubbed, so ``run_macro``'s own
		gate really executes. A refused run leaves exactly one ``failed`` row and no
		conversation; a dispatched one leaves a ``running`` row."""
		with patch("jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}):
			macro_scheduler.run_due_macros()

	def _mk_consumed_run(self, macro_name: str, steps: int, *, trigger="scheduled", status="completed"):
		"""A prior run that already spent ``steps`` turns this month."""
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"macro": macro_name,
				"status": status,
				"current_step": steps,
				"total_steps": steps,
				"trigger": trigger,
				"started_at": frappe.utils.now(),
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		frappe.db.set_value(RUN, run.name, "owner", OWNER_OK, update_modified=False)
		frappe.db.commit()
		return run.name

	def test_over_cap_run_is_refused_recorded_and_consumes_the_slot(self):
		m = _mk_macro(OWNER_OK, "over-cap")
		with patch("jarvis.chat.policy.validate_can_send", return_value=(False, "usage_limit")):
			self._run_due_gated()
		runs = _runs_for(m.name)
		self.assertEqual([r.status for r in runs], ["failed"], "a macro ran straight through the cap")
		self.assertIn("usage limit", runs[0].error)
		self.assertEqual(runs[0].owner, OWNER_OK)
		# usage_limit does not clear inside the hour, so the slot is consumed rather
		# than relogging the same refusal 24 times a day.
		nxt = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertGreater(nxt, now_datetime())

	def test_ungated_run_still_dispatches(self):
		m = _mk_macro(OWNER_OK, "under-cap")
		self._run_due_gated()
		self.assertEqual([r.status for r in _runs_for(m.name)], ["running"], "the gate over-reached")

	def test_suspended_subscription_refuses_the_run(self):
		m = _mk_macro(OWNER_OK, "suspended")
		with patch("jarvis.chat.policy.validate_can_send", return_value=(False, "subscription_suspended")):
			self._run_due_gated()
		runs = _runs_for(m.name)
		self.assertEqual([r.status for r in runs], ["failed"])
		self.assertIn("subscription", runs[0].error)

	def test_transient_block_leaves_the_slot_due_for_a_retry(self):
		m = _mk_macro(OWNER_OK, "rolling-out")
		before = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		with patch("jarvis.chat.policy.validate_can_send", return_value=(False, "release_update_required")):
			self._run_due_gated()
		self.assertEqual([r.status for r in _runs_for(m.name)], ["failed"])
		after = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertEqual(after, before, "a rollout that clears in minutes cost the macro its slot")

	def test_insufficient_workers_block_leaves_the_slot_due_for_a_retry(self):
		# #468's TRANSIENT shape now also covers a confidently-zero-workers snapshot:
		# it self-heals within the worker lane's debounce, so the slot must stay due
		# for the next hourly tick rather than being consumed like an entitlement
		# refusal.
		m = _mk_macro(OWNER_OK, "workers-low")
		before = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		with patch("jarvis.chat.policy.validate_can_send", return_value=(False, "insufficient_workers")):
			self._run_due_gated()
		self.assertEqual([r.status for r in _runs_for(m.name)], ["failed"])
		after = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertEqual(after, before, "a worker shortage that clears in seconds cost the macro its slot")

	def test_the_gate_creates_nothing_before_refusing(self):
		# A refused run must leave no conversation and no intro message behind —
		# only the scheduler's own failed record.
		m = _mk_macro(OWNER_OK, "no-residue")
		convs_before = frappe.db.count("Jarvis Conversation")
		with patch("jarvis.chat.policy.validate_can_send", return_value=(False, "usage_limit")):
			self._run_due_gated()
		self.assertEqual(frappe.db.count("Jarvis Conversation"), convs_before)
		self.assertFalse(_runs_for(m.name)[0].get("conversation"))

	def test_step_budget_refuses_once_the_month_is_spent(self):
		m = _mk_macro(OWNER_OK, "budget", steps=3)
		frappe.db.set_single_value("Jarvis Settings", "macro_step_budget_monthly", 40)
		self.addCleanup(frappe.db.set_single_value, "Jarvis Settings", "macro_step_budget_monthly", None)
		spent = self._mk_consumed_run(m.name, 40 - macros._scheduled_steps_this_month(OWNER_OK) - 1)
		self.addCleanup(frappe.delete_doc, RUN, spent, force=True, ignore_permissions=True)
		# One step of headroom left, and this macro wants three.
		self.assertFalse(macros._over_step_budget(OWNER_OK, 1))
		self.assertTrue(macros._over_step_budget(OWNER_OK, 3))
		self._run_due_gated()
		runs = _runs_for(m.name)
		failed = [r for r in runs if r.status == "failed"]
		self.assertTrue(failed, "the unattended step budget did not bind")
		self.assertNotIn("running", [r.status for r in runs])
		self.assertIn("budget", failed[0].error)

	def test_meter_counts_steps_not_runs_and_excludes_refusals(self):
		m = _mk_macro(OWNER_OK, "meter", steps=3)
		base = macros._scheduled_steps_this_month(OWNER_OK)
		spent = self._mk_consumed_run(m.name, 7)
		self.addCleanup(frappe.delete_doc, RUN, spent, force=True, ignore_permissions=True)
		self.assertEqual(macros._scheduled_steps_this_month(OWNER_OK), base + 7)
		# A manual run is not unattended work, so it does not spend the budget.
		manual = self._mk_consumed_run(m.name, 5, trigger="manual")
		self.addCleanup(frappe.delete_doc, RUN, manual, force=True, ignore_permissions=True)
		self.assertEqual(macros._scheduled_steps_this_month(OWNER_OK), base + 7)
		# And a refusal must never make the cap self-perpetuating. It carries
		# current_step=0, so it contributes nothing WITHOUT filtering on status.
		refused = self._mk_consumed_run(m.name, 0, status="failed")
		self.addCleanup(frappe.delete_doc, RUN, refused, force=True, ignore_permissions=True)
		self.assertEqual(macros._scheduled_steps_this_month(OWNER_OK), base + 7)

	def test_partly_failed_run_still_counts_the_steps_it_billed(self):
		# A run that failed HALFWAY really dispatched and billed its completed steps
		# (stop_on_error, the capacity-attempt cap, and the stale sweep all produce
		# exactly that). Excluding failed rows wholesale would erase those turns, so an
		# owner whose macros keep failing could spend past the budget indefinitely.
		m = _mk_macro(OWNER_OK, "part-failed", due=False, steps=3)
		base = macros._scheduled_steps_this_month(OWNER_OK)
		partial = self._mk_consumed_run(m.name, 2, status="failed")
		self.addCleanup(frappe.delete_doc, RUN, partial, force=True, ignore_permissions=True)
		self.assertEqual(macros._scheduled_steps_this_month(OWNER_OK), base + 2)

	def test_stopped_run_still_counts_the_steps_it_billed(self):
		m = _mk_macro(OWNER_OK, "stopped-run", due=False, steps=3)
		base = macros._scheduled_steps_this_month(OWNER_OK)
		stopped = self._mk_consumed_run(m.name, 2, status="stopped")
		self.addCleanup(frappe.delete_doc, RUN, stopped, force=True, ignore_permissions=True)
		self.assertEqual(macros._scheduled_steps_this_month(OWNER_OK), base + 2)

	def test_budget_floor_clamps_up_and_never_widens(self):
		self.addCleanup(frappe.db.set_single_value, "Jarvis Settings", "macro_step_budget_monthly", None)
		# Unset/blank means "no opinion" and takes the default.
		frappe.db.set_single_value("Jarvis Settings", "macro_step_budget_monthly", 0)
		self.assertEqual(
			macros._scheduled_step_budget_monthly(), macros.DEFAULT_SCHEDULED_STEP_BUDGET_MONTHLY
		)
		# A deliberately tight cap is clamped UP to the floor, never widened to the
		# default: an admin who configures 10 must not silently get 500.
		frappe.db.set_single_value("Jarvis Settings", "macro_step_budget_monthly", 10)
		self.assertEqual(macros._scheduled_step_budget_monthly(), macros.MIN_SCHEDULED_STEP_BUDGET_MONTHLY)
		# A value above the floor is honoured exactly.
		frappe.db.set_single_value("Jarvis Settings", "macro_step_budget_monthly", 77)
		self.assertEqual(macros._scheduled_step_budget_monthly(), 77)

	def test_manual_run_is_entitlement_gated_but_not_budget_gated(self):
		m = _mk_macro(OWNER_OK, "manual-gate", due=False)
		with patch("jarvis.chat.policy.validate_can_send", return_value=(False, "usage_limit")):
			with self.assertRaises(frappe.ValidationError):
				macros.run_macro(m.name, trigger="manual")
		# The budget is scheduled-only: an attended click is never refused by it.
		with patch.object(macros, "_over_step_budget", return_value=True):
			self.assertIsNone(macros.entitlement_block(OWNER_OK, steps=3, trigger="manual"))
			self.assertEqual(
				macros.entitlement_block(OWNER_OK, steps=3, trigger="scheduled"),
				macros.BLOCK_STEP_BUDGET,
			)


# --------------------------------------------------------------------------- #
# #471 — the stale-run reaper, and what it must NOT reap
# --------------------------------------------------------------------------- #
class TestStaleMacroRunReaper(MacroSchedulerBase):
	def _mk_run(self, tag: str, status: str, *, age_s: int):
		m = _mk_macro(OWNER_OK, tag, due=False)
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"macro": m.name,
				"status": status,
				"current_step": 0,
				"total_steps": 1,
				"trigger": "scheduled",
				"started_at": frappe.utils.now(),
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		stamp = add_to_date(now_datetime(), seconds=-age_s)
		frappe.db.sql(
			"UPDATE `tabJarvis Macro Run` SET modified=%(t)s, owner=%(o)s WHERE name=%(n)s",
			{"t": stamp, "o": OWNER_OK, "n": run.name},
		)
		frappe.db.commit()
		return run.name

	def test_stranded_running_run_is_terminalized(self):
		name = self._mk_run("stranded", "running", age_s=macros.STALE_RUN_AFTER_SECONDS + 600)
		self.assertEqual(macros.reap_stale_macro_runs(), 1)
		row = frappe.db.get_value(RUN, name, ["status", "error", "finished_at"], as_dict=True)
		self.assertEqual(row.status, "failed")
		self.assertTrue(row.error, "a reaped run must say WHY, not carry an empty error")
		self.assertTrue(row.finished_at)

	def test_parked_waiting_capacity_run_is_never_reaped(self):
		# #470 lets a run sit in waiting_capacity for ~100 min legitimately; the
		# reaper must not confuse deliberately parked with stranded. Aged far past
		# the cutoff so only the STATUS allowlist can be saving it.
		name = self._mk_run("parked", "waiting_capacity", age_s=macros.STALE_RUN_AFTER_SECONDS * 4)
		self.assertEqual(macros.reap_stale_macro_runs(), 0)
		self.assertEqual(frappe.db.get_value(RUN, name, "status"), "waiting_capacity")

	def test_recently_progressing_run_is_never_reaped(self):
		name = self._mk_run("progressing", "running", age_s=macros.STALE_RUN_AFTER_SECONDS // 2)
		self.assertEqual(macros.reap_stale_macro_runs(), 0)
		self.assertEqual(frappe.db.get_value(RUN, name, "status"), "running")

	def test_run_that_advanced_after_the_scan_is_left_alone(self):
		# The candidate snapshot is inherently stale: a step can advance between the
		# scan and the transition. The re-read under the lock must catch that.
		name = self._mk_run("raced", "running", age_s=macros.STALE_RUN_AFTER_SECONDS + 600)
		with patch.object(macros, "_stale_run_candidates", return_value=[name]):
			frappe.db.sql(
				"UPDATE `tabJarvis Macro Run` SET modified=%(t)s WHERE name=%(n)s",
				{"t": now_datetime(), "n": name},
			)
			frappe.db.commit()
			self.assertEqual(macros.reap_stale_macro_runs(), 0)
		self.assertEqual(frappe.db.get_value(RUN, name, "status"), "running")


# --------------------------------------------------------------------------- #
# #472 — schedule_time is validated, and one bad row cannot abort the sweep
# --------------------------------------------------------------------------- #
class TestScheduleTimeValidation(MacroSchedulerBase):
	"""A ``schedule_time`` Frappe never range-checks reached
	``datetime.replace(hour=...)`` inside ``validate()``, so the save 500'd, and a
	value already on a row aborted the WHOLE hourly sweep."""

	def _save(self, tag: str, schedule_time):
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-{tag}",
				"enabled": 1,
				"schedule_enabled": 1,
				"schedule_frequency": "daily",
				"schedule_time": schedule_time,
				"steps": [{"prompt": "p1"}],
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc

	def test_out_of_range_schedule_time_is_refused_cleanly(self):
		# frappe.ValidationError is what the SPA renders as a field error; a bare
		# ValueError from the arithmetic is the 500 this closes.
		for bad in ("99:00:00", "-01:00:00", "24:00:00", "12:99:00", "not-a-time", "1:2:3:4"):
			with self.subTest(bad=bad):
				with self.assertRaises(frappe.ValidationError):
					self._save(f"bad-{abs(hash(bad))}", bad)

	def test_a_bad_time_is_refused_even_with_the_schedule_off(self):
		# The latent limb: with schedule_enabled=0 the controller never computed a
		# next run, so the garbage persisted and armed the sweep for whenever the
		# schedule was turned on.
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-off-but-bad",
				"enabled": 1,
				"schedule_enabled": 0,
				"schedule_time": "99:00:00",
				"steps": [{"prompt": "p1"}],
			}
		)
		doc.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_valid_schedule_times_are_accepted_and_scheduled(self):
		for good in ("00:00:00", "09:30:00", "23:59:59"):
			with self.subTest(good=good):
				doc = self._save(f"ok-{good.replace(':', '')}", good)
				self.assertTrue(doc.next_run_at, "a valid time must still produce a next_run_at")

	def test_bad_persisted_time_does_not_abort_the_sweep(self):
		# Raw-write past the new validation to recreate a row saved before it existed,
		# then force it to the FRONT of the sweep (Jarvis Macro sorts modified DESC) so
		# a sweep that dies on it cannot reach the healthy macro behind it.
		bad = _mk_macro(OWNER_OK, "poisoned")
		good = _mk_macro(OWNER_OK, "healthy")
		frappe.db.sql(
			"UPDATE `tabJarvis Macro` SET schedule_time='99:00:00', modified=%(t)s WHERE name=%(n)s",
			{"t": add_to_date(now_datetime(), days=1), "n": bad.name},
		)
		frappe.db.commit()

		dispatched = self._run_due()

		self.assertIn(good.name, dispatched, "one bad row aborted the sweep for every other macro")
		self.assertIn(bad.name, dispatched, "the bad row should fall back, not be skipped")
		# The poisoned row's slot still advanced, on the 09:00 fallback an unset time gets.
		nxt = get_datetime(frappe.db.get_value(MACRO, bad.name, "next_run_at"))
		self.assertEqual((nxt.hour, nxt.minute), (9, 0))

	def test_one_exploding_macro_does_not_abort_the_sweep(self):
		# The structural guarantee, independent of schedule_time: whatever a single
		# row raises anywhere in its handling, every OTHER due macro still runs.
		first = _mk_macro(OWNER_OK, "explodes")
		second = _mk_macro(OWNER_OK, "survivor")
		frappe.db.sql(
			"UPDATE `tabJarvis Macro` SET modified=%(t)s WHERE name=%(n)s",
			{"t": add_to_date(now_datetime(), days=1), "n": first.name},
		)
		frappe.db.commit()

		def boom(m, *a, **kw):
			if m.name == first.name:
				raise RuntimeError("bookkeeping blew up")

		with (
			patch("jarvis.chat.macros.run_macro", return_value={"ok": True}) as mock_run,
			patch.object(macro_scheduler, "_settle", side_effect=boom),
		):
			macro_scheduler.run_due_macros()
		self.assertIn(second.name, [c.args[0] for c in mock_run.call_args_list])

	def test_compute_next_run_never_raises_on_a_stored_value(self):
		for bad in ("99:00:00", "-01:00:00", "838:59:59", "garbage"):
			with self.subTest(bad=bad):
				nxt = macro_scheduler.compute_next_run("daily", bad)
				self.assertEqual((nxt.hour, nxt.minute), (9, 0))


class TestScheduleDayOfMonthValidation(MacroSchedulerBase):
	"""The #653 twin of ``TestScheduleTimeValidation`` above: ``schedule_day_of_month``
	is an Int field Frappe never range-checks on its own, so a bad value must be
	refused the same way an out-of-range ``schedule_time`` is."""

	def _save(self, tag: str, day_of_month, frequency: str = "monthly"):
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-{tag}",
				"enabled": 1,
				"schedule_enabled": 1,
				"schedule_frequency": frequency,
				"schedule_day_of_month": day_of_month,
				"steps": [{"prompt": "p1"}],
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc

	def test_out_of_range_day_of_month_is_refused_cleanly(self):
		# 0 is deliberately NOT here: Frappe coerces a blank Int field to 0 (not
		# None), so the validator exempts it as "unset", not "the 0th day" - see
		# test_unset_day_of_month_is_exempt below.
		for bad in (32, -1, 100):
			with self.subTest(bad=bad):
				with self.assertRaises(frappe.ValidationError):
					self._save(f"bad-{bad}", bad)

	def test_valid_days_of_month_are_accepted_and_scheduled(self):
		for good in (1, 15, 31):
			with self.subTest(good=good):
				doc = self._save(f"ok-{good}", good)
				self.assertTrue(doc.next_run_at, "a valid day must still produce a next_run_at")

	def test_unset_day_of_month_is_exempt(self):
		# An Int field left blank reaches validate() as 0, not None - "not set",
		# never "the 0th day" (#653's exemption, mirroring the empty-string exemption
		# schedule_time gets). Both the None a caller might pass and the literal 0
		# Frappe coerces it to must be accepted, not refused.
		for unset in (None, 0):
			with self.subTest(unset=unset):
				doc = self._save(f"unset-{unset}", unset)
				self.assertTrue(doc.next_run_at)

	def test_out_of_range_weekday_select_is_refused_by_the_framework(self):
		# schedule_weekday is a Select field - Frappe's own _validate_selects()
		# refuses a value outside its options list, so no bespoke throw is needed
		# here (unlike the Int field above).
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-bad-weekday",
				"enabled": 1,
				"schedule_enabled": 1,
				"schedule_frequency": "weekly",
				"schedule_weekday": "Blursday",
				"steps": [{"prompt": "p1"}],
			}
		)
		doc.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			doc.insert()


class TestComputeNextRunAnchors(FrappeTestCase):
	"""Pure arithmetic, no DB writes needed - the anchors compute_next_run gained
	for #653 (weekly weekday, monthly day-of-month), and the TOTAL guarantee
	(#472) extended to cover them: a missing, None, or garbage anchor value must
	fall back to the plain +7-days / +1-month advance and never raise."""

	def test_weekly_anchor_skips_to_next_occurrence_when_todays_has_passed(self):
		# Monday 10:00, target Monday 09:00 -> already passed today, so next Monday.
		nxt = macro_scheduler.compute_next_run(
			"weekly", "09:00:00", from_dt="2026-09-07 10:00:00", weekday="Monday"
		)
		self.assertEqual(nxt.strftime("%A"), "Monday")
		self.assertEqual(nxt.date().isoformat(), "2026-09-14")

	def test_weekly_anchor_same_day_when_the_time_has_not_passed_yet(self):
		nxt = macro_scheduler.compute_next_run(
			"weekly", "09:00:00", from_dt="2026-09-07 08:00:00", weekday="Monday"
		)
		self.assertEqual(nxt.date().isoformat(), "2026-09-07")

	def test_weekly_anchor_picks_a_different_weekday(self):
		nxt = macro_scheduler.compute_next_run(
			"weekly", "09:00:00", from_dt="2026-09-07 08:00:00", weekday="Friday"
		)
		self.assertEqual(nxt.strftime("%A"), "Friday")
		self.assertEqual(nxt.date().isoformat(), "2026-09-11")

	def test_monthly_anchor_clamps_day_31_in_february_then_returns_to_31_in_march(self):
		# 2026 is not a leap year: Feb has 28 days.
		feb = macro_scheduler.compute_next_run(
			"monthly", "09:00:00", from_dt="2026-01-31 09:00:01", day_of_month=31
		)
		self.assertEqual(feb.date().isoformat(), "2026-02-28")

		mar = macro_scheduler.compute_next_run("monthly", "09:00:00", from_dt=str(feb), day_of_month=31)
		self.assertEqual(mar.date().isoformat(), "2026-03-31")

	def test_monthly_anchor_clamps_to_29_in_a_leap_february(self):
		nxt = macro_scheduler.compute_next_run(
			"monthly", "09:00:00", from_dt="2028-01-31 09:00:01", day_of_month=31
		)
		self.assertEqual(nxt.date().isoformat(), "2028-02-29")

	def test_no_anchor_falls_back_to_the_plain_advance(self):
		# Same shape every pre-#653 caller still uses: weekday/day_of_month omitted.
		weekly = macro_scheduler.compute_next_run("weekly", "09:00:00", from_dt="2026-09-07 10:00:00")
		self.assertEqual(weekly.date().isoformat(), "2026-09-14")
		monthly = macro_scheduler.compute_next_run("monthly", "09:00:00", from_dt="2026-01-31 10:00:00")
		self.assertEqual(monthly.date().isoformat(), "2026-02-28")

	def test_garbage_anchors_never_raise_and_fall_back_to_the_plain_advance(self):
		for weekday in ("Blursday", 0, 8, "", [], {}):
			with self.subTest(weekday=weekday):
				nxt = macro_scheduler.compute_next_run(
					"weekly", "09:00:00", from_dt="2026-09-07 10:00:00", weekday=weekday
				)
				self.assertEqual(nxt.date().isoformat(), "2026-09-14")
		for day in (0, 32, -1, "abc", None, [], {}):
			with self.subTest(day=day):
				nxt = macro_scheduler.compute_next_run(
					"monthly", "09:00:00", from_dt="2026-01-31 10:00:00", day_of_month=day
				)
				self.assertEqual(nxt.date().isoformat(), "2026-02-28")

	def test_a_garbage_schedule_time_still_never_raises_alongside_a_valid_anchor(self):
		# #472's TOTAL guarantee and #653's anchors compose: a bad time AND a good
		# anchor together still fall back cleanly (09:00 default time).
		nxt = macro_scheduler.compute_next_run(
			"weekly", "not-a-time", from_dt="2026-09-07 10:00:00", weekday="Monday"
		)
		self.assertEqual((nxt.hour, nxt.minute), (9, 0))


class TestDefaultScheduleAnchors(FrappeTestCase):
	"""``agents_api.install_agent`` resolves a listing's ``default_schedule`` JSON
	into the two anchors a fresh installation is born with. A first version of
	this reimplemented weekday normalization as a bespoke string-only check
	instead of reusing ``macro_scheduler._normalize_weekday`` (the reader every
	OTHER weekday input - the SPA, ``set_schedule``, both DocType controllers -
	goes through), so a listing authored with the ISO-int form (e.g. 3 for
	Wednesday) silently landed with no weekday at all. These call the exact
	functions ``install_agent`` calls, no DB fixture needed."""

	def test_int_weekday_in_default_schedule_resolves_to_its_name(self):
		from jarvis.chat.agents_api import _default_schedule_weekday

		# ISO weekday: 1=Monday .. 7=Sunday.
		cases = {1: "Monday", 3: "Wednesday", 7: "Sunday"}
		for iso, name in cases.items():
			with self.subTest(iso=iso):
				self.assertEqual(_default_schedule_weekday({"schedule_weekday": iso}), name)

	def test_weekday_name_in_default_schedule_still_works(self):
		from jarvis.chat.agents_api import _default_schedule_weekday

		self.assertEqual(_default_schedule_weekday({"schedule_weekday": "Friday"}), "Friday")
		self.assertEqual(_default_schedule_weekday({"schedule_weekday": "friday"}), "Friday")

	def test_garbage_weekday_in_default_schedule_resolves_to_none(self):
		from jarvis.chat.agents_api import _default_schedule_weekday

		for bad in ("Blursday", 0, 8, "", None, [], {}):
			with self.subTest(bad=bad):
				self.assertIsNone(_default_schedule_weekday({"schedule_weekday": bad}))

	def test_day_of_month_in_default_schedule(self):
		from jarvis.chat.agents_api import _default_schedule_day_of_month

		self.assertEqual(_default_schedule_day_of_month({"schedule_day_of_month": 15}), 15)
		for bad in (0, 32, -1, "abc", None):
			with self.subTest(bad=bad):
				self.assertIsNone(_default_schedule_day_of_month({"schedule_day_of_month": bad}))

	def test_install_agent_applies_an_int_weekday_default_schedule(self):
		# End-to-end through install_agent itself: a listing whose default_schedule
		# carries an ISO-int weekday must land on the installation AS ITS NAME, not
		# be silently dropped.
		from jarvis.chat import agents_api
		from jarvis.tests._agent_access import allow_listing_for, clear_listing_access

		listing_name = frappe.db.get_value("Jarvis Agent Listing", {"agent_slug": "close-auditor"}, "name")
		if not listing_name:
			self.skipTest("close-auditor listing not present on this site")
		owner = _ensure_user("msched-int-weekday@example.com", enabled=1)
		original_schedule = frappe.db.get_value("Jarvis Agent Listing", listing_name, "default_schedule")
		frappe.db.set_value(
			"Jarvis Agent Listing",
			listing_name,
			"default_schedule",
			frappe.as_json({"schedule_enabled": 0, "schedule_frequency": "weekly", "schedule_weekday": 3}),
			update_modified=False,
		)
		frappe.db.commit()
		# Agent access is deny-by-default (jarvis#1062 / PR #1095) - grant this
		# test's user access to the listing the same way test_agents_marketplace
		# and test_platform_agents_api_hardening do, or install_agent refuses
		# with a PermissionError before ever reaching the schedule handling this
		# test is about. _ensure_user already grants the Jarvis User role.
		allow_listing_for(listing_name, roles=["Jarvis User"])
		original_user = frappe.session.user
		try:
			frappe.set_user(owner)
			frappe.db.delete("Jarvis Agent Installation", {"owner": owner, "agent": listing_name})
			frappe.db.commit()
			agents_api.install_agent("close-auditor")
			weekday = frappe.db.get_value(
				"Jarvis Agent Installation", {"owner": owner, "agent": listing_name}, "schedule_weekday"
			)
			self.assertEqual(weekday, "Wednesday")
		finally:
			frappe.set_user(original_user)
			clear_listing_access(listing_name)
			frappe.db.delete("Jarvis Agent Installation", {"owner": owner, "agent": listing_name})
			frappe.db.set_value(
				"Jarvis Agent Listing",
				listing_name,
				"default_schedule",
				original_schedule,
				update_modified=False,
			)
			frappe.db.commit()
