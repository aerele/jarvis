"""Rollout signals script for inline report snapshots: S4, S5 and the abort thresholds.

Pure log parsing over synthetic lines in a temp dir. No database, no Redis writes.
"""

import inspect
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from jarvis.scripts import report_snapshot_signals as signals

NOW = datetime(2026, 11, 18, 12, 0, 0)
SITE = "test_jarvis"


def _line(event, outcome, minutes_ago=1, site=SITE, **fields):
	ts = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S.%f")
	record = {
		"kind": "report_snapshot",
		"event": event,
		"outcome": outcome,
		"ts": ts,
		"site": site,
		"conversation": fields.pop("conversation", "conv-1"),
		"snapshot_hash": fields.pop("snapshot_hash", "aaaaaaaaaaaa"),
		"generation": fields.pop("generation", 1),
		**fields,
	}
	return json.dumps(record)


def _compute(lines, now=None):
	records, malformed = signals.parse_records(lines)
	assert malformed == 0
	return signals.compute(records, now=now)


def _reloads(count, outcome="ok", reload_ms=1000, **fields):
	"""``count`` reload_end lines, each on its own snapshot so none is de-duplicated."""
	return [
		_line("reload_end", outcome, snapshot_hash=f"h{outcome}{i:04d}", reload_ms=reload_ms, **fields)
		for i in range(count)
	]


class TestS4(unittest.TestCase):
	def test_matching_read_after_reload_counts(self):
		result = _compute(
			[
				_line("reload_end", "ok", reload_ms=500),
				_line("read", "live", sql_executed=True),
			]
		)
		self.assertEqual(result["s4"], 1.0)

	def test_read_with_other_generation_does_not_count(self):
		result = _compute(
			[
				_line("reload_end", "ok", generation=2, reload_ms=500),
				_line("read", "live", generation=1, sql_executed=True),
			]
		)
		self.assertEqual(result["s4"], 0.0)

	def test_read_from_other_conversation_does_not_count(self):
		result = _compute(
			[
				_line("reload_end", "ok", reload_ms=500),
				_line("read", "live", conversation="conv-2", sql_executed=True),
			]
		)
		self.assertEqual(result["s4"], 0.0)

	def test_read_with_other_snapshot_does_not_count(self):
		result = _compute(
			[
				_line("reload_end", "ok", reload_ms=500),
				_line("read", "live", snapshot_hash="bbbbbbbbbbbb", sql_executed=True),
			]
		)
		self.assertEqual(result["s4"], 0.0)

	def test_read_without_sql_does_not_count(self):
		result = _compute(
			[
				_line("reload_end", "ok", reload_ms=500),
				_line("read", "live", sql_executed=False),
			]
		)
		self.assertEqual(result["s4"], 0.0)

	def test_read_before_reload_does_not_count(self):
		result = _compute(
			[
				_line("read", "live", sql_executed=True),
				_line("reload_end", "ok", reload_ms=500),
			]
		)
		self.assertEqual(result["s4"], 0.0)

	def test_share_over_ok_reloads_only(self):
		result = _compute(
			[
				_line("reload_end", "ok", snapshot_hash="s1", reload_ms=500),
				_line("read", "live", snapshot_hash="s1", sql_executed=True),
				_line("reload_end", "ok", snapshot_hash="s2", reload_ms=500),
				_line("reload_end", "failed:TimeoutError", snapshot_hash="s3", reload_ms=500),
			]
		)
		self.assertEqual(result["s4"], 0.5)

	def test_one_hundred_thousand_records_finish_quickly(self):
		records = []
		for i in range(25_000):
			key = {"conversation": f"c{i}", "snapshot_hash": f"h{i}", "generation": 1}
			records.append({"event": "reload_end", "outcome": "ok", "reload_ms": 10, **key})
			records.append({"event": "read", "outcome": "live", "sql_executed": False, **key})
			records.append({"event": "read", "outcome": "live", "sql_executed": i % 2 == 0, **key})
			records.append({"event": "read", "outcome": "live", "sql_executed": False, **key})
		started = time.monotonic()
		result = signals.compute(records)
		self.assertLess(time.monotonic() - started, 2)
		self.assertEqual(result["s4"], 0.5)

	def test_no_ok_reload_gives_none(self):
		self.assertIsNone(_compute([_line("read", "live", sql_executed=True)])["s4"])


