"""Deleting a macro does not hand its owner the month's unattended budget back.

The monthly budget for scheduled runs is added up from the owner's run rows
(``macros._scheduled_steps_this_month``), and ``macros_api.delete_macro`` deleted
every run row of the macro: delete the macro, create it again, and the month
started over (2026-10-01 macros review).

Now the delete keeps the finished rows that count toward the current month, with
no macro on them, and hides them everywhere runs are listed. The hourly macro job
removes them once their month is over.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from jarvis import onboarding
from jarvis.chat import api as chat_api
from jarvis.chat import feature_usage, macro_permissions, macros, macros_api
from jarvis.tests import test_macro_stop_delete as stop
from jarvis.tests._pending_action_helpers import ensure_user

CONV = stop.CONV
MACRO = stop.MACRO
RUN = stop.RUN
OWNER = stop.OWNER
BYSTANDER = stop.BYSTANDER


class BudgetBase(stop.StopBase):
	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.delete(RUN, {"owner": BYSTANDER})
		frappe.db.commit()
		super().tearDown()

	def _finished(self, *, steps=3, at_step=2, trigger="scheduled", status="completed", tag="m", age_days=0):
		"""A macro and one finished run of it that dispatched ``at_step`` steps,
		created ``age_days`` before the first day of this month (0: today)."""
		run, conv, macro = self._mk_run(steps=steps, at_step=at_step, trigger=trigger, tag=tag)
		values = {"status": status, "finished_at": frappe.utils.now()}
		if age_days:
			values["creation"] = frappe.utils.add_days(self._month_start(), -age_days)
		frappe.db.set_value(RUN, run, values, update_modified=False)
		frappe.db.commit()
		return run, conv, macro

	def _another_run(self, macro, **values):
		"""One more finished run row of ``macro``, written the way the engine leaves one."""
		frappe.set_user(OWNER)
		run = frappe.get_doc({"doctype": RUN, "macro": macro, "status": "completed", "total_steps": 3})
		run.flags.ignore_permissions = True
		run.insert()
		frappe.db.set_value(RUN, run.name, values, update_modified=False)
		frappe.db.commit()
		return run.name

	def _month_start(self):
		return frappe.utils.get_datetime(frappe.utils.get_first_day(frappe.utils.today()))

	def _spent(self, owner=OWNER):
		return macros._scheduled_steps_this_month(owner)

	def _delete(self, macro):
		frappe.set_user(OWNER)
		return macros_api.delete_macro(macro)

	def _row(self, run):
		return frappe.db.get_value(
			RUN, run, ["macro", "conversation", "status", "current_step", "owner", "error"], as_dict=True
		)

	def _next_month(self):
		"""The clock the budget month is read from, moved to the 3rd of next month."""
		day = frappe.utils.add_days(frappe.utils.get_first_day(frappe.utils.today(), d_months=1), 2)
		return patch("frappe.utils.today", return_value=str(day))


# --------------------------------------------------------------------------- #
# What a delete keeps, and what it removes
# --------------------------------------------------------------------------- #
class TestADeleteKeepsTheMonthsScheduledSteps(BudgetBase):
	def test_deleting_and_recreating_a_macro_does_not_lower_the_months_sum(self):
		run, conv, macro = self._finished(at_step=2)
		spent = self._spent()
		self.assertGreaterEqual(spent, 2)
		self.assertEqual(self._delete(macro), {"ok": True, "stopped_runs": 0})
		self.assertFalse(frappe.db.exists(MACRO, macro))
		self.assertEqual(self._spent(), spent)
		# Only the macro link is cleared: the row still points at its chat.
		kept = self._row(run)
		self.assertIsNone(kept.macro)
		self.assertEqual((kept.conversation, kept.status, kept.current_step), (conv, "completed", 2))
		# The same macro again, and a run of it, add to the month: nothing was reset.
		_, _, again = self._finished(at_step=1, tag="m")
		self.assertEqual(self._spent(), spent + 1)
		self._delete(again)
		self.assertEqual(self._spent(), spent + 1)

	def test_a_manual_run_is_deleted_not_kept(self):
		run, _, macro = self._finished(trigger="manual")
		self._delete(macro)
		self.assertFalse(frappe.db.exists(RUN, run))

	def test_a_run_from_an_earlier_month_is_deleted_not_kept(self):
		old, _, macro = self._finished(age_days=1)
		on_the_boundary = self._another_run(
			macro, trigger="scheduled", current_step=1, creation=self._month_start()
		)
		spent = self._spent()
		self._delete(macro)
		self.assertFalse(frappe.db.exists(RUN, old))
		# The first instant of the month counts toward it, so that row is kept.
		self.assertIsNone(self._row(on_the_boundary).macro)
		self.assertEqual(self._spent(), spent)

	def test_a_scheduled_run_that_never_dispatched_is_deleted(self):
		# A slot the scheduler refused is recorded with no step sent: nothing to count.
		refused, _, macro = self._finished(at_step=0, status="failed")
		self._delete(macro)
		self.assertFalse(frappe.db.exists(RUN, refused))

	def test_only_the_rows_that_count_are_kept_out_of_a_macros_whole_history(self):
		counted, _, macro = self._finished(at_step=3)
		manual = self._another_run(macro, trigger="manual", current_step=3)
		older = self._another_run(
			macro,
			trigger="scheduled",
			current_step=3,
			creation=frappe.utils.add_days(self._month_start(), -40),
		)
		refused = self._another_run(macro, trigger="scheduled", current_step=0, status="failed")
		stopped = self._another_run(macro, trigger="scheduled", current_step=1, status="stopped")
		spent = self._spent()
		self._delete(macro)
		self.assertEqual(
			[bool(frappe.db.exists(RUN, r)) for r in (counted, manual, older, refused, stopped)],
			[True, False, False, False, True],
		)
		self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 0)
		self.assertEqual(self._spent(), spent)

	def test_a_live_scheduled_run_is_stopped_first_and_kept_as_a_finished_row(self):
		run, conv, macro = self._mk_run(steps=3, at_step=2, armed=True, trigger="scheduled")
		spent = self._spent()
		self.assertEqual(self._delete(macro), {"ok": True, "stopped_runs": 1})
		kept = self._row(run)
		self.assertEqual((kept.macro, kept.status, kept.error), (None, "stopped", stop.DELETED))
		self.assertEqual(kept.conversation, conv)
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)
		self.assertEqual(self._spent(), spent)
		# No engine job picks a kept row up: it is not live.
		self.assertEqual(macros.live_runs_of(macro), [])
		with (
			patch("jarvis.chat.api._enqueue_turn") as enqueue,
			patch.object(frappe, "log_error") as log,
			# Handed to the stuck-run sweep by name, as a scan from before the delete would.
			patch.object(macros, "_stale_run_candidates", return_value=[run]),
		):
			macros.resume_waiting_capacity_runs()
			macros.advance_after_turn(conv, errored=False)
			self.assertEqual(macros.reap_stale_macro_runs(), 0)
		enqueue.assert_not_called()
		log.assert_not_called()
		self.assertEqual(self._row(run).status, "stopped")

	def test_a_kept_row_stays_with_its_own_owner(self):
		# The sum is by the run row's owner, so it is right whoever deletes the macro.
		run, _, macro = self._finished(at_step=2)
		frappe.set_user("Administrator")
		frappe.db.set_value(RUN, run, "owner", ensure_user(BYSTANDER), update_modified=False)
		frappe.db.commit()
		theirs, mine = self._spent(BYSTANDER), self._spent()
		self._delete(macro)
		self.assertEqual(self._row(run).owner, BYSTANDER)
		self.assertEqual((self._spent(BYSTANDER), self._spent()), (theirs, mine))

	def test_a_delete_that_fails_keeps_the_run_history_on_the_macro(self):
		# The rows are let go of and the macro deleted in one transaction.
		run, _, macro = self._finished(at_step=2)
		frappe.set_user(OWNER)
		with (
			patch.object(macros_api.frappe, "delete_doc", side_effect=RuntimeError("no")),
			patch.object(frappe, "log_error"),
		):
			out = macros_api.delete_macros_bulk([macro])
		self.assertEqual(out["deleted"], 0)
		self.assertEqual(self._row(run).macro, macro)

	def test_a_bulk_delete_keeps_them_too(self):
		one, _, first = self._finished(at_step=2, tag="bulk-1")
		two, _, second = self._finished(at_step=1, tag="bulk-2")
		gone, _, third = self._finished(at_step=3, trigger="manual", tag="bulk-3")
		spent = self._spent()
		frappe.set_user(OWNER)
		out = macros_api.delete_macros_bulk([first, second, third])
		self.assertEqual(out, {"deleted": 3, "skipped": [], "stopped_runs": 0})
		self.assertEqual(self._spent(), spent)
		self.assertEqual([self._row(r).macro for r in (one, two)], [None, None])
		self.assertFalse(frappe.db.exists(RUN, gone))

	def test_the_budget_still_refuses_a_run_when_the_kept_rows_are_over_it(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Jarvis Settings", "macro_step_budget_monthly", 40)
		self.addCleanup(frappe.db.set_single_value, "Jarvis Settings", "macro_step_budget_monthly", None)
		_, _, macro = self._finished(steps=25, at_step=max(40 - self._spent(), 1))
		with patch("jarvis.chat.policy.validate_can_send", return_value=(True, "")):
			self.assertEqual(
				macros.entitlement_block(OWNER, steps=1, trigger="scheduled"), macros.BLOCK_STEP_BUDGET
			)
			self._delete(macro)
			self.assertEqual(
				macros.entitlement_block(OWNER, steps=1, trigger="scheduled"), macros.BLOCK_STEP_BUDGET
			)
			self.assertIsNone(macros.entitlement_block(OWNER, steps=1, trigger="manual"))


# --------------------------------------------------------------------------- #
# A kept row is not run history
# --------------------------------------------------------------------------- #
class TestAKeptRowIsShownNowhere(BudgetBase):
	def _kept(self, **kw):
		run, conv, macro = self._finished(**kw)
		self._delete(macro)
		self.assertIsNone(self._row(run).macro)
		frappe.set_user(OWNER)
		return run, conv, macro

	def test_it_is_not_in_the_runs_tab(self):
		frappe.set_user(OWNER)
		before = macros_api.list_macro_runs()
		run, _, macro = self._kept()
		for out in (
			macros_api.list_macro_runs(),
			macros_api.list_macro_runs(status="completed"),
			macros_api.list_macro_runs(macro=macro),
		):
			self.assertNotIn(run, [r.name for r in out["runs"]])
		self.assertEqual(macros_api.list_macro_runs()["total"], before["total"])
		self.assertEqual(macros_api.list_macro_runs(macro=macro), {"runs": [], "has_more": False, "total": 0})

	def test_it_is_not_in_the_stats(self):
		_, _, alive = self._finished(tag="alive", trigger="manual")
		frappe.set_user(OWNER)
		before = macros_api.macro_run_stats()
		self.assertEqual(before["total"], 1)
		self._kept(status="completed", tag="ok")
		self._kept(status="failed", tag="bad")
		self._kept(status="stopped", tag="halted")
		frappe.db.sql(f"UPDATE `tab{RUN}` SET error = 'x' WHERE owner = %s AND status = 'stopped'", OWNER)
		self.assertEqual(macros_api.macro_run_stats(), before)

	def test_the_stats_of_a_user_with_only_kept_rows_are_empty(self):
		self._kept()
		out = macros_api.macro_run_stats()
		self.assertEqual((out["total"], out["success_rate"], out["last_run_at"]), (0, None, ""))

	def test_it_is_not_a_macros_last_run(self):
		run, _, macro = self._kept()
		_, _, other = self._finished(tag="other", trigger="manual")
		frappe.set_user(OWNER)
		out = macros_api._last_runs([macro, other], OWNER)
		self.assertEqual(list(out), [other])
		rows = macros_api.list_macros_page()["rows"]
		self.assertEqual([r["name"] for r in rows if r["last_run"]], [other])

	def test_it_cannot_be_polled_as_a_run(self):
		# The Runs screen polls a run by name. The run of a deleted macro reads as
		# gone, as it did when its row was deleted.
		run, _, _ = self._kept()
		with self.assertRaises(frappe.DoesNotExistError):
			macros_api.get_macro_run(run)

	def test_stop_on_it_does_nothing(self):
		run, _, _ = self._kept()
		self.assertEqual(macros_api.stop_macro_run(run), {"ok": True})
		self.assertEqual(self._row(run).status, "completed")

	def test_the_document_api_reads_it_for_its_owner_only_and_writes_it_for_nobody(self):
		run, _, _ = self._kept()
		doc = frappe.get_doc(RUN, run)
		self.assertTrue(macro_permissions.has_macro_run_permission(doc, "read", OWNER))
		self.assertTrue(doc.has_permission("read"))
		for ptype in ("write", "delete", "create"):
			self.assertFalse(macro_permissions.has_macro_run_permission(doc, ptype, OWNER), ptype)
		self.assertIn(run, frappe.get_list(RUN, pluck="name"))
		self._as(BYSTANDER)
		self.assertFalse(macro_permissions.has_macro_run_permission(doc, "read", BYSTANDER))
		self.assertNotIn(run, frappe.get_list(RUN, pluck="name"))
		self.assertIn(BYSTANDER, macro_permissions.macro_run_query_conditions(BYSTANDER))

	def test_the_other_readers_of_run_rows_take_it_as_it_is(self):
		run, conv, _ = self._kept()
		# "Used macros recently" is true: the run happened.
		self.assertTrue(feature_usage._macros(OWNER, self._month_start()))
		# Clearing chat history blanks the chat link, as on any run row, and the
		# month's sum is still there.
		spent = self._spent()
		chat_api.clear_chat_history()
		kept = self._row(run)
		self.assertEqual((kept.macro, kept.conversation), (None, None))
		self.assertEqual(self._spent(), spent)


# --------------------------------------------------------------------------- #
# The month ends
# --------------------------------------------------------------------------- #
class TestKeptRowsGoWhenTheirMonthIsOver(BudgetBase):
	def _kept_last_month(self):
		"""A row a delete kept, aged to last month, as it is once the month turns."""
		run, _, macro = self._finished(at_step=2, tag="aged")
		self._delete(macro)
		frappe.db.set_value(
			RUN, run, "creation", frappe.utils.add_days(self._month_start(), -1), update_modified=False
		)
		frappe.db.commit()
		return run

	def test_the_purge_deletes_last_months_kept_rows_and_leaves_this_months(self):
		old = self._kept_last_month()
		current, _, macro = self._finished(at_step=2, tag="now")
		self._delete(macro)
		# Last month's run of a macro that still exists is run history: not touched.
		history, _, _ = self._finished(at_step=2, tag="alive", age_days=1)
		self.assertEqual(macros.purge_kept_budget_rows(), 1)
		self.assertFalse(frappe.db.exists(RUN, old))
		self.assertTrue(frappe.db.exists(RUN, current))
		self.assertTrue(frappe.db.exists(RUN, history))
		self.assertEqual(macros.purge_kept_budget_rows(), 0)

	def test_the_purge_is_bounded(self):
		first, second = self._kept_last_month(), self._kept_last_month()
		with patch.object(macros, "_KEPT_ROW_PURGE_BATCH", 1):
			self.assertEqual(macros.purge_kept_budget_rows(), 1)
			self.assertEqual(sum(bool(frappe.db.exists(RUN, r)) for r in (first, second)), 1)
			self.assertEqual(macros.purge_kept_budget_rows(), 1)
		self.assertEqual(frappe.db.count(RUN, {"name": ["in", [first, second]]}), 0)

	def test_the_month_turning_over_starts_the_sum_again_and_the_rows_go(self):
		run, _, macro = self._finished(at_step=2)
		self._delete(macro)
		self.assertGreaterEqual(self._spent(), 2)
		self.assertEqual(macros.purge_kept_budget_rows(), 0)
		self.assertTrue(frappe.db.exists(RUN, run))
		with self._next_month():
			self.assertEqual(self._spent(), 0)
			self.assertGreaterEqual(macros.purge_kept_budget_rows(), 1)
		self.assertFalse(frappe.db.exists(RUN, run))

	def test_a_macro_deleted_next_month_keeps_nothing_of_this_month(self):
		run, _, macro = self._finished(at_step=2)
		with self._next_month():
			self._delete(macro)
		self.assertFalse(frappe.db.exists(RUN, run))

	def test_the_hourly_macro_job_runs_the_purge(self):
		# No new scheduler entry (that would need a migrate): the job that is there
		# does it, also on a pass with no stuck run to look at.
		old = self._kept_last_month()
		self.assertIn(
			"jarvis.chat.macros.reap_stale_macro_runs", frappe.get_hooks("scheduler_events")["hourly"]
		)
		with patch.object(macros, "_stale_run_candidates", return_value=[]):
			self.assertEqual(macros.reap_stale_macro_runs(), 0)
		frappe.db.rollback()  # the purge committed
		self.assertFalse(frappe.db.exists(RUN, old))

	def test_a_purge_that_fails_is_logged_and_the_stuck_runs_are_still_looked_at(self):
		stuck, _, _ = self._mk_run(tag="stuck")
		frappe.db.sql(
			f"UPDATE `tab{RUN}` SET modified = %s WHERE name = %s",
			(frappe.utils.add_days(frappe.utils.now_datetime(), -2), stuck),
		)
		frappe.db.commit()
		with (
			patch.object(macros, "purge_kept_budget_rows", side_effect=RuntimeError("no")),
			patch.object(macros, "_stale_run_candidates", return_value=[stuck]),
			patch.object(frappe, "log_error") as log,
		):
			self.assertEqual(macros.reap_stale_macro_runs(), 1)
		self.assertEqual(len(self._logged(log, "jarvis macro kept-row purge failed")), 1)
		self.assertEqual(self._row(stuck).status, "failed")


# --------------------------------------------------------------------------- #
# The workspace wipe is still a full reset
# --------------------------------------------------------------------------- #
class TestTheWorkspaceWipeRemovesKeptRows(BudgetBase):
	def test_the_wipe_removes_kept_rows_too(self):
		run, _, macro = self._finished(at_step=2)
		self._delete(macro)
		self.assertGreaterEqual(self._spent(), 2)
		self.assertIn(RUN, onboarding._WIPE_DOCTYPES)
		# This site is shared with every other suite: wipe the one table, look, and
		# take it back.
		try:
			with patch.object(onboarding, "_WIPE_DOCTYPES", (RUN,)):
				onboarding._wipe_workspace_content()
			self.assertFalse(frappe.db.exists(RUN, run))
			self.assertEqual(self._spent(), 0)
		finally:
			frappe.db.rollback()
		self.assertTrue(frappe.db.exists(RUN, run))
