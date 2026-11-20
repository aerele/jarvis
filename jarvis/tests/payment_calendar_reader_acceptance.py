"""Manual acceptance runs of the built-in reader (AC-10 to AC-12). Test code, never called at runtime.

Run through `bench --site <site> execute jarvis.tests.payment_calendar_reader_acceptance.<function> --kwargs ...`
(commands in the plan and in docs/architecture/payment-calendar.md). Each function checks its site FIRST, writes
a JSON file to `out` (outside the repository; companies as HMAC digests, rows by digest and field name, never
a party or voucher name), and returns a short summary without names.

Safety:
* `playground` and `timing` run only on `jarvis_playground.localhost`. The DB session is set READ ONLY for the
  whole run, so any write would fail loudly; the reads sit in `consistent_read()` views and nothing is written.
* `skip_eligibility=True` (acceptance only, default off) runs the reader for Administrator with the reader's
  eligibility call patched to "eligible" for the duration of the read, and records `eligibility_skipped`
  in the summary. It exists for a site where another app registers a permission hook that the gate (rightly)
  cannot audit: the run is then an exactness check of the reader against today's read, not an eligibility
  check. Every other guard stays: site check first, READ ONLY session, `consistent_read`, no writes.
* `drift` and `timing_guard_size` run only on `test_jarvis`. Their writes sit in a savepoint, under
  `write_guards` (a commit or full rollback raises), and are rolled back in `finally`.
* `drift` first makes sure the shared ledger company exists, OUTSIDE `write_guards`, and then commits: on a
  fresh site its creation makes HRMS run an ALTER TABLE, which commits by itself and would drop any savepoint
  taken before. That company is the only thing the run leaves behind; it exists before the before-probe, and
  both probes count companies.
* Each timed read runs in a forked child (`run_isolated`), so its memory figures are its own: a lifetime
  peak (`ru_maxrss`) would carry the fixture builder and earlier reads.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

import frappe
from frappe.utils import getdate, nowdate

from jarvis.payment_calendar import builtin_reader, compatibility, snapshot
from jarvis.tests import _erpnext_masters as masters
from jarvis.tests.payment_calendar_acceptance_support import (
	PLAYGROUND_SITE,
	SIDES,
	TEST_SITE,
	TOP_TIMERS,
	attempt,
	digest,
	error_label,
	peak_rss_mb,
	prepare_out,
	read_both,
	read_only_session,
	reader_record,
	require_site,
	run_isolated,
	stale_headers,
	today_lifted,
	window,
	write_out,
)
from jarvis.tests.payment_calendar_drift import frozen_clock, run_drift, write_guards
from jarvis.tests.payment_calendar_fixtures import sync_outstanding

FROZEN = "2025-06-16 10:00:00"
ANCHOR = date(2025, 6, 16)
DRAIN_EVERY = (1, 20)
SEED_SAVEPOINT = "acceptance_seed"
OUTER_SAVEPOINT = "acceptance_outer"
GUARD_SAVEPOINT = "acceptance_guard"
WARM_RUNS = 2


def playground(company: str, out: str, anchor: str | None = None, skip_eligibility: bool = False) -> dict:
	"""AC-10: both sides, reader against today's read (guards lifted) in one read view per side. Read only.

	`skip_eligibility`: see the module docstring; Administrator only.
	"""
	require_site(PLAYGROUND_SITE)
	_require_skip_allowed(skip_eligibility)
	path = prepare_out(out)
	anchor = str(getdate(anchor or nowdate()))
	_require_company(company)
	with read_only_session(), _eligibility_skipped(skip_eligibility):
		sides = {side: _compare_side(company, side, anchor) for side in SIDES}
	summary = {
		"site": "playground",
		"company": digest(company),
		"eligibility_skipped": skip_eligibility,
		"anchor": anchor,
		"diffs": sum(record["diffs"] for record in sides.values()),
		"fallbacks": [record["fallback"] for record in sides.values() if record["fallback"]],
		"control_passed": all(record["control_passed"] for record in sides.values()),
		"contract": {side: record["contract"] for side, record in sides.items()},
		"settled_rows_equal": all(
			record["settled_rows"]["reader"] == record["settled_rows"]["today"] for record in sides.values()
		),
		"stale_headers": {side: record["headers"]["stale"] for side, record in sides.items()},
	}
	write_out(path, {"summary": summary, "sides": sides})
	return summary


def _clean_up(savepoint: str) -> None:
	"""Roll back to the savepoint, roll back the transaction, clear the cache: each step even if one fails.

	`frappe.clear_cache()` clears the whole site cache (on `jarvis_playground` too), not only this run's.
	"""
	try:
		frappe.db.rollback(save_point=savepoint)
	finally:
		try:
			frappe.db.rollback()
		finally:
			frappe.clear_cache()


def drift(seed: int, ops: int, out: str) -> dict:
	"""AC-11: a seeded run of real controllers, reader against today's read on every drain. Test site only."""
	require_site(TEST_SITE)
	path = prepare_out(out)
	sides, started = [], time.monotonic()
	before = after = None
	# Setup, outside `write_guards` (module docstring). Committed, so both probes see the company.
	company = masters.ensure_ledger_company()
	frappe.db.commit()
	frappe.db.savepoint(OUTER_SAVEPOINT)
	try:
		before = _probe(company)
		frappe.db.savepoint(SEED_SAVEPOINT)
		try:
			# The seed (frozen date, no commit) and its drains, as in the CI drift test.
			with frozen_clock(FROZEN), write_guards() as guards:
				run = run_drift(
					seed,
					ops,
					anchor=ANCHOR,
					drain_every=DRAIN_EVERY,
					on_drain=lambda done: sides.extend(_drain(company, done)),
				)
		finally:
			frappe.db.rollback(save_point=SEED_SAVEPOINT)
	finally:
		_clean_up(OUTER_SAVEPOINT)
	after = _probe(company)
	outcomes = Counter(record["outcome"] for record in sides)
	summary = {
		"site": "test_jarvis",
		"company": digest(company),
		"seed": seed,
		"ops": run["ops"],
		"failures": run["failures"],
		"drain_sides": len(sides),
		"outcomes": dict(outcomes),
		"diffs": sum(record["diffs"] for record in sides),
		"fallbacks": outcomes["fallback"],
		"control_passed": all(
			record["control_passed"] for record in sides if record["outcome"] == "compared"
		),
		"isolation_equal": before == after,
		"commits": guards["commits"] + guards["rollbacks"] + guards["transaction_sql"],
		"seconds": round(time.monotonic() - started, 1),
	}
	write_out(
		path, {"summary": summary, "run": run, "drains": sides, "probe": {"before": before, "after": after}}
	)
	return summary


