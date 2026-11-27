"""Payment Calendar S4b B9: the once-per-day Error Log rows of the cache (plan S4 section 7).

`evaluate` is pure: heads, metrics, oracle records and counters in, alerts out. `alerts` reads the durable rows,
evaluates, and writes one Error Log row per (company, class, day). A row names a company only by its digest.
Dedupe keys use a day label that no real day has, so a re-run of the tests starts clean.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_alerts
"""

import json
import secrets
from datetime import timedelta
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from rq.timeouts import JobTimeoutException

from jarvis.payment_calendar import (
	cache,
	cache_alerts,
	cache_state,
	cache_status,
	cache_store,
	compatibility,
	read_log,
)
from jarvis.tests import test_payment_calendar_cache_status as status_tests
from jarvis.tests import test_payment_calendar_cache_tick as tick_tests
from jarvis.tests.test_payment_calendar_cache_store import GENERATION, HEAD, _sql, site_offset_s

FULL = cache_state.CacheSettings("shadow", ("Acme",), True)
# AlertCase patches `cache_alerts.lane_configured`; this is the real one.
REAL_LANE_CONFIGURED = cache_alerts.lane_configured


def head(**fields):
	return tick_tests.head(**fields)


def metric(**fields):
	return {"backoff_level": 0, "failures": 0, **fields}


def run(heads=None, metrics=None, **kwargs):
	args = {
		"full_cadence": True,
		"diffs": [],
		"lane_live": None,
		"lane_configured": True,
		"tdr_stuck": 0,
		"pin_drift": 0,
		"skips": {},
		**kwargs,
	}
	return cache_alerts.evaluate(heads or {}, metrics or {}, **args)


def klasses(alerts):
	return sorted(alert.klass for alert in alerts)


