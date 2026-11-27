"""Payment Calendar S4b B4: `cache` (note_opened, tick, enqueue, chains, pre-warm) and the one call in `jobs`.

Every Redis key and every row a test makes is its own and is removed again. `frappe.enqueue` is always a spy:
no job is queued on the site.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_tick
"""

import heapq
import time
from collections import deque
from datetime import datetime, timedelta
from itertools import count
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from rq.timeouts import JobTimeoutException

from jarvis.payment_calendar import cache, cache_alerts, cache_state, cache_store, jobs, read_log, routing
from jarvis.tests.test_payment_calendar_cache_store import GENERATION, HEAD, SideCase, _sql


def head(**fields):
	base = {
		"company": "Acme",
		"side": "Receivable",
		"hop_seq": 0,
		"current_generation": 5,
		"opened_age_s": 60,
		"claim_live": False,
		"memo_live": False,
		"last_failure": None,
		"duration_ms": 100_000,
		"age_s": 600,
	}
	return cache.Head(**{**base, **fields})


# The real reader of the switch, taken before any test patches it (a test that needs a real read of a faulty source).
REAL_SETTINGS = cache_state.cache_settings
REAL_BENCH_SLOTS = cache_state.bench_slots

FULL = cache_state.CacheSettings("shadow", ("Acme",), True)
SAMPLE = cache_state.CacheSettings("shadow", ("Acme",), False)


class TestStartAge(TestCase):
	def test_without_full_cadence_the_refresh_is_an_hourly_sample(self):
		self.assertEqual(cache.start_age_s(100_000, full_cadence=False), 3600)
		self.assertFalse(cache.continuous(100_000, full_cadence=False))

	def test_an_unknown_duration_is_the_longest_honest_iteration_so_the_chain_is_continuous(self):
		self.assertEqual(cache.start_age_s(None, full_cadence=True), 0)
		self.assertTrue(cache.continuous(None, full_cadence=True))

	def test_the_start_age_leaves_two_durations_and_a_margin_before_fourteen_minutes(self):
		self.assertEqual(cache.start_age_s(100_000, full_cadence=True), 840 - 200 - 300)
		self.assertEqual(cache.start_age_s(10_000, full_cadence=True), 840 - 20 - 300)

	def test_a_duration_of_270_seconds_or_more_is_continuous(self):
		self.assertEqual(cache.start_age_s(269_000, full_cadence=True), 2)
		self.assertEqual(cache.start_age_s(270_000, full_cadence=True), 0)

	def test_a_duration_far_above_the_bound_is_continuous(self):
		self.assertEqual(cache.start_age_s(10_000_000, full_cadence=True), 0)


class TestStale(TestCase):
	def test_no_head_or_no_generation_is_stale(self):
		self.assertTrue(cache.stale(None, FULL))
		self.assertTrue(cache.stale(head(current_generation=None, age_s=None), FULL))

	def test_a_generation_younger_than_the_start_age_is_fresh_and_an_older_one_is_stale(self):
		start = cache.start_age_s(100_000, full_cadence=True)
		self.assertFalse(cache.stale(head(age_s=start - 1), FULL))
		self.assertTrue(cache.stale(head(age_s=start), FULL))

	def test_a_live_claim_or_a_memo_is_never_stale(self):
		self.assertFalse(cache.stale(head(claim_live=True, age_s=10_000), FULL))
		self.assertFalse(cache.stale(head(memo_live=True, age_s=10_000), FULL))

	def test_the_hourly_sample_is_stale_after_an_hour(self):
		self.assertFalse(cache.stale(head(age_s=3599), SAMPLE))
		self.assertTrue(cache.stale(head(age_s=3600), SAMPLE))

	def test_opened_means_inside_the_thirty_minute_window(self):
		self.assertTrue(cache.opened(head(opened_age_s=cache.OPEN_WINDOW_S - 1)))
		self.assertFalse(cache.opened(head(opened_age_s=cache.OPEN_WINDOW_S)))
		self.assertFalse(cache.opened(head(opened_age_s=None)))
		self.assertFalse(cache.opened(None))


class TestPrewarmWindow(TestCase):
	def test_the_window_is_a_quarter_past_midnight_to_a_quarter_past_one(self):
		for stamp, inside in (
			("2026-11-24 00:14:59", False),
			("2026-11-24 00:15:00", True),
			("2026-11-24 01:14:59", True),
			("2026-11-24 01:15:00", False),
			("2026-11-24 12:00:00", False),
		):
			with self.subTest(stamp=stamp):
				self.assertEqual(cache.in_prewarm_window(datetime.fromisoformat(stamp)), inside)

	def test_a_job_id_carries_the_hop(self):
		self.assertEqual(cache.job_id("abc", "Bank", 7), "pcal-cache:abc:Bank:7")


