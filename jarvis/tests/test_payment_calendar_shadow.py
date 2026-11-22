"""Payment Calendar S2 T7: the nightly shadow comparison of the built-in reader against today's read.

No test commits a mode: `routing.reader_settings` is patched. Real Redis keys use a far-future night and
are removed by digest in `tearDown`. Real-data tests build committed fixtures like the snapshot tests.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_shadow
"""

import io
import json
import traceback
from contextlib import ExitStack, contextmanager, redirect_stdout
from time import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
from uuid import uuid4

import frappe
from frappe.tests.utils import FrappeTestCase
from rq.timeouts import JobTimeoutException

from jarvis.payment_calendar import read_log, routing, shadow, shadow_state
from jarvis.payment_calendar import sources as today_sources
from jarvis.payment_calendar.errors import TIMEOUTS, CalendarSourceUnavailable, ReaderFallback
from jarvis.tests.payment_calendar_drift import frozen_clock
from jarvis.tests.payment_calendar_fixtures import (
	CalendarFixtures,
	require_calendar_test_site,
	sync_outstanding,
)

# Names and keys are per run: parallel runs on the shared test site never meet. The ordinal of NIGHT is a
# multiple of 6, so the per-night rotation starts at the first company for two or three companies.
RUN = uuid4().hex[:8]
TEST_PREFIX = f"jarvis:pcal:shadowtest-{RUN}:"
NIGHT = "2031-03-15"
YESTERDAY = "2031-03-14"
COMPANY_A, COMPANY_B = f"Shadow Test Co A {RUN}", f"Shadow Test Co B {RUN}"
BUILD = "b" * 12
OTHER_BUILD = "c" * 12
DIFF_TITLE = "Payment Calendar reader shadow diff"
FALLBACK_TITLE = "Payment Calendar reader shadow fallback"
INCOMPLETE_TITLE = "Payment Calendar reader shadow incomplete"


def key_prefix():
	"""This run's own key root for the shadow state; started by each case, stopped by its `patch.stopall`."""
	return patch.object(shadow_state, "PREFIX", TEST_PREFIX).start()


MODULE_KEY = "k" * 44
_site_key = []
_start_settings = []


def setUpModule():
	"""Use a fixed test key and save the site key for module cleanup.

	Frappe reloads `frappe.local.conf` at class cleanup, so each setup must set the key again.
	Use direct assignment: `patch.stopall` must not remove the key before Redis cleanup.
	The explicit no-key tests patch an empty key and restore the test key.
	"""
	_site_key.append(frappe.conf.get("encryption_key"))
	frappe.conf["encryption_key"] = MODULE_KEY
	# The reader switch as the site has it now: the last test checks that the run left it as it was.
	_start_settings.append(routing.reader_settings())


def tearDownModule():
	try:
		frappe.conf["encryption_key"] = MODULE_KEY
		purge_keys()
	finally:
		original = _site_key.pop() if _site_key else None
		if original is None:
			frappe.conf.pop("encryption_key", None)
		else:
			frappe.conf["encryption_key"] = original


def settings(mode="shadow", served=(), shadow_list=(COMPANY_A, COMPANY_B)):
	return routing.ReaderSettings(mode, tuple(served), tuple(shadow_list))


def rows(*ids, party="ACME-SECRET"):
	return {
		"rows": [
			{"id": row_id, "voucher_no": f"V-{index}", "party": party, "remaining": "10"}
			for index, row_id in enumerate(ids)
		],
		"canonical_total": "10",
		"generated_at": "x",
	}


def digest(company):
	return read_log.company_digest(company)


def done(outcome="compared", reason=None, build=BUILD, **extra):
	return {"state": "done", "outcome": outcome, "reason": reason, "reader_build": build, **extra}


class Clock:
	"""A fake `monotonic`: the test moves it."""

	def __init__(self):
		self.now = 1000.0

	def __call__(self):
		return self.now

	def advance(self, seconds):
		self.now += seconds


def purge_keys():
	"""Every key of this run: the test prefix, and the keys named by a test company's digest."""
	frappe.cache.delete_keys(TEST_PREFIX)
	for company in (COMPANY_A, COMPANY_B):
		purge_digest_keys(company)


def purge_digest_keys(company):
	"""The Error Log windows, day counters and router memo of one company."""
	name = digest(company)
	for operation in ("shadow-diff", "shadow-fallback", "shadow-incomplete", "reader-daily", "reader"):
		frappe.cache.delete_keys(f"jarvis:pcal:errlog:{operation}:{name}")
	frappe.cache.delete_keys(f"jarvis:pcal:reader:day:{name}")
	frappe.cache.delete_keys(f"jarvis:pcal:reader:memo:{name}")


class ShadowCase(FrappeTestCase):
	"""Dispatch, units and the gate with the settings, the clock, the queue and the commit patched."""

	def setUp(self):
		frappe.conf["encryption_key"] = MODULE_KEY
		frappe.set_user("Administrator")
		key_prefix()
		purge_keys()
		self.addCleanup(purge_keys)
		self.current = settings()
		self.missing = ()  # the test companies are not real: none is "listed but missing" unless a test says so
		for target in (
			patch.object(routing, "reader_settings", side_effect=lambda: self.current),
			patch.object(routing, "reader_build", return_value=BUILD + "0" * 52),
			patch.object(routing, "listed_but_missing", side_effect=lambda current: self.missing),
			patch.object(shadow, "_today", return_value=NIGHT),
			patch.object(frappe.db, "commit"),
		):
			target.start()
			self.addCleanup(target.stop)
		self.enqueue = patch("frappe.enqueue").start()
		self.log_error = patch.object(frappe, "log_error").start()
		self.addCleanup(patch.stopall)

	def start_night(self, count=16, **kwargs):
		shadow_state.start_night(NIGHT, count, **kwargs)

	def unit_record(self, company, field):
		return shadow_state.records(digest(company), NIGHT).get(field)


class TestComparison(TestCase):
	def compare(self, today, reader):
		return shadow.compare_unit(today, reader, digest=lambda text: "d" * 16)

	def test_equal_results_with_equal_ids_compare(self):
		reordered = rows("a", "b")
		reordered["rows"].reverse()
		outcome = self.compare((rows("a", "b"), None), (reordered, None))
		self.assertEqual(outcome["outcome"], "compared")

	def test_a_difference_only_in_the_ids_is_a_diff(self):
		outcome = self.compare((rows("a"), None), (rows("z"), None))
		self.assertEqual(outcome["outcome"], "diff")
		self.assertEqual(outcome["counts"]["ids_differ"], 1)

	def test_a_difference_in_content_is_a_diff_with_counts_and_digests(self):
		reader = rows("a", "b")
		reader["rows"][1]["remaining"] = "99"
		outcome = self.compare((rows("a", "b"), None), (reader, None))
		self.assertEqual(outcome["outcome"], "diff")
		self.assertEqual(outcome["counts"]["only_today"], 1)
		self.assertEqual(outcome["counts"]["only_reader"], 1)
		self.assertEqual(outcome["digests"], ["d" * 16, "d" * 16])

	def test_a_difference_outside_the_rows_is_a_diff_that_names_the_field(self):
		reader = rows("a")
		reader["canonical_total"] = "11"
		outcome = self.compare((rows("a"), None), (reader, None))
		self.assertEqual(outcome["outcome"], "diff")
		self.assertEqual(outcome["counts"]["fields"], ["canonical_total"])

	def test_a_diff_keeps_at_most_twenty_digests_and_no_names(self):
		today = rows(*[f"t{i}" for i in range(30)])
		outcome = shadow.compare_unit(
			(today, None), (rows(), None), digest=lambda text: f"{abs(hash(text)) % 10**16:016d}"
		)
		self.assertEqual(len(outcome["digests"]), 20)
		self.assertNotIn("ACME-SECRET", json.dumps(outcome))
		self.assertNotIn("V-", json.dumps(outcome))

	def test_only_one_side_refusing_is_a_diff(self):
		refusal = CalendarSourceUnavailable("too_large")
		self.assertEqual(self.compare((rows("a"), None), (None, refusal))["outcome"], "diff")
		self.assertEqual(self.compare((None, refusal), (rows("a"), None))["outcome"], "diff")

	def test_the_same_refusal_agrees_and_different_refusals_differ(self):
		too_large, mismatch = CalendarSourceUnavailable("too_large"), CalendarSourceUnavailable("forbidden")
		self.assertEqual(self.compare((None, too_large), (None, too_large))["outcome"], "agree_refusal")
		self.assertEqual(self.compare((None, too_large), (None, mismatch))["outcome"], "diff")

	def test_a_reader_fallback_is_not_compared(self):
		for reason in ("deadline", "too_many_lines", "control_total"):
			outcome = self.compare((rows("a"), None), (None, ReaderFallback(reason)))
			self.assertEqual((outcome["outcome"], outcome["reason"]), ("not_compared", f"fallback:{reason}"))

	def test_an_ineligible_reader_is_not_compared(self):
		outcome = self.compare((rows("a"), None), (None, ReaderFallback("ineligible")))
		self.assertEqual((outcome["outcome"], outcome["reason"]), ("not_compared", "ineligible"))

	def test_an_unexpected_error_is_not_compared_and_carries_only_its_class(self):
		outcome = self.compare((rows("a"), None), (None, RuntimeError("secret company name")))
		self.assertEqual((outcome["outcome"], outcome["reason"]), ("not_compared", "error:RuntimeError"))
		self.assertNotIn("secret", json.dumps(outcome))