def timing(
	company: str, out: str, runs: int = 3, anchor: str | None = None, skip_eligibility: bool = False
) -> dict:
	"""AC-12 at a real company: run 1 per side is cold (cache cleared first), runs 2..n are warm. Read only.

	`skip_eligibility`: see the module docstring; Administrator only. The forked children inherit the patch.
	"""
	require_site(PLAYGROUND_SITE)
	_require_skip_allowed(skip_eligibility)
	path = prepare_out(out)
	anchor = str(getdate(anchor or nowdate()))
	_require_company(company)
	with read_only_session(), _eligibility_skipped(skip_eligibility):
		sides = {side: _timed_runs(company, side, anchor, runs, view=True) for side in SIDES}
	summary = {**_timing_summary("playground", company, sides), "eligibility_skipped": skip_eligibility}
	write_out(path, {"summary": summary, "sides": sides})
	return summary


def timing_guard_size(out: str, vouchers: int = 10_000, ledger_rows: int = 100_000) -> dict:
	"""AC-12 at the guard size, per side: `vouchers` invoices and `ledger_rows` ledger rows. Test site only.

	No-regression rule: the reader must be no slower than today's read (guards lifted, as in the differential)
	under the same load. For each run and side both reads run back to back in forked children, the order
	alternating (reader first on odd runs, today first on even runs) to cancel cache warm-up bias. The summary
	has `no_regression` (max of the reader <= max of today, every side) and `no_regression_median`.

	The fixture is raw-inserted in a savepoint and rolled back in `finally` (a read view would roll the
	fixture back, so the reads run directly, in forked children that borrow this connection). The memory
	of a read is the growth of its own child; `memory_mb` also holds the parent's lifetime peaks.
	"""
	from jarvis.tests.payment_calendar_guard_fixture import GuardCompany

	require_site(TEST_SITE)
	path = prepare_out(out)
	frappe.db.savepoint(GUARD_SAVEPOINT)
	started, company = time.monotonic(), None
	# `start` and `after_fixture` are this process's LIFETIME peaks (the fixture builder lives here); the
	# memory of a read is `read_growth_mb` of its run, measured in its forked child.
	stages = {"start": peak_rss_mb()}
	try:
		with write_guards() as guards:
			builder = GuardCompany(vouchers, ledger_rows)
			company = builder.fx.company
			built = builder.build()
			stages["fixture_seconds"], stages["after_fixture"] = (
				round(time.monotonic() - started, 1),
				peak_rss_mb(),
			)
			changed = sync_outstanding(company)
			anchor = str(getdate(builder.fx.today))
			sides = {side: _paired_runs(company, side, anchor, 1 + WARM_RUNS) for side in SIDES}
			stages["max_read_growth_mb"] = max(
				run["read_growth_mb"] for data in sides.values() for run in data["runs"]
			)
	finally:
		_clean_up(GUARD_SAVEPOINT)
	leftover = frappe.db.count("Payment Ledger Entry", {"company": company}) if company else 0
	summary = {
		**_timing_summary("test_jarvis guard size", company, sides),
		"vouchers_per_side": vouchers,
		**_regression_summary(sides),
		"headers_changed_by_sync": changed,
		"cleaned": leftover == 0 and guards["commits"] == 0,
	}
	write_out(path, {"summary": summary, "fixture": built, "memory_mb": stages, "sides": sides})
	return summary


