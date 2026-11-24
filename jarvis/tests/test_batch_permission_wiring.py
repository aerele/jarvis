"""Batch permission wiring (S2b-batch, T3): reader stop, job stop, event flush, module class, site counter.

Nothing here commits: the Redis keys of a fixed test date are deleted after each test, `frappe.log_error` and
`read_log.error_log_once` are patched where a test would write an Error Log row.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_batch_permission_wiring
"""

from contextlib import contextmanager
from time import monotonic
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from jarvis.payment_calendar import batch_permission, builtin_reader, jobs, read_log, routing, store
from jarvis.payment_calendar.errors import ReaderFallback
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site
from jarvis.tests.payment_calendar_results import schema2_result

DATE = "2031-05-01"
KLASS = "permission_batch:wiring"


def redis(*command):
	return frappe.cache.execute_command(*command)


class TestSiteCount(TestCase):
	def setUp(self):
		self.key = frappe.cache.make_key(f"jarvis:pcal:site:day:{DATE}:{KLASS}")
		redis("DEL", self.key)
		self.addCleanup(redis, "DEL", self.key)

	def test_it_returns_the_new_value_of_the_day_counter(self):
		self.assertEqual(
			[read_log.site_count(KLASS, DATE) for _ in range(3)],
			[1, 2, 3],
		)

	def test_the_counter_expires_with_the_company_day_counters(self):
		read_log.site_count(KLASS, DATE)
		self.assertEqual(redis("TTL", self.key), read_log.COUNTER_TTL_S)

	def test_every_call_renews_the_expiry(self):
		read_log.site_count(KLASS, DATE)
		redis("EXPIRE", self.key, 100)
		read_log.site_count(KLASS, DATE)
		self.assertEqual(redis("TTL", self.key), read_log.COUNTER_TTL_S)

	def test_a_class_and_a_day_each_have_their_own_counter(self):
		other_day, other_class = "2031-05-02", "permission_mismatch"
		keys = [
			frappe.cache.make_key(f"jarvis:pcal:site:day:{other_day}:{KLASS}"),
			frappe.cache.make_key(f"jarvis:pcal:site:day:{DATE}:{other_class}"),
		]
		for key in keys:
			redis("DEL", key)
			self.addCleanup(redis, "DEL", key)
		read_log.site_count(KLASS, DATE)
		self.assertEqual(read_log.site_count(KLASS, other_day), 1)
		self.assertEqual(read_log.site_count(other_class, DATE), 1)

	def test_the_key_is_site_level_under_the_calendar_prefix(self):
		read_log.site_count(KLASS, DATE)
		self.assertTrue(self.key.decode().split("jarvis:pcal:", 1)[1].startswith("site:day:"))
		self.assertEqual(redis("EXISTS", self.key), 1)

	def test_a_redis_error_propagates_to_the_caller(self):
		with (
			patch.object(frappe.cache, "execute_command", side_effect=ConnectionError("down")),
			self.assertRaises(ConnectionError),
		):
			read_log.site_count(KLASS, DATE)


class TestModuleClass(TestCase):
	def test_batch_permission_changes_the_reader_build(self):
		self.assertIn("batch_permission", routing.FIGURE_MODULES)
		self.assertNotIn("batch_permission", routing.OTHER_MODULES)


def switch(on: bool):
	return patch.object(batch_permission, "switch_enabled", return_value=on)


def forget_freeze():
	if hasattr(frappe.local, batch_permission.FREEZE):
		delattr(frappe.local, batch_permission.FREEZE)


def hooks(present: bool):
	return patch.object(builtin_reader, "effective_hooks", return_value=["a.hook"] if present else [])


