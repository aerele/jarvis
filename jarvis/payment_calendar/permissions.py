"""Reuse the report and ledger gates without granting another reader's access."""

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions
from frappe.permissions import get_role_permissions

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools._company_scope import assert_company_permitted
from jarvis.tools._ledger_access import gl_dimension_doctypes
from jarvis.tools._report_scope import _assert_company_readable

REPORTS = {"Payable": "Accounts Payable", "Receivable": "Accounts Receivable"}


def require_company(company: str) -> dict:
	if not isinstance(company, str) or not company:
		raise frappe.ValidationError("Choose a company.")
	try:
		_assert_company_readable(company, supplied=True)
	except PermissionDeniedError as exc:
		raise frappe.PermissionError("Company access is required.") from exc
	values = frappe.db.get_value(
		"Company", company, ["default_currency", "default_finance_book"], as_dict=True
	)
	if not values:
		raise frappe.DoesNotExistError("Company unavailable.")
	return values


def require_report(company: str, account_type: str):
	from frappe.desk.query_report import get_report_doc

	if account_type not in REPORTS:
		raise frappe.ValidationError("Choose payables or receivables.")
	require_company(company)
	report = get_report_doc(REPORTS[account_type])
	if report.report_type != "Script Report" or report.is_standard != "Yes" or report.module != "Accounts":
		raise frappe.PermissionError("The standard accounting report is unavailable.")
	return report


def incomplete_scope_reasons() -> list[str]:
	"""Do not pair a permission-limited obligation list with company-wide cash."""
	user = frappe.session.user
	relevant = {
		"Account",
		"Supplier",
		"Customer",
		"Sales Person",
		"Purchase Invoice",
		"Sales Invoice",
		"Journal Entry",
	}
	relevant.update(gl_dimension_doctypes())
	reasons = ["restricted_obligations"] if relevant.intersection(get_user_permissions(user)) else []
	for doctype in ("Purchase Invoice", "Sales Invoice", "Journal Entry"):
		if not frappe.has_permission(doctype, "read"):
			reasons.append("restricted_document_details")
		perms = get_role_permissions(frappe.get_meta(doctype), user)
		if perms.get("if_owner", {}).get("read") or perms.get("if_owner", {}).get("report"):
			reasons.append("owner_only_obligations")
	query_hooks = frappe.get_hooks("permission_query_conditions")
	if any(
		query_hooks.get(dt)
		for dt in ("Purchase Invoice", "Sales Invoice", "Payment Ledger Entry", "GL Entry", "Account")
	):
		reasons.append("custom_accounting_scope")
	return sorted(set(reasons))


def require_floor_write(company: str) -> None:
	try:
		assert_company_permitted(company)
	except PermissionDeniedError as exc:
		raise frappe.PermissionError("Company access is required.") from exc
	if not {"Accounts Manager", "System Manager"}.intersection(frappe.get_roles()):
		raise frappe.PermissionError("An Accounts Manager must configure the minimum cash balance.")
	if not frappe.has_permission("Company", "write", doc=company):
		raise frappe.PermissionError("Company write permission is required.")