class TestSignals(unittest.TestCase):
	def test_p95_is_nearest_rank_over_ran_reloads(self):
		lines = [_line("reload_end", "ok", snapshot_hash=f"h{i}", reload_ms=(i + 1) * 100) for i in range(20)]
		lines.append(_line("reload_end", "capped", snapshot_hash="cap", reload_ms=1))
		lines.append(_line("reload_end", "disabled", snapshot_hash="dis", reload_ms=1))
		# nearest rank over the 20 ok reloads: rank 19 of 100..2000 ms
		self.assertEqual(_compute(lines)["s5_p95_ms"], 1900)

	def test_p95_is_none_without_reloads(self):
		self.assertIsNone(_compute([_line("read", "live", sql_executed=False)])["s5_p95_ms"])

	def test_busy_counts_once_per_snapshot_and_generation(self):
		lines = [
			_line("reload_end", "busy", reload_ms=0),
			_line("reload_end", "busy", reload_ms=0),
			_line("reload_end", "busy", reload_ms=0),
			_line("reload_end", "busy", generation=2, reload_ms=0),
			_line("reload_end", "busy", snapshot_hash="other", reload_ms=0),
			_line("reload_end", "ok", reload_ms=100),
		]
		result = _compute(lines)
		self.assertEqual(result["busy_unique"], 3)
		self.assertEqual(result["reload_outcomes"], {"busy": 3, "ok": 1})
		self.assertEqual(result["reloads"], 4)

	def test_capped_or_busy_share_counts_capped_plus_deduped_busy(self):
		lines = [_line("reload_end", "busy", reload_ms=0) for _ in range(5)]  # one unique busy
		lines += _reloads(2, "capped") + _reloads(1, "ok")
		result = _compute(lines)
		self.assertEqual(result["reloads"], 4)
		self.assertEqual(result["capped_or_busy_share"], 0.75)  # (2 capped + 1 busy) / 4

	def test_denominator_is_ok_refused_failed_capped_and_busy(self):
		lines = (
			_reloads(1, "ok")
			+ _reloads(1, "refused:permission")
			+ _reloads(1, "failed:TimeoutError")
			+ _reloads(1, "capped")
			+ _reloads(1, "busy")
		)
		result = _compute(lines)
		self.assertEqual(result["reloads"], 5)
		self.assertEqual(result["capped_or_busy_share"], 0.4)

	def test_disabled_and_not_agent_path_are_excluded_everywhere(self):
		lines = _reloads(1, "ok") + _reloads(1, "capped")
		lines += _reloads(10, "disabled") + _reloads(10, "not_agent_path")
		result = _compute(lines)
		self.assertEqual(result["reloads"], 2)
		self.assertEqual(result["capped_or_busy_share"], 0.5)
		self.assertNotIn("disabled", result["reload_outcomes"])
		self.assertEqual(result["reload_excluded"], {"disabled": 10, "not_agent_path": 10})

	def test_evicted_share_of_all_reads(self):
		lines = [_line("read", "evicted", sql_executed=False)] + [
			_line("read", "live", sql_executed=False) for _ in range(3)
		]
		result = _compute(lines)
		self.assertEqual(result["evicted_share"], 0.25)
		self.assertEqual(result["read_outcomes"], {"evicted": 1, "live": 3})
		self.assertEqual(result["reads"], 4)

	def test_evicted_share_is_none_without_reads(self):
		self.assertIsNone(_compute(_reloads(1))["evicted_share"])

	def test_refused_prepared_is_counted_by_its_full_outcome(self):
		lines = _reloads(1, "refused:prepared") + _reloads(2, "refused:permission")
		result = _compute(lines)
		self.assertEqual(result["refused_prepared"], 1)
		self.assertEqual(result["reload_outcomes"]["refused:permission"], 2)


