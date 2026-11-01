"""Human-only APIs and validation for Cycle-Count Planner observations."""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _

from jarvis.chat import cycle_count_review
from jarvis.chat.collaboration import require_legacy_conversation
from jarvis.chat.purchase_match import digest
from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_user

APPROVAL = "Jarvis Approval Request"
COUNT = "Jarvis Physical Count"
AGENT = cycle_count_review.AGENT
MAX_ENTRIES = 40
_ENTRY_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def _parse_json(value, label: str):
	if isinstance(value, str):
		try:
			value = frappe.parse_json(value)
		except (TypeError, ValueError):
			frappe.throw(_("{0} must be valid JSON.").format(label))
	return value


def _parse_evidence(raw) -> dict:
	try:
		evidence = frappe.parse_json(raw or "{}")
	except (TypeError, ValueError):
		evidence = {}
	if not isinstance(evidence, dict):
		frappe.throw(_("The approved count plan has invalid sealed evidence."))
	return evidence


def _approval_context(request: str, *, lock: bool = False) -> tuple[object, dict]:
	ar = frappe.get_doc(APPROVAL, request, for_update=lock)
	require_legacy_conversation(ar.conversation)
	evidence = _parse_evidence(ar.assessment_evidence)
	finding = evidence.get("finding")
	proposal = finding.get("proposal") if isinstance(finding, dict) else None
	if (
		ar.source != "Agent Review"
		or ar.agent != AGENT
		or ar.result_class != "derived_candidate"
		or ar.ref_doctype != "Warehouse"
		or not isinstance(proposal, dict)
		or proposal.get("outcome") != "plan"
		or not isinstance(proposal.get("lines"), list)
		or finding.get("ref_name") != ar.ref_name
		or not isinstance(evidence.get("scope"), dict)
		or not evidence["scope"].get("company")
		or not evidence.get("reviewer")
		or not evidence.get("run")
		or not evidence.get("input_digest")
		or evidence.get("input_digest") != ar.get("assessment_input_digest")
	):
		frappe.throw(_("This request is not a valid Cycle-Count Planner plan."), frappe.PermissionError)
	return ar, evidence


def _may_view(ar, evidence: dict) -> bool:
	user = frappe.session.user
	if frappe.db.get_value("Jarvis Conversation", ar.conversation, "owner") == user:
		return True
	if evidence.get("reviewer") == user:
		return True
	return bool(
		frappe.db.exists(
			"DocShare",
			{"share_doctype": APPROVAL, "share_name": ar.name, "user": user, "read": 1},
		)
	)


def _may_correct(doc, evidence: dict) -> bool:
	return bool(doc.recorded_by == frappe.session.user or evidence.get("reviewer") == frappe.session.user)


def _require_visible(ar, evidence: dict) -> None:
	if not _may_view(ar, evidence):
		frappe.throw(_("You are not allowed to view this count plan."), frappe.PermissionError)


def _expected_lines(ar, evidence: dict) -> dict[str, dict]:
	lines = evidence["finding"]["proposal"]["lines"]
	if not lines or not isinstance(lines, list):
		frappe.throw(_("The approved plan has no countable lines."))
	result: dict[str, dict] = {}
	for line in lines:
		if not isinstance(line, dict):
			frappe.throw(_("The approved plan has invalid line data."))
		item_code = line.get("item_code")
		stock_uom = line.get("stock_uom")
		if (
			not isinstance(item_code, str)
			or not item_code
			or item_code in result
			or not isinstance(stock_uom, str)
			or not stock_uom
		):
			frappe.throw(_("The approved plan has invalid line data."))
		result[item_code] = line
	return result


def _require_native_read(doctype: str, name: str):
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	return doc


def _book_qty(warehouse: str, item_code: str, *, required: bool) -> str | None:
	if not frappe.has_permission("Bin", "read"):
		if required:
			frappe.throw(_("You need read access to current stock before recording a count."), frappe.PermissionError)
		return None
	rows = frappe.get_list(
		"Bin",
		filters={"warehouse": warehouse, "item_code": item_code},
		fields=["actual_qty"],
		limit=2,
	)
	if len(rows) > 1:
		frappe.throw(_("This item has duplicate current-stock rows and cannot be counted safely."))
	if not rows:
		if required:
			frappe.throw(_("The current stock row is no longer readable. Refresh the count plan."), frappe.PermissionError)
		return None
	return str(rows[0]["actual_qty"])


