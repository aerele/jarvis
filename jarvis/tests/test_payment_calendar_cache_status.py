"""Payment Calendar S4b B9: `cache_status` (the operator view) and the lane memo of `note_opened`.

The status reads durable rows only (heads, generations) and the Redis day counters; no name reaches a log,
a key or an Error Log row. Every Redis key a test makes sits under a date that no real day uses, and is
deleted again.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_status
"""

import json
import time
from decimal import Decimal
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from jarvis.payment_calendar import cache, cache_state, cache_status, cache_store, read_log, routing
from jarvis.tests import test_payment_calendar_cache_tick as tick_tests
from jarvis.tests.test_payment_calendar_cache_store import GENERATION, HEAD, _row, _sql
from jarvis.tests.test_payment_calendar_routing import _as, _ensure_system_manager

FAR_DAY = "2099-01-01"


def section_ok():
	return {"card": {"served": 3}}


def section_bad():
	raise ValueError("Acme Ltd")


def section_not_a_dict():
	return ["no"]


def with_hooks(*paths):
	real = frappe.get_hooks

	def get_hooks(name=None, *args, **kwargs):
		return list(paths) if name == cache_status.SECTIONS_HOOK else real(name, *args, **kwargs)

	return patch.object(frappe, "get_hooks", get_hooks)


