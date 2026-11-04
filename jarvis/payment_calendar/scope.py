"""Server-owned accounting scope; request fields cannot change its as-of date."""

import json
from datetime import date

import frappe
from frappe.utils import add_days, add_months, get_first_day, get_last_day, nowdate

from jarvis.exceptions import PermissionDeniedError
from jarvis.payment_calendar.banks import validate_bank_accounts
from jarvis.payment_calendar.permissions import require_company

PROTOCOL = 1


def _account_ids(accounts):
	if isinstance(accounts, str):
		if len(accounts) > 8192:
			raise frappe.ValidationError("Select up to 50 bank or cash accounts.")
		try:
			accounts = json.loads(accounts)
		except (ValueError, TypeError) as exc:
			raise frappe.ValidationError("Select bank or cash accounts.") from exc
	if accounts is None:
		accounts = []
	if (
		not isinstance(accounts, list)
		or len(accounts) > 50
		or any(not isinstance(value, str) or not value or len(value) > 140 for value in accounts)
	):
		raise frappe.ValidationError("Select up to 50 bank or cash accounts.")
	return accounts


def normalize_scope(company: str, accounts: list[str] | str | None, *, month: str | None = None) -> dict:
	if not isinstance(company, str) or not company.strip() or len(company) > 140:
		raise frappe.ValidationError("Choose a company.")
	accounts = _account_ids(accounts)
	company = company.strip()
	require_company(company)
	company = frappe.db.get_value("Company", company, "name")
	try:
		names = sorted(validate_bank_accounts(company, accounts)) if accounts else []
	except PermissionDeniedError as exc:
		raise frappe.PermissionError("Selected bank balances are outside your permitted scope.") from exc
	anchor = nowdate()
	first = str(get_first_day(add_months(anchor, -12)))
	last = str(get_last_day(add_months(anchor, 11)))
	window_start, window_end = first, add_days(last, 30)
	if month is not None:
		try:
			if not isinstance(month, str) or len(month) != 7:
				raise ValueError
			requested = date.fromisoformat(month + "-01")
			if requested.isoformat()[:7] != month or not first[:7] <= month <= last[:7]:
				raise ValueError
		except (TypeError, ValueError) as exc:
			raise frappe.ValidationError("Choose a month within the calendar range.") from exc
		window_start, window_end = month + "-01", str(get_last_day(requested))
	return {
		**({"month": month} if month is not None else {}),
		"protocol": PROTOCOL,
		"actor": frappe.session.user,
		"company": company,
		"accounts": names,
		"anchor": anchor,
		"selectable_start": first,
		"selectable_end": last,
		"window_start": window_start,
		"window_end": window_end,
	}
