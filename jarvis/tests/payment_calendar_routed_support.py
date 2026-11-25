"""Helpers of the S2 acceptance runs (`routed_playground`, `shadow_once`, `limits_at_guard_size`).

Test code only. The output names no company, party, voucher or user: identities are the digests of
`payment_calendar_acceptance_support`. The playground runs write nothing to the database (the caller holds a
READ ONLY session); the routing keys the router sets in Redis are deleted at the end, and the shadow run holds
the lease key only while it runs.
"""

from __future__ import annotations

import json
import secrets
import time
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import getdate

from jarvis.payment_calendar import builtin_reader, routing, shadow, shadow_state, snapshot, sources
from jarvis.payment_calendar.errors import TIMEOUTS
from jarvis.tests.payment_calendar_acceptance_support import (
	SIDES,
	attempt,
	compare_reads,
	current_rss_mb,
	error_label,
	peak_rss_mb,
	window,
)

HOOK_PATH = "jarvis.tests.payment_calendar_routed_support.read_neutral_hook"
# The limits the plan sets (AC-4); figures are recorded as booleans, and the CI tests never assert wall-clock.
DEADLINE_RUN_S = 1
DEADLINE_END_S = 16
JOB_LIMIT_S = 150
CAP_RSS_MB = 1024
LONGEST_STATEMENT_S = 15


def read_neutral_hook(doc=None, ptype=None, user=None, debug=False):
	"""A `has_permission` hook that never hides a document (Frappe reads a falsy answer as a denial)."""
	return True


@contextmanager
def neutral_wildcard_hook():
	"""`frappe.get_hooks` with a read-neutral `"*"` `has_permission` hook added; every other call real."""
	real = frappe.get_hooks

	def get_hooks(hook=None, *args, **kwargs):
		found = real(hook, *args, **kwargs)
		if hook == "has_permission":
			return {**found, "*": [*found.get("*", []), HOOK_PATH]}
		return found

	with patch("frappe.get_hooks", get_hooks):
		yield


@contextmanager
def mode_on(company: str):
	"""The router's switch patched to `on` with `company` served: nothing is saved."""
	with patch.object(routing, "reader_settings", return_value=routing.ReaderSettings("on", (company,), ())):
		yield


@contextmanager
def statement_timer():
	"""Yields a dict with `max_seconds` (the longest statement) and `count`, for the statements run inside."""
	seen = {"max_seconds": 0.0, "count": 0}
	original = frappe.db.sql

	def timed(*args, **kwargs):
		started = time.perf_counter()
		try:
			return original(*args, **kwargs)
		finally:
			seen["count"] += 1
			seen["max_seconds"] = max(seen["max_seconds"], time.perf_counter() - started)

	with patch.object(frappe.db, "sql", timed):
		yield seen


def temporary_user(prefix: str) -> str:
	"""An enabled Accounts Manager and Jarvis User, raw-inserted in the caller's savepoint (rolled back there).

	Not `CalendarFixtures.user`: its controller save starts background machinery that makes a process that
	forks afterwards segfault at exit.
	"""
	email = f"{prefix.lower()}-limits@example.com"
	stamp = frappe.utils.now()
	common = {"creation": stamp, "modified": stamp, "owner": "Administrator", "modified_by": "Administrator"}
	frappe.get_doc(
		{
			"doctype": "User",
			"name": email,
			"email": email,
			"first_name": "Limits fixture",
			"enabled": 1,
			"user_type": "System User",
			**common,
		}
	).db_insert()
	for index, role in enumerate(("Accounts Manager", "Jarvis User"), start=1):
		frappe.get_doc(
			{
				"doctype": "Has Role",
				"name": f"{prefix}{index}hr",
				"parent": email,
				"parenttype": "User",
				"parentfield": "roles",
				"role": role,
				"idx": index,
				**common,
			}
		).db_insert()
	frappe.cache.hdel("roles", email)
	return email


@contextmanager
def session_user(user: str):
	original = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(original)


def clear_routing_keys(company: str, owned: dict[bytes, str]) -> int:
	"""Delete this run's memos only while their tokens still match; return the number deleted.

	`read_side` releases its slots and in-flight keys in `finally`, with a token check.
	A slot that remains can belong to a live worker, so cleanup must leave it alone.
	"""
	keys = [routing.memo_key(company, side) for side in SIDES]
	return sum(int(frappe.cache.eval(routing._RELEASE, 1, key, owned[key])) for key in keys if key in owned)