class TestCanonical(TestCase):
	def test_canonical_drops_ids_and_sorts_rows(self):
		first, second = shadow.canonical(rows("a", "b")), shadow.canonical(rows("b", "a"))
		self.assertEqual(first, second)
		self.assertTrue(all("id" not in row for row in first["rows"]))

	def test_canonical_reply_drops_only_generated_at_and_keeps_ids(self):
		reply = {"generated_at": "now", "projection": {"x": 1}, "rows": [{"id": "r1"}]}
		self.assertEqual(shadow.canonical_reply(reply), {"projection": {"x": 1}, "rows": [{"id": "r1"}]})
		self.assertIn("generated_at", reply)

	def test_the_differential_uses_the_one_canonical_and_the_one_lift(self):
		from jarvis.tests import payment_calendar_differential as differential

		self.assertIs(differential._canonical, shadow.canonical)
		self.assertEqual(differential.LIFTED, shadow.LIFTED)
		self.assertNotIn("builtin_reader", vars(differential))


class TestScopesAndUnits(TestCase):
	def test_the_months_are_previous_current_next_in_site_time(self):
		self.assertEqual(shadow.month_keys("2026-11-20"), ("2026-10", "2026-11", "2026-12"))
		self.assertEqual(shadow.month_keys("2026-12-31"), ("2026-11", "2026-12", "2027-01"))
		self.assertEqual(shadow.month_keys("2026-01-01"), ("2025-12", "2026-01", "2026-02"))

	def test_today_is_the_site_date_not_the_utc_date(self):
		with patch("frappe.utils.data.get_system_timezone", return_value="Pacific/Kiritimati"):
			east = shadow._today()
			self.assertEqual(east, frappe.utils.nowdate())
		with patch("frappe.utils.data.get_system_timezone", return_value="Etc/GMT+12"):
			west = shadow._today()
			self.assertEqual(west, frappe.utils.nowdate())
		self.assertNotEqual(east, west)

	def test_a_company_has_eight_units_in_a_fixed_order(self):
		units = shadow.units_for(settings(shadow_list=(COMPANY_B,), served=(COMPANY_A,)))
		self.assertEqual(len(units), 16)
		self.assertEqual(units[0], (COMPANY_A, "Payable", "previous"))
		self.assertEqual(
			[(side, scope) for _, side, scope in units[:8]],
			[(s, k) for s in ("Payable", "Receivable") for k in ("previous", "current", "next", "window")],
		)
		self.assertEqual({company for company, _, _ in units[8:]}, {COMPANY_B})

	def test_a_company_on_both_lists_is_one_company(self):
		self.assertEqual(len(shadow.units_for(settings(served=(COMPANY_A,), shadow_list=(COMPANY_A,)))), 8)

	def test_the_start_company_rotates_with_the_night_so_no_company_is_always_last(self):
		current = settings(shadow_list=("Co A", "Co B", "Co C"))
		first = {
			night: shadow.units_for(current, night)[0][0]
			for night in ("2031-03-15", "2031-03-16", "2031-03-17", "2031-03-18")
		}
		# Three nights in a row start three different companies; the fourth wraps.
		self.assertEqual(len({first["2031-03-15"], first["2031-03-16"], first["2031-03-17"]}), 3)
		self.assertEqual(first["2031-03-18"], first["2031-03-15"])

	def test_a_rotated_night_keeps_each_company_block_whole_and_the_same_units(self):
		current = settings(shadow_list=("Co A", "Co B", "Co C"))
		plain, rotated = shadow.units_for(current), shadow.units_for(current, "2031-03-16")
		self.assertEqual(sorted(rotated), sorted(plain))
		for start in range(0, 24, 8):
			self.assertEqual(len({company for company, _, _ in rotated[start : start + 8]}), 1)
		self.assertEqual(shadow.units_for(current, "2031-03-16"), rotated)

	def test_no_night_means_no_rotation(self):
		current = settings(shadow_list=("Co B", "Co A"))
		self.assertEqual(shadow.units_for(current)[0][0], "Co A")


class TestDispatch(ShadowCase):
	def dispatch(self):
		shadow.dispatch()

	def test_a_night_starts_with_one_job_for_unit_zero(self):
		self.dispatch()
		self.enqueue.assert_called_once()
		args, kwargs = self.enqueue.call_args
		self.assertEqual(args[0], "jarvis.payment_calendar.shadow.run_unit")
		self.assertEqual(kwargs["queue"], "long")
		self.assertEqual(kwargs["timeout"], 600)
		self.assertEqual(kwargs["job_id"], f"pcal-shadow:{NIGHT}:0")
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual((kwargs["n"], kwargs["night"]), (0, NIGHT))
		self.assertEqual(shadow_state.night_info(NIGHT)["count"], 16)

	def test_it_does_nothing_in_mode_off_with_empty_lists_without_a_key_or_on_v15(self):
		cases = {
			"off": lambda: setattr(self, "current", settings(mode="off")),
			"empty": lambda: setattr(self, "current", settings(shadow_list=())),
		}
		for name, arrange in cases.items():
			with self.subTest(name):
				arrange()
				self.dispatch()
				self.enqueue.assert_not_called()
				self.assertIsNone(shadow_state.night_info(NIGHT))
				self.current = settings()
		with patch.dict(frappe.conf, {"encryption_key": ""}):
			self.dispatch()
		self.enqueue.assert_not_called()
		with patch.object(shadow, "source_profile", return_value=SimpleNamespace(major="15")):
			self.dispatch()
		self.enqueue.assert_not_called()

	def test_it_runs_while_the_mode_is_on_too(self):
		self.current = settings(mode="on", served=(COMPANY_A,), shadow_list=())
		self.dispatch()
		self.enqueue.assert_called_once()
		self.assertEqual(shadow_state.night_info(NIGHT)["count"], 8)

	def test_a_held_lease_means_a_unit_is_running_and_dispatch_waits(self):
		token = shadow_state.take_lease()
		self.addCleanup(shadow_state.release_lease, token)
		self.dispatch()
		self.enqueue.assert_not_called()

	def test_a_finished_night_is_not_started_again(self):
		self.start_night()
		shadow_state.set_cursor(NIGHT, 16)
		self.dispatch()
		self.enqueue.assert_not_called()

	def test_a_killed_unit_is_marked_and_the_chain_resumes_with_the_next_one(self):
		self.start_night()
		shadow_state.set_cursor(NIGHT, 3)
		units = shadow.units_for(self.current)
		company, side, scope = units[3]
		shadow_state.put_record(
			digest(company), NIGHT, f"{side}:{scope}", {"state": "in_progress", "reader_build": BUILD}
		)
		self.dispatch()
		record = self.unit_record(company, f"{side}:{scope}")
		self.assertEqual((record["outcome"], record["reason"]), ("not_compared", "killed"))
		self.assertEqual(shadow_state.cursor(NIGHT), 4)
		self.assertEqual(self.enqueue.call_args.kwargs["job_id"], f"pcal-shadow:{NIGHT}:4")

	def test_a_pending_unit_is_enqueued_again_under_its_own_job_id(self):
		self.start_night()
		shadow_state.set_cursor(NIGHT, 5)
		self.dispatch()
		self.assertEqual(self.enqueue.call_args.kwargs["job_id"], f"pcal-shadow:{NIGHT}:5")
		self.assertTrue(self.enqueue.call_args.kwargs["deduplicate"])

	def test_units_after_the_night_budget_get_time_cap(self):
		self.start_night(started=time() - shadow.NIGHT_BUDGET_S - 5)
		shadow_state.set_cursor(NIGHT, 14)
		self.dispatch()
		self.enqueue.assert_not_called()
		for n in (14, 15):
			company, side, scope = shadow.units_for(self.current)[n]
			record = self.unit_record(company, f"{side}:{scope}")
			self.assertEqual((record["outcome"], record["reason"]), ("not_compared", "time_cap"))
		self.assertEqual(shadow_state.cursor(NIGHT), 16)

	def test_the_night_stores_a_digest_of_its_unit_list_not_the_names(self):
		self.dispatch()
		info = shadow_state.night_info(NIGHT)
		self.assertEqual(info["restarts"], 0)
		self.assertRegex(info["units"], r"^[0-9a-f]{16}$")
		for name in (COMPANY_A, COMPANY_B):
			self.assertNotIn(name, info["units"])

	def test_a_changed_company_list_restarts_the_night_once_instead_of_stopping(self):
		self.dispatch()
		shadow_state.set_cursor(NIGHT, 5)
		self.enqueue.reset_mock()
		self.current = settings(shadow_list=(COMPANY_A,))
		self.dispatch()
		info = shadow_state.night_info(NIGHT)
		self.assertEqual((info["count"], info["cursor"], info["restarts"]), (8, 0, 1))
		self.assertEqual(self.enqueue.call_args.kwargs["job_id"], f"pcal-shadow:{NIGHT}:0")
		incomplete = [c for c in self.log_error.call_args_list if c.kwargs["title"] == INCOMPLETE_TITLE]
		self.assertEqual(len(incomplete), 1)
		self.assertIn("reason=units_changed", incomplete[0].kwargs["message"])

	def test_a_swap_of_one_company_for_another_with_the_same_count_also_restarts(self):
		self.dispatch()
		self.enqueue.reset_mock()
		self.current = settings(shadow_list=(COMPANY_A, COMPANY_B + " other"))
		self.dispatch()
		self.assertEqual(shadow_state.night_info(NIGHT)["restarts"], 1)
		self.enqueue.assert_called_once()

	def test_a_second_change_the_same_night_stops_the_chain_with_a_row(self):
		self.dispatch()
		self.current = settings(shadow_list=(COMPANY_A,))
		self.dispatch()
		self.enqueue.reset_mock()
		self.log_error.reset_mock()
		self.current = settings(shadow_list=(COMPANY_B,))
		self.dispatch()
		self.enqueue.assert_not_called()
		self.assertEqual(shadow_state.night_info(NIGHT)["restarts"], 1)
		self.assertEqual(self.log_error.call_args.kwargs["title"], INCOMPLETE_TITLE)
		self.assertIn("reason=units_changed_again", self.log_error.call_args.kwargs["message"])

	def test_a_killed_unit_writes_an_incomplete_row_with_the_digest_and_no_name(self):
		self.start_night()
		shadow_state.set_cursor(NIGHT, 2)
		shadow_state.put_record(
			digest(COMPANY_A), NIGHT, "Payable:next", {"state": "in_progress", "reader_build": BUILD}
		)
		self.dispatch()
		kwargs = self.log_error.call_args.kwargs
		self.assertEqual(kwargs["title"], INCOMPLETE_TITLE)
		self.assertIn("reason=killed", kwargs["message"])
		self.assertIn(f"company={digest(COMPANY_A)}", kwargs["message"])
		self.assertNotIn(COMPANY_A, kwargs["message"])

	def test_the_time_cap_writes_one_incomplete_row_per_company(self):
		self.start_night(started=time() - shadow.NIGHT_BUDGET_S - 5)
		shadow_state.set_cursor(NIGHT, 6)
		self.dispatch()
		rows_written = [c for c in self.log_error.call_args_list if c.kwargs["title"] == INCOMPLETE_TITLE]
		self.assertEqual(len(rows_written), 2)
		self.assertTrue(all("reason=time_cap" in c.kwargs["message"] for c in rows_written))

	def test_the_dispatch_starts_the_night_at_the_rotated_company(self):
		night = "2031-03-16"
		with patch.object(shadow, "_today", return_value=night):
			self.dispatch()
		units = shadow.units_for(self.current, night)
		self.assertEqual(self.enqueue.call_args.kwargs["night"], night)
		self.assertEqual(units[0][0], COMPANY_B)


