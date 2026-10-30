"""A macro run that waits for a report: what the app is told, who hears of its end and
what an operator can do (``jarvis.chat.macro_report_wait``, ``macros``, ``macros_api``).

* the runs API hands the app the awaited report names and ``waiting_since``, so the
  label survives a reload, and counts waiting runs in its stats;
* the owner hears every ending of a run that waited, their own Stop and the stale
  sweep included, and never twice;
* ``report_waits`` (read only) and ``end_all_report_waits`` (the kill-switch drain and
  the rollback step);
* the guard's read fault is logged once an hour per chat, not on every write call.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from jarvis.chat import macro_report_wait, macros, macros_api
from jarvis.tests.test_macro_report_wait import (
	CONV,
	OWNER,
	REPORT_B,
	RUN,
	ResumeBase,
)
from jarvis.tests.test_run_report import PREP_REPORT

MACRO = "Jarvis Macro"
NOTIFY = "Notification Log"
_ACME = {"company": "Acme"}
_OTHER = {"company": "Other"}


class OpsBase(ResumeBase):
	def _notes(self):
		return frappe.get_all(
			NOTIFY,
			filters={"for_user": OWNER},
			fields=["subject", "email_content", "link"],
			order_by="creation asc",
		)

	def _macro_name(self, macro=None):
		return frappe.db.get_value(MACRO, macro or self.macro, "macro_name")


class TestTheRunsApiCarriesTheWait(OpsBase):
	REPORTS = ((PREP_REPORT, _ACME), (REPORT_B, _ACME))

	def _as_owner(self):
		frappe.set_user(OWNER)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_the_run_history_names_the_reports_and_when_the_wait_began(self):
		self._as_owner()
		rows = macros_api.list_macro_runs()["runs"]
		(row,) = [r for r in rows if r.name == self.run]
		self.assertEqual(row.status, "waiting_report")
		self.assertEqual(row.waiting_reports, [PREP_REPORT, REPORT_B])
		self.assertTrue(row.waiting_since)
		self.assertNotIn("waiting_on", row)  # the stored JSON stays on the server

	def test_a_report_awaited_twice_is_named_once(self):
		frappe.db.set_value(
			RUN,
			self.run,
			"waiting_on",
			json.dumps(
				[
					{"prepared_report": self.prs[0], "report_name": PREP_REPORT, "filters": _ACME},
					{"prepared_report": self.prs[1], "report_name": PREP_REPORT, "filters": _OTHER},
				]
			),
			update_modified=False,
		)
		frappe.db.commit()
		self.assertEqual(
			macro_report_wait.awaited_names(frappe.db.get_value(RUN, self.run, "waiting_on")), [PREP_REPORT]
		)

	def test_a_run_that_is_not_waiting_names_nothing(self):
		frappe.db.set_value(RUN, self.run, "status", "running", update_modified=False)
		frappe.db.commit()
		self._as_owner()
		(row,) = [r for r in macros_api.list_macro_runs()["runs"] if r.name == self.run]
		self.assertEqual((row.waiting_reports, row.waiting_since), ([], ""))

	def test_the_macros_last_run_carries_it_for_the_list_and_the_form(self):
		self._as_owner()
		last = macros_api._last_runs([self.macro], OWNER)[self.macro]
		self.assertEqual(last["status"], "waiting_report")
		self.assertEqual(last["waiting_reports"], [PREP_REPORT, REPORT_B])
		self.assertTrue(last["waiting_since"])
		self.assertEqual(
			macros_api.get_macro(self.macro)["last_run"]["waiting_reports"], [PREP_REPORT, REPORT_B]
		)

	def test_someone_overseeing_the_macro_is_not_told_what_the_run_reads(self):
		# Like the conversation: an overseer may know a run waits, not which reports.
		frappe.set_user("Administrator")
		last = macros_api._last_runs([self.macro], OWNER)[self.macro]
		self.assertEqual(last["status"], "waiting_report")
		self.assertEqual((last["conversation"], last["waiting_reports"]), ("", []))

	def test_a_finished_last_run_has_an_empty_wait(self):
		frappe.db.set_value(RUN, self.run, "status", "completed", update_modified=False)
		frappe.db.commit()
		self._as_owner()
		last = macros_api._last_runs([self.macro], OWNER)[self.macro]
		self.assertEqual((last["waiting_reports"], last["waiting_since"]), ([], ""))

	def test_the_polled_run_carries_it(self):
		self._as_owner()
		got = macros_api.get_macro_run(self.run)
		self.assertEqual(got["status"], "waiting_report")
		self.assertEqual(got["waiting_reports"], [PREP_REPORT, REPORT_B])
		self.assertTrue(got["waiting_since"])

	def test_the_stats_count_waiting_runs(self):
		self._as_owner()
		stats = macros_api.macro_run_stats()
		self.assertEqual(stats["waiting_report"], 1)
		self.assertEqual(stats["running"], 0)
		self.assertEqual(stats["waiting_capacity"], 0)
		self.assertEqual(stats["total"], 1)


class TestTheOwnerHearsAWaitedRunEnd(OpsBase):
	def test_the_owners_own_stop_of_a_waited_run_is_told_once(self):
		frappe.set_user(OWNER)
		self.assertEqual(macros.stop_macro_run(self.run), {"ok": True})
		(note,) = self._notes()
		self.assertEqual(note.subject, f"Macro run stopped: {self._macro_name()}")
		self.assertEqual(note.link, f"/jarvis/c/{self.conv}")

	def test_a_stop_with_a_reason_says_the_reason(self):
		frappe.set_user("Administrator")
		self.assertTrue(macros._stop_run(self.run, reason="Stopped by an administrator.", by="Administrator"))
		(note,) = self._notes()
		self.assertIn("Stopped by an administrator.", note.email_content)

	def test_a_hold_that_stops_a_waited_run_is_told(self):
		frappe.set_user("Administrator")
		self.assertEqual(macros.stop_runs_of_macro(self.macro, reason="Put on hold.", by="Administrator"), 1)
		(note,) = self._notes()
		self.assertTrue(note.subject.startswith("Macro run stopped: "))

	def test_the_owners_stop_of_a_run_that_never_waited_stays_silent(self):
		run, _conv, _macro = self._mk_run(tag="plain")
		frappe.set_user(OWNER)
		macros.stop_macro_run(run)
		self.assertEqual(self._notes(), [])

	def test_the_stale_sweep_tells_the_owner_of_a_parked_run_it_closed(self):
		frappe.db.sql(
			f"UPDATE `tab{RUN}` SET modified=%s WHERE name=%s",
			(add_to_date(now_datetime(), seconds=-(macro_report_wait.STALE_WAIT_AFTER_S + 600)), self.run),
		)
		frappe.db.commit()
		frappe.set_user("Administrator")
		with patch.object(macros, "_stale_run_candidates", return_value=[self.run]):
			self.assertEqual(macros.reap_stale_macro_runs(), 1)
		(note,) = self._notes()
		self.assertEqual(note.subject, f"Macro run did not finish: {self._macro_name()}")
		self.assertEqual(note.link, f"/jarvis/c/{self.conv}")

	def test_a_wait_the_cap_ended_is_told_once(self):
		# Ended by the engine straight away (the report is ready, the run has used its
		# resumes): through ``_end_run`` -> ``_announce``, so one note, not two.
		self._ready()
		frappe.db.set_value(
			RUN, self.run, "resume_count", macro_report_wait._MAX_RESUMES, update_modified=False
		)
		frappe.db.commit()
		self._resume()
		self.assertEqual(self._row().status, "failed")
		(note,) = self._notes()
		self.assertTrue(note.subject.startswith("Macro run failed: "))

	def test_a_waited_run_the_owner_stopped_on_its_step_is_told_through_announce(self):
		# The one ending that does go through ``_announce`` with no reason: `stopped`, ""
		# is silent for an ordinary run (their own click) and not for one that waited.
		frappe.set_user("Administrator")
		with patch.object(macros, "_post_closing_message"), patch.object(macros, "_publish_done"):
			macros._announce(frappe.get_doc(RUN, self.run), frappe.get_doc(MACRO, self.macro), "stopped", "")
		(note,) = self._notes()
		self.assertTrue(note.subject.startswith("Macro run stopped: "))

	def test_announce_stays_silent_for_an_unwaited_runs_own_stop(self):
		run, _conv, macro = self._mk_run(tag="plain2")
		frappe.set_user("Administrator")
		with patch.object(macros, "_post_closing_message"), patch.object(macros, "_publish_done"):
			macros._announce(frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro), "stopped", "")
		self.assertEqual(self._notes(), [])


class TestReportWaits(OpsBase):
	REPORTS = ((PREP_REPORT, _ACME), (REPORT_B, _ACME))

	def test_it_lists_the_waiting_run_with_the_live_status_of_each_report(self):
		self._set_pr(self.prs[1], status="Queued")
		(wait,) = [w for w in macro_report_wait.report_waits() if w["run"] == self.run]
		self.assertEqual(wait["macro"], self.macro)
		self.assertEqual(wait["owner"], OWNER)
		self.assertEqual(wait["step"], "1/2")
		self.assertEqual(
			[(a["report"], a["prepared_report"], a["status"]) for a in wait["awaiting"]],
			[(PREP_REPORT, self.prs[0], "Started"), (REPORT_B, self.prs[1], "Queued")],
		)
		self.assertTrue(wait["waiting_since"])

	def test_the_deadline_is_when_the_sweep_gives_up_on_the_first_report(self):
		since = add_to_date(now_datetime(), minutes=-10).replace(microsecond=0)
		frappe.db.set_value(RUN, self.run, "waiting_since", since, update_modified=False)
		frappe.db.commit()
		(wait,) = [w for w in macro_report_wait.report_waits() if w["run"] == self.run]
		limit = min(macro_report_wait._wait_limit_s(PREP_REPORT), macro_report_wait._wait_limit_s(REPORT_B))
		self.assertEqual(wait["deadline"], str(add_to_date(since, seconds=limit)))

	def test_a_deleted_report_reads_missing(self):
		frappe.db.delete("Prepared Report", {"name": self.prs[0]})
		frappe.db.commit()
		(wait,) = [w for w in macro_report_wait.report_waits() if w["run"] == self.run]
		self.assertEqual(wait["awaiting"][0]["status"], "missing")

	def test_it_changes_nothing_and_leaves_other_runs_out(self):
		other, _conv, _macro = self._mk_run(tag="running")
		before = self._row()
		with patch.object(macros, "_cas_run_status") as cas, patch("frappe.db.commit") as commit:
			waits = macro_report_wait.report_waits()
		cas.assert_not_called()
		commit.assert_not_called()
		self.assertEqual(self._row(), before)
		self.assertIn(self.run, [w["run"] for w in waits])
		self.assertNotIn(other, [w["run"] for w in waits])

	def test_nothing_waiting_is_an_empty_list(self):
		frappe.db.set_value(RUN, self.run, "status", "stopped", update_modified=False)
		frappe.db.commit()
		self.assertEqual(macro_report_wait.report_waits(), [])


class TestEndAllReportWaits(OpsBase):
	def setUp(self):
		super().setUp()
		self.run2, self.conv2, self.macro2 = self._parked(armed=True, tag="second")

	def test_every_waiting_run_ends_failed_with_the_reason_and_its_chat_is_disarmed(self):
		running, _conv, _macro = self._mk_run(tag="running")
		frappe.set_user("Administrator")
		out = macro_report_wait.end_all_report_waits()
		self.assertEqual(sorted(out["ended"]), sorted([self.run, self.run2]))
		self.assertEqual(out["skipped"], [])
		for run in (self.run, self.run2):
			got = frappe.db.get_value(RUN, run, ["status", "error", "finished_at"], as_dict=True)
			self.assertEqual((got.status, got.error), ("failed", macro_report_wait.WAITS_ENDED))
			self.assertTrue(got.finished_at)
		self.assertFalse(frappe.db.get_value(CONV, self.conv2, "skip_confirmation"))
		self.assertEqual(frappe.db.get_value(RUN, running, "status"), "running")

	def test_each_owner_is_told_once(self):
		frappe.set_user("Administrator")
		macro_report_wait.end_all_report_waits()
		subjects = sorted(n.subject for n in self._notes())
		self.assertEqual(
			subjects,
			sorted(
				[
					f"Macro run failed: {self._macro_name(self.macro)}",
					f"Macro run failed: {self._macro_name(self.macro2)}",
				]
			),
		)

	def test_a_second_call_has_nothing_left_to_end(self):
		frappe.set_user("Administrator")
		macro_report_wait.end_all_report_waits()
		self.assertEqual(macro_report_wait.end_all_report_waits(), {"ended": [], "skipped": []})
		self.assertEqual(macro_report_wait.report_waits(), [])

	def test_a_run_another_worker_is_moving_is_skipped_not_forced(self):
		from jarvis._redis_lock import redis_lock

		frappe.set_user("Administrator")
		with (
			patch.object(macro_report_wait, "_END_ALL_LOCK_WAIT_S", 0.0),
			redis_lock(f"jarvis_macro_run:{self.run}", timeout_s=30, blocking_timeout_s=0.0) as held,
		):
			self.assertTrue(held)
			out = macro_report_wait.end_all_report_waits()
		self.assertEqual((out["ended"], out["skipped"]), ([self.run2], [self.run]))
		self.assertEqual(self._row().status, "waiting_report")

	def test_one_runs_fault_leaves_it_for_a_second_call_and_the_others_end(self):
		real = macros._fail_parked_run

		def flaky(run, macro_doc, expect, error):
			if run.name == self.run:
				raise RuntimeError("boom")
			return real(run, macro_doc, expect, error)

		frappe.set_user("Administrator")
		with patch.object(macros, "_fail_parked_run", side_effect=flaky):
			out = macro_report_wait.end_all_report_waits()
		self.assertEqual((out["ended"], out["skipped"]), ([self.run2], [self.run]))
		self.assertEqual(self._row().status, "waiting_report")
		self.assertEqual(macro_report_wait.end_all_report_waits()["ended"], [self.run])

	def test_a_run_that_was_stopped_meanwhile_keeps_its_outcome(self):
		real = macros._fail_parked_run

		def stopped_first(run, macro_doc, expect, error):
			frappe.db.set_value(RUN, run.name, "status", "stopped", update_modified=False)
			frappe.db.commit()
			return real(run, macro_doc, expect, error)

		frappe.set_user("Administrator")
		with patch.object(macros, "_fail_parked_run", side_effect=stopped_first):
			out = macro_report_wait.end_all_report_waits()
		self.assertEqual(out["ended"], [])
		self.assertEqual(self._row().status, "stopped")


class TestTheReadFaultIsLoggedOncePerChat(OpsBase):
	TITLE = "jarvis macro report wait read failed"

	def setUp(self):
		super().setUp()
		for conv in (self.conv, "other-chat"):
			frappe.cache().delete_value(f"jarvis_macro_wait_log:{self.TITLE}:{conv}")
		self.addCleanup(
			lambda: [
				frappe.cache().delete_value(f"jarvis_macro_wait_log:{self.TITLE}:{c}")
				for c in (self.conv, "other-chat")
			]
		)

	def test_a_database_fault_logs_once_an_hour_however_many_write_calls_read(self):
		with (
			patch.object(macro_report_wait, "_step_reads", side_effect=RuntimeError("db gone")),
			patch("frappe.log_error") as log,
		):
			for _ in range(5):
				self.assertEqual(macro_report_wait.step_reads(self.conv, {"name": "t"}), ([], [], None))
		log.assert_called_once()
		self.assertEqual(log.call_args.kwargs["title"], self.TITLE)

	def test_another_chat_still_logs(self):
		with (
			patch.object(macro_report_wait, "_step_reads", side_effect=RuntimeError("db gone")),
			patch("frappe.log_error") as log,
		):
			macro_report_wait.step_reads(self.conv, {"name": "t"})
			macro_report_wait.step_reads("other-chat", {"name": "t"})
		self.assertEqual(log.call_count, 2)

	def test_an_unreadable_row_logs_once_an_hour_too(self):
		frappe.cache().delete_value("jarvis_macro_wait_log:jarvis macro report wait row skipped:" + self.conv)
		self.addCleanup(
			frappe.cache().delete_value,
			"jarvis_macro_wait_log:jarvis macro report wait row skipped:" + self.conv,
		)
		seed = self._msg(self.conv, "user", origin="macro", content="step 1")
		turn = frappe._dict(name="none", seed_message=seed)
		row = {"status": "started", "tool_args": "{}", "run": "", "report_name": "", "waiting": False}
		with (
			patch.object(macro_report_wait, "_rows", return_value=[row, row, row]),
			patch.object(macro_report_wait, "_note", side_effect=RuntimeError("bad row")),
			patch("frappe.log_error") as log,
		):
			for _ in range(3):
				macro_report_wait.step_reads(self.conv, turn)
		log.assert_called_once()
		self.assertEqual(log.call_args.kwargs["title"], "jarvis macro report wait row skipped")


class TestTheOwnersPagesSeeTheRunGoOn(OpsBase):
	def _events(self):
		return [c.args[1] for c in self.publish.call_args_list]

	def setUp(self):
		super().setUp()
		patcher = patch("jarvis.chat.macro_report_wait.publish_to_user")
		self.publish = patcher.start()
		self.addCleanup(patcher.stop)

	def test_a_continuation_that_went_out_says_the_run_is_running_again(self):
		self._sending()
		self._ready()
		self._resume()
		self.assertEqual(self._row().status, "running")
		(event,) = self._events()
		self.assertEqual(self.publish.call_args.args[0], OWNER)
		self.assertEqual(
			(event["kind"], event["status"], event["macro"], event["macro_run"]),
			("macro:progress", "running", self.macro, self.run),
		)

	def test_a_report_still_generating_says_nothing(self):
		self._sending()
		self._resume()
		self.assertEqual(self._events(), [])

	def test_a_publish_that_fails_does_not_undo_the_resume(self):
		self._sending()
		self.publish.side_effect = RuntimeError("redis down")
		self._ready()
		self._resume()
		self.assertEqual(self._row().status, "running")
		self.assertEqual(self._row().resume_count, 1)


class TestTheWakeFailureIsLoggedOncePerHour(OpsBase):
	def setUp(self):
		super().setUp()
		self._wake_patch.stop()  # the real ``wake``
		self._forget_wake_log(self.run)
		self._forget_wake_log(None)

	def test_a_queue_that_is_down_logs_once_an_hour_per_run(self):
		with patch("frappe.enqueue", side_effect=RuntimeError("redis")), patch("frappe.log_error") as log:
			for _ in range(4):
				macro_report_wait.wake(self.run)
		log.assert_called_once()
		self.assertEqual(log.call_args.kwargs["title"], "jarvis macro report wake failed")

	def test_the_report_hook_logs_once_an_hour_for_the_site(self):
		doc = frappe._dict(name=self.pr, owner=OWNER, status="Completed")
		with patch("frappe.get_all", side_effect=RuntimeError("db gone")), patch("frappe.log_error") as log:
			for _ in range(3):
				macro_report_wait.on_prepared_report_update(doc)
		log.assert_called_once()
