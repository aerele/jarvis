"""Bank and cash accounts a user may total: the one rule for the account picker and the balances job."""

import frappe

from jarvis.exceptions import PermissionDeniedError
from jarvis.payment_calendar.banks import validate_bank_accounts
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.permissions import require_company, require_report

MAX_ACCOUNTS = 500
# `validate_bank_accounts` takes at most 50 names per call.
CHUNK = 50
DENIED = (frappe.PermissionError, PermissionDeniedError)


def request_gate(company: str) -> None:
	"""The company and Accounts Payable gates; reads no accounts."""
	require_company(company)
	require_report(company, "Payable")


def permitted_accounts(company: str) -> list:
	"""The request gate, then every enabled account the user may read balances for."""
	request_gate(company)
	accounts = frappe.get_list(
		"Account",
		filters={
			"company": company,
			"is_group": 0,
			"disabled": 0,
			"account_type": ["in", ["Bank", "Cash"]],
		},
		fields=["name", "account_name", "account_type", "account_currency"],
		order_by="account_name",
		limit_page_length=MAX_ACCOUNTS + 1,
	)
	if len(accounts) > MAX_ACCOUNTS:
		raise CalendarSourceUnavailable("too_large")
	allowed = set(_permitted_names(company, [account.name for account in accounts]))
	return [account for account in accounts if account.name in allowed]


def validate_in_chunks(company: str, names: list[str]) -> None:
	"""Validate every name; the first refusal stops the pass."""
	for chunk in chunks(names):
		validate_bank_accounts(company, chunk)


def chunks(names: list[str]):
	for start in range(0, len(names), CHUNK):
		yield names[start : start + CHUNK]


def _permitted_names(company: str, names: list[str]) -> list[str]:
	"""One call per chunk; only a chunk that fails is re-checked one account at a time."""
	allowed = []
	for chunk in chunks(names):
		try:
			validate_bank_accounts(company, chunk)
			allowed += chunk
		except DENIED:
			allowed += [name for name in chunk if _permitted(company, name)]
	return allowed


def _permitted(company: str, name: str) -> bool:
	try:
		validate_bank_accounts(company, [name])
		return True
	except DENIED:
		return False
