"""Human-only APIs and validation for Cycle-Count Planner observations."""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _

from jarvis._session import impersonate
from jarvis.chat import cycle_count_review
from jarvis.chat.collaboration import require_legacy_conversation
from jarvis.chat.purchase_match import digest
from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_user

APPROVAL = "Jarvis Approval Request"
COUNT = "Jarvis Physical Count"
AGENT = cycle_count_review.AGENT
MAX_ENTRIES = 40
MAX_LIST_CANDIDATES = 500
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
		or not isinstance(finding, dict)
		or not isinstance(proposal, dict)
		or proposal.get("outcome") != "plan"
		or not isinstance(proposal.get("lines"), list)
		or finding.get("ref_name") != ar.ref_name
		or not isinstance(evidence.get("scope"), dict)
		or not isinstance(evidence.get("policy"), dict)
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
	return evidence.get("reviewer") == user


def _may_correct(source, evidence: dict) -> bool:
	"""Return whether this human can correct a submitted count fact.

	A correction must use the original submitted fact as its authority.  A new
	amendment intentionally has no copied ``recorded_by`` value, so reading the
	new document here would let neither its recorder nor a later retry prove who
	recorded the original observation.
	"""
	return bool(source.recorded_by == frappe.session.user or evidence.get("reviewer") == frappe.session.user)


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
			frappe.throw(
				_("You need read access to current stock before recording a count."), frappe.PermissionError
			)
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
			frappe.throw(
				_("The current stock row is no longer readable. Refresh the count plan."),
				frappe.PermissionError,
			)
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
	if isinstance(value, datetime):
		parsed = value
	else:
		if not isinstance(value, str) or not value.strip() or len(value) > 32:
			frappe.throw(_("Physical count time is required."))
		try:
			parsed = frappe.utils.get_datetime(value)
		except Exception:
			frappe.throw(_("Physical count time must be a valid site date and time."))
	if not isinstance(parsed, datetime) or parsed.tzinfo is not None:
		frappe.throw(_("Physical count time must be a valid site date and time."))
	if parsed > frappe.utils.now_datetime():
		frappe.throw(_("Physical count time cannot be in the future."))
	return parsed


def _amendment_source(doc, ar, evidence: dict):
	"""Lock and validate the one cancelled fact that an amendment can replace."""
	name = doc.get("amended_from")
	if not isinstance(name, str) or not name or name == doc.get("name"):
		frappe.throw(_("Choose one cancelled physical-count fact to amend."), frappe.PermissionError)
	try:
		source = frappe.get_doc(COUNT, name, for_update=True)
	except frappe.DoesNotExistError:
		frappe.throw(_("The physical-count fact to amend no longer exists."), frappe.PermissionError)
	if (
		source.docstatus != 2
		or source.approval_request != ar.name
		or source.company != evidence["scope"]["company"]
		or source.warehouse != ar.ref_name
	):
		frappe.throw(
			_("The physical-count fact to amend is not the current approved-plan fact."),
			frappe.PermissionError,
		)
	# A cancelled fact is a one-way predecessor after a replacement has left
	# draft state. An abandoned Desk draft is not history and must not block a
	# later correction, but a submitted or cancelled successor keeps the chain
	# linear.
	successor = frappe.db.get_value(
		COUNT,
		{
			"amended_from": source.name,
			"docstatus": ["!=", 0],
			"name": ["!=", doc.get("name") or ""],
		},
		"name",
		for_update=True,
	)
	if successor:
		frappe.throw(_("This physical-count fact already has a newer correction."), frappe.ValidationError)
	return source


def _cancel_source(doc):
	"""Return the locked, stored fact for a Desk, REST, or ORM cancellation."""
	before = doc.get_doc_before_save()
	if not before or before.name != doc.name or before.doctype != COUNT or before.docstatus != 1:
		frappe.throw(_("The physical-count fact to cancel is no longer current."), frappe.PermissionError)
	return before


def _require_entry_rights(doc, ar, evidence: dict, *, action: str):
	"""Check the human actor and return an amendment predecessor when present."""
	amendment_source = None
	if action == "cancel":
		correction_source = _cancel_source(doc)
		permission = "cancel"
	elif doc.get("amended_from"):
		amendment_source = _amendment_source(doc, ar, evidence)
		correction_source = amendment_source
		permission = "amend"
	else:
		correction_source = None
		permission = "create" if doc.is_new() or doc.docstatus == 0 else "submit"
	if correction_source:
		if not _may_correct(correction_source, evidence):
			frappe.throw(
				_("Only the recorder or the named reviewer can correct this physical count."),
				frappe.PermissionError,
			)
	if not correction_source:
		_require_visible(ar, evidence)
	if not frappe.has_permission(COUNT, permission):
		frappe.throw(
			_("You need Stock User permission to record this physical count."), frappe.PermissionError
		)
	return amendment_source


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
			_(
				"Another warehouse review from this plan is still pending. Decide every plan review before recording a count."
			),
			frappe.ValidationError,
		)


