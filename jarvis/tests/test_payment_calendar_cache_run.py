"""Payment Calendar S4b B4: the refresh job `cache_refresh.run` against the real store.

The read is scripted (`_Chain._build` returns a canned build, or raises); everything else is real: the durable
claim, the publish with its swap, the loop, the release. A fake clock moves only when a scripted build runs.
These tests commit (the store does) and clean every cache row at the end.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_run
"""

import json
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from rq.timeouts import JobTimeoutException

from jarvis.payment_calendar import (
	builtin_reader,
	cache,
	cache_refresh,
	cache_shadow,
	cache_state,
	cache_store,
	read_log,
	routing,
	snapshot,
)
from jarvis.payment_calendar.errors import CalendarSourceUnavailable, ReaderFallback
from jarvis.tests.test_payment_calendar_cache_refresh import Clock, real_statement_timeout
from jarvis.tests.test_payment_calendar_cache_store import (
	GENERATION,
	HEAD,
	SideCase,
	_payload,
	_row,
)

REAL_TAKE_SLOT = cache_state.take_slot
REAL_RELEASE_SLOT = cache_state.release_slot
REAL_RENEW_SLOT = cache_state.renew_slot
CONTROL_LINES = 4_321


def canned(*rows, doctypes=("Sales Invoice", "Customer"), **payload):
	rows = list(rows) or [_row()]
	return cache_refresh.Build(rows, _payload(**payload), cache_store.BuildStats(), frozenset(doctypes))


class RunCase(SideCase):
	"""The company is the one real company of the site; the side is Receivable."""

	step = 100

	def setUp(self):
		super().setUp()
		self.settings = cache_state.CacheSettings("shadow", (self.company,), False)
		self.clock = Clock()
		self.script = []
		self.released = []
		self.digest = read_log.company_digest(self.company)
		self.addCleanup(self.drop_skip_memos)
		self.drop_skip_memos()
		self.events = []
		fixed = {
			"_clock": self.clock,
			"_scheduler_inactive": lambda: False,
			"_tdr_running": lambda: False,
			"_free_disk_bytes": lambda: 10**15,
			"_control": lambda company, side: cache_refresh.Control(
				frozenset({"Sales Invoice"}), CONTROL_LINES
			),
			"query_hook_offenders": lambda doctypes: [],
			"hook_fingerprint": lambda doctypes: "fp",
		}
		for name, value in fixed.items():
			self.start(patch.object(cache_refresh, name, value))
		self.start(patch.object(cache_state, "take_slot", side_effect=lambda token, night: "slot"))
		self.start(patch.object(cache_state, "release_slot", side_effect=lambda *a: self.released.append(a)))
		self.start(patch.object(cache_state, "renew_slot", return_value=True))
		self.start(patch.object(cache_refresh._Chain, "_build", autospec=True, side_effect=self.next_build))
		self.hop = self.start(patch.object(cache, "enqueue_hop", return_value=True))
		self.addCleanup(self.drop_night_marker)

	def start(self, patcher):
		mock = patcher.start()
		self.addCleanup(patcher.stop)
		return mock

	def one_long_worker_and_no_lane(self):
		"""The real slots of a standard bench: one generic worker on `long`, no lane (E8). Own Redis slot keys."""
		prefix = f"jarvis:pcal:test:cache_run:{frappe.generate_hash(length=10)}:slot:"
		self.start(patch.object(cache_state, "SLOT_PREFIX", prefix))
		self.addCleanup(lambda: frappe.cache.execute_command("DEL", cache_state.slot_key(0)))
		self.start(patch.object(cache_state, "lane_workers", return_value=0))
		self.start(patch.object(cache_state, "long_workers", return_value=1))
		self.start(patch.object(cache_state, "take_slot", REAL_TAKE_SLOT))
		self.start(patch.object(cache_state, "release_slot", REAL_RELEASE_SLOT))
		self.start(patch.object(cache_state, "renew_slot", REAL_RENEW_SLOT))

	def slot_is_held(self):
		return bool(frappe.cache.execute_command("EXISTS", cache_state.slot_key(0)))

	def next_build(self, chain):
		self.events.append("build")
		self.clock.now += self.step
		step = self.script.pop(0) if self.script else canned()
		if isinstance(step, BaseException):
			raise step
		step.stats.hook_fingerprint = chain.pre.fingerprint
		return step

	def go(self, **kwargs):
		kwargs.setdefault("opened", True)
		return cache_refresh.run(self.digest, self.side, **kwargs)

	def run_head(self):
		frappe.db.rollback()
		return frappe.db.sql(
			f"select last_opened, last_duration_ms from `tab{HEAD}` where name=%s",
			(cache_store.head_name(self.company, self.side),),
			as_dict=True,
		)[0]

	def generations(self, state=None):
		frappe.db.rollback()
		extra = " and state=%s" if state else ""
		values = (self.company, self.side) + ((state,) if state else ())
		return frappe.db.sql(
			f"select name, state, failure_class, outcome from `tab{GENERATION}` where company=%s and side=%s{extra} order by name",
			values,
			as_dict=True,
		)

	def night_marker(self):
		return frappe.cache.make_key(f"jarvis:pcal:cache:night:{self.digest}:{self.side}:{routing.today()}")

	def drop_skip_memos(self):
		"""A skip of any test remembers itself in Redis for minutes: no test may leave one for the next."""
		for side in cache_store.SIDES:
			frappe.cache.execute_command("DEL", cache_state.cache_key("skip", self.digest, side))

	def drop_night_marker(self):
		frappe.cache.execute_command("DEL", self.night_marker())


