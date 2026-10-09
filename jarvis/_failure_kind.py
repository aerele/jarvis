"""Which kind of failure a write hit, so the assistant knows what to do next (R2-3).

- ``fixable``: the request itself was wrong (a missing required value, a value of the
  wrong type, a link to a record that does not exist, a duplicate name). The assistant
  may correct only the fields named in the failure and ask again with a NEW card, once.
  A read stopped by the statement time limit is fixable too: a narrower question runs.
- ``retry_later``: the site was busy (a deadlock, a lock wait that timed out, a lock
  held by another write). Nothing was saved; the user can try again in a moment.
- ``not_fixable``: everything else (insufficient stock, credit limit, a closed period,
  a permission, any business rule, any error this module does not recognise). No retry.

FIXABLE is an explicit allow-list by exception class, because Frappe raises most
business rules (credit limit, frozen accounts, the period-closing lock, a bad Select)
as a plain ``frappe.ValidationError``: classifying by the base class would let the
assistant "correct" a credit limit. Anything unknown is NOT_FIXABLE.

``kind_of(exc)`` runs where the exception is (``api._translate_write_error``), and the
kind rides the envelope as ``error.kind``. ``envelope_kind(result)`` reads it back at
settlement, where an envelope without ``kind``, with a code this module does not
recognise, or with a write-risk refusal code is NOT_FIXABLE, so a refusal added by a
later change can never be self-corrected by accident."""

from __future__ import annotations

import functools
import re

import frappe

from jarvis.exceptions import InvalidFieldValueError, RetryLaterError

FIXABLE = "fixable"
NOT_FIXABLE = "not_fixable"
RETRY_LATER = "retry_later"
KINDS = (FIXABLE, NOT_FIXABLE, RETRY_LATER)

# Frappe classes the request can fix (both v15 and v16 define every one of them,
# frappe/exceptions.py). DuplicateEntryError is NOT a ValidationError (it subclasses
# frappe's own NameError), so it is listed on its own.
FIXABLE_CLASSES: tuple[type[BaseException], ...] = (
	frappe.MandatoryError,
	frappe.LinkValidationError,
	frappe.exceptions.CancelledLinkError,
	frappe.DuplicateEntryError,
	frappe.UniqueValidationError,
	frappe.InvalidEmailAddressError,
	frappe.InvalidPhoneNumberError,
	frappe.InvalidNameError,
	frappe.CharacterLengthExceededError,
	frappe.exceptions.DataTooLongException,
	frappe.DoesNotExistError,
	# Raised by Jarvis before the write: the field type check and the bad-Date rule.
	InvalidFieldValueError,
)

RETRY_LATER_CLASSES: tuple[type[BaseException], ...] = (
	frappe.QueryDeadlockError,
	frappe.QueryTimeoutError,
	RetryLaterError,
)

# The codes a fixable / retry-later envelope can carry (``api._translate_write_error``
# maps every FIXABLE class to ``InvalidArgumentError``). A ``kind`` next to any other
# code is not trusted.
_CODES_BY_KIND = {
	FIXABLE: frozenset({"InvalidArgumentError", "QueryTooSlowError"}),
	RETRY_LATER: frozenset({"RetryLaterError"}),
}

# Write-risk refusals (R2-4 / R2-8, E3): a structure or sensitive write never
# self-corrects. The codes are matched by value so this holds before and after the
# write-risk guard lands. Once it does, its STRUCTURE_DOCTYPES / SENSITIVE_DOCTYPES
# (jarvis/tools/_write_risk.py) can also be consulted here by target doctype.
REFUSAL_CODES = frozenset({"structure_refused", "sensitive_refused", "write_refused"})