class TestStorageReads(TestCase):
	def test_record_reads_propagate_both_job_timeouts(self):
		for timeout in TIMEOUTS:
			with (
				self.subTest(timeout.__name__),
				patch.object(frappe.cache, "execute_command", side_effect=timeout),
				self.assertRaises(timeout),
			):
				shadow_state.records("digest", NIGHT)

	def test_day_counts_propagate_both_job_timeouts(self):
		for timeout in TIMEOUTS:
			with (
				self.subTest(timeout.__name__),
				patch.object(frappe.cache, "execute_command", side_effect=timeout),
				self.assertRaises(timeout),
			):
				read_log.day_counts("digest", NIGHT)

	def test_a_cache_failure_still_returns_empty_records_and_counts(self):
		with patch.object(frappe.cache, "execute_command", side_effect=ConnectionError):
			self.assertEqual(shadow_state.records("digest", NIGHT), {})
			self.assertEqual(read_log.day_counts("digest", NIGHT), {})


class TestDailyRow(ShadowCase):
	def test_a_timeout_in_any_daily_step_stops_dispatch(self):
		for timeout in TIMEOUTS:
			for target, name in (
				(routing, "listed_but_missing"),
				(read_log, "day_counts"),
				(read_log, "error_log_once"),
			):
				with (
					self.subTest(timeout=timeout.__name__, step=name),
					patch.object(target, name, side_effect=timeout),
					self.assertRaises(timeout),
				):
					self.count(COMPANY_A, "probe")
					shadow.dispatch()
		self.enqueue.assert_not_called()

	def count(self, company, field, value=1, date=YESTERDAY):
		key = frappe.cache.make_key(f"jarvis:pcal:reader:day:{digest(company)}:{date}")
		frappe.cache.execute_command("HINCRBY", key, field, value)

	def test_failures_of_yesterday_write_one_row_however_often_dispatch_runs(self):
		self.count(COMPANY_A, "fallback:control_total", 2)
		for _ in range(3):
			shadow.dispatch()
		self.assertEqual(self.log_error.call_count, 1)
		kwargs = self.log_error.call_args.kwargs
		self.assertEqual(kwargs["title"], "Payment Calendar reader daily")
		self.assertIn(f"company={digest(COMPANY_A)}", kwargs["message"])
		self.assertIn("fallback:control_total=2", kwargs["message"])
		self.assertNotIn(COMPANY_A, kwargs["message"])

	def test_a_failure_class_with_digits_is_kept(self):
		self.count(COMPANY_A, "reader_exception:Error2Class", 3)
		shadow.dispatch()
		self.assertIn("reader_exception:Error2Class=3", self.log_error.call_args.kwargs["message"])

	def test_the_row_lists_every_counter_served_included_when_any_class_but_served_is_above_zero(self):
		for name, value in (("served", 40), ("probe", 3), ("busy", 1), ("ineligible:column_filter", 9)):
			self.count(COMPANY_A, name, value)
		shadow.dispatch()
		message = self.log_error.call_args.kwargs["message"]
		for part in ("served=40", "probe=3", "busy=1", "ineligible:column_filter=9", "failures=0"):
			self.assertIn(part, message)

	def test_a_listed_company_that_does_not_exist_is_in_the_row_even_with_no_other_counter(self):
		self.missing = (COMPANY_B,)
		shadow.dispatch()
		self.assertEqual(self.log_error.call_count, 1)
		message = self.log_error.call_args.kwargs["message"]
		self.assertIn(f"company={digest(COMPANY_B)}", message)
		self.assertIn("listed_but_missing=1", message)
		self.assertNotIn(COMPANY_B, message)

	def test_the_row_is_the_one_read_log_builds(self):
		self.count(COMPANY_A, "probe", 3)
		expected = read_log.daily_report(digest(COMPANY_A), YESTERDAY, extra={"listed_but_missing": 0})
		shadow.dispatch()
		self.assertEqual(self.log_error.call_args.kwargs["message"], expected)

	def test_a_company_that_was_only_served_writes_no_row(self):
		self.count(COMPANY_A, "served", 12)
		shadow.dispatch()
		self.log_error.assert_not_called()

	def test_a_company_that_was_never_served_because_of_its_size_is_not_silent(self):
		self.count(COMPANY_B, "probe", 30)
		shadow.dispatch()
		self.assertEqual(self.log_error.call_count, 1)
		self.assertIn("probe=30", self.log_error.call_args.kwargs["message"])

	def test_the_row_is_written_before_the_mode_check(self):
		self.current = settings(mode="off")
		self.count(COMPANY_B, "reader_exception:RuntimeError")
		shadow.dispatch()
		self.assertEqual(self.log_error.call_count, 1)

	def test_a_cache_failure_writes_no_row(self):
		self.count(COMPANY_A, "fallback:deadline")
		original = frappe.cache.execute_command

		def failing(*args, **kwargs):
			if args[0] == "SET" and b"errlog" in args[1]:
				raise ConnectionError
			return original(*args, **kwargs)

		with patch.object(frappe.cache, "execute_command", side_effect=failing):
			shadow.dispatch()
		self.log_error.assert_not_called()

	def test_each_company_has_its_own_row(self):
		self.count(COMPANY_A, "fallback:deadline")
		self.count(COMPANY_B, "fallback:deadline")
		shadow.dispatch()
		self.assertEqual(self.log_error.call_count, 2)