def _active_counted_items(approval_request: str) -> dict[str, str]:
	"""Return submitted facts after the approved-plan writer gate is held.

	Do not add a physical-count ``FOR UPDATE`` lock here. Desk cancellation
	already acquires its fact lock before this module runs; taking one after the
	plan-review lock would invert that order and can deadlock a normal record
	against a Desk correction. Every mutation path holds the sibling review gate
	before it calls this function; display reads need no writer lock.
	"""
	headers = frappe.db.get_all(
		COUNT,
		filters={"approval_request": approval_request, "docstatus": 1},
		fields=["name"],
	)
	if not headers:
		return {}
	parents = [row.name for row in headers]
	items = frappe.db.get_all(
		"Jarvis Physical Count Item",
		filters={"parent": ["in", parents]},
		fields=["parent", "item_code"],
	)
	return {row.item_code: row.parent for row in items}


def _cancelled_counted_items(approval_request: str) -> set[str]:
	"""Return item codes retained in cancelled facts for one approved plan.

	A cancelled observation can only re-enter the history through its controlled
	amendment. Treating it as absent would let a plan viewer replace another
	person's fact with a fresh, unlinked observation after cancellation.
	"""
	headers = frappe.db.get_all(
		COUNT,
		filters={"approval_request": approval_request, "docstatus": 2},
		fields=["name"],
	)
	if not headers:
		return set()
	parents = [row.name for row in headers]
	items = frappe.db.get_all(
		"Jarvis Physical Count Item", filters={"parent": ["in", parents]}, fields=["item_code"]
	)
	return {row.item_code for row in items}


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
		"amended_from": doc.get("amended_from") or "",
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


def _assert_amendment_lines(doc, source) -> None:
	"""Keep a correction on exactly the cancelled fact's approved line set."""
	prior = {row.item_code for row in source.items}
	current = {row.item_code for row in doc.items}
	if not prior or current != prior:
		frappe.throw(
			_("A correction must keep exactly the same planned items as the cancelled physical-count fact."),
			frappe.ValidationError,
		)


def _sealed_cohort_requests(ar, evidence: dict) -> set[str] | None:
	"""Return the approved-plan cohort sealed by one run and input digest."""
	run = evidence.get("run")
	input_digest = evidence.get("input_digest")
	if not isinstance(run, str) or not run or not isinstance(input_digest, str) or not input_digest:
		return None
	names = frappe.db.get_all(
		APPROVAL,
		filters={
			"agent": AGENT,
			"run": run,
			"conversation": ar.conversation,
			"source": "Agent Review",
			"result_class": "derived_candidate",
			"ref_doctype": "Warehouse",
			"assessment_input_digest": input_digest,
		},
		pluck="name",
	)
	if ar.name not in names:
		return None
	return set(names)


