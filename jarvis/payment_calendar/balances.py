"""All-account balances job (B5): worker read, status-read revalidation and the flat status body.

The stored result holds only `{generated_at, anchor, currency, precision, accounts: [{account, balance}], total}`.
Names, types and bank labels are added at status-read time (D6) and never stored.
"""

from datetime import datetime, timezone
from decimal import Decimal

import frappe
from frappe.utils import nowdate

from jarvis.exceptions import PermissionDeniedError
from jarvis.payment_calendar import bank_labels, catalog, store
from jarvis.payment_calendar.access import ScopeChanged, refresh_actor_access
from jarvis.payment_calendar.banks import read_bank_balances
from jarvis.payment_calendar.errors import CalendarSourceUnavailable, SnapshotExpired
from jarvis.payment_calendar.permissions import require_company, require_report
from jarvis.payment_calendar.snapshot import consistent_read, fiscal_years_read
from jarvis.payment_calendar.sources import money

KIND = "balances"
REVOKED = (frappe.PermissionError, PermissionDeniedError, frappe.ValidationError, frappe.DoesNotExistError)


def balances_status_body(status: dict, result: dict | None) -> dict:
	"""The flat status body; the golden dataset and the API both build it here."""
	data = store.public_status(status, "job_id")
	if not result:
		return data
	breakdown = bank_labels.bank_breakdown(status["scope"]["company"], result)
	data.update(
		generated_at=result["generated_at"],
		currency=result["currency"],
		precision=result["precision"],
		accounts=breakdown["accounts"],
		total=result["total"],
	)
	return data


def load(job_id: str) -> dict:
	"""The caller's balances job. The kind is checked before any authorization or discard path."""
	status = store.load_status(frappe.session.user, job_id)
	if not status or status.get("kind") != KIND:
		raise SnapshotExpired()
	return status


def checked(status: dict) -> tuple[dict, dict | None]:
	"""The gate of every status read.

	A queued or running job runs the request gate only. A completed job runs it plus one chunked
	validation of the result's accounts, and is discarded when an account is no longer permitted.
	"""
	company, actor = status["scope"]["company"], status["scope"]["actor"]
	if status["state"] in ("queued", "running"):
		catalog.request_gate(company)
	if status["state"] != "complete":
		return status, None
	result = store.load_result(actor, status["snapshot_id"])
	if result is None:
		raise SnapshotExpired()
	if result.get("anchor") != nowdate():
		# The balances are as of another day. A balances job leaves no tombstone: the next read is `expired`.
		store.discard(actor, status["snapshot_id"])
		raise ScopeChanged("Request the account balances again.")
	try:
		catalog.request_gate(company)
		catalog.validate_in_chunks(company, [row["account"] for row in result["accounts"]])
	except CalendarSourceUnavailable:
		# Not a revocation: the request gate answers as it does for a fresh request, and the job stays.
		raise
	except REVOKED as exc:
		store.discard(actor, status["snapshot_id"])
		raise ScopeChanged("Request the account balances again.") from exc
	return status, result


def collect(scope: dict) -> dict:
	"""Total every account of the job's catalog inside one repeatable-read source view."""
	company, names = scope["company"], scope["accounts"]
	refresh_actor_access()
	catalog.permitted_accounts(company)
	with consistent_read() as hits:
		generated_at = datetime.now(timezone.utc).isoformat()
		currency, precision, rows = _read(company, names, scope["anchor"])
		if names and not fiscal_years_read(hits):
			raise CalendarSourceUnavailable("source_contract_unavailable")
	# The view has ended: a revocation committed during the read stops the publish.
	require_report(company, "Payable")
	catalog.validate_in_chunks(company, names)
	total = sum((money(row["balance"], precision) for row in rows), Decimal(0))
	return {
		"generated_at": generated_at,
		"anchor": scope["anchor"],
		"currency": currency,
		"precision": precision,
		"accounts": rows,
		"total": str(money(total, precision)),
	}


def _read(company, names, anchor):
	from erpnext.accounts.utils import get_currency_precision

	precision = get_currency_precision() or 2
	rows = []
	for chunk in catalog.chunks(names):
		part = read_bank_balances(company, chunk, anchor)
		if part["precision"] != precision:
			raise CalendarSourceUnavailable("source_contract_unavailable")
		rows += part["accounts"]
	return require_company(company).default_currency, precision, rows