class TestEvaluate(TestCase):
	def setUp(self):
		self.digest = read_log.company_digest("Acme")

	def one(self, **fields):
		return {("Acme", "Receivable"): head(**fields)}

	def test_a_healthy_side_raises_nothing(self):
		self.assertEqual(run(self.one(), {("Acme", "Receivable"): metric()}), [])

	def test_a_repeated_too_large_backoff_raises_history_too_large(self):
		for level, expected in ((1, []), (2, ["history_too_large"]), (3, ["history_too_large"])):
			with self.subTest(level=level):
				found = run(self.one(), {("Acme", "Receivable"): metric(backoff_level=level)})
				self.assertEqual(klasses(found), expected)

	def test_a_readback_failure_raises_readback_and_not_the_failure_count(self):
		found = run(self.one(last_failure="readback"), {("Acme", "Receivable"): metric(failures=3)})
		self.assertEqual(klasses(found), ["readback"])

	def test_three_failures_raise_repeated_failures_with_the_class(self):
		heads = self.one(last_failure="error:ValueError")
		self.assertEqual(run(heads, {("Acme", "Receivable"): metric(failures=2)}), [])
		found = run(heads, {("Acme", "Receivable"): metric(failures=3)})
		self.assertEqual(klasses(found), ["failures"])
		self.assertIn("failures=3", found[0].detail)
		self.assertIn("error:ValueError", found[0].detail)

	def test_an_old_generation_in_use_at_full_cadence_raises_stale_risk(self):
		limit = cache.MAX_AGE_S
		for age, expected in ((limit, []), (limit + 1, ["stale_risk"])):
			with self.subTest(age=age):
				found = run(self.one(age_s=age), {("Acme", "Receivable"): metric()})
				self.assertEqual(klasses(found), expected)

	def test_stale_risk_needs_full_cadence_use_and_a_generation(self):
		metrics = {("Acme", "Receivable"): metric()}
		old = cache.MAX_AGE_S + 60
		self.assertEqual(run(self.one(age_s=old), metrics, full_cadence=False), [])
		self.assertEqual(run(self.one(age_s=old, opened_age_s=cache.OPEN_WINDOW_S), metrics), [])
		self.assertEqual(run(self.one(age_s=old, opened_age_s=None), metrics), [])
		self.assertEqual(run(self.one(age_s=None, current_generation=None), metrics), [])

	def test_stale_risk_needs_a_configured_lane_whatever_the_full_cadence(self):
		"""E8: a bench without a `jarvis_pcal` lane builds by night and ages by day; with one it still alerts."""
		metrics = {("Acme", "Receivable"): metric()}
		old = cache.MAX_AGE_S + 60
		self.assertEqual(run(self.one(age_s=old), metrics, full_cadence=True, lane_configured=False), [])
		self.assertEqual(
			klasses(run(self.one(age_s=old), metrics, full_cadence=True, lane_configured=True)),
			["stale_risk"],
		)

	def test_a_diff_record_raises_diff_for_its_company_with_the_counts(self):
		diffs = [
			{"company": "Acme", "side": "Payable", "counts": {"only_cache": 2, "fields": ["x"], "rows": 9}}
		]
		found = run({}, {}, diffs=diffs)
		self.assertEqual(klasses(found), ["diff"])
		self.assertEqual((found[0].digest, found[0].sides), (self.digest, ("Payable",)))
		self.assertIn("only_cache=2", found[0].detail)
		self.assertNotIn("fields", found[0].detail)

	def test_sides_of_one_company_and_class_are_one_alert(self):
		heads = {
			("Acme", "Receivable"): head(side="Receivable", last_failure="readback"),
			("Acme", "Payable"): head(side="Payable", last_failure="readback"),
		}
		found = run(heads, {})
		self.assertEqual(len(found), 1)
		self.assertEqual(found[0].sides, ("Payable", "Receivable"))

	def test_a_lane_that_is_down_while_a_company_is_in_use_raises_lane_not_live(self):
		self.assertEqual(klasses(run(self.one(), {}, lane_live=False)), ["lane"])
		self.assertEqual(run(self.one(), {}, lane_live=True), [])
		self.assertEqual(run(self.one(), {}, lane_live=None), [])
		found = run(self.one(), {}, lane_live=False)
		self.assertEqual(found[0].digest, "site")

	def test_a_deletion_record_that_blocks_refresh_for_long_raises_deletion(self):
		found = run({}, {}, tdr_stuck=2)
		self.assertEqual(klasses(found), ["deletion"])
		self.assertEqual((found[0].digest, found[0].detail), ("site", "records=2"))
		self.assertEqual(run({}, {}, tdr_stuck=0), [])

	def test_a_query_hook_skip_today_raises_not_buildable(self):
		found = run({}, {}, skips={"not_buildable:query_hook": 4, "off": 9})
		self.assertEqual(klasses(found), ["not_buildable"])
		self.assertEqual(found[0].detail, "count=4")
		self.assertEqual(run({}, {}, skips={"off": 9}), [])

	def test_a_drifted_pin_raises_pin_drift_for_the_site(self):
		found = run({}, {}, pin_drift=3)
		self.assertEqual(klasses(found), ["pin_drift"])
		self.assertEqual((found[0].digest, found[0].detail), ("site", "pins=3"))
		self.assertEqual(run({}, {}, pin_drift=0), [])

	def test_every_class_has_a_fixed_google_able_title_without_a_name(self):
		self.assertEqual(
			set(cache_alerts.TITLES),
			{
				"diff",
				"history_too_large",
				"readback",
				"failures",
				"stale_risk",
				"lane",
				"deletion",
				"not_buildable",
				"pin_drift",
				"tick",
				"janitor_lag",
			},
		)
		for title in cache_alerts.TITLES.values():
			self.assertTrue(title.startswith("jarvis.payment_calendar.cache: "), title)
			self.assertNotIn("Acme", title)

	def test_every_alert_is_one_that_the_jarvis_error_feed_forwards_to_a_person(self):
		from jarvis import api_errors

		for klass, title in cache_alerts.TITLES.items():
			alert = cache_alerts.Alert(klass, self.digest, ("Receivable",), "count=3")
			with self.subTest(klass):
				self.assertTrue(api_errors.is_jarvis_error(alert.title, alert.message), title)

	def test_the_alerts_of_a_stalled_tick_and_a_lagging_janitor(self):
		found = run(
			{}, {}, janitor={"purged": cache_alerts.JANITOR_LAG_COUNT, "retired_with_rows": 0, "oldest_s": 5}
		)
		self.assertEqual(klasses(found), ["janitor_lag"])
		self.assertEqual(found[0].digest, "site")
		self.assertEqual(run({}, {}, janitor={"purged": 3, "retired_with_rows": 4, "oldest_s": 60}), [])
		late = run(
			{}, {}, janitor={"purged": 0, "retired_with_rows": 1, "oldest_s": cache_alerts.JANITOR_LAG_S + 1}
		)
		self.assertEqual(klasses(late), ["janitor_lag"])
		self.assertEqual(run({}, {}, janitor=None), [])

	def test_a_message_carries_the_digest_the_sides_and_numbers_only(self):
		found = run(self.one(last_failure="readback"), {})
		message = found[0].message
		self.assertIn(f"company={self.digest}", message)
		self.assertIn("sides=Receivable", message)
		self.assertNotIn("Acme", message)
		self.assertNotIn("\n", message)

	def test_a_failure_class_that_is_not_plain_text_is_not_copied(self):
		heads = self.one(last_failure="error:Bad Class\nAcme Ltd")
		found = run(heads, {("Acme", "Receivable"): metric(failures=3)})
		self.assertNotIn("Acme", found[0].message)