class TestNoteOpenedOff(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.reload_doc("jarvis", "doctype", "jarvis_settings")

	def test_off_costs_one_singles_read_and_nothing_else(self):
		frappe.get_meta("Jarvis Settings")  # the meta is cached before the statements are counted
		with (
			patch.object(frappe.db, "get_single_value", return_value="off") as read,
			patch.object(frappe.db, "sql") as sql,
			patch.object(frappe, "enqueue") as enqueue,
			patch.object(cache, "heads_of") as heads,
		):
			cache.note_opened("Acme")
		read.assert_called_once()
		sql.assert_not_called()
		enqueue.assert_not_called()
		heads.assert_not_called()


class CacheCase(SideCase):
	def setUp(self):
		super().setUp()
		self.settings = cache_state.CacheSettings("shadow", (self.company,), True)
		self.enqueue = self.start(patch.object(frappe, "enqueue", return_value=object()))
		self.start(patch.object(cache_state, "lane_live", return_value=True))
		cache.reset_lane_memo()
		self.addCleanup(cache.reset_lane_memo)
		self.digest = read_log.company_digest(self.company)
		self.addCleanup(self.drop_skip_memos)
		self.drop_skip_memos()

	def drop_skip_memos(self):
		for side in cache_store.SIDES:
			frappe.cache.execute_command("DEL", cache_state.cache_key("skip", self.digest, side))

	def start(self, patcher):
		mock = patcher.start()
		self.addCleanup(patcher.stop)
		return mock

	def make_head(self, *, age=600, opened=60, duration_ms=100_000, claim=False, memo=False):
		held = self.claim()
		self.publish(held)
		_sql(
			f"update `tab{HEAD}` set last_opened=now(6) - interval %s second, last_duration_ms=%s, "
			"claim_token=%s, claim_expires=%s, memo_until=%s where name=%s",
			(
				opened,
				duration_ms,
				"t" if claim else None,
				frappe.utils.add_to_date(None, hours=1) if claim else None,
				frappe.utils.add_to_date(None, hours=1) if memo else None,
				cache_store.head_name(self.company, self.side),
			),
		)
		_sql(f"update `tab{GENERATION}` set as_of=now(6) - interval %s second where state='current'", (age,))
		frappe.db.commit()


class TestHeads(CacheCase):
	def test_a_head_reads_with_the_age_of_its_current_generation(self):
		self.make_head(age=600, opened=60, duration_ms=1234)
		found = cache.heads_of(self.company)[self.side]
		self.assertEqual((found.company, found.side), (self.company, self.side))
		self.assertTrue(595 <= found.age_s <= 610)
		self.assertTrue(55 <= found.opened_age_s <= 70)
		self.assertEqual((found.duration_ms, found.claim_live, found.memo_live), (1234, False, False))
		self.assertEqual(found.hop_seq, 1)

	def test_a_live_claim_and_a_live_memo_are_flagged(self):
		self.make_head(claim=True, memo=True)
		found = cache.heads_of(self.company)[self.side]
		self.assertTrue(found.claim_live and found.memo_live)

	def test_all_heads_come_keyed_by_company_and_side(self):
		self.make_head()
		self.assertIn((self.company, self.side), cache.heads_of())

	def test_mark_opened_writes_at_most_once_per_window(self):
		self.make_head(opened=cache.NOTE_EVERY_S + 5)
		self.assertTrue(cache.mark_opened(self.company, self.side))
		self.assertFalse(cache.mark_opened(self.company, self.side))
		frappe.db.rollback()

	def test_mark_opened_on_a_missing_head_writes_nothing(self):
		self.assertFalse(cache.mark_opened(self.company, "Payable"))


class TestNoteOpened(CacheCase):
	def test_every_stale_side_is_queued_and_marked_opened(self):
		with patch.object(cache, "enqueue_side") as enqueue_side:
			cache.note_opened(self.company)
		self.assertEqual(
			[call.args for call in enqueue_side.call_args_list],
			[(self.company, side) for side in cache_store.SIDES],
		)
		self.assertTrue(all(call.kwargs == {"opened": True} for call in enqueue_side.call_args_list))

	def test_a_fresh_side_is_not_queued(self):
		self.make_head(age=10)
		with patch.object(cache, "enqueue_side") as enqueue_side:
			cache.note_opened(self.company)
		queued = [call.args[1] for call in enqueue_side.call_args_list]
		self.assertNotIn(self.side, queued)

	def test_a_recent_open_is_not_written_again(self):
		self.make_head(opened=30)
		with patch.object(cache, "mark_opened") as mark, patch.object(cache, "enqueue_side"):
			cache.note_opened(self.company)
		mark.assert_not_called()

	def test_an_old_open_is_written_again(self):
		self.make_head(opened=cache.NOTE_EVERY_S + 30)
		with patch.object(cache, "mark_opened") as mark, patch.object(cache, "enqueue_side"):
			cache.note_opened(self.company)
		mark.assert_called_once_with(self.company, self.side)
		frappe.db.rollback()

	def test_an_unlisted_company_does_nothing(self):
		self.settings = cache_state.CacheSettings("shadow", (), True)
		with patch.object(cache, "enqueue_side") as enqueue_side:
			cache.note_opened(self.company)
		enqueue_side.assert_not_called()

	def test_an_error_is_logged_by_class_and_never_raised(self):
		logger = MagicMock()
		with (
			patch.object(cache, "heads_of", side_effect=ValueError(self.company)),
			patch.object(frappe, "logger", return_value=logger),
		):
			cache.note_opened(self.company)
		text = str(logger.method_calls)
		self.assertIn("ValueError", text)
		self.assertNotIn(self.company, text)

	def test_a_socket_timeout_does_not_escape_a_web_request(self):
		logger = MagicMock()
		with (
			patch.object(cache, "heads_of", side_effect=TimeoutError(self.company)),
			patch.object(frappe, "logger", return_value=logger),
		):
			cache.note_opened(self.company)
		self.assertIn("TimeoutError", str(logger.method_calls))
		self.assertNotIn(self.company, str(logger.method_calls))

	def test_a_job_deadline_passes_through(self):
		with (
			patch.object(cache, "heads_of", side_effect=JobTimeoutException()),
			self.assertRaises(JobTimeoutException),
		):
			cache.note_opened(self.company)

	def test_on_acts_as_shadow(self):
		self.settings = cache_state.CacheSettings("on", (self.company,), True)
		with patch.object(cache, "enqueue_side") as enqueue_side:
			cache.note_opened(self.company)
		self.assertEqual(enqueue_side.call_count, 3)


class TestEnqueueSide(CacheCase):
	def test_a_stale_side_is_queued_on_the_lane_with_an_explicit_timeout_and_a_digest_only(self):
		self.assertEqual(cache.enqueue_side(self.company, self.side, opened=True), "queued")
		args, kwargs = self.enqueue.call_args
		self.assertEqual(args, (cache.JOB,))
		self.assertEqual(kwargs["queue"], cache_state.LANE_QUEUE)
		self.assertEqual(kwargs["timeout"], cache_state.JOB_TIMEOUT_DAY_S)
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["job_id"], cache.job_id(self.digest, self.side, 0))
		self.assertEqual(
			{key: kwargs[key] for key in ("digest", "side", "night", "opened")},
			{"digest": self.digest, "side": self.side, "night": False, "opened": True},
		)
		self.assertNotIn(self.company, str(self.enqueue.call_args))

	def test_the_job_id_uses_the_heads_hop_seq(self):
		self.make_head()
		cache.enqueue_side(self.company, self.side)
		self.assertEqual(self.enqueue.call_args.kwargs["job_id"], cache.job_id(self.digest, self.side, 1))

	def test_a_night_unit_with_no_long_worker_to_spare_is_not_queued_and_is_counted(self):
		with (
			patch.object(cache_state, "bench_slots", return_value=0) as slots,
			patch.object(cache, "count_skip") as counted,
		):
			self.assertEqual(cache.enqueue_side(self.company, self.side, night=True), "busy")
		slots.assert_called_once_with(night=True)
		counted.assert_called_once_with("no_slot")
		self.enqueue.assert_not_called()

	def test_a_stale_side_that_a_job_skipped_for_a_lasting_reason_is_not_queued_again_for_a_while(self):
		key = cache_state.cache_key("skip", self.digest, self.side)
		self.addCleanup(frappe.cache.execute_command, "DEL", key)
		cache.remember_skip(self.digest, self.side, "no_disk")
		with patch.object(cache, "count_skip") as counted:
			self.assertEqual(cache.enqueue_side(self.company, self.side), "busy")
			self.assertEqual(cache.enqueue_side(self.company, self.side, opened=True), "busy")
		self.assertEqual([call.args for call in counted.call_args_list], [("skip_memo",)] * 2)
		self.enqueue.assert_not_called()
		frappe.cache.execute_command("DEL", key)
		self.assertEqual(cache.enqueue_side(self.company, self.side), "queued")

	def test_a_skip_memo_is_per_side(self):
		key = cache_state.cache_key("skip", self.digest, self.side)
		self.addCleanup(frappe.cache.execute_command, "DEL", key)
		cache.remember_skip(self.digest, self.side, "no_disk")
		self.assertEqual(cache.enqueue_side(self.company, "Payable"), "queued")

	def test_a_night_unit_goes_to_long_with_the_night_timeout_and_needs_no_lane(self):
		with (
			patch.object(cache_state, "lane_live", return_value=False),
			patch.object(cache_state, "bench_slots", return_value=2),
		):
			self.assertEqual(cache.enqueue_side(self.company, self.side, night=True), "queued")
		kwargs = self.enqueue.call_args.kwargs
		self.assertEqual((kwargs["queue"], kwargs["timeout"]), ("long", cache_state.JOB_TIMEOUT_NIGHT_S))
		self.assertTrue(kwargs["night"])

	def test_a_day_refresh_without_a_lane_is_busy_and_queues_nothing(self):
		with patch.object(cache_state, "lane_live", return_value=False):
			self.assertEqual(cache.enqueue_side(self.company, self.side), "busy")
		self.enqueue.assert_not_called()

	def test_off_and_unlisted_are_busy(self):
		self.settings = cache_state.CacheSettings("off", (self.company,), True)
		self.assertEqual(cache.enqueue_side(self.company, self.side), "busy")
		self.settings = cache_state.CacheSettings("shadow", (), True)
		self.assertEqual(cache.enqueue_side(self.company, self.side), "busy")
		self.enqueue.assert_not_called()

	def test_a_too_large_backoff_answers_backoff_and_another_memo_answers_memo(self):
		self.make_head(memo=True)
		_sql(f"update `tab{HEAD}` set last_failure='history_too_large' where company=%s", (self.company,))
		_sql(
			f"update `tab{GENERATION}` set state='failed', cache_build=%s where state='retired'",
			(routing.cache_build(),),
		)
		frappe.db.commit()
		self.assertEqual(cache.enqueue_side(self.company, self.side), "backoff")
		_sql(f"update `tab{HEAD}` set last_failure='error:ValueError' where company=%s", (self.company,))
		frappe.db.commit()
		self.assertEqual(cache.enqueue_side(self.company, self.side), "memo")
		self.enqueue.assert_not_called()

	def test_a_live_claim_is_busy(self):
		self.make_head(claim=True)
		self.assertEqual(cache.enqueue_side(self.company, self.side), "busy")
		self.enqueue.assert_not_called()

	def test_a_duplicate_dropped_by_the_queue_is_busy(self):
		self.enqueue.return_value = None
		self.assertEqual(cache.enqueue_side(self.company, self.side), "busy")

	def test_a_queue_error_is_busy_and_logged_by_class(self):
		self.enqueue.side_effect = ConnectionError(self.company)
		logger = MagicMock()
		with patch.object(frappe, "logger", return_value=logger):
			self.assertEqual(cache.enqueue_side(self.company, self.side), "busy")
		self.assertIn("ConnectionError", str(logger.method_calls))
		self.assertNotIn(self.company, str(logger.method_calls))

	def test_a_job_deadline_in_the_enqueue_passes_through(self):
		self.enqueue.side_effect = JobTimeoutException()
		with self.assertRaises(JobTimeoutException):
			cache.enqueue_side(self.company, self.side)

	def test_a_hop_is_queued_only_with_a_lane(self):
		self.assertTrue(cache.enqueue_hop(self.company, self.side, 4))
		self.assertEqual(self.enqueue.call_args.kwargs["job_id"], cache.job_id(self.digest, self.side, 4))
		with patch.object(cache_state, "lane_live", return_value=False):
			self.assertFalse(cache.enqueue_hop(self.company, self.side, 5))


