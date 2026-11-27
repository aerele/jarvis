"""Nightly shadow: the built-in reader against today's read with its size guards lifted.

A company's last unit is the bank unit: today's `get_balance_on` read of its bank and cash accounts against
the one-pass bank read (`banks.py`), both in one view. The one pass never serves in `shadow` mode, and the
unit never serves either: it compares and records.

`dispatch` runs from one cron entry every 15 minutes between 02:00 and 05:59 site time. It starts the night's
chain or resumes it. A unit is one (company, side, scope), or the company's `Bank:all` unit; one RQ job runs one unit, then enqueues
the next.
Both reads of a unit run in ONE `consistent_read()` view as Administrator, so the comparison proves data
exactness only; the per-user proof is the reply-level matrix and the visitor flow. Nothing is written inside
the view. Records hold counts and HMAC digests, never a company, party or voucher name.

The gate (`summary`) is what lets a company move from the shadow list to the served list.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from contextlib import contextmanager
from datetime import date as Date
from functools import partial
from time import monotonic, time

import frappe
from frappe.utils import add_days, add_months, get_first_day

from jarvis.payment_calendar import banks, books, builtin_reader, read_log, routing, shadow_state
from jarvis.payment_calendar import sources as today_sources
from jarvis.payment_calendar.compatibility import bank_one_pass_blocker, source_profile
from jarvis.payment_calendar.errors import TIMEOUTS, CalendarSourceUnavailable, ReaderFallback
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.snapshot import consistent_read

SIDES = ("Payable", "Receivable")
SCOPES = ("previous", "current", "next", "window")
# The bank unit of a company: one per night, after the eight side units.
BANK_UNIT = ("Bank", "all")
BANK_FIELD = ":".join(BANK_UNIT)
COMPANY_UNITS = (*((side, scope) for side in SIDES for scope in SCOPES), BANK_UNIT)
# The size guards of today's read, lifted for the oracle: no real ledger reaches this.
LIFTED = 10**12
# The oracle (today's read, guards lifted) holds the whole ledger in memory and runs on the one `long` worker,
# so the shadow takes no company above today's own ledger guard.
SHADOW_LEDGER_CAP = today_sources.MAX_LEDGER_ROWS
READER_STOP_S = 60
WOULD_DEADLINE_S = routing.READER_DEADLINE_S
RSS_CAP_MB = 800
NIGHT_BUDGET_S = 1_800
UNIT_TIMEOUT_S = 600
MAX_DIGESTS = 20
# A unit that failed on a database error is tried again, up to this many runs in all.
RETRY_MAX = 2
DIFF_TITLE = "Payment Calendar reader shadow diff"
FALLBACK_TITLE = "Payment Calendar reader shadow fallback"
INCOMPLETE_TITLE = "Payment Calendar reader shadow incomplete"
DAILY_TITLE = "Payment Calendar reader daily"
JOB = "jarvis.payment_calendar.shadow.run_unit"
# Reasons of a skipped unit that need an operator: a row once per company, night and reason.
_FALLBACK_REASONS = ("ineligible", "shadow_too_large", "rss_cap", "unpinned", "one_pass_unavailable")
TRANSIENT = "error_transient"
# Fallbacks that say the company is too slow or too big for the reader: they fail the gate on their own.
_SLOW_REASONS = ("fallback:deadline", "fallback:assemble_budget")


def _today() -> str:
	"""The site date (the cron runs after midnight in site time): `routing._today`, the one date seam."""
	return routing._today()


# ------------------------------------------------------------------------------------------ dispatch


def dispatch() -> None:
	"""Write yesterday's daily row, then start tonight's chain or resume it. Idempotent."""
	night = _today()
	current = routing.reader_settings()
	_write_daily_rows(add_days(night, -1), current)
	units = _runnable_units(current, night)
	if units is None or shadow_state.lease_held():
		return
	info = _open_night(units, night)
	if info is None:
		return
	next_unit = _after_killed(units, night, info["cursor"])
	if next_unit < len(units):
		_chain(units, night, next_unit)
	else:
		_retry_transients(units, night)


def _runnable_units(current: routing.ReaderSettings, night: str | None = None) -> list | None:
	"""The night's units, or None when the shadow must not run: v15, mode off, no company or no key."""
	if current.mode == "off" or not frappe.conf.get("encryption_key"):
		return None
	try:
		if source_profile().major != "16":
			return None
	except CalendarSourceUnavailable:
		return None
	return units_for(current, night) or None


