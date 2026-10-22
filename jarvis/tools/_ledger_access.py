"""Ledger-read gate for the balance tools.

A party or account balance is a sum of GL Entries, so reading one needs the
same access as reading the ledger. Read on the Customer (or Account) is not
enough: Stock and Sales Users read customers, and Sales/Purchase Users read
accounts, without any ledger access. ERPNext v16 doesn't expose these helpers
over HTTP, so the jarvis tools are the only route to those numbers and must
apply the gate themselves.

Allowed when the user can read GL Entry (the General Ledger report's own
gate), or, for a Customer / Supplier balance, can run Accounts Receivable /
Accounts Payable, the reports that already show that balance.

The stock-quantity tools get the matching gate here: the Item, and the
Warehouse together with its Company, must be readable by the caller.
"""

from __future__ import annotations

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools._company_scope import assert_company_permitted

_PARTY_REPORT = {
	"Customer": "Accounts Receivable",
	"Supplier": "Accounts Payable",
}


def _can_run_report(report_name: str) -> bool:
	# The checks frappe.desk.query_report.get_report_doc applies, without its
	# frappe.throw (which would leave a message in the response's message_log).
	if not frappe.db.exists("Report", report_name):
		return False
	report = frappe.get_cached_doc("Report", report_name)
	return bool(
		not report.disabled and report.is_permitted() and frappe.has_permission(report.ref_doctype, "report")
	)


def can_read_ledger(party_type: str | None = None) -> bool:
	if frappe.has_permission("GL Entry", "read"):
		return True
	report = _PARTY_REPORT.get(party_type or "")
	return bool(report and _can_run_report(report))


def assert_ledger_readable(party_type: str | None = None) -> None:
	if not can_read_ledger(party_type):
		raise PermissionDeniedError(
			"no permission to read ledger balances (needs GL Entry read"
			+ (f" or the {_PARTY_REPORT[party_type]} report" if party_type in _PARTY_REPORT else "")
			+ ")"
		)


def assert_item_readable(item_code: str) -> None:
	# doc= applies Item User Permissions; ERPNext's own check is doctype-level only.
	if not frappe.has_permission("Item", "read", doc=item_code):
		raise PermissionDeniedError(f"no read permission on Item {item_code}")


def assert_warehouse_readable(warehouse: str) -> None:
	"""The caller can read ``warehouse`` (doc-level, so Warehouse User
	Permissions apply) and is permitted the Company that owns it."""
	if not frappe.has_permission("Warehouse", "read", doc=warehouse):
		raise PermissionDeniedError(f"no read permission on Warehouse {warehouse}")
	assert_company_permitted(frappe.db.get_value("Warehouse", warehouse, "company"))


def assert_warehouse_named_if_restricted(warehouse: str | None) -> None:
	"""Without a warehouse, ERPNext's valuation rate comes from the item's
	default warehouse or is averaged across every warehouse, so a caller with
	Warehouse User Permissions must name a warehouse (checked separately by
	``assert_warehouse_readable``)."""
	if warehouse:
		return
	perms = get_user_permissions(frappe.session.user) or {}
	# A permission applicable only to some other DocType doesn't scope warehouses.
	if any(p.get("applicable_for") in (None, "", "Warehouse", "Bin") for p in perms.get("Warehouse") or []):
		raise PermissionDeniedError(
			"warehouse is required: your access is restricted by Warehouse User Permission, "
			"so a rate across all warehouses can't be returned. Pass one of your permitted warehouses"
		)