class TestRunUnit(ShadowCase):
	def run_unit(self, n=0, measure=None):
		measure = measure or MagicMock(return_value=done())
		with patch.object(shadow, "_measure", measure):
			shadow.run_unit(n, NIGHT)
		return measure

	def test_a_unit_stores_its_record_advances_the_cursor_and_chains_the_next(self):
		self.start_night()
		self.run_unit(0)
		record = self.unit_record(COMPANY_A, "Payable:previous")
		self.assertEqual(
			(record["state"], record["outcome"], record["reader_build"]), ("done", "compared", BUILD)
		)
		self.assertEqual(shadow_state.cursor(NIGHT), 1)
		self.assertEqual(self.enqueue.call_args.kwargs["job_id"], f"pcal-shadow:{NIGHT}:1")

	def test_the_last_unit_chains_nothing(self):
		self.start_night()
		self.run_unit(15)
		self.enqueue.assert_not_called()
		self.assertEqual(shadow_state.cursor(NIGHT), 16)

	def test_the_lease_is_held_with_a_token_while_it_runs_and_freed_before_the_next_job(self):
		self.start_night()
		seen = {}

		def measure(*args):
			seen["token"] = shadow_state.lease_value()
			seen["record"] = self.unit_record(COMPANY_A, "Payable:previous")
			return done()

		self.enqueue.side_effect = lambda *a, **k: seen.setdefault(
			"held_at_enqueue", shadow_state.lease_held()
		)
		self.run_unit(0, MagicMock(side_effect=measure))
		self.assertTrue(seen["token"])
		self.assertEqual(seen["record"]["state"], "in_progress")
		self.assertFalse(seen["held_at_enqueue"])
		self.assertFalse(shadow_state.lease_held())

	def test_a_unit_does_not_run_while_another_holds_the_lease(self):
		self.start_night()
		token = shadow_state.take_lease()
		self.addCleanup(shadow_state.release_lease, token)
		measure = self.run_unit(0)
		measure.assert_not_called()
		self.assertTrue(shadow_state.lease_held())

	def test_a_lease_is_released_only_by_its_token(self):
		token = shadow_state.take_lease()
		self.addCleanup(shadow_state.release_lease, token)
		shadow_state.release_lease("not-the-token")
		self.assertTrue(shadow_state.lease_held())
		shadow_state.release_lease(token)
		self.assertFalse(shadow_state.lease_held())

	def test_an_unexpected_error_is_a_not_compared_record_and_an_error_log_row_without_text(self):
		self.start_night()
		self.run_unit(0, MagicMock(side_effect=RuntimeError("Shadow Test Co A leaked")))
		record = self.unit_record(COMPANY_A, "Payable:previous")
		self.assertEqual((record["outcome"], record["reason"]), ("not_compared", "error:RuntimeError"))
		self.assertEqual(self.log_error.call_args.kwargs["title"], DIFF_TITLE)
		self.assertNotIn("leaked", self.log_error.call_args.kwargs["message"])
		self.assertEqual(shadow_state.cursor(NIGHT), 1)

	def test_a_job_timeout_leaves_the_marker_frees_the_lease_and_chains_nothing(self):
		self.start_night()
		with self.assertRaises(JobTimeoutException):
			self.run_unit(0, MagicMock(side_effect=JobTimeoutException()))
		self.assertEqual(self.unit_record(COMPANY_A, "Payable:previous")["state"], "in_progress")
		self.assertFalse(shadow_state.lease_held())
		self.enqueue.assert_not_called()
		self.assertEqual(shadow_state.cursor(NIGHT), 0)
		shadow.dispatch()  # the resume cron
		self.assertEqual(self.unit_record(COMPANY_A, "Payable:previous")["reason"], "killed")
		self.assertEqual(self.enqueue.call_args.kwargs["job_id"], f"pcal-shadow:{NIGHT}:1")

	def test_a_diff_writes_one_error_log_row_per_company_and_night(self):
		self.start_night()
		diff = done("diff", counts={"only_today": 1})
		self.run_unit(0, MagicMock(return_value=diff))
		self.run_unit(1, MagicMock(return_value=diff))
		self.assertEqual(self.log_error.call_count, 1)
		message = self.log_error.call_args.kwargs["message"]
		self.assertIn(f"company={digest(COMPANY_A)}", message)
		self.assertNotIn(COMPANY_A, message)

	def test_a_unit_writes_one_info_line_with_digest_outcome_and_times_and_no_name(self):
		self.start_night()
		record = done(today_ms=120, reader_ms=45)
		with patch.object(frappe, "logger") as logger:
			self.run_unit(0, MagicMock(return_value=record))
		logger.assert_called_with("payment_calendar")
		line = logger.return_value.info.call_args.args[0]
		for part in (
			f"company={digest(COMPANY_A)}",
			"unit=Payable:previous",
			"outcome=compared",
			"today_ms=120",
			"reader_ms=45",
		):
			self.assertIn(part, line)
		self.assertNotIn(COMPANY_A, line)
		self.assertEqual(logger.return_value.info.call_count, 1)

	def test_a_compared_unit_writes_no_error_log_row(self):
		self.start_night()
		self.run_unit(0)
		self.log_error.assert_not_called()

	def test_an_already_finished_unit_is_not_run_again(self):
		self.start_night()
		shadow_state.put_record(digest(COMPANY_A), NIGHT, "Payable:previous", done())
		measure = self.run_unit(0)
		measure.assert_not_called()
		self.assertEqual(self.enqueue.call_args.kwargs["job_id"], f"pcal-shadow:{NIGHT}:1")

	def test_a_unit_after_the_night_budget_is_capped_not_run(self):
		self.start_night(started=time() - shadow.NIGHT_BUDGET_S - 5)
		measure = self.run_unit(2)
		measure.assert_not_called()
		self.assertEqual(self.unit_record(COMPANY_A, "Payable:next")["reason"], "time_cap")

	def test_mode_off_stops_the_unit(self):
		self.start_night()
		self.current = settings(mode="off")
		self.assertEqual(self.run_unit(0).call_count, 0)
		self.enqueue.assert_not_called()

	def test_a_changed_list_restarts_the_night_once_and_the_unit_runs_on_the_new_list(self):
		self.start_night(units_hash="0" * 16)
		self.assertEqual(self.run_unit(0).call_count, 1)
		self.assertEqual(shadow_state.night_info(NIGHT)["restarts"], 1)
		self.assertEqual(shadow_state.night_info(NIGHT)["cursor"], 1)

	def test_a_second_mismatch_stops_the_unit(self):
		self.start_night(units_hash="0" * 16, restarts=1)
		self.assertEqual(self.run_unit(0).call_count, 0)
		self.enqueue.assert_not_called()

	def test_a_unit_chain_covers_the_rotated_order(self):
		night = "2031-03-16"
		units = shadow.units_for(self.current, night)
		measure = MagicMock(return_value=done())
		with patch.object(shadow, "_measure", measure):
			shadow.run_unit(0, night)
		self.assertEqual(measure.call_args.args[0], units[0][0])
		self.assertEqual(units[0][0], COMPANY_B)

	def test_the_lease_outlives_a_unit_the_queue_kills_late(self):
		# RQ kills a hung job at its timeout plus a grace; the lease must still be held until then.
		self.assertGreaterEqual(shadow_state.LEASE_TTL, 900)
		self.assertGreater(shadow_state.LEASE_TTL, shadow.UNIT_TIMEOUT_S + 60)
		token = shadow_state.take_lease()
		self.addCleanup(shadow_state.release_lease, token)
		ttl = frappe.cache.execute_command("TTL", frappe.cache.make_key(shadow_state.lease_key()))
		self.assertGreater(ttl, 890)

	def test_a_job_timeout_reaches_the_runner_as_a_fresh_exception_with_no_names_in_its_traceback(self):
		self.start_night()

		def measure(company, side, scope):
			secret_party = "PARTY-" + company  # a local of a frame under the unit
			raise JobTimeoutException(secret_party)

		with patch.dict(frappe.conf, {"developer_mode": 1}), self.assertRaises(JobTimeoutException) as raised:
			self.run_unit(0, MagicMock(side_effect=measure))
		error = raised.exception
		self.assertIsNone(error.__cause__)
		self.assertTrue(error.__suppress_context__)
		self.assertEqual(error.args, ())
		# What Frappe's job runner stores in developer mode: the traceback with the locals of every frame.
		try:
			raise error
		except JobTimeoutException:
			report = frappe.utils.get_traceback(with_context=True)
		self.assertNotIn(COMPANY_A, report)
		self.assertNotIn("PARTY-", report)
		for frame, _ in traceback.walk_tb(error.__traceback__):
			self.assertNotIn(COMPANY_A, repr(frame.f_locals))
		self.assertEqual(self.unit_record(COMPANY_A, "Payable:previous")["state"], "in_progress")
		self.assertFalse(shadow_state.lease_held())

	def test_a_plain_timeout_error_is_fresh_too(self):
		self.start_night()
		with self.assertRaises(TimeoutError) as raised:
			self.run_unit(0, MagicMock(side_effect=TimeoutError("secret " + COMPANY_A)))
		self.assertEqual(raised.exception.args, ())

	def test_a_transient_database_error_is_retryable_and_writes_no_row(self):
		self.start_night()
		retry = done("not_compared", "error_transient", state="retry")
		self.run_unit(0, MagicMock(return_value=retry))
		record = self.unit_record(COMPANY_A, "Payable:previous")
		self.assertEqual((record["state"], record["attempts"]), ("retry", 1))
		self.log_error.assert_not_called()
		self.assertEqual(shadow_state.cursor(NIGHT), 1)

	def test_a_unit_in_retry_state_runs_again_and_a_success_replaces_it(self):
		self.start_night()
		shadow_state.put_record(
			digest(COMPANY_A),
			NIGHT,
			"Payable:previous",
			done("not_compared", "error_transient", state="retry", attempts=1),
		)
		measure = self.run_unit(0)
		measure.assert_called_once()
		self.assertEqual(self.unit_record(COMPANY_A, "Payable:previous")["outcome"], "compared")

	def test_the_second_transient_failure_ends_the_unit_as_done(self):
		self.start_night()
		shadow_state.put_record(
			digest(COMPANY_A),
			NIGHT,
			"Payable:previous",
			done("not_compared", "error_transient", state="retry", attempts=1),
		)
		retry = done("not_compared", "error_transient", state="retry")
		self.run_unit(0, MagicMock(return_value=retry))
		record = self.unit_record(COMPANY_A, "Payable:previous")
		self.assertEqual(
			(record["state"], record["attempts"], record["reason"]), ("done", 2, "error_transient")
		)

	def test_a_retry_unit_run_moves_neither_the_cursor_nor_the_chain(self):
		self.start_night()
		shadow_state.set_cursor(NIGHT, 16)
		shadow_state.put_record(
			digest(COMPANY_A),
			NIGHT,
			"Payable:previous",
			done("not_compared", "error_transient", state="retry", attempts=1),
		)
		with patch.object(shadow, "_measure", MagicMock(return_value=done())):
			shadow.run_unit(0, NIGHT, retry=True)
		self.assertEqual(shadow_state.cursor(NIGHT), 16)
		self.enqueue.assert_not_called()

	def test_a_finished_night_re_enqueues_a_retry_unit_under_its_own_job_id(self):
		self.start_night()
		shadow_state.set_cursor(NIGHT, 16)
		shadow_state.put_record(
			digest(COMPANY_A),
			NIGHT,
			"Payable:next",
			done("not_compared", "error_transient", state="retry", attempts=1),
		)
		shadow.dispatch()
		self.assertEqual(self.enqueue.call_args.kwargs["job_id"], f"pcal-shadow:{NIGHT}:2:r1")
		self.assertIs(self.enqueue.call_args.kwargs["retry"], True)

	def test_a_finished_night_without_retry_units_enqueues_nothing(self):
		self.start_night()
		shadow_state.set_cursor(NIGHT, 16)
		shadow_state.put_record(digest(COMPANY_A), NIGHT, "Payable:next", done())
		shadow.dispatch()
		self.enqueue.assert_not_called()

	def test_a_killed_retry_run_leaves_a_retry_record_with_the_attempt_counted_not_a_stuck_marker(self):
		self.start_night()
		shadow_state.set_cursor(NIGHT, 16)
		shadow_state.put_record(
			digest(COMPANY_A),
			NIGHT,
			"Payable:previous",
			done("not_compared", "error_transient", state="retry", attempts=1),
		)
		with self.assertRaises(JobTimeoutException):
			with patch.object(shadow, "_measure", MagicMock(side_effect=JobTimeoutException())):
				shadow.run_unit(0, NIGHT, retry=True)
		record = self.unit_record(COMPANY_A, "Payable:previous")
		self.assertEqual((record["state"], record["attempts"]), ("retry", 2))
		self.assertFalse(shadow_state.lease_held())

	def test_a_retry_unit_out_of_attempts_is_closed_as_done_and_not_enqueued_again(self):
		self.start_night()
		shadow_state.set_cursor(NIGHT, 16)
		shadow_state.put_record(
			digest(COMPANY_A),
			NIGHT,
			"Payable:next",
			done("not_compared", "error_transient", state="retry", attempts=shadow.RETRY_MAX),
		)
		shadow.dispatch()
		self.enqueue.assert_not_called()
		record = self.unit_record(COMPANY_A, "Payable:next")
		self.assertEqual((record["state"], record["reason"]), ("done", "error_transient"))

	def test_a_failed_row_write_does_not_skip_the_commit_or_the_next_unit(self):
		self.start_night()
		diff = done("diff", counts={"only_today": 1})
		with patch.object(read_log, "error_log_once", side_effect=RuntimeError("db blip")):
			self.run_unit(0, MagicMock(return_value=diff))
		self.assertEqual(self.unit_record(COMPANY_A, "Payable:previous")["outcome"], "diff")
		self.assertEqual(shadow_state.cursor(NIGHT), 1)
		self.assertEqual(self.enqueue.call_args.kwargs["job_id"], f"pcal-shadow:{NIGHT}:1")
		frappe.db.commit.assert_called()

	def test_a_job_timeout_in_the_row_write_still_propagates(self):
		self.start_night()
		with patch.object(read_log, "error_log_once", side_effect=JobTimeoutException()):
			with self.assertRaises(JobTimeoutException):
				self.run_unit(0, MagicMock(return_value=done("diff")))

	def test_a_shadow_fallback_writes_one_row_per_company_night_and_reason(self):
		self.start_night()
		fallback = done("not_compared", "fallback:control_total")
		self.run_unit(0, MagicMock(return_value=fallback))
		self.run_unit(1, MagicMock(return_value=fallback))
		rows_written = [c for c in self.log_error.call_args_list if c.kwargs["title"] == FALLBACK_TITLE]
		self.assertEqual(len(rows_written), 1)
		message = rows_written[0].kwargs["message"]
		self.assertIn("reason=fallback:control_total", message)
		self.assertIn(f"company={digest(COMPANY_A)}", message)
		self.assertNotIn(COMPANY_A, message)

	def test_an_ineligible_unit_and_a_size_skip_write_a_fallback_row(self):
		self.start_night()
		self.run_unit(0, MagicMock(return_value=done("not_compared", "ineligible")))
		self.run_unit(1, MagicMock(return_value=done("not_compared", "shadow_too_large")))
		titles = [c.kwargs["title"] for c in self.log_error.call_args_list]
		self.assertEqual(titles, [FALLBACK_TITLE, FALLBACK_TITLE])

	def test_a_source_change_and_a_kill_marker_write_no_fallback_row(self):
		self.start_night()
		self.run_unit(0, MagicMock(return_value=done("not_compared", "source_changed")))
		self.log_error.assert_not_called()

	def test_records_are_json_so_a_redis_failure_raises_instead_of_vanishing(self):
		original = frappe.cache.execute_command

		def failing(*args, **kwargs):
			if args[0] in ("HSET", "EXPIRE"):
				raise ConnectionError
			return original(*args, **kwargs)

		with (
			patch.object(frappe.cache, "execute_command", side_effect=failing),
			self.assertRaises(ConnectionError),
		):
			shadow_state.put_record(digest(COMPANY_A), NIGHT, "Payable:next", done())
		shadow_state.put_record(
			digest(COMPANY_A), NIGHT, "Payable:next", done(counts={"rows": 3}, digests=["a"])
		)
		self.assertEqual(
			shadow_state.records(digest(COMPANY_A), NIGHT)["Payable:next"]["counts"], {"rows": 3}
		)

	def test_records_keys_are_under_the_persistent_prefix_with_a_ttl(self):
		self.start_night()
		self.run_unit(0)
		name = shadow_state.record_key(digest(COMPANY_A), NIGHT)
		persistent = frappe.get_hooks("persistent_cache_keys")
		for key in (name, shadow_state.lease_key(), shadow_state.night_key(NIGHT)):
			self.assertTrue(key.startswith("jarvis:pcal:"), key)
			self.assertTrue(any(key.startswith(prefix) for prefix in persistent), key)
		ttl = frappe.cache.execute_command("TTL", frappe.cache.make_key(name))
		self.assertTrue(34 * 86400 < ttl <= 35 * 86400, ttl)

	def test_records_survive_a_full_cache_clear(self):
		self.start_night()
		self.run_unit(0)
		frappe.clear_cache()
		self.assertIn("Payable:previous", shadow_state.records(digest(COMPANY_A), NIGHT))
		self.assertIsNotNone(shadow_state.night_info(NIGHT))


