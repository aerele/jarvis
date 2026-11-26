"""The operator view of the cache: `cache_status()`.

It reads only durable rows (heads, generations) and the Redis day counters, so it gives the same answer after a
Redis restart, except for the counters. A company is shown by its digest; its name only when the caller may read
that Company. No row, party or voucher name is ever returned.

The status shows, per company and side: the state (`fresh`, `due`, `running`, `memo`, `backoff`), the age of the
current generation, the last outcome and failure class, the failure count and backoff level, the generation counts
by state, the duration p95 of the last `P95_DAYS` days, and the last oracle outcome with the nights counted on the
deployed build. A generation above the measured size (`cache_state.within_measured_size`) is no night: the side
shows `size_gate` (`within` or `above_measured`, by its current generation). At the top: the switch, the build,
the lane (`day_lane` is `live` or `absent`; `refresh` is `day+night` or `night-only`, the bench with no day lane
whose generation ages by day), and the skip counters of today and yesterday (`cache.count_skip`).

Other modules add sections through the Frappe hook `pcal_cache_status_sections` (card counters of a later release). Each path is
called with no argument and returns a dict that is merged under `sections`. A failing path is logged by class and
skipped. Run it with `bench --site <site> execute jarvis.payment_calendar.cache_status.cache_status`.
"""

import json
import math
from collections import Counter, defaultdict

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar import cache, cache_state, cache_store, read_log, routing
from jarvis.payment_calendar.errors import TIMEOUTS
from jarvis.permissions import is_system_manager

SECTIONS_HOOK = "pcal_cache_status_sections"
SIDES = cache_store.SIDES
HEAD = cache_store.HEAD
GENERATION = cache_store.GENERATION
SKIP_CLASS = "cache_skip:"
# The duration p95 and the oracle nights look back this far (generation metadata is kept 30 days).
P95_DAYS = 30
ORACLE_DAYS = 30
# `size_gate` of a side: its current generation is within the measured size, or above it.
WITHIN = "within"
ABOVE_MEASURED = "above_measured"
# Generations read for the p95 and oracle records per call; the newest first.
MAX_GENERATIONS = 20_000


@frappe.whitelist(methods=["POST"])
def cache_status() -> dict:
	"""The status for a System Manager (and Administrator); everyone else gets `PermissionError`."""
	if not is_system_manager():
		raise frappe.PermissionError
	return build_status()


def build_status() -> dict:
	"""The status without the role check (the bench and the tests call it)."""
	settings = cache_state.cache_settings()
	heads, metrics = cache.heads_of(), head_metrics()
	latest, counts = latest_generations(), generation_counts()
	oracle, p95, gates = oracle_by_side(), duration_p95(), size_gates()
	companies = sorted({*settings.companies, *(company for company, _ in heads)})
	today = routing.today()
	workers = cache_state.lane_workers()
	live = workers >= 1
	return {
		"mode": settings.mode,
		"effective_mode": settings.effective_mode,
		"full_cadence": settings.full_cadence,
		"build": routing.cache_build(),
		"lane": {"workers": workers, "live": live},
		# No day lane: the bench builds once a night on its `long` worker(s) and the generation ages by day.
		"day_lane": "live" if live else "absent",
		"refresh": "day+night" if live else "night-only",
		"tick": _tick(),
		"janitor": cache_store.janitor_backlog(),
		"companies": [
			_company(company, settings, heads, metrics, latest, counts, oracle, p95, gates)
			for company in companies
		],
		"skips": {date: skip_counts(date) for date in (today, add_days(today, -1))},
		"sections": _sections(),
	}


# ------------------------------------------------------------------------------------------- one side


def _tick() -> dict:
	"""The age of the last 5-minute tick and whether it is late: a stopped scheduler shows here."""
	age = cache.last_tick_age_s()
	return {"last_age_s": age, "late": age is not None and age > cache.TICK_LATE_S}


def _company(company, settings, heads, metrics, latest, counts, oracle, p95, gates) -> dict:
	"""One company of the status: its identity, and its sides only when the caller may read the Company."""
	found = _identity(company)
	found["listed"] = company in settings.companies
	found["sides"] = (
		{
			side: _side(
				company,
				side,
				settings,
				heads.get((company, side)),
				metrics.get((company, side), {}),
				latest.get((company, side), {}),
				counts.get((company, side), {}),
				oracle.get((company, side), {"last": None, "reason": None, "on_build": 0, "nights": {}}),
				p95.get((company, side)),
				gates.get((company, side), WITHIN),
			)
			for side in SIDES
		}
		if found["readable"]
		else {}
	)
	return found