# ---------------------------------------------------------------------------------------------- AC-10
def _compare_side(company: str, side: str, anchor: str) -> dict:
	"""Reader and today's read in ONE read view, then the contract collect applies to a source."""
	args = (company, side, anchor, *window(getdate(anchor)))
	with snapshot.consistent_read() as hits:
		read = read_both(args)
		record = reader_record(read, args)
		record["contract"] = _contract_code(hits, read["reader"][0], company, side)
		record["headers"] = stale_headers(company, side)
	return record


def _contract_code(hits, result, company: str, side: str) -> str | None:
	"""None when `check_source_contract` accepts the reader's result in this view, else the refusal code."""
	if result is None:
		return "no_result"
	from erpnext.accounts.utils import get_currency_precision

	precision = get_currency_precision() or 2
	currency = frappe.get_cached_value("Company", company, "default_currency")
	try:
		snapshot.check_source_contract(
			hits, {side: result}, None, currency, precision, compatibility.source_profile()
		)
	except Exception as error:
		return error_label(error)
	return None


# ---------------------------------------------------------------------------------------------- AC-11
def _drain(company: str, done: int) -> list[dict]:
	"""Both sides at a drain point; the seed's rows are uncommitted, so no read view and no header sync."""
	records = []
	for side in SIDES:
		args = (company, side, str(ANCHOR), *window(ANCHOR))
		record = reader_record(read_both(args), args)
		# The seed is one uncommitted, rolled-back transaction on a tiny ledger: the timings of a drain say
		# nothing about speed, so the summary carries counts and outcomes only.
		for frozen in ("timings_ms", "reader_seconds"):
			record.pop(frozen, None)
		records.append({"op": done, "side": side, **record})
	return records


def _probe(company: str) -> dict:
	"""Committed-state evidence; equal before and after the seed means nothing leaked."""
	counts = {
		doctype: frappe.db.count(doctype, {"company": company})
		for doctype in ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry")
	}
	counts["Payment Ledger Entry"] = frappe.db.count("Payment Ledger Entry", {"company": company})
	counts["Company"] = frappe.db.count("Company")
	counts["series"] = [
		list(row) for row in frappe.db.sql("select name, current from tabSeries order by name")
	]
	return counts


# ---------------------------------------------------------------------------------------------- AC-12
def _timed_runs(company: str, side: str, anchor: str, runs: int, *, view: bool) -> dict:
	"""`runs` reader reads of one side: the first cold (cache cleared), the rest warm.

	`frappe.clear_cache()` clears the whole site cache, so on the playground a run also cools every
	other user of that site's Redis cache for a moment.
	"""
	args = (company, side, anchor, *window(getdate(anchor)))
	frappe.clear_cache()
	records = []
	for number in range(1, runs + 1):
		records.append({"run": number, "kind": "cold" if number == 1 else "warm", **_timed_read(args, view)})
	return {"runs": records, "max_seconds": max(record["seconds"] for record in records)}


def _paired_runs(company: str, side: str, anchor: str, runs: int) -> dict:
	"""`runs` pairs of reads of one side at guard size, back to back, each read in its own forked child.

	Odd runs read the reader first, even runs today's read first. Each run record is the reader's record
	(as `_timed_runs` gives) with today's under `today`.
	"""
	args = (company, side, anchor, *window(getdate(anchor)))
	frappe.clear_cache()
	records = []
	for number in range(1, runs + 1):
		reader_first = number % 2 == 1
		done = {}
		for name in ("reader", "today") if reader_first else ("today", "reader"):
			done[name] = _timed_read(args, False, today=name == "today")
		records.append(
			{
				"run": number,
				"kind": "cold" if number == 1 else "warm",
				"order": "reader_first" if reader_first else "today_first",
				**done["reader"],
				"today": done["today"],
			}
		)
	return {"runs": records, "max_seconds": max(record["seconds"] for record in records)}


