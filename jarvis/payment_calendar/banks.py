"""Company-currency book cash, after the existing ledger permission gates."""

import frappe
from frappe.utils import getdate

from jarvis.payment_calendar.books import validate_finance_books
from jarvis.payment_calendar.permissions import require_company
from jarvis.payment_calendar.sources import CalendarSourceUnavailable, money
from jarvis.tools._ledger_access import assert_gl_balance_in_scope, assert_ledger_readable


def validate_bank_accounts(company: str, accounts: list[str]) -> list[str]:
	"""Authorize selection without reading money; deduplicate database identities."""
	require_company(company)
	assert_ledger_readable()
	if (
		not isinstance(accounts, list)
		or not accounts
		or len(accounts) > 50
		or any(not isinstance(a, str) or not a for a in accounts)
	):
		raise frappe.ValidationError("Select bank or cash accounts.")
	names = []
	for name in dict.fromkeys(accounts):
		assert_gl_balance_in_scope(name, None)
		if not frappe.has_permission("Account", "read", doc=name):
			raise frappe.PermissionError("A selected account is unavailable.")
		account = frappe.db.get_value(
			"Account", name, ["name", "company", "is_group", "disabled", "account_type"], as_dict=True
		)
		if (
			not account
			or account.company != company
			or account.is_group
			or account.disabled
			or account.account_type not in ("Bank", "Cash")
		):
			raise frappe.ValidationError("Select enabled bank or cash ledger accounts from this company.")
		if account.name not in names:
			names.append(account.name)
	return names


def read_bank_balances(company: str, accounts: list[str], as_of: str) -> dict:
	if not as_of:
		raise frappe.ValidationError("The accounting date is required.")

	from erpnext.accounts.utils import (
		FiscalYearError,
		get_balance_on,
		get_currency_precision,
		get_fiscal_year,
	)

	values = require_company(company)
	names = validate_bank_accounts(company, accounts)
	try:
		get_fiscal_year(getdate(as_of), company=company, verbose=0)
	except FiscalYearError as exc:
		raise CalendarSourceUnavailable("fiscal_year_unavailable") from exc
	validate_finance_books(company, names, as_of)
	precision = get_currency_precision() or 2
	previous_form = frappe.local.form_dict
	try:
		# ERPNext falls back to request fields even for explicit None arguments.
		frappe.local.form_dict = frappe._dict()
		balances = [
			{
				"account": name,
				"balance": str(
					money(
						get_balance_on(
							account=name,
							date=getdate(as_of),
							company=company,
							in_account_currency=False,
							party_type=None,
							party=None,
							cost_center=None,
							ignore_account_permission=False,
							finance_book=None,
							include_default_fb_balances=False,
						),
						precision,
					)
				),
			}
			for name in names
		]
	finally:
		frappe.local.form_dict = previous_form
	total = sum(money(row["balance"], precision) for row in balances)
	return {
		"total": str(money(total, precision)),
		"accounts": balances,
		"currency": values.default_currency,
		"precision": precision,
	}