class TestAbort(unittest.TestCase):
	def _healthy(self):
		"""20 ok reloads, each followed by a SQL read; fast; plenty of live reads."""
		lines = []
		for i in range(20):
			lines.append(_line("reload_end", "ok", snapshot_hash=f"h{i}", reload_ms=2000))
			lines.append(_line("read", "live", snapshot_hash=f"h{i}", sql_executed=True))
		return lines

	def test_healthy_sample_has_no_abort(self):
		result = _compute(self._healthy())
		self.assertFalse(result["insufficient_sample"])
		self.assertEqual(result["abort"], [])

	def test_insufficient_sample_below_twenty_reloads(self):
		# slow, unfollowed and refused:prepared would all abort at 20 reloads
		lines = _reloads(18, "ok", reload_ms=20000) + _reloads(1, "refused:prepared")
		result = _compute(lines)
		self.assertEqual(result["reloads"], 19)
		self.assertTrue(result["insufficient_sample"])
		self.assertEqual(result["abort"], [])

	def test_twenty_reloads_is_enough(self):
		self.assertFalse(_compute(self._healthy())["insufficient_sample"])

	def test_disabled_and_not_agent_path_do_not_reach_the_minimum(self):
		lines = self._healthy()[2:] + _reloads(5, "disabled") + _reloads(5, "not_agent_path")
		result = _compute(lines)  # 19 counted reloads
		self.assertEqual(result["reloads"], 19)
		self.assertTrue(result["insufficient_sample"])

	def test_busy_duplicates_do_not_reach_the_minimum(self):
		lines = self._healthy()[4:] + [_line("reload_end", "busy", reload_ms=0) for _ in range(10)]
		self.assertTrue(_compute(lines)["insufficient_sample"])

	def test_s5_over_twelve_seconds_aborts(self):
		lines = self._healthy() + _reloads(2, "ok", reload_ms=13000)
		abort = _compute(lines)["abort"]
		self.assertEqual(len(abort), 1)
		self.assertIn("S5", abort[0])

	def test_s5_at_twelve_seconds_does_not_abort(self):
		lines = [_line("reload_end", "ok", snapshot_hash=f"h{i}", reload_ms=12000) for i in range(20)] + [
			_line("read", "live", snapshot_hash=f"h{i}", sql_executed=True) for i in range(20)
		]
		self.assertEqual(_compute(lines)["abort"], [])

	def test_capped_over_ten_percent_aborts(self):
		lines = self._healthy() + _reloads(3, "capped")  # 3 of 23 = 13 %
		abort = _compute(lines)["abort"]
		self.assertEqual(len(abort), 1)
		self.assertIn("capped or busy", abort[0])

	def test_capped_exactly_ten_percent_does_not_abort(self):
		lines = self._healthy()[:36] + _reloads(2, "capped")  # 18 ok + 2 capped = 10 %
		result = _compute(lines)
		self.assertEqual(result["capped_or_busy_share"], 0.1)
		self.assertEqual(result["abort"], [])

	def test_busy_counts_toward_the_threshold(self):
		lines = self._healthy()[:34] + _reloads(3, "busy")  # 17 ok + 3 busy = 15 %
		abort = _compute(lines)["abort"]
		self.assertEqual(len(abort), 1)
		self.assertIn("capped or busy", abort[0])

	def test_repeated_busy_does_not_breach_the_threshold(self):
		lines = self._healthy() + [_line("reload_end", "busy", reload_ms=0) for _ in range(50)]
		result = _compute(lines)  # 20 ok + 1 unique busy = 4.8 %
		self.assertEqual(result["abort"], [])

	def test_s4_below_ninety_percent_aborts(self):
		lines = self._healthy()
		del lines[1]  # first reload loses its read: 19 / 20 = 95 %, still fine
		self.assertEqual(_compute(lines)["abort"], [])
		del lines[2]  # second loses its read too: 18 / 20 = 90 %, the edge, not below
		self.assertEqual(_compute(lines)["abort"], [])
		del lines[3]  # 17 / 20 = 85 %
		abort = _compute(lines)["abort"]
		self.assertEqual(len(abort), 1)
		self.assertIn("S4", abort[0])

	def test_refused_prepared_aborts(self):
		lines = self._healthy() + _reloads(1, "refused:prepared", reload_ms=100)
		abort = _compute(lines)["abort"]
		self.assertTrue(any("refused:prepared" in reason for reason in abort))

	def test_other_refusals_do_not_abort(self):
		lines = self._healthy() + _reloads(1, "refused:permission", reload_ms=100)
		self.assertEqual(_compute(lines)["abort"], [])

	def test_evicted_over_five_percent_aborts(self):
		lines = self._healthy() + [_line("read", "evicted", sql_executed=False) for _ in range(2)]
		# 2 of 22 reads = 9 %
		abort = _compute(lines)["abort"]
		self.assertEqual(len(abort), 1)
		self.assertIn("evicted", abort[0])

	def test_evicted_exactly_five_percent_does_not_abort(self):
		lines = self._healthy() + [_line("read", "live", sql_executed=False) for _ in range(18)]
		lines += [_line("read", "evicted", sql_executed=False) for _ in range(2)]  # 2 of 40
		result = _compute(lines)
		self.assertEqual(result["evicted_share"], 0.05)
		self.assertEqual(result["abort"], [])


