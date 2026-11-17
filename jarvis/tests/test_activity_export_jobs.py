"""Background export jobs record exactly one ``export`` Jarvis Activity Event, at the job's end.

Plan ``.claude/workflow/plans/2026-11-06-user-activity-report.md`` 4.3(4): no row at queue
time; ``ok`` inside ``_publish_locked`` on the same commit as ``ready``; ``failed`` at both
``_finish`` (failed / too large / too slow) and the reaper's ``_recover_row``; a user's
cancel records nothing. Mirrors the fixtures of ``test_background_exports``."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.tests.test_run_report import _ENQUEUE, PREP_REPORT, _ensure_report, _make_pr
from jarvis.tools._export import jobs

EVENT = "Jarvis Activity Event"
OWNER = "Administrator"


def _export_rows():
	return frappe.get_all(
		EVENT,
		filters={"event": "export", "method": "export_query", "actor": OWNER},
		fields=["name", "outcome", "row_count", "ref_doctype", "channel", "code", "actor"],
	)


class TestActivityExportJobs(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.conversation = (
			frappe.get_doc({"doctype": "Jarvis Conversation", "title": "activity export test"})
			.insert(ignore_permissions=True)
			.name
		)
		self.names = []
		frappe.db.commit()
		self.before = {r.name for r in _export_rows()}

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for name in self.names:
			if frappe.db.exists(jobs.DOCTYPE, name):
				jobs._delete_file(frappe.get_doc(jobs.DOCTYPE, name))
				frappe.db.delete(jobs.DOCTYPE, {"name": name})
		frappe.db.delete(
			"Notification Log", {"document_type": jobs.DOCTYPE, "document_name": ["in", self.names]}
		)
		frappe.db.delete("Jarvis Chat Message", {"conversation": self.conversation})
		frappe.db.delete("Jarvis Conversation", {"name": self.conversation})
		# Only this test's rows: other rows of the same shape are not ours to delete.
		for row in self.new_rows():
			frappe.db.delete(EVENT, {"name": row.name})
		frappe.db.commit()
		super().tearDown()

	def job(self, **values):
		job = frappe.get_doc(
			{
				"doctype": jobs.DOCTYPE,
				"conversation": self.conversation,
				"source": json.dumps({"spec": {"from": "DocType", "select": ["name"], "limit": 2}}),
				"title": "Activity export test",
				"format": "csv",
				"status": "running",
				"attempt_token": "abc123",
				"heartbeat": jobs.now_datetime(),
				"source_doctype": "ToDo",
				**values,
			}
		)
		job.flags.jarvis_server_write = True
		job.insert(ignore_permissions=True)
		self.names.append(job.name)
		frappe.db.commit()
		return job

	def new_rows(self):
		return [r for r in _export_rows() if r.name not in self.before]

	def publish(self, job, rows=7):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "export.part"
			path.write_text("name\nA\nB\n")
			snapshot = jobs._get_job(job.name)
			jobs._publish_file(snapshot, "abc123", path, {"rows_written": rows}, "permission")

	def test_success_after_two_attempts_records_one_ok_row(self):
		job = self.job()
		# Attempt 1 is interrupted (site update) and queued for its one retry: no row yet.
		jobs._finish(job.name, "abc123", "paused", "site update")
		self.assertEqual(job.reload().retries, 1)
		self.assertEqual(self.new_rows(), [])
		frappe.db.set_value(jobs.DOCTYPE, job.name, {"status": "running", "attempt_token": "abc123"})
		frappe.db.commit()
		# Attempt 2 publishes.
		self.publish(job, rows=7)
		self.assertEqual(job.reload().status, "ready")
		rows = self.new_rows()
		self.assertEqual(len(rows), 1)
		self.assertEqual(
			(rows[0].outcome, rows[0].row_count, rows[0].actor, rows[0].ref_doctype, rows[0].channel),
			("ok", job.rows_written, job.owner, "ToDo", "chat"),
		)
		self.assertEqual(rows[0].row_count, 7)

	def _publish_commit(self, job, then):
		"""A ``_commit`` stand-in that calls ``then()`` ONLY for the commit issued by
		``_publish_locked`` while ``ready`` is staged (the up-front read commit and every other
		caller's commit pass straight through), then performs the real commit."""
		real = jobs._commit

		def commit():
			# The first frame inside jobs.py is the caller (the mock's own frames sit above it).
			frame = sys._getframe(1)
			while frame and frame.f_code.co_filename != jobs.__file__:
				frame = frame.f_back
			caller = frame.f_code.co_name if frame else ""
			if (
				caller == "_publish_locked"
				and frappe.db.get_value(jobs.DOCTYPE, job.name, "status") == "ready"
			):
				then()
			return real()

		return patch.object(jobs, "_commit", side_effect=commit)

	def test_failed_publish_commit_rolls_back_row_and_ready_then_retry_records_once(self):
		job = self.job()

		def boom():
			raise RuntimeError("commit failed")

		with self._publish_commit(job, boom), self.assertRaisesRegex(RuntimeError, "commit failed"):
			self.publish(job)
		self.assertEqual(job.reload().status, "running")
		self.assertEqual(self.new_rows(), [])
		self.publish(job)
		self.assertEqual(job.reload().status, "ready")
		self.assertEqual(len(self.new_rows()), 1)

	def test_the_row_is_staged_before_the_ready_commit(self):
		job = self.job()
		seen = []
		with self._publish_commit(job, lambda: seen.append(len(self.new_rows()))):
			self.publish(job)
		self.assertEqual(seen, [1])

	def test_terminal_failure_via_finish_records_one_failed_row(self):
		for status in ("failed", "too large", "too slow"):
			job = self.job()
			jobs._finish(job.name, "abc123", status, "boom")
			self.assertEqual(job.reload().status, status)
		rows = self.new_rows()
		self.assertEqual(len(rows), 3)
		self.assertEqual({r.outcome for r in rows}, {"failed"})
		self.assertEqual({r.row_count for r in rows}, {0})
		self.assertEqual({r.code for r in rows}, {"failed", "too large", "too slow"})
		self.assertEqual({r.actor for r in rows}, {OWNER})

	def test_second_interruption_fails_the_job_and_records_once(self):
		job = self.job()
		jobs._finish(job.name, "abc123", "paused", "x")
		self.assertEqual(self.new_rows(), [])
		frappe.db.set_value(jobs.DOCTYPE, job.name, "status", "running")
		frappe.db.commit()
		jobs._finish(job.name, "abc123", "paused", "y")
		self.assertEqual(job.reload().status, "failed")
		rows = self.new_rows()
		self.assertEqual([(r.outcome, r.row_count, r.code) for r in rows], [("failed", 0, "failed")])

	def test_finish_by_a_stale_attempt_records_nothing(self):
		job = self.job()
		jobs._finish(job.name, "stale", "failed", "wrong worker")
		self.assertEqual(self.new_rows(), [])

	def test_reaper_failure_records_one_failed_row(self):
		old = frappe.utils.add_to_date(jobs.now_datetime(), minutes=-2)
		job = self.job(heartbeat=old, started_at=old, retries=1)
		jobs._recover_row(job.name)
		self.assertEqual(job.reload().status, "failed")
		rows = self.new_rows()
		self.assertEqual(
			[(r.outcome, r.row_count, r.code, r.actor) for r in rows], [("failed", 0, "failed", OWNER)]
		)

	def test_reaper_first_retry_records_nothing(self):
		old = frappe.utils.add_to_date(jobs.now_datetime(), minutes=-2)
		job = self.job(heartbeat=old, started_at=old)
		jobs._recover_row(job.name)
		self.assertEqual(job.reload().status, "queued")
		self.assertEqual(self.new_rows(), [])

	def test_cancelled_records_nothing(self):
		# A user cancel observed by the reaper, by _finish, and a cancelled attempt at publish.
		old = frappe.utils.add_to_date(jobs.now_datetime(), minutes=-2)
		reaped = self.job(heartbeat=old, started_at=old, cancel_requested=1, retries=1)
		jobs._recover_row(reaped.name)
		self.assertEqual(reaped.reload().status, "cancelled")
		finished = self.job(cancel_requested=1)
		jobs._finish(finished.name, "abc123", "failed", "boom")
		self.assertEqual(finished.reload().status, "cancelled")
		explicit = self.job()
		jobs._finish(explicit.name, "abc123", "cancelled", "Export cancelled.")
		self.assertEqual(explicit.reload().status, "cancelled")
		published = self.job(cancel_requested=1)
		self.publish(published)
		self.assertEqual(published.reload().status, "cancelled")
		self.assertEqual(self.new_rows(), [])

	def test_enqueue_records_nothing(self):
		source = {"spec": {"from": "DocType", "select": ["name"], "limit": 2}}
		with (
			patch.object(jobs, "current_conversation", return_value=self.conversation),
			patch.object(jobs, "dispatch"),
		):
			queued = jobs.enqueue(source, "csv", "Queued only")
		self.names.append(queued["export_job"])
		self.assertEqual(frappe.db.get_value(jobs.DOCTYPE, queued["export_job"], "status"), "queued")
		self.assertEqual(self.new_rows(), [])

	def test_recording_failure_never_fails_the_export(self):
		job = self.job()
		with patch("jarvis.activity_log.record", side_effect=RuntimeError("audit down")):
			self.publish(job)
		self.assertEqual(job.reload().status, "ready")

	def test_late_worker_publish_after_the_reaper_failed_the_job_records_only_the_failure(self):
		old = frappe.utils.add_to_date(jobs.now_datetime(), minutes=-2)
		job = self.job(heartbeat=old, started_at=old, retries=1)
		jobs._recover_row(job.name)
		self.assertEqual(job.reload().status, "failed")
		self.publish(job)  # the presumed-dead worker finishes with its old token
		self.assertEqual(job.reload().status, "failed")
		self.assertEqual([r.outcome for r in self.new_rows()], ["failed"])

	def test_dispatch_settling_a_report_that_never_arrived_records_one_failed_row(self):
		_ensure_report(PREP_REPORT, prepared=1)
		self.addCleanup(
			lambda: (frappe.db.delete("Prepared Report", {"report_name": PREP_REPORT}), frappe.db.commit())
		)
		_make_pr("Queued")
		stale = frappe.utils.add_to_date(jobs.now_datetime(), seconds=-(25 * 60 + 5 * 60 + 60))
		job = self.job(
			status="queued",
			attempt_token="",
			phase="generating",
			asked_at=stale,
			source=json.dumps({"report_name": PREP_REPORT, "filters": {}}),
			source_doctype=None,
		)
		for _ in range(2):
			with (
				patch("jarvis.chat.pump._probe_worker_count", return_value=3),
				patch.object(frappe, "enqueue"),
				patch.object(jobs, "announce"),
				patch(_ENQUEUE),
			):
				jobs.dispatch()
		self.assertEqual(job.reload().status, "too slow")
		rows = self.new_rows()
		self.assertEqual(
			[(r.outcome, r.row_count, r.code, r.actor, r.channel) for r in rows],
			[("failed", 0, "too slow", OWNER, "chat")],
		)

	def test_a_row_settled_elsewhere_is_not_recorded_again_by_the_report_gate(self):
		_ensure_report(PREP_REPORT, prepared=1)
		self.addCleanup(
			lambda: (frappe.db.delete("Prepared Report", {"report_name": PREP_REPORT}), frappe.db.commit())
		)
		_make_pr("Queued")
		stale = frappe.utils.add_to_date(jobs.now_datetime(), seconds=-(25 * 60 + 5 * 60 + 60))
		job = self.job(
			status="cancelled",
			attempt_token="",
			phase="generating",
			asked_at=stale,
			source=json.dumps({"report_name": PREP_REPORT, "filters": {}}),
		)
		# A stale snapshot taken while the row still looked queued.
		snapshot = frappe.db.get_value(
			jobs.DOCTYPE, job.name, ["name", "owner", "source", "asked_at", "phase"], as_dict=True
		)
		with patch.object(jobs, "announce"):
			self.assertFalse(jobs._report_copy_ready(snapshot))
		self.assertEqual(job.reload().status, "cancelled")
		self.assertEqual(self.new_rows(), [])

	# --- report gate locking (fix wave A8) ---------------------------------------------
	# ``_report_copy_ready`` is polled by the dispatcher inside ITS transaction. The re-check
	# of a row about to be settled takes a row lock; it must not end that transaction (a bare
	# ``frappe.db.rollback()`` also discards the dispatcher's own uncommitted reservations) and
	# it must not wait out a long lock-wait on a busy row.

	def _stale_waiting_job(self, **values):
		_ensure_report(PREP_REPORT, prepared=1)
		self.addCleanup(
			lambda: (frappe.db.delete("Prepared Report", {"report_name": PREP_REPORT}), frappe.db.commit())
		)
		_make_pr("Queued")
		stale = frappe.utils.add_to_date(jobs.now_datetime(), seconds=-(25 * 60 + 5 * 60 + 60))
		job = self.job(
			**{
				"status": "queued",
				"attempt_token": "",
				"phase": "generating",
				"asked_at": stale,
				"source": json.dumps({"report_name": PREP_REPORT, "filters": {}}),
				"source_doctype": None,
				**values,
			}
		)
		snapshot = frappe.db.get_value(
			jobs.DOCTYPE, job.name, ["name", "owner", "source", "asked_at", "phase"], as_dict=True
		)
		return job, snapshot

	def _spy_get_value(self, locked_answer=None, locked_raises=None):
		"""Wrap ``frappe.db.get_value``; answer the FOR UPDATE read from the test instead."""
		real = frappe.db.get_value
		locked_calls = []

		def get_value(*a, **kw):
			if kw.get("for_update"):
				locked_calls.append((a, kw))
				if locked_raises is not None:
					raise locked_raises
				return locked_answer
			return real(*a, **kw)

		return locked_calls, patch.object(frappe.db, "get_value", side_effect=get_value)

	def test_gate_row_settled_elsewhere_is_found_by_the_unlocked_precheck_without_locking(self):
		job, snapshot = self._stale_waiting_job(status="cancelled")
		locked_calls, spy = self._spy_get_value()
		with spy, patch.object(frappe.db, "rollback") as rollback, patch.object(jobs, "announce"):
			self.assertFalse(jobs._report_copy_ready(snapshot))
		self.assertEqual(locked_calls, [])  # nothing to lock: it already settled
		rollback.assert_not_called()
		self.assertEqual(job.reload().status, "cancelled")

	def test_gate_row_settled_between_precheck_and_lock_rolls_back_to_the_savepoint_only(self):
		job, snapshot = self._stale_waiting_job()  # still queued for the pre-check
		settled = frappe._dict(status="cancelled", owner=OWNER, source_doctype=None, conversation=None)
		locked_calls, spy = self._spy_get_value(locked_answer=settled)
		with spy, patch.object(frappe.db, "rollback") as rollback, patch.object(jobs, "announce"):
			self.assertFalse(jobs._report_copy_ready(snapshot))
		self.assertEqual(len(locked_calls), 1)
		self.assertTrue(rollback.called)
		for call in rollback.call_args_list:
			self.assertTrue(
				call.kwargs.get("save_point"), "a bare rollback ends the dispatcher's transaction"
			)
		self.assertEqual(job.reload().status, "queued")  # untouched here
		self.assertEqual(self.new_rows(), [])

	def test_gate_lock_wait_timeout_means_not_ready_and_settles_nothing(self):
		job, snapshot = self._stale_waiting_job()
		locked_calls, spy = self._spy_get_value(locked_raises=frappe.QueryTimeoutError())
		with spy, patch.object(jobs, "announce") as announce, patch.object(jobs, "_log_failure") as failure:
			self.assertFalse(jobs._report_copy_ready(snapshot))  # False, NOT "ready": it is still waiting
		self.assertEqual(len(locked_calls), 1)
		failure.assert_not_called()  # a busy row is not a failed peek
		announce.assert_not_called()
		self.assertEqual(job.reload().status, "queued")
		self.assertEqual(self.new_rows(), [])

	def test_gate_happy_settle_still_records_one_failed_row(self):
		job, snapshot = self._stale_waiting_job()
		with patch.object(jobs, "announce"):
			self.assertFalse(jobs._report_copy_ready(snapshot))
		self.assertEqual(job.reload().status, "too slow")
		self.assertEqual([(r.outcome, r.code) for r in self.new_rows()], [("failed", "too slow")])
