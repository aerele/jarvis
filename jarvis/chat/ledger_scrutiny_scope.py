"""Server-owned launch boundary for the Ledger Scrutiny Auditor.

Ledger Scrutiny is a company-period assessment.  Unlike a best-effort generic
assistant turn, it must not start without a readable Company and a complete,
parseable period/report-date boundary.  The resolver runs as the mapped run-as
user, so this module never uses the installation owner as an access fallback.
"""

from __future__ import annotations

from datetime import date

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._company_scope import assert_company_permitted

AGENT = "ledger-scrutiny-auditor"


def requires_bound_scope(agent: str | None) -> bool:
	"""Return whether a scope fault must refuse launch for this agent."""
	return agent == AGENT


def validate_bound_scope(scope: dict | None) -> None:
	"""Validate the generic resolver output under the run-as identity.

	The generic resolver may deliberately degrade a non-audit turn to an
	unscoped conversation.  That would make a ledger conclusion unsafe, so
	Ledger Scrutiny validates the resolved values before dispatch instead.
	"""
	if not isinstance(scope, dict):
		raise InvalidArgumentError("Ledger Scrutiny requires a company and a complete period scope")
	company = scope.get("company")
	if not isinstance(company, str) or not company.strip():
		raise InvalidArgumentError("Ledger Scrutiny requires a readable company")
	fiscal_year = scope.get("fiscal_year")
	if not isinstance(fiscal_year, str) or not fiscal_year.strip():
		raise InvalidArgumentError("Ledger Scrutiny requires a fiscal year for its Trial Balance evidence")
	from_date = _date(scope.get("from_date"), "from_date")
	to_date = _date(scope.get("to_date"), "to_date")
	report_date = _date(scope.get("report_date"), "report_date")
	if from_date > to_date:
		raise InvalidArgumentError("Ledger Scrutiny requires from_date on or before to_date")
	if from_date > min(to_date, report_date):
		raise InvalidArgumentError("Ledger Scrutiny report_date cannot cap before from_date")
	if not frappe.db.exists("Company", company):
		raise InvalidArgumentError("Ledger Scrutiny requires an existing company")
	assert_company_permitted(company)
	# ERPNext's Auditor can read GL Entry while holding Company ``select`` (not
	# ``read``).  The launch gate must accept that valid aggregate identity, but
	# it still checks both Company User Permission and Company visibility.
	if not frappe.has_permission("Company", "read", doc=company) and not frappe.has_permission(
		"Company", "select", doc=company
	):
		raise PermissionDeniedError("Ledger Scrutiny run-as user cannot access the selected company")


def scoped_visibility(user: str) -> bool:
	"""Return the launch-time GL-dimension slice state for ``user``.

	The result is stamped into this run's immutable scope.  If the permission
	profile cannot be read, use the safe value: a ledger reviewer must see a
	partial result, never a falsely firm-wide conclusion.
	"""
	from frappe.permissions import get_user_permissions

	from jarvis.jarvis.doctype.jarvis_agent_installation.jarvis_agent_installation import (
		_GL_SCOPED_DIMENSIONS,
	)

	try:
		permissions = get_user_permissions(user) or {}
	except Exception:
		return True
	return any(permissions.get(doctype) for doctype in _GL_SCOPED_DIMENSIONS)


def bind_writeback_scope(launch_scope: dict | None, delegate_scope: dict | None) -> dict:
	"""Return the server scope only when the delegate repeated it unchanged.

	The evaluator's output contains its input scope to make a replay reviewable.
	That echo is evidence, not an authority to edit the launch snapshot.  This
	comparison is intentionally strict for all accounting dimensions, including
	the optional prior window and the server-owned permission-slice flag.
	"""
	launch = _canonical_scope(launch_scope, source="launch")
	delegate = _canonical_scope(delegate_scope, source="writeback")
	if delegate != launch:
		raise InvalidArgumentError("Ledger Scrutiny writeback scope does not match its server launch scope")
	return dict(launch_scope)