class TestPublish(RunCase):
	def test_a_run_publishes_the_generation_and_releases_the_claim_and_the_slot(self):
		result = self.go()
		self.assertEqual((result.outcome, result.iterations), ("swapped", 1))
		head = self.head()
		self.assertEqual(head.current_generation, self.generations("current")[0].name)
		self.assertIsNone(head.claim_token)
		self.assertEqual(head.hop_seq, 1)
		self.assertEqual(len(self.released), 1)
		self.assertEqual(self.released[0][0], "slot")

	def test_the_second_run_of_equal_rows_is_unchanged(self):
		self.go()
		result = self.go()
		self.assertEqual(result.outcome, "unchanged")
		self.assertEqual(len(self.generations("current")), 1)

	def test_the_head_records_the_iteration_duration_from_the_clock(self):
		self.step = 7
		self.go()
		self.assertEqual(self.run_head().last_duration_ms, 7000)

	def test_opened_marks_the_head_and_a_plain_run_does_not(self):
		self.go(opened=False)
		self.assertIsNone(self.run_head().last_opened)
		self.go(opened=True)
		self.assertIsNotNone(self.run_head().last_opened)

	def test_the_run_is_administrator_and_the_user_is_restored(self):
		frappe.set_user("Guest")
		seen = []
		self.script = [canned()]
		with patch.object(cache_store, "publish", wraps=cache_store.publish) as publish:
			publish.side_effect = lambda *a, **k: seen.append(frappe.session.user) or cache_store.Published(
				"swapped", 1, 1, 1, 0, 0, 1, False
			)
			self.go()
		self.assertEqual(seen, ["Administrator"])
		self.assertEqual(frappe.session.user, "Guest")
		frappe.set_user("Administrator")

	def test_the_hook_fingerprint_of_the_preflight_is_recorded(self):
		self.go()
		frappe.db.rollback()
		fingerprint = frappe.db.sql(f"select hook_fingerprint from `tab{GENERATION}` where state='current'")[
			0
		][0]
		self.assertEqual(fingerprint, "fp")

	def test_as_of_is_the_database_clock_read_before_the_view(self):
		stamp = datetime(2026, 11, 24, 8, 0, 0)
		with (
			patch.object(cache_refresh, "_db_now", side_effect=lambda: self.events.append("now") or stamp),
			patch.object(cache_store, "publish", wraps=cache_store.publish) as publish,
			patch.object(
				frappe.utils, "now_datetime", side_effect=AssertionError("the site clock is not as_of")
			),
		):
			self.go()
		self.assertEqual(self.events, ["now", "build"])
		self.assertEqual(publish.call_args.kwargs["as_of"], stamp)

	def test_the_database_clock_is_asked_with_now_and_not_the_workers_time(self):
		with patch.object(frappe.utils, "now_datetime", side_effect=AssertionError("worker clock")):
			stamp = cache_refresh._db_now()
		frappe.db.rollback()
		database = frappe.db.sql("SELECT NOW(6)")[0][0]
		self.assertLess(abs((database - stamp).total_seconds()), 5)

	def test_a_bad_side_is_skipped(self):
		self.assertEqual(cache_refresh.run(self.digest, "Weekly").outcome, "skipped:bad_side")


class TestUnexpectedFailures(RunCase):
	"""An error outside the reader's own failure path is counted, recorded on the generation, and leaves frames."""

	def test_an_error_while_publishing_fails_the_generation_counts_it_and_keeps_a_name_free_trace(self):
		with (
			patch.object(cache_store, "publish", side_effect=RuntimeError(self.company)),
			patch.object(cache, "count_skip") as counted,
		):
			result = self.go()
		self.assertEqual(result.outcome, "failed:error:RuntimeError")
		counted.assert_any_call("failed:error:RuntimeError")
		head = self.head()
		self.assertEqual((head.failures, head.last_failure), (1, "error:RuntimeError"))
		self.assertIsNone(head.claim_token)
		generation = self.generations()[0]
		self.assertEqual((generation.state, generation.failure_class), ("failed", "error:RuntimeError"))
		trace = frappe.db.get_value(GENERATION, generation.name, "failure_trace")
		self.assertTrue(trace.startswith("RuntimeError"))
		self.assertNotIn(self.company, trace)

	def test_three_in_a_row_start_the_memo_and_so_the_repeated_failures_alert(self):
		for _ in range(cache_store.FAILURES_BEFORE_MEMO):
			frappe.db.sql(f"update `tab{HEAD}` set memo_until=NULL")
			frappe.db.commit()
			with patch.object(cache_store, "publish", side_effect=RuntimeError("x")):
				self.go()
		head = self.head()
		self.assertEqual(head.failures, cache_store.FAILURES_BEFORE_MEMO)
		self.assertIsNotNone(head.memo_until)

	def test_an_error_before_the_claim_is_counted_and_logged_with_frames_and_no_name(self):
		logger = MagicMock()
		with (
			patch.object(cache_refresh, "preflight", side_effect=KeyError(self.company)),
			patch.object(cache, "count_skip") as counted,
			patch.object(frappe, "logger", return_value=logger),
		):
			result = self.go()
		self.assertEqual(result.outcome, "failed:error:KeyError")
		counted.assert_any_call("failed:error:KeyError")
		text = str(logger.method_calls)
		self.assertIn("KeyError", text)
		self.assertIn("_admit", text)
		self.assertNotIn(self.company, text)
		self.assertEqual(self.generations(), [])

	def test_a_job_deadline_is_not_an_unexpected_failure(self):
		with patch.object(cache_store, "publish", side_effect=JobTimeoutException()):
			with self.assertRaises(JobTimeoutException):
				self.go()
		self.assertEqual(self.head().failures, 0)

	def test_a_reader_failure_keeps_a_trace_too(self):
		self.script = [ValueError(self.company)]
		self.go()
		generation = self.generations()[0]
		trace = frappe.db.get_value(GENERATION, generation.name, "failure_trace")
		self.assertTrue(trace.startswith("ValueError"))
		self.assertNotIn(self.company, trace)


