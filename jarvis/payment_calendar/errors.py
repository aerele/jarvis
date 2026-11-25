import re

import frappe
from rq.timeouts import JobTimeoutException

# A job deadline must reach the job runner untouched: broad `except` blocks re-raise these first.
TIMEOUTS = (TimeoutError, JobTimeoutException)

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
# Why the built-in reader declines a read so the caller can run today's read instead.
READER_FALLBACK_REASONS = frozenset(
	{
		"ineligible",
		"eligibility_unknown",
		"unsupported_major",
		"pin_drift",
		"sales_person_scope",
		"control_total",
		"reader_error",
		"rekey_unresolved",
		"too_many_lines",
		# The reader ran out of its own time or its estimated time before the permission filter.
		"deadline",
		"assemble_budget",
	}
)
# The refusal a caller that cannot fall back gives for a reason; other reasons have none. Kept as the one
# written mapping (a test pins it), so such a caller does not invent its own.
_FALLBACK_REFUSALS = {
	"too_many_lines": "too_large",
	"deadline": "too_large",
	"assemble_budget": "too_large",
	"control_total": "reconciliation_mismatch",
	"pin_drift": "source_contract_unavailable",
	"unsupported_major": "source_contract_unavailable",
}
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


class ReaderFallback(Exception):
	"""The built-in reader declines this read; today's read must serve it.

	Not a `CalendarSourceUnavailable`: a fallback is never a refusal, so it can never be mistaken for one.
	The message is fixed and carries no data. `detail` is a tuple of fixed strings that say more about the
	reason (drifted pin names, eligibility reason codes), never a data value such as a party or a voucher.
	"""

	def __init__(self, reason: str, detail: tuple[str, ...] = ()):
		if reason not in READER_FALLBACK_REASONS:
			raise ValueError("Unknown reader fallback reason.")
		if not isinstance(detail, tuple) or not all(isinstance(item, str) for item in detail):
			raise ValueError("A reader fallback detail is a tuple of fixed strings.")
		self.reason = reason
		self.detail = detail
		super().__init__("The built-in reader cannot serve this read.")

	@property
	def refusal_code(self) -> str | None:
		"""The refusal code a stage with no fallback path would give, or None."""
		return _FALLBACK_REFUSALS.get(self.reason)


class SnapshotExpired(frappe.DoesNotExistError):
	"""An id that is unknown, expired, another user's, or another job kind's."""


class ScopeChanged(frappe.PermissionError):
	"""A stored proof no longer holds. `kind` says which check refused, for the refusal log."""

	code = "scope_changed"

	def __init__(self, message="Refresh the Payment Calendar snapshot.", kind="scope"):
		super().__init__(message)
		self.kind = kind
