"""The scheduled-agent sweep runs every five minutes (the agent twin of the macro
sweep's fix, jarvis#1568, admin-v2#675).

An hourly sweep started a 10:15 audit at 11:00. Moving it to five minutes is only safe
because of what these tests pin down:

  * the slot is CLAIMED before the launch, so a later sweep never re-dispatches it;
  * two sweeps that overlap launch the slot once;
  * a slot whose launch failed is retried 55 minutes later, again after each failed
    retry until its next natural time, and not on every five-minute sweep (which
    would record a failed run and notify the owner twelve times an hour);
  * one sweep dispatches a bounded number of installations, oldest slot first.

Run:
  bench --site test_site run-tests --app jarvis --module jarvis.tests.test_agent_sweep_cadence
"""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, get_datetime, now_datetime
from rq.timeouts import JobTimeoutException

from jarvis import admin_client
from jarvis.chat import agent_models, agent_scheduler
from jarvis.tests._agent_access import allow_listing_for
from jarvis.tests.test_agent_dispatch_idempotency import (
	INSTALLATION,
	NOTIFICATION,
	SESSION,
	SLUG,
	DispatchIdempotencyTestCase,
	_mk_user,
)

SWEEP = "jarvis.chat.agent_scheduler.run_due_agent_audits"
SECOND_OWNER = "sweep-cadence-second@example.com"


def _unreachable(**kw):
	"""A confirmed refusal from admin: the launch fails, nothing is running."""
	raise admin_client.AdminUnreachableError("admin returned a 400 error")