class TestRebuild(RunCase):
	"""`cache_refresh.rebuild`: the operator's command, whose result `bench execute` can print."""

	def test_it_returns_a_plain_dict_for_a_build(self):
		found = cache_refresh.rebuild(self.company, self.side)
		self.assertEqual(json.loads(json.dumps(found)), found)
		self.assertEqual(found, {"outcome": "swapped", "iterations": 1})

	def test_it_says_why_nothing_ran(self):
		self.settings = cache_state.CacheSettings("off", (self.company,), False)
		self.assertEqual(cache_refresh.rebuild(self.company, self.side)["outcome"], "skipped:off")
		self.settings = cache_state.CacheSettings("shadow", (), False)
		self.assertEqual(cache_refresh.rebuild(self.company, self.side)["outcome"], "skipped:not_listed")
		self.assertEqual(cache_refresh.rebuild(self.company, "Sideways")["outcome"], "skipped:bad_side")

	def test_by_default_a_bench_with_one_long_worker_and_no_lane_skips_for_no_slot(self):
		self.one_long_worker_and_no_lane()
		with patch.object(cache, "in_prewarm_window", return_value=False):
			found = cache_refresh.rebuild(self.company, self.side)
		self.assertEqual(found, {"outcome": "skipped:no_slot", "iterations": 0})
		self.assertEqual(self.generations(), [])

	def test_the_night_slot_flag_swaps_a_generation_by_day_on_a_one_worker_bench(self):
		self.one_long_worker_and_no_lane()
		with patch.object(cache, "in_prewarm_window", return_value=False):
			found = cache_refresh.rebuild(self.company, self.side, use_night_slot=True)
		self.assertEqual(found, {"outcome": "swapped", "iterations": 1})
		self.assertEqual([row.state for row in self.generations()], ["current"])
		self.assertFalse(self.slot_is_held())
		self.assertEqual(json.loads(json.dumps(found)), found)

	def test_the_night_slot_flag_takes_the_night_slot_and_runs_the_night_build(self):
		with (
			patch.object(cache, "in_prewarm_window", return_value=False),
			patch.object(
				cache_state, "take_slot", side_effect=lambda token, night: "slot" if night else None
			) as take,
		):
			found = cache_refresh.rebuild(self.company, self.side, use_night_slot=True)
		self.assertEqual(found["outcome"], "swapped")
		self.assertEqual(take.call_args.kwargs, {"night": True})

	def test_the_night_slot_flag_still_needs_the_switch_and_the_listing(self):
		self.settings = cache_state.CacheSettings("off", (self.company,), False)
		self.assertEqual(
			cache_refresh.rebuild(self.company, self.side, use_night_slot=True)["outcome"], "skipped:off"
		)
		self.settings = cache_state.CacheSettings("shadow", (), False)
		self.assertEqual(
			cache_refresh.rebuild(self.company, self.side, use_night_slot=True)["outcome"],
			"skipped:not_listed",
		)

	def test_the_night_slot_flag_still_waits_for_a_free_night_slot(self):
		self.one_long_worker_and_no_lane()
		held = cache_state.take_slot("other", night=True)
		self.assertIsNotNone(held)
		found = cache_refresh.rebuild(self.company, self.side, use_night_slot=True)
		self.assertEqual(found["outcome"], "skipped:no_slot")
		self.assertTrue(self.slot_is_held())  # the other holder's slot is not released by the skipped run

	def test_the_night_slot_flag_neither_reads_nor_takes_the_night_marker(self):
		"""An operator build by day is not a pre-warm unit: tonight's unit and a second operator build still run
		(the same canned rows make the later builds `unchanged`, never `skipped:night_done`)."""
		with patch.object(cache, "in_prewarm_window", return_value=False):
			self.assertEqual(
				cache_refresh.rebuild(self.company, self.side, use_night_slot=True)["outcome"], "swapped"
			)
			self.assertFalse(frappe.cache.execute_command("EXISTS", self.night_marker()))
			self.assertEqual(
				cache_refresh.rebuild(self.company, self.side, use_night_slot=True)["outcome"], "unchanged"
			)
			self.assertTrue(cache.take_night_marker(self.digest, self.side))
			self.assertEqual(
				cache_refresh.rebuild(self.company, self.side, use_night_slot=True)["outcome"], "unchanged"
			)

	def test_the_night_flag_alone_still_keeps_the_window(self):
		with patch.object(cache, "in_prewarm_window", return_value=False):
			found = cache_refresh.rebuild(self.company, self.side, night=True)
		self.assertEqual(found["outcome"], "skipped:outside_window")

	def test_the_bench_command_form_runs(self):
		# `bench execute` calls the function with keyword arguments from `--kwargs` and prints a JSON result.
		found = frappe.call(cache_refresh.rebuild, company=self.company, side=self.side)
		json.dumps(found)


class TestSkips(RunCase):
	def skipped(self, reason, **kwargs):
		result = self.go(**kwargs)
		self.assertEqual(result.outcome, f"skipped:{reason}")
		self.assertEqual(self.generations(), [])
		return result

	def test_off_skips(self):
		self.settings = cache_state.CacheSettings("off", (self.company,), False)
		self.skipped("off")

	def test_an_unlisted_company_skips(self):
		self.settings = cache_state.CacheSettings("shadow", (), False)
		self.skipped("not_listed")

	def test_an_unknown_digest_skips(self):
		self.assertEqual(cache_refresh.run("0" * 16, self.side).outcome, "skipped:not_listed")

	def test_no_free_slot_skips_before_any_claim(self):
		with patch.object(cache_state, "take_slot", return_value=None):
			self.skipped("no_slot")
		frappe.db.rollback()
		self.assertEqual(frappe.db.sql(f"select count(*) from `tab{HEAD}`")[0][0], 0)

	def test_a_preflight_skip_makes_no_generation_and_frees_the_slot(self):
		with patch.object(cache_refresh, "_tdr_running", lambda: True):
			self.skipped("deletion_running")
		self.assertEqual(len(self.released), 1)

	def test_a_running_deletion_is_no_failure_and_sets_no_memo(self):
		with patch.object(cache_refresh, "_tdr_running", lambda: True):
			self.skipped("deletion_running")
			self.skipped("deletion_running")
		frappe.db.rollback()
		self.assertEqual(frappe.db.sql(f"select count(*) from `tab{HEAD}`")[0][0], 0)

	def test_a_hooked_query_makes_no_generation(self):
		with patch.object(cache_refresh, "query_hook_offenders", lambda doctypes: ["Expense Claim"]):
			self.skipped("not_buildable:query_hook")

	def test_a_memo_skips_after_a_too_large_failure(self):
		self.script = [ReaderFallback("deadline")]
		self.go()
		self.assertEqual(self.go().outcome, "skipped:memo")

	def test_a_live_claim_skips(self):
		held = self.claim()
		self.assertEqual(held.side, self.side)
		result = self.go()
		self.assertEqual(result.outcome, "skipped:busy")
		self.assertEqual(self.head().claim_token, held.token)

	def test_a_skip_is_counted_by_reason_without_a_name(self):
		self.settings = cache_state.CacheSettings("shadow", (), False)
		with patch.object(cache, "count_skip") as counted:
			self.go()
		counted.assert_called_once_with("not_listed")

	def test_a_site_without_an_encryption_key_skips_by_class(self):
		with patch.object(cache_refresh, "company_for", return_value=None):
			self.skipped("not_listed")


