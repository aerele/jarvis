"""Prepared-report handling for the ``run_report`` tool (Phase 0, pull-model).

A Frappe **Prepared Report** is one whose author flagged too heavy to run
inline: ``run()`` never computes it, it only looks up an already-generated
copy, and generation happens in a background job (25-min budget). So the bare
``run_report`` tool returned an empty ``{prepared_report: True, doc: None}`` for
a prepared report and the agent relayed that as "no data" - the bug this fixes.

Instead we return a **self-describing status envelope**. On every call we
recompute from live state (there is no stored ticket):
  - ``ready``       - a completed copy exists; its rows + ``as_of`` ride along.
  - ``generating``  - a background run for these filters is in flight.
  - ``failed``      - the most recent run terminally errored; surfaced, not looped.
  - ``started``     - nothing usable, so we kicked off a background run.
A paused scheduler / unreachable job queue is RAISED (InvalidArgumentError),
mirroring ``run_import`` - so the infra condition is a proper wire error, not a
silent success envelope, and we never leave an orphan row that can't run.

No proactive delivery - that is a later phase; this is pull-only and stateless.

Every gate ``run()`` applies is applied here as the caller, because we bypass
``run()``:
  - ``get_report_doc`` - the report's "Has Role" allow-list (``is_permitted``),
    the ``ref_doctype`` report permission, the disabled-check, Custom-Report
    resolution. (``make_prepared_report`` also calls it, but the completed-copy
    READ path would otherwise be ungated.)
  - ``validate_filters_permissions`` - the record-level guard that a Link filter
    value points at a doc the caller may read. ``make_prepared_report`` inserts
    with ``ignore_permissions`` and skips this one, so we must apply it.

The completed copy is read DIRECTLY from its ``Prepared Report`` doc, never via
``run()`` by name: ``run()`` would (a) drop our filters for a Custom Report's
``custom_filters`` and (b) throw ``PermissionError`` because a normal ERP user
lacks the ``Prepared Report`` read role.
"""

from __future__ import annotations

import html
import json
import re

import frappe
from frappe.core.doctype.prepared_report.prepared_report import make_prepared_report
from frappe.desk.query_report import get_report_doc, validate_filters_permissions
from frappe.utils import cint
from frappe.utils.background_jobs import get_redis_conn
from frappe.utils.scheduler import is_scheduler_inactive

from jarvis import compat
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.get_report_filters import get_report_filters

# Cap rows folded into the envelope. A prepared report is the likeliest place to
# hit a huge result; an uncapped dump becomes a chat-message payload AND a
# recurring per-turn token tax once re-serialised into the model context. Same
# bound dashboards_api uses for the identical run_report-into-a-payload risk.
_MAX_ROWS = 2000

# Frappe's own default background budget for a prepared report
# (prepared_report.REPORT_TIMEOUT); a report may override it via Report.timeout.
_DEFAULT_REPORT_TIMEOUT = 25 * 60
# A Started report is treated as stuck (worker died mid-run) once it passes its
# own timeout plus this margin - only then do we re-generate. A Queued report is
# NOT re-triggered on age: it is enqueued and waiting, and a `long`-queue backlog
# can legitimately exceed the 25-min job budget, so re-triggering would pile
# duplicate 25-min jobs. A stuck/orphan Queued therefore shows `generating` until
# an operator intervenes - we never amplify the backlog.
_STARTED_STALL_MARGIN = 5 * 60

# Terminal-failure statuses (surface, do not auto-retrigger) and the statuses
# that mean a run is genuinely in flight.
_FAILED_STATUSES = ("Error", "Failed")
_INFLIGHT_STATUSES = ("Queued", "Started")

# Purely-cosmetic bookkeeping key run() itself pops before using filters. NOT
# translate_data - run() passes that through and it changes the result content,
# so it is a real request parameter that belongs in the match key.
_BOOKKEEPING_KEYS = ("prepared_report_name",)
# The caller's newest runs of one report compared by filter meaning (see _latest).
_MATCH_SCAN = 50


