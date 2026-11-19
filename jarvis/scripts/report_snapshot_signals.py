"""Rollout signals for inline report snapshots (plan B.7 and section 7).

Run by the release owner, daily for the first 7 days:

	bench --site <site> execute jarvis.scripts.report_snapshot_signals.run
	bench --site <site> execute jarvis.scripts.report_snapshot_signals.run --kwargs "{'days': 7}"

``bench execute`` prints the returned dict as JSON, so ``run`` does not print. It returns
a dict and has no other output.

Reads the ``jarvis.report_snapshot`` log (rotated files included). The bench-wide copy is
read by default, so every site sharing the bench is covered and small sites can be pooled;
the per-site copy is the fallback. It computes the success signals S4 and S5 and the abort
thresholds of section 7. Read-only: no database write and no Redis write. Lines are
streamed and records older than the window are dropped while streaming.

Definitions (a "reload" is one ``reload_end`` event that reached the lock):
  reloads  ok + refused:* + failed:* + capped + busy. ``disabled``, ``not_agent_path`` and
           ``refused:slow`` end before the lock: they are excluded from the sample size
           (minimum 20), from every share and from S5, and only listed in
           ``reload_excluded``.
  S4       share of ``reload_end`` (ok) events followed by a ``read`` with sql_executed=true
           for the same conversation, snapshot_hash and generation. With 20 reloads or
           more and no ok reload at all, S4 is undefined and that aborts.
  S5       p95 of ``reload_ms`` over the reloads that ran (ok, refused:*, failed:*).
  ok_share, failed_share, failed_classes
           share of ok reloads, share of ``failed:*`` reloads (abort above 20 %), and the
           count of each ``failed:<class>`` outcome.
  capped_or_busy_share
           (capped + busy) / reloads. A ``busy`` reload counts once per (snapshot_hash,
           generation), in the numerator and in the denominator.
  unmatched_reload_starts
           ``reload_start`` events with no later ``reload_end`` of the same conversation and
           snapshot_hash (a killed reload). A start carries the generation before the reload:
           an ``ok`` end carries the generation after it (start + 1), every other end the
           same one. An end of outcome busy, capped or refused:lock_stale never matches: those
           never follow their own start. A start in the last IN_FLIGHT_S seconds may still
           run and is ignored. They are a separate abort signal (above 5 % of reloads + unmatched starts), not part of S5.
  evicted_share
           reads that were evicted (outcome ``evicted`` or ``evicted=true``) / all reads.
           It aborts above 5 % with at least MIN_READS reads, also for a site below the
           reload minimum.
  refused_prepared
           every ``reload_end`` with outcome ``refused:prepared``. The plan says "that
           follows a reload"; counting all of them is a deliberate, stricter superset.

Pooling: a site with fewer than MIN_RELOADS reloads is too small for its own verdict. The
small sites are pooled (never the large ones, so no site counts twice) and the pool gets
one verdict. The top-level ``abort`` list holds each large site's breaches tagged with the
site name and the pool's breaches tagged ``pooled``.

Warnings: no log file (for example FRAPPE_STREAM_LOGGING is set), no valid record, newest
record older than a day, and ``window_truncated`` when the files are at the rotation limit
and the oldest record is newer than the window start (older events were rotated away).
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
from datetime import datetime, timedelta

LOG_FILE = "jarvis.report_snapshot.log"
LOG_NAME = re.compile(r"jarvis\.report_snapshot\.log(\.\d+)?")
LOG_FILE_COUNT = 30  # frappe.logger(..., file_count=30) in telemetry._snapshot_logger
MIN_RELOADS = 20
MIN_READS = 20
S5_P95_MAX_MS = 12_000
CAPPED_MAX_SHARE = 0.10
S4_MIN_SHARE = 0.90
EVICTED_MAX_SHARE = 0.05
FAILED_MAX_SHARE = 0.20
UNMATCHED_MAX_SHARE = 0.05
IN_FLIGHT_S = 60
STALE_NEWEST = timedelta(days=1)
RAN_PREFIXES = ("ok", "refused", "failed")
ATTEMPT_PREFIXES = (*RAN_PREFIXES, "capped", "busy")
EXCLUDED_OUTCOMES = ("disabled", "not_agent_path", "refused:slow")
NEVER_AFTER_OWN_START = ("busy", "capped", "refused:lock_stale")
NO_LOG_WARNING = (
	"no jarvis.report_snapshot log file found; if FRAPPE_STREAM_LOGGING is set the events go "
	"to stdout and this script cannot read them"
)
NO_RECORDS_WARNING = "the log files hold no valid snapshot record (empty or every line malformed)"


def run(
	days: int = 7,
	site: str | None = None,
	now: datetime | None = None,
	log_paths: list[str] | None = None,
) -> dict:
	"""Entry for ``bench execute``: compute the signals for the last ``days`` days.

	``now`` defaults to the site time (the log ``ts`` is written in site time).
	``log_paths`` replaces the log lookup (for tests)."""
	import frappe
	from frappe.utils import get_bench_path, now_datetime

	site = site or frappe.local.site
	now = now or now_datetime()
	cutoff = now - timedelta(days=days)
	paths = log_paths if log_paths is not None else _log_file_paths(site, get_bench_path())
	counter = {"lines": 0}
	span: dict = {}
	records, malformed = parse_records(_counted(_read_lines(paths), counter), cutoff, span)
	truncated = _window_truncated(paths, span, cutoff)
	return {
		"days": days,
		"log_files": paths,
		"lines": counter["lines"],
		"malformed_lines": malformed,
		"window_truncated": truncated,
		"warnings": _warnings(paths, span, now, truncated),
		**evaluate(records, site, now),
		"redis": redis_info(),
	}


def _counted(lines, counter: dict):
	for line in lines:
		counter["lines"] += 1
		yield line


def _log_file_paths(site: str, bench_path: str) -> list[str]:
	"""Bench-wide log files if any exist (all sites), else this site's; oldest rotation first.

	RotatingFileHandler names backups ``<file>.1`` (newest backup) to ``<file>.N`` (oldest);
	the file without a suffix is the current one. The bench and site copies hold the same
	messages, so only one set is read."""
	for folder in (
		os.path.join(bench_path, "logs"),
		os.path.join(bench_path, "sites", site, "logs"),
	):
		found = [
			path
			for path in glob.glob(os.path.join(folder, LOG_FILE + "*"))
			if LOG_NAME.fullmatch(os.path.basename(path))
		]
		if found:
			return sorted(found, key=_oldest_first)
	return []


def _oldest_first(path: str) -> int:
	match = re.search(r"\.log\.(\d+)$", path)
	return -int(match.group(1)) if match else 1


def _read_lines(paths: list[str]):
	"""Yield the lines of each file in turn; an unreadable file is skipped."""
	for path in paths:
		try:
			# nosemgrep: frappe-security-file-traversal -- bench log files, not user input
			with open(path, encoding="utf-8", errors="replace") as handle:
				yield from handle
		except OSError:
			continue


def parse_records(lines, cutoff: datetime | None = None, span: dict | None = None):
	"""Snapshot records in arrival order, and the number of lines that are not valid records.

	``lines`` may be any iterable (a generator streams). Each record carries its parsed
	timestamp as ``_ts`` (a timezone is dropped). A line may carry a logger prefix before
	the first brace. Lines of another kind are ignored. A line with a missing or unreadable
	``ts`` counts as malformed. Records older than ``cutoff`` are dropped while streaming.
	``span``, when given, receives the ``oldest`` and ``newest`` ts of every snapshot
	record, including the dropped ones."""
	records, malformed = [], 0
	for line in lines:
		if not line.strip():
			continue
		record = _parse_line(line)
		if record is None:
			malformed += 1
			continue
		if record.get("kind") != "report_snapshot":
			continue
		if span is not None:
			_widen(span, record["_ts"])
		if cutoff is None or record["_ts"] >= cutoff:
			records.append(record)
	return records, malformed


def _widen(span: dict, ts: datetime) -> None:
	span["oldest"] = min(span.get("oldest", ts), ts)
	span["newest"] = max(span.get("newest", ts), ts)


def _parse_line(line: str) -> dict | None:
	start = line.find("{")
	if start == -1:
		return None
	try:
		record = json.loads(line[start:])
	except ValueError:
		return None
	if not isinstance(record, dict):
		return None
	record["_ts"] = _parse_ts(record.get("ts"))
	return record if record["_ts"] is not None else None


def _parse_ts(value) -> datetime | None:
	try:
		return datetime.fromisoformat(str(value)).replace(tzinfo=None)
	except ValueError:
		return None


def _window_truncated(paths: list[str], span: dict, cutoff: datetime) -> bool:
	"""Rotation dropped events of the window: the files are at the limit and the oldest
	record is newer than the window start."""
	return len(paths) >= LOG_FILE_COUNT and "oldest" in span and span["oldest"] > cutoff


def _warnings(paths: list[str], span: dict, now: datetime, truncated: bool) -> list[str]:
	if not paths:
		return [NO_LOG_WARNING]
	if not span:
		return [NO_RECORDS_WARNING]
	warnings = []
	if span["newest"] < now - STALE_NEWEST:
		warnings.append(f"the newest record is from {span['newest']}, older than a day")
	if truncated:
		warnings.append(
			f"log rotation limit reached: the oldest record ({span['oldest']}) is newer than "
			"the window start, so older events were rotated away"
		)
	return warnings


def evaluate(records: list[dict], default_site: str, now: datetime | None = None) -> dict:
	"""Per-site signals, the pool of the small sites, and the one abort verdict."""
	by_site: dict[str, list[dict]] = {}
	for record in records:
		by_site.setdefault(_site_name(record, default_site), []).append(record)
	sites = {name: compute(rows, now) for name, rows in by_site.items()}
	small = {name for name, signals in sites.items() if signals["insufficient_sample"]}
	pooled = compute([r for r in records if _site_name(r, default_site) in small], now)
	abort = [f"{name}: {reason}" for name, signals in sites.items() for reason in signals["abort"]]
	abort += [f"pooled: {reason}" for reason in pooled["abort"]]
	return {"sites": sites, "pooled": pooled, "abort": abort}


def _site_name(record: dict, default_site: str) -> str:
	return str(record.get("site") or default_site)


def compute(records: list[dict], now: datetime | None = None) -> dict:
	"""Signals and abort list for records in arrival order. ``now`` enables the in-flight
	grace of ``unmatched_reload_starts``."""
	reads = [r for r in records if r.get("event") == "read"]
	reload_ends = [r for r in records if r.get("event") == "reload_end"]
	reloads = _dedupe_busy([r for r in reload_ends if _is_attempt(r)])
	read_outcomes = _count_by_outcome(reads)
	reload_outcomes = _count_by_outcome(reloads)
	failed_classes = {k: v for k, v in reload_outcomes.items() if k.startswith("failed")}
	unmatched = _unmatched_starts(records, now)
	signals = {
		"reads": len(reads),
		"read_outcomes": read_outcomes,
		"reloads": len(reloads),
		"reload_outcomes": reload_outcomes,
		"reload_excluded": _count_by_outcome([r for r in reload_ends if _is_excluded(r)]),
		"s4": _s4(records),
		"s5_p95_ms": _percentile(_ran_reload_ms(reloads), 95),
		"ok_share": _share(reload_outcomes.get("ok", 0), len(reloads)),
		"failed_share": _share(sum(failed_classes.values()), len(reloads)),
		"failed_classes": failed_classes,
		"capped_or_busy_share": _share(
			reload_outcomes.get("capped", 0) + reload_outcomes.get("busy", 0), len(reloads)
		),
		"busy_unique": reload_outcomes.get("busy", 0),
		"unmatched_reload_starts": unmatched,
		"unmatched_start_share": _share(unmatched, len(reloads) + unmatched),
		"evicted_share": _share(sum(1 for r in reads if _is_evicted_read(r)), len(reads)),
		"refused_prepared": reload_outcomes.get("refused:prepared", 0),
	}
	if len(reloads) < MIN_RELOADS:
		small_abort = _evicted_reasons(signals) if len(reads) >= MIN_READS else []
		return {**signals, "insufficient_sample": True, "abort": small_abort}
	return {**signals, "insufficient_sample": False, "abort": _abort_reasons(signals)}


def _hashable(value):
	"""A dict key part for any JSON value (a list or dict in a log line must not crash)."""
	try:
		hash(value)
		return value
	except TypeError:
		return json.dumps(value, sort_keys=True, default=str)


def _snapshot_key(record: dict) -> tuple:
	return tuple(_hashable(record.get(name)) for name in ("conversation", "snapshot_hash", "generation"))


def _outcome_prefix(record: dict) -> str:
	return str(record.get("outcome", "")).split(":")[0]


def _is_attempt(record: dict) -> bool:
	return _outcome_prefix(record) in ATTEMPT_PREFIXES and not _is_excluded(record)


def _is_excluded(record: dict) -> bool:
	return record.get("outcome") in EXCLUDED_OUTCOMES


def _is_evicted_read(record: dict) -> bool:
	return record.get("outcome") == "evicted" or record.get("evicted") is True


def _dedupe_busy(reload_ends: list[dict]) -> list[dict]:
	"""Keep every reload_end, but only the first busy one per (snapshot_hash, generation)."""
	seen, kept = set(), []
	for record in reload_ends:
		if record.get("outcome") == "busy":
			key = (_hashable(record.get("snapshot_hash")), _hashable(record.get("generation")))
			if key in seen:
				continue
			seen.add(key)
		kept.append(record)
	return kept


def _count_by_outcome(records: list[dict]) -> dict[str, int]:
	counts: dict[str, int] = {}
	for record in records:
		outcome = str(record.get("outcome"))
		counts[outcome] = counts.get(outcome, 0) + 1
	return counts


def _s4(records: list[dict]) -> float | None:
	"""Share of ok reloads that a later SQL-executing read of the same snapshot follows.

	One reverse pass: the reads seen so far are the reads that come after the current record."""
	sql_read_keys = set()
	ok_reloads = 0
	followed = 0
	for record in reversed(records):
		event = record.get("event")
		if event == "read" and record.get("sql_executed") is True:
			sql_read_keys.add(_snapshot_key(record))
		elif event == "reload_end" and record.get("outcome") == "ok":
			ok_reloads += 1
			followed += _snapshot_key(record) in sql_read_keys
	return _share(followed, ok_reloads)


def _start_key_of_end(record: dict) -> tuple:
	"""The key of the ``reload_start`` that a ``reload_end`` closes. A start carries the
	generation before the reload; an ``ok`` end carries the one after it (g + 1), every
	other end the unchanged one."""
	conversation, snapshot_hash, generation = _snapshot_key(record)
	if record.get("outcome") == "ok" and isinstance(generation, int) and not isinstance(generation, bool):
		generation -= 1
	return conversation, snapshot_hash, generation


def _unmatched_starts(records: list[dict], now: datetime | None) -> int:
	"""``reload_start`` events with no later ``reload_end`` for the same snapshot (an ok end
	one generation higher, any other end at the same generation). One reverse pass, like S4."""
	end_keys = set()
	unmatched = 0
	for record in reversed(records):
		event = record.get("event")
		if event == "reload_end" and record.get("outcome") not in NEVER_AFTER_OWN_START:
			end_keys.add(_start_key_of_end(record))
		elif event == "reload_start" and _snapshot_key(record) not in end_keys:
			unmatched += not _in_flight(record, now)
	return unmatched


def _in_flight(record: dict, now: datetime | None) -> bool:
	ts = record.get("_ts")
	return now is not None and ts is not None and now - ts < timedelta(seconds=IN_FLIGHT_S)


def _ran_reload_ms(reload_ends: list[dict]) -> list[float]:
	return [
		r["reload_ms"]
		for r in reload_ends
		if _outcome_prefix(r) in RAN_PREFIXES and isinstance(r.get("reload_ms"), int | float)
	]


def _percentile(values: list[float], pct: float):
	"""Nearest-rank percentile; None for no values."""
	if not values:
		return None
	ordered = sorted(values)
	rank = max(1, min(len(ordered), math.ceil(pct / 100 * len(ordered))))
	return ordered[rank - 1]


def _share(part: int, whole: int) -> float | None:
	return part / whole if whole else None


def _abort_reasons(signals: dict) -> list[str]:
	"""Each section-7 threshold that the signals breach (with at least MIN_RELOADS)."""
	reasons = []
	if signals["s5_p95_ms"] is not None and signals["s5_p95_ms"] > S5_P95_MAX_MS:
		reasons.append(f"S5 reload p95 {signals['s5_p95_ms']} ms > {S5_P95_MAX_MS} ms")
	share = signals["capped_or_busy_share"]
	if share is not None and share > CAPPED_MAX_SHARE:
		reasons.append(f"capped or busy {share:.1%} of reloads > {CAPPED_MAX_SHARE:.0%}")
	if signals["s4"] is None:
		reasons.append(f"S4 undefined: no ok reload in {signals['reloads']} reloads")
	elif signals["s4"] < S4_MIN_SHARE:
		reasons.append(f"S4 {signals['s4']:.1%} < {S4_MIN_SHARE:.0%}")
	failed = signals["failed_share"]
	if failed is not None and failed > FAILED_MAX_SHARE:
		reasons.append(
			f"failed {failed:.1%} of reloads > {FAILED_MAX_SHARE:.0%} ({signals['failed_classes']})"
		)
	unmatched = signals["unmatched_start_share"]
	if unmatched is not None and unmatched > UNMATCHED_MAX_SHARE:
		reasons.append(
			f"killed reloads: {signals['unmatched_reload_starts']} reload_start without a "
			f"reload_end ({unmatched:.1%}) > {UNMATCHED_MAX_SHARE:.0%}"
		)
	if signals["refused_prepared"] > 0:
		reasons.append(f"refused:prepared after a reload: {signals['refused_prepared']}")
	return reasons + _evicted_reasons(signals)


def _evicted_reasons(signals: dict) -> list[str]:
	share = signals["evicted_share"]
	if share is not None and share > EVICTED_MAX_SHARE:
		return [f"evicted {share:.1%} of rs_ reads > {EVICTED_MAX_SHARE:.0%}"]
	return []


def redis_info() -> dict:
	"""Server-wide evicted_keys and uptime, for a manual check only (no write)."""
	import frappe

	try:
		cache = frappe.cache()
		return {
			"evicted_keys": cache.info("stats").get("evicted_keys"),
			"uptime_in_seconds": cache.info("server").get("uptime_in_seconds"),
			"note": "server-wide counters, manual check only",
		}
	except Exception as exc:
		return {"error": type(exc).__name__}
