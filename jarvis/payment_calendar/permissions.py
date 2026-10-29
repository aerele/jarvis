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
		"Payment Entry",
	}
	relevant.update(gl_dimension_doctypes())
	relevant.update(
		frappe.get_all(
			"Party Type", filters={"account_type": ["in", ["Payable", "Receivable"]]}, pluck="name"
		)
	)
	reasons = ["restricted_obligations"] if relevant.intersection(get_user_permissions(user)) else []
	for doctype in ("Purchase Invoice", "Sales Invoice", "Journal Entry", "Payment Entry"):
		if not frappe.has_permission(doctype, "read"):
			reasons.append("restricted_document_details")
		perms = get_role_permissions(frappe.get_meta(doctype), user)
		if {"read", "report"}.intersection(perms.get("if_owner", {})):
			reasons.append("owner_only_obligations")
	query_hooks = frappe.get_hooks("permission_query_conditions")
	if any(
		query_hooks.get(dt)
		for dt in ("Purchase Invoice", "Sales Invoice", "Payment Ledger Entry", "GL Entry", "Account", "*")
	):
		reasons.append("custom_accounting_scope")
	return sorted(set(reasons))


def match_columns(columns):
	"""Report columns for Frappe's ``get_filtered_data`` user-match filter.

	ERPNext 15.124 / 16.x link Party Type and Voucher Type to DocType. On Frappe v15
	the match filter then needs read on DocType itself and throws for anyone but a
	System Manager. They name doctypes, not records, and every caller rechecks each
	voucher and party with ``visible_names``, so they are matched as plain values.
	"""
	return [
		{**col, "fieldtype": "Data"}
		if col.get("fieldtype") == "Link" and col.get("options") == "DocType"
		else col
		for col in columns
	]


def visible_names(doctype: str, names, filters: dict | None = None) -> set[str]:
	"""Use Frappe's read-or-select query gate, including custom party scopes."""
	names = sorted(set(names))
	allowed = set()
	try:
		for offset in range(0, len(names), 500):
			allowed.update(
				frappe.get_list(
					doctype,
					filters={**(filters or {}), "name": ["in", names[offset : offset + 500]]},
					pluck="name",
					limit_page_length=0,
				)
			)
	except frappe.PermissionError:
		return set()
	hooks = frappe.get_hooks("has_permission")
	if hooks.get(doctype) or hooks.get("*"):
		ptype = "select" if frappe.only_has_select_perm(doctype) else "read"
		visible = set()
		for name in allowed:
			try:
				if frappe.has_permission(doctype, ptype, doc=name):
					visible.add(name)
			except (frappe.DoesNotExistError, frappe.PermissionError):
				continue
		return visible
	return allowed


def require_floor_write(company: str) -> None:
	try:
		assert_company_permitted(company)
	except PermissionDeniedError as exc:
		raise frappe.PermissionError("Company access is required.") from exc
	if not {"Accounts Manager", "System Manager"}.intersection(frappe.get_roles()):
		raise frappe.PermissionError("An Accounts Manager must configure the minimum cash balance.")
	if not frappe.has_permission("Company", "write", doc=company):
		raise frappe.PermissionError("Company write permission is required.")