class TestReaderStop(TestCase):
	"""`_permission_deadline`: the switch picks the mechanism, and it is read once at entry."""

	def entered(self, deadline, *, on, hook):
		"""The stop spy, and whether `frappe.has_permission` was rebound inside, for one entry."""
		original = frappe.has_permission
		with (
			switch(on) as read,
			hooks(hook),
			patch.object(batch_permission, "permission_stop", wraps=batch_permission.permission_stop) as stop,
			builtin_reader._permission_deadline(deadline),
		):
			rebound = frappe.has_permission is not original
		self.assertIs(frappe.has_permission, original, "the entry always restores the binding")
		return stop, rebound, read

	def test_switch_on_rebinds_nothing_with_or_without_a_hook(self):
		for hook in (True, False):
			with self.subTest(hook=hook):
				stop, rebound, _ = self.entered(monotonic() + 60, on=True, hook=hook)
				self.assertFalse(rebound)
				self.assertEqual(stop.call_count, 1)

	def test_switch_on_stops_at_the_deadline_plus_the_grace_with_a_deadline_fallback(self):
		deadline = monotonic() + 60
		stop, _, _ = self.entered(deadline, on=True, hook=True)
		stop_at, factory = stop.call_args.args
		self.assertEqual(stop_at, deadline + builtin_reader.ASSEMBLE_GRACE_S)
		error = factory()
		self.assertIsInstance(error, ReaderFallback)
		self.assertEqual(error.reason, "deadline")
		self.assertNotIsInstance(error, TimeoutError)

	def test_switch_on_a_check_past_the_stop_raises_the_fallback_and_the_stop_ends_with_the_entry(self):
		with switch(True), hooks(False):
			with builtin_reader._permission_deadline(monotonic() - 100):
				with self.assertRaises(ReaderFallback) as raised:
					batch_permission.check_permission_stop()
			self.assertEqual(raised.exception.reason, "deadline")
			batch_permission.check_permission_stop()

	def test_switch_on_a_check_before_the_stop_passes(self):
		with switch(True), hooks(False), builtin_reader._permission_deadline(monotonic() + 60):
			batch_permission.check_permission_stop()

	def test_switch_off_with_a_hook_wraps_has_permission_as_today_and_sets_no_stop(self):
		stop, rebound, _ = self.entered(monotonic() + 60, on=False, hook=True)
		self.assertTrue(rebound)
		stop.assert_not_called()

	def test_switch_off_the_wrapper_raises_the_deadline_fallback_past_the_stop(self):
		with switch(False), hooks(True), builtin_reader._permission_deadline(monotonic() - 100):
			with self.assertRaises(ReaderFallback) as raised:
				frappe.has_permission("User", "read", doc="Administrator", user="Administrator")
			self.assertEqual(raised.exception.reason, "deadline")
			batch_permission.check_permission_stop()

	def test_switch_off_without_a_hook_wraps_nothing_and_sets_no_stop(self):
		stop, rebound, _ = self.entered(monotonic() + 60, on=False, hook=False)
		self.assertFalse(rebound)
		stop.assert_not_called()

	def test_no_deadline_means_no_stop_and_no_wrapper(self):
		for on in (True, False):
			with self.subTest(on=on):
				stop, rebound, _ = self.entered(None, on=on, hook=True)
				self.assertFalse(rebound)
				stop.assert_not_called()

	def test_the_switch_is_read_once_per_entry(self):
		for on in (True, False):
			with self.subTest(on=on):
				_, _, read = self.entered(monotonic() + 60, on=on, hook=True)
				self.assertEqual(read.call_count, 1)


class JobCase(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("batch_wiring")
		self.fx = CalendarFixtures()
		self.scope = normalize_scope(self.fx.company, [])
		patch.object(jobs, "get_job", return_value=None).start()
		patch.object(frappe, "enqueue", return_value=object()).start()
		patch.object(frappe, "log_error").start()
		self.error_log = patch.object(read_log, "error_log_once").start()
		frappe.local.pcal_permission_events = []
		self.sid = jobs.request_snapshot(self.scope)["snapshot_id"]

	def tearDown(self):
		patch.stopall()
		store.purge_actor("Administrator")
		frappe.local.pcal_permission_events = []
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="batch_wiring")

	def run_job(self):
		jobs.run_snapshot(actor="Administrator", snapshot_id=self.sid)
		return store.load_status("Administrator", self.sid)