def units_for(current: routing.ReaderSettings, night: str | None = None) -> list[tuple[str, str, str]]:
	"""Every (company, side, scope) of the night; a company's nine units (the bank unit last) stay together.

	With a `night` the companies start at an offset that moves by one each night, so the last company of one
	night is never the last of every night (the night budget would starve it). Without one the order is plain.
	"""
	companies = sorted({*current.served, *current.shadow})
	if night and companies:
		offset = Date.fromisoformat(night).toordinal() % len(companies)
		companies = companies[offset:] + companies[:offset]
	return [(company, side, scope) for company in companies for side, scope in COMPANY_UNITS]


def month_keys(today: str) -> tuple[str, str, str]:
	"""The previous, current and next month (`YYYY-MM`) of a site date."""
	first = get_first_day(today)
	return tuple(str(add_months(first, offset))[:7] for offset in (-1, 0, 1))


def _units_hash(units: list) -> str:
	"""A keyed digest of the companies of the night (no name is stored)."""
	return read_log.ops_digest("\n".join(sorted({company for company, _, _ in units})))


def _open_night(units: list, night: str) -> dict | None:
	"""The night's state, started if new. A changed unit list restarts it once; a second change stops it."""
	wanted = _units_hash(units)
	info = shadow_state.night_info(night)
	if info is None:
		shadow_state.start_night(night, len(units), units_hash=wanted)
		return shadow_state.night_info(night)
	if info["count"] == len(units) and info["units"] in (None, wanted):
		return info
	if info["restarts"] < 1:
		shadow_state.start_night(night, len(units), units_hash=wanted, restarts=info["restarts"] + 1)
		_incomplete_row("units_changed", None, night)
		return shadow_state.night_info(night)
	_incomplete_row("units_changed_again", None, night)
	return None


def _after_killed(units: list, night: str, cursor: int) -> int:
	"""A unit with an `in_progress` marker and no lease was killed: record it and move on."""
	if cursor >= len(units):
		return cursor
	company, side, scope = units[cursor]
	digest = read_log.company_digest(company)
	marker = shadow_state.records(digest, night).get(f"{side}:{scope}")
	if marker and marker.get("state") == "in_progress":
		_store(digest, night, f"{side}:{scope}", _not_compared("killed"))
		_incomplete_row("killed", digest, night)
		cursor += 1
		shadow_state.set_cursor(night, cursor)
	return cursor


def _chain(units: list, night: str, n: int) -> None:
	"""Enqueue unit `n`, or cap every remaining unit when the night's budget is spent."""
	info = shadow_state.night_info(night)
	if info and time() - info["started"] > NIGHT_BUDGET_S:
		_cap_remaining(units, night, n)
		return
	_enqueue(night, n, f"pcal-shadow:{night}:{n}")


def _enqueue(night: str, n: int, job_id: str, **extra) -> None:
	frappe.enqueue(
		JOB,
		queue="long",
		timeout=UNIT_TIMEOUT_S,
		job_id=job_id,
		deduplicate=True,
		enqueue_after_commit=False,
		n=n,
		night=night,
		**extra,
	)


def _cap_remaining(units: list, night: str, first: int) -> None:
	for company, side, scope in units[first:]:
		digest = read_log.company_digest(company)
		if f"{side}:{scope}" not in shadow_state.records(digest, night):
			_store(digest, night, f"{side}:{scope}", _not_compared("time_cap"))
			_incomplete_row("time_cap", digest, night)
	shadow_state.set_cursor(night, len(units))


def _retry_transients(units: list, night: str) -> None:
	"""After the chain, start each unit that failed on a database error again, within the night's budget."""
	info = shadow_state.night_info(night)
	if info is None or time() - info["started"] > NIGHT_BUDGET_S:
		return
	for n, (company, side, scope) in enumerate(units):
		record = shadow_state.records(read_log.company_digest(company), night).get(f"{side}:{scope}")
		if record and record.get("state") == "retry":
			attempts = record.get("attempts", 1)
			if attempts >= RETRY_MAX:
				# Out of attempts (the last one was killed): close the unit as not compared.
				_store(
					read_log.company_digest(company), night, f"{side}:{scope}", {**record, "state": "done"}
				)
				continue
			_enqueue(night, n, f"pcal-shadow:{night}:{n}:r{attempts}", retry=True)
			return