def _identity(company: str) -> dict:
	"""The digest always; the name and `readable` by the caller's read permission on the Company.

	A Company that no longer exists (renamed or deleted: its head waits for the janitor, or it is still listed)
	cannot be checked, since `frappe.has_permission` raises for a missing name. It is shown as `missing`, by digest
	only, with its sides: they are what the operator needs to see.
	"""
	found = {"digest": digest_of(company), "readable": True}
	if not frappe.db.exists("Company", company):
		found["missing"] = True
	elif frappe.has_permission("Company", "read", company):
		found["company"] = company
	else:
		found["readable"] = False
	return found


def _side(company, side, settings, head, metric, latest, counts, oracle, p95, gate) -> dict:
	return {
		"state": _state(head, settings),
		"has_generation": bool(head and head.current_generation is not None),
		"generation": head.current_generation if head else None,
		"age_s": head.age_s if head else None,
		"opened_age_s": head.opened_age_s if head else None,
		"hop_seq": head.hop_seq if head else 0,
		"duration_ms": head.duration_ms if head else None,
		"last_failure": head.last_failure if head else None,
		"next_due_s": _next_due(head, settings),
		"d_p95_ms": p95,
		"last_success_age_s": metric.get("success_age_s"),
		"rows": metric.get("rows"),
		"rss_mb": metric.get("rss_mb"),
		"failures": metric.get("failures", 0),
		"backoff_level": metric.get("backoff_level", 0),
		"memo_s": metric.get("memo_s", 0),
		"max_age_s_today": metric.get("max_age_s_today"),
		"generation_state": latest.get("state"),
		"last_outcome": latest.get("outcome"),
		"failure_class": latest.get("failure_class"),
		"failure_trace": latest.get("failure_trace"),
		"bytes": latest.get("bytes"),
		"generations": counts,
		"oracle": oracle,
		"size_gate": gate,
	}


def _state(head, settings) -> str:
	if head is None:
		return "due"
	if head.claim_live:
		return "running"
	if head.memo_live:
		return "backoff" if head.last_failure == "history_too_large" else "memo"
	return "due" if cache.stale(head, settings) else "fresh"


def _next_due(head, settings) -> int | None:
	"""Seconds until the next refresh starts; None when nobody uses the company or a refresh is running or waiting."""
	if head is None or head.claim_live or head.memo_live or not cache.opened(head) or head.age_s is None:
		return None
	return max(cache.start_age_s(head.duration_ms, full_cadence=settings.full_cadence) - head.age_s, 0)


# ---------------------------------------------------------------------------------------------- readers


def head_metrics() -> dict:
	"""Durable head columns that `cache.heads_of` does not carry: `{(company, side): {...}}`."""
	rows = frappe.db.sql(
		"SELECT company, side, backoff_level, failures, "
		"IF(memo_until IS NOT NULL AND memo_until > NOW(6), TIMESTAMPDIFF(SECOND, NOW(6), memo_until), 0), "
		"TIMESTAMPDIFF(SECOND, last_success, NOW(6)), last_rows, peak_rss_mb, "
		"IF(max_age_date = CURDATE(), max_age_s_today, NULL) "
		f"FROM `tab{HEAD}`"
	)
	return {
		(company, side): {
			"backoff_level": level or 0,
			"failures": failures or 0,
			"memo_s": memo or 0,
			"success_age_s": success,
			"rows": rows_,
			"rss_mb": rss,
			"max_age_s_today": max_age,
		}
		for company, side, level, failures, memo, success, rows_, rss, max_age in rows
	}


def latest_generations() -> dict:
	"""The newest generation of each side: its state, outcome, failure class and bytes."""
	rows = frappe.db.sql(
		"SELECT g.company, g.side, g.state, g.outcome, g.failure_class, g.bytes, g.failure_trace "
		f"FROM `tab{GENERATION}` g JOIN (SELECT MAX(name) AS newest FROM `tab{GENERATION}` "
		"GROUP BY company, side) n ON g.name = n.newest"
	)
	return {
		(company, side): {
			"state": state,
			"outcome": outcome,
			"failure_class": failure,
			"bytes": size,
			"failure_trace": trace,
		}
		for company, side, state, outcome, failure, size, trace in rows
	}


def generation_counts() -> dict:
	"""`{(company, side): {state: count}}` over every generation row that is still kept."""
	counts: dict = defaultdict(dict)
	for company, side, state, total in frappe.db.sql(
		f"SELECT company, side, state, COUNT(*) FROM `tab{GENERATION}` GROUP BY company, side, state"
	):
		counts[(company, side)][state] = total
	return dict(counts)