class TestMeasure(ShadowCase):
	"""One unit with the two reads patched: the view, the caps, the user and what is stored."""

	def setUp(self):
		super().setUp()
		self.clock = Clock()
		self.events = []
		self.in_view = False
		# `normalize_scope` checks that the company exists: a CI site has none, so this test makes one (bare, in
		# its own transaction; the commit is patched and the rows are deleted at the end).
		require_calendar_test_site()
		self.fx = CalendarFixtures()
		self.addCleanup(self.drop_fixture_rows)
		self.company = self.fx.company
		self.addCleanup(purge_real_keys, self.company)
		# One date for the fixtures, the scope check and the month assertions: a run over midnight keeps it.
		clock = ExitStack()
		clock.enter_context(frozen_clock(frappe.utils.now()[:19]))
		self.addCleanup(clock.close)
		patch.object(shadow, "_today", frappe.utils.nowdate).start()  # normalize_scope checks the real date
		patch.object(shadow, "monotonic", self.clock).start()
		patch.object(routing, "monotonic", self.clock).start()  # routing._ms times the reads
		patch.object(shadow, "consistent_read", self.view).start()
		patch.object(shadow, "_rss_mb", return_value=10.0).start()
		patch.object(today_sources, "_ledger_rows_exceed", return_value=False).start()
		self.today = MagicMock(side_effect=self.read_today)
		self.reader = MagicMock(side_effect=self.read_reader)
		patch.object(shadow, "today_unguarded", self.today).start()
		patch.object(shadow.builtin_reader, "read_obligations", self.reader).start()

	def drop_fixture_rows(self):
		for doctype, name in reversed(self.fx.created):
			frappe.db.delete(doctype, {"name": name})

	@contextmanager
	def view(self):
		self.views = getattr(self, "views", 0) + 1
		self.in_view = True
		try:
			yield {}
		finally:
			self.in_view = False

	def read_today(self, *args, **kwargs):
		self.assertTrue(self.in_view, "today's read ran outside the view")
		self.assertEqual(frappe.session.user, "Administrator")
		return rows("a")

	def read_reader(self, *args, **kwargs):
		self.assertTrue(self.in_view, "the reader ran outside the view")
		self.assertEqual(frappe.session.user, "Administrator")
		return rows("a")

	def measure(self, side="Payable", scope="current"):
		return shadow._measure(self.company, side, scope)

	def test_both_reads_run_in_one_view_and_compare(self):
		record = self.measure()
		self.assertEqual(self.views, 1)
		self.assertEqual(record["outcome"], "compared")
		self.assertEqual(record["reader_build"], BUILD)
		self.assertFalse(record["would_deadline"])
		self.today.assert_called_once()
		self.reader.assert_called_once()

	def test_the_reads_get_the_scope_of_the_unit(self):
		self.measure("Receivable", "next")
		args = self.today.call_args.args
		self.assertEqual((args[0], args[1]), (self.company, "Receivable"))
		first, last = args[3], args[4]
		self.assertEqual(first[:7], shadow.month_keys(frappe.utils.nowdate())[2])
		self.assertEqual(last[:7], first[:7])
		self.measure("Receivable", "window")
		args = self.today.call_args.args
		self.assertLess(args[3], args[4])
		self.assertGreater(len(args[4]), 0)
		self.assertNotEqual(args[3][:7], args[4][:7])

	def test_the_reader_has_a_sixty_second_stop_and_the_user_is_restored(self):
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		self.measure()
		self.assertEqual(frappe.session.user, "Guest")
		deadline = self.reader.call_args.kwargs["deadline"]
		self.assertAlmostEqual(deadline - self.clock.now, shadow.READER_STOP_S, delta=1)
		self.assertIsInstance(self.reader.call_args.kwargs["stats"], dict)

	def test_a_ledger_over_the_cap_is_not_compared_and_neither_read_runs(self):
		with patch.object(today_sources, "_ledger_rows_exceed", return_value=True) as probe:
			record = self.measure()
		self.assertEqual(probe.call_args.args[3], shadow.SHADOW_LEDGER_CAP)
		self.assertEqual((record["outcome"], record["reason"]), ("not_compared", "shadow_too_large"))
		self.today.assert_not_called()
		self.reader.assert_not_called()

	def test_the_shadow_ledger_cap_is_todays_own_guard_so_the_oracle_read_stays_bounded(self):
		self.assertEqual(shadow.SHADOW_LEDGER_CAP, today_sources.MAX_LEDGER_ROWS)
		self.assertEqual(shadow.WOULD_DEADLINE_S, routing.READER_DEADLINE_S)
		self.assertNotIn("ERROR_LOG_WINDOW", vars(shadow))
		self.assertNotIn("_ms", vars(shadow))  # routing's helper is the one
		self.assertEqual(shadow_state.RECORD_TTL, read_log.COUNTER_TTL_S)

	def test_the_probe_runs_before_the_oracle_read(self):
		order = []
		with patch.object(
			today_sources, "_ledger_rows_exceed", side_effect=lambda *a: order.append("probe") or False
		):
			self.today.side_effect = lambda *a, **k: order.append("oracle") or rows("a")
			self.measure()
		self.assertEqual(order[:2], ["probe", "oracle"])

	def test_memory_over_the_cap_between_the_reads_skips_the_reader(self):
		with patch.object(shadow, "_rss_mb", side_effect=[10.0, shadow.RSS_CAP_MB + 1]):
			record = self.measure()
		self.assertEqual((record["outcome"], record["reason"]), ("not_compared", "rss_cap"))
		self.today.assert_called_once()
		self.reader.assert_not_called()

	def test_memory_over_the_cap_before_the_oracle_skips_both_reads(self):
		with patch.object(shadow, "_rss_mb", return_value=shadow.RSS_CAP_MB + 1):
			record = self.measure()
		self.assertEqual((record["outcome"], record["reason"]), ("not_compared", "rss_cap"))
		self.today.assert_not_called()
		self.reader.assert_not_called()

	def test_a_reader_that_ran_past_thirty_seconds_sets_would_deadline(self):
		self.reader.side_effect = lambda *a, **k: (self.clock.advance(31), rows("a"))[1]
		record = self.measure()
		self.assertTrue(record["would_deadline"])
		self.assertEqual(record["outcome"], "compared")

	def test_a_fast_reader_does_not_set_would_deadline(self):
		self.reader.side_effect = lambda *a, **k: (self.clock.advance(29), rows("a"))[1]
		self.assertFalse(self.measure()["would_deadline"])

	def test_a_reader_fallback_is_not_compared(self):
		self.reader.side_effect = ReaderFallback("deadline")
		record = self.measure()
		self.assertEqual((record["outcome"], record["reason"]), ("not_compared", "fallback:deadline"))

	def test_a_broken_view_is_source_changed(self):
		@contextmanager
		def broken():
			yield {}
			raise CalendarSourceUnavailable("source_changed")

		with patch.object(shadow, "consistent_read", broken):
			record = self.measure()
		self.assertEqual((record["outcome"], record["reason"]), ("not_compared", "source_changed"))

	def test_a_lost_connection_in_a_read_is_a_retryable_record_not_a_fallback(self):
		for error in (
			frappe.db.OperationalError(2013, "lost"),
			frappe.db.InterfaceError("closed"),
			frappe.QueryDeadlockError("deadlock"),
			frappe.QueryTimeoutError("timeout"),
		):
			with self.subTest(type(error).__name__):
				self.today.side_effect = error
				record = self.measure()
				self.assertEqual(
					(record["state"], record["outcome"], record["reason"]),
					("retry", "not_compared", "error_transient"),
				)

	def test_a_transient_error_in_the_reader_is_retryable_too(self):
		self.reader.side_effect = frappe.db.OperationalError(2013, "lost")
		record = self.measure()
		self.assertEqual((record["state"], record["reason"]), ("retry", "error_transient"))

	def test_any_other_error_is_a_done_error_record_with_its_class(self):
		self.reader.side_effect = RuntimeError("boom")
		record = self.measure()
		self.assertEqual((record["state"], record["reason"]), ("done", "error:RuntimeError"))

	def test_the_shadow_no_longer_imports_the_view_lost_test(self):
		self.assertNotIn("is_view_lost", vars(shadow))

	def test_a_job_timeout_in_a_read_propagates(self):
		self.reader.side_effect = JobTimeoutException()
		with self.assertRaises(JobTimeoutException):
			self.measure()

	def test_nothing_is_written_while_the_view_is_open(self):
		self.current = settings(shadow_list=(self.company,))
		self.today.side_effect = lambda *a, **k: rows("a", "b")
		self.reader.side_effect = lambda *a, **k: rows("a")

		def guarded(original):
			def wrapper(*args, **kwargs):
				self.assertFalse(self.in_view, "a write ran inside the view")
				return original(*args, **kwargs)

			return wrapper

		with (
			patch.object(shadow_state, "put_record", guarded(shadow_state.put_record)),
			patch.object(shadow.read_log, "error_log_once", guarded(read_log.error_log_once)),
			patch.object(frappe.cache, "execute_command", guarded(frappe.cache.execute_command)),
		):
			shadow.run_unit(0, NIGHT)
		record = shadow_state.records(digest(self.company), NIGHT)["Payable:previous"]
		self.assertEqual(record["outcome"], "diff")
		self.assertEqual(self.views, 1)

	def test_a_measured_unit_is_stored_without_names_or_rows(self):
		self.current = settings(shadow_list=(self.company,))
		shadow.run_unit(1, NIGHT)
		record = shadow_state.records(digest(self.company), NIGHT)["Payable:current"]
		text = json.dumps(record)
		self.assertNotIn(self.company, text)
		self.assertNotIn("ACME-SECRET", text)
		self.assertNotIn("V-a", text)


