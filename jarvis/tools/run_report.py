import frappe
from frappe.desk.query_report import run as frappe_run_report

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import _prepared_reports


def run_report(report_name: str, filters: dict | None = None) -> dict:
	"""Execute a saved Frappe Report by name.

	A normal report runs inline and returns ``{columns, result}``. A **Prepared
	Report** (flagged too heavy to run inline) is handled by ``_prepared_reports``:
	instead of Frappe's empty ``{prepared_report: True, doc: None}`` it returns a
	status envelope (ready / generating / started / failed) and, when nothing is
	ready, starts the background generation - see that module. A paused scheduler
	or unreachable job queue is raised as InvalidArgumentError.

	Frappe enforces report-level permissions internally and raises
	frappe.PermissionError on denial; we translate that to PermissionDeniedError
	so all tools share one exception contract.
	"""
	if not report_name:
		raise InvalidArgumentError("report_name is required")

	if not frappe.db.exists("Report", report_name):
		raise InvalidArgumentError(f"unknown Report: {report_name}")

	# A raw get_value on the named report reads the correct flag even for a
	# Custom Report (get_report_doc carries the custom doc's own prepared_report).
	if frappe.db.get_value("Report", report_name, "prepared_report"):
		return _prepared_reports.handle_prepared(report_name, filters or {}, user=frappe.session.user)

	try:
		return frappe_run_report(report_name=report_name, filters=filters or {})
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"no permission to run report {report_name}") from e
