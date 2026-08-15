"""Analyze jarvis.tool_telemetry lines: was describe_customizations called
before the agent touched a custom doctype? PURE at module level (frappe only
inside run()).

Escalation gate: if after ~2 weeks of live traffic the activation rate stays
below ~70% with the customizations clause enabled, revisit Option A (a slim
1-2 KB generated catalog-router skill reusing collect/render as-is; sync
machinery per the design doc). Do not implement Option A before that
evidence.

Bench entry: bench --site jarvis.local execute jarvis.site_profile.analyze.run
"""

import glob
import json
import math
import os
import re

_LOG_MODULE = "jarvis.tool_telemetry"


def parse_lines(lines: list) -> list:
	"""Tolerant JSONL parse: each line may be raw JSON or logger-decorated
	(timestamp/level prefix before the first '{'). Skips lines that fail to
	parse or lack a "kind" key. Preserves order."""
	records = []
	for line in lines:
		start = line.find("{")
		if start == -1:
			continue
		try:
			obj = json.loads(line[start:])
		except ValueError:
			continue
		if isinstance(obj, dict) and "kind" in obj:
			records.append(obj)
	return records


def percentile(values: list, p: float):
	"""Nearest-rank percentile on a sorted copy of ``values``. None for empty input."""
	if not values:
		return None
	ordered = sorted(values)
	rank = max(1, min(len(ordered), math.ceil(p / 100 * len(ordered))))
	return ordered[rank - 1]


def _add_number(bucket: list, value) -> None:
	if isinstance(value, (int, float)) and not isinstance(value, bool):
		bucket.append(value)


def compute(records: list) -> dict:
	"""Activation: over conversations with >=1 custom_target tool line, the
	fraction where a describe_customizations line appears earlier in arrival
	order than that conversation's first custom_target line. Lines without a
	"conversation" key are ignored for activation only."""
	tool_calls = 0
	tool_duration_ms = []
	tool_result_chars = []
	turns_custom = 0
	turns_non_custom = 0
	turn_duration_custom = []
	turn_duration_non_custom = []

	custom_target_convs = set()
	first_custom_idx = {}
	first_describe_idx = {}

	for order, rec in enumerate(records):
		kind = rec.get("kind")
		conv = rec.get("conversation")
		if kind == "tool":
			tool = rec.get("tool")
			if tool == "describe_customizations":
				tool_calls += 1
				_add_number(tool_duration_ms, rec.get("duration_ms"))
				_add_number(tool_result_chars, rec.get("result_chars"))
				if conv is not None and conv not in first_describe_idx:
					first_describe_idx[conv] = order
			if conv is not None and rec.get("custom_target"):
				custom_target_convs.add(conv)
				if conv not in first_custom_idx:
					first_custom_idx[conv] = order
		elif kind == "turn":
			if rec.get("touched_custom"):
				turns_custom += 1
				_add_number(turn_duration_custom, rec.get("duration_ms"))
			else:
				turns_non_custom += 1
				_add_number(turn_duration_non_custom, rec.get("duration_ms"))

	activated = sum(
		1
		for conv in custom_target_convs
		if conv in first_describe_idx and first_describe_idx[conv] < first_custom_idx[conv]
	)
	activation_rate = (activated / len(custom_target_convs)) if custom_target_convs else None

	return {
		"conversations_touching_custom": len(custom_target_convs),
		"activation_rate": activation_rate,
		"tool_calls": tool_calls,
		"tool_duration_ms": {
			"p50": percentile(tool_duration_ms, 50),
			"p95": percentile(tool_duration_ms, 95),
		},
		"tool_result_chars": {
			"p50": percentile(tool_result_chars, 50),
			"p95": percentile(tool_result_chars, 95),
		},
		"turns": {"custom": turns_custom, "non_custom": turns_non_custom},
		"turn_duration_ms": {
			"custom": {
				"p50": percentile(turn_duration_custom, 50),
				"p95": percentile(turn_duration_custom, 95),
			},
			"non_custom": {
				"p50": percentile(turn_duration_non_custom, 50),
				"p95": percentile(turn_duration_non_custom, 95),
			},
		},
	}


def _log_file_paths(site: str, bench_path: str) -> list:
	"""Match frappe.utils.logger.create_handler/get_logger: for
	frappe.logger("jarvis.tool_telemetry"), module == the logger name itself,
	so the file is "jarvis.tool_telemetry.log", written via RotatingFileHandler
	to BOTH <bench>/logs/ (aggregates every site sharing this bench) and
	<bench>/sites/<site>/logs/ (this site only) - same messages in both, since
	create_handler() returns both handlers whenever a site is resolved. We read
	the site-scoped copy only, so records are already single-site with no
	cross-site de-dup needed; the bench-wide file is a fallback for when no
	site-level file exists yet. Rotation names backups "<file>.1" (newest
	backup) .. "<file>.N" (oldest); the un-suffixed file is the current one
	being written. We glob rather than assume a backupCount so any file_count
	the logger call used is picked up."""
	logfile = f"{_LOG_MODULE}.log"
	site_dir = os.path.join(bench_path, "sites", site, "logs")
	candidates = glob.glob(os.path.join(site_dir, logfile + "*"))
	if not candidates:
		bench_dir = os.path.join(bench_path, "logs")
		candidates = glob.glob(os.path.join(bench_dir, logfile + "*"))

	def _sort_key(path: str):
		m = re.search(r"\.log\.(\d+)$", path)
		return -int(m.group(1)) if m else 1  # higher suffix = older; current file (no suffix) last

	return sorted(candidates, key=_sort_key)


def _read_log_lines(site: str, bench_path: str) -> list:
	lines = []
	for path in _log_file_paths(site, bench_path):
		try:
			with open(path, encoding="utf-8", errors="replace") as f:
				lines.extend(f.readlines())
		except OSError:
			continue
	return lines


def run(days: int = 14, site: str | None = None) -> dict:
	"""Bench entry: bench --site jarvis.local execute jarvis.site_profile.analyze.run

	Reads jarvis.tool_telemetry's log file(s) for ``site`` (default: the
	current bench-execute site), computes activation metrics, prints the
	result as JSON and returns it.

	``days`` is accepted for the bench-execute signature but not used for
	line-level filtering: log lines carry no reliable-to-reparse timestamp
	ordering guarantee beyond arrival order, and RotatingFileHandler rotation
	already keeps each file short, so reading every file we find is a
	best-effort recent-window already. We do not invent ts parsing here.
	"""
	import frappe
	from frappe.utils import get_bench_path

	site = site or frappe.local.site
	result = compute(parse_lines(_read_log_lines(site, get_bench_path())))
	print(json.dumps(result, indent=2))
	return result