def _incomplete_row(reason: str, digest: str | None, night: str) -> None:
	"""One Error Log row per company (or per night) and reason: the night did not finish its work."""
	read_log.error_log_once(
		INCOMPLETE_TITLE,
		f"reason={reason} company={digest or '-'} night={night}",
		f"shadow-incomplete:{digest or 'all'}:{night}",
		reason,
		window=read_log.OUTCOME_ROW_WINDOW,
		when_cache_down=False,
	)


# -------------------------------------------------------------------------------------------- one unit


def run_unit(n: int, night: str | None = None, retry: bool = False) -> None:
	"""Run unit `n` of the night under the lease, then enqueue the next one (not after a retry run).

	A job timeout leaves the in-progress marker and leaves the job as a fresh exception raised from here: the
	original carries frames with the company and the rows, and Frappe's runner stores a traceback with locals
	in developer mode.
	"""
	night = night or _today()
	token = shadow_state.take_lease()
	if token is None:
		return
	try:
		units = _run_locked(n, night, retry)
	except TIMEOUTS as exc:
		raise type(exc)() from None
	finally:
		shadow_state.release_lease(token)
	if units is not None and not retry and n + 1 < len(units):
		_chain(units, night, n + 1)


def _run_locked(n: int, night: str, retry: bool = False) -> list | None:
	"""Run one unit; returns the night's units when the chain goes on, else None."""
	units = _runnable_units(routing.reader_settings(), night)
	if units is None or n >= len(units):
		return None
	info = _open_night(units, night)
	if info is None:
		return None
	company, side, scope = units[n]
	digest, field = read_log.company_digest(company), f"{side}:{scope}"
	if time() - info["started"] > NIGHT_BUDGET_S:
		_cap_remaining(units, night, n)
		return None
	existing = shadow_state.records(digest, night).get(field)
	if existing and existing.get("state") == "done":
		return units
	retrying = bool(existing and existing.get("state") == "retry")
	attempts = existing.get("attempts", 0) if retrying else 0
	# A retry run keeps its `retry` record (with this attempt counted) as its marker: if the job is killed the
	# record is still one `_retry_transients` reads, and the attempt is not lost. A first run writes `in_progress`.
	marker = (
		{**existing, "attempts": attempts + 1}
		if retrying
		else {"state": "in_progress", "reader_build": _build(), "attempts": 0}
	)
	_store(digest, night, field, marker)
	record = _measure_safely(company, side, scope)
	if record["state"] == "retry":
		record["attempts"] = attempts + 1
		if record["attempts"] >= RETRY_MAX:
			record["state"] = "done"
	_finish(digest, night, field, record)
	if not retry:
		shadow_state.set_cursor(night, n + 1)
	return units


def _measure_safely(company: str, side: str, scope: str) -> dict:
	"""The unit's record. A job timeout propagates (the marker stays); any other error is a record."""
	try:
		return _measure(company, side, scope)
	except TIMEOUTS:
		raise
	except Exception as exc:
		return _error_record(exc)


def _finish(digest: str, night: str, field: str, record: dict) -> None:
	_store(digest, night, field, record)
	frappe.logger("payment_calendar").info(
		f"shadow_unit company={digest} unit={field} outcome={record['outcome']} reason={record.get('reason')} "
		f"today_ms={record.get('today_ms', 0)} reader_ms={record.get('reader_ms', 0)}"
	)
	try:
		_write_unit_row(digest, night, field, record)
	except TIMEOUTS:
		raise
	except Exception:
		# The record is stored; a failed row (a database blip) must not skip the commit or the next unit.
		frappe.logger("payment_calendar").warning("shadow_row_unavailable")
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- a background job, outside any request; one commit per unit


def _write_unit_row(digest: str, night: str, field: str, record: dict) -> None:
	reason = str(record.get("reason") or "")
	if record["outcome"] == "diff" or reason.startswith("error:"):
		_unit_row(DIFF_TITLE, f"shadow-diff:{digest}:{field}:{night}", "shadow", field, record, digest)
	elif reason.startswith("fallback:") or reason in _FALLBACK_REASONS:
		_unit_row(FALLBACK_TITLE, f"shadow-fallback:{digest}:{night}", reason, field, record, digest)


