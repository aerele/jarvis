import json

import frappe
from frappe.desk.query_report import run as frappe_run_report

from jarvis.chat import macro_report_wait
from jarvis.exceptions import (
	DelegateReportRefused,
	FefoScopeRefused,
	InvalidArgumentError,
	PermissionDeniedError,
	SnapshotReloadRefused,
)
from jarvis.tools import _prepared_reports
from jarvis.tools._report_scope import attach_scope, resolve_scope


def run_report(report_name: str, filters: dict | str | None = None) -> dict:
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
	return _run_report(report_name, filters)


def _run_report(
	report_name: str,
	filters: dict | str | None = None,
	*,
	inline_only: bool = False,
	no_background: bool = False,
) -> dict:
	"""The body of ``run_report``. ``inline_only`` is for a report snapshot reload and is
	not a tool argument: a Prepared Report raises ``SnapshotReloadRefused`` instead of
	being enqueued, and the given filters are used as they are: a Custom Report's saved
	default filters (which Frappe would substitute) cannot move the scope of a reload.

	``no_background`` is for the ``compute`` sandbox, which never starts background work
	and wants every row in one call: a Prepared Report answers with its finished copy for
	these filters (every row, ``prepared: True`` and its ``as_of``), and with no finished
	copy it runs inline (``ignore_prepared_report``) as the user, under the sandbox's own
	deadline and read-only enforcement (``prepared: False``)."""
	if not report_name:
		raise InvalidArgumentError("report_name is required")
	filters = _normalize_filters(filters)
	# Apply the FEFO scope gate before report lookup or execution. A forged
	# request must not reveal another source or reach the report engine.
	from jarvis.chat.agent_scope import assert_active_fefo_report_scope

	try:
		assert_active_fefo_report_scope(report_name, filters)
	except InvalidArgumentError as e:
		raise FefoScopeRefused(str(e)) from e
	# A snapshot-config delegate gets configuration only from its immutable Run.
	# Saved report definitions are mutable data-access policy, so this execution-
	# boundary guard refuses them even for an already-started run that still carries
	# an old report declaration. New bundle contracts do not declare this tool.
	from jarvis.tools import _delegate_capability

	refusal = _delegate_capability.snapshot_config_report_refusal(report_name)
	if refusal:
		raise DelegateReportRefused(refusal)

	if not frappe.db.exists("Report", report_name):
		raise InvalidArgumentError(f"unknown Report: {report_name}")
	filters, scope = resolve_scope(report_name, filters)

	# A raw get_value on the named report reads the correct flag even for a
	# Custom Report (get_report_doc carries the custom doc's own prepared_report).
	if frappe.db.get_value("Report", report_name, "prepared_report"):
		if inline_only:
			raise SnapshotReloadRefused("prepared")
		if no_background:
			return attach_scope(_copy_or_inline(report_name, filters), scope)
		# Inside a macro run's own step a copy must be as new as the run (see macro_report_wait).
		ctx = macro_report_wait.current_context()
		return attach_scope(
			_prepared_reports.handle_prepared(
				report_name, filters, user=frappe.session.user, since=ctx.started_at if ctx else None
			),
			scope,
		)

	try:
		# A reload runs with the filters stored with the snapshot: `are_default_filters=False`
		# keeps Frappe from substituting a Custom Report's saved defaults (v15 and v16 both
		# have the argument). A normal run is called exactly as before.
		pinned = {"are_default_filters": False} if inline_only else {}
		return attach_scope(frappe_run_report(report_name=report_name, filters=filters, **pinned), scope)
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"no permission to run report {report_name}") from e


def _copy_or_inline(report_name: str, filters: dict) -> dict:
	"""A Prepared Report for the compute sandbox: its finished copy for ``filters``
	(``{columns, result, as_of, run}``, every row, read as the caller after the gates
	``run()`` applies), else the report run inline as the caller. Starts nothing in the
	background either way."""
	user = frappe.session.user
	filled = _prepared_reports.gate_and_fill(report_name, filters, user=user)
	state, completed = _prepared_reports.peek(report_name, filled, user=user)
	if state == "ready" and completed is not None:
		return {**completed, "prepared": True}
	try:
		data = frappe_run_report(report_name=report_name, filters=filled, ignore_prepared_report=True)
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"no permission to run report {report_name}") from e
	return {**data, "prepared": False}


def _normalize_filters(filters: dict | str | None) -> dict:
	"""One dict for scope resolution and both execution paths. Tool callers may
	send filters as a JSON string; anything that is not an object is a caller
	error, never an uncaught 500 from ``dict(filters)`` downstream."""
	if not filters:
		return {}
	if isinstance(filters, str):
		try:
			filters = json.loads(filters)
		except (ValueError, TypeError) as e:
			raise InvalidArgumentError("filters must be a JSON object of field: value pairs") from e
	if not isinstance(filters, dict):
		raise InvalidArgumentError("filters must be an object of field: value pairs")
	return filters