class TestHopQueued(CacheCase):
	def test_a_queued_job_with_the_heads_hop_id_is_queued(self):
		self.make_head()
		for status, expected in (
			("queued", True),
			("scheduled", True),
			("started", False),
			("finished", False),
		):
			job = MagicMock()
			job.get_status.return_value = status
			with self.subTest(status=status), patch.object(cache, "get_job", return_value=job) as get:
				self.assertEqual(cache.hop_queued(self.digest, self.side), expected)
				get.assert_called_once_with(cache.job_id(self.digest, self.side, 1))

	def test_no_job_or_no_head_is_not_queued(self):
		self.make_head()
		with patch.object(cache, "get_job", return_value=None):
			self.assertFalse(cache.hop_queued(self.digest, self.side))
		self.assertFalse(cache.hop_queued("0" * 16, self.side))

	def test_a_queue_error_is_not_queued(self):
		self.make_head()
		with patch.object(cache, "get_job", side_effect=ConnectionError):
			self.assertFalse(cache.hop_queued(self.digest, self.side))


class TestTick(CacheCase):
	def setUp(self):
		super().setUp()
		self.janitor = self.start(patch.object(cache_store, "janitor", return_value=0))
		self.queue = self.start(patch.object(cache, "enqueue_side"))
		self.start(patch.object(cache, "_prewarm"))

	def test_off_with_no_head_does_nothing(self):
		self.settings = cache_state.CacheSettings("off", (), False)
		cache.tick()
		self.janitor.assert_not_called()
		self.queue.assert_not_called()

	def test_off_with_heads_purges_every_company(self):
		self.make_head()
		self.settings = cache_state.CacheSettings("off", (self.company,), False)
		cache.tick()
		self.assertEqual(self.janitor.call_args.kwargs["listed"], ())
		self.queue.assert_not_called()

	def test_active_runs_the_janitor_with_the_list_and_the_cache_build(self):
		cache.tick()
		self.assertEqual(
			self.janitor.call_args.kwargs, {"listed": (self.company,), "cache_build": routing.cache_build()}
		)

	def test_an_opened_stale_side_is_queued(self):
		self.make_head(age=600, opened=60)
		cache.tick()
		self.queue.assert_called_once_with(self.company, self.side)

	def test_a_fresh_side_is_not_queued(self):
		self.make_head(age=10, opened=60)
		cache.tick()
		self.queue.assert_not_called()

	def test_a_side_nobody_opened_in_half_an_hour_is_not_queued(self):
		self.make_head(age=600, opened=cache.OPEN_WINDOW_S + 60)
		cache.tick()
		self.queue.assert_not_called()

	def test_a_side_with_a_claim_or_a_memo_is_not_queued(self):
		self.make_head(age=600, claim=True)
		cache.tick()
		self.queue.assert_not_called()

	def test_a_failing_step_does_not_stop_the_enqueues_and_is_logged_by_class(self):
		self.make_head(age=600, opened=60)
		for step, target in (("expired", "_record_expired_builds"), ("janitor", None)):
			with self.subTest(step=step):
				self.queue.reset_mock()
				logger = MagicMock()
				if target:
					patcher = patch.object(cache, target, side_effect=ValueError(self.company))
				else:
					patcher = patch.object(self.janitor, "side_effect", ValueError(self.company))
				with patcher, patch.object(frappe, "logger", return_value=logger):
					cache.tick()
				self.queue.assert_called_once_with(self.company, self.side)
				self.assertIn("ValueError", str(logger.method_calls))
				self.assertNotIn(self.company, str(logger.method_calls))

	def test_a_job_deadline_in_a_step_still_stops_the_tick(self):
		self.make_head(age=600, opened=60)
		with patch.object(cache, "_record_expired_builds", side_effect=JobTimeoutException()):
			with self.assertRaises(JobTimeoutException):
				cache.tick()
		self.queue.assert_not_called()

	def test_a_rollback_class_error_in_a_step_still_stops_the_tick(self):
		self.make_head(age=600, opened=60)
		self.janitor.side_effect = frappe.QueryDeadlockError()
		with self.assertRaises(frappe.QueryDeadlockError):
			cache.tick()
		self.queue.assert_not_called()

	def test_a_company_that_was_never_built_is_not_queued_by_the_tick(self):
		cache.tick()
		self.queue.assert_not_called()

	def test_a_building_generation_that_outlived_its_job_is_recorded_as_a_lost_worker(self):
		held = self.claim()
		_sql(
			f"update `tab{GENERATION}` set deadline=now(6) - interval 5 second where name=%s",
			(held.generation,),
		)
		_sql(
			f"update `tab{HEAD}` set claim_expires=now(6) - interval 5 second where company=%s",
			(self.company,),
		)
		frappe.db.commit()
		cache.tick()
		frappe.db.rollback()
		head = self.head()
		# A job that left no trace is not "too large": it is one plain failure, with no 6 h backoff.
		self.assertEqual((head.last_failure, head.failures, head.backoff_level), ("worker_lost", 1, 0))
		self.assertIsNone(head.memo_until)
		generation = self.gen(held.generation)
		self.assertEqual((generation.state, generation.failure_class), ("failed", "worker_lost"))

	def test_a_building_generation_whose_claim_was_already_released_is_still_counted(self):
		held = self.claim()
		_sql(
			f"update `tab{GENERATION}` set deadline=now(6) - interval 5 second where name=%s",
			(held.generation,),
		)
		_sql(f"update `tab{HEAD}` set claim_token=NULL, claim_expires=NULL where company=%s", (self.company,))
		frappe.db.commit()
		cache.tick()
		frappe.db.rollback()
		head = self.head()
		self.assertEqual((head.last_failure, head.failures), ("worker_lost", 1))
		self.assertEqual(self.gen(held.generation).state, "failed")

	def test_a_killed_job_that_a_later_build_replaced_puts_no_memo_on_the_healthy_head(self):
		killed = self.claim()
		self.expire_claim()
		later = self.claim()
		self.publish(later)
		# Only now does the killed job's deadline pass: the head holds a newer, good generation.
		_sql(
			f"update `tab{GENERATION}` set deadline=now(6) - interval 5 second where name=%s",
			(killed.generation,),
		)
		frappe.db.commit()
		cache.tick()
		frappe.db.rollback()
		head = self.head()
		self.assertEqual((head.failures, head.backoff_level, head.last_failure), (0, 0, None))
		self.assertIsNone(head.memo_until)
		self.assertEqual(self.head().current_generation, later.generation)
		self.assertEqual(self.gen(killed.generation).state, "failed")

	def test_the_orphan_is_failed_through_the_store_without_a_claim(self):
		held = self.claim()
		_sql(
			f"update `tab{GENERATION}` set deadline=now(6) - interval 5 second where name=%s",
			(held.generation,),
		)
		frappe.db.commit()
		with patch.object(cache_store, "fail_orphan", wraps=cache_store.fail_orphan) as orphan:
			cache._record_expired_builds()
		orphan.assert_called_once_with(self.company, self.side, held.generation, "worker_lost")

	def test_a_building_generation_of_a_live_claim_is_left_alone(self):
		held = self.claim()
		_sql(
			f"update `tab{GENERATION}` set deadline=now(6) - interval 5 second where name=%s",
			(held.generation,),
		)
		frappe.db.commit()
		cache.tick()
		self.assertEqual(self.gen(held.generation).state, "building")


