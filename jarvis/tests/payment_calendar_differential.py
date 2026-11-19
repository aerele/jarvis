"""Comparators that run a reference read beside `read_obligations` for every scenario of a test module.

`compare_with_oracle(real)` returns a drop-in for `read_obligations`: it calls `real` and the frozen
oracle with the same arguments, fails on any difference, and returns the real result (or raises the real
error). Tests that cannot be compared opt out with `@no_oracle_compare(reason)`.

`compare_reader_with_today(reader, today)` is the same for the built-in reader against today's read (the real
oracle of the reader). Its one accepted difference is documented at `ids_may_differ`.
"""

import json
from collections import Counter
from functools import wraps
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import builtin_reader, compatibility
from jarvis.payment_calendar import sources as today_sources
from jarvis.payment_calendar.errors import CalendarSourceUnavailable, ReaderFallback
from jarvis.tests.payment_calendar_fixtures import sync_outstanding
from jarvis.tests.payment_calendar_oracle_1a import oracle_read_obligations

# The one intended difference (1a Task 4, v16 only): the size probe runs before the finance-book scan, so a
# company that is both multi-book and too large is refused as `too_large`; the oracle says `multibook_unsupported`.
ACCEPTED_REFUSAL_CHANGE = ("too_large", "multibook_unsupported")  # (real, oracle)

TALLY = {"compared": 0, "refusals": 0, "accepted_changes": 0}
# The reader differential. `compared`: reader and today both served or both refused, and agreed. `lifted`: today
# refused `too_large`, so the reader was compared with today's read under lifted guards. `fallbacks`: declared
# fallbacks by reason. `id_only`: results equal but for row ids, explained by an unread first line.
READER_TALLY = {"compared": 0, "refusals": 0, "lifted": 0, "id_only": 0, "fallbacks": Counter()}
LIFTED = 10**12


def no_oracle_compare(reason: str):
	"""Opt a test out of the differential; `reason` says why a comparison is impossible or meaningless."""

	def mark(test):
		test.no_oracle_compare = reason
		return test

	return mark


def compare_with_oracle(real, oracle=oracle_read_obligations):
	def compare(*args, **kwargs):
		real_result, real_error = _attempt(real, args, kwargs)
		oracle_result, oracle_error = _attempt(oracle, args, kwargs)
		frozen = oracle is oracle_read_obligations
		TALLY["compared"] += frozen
		if real_error or oracle_error:
			_compare_errors(real_error, oracle_error, frozen)
			TALLY["refusals"] += frozen
			raise real_error
		_assert_equal(_canonical(real_result), _canonical(oracle_result), "result differs from the oracle")
		return real_result

	return compare


def _attempt(function, args, kwargs):
	try:
		return function(*args, **kwargs), None
	except Exception as error:
		return None, error


def _compare_errors(real_error, oracle_error, frozen, accept_change=True):
	if real_error is None or oracle_error is None:
		raise AssertionError(f"only one side refused: real={real_error!r} oracle={oracle_error!r}")
	if isinstance(real_error, CalendarSourceUnavailable) and isinstance(
		oracle_error, CalendarSourceUnavailable
	):
		pair = (real_error.code, oracle_error.code)
		if accept_change and pair == ACCEPTED_REFUSAL_CHANGE and compatibility.source_profile().major == "16":
			TALLY["accepted_changes"] += frozen
			return
		_assert_equal(pair[0], pair[1], "refusal code differs from the oracle")
		return
	_assert_equal(
		(type(real_error), str(real_error)),
		(type(oracle_error), str(oracle_error)),
		"error differs from the oracle",
	)


def _canonical(result):
	"""Rows in a fixed order without their ids (an id encodes the pre-sort position); the rest as is."""
	rows = [{key: value for key, value in row.items() if key != "id"} for row in result["rows"]]
	rows.sort(key=lambda row: json.dumps(row, sort_keys=True, default=str))
	return {**result, "rows": rows}


def _assert_equal(actual, expected, message):
	case = TestCase()
	case.maxDiff = None
	case.assertEqual(actual, expected, message)