def handle_prepared(report_name: str, raw_filters: dict, *, user: str) -> dict:
	"""Return a status envelope (ready / generating / failed / started) for a
	prepared report. Pull-model, stateless, runs as ``user``. Raises
	InvalidArgumentError when the background queue can't run the report."""
	filters = gate_and_fill(report_name, raw_filters, user=user)

	# 1. A completed copy we can read? (0 rows is a valid answer; unreadable -> fall through)
	row = _latest(report_name, filters, user, ("Completed",))
	if row:
		ready = _read_completed(row.name)
		if ready is not None:
			return ready
	return _not_ready(report_name, filters, user)


def completed_or_status(report_name: str, filters: dict, *, user: str) -> tuple[dict | None, dict | None]:
	"""For an export, after the caller ran the gates and ``fill_defaults``:
	``(completed, None)`` with EVERY row of the completed copy (``{columns, result,
	as_of, run}``, no ``_MAX_ROWS`` cap: the rows go to a file, not to the model), or
	``(None, status)`` with the same ``generating`` / ``failed`` / ``started``
	envelope ``handle_prepared`` returns. One pass over the live state, so a copy
	that completes between two lookups is never answered with a capped ``ready``
	envelope."""
	row = _latest(report_name, filters, user, ("Completed",))
	if row:
		completed = read_completed_rows(row.name)
		if completed is not None:
			return completed, None
	return None, _not_ready(report_name, filters, user)


def peek(report_name: str, filters: dict, *, user: str) -> tuple[str, dict | None]:
	"""The state of ``user``'s copy for already-filled ``filters``, WITHOUT starting a run:
	``("ready", completed)``, ``("generating", envelope)``, ``("failed", envelope)`` or
	``("missing", None)`` when nothing usable exists (never run, or the run stalled). The
	export job dispatcher polls with this from the scheduler, so it must have no side
	effect and must be called under the owner (``_latest`` is owner-scoped)."""
	row = _latest(report_name, filters, user, ("Completed",))
	if row:
		completed = read_completed_rows(row.name)
		if completed is not None:
			return "ready", completed
	pending = _pending_state(report_name, filters, user)
	if pending is None:
		return "missing", None
	return pending["status"], pending


def report_deadline_s(report_name: str) -> int:
	"""Seconds after which a run started for this report counts as stuck."""
	return _report_timeout(report_name) + _STARTED_STALL_MARGIN


def gate_and_fill(report_name: str, raw_filters: dict, *, user: str) -> dict:
	"""The gates ``run()`` applies, then the filters as Desk would send them."""
	gate_report(report_name)
	filters = fill_defaults(report_name, raw_filters)
	gate_filters(report_name, filters, user)
	return filters


def fill_defaults(report_name: str, raw_filters: dict | str | None) -> dict:
	"""The report's own defaults under the caller's values, as Desk sends them. A
	run started with these is the run Desk would start, and a Desk run is found
	again; a Script Report's required filter with a default never reaches the
	script unset."""
	return {**_report_defaults(report_name), **_canonical_filters(raw_filters)}


def _not_ready(report_name: str, filters: dict, user: str) -> dict:
	"""No readable completed copy: the most recent attempt decides (generating or a
	terminal failure to surface), else (re)generate. Refuses (raises) if the queue
	can't run it, so we never leave an orphan Queued row that no worker will drain."""
	pending = _pending_state(report_name, filters, user)
	if pending is not None:
		return pending
	_require_worker()
	run = make_prepared_report(report_name, filters)["name"]
	return _envelope(
		report_name,
		"started",
		"This report is heavy, so it runs in the background - I've started it. "
		"Ask me again in a little while and I'll have the results.",
		run=run,
	)


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
def gate_report(report_name: str):
	"""Gate the report exactly as run() does: Has-Role allow-list, ref_doctype
	report permission, disabled-check, Custom-Report resolution. Returns the
	resolved report doc (the reference report, for a Custom Report)."""
	try:
		return get_report_doc(report_name)
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"no permission to run report {report_name}") from e
	except frappe.ValidationError as e:
		# a disabled report - not a permission problem; surface as an argument error
		raise InvalidArgumentError(str(e) or f"report {report_name} is unavailable") from e