class TestTickSteps(CacheCase):
	"""A failing housekeeping step is named in the log and counted; the tick keeps its stamp for the dead-man check."""

	def setUp(self):
		super().setUp()
		self.janitor = self.start(patch.object(cache_store, "janitor", return_value=7))
		self.queue = self.start(patch.object(cache, "enqueue_side"))
		self.start(patch.object(cache, "_prewarm"))
		self.addCleanup(self.drop_stamps)
		self.drop_stamps()

	def drop_stamps(self):
		for name in ("last_tick", "tick_watch"):
			frappe.cache.execute_command("DEL", cache_state.cache_key(name))

	def test_each_step_is_named_in_the_log_and_counted_when_it_fails(self):
		for name, target in (
			("expired_builds", "_record_expired_builds"),
			("deletions", "purge_after_deletion"),
		):
			with self.subTest(name):
				logger = MagicMock()
				with (
					patch.object(cache, target, side_effect=ValueError(self.company)),
					patch.object(frappe, "logger", return_value=logger),
					patch.object(cache, "count_skip") as counted,
				):
					cache.tick()
				self.assertIn(f"'cache_tick {name}'", str(logger.method_calls))
				self.assertIn("ValueError", str(logger.method_calls))
				self.assertNotIn(self.company, str(logger.method_calls))
				counted.assert_any_call(f"tick_step_failed:{name}")

	def test_a_failing_janitor_is_named_too(self):
		self.janitor.side_effect = ValueError(self.company)
		logger = MagicMock()
		with (
			patch.object(frappe, "logger", return_value=logger),
			patch.object(cache, "count_skip") as counted,
		):
			cache.tick()
		self.assertIn("'cache_tick janitor'", str(logger.method_calls))
		counted.assert_any_call("tick_step_failed:janitor")

	def test_the_tick_logs_the_rows_the_janitor_deleted(self):
		logger = MagicMock()
		with patch.object(frappe, "logger", return_value=logger):
			cache.tick()
		self.assertIn("cache_janitor rows_deleted=%s', 7", str(logger.method_calls))

	def test_a_tick_stamps_the_time_it_ran_even_when_it_does_nothing(self):
		self.assertIsNone(cache.last_tick_age_s())
		self.settings = cache_state.CacheSettings("off", (), False)
		cache.tick()
		self.assertTrue(0 <= cache.last_tick_age_s() <= 5)

	def test_a_tick_that_stops_early_on_an_unreadable_switch_still_stamps(self):
		self.settings = cache_state.UNKNOWN
		cache.tick()
		self.assertTrue(0 <= cache.last_tick_age_s() <= 5)