def _decimal_text(value) -> str:
	if not isinstance(value, str) or not value or value != value.strip() or not _DECIMAL.fullmatch(value):
		frappe.throw(_("Counted quantity must be a non-negative decimal text. Empty is not zero."))
	try:
		decimal = Decimal(value)
	except InvalidOperation:
		frappe.throw(_("Counted quantity must be a finite decimal."))
	if not decimal.is_finite() or decimal < 0:
		frappe.throw(_("Counted quantity must be a non-negative finite decimal."))
	# Canonical text makes an idempotent retry independent of harmless leading
	# zeroes or decimal padding, while preserving zero as a real observation.
	return format(decimal.normalize(), "f") if decimal else "0"


def _counted_at(value) -> datetime:
	if not isinstance(value, str) or not value.strip() or len(value) > 32:
		frappe.throw(_("Physical count time is required."))
	try:
		parsed = frappe.utils.get_datetime(value)
	except Exception:
		frappe.throw(_("Physical count time must be a valid site date and time."))
	if parsed > frappe.utils.now_datetime():
		frappe.throw(_("Physical count time cannot be in the future."))
	return parsed


def _require_entry_rights(doc, ar, evidence: dict, *, action: str) -> None:
	if action == "cancel" or doc.get("amended_from"):
		if not _may_correct(doc, evidence):
			frappe.throw(_("Only the recorder or the named reviewer can correct this physical count."), frappe.PermissionError)
		permission = "cancel" if action == "cancel" else "amend"
	else:
		_require_visible(ar, evidence)
		permission = "create" if doc.is_new() or doc.docstatus == 0 else "submit"
	if not frappe.has_permission(COUNT, permission):
		frappe.throw(_("You need Stock User permission to record this physical count."), frappe.PermissionError)


def _lock_sibling_reviews(input_digest: str) -> list[str]:
	"""Lock every same-input review in one order before writing a count."""
	names = frappe.db.get_all(
		APPROVAL,
		filters={"agent": AGENT, "assessment_input_digest": input_digest},
		pluck="name",
		order_by="name",
	)
	for name in names:
		frappe.db.get_value(APPROVAL, name, "name", for_update=True)
	return names


def _assert_no_pending_sibling(ar, evidence: dict) -> None:
	pending = frappe.db.get_all(
		APPROVAL,
		filters={
			"agent": AGENT,
			"assessment_input_digest": evidence["input_digest"],
			"status": "Pending",
		},
		pluck="name",
	)
	if pending:
		frappe.throw(
			_("Another warehouse review from this plan is still pending. Decide every plan review before recording a count."),
			frappe.ValidationError,
		)


def _active_counted_items(approval_request: str, *, lock: bool = False) -> dict[str, str]:
	"""Return submitted facts, with a current read when a writer holds the plan lock."""
	if lock:
		headers_table = frappe.qb.DocType(COUNT)
		headers = (
			frappe.qb.from_(headers_table)
			.select(headers_table.name)
			.where((headers_table.approval_request == approval_request) & (headers_table.docstatus == 1))
			.for_update()
			.run(as_dict=True)
		)
	else:
		headers = frappe.db.get_all(
			COUNT,
			filters={"approval_request": approval_request, "docstatus": 1},
			fields=["name"],
		)
	if not headers:
		return {}
	parents = [row.name for row in headers]
	if lock:
		items_table = frappe.qb.DocType("Jarvis Physical Count Item")
		items = (
			frappe.qb.from_(items_table)
			.select(items_table.parent, items_table.item_code)
			.where(items_table.parent.isin(parents))
			.for_update()
			.run(as_dict=True)
		)
	else:
		items = frappe.db.get_all(
			"Jarvis Physical Count Item",
			filters={"parent": ["in", parents]},
			fields=["parent", "item_code"],
		)
	return {row.item_code: row.parent for row in items}


