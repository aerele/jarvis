"""Bind Statutory Payroll runs to one readable, completed Payroll Entry.

The statutory evaluator is intentionally narrow.  It reads one submitted
monthly Payroll Entry, its roster, and the required comparison months.  The
installation may store the selected entry, but it must never supply the
company, dates, record scope, or as-of date that reach a run.  This module
derives those values from the selected record while running as ``run_as_user``.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

import frappe

from jarvis.exceptions import PermissionDeniedError

AGENT = "statutory-payroll-compliance-auditor"
RECORD_SCOPE = "Payroll Entry"
_SERVER_SCOPE_KEYS = (
	"company",
	"fiscal_year",
	"from_date",
	"to_date",
	"prior_period_from",
	"prior_period_to",
	"report_date",
	"record_scope",
)


class ConfigurationError(ValueError):
	"""A safe configuration error for the Configure form and launch guard."""

	def __init__(self, field: str, message: str):
		super().__init__(message)
		self.field = field
		self.message = message


def is_statutory_payroll(agent: str | None) -> bool:
	return agent == AGENT


def configuration_issues(raw, *, require_complete: bool = True) -> dict[str, str]:
	"""Return only safe setup faults for the configured execution identity.

	A disabled installation can retain an empty draft.  Once an entry is given it
	must already be readable and usable by the run-as user.  Enablement and launch
	require the complete selection, so they cannot fall back to a company-wide or
	fiscal-year scope.
	"""
	try:
		_validate_configuration(raw, require_complete=require_complete)
	except ConfigurationError as error:
		return {error.field: error.message}
	except (
		frappe.PermissionError,
		frappe.DoesNotExistError,
		frappe.ValidationError,
		PermissionDeniedError,
	):
		return {"payroll_entry": "Select a readable submitted monthly Payroll Entry."}
	return {}


def validate_saved_configuration(raw) -> dict:
	"""Validate supplied draft values without requiring a disabled draft to be full."""
	issues = configuration_issues(raw, require_complete=False)
	if issues:
		frappe.throw(list(issues.values()), title="Check Statutory Payroll configuration", as_list=True)
	return _parse(raw) or {}


def require_configuration(raw) -> dict:
	"""Require a complete readable Payroll Entry before an enabled run starts."""
	issues = configuration_issues(raw, require_complete=True)
	if issues:
		frappe.throw(list(issues.values()), title="Complete Statutory Payroll configuration", as_list=True)
	return _parse(raw) or {}


def resolve_scope(raw) -> dict:
	"""Return the server-derived scope for one selected Payroll Entry.

	The result contains no caller-provided company, period, record scope, or
	report date.  ``payroll_entry`` remains an installation-only selection; the
	delegate receives its identity only as the server-created ``record_scope``.
	"""
	config, entry = _validate_configuration(raw, require_complete=True)
	# ``entry`` is present when require_complete=True.  Keep the assertion local
	# so a future change cannot accidentally reintroduce a generic fallback.
	if entry is None:
		raise ConfigurationError("payroll_entry", "Select a submitted monthly Payroll Entry.")
	start = entry["start_date"]
	end = entry["end_date"]
	prior_end = start - timedelta(days=1)
	prior_start = prior_end.replace(day=1)
	return {
		"company": entry["company"],
		"fiscal_year": None,
		"from_date": start.isoformat(),
		"to_date": end.isoformat(),
		"prior_period_from": prior_start.isoformat(),
		"prior_period_to": prior_end.isoformat(),
		"report_date": frappe.utils.today(),
		"record_scope": {"doctype": RECORD_SCOPE, "name": entry["name"]},
	}


def canonical_writeback_scope(stamped_scope, returned_scope) -> dict:
	"""Return the fixed scope only when a delegate returned it unchanged.

	The scope in a delegate writeback is model-relayed.  It must never replace
	the Payroll Entry, company, period, preceding month, or server report date
	that the bench stamped before dispatch.  Extra returned keys are discarded;
	they are not a channel to extend ``Run.scope_json``.
	"""
	if not isinstance(stamped_scope, dict):
		raise ConfigurationError("scope", "This run has no usable server-bound Payroll Entry scope.")
	try:
		expected = {key: stamped_scope[key] for key in _SERVER_SCOPE_KEYS}
	except KeyError:
		raise ConfigurationError(
			"scope", "This run has no usable server-bound Payroll Entry scope."
		) from None
	_validate_server_scope(expected)
	if not isinstance(returned_scope, dict) or any(
		returned_scope.get(key) != expected[key] for key in _SERVER_SCOPE_KEYS
	):
		raise ConfigurationError(
			"scope", "The output does not match this run's server-bound Payroll Entry scope."
		)
	return expected


def _validate_configuration(raw, *, require_complete: bool) -> tuple[dict, dict | None]:
	config = _parse(raw)
	if config is None:
		raise ConfigurationError("config", "Configuration must be a JSON object.")
	target = config.get("payroll_entry")
	if target is None:
		if require_complete:
			raise ConfigurationError("payroll_entry", "Select a submitted monthly Payroll Entry.")
		return config, None
	if not _valid_text(target):
		raise ConfigurationError("payroll_entry", "Select a submitted monthly Payroll Entry.")
	return config, _read_entry(target)


def _parse(raw) -> dict | None:
	try:
		value = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (TypeError, ValueError):
		return None
	return value if isinstance(value, dict) else None


def _valid_text(value) -> bool:
	return isinstance(value, str) and bool(value) and value == value.strip()


def _read_entry(name: str) -> dict:
	"""Read the entry and its Company without bypassing field-level permissions."""
	entry = frappe.get_doc(RECORD_SCOPE, name)
	entry.check_permission("read")
	fields = ("company", "start_date", "end_date", "docstatus")
	values = _readable_values(entry, fields)
	company = values["company"]
	if not _valid_text(company):
		raise ConfigurationError("payroll_entry", "The Payroll Entry company is not readable.")
	if frappe.utils.cint(values["docstatus"]) != 1:
		raise ConfigurationError("payroll_entry", "Select a submitted monthly Payroll Entry.")
	start = _as_date(values["start_date"])
	end = _as_date(values["end_date"])
	if start is None or end is None:
		raise ConfigurationError("payroll_entry", "The Payroll Entry period is not usable.") from None
	if not _is_complete_month(start, end):
		raise ConfigurationError("payroll_entry", "Select a complete calendar-month Payroll Entry.")
	report_date = _as_date(frappe.utils.today())
	if report_date is None:
		raise ConfigurationError("payroll_entry", "The site report date is not usable.") from None
	if end > report_date:
		raise ConfigurationError("payroll_entry", "Select a Payroll Entry whose month has ended.")
	_validate_company(company)
	return {"name": name, "company": company, "start_date": start, "end_date": end}


def _readable_values(doc, fields: tuple[str, ...]) -> dict:
	before = {field: doc.get(field) for field in fields}
	doc.apply_fieldlevel_read_permissions()
	after = {field: doc.get(field) for field in fields}
	if before != after:
		raise frappe.PermissionError("Required Payroll Entry fields are not readable.")
	return after


def _validate_company(name: str) -> None:
	from jarvis.tools._company_scope import assert_company_permitted

	assert_company_permitted(name)
	company = frappe.get_doc("Company", name)
	company.check_permission("read")
	values = _readable_values(company, ("is_group",))
	if values["is_group"] is None:
		raise frappe.PermissionError("Company group status is not readable.")
	if frappe.utils.cint(values["is_group"]):
		raise ConfigurationError("payroll_entry", "The Payroll Entry must belong to a leaf Company.")


def _is_complete_month(start: date, end: date) -> bool:
	return start.day == 1 and end == date(
		start.year, start.month, calendar.monthrange(start.year, start.month)[1]
	)


def _as_date(value) -> date | None:
	"""Accept only a real, non-empty date from Frappe's permissive helper."""
	if value is None or not isinstance(value, (str, date)):
		return None
	if value in ("", "0000-00-00") or (isinstance(value, str) and value != value.strip()):
		return None
	try:
		result = frappe.utils.getdate(value)
	except (TypeError, ValueError, frappe.ValidationError):
		return None
	if not isinstance(result, date):
		return None
	if isinstance(value, str) and result.isoformat() != value:
		return None
	return result