def _unit_row(title: str, operation: str, klass: str, field: str, record: dict, digest: str) -> None:
	read_log.error_log_once(
		title,
		f"unit={field} outcome={record['outcome']} reason={record.get('reason')} "
		f"counts={json.dumps(record.get('counts'), sort_keys=True)} digests={record.get('digests') or []} "
		f"company={digest} build={record['reader_build']}",
		operation,
		klass,
		window=read_log.OUTCOME_ROW_WINDOW,
		when_cache_down=False,
	)


def _measure(company: str, side: str, scope_key: str) -> dict:
	"""Both reads of one unit in one view, as Administrator; the record of what they showed."""
	original_user = frappe.session.user
	frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- the audited impersonation seam
	try:
		if (side, scope_key) == BANK_UNIT:
			return _in_view(partial(_read_bank, company), _compare_bank_paths)
		scope = _scope(company, scope_key)
		args = (company, side, scope["anchor"], scope["window_start"], scope["window_end"])
		return _in_view(partial(_read_sides, args), _compare_sides)
	finally:
		frappe.set_user(original_user)  # nosemgrep: frappe-setuser -- restores the caller


def _scope(company: str, scope_key: str) -> dict:
	months = month_keys(_today())
	month = {"previous": months[0], "current": months[1], "next": months[2]}.get(scope_key)
	return normalize_scope(company, [], month=month, check_accounts=False)


def _in_view(read, compare) -> dict:
	"""`read()` inside one `consistent_read()` view, then `compare(*reads)` once the view is closed.

	`read` returns the reads as a tuple, or a finished record (a dict) when the unit ends inside the view.
	A view that was lost is `source_changed`, whatever the reads showed.
	"""
	try:
		with consistent_read():
			reads = read()
	except CalendarSourceUnavailable as exc:
		if exc.code == "source_changed":
			return _not_compared("source_changed")
		raise
	except TIMEOUTS:
		raise
	except Exception as exc:
		return _error_record(exc)
	return reads if isinstance(reads, dict) else compare(*reads)


def _read_sides(args: tuple) -> dict | tuple:
	"""Today's lifted read and the reader of one side and scope, inside the view."""
	stats: dict = {}
	timing = {"today_ms": 0, "reader_ms": 0}
	if today_sources._ledger_rows_exceed(args[0], args[1], args[2], SHADOW_LEDGER_CAP):
		return _not_compared("shadow_too_large")
	rss = _rss_mb()
	if rss > RSS_CAP_MB:
		return _not_compared("rss_cap", rss_mb=round(rss))
	started = monotonic()
	today = _attempt(today_unguarded, args)
	timing["today_ms"] = routing._ms(started)
	rss = _rss_mb()
	if rss > RSS_CAP_MB:
		return _not_compared("rss_cap", rss_mb=round(rss))
	started = monotonic()
	reader = _attempt(_read_reader, args, stats=stats)
	timing["reader_ms"] = routing._ms(started)
	return today, reader, timing, rss


def _compare_sides(today: tuple, reader: tuple, timing: dict, rss: float) -> dict:
	outcome = compare_unit(today, reader, digest=read_log.ops_digest)
	return {
		**_finished(outcome),
		"would_deadline": timing["reader_ms"] > WOULD_DEADLINE_S * 1000,
		"rss_mb": round(rss),
		**timing,
	}


def _finished(outcome: dict) -> dict:
	"""The record of a compared unit: a transient error is tried again, anything else is done."""
	return {
		**outcome,
		"state": "retry" if outcome.get("reason") == TRANSIENT else "done",
		"reader_build": _build(),
	}


# ---------------------------------------------------------------------------------------- bank unit