def _ensure_line_access(doc, expected: dict[str, dict]) -> None:
	warehouse = _require_native_read("Warehouse", doc.warehouse)
	if warehouse.company != doc.company or warehouse.is_group or warehouse.disabled:
		frappe.throw(_("The plan warehouse is not an enabled leaf warehouse in its company."))
	seen = set()
	if not 1 <= len(doc.items or []) <= MAX_ENTRIES:
		frappe.throw(_("Record 1 to {0} planned items at a time.").format(MAX_ENTRIES))
	for row in doc.items:
		item_code = row.item_code
		if item_code in seen or item_code not in expected:
			frappe.throw(_("Every counted item must appear once in the approved plan."))
		seen.add(item_code)
		item = _require_native_read("Item", item_code)
		if not cycle_count_review.is_countable_item(item):
			frappe.throw(_("A planned item is no longer available for a physical count."))
		if item.stock_uom != expected[item_code]["stock_uom"]:
			frappe.throw(_("A planned item's stock unit changed. Refresh the plan before recording a count."))
		row.stock_uom = item.stock_uom
		quantity = _decimal_text(row.counted_qty)
		if frappe.get_cached_value("UOM", item.stock_uom, "must_be_whole_number") and Decimal(quantity) % 1:
			frappe.throw(_("{0} uses a whole-number stock unit.").format(item_code))
		row.counted_qty = quantity
		# This record is deliberately total-only for batch/serial items. The flag
		# prevents a reader from mistaking it for normal ERPNext lot detail.
		row.batch_detail_recorded = 0
		_book_qty(doc.warehouse, item_code, required=True)


def _immutable_payload(doc) -> dict:
	return {
		"approval_request": doc.approval_request,
		"company": doc.company,
		"warehouse": doc.warehouse,
		"counted_at": str(frappe.utils.get_datetime(doc.counted_at)),
		"items": sorted(
			[
				{"item_code": row.item_code, "counted_qty": row.counted_qty, "stock_uom": row.stock_uom}
				for row in doc.items
			],
			key=lambda row: row["item_code"],
		),
	}


def validate_count_document(doc, *, action: str = "record") -> None:
	"""Apply the count-entry contract on endpoint, Desk, REST, and ORM paths."""
	refuse_in_tool_dispatch()
	ar, evidence = _approval_context(doc.approval_request)
	if ar.status != "Approved":
		frappe.throw(_("Only an approved count plan can receive a physical count."), frappe.PermissionError)
	_require_entry_rights(doc, ar, evidence, action=action)
	if action != "cancel":
		doc.company = evidence["scope"]["company"]
		doc.warehouse = ar.ref_name
		doc.source_run = evidence["run"]
		counted_at = _counted_at(doc.counted_at)
		if counted_at < frappe.utils.get_datetime(ar.decided_at):
			frappe.throw(_("Physical count time cannot be before the plan was approved."))
		_ensure_line_access(doc, _expected_lines(ar, evidence))
		if not doc.entry_key:
			doc.entry_key = frappe.generate_hash(length=32)
		if not isinstance(doc.entry_key, str) or not _ENTRY_KEY.fullmatch(doc.entry_key):
			frappe.throw(_("Physical count entry key is invalid."))
		doc.entry_digest = digest(_immutable_payload(doc))
		if doc.docstatus == 1:
			_lock_sibling_reviews(evidence["input_digest"])
			_assert_no_pending_sibling(ar, evidence)
			already_counted = _active_counted_items(ar.name, lock=True)
			if any(row.item_code in already_counted for row in doc.items):
				frappe.throw(_("A planned item already has a submitted physical-count fact."))


def stamp_submitted_count(doc) -> None:
	"""Stamp author and time only at the submit boundary."""
	validate_count_document(doc)
	if doc.recorded_by and doc.recorded_by != frappe.session.user:
		frappe.throw(_("Recorded By is set by the server at submit time."), frappe.PermissionError)
	doc.recorded_by = frappe.session.user
	doc.recorded_at = frappe.utils.now_datetime()
	doc.entry_digest = digest(_immutable_payload(doc))


