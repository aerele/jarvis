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

import json

import frappe
from frappe.core.doctype.prepared_report.prepared_report import (
	get_completed_prepared_report,
	make_prepared_report,
	process_filters_for_prepared_report,
)
from frappe.desk.query_report import get_report_doc, validate_filters_permissions
from frappe.utils import cint
from frappe.utils.background_jobs import get_redis_conn
from frappe.utils.scheduler import is_scheduler_inactive

from jarvis import compat
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError

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


def handle_prepared(report_name: str, raw_filters: dict, *, user: str) -> dict:
	"""Return a status envelope (ready / generating / failed / started) for a
	prepared report. Pull-model, stateless, runs as ``user``. Raises
	InvalidArgumentError when the background queue can't run the report."""
	_gate_report(report_name)
	filters = _canonical_filters(raw_filters)
	_gate_filters(report_name, filters, user)

	# 1. A completed copy we can read? (0 rows is a valid answer; unreadable -> fall through)
	dn = get_completed_prepared_report(filters, user, report_name)
	if dn:
		ready = _read_completed(dn)
		if ready is not None:
			return ready

	# 2. The most recent non-completed attempt decides: still generating, or a
	#    terminal failure to surface. A stalled attempt returns None so we re-run.
	pending = _pending_state(report_name, filters, user)
	if pending is not None:
		return pending

	# 3. Nothing usable -> (re)generate. Refuse (raise) if the queue can't run it,
	#    so we never leave an orphan Queued row that no worker will drain.
	_require_worker()
	make_prepared_report(report_name, filters)
	return _envelope(
		report_name,
		"started",
		"This report is heavy, so it runs in the background - I've started it. "
		"Ask me again in a little while and I'll have the results.",
	)


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
def _gate_report(report_name: str) -> None:
	"""Gate the report exactly as run() does: Has-Role allow-list, ref_doctype
	report permission, disabled-check, Custom-Report resolution."""
	try:
		get_report_doc(report_name)
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"no permission to run report {report_name}") from e
	except frappe.ValidationError as e:
		# a disabled report - not a permission problem; surface as an argument error
		raise InvalidArgumentError(str(e) or f"report {report_name} is unavailable") from e


def _gate_filters(report_name: str, filters: dict, user: str) -> None:
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


# --------------------------------------------------------------------------- #
# completed copy (read DIRECTLY - never via run() by name)
# --------------------------------------------------------------------------- #
def _read_completed(dn: str) -> dict | None:
	"""Read a completed Prepared Report's rows straight from its doc. Returns a
	`ready` envelope, or None if the stored output can't be read (missing/corrupt
	gz) so the caller falls through to (re)generate rather than emit empty."""
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
	capped, note = _cap_rows(result)
	as_of = doc.get("report_end_time") or doc.get("creation")
	env = _envelope(doc.report_name, "ready", _ready_message(len(result), as_of))
	env["columns"] = columns or []
	env["result"] = capped
	env["as_of"] = str(as_of) if as_of else None
	if note:
		env["row_note"] = note
	return env


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


def _cap_rows(result: list):
	if len(result) <= _MAX_ROWS:
		return result, None
	note = (
		f"Showing the first {_MAX_ROWS} of {len(result)} rows. Ask me to narrow "
		f"the filters, or to export the full report."
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
	row = _latest(
		report_name,
		filters,
		user,
		(*_INFLIGHT_STATUSES, *_FAILED_STATUSES),
		extra_fields=["status", "creation"],
	)
	if not row:
		return None
	if row.status == "Queued":
		# Enqueued and waiting for a worker - never re-trigger (see _STARTED_STALL_MARGIN).
		return _generating(report_name)
	if row.status == "Started":
		age = frappe.utils.time_diff_in_seconds(frappe.utils.now(), row.creation)
		if age <= _report_timeout(report_name) + _STARTED_STALL_MARGIN:
			return _generating(report_name)
		return None  # started but the worker died mid-run -> caller re-generates
	# Error / Failed - surface it and stop; re-firing the same doomed job on every
	# ask is the loop we are avoiding. The user can adjust the filters (a new match
	# key -> a fresh run). The raw error (a traceback) stays out of the message;
	# Frappe already logged it on the failing job.
	return _envelope(
		report_name,
		"failed",
		"That report failed to generate. If it needs a filter you didn't set "
		"(a company, a date range, a required value), tell me and I'll run it "
		"again with it.",
	)


def _latest(report_name, filters, user, statuses, extra_fields=None):
	"""Newest owner-scoped Prepared Report for these filters in one of ``statuses``."""
	rows = frappe.get_all(
		"Prepared Report",
		filters={
			"report_name": report_name,
			"filters": process_filters_for_prepared_report(filters),
			"owner": user,
			"status": ("in", statuses),
		},
		fields=["name", *(extra_fields or [])],
		order_by="creation desc",
		limit=1,
		ignore_permissions=True,
	)
	return rows[0] if rows else None


def _generating(report_name: str) -> dict:
	return _envelope(
		report_name,
		"generating",
		"Your report is still running in the background. Ask me again in a "
		"little while and I'll have the results.",
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