# MariaDB error 1292 (ER_TRUNCATED_WRONG_VALUE) on a DATE / DATETIME / TIME column. The
# column is quoted with backticks by MariaDB (`db`.`tabToDo`.`date`) and with single
# quotes by MySQL ('date'); pymysql (v15, and v16 with use_mysqlclient: 0) and
# mysqlclient (v16) carry the same ``(1292, message)`` args.
_BAD_DATE = re.compile(
	r"Incorrect (?P<type>date|datetime|time) value: '(?P<value>.*)' for column (?P<column>.+?) at row",
	re.IGNORECASE | re.DOTALL,
)


@functools.cache
def _erpnext_not_fixable() -> tuple[type[BaseException], ...]:
	"""ERPNext business-rule classes, resolved lazily (ERPNext may be absent). Listed so
	that a future ERPNext subclassing a FIXABLE class still reads NOT_FIXABLE."""
	found: list[type[BaseException]] = []
	for module, names in (
		("erpnext.stock.stock_ledger", ("NegativeStockError", "SerialNoExistsInFutureTransaction")),
		(
			"erpnext.accounts.doctype.accounting_period.accounting_period",
			("ClosedAccountingPeriod",),
		),
		("erpnext.accounts.doctype.budget.budget", ("BudgetError",)),
		("erpnext.exceptions", ("PartyFrozen", "PartyDisabled")),
		("erpnext.controllers.status_updater", ("OverAllowanceError",)),
	):
		try:
			mod = __import__(module, fromlist=list(names))
		except ImportError:
			continue
		found.extend(getattr(mod, n) for n in names if isinstance(getattr(mod, n, None), type))
	return tuple(found)


def bad_date(exc: BaseException) -> dict | None:
	"""``{type, value, doctype, fieldname}`` when ``exc`` is the database refusing a
	date / datetime / time value (error 1292), else None. Any other database error,
	including another 1292 (a bad number in a strict column), is not this."""
	args = getattr(exc, "args", None) or ()
	if len(args) < 2 or args[0] != 1292:
		return None
	m = _BAD_DATE.search(str(args[1]))
	if not m:
		return None
	parts = [p.strip("`'\" ") for p in m.group("column").split(".")]
	fieldname = parts[-1] if parts else ""
	table = parts[-2] if len(parts) >= 2 else ""
	doctype = table[3:] if table.lower().startswith("tab") else ""
	return {
		"type": m.group("type").lower(),
		"value": m.group("value"),
		"doctype": doctype,
		"fieldname": fieldname,
	}


def too_slow(exc: BaseException) -> bool:
	"""Whether ``exc`` is the database stopping a statement at its time limit
	(MariaDB error 1969), on the exception itself or the driver error it wraps."""
	for candidate in (exc, exc.__cause__):
		if candidate is not None and frappe.db.is_statement_timeout(candidate):
			return True
	return False


def kind_of(exc: BaseException) -> str:
	"""The failure kind of ``exc`` raised by a write."""
	if getattr(exc, "code", None) in REFUSAL_CODES:
		return NOT_FIXABLE
	if isinstance(exc, _erpnext_not_fixable()):
		return NOT_FIXABLE
	if isinstance(exc, RETRY_LATER_CLASSES):
		return RETRY_LATER
	if isinstance(exc, FIXABLE_CLASSES) or bad_date(exc) or too_slow(exc):
		return FIXABLE
	return NOT_FIXABLE


def envelope_kind(result) -> str:
	"""The failure kind a failed tool envelope carries, trusted only when its code is
	one this module knows for that kind. No ``error``, no ``kind``, an unknown code or
	a refusal: NOT_FIXABLE."""
	err = result.get("error") if isinstance(result, dict) else None
	if not isinstance(err, dict):
		return NOT_FIXABLE
	markers = {err.get("code"), err.get("reason_code"), result.get("reason_code")}
	if markers & REFUSAL_CODES:
		return NOT_FIXABLE
	kind = err.get("kind")
	if kind in _CODES_BY_KIND and err.get("code") in _CODES_BY_KIND[kind]:
		return kind
	return NOT_FIXABLE