def _source_digest_excluding_cohort_facts(current: dict, approval_requests: set[str]) -> str | None:
	"""Return a fresh source digest without observations made for one sealed cohort.

	A staged run can create one approved plan per warehouse. The cohort's own
	submitted observations are part of the planner history source, but none of
	them existed when that one run was sealed. They must not make a later sibling
	observation or correction appear stale. Facts from another run remain in the
	comparison. Malformed collector output has no safe projection and therefore
	fails closed.
	"""
	if (
		not isinstance(current, dict)
		or current.get("complete") is not True
		or not approval_requests
		or any(not isinstance(name, str) or not name for name in approval_requests)
	):
		return None
	datasets = current.get("datasets")
	source_counts = current.get("source_counts")
	if not isinstance(datasets, dict) or not isinstance(source_counts, dict):
		return None
	counts = datasets.get("physical_counts")
	items = datasets.get("physical_count_items")
	count_total = source_counts.get(COUNT)
	item_total = source_counts.get("Jarvis Physical Count Item", 0)
	if (
		not isinstance(counts, list)
		or not isinstance(items, list)
		or type(count_total) is not int
		or type(item_total) is not int
		or count_total != len(counts)
		or item_total != len(items)
	):
		return None
	removed = set()
	count_names = set()
	kept_counts = []
	for row in counts:
		if not isinstance(row, dict):
			return None
		name = row.get("name")
		request = row.get("approval_request")
		if (
			not isinstance(name, str)
			or not name
			or name in count_names
			or not isinstance(request, str)
			or not request
		):
			return None
		count_names.add(name)
		if request in approval_requests:
			removed.add(name)
		else:
			kept_counts.append(row)
	kept_items = []
	parents_with_items = set()
	for row in items:
		if not isinstance(row, dict):
			return None
		parent = row.get("parent")
		if not isinstance(parent, str) or not parent or parent not in count_names:
			return None
		parents_with_items.add(parent)
		if parent not in removed:
			kept_items.append(row)
	if any(name not in parents_with_items for name in count_names):
		return None
	normalized_counts = {**source_counts, COUNT: len(kept_counts)}
	# The collector calls the child-table reader only when at least one header
	# exists. Restore that exact zero-header shape before hashing.
	if kept_counts:
		normalized_counts["Jarvis Physical Count Item"] = len(kept_items)
	else:
		normalized_counts.pop("Jarvis Physical Count Item", None)
	normalized = {key: value for key, value in current.items() if key != "source_digest"}
	normalized["datasets"] = {
		**datasets,
		"physical_counts": kept_counts,
		"physical_count_items": kept_items,
	}
	normalized["source_counts"] = normalized_counts
	return digest(normalized)


def _assert_current_evidence(ar, evidence: dict) -> None:
	"""Refuse a new observation when its sealed source changed after approval.

	The sealed reviewer supplied the approval evidence.  Re-read with that same
	identity so an owner cannot turn a later permission loss into an apparent
	no-change result. This guard applies to every submitted observation. The
	comparison removes only submitted facts made for this sealed plan cohort. It
	still refuses every other source or permission change.
	"""
	scope = evidence.get("scope")
	policy = evidence.get("policy")
	reviewer = evidence.get("reviewer")
	input_digest = evidence.get("input_digest")
	if (
		not isinstance(scope, dict)
		or not isinstance(policy, dict)
		or not isinstance(reviewer, str)
		or not reviewer
		or not isinstance(input_digest, str)
		or not input_digest
	):
		frappe.throw(_("The approved count plan has incomplete sealed evidence."), frappe.PermissionError)
	try:
		cohort_requests = _sealed_cohort_requests(ar, evidence)
		if cohort_requests is None:
			raise frappe.PermissionError
		if not frappe.utils.cint(frappe.db.get_value("User", reviewer, "enabled")):
			raise frappe.PermissionError
		with impersonate(reviewer):
			current = cycle_count_review.collect(scope, policy)
	except Exception:
		frappe.throw(
			_(
				"This count plan is stale or no longer fully readable. Re-run the assessment before recording."
			),
			frappe.ValidationError,
		)
	if _source_digest_excluding_cohort_facts(current, cohort_requests) != input_digest:
		frappe.throw(
			_(
				"This count plan is stale or no longer fully readable. Re-run the assessment before recording."
			),
			frappe.ValidationError,
		)