class TestTickDeadMan(CacheCase):
	"""A request notices a tick that has not run, because a dead scheduler cannot raise its own alert."""

	def setUp(self):
		super().setUp()
		self.start(patch.object(cache, "enqueue_side"))
		self.addCleanup(self.drop_stamps)
		self.drop_stamps()
		self.rows_before = self.alert_rows()

	def drop_stamps(self):
		for name in ("last_tick", "tick_watch"):
			frappe.cache.execute_command("DEL", cache_state.cache_key(name))
		frappe.cache.delete_keys("jarvis:pcal:errlog:cache:")

	def stamp(self, age_s):
		frappe.cache.execute_command(
			"SET", cache_state.cache_key("last_tick"), str(int(time.time()) - age_s), "EX", 3600
		)

	def alert_rows(self):
		frappe.db.rollback()
		return frappe.db.count("Error Log", {"method": cache_alerts.TITLES["tick"]})

	def open(self):
		cache.note_opened(self.company)

	def test_a_tick_that_is_older_than_the_limit_writes_one_alert_row_a_day(self):
		self.stamp(cache.TICK_LATE_S + 60)
		self.open()
		self.open()
		self.assertEqual(self.alert_rows(), self.rows_before + 1)
		frappe.db.rollback()
		frappe.db.delete("Error Log", {"method": cache_alerts.TITLES["tick"]})
		frappe.db.commit()

	def test_a_fresh_tick_writes_nothing(self):
		self.stamp(60)
		self.open()
		self.assertEqual(self.alert_rows(), self.rows_before)

	def test_the_alert_never_commits_the_requests_transaction(self):
		self.stamp(cache.TICK_LATE_S + 60)
		with patch.object(frappe.db, "commit") as commit:
			self.open()
		commit.assert_not_called()
		frappe.db.rollback()
		frappe.db.delete("Error Log", {"method": cache_alerts.TITLES["tick"]})
		frappe.db.commit()

	def test_with_no_tick_ever_seen_the_clock_starts_at_the_first_request_and_an_old_watch_alerts(self):
		self.open()
		self.assertEqual(self.alert_rows(), self.rows_before)
		self.assertTrue(0 <= cache.tick_age_s() <= 5)
		frappe.cache.execute_command(
			"SET",
			cache_state.cache_key("tick_watch"),
			str(int(time.time()) - cache.TICK_LATE_S - 60),
			"EX",
			3600,
		)
		self.open()
		self.assertEqual(self.alert_rows(), self.rows_before + 1)
		frappe.db.rollback()
		frappe.db.delete("Error Log", {"method": cache_alerts.TITLES["tick"]})
		frappe.db.commit()

	def test_an_unlisted_company_or_a_switch_that_is_off_does_not_look_at_the_tick(self):
		self.settings = cache_state.CacheSettings("shadow", (), True)
		with patch.object(cache, "tick_age_s") as age:
			self.open()
		age.assert_not_called()

	def test_a_redis_error_in_the_check_never_reaches_the_request(self):
		with patch.object(frappe.cache, "execute_command", side_effect=ConnectionError("x")):
			self.assertIsNone(cache.tick_age_s())
			self.open()