class TestSkipMemo(RunCase):
	"""A job that skipped for a lasting reason that sets no marker or memo says so: the tick and the requests wait."""

	def memo(self):
		return cache_state.cache_key("skip", self.digest, self.side)

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.cache.execute_command, "DEL", self.memo())
		self.queue = self.start(patch.object(frappe, "enqueue", return_value=object()))
		self.start(patch.object(cache_state, "lane_live", return_value=True))
		cache.reset_lane_memo()

	REASONS = {
		"no_disk": ("_free_disk_bytes", 0),
		"deletion_running": ("_tdr_running", True),
		"scheduler_inactive": ("_scheduler_inactive", True),
	}

	def test_each_lasting_skip_sets_a_memo_for_its_own_time_and_the_next_enqueue_is_refused(self):
		for reason, (name, value) in self.REASONS.items():
			with self.subTest(reason):
				frappe.cache.execute_command("DEL", self.memo())
				with patch.object(cache_refresh, name, lambda *a, value=value: value):
					self.assertEqual(self.go().outcome, f"skipped:{reason}")
				ttl = frappe.cache.execute_command("TTL", self.memo())
				self.assertTrue(0 < ttl <= cache.SKIP_MEMO_S[reason], (reason, ttl))
				self.queue.reset_mock()
				self.assertEqual(cache.enqueue_side(self.company, self.side), "busy")
				self.queue.assert_not_called()

	def test_no_slot_sets_a_short_memo(self):
		with patch.object(cache_state, "take_slot", return_value=None):
			self.assertEqual(self.go().outcome, "skipped:no_slot")
		ttl = frappe.cache.execute_command("TTL", self.memo())
		self.assertTrue(0 < ttl <= cache.SKIP_MEMO_S["no_slot"])
		self.assertLess(cache.SKIP_MEMO_S["no_slot"], cache.SKIP_MEMO_S["no_disk"])

	def test_an_unsupported_major_sets_a_memo(self):
		with patch.object(cache_refresh, "source_profile", return_value=SimpleNamespace(major="15")):
			self.assertEqual(self.go().outcome, "skipped:unsupported_major")
		self.assertGreater(frappe.cache.execute_command("TTL", self.memo()), 0)

	def test_a_run_that_builds_sets_no_memo(self):
		self.assertEqual(self.go().outcome, "swapped")
		self.assertEqual(frappe.cache.execute_command("EXISTS", self.memo()), 0)

	def test_a_skip_that_has_its_own_guard_sets_no_memo(self):
		self.settings = cache_state.CacheSettings("off", (self.company,), False)
		self.assertEqual(self.go().outcome, "skipped:off")
		self.assertEqual(frappe.cache.execute_command("EXISTS", self.memo()), 0)


class TestPreflightAggregate(RunCase):
	def test_a_statement_timeout_in_the_control_aggregate_is_recorded_as_too_large(self):
		with patch.object(cache_refresh, "_control", side_effect=real_statement_timeout()):
			result = self.go()
		self.assertEqual(result.outcome, "skipped:history_too_large")
		head = self.head()
		self.assertEqual((head.last_failure, head.backoff_level), ("history_too_large", 1))
		self.assertIsNotNone(head.memo_until)
		self.assertIsNone(head.claim_token)
		self.assertEqual([row.state for row in self.generations()], ["failed"])
		self.assertEqual(len(self.released), 1)

	def test_a_second_statement_timeout_walks_the_ladder_and_each_is_counted(self):
		with (
			patch.object(cache_refresh, "_control", side_effect=real_statement_timeout()),
			patch.object(cache, "count_skip") as counted,
		):
			self.go()
			frappe.db.sql(f"update `tab{HEAD}` set memo_until=NULL")
			frappe.db.commit()
			self.go()
		self.assertEqual(self.head().backoff_level, 2)
		self.assertEqual([call.args for call in counted.call_args_list], [("history_too_large",)] * 2)

	def test_a_lock_wait_in_the_aggregate_is_not_a_too_large_history(self):
		with patch.object(cache_refresh, "_control", side_effect=frappe.QueryTimeoutError()):
			result = self.go()
		self.assertEqual(result.outcome, "failed:error:QueryTimeoutError")
		self.assertEqual(self.generations(), [])
		self.assertEqual(frappe.db.sql(f"select count(*) from `tab{HEAD}` where backoff_level > 0")[0][0], 0)

	def test_the_slot_is_renewed_around_the_aggregate(self):
		order = []
		with (
			patch.object(
				cache_state, "renew_slot", side_effect=lambda *a, **k: order.append("renew") or True
			),
			patch.object(
				cache_refresh,
				"_control",
				side_effect=lambda *a: order.append("control") or cache_refresh.Control(),
			),
		):
			self.go()
		self.assertEqual(order[:3], ["renew", "control", "renew"])

	def test_a_slot_lost_during_the_aggregate_skips(self):
		with patch.object(cache_state, "renew_slot", return_value=False):
			result = self.go()
		self.assertEqual(result.outcome, "skipped:lease_lost")
		self.assertEqual(self.generations(), [])