class AlertCase(status_tests.StableCase):
	def setUp(self):
		super().setUp()
		self.day = f"test-{secrets.token_hex(4)}"
		self.addCleanup(self.clean)
		self.clean()
		self.start(patch.object(cache_state, "lane_live", return_value=True))
		self.start(patch.object(cache_alerts, "lane_configured", return_value=True))
		self.start(patch.object(compatibility, "reader_pin_drift", return_value=[]))

	def clean(self):
		frappe.db.rollback()
		for title in cache_alerts.TITLES.values():
			frappe.db.delete("Error Log", {"method": title})
		frappe.db.commit()
		frappe.cache.delete_keys("jarvis:pcal:errlog:cache:")
		frappe.cache.delete_keys(cache_alerts.RECOVERY_PREFIX)

	def rows(self, klass=None):
		frappe.db.rollback()
		titles = [cache_alerts.TITLES[klass]] if klass else list(cache_alerts.TITLES.values())
		return frappe.get_all("Error Log", filters={"method": ["in", titles]}, fields=["method", "error"])

	def raise_alerts(self):
		return cache_alerts.alerts(self.settings, cache.heads_of(), today=self.day)


class TestAlertsWrite(AlertCase):
	def test_a_repeated_too_large_backoff_writes_one_row_a_day_and_no_name(self):
		self.make_head()
		_sql(f"update `tab{HEAD}` set backoff_level=2, last_failure='history_too_large'")
		frappe.db.commit()
		self.assertEqual(klasses(self.raise_alerts()), ["history_too_large"])
		self.raise_alerts()
		rows = self.rows("history_too_large")
		self.assertEqual(len(rows), 1)
		self.assertIn(f"company={self.digest}", rows[0].error)
		self.assertNotIn(self.company, rows[0].error)

	def test_a_new_day_writes_a_new_row(self):
		self.make_head()
		_sql(f"update `tab{HEAD}` set last_failure='readback'")
		frappe.db.commit()
		self.raise_alerts()
		self.day += "-next"
		self.raise_alerts()
		self.assertEqual(len(self.rows("readback")), 2)

	def test_a_stale_generation_in_use_writes_stale_risk(self):
		self.make_head(age=cache.MAX_AGE_S + 120)
		self.raise_alerts()
		self.assertEqual(len(self.rows("stale_risk")), 1)

	def test_a_diff_record_of_the_last_day_writes_diff_and_an_old_one_does_not(self):
		self.make_head()
		record = json.dumps({"outcome": "diff", "counts": {"only_cache": 1}, "digests": ["a" * 16]})
		_sql(f"update `tab{GENERATION}` set shadow=%s where state='current'", (record,))
		frappe.db.commit()
		self.raise_alerts()
		rows = self.rows("diff")
		self.assertEqual(len(rows), 1)
		self.assertNotIn("a" * 16, rows[0].error)
		self.clean()
		_sql(f"update `tab{GENERATION}` set as_of=now(6) - interval 36 hour where state='current'")
		frappe.db.commit()
		self.raise_alerts()
		self.assertEqual(self.rows("diff"), [])

	def test_a_compared_record_writes_nothing(self):
		self.make_head()
		_sql(f"update `tab{GENERATION}` set shadow='{{\"outcome\":\"compared\"}}' where state='current'")
		frappe.db.commit()
		self.assertEqual(self.raise_alerts(), [])

	def test_the_lane_is_probed_only_when_a_company_is_in_use(self):
		self.make_head(opened=cache.OPEN_WINDOW_S + 60)
		with patch.object(cache_state, "lane_live", return_value=False) as probe:
			self.raise_alerts()
		probe.assert_not_called()
		_sql(f"update `tab{HEAD}` set last_opened=now(6)")
		frappe.db.commit()
		with patch.object(cache_state, "lane_live", return_value=False) as probe:
			self.raise_alerts()
		probe.assert_called_once()
		self.assertEqual(len(self.rows("lane")), 1)

	def test_a_site_with_no_lane_configured_writes_no_lane_row_and_never_probes(self):
		"""A night-pre-warm-only site is a valid setup: no daily row (review-B9 m7)."""
		self.make_head()
		with (
			patch.object(cache_alerts, "lane_configured", return_value=False),
			patch.object(cache_state, "lane_live", return_value=False) as probe,
		):
			self.raise_alerts()
		probe.assert_not_called()
		self.assertEqual(self.rows("lane"), [])

	def test_full_cadence_without_a_configured_lane_writes_no_stale_risk_row(self):
		self.assertTrue(self.settings.full_cadence)
		self.make_head(age=cache.MAX_AGE_S + 120)
		with patch.object(cache_alerts, "lane_configured", return_value=False):
			self.assertEqual(self.raise_alerts(), [])
		self.assertEqual(self.rows("stale_risk"), [])

	def test_a_night_only_bench_with_a_day_old_generation_in_use_raises_no_alert_at_all(self):
		"""E8: no lane configured, `full_cadence` off (the setting of a night-only bench): a generation that is a day
		old and in use is the normal state, so no `stale_risk`, no `lane` row, no probe."""
		self.settings = cache_state.CacheSettings("shadow", (self.company,), False)
		self.make_head(age=20 * 3600, opened=60)
		with (
			patch.object(cache_alerts, "lane_configured", return_value=False),
			patch.object(cache_state, "lane_live", return_value=False) as probe,
		):
			self.assertEqual(self.raise_alerts(), [])
		probe.assert_not_called()
		self.assertEqual(self.rows(), [])

	def test_lane_configured_reads_the_workers_of_the_site_config(self):
		with patch.object(
			frappe, "get_conf", return_value=frappe._dict(workers={cache_state.LANE_QUEUE: {}})
		):
			self.assertTrue(REAL_LANE_CONFIGURED())
		with patch.object(frappe, "get_conf", return_value=frappe._dict(workers={"other": {}})):
			self.assertFalse(REAL_LANE_CONFIGURED())
		with patch.object(frappe, "get_conf", return_value=frappe._dict()):
			self.assertFalse(REAL_LANE_CONFIGURED())

	def test_a_drifted_pin_writes_a_pin_drift_row(self):
		self.make_head()
		with patch.object(compatibility, "reader_pin_drift", return_value=["a", "b"]):
			self.raise_alerts()
		rows = self.rows("pin_drift")
		self.assertEqual(len(rows), 1)
		self.assertIn("pins=2", rows[0].error)

	def test_a_cache_that_is_down_writes_no_row_and_does_not_raise(self):
		self.make_head()
		_sql(f"update `tab{HEAD}` set last_failure='readback'")
		frappe.db.commit()
		with patch.object(read_log, "window_open", return_value=None):
			self.raise_alerts()
		self.assertEqual(self.rows(), [])

	def make_two_alerts(self):
		self.make_head(age=cache.MAX_AGE_S + 120)
		_sql(f"update `tab{HEAD}` set last_failure='readback'")
		frappe.db.commit()

	def test_the_dedupe_key_is_set_only_after_the_row_is_committed(self):
		self.make_head()
		_sql(f"update `tab{HEAD}` set last_failure='readback'")
		frappe.db.commit()
		operation = f"cache:readback:{self.digest}:{self.day}"
		with patch.object(frappe.db, "commit", side_effect=ValueError(self.company)):
			self.raise_alerts()
		self.assertTrue(read_log.window_open(operation, cache_alerts.WINDOW_CLASS))
		self.raise_alerts()
		self.assertFalse(read_log.window_open(operation, cache_alerts.WINDOW_CLASS))

	def test_a_row_error_is_logged_by_class_and_the_next_alert_is_still_written(self):
		self.make_two_alerts()
		real, logger = frappe.log_error, MagicMock()
		calls = []

		def first_fails(*args, **kwargs):
			calls.append(kwargs.get("title"))
			if len(calls) == 1:
				raise ValueError(self.company)
			return real(*args, **kwargs)

		with (
			patch.object(frappe, "log_error", first_fails),
			patch.object(frappe, "logger", return_value=logger),
		):
			self.raise_alerts()
		self.assertEqual(len(self.rows()), 1)
		self.assertIn("ValueError", str(logger.method_calls))
		self.assertNotIn(self.company, str(logger.method_calls))
		self.raise_alerts()
		self.assertEqual(len(self.rows()), 2)

	def test_a_rollback_class_error_propagates_and_keeps_no_key(self):
		self.make_head()
		_sql(f"update `tab{HEAD}` set last_failure='readback'")
		frappe.db.commit()
		with (
			patch.object(frappe, "log_error", side_effect=frappe.QueryDeadlockError),
			self.assertRaises(frappe.QueryDeadlockError),
		):
			self.raise_alerts()
		self.assertTrue(
			read_log.window_open(f"cache:readback:{self.digest}:{self.day}", cache_alerts.WINDOW_CLASS)
		)
		self.raise_alerts()
		self.assertEqual(len(self.rows("readback")), 1)

	def test_a_job_deadline_propagates(self):
		self.make_head()
		_sql(f"update `tab{HEAD}` set last_failure='readback'")
		frappe.db.commit()
		with (
			patch.object(frappe, "log_error", side_effect=JobTimeoutException()),
			self.assertRaises(JobTimeoutException),
		):
			self.raise_alerts()

	def test_the_row_text_holds_no_name_at_all(self):
		self.make_head(age=cache.MAX_AGE_S + 120)
		_sql(f"update `tab{HEAD}` set last_failure='readback', backoff_level=2")
		frappe.db.commit()
		self.raise_alerts()
		text = " ".join(f"{row.method} {row.error}" for row in self.rows())
		self.assertTrue(text)
		self.assertNotIn(self.company, text)


