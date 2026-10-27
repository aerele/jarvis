"""Canonical sources are unfiltered; reject overlapping alternative books."""

import frappe
from frappe.utils import getdate

from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.permissions import require_company


def _validate_books(doctype, company, as_of, filters):
	default = require_company(company).default_finance_book
	allowed = {None, "", default}
	books = frappe.get_all(
		doctype,
		filters={"company": company, "posting_date": ["<=", getdate(as_of)], **filters},
		pluck="finance_book",
		distinct=True,
		limit_page_length=0,
	)
	if any(book not in allowed for book in books):
		raise CalendarSourceUnavailable("multibook_unsupported")


def validate_obligation_books(company, account_type, as_of):
	_validate_books("Payment Ledger Entry", company, as_of, {"account_type": account_type, "delinked": 0})


def validate_finance_books(company, accounts, as_of):
	_validate_books("GL Entry", company, as_of, {"account": ["in", accounts], "is_cancelled": 0})
	_validate_books("Payment Ledger Entry", company, as_of, {"delinked": 0})
