"""A saved Frappe Report as an export source (``export_query(report_name=...)``).

The gap this fills (#2102): ``run_report`` shows the model at most a few thousand
rows (its result budget and the prepared-report row cap), and no tool wrote the
WHOLE report to a file, so "give me that report as Excel" ended with "use Desk".
Here the rows go from the report straight into the file, server-side, and never
pass through the model.

Two outcomes, because a Prepared Report may not be ready:

* an :class:`ExportModel` with every row, for the renderers; or
* the ``generating`` / ``failed`` / ``started`` envelope ``run_report`` returns for
  a prepared report whose copy is not there yet (``started`` kicks the background
  run off, as ``run_report`` would). The caller hands that envelope back as is.

Gates, in order, the same ones ``run_report`` applies plus Desk's export gate:
``get_report_doc`` (Has-Role allow-list, ``ref_doctype`` report permission,
disabled check, Custom Report resolution), the Export DocPerm on ``ref_doctype``
(``frappe.permissions.can_export``, what Desk's own Excel export checks), the
receivable/payable scope (``_report_scope``), the report's filter defaults (as
Desk sends them, for BOTH the inline and the prepared path), and
``validate_filters_permissions``. A Report Builder report is a saved list view,
not a columns+rows result: refused with the ``doctype`` source as the way.
"""

from __future__ import annotations

import frappe
from frappe.desk.query_report import run as frappe_run_report
from frappe.utils import now_datetime

from jarvis.exceptions import InvalidArgumentError, NoDataError, PermissionDeniedError
from jarvis.tools import _prepared_reports, _report_rows
from jarvis.tools._export.model import ExportModel
from jarvis.tools._export.resolvers import _EXPORT_SELECT_LIMIT_S, immediate_row_limit
from jarvis.tools._report_scope import resolve_scope
from jarvis.tools.get_report_filters import get_report_filters
from jarvis.tools.run_report import _normalize_filters


class ReportTooLarge(InvalidArgumentError):
	"""A PREPARED report's completed copy is over the immediate row limit. The copy exists,
	so the background export job can convert it under its own (larger) cap: export_query
	hands off when that route is available, else this is the refusal shown."""

	def __init__(self, message: str, filters: dict):
		super().__init__(message)
		self.filters = filters


def too_large_message(report_name: str, cap: int) -> str:
	return (
		f"{report_name} has more than {cap:,} rows for these filters - too many for one export. "
		f"Narrow the filters (a date range, a company, a status), or use the report's own "
		f"Excel export in Desk (Menu > Export)."
	)


def resolve(report_name: str, filters=None) -> tuple[ExportModel | None, dict | None]:
	"""``(model, None)`` with every row of the report, or ``(None, status)`` for a
	Prepared Report that has no completed copy yet (see the module docstring)."""
	report, filters = authorize(report_name, filters)
	if report.prepared_report:
		completed, status = _prepared_reports.completed_or_status(
			report_name, filters, user=frappe.session.user
		)
		if status is not None:
			# The filled filters ride along: a background job started from this status
			# must look for (and the run was started with) exactly these.
			status["filters"] = filters
			return None, status
		columns, result, as_of = completed["columns"], completed["result"], completed["as_of"]
	else:
		columns, result = _run_inline(report_name, filters)
		as_of = str(now_datetime())

	return _model(report, report_name, columns, result, as_of, filters=filters), None


def authorize(report_name: str, filters=None):
	"""Every gate, then the filters as the run gets them: ``(report doc, filled filters)``.
	Shared by the immediate export and the background job's ``prepare``, so both refuse
	the same input in the same words."""
	if not report_name:
		raise InvalidArgumentError("report_name is required")
	if not frappe.db.exists("Report", report_name):
		raise InvalidArgumentError(f"unknown Report: {report_name}")
	user = frappe.session.user
	report = _gate(report_name)
	filters, _scope = resolve_scope(report_name, _normalize_filters(filters))
	# Desk sends the report's own defaults under the user's values; so do we, on
	# both paths: a Script Report's required filter with a default must not reach
	# the script unset, and a prepared run started here must match a Desk run.
	filters = _prepared_reports.fill_defaults(report_name, filters)
	_prepared_reports.gate_filters(report_name, filters, user)
	_require_filters(report_name, filters)
	return report, filters