class TestReaderFailures(RunCase):
	def failed(self, error, klass, *, failures=1, level=0, recorded=True):
		self.script = [error]
		result = self.go()
		self.assertEqual(result.outcome, f"failed:{klass}")
		head = self.head()
		self.assertEqual((head.failures, head.backoff_level), (failures, level))
		self.assertEqual(head.last_failure, klass if recorded else None)
		self.assertIsNone(head.claim_token)
		self.assertEqual(self.generations()[0].failure_class, klass)
		self.assertEqual(self.generations()[0].state, "failed")
		return head

	def test_a_deadline_is_history_too_large_with_a_backoff(self):
		head = self.failed(ReaderFallback("deadline"), "history_too_large", failures=0, level=1)
		self.assertIsNotNone(head.memo_until)

	def test_too_many_lines_is_history_too_large(self):
		self.failed(ReaderFallback("too_many_lines"), "history_too_large", failures=0, level=1)

	def test_another_fallback_keeps_its_reason(self):
		self.failed(ReaderFallback("pin_drift"), "reader_fallback:pin_drift")

	def test_a_refusal_keeps_its_code(self):
		self.failed(CalendarSourceUnavailable("too_large"), "refused:too_large")

	def test_an_unexpected_error_is_its_class_only(self):
		self.failed(ValueError(self.company), "error:ValueError")

	def test_a_lost_slot_counts_nothing(self):
		self.failed(cache_refresh.LeaseLost(), "lease_lost", failures=0, recorded=False)

	def test_a_statement_timeout_is_history_too_large(self):
		self.failed(real_statement_timeout(), "history_too_large", failures=0, level=1)

	def test_a_lock_wait_is_one_plain_failure_and_no_backoff(self):
		self.failed(frappe.QueryTimeoutError(), "error:QueryTimeoutError")

	def test_a_memory_error_reopens_the_connection_then_records_the_failure(self):
		order = []
		self.script = [MemoryError()]
		real = cache_store.fail
		with (
			patch.object(cache_refresh, "_reopen_connection", side_effect=lambda: order.append("reopen")),
			patch.object(cache_store, "fail", side_effect=lambda *a: order.append("fail") or real(*a)),
		):
			result = self.go()
		self.assertEqual(result.outcome, "failed:history_too_large")
		self.assertEqual(order, ["reopen", "fail"])
		self.assertEqual(self.head().backoff_level, 1)

	def test_an_error_while_recording_a_failure_does_not_escape_and_the_claim_is_still_released(self):
		self.script = [ValueError("x")]
		with patch.object(cache_store, "fail", side_effect=RuntimeError("db gone")):
			result = self.go()
		self.assertEqual(result.outcome, "failed:error:ValueError")
		self.assertIsNone(self.head().claim_token)

	def test_a_failure_leaves_no_name_in_the_log_or_the_row(self):
		self.script = [ValueError(self.company)]
		logger = MagicMock()
		with patch.object(frappe, "logger", return_value=logger):
			self.go()
		text = " ".join(str(call) for call in logger.method_calls)
		self.assertNotIn(self.company, text)
		self.assertIn("error:ValueError", text)
		self.assertNotIn(self.company, str(self.generations()))

	def test_a_job_deadline_is_not_swallowed_and_the_claim_and_slot_are_released(self):
		self.script = [JobTimeoutException()]
		with self.assertRaises(JobTimeoutException):
			self.go()
		self.assertIsNone(self.head().claim_token)
		self.assertEqual(len(self.released), 1)
		self.assertEqual(self.generations()[0].state, "building")

	def test_the_next_claim_works_at_once_after_a_job_deadline(self):
		self.script = [JobTimeoutException()]
		with self.assertRaises(JobTimeoutException):
			self.go()
		self.assertEqual(self.go().outcome, "swapped")


class TestStoreFailures(RunCase):
	def test_a_fence_ends_the_run_and_the_chain_never_records_a_failure(self):
		with (
			patch.object(cache_store, "publish", side_effect=cache_store.Fenced("epoch")),
			patch.object(cache_refresh._Chain, "_record") as record,
		):
			result = self.go()
		self.assertEqual(result.outcome, "fenced")
		record.assert_not_called()
		self.assertIsNone(self.head().claim_token)

	def test_a_slow_write_is_its_own_class_and_is_recorded_once_by_the_store(self):
		with (
			patch.object(cache_store, "publish", side_effect=cache_store.WriteDeadline("write_deadline")),
			patch.object(cache_refresh._Chain, "_record") as record,
		):
			self.assertEqual(self.go().outcome, "failed:write_deadline")
		record.assert_not_called()

	def test_a_readback_failure_is_reported_by_class(self):
		with patch.object(cache_store, "publish", side_effect=cache_store.ReadbackFailed("checksum")):
			self.assertEqual(self.go().outcome, "failed:readback")

	def test_hooks_that_changed_during_the_view_stop_the_swap(self):
		self.go()
		before = self.head().current_generation
		fingerprints = iter(["fp", "changed"])
		self.script = [canned(_row(voucher_no="SI-2"))]
		with patch.object(cache_refresh, "hook_fingerprint", lambda doctypes: next(fingerprints)):
			result = self.go()
		self.assertEqual(result.outcome, "failed:not_buildable:query_hook")
		self.assertEqual(self.head().current_generation, before)
		self.assertEqual(self.generations("failed")[0].failure_class, "not_buildable:query_hook")

	def test_a_doctype_that_preflight_did_not_check_stops_the_swap(self):
		self.script = [canned(doctypes=("Sales Invoice", "Some Other DocType"))]
		result = self.go()
		self.assertEqual(result.outcome, "failed:not_buildable:query_hook")
		self.assertIsNone(self.head().current_generation)

	def test_a_preflight_that_found_no_hook_swaps_when_the_doctypes_are_checked(self):
		self.script = [canned(doctypes=("Sales Invoice", "Payment Entry"))]
		self.assertEqual(self.go().outcome, "swapped")