def gate_filters(report_name: str, filters: dict, user: str) -> None:
	"""Record-level guard that a Link filter value points at a doc the caller may
	read. make_prepared_report skips this (ignore_permissions insert), and the
	completed-copy read path bypasses run() too, so we apply it once, up front."""
	if not filters:
		return
	try:
		validate_filters_permissions(report_name, filters, user)
	except (frappe.PermissionError, frappe.ValidationError) as e:
		raise PermissionDeniedError(
			str(e) or f"no permission for the requested filters on {report_name}"
		) from e


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #
def _canonical_filters(raw: dict | str | None) -> dict:
	"""One canonical filter value used for the completed-copy lookup, the
	in-flight check AND the trigger - so a report WE start is always found by OUR
	lookup. Strip bookkeeping keys and drop empty values; ordering/spacing is
	normalised downstream by process_filters_for_prepared_report."""
	if not raw:
		return {}
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except (ValueError, TypeError) as e:
			raise InvalidArgumentError("filters must be a JSON object of field: value pairs") from e
	if not isinstance(raw, dict):
		raise InvalidArgumentError("filters must be an object of field: value pairs")
	out = {}
	for key, value in raw.items():
		if key in _BOOKKEEPING_KEYS:
			continue
		if value is None or value == "":
			continue
		out[key] = value
	return out


def _report_defaults(report_name: str) -> dict:
	"""The literal filter defaults the report's script declares, ``{fieldname: value}``.
	Empty when its filters can't be read: then a Desk run may simply not be reused."""
	try:
		filters = get_report_filters(report_name)["filters"]
	except (frappe.ValidationError, frappe.PermissionError, InvalidArgumentError, PermissionDeniedError):
		return {}
	return {f["fieldname"]: f["default"] for f in filters if not _unset(f.get("default"))}


def is_unset(value) -> bool:
	"""Desk sends only the filters that have a value (``get_filter_values``): a blank,
	0 / false, "%" or an empty selection reads as not set."""
	if isinstance(value, list | tuple):
		return not value
	return value is None or value in ("", "%") or (isinstance(value, int | float) and value == 0)


_unset = is_unset  # the name the module's own callers and tests use


# --------------------------------------------------------------------------- #
# completed copy (read DIRECTLY - never via run() by name)
# --------------------------------------------------------------------------- #
def _read_completed(dn: str) -> dict | None:
	"""Read a completed Prepared Report's rows straight from its doc. Returns a
	`ready` envelope (rows capped at ``_MAX_ROWS`` for the model), or None if the
	stored output can't be read (missing/corrupt gz) so the caller falls through to
	(re)generate rather than emit empty."""
	completed = read_completed_rows(dn)
	if completed is None:
		return None
	result = completed["result"]
	capped, note = _cap_rows(result, completed["report_name"])
	as_of = completed["as_of"]
	env = _envelope(completed["report_name"], "ready", _ready_message(len(result), as_of))
	env["columns"] = completed["columns"]
	env["result"] = capped
	env["as_of"] = as_of
	env["run"] = dn
	env["source_rows"] = len(result)
	env["preview_complete"] = len(capped) == len(result)
	env["next_query"] = {"tool": "query_report", "args": {"prepared_report": dn}}
	if note:
		env["row_note"] = note
	return env


