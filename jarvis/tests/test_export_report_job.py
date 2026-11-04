"""A prepared report that is not ready becomes a background export job that waits for the
report's own copy and delivers the file on the export card (#2102, option 2)."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date

from jarvis.tests.test_run_report import _ENQUEUE, PREP_REPORT, _completed, _ensure_report, _make_pr
from jarvis.tools._export import jobs, reports
from jarvis.tools.export_query import export_query

COLUMNS = [
	{"fieldname": "name", "label": "ID", "fieldtype": "Link", "options": "ToDo"},
	{"fieldname": "description", "label": "Description", "fieldtype": "Data"},
	{"fieldname": "date", "label": "Date", "fieldtype": "Date"},
]


def _rows(n):
	return [{"name": f"r{i}", "description": f"row {i}", "date": "2026-10-21"} for i in range(n)]


class TestReportExportJob(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_report(PREP_REPORT, prepared=1)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.delete("Prepared Report", {"report_name": PREP_REPORT})
		self.conversation = (
			frappe.get_doc({"doctype": "Jarvis Conversation", "title": "report export job test"})
			.insert(ignore_permissions=True)
			.name
		)
		self.names = []
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in self.names:
			if frappe.db.exists(jobs.DOCTYPE, name):
				job = frappe.get_doc(jobs.DOCTYPE, name)
				jobs._delete_file(job)
				frappe.db.delete(jobs.DOCTYPE, {"name": name})
		frappe.db.delete(
			"Notification Log", {"document_type": jobs.DOCTYPE, "document_name": ["in", self.names]}
		)
		frappe.db.delete(
			"Error Log", {"reference_doctype": jobs.DOCTYPE, "reference_name": ["in", self.names]}
		)
		frappe.db.delete("Jarvis Chat Message", {"conversation": self.conversation})
		frappe.db.delete("Jarvis Conversation", {"name": self.conversation})
		frappe.db.delete("Prepared Report", {"report_name": PREP_REPORT})
		frappe.db.commit()
		super().tearDown()

	def _in_chat(self):
		return patch.object(jobs, "current_conversation", return_value=self.conversation)

	def _track(self, state):
		if state and state.get("export_job"):
			self.names.append(state["export_job"])
		return state

	def _job(self, **values):
		job = frappe.get_doc(
			{
				"doctype": jobs.DOCTYPE,
				"conversation": self.conversation,
				"source": json.dumps({"report_name": PREP_REPORT, "filters": {}}),
				"title": PREP_REPORT,
				"format": "csv",
				"status": "running",
				"attempt_token": "abc123",
				"heartbeat": jobs.now_datetime(),
				**values,
			}
		)
		job.flags.jarvis_server_write = True
		job.insert(ignore_permissions=True)
		self.names.append(job.name)
		frappe.db.commit()
		return job

	# --- the hand-off from export_query ---------------------------------------
	def test_not_ready_report_becomes_a_generating_job(self):
		before = frappe.db.count("Prepared Report", {"report_name": PREP_REPORT})
		with self._in_chat(), patch(_ENQUEUE), patch.object(jobs, "dispatch"):
			state = self._track(export_query(report_name=PREP_REPORT, format="csv"))
		self.assertEqual(state["status"], "queued")
		self.assertEqual(state["phase"], "generating")
		self.assertEqual(state["title"], PREP_REPORT)
		job = frappe.get_doc(jobs.DOCTYPE, state["export_job"])
		self.assertEqual(json.loads(job.source), {"report_name": PREP_REPORT, "filters": {}})
		# One run started (by the status lookup), none by the job itself.
		self.assertEqual(frappe.db.count("Prepared Report", {"report_name": PREP_REPORT}), before + 1)
		# A repeat in the same chat returns the same job, no second one.
		with self._in_chat(), patch(_ENQUEUE), patch.object(jobs, "dispatch"):
			again = export_query(report_name=PREP_REPORT, format="csv")
		self.assertEqual(again["export_job"], state["export_job"])

	def test_without_a_chat_the_status_envelope_is_the_answer(self):
		with patch.object(jobs, "current_conversation", return_value=None), patch(_ENQUEUE):
			env = export_query(report_name=PREP_REPORT, format="csv")
		self.assertTrue(env.get("prepared_report"))
		self.assertEqual(env["status"], "started")
		self.assertNotIn("filters", env)
		self.assertNotIn("export_job", env)

	def test_a_ready_copy_over_the_immediate_limit_goes_to_the_job(self):
		with (
			self._in_chat(),
			patch.object(reports, "immediate_row_limit", return_value=2),
			_completed(_rows(3), columns=COLUMNS),
			patch.object(jobs, "dispatch"),
		):
			state = self._track(export_query(report_name=PREP_REPORT))
		self.assertEqual(state["status"], "queued")

	# --- the dispatcher gate ---------------------------------------------------
	def test_dispatch_leaves_a_generating_job_queued_and_takes_a_ready_one(self):
		_make_pr("Queued")
		job = self._job(status="queued", attempt_token="", phase="generating", asked_at=jobs.now_datetime())
		row = frappe.db.get_value(
			jobs.DOCTYPE, job.name, ["name", "owner", "source", "asked_at", "phase"], as_dict=True
		)
		self.assertFalse(jobs._report_copy_ready(row))
		self.assertEqual(frappe.db.get_value(jobs.DOCTYPE, job.name, "status"), "queued")
		frappe.db.delete("Prepared Report", {"report_name": PREP_REPORT})
		with _completed(_rows(2), columns=COLUMNS):
			self.assertTrue(jobs._report_copy_ready(row))

	def test_dispatch_settles_a_run_that_never_completes(self):
		_make_pr("Queued")
		stale = add_to_date(jobs.now_datetime(), seconds=-(25 * 60 + 5 * 60 + 60))
		job = self._job(status="queued", attempt_token="", phase="generating", asked_at=stale)
		row = frappe.db.get_value(
			jobs.DOCTYPE, job.name, ["name", "owner", "source", "asked_at", "phase"], as_dict=True
		)
		with patch.object(jobs, "announce") as announce:
			self.assertFalse(jobs._report_copy_ready(row))
		self.assertEqual(frappe.db.get_value(jobs.DOCTYPE, job.name, "status"), "too slow")
		announce.assert_called_once()

	def test_a_plain_source_is_always_ready(self):
		job = self._job(source=json.dumps({"spec": {"from": "DocType", "select": ["name"], "limit": 2}}))
		row = frappe.db.get_value(
			jobs.DOCTYPE, job.name, ["name", "owner", "source", "asked_at", "phase"], as_dict=True
		)
		self.assertTrue(jobs._report_copy_ready(row))

	# --- the worker ------------------------------------------------------------
	def test_worker_converts_the_copy_and_publishes(self):
		import csv

		job = self._job()
		with _completed(_rows(3), columns=COLUMNS), patch.object(jobs, "dispatch"):
			jobs.run(job.name, "abc123")
		job.reload()
		self.assertEqual(job.status, "ready", job.error)
		self.assertEqual(job.rows_written, 3)
		self.assertEqual(job.total_rows, 3)
		self.assertTrue(jobs._file_exists(job))
		content = frappe.get_doc("File", job.file).get_content()
		text = content.decode() if isinstance(content, bytes) else content
		lines = list(csv.reader(text.splitlines()))
		self.assertEqual(lines[0], ["ID", "Description", "Date"])
		self.assertEqual(len(lines), 4)
		self.assertEqual(lines[1][0], "r0")

	def test_worker_with_no_copy_restarts_the_run_and_requeues(self):
		job = self._job()
		before = frappe.db.count("Prepared Report", {"report_name": PREP_REPORT})
		with patch(_ENQUEUE), patch.object(jobs, "dispatch"):
			jobs.run(job.name, "abc123")
		job.reload()
		self.assertEqual(job.status, "queued")
		self.assertEqual(job.phase, "generating")
		self.assertEqual(frappe.db.count("Prepared Report", {"report_name": PREP_REPORT}), before + 1)

	def test_worker_reports_a_failed_run(self):
		_make_pr("Error")
		job = self._job()
		with patch.object(jobs, "dispatch"):
			jobs.run(job.name, "abc123")
		job.reload()
		self.assertEqual(job.status, "failed")
		self.assertIn("failed to generate", job.error)

	def test_worker_refuses_over_the_job_row_cap(self):
		job = self._job()
		with (
			_completed(_rows(3), columns=COLUMNS),
			patch.object(jobs, "limits", return_value=(2, 50 * 1024 * 1024)),
			patch.object(jobs, "dispatch"),
		):
			jobs.run(job.name, "abc123")
		job.reload()
		self.assertEqual(job.status, "too large")

	# --- the wake-up ------------------------------------------------------------
	def test_a_finished_prepared_run_wakes_the_dispatcher(self):
		self._job(status="queued", attempt_token="", phase="generating")
		doc = frappe.get_doc("Prepared Report", _make_pr("Completed"))
		with patch.object(jobs.frappe, "enqueue") as enqueue:
			jobs.on_prepared_report_update(doc)
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.args[0], "jarvis.tools._export.jobs.dispatch")
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

	def test_a_run_nobody_waits_for_wakes_nothing(self):
		doc = frappe.get_doc("Prepared Report", _make_pr("Completed"))
		with patch.object(jobs.frappe, "enqueue") as enqueue:
			jobs.on_prepared_report_update(doc)
		enqueue.assert_not_called()
		doc.status = "Queued"
		self._job(status="queued", attempt_token="", phase="generating")
		with patch.object(jobs.frappe, "enqueue") as enqueue:
			jobs.on_prepared_report_update(doc)
		enqueue.assert_not_called()

	# --- the card --------------------------------------------------------------
	def test_public_state_shows_the_generating_phase_while_queued(self):
		job = self._job(status="queued", attempt_token="", phase="generating")
		state = jobs.public(jobs._get_job(job.name))
		self.assertEqual(state["phase"], "generating")

	def test_cancel_while_waiting_for_the_copy(self):
		job = self._job(status="queued", attempt_token="", phase="generating")
		with patch.object(jobs, "dispatch"):
			state = jobs.cancel(job.name)
		self.assertEqual(state["status"], "cancelled")