class TestChain(RunCase):
	"""A continuous chain: full cadence, an opened company, iterations longer than the loop threshold allows."""

	step = 300

	def setUp(self):
		super().setUp()
		self.settings = cache_state.CacheSettings("shadow", (self.company,), True)

	def test_the_job_loops_while_it_holds_the_claim_and_hands_over_when_its_time_is_spent(self):
		result = self.go()
		# 1,200 s timeout: after 300 s and 600 s at least 480 s remain, after 900 s only 300 s.
		self.assertEqual((result.outcome, result.iterations), ("unchanged", 3))
		head = self.head()
		self.assertIsNone(head.claim_token)
		self.assertGreaterEqual(head.hop_seq, 1)
		self.hop.assert_called_once_with(self.company, self.side, head.hop_seq)

	def test_the_claim_is_kept_between_iterations(self):
		claims = []
		real = cache_store.next_generation

		def spy(held, **kwargs):
			claims.append(self.head().claim_token)
			return real(held, **kwargs)

		with patch.object(cache_store, "next_generation", side_effect=spy):
			self.go()
		self.assertEqual(len(claims), 2)
		self.assertTrue(all(claims))

	def test_a_loop_makes_one_generation_row_per_iteration_and_a_skip_keeps_the_current(self):
		self.go()
		states = [row.state for row in self.generations()]
		self.assertEqual(states.count("current"), 1)
		self.assertEqual(len(states), 3)

	def test_the_hop_follows_skip_only_iterations_with_a_new_hop_seq(self):
		self.go()
		first = self.head().hop_seq
		self.go()
		self.assertGreater(self.head().hop_seq, first)
		self.assertEqual(self.hop.call_count, 2)
		self.assertNotEqual(self.hop.call_args_list[0].args[2], self.hop.call_args_list[1].args[2])

	def test_every_pass_of_a_chain_writes_the_ledger_lines_of_its_preflight(self):
		self.go()
		frappe.db.rollback()
		stored = frappe.db.sql(
			f"select ledger_lines from `tab{GENERATION}` where company=%s and side=%s",
			(self.company, self.side),
		)
		self.assertEqual(stored, ((CONTROL_LINES,),) * 3)

	def test_a_company_not_opened_does_not_loop_and_is_not_handed_over(self):
		result = self.go(opened=False)
		self.assertEqual(result.iterations, 1)
		self.assertIsNone(self.head().claim_token)
		self.hop.assert_not_called()

	def test_a_short_iteration_is_not_a_continuous_chain(self):
		self.step = 20
		result = self.go()
		self.assertEqual(result.iterations, 1)
		self.hop.assert_not_called()

	def test_without_full_cadence_there_is_one_iteration_and_no_hop(self):
		self.settings = cache_state.CacheSettings("shadow", (self.company,), False)
		result = self.go()
		self.assertEqual(result.iterations, 1)
		self.hop.assert_not_called()

	def test_a_switch_turned_off_between_iterations_stops_the_chain_before_another_read(self):
		real = cache.heads_of
		off = cache_state.CacheSettings("off", (self.company,), True)

		def flip(*args):
			self.settings = off
			return real(*args)

		with patch.object(cache, "heads_of", side_effect=flip):
			result = self.go()
		self.assertEqual((result.outcome, result.iterations), ("skipped:off", 1))
		self.assertEqual(len(self.generations()), 1)
		self.assertIsNone(self.head().claim_token)

	def test_a_company_removed_from_the_list_between_iterations_stops_the_chain(self):
		real = cache.heads_of

		def flip(*args):
			self.settings = cache_state.CacheSettings("shadow", (), True)
			return real(*args)

		with patch.object(cache, "heads_of", side_effect=flip):
			result = self.go()
		self.assertEqual((result.outcome, result.iterations), ("skipped:not_listed", 1))

	def test_a_failed_iteration_ends_the_chain_without_a_hop(self):
		self.script = [canned(), ReaderFallback("pin_drift")]
		result = self.go()
		self.assertEqual(result.outcome, "failed:reader_fallback:pin_drift")
		self.assertEqual(result.iterations, 2)
		self.hop.assert_not_called()
		self.assertIsNone(self.head().claim_token)

	def test_a_preflight_that_turns_bad_between_iterations_ends_the_chain(self):
		answers = iter([[], ["Expense Claim"]])
		with patch.object(cache_refresh, "query_hook_offenders", lambda doctypes: next(answers)):
			result = self.go()
		self.assertEqual(result.outcome, "skipped:not_buildable:query_hook")
		self.assertEqual(result.iterations, 1)

	def test_a_purge_between_iterations_fences_the_chain(self):
		real = cache_store.next_generation

		def purged(held, **kwargs):
			cache_store.bump_epoch(self.company)
			frappe.db.commit()
			return real(held, **kwargs)

		with patch.object(cache_store, "next_generation", side_effect=purged):
			result = self.go()
		self.assertEqual(result.outcome, "fenced")
		self.hop.assert_not_called()

	def test_a_tick_queued_job_cannot_take_the_side_between_iterations(self):
		seen = []
		real = cache_store.next_generation

		def other_job(held, **kwargs):
			try:
				cache_store.claim(
					self.company, self.side, anchor=held.anchor, cache_build=held.cache_build, night=False
				)
			except cache_store.NotClaimed as exc:
				seen.append(exc.reason)
			return real(held, **kwargs)

		with patch.object(cache_store, "next_generation", side_effect=other_job):
			self.go()
		self.assertEqual(seen, ["busy", "busy"])


class TestNight(RunCase):
	def setUp(self):
		super().setUp()
		self.start(patch.object(cache, "in_prewarm_window", return_value=True))

	def test_outside_the_window_the_unit_skips(self):
		with patch.object(cache, "in_prewarm_window", return_value=False):
			result = self.go(night=True)
		self.assertEqual(result.outcome, "skipped:outside_window")
		self.assertEqual(self.generations(), [])

	def test_the_unit_runs_once_and_uses_the_night_slots_and_claim(self):
		with patch.object(
			cache_state, "take_slot", side_effect=lambda token, night: "slot" if night else None
		) as take:
			self.assertEqual(self.go(night=True).outcome, "swapped")
		self.assertEqual(take.call_args.kwargs, {"night": True})

	def test_a_second_unit_of_the_same_night_finds_the_marker_and_skips_before_any_claim(self):
		self.go(night=True)
		before = len(self.generations())
		result = self.go(night=True)
		self.assertEqual(result.outcome, "skipped:night_done")
		self.assertIsNone(self.head().claim_token)
		self.assertEqual(len(self.generations()), before)

	def test_a_unit_that_loses_the_race_for_the_marker_closes_its_generation_at_once(self):
		with (
			patch.object(cache, "night_marker_set", return_value=False),
			patch.object(cache, "take_night_marker", return_value=False),
		):
			result = self.go(night=True)
		self.assertEqual(result.outcome, "skipped:night_done")
		self.assertEqual([row.state for row in self.generations()], ["failed"])
		self.assertEqual(self.head().failures, 0)
		self.assertIsNone(self.head().claim_token)

	def test_a_night_unit_never_loops_even_on_full_cadence(self):
		self.settings = cache_state.CacheSettings("shadow", (self.company,), True)
		self.step = 300
		result = self.go(night=True)
		self.assertEqual(result.iterations, 1)
		self.hop.assert_not_called()

	def test_a_unit_that_is_refused_a_slot_leaves_the_marker_for_a_later_try(self):
		with patch.object(cache_state, "take_slot", return_value=None):
			self.assertEqual(self.go(night=True).outcome, "skipped:no_slot")
		self.assertEqual(self.go(night=True).outcome, "swapped")