def read_completed_rows(dn: str) -> dict | None:
	"""EVERY row of a completed Prepared Report, read DIRECTLY from its doc (no
	``Prepared Report`` read role needed): ``{report_name, columns, result, as_of,
	run}``, or None when the stored output can't be read (missing/corrupt gz) so the
	caller regenerates rather than emits empty. Uncapped: the export path writes
	these rows to a file; ``_read_completed`` caps them for the model."""
	try:
		doc = frappe.get_doc("Prepared Report", dn)
		raw = doc.get_prepared_data()
		data = json.loads(raw.decode("utf-8")) if raw else None
	except frappe.DoesNotExistError:
		return None  # a concurrent cleanup removed it - benign, just regenerate
	except Exception as exc:
		# A completed copy whose stored output won't read is an internal fault, not
		# a benign miss - log it (exception TYPE + name only; never the payload/
		# filters, so no report data lands in the Error Log) so "why did this
		# regenerate" is answerable, then fall through to regenerate.
		frappe.log_error(
			title=f"Jarvis: prepared report output unreadable ({dn})",
			message=f"{type(exc).__name__} reading prepared report {dn}",
		)
		return None
	if data is None:
		return None
	columns, result = _split_columns_result(doc, data)
	# A Completed report can store a null result (columns but no rows) - that is a
	# genuine 0-row answer, not an unreadable copy.
	if result is None:
		result = []
	if not isinstance(result, list):
		return None
	as_of = doc.get("report_end_time") or doc.get("creation")
	return {
		"report_name": doc.report_name,
		"columns": columns or [],
		"result": result,
		"as_of": str(as_of) if as_of else None,
		"run": dn,
	}


def _split_columns_result(doc, data):
	"""Pull (columns, result) out of the stored payload. Modern prepared reports
	store the generate_report_result dict; a legacy shape stored a bare list."""
	if isinstance(data, dict):
		return (data.get("columns") or []), data.get("result")
	if isinstance(data, list):
		try:
			columns = json.loads(doc.columns) if doc.get("columns") else []
		except (ValueError, TypeError):
			columns = []
		return columns, data
	return [], None


def _cap_rows(result: list, report_name: str):
	if len(result) <= _MAX_ROWS:
		return result, None
	note = (
		f"Showing the first {_MAX_ROWS} of {len(result)} stored rows. For questions, counts or totals "
		"use query_report with this response's exact run ID. Inspect first, then pin its version "
		"for queries over all stored rows. This preview is not a complete report read."
	)
	return result[:_MAX_ROWS], note


def _ready_message(row_count: int, as_of) -> str:
	# Phase 0 serves the existing completed copy for identical filters; there is no
	# same-filters force-refresh, so the message must NOT promise an on-demand
	# re-run - it states how current the figures are via as_of instead.
	when = f" (generated {as_of})" if as_of else ""
	if row_count == 0:
		return f"I ran the report{when} - it returned no matching rows."
	return (
		f"Here are the results{when}. This report is generated in the background, "
		f"so the figures are as of that run."
	)


# --------------------------------------------------------------------------- #
# in-flight / recent failure / worker
# --------------------------------------------------------------------------- #
def _pending_state(report_name: str, filters: dict, user: str) -> dict | None:
	"""State from the most recent NON-completed run for this owner+filters:
	`generating` if a run is in flight within its stall deadline, `failed` if the
	most recent run terminally errored, or None (nothing pending, or the in-flight
	run has stalled) so the caller (re)generates.

	One query over the SINGLE newest attempt, so an old failure can never shadow a
	newer in-flight run. Owner-scoped with ignore_permissions - a normal ERP user
	lacks the Prepared Report read role, so a permission-checked read finds nothing."""
	row = _latest(report_name, filters, user, (*_INFLIGHT_STATUSES, *_FAILED_STATUSES, "Cancelled"))
	if not row or row.status == "Cancelled":
		# A run someone stopped (v16 Cancelled) is not pending and is no failure to
		# surface: asked again, start it again. Counted so it shadows an older Error.
		return None
	if row.status == "Queued":
		# Enqueued and waiting for a worker - never re-trigger (see _STARTED_STALL_MARGIN).
		return _generating(report_name, row.name)
	if row.status == "Started":
		age = frappe.utils.time_diff_in_seconds(frappe.utils.now(), row.creation)
		if age <= _report_timeout(report_name) + _STARTED_STALL_MARGIN:
			return _generating(report_name, row.name)
		return None  # started but the worker died mid-run -> caller re-generates
	# Error / Failed - surface it and stop; re-firing the same doomed job on every
	# ask is the loop we are avoiding. The user can adjust the filters (a new match
	# key -> a fresh run). Only the exception's own message reaches the user (see
	# failure_reason); the traceback stays on the Prepared Report for an admin.
	reason, detail = failure_reason(frappe.db.get_value("Prepared Report", row.name, "error_message"))
	return _envelope(
		report_name,
		"failed",
		_FAILED_MESSAGES[reason].format(detail=detail, run=row.name),
		run=row.name,
		reason=reason,
		error=detail,
	)


