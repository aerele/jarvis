"""POST-only, permission-checked SPA API. Refusals contain fixed codes only."""

from __future__ import annotations

import json
from contextlib import contextmanager
from functools import wraps
from time import monotonic

import frappe

from jarvis.exceptions import PermissionDeniedError
from jarvis.payment_calendar import (
	access,
	balances,
	bank_labels,
	catalog,
	detail,
	jobs,
	preferences,
	read_log,
	store,
)
from jarvis.payment_calendar.access import authorize_scope, refresh_actor_access
from jarvis.payment_calendar.errors import (
	CalendarSourceUnavailable,
	ScopeChanged,
	SnapshotExpired,
	safe_code,
	safe_detail,
)
from jarvis.payment_calendar.permissions import require_floor_write
from jarvis.payment_calendar.projection import ProjectionError, upcoming_days
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.settings import get_settings, update_safety_floor

STATUS_RESULT_KEYS = store.STATUS_RESULT_KEYS
STATUS_SCOPE_KEYS = (
	"company",
	"accounts",
	"anchor",
	"selectable_start",
	"selectable_end",
	"window_start",
	"window_end",
)


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
				state, code = "unavailable", safe_code(exc, "source_contract_unavailable")
			elif isinstance(exc, frappe.TimestampMismatchError):
				state, code = "scope_changed", "settings_changed"
			elif isinstance(exc, frappe.ValidationError):
				state, code = "invalid", "invalid_request"
			else:
				state, code = "failed", "internal_error"
				_log_internal_error(fn.__name__, exc)
			frappe.local.message_log = []
			return {
				"ok": False,
				"state": state,
				"code": code,
				"retryable": state != "forbidden",
				"snapshot_id": None,
			}

	return call


def _log_internal_error(operation, exc):
	"""The operation and error classes, plus key names or a fixed code when the error has one; no other text.

	`cause_class` is the class of the error that was wrapped (a hook error inside the identifier check).
	One row per operation and exception class in 300 s.
	"""
	body = {"code": "internal_error", "operation": operation, "exception_class": type(exc).__name__}
	if exc.__cause__ is not None:
		body["cause_class"] = type(exc.__cause__).__name__
	detail = safe_detail(exc)
	if detail:
		body["detail"] = detail
	try:
		read_log.error_log_once(
			f"Payment Calendar API {operation}", json.dumps(body), operation, body["exception_class"]
		)
	except Exception:
		frappe.logger().warning("Payment Calendar API: safe_failure_unavailable")


def _load(snapshot_id, *, bank=False, endpoint="_load", side=None):
	"""The caller's snapshot row and, once complete, its small status part; never a side part.

	The stored scope is authorized once, without a result: the rows a reply returns are checked
	by the caller. `bank` runs the accounts gate, and only on a complete snapshot.
	"""
	actor = frappe.session.user
	status = store.load_status(actor, snapshot_id)
	if status is None:
		raise store.gone(actor, snapshot_id)
	# The kind comes before any discard: a balances id must never reach the discard path below.
	if status.get("kind", "snapshot") != "snapshot":
		raise SnapshotExpired()
	complete = status["state"] == "complete"
	part = _stored(endpoint, "status", store.load_part, actor, snapshot_id, "status") if complete else None
	try:
		_authorize(status, part, check_accounts=bank and complete)
	except (frappe.PermissionError, PermissionDeniedError) as exc:
		_discard(snapshot_id, exc, endpoint, side)
		raise
	except CalendarSourceUnavailable as exc:
		if exc.code == "restricted_obligations":
			read_log.refused(endpoint, "obligation", side)
		raise
	return status, part


def _stored(endpoint, name, load, actor, snapshot_id, *args):
	"""One stored part or hash read. A key that is gone while its status row lives is an eviction: log it."""
	try:
		return load(actor, snapshot_id, *args)
	except (SnapshotExpired, ScopeChanged):
		if store.load_status(actor, snapshot_id) is not None:
			read_log.part_missing(endpoint, name)
		raise


