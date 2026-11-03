from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import jobs, store
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarJobs(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_jobs")
		self.fx = CalendarFixtures()
		self.scope = normalize_scope(self.fx.company, [])
		self.lookup = patch.object(jobs, "get_job", return_value=None).start()
		self.enqueue = patch.object(frappe, "enqueue", return_value=object()).start()

	def tearDown(self):
		patch.stopall()
		store.purge_actor("Administrator")
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_jobs")

	def request(self):
		return jobs.request_snapshot(self.scope)

	def test_request_joins_an_active_job_without_enqueueing_twice(self):
		first = self.request()
		second = self.request()
		self.assertEqual(first["snapshot_id"], second["snapshot_id"])
		self.assertEqual(self.enqueue.call_count, 1)
		kwargs = self.enqueue.call_args.kwargs
		self.assertEqual(kwargs["queue"], "long")
		self.assertEqual(kwargs["timeout"], 300)
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(
			set(kwargs) - {"queue", "timeout", "job_id", "deduplicate", "enqueue_after_commit"},
			{"actor", "snapshot_id"},
		)

	def test_job_cannot_borrow_the_queued_actors_privileges(self):
		sid = self.request()["snapshot_id"]
		with patch.object(jobs, "collect") as collect:
			frappe.set_user("Guest")
			jobs.run_snapshot(actor="Administrator", snapshot_id=sid)
			collect.assert_not_called()
		self.assertIsNone(store.load_result("Administrator", sid))

	def test_framework_retry_errors_do_not_escape_or_leak_source_text(self):
		sid = self.request()["snapshot_id"]
		with (
			patch.object(jobs, "collect", side_effect=frappe.RetryBackgroundJobError("PRIVATE_SUPPLIER_123")),
			patch.object(frappe, "log_error") as log,
		):
			jobs.run_snapshot(actor="Administrator", snapshot_id=sid)
			self.assertNotIn("PRIVATE_SUPPLIER_123", str(log.call_args))
		self.assertEqual(store.load_status("Administrator", sid)["state"], "failed")
		self.assertIsNone(store.load_result("Administrator", sid))

	def test_enqueue_failure_is_recoverable(self):
		self.enqueue.side_effect = ConnectionError("PRIVATE_SOURCE")
		first = self.request()
		self.assertEqual(first["state"], "failed")
		self.enqueue.side_effect = None
		self.assertNotEqual(self.request()["snapshot_id"], first["snapshot_id"])

	def test_cache_loss_during_an_old_rq_run_can_start_a_fresh_fenced_run(self):
		from types import SimpleNamespace

		self.lookup.return_value = SimpleNamespace(get_status=lambda **kwargs: "started")
		result = self.request()
		self.assertEqual(result["state"], "queued")
		self.assertIn(result["snapshot_id"], self.enqueue.call_args.kwargs["job_id"])

	def test_a_skipped_enqueue_does_not_leave_an_orphan_queued_status(self):
		self.enqueue.return_value = None
		result = self.request()
		self.assertEqual(result["state"], "failed")
		self.assertEqual(result["code"], "worker_unavailable")

	def test_expired_run_recovers_while_its_real_rq_hash_is_still_started(self):
		from frappe.utils.background_jobs import create_job_id, get_redis_conn
		from rq.job import Job, JobStatus

		old = self.request()["snapshot_id"]
		status = store.load_status("Administrator", old)
		store.mark_running("Administrator", old)
		rq_job = Job.create(
			"jarvis.payment_calendar.jobs.run_snapshot",
			id=create_job_id("pcal:" + status["scope_hash"] + ":" + old),
			connection=get_redis_conn(),
		)
		rq_job.save()
		rq_job.set_status(JobStatus.STARTED)
		try:
			frappe.cache.expire(store._key("Administrator", old, "status"), 0)
			frappe.cache.expire(store._active(status["scope_hash"]), 0)
			new = self.request()["snapshot_id"]
			self.assertNotEqual(new, old)
			self.assertEqual(rq_job.get_status(refresh=True), JobStatus.STARTED)
			self.assertIn(new, self.enqueue.call_args.kwargs["job_id"])
			self.assertFalse(store.publish("Administrator", old, {"sources": {}}))
		finally:
			rq_job.delete()

	def test_late_output_after_purge_cannot_recreate_a_result(self):
		sid = self.request()["snapshot_id"]

		def collect(*args):
			store.purge_actor("Administrator")
			return {"sources": {}, "projection": {}}

		with patch.object(jobs, "collect", side_effect=collect):
			jobs.run_snapshot(actor="Administrator", snapshot_id=sid)
		self.assertIsNone(store.load_result("Administrator", sid))