class TestNightOnOneLongWorker(RunCase):
	"""E8-1: a standard bench (one generic worker on `long`, no lane) builds a generation in the night window."""

	def setUp(self):
		super().setUp()
		self.start(patch.object(cache, "in_prewarm_window", return_value=True))
		self.one_long_worker_and_no_lane()

	def test_a_night_unit_is_admitted_and_swaps_a_generation_and_frees_the_slot(self):
		result = self.go(night=True)
		self.assertEqual((result.outcome, result.iterations), ("swapped", 1))
		self.assertEqual([row.state for row in self.generations()], ["current"])
		self.assertFalse(self.slot_is_held())

	def test_a_day_unit_on_the_same_bench_finds_no_slot(self):
		self.assertEqual(self.go(night=False).outcome, "skipped:no_slot")
		self.assertEqual(self.generations(), [])

	def test_a_second_unit_waits_while_the_only_night_slot_is_held(self):
		self.assertIsNotNone(cache_state.take_slot("other", night=True))
		self.assertEqual(self.go(night=True).outcome, "skipped:no_slot")


class TestLedgerLinesWritten(RunCase):
	"""E10-3: every build writes the ledger lines its preflight counted (Bank: the GL rows its read counted)."""

	def stored(self):
		frappe.db.rollback()
		return [
			row[0]
			for row in frappe.db.sql(
				f"select ledger_lines from `tab{GENERATION}` where company=%s and side=%s and state='current'",
				(self.company, self.side),
			)
		]

	def test_a_day_build_writes_the_control_pair_line_total(self):
		self.assertEqual(self.go().outcome, "swapped")
		self.assertEqual(self.stored(), [CONTROL_LINES])

	def test_a_night_build_writes_the_control_pair_line_total(self):
		with patch.object(cache, "in_prewarm_window", return_value=True):
			self.assertEqual(self.go(night=True).outcome, "swapped")
		self.assertEqual(self.stored(), [CONTROL_LINES])

	def test_an_operator_build_writes_the_control_pair_line_total(self):
		self.assertEqual(self.go(night=True, any_time=True).outcome, "swapped")
		self.assertEqual(self.stored(), [CONTROL_LINES])

	def test_the_operator_rebuild_writes_the_control_pair_line_total(self):
		with patch.object(cache, "in_prewarm_window", return_value=False):
			cache_refresh.rebuild(self.company, self.side, use_night_slot=True)
		self.assertEqual(self.stored(), [CONTROL_LINES])

	def test_the_bank_side_keeps_the_gl_rows_its_read_counted(self):
		self.side = "Bank"
		build = canned(doctypes=("Account", "GL Entry"))
		build.stats.ledger_lines = 20_416
		self.script = [build]
		self.assertEqual(self.go().outcome, "swapped")
		self.assertEqual(self.stored(), [20_416])


class TestOracle(RunCase):
	def with_oracle(self, record=None):
		build = canned()
		build.oracle = {"outcome": "compared"} if record is None else record
		return build

	def shadow(self, generation):
		frappe.db.rollback()
		return frappe.db.sql(f"select shadow from `tab{GENERATION}` where name=%s", (generation,))[0][0]

	def test_the_oracle_result_of_a_build_is_the_result_of_the_run(self):
		self.script = [self.with_oracle()]
		self.assertEqual(self.go().oracle, {"outcome": "compared"})

	def test_the_record_is_stored_on_the_swapped_generation_and_committed(self):
		self.script = [self.with_oracle()]
		self.go()
		current = self.generations("current")[0].name
		self.assertEqual(json.loads(self.shadow(current)), {"outcome": "compared"})

	def test_an_operator_build_stores_a_record_marked_as_operator(self):
		self.script = [self.with_oracle()]
		result = self.go(night=True, any_time=True)
		marked = {"outcome": "compared", "source": "operator"}
		self.assertEqual(result.oracle, marked)
		self.assertEqual(json.loads(self.shadow(self.generations("current")[0].name)), marked)

	def test_the_operator_rebuild_with_the_night_slot_stores_a_marked_record(self):
		self.script = [self.with_oracle()]
		with patch.object(cache, "in_prewarm_window", return_value=False):
			cache_refresh.rebuild(self.company, self.side, use_night_slot=True)
		stored = json.loads(self.shadow(self.generations("current")[0].name))
		self.assertEqual(stored["source"], "operator")

	def test_a_night_unit_stores_its_record_unmarked(self):
		self.script = [self.with_oracle()]
		with patch.object(cache, "in_prewarm_window", return_value=True):
			result = self.go(night=True)
		self.assertEqual(result.oracle, {"outcome": "compared"})
		self.assertEqual(
			json.loads(self.shadow(self.generations("current")[0].name)), {"outcome": "compared"}
		)

	def test_a_skip_stores_the_record_on_the_generation_that_stays_current(self):
		self.go()
		kept = self.generations("current")[0].name
		self.script = [self.with_oracle()]
		self.assertEqual(self.go().outcome, "unchanged")
		self.assertEqual(json.loads(self.shadow(kept)), {"outcome": "compared"})

	def test_persist_runs_after_the_swap_and_not_without_a_record(self):
		with patch.object(cache_shadow, "persist", return_value=True) as persist:
			self.script = [canned()]
			self.go()
			persist.assert_not_called()
			self.script = [self.with_oracle()]
			self.go()
		persist.assert_called_once()
		self.assertEqual(persist.call_args.args[1], {"outcome": "compared"})

	def test_a_generation_that_is_gone_is_logged_by_class_and_the_run_still_swaps(self):
		self.script = [self.with_oracle()]
		logger = MagicMock()
		with (
			patch.object(cache_shadow, "persist", return_value=False),
			patch.object(frappe, "logger", return_value=logger),
		):
			result = self.go()
		self.assertEqual(result.outcome, "swapped")
		self.assertIn("persist_generation_gone", str(logger.method_calls))

	def test_a_persist_error_is_logged_by_class_and_never_fails_the_refresh(self):
		self.script = [self.with_oracle()]
		logger = MagicMock()
		with (
			patch.object(cache_shadow, "persist", side_effect=ValueError(self.company)),
			patch.object(frappe, "logger", return_value=logger),
		):
			result = self.go()
		self.assertEqual(result.outcome, "swapped")
		self.assertIn("persist:ValueError", str(logger.method_calls))
		self.assertNotIn(self.company, str(logger.method_calls))
		self.assertIsNone(self.head().claim_token)

	def test_a_job_deadline_in_persist_passes_through(self):
		self.script = [self.with_oracle()]
		with patch.object(cache_shadow, "persist", side_effect=JobTimeoutException()):
			with self.assertRaises(JobTimeoutException):
				self.go()
		self.assertIsNone(self.head().claim_token)