def _parse_entries(value: str | list[dict]) -> list[dict]:
	entries = _parse_json(value, "Counted items")
	if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES:
		frappe.throw(_("Record 1 to {0} planned items at a time.").format(MAX_ENTRIES))
	parsed = []
	seen = set()
	for entry in entries:
		if not isinstance(entry, dict) or set(entry) != {"item_code", "counted_qty"}:
			frappe.throw(_("Each counted item must contain only item_code and counted_qty."))
		item_code = entry.get("item_code")
		if not isinstance(item_code, str) or not item_code or item_code != item_code.strip() or item_code in seen:
			frappe.throw(_("Counted item codes must be unique."))
		seen.add(item_code)
		parsed.append({"item_code": item_code, "counted_qty": _decimal_text(entry.get("counted_qty"))})
	return parsed


def _existing_entry(entry_key: str, *, lock: bool = False):
	name = frappe.db.get_value(COUNT, {"entry_key": entry_key}, "name", for_update=lock)
	return frappe.get_doc(COUNT, name) if name else None


def _same_entry(doc, approval_request: str, counted_at: str, entries: list[dict]) -> bool:
	if doc.approval_request != approval_request:
		return False
	try:
		when = str(frappe.utils.get_datetime(counted_at))
	except Exception:
		return False
	if str(frappe.utils.get_datetime(doc.counted_at)) != when:
		return False
	return sorted(
		[{"item_code": row.item_code, "counted_qty": row.counted_qty} for row in doc.items],
		key=lambda row: row["item_code"],
	) == sorted(entries, key=lambda row: row["item_code"])


def _count_summary(doc, *, idempotent: bool) -> dict:
	return {
		"name": doc.name,
		"entry_key": doc.entry_key,
		"item_codes": [row.item_code for row in doc.items],
		"idempotent": int(idempotent),
	}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def list_count_plans(start: int = 0, page_length: int = 20) -> dict:
	"""List only approved plans visible to the present human user."""
	refuse_in_tool_dispatch()
	try:
		start = max(0, int(start))
		page_length = max(1, min(int(page_length), 100))
	except (TypeError, ValueError):
		frappe.throw(_("Invalid page request."))
	rows = frappe.db.get_all(
		APPROVAL,
		filters={"agent": AGENT, "source": "Agent Review", "status": "Approved"},
		fields=["name"],
		order_by="decided_at desc, name desc",
		limit=500,
	)
	visible = []
	for row in rows:
		try:
			ar, evidence = _approval_context(row.name)
			if not _may_view(ar, evidence):
				continue
			planned = _expected_lines(ar, evidence)
			visible.append(
				{
					"request": ar.name,
					"title": ar.title,
					"company": evidence["scope"]["company"],
					"warehouse": ar.ref_name,
					"line_count": len(planned),
					"decided_at": str(ar.decided_at or ""),
					"can_record": int(_can_record(ar, evidence)),
				}
			)
		except frappe.PermissionError:
			continue
	page = visible[start : start + page_length]
	return {
		"rows": page,
		"total": len(visible),
		"has_more": int(start + page_length < len(visible)),
		"start": start,
		"page_length": page_length,
	}


def _can_record(ar, evidence: dict) -> bool:
	try:
		if ar.status != "Approved" or not _may_view(ar, evidence):
			return False
		if not frappe.has_permission(COUNT, "create") or not frappe.has_permission(COUNT, "submit"):
			return False
		warehouse = _require_native_read("Warehouse", ar.ref_name)
		if warehouse.company != evidence["scope"]["company"] or warehouse.is_group or warehouse.disabled:
			return False
		for item_code in _expected_lines(ar, evidence):
			_require_native_read("Item", item_code)
			_book_qty(ar.ref_name, item_code, required=True)
		return True
	except (frappe.PermissionError, frappe.DoesNotExistError, frappe.ValidationError):
		return False