class TestMemoLiftedByANewBuild(CacheCase):
	"""A memo holds for the build that failed: a deploy of a new build starts the side again at once."""

	OTHER_BUILD = "c" * 64

	def setUp(self):
		super().setUp()
		self.start(patch.object(cache_store, "janitor", return_value=0))
		self.queue = self.start(patch.object(cache, "enqueue_side"))
		self.start(patch.object(cache, "_prewarm"))
		self.make_head(age=600, opened=60, memo=True)

	def failed_by(self, build):
		_sql(
			f"insert into `tab{GENERATION}` (name, creation, modified, company, side, state, outcome, cache_build, "
			"deadline) values (%s, now(6), now(6), %s, %s, 'failed', 'failed', %s, now(6))",
			(frappe.db.get_next_sequence_val(GENERATION), self.company, self.side, build),
		)
		frappe.db.commit()

	def queued_sides(self):
		return [call.args[1] for call in self.queue.call_args_list]

	def test_the_tick_queues_a_side_whose_memo_belongs_to_an_older_build(self):
		self.failed_by(self.OTHER_BUILD)
		cache.tick()
		self.queue.assert_called_once_with(self.company, self.side)

	def test_the_tick_leaves_a_memo_of_the_running_build_alone(self):
		self.failed_by(routing.cache_build())
		cache.tick()
		self.queue.assert_not_called()

	def test_a_memo_with_no_failed_generation_holds(self):
		cache.tick()
		self.queue.assert_not_called()

	def test_a_request_queues_a_side_whose_memo_belongs_to_an_older_build(self):
		self.failed_by(self.OTHER_BUILD)
		cache.note_opened(self.company)
		self.assertIn(self.side, self.queued_sides())

	def test_a_request_leaves_a_memo_of_the_running_build_alone(self):
		self.failed_by(routing.cache_build())
		cache.note_opened(self.company)
		self.assertNotIn(self.side, self.queued_sides())

	def test_the_head_reads_as_memo_live_only_for_the_build_that_failed(self):
		self.failed_by(self.OTHER_BUILD)
		self.assertFalse(cache.heads_of(self.company)[self.side].memo_live)
		self.failed_by(routing.cache_build())
		self.assertTrue(cache.heads_of(self.company)[self.side].memo_live)


class TestReset(CacheCase):
	"""`cache.reset`: the operator clears the memo, the backoff and the counters of a company."""

	USER = "pcal-fw1-reset@example.com"

	def setUp(self):
		super().setUp()
		self.make_head()
		_sql(
			f"update `tab{HEAD}` set memo_until=now(6) + interval 6 hour, backoff_level=2, failures=3, "
			"last_failure='history_too_large' where company=%s",
			(self.company,),
		)
		frappe.db.commit()

	def reset(self, *args):
		"""The call, then the commit that a request or `bench execute` makes after it."""
		found = cache.reset(*args)
		frappe.db.commit()
		return found

	def state(self, side="Receivable"):
		frappe.db.rollback()
		return _sql(
			f"select memo_until, backoff_level, failures, last_failure from `tab{HEAD}` where company=%s and side=%s",
			(self.company, side),
		)[0]

	def test_it_clears_memo_backoff_failures_and_the_last_failure_of_every_side(self):
		self.assertEqual(self.reset(self.company), {"heads": 1})
		self.assertEqual(self.state(), (None, 0, 0, None))

	def test_one_side_only_when_a_side_is_named(self):
		_sql(
			f"insert into `tab{HEAD}` (name, creation, modified, company, side, company_creation, backoff_level, failures) "
			"values (%s, now(6), now(6), %s, 'Payable', now(6), 1, 2)",
			(cache_store.head_name(self.company, "Payable"), self.company),
		)
		frappe.db.commit()
		self.assertEqual(self.reset(self.company, "Payable"), {"heads": 1})
		self.assertEqual(self.state("Payable")[1:3], (0, 0))
		self.assertEqual(self.state()[1:3], (2, 3))

	def test_a_company_with_no_head_resets_nothing(self):
		self.assertEqual(self.reset("PCAL-FW1-no-heads"), {"heads": 0})

	def test_an_unknown_side_is_refused(self):
		with self.assertRaises(ValueError):
			cache.reset(self.company, "Sideways")
		self.assertEqual(self.state()[1], 2)

	def test_the_reset_keeps_the_claim_and_the_current_generation(self):
		before = self.head()
		self.reset(self.company)
		after = self.head()
		self.assertEqual((after.current_generation, after.purge_epoch), (before.current_generation, 0))

	def test_the_next_tick_queues_the_side_again(self):
		self.start(patch.object(cache_store, "janitor", return_value=0))
		queue = self.start(patch.object(cache, "enqueue_side"))
		self.start(patch.object(cache, "_prewarm"))
		_sql(f"update `tab{GENERATION}` set as_of=now(6) - interval 600 second where state='current'")
		frappe.db.commit()
		cache.tick()
		queue.assert_not_called()
		self.reset(self.company)
		cache.tick()
		queue.assert_called_once_with(self.company, self.side)

	def test_only_a_system_manager_may_call_it(self):
		from jarvis.tests.test_payment_calendar_routing import _as, _ensure_system_manager

		_ensure_system_manager(self.USER)
		with _as(self.USER):
			self.assertEqual(self.reset(self.company), {"heads": 1})
		self.assertEqual(self.state(), (None, 0, 0, None))
		plain = "pcal-fw1-plain@example.com"
		if not frappe.db.exists("User", plain):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": plain,
					"first_name": "plain",
					"send_welcome_email": 0,
					"enabled": 1,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		_sql(f"update `tab{HEAD}` set backoff_level=2, memo_until=now(6) + interval 1 hour")
		frappe.db.commit()
		with _as(plain), self.assertRaises(frappe.PermissionError):
			cache.reset(self.company)
		self.assertEqual(self.state()[1], 2)

	def test_it_is_a_whitelisted_post(self):
		self.assertIn(cache.reset, frappe.whitelisted)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[cache.reset], ["POST"])


