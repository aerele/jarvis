"""The time-based CI shard split: log parsing, the partition, and the hook into frappe's runner."""

import io
import itertools
import json
import random
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from jarvis.ci import test_shards


def _log(*events):
	"""GitHub-style raw job log lines from (seconds-after-10:00, text)."""
	lines = []
	for offset, text in events:
		minutes, seconds = divmod(offset, 60)
		lines.append(f"2026-10-31T10:{int(minutes):02d}:{seconds:010.7f}Z {text}\n")
	return lines


def _ok(name):
	return f"  \x1b[32m ✔ \x1b[0m {name}"


class TestModuleSeconds(unittest.TestCase):
	def test_charges_setupclass_to_the_module_and_ignores_the_trailing_summary(self):
		lines = _log(
			(0, "setting up"),
			(1, ""),
			(1, "jarvis.tests.test_a.TestOne"),
			(3, _ok("test_x")),  # 2s
			(4, _ok("test_y")),  # 1s
			(4, ""),
			# setUpClass of the next class runs BEFORE its header: 10s, charged to test_b
			(14, "jarvis.tests.test_b.TestTwo"),
			(15, "  \x1b[31m ✖ \x1b[0m test_fails"),  # 11s
			(15, "jarvis.exceptions.SomeError: not a header, has a message"),
			(16, "jarvis.helpers.Thing"),  # a stray dotted name: not a test module
			(17, "  \x1b[37m = \x1b[0m test_skipped"),  # 2s, still test_b
			(18, "jarvis.tests.test_a.TestThree"),  # same module, another class
			(20, _ok("test_z")),  # 3s
			(80, "Traceback of the failure, printed after the run"),
			(90, "Tests: 5, Failing: 1, Errors: 0"),
		)
		lines[0] = "﻿" + lines[0]  # GitHub's raw logs open with a byte-order mark
		self.assertEqual(
			test_shards.module_seconds(lines), {"jarvis.tests.test_a": 6.0, "jarvis.tests.test_b": 13.0}
		)

	def test_subpackages_and_doctype_tests_keep_their_full_module_path(self):
		lines = _log(
			(0, "jarvis.tests.learning.test_q.TestQ"),
			(2, _ok("test_q")),
			(2, "jarvis.jarvis.doctype.jarvis_settings.test_jarvis_settings.TestJarvisSettings"),
			(5, _ok("test_s")),
		)
		self.assertEqual(
			test_shards.module_seconds(lines),
			{
				"jarvis.tests.learning.test_q": 2.0,
				"jarvis.jarvis.doctype.jarvis_settings.test_jarvis_settings": 3.0,
			},
		)

	def test_a_log_crossing_midnight_is_not_negative(self):
		lines = [
			"2026-10-31T23:59:59.0000000Z jarvis.tests.test_a.TestA\n",
			"2026-11-01T00:00:01.5000000Z " + _ok("test_a") + "\n",
		]
		self.assertEqual(test_shards.module_seconds(lines), {"jarvis.tests.test_a": 2.5})

	def test_a_log_without_test_output_gives_nothing(self):
		self.assertEqual(test_shards.module_seconds(_log((0, "pip install"), (1, "done"))), {})

	def test_median_across_runs(self):
		merged = test_shards.merge_runs(
			[{"m.test_a": 10.0, "m.test_b": 1.0}, {"m.test_a": 30.0}, {"m.test_a": 11.0}]
		)
		self.assertEqual(merged, {"m.test_a": 11.0, "m.test_b": 1.0})


def _best_contiguous_max(weights, k):
	"""Brute force: the smallest possible heaviest run over every way to cut into <= k runs."""
	n = len(weights)
	best = float("inf")
	for cut_count in range(min(k, max(n, 1))):
		for cuts in itertools.combinations(range(1, n), cut_count):
			bounds = [0, *cuts, n]
			best = min(best, max(sum(weights[a:b]) for a, b in itertools.pairwise(bounds)))
	return best if n else 0.0