class TestStuckDeletion(AlertCase):
	NAME = "PCAL-B9-TDR-1"

	def setUp(self):
		super().setUp()
		self.addCleanup(self.drop)
		self.drop()

	def drop(self):
		frappe.db.rollback()
		frappe.db.delete("Transaction Deletion Record", {"name": self.NAME})
		frappe.db.commit()

	def insert(self, status, hours):
		"""A record created `hours` ago whose `modified` is now: ERPNext `db_set` bumps it at every step.

		Both stamps are SITE time, as Frappe writes them; the database clock is another zone.
		"""
		offset = site_offset_s()
		frappe.db.sql(
			"INSERT INTO `tabTransaction Deletion Record` (name, creation, modified, modified_by, owner, "
			"docstatus, company, status) VALUES (%s, now(6) + interval %s second - interval %s hour, "
			"now(6) + interval %s second, 'Administrator', 'Administrator', 1, %s, %s)",
			(self.NAME, offset, hours, offset, self.company, status),
		)
		frappe.db.commit()

	def insert_in_zone(self, zone: int, seconds_ago: int):
		"""A Running record created `seconds_ago` ago by a site whose clock is `zone` seconds ahead of the database."""
		stamp = frappe.db.sql("select now(6)")[0][0] + timedelta(seconds=zone - seconds_ago)
		frappe.db.sql(
			"INSERT INTO `tabTransaction Deletion Record` (name, creation, modified, modified_by, owner, "
			"docstatus, company, status) VALUES (%s, %s, %s, 'Administrator', 'Administrator', 1, %s, 'Running')",
			(self.NAME, stamp, stamp, self.company),
		)
		frappe.db.commit()

	def count_in_zone(self, zone: int, seconds_ago: int) -> int:
		self.insert_in_zone(zone, seconds_ago)
		with patch(
			"frappe.utils.now_datetime",
			side_effect=lambda: frappe.db.sql("select now(6)")[0][0] + timedelta(seconds=zone),
		):
			return cache_alerts.stuck_deletions()

	def test_a_site_ahead_of_the_database_counts_a_two_hour_old_record_and_not_a_ten_minute_one(self):
		zone = 5 * 3600 + 1800
		self.assertEqual(self.count_in_zone(zone, 7200), 1)
		self.drop()
		self.assertEqual(self.count_in_zone(zone, 600), 0)

	def test_a_site_behind_the_database_counts_a_two_hour_old_record_and_not_a_ten_minute_one(self):
		zone = -3 * 3600
		self.assertEqual(self.count_in_zone(zone, 7200), 1)
		self.drop()
		self.assertEqual(self.count_in_zone(zone, 600), 0)

	def test_a_running_record_created_over_an_hour_ago_counts_though_it_keeps_changing(self):
		self.insert("Running", 2)
		self.assertEqual(cache_alerts.stuck_deletions(), 1)

	def test_a_recent_or_finished_record_does_not_count(self):
		self.insert("Running", 0)
		self.assertEqual(cache_alerts.stuck_deletions(), 0)
		self.drop()
		self.insert("Completed", 5)
		self.assertEqual(cache_alerts.stuck_deletions(), 0)

	def test_a_queued_record_older_than_an_hour_writes_the_row(self):
		self.insert("Queued", 3)
		self.make_head()
		self.raise_alerts()
		self.assertEqual(len(self.rows("deletion")), 1)


