"""Execution scope for ERPNext's standard payable/receivable reports.

Resolve defaults BEFORE execution (and prepared-report cache lookup), never
guess them later from the answer or the current site settings. Other reports
have different semantics and deliberately do not receive this receipt.
"""

import frappe
from frappe.utils import getdate, nowdate

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools._company_scope import assert_company_permitted

REPORTS = frozenset(
	("Accounts Payable", "Accounts Payable Summary", "Accounts Receivable", "Accounts Receivable Summary")
)


def resolve_scope(report_name: str, filters: dict) -> tuple[dict, dict | None]:
	if report_name not in REPORTS:
		return filters, None
	report = frappe.get_cached_doc("Report", report_name)
	if report.report_type != "Script Report" or report.is_standard != "Yes" or report.module != "Accounts":
		return filters, None
	resolved = dict(filters)
	company = resolved.get("company") or frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		return filters, None
	assert_company_permitted(company)
	# Standard reports often define Link filters in JS, which run() does not
	# receive here. Gate the effective company explicitly, including defaults.
	if not frappe.has_permission("Company", "read", doc=company) and not frappe.has_permission(
		"Company", "select", doc=company
	):
		raise PermissionDeniedError("No permission to access the report company")
	resolved["company"] = company
	resolved["report_date"] = str(getdate(resolved.get("report_date") or nowdate()))
	return resolved, {
		"version": 1,
		"report_name": report_name,
		"company": company,
		"report_date": resolved["report_date"],
		"currency_mode": "account"
		if resolved.get("in_party_currency") or resolved.get("party_account")
		else "company",
	}


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
	return {**result, "report_scope": scope}
