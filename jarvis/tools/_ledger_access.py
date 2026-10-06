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
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import PermissionDeniedError

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