class TestRecovery(AlertCase):
	"""A class that alerted and no longer does gives one INFO line (plan section 7)."""

	def fail_head(self, value="readback"):
		_sql(f"update `tab{HEAD}` set last_failure=%s", (value,))
		frappe.db.commit()

	def info_lines(self, logger):
		return [str(call) for call in logger.info.call_args_list]

	def test_recovery_is_one_info_line_with_class_and_digest(self):
		self.make_head()
		self.fail_head()
		self.raise_alerts()
		self.fail_head(None)
		logger = MagicMock()
		with patch.object(frappe, "logger", return_value=logger):
			self.raise_alerts()
			self.raise_alerts()
		lines = self.info_lines(logger)
		self.assertEqual(len(lines), 1)
		self.assertIn("cache_alert recovered", lines[0])
		self.assertIn("readback", lines[0])
		self.assertIn(self.digest, lines[0])
		self.assertNotIn(self.company, lines[0])

	def test_an_alert_that_goes_on_is_not_recovered(self):
		self.make_head()
		self.fail_head()
		logger = MagicMock()
		with patch.object(frappe, "logger", return_value=logger):
			self.raise_alerts()
			self.raise_alerts()
		self.assertEqual(self.info_lines(logger), [])

	def test_the_markers_are_found_without_the_blocking_keys_command(self):
		self.make_head()
		self.fail_head()
		self.raise_alerts()
		self.fail_head(None)
		logger = MagicMock()
		with (
			patch.object(frappe.cache, "keys", side_effect=AssertionError("KEYS")),
			patch.object(frappe, "logger", return_value=logger),
		):
			self.raise_alerts()
		self.assertEqual(len(self.info_lines(logger)), 1)

	def test_a_class_that_never_alerted_gives_no_line(self):
		self.make_head()
		logger = MagicMock()
		with patch.object(frappe, "logger", return_value=logger):
			self.raise_alerts()
		self.assertEqual(self.info_lines(logger), [])

	def test_the_marker_is_removed_after_the_line(self):
		self.make_head()
		self.fail_head()
		self.raise_alerts()
		self.assertEqual(len(frappe.cache.get_keys(cache_alerts.RECOVERY_PREFIX)), 1)
		self.fail_head(None)
		self.raise_alerts()
		self.assertEqual(frappe.cache.get_keys(cache_alerts.RECOVERY_PREFIX), [])


class TestTickRaisesAlerts(AlertCase):
	def test_the_tick_raises_the_alerts_with_the_settings_and_the_heads(self):
		self.make_head()
		with patch.object(cache_alerts, "alerts") as raised:
			cache.tick()
		raised.assert_called_once()
		self.assertEqual(raised.call_args.args[0], self.settings)

	def test_an_error_in_the_alerts_does_not_stop_the_enqueues_of_the_tick(self):
		self.make_head(age=3000)
		with patch.object(cache_alerts, "alerts", side_effect=ValueError(self.company)):
			cache.tick()
		self.assertTrue(self.enqueue.called)

	def test_with_the_switch_off_the_tick_raises_no_alert(self):
		self.settings = cache_state.CacheSettings("off", (), False)
		with patch.object(cache_alerts, "alerts") as raised:
			cache.tick()
		raised.assert_not_called()