class TestPartition(unittest.TestCase):
	def assert_valid(self, weights, k, chunks):
		self.assertEqual(len(chunks), k)
		self.assertEqual(
			[i for run in chunks for i in run], list(range(len(weights)))
		)  # contiguous, in order

	def test_is_optimal_against_brute_force(self):
		rng = random.Random(20261031)
		for _ in range(300):
			n = rng.randint(0, 9)
			k = rng.randint(1, 4)
			weights = [round(rng.choice([rng.uniform(0, 3), rng.uniform(0, 300)]), 1) for _ in range(n)]
			chunks = test_shards.partition(weights, k)
			self.assert_valid(weights, k, chunks)
			heaviest = max((sum(weights[i] for i in run) for run in chunks), default=0.0)
			self.assertAlmostEqual(heaviest, _best_contiguous_max(weights, k), places=4, msg=(weights, k))

	def test_one_dominant_file_gets_a_shard_of_its_own(self):
		weights = [1.0] * 30 + [300.0] + [1.0] * 30
		chunks = test_shards.partition(weights, 4)
		self.assertIn([30], chunks)

	def test_more_shards_than_files_leaves_trailing_shards_empty(self):
		chunks = test_shards.partition([5.0, 5.0], 4)
		self.assert_valid([5.0, 5.0], 4, chunks)
		self.assertEqual(chunks, [[0], [1], [], []])

	def test_rejects_bad_input(self):
		with self.assertRaises(ValueError):
			test_shards.partition([1.0], 0)
		with self.assertRaises(ValueError):
			test_shards.partition([1.0, -1.0], 2)


class TestTimingsFile(unittest.TestCase):
	def test_the_checked_in_file_loads(self):
		timings = test_shards.load_timings()
		self.assertGreater(len(timings), 100)
		self.assertIn("jarvis.tests.test_holiday_touch_resolve", timings)

	def write(self, document):
		handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
		self.addCleanup(Path(handle.name).unlink)
		with handle:
			json.dump(document, handle)
		return Path(handle.name)

	def test_rejects_a_missing_malformed_or_wrong_version_file(self):
		with self.assertRaises(FileNotFoundError):
			test_shards.load_timings(Path("/nonexistent/test-timings.json"))
		for document in (
			{"version": 2, "seconds": {"jarvis.tests.test_a": 1}},
			{"version": 1, "seconds": {}},
			{"version": 1, "seconds": {"jarvis.tests.test_a": -1}},
			{"version": 1, "seconds": {"jarvis.tests.test_a": "fast"}},
			{"version": 1, "seconds": {"jarvis.helpers": 1}},
		):
			with self.subTest(document=document), self.assertRaises(ValueError):
				test_shards.load_timings(self.write(document))


class TestSplitter(unittest.TestCase):
	ROOT = "/bench/apps/jarvis"
	WORK = [
		[f"{ROOT}/jarvis/tests", "test_a.py"],
		[f"{ROOT}/jarvis/tests", "test_b.py"],
		[f"{ROOT}/jarvis/tests/learning", "test_c.py"],
		[f"{ROOT}/jarvis/tests", "test_new.py"],
	]

	def test_module_name(self):
		self.assertEqual(test_shards.module_name(*self.WORK[2], self.ROOT), "jarvis.tests.learning.test_c")
		with self.assertRaises(ValueError):
			test_shards.module_name("/elsewhere", "test_x.py", self.ROOT)

	def test_untimed_files_cost_the_measured_average_per_test(self):
		timings = {
			"jarvis.tests.test_a": 10.0,
			"jarvis.tests.test_b": 30.0,
			"jarvis.tests.learning.test_c": 0.0,
		}
		# 40s over 2 + 6 + 2 = 10 tests -> 4s/test, so the 5-test new file costs 20s
		_names, seconds, untimed = test_shards.estimate(self.WORK, [2, 6, 2, 5], timings, self.ROOT)
		self.assertEqual(seconds, [10.0, 30.0, 0.0, 20.0])
		self.assertEqual(untimed, ["jarvis.tests.test_new"])

	def test_returns_frappe_shaped_chunks_and_reports_the_plan(self):
		timings = {
			"jarvis.tests.test_a": 50.0,
			"jarvis.tests.test_b": 50.0,
			"jarvis.tests.learning.test_c": 100.0,
		}
		out = io.StringIO()
		# 200s over 30 timed tests: the 1-test new file costs ~6.7s, ~3% of the suite (not stale)
		chunks = test_shards.make_splitter(timings, self.ROOT, out=out)(self.WORK, [10, 10, 10, 1], 2)
		self.assertEqual(chunks, [self.WORK[:2], self.WORK[2:]])
		self.assertIn("shard 1:    2 files", out.getvalue())
		self.assertIn("1 untimed files", out.getvalue())
		self.assertNotIn("::warning", out.getvalue())

	def test_warns_when_the_timings_file_has_gone_stale(self):
		timings = {"jarvis.tests.test_a": 1.0}
		out = io.StringIO()
		test_shards.make_splitter(timings, self.ROOT, out=out)(self.WORK, [1, 50, 50, 50], 2)
		self.assertIn("::warning title=Stale test timings::", out.getvalue())