class RealShadowCase(TestCase):
	"""Real fixtures, the real view and the real reader (committed rows, as the snapshot tests)."""

	def setUp(self):
		require_calendar_test_site()
		frappe.conf["encryption_key"] = MODULE_KEY
		frappe.set_user("Administrator")
		self.fx = CalendarFixtures()
		self.fx.invoice((100,))
		self.fx.invoice((100,), side="Receivable")
		self.fx.fiscal_year()
		self.fx.gl(1000)
		# A reader read falls back with `control_total` when an invoice header is stale against the ledger.
		sync_outstanding(self.fx.company)
		frappe.db.commit()
		key_prefix()
		patch.object(
			routing, "reader_settings", return_value=settings(shadow_list=(self.fx.company,))
		).start()
		self.addCleanup(patch.stopall)
		self.addCleanup(self.cleanup_rows)
		self.addCleanup(purge_real_keys, self.fx.company)

	def cleanup_rows(self):
		frappe.db.rollback()
		for doctype, name in reversed(self.fx.created):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		frappe.cache.hdel("fiscal_years", self.fx.company)
		frappe.clear_document_cache("Company", self.fx.company)

	def window_args(self, side="Payable"):
		start = frappe.utils.add_days(self.fx.today, -30)
		return (self.fx.company, side, self.fx.today, start, frappe.utils.add_days(self.fx.today, 40))