class StableCase(tick_tests.CacheCase):
	"""The company is the oldest real one: other test runs create and delete `_JPC_` companies on this site."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = stable_company()


def stable_company() -> str:
	return frappe.get_all(
		"Company", filters={"name": ["not like", "_JPC_%"]}, order_by="creation asc", pluck="name", limit=1
	)[0]


class TestNearestRank(TestCase):
	def test_the_95th_percentile_is_the_nearest_rank(self):
		self.assertEqual(cache_status.nearest_rank(list(range(1, 101)), 0.95), 95)
		self.assertEqual(cache_status.nearest_rank([7], 0.95), 7)
		self.assertEqual(cache_status.nearest_rank([30, 10, 20], 0.95), 30)

	def test_no_values_give_none(self):
		self.assertIsNone(cache_status.nearest_rank([], 0.95))


class TestLaneMemo(StableCase):
	"""`note_opened` must not probe the lane per request per stale side."""

	def setUp(self):
		super().setUp()
		cache.reset_lane_memo()
		self.addCleanup(cache.reset_lane_memo)
		self.probe = self.start(patch.object(cache_state, "lane_live", return_value=True))
		self.clock = [1000.0]
		self.start(patch.object(cache, "_monotonic", lambda: self.clock[0]))

	def test_requests_inside_the_memo_probe_the_lane_once(self):
		for side in cache_store.SIDES:
			cache.enqueue_side(self.company, side, opened=True)
		cache.enqueue_side(self.company, "Receivable", opened=True)
		self.assertEqual(self.probe.call_count, 1)

	def test_the_memo_life_is_a_minute_at_most(self):
		"""A dead lane must not stay hidden for long: this bound does not follow the constant."""
		self.assertLessEqual(cache.LANE_MEMO_S, 60)
		self.assertGreater(cache.LANE_MEMO_S, 0)

	def test_the_memo_ends_after_its_lifetime(self):
		cache.enqueue_side(self.company, "Receivable", opened=True)
		self.clock[0] += cache.LANE_MEMO_S - 1
		cache.enqueue_side(self.company, "Receivable", opened=True)
		self.assertEqual(self.probe.call_count, 1)
		self.clock[0] += 1
		cache.enqueue_side(self.company, "Receivable", opened=True)
		self.assertEqual(self.probe.call_count, 2)

	def test_a_lane_that_is_down_is_remembered_too(self):
		self.probe.return_value = False
		self.assertEqual(cache.enqueue_side(self.company, "Receivable", opened=True), "busy")
		self.probe.return_value = True
		self.assertEqual(cache.enqueue_side(self.company, "Payable", opened=True), "busy")
		self.assertEqual(self.probe.call_count, 1)
		self.enqueue.assert_not_called()

	def test_the_tick_and_other_callers_always_probe_live(self):
		cache.enqueue_side(self.company, "Receivable")
		cache.enqueue_side(self.company, "Payable")
		self.assertEqual(self.probe.call_count, 2)

	def test_a_call_that_does_not_remember_does_not_fill_the_memo(self):
		cache.enqueue_side(self.company, "Receivable")
		cache.enqueue_side(self.company, "Payable", opened=True)
		self.assertEqual(self.probe.call_count, 2)

	def test_a_night_unit_never_probes(self):
		cache.enqueue_side(self.company, "Receivable", night=True, opened=True)
		self.probe.assert_not_called()


class TestStatusForASystemManager(StableCase):
	"""A real System Manager who is not Administrator: `frappe.has_permission` is never patched here."""

	USER = "pcal-fw1-sysman@example.com"
	GONE = "PCAL-FW1-no-such-company"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_system_manager(cls.USER)

	def setUp(self):
		super().setUp()
		self.addCleanup(self.drop_permissions)
		self.drop_permissions()

	def drop_permissions(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.delete("User Permission", {"user": self.USER})
		frappe.db.commit()
		frappe.clear_cache(user=self.USER)

	def head_of_a_gone_company(self):
		_sql(
			f"insert into `tab{HEAD}` (name, creation, modified, company, side, company_creation) "
			"values (%s, now(6), now(6), %s, 'Receivable', now(6))",
			(cache_store.head_name(self.GONE, "Receivable"), self.GONE),
		)
		frappe.db.commit()

	def restrict_to_another_company(self):
		other = next(
			iter(frappe.get_all("Company", filters={"name": ["!=", self.company]}, pluck="name", limit=1)),
			None,
		)
		if other is None:
			self.skipTest("the site has one company only")
		frappe.get_doc(
			{"doctype": "User Permission", "user": self.USER, "allow": "Company", "for_value": other}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache(user=self.USER)

	def status(self):
		with _as(self.USER):
			return cache_status.cache_status()

	def entry(self, found, company):
		digest = cache_status.digest_of(company)
		return next(item for item in found["companies"] if item["digest"] == digest)

	def test_a_company_that_is_gone_does_not_break_the_call(self):
		self.make_head()
		self.head_of_a_gone_company()
		self.settings = cache_state.CacheSettings("shadow", (self.company, self.GONE), True)
		found = self.status()
		gone = self.entry(found, self.GONE)
		self.assertTrue(gone["missing"])
		self.assertNotIn("company", gone)
		self.assertNotIn(self.GONE, json.dumps(found, default=str))
		self.assertEqual(self.entry(found, self.company)["company"], self.company)

	def test_a_company_the_user_cannot_read_has_no_name_and_no_counts(self):
		self.make_head()
		self.restrict_to_another_company()
		found = self.status()
		entry = self.entry(found, self.company)
		self.assertEqual((entry["readable"], entry["sides"], entry["listed"]), (False, {}, True))
		self.assertNotIn("company", entry)
		self.assertNotIn(self.company, json.dumps(found, default=str))

	def test_a_readable_company_keeps_its_name_and_its_sides(self):
		self.make_head()
		entry = self.entry(self.status(), self.company)
		self.assertEqual((entry["readable"], entry["company"]), (True, self.company))
		self.assertIn("Receivable", entry["sides"])


class TestStatus(StableCase):
	"""One company with one published generation per call of `make_head`."""

	def side_status(self, **kwargs):
		found = cache_status.build_status(**kwargs)
		entry = next(item for item in found["companies"] if item["digest"] == self.digest)
		return found, entry, entry["sides"][self.side]

	def test_the_top_level_shows_the_mode_the_build_and_the_lane(self):
		self.make_head()
		with patch.object(cache_state, "lane_workers", return_value=2):
			found, entry, _ = self.side_status()
		self.assertEqual(
			(found["mode"], found["effective_mode"], found["full_cadence"]), ("shadow", "shadow", True)
		)
		self.assertEqual(found["build"], routing.cache_build())
		self.assertEqual(found["lane"], {"workers": 2, "live": True})
		self.assertTrue(entry["listed"])

	def test_a_bench_without_a_lane_shows_the_day_lane_absent_and_the_refresh_night_only(self):
		self.make_head()
		with patch.object(cache_state, "lane_workers", return_value=0):
			found, _, side = self.side_status()
		self.assertEqual((found["day_lane"], found["refresh"]), ("absent", "night-only"))
		self.assertEqual(found["lane"], {"workers": 0, "live": False})
		self.assertIsNotNone(side["age_s"])  # the age of the night generation is the freshness to read

	def test_a_bench_with_a_live_lane_shows_the_day_lane_live_and_the_refresh_day_and_night(self):
		self.make_head()
		with patch.object(cache_state, "lane_workers", return_value=1):
			found, _, _ = self.side_status()
		self.assertEqual((found["day_lane"], found["refresh"]), ("live", "day+night"))

	def test_on_is_shown_as_the_mode_and_acts_as_shadow(self):
		self.settings = cache_state.CacheSettings("on", (self.company,), True)
		found = cache_status.build_status()
		self.assertEqual((found["mode"], found["effective_mode"]), ("on", "shadow"))

	def test_a_company_is_named_only_when_the_caller_may_read_it(self):
		self.make_head()
		_, entry, _ = self.side_status()
		self.assertEqual(entry["company"], self.company)
		with patch.object(frappe, "has_permission", return_value=False):
			found = cache_status.build_status()
		entry = next(item for item in found["companies"] if item["digest"] == self.digest)
		self.assertNotIn("company", entry)
		self.assertEqual(entry["digest"], self.digest)

	def test_the_status_holds_no_company_name_without_the_permission(self):
		self.make_head()
		with patch.object(frappe, "has_permission", return_value=False):
			text = json.dumps(cache_status.build_status(), default=str)
		self.assertNotIn(self.company, text)

	def test_a_fresh_side_shows_its_age_rows_and_the_last_run(self):
		self.make_head(age=120)
		_sql(
			f"update `tab{HEAD}` set last_rows=7, peak_rss_mb=321, last_duration_ms=4500 where company=%s",
			(self.company,),
		)
		_sql(f"update `tab{GENERATION}` set bytes=999, duration_ms=4500 where state='current'")
		frappe.db.commit()
		_, _, side = self.side_status()
		self.assertEqual(side["state"], "fresh")
		self.assertTrue(115 <= side["age_s"] <= 135)
		self.assertEqual((side["rows"], side["rss_mb"], side["duration_ms"]), (7, 321, 4500))
		self.assertEqual(
			(side["bytes"], side["last_outcome"], side["generation_state"]), (999, "swapped", "current")
		)
		self.assertEqual(side["generations"], {"current": 1})
		self.assertTrue(side["has_generation"])

	def test_states(self):
		for kwargs, state in (
			({"age": 3000}, "due"),
			({"claim": True}, "running"),
			({"memo": True}, "memo"),
		):
			with self.subTest(state=state):
				self._purge_everything()
				self.make_head(**kwargs)
				self.assertEqual(self.side_status()[2]["state"], state)

	def test_a_too_large_backoff_is_its_own_state(self):
		self.make_head(memo=True)
		_sql(f"update `tab{HEAD}` set last_failure='history_too_large', backoff_level=2")
		frappe.db.commit()
		_, _, side = self.side_status()
		self.assertEqual((side["state"], side["backoff_level"]), ("backoff", 2))
		self.assertTrue(side["memo_s"] > 0)

	def test_a_side_with_no_generation_is_due_and_says_so(self):
		held = self.claim()
		cache_store.release_claim(held)
		_, _, side = self.side_status()
		self.assertEqual((side["state"], side["has_generation"], side["age_s"]), ("due", False, None))

	def test_the_failure_class_the_count_and_the_failed_generation_show(self):
		self.make_head()
		_sql(f"update `tab{HEAD}` set last_failure='error:ValueError', failures=2")
		held = self.claim()
		cache_store.fail(held, "error:ValueError")
		frappe.db.commit()
		_, _, side = self.side_status()
		self.assertEqual((side["last_failure"], side["last_outcome"]), ("error:ValueError", "failed"))
		self.assertEqual(side["failure_class"], "error:ValueError")
		self.assertEqual(side["generation_state"], "failed")
		self.assertEqual(side["generations"]["failed"], 1)

	def test_the_next_due_is_the_start_age_less_the_age(self):
		self.make_head(age=100, duration_ms=100_000)
		start = cache.start_age_s(100_000, full_cadence=True)
		_, _, side = self.side_status()
		self.assertTrue(abs(side["next_due_s"] - (start - 100)) <= 15)

	def test_a_side_nobody_opened_has_no_next_due(self):
		self.make_head(age=100, opened=cache.OPEN_WINDOW_S + 60)
		self.assertIsNone(self.side_status()[2]["next_due_s"])

	def test_the_duration_p95_is_taken_over_the_stored_generations(self):
		for _ in range(3):
			self.make_head()
		names = [row[0] for row in _sql(f"select name from `tab{GENERATION}` order by name")]
		for name, ms in zip(names, (100, 200, 300), strict=True):
			_sql(f"update `tab{GENERATION}` set duration_ms=%s where name=%s", (ms, name))
		frappe.db.commit()
		self.assertEqual(self.side_status()[2]["d_p95_ms"], 300)

	def test_a_failed_generation_does_not_count_in_the_duration_p95(self):
		self.make_head()
		_sql(f"update `tab{GENERATION}` set duration_ms=100 where state='current'")
		held = self.claim()
		cache_store.fail(held, "error:ValueError")
		_sql(f"update `tab{GENERATION}` set duration_ms=99999, as_of=now(6) where state='failed'")
		frappe.db.commit()
		self.assertEqual(self.side_status()[2]["d_p95_ms"], 100)

	def test_the_max_age_of_today_shows_only_for_today(self):
		self.make_head()
		_sql(f"update `tab{HEAD}` set max_age_s_today=777, max_age_date=curdate()")
		frappe.db.commit()
		self.assertEqual(self.side_status()[2]["max_age_s_today"], 777)
		_sql(f"update `tab{HEAD}` set max_age_date=curdate() - interval 1 day")
		frappe.db.commit()
		self.assertIsNone(self.side_status()[2]["max_age_s_today"])

	def test_a_listed_company_with_no_head_is_shown_with_no_sides_state(self):
		found = cache_status.build_status()
		entry = next(item for item in found["companies"] if item["digest"] == self.digest)
		self.assertTrue(entry["listed"])
		self.assertEqual(
			{side: item["state"] for side, item in entry["sides"].items()},
			dict.fromkeys(cache_store.SIDES, "due"),
		)

	def test_a_head_of_a_company_off_the_list_is_shown_as_not_listed(self):
		self.make_head()
		self.settings = cache_state.CacheSettings("shadow", (), True)
		_, entry, _ = self.side_status()
		self.assertFalse(entry["listed"])


class TestOracleInStatus(StableCase):
	def record(self, outcome, *, build=None, reason=None, counts=None, source=None):
		fields = {"outcome": outcome, "reason": reason, "counts": counts or {}, "digests": ["d" * 16]}
		if source:
			fields["source"] = source
		text = json.dumps(fields)
		_sql(
			f"update `tab{GENERATION}` set shadow=%s, cache_build=%s where state='current'",
			(text, build or routing.cache_build()),
		)
		frappe.db.commit()

	def oracle(self):
		found = cache_status.build_status()
		entry = next(item for item in found["companies"] if item["digest"] == self.digest)
		return entry["sides"][self.side]["oracle"]

	def test_the_last_outcome_and_the_nights_on_this_build_show(self):
		self.make_head()
		self.record("compared", counts={"rows": 4})
		found = self.oracle()
		self.assertEqual(found["last"], "compared")
		self.assertEqual(found["nights"], {"compared": 1})
		self.assertEqual(found["on_build"], 1)

	def test_an_operator_record_is_not_a_night_on_this_build_but_still_shows_as_the_last_outcome(self):
		self.make_head()
		self.record("compared", source="operator")
		found = self.oracle()
		self.assertEqual((found["nights"], found["on_build"]), ({}, 0))
		self.assertEqual(found["last"], "compared")
		self.assertTrue(cache_status.oracle_records()[0]["operator"])

	def test_a_night_record_is_flagged_as_no_operator_record_and_counts(self):
		self.make_head()
		self.record("compared")
		self.assertFalse(cache_status.oracle_records()[0]["operator"])
		self.assertEqual(self.oracle()["on_build"], 1)

	def test_a_generation_of_another_build_is_not_counted_as_a_night_on_this_build(self):
		self.make_head()
		self.record("compared", build="f" * 64)
		found = self.oracle()
		self.assertEqual((found["last"], found["on_build"]), ("compared", 0))

	def test_a_reason_and_no_digest_come_out(self):
		self.make_head()
		self.record("not_compared", reason="too_many_accounts")
		found = self.oracle()
		self.assertEqual((found["last"], found["reason"]), ("not_compared", "too_many_accounts"))
		self.assertNotIn("d" * 16, json.dumps(cache_status.build_status(), default=str))

	def test_a_side_with_no_record_has_an_empty_oracle(self):
		self.make_head()
		self.assertEqual(self.oracle(), {"last": None, "reason": None, "on_build": 0, "nights": {}})

	def test_a_record_that_is_not_an_object_is_read_as_unreadable_and_never_raises(self):
		self.make_head()
		_sql(f"update `tab{GENERATION}` set shadow='[1]' where state='current'")
		frappe.db.commit()
		self.assertEqual(self.oracle()["last"], "unreadable")


class TestWithinMeasuredSize(TestCase):
	"""E10: the one rule of the size gate, at its limits (the measured playground company)."""

	def test_the_measured_limits_are_the_playground_values(self):
		self.assertEqual(cache_state.MEASURED_LEDGER_LINES, 33_621)
		self.assertEqual(cache_state.MEASURED_STORED_ROWS, 2_962)
		self.assertEqual(cache_state.MEASURED_BANK_GL_ROWS, 20_416)

	def test_a_receivable_or_payable_side_exactly_at_both_limits_is_within(self):
		for side in ("Receivable", "Payable"):
			with self.subTest(side):
				self.assertTrue(cache_state.within_measured_size(side, 33_621, 2_962))

	def test_one_ledger_line_above_the_limit_is_above_on_receivable_and_payable(self):
		for side in ("Receivable", "Payable"):
			with self.subTest(side):
				self.assertFalse(cache_state.within_measured_size(side, 33_622, 0))

	def test_one_stored_row_above_the_limit_is_above_on_receivable_and_payable(self):
		for side in ("Receivable", "Payable"):
			with self.subTest(side):
				self.assertFalse(cache_state.within_measured_size(side, 0, 2_963))

	def test_the_bank_side_is_limited_by_its_gl_rows_alone(self):
		self.assertTrue(cache_state.within_measured_size("Bank", 20_416, 10**6))
		self.assertFalse(cache_state.within_measured_size("Bank", 20_417, 2))

	def test_a_side_with_nothing_counted_is_within(self):
		for side in cache_store.SIDES:
			self.assertTrue(cache_state.within_measured_size(side, 0, 0))


class TestSizeGateInStatus(StableCase):
	"""E10-1, E10-2: a generation above a limit earns no night evidence and the side shows the gate."""

	def set_size(self, ledger_lines, rows):
		_sql(
			f"update `tab{GENERATION}` set ledger_lines=%s, row_count=%s where state='current'",
			(ledger_lines, rows),
		)
		self.record("compared")

	def record(self, outcome):
		_sql(
			f"update `tab{GENERATION}` set shadow=%s, cache_build=%s where state='current'",
			(json.dumps({"outcome": outcome, "counts": {}}), routing.cache_build()),
		)
		frappe.db.commit()

	def side_status(self):
		found = cache_status.build_status()
		entry = next(item for item in found["companies"] if item["digest"] == self.digest)
		return entry["sides"][self.side]

	def assert_counted(self):
		side = self.side_status()
		self.assertEqual((side["oracle"]["on_build"], side["oracle"]["nights"]), (1, {"compared": 1}))
		self.assertEqual(side["size_gate"], "within")

	def assert_withheld(self):
		side = self.side_status()
		self.assertEqual((side["oracle"]["on_build"], side["oracle"]["nights"]), (0, {}))
		self.assertEqual(side["oracle"]["last"], "compared")
		self.assertEqual(side["size_gate"], "above_measured")

	def test_a_generation_within_the_limits_counts_as_a_night(self):
		self.make_head()
		self.set_size(1_000, 100)
		self.assert_counted()

	def test_a_generation_exactly_at_the_limits_counts(self):
		self.make_head()
		self.set_size(33_621, 2_962)
		self.assert_counted()

	def test_a_generation_one_ledger_line_above_the_limit_is_withheld_but_still_the_last(self):
		self.make_head()
		self.set_size(33_622, 100)
		self.assert_withheld()

	def test_a_generation_one_stored_row_above_the_limit_is_withheld_but_still_the_last(self):
		self.make_head()
		self.set_size(1_000, 2_963)
		self.assert_withheld()

	def test_a_bank_generation_exactly_at_its_limit_counts(self):
		self.side = "Bank"
		self.make_head()
		self.set_size(20_416, 2)
		self.assert_counted()

	def test_a_bank_generation_one_gl_row_above_its_limit_is_withheld_but_still_the_last(self):
		self.side = "Bank"
		self.make_head()
		self.set_size(20_417, 2)
		self.assert_withheld()

	def test_a_payable_generation_above_the_limit_is_withheld(self):
		self.side = "Payable"
		self.make_head()
		self.set_size(33_622, 100)
		self.assert_withheld()

	def test_a_side_with_no_generation_shows_within(self):
		self.make_head()
		_sql(f"update `tab{HEAD}` set current_generation=NULL")
		frappe.db.commit()
		self.assertEqual(self.side_status()["size_gate"], "within")

	def test_the_gate_reads_the_current_generation_not_an_older_one(self):
		self.make_head()
		self.set_size(40_000, 100)
		held = self.claim()
		self.publish(held, [_row(remaining=Decimal("5.00"))])
		side = self.side_status()
		self.assertEqual(side["size_gate"], "within")
		self.assertEqual(side["oracle"]["on_build"], 0)
		self.assertEqual(side["oracle"]["last"], "compared")


class TestStatusSections(StableCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(self.drop_stamps)
		self.drop_stamps()

	def drop_stamps(self):
		for name in ("last_tick", "tick_watch"):
			frappe.cache.execute_command("DEL", cache_state.cache_key(name))

	def test_the_status_shows_the_age_of_the_last_tick_and_whether_it_is_late(self):
		found = cache_status.build_status()
		self.assertEqual(found["tick"], {"last_age_s": None, "late": False})
		frappe.cache.execute_command(
			"SET",
			cache_state.cache_key("last_tick"),
			str(int(time.time()) - cache.TICK_LATE_S - 30),
			"EX",
			3600,
		)
		found = cache_status.build_status()["tick"]
		self.assertGreater(found["last_age_s"], cache.TICK_LATE_S)
		self.assertTrue(found["late"])

	def test_the_status_shows_the_janitor_backlog(self):
		self.make_head()
		found = cache_status.build_status()["janitor"]
		self.assertEqual(set(found), {"purged", "retired_with_rows", "oldest_s"})

	def test_a_failed_generation_shows_its_trace(self):
		self.make_head()
		held = self.claim_again()
		cache_store.fail(held, "error:ValueError", "ValueError\n  in somewhere")
		_, _, side = self.side_status()
		self.assertTrue(side["failure_trace"].startswith("ValueError"))

	def claim_again(self):
		self.expire_claim()
		return self.claim()

	def side_status(self):
		digest = read_log.company_digest(self.company)
		found = cache_status.build_status()
		entry = next(item for item in found["companies"] if item["digest"] == digest)
		return found, entry, entry["sides"][self.side]


class TestSkipCounters(StableCase):
	def setUp(self):
		super().setUp()
		self.start(patch.object(routing, "today", return_value=FAR_DAY))
		self.addCleanup(self.clear)
		self.clear()

	def clear(self):
		keys = frappe.cache.get_keys(f"jarvis:pcal:site:day:{FAR_DAY}:")
		frappe.cache.delete_value(keys, make_keys=False)

	def test_each_skip_reason_has_a_day_count(self):
		for reason in ("no_lane", "no_lane", "off", "not_buildable:query_hook"):
			cache.count_skip(reason)
		self.assertEqual(
			cache_status.skip_counts(FAR_DAY), {"no_lane": 2, "off": 1, "not_buildable:query_hook": 1}
		)

	def test_the_counters_are_found_without_the_blocking_keys_command(self):
		cache.count_skip("off")
		with patch.object(frappe.cache, "keys", side_effect=AssertionError("KEYS")):
			self.assertEqual(cache_status.skip_counts(FAR_DAY), {"off": 1})

	def test_other_site_counters_are_not_skips(self):
		read_log.site_count("bank_today:something", FAR_DAY)
		self.assertEqual(cache_status.skip_counts(FAR_DAY), {})

	def test_the_status_shows_today_and_yesterday(self):
		cache.count_skip("no_slot")
		found = cache_status.build_status()
		self.assertEqual(found["skips"][FAR_DAY], {"no_slot": 1})
		self.assertIn(frappe.utils.add_days(FAR_DAY, -1), found["skips"])

	def test_a_cache_that_is_down_gives_no_counts(self):
		with patch.object(frappe.cache, "get_keys", side_effect=ConnectionError()):
			self.assertEqual(cache_status.skip_counts(FAR_DAY), {})


class TestSections(StableCase):
	def sections(self, *paths):
		with with_hooks(*paths):
			return cache_status.build_status()["sections"]

	def test_no_hook_gives_no_sections(self):
		self.assertEqual(cache_status.build_status()["sections"], {})

	def test_a_hook_adds_its_sections(self):
		path = f"{__name__}.section_ok"
		self.assertEqual(self.sections(path), {"card": {"served": 3}})

	def test_a_failing_or_malformed_hook_is_logged_by_class_and_skipped(self):
		logger = MagicMock()
		ok = f"{__name__}.section_ok"
		with patch.object(frappe, "logger", return_value=logger):
			found = self.sections(f"{__name__}.section_bad", f"{__name__}.section_not_a_dict", ok)
		self.assertEqual(found, {"card": {"served": 3}})
		self.assertIn("ValueError", str(logger.method_calls))
		self.assertNotIn("Acme", str(logger.method_calls))

	def test_the_hook_is_declared_empty_for_other_modules_to_fill(self):
		self.assertEqual(frappe._load_app_hooks("jarvis").get(cache_status.SECTIONS_HOOK), [])


class TestPermission(StableCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_administrator_gets_the_status(self):
		self.assertIn("companies", cache_status.cache_status())

	def test_a_guest_is_refused(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			cache_status.cache_status()

	def test_a_user_without_system_manager_is_refused(self):
		users = frappe.get_all(
			"User", filters={"enabled": 1, "name": ["not in", ["Administrator", "Guest"]]}, pluck="name"
		)
		plain = next((name for name in users if "System Manager" not in frappe.get_roles(name)), None)
		if plain is None:
			self.skipTest("the site has no user without System Manager")
		frappe.set_user(plain)
		with self.assertRaises(frappe.PermissionError):
			cache_status.cache_status()

	def test_the_function_is_whitelisted_for_post(self):
		self.assertIn(cache_status.cache_status, frappe.whitelisted)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[cache_status.cache_status], ["POST"]
		)