def validate_count_document(doc, *, action: str = "record") -> None:
	"""Apply the count-entry contract on endpoint, Desk, REST, and ORM paths."""
	refuse_in_tool_dispatch()
	ar, evidence = _approval_context(doc.approval_request)
	if ar.status != "Approved":
		frappe.throw(_("Only an approved count plan can receive a physical count."), frappe.PermissionError)
	amendment_source = _require_entry_rights(doc, ar, evidence, action=action)
	if action == "cancel":
		# Cancellation changes the same history dataset as a new observation.  Do
		# not silently invalidate another warehouse's still-pending review.
		_lock_sibling_reviews(evidence["input_digest"])
		_assert_no_pending_sibling(ar, evidence)
	if action != "cancel":
		doc.company = evidence["scope"]["company"]
		doc.warehouse = ar.ref_name
		doc.source_run = evidence["run"]
		counted_at = _counted_at(doc.counted_at)
		if counted_at < frappe.utils.get_datetime(ar.decided_at):
			frappe.throw(_("Physical count time cannot be before the plan was approved."))
		_ensure_line_access(doc, _expected_lines(ar, evidence))
		if amendment_source:
			_assert_amendment_lines(doc, amendment_source)
		if not doc.entry_key:
			doc.entry_key = frappe.generate_hash(length=32)
		if not isinstance(doc.entry_key, str) or not _ENTRY_KEY.fullmatch(doc.entry_key):
			frappe.throw(_("Physical count entry key is invalid."))
		doc.entry_digest = digest(_immutable_payload(doc))
		if doc.docstatus == 1:
			_lock_sibling_reviews(evidence["input_digest"])
			_assert_no_pending_sibling(ar, evidence)
			# Re-read under the sealed reviewer on every submit path, including an
			# amendment. The comparison excludes only this plan's own submitted
			# facts and fails for every other source change.
			_assert_current_evidence(ar, evidence)
			already_counted = _active_counted_items(ar.name)
			if any(row.item_code in already_counted for row in doc.items):
				frappe.throw(_("A planned item already has a submitted physical-count fact."))
			if not amendment_source and any(
				row.item_code in _cancelled_counted_items(ar.name) for row in doc.items
			):
				frappe.throw(
					_("A cancelled physical-count fact must be corrected through its controlled amendment."),
					frappe.PermissionError,
				)


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
		if (
			not isinstance(item_code, str)
			or not item_code
			or item_code != item_code.strip()
			or item_code in seen
		):
			frappe.throw(_("Counted item codes must be unique."))
		seen.add(item_code)
		parsed.append({"item_code": item_code, "counted_qty": _decimal_text(entry.get("counted_qty"))})
	return parsed


def _existing_entry(entry_key: str, *, lock: bool = False):
	name = frappe.db.get_value(COUNT, {"entry_key": entry_key}, "name", for_update=lock)
	return frappe.get_doc(COUNT, name) if name else None


def _same_entry(
	doc, approval_request: str, counted_at: str, entries: list[dict], *, amended_from: str = ""
) -> bool:
	if (
		doc.docstatus != 1
		or doc.approval_request != approval_request
		or (doc.get("amended_from") or "") != amended_from
	):
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


def _active_count_details(ar, evidence: dict, active: dict[str, str], *, can_record: bool) -> list[dict]:
	"""Project current facts needed by the human correction form only."""
	result = []
	for name in sorted(set(active.values())):
		try:
			doc = frappe.get_doc(COUNT, name)
		except frappe.DoesNotExistError:
			continue
		if doc.docstatus != 1 or doc.approval_request != ar.name:
			continue
		result.append(
			{
				"name": doc.name,
				"can_correct": int(
					can_record
					and _may_correct(doc, evidence)
					and frappe.has_permission(COUNT, "cancel")
					and frappe.has_permission(COUNT, "amend")
				),
				"items": [
					{"item_code": row.item_code, "counted_qty": str(row.counted_qty)} for row in doc.items
				],
			}
		)
	return result


def _new_count_document(
	approval_request: str,
	counted_at: str,
	entries: list[dict],
	entry_key: str,
	*,
	amended_from: str = "",
):
	values = {
		"doctype": COUNT,
		"approval_request": approval_request,
		"counted_at": counted_at,
		"entry_key": entry_key,
		"items": entries,
	}
	if amended_from:
		values["amended_from"] = amended_from
	doc = frappe.get_doc(values)
	# Direct Desk amendments must discard inherited server fields. This endpoint
	# owns a caller-supplied idempotency key, so mark only that controlled path for
	# the DocType hook to preserve it.
	if amended_from:
		doc.flags.cycle_count_controlled_entry = True
	return doc


def _rollback_entry_savepoint(name: str) -> None:
	"""Undo a locally failed count write without undoing the caller's locks."""
	frappe.db.rollback(save_point=name)


def _visible_plan_where() -> tuple[str, dict]:
	"""Return the sealed owner/reviewer predicate for the count-plan list.

	``frappe.db.get_all`` bypasses the Approval Request permission query, which
	previously let the newest 500 rows of other users crowd a person's older
	plans out of this list.  The sealed reviewer is not a general Approval
	Request reader, so this narrow list predicate is intentionally local to the
	count-entry flow.
	"""
	legacy = ""
	if frappe.db.has_column("Jarvis Conversation", "collaboration_active"):
		legacy = " AND COALESCE(c.collaboration_active, 0) = 0"
	where = (
		"""
		ar.agent = %(agent)s
		AND ar.source = 'Agent Review'
		AND ar.status = 'Approved'
		AND ar.result_class = 'derived_candidate'
		AND ar.ref_doctype = 'Warehouse'
		AND c.name IS NOT NULL
		AND JSON_VALID(COALESCE(ar.assessment_evidence, ''))
		AND (
			c.owner = %(user)s
			OR JSON_UNQUOTE(JSON_EXTRACT(ar.assessment_evidence, '$.reviewer')) = %(user)s
		)
	"""
		+ legacy
	)
	return where, {"agent": AGENT, "user": frappe.session.user}