def _read_bank(company: str) -> dict | tuple:
	"""Today's `get_balance_on` read and the one-pass read of the bank and cash accounts, inside the view.

	`banks.today_amounts` and `banks.one_pass_amounts` are the two paths themselves, called whatever the
	switch says: the unit proves the one pass in `shadow` mode, and it serves nothing. At most
	`banks.MAX_BANK_ACCOUNTS` accounts are read: that is one request's worth, so the book checks run once
	per path. There is no `would_deadline`: the bank read has no deadline of its own, and `reader_ms` is the
	one pass.
	"""
	from erpnext.accounts.utils import get_balance_on

	as_of = _today()
	try:
		inputs = banks.bank_read_inputs(company, as_of)
	except CalendarSourceUnavailable as exc:
		if exc.code == "fiscal_year_unavailable":
			return _not_compared("fiscal_year_unavailable")
		raise
	blocker = bank_one_pass_blocker(get_balance_on)
	if blocker:
		return _not_compared(blocker)
	names = _bank_accounts(company)
	if len(names) > banks.MAX_BANK_ACCOUNTS:
		return _not_compared("shadow_too_large")
	if not names:
		# Nothing to read: both paths agree on no account (an empty `IN ()` is not valid SQL).
		return ({}, None), ({}, None), inputs.money_precision, {"today_ms": 0, "reader_ms": 0}, None
	timing = {"today_ms": 0, "reader_ms": 0}
	started = monotonic()
	today = _attempt(banks.today_amounts, (company, names, as_of, get_balance_on))
	timing["today_ms"] = routing._ms(started)
	started = monotonic()
	one_pass = _attempt(
		banks.one_pass_amounts, (company, names, as_of, inputs.row_precision, inputs.default_book)
	)
	timing["reader_ms"] = routing._ms(started)
	return (
		today,
		one_pass,
		inputs.money_precision,
		timing,
		_book_decisions(company, as_of, inputs.default_book),
	)


def _book_decisions(company: str, as_of: str, default_book: str | None) -> tuple[bool, bool] | None:
	"""(the memo's refusal, the direct query's refusal) of the Payment Ledger book check; None off the memo.

	The view memo answers the book check for a served company in mode `on`, and both bank paths share that
	answer, so only a direct query can prove it: it is read in the same view and must refuse alike.
	"""
	settings = books.view_settings()
	if settings.mode != "on" or company not in settings.served:
		return None
	try:
		books.validate_bank_ple_books(company, as_of)
		memo_refuses = False
	except CalendarSourceUnavailable as exc:
		if exc.code != "multibook_unsupported":
			raise
		memo_refuses = True
	allowed = books.allowed_books(company, default_book)
	direct_refuses = any(book not in allowed for _kind, book in books.ple_book_groups_direct(company, as_of))
	return memo_refuses, direct_refuses


def _compare_bank_paths(today: tuple, one_pass: tuple, precision: int, timing: dict, decisions) -> dict:
	"""The bank unit's record: the two paths compared, and the memo's book decision against the direct one."""
	outcome = compare_bank(today, one_pass, precision=precision, digest=read_log.ops_digest)
	if decisions is not None and decisions[0] != decisions[1] and outcome["outcome"] != "not_compared":
		outcome = _outcome(
			"diff", counts={"memo_refuses": int(decisions[0]), "direct_refuses": int(decisions[1])}
		)
	return {**_finished(outcome), **timing}


def _bank_accounts(company: str) -> list[str]:
	"""The enabled bank and cash ledger accounts of the company, one more than a request may select."""
	return frappe.get_all(
		"Account",
		filters={
			"company": company,
			"is_group": 0,
			"disabled": 0,
			"account_type": ["in", ["Bank", "Cash"]],
		},
		pluck="name",
		order_by="name",
		limit_page_length=banks.MAX_BANK_ACCOUNTS + 1,
	)


def _read_reader(*args, stats):
	return builtin_reader.read_obligations(*args, stats=stats, deadline=monotonic() + READER_STOP_S)


def _attempt(read, args, **kwargs):
	"""`(result, None)` or `(None, error)`; a job timeout is not an outcome and propagates.

	A lost connection is an error like any other here: the view's exit check then ends the unit as
	`source_changed`, or the error is a retryable record.
	"""
	try:
		return read(*args, **kwargs), None
	except TIMEOUTS:
		raise
	except Exception as exc:
		return None, exc


def _is_transient(error: BaseException) -> bool:
	"""A database or connection failure: the next run may well succeed, so it is no result of the reader.

	The interface error is the driver's own check, not `frappe.db.InterfaceError`: Frappe 15 has no such attribute.
	"""
	return isinstance(
		error,
		(frappe.db.OperationalError, frappe.QueryDeadlockError, frappe.QueryTimeoutError),
	) or frappe.db.is_interface_error(error)


def _error_reason(error: BaseException) -> str:
	return TRANSIENT if _is_transient(error) else f"error:{type(error).__name__}"


def _error_record(error: BaseException) -> dict:
	reason = _error_reason(error)
	return _not_compared(reason, **({"state": "retry"} if reason == TRANSIENT else {}))


def _rss_mb() -> float:
	with open("/proc/self/statm") as statm:  # nosemgrep: frappe-security-file-traversal -- constant path
		return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)


