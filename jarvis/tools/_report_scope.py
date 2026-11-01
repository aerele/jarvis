"""Execution scope receipts for selected ERPNext standard reports.

Resolve effective filters before execution (and prepared-report cache lookup),
never infer them later from the answer or current site settings.  A receipt is
only attached where the tool can state the report's company and period exactly.
Other reports deliberately receive no receipt.
"""

import json

import frappe
from frappe.exceptions import ValidationError
from frappe.utils import getdate, nowdate

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._company_scope import assert_company_permitted

PARTY_REPORTS = frozenset(
	("Accounts Payable", "Accounts Payable Summary", "Accounts Receivable", "Accounts Receivable Summary")
)
LEDGER_REPORTS = frozenset(("General Ledger", "Trial Balance"))
REPORTS = PARTY_REPORTS | LEDGER_REPORTS


def resolve_scope(report_name: str, filters: dict) -> tuple[dict, dict | None]:
	if report_name not in REPORTS:
		return filters, None
	report = frappe.get_cached_doc("Report", report_name)
	if report.report_type != "Script Report" or report.is_standard != "Yes" or report.module != "Accounts":
		return filters, None
	if report_name in LEDGER_REPORTS:
		return _ledger_scope(report_name, filters)

	resolved = dict(filters)
	supplied = resolved.get("company")
	company = supplied or frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		return filters, None
	_assert_company_readable(company, supplied=bool(supplied))
	resolved["company"] = company
	resolved["report_date"] = _report_date(resolved.get("report_date"))
	return resolved, {
		"version": 1,
		"report_name": report_name,
		"company": company,
		"report_date": resolved["report_date"],
		"currency_mode": "account"
		if resolved.get("in_party_currency") or resolved.get("party_account")
		else "company",
	}


def _ledger_scope(report_name: str, filters: dict) -> tuple[dict, dict | None]:
	"""Return a receipt only for an explicit, company-currency ledger window.

	``General Ledger`` and ``Trial Balance`` have different default rules from
	the payable reports.  Do not inject defaults for them: a caller that needs a
	trusted period must supply the exact company/from/to window it intends to
	measure.  Normal interactive calls retain ERPNext's existing defaults but do
	not gain a receipt they could mistake for a fixed audit boundary.
	"""
	resolved = dict(filters)
	# An ordinary report call follows its normal ERP permissions.  A running
	# Ledger Scrutiny delegate has one additional, server-stamped boundary: its
	# native evidence reports can only read the exact current/as-of window (or
	# its explicit prior-FY GL baseline).  Check before the normal "no receipt"
	# return so an incomplete or defaulted report cannot escape that boundary.
	company = resolved.get("company")
	from_date = _window_date(resolved.get("from_date"), "from_date")
	to_date = _window_date(resolved.get("to_date"), "to_date")
	# Native Ledger Scrutiny evidence must retain opening GL detail and submitted
	# Period Closing Voucher movement. These are server-owned additions only for an
	# active Ledger run; ordinary report calls retain their filters unchanged.
	launch_scope = _active_ledger_scrutiny_scope()
	if launch_scope is not None:
		if report_name == "General Ledger":
			resolved["show_opening_entries"] = 1
		elif (
			report_name == "Trial Balance" and "with_period_closing_entry_for_current_period" not in resolved
		):
			resolved["with_period_closing_entry_for_current_period"] = 1
	if launch_scope is not None:
		from jarvis.chat.ledger_scrutiny_scope import validate_ledger_report_scope

		candidate = {**resolved, "from_date": from_date, "to_date": to_date}
		validate_ledger_report_scope(launch_scope, report_name, candidate)
	if not company or not from_date or not to_date:
		return filters, None
	if from_date > to_date:
		raise InvalidArgumentError("from_date cannot be after to_date")
	if report_name == "Trial Balance":
		fiscal_year = str(resolved.get("fiscal_year") or "").strip()
		if not fiscal_year:
			return filters, None
		_validate_trial_balance_window(fiscal_year, from_date, to_date)
		# The raw GL source contains submitted Period Closing Voucher rows. Make
		# the trusted Trial Balance receipt use the same population; otherwise the
		# report silently drops those rows and cannot certify evaluator parity.
		resolved["with_period_closing_entry_for_current_period"] = 1
	_assert_company_readable(company, supplied=True)
	resolved["company"] = company
	resolved["from_date"] = from_date
	resolved["to_date"] = to_date
	scope = {
		"version": 1,
		"report_name": report_name,
		"company": company,
		"from_date": from_date,
		"to_date": to_date,
		"currency_mode": "company",
	}
	if report_name == "Trial Balance":
		scope["fiscal_year"] = fiscal_year
		scope["period_closing_entries"] = "included"
	return resolved, scope


