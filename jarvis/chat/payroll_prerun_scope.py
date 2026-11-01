"""Server-owned scope for the Payroll Pre-Run auditor.

The selected Payroll Entry is the only customer-supplied boundary.  Company,
period, report cutoff, and the record boundary are facts read from that entry
as the run-as user.  They must never come from the mutable installation JSON.
"""

from __future__ import annotations

from datetime import date

import frappe

from jarvis.exceptions import InvalidArgumentError

AGENT = "payroll-prerun-auditor"
CONFIG_KEYS = frozenset({"payroll_entry", "reviewer_of_record"})


def is_payroll_prerun(agent: str | None) -> bool:
	"""Return whether an installation uses the single-Payroll-Entry contract."""
	return agent == AGENT


def requires_bound_scope(agent: str | None) -> bool:
	"""Return whether a scope-resolution fault must stop a launch."""
	return is_payroll_prerun(agent)


def configuration_issues(raw, *, require_complete: bool = True) -> dict[str, str]:
	"""Return safe configuration faults for the current (run-as) user.

	A disabled installation can keep an empty draft.  Once a value is supplied,
	it is checked as the identity that will read payroll.  An enabled or launched
	installation needs both declared values and one readable, usable entry.
	"""
	config = _parse(raw)
	if config is None:
		return {"config": "Configuration must be a JSON object."}

	issues: dict[str, str] = {}
	unsupported = set(config) - CONFIG_KEYS
	if unsupported:
		issues["config"] = "Use only the selected Payroll Entry and named payroll reviewer."

	entry = config.get("payroll_entry")
	if entry is None:
		if require_complete:
			issues["payroll_entry"] = (
				"Select a readable Payroll Entry that is not cancelled or fully submitted."
			)
	elif not _valid_text(entry):
		issues["payroll_entry"] = "Select a readable Payroll Entry that is not cancelled or fully submitted."
	else:
		try:
			_validate_entry(str(entry))
		except (frappe.PermissionError, frappe.DoesNotExistError, frappe.ValidationError, ValueError):
			issues["payroll_entry"] = (
				"Select a readable Payroll Entry that is not cancelled or fully submitted."
			)

	reviewer = config.get("reviewer_of_record")
	if reviewer is None:
		if require_complete:
			issues["reviewer_of_record"] = "Enter the named payroll reviewer."
	elif not _valid_text(reviewer):
		issues["reviewer_of_record"] = "Enter the named payroll reviewer."

	return issues


def validate_saved_configuration(raw) -> dict:
	"""Reject malformed supplied values while allowing an empty disabled draft."""
	issues = configuration_issues(raw, require_complete=False)
	if issues:
		frappe.throw(list(issues.values()), title="Check Payroll Pre-Run configuration", as_list=True)
	return _parse(raw) or {}


def require_configuration(raw) -> dict:
	"""Require a complete configuration before enablement or launch."""
	issues = configuration_issues(raw, require_complete=True)
	if issues:
		frappe.throw(list(issues.values()), title="Complete Payroll Pre-Run configuration", as_list=True)
	return _parse(raw) or {}


def scope_from_config(raw) -> dict:
	"""Build the immutable run scope from one readable Payroll Entry.

	The caller is already impersonating the run-as user.  This function reads no
	company or period from configuration.  It also clamps the report date before
	dispatch, so every later source read has the same as-of ceiling.
	"""
	config = require_configuration(raw)
	entry = _validate_entry(config["payroll_entry"])

	# Import at call time: ``agent_scope.resolve_scope`` routes to this module,
	# while this helper reuses its shared fiscal-year and prior-FY derivation.
	from jarvis.chat.agent_scope import _resolve

	scope = _resolve(entry["company"], None, entry["from_date"], entry["to_date"])
	today = _as_date(frappe.utils.today())
	if today is None:
		raise InvalidArgumentError("the site report date is invalid")
	scope["report_date"] = min(today, _as_date(entry["to_date"])).isoformat()
	scope["record_scope"] = {"doctype": "Payroll Entry", "name": entry["name"]}
	scope["scope_kind"] = "single_batch"
	return scope


def _parse(raw) -> dict | None:
	try:
		value = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (TypeError, ValueError):
		return None
	return value if isinstance(value, dict) else None


def _valid_text(value) -> bool:
	return isinstance(value, str) and bool(value) and value == value.strip()


def _as_date(value) -> date | None:
	try:
		return date.fromisoformat(str(value)[:10])
	except (TypeError, ValueError):
		return None


def _truthy(value) -> bool:
	return str(value).lower() in {"1", "1.0", "true", "yes"}


def _validate_entry(name: str) -> dict:
	"""Read and validate one entry without exposing payroll values.

	Field-level permissions are checked after the document read.  A run-as user
	that can see a Payroll Entry title but not the fields that form its boundary
	cannot start an audit with an invented or incomplete period.
	"""
	entry = frappe.get_doc("Payroll Entry", name)
	entry.check_permission("read")
	fields = (
		"name",
		"company",
		"start_date",
		"end_date",
		"docstatus",
		"salary_slips_submitted",
		"salary_slip_based_on_timesheet",
	)
	before = {field: entry.get(field) for field in fields}
	entry.apply_fieldlevel_read_permissions()
	if before != {field: entry.get(field) for field in fields}:
		raise frappe.PermissionError("Payroll Entry boundary fields are not readable.")

	company = entry.get("company")
	start, end = _as_date(entry.get("start_date")), _as_date(entry.get("end_date"))
	if not _valid_text(company) or not start or not end or start > end:
		raise ValueError("Payroll Entry has no valid company and period.")
	if str(entry.get("docstatus")) not in {"0", "0.0", "1", "1.0"}:
		raise ValueError("Payroll Entry is cancelled.")
	if _truthy(entry.get("salary_slips_submitted")):
		raise ValueError("Payroll Entry salary slips are already submitted.")
	if _truthy(entry.get("salary_slip_based_on_timesheet")):
		raise ValueError("Timesheet-based Payroll Entry is unsupported.")

	return {
		"name": str(entry.get("name") or name),
		"company": str(company),
		"from_date": start.isoformat(),
		"to_date": end.isoformat(),
	}