def _build() -> str:
	return routing.reader_build()[:12]


def _not_compared(reason: str, **extra) -> dict:
	return {"state": "done", "outcome": "not_compared", "reason": reason, "reader_build": _build(), **extra}


def _store(digest: str, night: str, field: str, record: dict) -> None:
	shadow_state.put_record(digest, night, field, record)


# ----------------------------------------------------------------------------------------- comparison


@contextmanager
def lifted_guards():
	"""Today's size guards (voucher count, ledger row count) out of the way, restored on exit."""
	saved = {name: getattr(today_sources, name) for name in ("MAX_VOUCHERS", "MAX_LEDGER_ROWS")}
	for name in saved:
		setattr(today_sources, name, LIFTED)
	try:
		yield
	finally:
		for name, value in saved.items():
			setattr(today_sources, name, value)


def today_unguarded(*args, **kwargs) -> dict:
	"""Today's `read_obligations` with the guards lifted: the oracle of the reader. The one lift."""
	with lifted_guards():
		return today_sources.read_obligations(*args, **kwargs)


def canonical(result: dict) -> dict:
	"""Rows in a fixed order without their ids (an id encodes the pre-sort position); the rest as is."""
	rows = [{key: value for key, value in row.items() if key != "id"} for row in result["rows"]]
	rows.sort(key=_body)
	return {**result, "rows": rows}


def canonical_reply(reply: dict) -> dict:
	"""A reply without `generated_at`, the only field that differs between two runs. Ids stay."""
	return {key: value for key, value in reply.items() if key != "generated_at"}


def canonical_ids(result: dict) -> dict[str, list]:
	"""Each row's id by the row without its id; identical rows hold their ids as a sorted list."""
	ids: dict[str, list] = {}
	for row in result["rows"]:
		ids.setdefault(_body(row), []).append(row["id"])
	return {body: sorted(values) for body, values in ids.items()}


def _body(row: dict) -> str:
	return json.dumps({key: value for key, value in row.items() if key != "id"}, sort_keys=True, default=str)


def compare_unit(today: tuple, reader: tuple, *, digest=None) -> dict:
	"""The outcome of one unit from `(result, error)` pairs of today's lifted read and the reader. Pure.

	`digest` maps text to a short HMAC; without it a diff carries counts only.
	"""
	(today_result, today_error), (reader_result, reader_error) = today, reader
	if isinstance(reader_error, ReaderFallback):
		reason = "ineligible" if reader_error.reason == "ineligible" else f"fallback:{reader_error.reason}"
		return _outcome("not_compared", reason=reason)
	for error in (reader_error, today_error):
		if error is not None and not isinstance(error, CalendarSourceUnavailable):
			return _outcome("not_compared", reason=_error_reason(error))
	if today_error is not None or reader_error is not None:
		return _compare_refusals(today_error, reader_error)
	return _compare_results(today_result, reader_result, digest)


def compare_bank(today: tuple, one_pass: tuple, *, precision: int, digest=None) -> dict:
	"""The outcome of a bank unit from `(amounts, error)` pairs of today's read and the one pass. Pure.

	Amounts are compared as the reply shows them (`money` at the reply's precision), account by account.
	`one_pass` amounts of None mean the database lacks the index of the query hint (`banks`): not compared,
	whatever today's read did, because the one pass never ran.
	`digest` maps an account name to a short HMAC; without it a diff carries counts only.
	"""
	(today_amounts, today_error), (pass_amounts, pass_error) = today, one_pass
	if pass_amounts is None and pass_error is None:
		return _outcome("not_compared", reason="one_pass_unavailable")
	for error in (pass_error, today_error):
		if error is not None and not isinstance(error, CalendarSourceUnavailable):
			return _outcome("not_compared", reason=_error_reason(error))
	if today_error is not None or pass_error is not None:
		return _compare_refusals(today_error, pass_error)
	shown = [
		{name: str(today_sources.money(value, precision)) for name, value in amounts.items()}
		for amounts in (today_amounts, pass_amounts)
	]
	counts = {"accounts": len(shown[0])}
	if shown[0] == shown[1]:
		return _outcome("compared", counts=counts)
	differing = sorted(name for name in {*shown[0], *shown[1]} if shown[0].get(name) != shown[1].get(name))
	counts["differing"] = len(differing)
	digests = [digest(name) for name in differing[:MAX_DIGESTS]] if digest else []
	return _outcome("diff", counts=counts, digests=digests)