def can_record_count_plan(approval_request) -> bool:
	"""Return whether this human may open the count-entry flow for a plan.

	This is a display hint only. ``record_counts`` repeats every authorization and
	state check inside its database transaction.
	"""
	try:
		ar, evidence = _approval_context(approval_request.name)
		return bool(ar.status == "Approved" and _can_record(ar, evidence))
	except (frappe.PermissionError, frappe.DoesNotExistError, frappe.ValidationError):
		return False


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def get_count_plan(request: str, entry_key: str | None = None) -> dict:
	"""Return an approved plan and current human-entry state, not source evidence."""
	refuse_in_tool_dispatch()
	ar, evidence = _approval_context(request)
	if ar.status != "Approved":
		frappe.throw(_("This count plan is no longer approved."), frappe.PermissionError)
	_require_visible(ar, evidence)
	can_record = _can_record(ar, evidence)
	active = _active_counted_items(ar.name) if frappe.has_permission(COUNT, "read") else {}
	entry = _existing_entry(entry_key) if entry_key and _ENTRY_KEY.fullmatch(entry_key) else None
	if entry and entry.approval_request != ar.name:
		entry = None
	lines = []
	for item_code, line in _expected_lines(ar, evidence).items():
		book_qty = _book_qty(ar.ref_name, item_code, required=False)
		lines.append(
			{
				"item_code": item_code,
				"stock_uom": line["stock_uom"],
				"capture_required": int(bool(line.get("capture_required"))),
				"current_book_qty": book_qty,
				"count_name": active.get(item_code),
				"saved_in_entry": int(bool(entry and any(row.item_code == item_code for row in entry.items))),
			}
		)
	return {
		"request": ar.name,
		"title": ar.title,
		"status": ar.status,
		"company": evidence["scope"]["company"],
		"warehouse": ar.ref_name,
		"approved_at": str(ar.decided_at or ""),
		"can_record": int(can_record),
		"entry": _count_summary(entry, idempotent=True) if entry else None,
		"lines": lines,
	}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def record_counts(request: str, counted_at: str, entries: str | list[dict], entry_key: str) -> dict:
	"""Atomically submit one human count session with idempotent retry protection."""
	refuse_in_tool_dispatch()
	if not isinstance(entry_key, str) or not _ENTRY_KEY.fullmatch(entry_key):
		frappe.throw(_("Use a valid count-entry key."))
	parsed_entries = _parse_entries(entries)
	_counted_at(counted_at)
	ar, evidence = _approval_context(request)
	if ar.status != "Approved":
		frappe.throw(_("Only an approved count plan can receive a physical count."), frappe.PermissionError)
	_require_visible(ar, evidence)
	if not _can_record(ar, evidence):
		frappe.throw(_("You do not have the required stock access to record this count."), frappe.PermissionError)

	# Lock every sibling in canonical order. A count in one warehouse otherwise
	# changes the shared source digest while another review stays Pending.
	_lock_sibling_reviews(evidence["input_digest"])
	ar, evidence = _approval_context(request, lock=True)
	_assert_no_pending_sibling(ar, evidence)
	existing = _existing_entry(entry_key, lock=True)
	if existing:
		if _same_entry(existing, ar.name, counted_at, parsed_entries):
			return {"request": ar.name, "saved": [_count_summary(existing, idempotent=True)]}
		frappe.throw(_("This count-entry key was already used with different data."), frappe.ValidationError)
	active = _active_counted_items(ar.name, lock=True)
	if any(entry["item_code"] in active for entry in parsed_entries):
		frappe.throw(_("One or more planned items already have a submitted physical-count fact."))
	doc = frappe.get_doc(
		{
			"doctype": COUNT,
			"approval_request": ar.name,
			"counted_at": counted_at,
			"entry_key": entry_key,
			"items": parsed_entries,
		}
	)
	try:
		doc.insert()
		doc.submit()
	except frappe.DuplicateEntryError:
		existing = _existing_entry(entry_key, lock=True)
		if existing and _same_entry(existing, ar.name, counted_at, parsed_entries):
			return {"request": ar.name, "saved": [_count_summary(existing, idempotent=True)]}
		raise
	return {"request": ar.name, "saved": [_count_summary(doc, idempotent=False)]}
