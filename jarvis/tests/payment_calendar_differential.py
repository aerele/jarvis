"""Comparator that runs the frozen 1a oracle beside `read_obligations` for every scenario of a test module.

`compare_with_oracle(real)` returns a drop-in for `read_obligations`: it calls `real` and the frozen
oracle with the same arguments, fails on any difference, and returns the real result (or raises the real
error). Tests that cannot be compared opt out with `@no_oracle_compare(reason)`.
"""

import json
from unittest import TestCase

from jarvis.payment_calendar import compatibility
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.tests.payment_calendar_oracle_1a import oracle_read_obligations

# The one intended difference (1a Task 4, v16 only): the size probe runs before the finance-book scan, so a
# company that is both multi-book and too large is refused as `too_large`; the oracle says `multibook_unsupported`.
ACCEPTED_REFUSAL_CHANGE = ("too_large", "multibook_unsupported")  # (real, oracle)

TALLY = {"compared": 0, "refusals": 0, "accepted_changes": 0}


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


def _compare_errors(real_error, oracle_error, frozen):
	if real_error is None or oracle_error is None:
		raise AssertionError(f"only one side refused: real={real_error!r} oracle={oracle_error!r}")
	if isinstance(real_error, CalendarSourceUnavailable) and isinstance(
		oracle_error, CalendarSourceUnavailable
	):
		pair = (real_error.code, oracle_error.code)
		if pair == ACCEPTED_REFUSAL_CHANGE and compatibility.source_profile().major == "16":
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