def _outcome(outcome: str, **fields) -> dict:
	return {"outcome": outcome, **fields}


def _compare_refusals(today_error, reader_error) -> dict:
	if today_error is not None and reader_error is not None and today_error.code == reader_error.code:
		return _outcome("agree_refusal", reason=today_error.code)
	return _outcome(
		"diff", counts={"refusals": [getattr(today_error, "code", None), getattr(reader_error, "code", None)]}
	)


def _compare_results(today: dict, reader: dict, digest) -> dict:
	counts = {"rows": len(reader["rows"])}
	if canonical(today) == canonical(reader) and canonical_ids(today) == canonical_ids(reader):
		return _outcome("compared", counts=counts)
	bodies = (Counter(_body(row) for row in today["rows"]), Counter(_body(row) for row in reader["rows"]))
	only_today, only_reader = bodies[0] - bodies[1], bodies[1] - bodies[0]
	ids_today, ids_reader = canonical_ids(today), canonical_ids(reader)
	ids_differ = sorted(
		body for body in ids_today if body in ids_reader and ids_today[body] != ids_reader[body]
	)
	counts.update(
		today_rows=len(today["rows"]),
		only_today=sum(only_today.values()),
		only_reader=sum(only_reader.values()),
		ids_differ=len(ids_differ),
		fields=sorted(key for key in {*today, *reader} - {"rows"} if today.get(key) != reader.get(key)),
	)
	keys = sorted({*only_today, *only_reader, *ids_differ})[:MAX_DIGESTS]
	return _outcome("diff", counts=counts, digests=[digest(body) for body in keys] if digest else [])


# -------------------------------------------------------------------------------------- daily row, gate


def _write_daily_rows(date: str, current: routing.ReaderSettings) -> None:
	"""One Error Log row per company for `date`, when `read_log.daily_report` has something to say. Best effort.

	The report lists every counter of the day, `served` included, plus `listed_but_missing` (a listed company
	that no Company has), so a company that is never served is as visible as one that fails.
	"""
	try:
		if not frappe.conf.get("encryption_key"):
			return
		missing = set(routing.listed_but_missing(current))
		for company in sorted({*current.served, *current.shadow}):
			digest = read_log.company_digest(company)
			message = read_log.daily_report(
				digest, date, extra={"listed_but_missing": int(company in missing)}
			)
			if message:
				read_log.error_log_once(
					DAILY_TITLE,
					message,
					f"reader-daily:{digest}:{date}",
					"daily",
					window=read_log.OUTCOME_ROW_WINDOW,
					when_cache_down=False,
				)
	except TIMEOUTS:
		raise
	except Exception:
		frappe.logger("payment_calendar").warning("shadow_daily_unavailable")


def summary(days: int = 7, nights: int | None = None) -> dict:
	"""The gate per company on the shadow and served lists, over the `days` nights before tonight and tonight.

	The period holds `days + 1` dates: tonight's chain runs 02:00-05:59, so before it (and while it runs) the
	`days` complete nights are the ones before it. Tonight's records count when they are there and are checked
	for diffs, but a missing tonight never fails the gate.

	A company passes when, for every side and scope and for the `Bank:all` unit, at least `nights` (default `days`) nights hold a
	`compared` record on the deployed reader build, and the period has no diff or error row (Error Log or
	record), no `would_deadline`, and no `deadline` or `assemble_budget` fallback. `missing` lists the side and scope that lack
	nights, with the count found; `reasons` counts, per side and scope, why the units that were not compared
	were not (fixed words only).
	"""
	nights = days if nights is None else nights
	build, current = _build(), routing.reader_settings()
	period = [add_days(_today(), -back) for back in range(days + 1)]
	return {
		company: _gate(company, period, nights, build)
		for company in sorted({*current.served, *current.shadow})
	}


