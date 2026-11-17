import re

import frappe

# The only failure codes a stored status, a reply or a log may carry; anything else becomes a default.
SAFE_CODES = frozenset(
	{
		"internal_error",
		"timeout",
		"source_changed",
		"scope_changed",
		"forbidden",
		"expired",
		"source_contract_unavailable",
		"source_configuration_unavailable",
		"fiscal_year_unavailable",
		"multibook_unsupported",
		"too_large",
		"invalid_source_amount",
		"reconciliation_mismatch",
		"currency_reconciliation_unavailable",
		"settlement_schedule_mismatch",
		"multiple_invoice_accounts",
		"masked_source_unsupported",
		"restricted_obligations",
		"job_lost",
		"worker_unavailable",
		"worker_busy",
	}
)
# A KeyError names its missing key in a log only when it looks like one of our own fixed key names.
_FIXED_KEY = re.compile(r"[a-z_.]+")


def safe_code(exc: Exception, default: str | None = None) -> str | None:
	"""The fixed `code` of an error, or `default` when it has none or an unknown one."""
	code = getattr(exc, "code", None)
	return code if isinstance(code, str) and code in SAFE_CODES else default


def safe_detail(exc: Exception) -> str | None:
	"""What a failure log may say about an error: fixed key names or a fixed code, never other text.

	A key that does not look like a fixed name (it may come from data: a party, a voucher) hides the whole list.
	"""
	if isinstance(exc, KeyError):
		keys = exc.args
		if keys and all(isinstance(key, str) and _FIXED_KEY.fullmatch(key) for key in keys):
			return ("missing key " + ", ".join(repr(key) for key in keys))[:200]
		return None
	return safe_code(exc)


class CalendarSourceUnavailable(frappe.ValidationError):
	def __init__(self, code="source_contract_unavailable"):
		self.code = code
		super().__init__("The accounting source cannot provide a complete Payment Calendar snapshot.")


class SnapshotExpired(frappe.DoesNotExistError):
	"""An id that is unknown, expired, another user's, or another job kind's."""


class ScopeChanged(frappe.PermissionError):
	"""A stored proof no longer holds. `kind` says which check refused, for the refusal log."""

	code = "scope_changed"

	def __init__(self, message="Refresh the Payment Calendar snapshot.", kind="scope"):
		super().__init__(message)
		self.kind = kind