class TestTickOnTheRealSwitch(CacheCase):
	"""The tick reads the real switch and runs the real janitor; only the source of the switch is made to fail."""

	def setUp(self):
		super().setUp()
		self.make_head()
		self.current = self.head().current_generation
		self.start(patch.object(cache_state, "cache_settings", side_effect=REAL_SETTINGS))
		self.queue = self.start(patch.object(cache, "enqueue_side"))
		self.start(patch.object(cache, "_prewarm"))

	def tick_reading(self, **read):
		with patch.object(frappe.db, "get_single_value", **read):
			cache.tick()
		frappe.db.rollback()

	def test_an_unreadable_switch_purges_nothing(self):
		for exc in (RuntimeError("boom"), frappe.db.OperationalError(1054, "x")):
			with self.subTest(exc=repr(exc)):
				self.tick_reading(side_effect=exc)
				head = self.head()
				self.assertEqual((head.current_generation, head.purge_epoch), (self.current, 0))
				self.assertEqual(self.gen(self.current).state, "current")
				self.assertEqual(self.row_count(self.current), 1)
				self.queue.assert_not_called()

	def test_an_unreadable_switch_is_counted_as_a_skip(self):
		with patch.object(cache, "count_skip") as skip:
			self.tick_reading(side_effect=RuntimeError("boom"))
		skip.assert_called_once_with("settings_unknown")

	def test_a_rollback_class_error_of_the_switch_read_stops_the_tick(self):
		with self.assertRaises(frappe.QueryDeadlockError):
			self.tick_reading(side_effect=frappe.QueryDeadlockError("x"))
		self.assertEqual(self.head().purge_epoch, 0)

	def test_a_switch_read_as_off_purges_every_company(self):
		self.tick_reading(return_value="off")
		head = self.head()
		self.assertEqual((head.current_generation, head.purge_epoch), (None, 1))
		self.assertEqual(self.row_count(self.current), 0)

	def test_a_company_that_a_read_list_leaves_out_is_purged(self):
		self.tick_reading(
			side_effect=lambda doctype, field, **_: "shadow" if "companies" not in field else ""
		)
		self.assertEqual(self.head().purge_epoch, 1)


class TestPrewarm(CacheCase):
	def setUp(self):
		super().setUp()
		self.queue = self.start(patch.object(cache, "enqueue_side"))
		jitter = int(self.digest[:8], 16) % cache.PREWARM_JITTER_S
		self.after_jitter = datetime(2026, 11, 24, 0, 15, 0) + timedelta(seconds=jitter)
		self.start(patch.object(cache, "night_marker_set", return_value=False))
		self.slots = self.start(patch.object(cache_state, "bench_slots", return_value=9))
		self.jitter = jitter

	def prewarm(self, now):
		cache._prewarm(self.settings, now)

	def test_in_the_window_after_the_jitter_every_side_is_queued_as_a_night_unit(self):
		self.prewarm(self.after_jitter)
		self.assertEqual(
			[(call.args, call.kwargs) for call in self.queue.call_args_list],
			[((self.company, side), {"night": True}) for side in cache_store.SIDES],
		)

	def test_one_tick_queues_no_more_night_units_than_the_bench_has_slots(self):
		self.slots.return_value = 2
		self.queue.return_value = "queued"
		self.settings = cache_state.CacheSettings("shadow", ("A Co", "B Co", "C Co"), True)
		self.prewarm(datetime(2026, 11, 24, 1, 14, 59))
		self.assertEqual(self.queue.call_count, 2)
		self.slots.assert_called_with(night=True)

	def test_a_unit_that_was_not_queued_does_not_use_up_a_slot(self):
		self.slots.return_value = 2
		self.queue.return_value = "busy"
		self.settings = cache_state.CacheSettings("shadow", ("A Co", "B Co"), True)
		self.prewarm(datetime(2026, 11, 24, 1, 14, 59))
		self.assertEqual(self.queue.call_count, 6)

	def test_with_no_slot_at_all_nothing_is_queued_and_it_is_counted_once(self):
		self.slots.return_value = 0
		with patch.object(cache, "count_skip") as counted:
			self.prewarm(self.after_jitter)
		self.queue.assert_not_called()
		counted.assert_called_once_with("no_slot")

	def prewarm_with_long_workers(self, long, now=None):
		"""The real slot rule with `long` live `long` workers and no lane (a standard one-worker bench)."""
		self.slots.side_effect = lambda night: REAL_BENCH_SLOTS(night=night)
		self.queue.return_value = "queued"
		self.settings = cache_state.CacheSettings("shadow", ("A Co", "B Co"), True)
		with (
			patch.object(cache_state, "lane_workers", return_value=0),
			patch.object(cache_state, "long_workers", return_value=long),
		):
			self.prewarm(now or datetime(2026, 11, 24, 1, 14, 59))

	def test_a_bench_with_one_long_worker_and_no_lane_queues_one_unit_in_the_window(self):
		self.prewarm_with_long_workers(1)
		self.assertEqual(self.queue.call_count, 1)
		self.assertEqual(self.queue.call_args.kwargs, {"night": True})

	def test_two_long_workers_queue_one_unit_and_three_queue_two(self):
		for long, units in ((2, 1), (3, 2)):
			with self.subTest(long=long):
				self.queue.reset_mock()
				self.prewarm_with_long_workers(long)
				self.assertEqual(self.queue.call_count, units)

	def test_a_bench_with_no_long_worker_queues_nothing_and_counts_it_once(self):
		with patch.object(cache, "count_skip") as counted:
			self.prewarm_with_long_workers(0)
		self.queue.assert_not_called()
		counted.assert_called_once_with("no_slot")

	def test_a_bench_with_one_long_worker_queues_nothing_by_day(self):
		self.prewarm_with_long_workers(1, now=datetime(2026, 11, 24, 12, 0, 0))
		self.queue.assert_not_called()

	def test_before_the_jitter_nothing_is_queued(self):
		if self.jitter == 0:
			self.skipTest("this digest has no jitter")
		self.prewarm(datetime(2026, 11, 24, 0, 15, 0))
		self.queue.assert_not_called()

	def test_outside_the_window_nothing_is_queued(self):
		self.prewarm(datetime(2026, 11, 24, 1, 15, 0))
		self.prewarm(datetime(2026, 11, 24, 12, 0, 0))
		self.queue.assert_not_called()

	def test_a_unit_that_started_tonight_is_not_queued_again(self):
		with patch.object(cache, "night_marker_set", return_value=True):
			self.prewarm(self.after_jitter)
		self.queue.assert_not_called()

	def test_the_night_marker_is_taken_once_per_night(self):
		key = frappe.cache.make_key(f"jarvis:pcal:cache:night:{self.digest}:Bank:{routing.today()}")
		self.addCleanup(frappe.cache.execute_command, "DEL", key)
		self.assertTrue(cache.take_night_marker(self.digest, "Bank"))
		self.assertFalse(cache.take_night_marker(self.digest, "Bank"))
		ttl = frappe.cache.execute_command("TTL", key)
		self.assertTrue(0 < ttl <= cache.NIGHT_MARKER_TTL_S)
		self.assertTrue(key.startswith(frappe.cache.make_key("jarvis:pcal:")))


