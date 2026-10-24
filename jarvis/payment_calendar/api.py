"""POST-only, permission-checked SPA API. Refusals contain fixed codes only."""

from __future__ import annotations

import json
from datetime import date as Date
from decimal import Decimal
from functools import wraps
from urllib.parse import quote

import frappe

from jarvis.exceptions import PermissionDeniedError
from jarvis.payment_calendar import jobs, preferences, store
from jarvis.payment_calendar.access import ScopeChanged, authorize_scope, refresh_actor_access
from jarvis.payment_calendar.banks import validate_bank_accounts
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.permissions import require_company, require_floor_write, require_report
from jarvis.payment_calendar.projection import ProjectionError, upcoming_days
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.settings import get_settings, update_safety_floor


class SnapshotExpired(frappe.DoesNotExistError):
	pass


def _safe(fn):
	@wraps(fn)
	def call(*args, **kwargs):
		writing = fn.__name__ in ("set_preferences", "configure_floor")
		saved = False
		try:
			refresh_actor_access()
			if writing:
				frappe.db.savepoint("payment_calendar_api")
				saved = True
			return {"ok": True, "data": fn(*args, **kwargs)}
		except Exception as exc:
			if saved:
				try:
					frappe.db.rollback(save_point="payment_calendar_api")
				except Exception:
					frappe.db.rollback()
			if isinstance(exc, SnapshotExpired):
				state, code = "expired", "not_found_or_expired"
			elif isinstance(exc, ScopeChanged):
				state, code = "scope_changed", "scope_changed"
			elif isinstance(exc, store.ActorBusy):
				state, code = "unavailable", "worker_busy"
			elif isinstance(exc, (frappe.PermissionError, PermissionDeniedError, frappe.DoesNotExistError)):
				state, code = "forbidden", "forbidden"
			elif isinstance(exc, (CalendarSourceUnavailable, ProjectionError)):
				state = "unavailable"
				code = exc.code if exc.code in store.SAFE_CODES else "source_contract_unavailable"
			elif isinstance(exc, frappe.TimestampMismatchError):
				state, code = "scope_changed", "settings_changed"
			elif isinstance(exc, frappe.ValidationError):
				state, code = "invalid", "invalid_request"
			else:
				state, code = "failed", "internal_error"
				try:
					frappe.log_error(
						title="Payment Calendar API",
						message=json.dumps(
							{"code": code, "operation": fn.__name__, "exception_class": type(exc).__name__}
						),
					)
				except Exception:
					frappe.logger().warning("Payment Calendar API: safe_failure_unavailable")
			frappe.local.message_log = []
			return {
				"ok": False,
				"state": state,
				"code": code,
				"retryable": state != "forbidden",
				"snapshot_id": None,
			}

	return call


def _load(snapshot_id):
	actor = frappe.session.user
	status = store.load_status(actor, snapshot_id)
	if not status:
		raise SnapshotExpired()
	result = store.load_result(actor, snapshot_id)
	try:
		authorize_scope(status["scope"], expected_fingerprint=status["fingerprint"], result=result)
	except (frappe.PermissionError, PermissionDeniedError):
		store.discard(actor, snapshot_id)
		raise
	if status["state"] == "complete" and result is None:
		raise SnapshotExpired()
	return status, result


def _complete(snapshot_id):
	status, result = _load(snapshot_id)
	if not result:
		raise SnapshotExpired()
	return status, result


def _date(value, scope):
	try:
		if not isinstance(value, str) or len(value) != 10 or Date.fromisoformat(value).isoformat() != value:
			raise ValueError
	except ValueError as exc:
		raise frappe.ValidationError("Choose a calendar date.") from exc
	if not scope["window_start"] <= value <= scope["window_end"]:
		raise frappe.ValidationError("Choose a date in the snapshot window.")
	return value


def _page(value, size):
	def integer(number):
		if (
			isinstance(number, bool)
			or not isinstance(number, (int, str))
			or not str(number).isascii()
			or not str(number).isdigit()
		):
			raise frappe.ValidationError("Choose a page.")
		return int(number)

	value, size = integer(value), integer(size)
	if not 1 <= value <= 100000 or not 1 <= size <= 100:
		raise frappe.ValidationError("Choose a page size up to 100.")
	return value, size


def _slice(rows, page, size):
	page, size = _page(page, size)
	offset = (page - 1) * size
	return {
		"rows": rows[offset : offset + size],
		"total": len(rows),
		"page": page,
		"page_size": size,
		"has_more": offset + size < len(rows),
	}


def _source_rows(result, side):
	if side not in ("Payable", "Receivable"):
		raise frappe.ValidationError("Choose payables or receivables.")
	source = result["sources"].get(side)
	if source is None:
		code = result["unavailable"].get(side, "source_contract_unavailable")
		if code == "forbidden":
			raise frappe.PermissionError("Report access is unavailable.")
		raise CalendarSourceUnavailable(code)
	return source["rows"]


def _public_row(row):
	row = {key: value for key, value in row.items() if not key.startswith("_")}
	doctype = row["voucher_type"]
	row["source_link"] = (
		"/app/" + frappe.scrub(doctype).replace("_", "-") + "/" + quote(row["voucher_no"], safe="")
		if doctype in ("Purchase Invoice", "Sales Invoice", "Journal Entry", "Payment Entry")
		else None
	)
	return row