def purge_real_keys(company):
	"""The keys of a company other than the two test names: records under the test prefix, windows, counters."""
	frappe.cache.delete_keys(TEST_PREFIX)
	purge_digest_keys(company)


class TestTodayUnguarded(RealShadowCase):
	def test_below_the_guard_it_is_todays_read(self):
		today = today_sources.read_obligations(*self.window_args())
		self.assertEqual(shadow.today_unguarded(*self.window_args()), today)

	def test_above_the_guard_it_is_the_lifted_read_and_the_guards_come_back(self):
		below = today_sources.read_obligations(*self.window_args())
		with (
			patch.object(today_sources, "MAX_VOUCHERS", 0),
			patch.object(today_sources, "MAX_LEDGER_ROWS", 0),
		):
			with self.assertRaises(CalendarSourceUnavailable) as raised:
				today_sources.read_obligations(*self.window_args())
			self.assertEqual(raised.exception.code, "too_large")
			lifted = shadow.today_unguarded(*self.window_args())
			self.assertEqual((today_sources.MAX_VOUCHERS, today_sources.MAX_LEDGER_ROWS), (0, 0))
		self.assertEqual(shadow.canonical(lifted), shadow.canonical(below))


class TestRealUnit(RealShadowCase):
	def run_real(self, n):
		shadow_state.start_night(NIGHT, 8)
		with patch("frappe.enqueue"):
			shadow.run_unit(n, NIGHT)
		return shadow_state.records(digest(self.fx.company), NIGHT)

	# The fixture terms fall due 10 and 20 days from today. The window scope covers them on any day of the
	# month; a month scope would hold no due voucher late in the month.
	def test_a_real_unit_compares_the_reader_with_todays_read(self):
		records = self.run_real(3)  # Payable:window
		record = records["Payable:window"]
		self.assertEqual((record["outcome"], record.get("reason")), ("compared", None), record)
		self.assertGreater(record["counts"]["rows"], 0)

	def test_above_todays_guard_the_reader_is_compared_with_the_lifted_read(self):
		with (
			patch.object(today_sources, "MAX_VOUCHERS", 0),
			patch.object(today_sources, "MAX_LEDGER_ROWS", 0),
		):
			records = self.run_real(7)  # Receivable:window
		self.assertEqual(records["Receivable:window"]["outcome"], "compared", records)


class TestGate(ShadowCase):
	NIGHTS = 3

	def setUp(self):
		super().setUp()
		self.current = settings(shadow_list=(COMPANY_A,))
		self.addCleanup(self.delete_error_rows)

	@staticmethod
	def delete_error_rows():
		for company in (COMPANY_A, COMPANY_B):
			frappe.db.delete(
				"Error Log", {"method": DIFF_TITLE, "error": ["like", f"%company={digest(company)}%"]}
			)

	def seed(self, nights=NIGHTS, company=COMPANY_A, **overrides):
		"""`nights` nights of `compared` records for every side and scope."""
		for back in range(nights):
			night = frappe.utils.add_days(NIGHT, -back)
			for side in shadow.SIDES:
				for scope in shadow.SCOPES:
					record = done(**overrides)
					shadow_state.put_record(digest(company), night, f"{side}:{scope}", record)

	def summary(self, **kwargs):
		return shadow.summary(days=self.NIGHTS, **kwargs)[COMPANY_A]

	def test_enough_compared_nights_on_the_deployed_build_pass(self):
		self.seed()
		result = self.summary()
		self.assertTrue(result["pass"], result)
		self.assertEqual(result["missing"], [])

	def test_too_few_nights_fail_and_the_missing_nights_are_listed(self):
		self.seed(nights=2)
		result = self.summary()
		self.assertFalse(result["pass"])
		self.assertIn("Payable:current 2/3", result["missing"])
		self.assertEqual(len(result["missing"]), 8)

	def test_a_night_on_another_reader_build_does_not_count(self):
		self.seed()
		shadow_state.put_record(digest(COMPANY_A), NIGHT, "Payable:window", done(build=OTHER_BUILD))
		result = self.summary()
		self.assertFalse(result["pass"])
		self.assertEqual(result["missing"], ["Payable:window 2/3"])

	def test_a_missing_side_fails(self):
		self.seed()
		frappe.cache.execute_command(
			"HDEL",
			frappe.cache.make_key(shadow_state.record_key(digest(COMPANY_A), NIGHT)),
			"Receivable:next",
		)
		self.assertFalse(self.summary()["pass"])

	def test_an_error_log_row_for_a_diff_fails_the_gate(self):
		self.seed()
		with patch.object(shadow, "_diff_error_rows", return_value=1) as query:
			result = self.summary()
		self.assertFalse(result["pass"])
		self.assertEqual(result["diff_rows"], 1)
		self.assertEqual(query.call_args.args[0], digest(COMPANY_A))

	def test_the_real_error_log_query_counts_only_this_companys_rows_in_the_period(self):
		self.seed()
		before = self.summary()
		self.assertTrue(before["pass"])
		for company, expected in ((COMPANY_B, True), (COMPANY_A, False)):
			row = frappe.get_doc(
				{
					"doctype": "Error Log",
					"method": DIFF_TITLE,
					"error": f"unit=Payable:current outcome=diff company={digest(company)} build={BUILD}",
				}
			).insert(ignore_permissions=True)
			frappe.db.set_value("Error Log", row.name, "creation", f"{NIGHT} 03:00:00")
			self.assertEqual(self.summary()["pass"], expected, company)

	def test_a_diff_record_on_the_deployed_build_fails_even_without_a_row(self):
		self.seed()
		shadow_state.put_record(
			digest(COMPANY_A), NIGHT, "Payable:window", done("diff", counts={"ids_differ": 1})
		)
		# Two compared nights are still enough: the diff alone fails the gate.
		self.assertFalse(self.summary(nights=2)["pass"])

	def test_an_error_after_seven_good_nights_fails_when_its_log_row_cannot_be_written(self):
		self.seed(nights=8)
		with patch.object(shadow, "_write_unit_row", side_effect=RuntimeError):
			shadow._finish(
				digest(COMPANY_A), NIGHT, "Payable:window", done("not_compared", "error:RuntimeError")
			)
		result = shadow.summary(days=7)[COMPANY_A]
		self.assertEqual(result["missing"], [])
		self.assertEqual(result["diff_rows"], 0)
		self.assertFalse(result["pass"], result)
		self.assertEqual(result["error_records"], 1)

	def test_an_error_record_on_an_old_build_does_not_fail_the_current_build(self):
		self.seed(nights=8)
		shadow_state.put_record(
			digest(COMPANY_A),
			NIGHT,
			"Payable:window",
			done("not_compared", "error:RuntimeError", build=OTHER_BUILD),
		)
		result = shadow.summary(days=7)[COMPANY_A]
		self.assertTrue(result["pass"], result)
		self.assertEqual(result["error_records"], 0)

	def test_an_ids_only_difference_counts_as_a_diff(self):
		outcome = shadow.compare_unit((rows("a"), None), (rows("z"), None))
		self.seed()
		shadow_state.put_record(
			digest(COMPANY_A), NIGHT, "Payable:window", {**outcome, "state": "done", "reader_build": BUILD}
		)
		self.assertFalse(self.summary(nights=2)["pass"])

	def test_would_deadline_fails(self):
		self.seed()
		shadow_state.put_record(digest(COMPANY_A), NIGHT, "Payable:window", done(would_deadline=True))
		self.assertFalse(self.summary(nights=2)["pass"])

	def test_a_deadline_or_assemble_budget_fallback_fails(self):
		for reason in ("fallback:deadline", "fallback:assemble_budget"):
			with self.subTest(reason):
				self.seed()
				shadow_state.put_record(
					digest(COMPANY_A), NIGHT, "Payable:window", done("not_compared", reason)
				)
				self.assertTrue(self.summary(nights=2)["missing"] == [])
				self.assertFalse(self.summary(nights=2)["pass"])

	def test_a_missing_night_gets_its_reasons_counted_per_unit(self):
		self.seed(nights=2)
		shadow_state.put_record(
			digest(COMPANY_A),
			frappe.utils.add_days(NIGHT, -2),
			"Payable:window",
			done("not_compared", "fallback:control_total"),
		)
		shadow_state.put_record(
			digest(COMPANY_A),
			frappe.utils.add_days(NIGHT, -3),
			"Payable:window",
			done("not_compared", "fallback:control_total"),
		)
		shadow_state.put_record(
			digest(COMPANY_A),
			frappe.utils.add_days(NIGHT, -4),
			"Payable:window",
			done("not_compared", "killed"),
		)
		result = shadow.summary(days=5, nights=5)[COMPANY_A]
		self.assertEqual(result["reasons"]["Payable:window"], {"fallback:control_total": 2, "killed": 1})
		self.assertIn("Payable:window 2/5", result["missing"])

	def test_a_transient_unit_is_not_compared_not_a_diff_and_not_an_error_row(self):
		self.seed()
		shadow_state.put_record(
			digest(COMPANY_A),
			NIGHT,
			"Payable:window",
			done("not_compared", "error_transient", state="retry", attempts=1),
		)
		result = self.summary(nights=2)
		self.assertEqual((result["diff_rows"], result["diff_records"]), (0, 0))
		self.assertTrue(result["pass"], result)
		self.assertEqual(self.summary()["reasons"]["Payable:window"], {"error_transient": 1})

	def test_the_error_log_query_counts_only_the_diff_title(self):
		self.seed()
		for title in (FALLBACK_TITLE, INCOMPLETE_TITLE, "Payment Calendar reader daily"):
			row = frappe.get_doc(
				{
					"doctype": "Error Log",
					"method": title,
					"error": f"unit=Payable:current company={digest(COMPANY_A)} build={BUILD}",
				}
			).insert(ignore_permissions=True)
			frappe.db.set_value("Error Log", row.name, "creation", f"{NIGHT} 03:00:00")
		self.assertTrue(self.summary()["pass"])

	def test_deleting_the_redis_keys_makes_the_gate_false(self):
		self.seed()
		self.assertTrue(self.summary()["pass"])
		purge_keys()
		self.assertFalse(self.summary()["pass"])

	def test_seven_nights_before_tonight_pass_a_seven_day_gate_before_tonights_run(self):
		# 01:30 site time: tonight's chain has not started, the seven earlier nights are complete.
		for back in range(1, 8):
			night = frappe.utils.add_days(NIGHT, -back)
			for side in shadow.SIDES:
				for scope in shadow.SCOPES:
					shadow_state.put_record(digest(COMPANY_A), night, f"{side}:{scope}", done())
		self.assertEqual(shadow_state.records(digest(COMPANY_A), NIGHT), {})
		result = shadow.summary(days=7)[COMPANY_A]
		self.assertTrue(result["pass"], result)

	def test_six_nights_before_tonight_do_not_pass_a_seven_night_gate(self):
		for back in range(1, 7):
			night = frappe.utils.add_days(NIGHT, -back)
			for side in shadow.SIDES:
				for scope in shadow.SCOPES:
					shadow_state.put_record(digest(COMPANY_A), night, f"{side}:{scope}", done())
		self.assertFalse(shadow.summary(days=7)[COMPANY_A]["pass"])

	def test_the_nights_default_to_the_days(self):
		self.seed(nights=2)
		self.assertFalse(shadow.summary(days=3)[COMPANY_A]["pass"])
		self.assertTrue(shadow.summary(days=3, nights=2)[COMPANY_A]["pass"])