class TestJobStop(JobCase):
	def test_the_stop_is_inside_the_rq_timeout_and_outside_the_reader(self):
		self.assertEqual(jobs.JOB_PERMISSION_STOP_S, 280)
		reader_total = routing.JOB_BUDGET_S + routing.READER_DEADLINE_S + builtin_reader.ASSEMBLE_GRACE_S
		self.assertGreater(jobs.JOB_PERMISSION_STOP_S, reader_total)
		# The RQ timeout given to `frappe.enqueue` is 300 s.
		self.assertLess(jobs.JOB_PERMISSION_STOP_S, 300)

	def stop_trace(self, *, on):
		"""The order of the stop and the job's two permission steps, with a recording stop."""
		trace = []

		@contextmanager
		def recording(stop, factory):
			trace.append(("enter", stop, factory))
			try:
				yield
			finally:
				trace.append("exit")

		def collect(*args):
			trace.append("collect")
			return schema2_result()

		with (
			switch(on),
			patch.object(jobs, "monotonic", return_value=1000.0),
			patch.object(batch_permission, "permission_stop", recording),
			patch.object(jobs, "collect", side_effect=collect),
			patch.object(jobs, "authorize_scope", side_effect=lambda *a, **k: trace.append("authorize")),
		):
			status = self.run_job()
		return trace, status

	def test_switch_on_the_stop_covers_collect_and_the_publish_authorization(self):
		trace, status = self.stop_trace(on=True)
		self.assertEqual(
			trace,
			[("enter", 1000.0 + jobs.JOB_PERMISSION_STOP_S, TimeoutError), "collect", "authorize", "exit"],
		)
		self.assertEqual(status["state"], "complete")

	def test_switch_off_no_stop_is_set(self):
		trace, status = self.stop_trace(on=False)
		self.assertEqual(trace, ["collect", "authorize"])
		self.assertEqual(status["state"], "complete")

	def expired(self):
		"""A job clock that is 10 s past the job stop."""
		late = jobs.JOB_PERMISSION_STOP_S + 10
		return patch.object(jobs, "monotonic", side_effect=lambda: monotonic() - late)

	def per_document_loop(self, names=20_000):
		"""Stands in for collect on the per-document path: one stop check per name; the checks seen."""
		checks = []

		def collect(*args):
			for _ in range(names):
				checks.append(1)
				batch_permission.check_permission_stop()
			return schema2_result()

		return checks, collect

	def test_switch_on_a_per_document_collect_past_the_stop_fails_as_a_timeout_at_the_first_check(self):
		checks, collect = self.per_document_loop()
		with switch(True), self.expired(), patch.object(jobs, "collect", side_effect=collect):
			status = self.run_job()
		self.assertEqual(len(checks), 1)
		self.assertEqual((status["state"], status["code"]), ("failed", "timeout"))
		batch_permission.check_permission_stop()

	def test_switch_on_a_per_document_collect_inside_the_stop_runs_to_the_end(self):
		checks, collect = self.per_document_loop(2_000)
		with (
			switch(True),
			patch.object(jobs, "monotonic", side_effect=lambda: monotonic() - 100),
			patch.object(jobs, "collect", side_effect=collect),
			patch.object(jobs, "authorize_scope"),
		):
			status = self.run_job()
		self.assertEqual(len(checks), 2_000)
		self.assertEqual(status["state"], "complete")

	def test_switch_on_the_publish_authorization_is_stopped_too(self):
		publish_checks = []

		def authorize(*args, **kwargs):
			publish_checks.append(1)
			batch_permission.check_permission_stop()

		with (
			switch(True),
			self.expired(),
			patch.object(jobs, "collect", return_value=schema2_result()),
			patch.object(jobs, "authorize_scope", side_effect=authorize),
		):
			status = self.run_job()
		self.assertEqual(publish_checks, [1])
		self.assertEqual((status["state"], status["code"]), ("failed", "timeout"))

	def test_switch_off_the_same_collect_is_never_stopped(self):
		checks, collect = self.per_document_loop(500)
		with (
			switch(False),
			self.expired(),
			patch.object(jobs, "collect", side_effect=collect),
			patch.object(jobs, "authorize_scope"),
			patch.object(batch_permission, "permission_stop") as stop,
		):
			status = self.run_job()
		stop.assert_not_called()
		self.assertEqual(len(checks), 500)
		self.assertEqual(status["state"], "complete")

	def frozen_during_collect(self, *, on):
		"""The one-mode-per-job record as `collect` sees it: None when the job did not start one."""
		seen = []

		def collect(*args):
			seen.append(getattr(frappe.local, batch_permission.FREEZE, None))

		with (
			switch(on),
			patch.object(jobs, "collect", side_effect=collect),
			patch.object(jobs, "authorize_scope"),
		):
			forget_freeze()
			self.run_job()
		forget_freeze()
		return seen

	def test_switch_on_the_job_starts_the_one_mode_record(self):
		self.assertEqual(self.frozen_during_collect(on=True), [{}])

	def test_switch_off_the_job_starts_no_record(self):
		self.assertEqual(self.frozen_during_collect(on=False), [None])

	def test_the_next_job_in_the_process_has_no_stop(self):
		checks, collect = self.per_document_loop(1)
		with switch(True), self.expired(), patch.object(jobs, "collect", side_effect=collect):
			self.run_job()
		batch_permission.check_permission_stop()