class TestFailureSignals(unittest.TestCase):
	def _pairs(self, count):
		lines = []
		for i in range(count):
			lines.append(_line("reload_end", "ok", snapshot_hash=f"h{i}", reload_ms=2000))
			lines.append(_line("read", "live", snapshot_hash=f"h{i}", sql_executed=True))
		return lines

	def test_undefined_s4_aborts_with_twenty_reloads(self):
		result = _compute(_reloads(20, "refused:permission"))
		self.assertIsNone(result["s4"])
		self.assertEqual(len(result["abort"]), 1)
		self.assertIn("S4", result["abort"][0])

	def test_undefined_s4_below_the_sample_does_not_abort(self):
		self.assertEqual(_compute(_reloads(19, "refused:permission"))["abort"], [])

	def test_ok_and_failed_shares_and_classes(self):
		lines = self._pairs(6) + _reloads(2, "failed:TimeoutError") + _reloads(1, "failed:RuntimeError")
		lines += _reloads(1, "refused:permission")
		result = _compute(lines)  # 10 reloads
		self.assertEqual(result["ok_share"], 0.6)
		self.assertEqual(result["failed_share"], 0.3)
		self.assertEqual(result["failed_classes"], {"failed:TimeoutError": 2, "failed:RuntimeError": 1})

	def test_failed_over_twenty_percent_aborts_and_names_the_classes(self):
		lines = self._pairs(15) + _reloads(5, "failed:TimeoutError")  # 25 %
		abort = _compute(lines)["abort"]
		self.assertEqual(len(abort), 1)
		self.assertIn("failed", abort[0])
		self.assertIn("failed:TimeoutError", abort[0])

	def test_failed_exactly_twenty_percent_does_not_abort(self):
		lines = self._pairs(16) + _reloads(4, "failed:TimeoutError")
		result = _compute(lines)
		self.assertEqual(result["failed_share"], 0.2)
		self.assertEqual(result["abort"], [])

	def test_failed_share_is_none_without_reloads(self):
		result = _compute([_line("read", "live", sql_executed=False)])
		self.assertIsNone(result["failed_share"])
		self.assertIsNone(result["ok_share"])


class TestUnmatchedStarts(unittest.TestCase):
	def _start(self, **fields):
		return _line("reload_start", "started", **{"reload_ms": 0, **fields})

	def test_start_with_its_end_is_matched(self):
		lines = [self._start(generation=1), _line("reload_end", "ok", generation=2, reload_ms=900)]
		self.assertEqual(_compute(lines)["unmatched_reload_starts"], 0)

	def test_a_non_ok_end_matches_at_the_same_generation_only(self):
		for outcome in ("failed:QueryDeadlockError", "refused:fefo", "refused:gone"):
			with self.subTest(outcome):
				same = [self._start(generation=1), _line("reload_end", outcome, generation=1, reload_ms=9)]
				higher = [self._start(generation=1), _line("reload_end", outcome, generation=2, reload_ms=9)]
				self.assertEqual(_compute(same)["unmatched_reload_starts"], 0)
				self.assertEqual(_compute(higher)["unmatched_reload_starts"], 1)

	def test_an_ok_end_at_the_start_generation_does_not_match(self):
		lines = [self._start(generation=1), _line("reload_end", "ok", generation=1, reload_ms=900)]
		self.assertEqual(_compute(lines)["unmatched_reload_starts"], 1)

	def test_start_without_an_end_is_counted(self):
		self.assertEqual(_compute([self._start()])["unmatched_reload_starts"], 1)

	def test_end_of_another_generation_does_not_match(self):
		lines = [self._start(generation=1), _line("reload_end", "ok", generation=3, reload_ms=900)]
		self.assertEqual(_compute(lines)["unmatched_reload_starts"], 1)

	def test_end_of_another_snapshot_or_conversation_does_not_match(self):
		lines = [
			self._start(),
			_line("reload_end", "ok", snapshot_hash="other", generation=2, reload_ms=900),
			_line("reload_end", "ok", conversation="conv-2", generation=2, reload_ms=900),
		]
		self.assertEqual(_compute(lines)["unmatched_reload_starts"], 1)

	def test_end_before_the_start_does_not_match(self):
		lines = [_line("reload_end", "ok", generation=2, reload_ms=900), self._start()]
		self.assertEqual(_compute(lines)["unmatched_reload_starts"], 1)

	def test_busy_capped_and_lock_stale_ends_never_match_a_start(self):
		for outcome in ("busy", "capped", "refused:lock_stale"):
			with self.subTest(outcome):
				lines = [self._start(), _line("reload_end", outcome, reload_ms=0)]
				self.assertEqual(_compute(lines)["unmatched_reload_starts"], 1)

	def test_a_start_in_the_last_minute_is_still_in_flight(self):
		lines = [self._start(minutes_ago=0.5), self._start(minutes_ago=10, snapshot_hash="old")]
		self.assertEqual(_compute(lines, now=NOW)["unmatched_reload_starts"], 1)
		self.assertEqual(_compute(lines)["unmatched_reload_starts"], 2)

	def _healthy_with_starts(self):
		lines = []
		for i in range(20):
			lines.append(self._start(snapshot_hash=f"h{i}", generation=0))
			lines.append(_line("reload_end", "ok", snapshot_hash=f"h{i}", generation=1, reload_ms=2000))
			lines.append(_line("read", "live", snapshot_hash=f"h{i}", generation=1, sql_executed=True))
		return lines

	def test_up_to_five_percent_killed_does_not_abort(self):
		lines = self._healthy_with_starts() + [self._start(snapshot_hash="k0")]  # 1 of 21
		result = _compute(lines)
		self.assertEqual(result["unmatched_reload_starts"], 1)
		self.assertEqual(result["abort"], [])

	def test_over_five_percent_killed_aborts(self):
		lines = self._healthy_with_starts() + [self._start(snapshot_hash=f"k{i}") for i in range(2)]
		abort = _compute(lines)["abort"]  # 2 of 22 = 9 %
		self.assertEqual(len(abort), 1)
		self.assertIn("killed", abort[0])

	def test_unmatched_starts_do_not_join_the_s5_sample(self):
		lines = [self._start(reload_ms=99999)] + [
			_line("reload_end", "ok", snapshot_hash=f"h{i}", reload_ms=100) for i in range(3)
		]
		self.assertEqual(_compute(lines)["s5_p95_ms"], 100)