@contextmanager
def owned_routing_memos(company: str):
	"""Track this run's memo tokens, including fallbacks, so cleanup cannot delete another worker's memo."""
	owned = {}
	keys = {routing.memo_key(company, side) for side in SIDES}
	original = routing._remember

	def remember(key, ttl, value="1"):
		if key in keys:
			value = secrets.token_hex(16) if value == "1" else value
			owned[key] = value
		return original(key, ttl, value)

	try:
		with patch.object(routing, "_remember", remember):
			yield
	finally:
		clear_routing_keys(company, owned)


# ---------------------------------------------------------------------------------- routed playground


def routed_sides(company: str, anchor: str, user: str) -> dict[str, dict]:
	"""Both sides through the router in ONE read view as `user`, each compared with today's lifted read.

	The caller holds a READ ONLY session. The mode is patched on for `company`.
	"""
	with owned_routing_memos(company), session_user(user), mode_on(company), snapshot.consistent_read():
		started = time.monotonic()
		return {side: _routed_side(company, side, anchor, started) for side in SIDES}


def _routed_side(company: str, side: str, anchor: str, started: float) -> dict:
	args = (company, side, anchor, *window(getdate(anchor)))
	outcomes: list[dict] = []
	began = time.perf_counter()
	routed = attempt(routing.read_side, snapshot.read_obligations, *args, started=started, outcomes=outcomes)
	seconds = time.perf_counter() - began
	oracle = attempt(shadow.today_unguarded, *args)
	found = compare_reads(routed, oracle)
	record = outcomes[0] if outcomes else {}
	return {
		"path": record.get("path"),
		"outcome": error_label(routed[1]) if routed[1] else "served",
		"step": record.get("step"),
		"fallback": record.get("fallback"),
		"seconds": round(seconds, 3),
		"today_ms": record.get("today_ms"),
		"reader_ms": record.get("reader_ms"),
		"counts": record.get("counts") or {},
		"diffs": found["diffs"],
		"ids": found.get("ids"),
		"rows": len(routed[0]["rows"]) if routed[0] else None,
		"oracle_rows": len(oracle[0]["rows"]) if oracle[0] else None,
	}


# ---------------------------------------------------------------------------------------- shadow once


def shadow_units_once(company: str) -> dict:
	"""Every unit (both sides, four scopes) of `company` once, under the shadow lease. Writes no record.

	Calls the unit's measurement only: no in-progress marker, night state, Error Log row or settings read.
	Returns the unit records and whether the lease key is gone.
	"""
	token = shadow_state.take_lease()
	if token is None:
		raise RuntimeError("a shadow unit is running; try again later")
	try:
		records = {
			f"{side}:{scope}": shadow._measure(company, side, scope)
			for side in shadow.SIDES
			for scope in shadow.SCOPES
		}
	finally:
		shadow_state.release_lease(token)
	return {"units": records, "lease_deleted": not shadow_state.lease_held()}


def shadow_summary(units: dict[str, dict]) -> dict:
	"""Counts only: compared units per side and scope, diffs, would-deadline units and the peak memory."""
	compared = {}
	for field, record in units.items():
		compared[field] = record["outcome"] == "compared"
	return {
		"units": len(units),
		"compared": sum(compared.values()),
		"compared_by_unit": compared,
		"diffs": sum(record["outcome"] == "diff" for record in units.values()),
		"not_compared": {
			field: record["reason"] for field, record in units.items() if record["outcome"] == "not_compared"
		},
		"would_deadline": sum(bool(record.get("would_deadline")) for record in units.values()),
		"max_rss_mb": max((record.get("rss_mb") or 0) for record in units.values()),
		"all_sides_and_scopes_compared": all(compared.values()),
	}


# ------------------------------------------------------------------------------------- guard-size limits