# ---------------------------------------------------------------------------------------------- reader


def no_reader_compare(reason: str):
	"""Opt a test out of the reader differential; `reason` says why a comparison is impossible or meaningless."""

	def mark(test):
		test.no_reader_compare = reason
		return test

	return mark


def compare_reader_with_today(
	reader, today, *, sync=sync_outstanding, expected_fallback=None, explain_ids=None
):
	"""A drop-in for `read_obligations` that checks `reader` against `today` and serves the reader's outcome.

	Calls `sync(company)` (a header upkeep for raw-insert fixtures; None for none), then `reader`, then `today`.

	* The reader raised `ReaderFallback`: it must be `expected_fallback`; the outcome is today's, which is what
	  the caller gets when a fallback path serves the read.
	* `expected_fallback` set and no fallback: AssertionError.
	* Today refused `too_large`: today runs again with its guards lifted and the reader is compared with that;
	  today's `too_large` is then raised (what the scenario expects of today).
	* Otherwise both outcomes are compared (today's rules WITHOUT the 1a accepted refusal pair): equal results
	  up to row order, or the same refusal. Row ids must be equal too, except in the one accepted case (see
	  `ids_may_differ`, which gets the keys of the rows whose ids differ; `explain_ids` replaces that check in
	  unit tests). The reader's outcome is served.
	"""
	explain = explain_ids or ids_may_differ

	def compare(*args, **kwargs):
		company = args[0] if args else kwargs["company"]
		if sync is not None:
			sync(company)
		with _recording_lines() as read_names:
			reader_result, reader_error = _attempt(reader, args, kwargs)
		if isinstance(reader_error, ReaderFallback):
			READER_TALLY["fallbacks"][reader_error.reason] += 1
			_assert_equal(reader_error.reason, expected_fallback, "undeclared reader fallback")
			today_result, today_error = _attempt(today, args, kwargs)
			if today_error:
				raise today_error
			return today_result
		if expected_fallback is not None:
			raise AssertionError(f"declared fallback {expected_fallback!r} did not happen")
		today_result, today_error = _attempt(today, args, kwargs)
		too_large = isinstance(today_error, CalendarSourceUnavailable) and today_error.code == "too_large"
		expected, expected_error = today_result, today_error
		if too_large:
			with (
				patch.object(today_sources, "MAX_VOUCHERS", LIFTED),
				patch.object(today_sources, "MAX_LEDGER_ROWS", LIFTED),
			):
				expected, expected_error = _attempt(today, args, kwargs)
		_compare_outcomes(
			(reader_result, reader_error), (expected, expected_error), (args, kwargs), read_names, explain
		)
		if too_large:
			READER_TALLY["lifted"] += 1
			raise today_error
		READER_TALLY["compared"] += 1
		if reader_error:
			READER_TALLY["refusals"] += 1
			raise reader_error
		return reader_result

	return compare


def _compare_outcomes(actual, expected, call, read_names, explain):
	(result, error), (expected_result, expected_error) = actual, expected
	if error or expected_error:
		_compare_errors(error, expected_error, True, accept_change=False)
		return
	_assert_equal(_canonical(result), _canonical(expected_result), "reader result differs from today's read")
	ids, expected_ids = _ids(result), _ids(expected_result)
	if ids != expected_ids:
		keys = _row_keys(
			body for body in ids.keys() | expected_ids.keys() if ids.get(body) != expected_ids.get(body)
		)
		if not explain(*call, read_names, keys):
			raise AssertionError("row ids differ from today's read outside the accepted case")
		READER_TALLY["id_only"] += 1


def _ids(result):
	"""Each row's id by the row without its id; identical rows hold their ids as a sorted list."""
	ids = {}
	for row in result["rows"]:
		body = json.dumps(
			{key: value for key, value in row.items() if key != "id"}, sort_keys=True, default=str
		)
		ids.setdefault(body, []).append(row["id"])
	return {body: sorted(values) for body, values in ids.items()}