class TestErrorLogOnceExtension(FrappeTestCase):
	def setUp(self):
		frappe.conf["encryption_key"] = MODULE_KEY
		self.log_error = patch.object(frappe, "log_error").start()
		self.addCleanup(patch.stopall)
		self.addCleanup(frappe.cache.delete_keys, "jarvis:pcal:errlog:shadow-test-op")

	def test_the_window_is_the_key_ttl(self):
		read_log.error_log_once("t", "m", "shadow-test-op", "Cls", window=86_400)
		ttl = frappe.cache.execute_command(
			"TTL", frappe.cache.make_key("jarvis:pcal:errlog:shadow-test-op:Cls")
		)
		self.assertTrue(86_000 < ttl <= 86_400, ttl)

	def test_the_default_window_and_cache_down_rule_are_unchanged(self):
		with patch.object(frappe.cache, "execute_command", side_effect=ConnectionError):
			read_log.error_log_once("t", "m", "shadow-test-op", "Cls")
		self.assertEqual(self.log_error.call_count, 1)

	def test_when_cache_down_false_writes_no_row(self):
		with patch.object(frappe.cache, "execute_command", side_effect=ConnectionError):
			read_log.error_log_once("t", "m", "shadow-test-op", "Cls", when_cache_down=False)
		self.log_error.assert_not_called()

	def test_the_digest_is_stable_sixteen_hex_and_hides_the_name(self):
		first = read_log.company_digest("Some Company")
		self.assertEqual(first, read_log.company_digest("Some Company"))
		self.assertRegex(first, r"^[0-9a-f]{16}$")
		self.assertNotEqual(first, read_log.company_digest("Other Company"))
		self.assertNotIn("Some", first)

	def test_the_digest_needs_a_key_and_never_makes_one(self):
		with patch.dict(frappe.conf, {"encryption_key": ""}), self.assertRaises(RuntimeError):
			read_log.ops_digest("x")


class TestStatus(ShadowCase):
	"""`bench --site <site> execute jarvis.payment_calendar.shadow.status`: the operator's view."""

	def setUp(self):
		super().setUp()
		self.current = settings(mode="on", served=(COMPANY_A,), shadow_list=(COMPANY_B,))

	def status(self, **kwargs):
		out = io.StringIO()
		with redirect_stdout(out):
			result = shadow.status(**kwargs)
		return result, out.getvalue()

	def test_it_prints_the_mode_the_lists_and_the_digest_of_each_company(self):
		_, text = self.status()
		self.assertIn("mode: on", text)
		self.assertIn(f"served: {COMPANY_A}", text)
		self.assertIn(f"shadow: {COMPANY_B}", text)
		for company in (COMPANY_A, COMPANY_B):
			self.assertIn(f"{digest(company)} = {company}", text)

	def test_it_prints_todays_and_yesterdays_counters_per_company(self):
		key = lambda date: frappe.cache.make_key(f"jarvis:pcal:reader:day:{digest(COMPANY_A)}:{date}")  # noqa: E731
		frappe.cache.execute_command("HINCRBY", key(NIGHT), "served", 4)
		frappe.cache.execute_command("HINCRBY", key(YESTERDAY), "probe", 2)
		result, text = self.status()
		self.assertEqual(result["companies"][COMPANY_A]["counters"][NIGHT], {"served": 4})
		self.assertEqual(result["companies"][COMPANY_A]["counters"][YESTERDAY], {"probe": 2})
		self.assertIn("served=4", text)
		self.assertIn("probe=2", text)

	def test_it_prints_the_gate_per_side_and_scope_with_the_reasons_of_missing_nights(self):
		shadow_state.put_record(
			digest(COMPANY_B), NIGHT, "Payable:window", done("not_compared", "fallback:control_total")
		)
		result, text = self.status(days=3)
		gate = result["companies"][COMPANY_B]["gate"]
		self.assertFalse(gate["pass"])
		self.assertIn("Payable:window", text)
		self.assertIn("0/3", text)
		self.assertIn("fallback:control_total=1", text)
		self.assertIn("gate: FAIL", text)

	def test_a_passing_company_says_so(self):
		for back in range(2):
			for side in shadow.SIDES:
				for scope in shadow.SCOPES:
					shadow_state.put_record(
						digest(COMPANY_A), frappe.utils.add_days(NIGHT, -back), f"{side}:{scope}", done()
					)
		_, text = self.status(days=2)
		self.assertIn("gate: PASS", text)

	def test_it_names_the_build_and_runs_with_the_mode_off(self):
		self.current = settings(mode="off", shadow_list=(COMPANY_A,))
		result, text = self.status()
		self.assertIn("mode: off", text)
		self.assertEqual(result["mode"], "off")
		self.assertEqual(result["build"], BUILD)

	def test_it_names_a_site_without_a_key_instead_of_failing(self):
		with patch.dict(frappe.conf, {"encryption_key": ""}):
			result, text = self.status()
		self.assertIn("no encryption key", text)
		self.assertEqual(result["companies"], {})

	def test_it_is_reachable_by_bench_execute_with_no_arguments(self):
		import inspect

		self.assertEqual(
			[p for p in inspect.signature(shadow.status).parameters.values() if p.default is p.empty], []
		)


class TestZZNothingLeftBehind(FrappeTestCase):
	"""Scoped to this run's key prefix: another session may run the shadow on the shared site at the time."""

	def test_no_test_left_a_lease_a_night_or_a_mode(self):
		with patch.object(shadow_state, "PREFIX", TEST_PREFIX):
			self.assertEqual(frappe.cache.get_keys(shadow_state.lease_key()), [])
			self.assertEqual(frappe.cache.get_keys(shadow_state.night_key("")), [])
		self.assertEqual(frappe.cache.get_keys(TEST_PREFIX), [])
		self.assertEqual(routing.reader_settings(), _start_settings[-1])