class TestPreLockRefusals(unittest.TestCase):
	def test_refused_slow_is_excluded_from_reloads_and_s5(self):
		lines = _reloads(1, "ok", reload_ms=100) + _reloads(5, "refused:slow", reload_ms=99999)
		result = _compute(lines)
		self.assertEqual(result["reloads"], 1)
		self.assertEqual(result["s5_p95_ms"], 100)
		self.assertNotIn("refused:slow", result["reload_outcomes"])
		self.assertEqual(result["reload_excluded"], {"refused:slow": 5})

	def test_refused_slow_does_not_reach_the_minimum(self):
		lines = _reloads(19, "ok") + _reloads(5, "refused:slow")
		self.assertTrue(_compute(lines)["insufficient_sample"])


class TestEvicted(unittest.TestCase):
	def test_evicted_field_on_a_read_counts_like_the_outcome(self):
		lines = [
			_line("read", "live", sql_executed=False, evicted=True),
			_line("read", "evicted", sql_executed=False),
			_line("read", "live", sql_executed=False),
			_line("read", "live", sql_executed=False, evicted=False),
		]
		result = _compute(lines)
		self.assertEqual(result["evicted_share"], 0.5)

	def test_evicted_aborts_a_small_reload_sample_with_enough_reads(self):
		lines = _reloads(5, "ok") + [_line("read", "live", sql_executed=False) for _ in range(35)]
		lines += [_line("read", "evicted", sql_executed=False) for _ in range(5)]  # 5 of 40 reads
		result = _compute(lines)
		self.assertTrue(result["insufficient_sample"])
		self.assertEqual(len(result["abort"]), 1)
		self.assertIn("evicted", result["abort"][0])

	def test_evicted_does_not_abort_with_fewer_than_twenty_reads(self):
		lines = [_line("read", "evicted", sql_executed=False) for _ in range(10)]
		self.assertEqual(_compute(lines)["abort"], [])

	def test_evicted_exactly_five_percent_of_a_small_sample_does_not_abort(self):
		lines = [_line("read", "live", sql_executed=False) for _ in range(38)]
		lines += [_line("read", "evicted", sql_executed=False) for _ in range(2)]
		self.assertEqual(_compute(lines)["abort"], [])