class TestJobsCall(TestCase):
	"""`jobs.request_snapshot` makes exactly one call, after `_submit`, and never changes its reply."""

	def setUp(self):
		self.calls = []
		self.status = {"state": "queued", "snapshot_id": "s1"}
		scope = {"actor": "a@example.com", "company": "Acme"}
		self.scope = scope

		def submit(*args, **kwargs):
			self.calls.append("submit")
			return self.status

		patches = [
			patch.object(jobs, "authorize_scope", return_value={"fingerprint": "f"}),
			patch.object(jobs, "_submit", side_effect=submit),
			patch.object(jobs.store, "public_status", side_effect=lambda status, *a: dict(status)),
		]
		for patcher in patches:
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_note_opened_is_called_once_after_submit_with_the_company(self):
		with patch.object(
			cache, "note_opened", side_effect=lambda company: self.calls.append(("note", company))
		):
			reply = jobs.request_snapshot(self.scope)
		self.assertEqual(self.calls, ["submit", ("note", "Acme")])
		self.assertEqual(reply, {"state": "queued", "snapshot_id": "s1"})

	def test_an_exception_inside_the_cache_leaves_the_reply_equal(self):
		with patch.object(cache_state, "cache_settings", return_value=FULL):
			plain = jobs.request_snapshot(self.scope)
			with patch.object(cache, "heads_of", side_effect=ValueError("boom")):
				failing = jobs.request_snapshot(self.scope)
		self.assertEqual(plain, failing)

	def test_with_the_switch_off_the_reply_is_equal_and_nothing_is_queued(self):
		with (
			patch.object(
				cache_state, "cache_settings", return_value=cache_state.CacheSettings("off", (), False)
			),
			patch.object(frappe, "enqueue") as enqueue,
		):
			reply = jobs.request_snapshot(self.scope)
		self.assertEqual(reply, {"state": "queued", "snapshot_id": "s1"})
		enqueue.assert_not_called()


def simulate(workers: int, iteration_s: int, *, chains: int = 3, hours: int = 8) -> list[int]:
	"""Fake-clock model of the lane: `chains` continuous chains share `workers` lane workers.

	A job loops while `LOOP_MIN_LEFT_S` of its timeout remain, then hands over: the hop waits in a FIFO queue for a
	free worker. Returns each chain's maximum generation age in seconds (the age just before each swap, from the
	second swap on). It uses the module's own constants and `cache.continuous`.
	"""
	assert cache.continuous(iteration_s * 1000, full_cadence=True)
	timeout, horizon = cache_state.job_timeout_s(night=False), hours * 3600
	free, waiting, heap, ticket = workers, deque(range(chains)), [], count()
	as_of, worst = [None] * chains, [0] * chains

	def dispatch(at):
		nonlocal free
		while waiting and free:
			free -= 1
			heapq.heappush(heap, (at, next(ticket), waiting.popleft(), at))

	dispatch(0)
	while heap:
		now, _, chain, began = heapq.heappop(heap)
		if now > horizon:
			break
		swap = now + iteration_s
		if as_of[chain] is not None:
			worst[chain] = max(worst[chain], swap - as_of[chain])
		as_of[chain] = now
		if timeout - (swap - began) >= cache_state.LOOP_MIN_LEFT_S:
			heapq.heappush(heap, (swap, next(ticket), chain, began))
		else:
			free += 1
			waiting.append(chain)
			dispatch(swap)
	return worst


class TestFreshnessModel(TestCase):
	def test_three_workers_hold_the_maximum_age_of_every_chain_under_fourteen_minutes(self):
		worst = simulate(3, cache_state.D_MAX_S)
		self.assertEqual(len(worst), 3)
		self.assertLessEqual(max(worst), 14 * 60)

	def test_fewer_workers_than_chains_let_a_chain_go_stale(self):
		for workers in (1, 2):
			with self.subTest(workers=workers):
				self.assertGreater(max(simulate(workers, cache_state.D_MAX_S)), 14 * 60)

	def test_the_enforced_bound_per_iteration_keeps_the_age_at_twice_the_bound(self):
		self.assertLessEqual(2 * cache_state.D_MAX_S, 14 * 60)
