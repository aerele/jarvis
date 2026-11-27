"""``compute``: run a short read-only Python script and return only its small answer.

For totals, counts, rankings, comparisons and cross-checks over many records: one
call reads what it needs (``get_list``, ``get_doc``, ``run_report`` ...), filters,
groups, sums and ranks in Python, and returns the shaped result, instead of a chain
of query / run_report / export calls whose rows pass through the model.

The sandbox, its namespace, its read-only enforcement and its limits are in
``jarvis.tools._compute_sandbox``; this module is the tool around it: the kill
switch, argument checks, teaching errors, and a JSON-safe, size-checked result.

Every read inside runs as the chat user (``jarvis.api.call_tool`` has already set the
session user), with that user's permissions: the same rule as every other tool.
"""

from __future__ import annotations

import ast
import datetime
import decimal
import json
import math
import re

import frappe

from jarvis.exceptions import (
	ComputeCodeError,
	FeatureDisabledError,
	InvalidArgumentError,
	JarvisError,
	PermissionDeniedError,
	PreviewSandboxLost,
)
from jarvis.tools import _compute_sandbox as sandbox

# Site config switch (no DocType field, no migration). Read explicitly and failing
# closed: true / 1 / yes / on switch compute off; false / 0 / no / off / empty (or the
# key absent) leave it on; any other value switches it off.
DISABLE_KEY = "jarvis_disable_compute"
_ENABLED_VALUES = frozenset({"false", "0", "no", "off", ""})

_DISABLED_MESSAGE = (
	"The compute tool is switched off on this site. Do not retry it; answer with get_list, "
	"query or run_report instead."
)

# Part of an error message that repeats what the code printed before it failed.
_PRINT_IN_ERROR = 500


def compute(code: str, description: str) -> dict:
	"""Run ``code`` (read-only Python, no imports) and return the value it assigns to
	``result``: ``{"warnings"?, "result", "reads", "rows_read", "elapsed_ms", "printed"}``.

	``warnings`` (first, only when there are any) are caveats the model must act on:
	amounts in several currencies, companies or including drafts returned row by row, a
	read that stopped at the limit the code gave it. ``reads`` is one compact receipt per
	read: doctype (or report), rows, currency when known, limit when set, a prepared
	report's ``as_of``, and the filters cut short. ``printed`` only when something was.

	Site config ``jarvis_disable_compute``: true / 1 / yes / on (or any value other than
	false / 0 / no / off / empty) switches the tool off.

	``description`` is one plain sentence saying what the code works out; it is shown
	in the chat step and kept, with the code, in the stored tool call."""
	if _disabled():
		raise FeatureDisabledError(_DISABLED_MESSAGE)
	code = _check_code(code)
	_check_description(description)

	try:
		tree = ast.parse(code, filename=sandbox.SCRIPT_FILENAME)
	except SyntaxError as e:
		raise ComputeCodeError(f"line {e.lineno}: SyntaxError: {e.msg}") from None

	run = sandbox._Run(wall_clock_s=sandbox.WALL_CLOCK_S)
	glb, available, frappe_names = sandbox.build_namespace(run, frappe.session.user)
	errors = sandbox.static_errors(tree, available, frappe_names)
	if errors:
		raise ComputeCodeError(" ".join(errors[:10]))
	code_obj = sandbox.compile_code(code)

	mark = _message_log_mark()
	try:
		error = sandbox.execute(code_obj, glb, run)
	finally:
		_drop_messages_since(mark)

	if run.failure is not None:
		cls = InvalidArgumentError if run.failure_kind == "write" else ComputeCodeError
		raise cls(_with_printed(run.failure, run))
	if error is not None:
		raise _translate(error, run)

	result = glb.get("result", sandbox._MISSING)
	if result is sandbox._MISSING:
		raise ComputeCodeError(
			"The code finished without assigning a variable named result. Put the answer in result, "
			"e.g. result = {'total': total, 'rows': n}."
		)
	if result is None:
		raise ComputeCodeError(
			"result is None, so there is no answer to give. Assign the answer itself; when nothing "
			"matched, say so with a value, e.g. result = {'total': 0, 'rows': 0}."
		)
	try:
		return _shape(result, run)
	except (JarvisError, frappe.ValidationError):
		raise
	except Exception as e:
		# compute's own failure (shaping the answer), not the script's: logged, then refused.
		sandbox._log_internal("result", e)
		raise ComputeCodeError(
			"compute hit an internal error while shaping the result (it is logged). Return plain "
			"numbers, text, lists and dicts."
		) from None