def _gate(report_name: str):
	"""The report doc, resolved and gated as ``run()`` gates it, plus the Export
	DocPerm Desk's export checks. Report Builder reports are refused here, before
	any further work."""
	report = _prepared_reports.gate_report(report_name)
	if report.report_type == "Report Builder":
		raise InvalidArgumentError(
			f"{report_name} is a Report Builder report (a saved list view). Export its records with "
			f"export_query(doctype={report.ref_doctype!r}, filters=..., fields=[...]) instead."
		)
	if not frappe.permissions.can_export(report.ref_doctype):
		raise PermissionDeniedError(
			f"no export permission on {report.ref_doctype}, which {report_name} reports on - "
			f"the report can be read (run_report) but not exported to a file."
		)
	return report


def _require_filters(report_name: str, filters: dict) -> None:
	"""One error naming every required filter still unset (the defaults are already
	filled in), instead of the report script's own failure (``format requires a
	mapping``, a KeyError). Unreadable filter metadata: nothing to check."""
	try:
		required = get_report_filters(report_name).get("required") or []
	except Exception:
		return
	missing = [name for name in required if _prepared_reports.is_unset(filters.get(name))]
	if missing:
		raise InvalidArgumentError(
			f"{report_name} needs these filters: {', '.join(missing)}. Pass them in filters "
			f"(get_report_filters lists every filter and its type)."
		)


def _run_inline(report_name: str, filters: dict) -> tuple[list, list]:
	"""Run a Query / Script Report now, as ``run_report`` and ``report_pdf`` do, with
	each of its statements under the export SELECT limit (``resolvers._run_bounded``
	has the reasoning): the plugin aborts the call at 30 s and the render and save
	still follow. ``add_total_row`` is already applied inside ``run``."""
	from jarvis import _failure_kind
	from jarvis.api import _agent_read_timeout_s, statement_time_limit

	agent_limit = _agent_read_timeout_s()
	limit = min(agent_limit, _EXPORT_SELECT_LIMIT_S) if agent_limit else 0
	try:
		with statement_time_limit(limit):
			res = frappe_run_report(report_name=report_name, filters=filters, ignore_prepared_report=True)
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"no permission to run report {report_name}") from e
	except Exception as e:
		if _failure_kind.too_slow(e):
			raise InvalidArgumentError(
				f"{report_name} ran past the {limit}-second limit for one export call, so the database "
				f"stopped it. Narrow the filters, or use the report's own Excel export in Desk."
			) from None
		# The report itself failed: a bad filter value, invalid report SQL. A caller
		# error from this tool's point of view, not a 500; the traceback stays in the
		# Error Log for a genuine fault.
		frappe.log_error(
			title=f"export_query: running report {report_name} failed",
			message=frappe.get_traceback(),
		)
		raise InvalidArgumentError(f"could not run report {report_name}: {e}") from e
	return res.get("columns") or [], res.get("result") or []


def _model(
	report, report_name: str, columns: list, result: list, as_of: str | None, *, filters=None
) -> ExportModel:
	"""Every row under the visible columns, as file values. Over the immediate row limit
	a PREPARED report's copy is handed to the background job (``ReportTooLarge``, caught
	by export_query); an inline report is refused with a plain InvalidArgumentError, since
	nothing can convert it without running it again."""
	visible = _report_rows.visible_columns(columns)
	if not visible:
		raise InvalidArgumentError(
			f"report {report_name} returned no columns to export (it may need filters, or be a "
			f"layout with no tabular result)"
		)
	if not result:
		raise NoDataError(f"{report_name} returned no rows for these filters - nothing to export.")
	cap = immediate_row_limit()
	if len(result) > cap:
		message = too_large_message(report_name, cap)
		if report.prepared_report:
			raise ReportTooLarge(message, filters or {})
		raise InvalidArgumentError(message)
	rows = [
		[_report_rows.as_file_value(_report_rows.cell(row, col, idx), col) for idx, col in visible]
		for row in result
	]
	return ExportModel(
		# Keys are unique (a highlight rule names a column by key or header), the
		# values still come from the report's own fieldname per column.
		columns=_unique([col["fieldname"] for _, col in visible]),
		rows=rows,
		total=len(rows),
		meta={
			"doctype": report.ref_doctype,
			"report_name": report_name,
			"headers": _unique([col["label"] for _, col in visible]),
			"title": report_name,
			"as_of": as_of,
		},
	)


def _unique(names: list[str]) -> list[str]:
	"""Two columns with one name are told apart by a counter, so no column is lost."""
	seen: dict[str, int] = {}
	out = []
	for name in names:
		n = seen.get(name, 0)
		seen[name] = n + 1
		out.append(name if n == 0 else f"{name} ({n + 1})")
	return out