class TestFrappeHook(unittest.TestCase):
	"""Against frappe's real runner: the patch must be the function get_test_file_list calls."""

	def setUp(self):
		import frappe.parallel_test_runner as runner

		self.runner = runner
		patcher = mock.patch.object(runner, "split_by_weight", runner.split_by_weight)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_every_shard_of_the_real_suite_is_disjoint_and_together_complete(self):
		runner = test_shards.install(out=io.StringIO())
		self.assertEqual(runner.split_by_weight.__name__, "split_by_time")
		cls = runner.ParallelTestRunner
		weigh = getattr(cls, "get_test_weight", None) or cls.get_test_count  # v16 / v15 spelling
		shards = []
		for number in range(1, 5):
			fake = types.SimpleNamespace(
				app="jarvis", total_builds=4, build_number=number, get_test_weight=weigh, get_test_count=weigh
			)
			shards.append([tuple(item) for item in cls.get_test_file_list(fake)])
		every = [item for shard in shards for item in shard]
		self.assertEqual(len(every), len(set(every)))
		self.assertEqual(sorted(every), sorted(tuple(t) for t in runner.get_all_tests("jarvis")))
		self.assertTrue(all(shards))


class TestCli(unittest.TestCase):
	def test_timings_writes_the_median_document(self):
		with tempfile.TemporaryDirectory() as directory:
			logs = []
			for number, seconds in enumerate((4, 6, 100)):
				path = Path(directory, f"shard-{number}.log")
				path.write_text("".join(_log((0, "jarvis.tests.test_a.TestA"), (seconds, _ok("test_a")))))
				logs.append(str(path))
			output = Path(directory, "out.json")
			with mock.patch("sys.stdout", io.StringIO()):
				self.assertEqual(test_shards.main(["timings", *logs, "--output", str(output)]), 0)
			document = json.loads(output.read_text())
			self.assertEqual(document["version"], 1)
			self.assertEqual(document["seconds"], {"jarvis.tests.test_a": 6.0})
			self.assertEqual(test_shards.load_timings(output), {"jarvis.tests.test_a": 6.0})

	def test_timings_refuses_a_log_with_no_test_output(self):
		with tempfile.NamedTemporaryFile("w", suffix=".log") as log, mock.patch("sys.stderr", io.StringIO()):
			log.write("".join(_log((0, "Set up job"))))
			log.flush()
			self.assertEqual(test_shards.main(["timings", log.name, "--output", "/dev/null"]), 1)

	def test_run_installs_the_split_then_hands_frappe_the_same_argv_bench_would(self):
		seen = {}
		fake_helper = types.SimpleNamespace(main=lambda: seen.setdefault("argv", list(sys.argv)))
		with (
			mock.patch.object(test_shards, "install") as install,
			mock.patch.dict(sys.modules, {"frappe.utils.bench_helper": fake_helper}),
			mock.patch("frappe.utils.bench_helper", fake_helper, create=True),
			mock.patch.object(sys, "argv", ["test_shards.py"]),
		):
			args = [
				"run",
				"--",
				"--site",
				"s.test",
				"run-parallel-tests",
				"--app",
				"jarvis",
				"--total-builds",
				"4",
			]
			self.assertEqual(test_shards.main(args), 0)
		install.assert_called_once_with(test_shards.TIMINGS_FILE)
		self.assertEqual(
			seen["argv"],
			[
				"test_shards.py",
				"frappe",
				"--site",
				"s.test",
				"run-parallel-tests",
				"--app",
				"jarvis",
				"--total-builds",
				"4",
			],
		)