def _shape(result, run) -> dict:
	# warnings first: the model reads the caveats before the numbers.
	out = {"warnings": list(run.warnings)} if run.warnings else {}
	out.update(
		{
			"result": _json_safe(result),
			"reads": run.receipts(),
			"rows_read": run.rows_read,
			"elapsed_ms": run.elapsed_ms(),
		}
	)
	printed = run.printed_text()
	if printed:
		out["printed"] = printed
	_check_size(out)
	return out


def _disabled() -> bool:
	"""Whether site config switches compute off. See DISABLE_KEY: fails closed."""
	value = frappe.conf.get(DISABLE_KEY)
	if value is None or value is False:
		return False
	if value is True:
		return True
	return str(value).strip().casefold() not in _ENABLED_VALUES


def _check_code(code) -> str:
	if not isinstance(code, str) or not code.strip():
		raise ComputeCodeError("code must be a non-empty Python script that assigns its answer to result.")
	if len(code) > sandbox.MAX_CODE_CHARS:
		raise ComputeCodeError(
			f"code is {len(code):,} characters; the limit is {sandbox.MAX_CODE_CHARS:,}. Write a "
			"shorter script: read with filters and aggregate, do not paste data into the code."
		)
	return code


def _check_description(description) -> None:
	if not isinstance(description, str) or not description.strip():
		raise ComputeCodeError("description is required: one plain sentence saying what the code works out.")
	if len(description) > sandbox.MAX_DESCRIPTION_CHARS:
		raise ComputeCodeError(
			f"description must be one plain sentence of at most {sandbox.MAX_DESCRIPTION_CHARS} characters."
		)


def _message_log_mark() -> int:
	log = getattr(frappe.local, "message_log", None)
	return len(log) if log else 0


def _drop_messages_since(mark: int) -> None:
	"""Frappe throws log a message for the HTTP response; what the script caught or
	what compute turns into its own error must not ride out as a second popup."""
	log = getattr(frappe.local, "message_log", None)
	if log and len(log) > mark:
		del log[mark:]


def _with_printed(message: str, run) -> str:
	printed = run.printed_text()
	if not printed:
		return message
	return f"{message} Printed before it stopped: {printed[:_PRINT_IN_ERROR]}"


def _translate(error: BaseException, run) -> Exception:
	"""The error the code ended with, as the tool's own error, naming the line."""
	line = sandbox.script_line(error)
	where = f"line {line}: " if line else ""
	if isinstance(error, PreviewSandboxLost):
		return error
	if isinstance(error, frappe.QueryDeadlockError | frappe.QueryTimeoutError):
		return error  # the site was busy: retry later
	if isinstance(error, PermissionDeniedError):
		return PermissionDeniedError(f"{where}{error}")
	if isinstance(error, frappe.PermissionError):
		return PermissionDeniedError(f"{where}{sandbox._clean(error) or 'permission denied'}")
	from jarvis import _failure_kind

	if _failure_kind.too_slow(error):
		return ComputeCodeError(
			f"{where}one database read ran past {sandbox.STATEMENT_TIME_S} seconds and was stopped. "
			"Add filters (a date range, a status) or let the database aggregate with group_by."
		)
	from jarvis.tools._compute_guards import GuardRefused

	if isinstance(error, JarvisError | sandbox.SandboxUsageError | GuardRefused):
		message = sandbox.error_text(error, 4_000)
	else:
		message = f"{type(error).__name__}: {sandbox._clean(error) or type(error).__name__}"
	hint = _hint(error)
	return ComputeCodeError(_with_printed(f"{where}{message}{hint}", run))


def _hint(error: BaseException) -> str:
	from jarvis.tools._compute_guards import error_text

	text = error_text(error)
	if isinstance(error, KeyError):
		return (
			" A row or dict has no such key: ask for the field in fields=[...], use row.get(key) for "
			"an optional one, and d[k] = d.get(k, 0) + v to start a running total."
		)
	if isinstance(error, TypeError) and "NoneType" in text:
		return " A value is empty (None): wrap numbers with flt(x) and text with cstr(x)."
	if isinstance(error, ZeroDivisionError):
		return " Check the divisor before dividing."
	if isinstance(error, NameError):
		return " Assign the name before using it; no imports are needed."
	if isinstance(error, frappe.ValidationError) and "field" in text.lower():
		return " Check the field names with get_schema."
	return ""