def _validate_server_scope(scope: dict) -> None:
	"""Verify the stamped scope is one exact completed Payroll Entry month."""
	if not _valid_text(scope.get("company")) or scope.get("fiscal_year") is not None:
		raise ConfigurationError("scope", "This run has no usable server-bound Payroll Entry scope.")
	record_scope = scope.get("record_scope")
	if (
		not isinstance(record_scope, dict)
		or set(record_scope) != {"doctype", "name"}
		or record_scope.get("doctype") != RECORD_SCOPE
		or not _valid_text(record_scope.get("name"))
	):
		raise ConfigurationError("scope", "This run has no usable server-bound Payroll Entry scope.")
	start = _as_date(scope.get("from_date"))
	end = _as_date(scope.get("to_date"))
	prior_start = _as_date(scope.get("prior_period_from"))
	prior_end = _as_date(scope.get("prior_period_to"))
	report_date = _as_date(scope.get("report_date"))
	if not all((start, end, prior_start, prior_end, report_date)) or not _is_complete_month(start, end):
		raise ConfigurationError("scope", "This run has no usable server-bound Payroll Entry scope.")
	expected_prior_end = start - timedelta(days=1)
	if (
		prior_start != expected_prior_end.replace(day=1)
		or prior_end != expected_prior_end
		or end > report_date
	):
		raise ConfigurationError("scope", "This run has no usable server-bound Payroll Entry scope.")