# What the agent relays per failure reason. Only ``report_error`` invites a filter
# change: a permission refusal or a timeout would fail again whatever the filters.
_FAILED_MESSAGES = {
	"permission": "I couldn't run that report: {detail} Changing the filters won't help; "
	"this needs access from an administrator.",
	"timed_out": "That report failed to generate: {detail} A narrower date range or "
	"fewer rows may finish in time, or an administrator can raise the report's time limit.",
	"report_error": "That report failed to generate: {detail} If a filter needs changing, "
	"tell me and I'll run it again.",
	"internal": "That report failed to generate. If it needs a filter you didn't set "
	"(a company, a date range, a required value), tell me and I'll run it again with "
	"it. An administrator can see the error in Prepared Report {run}.",
}
_FAILURE_DETAIL_MAX = 300
_TIMED_OUT = "It took too long to generate and was stopped."
_NO_PERMISSION = "You don't have permission to run this report."
# frappe.exceptions that wrap a database error: their message is the driver's
# (an error code and SQL fragments), not one written for the user.
_FRAPPE_DB_ERRORS = frozenset(
	{"QueryDeadlockError", "DuplicateEntryError", "UniqueValidationError", "InvalidColumnName", "SQLError"}
)
# The traceback's final exception line, at column 0 (a traceback's frames and
# variables are indented): "frappe.exceptions.PermissionError: <message>". The
# message may run on over further lines, up to the end of the text.
_EXC_LINE = re.compile(r"^((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Timeout|Exit)\w*)(?::(.*))?$")


def failure_reason(error_message: str | None) -> tuple[str, str]:
	"""Why a Prepared Report failed, as ``(reason, detail)`` safe to show its owner.

	``error_message`` is what Frappe stored: a traceback with variables (the user's
	roles, the filters, the report's config), or "Report timed out." for a run the
	stall sweep failed. Only the final exception's own message is kept, and only
	from a ``frappe.*`` exception - the text ``frappe.throw`` addresses to the user,
	the same text Desk would show them. Anything else (KeyError, SQL errors) is
	developer-facing and reads as ``internal`` with no detail.

	``reason`` is ``permission``, ``timed_out``, ``report_error`` or ``internal``."""
	text = (error_message or "").strip()
	if not text:
		return "internal", ""
	if text.startswith("Report timed out"):
		return "timed_out", _TIMED_OUT
	lines = text.splitlines()
	for i in range(len(lines) - 1, -1, -1):
		match = _EXC_LINE.match(lines[i])
		if match:
			exc = match.group(1)
			message = "\n".join([match.group(2) or "", *lines[i + 1 :]])
			break
	else:
		return "internal", ""
	name = exc.rsplit(".", 1)[-1]
	if name in ("JobTimeoutException", "QueryTimeoutError"):
		return "timed_out", _TIMED_OUT
	if not exc.startswith("frappe.") or name in _FRAPPE_DB_ERRORS:
		return "internal", ""
	detail = _clean_detail(message)
	if exc.endswith("PermissionError"):
		return "permission", detail or _NO_PERMISSION
	return ("report_error", detail) if detail else ("internal", "")


