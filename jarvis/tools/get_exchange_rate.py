"""Currency exchange rate as of a date.

Wraps ``erpnext.setup.utils.get_exchange_rate``. The underlying helper
consults Currency Exchange records first, then optionally fetches a
live rate from the configured provider. Lets the agent answer
"what's USD-INR today?" or "convert 1000 GBP to AED for the 2026-01
fiscal close" without manual lookups.

Read-only. ERPNext's helper rejects unknown currencies internally;
we surface that as InvalidArgumentError at the boundary.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError


def get_exchange_rate(
	from_currency: str,
	to_currency: str,
	transaction_date: str | None = None,
) -> dict:
	"""Return ``{rate, from_currency, to_currency, transaction_date}``
	for the rate as of ``transaction_date`` (defaults to today)."""
	if not from_currency:
		raise InvalidArgumentError("from_currency is required")
	if not to_currency:
		raise InvalidArgumentError("to_currency is required")
	if not frappe.db.exists("Currency", from_currency):
		raise InvalidArgumentError(f"unknown Currency: {from_currency}")
	if not frappe.db.exists("Currency", to_currency):
		raise InvalidArgumentError(f"unknown Currency: {to_currency}")

	from erpnext.setup.utils import get_exchange_rate as _ger

	rate = _ger(
		from_currency=from_currency,
		to_currency=to_currency,
		transaction_date=transaction_date,
	)
	return {
		"rate": float(rate or 0),
		"from_currency": from_currency,
		"to_currency": to_currency,
		"transaction_date": transaction_date,
	}