def _visible_plan_count(where: str, params: dict) -> int:
	return int(
		frappe.db.sql(
			"SELECT COUNT(*) FROM `tabJarvis Approval Request` ar "
			"INNER JOIN `tabJarvis Conversation` c ON c.name = ar.conversation WHERE " + where,
			params,
		)[0][0]
	)


def _visible_plan_names(where: str, params: dict, start: int, limit: int) -> list[str]:
	rows = frappe.db.sql(
		"SELECT ar.name FROM `tabJarvis Approval Request` ar "
		"INNER JOIN `tabJarvis Conversation` c ON c.name = ar.conversation WHERE "
		+ where
		+ " ORDER BY ar.decided_at DESC, ar.name DESC LIMIT %(limit)s OFFSET %(start)s",
		{**params, "limit": limit, "start": start},
		as_dict=True,
	)
	return [row.name for row in rows]


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
	where, params = _visible_plan_where()
	total = _visible_plan_count(where, params)
	visible = []
	next_start = start
	inspected = 0
	# A damaged legacy approval must not make every later valid plan disappear.
	# Keep the scan bounded: the client receives an advancing raw cursor and can
	# continue safely if an unusual number of bad candidates remain.
	while len(visible) < page_length and next_start < total and inspected < MAX_LIST_CANDIDATES:
		limit = min(page_length, MAX_LIST_CANDIDATES - inspected)
		names = _visible_plan_names(where, params, next_start, limit)
		if not names:
			next_start = total
			break
		for name in names:
			if len(visible) >= page_length:
				break
			next_start += 1
			inspected += 1
			try:
				ar, evidence = _approval_context(name)
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
			except (
				frappe.DoesNotExistError,
				frappe.PermissionError,
				frappe.ValidationError,
				AttributeError,
				KeyError,
				TypeError,
				ValueError,
			):
				continue
	return {
		"rows": visible,
		"total": total,
		"has_more": int(next_start < total),
		"start": start,
		"next_start": next_start,
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
	if entry and (entry.approval_request != ar.name or entry.docstatus != 1):
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
		"active_counts": _active_count_details(ar, evidence, active, can_record=can_record),
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
		frappe.throw(
			_("You do not have the required stock access to record this count."), frappe.PermissionError
		)

	# Resolve a pre-existing idempotency key before the shared review gate. It can
	# name a fact that Desk has already locked for cancellation; taking that lock
	# before the review gate keeps the same fact-then-review order as Desk.
	existing = _existing_entry(entry_key, lock=True)
	if existing:
		if _same_entry(existing, ar.name, counted_at, parsed_entries):
			return {"request": ar.name, "saved": [_count_summary(existing, idempotent=True)]}
		frappe.throw(_("This count-entry key was already used with different data."), frappe.ValidationError)
	# Lock every sibling in canonical order. A count in one warehouse otherwise
	# changes the shared source digest while another review stays Pending.
	_lock_sibling_reviews(evidence["input_digest"])
	ar, evidence = _approval_context(request, lock=True)
	# A writer that already passed this shared gate may have created this key.
	# Do not acquire a fact lock after the gate: Desk cancellation holds its fact
	# lock before it waits on the gate.
	existing = _existing_entry(entry_key)
	if existing:
		if _same_entry(existing, ar.name, counted_at, parsed_entries):
			return {"request": ar.name, "saved": [_count_summary(existing, idempotent=True)]}
		frappe.throw(_("This count-entry key was already used with different data."), frappe.ValidationError)
	_assert_no_pending_sibling(ar, evidence)
	active = _active_counted_items(ar.name)
	if any(entry["item_code"] in active for entry in parsed_entries):
		frappe.throw(_("One or more planned items already have a submitted physical-count fact."))
	doc = _new_count_document(ar.name, counted_at, parsed_entries, entry_key)
	savepoint = "cycle_count_entry_" + frappe.generate_hash(length=12)
	frappe.db.savepoint(savepoint)
	try:
		doc.insert()
		doc.submit()
	except frappe.DuplicateEntryError:
		_rollback_entry_savepoint(savepoint)
		existing = _existing_entry(entry_key)
		if existing and _same_entry(existing, ar.name, counted_at, parsed_entries):
			return {"request": ar.name, "saved": [_count_summary(existing, idempotent=True)]}
		raise
	except Exception:
		_rollback_entry_savepoint(savepoint)
		raise
	return {"request": ar.name, "saved": [_count_summary(doc, idempotent=False)]}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def correct_counts(
	request: str,
	replaces: str,
	counted_at: str,
	entries: str | list[dict],
	entry_key: str,
) -> dict:
	"""Cancel one submitted fact and submit its one controlled correction.

	The original fact and its successor live in one transaction.  A lost client
	response can retry the same key, but a stale browser cannot fork an earlier
	correction chain or amend another plan's fact.
	"""
	refuse_in_tool_dispatch()
	if not isinstance(replaces, str) or not replaces:
		frappe.throw(_("Choose the submitted physical-count fact to correct."))
	if not isinstance(entry_key, str) or not _ENTRY_KEY.fullmatch(entry_key):
		frappe.throw(_("Use a valid count-entry key."))
	parsed_entries = _parse_entries(entries)
	_counted_at(counted_at)
	ar, evidence = _approval_context(request)
	if ar.status != "Approved":
		frappe.throw(_("Only an approved count plan can receive a physical count."), frappe.PermissionError)
	_require_visible(ar, evidence)
	if not _can_record(ar, evidence):
		frappe.throw(
			_("You do not have the required stock access to correct this count."), frappe.PermissionError
		)

	# Resolve an existing retry before the shared review gate. Its row can be a
	# fact currently locked by Desk cancellation, so this keeps fact locks before
	# the shared gate on every correction path.
	existing = _existing_entry(entry_key, lock=True)
	if existing:
		if _same_entry(existing, ar.name, counted_at, parsed_entries, amended_from=replaces):
			return {
				"request": ar.name,
				"replaced": replaces,
				"saved": [_count_summary(existing, idempotent=True)],
			}
		frappe.throw(_("This count-entry key was already used with different data."), frappe.ValidationError)
	try:
		source = frappe.get_doc(COUNT, replaces, for_update=True)
	except frappe.DoesNotExistError:
		frappe.throw(
			_("The submitted physical-count fact to correct no longer exists."), frappe.PermissionError
		)
	if source.approval_request != ar.name:
		frappe.throw(
			_("The submitted physical-count fact is not current for this plan."), frappe.PermissionError
		)
	# Native Desk cancellation acquires this fact lock before it enters the
	# controller. Keep the correction in that same fact-then-review order. Normal
	# record paths use the review gate without a later fact lock, so they cannot
	# invert this order.
	_lock_sibling_reviews(evidence["input_digest"])
	ar, evidence = _approval_context(request, lock=True)
	# A writer that already passed this shared gate may have created this key.
	# Do not acquire a fact lock after the gate: Desk cancellation holds its fact
	# lock before it waits on the gate.
	existing = _existing_entry(entry_key)
	if existing:
		if _same_entry(existing, ar.name, counted_at, parsed_entries, amended_from=replaces):
			return {
				"request": ar.name,
				"replaced": replaces,
				"saved": [_count_summary(existing, idempotent=True)],
			}
		frappe.throw(_("This count-entry key was already used with different data."), frappe.ValidationError)
	if source.approval_request != ar.name or source.docstatus != 1:
		frappe.throw(
			_("The submitted physical-count fact is not current for this plan."), frappe.PermissionError
		)
	_assert_no_pending_sibling(ar, evidence)

	savepoint = "cycle_count_correct_" + frappe.generate_hash(length=12)
	frappe.db.savepoint(savepoint)
	try:
		source.cancel()
		doc = _new_count_document(
			ar.name,
			counted_at,
			parsed_entries,
			entry_key,
			amended_from=source.name,
		)
		doc.insert()
		doc.submit()
	except frappe.DuplicateEntryError:
		_rollback_entry_savepoint(savepoint)
		existing = _existing_entry(entry_key)
		if existing and _same_entry(existing, ar.name, counted_at, parsed_entries, amended_from=replaces):
			return {
				"request": ar.name,
				"replaced": replaces,
				"saved": [_count_summary(existing, idempotent=True)],
			}
		raise
	except Exception:
		_rollback_entry_savepoint(savepoint)
		raise
	return {
		"request": ar.name,
		"replaced": source.name,
		"saved": [_count_summary(doc, idempotent=False)],
	}
