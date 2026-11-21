"""Payment Calendar S2 T5: `read_log.reader_outcomes`, the logs and counters of the routed reads.

Records come from `routing.read_side`. Nothing here commits: `frappe.log_error` is patched, and the Redis
keys of a fixed test date are deleted by digest after each test.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_reader_outcomes
"""

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from rq.timeouts import JobTimeoutException

from jarvis.payment_calendar import read_log, routing
from jarvis.payment_calendar.errors import READER_FALLBACK_REASONS

COMPANY = "Outcome Test Company"
SECRET = "ACME-PRIVATE-PARTY-4711"
KEY = "k" * 44
DATE = "2031-04-01"
DAY = 24 * 3600
# The reasons that write an Error Log row, and those that are counted only (D-5).
FALLBACK_ROWS = {
	"control_total",
	"rekey_unresolved",
	"reader_error",
	"pin_drift",
	"unsupported_major",
	"eligibility_unknown",
	"too_many_lines",
	"deadline",
	"assemble_budget",
}
COUNTED_ONLY = {"ineligible", "sales_person_scope"}
COUNTED_STEPS = {"busy", "memo", "budget", "no_key"}


def redis(*command):
	return frappe.cache.execute_command(*command)


def record(**fields):
	base = {
		"side": "Receivable",
		"path": "today",
		"outcome": "refused",
		"routed": True,
		"company_digest": read_log.company_digest(COMPANY),
		"today_ms": 5,
		"view_ms": 100,
		"reader_build": "a" * 12,
	}
	base.update(fields)
	return base