class TestRobustness(unittest.TestCase):
	def test_offset_aware_timestamps_do_not_crash(self):
		aware = json.dumps(
			{"kind": "report_snapshot", "event": "read", "outcome": "live", "ts": "2026-11-18T11:59:00+05:30"}
		)
		naive = _line("read", "live", sql_executed=False)
		span = {}
		records, malformed = signals.parse_records([naive, aware], cutoff=NOW - timedelta(days=1), span=span)
		self.assertEqual((len(records), malformed), (2, 0))
		self.assertIsNone(records[1]["_ts"].tzinfo)
		self.assertIsNone(span["newest"].tzinfo)

	def test_unhashable_values_do_not_crash(self):
		odd = {"conversation": ["a"], "snapshot_hash": {"x": 1}, "generation": [1, 2]}
		lines = [
			_line("reload_start", "started", reload_ms=0, **odd),
			_line("reload_end", "ok", reload_ms=5, **odd),
			_line("reload_end", "busy", reload_ms=0, **odd),
			_line("reload_end", "busy", reload_ms=0, **odd),
			_line("read", "live", sql_executed=True, **odd),
		]
		result = _compute(lines)
		self.assertEqual(result["s4"], 1.0)
		self.assertEqual(result["busy_unique"], 1)
		self.assertEqual(result["unmatched_reload_starts"], 0)

	def test_unhashable_site_does_not_crash_evaluate(self):
		line = _line("read", "live", sql_executed=False, site=["a"])
		records, _ = signals.parse_records([line])
		self.assertEqual(list(signals.evaluate(records, SITE)["sites"]), ["['a']"])


class TestParsing(unittest.TestCase):
	def test_malformed_lines_are_skipped_and_counted(self):
		good = _line("read", "live", sql_executed=False)
		lines = [
			good,
			"not json at all",
			'2026-11-18 11:59:00,000 INFO jarvis.report_snapshot {"kind": "report_snapshot", "ev',
			'{"kind": "report_snapshot", "event": "read", "outcome": "live"}',  # no ts
			"[1, 2, 3]",
			"\n",
			'{"kind": "tool", "ts": "2026-11-18 11:59:00"}',  # another kind: ignored, not malformed
		]
		records, malformed = signals.parse_records(lines)
		self.assertEqual(len(records), 1)
		self.assertEqual(malformed, 4)

	def test_logger_prefix_before_the_json_is_accepted(self):
		line = "2026-11-18 11:59:00,000 INFO jarvis.report_snapshot " + _line("read", "live")
		records, malformed = signals.parse_records([line])
		self.assertEqual((len(records), malformed), (1, 0))

	def test_days_window_drops_older_records_while_streaming(self):
		lines = [
			_line("read", "live", minutes_ago=60),
			_line("read", "live", minutes_ago=60 * 24 * 6),
			_line("read", "live", minutes_ago=60 * 24 * 8),
		]
		records, _ = signals.parse_records(iter(lines), cutoff=NOW - timedelta(days=7))
		self.assertEqual(len(records), 2)
		records, _ = signals.parse_records(iter(lines), cutoff=NOW - timedelta(days=1))
		self.assertEqual(len(records), 1)

	def test_span_covers_all_records_even_those_before_the_cutoff(self):
		lines = [
			_line("read", "live", minutes_ago=60 * 24 * 8),
			_line("read", "live", minutes_ago=60),
		]
		span = {}
		signals.parse_records(lines, cutoff=NOW - timedelta(days=1), span=span)
		self.assertEqual(span["newest"], NOW - timedelta(minutes=60))
		self.assertEqual(span["oldest"], NOW - timedelta(minutes=60 * 24 * 8))