def validate_ledger_report_scope(launch_scope: dict | None, report_name: str, filters: dict) -> None:
	"""Refuse a Ledger report outside an active Ledger Scrutiny run's scope.

	Generic report permissions still apply.  This additional boundary prevents a
	delegate with broad run-as access from using its audit session to read another
	company or period.  The current window ends at the server-owned as-of date;
	the only other General Ledger window is the stamped prior-FY baseline.
	"""
	launch = _canonical_scope(launch_scope, source="launch")
	if not isinstance(filters, dict):
		raise PermissionDeniedError("Ledger Scrutiny can only run reports in its server launch scope")
	if report_name not in {"General Ledger", "Trial Balance"}:
		raise PermissionDeniedError("Ledger Scrutiny can only run its declared ledger reports")
	allowed_fields = {
		"General Ledger": {"company", "from_date", "to_date", "show_opening_entries"},
		"Trial Balance": {
			"company",
			"fiscal_year",
			"from_date",
			"to_date",
			"with_period_closing_entry_for_current_period",
		},
	}[report_name]
	if any(
		key not in allowed_fields and value not in (None, "", [], {}, False, 0)
		for key, value in filters.items()
	):
		raise PermissionDeniedError("Ledger Scrutiny can only run complete native ledger reports")
	company = str(filters.get("company") or "").strip()
	from_date = str(filters.get("from_date") or "").strip()
	to_date = str(filters.get("to_date") or "").strip()
	current = (launch["from_date"], min(launch["to_date"], launch["report_date"]))
	prior = (launch["prior_fy_start"], launch["prior_fy_end"])
	allowed_window = (from_date, to_date) == current or (
		report_name == "General Ledger" and prior[0] and prior[1] and (from_date, to_date) == prior
	)
	if company != launch["company"] or not allowed_window:
		raise PermissionDeniedError("Ledger Scrutiny can only run reports in its server launch scope")
	if (
		report_name == "Trial Balance"
		and str(filters.get("fiscal_year") or "").strip() != launch["fiscal_year"]
	):
		raise PermissionDeniedError("Ledger Scrutiny can only run reports in its server launch scope")
	if report_name == "General Ledger" and not _enabled(filters.get("show_opening_entries")):
		raise PermissionDeniedError("Ledger Scrutiny can only run complete native ledger reports")
	if report_name == "Trial Balance" and not _enabled(
		filters.get("with_period_closing_entry_for_current_period")
	):
		raise PermissionDeniedError("Ledger Scrutiny can only run complete native ledger reports")


def _canonical_scope(scope: dict | None, *, source: str) -> dict:
	"""Validate and normalize the immutable values used at the trust boundary."""
	if not isinstance(scope, dict):
		raise InvalidArgumentError(f"Ledger Scrutiny {source} scope is missing")
	company = str(scope.get("company") or "").strip()
	fiscal_year = str(scope.get("fiscal_year") or "").strip()
	if not company or not fiscal_year:
		raise InvalidArgumentError(f"Ledger Scrutiny {source} scope is incomplete")
	from_date = _date(scope.get("from_date"), "from_date").isoformat()
	to_date = _date(scope.get("to_date"), "to_date").isoformat()
	report_date = _date(scope.get("report_date"), "report_date").isoformat()
	if from_date > to_date or from_date > min(to_date, report_date):
		raise InvalidArgumentError(f"Ledger Scrutiny {source} scope has an invalid period")
	prior_start = _optional_date(scope.get("prior_fy_start"), "prior_fy_start")
	prior_end = _optional_date(scope.get("prior_fy_end"), "prior_fy_end")
	if bool(prior_start) != bool(prior_end) or (prior_start and prior_start > prior_end):
		raise InvalidArgumentError(f"Ledger Scrutiny {source} scope has an invalid prior period")
	if "scoped_visibility" not in scope:
		raise InvalidArgumentError(f"Ledger Scrutiny {source} scope has no permission-slice state")
	return {
		"company": company,
		"fiscal_year": fiscal_year,
		"from_date": from_date,
		"to_date": to_date,
		"report_date": report_date,
		"prior_fy_start": prior_start,
		"prior_fy_end": prior_end,
		"scoped_visibility": _boolean(scope["scoped_visibility"], "scoped_visibility"),
	}


def _date(value, name: str) -> date:
	try:
		parsed = date.fromisoformat(str(value or "")[:10])
	except (TypeError, ValueError) as e:
		raise InvalidArgumentError(f"Ledger Scrutiny requires a valid {name}") from e
	return parsed


def _optional_date(value, name: str) -> str | None:
	if value in (None, ""):
		return None
	return _date(value, name).isoformat()


def _boolean(value, name: str) -> bool:
	"""Accept the JSON boolean forms without treating arbitrary text as true."""
	if isinstance(value, bool):
		return value
	if isinstance(value, int) and value in (0, 1):
		return bool(value)
	if isinstance(value, str) and value.strip().lower() in {"0", "1", "false", "true"}:
		return value.strip().lower() in {"1", "true"}
	raise InvalidArgumentError(f"Ledger Scrutiny {name} must be a boolean")


def _enabled(value) -> bool:
	"""Return true only for an explicit report flag that preserves full evidence."""
	return (
		value is True
		or value == 1
		or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"})
	)
