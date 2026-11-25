"""Company-currency book cash, after the existing ledger permission gates."""

from decimal import Decimal
from typing import NamedTuple

import frappe
from frappe.utils import flt, getdate

from jarvis.payment_calendar import books, read_log
from jarvis.payment_calendar.books import validate_finance_books
from jarvis.payment_calendar.builtin_reader import MISSING_KEY as MISSING_INDEX_ERROR
from jarvis.payment_calendar.compatibility import (
	bank_balance_arguments,
	bank_one_pass_blocker,
	helper_source_hash,
	source_profile,
)
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.permissions import require_company
from jarvis.payment_calendar.sources import money
from jarvis.tools._ledger_access import assert_gl_balance_in_scope, assert_ledger_readable

# Accounts one bank read takes; `validate_bank_accounts` refuses more, and the shadow unit reads in chunks of it.
MAX_BANK_ACCOUNTS = 50


def validate_bank_accounts(company: str, accounts: list[str]) -> list[str]:
	"""Authorize selection without reading money; deduplicate database identities."""
	require_company(company)
	assert_ledger_readable()
	if (
		not isinstance(accounts, list)
		or not accounts
		or len(accounts) > MAX_BANK_ACCOUNTS
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


# The one-pass read: the body of `get_balance_on` (erpnext/accounts/utils.py, audited sources only, see
# `compatibility.bank_one_pass_blocker`) grouped by account and finance book instead of one call per account:
#   is_cancelled=0            line 225      posting_date <= date          line 229
#   gle.account = ..          line 285      company and the finance-book filter, lines 308-319
#   sum(round(debit, p)) - sum(round(credit, p)) with p = get_currency_precision()   lines 325-331
# The sum is selected as text, so Python adds the groups exactly. `IGNORE INDEX(company)` is only a plan hint
# (it avoids an intersect with the whole-company index); a site without that index gets MISSING_INDEX_ERROR
# and reads today's way.
_ONE_PASS_SQL = """
	SELECT account, finance_book,
		CAST(SUM(ROUND(debit, %(precision)s)) - SUM(ROUND(credit, %(precision)s)) AS CHAR)
	FROM `tabGL Entry` IGNORE INDEX (company)
	WHERE company = %(company)s AND account IN %(accounts)s AND is_cancelled = 0 AND posting_date <= %(as_of)s
	GROUP BY account, finance_book
"""

# The path of the last bank read of a served company, for the collect log line: "one_pass" or "today".
# It is set before any refusal can raise, and left unset when the switch is off or the company is not served.
BANK_PATH_KEY = "payment_calendar_bank_path"
# Why a served company's read took today's path: one of FALLBACK_CAUSES. Unset when the path is not "today".
BANK_FALLBACK_KEY = "payment_calendar_bank_fallback"
FALLBACK_CAUSES = ("unpinned", "v15", "missing_index", "account_label")
# Causes already logged in this process: each is one ERROR line, because it repeats until someone acts.
_logged_fallbacks: set[str] = set()


class BankReadInputs(NamedTuple):
	"""What a bank read needs besides the accounts: the checked fiscal year, and the two figures that rule rounding."""

	fiscal_year_ok: bool
	row_precision: int
	default_book: str | None

	@property
	def money_precision(self) -> int:
		"""The places the reply shows: the raw precision, or 2 when it is 0."""
		return self.row_precision or 2


def bank_read_inputs(company: str, as_of: str) -> BankReadInputs:
	"""The fiscal-year check, the raw currency precision and the default finance book of one bank read.

	Raises ``fiscal_year_unavailable`` when no fiscal year covers `as_of`. The default book is read the way
	`get_balance_on` reads it (the cached Company value), because the summed books must be the same ones.
	"""
	from erpnext.accounts.utils import FiscalYearError, get_currency_precision, get_fiscal_year

	try:
		get_fiscal_year(getdate(as_of), company=company, verbose=0)
	except FiscalYearError as exc:
		raise CalendarSourceUnavailable("fiscal_year_unavailable") from exc
	return BankReadInputs(
		True, get_currency_precision(), frappe.get_cached_value("Company", company, "default_finance_book")
	)


def read_bank_balances(company: str, accounts: list[str], as_of: str) -> dict:
	_set_bank_route(None, None)
	if not as_of:
		raise frappe.ValidationError("The accounting date is required.")
	source_profile()

	from erpnext.accounts.utils import get_balance_on

	path, cause = _bank_route(company, get_balance_on)
	_set_bank_route(path, cause)
	values = require_company(company)
	names = validate_bank_accounts(company, accounts)
	inputs = bank_read_inputs(company, as_of)
	amounts = None
	if path == "one_pass":
		amounts = one_pass_amounts(company, names, as_of, inputs.row_precision, inputs.default_book)
		if amounts is None:
			_set_local(BANK_PATH_KEY, "today")
	if amounts is None:
		amounts = today_amounts(company, names, as_of, get_balance_on)
	precision = inputs.money_precision
	balances = [{"account": name, "balance": str(money(amounts[name], precision))} for name in names]
	total = sum(money(row["balance"], precision) for row in balances)
	return {
		"total": str(money(total, precision)),
		"accounts": balances,
		"currency": values.default_currency,
		"precision": precision,
	}


def _bank_route(company: str, helper) -> tuple[str | None, str | None]:
	"""(path, cause): `one_pass` when the switch is `on`, the company is served and `get_balance_on` is audited.

	`today` with the cause when the company is served but the source blocks the one pass; (None, None) when
	the switch is off or the company is not served. In mode `off` the settings read is the only extra work,
	made once per view. It fails closed to `off`.
	"""
	settings = books.view_settings()
	if settings.mode != "on" or company not in settings.served:
		return None, None
	cause = bank_one_pass_blocker(helper)
	return ("today", cause) if cause else ("one_pass", None)


def _set_bank_route(path: str | None, cause: str | None) -> None:
	"""Record path and cause for the log line (None removes them)."""
	_set_local(BANK_PATH_KEY, path)
	_note_fallback(cause)


def _set_local(key: str, value: str | None) -> None:
	if value:
		setattr(frappe.local, key, value)
	elif hasattr(frappe.local, key):
		delattr(frappe.local, key)


def _log_fallback(cause: str) -> None:
	"""One ERROR line per cause and process, with no company or account (the read_log speed-fallback line)."""
	try:
		import erpnext
		from erpnext.accounts.utils import get_balance_on

		read_log.speed_fallback(
			f"bank_one_pass:{cause}", helper_source_hash(get_balance_on), erpnext.__version__
		)
	except Exception:
		pass


def today_amounts(company: str, names: list[str], as_of: str, get_balance_on) -> dict[str, float]:
	"""Today's read: the finance-book gate, then `get_balance_on` for each account."""
	validate_finance_books(company, names, as_of)
	# Reject alternative books before selecting the audited helper arguments that
	# include NULL, blank and company-default books on this source revision.
	book_arguments = bank_balance_arguments(get_balance_on)
	previous_form = frappe.local.form_dict
	try:
		# ERPNext falls back to request fields even for explicit None arguments.
		frappe.local.form_dict = frappe._dict()
		return {
			name: get_balance_on(
				account=name,
				date=getdate(as_of),
				company=company,
				in_account_currency=False,
				party_type=None,
				party=None,
				cost_center=None,
				ignore_account_permission=False,
				**book_arguments,
			)
			for name in names
		}
	finally:
		frappe.local.form_dict = previous_form


def one_pass_amounts(
	company: str, names: list[str], as_of: str, row_precision: int, default_book: str | None
) -> dict[str, float] | None:
	"""`get_balance_on` for every name from one GL query; the same values, the same refusals.

	A group whose book is not NULL, blank or the company default (read fresh, as today's gate reads it)
	refuses `multibook_unsupported` before any account permission check. The summed books are those of
	`default_book`, as in `get_balance_on`. An account with no row is 0.

	None, with the cause in `BANK_FALLBACK_KEY`, when the caller must read today's way: the database says
	the index of the plan hint does not exist, or a ledger label matches no selected account. Any other
	database error propagates.
	"""
	try:
		rows = frappe.db.sql(
			_ONE_PASS_SQL,
			{
				"precision": row_precision,
				"company": company,
				"accounts": tuple(names),
				"as_of": getdate(as_of),
			},
		)
	except Exception as exc:
		if exc.args and exc.args[0] == MISSING_INDEX_ERROR:
			_note_fallback("missing_index")
			return None
		raise
	gate = books.allowed_books(company, require_company(company).default_finance_book)
	counted = books.allowed_books(company, default_book)
	totals = dict.fromkeys(names, Decimal(0))
	for label, book, balance in rows:
		if book not in gate:
			raise CalendarSourceUnavailable("multibook_unsupported")
		account = _canonical_account(label, totals)
		if account is None:
			_note_fallback("account_label")
			return None
		if book in counted:
			totals[account] += Decimal(balance)
	books.validate_bank_ple_books(company, as_of)
	for name in names:
		frappe.get_doc("Account", name).check_permission("read")
	return {name: flt(total) for name, total in totals.items()}


def _canonical_account(label: str, totals: dict) -> str | None:
	"""The selected account a ledger label names: the ledger compares names without case, accents and spaces."""
	if label in totals:
		return label
	found = frappe.db.get_value("Account", label, "name")
	return found if found in totals else None


def _note_fallback(cause: str | None) -> None:
	"""Record the cause for the log line (None removes it); a cause new to this process logs one ERROR line."""
	_set_local(BANK_FALLBACK_KEY, cause)
	if cause and cause not in _logged_fallbacks:
		_logged_fallbacks.add(cause)
		_log_fallback(cause)