def duration_p95() -> dict:
	"""`{(company, side): p95 of duration_ms}` over the finished generations of the last `P95_DAYS` days."""
	found: dict = defaultdict(list)
	for company, side, ms in frappe.db.sql(
		f"SELECT company, side, duration_ms FROM `tab{GENERATION}` WHERE duration_ms > 0 "
		"AND state IN ('current', 'retired') AND as_of >= NOW(6) - INTERVAL %s DAY "
		"ORDER BY name DESC LIMIT %s",
		(P95_DAYS, MAX_GENERATIONS),
	):
		found[(company, side)].append(ms)
	return {key: nearest_rank(values, 0.95) for key, values in found.items()}


def nearest_rank(values: list, share: float):
	"""The nearest-rank percentile of `values`; None for no values."""
	if not values:
		return None
	ordered = sorted(values)
	rank = min(max(math.ceil(len(ordered) * share), 1), len(ordered))
	return ordered[rank - 1]


def oracle_records(days: int = ORACLE_DAYS) -> list[dict]:
	"""The stored night records of the last `days` days, newest first. Name-free: fixed words and counts only."""
	records = []
	for company, side, generation, build, age, text, lines, rows in frappe.db.sql(
		f"SELECT company, side, name, cache_build, TIMESTAMPDIFF(SECOND, as_of, NOW(6)), shadow, ledger_lines, row_count "
		f"FROM `tab{GENERATION}` FORCE INDEX (as_of) WHERE shadow IS NOT NULL AND shadow != '' AND state != 'purged' "
		"AND as_of >= NOW(6) - INTERVAL %s DAY ORDER BY name DESC LIMIT %s",
		(days, MAX_GENERATIONS),
	):
		records.append(
			{
				"company": company,
				"side": side,
				"generation": generation,
				"cache_build": build,
				"age_s": age,
				"within_size": cache_state.within_measured_size(side, lines or 0, rows or 0),
				**_record(text),
			}
		)
	return records


def _record(text) -> dict:
	try:
		record = json.loads(text)
		counts = record.get("counts")
		return {
			"outcome": str(record.get("outcome")),
			"reason": str(record["reason"]) if record.get("reason") else None,
			"counts": counts if isinstance(counts, dict) else {},
			"operator": record.get("source") == cache_store.OPERATOR_SOURCE,
		}
	except (TypeError, ValueError, AttributeError):
		return {"outcome": "unreadable", "reason": None, "counts": {}, "operator": False}


def oracle_by_side() -> dict:
	"""Per side: the last outcome and reason, and the nights by outcome counted on the deployed build.

	A record of an operator build (`source: operator`) shows as the last outcome but is no night: it counts in
	neither `on_build` nor `nights`. A record of a generation above the measured size is the same (E10).
	"""
	build = routing.cache_build()
	found: dict = {}
	for record in oracle_records():
		key = (record["company"], record["side"])
		entry = found.setdefault(
			key, {"last": record["outcome"], "reason": record["reason"], "on_build": 0, "nights": Counter()}
		)
		if record["cache_build"] == build and not record["operator"] and record["within_size"]:
			entry["on_build"] += 1
			entry["nights"][record["outcome"]] += 1
	return {key: {**entry, "nights": dict(entry["nights"])} for key, entry in found.items()}


def size_gates() -> dict:
	"""`{(company, side): "within" | "above_measured"}` by the current generation of each head."""
	gates = {}
	for company, side, lines, rows in frappe.db.sql(
		"SELECT g.company, g.side, g.ledger_lines, g.row_count "
		f"FROM `tab{GENERATION}` g JOIN `tab{HEAD}` h ON g.name = h.current_generation"
	):
		within = cache_state.within_measured_size(side, lines or 0, rows or 0)
		gates[(company, side)] = WITHIN if within else ABOVE_MEASURED
	return gates


def skip_counts(date: str) -> dict:
	"""`{reason: count}` of the refreshes skipped on `date` (the counters of `cache.count_skip`); {} on a cache error."""
	prefix = f"jarvis:pcal:site:day:{date}:{SKIP_CLASS}"
	try:
		found = {}
		for key in cache_state.scan_keys(prefix):
			text = key.decode() if isinstance(key, bytes) else str(key)
			value = frappe.cache.execute_command("GET", key)
			found[text.split(prefix, 1)[1]] = int(value or 0)
		return found
	except TIMEOUTS:
		raise
	except Exception:
		return {}


def digest_of(company: str) -> str | None:
	return read_log.company_digest_or(company)


def _sections() -> dict:
	merged: dict = {}
	for path in frappe.get_hooks(SECTIONS_HOOK) or []:
		try:
			section = frappe.get_attr(path)()
		except TIMEOUTS:
			raise
		except Exception as exc:
			frappe.logger("payment_calendar").warning(
				"cache_status section failed class=%s", type(exc).__name__
			)
			continue
		if isinstance(section, dict):
			merged.update(section)
	return merged