def _plain(obj):
	"""``json.dumps`` default: the non-JSON values a computation legitimately returns.
	Not Frappe's ``json_handler``, which turns any callable or class into its repr and
	drains any iterable: a result is data, nothing else."""
	if isinstance(obj, datetime.datetime | datetime.date | datetime.time | datetime.timedelta):
		return str(obj)
	if isinstance(obj, decimal.Decimal):
		return float(obj)
	if isinstance(obj, set | frozenset):
		return sorted(obj, key=repr)
	what = "function" if callable(obj) else type(obj).__name__
	raise TypeError(f"a {what} is not data")


# Float noise: a run of zeros or nines right before the last one or two digits of a
# float printed at full precision (15+ significant digits), e.g. 12345.670000000002,
# 0.30000000000000004, 66987822.72000001, 2.9999999999999996. Only such a run is cut;
# every digit a double really carries is kept (12345678901234.56, 0.012345678).
_NOISE = re.compile(r"(?:0{3,}|9{3,})\d{1,2}$")
_NOISE_SIG_DIGITS = 15
_NOISE_ULPS = 8


def _tidy_float(value: float) -> float:
	text = repr(value)
	if "e" in text or "n" in text or "." not in text:
		return value
	whole, fraction = text.lstrip("-").split(".")
	significant = len((whole + fraction).lstrip("0"))
	if significant < _NOISE_SIG_DIGITS:
		return value
	m = _NOISE.search(fraction)
	if not m:
		return value
	tidy = round(value, m.start())
	return tidy if abs(tidy - value) <= _NOISE_ULPS * math.ulp(value) else value


def _tidy_floats(value):
	"""Floats without binary noise (see _NOISE); everything else as it is."""
	if isinstance(value, float):
		return _tidy_float(value)
	if isinstance(value, list):
		return [_tidy_floats(v) for v in value]
	if isinstance(value, dict):
		return {k: _tidy_floats(v) for k, v in value.items()}
	return value


def _json_safe(value):
	"""``value`` as plain JSON data: dates and datetimes as text (``2026-10-05``,
	``2026-10-05 10:30:00``), Decimals as numbers, sets as lists, floats tidied."""
	from jarvis.tools._compute_guards import GuardRefused, check_written_size

	try:
		check_written_size(value, "result")
	except GuardRefused as e:
		raise ComputeCodeError(str(e)) from None
	try:
		return _tidy_floats(json.loads(json.dumps(value, default=_plain, allow_nan=False)))
	except ValueError as e:
		if "limit" in sandbox.error_text(e, 200).casefold():
			raise ComputeCodeError(
				"result holds a number too large to return (more than 4,300 digits). Return it rounded, "
				"as a float, or as its number of digits."
			) from None
		raise ComputeCodeError(
			"result holds NaN or Infinity (a division by zero?). Return a number or None instead."
		) from None
	except TypeError as e:
		raise ComputeCodeError(
			f"result must be plain data (numbers, text, dates, lists and dicts): {e}."
		) from None


def _check_size(out: dict) -> None:
	from jarvis.tools._result_guard import MAX_RESULT_CHARS, measure

	size = measure(out)
	# The receipts give way first: they are the audit trail, the result is the answer.
	reads = out.get("reads") or []
	while (
		size is not None
		and size > MAX_RESULT_CHARS
		and reads
		and not (len(reads) == 1 and "more" in reads[0])
	):
		kept = [r for r in reads if "more" not in r]
		hidden = sum(r.get("more", 0) for r in reads) + len(kept) - len(kept) // 2
		out["reads"] = reads = kept[: len(kept) // 2] + [{"more": hidden}]
		size = measure(out)
	if size is not None and size > MAX_RESULT_CHARS:
		raise ComputeCodeError(
			f"result is {size:,} characters, over the {MAX_RESULT_CHARS:,} a tool result may carry. "
			"Return aggregates (totals by customer, by month) or a top-N (sorted(...)[:20]) with a "
			"count of the rest, not the rows themselves."
		)