def _active_ledger_scrutiny_scope() -> dict | None:
	"""Return the active Ledger Scrutiny launch scope for this tool request.

	``None`` means this is an ordinary report call.  A session that belongs to a
	running Ledger Scrutiny run returns its stored scope even when malformed; the
	validator then fails closed instead of silently treating it as unscoped.
	"""
	from jarvis.tools._agent_run_ctx import get_session_key

	session_key = get_session_key()
	if not session_key:
		return None
	run = frappe.db.get_value(
		"Jarvis Agent Run",
		{"session_key": session_key, "status": "running"},
		["agent", "scope_json"],
		as_dict=True,
	)
	if not run or run.get("agent") != "ledger-scrutiny-auditor":
		return None
	try:
		scope = json.loads(run.get("scope_json") or "{}")
	except (TypeError, ValueError):
		return {}
	return scope if isinstance(scope, dict) else {}


def _window_date(value, name: str) -> str | None:
	"""Parse an explicitly supplied ledger-window endpoint."""
	if value in (None, ""):
		return None
	try:
		parsed = getdate(value)
	except (ValidationError, ValueError, TypeError) as e:
		raise InvalidArgumentError(f"{name} is not a valid date: {value!r}") from e
	if not parsed:
		raise InvalidArgumentError(f"{name} is not a valid date: {value!r}")
	return str(parsed)


def _validate_trial_balance_window(fiscal_year: str, from_date: str, to_date: str) -> None:
	"""Refuse a window ERPNext would otherwise silently rewrite to its FY bounds."""
	fy = frappe.db.get_value("Fiscal Year", fiscal_year, ["year_start_date", "year_end_date"], as_dict=True)
	if not fy:
		raise InvalidArgumentError(f"unknown fiscal_year: {fiscal_year}")
	start = str(getdate(fy.year_start_date))
	end = str(getdate(fy.year_end_date))
	if from_date < start or to_date > end:
		raise InvalidArgumentError(
			f"Trial Balance window must be within fiscal_year {fiscal_year} ({start} to {end})"
		)


def _assert_company_readable(company: str, *, supplied: bool) -> None:
	# Standard reports often define Link filters in JS, which run() does not
	# receive here. Gate the effective company explicitly, including defaults.
	try:
		assert_company_permitted(company)
		if not frappe.has_permission("Company", "read", doc=company) and not frappe.has_permission(
			"Company", "select", doc=company
		):
			raise PermissionDeniedError("No permission to access the report company")
	except PermissionDeniedError:
		if supplied:
			raise
		# The site default was never shown to this user; do not reveal its name.
		raise PermissionDeniedError(
			"No permission for the default report company; specify a company you can access"
		) from None


def _report_date(value) -> str:
	try:
		parsed = getdate(value or nowdate())
	except (ValidationError, ValueError, TypeError) as e:
		raise InvalidArgumentError(f"report_date is not a valid date: {value!r}") from e
	if not parsed:
		raise InvalidArgumentError(f"report_date is not a valid date: {value!r}")
	return str(parsed)


def attach_scope(result: dict, scope: dict | None) -> dict:
	# Queued/failed prepared reports are not answers, even when the outer tool
	# call succeeded. An empty *completed* result is still a scoped report.
	if not scope or not isinstance(result, dict) or not isinstance(result.get("result"), list):
		return result
	if result.get("prepared_report") and result.get("status") != "ready":
		return result
	if result.get("status") not in (None, "ready"):
		return result
	scope = dict(scope)
	# Account currency may vary per row; do not label it with the company's
	# currency, even for an empty result. The UI names the basis explicitly.
	if scope["currency_mode"] == "company" and not result.get("prepared_report"):
		scope["currency"] = frappe.get_cached_value("Company", scope["company"], "default_currency")
	# These standard reports expose their row currency as `currency`. Preserve
	# mixed currencies; never imply a consolidated monetary total. Missing row
	# codes make the list incomplete, so retain only the currency basis then.
	rows = result["result"]
	if rows and all(
		isinstance(row, dict) and isinstance(row.get("currency"), str) and row["currency"].strip()
		for row in rows
	):
		scope["currencies"] = sorted({row["currency"].strip() for row in rows})
	return {**result, "report_scope": scope}