def _clean_detail(message: str) -> str:
	"""The exception message as one plain sentence: tags dropped (``<br>`` reads as a
	space), entities decoded, whitespace collapsed, capped, and closed with a full
	stop so the agent's next sentence does not run into it."""
	text = html.unescape(re.sub(r"<[^>]*>", " ", message))
	text = " ".join(text.split())
	if len(text) > _FAILURE_DETAIL_MAX:
		text = text[: _FAILURE_DETAIL_MAX - 1].rstrip() + "…"
	elif text and text[-1] not in ".!?":
		text += "."
	return text


def _latest(report_name, filters, user, statuses):
	"""Newest owner-scoped Prepared Report for these filters in one of ``statuses``.

	Matched on the filters' meaning, not Frappe's exact JSON string (#635): a run
	started from Desk stores the empty filters it showed and its own value types
	(1 for true, a multi-select in its own order), so the copy it left was never
	found and the report ran again."""
	want = _match_key(filters)
	rows = frappe.get_all(
		"Prepared Report",
		filters={"report_name": report_name, "owner": user, "status": ("in", statuses)},
		fields=["name", "filters", "status", "creation"],
		order_by="creation desc",
		limit=_MATCH_SCAN,
		ignore_permissions=True,
	)
	for row in rows:
		try:
			stored = json.loads(row.filters or "{}")
		except (ValueError, TypeError):
			continue
		if isinstance(stored, dict) and _match_key(stored) == want:
			return row
	return None


def _match_key(filters: dict) -> str:
	"""The filters as a comparable string: bookkeeping and unset values dropped,
	scalars as text (1, 1.0, true and "1" read the same), lists as sorted text."""

	def norm(value):
		if isinstance(value, bool):
			value = int(value)
		if isinstance(value, float) and value.is_integer():
			value = int(value)
		if isinstance(value, list | tuple):
			return sorted(str(norm(v)) for v in value)
		return str(value)

	return json.dumps(
		{
			key: norm(value)
			for key, value in filters.items()
			if key not in _BOOKKEEPING_KEYS and not _unset(value)
		},
		sort_keys=True,
	)


def _generating(report_name: str, run: str) -> dict:
	return _envelope(
		report_name,
		"generating",
		"Your report is still running in the background. Ask me again in a "
		"little while and I'll have the results.",
		run=run,
	)


def _report_timeout(report_name: str) -> int:
	# Key on the report_name we were called with (the custom name for a Custom
	# Report), matching what after_insert uses to set the job timeout.
	return cint(frappe.db.get_value("Report", report_name, "timeout")) or _DEFAULT_REPORT_TIMEOUT


def _require_worker() -> None:
	"""Raise if the background queue can't run the report, so we never leave an
	orphan Queued row. Tests / developer_mode run inline, so skip the check."""
	if compat.in_test() or frappe.conf.get("developer_mode"):
		return
	if is_scheduler_inactive():
		raise InvalidArgumentError(
			"This report runs in the background, but the site scheduler is paused, so "
			"it can't start. Ask an administrator to enable it, then try again."
		)
	if not _queue_reachable():
		raise InvalidArgumentError(
			"This report runs in the background, but the job queue is unreachable right "
			"now, so it can't start. Try again shortly, or ask an administrator to check it."
		)


def _queue_reachable() -> bool:
	# Fail-closed queue probe using Frappe's auth-correct connection. A down
	# (refused) Redis raises quickly - the common localhost case; a black-holed
	# host could block, but that is bounded by the outer chat-turn timeout and is
	# not a realistic localhost failure.
	try:
		get_redis_conn().ping()
		return True
	except Exception:
		return False


# --------------------------------------------------------------------------- #
# envelope
# --------------------------------------------------------------------------- #
def _envelope(report_name: str, status: str, message: str, **extra) -> dict:
	# ``run`` (every status) names the Prepared Report it answers for: the chat's
	# report card reads it (jarvis.chat.report_runs).
	# prepared_report stays ALWAYS truthy so the dashboards_api run-time backstop
	# (which rejects a tile whose run_report result carries prepared_report) keeps
	# working regardless of status.
	env = {
		"prepared_report": True,
		"status": status,
		"report_name": report_name,
		"message": message,
	}
	env.update(extra)
	return env