class TestExcludedCount(RunCase):
	def test_the_stored_fx_rows_are_counted_as_excluded(self):
		fx = _row(kind="settled_fx", voucher_no="SI-9", remaining=Decimal("0.40"))
		self.script = [canned(_row(), fx)]
		self.go()
		frappe.db.rollback()
		counts = frappe.db.sql(
			f"select row_count, excluded_count from `tab{GENERATION}` where state='current'"
		)
		self.assertEqual(counts, ((2, 1),))

	def test_the_store_uses_the_readers_word_for_an_fx_row(self):
		self.assertEqual(cache_store.KIND_EXCLUDED, "settled_fx")


class TestBuildStep(TestCase):
	"""`_Chain._build` and `build_generation` themselves, with the reads replaced."""

	def chain(self, night):
		chain = cache_refresh._Chain("d" * 16, "Receivable", night, False)
		chain.company, chain.slot = "Acme", "slot"
		chain.held = SimpleNamespace(anchor=date(2026, 11, 24))
		chain.pre = cache_refresh.Preflight(None, "fp-pre", frozenset({"Sales Invoice"}))
		return chain

	def test_a_night_build_gets_the_oracle_and_a_day_build_gets_none(self):
		with patch.object(cache_refresh, "build_generation", return_value=canned()) as build:
			self.chain(True)._build()
			self.assertIs(build.call_args.kwargs["oracle"], cache_shadow.night_oracle)
			self.assertTrue(build.call_args.kwargs["night"])
			self.chain(False)._build()
			self.assertIsNone(build.call_args.kwargs["oracle"])

	def test_the_build_records_the_fingerprint_of_the_preflight(self):
		with patch.object(cache_refresh, "build_generation", return_value=canned()):
			self.assertEqual(self.chain(False)._build().stats.hook_fingerprint, "fp-pre")

	def test_the_view_runs_under_the_address_cap_the_statement_cap_and_the_raised_caps(self):
		events = []

		@contextmanager
		def step(name, **extra):
			events.append(name + "_on")
			yield extra.get("value")
			events.append(name + "_off")

		def read(company, side, anchor, hits, clock):
			events.append(("read", builtin_reader.TOTAL_LINE_CAP == cache_refresh.REFRESH_TOTAL_LINE_CAP))
			return canned()

		clock = Clock()
		with (
			patch.object(cache_refresh, "_clock", clock),
			patch.object(cache_refresh, "address_space_cap", lambda mb: step("cap")),
			patch.object(frappe.db, "execution_timeout", lambda seconds: step("timeout", value=seconds)),
			patch.object(snapshot, "consistent_read", lambda: step("view", value=set())),
			patch.object(cache_refresh, "_read_side", side_effect=read),
		):
			build = cache_refresh.build_generation(
				"Acme", "Receivable", date(2026, 11, 24), night=False, slot=None, token="t", oracle=None
			)
		self.assertEqual(
			events,
			["cap_on", "timeout_on", "view_on", ("read", True), "view_off", "timeout_off", "cap_off"],
		)
		self.assertGreaterEqual(build.stats.peak_rss_mb, 1)

	def test_the_oracle_runs_inside_the_view_on_the_built_rows(self):
		order = []

		@contextmanager
		def view():
			order.append("on")
			yield set()
			order.append("off")

		def oracle(company, side, anchor, rows, payload):
			order.append("oracle")
			return {"rows": len(rows)}

		with (
			patch.object(snapshot, "consistent_read", view),
			patch.object(cache_refresh, "_read_side", return_value=canned()),
		):
			build = cache_refresh.build_generation(
				"Acme", "Receivable", date(2026, 11, 24), night=True, slot=None, token="t", oracle=oracle
			)
		self.assertEqual(order, ["on", "oracle", "off"])
		self.assertEqual(build.oracle, {"rows": 1})

	def test_the_night_view_cap_is_longer_than_the_day_cap(self):
		caps = []
		with (
			patch.object(snapshot, "consistent_read", lambda: _null()),
			patch.object(cache_refresh, "_read_bank", side_effect=lambda *a: canned()),
			patch.object(
				cache_refresh.ViewClock,
				"__init__",
				autospec=True,
				side_effect=lambda self, cap, *a: caps.append(cap)
				or setattr(self, "check", lambda: None)
				or setattr(self, "deadline", lambda: 0),
			),
		):
			for night in (False, True):
				cache_refresh.build_generation(
					"Acme", "Bank", date(2026, 11, 24), night=night, slot=None, token="t"
				)
		self.assertEqual(caps, [cache_state.VIEW_CAP_DAY_S, cache_state.VIEW_CAP_NIGHT_S])


@contextmanager
def _null():
	yield set()