def _gate(company: str, period: list[str], nights: int, build: str) -> dict:
	digest = read_log.company_digest(company)
	compared, bad, reasons = Counter(), Counter(), {}
	for night in period:
		for field, record in shadow_state.records(digest, night).items():
			on_build = record.get("reader_build") == build
			compared[field] += on_build and record.get("outcome") == "compared"
			bad["diff"] += on_build and record.get("outcome") == "diff"
			bad["error"] += on_build and str(record.get("reason") or "").startswith("error:")
			bad["would_deadline"] += on_build and bool(record.get("would_deadline"))
			bad["deadline"] += on_build and record.get("reason") in _SLOW_REASONS
			if on_build and record.get("outcome") == "not_compared":
				reasons.setdefault(field, Counter())[str(record.get("reason"))] += 1
	missing = [
		f"{side}:{scope} {compared[f'{side}:{scope}']}/{nights}"
		for side, scope in COMPANY_UNITS
		if compared[f"{side}:{scope}"] < nights
	]
	diff_rows = _diff_error_rows(digest, period[-1])
	failing = diff_rows + bad["diff"] + bad["error"] + bad["would_deadline"] + bad["deadline"]
	return {
		"pass": not missing and failing == 0,
		"missing": missing,
		"reasons": {field: dict(counter) for field, counter in reasons.items()},
		"diff_rows": diff_rows,
		"diff_records": bad["diff"],
		"error_records": bad["error"],
		"would_deadline": bad["would_deadline"],
		"deadline_fallbacks": bad["deadline"],
		"build": build,
	}


def _diff_error_rows(digest: str, since: str) -> int:
	"""Error Log rows of the shadow diff for one company digest, from `since` (a date) on."""
	return frappe.db.count(
		"Error Log",
		{"method": DIFF_TITLE, "error": ["like", f"%company={digest}%"], "creation": [">=", since]},
	)


# ------------------------------------------------------------------------------------------ operator


def status(days: int = 7) -> dict:
	"""Print and return the shadow's state for an operator: `bench --site <site> execute jarvis.payment_calendar.shadow.status`.

	Names appear here, on the console only (never in a log, key or row): the mode, both lists, the digest
	that the Error Log rows carry for each company, today's and yesterday's counters, the gate per side
	and scope (the bank unit included) with the reasons of the nights that were not compared, and the
	newest record of the bank unit with its counts and digests. A digest of an account name is
	`read_log.ops_digest(name)`: compute it in the console for each selected account to find the one.
	"""
	current, build, today = routing.reader_settings(), _build(), _today()
	result = {"mode": current.mode, "build": build, "companies": {}}
	print(f"mode: {current.mode}  build: {build}  site date: {today}")
	print(f"served: {', '.join(current.served) or '-'}")
	print(f"shadow: {', '.join(current.shadow) or '-'}")
	if not frappe.conf.get("encryption_key"):
		print("no encryption key on this site: the shadow and the counters need one")
		return result
	nights = days
	period = [add_days(today, -back) for back in range(days + 1)]
	for company in sorted({*current.served, *current.shadow}):
		digest = read_log.company_digest(company)
		counters = {date: read_log.day_counts(digest, date) for date in (today, add_days(today, -1))}
		gate = _gate(company, period, nights, build)
		bank = _latest_bank_record(digest, period)
		result["companies"][company] = {"digest": digest, "counters": counters, "gate": gate, "bank": bank}
		print(f"\n{digest} = {company}")
		for date, counts in counters.items():
			print(f"  counters {date}: " + (" ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "-"))
		print(
			f"  gate: {'PASS' if gate['pass'] else 'FAIL'} (diff rows {gate['diff_rows']}, diff records {gate['diff_records']})"
		)
		for line in _gate_lines(gate, nights):
			print(f"    {line}")
		print(f"  bank unit: {_bank_line(bank)}")
	return result


def _latest_bank_record(digest: str, period: list[str]) -> dict | None:
	"""The newest night's record of the bank unit within `period` (a list of dates, newest first), or None."""
	for night in period:
		record = shadow_state.records(digest, night).get(BANK_FIELD)
		if record:
			return {**record, "night": night}
	return None


def _bank_line(bank: dict | None) -> str:
	if bank is None:
		return "no record"
	return (
		f"{bank['night']} outcome={bank.get('outcome')} reason={bank.get('reason')} "
		f"counts={json.dumps(bank.get('counts'), sort_keys=True)} digests={bank.get('digests') or []}"
	)


def _gate_lines(gate: dict, nights: int) -> list[str]:
	"""One line per side and scope that lacks nights: found/needed and the counted reasons."""
	lines = []
	for entry in gate["missing"]:
		field = entry.rsplit(" ", 1)[0]
		why = " ".join(f"{k}={v}" for k, v in sorted(gate["reasons"].get(field, {}).items()))
		lines.append(f"{entry}" + (f"  because {why}" if why else ""))
	return lines