def _detail(rows, result, page, page_size, party_page):
	parties = {}
	for row in rows:
		key = (row["party_type"], row["party"])
		party = parties.setdefault(
			key,
			{
				"party_type": key[0],
				"party": key[1],
				"party_name": row["party_name"],
				"remaining": Decimal(0),
				"terms": 0,
				"invoices": set(),
			},
		)
		party["remaining"] += Decimal(row["remaining"])
		party["terms"] += 1
		party["invoices"].add((row["voucher_type"], row["voucher_no"]))
	party_rows = [
		{**party, "remaining": str(party["remaining"]), "invoices": len(party["invoices"])}
		for party in parties.values()
	]
	party_rows.sort(key=lambda r: (-Decimal(r["remaining"]), r["party_type"], r["party"]))
	return {
		**_slice([_public_row(row) for row in rows], page, page_size),
		"parties": _slice(party_rows, party_page, page_size)["rows"],
		"party_total": len(party_rows),
		"party_page": _page(party_page, page_size)[0],
		"generated_at": result["generated_at"],
	}


@frappe.whitelist(methods=["POST"])
@_safe
def get_capabilities(company: str | None = None) -> dict:
	companies = frappe.get_list(
		"Company", fields=["name", "default_currency"], order_by="name", limit_page_length=501
	)
	if len(companies) > 500:
		raise CalendarSourceUnavailable("too_large")
	data = {"companies": companies, "accounts": [], "settings": None, "can_configure": False}
	if company:
		require_company(company)
		require_report(company, "Payable")
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
			limit_page_length=501,
		)
		if len(accounts) > 500:
			raise CalendarSourceUnavailable("too_large")
		for account in accounts:
			try:
				validate_bank_accounts(company, [account.name])
				data["accounts"].append(account)
			except (frappe.PermissionError, PermissionDeniedError):
				continue
		data["settings"] = get_settings(company)
		try:
			require_floor_write(company)
			data["can_configure"] = True
		except frappe.PermissionError:
			frappe.local.message_log = []
	return data


@frappe.whitelist(methods=["POST"])
@_safe
def get_preferences() -> dict | None:
	return preferences.get_preferences()


@frappe.whitelist(methods=["POST"])
@_safe
def set_preferences(company: str, accounts: list[str] | str | None = None) -> dict:
	return preferences.save_preferences(company, accounts)


@frappe.whitelist(methods=["POST"])
@_safe
def configure_floor(
	company: str, value: str, expected_revision: int, configuration_id: str | None = None
) -> dict:
	require_floor_write(company)
	frappe.db.get_value("Company", company, "name", for_update=True)
	# A locking read sees the current row even if earlier authorization pinned
	# an older repeatable-read view before deletion/recreation of the floor.
	current_id = frappe.db.get_value(
		"Jarvis Payment Calendar Settings", {"company": company}, "name", for_update=True
	)
	if current_id != configuration_id:
		raise frappe.TimestampMismatchError("Settings changed.")
	return update_safety_floor(company, value, expected_revision)


@frappe.whitelist(methods=["POST"])
@_safe
def request_snapshot(company: str, accounts: list[str] | str | None = None) -> dict:
	return jobs.request_snapshot(normalize_scope(company, accounts))


@frappe.whitelist(methods=["POST"])
@_safe
def get_snapshot_status(snapshot_id: str) -> dict:
	status, result = _load(snapshot_id)
	if result is None:
		status = jobs.observe_job(status)
	data = jobs.public_status(status)
	if result:
		data.update(
			{
				key: result[key]
				for key in ("projection", "currency", "floor", "generated_at", "bank_code", "unavailable")
			}
		)
		data["scope"] = {
			key: status["scope"][key]
			for key in (
				"company",
				"accounts",
				"anchor",
				"selectable_start",
				"selectable_end",
				"window_start",
				"window_end",
			)
		}
	return data


@frappe.whitelist(methods=["POST"])
@_safe
def get_day_detail(
	snapshot_id: str,
	date: str,
	side: str = "Payable",
	page: int = 1,
	page_size: int = 25,
	party_page: int = 1,
) -> dict:
	status, result = _complete(snapshot_id)
	selected = _date(date, status["scope"])
	rows = [
		row
		for row in _source_rows(result, side)
		if row["due_date"] == selected
		and (
			Decimal(row["remaining"]) > 0
			or (
				Decimal(row["remaining"]) == 0
				and Decimal(row["original"]) > 0
				and row["kind"] == "obligation"
			)
		)
	]
	return _detail(rows, result, page, page_size, party_page)


@frappe.whitelist(methods=["POST"])
@_safe
def get_upcoming(snapshot_id: str, date: str, page: int = 1, page_size: int = 30) -> dict:
	status, result = _complete(snapshot_id)
	_source_rows(result, "Payable")
	selected = _date(date, status["scope"])
	if selected > status["scope"]["selectable_end"]:
		raise frappe.ValidationError("Choose a date inside the selectable range.")
	return {
		**_slice(upcoming_days(result["projection"], selected), page, page_size),
		"generated_at": result["generated_at"],
	}


@frappe.whitelist(methods=["POST"])
@_safe
def get_bucket_detail(
	snapshot_id: str,
	bucket: str,
	side: str = "Payable",
	page: int = 1,
	page_size: int = 25,
	party_page: int = 1,
) -> dict:
	status, result = _complete(snapshot_id)
	if bucket not in ("overdue", "undated", "adjustments", "outside_horizon"):
		raise frappe.ValidationError("Choose a reconciliation bucket.")
	scope = status["scope"]

	def belongs(row):
		amount, due = Decimal(row["remaining"]), row["due_date"]
		if amount < 0:
			return bucket == "adjustments"
		if amount <= 0:
			return False
		return bucket == (
			"undated"
			if due is None
			else "overdue"
			if due < scope["anchor"]
			else "outside_horizon"
			if due > scope["window_end"]
			else "current_future"
		)

	return _detail(
		[row for row in _source_rows(result, side) if belongs(row)], result, page, page_size, party_page
	)