def _timed_read(args: tuple, view: bool, today: bool = False) -> dict:
	"""One reader read in a forked child: its time, per-step timings and its OWN memory (`read_growth_mb`).

	`view=True` (committed data, read only): the child opens its own connection. `view=False` (the parent's
	uncommitted fixture): the child borrows the parent's. See `run_isolated`.
	"""
	work, memory = run_isolated(lambda: _measure_read(args, view, today), reopen=view)
	return {**work, **memory}


def _measure_read(args: tuple, view: bool, today: bool = False) -> dict:
	"""Runs in the child: the read timed as a whole (view setup included) with the reader's own timings.

	`today=True` times today's read (`today_lifted`, guards lifted) instead; it has no reader timings.
	"""
	stats = {}
	if view:
		# The parent's read-only session does not reach the child's new connection.
		frappe.db.sql("SET SESSION TRANSACTION READ ONLY")

	def read():
		if today:
			return today_lifted(*args)
		if view:
			with snapshot.consistent_read():
				return builtin_reader.read_obligations(*args, stats=stats)
		return builtin_reader.read_obligations(*args, stats=stats)

	started = time.perf_counter()
	result, error = attempt(read)
	seconds = time.perf_counter() - started
	timings = stats.get("timings_ms", {})
	steps = sum(timings.get(name, 0) for name in TOP_TIMERS)
	return {
		"seconds": round(seconds, 3),
		"outcome": error_label(error) if error else "served",
		"rows": len(result["rows"]) if result else None,
		"timings_ms": timings,
		# Gates, the size-independent checks and today's result assembly: whatever the timers above do not cover.
		"other_ms": round(seconds * 1000 - steps, 1),
		"counts": stats.get("counts", {}),
		"control": stats.get("control", {}),
	}


def _timing_summary(site: str, company: str, sides: dict) -> dict:
	records = [run for side in sides.values() for run in side["runs"]]
	return {
		"site": site,
		"company": digest(company),
		"max_seconds": max(run["seconds"] for run in records),
		"within_30s": all(run["seconds"] <= 30 for run in records),
		"served": all(run["outcome"] == "served" for run in records),
		"control_passed": all(run["control"].get("passed") for run in records),
		"max_read_growth_mb": max(run["read_growth_mb"] for run in records),
		"max_read_peak_mb": max(run["peak_rss_mb"] for run in records),
		"per_side_max_seconds": {side: data["max_seconds"] for side, data in sides.items()},
	}


def _regression_summary(sides: dict) -> dict:
	"""AC-12 no-regression figures per side: the reader's and today's max and median seconds, and read growth."""
	per_side = {}
	for side, data in sides.items():
		reader = [run["seconds"] for run in data["runs"]]
		today = [run["today"]["seconds"] for run in data["runs"]]
		per_side[side] = {
			"reader_max_seconds": max(reader),
			"today_max_seconds": max(today),
			"reader_median_seconds": statistics.median(reader),
			"today_median_seconds": statistics.median(today),
			"reader_seconds": reader,
			"today_seconds": today,
			"reader_growth_mb": [run["read_growth_mb"] for run in data["runs"]],
			"today_growth_mb": [run["today"]["read_growth_mb"] for run in data["runs"]],
			"today_outcomes": [run["today"]["outcome"] for run in data["runs"]],
			"order": [run["order"] for run in data["runs"]],
		}
	return {
		"no_regression": all(r["reader_max_seconds"] <= r["today_max_seconds"] for r in per_side.values()),
		"no_regression_median": all(
			r["reader_median_seconds"] <= r["today_median_seconds"] for r in per_side.values()
		),
		"per_side": per_side,
	}


def _require_skip_allowed(skip_eligibility: bool) -> None:
	"""The eligibility bypass is for Administrator only (for whom the gate narrows nothing by design)."""
	if skip_eligibility and frappe.session.user != "Administrator":
		raise RuntimeError("skip_eligibility is for the Administrator session only")


@contextmanager
def _eligibility_skipped(skip: bool):
	"""While active the reader's eligibility call reports no reason; off by default, then a no-op."""
	if not skip:
		yield
		return
	with patch.object(builtin_reader, "reader_ineligibility", return_value=[]):
		yield


def _require_company(company: str) -> None:
	if not frappe.db.exists("Company", company):
		raise RuntimeError("no such company on this site")
