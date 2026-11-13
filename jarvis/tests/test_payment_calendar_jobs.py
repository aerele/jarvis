import json
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from jarvis.payment_calendar import jobs, store
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site
from jarvis.tests.payment_calendar_results import SCOPE, schema2_result


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

	def assert_no_result_stored(self, sid):
		"""The reader assembles parts, so `load_result is None` alone does not prove nothing is stored."""
		self.assertIsNone(store.load_result("Administrator", sid))
		keys = [store._key("Administrator", sid, "result")]
		keys += [store._part_key("Administrator", sid, part) for part in store.PARTS]
		keys += [store._key("Administrator", sid, name) for name in store.HASHES]
		for key in keys:
			self.assertEqual(frappe.cache.execute_command("EXISTS", key), 0, key)

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
		self.assert_no_result_stored(sid)

	def test_a_balances_status_row_is_never_run_as_a_snapshot(self):
		scope = {"actor": "Administrator", "company": self.fx.company, "accounts": [], "anchor": "2026-11-11"}
		sid, _ = store.admit("Administrator", "f" * 64, scope, "", kind="balances")
		with patch.object(jobs, "collect") as collect:
			jobs.run_snapshot(actor="Administrator", snapshot_id=sid)
		collect.assert_not_called()
		self.assertEqual(store.load_status("Administrator", sid)["state"], "queued")

	def test_framework_retry_errors_do_not_escape_or_leak_source_text(self):
		sid = self.request()["snapshot_id"]
		with (
			patch.object(jobs, "collect", side_effect=frappe.RetryBackgroundJobError("PRIVATE_SUPPLIER_123")),
			patch.object(frappe, "log_error") as log,
		):
			jobs.run_snapshot(actor="Administrator", snapshot_id=sid)
			self.assertNotIn("PRIVATE_SUPPLIER_123", str(log.call_args))
		self.assertEqual(store.load_status("Administrator", sid)["state"], "failed")
		self.assert_no_result_stored(sid)

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

	def test_a_job_that_could_not_be_queued_is_logged_with_its_kind_and_class(self):
		cases = (
			(dict(side_effect=ConnectionError("PRIVATE_SOURCE")), "ConnectionError"),
			(dict(return_value=None), "not_queued"),
		)
		for options, reason in cases:
			with self.subTest(reason=reason):
				self.enqueue.reset_mock(return_value=True, side_effect=True)
				self.enqueue.configure_mock(**options)
				logger = MagicMock()
				with patch.object(frappe, "logger", return_value=logger):
					self.assertEqual(self.request()["code"], "worker_unavailable")
				# ERROR: the production log level hides anything lower.
				logger.warning.assert_not_called()
				message = logger.error.call_args.args[0]
				for part in ("worker_unavailable", "kind=snapshot", reason):
					self.assertIn(part, message)
				self.assertNotIn("PRIVATE_SOURCE", message)
				store.purge_actor("Administrator")

	def lost_status(self):
		sid = self.request()["snapshot_id"]
		status = store.load_status("Administrator", sid)
		status["queued_at"] = "2020-01-01T00:00:00+00:00"
		return status

	def test_a_job_lost_from_rq_is_logged_at_error_level_with_its_kind(self):
		status = self.lost_status()
		logger = MagicMock()
		with patch.object(frappe, "logger", return_value=logger):
			jobs.observe_job(status)
		logger.warning.assert_not_called()
		message = logger.error.call_args.args[0]
		self.assertIn("job_lost", message)
		self.assertIn("kind=snapshot", message)

	def test_a_job_lost_is_logged_only_when_the_failure_was_stored(self):
		status = self.lost_status()
		for stored in (True, False):
			with self.subTest(stored=stored):
				logger = MagicMock()
				with (
					patch.object(frappe, "logger", return_value=logger),
					patch.object(store, "fail", return_value=stored),
				):
					jobs.observe_job(status)
				self.assertEqual(logger.error.called, stored)

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
			parts = store.split_result(schema2_result(), SCOPE)
			self.assertFalse(store.publish_snapshot("Administrator", old, parts))
		finally:
			rq_job.delete()

	def test_late_output_after_purge_cannot_recreate_a_result(self):
		sid = self.request()["snapshot_id"]

		def collect(*args):
			store.purge_actor("Administrator")
			return schema2_result()

		with patch.object(jobs, "collect", side_effect=collect), patch.object(jobs, "authorize_scope"):
			jobs.run_snapshot(actor="Administrator", snapshot_id=sid)
		self.assert_no_result_stored(sid)

	def test_a_finished_run_publishes_every_part_and_completes_the_status(self):
		sid = self.request()["snapshot_id"]
		result = schema2_result()
		with patch.object(jobs, "collect", return_value=result), patch.object(jobs, "authorize_scope"):
			jobs.run_snapshot(actor="Administrator", snapshot_id=sid)
		self.assertEqual(store.load_status("Administrator", sid)["state"], "complete")
		parts = store.split_result(result, SCOPE)
		for part in store.PARTS:
			stored = json.loads(store._json(parts[part]))
			self.assertEqual(store.load_part("Administrator", sid, part), stored)
		result_key = store._key("Administrator", sid, "result")
		self.assertEqual(frappe.cache.execute_command("EXISTS", result_key), 0)

	def test_an_incomplete_result_fails_the_job_and_the_log_names_the_missing_keys_only(self):
		sid = self.request()["snapshot_id"]
		result = schema2_result()
		del result["horizon"], result["projection"]["month_totals"]
		with (
			patch.object(jobs, "collect", return_value=result),
			patch.object(jobs, "authorize_scope"),
			patch.object(frappe, "log_error") as log,
		):
			jobs.run_snapshot(actor="Administrator", snapshot_id=sid)
		status = store.load_status("Administrator", sid)
		self.assertEqual((status["state"], status["code"]), ("failed", "internal_error"))
		self.assert_no_result_stored(sid)
		body = json.loads(log.call_args.kwargs["message"])
		self.assertEqual(body["exception_class"], "KeyError")
		self.assertEqual(body["stage"], "publish")
		self.assertEqual(body["detail"], "missing key 'horizon', 'projection.month_totals'")