def deadline_read(args: tuple, user: str) -> dict:
	"""Child work: the reader with a deadline of now + `DEADLINE_RUN_S`, as `user`. Expect `fallback:deadline`."""
	stats: dict = {}
	started = time.perf_counter()
	with session_user(user), neutral_wildcard_hook(), statement_timer() as statements:
		_, error = attempt(
			builtin_reader.read_obligations,
			*args,
			stats=stats,
			deadline=time.monotonic() + DEADLINE_RUN_S,
		)
	seconds = time.perf_counter() - started
	return {
		"seconds": round(seconds, 3),
		"outcome": error_label(error) if error else "served",
		"longest_statement_s": round(statements["max_seconds"], 3),
		"timings_ms": stats.get("timings_ms", {}),
	}


def routed_job(company: str, anchors: dict[str, tuple], user: str) -> dict:
	"""Child work: both sides through the router as `user`, as `collect` calls it, without a read view.

	`anchors` maps a side to the read arguments after the company and side. The whole job time, each side's
	outcome and the snapshot bytes of a served side are recorded; the router's own record says the path.
	"""
	outcomes: list[dict] = []
	started, sides = time.monotonic(), {}
	with (
		owned_routing_memos(company),
		session_user(user),
		neutral_wildcard_hook(),
		mode_on(company),
		statement_timer() as statements,
	):
		for side in SIDES:
			args = (company, side, *anchors[side])
			began = time.perf_counter()
			result, error = attempt(
				routing.read_side, snapshot.read_obligations, *args, started=started, outcomes=outcomes
			)
			sides[side] = {
				"seconds": round(time.perf_counter() - began, 3),
				"outcome": error_label(error) if error else "served",
				"path": outcomes[-1].get("path") if outcomes else None,
				"rows": len(result["rows"]) if result else None,
				"snapshot_bytes": len(json.dumps(result, default=str).encode("utf-8")) if result else None,
			}
	return {
		"job_seconds": round(time.monotonic() - started, 3),
		"sides": sides,
		"records": [_public(record) for record in outcomes],
		"longest_statement_s": round(statements["max_seconds"], 3),
		"statements": statements["count"],
		"rss_mb": current_rss_mb(),
		"peak_rss_mb": peak_rss_mb(),
	}


def _public(record: dict) -> dict:
	"""The router's record without anything that could carry a name."""
	return {
		key: record.get(key)
		for key in ("side", "path", "outcome", "step", "fallback", "today_ms", "reader_ms", "counts")
	}


def limits_summary(deadline: dict, job: dict, cap: dict) -> dict:
	"""AC-4 figures; a pass requires both reader results in the guard and cap runs."""
	timeouts = {f"error:{timeout.__name__}" for timeout in TIMEOUTS}
	checks = {
		"deadline_ended_in_time": deadline["outcome"] == "fallback:deadline"
		and deadline["seconds"] <= DEADLINE_END_S,
		"job_within_150s": job["job_seconds"] < JOB_LIMIT_S,
		"job_has_no_timeout": all(side["outcome"] not in timeouts for side in job["sides"].values()),
		"cap_has_no_timeout": all(side["outcome"] not in timeouts for side in cap["sides"].values()),
		"job_both_readers_served": _both_readers_served(job),
		"cap_both_readers_served": _both_readers_served(cap),
		"cap_peak_under_1gb": cap["peak_rss_mb"] < CAP_RSS_MB,
		"cap_longest_statement_under_15s": cap["longest_statement_s"] < LONGEST_STATEMENT_S,
	}
	return {
		**checks,
		"pass": all(checks.values()),
		"deadline_outcome": deadline["outcome"],
		"deadline_seconds": deadline["seconds"],
		"job_seconds": job["job_seconds"],
		"job_peak_rss_mb": job["peak_rss_mb"],
		"snapshot_bytes": {side: data["snapshot_bytes"] for side, data in job["sides"].items()},
		"cap_snapshot_bytes": {side: data["snapshot_bytes"] for side, data in cap["sides"].items()},
		"cap_job_seconds": cap["job_seconds"],
		"cap_peak_rss_mb": cap["peak_rss_mb"],
		"cap_longest_statement_s": cap["longest_statement_s"],
	}


def _both_readers_served(run: dict) -> bool:
	for side in SIDES:
		data = run["sides"].get(side, {})
		if data.get("path") != "reader" or data.get("outcome") != "served" or not data.get("snapshot_bytes"):
			return False
	return True