def _row_keys(bodies):
	"""The report keys (account, voucher type, voucher, party) of the rows given as `_ids` bodies."""
	keys = set()
	for body in bodies:
		row = json.loads(body)
		keys.add((row.get("account"), row.get("voucher_type"), row.get("voucher_no"), row.get("party")))
	return keys


class _recording_lines:
	"""Context manager: the names of the ledger lines the built-in reader reads, whatever the call path."""

	def __enter__(self):
		self.names: list[str] = []
		engine = builtin_reader._builtin_class()
		original = engine._narrow_lines
		names = self.names

		@wraps(original)
		def narrow(engine_self, discovery):
			lines = original(engine_self, discovery)
			names.extend(line.name for line in lines)
			return lines

		self.patch = patch.object(engine, "_narrow_lines", narrow)
		self.patch.start()
		return self.names

	def __exit__(self, *exc):
		self.patch.stop()


def ids_may_differ(args, kwargs, read_names, keys=None) -> bool:
	"""The accepted difference: a key of a row whose id differs has lines read but its own first line unread.

	The row id encodes the row's position in the whole result, which follows the order in which the first line
	of each key was seen. When a key's own first line in the full ledger is not read, the reader sees the key
	first at its first READ line, which moves it among the keys that tie. Never an amount: only ids (and the row
	order they decide) may differ, and only then. `keys` are the report keys of the rows whose ids differ: a
	qualifying key elsewhere excuses nothing. The answer comes from the full ledger, not from the reader.
	"""
	names = ("company", "account_type", "as_of")
	company, account_type, as_of = (
		args[index] if len(args) > index else kwargs[name] for index, name in enumerate(names)
	)
	return first_line_unread(ledger_lines(company, account_type, as_of), set(read_names), keys)


def ledger_lines(company, account_type, anchor):
	"""The full-ledger lines under the report's predicate, as dicts, with each line's report key."""
	ple = frappe.qb.DocType("Payment Ledger Entry")
	accounts = frappe.get_all(
		"Account", filters={"account_type": account_type, "company": company}, pluck="name"
	)
	query = (
		frappe.qb.from_(ple)
		.select(
			ple.name,
			ple.account,
			ple.party,
			ple.voucher_type,
			ple.voucher_no,
			ple.against_voucher_type,
			ple.against_voucher_no,
			ple.posting_date,
		)
		.where(
			(ple.company == company)
			& ple.account.isin(accounts or [""])
			& (ple.delinked == 0)
			& (ple.posting_date <= anchor)
		)
	)
	if account_type == "Receivable":
		query = query.where(ple.party_type != "Employee")
	lines = query.run(as_dict=True)
	folded = {}
	for doctype in ("Sales Invoice", "Purchase Invoice"):
		returns = frappe.get_all(
			doctype,
			filters={"company": company, "docstatus": 1, "is_return": 1},
			fields=["name", "return_against"],
		)
		folded[doctype] = {row.name: row.return_against for row in returns if row.return_against}
	for line in lines:
		own = not line["against_voucher_no"]
		against = line["voucher_no"] if own else line["against_voucher_no"]
		against_type = line["voucher_type"] if own else line["against_voucher_type"]
		original = folded.get(against_type, {}).get(against, against)
		line["key"] = (line["account"], against_type, original, line["party"])
	return lines


def first_line_unread(lines, read_names, keys=None) -> bool:
	"""True when a key (of `keys`; None means any key) has a read line and an unread line dated on or before
	its earliest read line.

	A tie on the date counts: which line the report orders first then depends on the database collation, so the
	check stays on the permissive side of the accepted difference (it never rejects the accepted case).
	"""
	earliest_read, earliest_unread = {}, {}
	for line in lines:
		target = earliest_read if line["name"] in read_names else earliest_unread
		current = target.get(line["key"])
		if current is None or line["posting_date"] < current:
			target[line["key"]] = line["posting_date"]
	return any(
		key in earliest_unread and earliest_unread[key] <= posted
		for key, posted in earliest_read.items()
		if keys is None or key in keys
	)