class OutcomeCase(TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		patcher = patch.dict(frappe.conf, {"encryption_key": KEY})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.digest = read_log.company_digest(COMPANY)
		self.log_error = MagicMock()
		self.logger = MagicMock()
		for patched in (
			patch.object(frappe, "log_error", self.log_error),
			patch.object(frappe, "logger", return_value=self.logger),
		):
			patched.start()
			self.addCleanup(patched.stop)
		self.clean()
		self.addCleanup(self.clean)

	def clean(self):
		keys = redis("KEYS", frappe.cache.make_key(f"jarvis:pcal:*{self.digest}*"))
		for key in keys:
			redis("DEL", key)

	def write(self, *records, date=DATE):
		read_log.reader_outcomes(list(records), date)

	def counts(self):
		return read_log.day_counts(self.digest, DATE)

	def lines(self, level="info"):
		return [call.args[0] for call in getattr(self.logger, level).call_args_list]

	def rows(self):
		return [call.kwargs for call in self.log_error.call_args_list]


class TestCounters(OutcomeCase):
	def test_a_served_read_and_each_class_are_counted_as_raw_integers(self):
		self.write(
			record(path="reader", outcome="served"),
			record(fallback="deadline", fallback_detail=[]),
			record(fallback="deadline", fallback_detail=[]),
			record(reader_refused="multiple_invoice_accounts"),
			record(reader_exception="RuntimeError"),
			record(step="probe"),
		)
		self.assertEqual(
			self.counts(),
			{
				"served": 1,
				"fallback:deadline": 2,
				"reader_refused:multiple_invoice_accounts": 1,
				"reader_exception:RuntimeError": 1,
				"probe": 1,
			},
		)

	def test_the_counter_hash_is_the_documented_key_with_a_35_day_ttl(self):
		self.write(record(fallback="deadline"))
		key = frappe.cache.make_key(f"jarvis:pcal:reader:day:{self.digest}:{DATE}")
		self.assertEqual(redis("HGET", key, "fallback:deadline"), b"1")
		self.assertAlmostEqual(redis("TTL", key), 35 * DAY, delta=10)

	def test_the_counted_only_reasons_are_counted_with_plain_names(self):
		for reason in sorted(COUNTED_ONLY):
			self.write(record(fallback=reason))
		for step in sorted(COUNTED_STEPS):
			self.write(record(step=step))
		self.assertEqual(self.counts(), dict.fromkeys(COUNTED_ONLY | COUNTED_STEPS, 1))
		# Plain names: the daily failure count does not include them.
		self.assertFalse(any(name.startswith(read_log.FAILURE_PREFIXES) for name in self.counts()))

	def test_a_side_the_switch_keeps_out_is_not_counted(self):
		self.write(record(step="settings"), record(step="v15"))
		self.assertEqual(self.counts(), {"v15": 1})

	def test_a_side_today_served_or_refused_otherwise_is_not_counted(self):
		self.write(
			record(routed=False, outcome="served"),
			record(routed=False, refusal="forbidden"),
		)
		self.assertEqual(self.counts(), {})

	def test_a_record_without_a_digest_is_logged_but_not_counted(self):
		self.write(record(company_digest=None, fallback="deadline"))
		self.assertEqual(self.counts(), {})
		self.assertEqual(self.rows(), [])
		self.assertTrue(self.lines())


class TestErrorLogRows(OutcomeCase):
	def test_each_failure_class_writes_one_row_and_a_served_read_none(self):
		failures = [record(fallback=reason, fallback_detail=[]) for reason in sorted(FALLBACK_ROWS)]
		failures += [record(reader_refused="multiple_invoice_accounts"), record(reader_exception="KeyError")]
		self.write(record(path="reader", outcome="served"), *failures)
		self.assertEqual(len(self.rows()), len(FALLBACK_ROWS) + 2)
		self.assertEqual({row["title"] for row in self.rows()}, {read_log.READER_TITLE})

	def test_counted_only_classes_write_no_row(self):
		self.write(
			*[record(fallback=reason) for reason in sorted(COUNTED_ONLY)],
			*[record(step=step) for step in sorted(COUNTED_STEPS)],
			record(step="probe"),
		)
		self.assertEqual(self.rows(), [])

	def test_a_second_failure_of_the_same_class_that_day_writes_no_row(self):
		self.write(record(fallback="deadline"))
		self.write(record(fallback="deadline"))
		self.assertEqual(len(self.rows()), 1)
		self.assertEqual(self.counts()["fallback:deadline"], 2)
		self.write(record(fallback="pin_drift"))
		self.assertEqual(len(self.rows()), 2)

	def test_the_same_class_of_another_company_writes_its_own_row(self):
		other = record(fallback="deadline", company_digest=read_log.company_digest(COMPANY + " 2"))
		self.write(record(fallback="deadline"), other)
		self.assertEqual(len(self.rows()), 2)
		redis("DEL", frappe.cache.make_key(f"jarvis:pcal:reader:day:{other['company_digest']}:{DATE}"))
		for key in redis(
			"KEYS", frappe.cache.make_key(f"jarvis:pcal:errlog:reader:{other['company_digest']}*")
		):
			redis("DEL", key)

	def test_the_window_is_a_day_and_the_cache_down_case_writes_no_row(self):
		with patch.object(read_log, "error_log_once", wraps=read_log.error_log_once) as once:
			self.write(record(fallback="deadline"))
		self.assertEqual(once.call_args.kwargs, {"window": 86_400, "when_cache_down": False})
		self.assertEqual(once.call_args.args[2], f"reader:{self.digest}:fallback:deadline")
		self.log_error.reset_mock()
		self.clean()
		with patch.object(frappe.cache, "execute_command", side_effect=ConnectionError("down")):
			self.write(record(fallback="pin_drift"))
		self.assertEqual(self.rows(), [])

	def test_names_in_an_exception_text_appear_nowhere_and_no_traceback_is_stored(self):
		hostile = record(
			reader_exception="RuntimeError",
			fallback_detail=[SECRET, "pin.source"],
			reader_refused=f"code {SECRET}",
		)
		self.write(hostile, record(reader_exception=f"Bad {SECRET}"))
		everything = repr(self.rows()) + repr(self.logger.mock_calls) + repr(self.counts())
		self.assertNotIn(SECRET, everything)
		self.assertNotIn(COMPANY, everything)
		self.assertNotIn("Traceback", everything)
		self.assertTrue(all("traceback" not in row["message"].lower() for row in self.rows()))

	def test_the_message_holds_fixed_text_and_numbers_only(self):
		self.write(
			record(fallback="pin_drift", fallback_detail=["compat.source"], reader_ms=7, counts={"lines": 3})
		)
		message = self.rows()[0]["message"]
		self.assertIn(f"company={self.digest}", message)
		self.assertIn("class=fallback:pin_drift", message)
		self.assertIn("compat.source", message)
		self.assertNotIn(COMPANY, message)


class TestInfoAndWarningLines(OutcomeCase):
	def test_a_counted_routed_record_writes_one_info_line_with_the_digest_and_no_name(self):
		self.write(record(path="reader", outcome="served", reader_ms=9), record(fallback="deadline"))
		lines = self.lines()
		self.assertEqual(len(lines), 2)
		self.assertIn("path=reader", lines[0])
		self.assertIn(f"company={self.digest}", lines[0])
		self.assertIn("class=fallback:deadline", lines[1])
		for line in lines:
			self.assertNotIn(COMPANY, line)

	def test_a_side_the_reader_never_saw_writes_no_info_line(self):
		# Mode off and every side today serves: no new log noise in every job.
		self.write(
			record(routed=False, outcome="served"),
			record(routed=False, refusal="forbidden"),
			record(step="settings"),
		)
		self.assertEqual(self.lines(), [])

	def test_a_slow_view_of_a_side_the_reader_never_saw_writes_no_warning(self):
		# Mode off: every job would warn about its own view length.
		self.write(
			record(routed=False, outcome="served", view_ms=90_000), record(step="settings", view_ms=90_000)
		)
		self.assertEqual(self.lines("warning"), [])

	def test_a_view_over_sixty_seconds_writes_a_warning(self):
		self.write(record(fallback="deadline", view_ms=60_000), record(fallback="deadline", view_ms=60_001))
		self.assertEqual(len(self.lines("warning")), 1)

	def test_a_served_read_after_failures_that_day_is_logged_as_recovered(self):
		self.write(record(path="reader", outcome="served"))
		self.assertNotIn("recovered=1", self.lines()[-1])
		self.write(record(fallback="deadline"))
		self.write(record(path="reader", outcome="served"))
		self.assertIn("recovered=1", self.lines()[-1])


class TestNeverRaises(OutcomeCase):
	def test_a_failing_cache_or_log_never_raises(self):
		with patch.object(frappe.cache, "execute_command", side_effect=ConnectionError("down")):
			self.write(record(fallback="deadline"))
		self.log_error.side_effect = RuntimeError("db down")
		self.write(record(fallback="pin_drift"))
		with patch.object(frappe, "logger", side_effect=RuntimeError("no log")):
			self.write(record(fallback="control_total"))

	def test_a_malformed_record_does_not_stop_the_others(self):
		self.write({"side": None}, "not a dict", record(fallback="deadline"))
		self.assertEqual(self.counts(), {"fallback:deadline": 1})

	def test_a_job_deadline_is_not_swallowed(self):
		with patch.object(frappe.cache, "execute_command", side_effect=JobTimeoutException()):
			with self.assertRaises(JobTimeoutException):
				self.write(record(fallback="deadline"))

	def test_no_records_do_nothing(self):
		self.write()
		self.assertEqual((self.counts(), self.rows(), self.lines()), ({}, [], []))


class TestKeysSurviveTheWatchdogClear(OutcomeCase):
	def test_a_full_cache_clear_keeps_the_reader_and_shadow_keys(self):
		self.write(record(fallback="deadline"))
		shadow_key = frappe.cache.make_key(f"jarvis:pcal:shadow:{self.digest}:{DATE}")
		memo = routing.memo_key(COMPANY, "Receivable")
		for key in (shadow_key, memo):
			redis("SET", key, "1", "EX", 300)
		frappe.clear_cache()
		self.assertEqual(self.counts(), {"fallback:deadline": 1})
		self.assertTrue(redis("EXISTS", shadow_key))
		self.assertTrue(redis("EXISTS", memo))

	def test_every_reason_the_router_can_record_has_a_class(self):
		# A new fallback reason must be classified: written as a row, or counted only.
		self.assertEqual(
			READER_FALLBACK_REASONS, FALLBACK_ROWS | COUNTED_ONLY, "classify the new reason in read_log"
		)


class TestWaveOneClasses(OutcomeCase):
	"""Cycle-1 findings: aborted reads, causes, ineligible reasons, probe errors, the daily report."""

	def test_a_job_timeout_in_the_reader_is_counted_and_writes_its_row(self):
		self.write(record(reader_exception="JobTimeoutException"))
		self.assertEqual(self.counts(), {"reader_exception:JobTimeoutException": 1})
		self.assertEqual(len(self.rows()), 1)
		self.write(record(reader_exception="JobTimeoutException"))
		self.assertEqual(len(self.rows()), 1, "once per company, class and day")

	def test_a_statement_error_counts_both_classes_and_writes_one_row_with_the_cause(self):
		self.write(
			record(fallback="reader_error", reader_exception="OperationalError", cause="OperationalError")
		)
		self.assertEqual(self.counts(), {"fallback:reader_error": 1, "reader_exception:OperationalError": 1})
		self.assertEqual(len(self.rows()), 1)
		self.assertIn("class=fallback:reader_error", self.rows()[0]["message"])
		self.assertIn("cause=OperationalError", self.rows()[0]["message"])

	def test_the_cause_is_a_safe_class_name_only(self):
		self.write(record(fallback="reader_error", cause=SECRET))
		self.assertIn("cause=unknown", self.rows()[0]["message"])
		self.assertNotIn(SECRET, repr(self.rows()))

	def test_a_failing_probe_is_counted_as_its_own_class_not_as_a_probe_that_exceeds(self):
		self.write(record(step="probe", probe_error="OperationalError"), record(step="probe"))
		self.assertEqual(self.counts(), {"probe_error:OperationalError": 1, "probe": 1})

	def test_an_ineligible_read_is_counted_by_its_reasons(self):
		self.write(record(fallback="ineligible", fallback_detail=["column_filter", "user_permission"]))
		self.assertEqual(
			self.counts(),
			{"ineligible": 1, "ineligible:column_filter": 1, "ineligible:user_permission": 1},
		)
		self.assertEqual(self.rows(), [], "counted only")

	def test_the_daily_report_lists_every_counter_when_anything_but_served_happened(self):
		self.write(
			record(path="reader", outcome="served"),
			record(path="reader", outcome="served"),
			record(step="busy"),
			record(step="probe", probe_error="OperationalError"),
			record(fallback="ineligible", fallback_detail=["column_filter"]),
			record(fallback="deadline"),
		)
		message = read_log.daily_report(self.digest, DATE)
		for part in (
			f"company={self.digest}",
			f"date={DATE}",
			"failures=2",
			"served=2",
			"busy=1",
			"ineligible=1",
			"ineligible:column_filter=1",
			"probe_error:OperationalError=1",
			"fallback:deadline=1",
		):
			self.assertIn(part, message)
		self.assertNotIn(COMPANY, message)

	def test_the_daily_report_is_silent_when_only_served_was_counted_and_takes_extra_counters(self):
		self.write(record(path="reader", outcome="served"))
		self.assertIsNone(read_log.daily_report(self.digest, DATE))
		message = read_log.daily_report(self.digest, DATE, {"listed_but_missing": 2})
		self.assertIn("listed_but_missing=2", message)
		self.assertIn("served=1", message)

	def test_a_company_with_no_counters_has_no_daily_report(self):
		self.assertIsNone(read_log.daily_report(self.digest, DATE))
		self.assertIsNone(read_log.daily_report(self.digest, DATE, {"listed_but_missing": 0}))


class TestErrorLogWindow(OutcomeCase):
	def test_a_failed_insert_frees_the_window_so_the_next_failure_writes_the_row(self):
		self.log_error.side_effect = RuntimeError("db blip")
		with self.assertRaises(RuntimeError):
			read_log.error_log_once(
				"T", "m", f"reader:{self.digest}:x", "reader", window=86_400, when_cache_down=False
			)
		self.log_error.side_effect = None
		read_log.error_log_once(
			"T", "m", f"reader:{self.digest}:x", "reader", window=86_400, when_cache_down=False
		)
		self.assertEqual(self.log_error.call_count, 2)
		# And now the window is closed again.
		read_log.error_log_once(
			"T", "m", f"reader:{self.digest}:x", "reader", window=86_400, when_cache_down=False
		)
		self.assertEqual(self.log_error.call_count, 2)

	def test_the_outcome_writer_survives_a_failed_insert_and_a_later_failure_still_writes(self):
		self.log_error.side_effect = RuntimeError("db blip")
		self.write(record(fallback="deadline"))
		self.log_error.side_effect = None
		self.write(record(fallback="deadline"))
		self.assertEqual(self.log_error.call_count, 2)


class TestOpsDigest(TestCase):
	def test_it_is_keyed_by_a_subkey_never_by_the_site_key_itself(self):
		import hashlib
		import hmac

		with patch.dict(frappe.conf, {"encryption_key": KEY}):
			subkey = hmac.new(KEY.encode(), b"pcal-ops", hashlib.sha256).digest()
			expected = hmac.new(subkey, COMPANY.encode(), hashlib.sha256).hexdigest()[:16]
			self.assertEqual(read_log.ops_digest(COMPANY), expected)
			direct = hmac.new(KEY.encode(), COMPANY.encode(), hashlib.sha256).hexdigest()[:16]
			self.assertNotEqual(read_log.ops_digest(COMPANY), direct)
