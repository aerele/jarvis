"""Canonical sources are unfiltered; reject overlapping alternative books.

Inside a snapshot view (`book_memo`, opened by `snapshot.consistent_read`) with the reader switch `on` and
the company served, the Payment Ledger book groups are read ONCE per (company, as_of) and shared by the bank
check and both side checks. The view is one repeatable-read snapshot, so every check sees the same rows and
the answer equals today's by construction. The memo holds only (account_type, book) pairs, never leaves the
process and is never written. Everywhere else (no view, switch not `on`, company not served, v15) today's
queries run.
"""

from contextlib import contextmanager

import frappe
from frappe.utils import getdate

from jarvis.payment_calendar.compatibility import source_profile
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.permissions import require_company

_MEMO = "payment_calendar_book_memo"
_GROUPS = (
	"SELECT account_type, finance_book FROM `tabPayment Ledger Entry` "
	"WHERE company = %s AND posting_date <= %s AND delinked = 0 GROUP BY account_type, finance_book"
)


def validate_obligation_books(company, account_type, as_of):
	memo = _active_memo()
	if memo is None or not memo.serves(company):
		_validate_books("Payment Ledger Entry", company, as_of, {"account_type": account_type, "delinked": 0})
		return
	default = require_company(company).default_finance_book
	# The database compares `account_type` without case and trailing spaces; the memo must refuse the same rows.
	wanted = _same_type_key(account_type)
	_refuse_other_books(
		company,
		[book for kind, book in memo.groups(company, as_of) if _same_type_key(kind) == wanted],
		default,
	)


def validate_finance_books(company, accounts, as_of):
	_validate_books("GL Entry", company, as_of, {"account": ["in", accounts], "is_cancelled": 0})
	validate_bank_ple_books(company, as_of)


def validate_bank_ple_books(company, as_of):
	"""The bank's Payment Ledger check: every account type of the company, from the memo inside a view."""
	memo = _active_memo()
	if memo is None or not memo.serves(company):
		_validate_books("Payment Ledger Entry", company, as_of, {"delinked": 0})
		return
	default = require_company(company).default_finance_book
	_refuse_other_books(company, [book for _kind, book in memo.groups(company, as_of)], default)


def allowed_books(company, default_book) -> set:
	"""The finance books a canonical source may hold: none, blank, or the company default.

	`company` names the company `default_book` belongs to; every book check uses this one rule.
	"""
	return {None, "", default_book}


def ple_book_groups_direct(company, as_of) -> list[tuple]:
	"""The (account_type, finance_book) groups of the company's Payment Ledger, read now and never from the memo."""
	return [tuple(row) for row in frappe.db.sql(_GROUPS, (company, getdate(as_of)))]


def view_settings():
	"""The reader switch: read once per view (shared with the memo and the bank one-pass check), else fresh."""
	memo = _active_memo()
	return memo.settings() if memo else _read_settings()


@contextmanager
def book_memo():
	"""Open the view-scoped memo; it is removed on exit, also after an exception."""
	previous = getattr(frappe.local, _MEMO, None)
	setattr(frappe.local, _MEMO, _BookMemo())
	try:
		yield
	finally:
		setattr(frappe.local, _MEMO, previous)


class _BookMemo:
	"""Switch decision and Payment Ledger book groups of one view."""

	def __init__(self):
		self._settings = None
		self._groups = {}

	def settings(self):
		if self._settings is None:
			self._settings = _read_settings()
		return self._settings

	def serves(self, company) -> bool:
		"""True when the source is v16, the switch is `on` and the company is served."""
		try:
			if source_profile().major != "16":
				return False
		except CalendarSourceUnavailable:
			return False
		settings = self.settings()
		return settings.mode == "on" and company in settings.served

	def groups(self, company, as_of) -> list[tuple]:
		key = (company, getdate(as_of))
		if key not in self._groups:
			self._groups[key] = ple_book_groups_direct(*key)
		return self._groups[key]


def _read_settings():
	# Imported here: `routing` imports the reader, which imports this module.
	from jarvis.payment_calendar import routing

	return routing.reader_settings(lists_when_off=False)


def _active_memo() -> "_BookMemo | None":
	return getattr(frappe.local, _MEMO, None)


def _same_type_key(account_type) -> str:
	return (account_type or "").rstrip().casefold()


def _refuse_other_books(company, books, default):
	allowed = allowed_books(company, default)
	if any(book not in allowed for book in books):
		raise CalendarSourceUnavailable("multibook_unsupported")


def _validate_books(doctype, company, as_of, filters):
	default = require_company(company).default_finance_book
	books = frappe.get_all(
		doctype,
		filters={"company": company, "posting_date": ["<=", getdate(as_of)], **filters},
		pluck="finance_book",
		distinct=True,
		limit_page_length=0,
	)
	_refuse_other_books(company, books, default)