class TestAgentSweepCadence(DispatchIdempotencyTestCase):
	def setUp(self):
		super().setUp()
		# A site whose schema predates the min-model setting cannot read it, and every
		# launch then fails in the model gate before it is dispatched. Not what this
		# module is about; a migrated site has the field and runs the real gate.
		if not frappe.get_meta("Jarvis Settings").has_field("enforce_agent_min_model"):
			patcher = patch.object(agent_models, "is_enforced", return_value=False)
			patcher.start()
			self.addCleanup(patcher.stop)

	def _notifications(self) -> list:
		return frappe.get_all(NOTIFICATION, filters={"for_user": self.owner}, pluck="name")

	def _the_wait_passes(self, inst: str) -> None:
		frappe.db.set_value(
			INSTALLATION, inst, "next_run_at", add_to_date(now_datetime(), minutes=-1), update_modified=False
		)
		frappe.db.commit()

	def _schedule_far_from_now(self, inst: str) -> None:
		"""A daily slot about twelve hours away, so a retry (under an hour away) and the
		next natural slot can never be mistaken for each other, whatever the time of day."""
		far = add_to_date(now_datetime(), hours=12)
		frappe.db.set_value(
			INSTALLATION, inst, "schedule_time", far.strftime("%H:%M:00"), update_modified=False
		)
		frappe.db.commit()

	def _assert_retry_scheduled(self, inst: str, before) -> None:
		nxt = get_datetime(self._next_run_at(inst))
		self.assertGreater(nxt, before, "the slot was left due, so every sweep retries it")
		self.assertLessEqual(
			nxt,
			add_to_date(now_datetime(), seconds=agent_scheduler._RETRY_AFTER_S + 5),
			"the retry is about an hour out, not the next day",
		)

	# --- registration ------------------------------------------------------- #

	def test_the_sweep_is_registered_every_five_minutes_not_hourly(self):
		from jarvis import hooks

		self.assertIn(SWEEP, hooks.scheduler_events["cron"]["*/5 * * * *"])
		self.assertNotIn(
			SWEEP,
			hooks.scheduler_events["hourly"],
			"an hourly sweep starts a 10:15 audit at 11:00",
		)

	# --- claim before dispatch, no double dispatch ---------------------------- #

	def test_the_slot_is_claimed_before_the_audit_is_dispatched(self):
		inst = self._due_install()
		seen = {}

		def _dispatch(**kw):
			seen["next_run_at"] = get_datetime(frappe.db.get_value(INSTALLATION, inst, "next_run_at"))
			return self._dispatch_stub(**kw)

		self._sweep(dispatch=_dispatch)

		self.assertEqual(len(self._dispatched_for(inst)), 1)
		self.assertGreater(
			seen["next_run_at"], now_datetime(), "the slot was still due while the audit was dispatching"
		)

	def test_a_sweep_overlapping_a_dispatch_does_not_launch_the_slot_again(self):
		"""A second sweep fires while the first is inside the launch of the same slot
		(re-entered from the dispatch call, which is where two real sweeps overlap)."""
		inst = self._due_install()
		nested = {"ran": False}

		def _dispatch(**kw):
			if not nested["ran"]:
				nested["ran"] = True
				self._sweep_as_administrator()
			return self._dispatch_stub(**kw)

		self._sweep(dispatch=_dispatch)

		self.assertTrue(nested["ran"])
		self.assertEqual(len(self._dispatched_for(inst)), 1, "the overlapping sweep launched it again")
		self.assertEqual(len(self._launched(inst)), 1)

	def test_back_to_back_sweeps_launch_a_slot_once(self):
		inst = self._due_install()
		self._sweep()
		self._sweep()
		self.assertEqual(len(self._dispatched_for(inst)), 1)

	# --- a failed slot is retried 55 minutes later ---------------------------- #

	def test_a_failed_launch_is_not_retried_by_the_next_sweep(self):
		inst = self._due_install()
		before = now_datetime()

		self._sweep(dispatch=_unreachable)
		self._sweep(dispatch=_unreachable)

		self.assertEqual(self._runs(inst, "running"), [])
		self.assertEqual(
			len(self._notifications()), 1, "the next five-minute sweep tried again and notified again"
		)
		self._assert_retry_scheduled(inst, before)

	def test_a_failed_launch_is_retried_once_the_wait_is_over(self):
		inst = self._due_install()
		attempts = []

		def _count_then_refuse(**kw):
			attempts.append(kw)
			raise admin_client.AdminUnreachableError("admin returned a 400 error")

		self._sweep(dispatch=_count_then_refuse)
		self._the_wait_passes(inst)
		self._sweep()

		self.assertEqual(len(attempts), 1)
		self.assertEqual(len(self._dispatched_for(inst)), 1, "the retry never ran")

	def test_a_failed_launch_does_not_stamp_a_last_run(self):
		inst = self._due_install()
		self._sweep(dispatch=_unreachable)
		self.assertIsNone(frappe.db.get_value(INSTALLATION, inst, "last_run_at"), "nothing ran")

	def test_a_launch_that_raised_before_dispatch_is_retried_later_not_every_sweep(self):
		inst = self._due_install()
		before = now_datetime()
		attempts = []

		def _boom(inst_doc, **kw):
			attempts.append(inst_doc.name)
			raise RuntimeError("enqueue exploded")

		self._sweep(launch=_boom)
		self._sweep(launch=_boom)

		self.assertEqual(attempts, [inst], "the second sweep launched the failed slot again")
		self.assertEqual(len(self._runs(inst, "failed")), 1)
		self._assert_retry_scheduled(inst, before)

	def test_the_owner_is_told_the_retry_is_within_the_hour(self):
		self._due_install()
		self._sweep(dispatch=_unreachable)
		body = frappe.db.get_value(NOTIFICATION, self._notifications()[0], "email_content") or ""
		self.assertIn("within the hour", body)
		self.assertNotIn("hourly run", body)

	def test_a_container_mid_apply_is_retried_later_not_every_sweep(self):
		inst = self._due_install()
		before = now_datetime()
		calls = []

		def _refuse(*a, **kw):
			calls.append(1)
			raise agent_models.ModelRunRefused("apply_in_progress", "the container is applying a change")

		with patch.object(agent_scheduler, "_launch_audit", side_effect=_refuse):
			self._sweep()
			self._sweep()

		self.assertEqual(len(calls), 1)
		self._assert_retry_scheduled(inst, before)

	def test_a_failure_before_the_claim_is_retried_later_not_every_sweep(self):
		"""Anything raising ahead of the claim (here the model gate) left the slot due,
		and the five-minute sweep would hit it, and log it, twelve times an hour."""
		inst = self._due_install()
		before = now_datetime()

		with patch.object(agent_models, "gate_run", side_effect=RuntimeError("db blip")) as gate:
			self._sweep()
			self._sweep()

		self.assertEqual(gate.call_count, 1, "the next sweep hit the failing slot again")
		self.assertEqual(self._dispatched_for(inst), [])
		self._assert_retry_scheduled(inst, before)

	def test_a_failure_after_the_claim_is_not_turned_into_a_retry(self):
		"""The audit WAS launched; only the bookkeeping after it raised. The slot holds
		its next natural occurrence and must not be pulled back to an hour from now."""
		inst = self._due_install()
		self._schedule_far_from_now(inst)
		self._sweep(launch=self._launch_then_crash())
		self.assertEqual(len(self._launched(inst)), 1)
		nxt = get_datetime(self._next_run_at(inst))
		self.assertGreater(nxt, add_to_date(now_datetime(), seconds=agent_scheduler._RETRY_AFTER_S + 60))

	def test_the_retry_waits_55_minutes(self):
		inst = self._due_install()
		self._schedule_far_from_now(inst)
		before = now_datetime()
		self._sweep(dispatch=_unreachable)
		wait = get_datetime(self._next_run_at(inst)) - before
		self.assertGreaterEqual(wait.total_seconds(), 55 * 60)
		self.assertLessEqual(wait.total_seconds(), 55 * 60 + 10)

	def test_a_retry_that_fails_again_is_retried_again(self):
		"""A failed retry is claimed and fails like any slot, so it gets another retry
		55 minutes later; the chain ends at the slot's next natural time. The same as
		the macro sweep, and as the hourly sweep that handed a failed slot straight back."""
		inst = self._due_install()
		self._schedule_far_from_now(inst)
		self._sweep(dispatch=_unreachable)
		self._the_wait_passes(inst)
		before = now_datetime()

		self._sweep(dispatch=_unreachable)

		# Each failed launch fails its own run and records why the slot did not run.
		self.assertEqual(len(self._runs(inst, "failed")), 4)
		self.assertEqual(len(self._notifications()), 2)
		self._assert_retry_scheduled(inst, before)
		self.assertIsNone(frappe.db.get_value(INSTALLATION, inst, "last_run_at"), "nothing ran")

	def test_a_retry_does_not_overwrite_a_schedule_saved_during_the_launch(self):
		inst = self._due_install()
		saved = add_days(now_datetime(), 3).replace(microsecond=0)

		def _owner_saves_then_refused(**kw):
			frappe.db.set_value(INSTALLATION, inst, "next_run_at", saved, update_modified=False)
			frappe.db.commit()
			raise admin_client.AdminUnreachableError("admin returned a 400 error")

		self._sweep(dispatch=_owner_saves_then_refused)

		self.assertEqual(get_datetime(self._next_run_at(inst)), saved)
		self.assertIsNone(frappe.db.get_value(INSTALLATION, inst, "last_run_at"), "nothing ran")
		body = frappe.db.get_value(NOTIFICATION, self._notifications()[0], "email_content") or ""
		self.assertIn("next scheduled time", body, "the notice promised a retry that was not written")

	def test_a_retry_is_never_later_than_the_next_natural_slot(self):
		inst = self._due_install()
		soon = add_to_date(now_datetime(), minutes=20)
		frappe.db.set_value(
			INSTALLATION, inst, "schedule_time", soon.strftime("%H:%M:00"), update_modified=False
		)
		frappe.db.commit()

		self._sweep(dispatch=_unreachable)

		self.assertLessEqual(get_datetime(self._next_run_at(inst)), add_to_date(now_datetime(), minutes=21))

	# --- bounded per sweep ---------------------------------------------------- #

	def test_one_sweep_dispatches_at_most_the_cap_oldest_slot_first(self):
		frappe.set_user("Administrator")
		second_owner = _mk_user(SECOND_OWNER)
		allow_listing_for(SLUG, user=second_owner)
		older = self._due_install()
		newer = self._due_install_for(second_owner)
		frappe.db.set_value(
			INSTALLATION, older, "next_run_at", add_days(now_datetime(), -2), update_modified=False
		)
		frappe.db.commit()

		with patch.object(agent_scheduler, "SWEEP_MAX_PER_TICK", 1):
			self._sweep()
			self.assertEqual(len(self._dispatched_for(older)), 1, "the oldest slot goes first")
			self.assertEqual(self._dispatched_for(newer), [], "the sweep went past its cap")
			self._sweep()
		self.assertEqual(len(self._dispatched_for(newer)), 1, "the next sweep takes the rest")

	def test_a_sweep_starts_no_launch_once_its_time_budget_is_spent(self):
		frappe.set_user("Administrator")
		second_owner = _mk_user(SECOND_OWNER)
		allow_listing_for(SLUG, user=second_owner)
		first = self._due_install()
		second = self._due_install_for(second_owner)
		frappe.db.set_value(
			INSTALLATION, first, "next_run_at", add_days(now_datetime(), -2), update_modified=False
		)
		frappe.db.commit()
		left_due = get_datetime(self._next_run_at(second))
		# The sweep reads the clock once to start, then before each launch: the first
		# launch starts at 0 s, the second would start past the budget.
		clock = iter([0, 0, agent_scheduler.SWEEP_TIME_BUDGET_S + 1])

		with (
			patch.object(agent_scheduler, "_sweep_clock", side_effect=lambda: next(clock)),
			patch.object(agent_scheduler, "_sweep_log") as log,
		):
			self._sweep()

		self.assertEqual(len(self._dispatched_for(first)), 1)
		self.assertEqual(self._dispatched_for(second), [], "a launch started after the time budget")
		self.assertEqual(get_datetime(self._next_run_at(second)), left_due, "the slot left for later moved")
		self.assertIn("time budget", log.call_args.args[0])

	def test_the_time_budget_ends_inside_the_queue_timeout(self):
		from jarvis import admin_client as client

		self.assertLessEqual(agent_scheduler.SWEEP_TIME_BUDGET_S + client.DEFAULT_TIMEOUT_S, 300 - 20)

	def test_a_sweep_that_hits_the_cap_says_so_in_the_log(self):
		frappe.set_user("Administrator")
		second_owner = _mk_user(SECOND_OWNER)
		allow_listing_for(SLUG, user=second_owner)
		self._due_install()
		self._due_install_for(second_owner)
		with (
			patch.object(agent_scheduler, "SWEEP_MAX_PER_TICK", 1),
			patch.object(agent_scheduler, "_sweep_log") as log,
		):
			self._sweep()
		self.assertIn("more than 1", log.call_args.args[0])

	def test_a_sweep_under_the_cap_logs_nothing(self):
		self._due_install()
		with patch.object(agent_scheduler, "_sweep_log") as log:
			self._sweep()
		log.assert_not_called()

	# --- a job timeout during the dispatch ------------------------------------- #

	def test_a_job_timeout_during_the_dispatch_keeps_the_run_and_the_claim(self):
		"""RQ raises its job timeout inside the admin call. Admin may have the request,
		so the run must stay in flight under its own id, never be retried under a new one."""
		inst = self._due_install()
		self._schedule_far_from_now(inst)

		def _timed_out(**kw):
			self.dispatched.append(kw)  # admin received it
			raise JobTimeoutException("Task exceeded maximum timeout value (300 seconds)")

		self._sweep(dispatch=_timed_out)

		self.assertEqual(len(self._runs(inst, "running")), 1)
		self.assertEqual(self._runs(inst, "failed"), [], "the timeout was taken for a confirmed failure")
		self.assertTrue(frappe.db.exists(SESSION, {"user": self.owner}), "the run's session was torn down")
		self.assertEqual(self._notifications(), [])
		self.assertGreater(
			get_datetime(self._next_run_at(inst)),
			add_to_date(now_datetime(), hours=2),
			"the slot was handed to a retry, which would launch it again under a new run id",
		)

	def _due_install_for(self, owner: str) -> str:
		doc = frappe.get_doc(
			{"doctype": INSTALLATION, "agent": SLUG, "run_as_user": owner, "reviewer": owner, "enabled": 1}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(
			INSTALLATION,
			doc.name,
			{
				"owner": owner,
				"schedule_enabled": 1,
				"schedule_frequency": "daily",
				"next_run_at": add_days(now_datetime(), -1),
			},
			update_modified=False,
		)
		frappe.db.commit()
		return doc.name

	def tearDown(self):
		super().tearDown()
		for name in frappe.get_all(
			NOTIFICATION, filters={"for_user": SECOND_OWNER}, pluck="name", ignore_permissions=True
		):
			frappe.delete_doc(NOTIFICATION, name, force=True, ignore_permissions=True)
		frappe.db.commit()