class TestEventFlush(JobCase):
	@contextmanager
	def finishing_job(self):
		with (
			patch.object(jobs, "collect", return_value=schema2_result()),
			patch.object(jobs, "authorize_scope"),
		):
			yield

	EVENTS = (
		{"operation": "pcal_permission_pin_drift", "message": "pin drift file=3"},
		{"operation": "pcal_permission_breaker", "message": "tripped"},
	)

	def queue(self):
		frappe.local.pcal_permission_events = [dict(event) for event in self.EVENTS]

	def written(self):
		return [(call.args[1], call.args[2]) for call in self.error_log.call_args_list]

	def test_a_finished_job_writes_each_event_once_and_clears_the_list(self):
		self.queue()
		with self.finishing_job():
			self.run_job()
		self.assertEqual(
			[(message, operation.split(":")[0]) for message, operation in self.written()],
			[(event["message"], event["operation"]) for event in self.EVENTS],
		)
		self.assertEqual(frappe.local.pcal_permission_events, [])

	def test_a_failed_job_writes_the_events_after_the_failure_is_recorded(self):
		self.queue()
		order = []
		real_fail = jobs._fail_safely

		def fail(*args, **kwargs):
			order.append("fail")
			real_fail(*args, **kwargs)

		self.error_log.side_effect = lambda *a, **k: order.append("event")
		with (
			patch.object(jobs, "collect", side_effect=lambda *a: order.append("collect") or 1 / 0),
			patch.object(jobs, "_fail_safely", side_effect=fail),
		):
			status = self.run_job()
		self.assertEqual(order, ["collect", "fail", "event", "event"])
		self.assertEqual(status["state"], "failed")
		self.assertEqual(frappe.local.pcal_permission_events, [])

	def test_a_job_without_events_writes_nothing(self):
		with self.finishing_job():
			self.run_job()
		self.error_log.assert_not_called()

	def test_a_failed_event_write_neither_fails_the_job_nor_drops_the_next_event(self):
		self.queue()
		self.error_log.side_effect = [RuntimeError("insert failed"), None]
		with self.finishing_job():
			status = self.run_job()
		self.assertEqual(status["state"], "complete")
		self.assertEqual(self.error_log.call_count, 2)
		self.assertEqual(frappe.local.pcal_permission_events, [])

	def test_a_timeout_on_one_event_write_still_tries_every_event_then_reraises_it(self):
		self.queue()
		self.error_log.side_effect = [TimeoutError(), None]
		with self.finishing_job(), self.assertRaises(TimeoutError):
			self.run_job()
		self.assertEqual(self.error_log.call_count, 2)
		self.assertEqual(frappe.local.pcal_permission_events, [])

	def test_distinct_events_of_one_operation_get_distinct_dedupe_keys_and_a_repeat_the_same(self):
		def event(message):
			return {"operation": "pcal_permission_breaker", "message": message}

		frappe.local.pcal_permission_events = [
			event("mode changed: A"),
			event("mode changed: B"),
			event("mode changed: A"),
		]
		with self.finishing_job():
			self.run_job()
		keys = [(call.args[2], call.args[3]) for call in self.error_log.call_args_list]
		self.assertEqual(keys[0], keys[2])
		self.assertNotEqual(keys[0], keys[1])
		self.assertTrue(all(operation.startswith("pcal_permission_breaker") for operation, _ in keys))
		self.assertNotIn("mode changed", "".join(operation for operation, _ in keys))

	def test_the_events_are_written_after_the_job_view_has_ended(self):
		"""The flush runs after `collect` returned, so the view that `collect` opened is closed (AC-B26)."""
		self.queue()
		view = {"open": False}

		def collect(*args):
			view["open"] = True
			try:
				return schema2_result()
			finally:
				view["open"] = False

		self.error_log.side_effect = lambda *a, **k: self.assertFalse(view["open"])
		with patch.object(jobs, "collect", side_effect=collect), patch.object(jobs, "authorize_scope"):
			self.run_job()
		self.assertEqual(self.error_log.call_count, 2)
