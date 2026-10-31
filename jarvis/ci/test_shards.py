"""Split the CI test suite across shards by measured time instead of by test count.

``bench run-parallel-tests`` gives each shard the same number of ``def test_`` functions
(frappe/parallel_test_runner.py ``split_by_weight``), in file order. Test cost is nothing like
uniform: the holiday-touch files build a real Employee + Holiday List per test and cost ~1.3s
each, so all of them landing on one shard made that shard take 27 min of tests on version-15
while the other three took 11-13.

Two subcommands:

``timings LOG...``
	Read the raw logs of a CI run's ``tests (N)`` jobs and write per-module seconds to
	``jarvis/ci/data/test-timings.json``. With logs from several runs, each module keeps its
	median. Fetch the logs with::

		for j in $(gh api 'repos/aerele/jarvis/actions/runs/<RUN>/jobs?per_page=50' \\
			-q '.jobs[]|select(.name|startswith("tests ("))|.id'); do
			gh api "repos/aerele/jarvis/actions/jobs/$j/logs" > "shard-$j.log"; done
		python3 -m jarvis.ci.test_shards timings shard-*.log

``run -- <frappe args>``
	What CI runs from ``sites/`` with the bench's python, in place of ``bench --site ...
	run-parallel-tests ...``. It replaces frappe's ``split_by_weight`` with :func:`partition`
	over the recorded timings and then hands the arguments to ``frappe.utils.bench_helper``
	exactly as ``bench`` itself does (bench/cli.py ``frappe_cmd``). Everything else, including
	coverage and the ``CI``-gated non-zero exit on failure, is still frappe's own runner.

The split stays CONTIGUOUS in frappe's file order on purpose: each shard runs the same files in
the same order and next to the same neighbours as a count-based split would, so this cannot
surface a hidden order dependency between test files. It only moves the cut points.

A file with no recorded timing (a new test file) is costed at the suite's measured average
seconds per ``def test_``, so new tests still spread sensibly before the next regeneration.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

TIMINGS_FILE = Path(__file__).parent / "data" / "test-timings.json"
VERSION = 1

# Above this share of the suite's estimated time sitting in untimed files, the run warns that the
# timings file is stale. It still runs: a stale file degrades the balance, never correctness.
STALE_SHARE = 0.15

_STAMP = re.compile(r"^\ufeff?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)Z ?(.*)$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# frappe's TestResult prints `unittest.util.strclass(test.__class__)` when the class changes,
# i.e. "jarvis.tests.test_x.TestSomething": dotted module path, then a class name.
_HEADER = re.compile(r"^(jarvis(?:\.\w+)+)\.[A-Za-z_]\w*$")
# One line per finished test: ✔ pass, ✖ fail/error, = skip.
_STATUS = re.compile(r"^ {2,}[✔✖=] ")


def _is_test_module(module: str) -> bool:
	"""A class header names a test module; a stray dotted name in a traceback does not."""
	return any(part.startswith("test") for part in module.split(".")[1:])


def _seconds(stamp: str) -> float:
	"""Seconds since midnight of a GitHub log timestamp, microsecond precision.

	Hand-parsed because Python 3.10 (the version-15 floor) cannot fromisoformat() GitHub's 7-digit
	fractions. Midnight-relative is enough: a shard's tests never span two days, and a wrap is
	handled where the deltas are taken."""
	_date, clock = stamp.split("T")
	hh, mm, ss = clock.split(":")
	return int(hh) * 3600 + int(mm) * 60 + float(ss[:9])


def module_seconds(lines: Iterable[str]) -> dict[str, float]:
	"""Seconds each test module took in ONE job log.

	A module is charged from the previous test's status line to each of its own status lines, so
	its setUpClass (which runs before its class header is printed) is counted against it, and the
	failure tracebacks and summary frappe prints after the last test are not counted at all."""
	totals: dict[str, float] = {}
	current = None
	previous = None
	for raw in lines:
		match = _STAMP.match(raw.rstrip("\r\n"))
		if not match:
			continue
		now = _seconds(match.group(1))
		body = _ANSI.sub("", match.group(2))
		header = _HEADER.match(body)
		if header and _is_test_module(header.group(1)):
			current = header.group(1)
			if previous is None:
				previous = now
		elif current and _STATUS.match(body):
			delta = now - previous
			if delta < 0:  # the log crossed midnight UTC
				delta += 86400
			totals[current] = totals.get(current, 0.0) + delta
			previous = now
	return totals


def merge_runs(per_log: Sequence[dict[str, float]]) -> dict[str, float]:
	"""One figure per module across several logs: the median of what each log measured.

	Each module runs in exactly one shard per CI run, so N runs give N observations; the median
	keeps one noisy runner from moving a cut point."""
	observed: dict[str, list[float]] = {}
	for totals in per_log:
		for module, seconds in totals.items():
			observed.setdefault(module, []).append(seconds)
	return {module: round(statistics.median(values), 1) for module, values in sorted(observed.items())}


def load_timings(path: Path = TIMINGS_FILE) -> dict[str, float]:
	"""The recorded per-module seconds. A missing or malformed file is an error, not a fallback:
	it is checked in, so its absence is a bug to see, not a reason to silently split by count."""
	document = json.loads(Path(path).read_text())
	if document.get("version") != VERSION:
		raise ValueError(f"{path}: unsupported timings version {document.get('version')!r}")
	seconds = document.get("seconds")
	if not isinstance(seconds, dict) or not seconds:
		raise ValueError(f"{path}: 'seconds' must be a non-empty object")
	for module, value in seconds.items():
		if (
			not _HEADER.match(f"{module}.X")
			or not _is_test_module(module)
			or not isinstance(value, int | float)
			or value < 0
		):
			raise ValueError(f"{path}: bad entry {module!r}: {value!r}")
	return {module: float(value) for module, value in seconds.items()}


def partition(weights: Sequence[float], chunk_count: int) -> list[list[int]]:
	"""Split item indexes 0..n-1 into exactly ``chunk_count`` contiguous runs, minimising the
	heaviest run (the shard that sets the job's wall clock).

	Binary search over the cap with a greedy feasibility check, which is exact for contiguous
	partitions. Trailing runs may be empty when there are fewer items than chunks."""
	if chunk_count < 1:
		raise ValueError("chunk_count must be at least 1")
	if any(w < 0 for w in weights):
		raise ValueError("weights must be non-negative")

	def cuts(cap: float) -> list[list[int]] | None:
		chunks: list[list[int]] = [[]]
		load = 0.0
		for index, weight in enumerate(weights):
			if chunks[-1] and load + weight > cap:
				if len(chunks) == chunk_count:
					return None
				chunks.append([])
				load = 0.0
			chunks[-1].append(index)
			load += weight
		return chunks

	low = max(weights, default=0.0)
	high = float(sum(weights))
	for _ in range(100):
		if high - low <= 1e-6:
			break
		middle = (low + high) / 2
		if cuts(middle) is None:
			low = middle
		else:
			high = middle
	chunks = cuts(high)
	if chunks is None:  # float edge: the total always fits in one run per item
		chunks = cuts(float(sum(weights)) + 1.0)
	chunks.extend([] for _ in range(chunk_count - len(chunks)))
	return chunks


def module_name(path: str, filename: str, package_parent: str) -> str:
	"""``[".../apps/jarvis/jarvis/tests", "test_x.py"]`` -> ``"jarvis.tests.test_x"``."""
	relative = os.path.relpath(os.path.join(path, filename[: -len(".py")]), package_parent)
	if relative.startswith(".."):
		raise ValueError(f"{path}/{filename} is not under {package_parent}")
	return relative.replace(os.sep, ".")


def estimate(work, test_counts, timings: dict[str, float], package_parent: str):
	"""(seconds per file, names of untimed files). Untimed files cost the suite's average seconds
	per ``def test_`` times their own count."""
	names = [module_name(path, filename, package_parent) for path, filename in work]
	timed = [(timings[n], c) for n, c in zip(names, test_counts, strict=True) if n in timings]
	rate = sum(s for s, _ in timed) / max(1, sum(c for _, c in timed))
	seconds = [timings.get(n, c * rate) for n, c in zip(names, test_counts, strict=True)]
	return names, seconds, [n for n in names if n not in timings]


def make_splitter(timings: dict[str, float], package_parent: str, out=sys.stdout):
	"""A drop-in for frappe's ``split_by_weight(work, weights, chunk_count)``.

	frappe passes per-file ``def test_`` counts as ``weights`` (v15 and v16 alike); they are used
	only to cost untimed files."""

	def split_by_time(work, weights, chunk_count):
		names, seconds, untimed = estimate(work, list(weights), timings, package_parent)
		indexes = partition(seconds, chunk_count)
		chunks = [[work[i] for i in run] for run in indexes]
		assert sorted(i for run in indexes for i in run) == list(range(len(work)))

		total = sum(seconds) or 1.0
		print(f"time-based split of {len(work)} test files into {chunk_count} shards:", file=out)
		for number, run in enumerate(indexes, 1):
			load = sum(seconds[i] for i in run)
			print(f"  shard {number}: {len(run):4d} files, est. {load / 60:5.1f} min", file=out)
		untimed_names = set(untimed)
		untimed_share = sum(s for n, s in zip(names, seconds, strict=True) if n in untimed_names) / total
		if untimed:
			print(f"  {len(untimed)} untimed files (est. {untimed_share:.0%} of the suite)", file=out)
		if untimed_share > STALE_SHARE:
			print(
				f"::warning title=Stale test timings::{untimed_share:.0%} of the estimated suite time is in "
				"files with no recorded timing; regenerate jarvis/ci/data/test-timings.json "
				"(see jarvis/ci/test_shards.py)",
				file=out,
			)
		return chunks

	return split_by_time


def install(timings_path: Path = TIMINGS_FILE, out=sys.stdout):
	"""Point frappe's parallel runner at the time-based splitter. get_test_file_list() looks
	``split_by_weight`` up as a module global at call time, so patching the module is enough."""
	import frappe.parallel_test_runner as runner

	import jarvis

	package_parent = str(Path(jarvis.__file__).resolve().parent.parent)
	runner.split_by_weight = make_splitter(load_timings(timings_path), package_parent, out=out)
	return runner


def _cmd_timings(args) -> int:
	per_log = []
	for log in args.logs:
		with open(log, encoding="utf-8", errors="replace") as handle:
			totals = module_seconds(handle)
		if not totals:
			print(f"{log}: no test output found", file=sys.stderr)
			return 1
		per_log.append(totals)
	seconds = merge_runs(per_log)
	document = {
		"version": VERSION,
		"note": "Generated by `python3 -m jarvis.ci.test_shards timings`; do not hand-edit.",
		"seconds": seconds,
	}
	Path(args.output).write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
	print(f"wrote {len(seconds)} modules, {sum(seconds.values()) / 60:.1f} min total, to {args.output}")
	return 0


def _cmd_run(args) -> int:
	install(Path(args.timings))
	from frappe.utils import bench_helper

	rest = args.frappe_args[1:] if args.frappe_args[:1] == ["--"] else args.frappe_args
	sys.argv = [sys.argv[0], "frappe", *rest]
	bench_helper.main()
	return 0


def main(argv=None) -> int:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	commands = parser.add_subparsers(dest="command", required=True)

	timings = commands.add_parser("timings", help="regenerate the timings file from CI job logs")
	timings.add_argument("logs", nargs="+", help="raw logs of one or more runs' `tests (N)` jobs")
	timings.add_argument("--output", default=str(TIMINGS_FILE))
	timings.set_defaults(func=_cmd_timings)

	run = commands.add_parser("run", help="run frappe's CLI with the time-based split installed")
	run.add_argument("--timings", default=str(TIMINGS_FILE))
	run.add_argument("frappe_args", nargs=argparse.REMAINDER, help="-- --site S run-parallel-tests ...")
	run.set_defaults(func=_cmd_run)

	args = parser.parse_args(argv)
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())