def _authorize(status, part, *, check_accounts):
	sides = [side for side in store.SIDES if side not in part["unavailable"]] if part is not None else []
	authorize_scope(
		status["scope"],
		expected_fingerprint=status["fingerprint"],
		check_accounts=check_accounts,
		actor_refreshed=True,
		sides=tuple(sides),
	)


def _complete(snapshot_id, *, bank=False, endpoint="_complete", side=None):
	status, part = _load(snapshot_id, bank=bank, endpoint=endpoint, side=side)
	if part is None:
		raise SnapshotExpired()
	return status, part


def _discard(snapshot_id, exc, endpoint, side):
	"""A refused snapshot is deleted and tombstoned, the refusal is logged and counted."""
	actor, kind = frappe.session.user, getattr(exc, "kind", "scope")
	store.discard(actor, snapshot_id)
	read_log.refused(endpoint, kind, side)
	read_log.count_discard(actor, snapshot_id, kind)


@contextmanager
def _verified(snapshot_id, endpoint, side=None):
	"""Wraps only the check of returned identifiers.

	A refusal discards the snapshot. An error of a hook or lookup is not a refusal: it keeps
	the snapshot and answers `internal_error`, also a ValidationError, which `_safe` would show as invalid.
	"""
	try:
		yield
	except ScopeChanged as exc:
		_discard(snapshot_id, exc, endpoint, side)
		raise
	except CalendarSourceUnavailable:
		raise
	except frappe.ValidationError as exc:
		raise RuntimeError("pcal_identifier_check") from exc


def _verify_top_proofs(snapshot_id, status, part):
	"""Check the stored proofs of every day's top invoice, both sides as present; returns the row count."""
	count = 0
	with _verified(snapshot_id, "get_snapshot_status"):
		for side, proofs in part["_top_proofs"].items():
			access.verify_returned_rows(status["scope"], side, part["_permission_columns"][side], proofs)
			count += len(proofs)
	return count


def snapshot_status_body(status, part):
	"""The status response body; `part` is the stored status part, which must hold every key."""
	data = store.public_status(status)
	if part is None:
		return data
	data.update({key: part[key] for key in STATUS_RESULT_KEYS})
	data["horizon"] = part["horizon"]
	data["bank"] = bank_labels.bank_breakdown(status["scope"]["company"], part["bank"])
	data["scope"] = {key: status["scope"][key] for key in STATUS_SCOPE_KEYS}
	if "month" in status["scope"]:
		data["scope"]["month"] = status["scope"]["month"]
	return data


def _flag(value):
	"""A boolean from a bool or its text form (a whitelisted call may bring either)."""
	text = value.strip().lower() if isinstance(value, str) else value
	if text in (True, 1, "1", "true"):
		return True
	if text in (False, 0, "0", "false"):
		return False
	raise frappe.ValidationError("Choose true or false.")


def _detail(endpoint, make_query, snapshot_id, side, parties, **request):
	"""A day or bucket reply: authorize, validate, load the side part and entry, check the page, render."""
	started = monotonic()
	status, part = _complete(snapshot_id, endpoint=endpoint, side=side)
	# A forbidden or unavailable side answers before the request is read; it never discards.
	detail.check_side(part, side)
	# Every invalid request answers before a side part is loaded.
	query = make_query(status, side=side, parties=_flag(parties), **request)
	actor, name = frappe.session.user, store.SIDE_PARTS[side]
	source = _stored(endpoint, name, store.load_part, actor, snapshot_id, name)
	entry = _stored(endpoint, f"index-{name}", store.load_index_entry, actor, snapshot_id, side, query.field)
	source["index"] = {} if entry is None else {query.field: entry}
	page = detail.page_of(query, {**part, "sources": {side: source}})
	rows = _rows_with_proofs(endpoint, snapshot_id, side, page.rows)
	with _verified(snapshot_id, endpoint, side):
		columns = part["_permission_columns"][side]
		access.verify_returned_rows(status["scope"], side, columns, rows, page.party_keys)
	read_log.slow(endpoint, started, len(page.rows), len(page.party_keys))
	return detail.render(page)