class TestFiles(unittest.TestCase):
	def setUp(self):
		self.bench = tempfile.TemporaryDirectory()
		self.addCleanup(self.bench.cleanup)

	def _write(self, relative, lines):
		path = os.path.join(self.bench.name, relative)
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, "w", encoding="utf-8") as handle:
			handle.write("\n".join(lines) + "\n")
		return path

	def _bench_log(self, suffix=""):
		return f"logs/jarvis.report_snapshot.log{suffix}"

	def _site_log(self, suffix=""):
		return f"sites/{SITE}/logs/jarvis.report_snapshot.log{suffix}"

	def _paths(self):
		return signals._log_file_paths(SITE, self.bench.name)

	def test_rotated_files_are_read_oldest_first(self):
		self._write(self._bench_log(), ["current"])
		self._write(self._bench_log(".1"), ["newer backup"])
		self._write(self._bench_log(".2"), ["oldest backup"])
		self.assertEqual(
			list(signals._read_lines(self._paths())),
			["oldest backup\n", "newer backup\n", "current\n"],
		)

	def test_bench_log_is_preferred_over_site_log(self):
		self._write(self._bench_log(), ["bench"])
		self._write(self._site_log(), ["site"])
		self.assertEqual(list(signals._read_lines(self._paths())), ["bench\n"])

	def test_site_log_is_the_fallback(self):
		self._write(self._site_log(), ["site"])
		self.assertEqual(list(signals._read_lines(self._paths())), ["site\n"])

	def test_only_log_and_numbered_rotations_match(self):
		self._write(self._bench_log(), ["current"])
		self._write(self._bench_log(".1"), ["backup"])
		self._write(self._bench_log(".gz"), ["no"])
		self._write(self._bench_log(".1.gz"), ["no"])
		self._write(self._bench_log(".bak"), ["no"])
		names = [os.path.basename(path) for path in self._paths()]
		self.assertEqual(names, ["jarvis.report_snapshot.log.1", "jarvis.report_snapshot.log"])

	def test_missing_logs_give_no_lines(self):
		self.assertEqual(self._paths(), [])
		self.assertEqual(list(signals._read_lines(self._paths())), [])

	def test_read_lines_is_a_generator(self):
		self.assertTrue(inspect.isgenerator(signals._read_lines([])))

	def _run(self, **kwargs):
		cache = MagicMock()
		cache.info.side_effect = lambda section: {
			"stats": {"evicted_keys": 4},
			"server": {"uptime_in_seconds": 99},
		}[section]
		with (
			patch("frappe.utils.get_bench_path", return_value=self.bench.name),
			patch("frappe.cache", return_value=cache),
			patch("frappe.utils.now_datetime", return_value=NOW),
			patch("builtins.print") as printed,
		):
			result = signals.run(site=SITE, **kwargs)
		printed.assert_not_called()  # bench execute prints the return value
		return result

	def test_run_reads_the_bench_wide_log_groups_by_site_and_reports_files(self):
		self._write(
			self._bench_log(".1"),
			[
				_line("reload_end", "ok", minutes_ago=3000, reload_ms=700),
				_line("read", "live", minutes_ago=2999, sql_executed=True),
				"garbage",
			],
		)
		self._write(
			self._bench_log(),
			[
				_line("read", "evicted", sql_executed=False),
				_line("read", "live", site="other_site", sql_executed=False),
				_line("read", "live", minutes_ago=60 * 24 * 30, sql_executed=False),  # outside window
			],
		)
		result = self._run(days=7)
		self.assertEqual(result["malformed_lines"], 1)
		self.assertEqual(result["lines"], 6)
		self.assertEqual(len(result["log_files"]), 2)
		self.assertEqual(result["warnings"], [])
		self.assertFalse(result["window_truncated"])
		self.assertEqual(sorted(result["sites"]), ["other_site", SITE])
		self.assertEqual(result["sites"][SITE]["reads"], 2)
		self.assertEqual(result["sites"][SITE]["s4"], 1.0)
		self.assertEqual(result["sites"]["other_site"]["reads"], 1)
		self.assertEqual(
			result["redis"],
			{
				"evicted_keys": 4,
				"uptime_in_seconds": 99,
				"note": "server-wide counters, manual check only",
			},
		)

	def test_run_warns_when_no_log_file_exists(self):
		result = self._run()
		self.assertEqual(result["log_files"], [])
		self.assertEqual(result["lines"], 0)
		self.assertTrue(any("FRAPPE_STREAM_LOGGING" in warning for warning in result["warnings"]))

	def test_run_accepts_explicit_log_paths(self):
		path = self._write("custom/snap.log", [_line("read", "live", sql_executed=False)])
		result = self._run(log_paths=[path])
		self.assertEqual(result["log_files"], [path])
		self.assertEqual(result["sites"][SITE]["reads"], 1)

	def test_run_uses_site_time_for_the_window(self):
		# 6.5 days before NOW is inside 7 days; with a now 2 days later it falls outside.
		path = self._write("custom/snap.log", [_line("read", "live", minutes_ago=60 * 24 * 6.5)])
		self.assertEqual(self._run(log_paths=[path])["pooled"]["reads"], 1)
		with (
			patch("frappe.utils.now_datetime", return_value=NOW + timedelta(days=2)),
			patch("frappe.cache", side_effect=RuntimeError),
		):
			late = signals.run(site=SITE, log_paths=[path])
		self.assertEqual(late["sites"], {})

	def test_empty_log_warns(self):
		path = self._write("custom/snap.log", [])
		result = self._run(log_paths=[path])
		self.assertTrue(any("no valid" in warning for warning in result["warnings"]))

	def test_all_malformed_log_warns(self):
		path = self._write("custom/snap.log", ["garbage", "{broken"])
		result = self._run(log_paths=[path])
		self.assertEqual(result["malformed_lines"], 2)
		self.assertTrue(any("no valid" in warning for warning in result["warnings"]))

	def test_newest_record_older_than_a_day_warns(self):
		path = self._write("custom/snap.log", [_line("read", "live", minutes_ago=60 * 36)])
		warnings = self._run(log_paths=[path])["warnings"]
		self.assertTrue(any("older than a day" in warning for warning in warnings))

	def test_recent_record_does_not_warn_about_age(self):
		path = self._write("custom/snap.log", [_line("read", "live", minutes_ago=60 * 23)])
		self.assertEqual(self._run(log_paths=[path])["warnings"], [])

	def _rotated(self, count, oldest_minutes_ago):
		"""``count`` log files; the oldest holds a record ``oldest_minutes_ago`` old."""
		paths = []
		for index in range(count):
			minutes = oldest_minutes_ago if index == 0 else 5
			paths.append(
				self._write(f"custom/snap.log.{count - index}", [_line("read", "live", minutes_ago=minutes)])
			)
		return paths

	def test_rotation_at_the_backup_limit_truncates_the_window(self):
		paths = self._rotated(signals.LOG_FILE_COUNT, 60 * 24 * 3)  # oldest is 3 days old, window 7
		result = self._run(log_paths=paths, days=7)
		self.assertTrue(result["window_truncated"])
		self.assertTrue(any("rotation" in warning for warning in result["warnings"]))

	def test_few_files_do_not_truncate_the_window(self):
		paths = self._rotated(signals.LOG_FILE_COUNT - 1, 60 * 24 * 3)
		result = self._run(log_paths=paths, days=7)
		self.assertFalse(result["window_truncated"])

	def test_log_that_reaches_the_window_start_is_not_truncated(self):
		paths = self._rotated(signals.LOG_FILE_COUNT, 60 * 24 * 8)
		result = self._run(log_paths=paths, days=7)
		self.assertFalse(result["window_truncated"])

	def test_redis_failure_is_reported_not_raised(self):
		with patch("frappe.cache", side_effect=RuntimeError("down")):
			self.assertEqual(signals.redis_info(), {"error": "RuntimeError"})


