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

A balance also sums GL Entries across every Cost Center, Project and Branch,
so a User Permission on one of those dimensions has to be honoured by the
caller (``assert_gl_balance_in_scope``).

The stock-quantity tools get the matching gate here: the Item, and the
Warehouse together with its Company, must be readable by the caller.
"""

from __future__ import annotations

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools._company_scope import assert_company_permitted

# GL dimensions whose User Permissions slice a ledger sum (Company is scoped
# separately by _company_scope). Frappe has already expanded group values to
# their descendants in get_user_permissions.
_GL_DIMENSIONS = ("Cost Center", "Project", "Branch")

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


def gl_dimension_restrictions(user: str | None = None) -> dict[str, set[str]]:
	"""``{dimension: permitted values}`` for each GL dimension the user is
	restricted on for GL Entry. A User Permission whose ``applicable_for`` is
	another DocType does not slice GL Entry, so it is not counted."""
	perms = get_user_permissions(user or frappe.session.user) or {}
	restrictions = {}
	for dimension in _GL_DIMENSIONS:
		values = {
			p.get("doc")
			for p in perms.get(dimension) or []
			if p.get("applicable_for") in (None, "", "GL Entry")
		}
		if values:
			restrictions[dimension] = values
	return restrictions


def assert_gl_balance_in_scope(account: str | None, cost_center: str | None) -> None:
	"""Refuse a balance that would sum GL Entries outside the caller's GL
	dimension User Permissions.

	ERPNext's get_balance_on can only narrow by Cost Center, and only for a
	Profit and Loss account (it ignores cost_center for Balance Sheet accounts
	and party-only queries). So a Cost Center restriction is honoured only for
	a P&L account queried with a permitted cost_center; a Project or Branch
	restriction can't be honoured at all."""
	restrictions = gl_dimension_restrictions()
	if not restrictions:
		return
	unsupported = [d for d in ("Project", "Branch") if d in restrictions]
	if unsupported:
		raise PermissionDeniedError(
			f"balances can't be returned: your access is restricted by {' and '.join(unsupported)} "
			"User Permission, and a balance sums ledger entries across every " + " and ".join(unsupported)
		)
	allowed = restrictions["Cost Center"]
	if not cost_center or cost_center not in allowed:
		raise PermissionDeniedError(
			"balances need a cost_center: your access is restricted by Cost Center User Permission, "
			"so pass one of your permitted cost centers" + (f" (not {cost_center!r})" if cost_center else "")
		)
	if not account or frappe.db.get_value("Account", account, "report_type") != "Profit and Loss":
		raise PermissionDeniedError(
			"a cost_center narrows only a Profit and Loss account balance; with a Cost Center User "
			"Permission, query a Profit and Loss account"
		)


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