def _rows_with_proofs(endpoint, snapshot_id, side, rows):
	"""The page rows with their proofs: one HMGET of exactly their ids, none for an empty page."""
	if not rows:
		return rows
	ids = [row["id"] for row in rows]
	name = f"proofs-{store.SIDE_PARTS[side]}"
	proofs = _stored(endpoint, name, store.load_proofs, frappe.session.user, snapshot_id, side, ids)
	return store.with_proofs(rows, proofs)


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
		data["accounts"] = catalog.permitted_accounts(company)
		labels = bank_labels.bank_labels(company, [account.name for account in data["accounts"]])
		for account in data["accounts"]:
			account.update(labels[account.name])
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
def request_snapshot(company: str, accounts: list[str] | str | None = None, month: str | None = None) -> dict:
	# New clients request "current" explicitly; older clients retain their full window.
	month = frappe.utils.nowdate()[:7] if month == "current" else month
	return jobs.request_snapshot(normalize_scope(company, accounts, month=month))


@frappe.whitelist(methods=["POST"])
@_safe
def get_snapshot_status(snapshot_id: str) -> dict:
	started, endpoint = monotonic(), "get_snapshot_status"
	status, part = _load(snapshot_id, bank=True, endpoint=endpoint)
	if part is None:
		status = jobs.observe_job(status)
		if status["state"] == "complete":
			# Publication landed between the Redis reads above: serve it only through the complete path.
			status, part = _complete(snapshot_id, bank=True, endpoint=endpoint)
	proofs = _verify_top_proofs(snapshot_id, status, part) if part is not None else 0
	read_log.slow(endpoint, started, proofs, 0)
	return snapshot_status_body(status, part)


@frappe.whitelist(methods=["POST"])
@_safe
def request_account_balances(company: str) -> dict:
	return jobs.request_account_balances(company)


@frappe.whitelist(methods=["POST"])
@_safe
def get_account_balances_status(job_id: str) -> dict:
	status, result = balances.checked(balances.load(job_id))
	if result is None and status["state"] in ("queued", "running"):
		status = jobs.observe_job(status)
		if status["state"] == "complete":
			# Publication landed while the job looked queued: serve it only through the gate.
			status, result = balances.checked(status)
	return balances.balances_status_body(status, result)


@frappe.whitelist(methods=["POST"])
@_safe
def get_day_detail(
	snapshot_id: str,
	date: str,
	side: str = "Payable",
	page: int = 1,
	page_size: int = 25,
	party_page: int = 1,
	sort: str | None = None,
	order: str | None = None,
	parties: bool | str = True,
) -> dict:
	return _detail(
		"get_day_detail",
		detail.day_query,
		snapshot_id,
		side,
		parties,
		date=date,
		page=page,
		page_size=page_size,
		party_page=party_page,
		sort=sort,
		order=order,
	)


@frappe.whitelist(methods=["POST"])
@_safe
def get_upcoming(snapshot_id: str, date: str, page: int = 1, page_size: int = 30) -> dict:
	started, endpoint = monotonic(), "get_upcoming"
	status, part = _complete(snapshot_id, endpoint=endpoint, side="Payable")
	detail.check_side(part, "Payable")
	selected = detail.check_date(date, status["scope"])
	if selected > status["scope"]["selectable_end"]:
		raise frappe.ValidationError("Choose a date inside the selectable range.")
	reply = {
		**detail.page_rows(upcoming_days(part["projection"], selected), page, page_size),
		"generated_at": part["generated_at"],
	}
	read_log.slow(endpoint, started, 0, 0)
	return reply


@frappe.whitelist(methods=["POST"])
@_safe
def get_bucket_detail(
	snapshot_id: str,
	bucket: str,
	side: str = "Payable",
	page: int = 1,
	page_size: int = 25,
	party_page: int = 1,
	sort: str | None = None,
	order: str | None = None,
	parties: bool | str = True,
) -> dict:
	return _detail(
		"get_bucket_detail",
		detail.bucket_query,
		snapshot_id,
		side,
		parties,
		bucket=bucket,
		page=page,
		page_size=page_size,
		party_page=party_page,
		sort=sort,
		order=order,
	)