class TestPooling(unittest.TestCase):
	def _site_lines(self, site, ok, capped):
		"""``ok`` healthy reload pairs and ``capped`` capped reloads for ``site``."""
		lines = []
		for i in range(ok):
			lines.append(_line("reload_end", "ok", site=site, snapshot_hash=f"{site}ok{i}", reload_ms=100))
			lines.append(_line("read", "live", site=site, snapshot_hash=f"{site}ok{i}", sql_executed=True))
		for i in range(capped):
			lines.append(
				_line("reload_end", "capped", site=site, snapshot_hash=f"{site}cap{i}", reload_ms=100)
			)
		return lines

	def _evaluate(self, *site_lines):
		lines = [line for group in site_lines for line in group]
		records, _ = signals.parse_records(lines)
		return signals.evaluate(records, SITE)

	def test_two_small_sites_pool_into_a_breach(self):
		# each site: 9 ok + 2 capped = 11 reloads, 18 % capped; pooled 22 reloads
		result = self._evaluate(self._site_lines("a", 9, 2), self._site_lines("b", 9, 2))
		self.assertTrue(result["sites"]["a"]["insufficient_sample"])
		self.assertTrue(result["sites"]["b"]["insufficient_sample"])
		self.assertEqual(result["pooled"]["reloads"], 22)
		self.assertFalse(result["pooled"]["insufficient_sample"])
		self.assertEqual(len(result["abort"]), 1)
		self.assertIn("pooled", result["abort"][0])
		self.assertIn("capped or busy", result["abort"][0])

	def test_two_small_sites_below_the_minimum_together_do_not_abort(self):
		result = self._evaluate(self._site_lines("a", 5, 3), self._site_lines("b", 5, 3))
		self.assertTrue(result["pooled"]["insufficient_sample"])
		self.assertEqual(result["abort"], [])

	def test_large_sites_are_not_double_counted_in_the_pool(self):
		result = self._evaluate(self._site_lines("big", 20, 5), self._site_lines("small", 3, 0))
		self.assertEqual(result["pooled"]["reloads"], 3)
		self.assertEqual(len(result["abort"]), 1)
		self.assertTrue(result["abort"][0].startswith("big: "))

	def test_each_large_site_aborts_under_its_own_name(self):
		result = self._evaluate(self._site_lines("a", 20, 5), self._site_lines("b", 20, 5))
		self.assertEqual(sorted(reason.split(":")[0] for reason in result["abort"]), ["a", "b"])

	def test_healthy_large_site_and_small_site_give_no_abort(self):
		result = self._evaluate(self._site_lines("a", 25, 0), self._site_lines("b", 4, 0))
		self.assertEqual(result["abort"], [])
