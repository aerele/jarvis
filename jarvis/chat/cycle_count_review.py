"""Bounded evidence custody for the human Cycle-Count Planner.

The private bundle ranks a sealed input file. This module only validates the
scope, collects permission-bounded source rows, and verifies the returned plan.
It never creates a Stock Reconciliation or posts stock.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import frappe

from jarvis.chat.purchase_match import digest

AGENT = "cycle-count-planner-operator"
TOKEN = "ccp-plan-v1"
LABEL = "Physical count plan"
REVIEW_QUESTION = (
	"Approve this human physical-count plan, or reject it. Approval does not record a count, "
	"post stock, submit a reconciliation, or change inventory."
)
MAX_WAREHOUSES = 10
MAX_BIN_ROWS = 500
MAX_SOURCE_ROWS = 20_000
MAX_SECONDS = 25
MAX_OUTPUT_BYTES = 16 * 1024

# These are the only fields a person may save on an installation. ``plan_date``
# and ``reviewer`` are server-stamped at launch and never read from saved JSON.
USER_CONFIG_KEYS = (
	"company",
	"from_date",
	"to_date",
	"count_history_from",
	"warehouses",
	"line_limit",
	"recount_after_days",
	"basis",
	"review_scope",
	"policy_version",
)
LAUNCH_CONFIG_KEYS = (*USER_CONFIG_KEYS, "plan_date", "reviewer")
BIN_FIELDS = ("name", "item_code", "warehouse", "actual_qty", "stock_value", "valuation_rate")
ITEM_FIELDS = (
	"name",
	"is_stock_item",
	"disabled",
	"end_of_life",
	"stock_uom",
	"has_batch_no",
	"has_serial_no",
)
LEDGER_FIELDS = (
	"name",
	"item_code",
	"warehouse",
	"company",
	"posting_date",
	"posting_time",
	"actual_qty",
	"voucher_type",
	"voucher_no",
	"is_cancelled",
)
ENTRY_FIELDS = ("name", "company", "purpose", "docstatus")
COUNT_FIELDS = ("name", "company", "warehouse", "counted_at", "approval_request")
COUNT_ITEM_FIELDS = (
	"name",
	"parent",
	"item_code",
	"counted_qty",
	"stock_uom",
	"batch_detail_recorded",
)
OUTWARD_STOCK_ENTRY_PURPOSES = frozenset(
	{"Material Issue", "Material Transfer for Manufacture", "Manufacture", "Send to Subcontractor"}
)
OUTWARD_NON_ENTRY_VOUCHERS = frozenset({"Delivery Note"})
PROPOSAL_KEYS = frozenset(
	{"outcome", "reason_code", "lines_considered", "lines_planned", "lines_rolled", "due_total", "lines"}
)
LINE_KEYS = frozenset(
	{
		"item_code",
		"stock_uom",
		"abc",
		"velocity",
		"outward_qty",
		"actual_qty",
		"stock_value",
		"last_physical_count_on",
		"capture_required",
	}
)
FINDING_KEYS = frozenset(
	{
		"token",
		"ref_doctype",
		"ref_name",
		"amount",
		"severity",
		"result_class",
		"confidence",
		"match_basis",
		"false_positive_path",
		"note",
		"proposal",
	}
)


class EvidenceGap(Exception):
	def __init__(self, reason: str, detail: str):
		self.reason = reason
		self.detail = detail


def _site_date() -> date:
	return date.fromisoformat(frappe.utils.today())


def _date(value, field: str) -> date:
	if not isinstance(value, str):
		raise ValueError(field)
	parsed = date.fromisoformat(value)
	if parsed.isoformat() != value:
		raise ValueError(field)
	return parsed


def _warehouse(name: str, company: str):
	doc = frappe.get_doc("Warehouse", name)
	doc.check_permission("read")
	fields = ("name", "company", "is_group", "disabled")
	before = {field: doc.get(field) for field in fields}
	doc.apply_fieldlevel_read_permissions()
	if before != {field: doc.get(field) for field in fields}:
		raise frappe.PermissionError("Required warehouse fields are not readable.")
	if doc.company != company or doc.is_group or doc.disabled:
		frappe.throw("Choose an enabled leaf warehouse in the selected company.")
	return doc


def configuration_defaults(company: str | None = None) -> dict:
	today = _site_date()
	result = {
		"defaults": {
			"from_date": (today - timedelta(days=90)).isoformat(),
			"to_date": today.isoformat(),
			"count_history_from": (today - timedelta(days=366)).isoformat(),
			"line_limit": 30,
			"recount_after_days": 30,
			"basis": "current_records",
			"review_scope": "count_plan_only",
			"policy_version": "CYCLE-COUNT-v1",
		},
		"warehouse_options": [],
		"message": "",
		"site_date": today.isoformat(),
	}
	company = company or frappe.defaults.get_user_default("Company")
	if not company:
		result["message"] = "Select a company to load readable warehouses."
		return result
	try:
		frappe.get_doc("Company", company).check_permission("read")
	except (frappe.PermissionError, frappe.DoesNotExistError):
		result["message"] = "Select a readable company to load warehouses."
		return result
	result["defaults"]["company"] = company
	names = frappe.get_list(
		"Warehouse",
		filters={"company": company, "is_group": 0, "disabled": 0},
		pluck="name",
		order_by="name",
		limit=201,
	)
	result["warehouse_options"] = names[:200]
	if len(names) > 200:
		result["message"] = (
			"Only the first 200 readable warehouses are listed. Narrow the company scope before selecting a plan."
		)
	return result


def configuration_issues(raw) -> dict[str, str]:
	try:
		config = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (ValueError, TypeError):
		return {"config": "Enter a valid configuration object."}
	if not isinstance(config, dict):
		return {"config": "Enter a valid configuration object."}
	issues: dict[str, str] = {}
	if set(config) - set(USER_CONFIG_KEYS):
		issues["config"] = "Remove unsupported configuration fields."
	company = config.get("company")
	if not isinstance(company, str) or not company.strip():
		issues["company"] = "Select a company."
	else:
		try:
			frappe.get_doc("Company", company).check_permission("read")
		except (frappe.PermissionError, frappe.DoesNotExistError):
			issues["company"] = "Select a readable company."
	dates: dict[str, date] = {}
	for field in ("from_date", "to_date", "count_history_from"):
		try:
			dates[field] = _date(config.get(field), field)
		except (ValueError, TypeError):
			issues[field] = "Choose a valid date."
	if {"from_date", "to_date"} <= set(dates):
		if not 0 <= (dates["to_date"] - dates["from_date"]).days < 366:
			issues["to_date"] = "Choose a movement window of at most 366 calendar dates, in date order."
	if "to_date" in dates and dates["to_date"] > _site_date():
		issues["to_date"] = "Movement evidence cannot end after today on this site."
	if "count_history_from" in dates and dates["count_history_from"] > _site_date():
		issues["count_history_from"] = "Count history cannot begin after today on this site."
	names = config.get("warehouses")
	if (
		not isinstance(names, list)
		or not 1 <= len(names) <= MAX_WAREHOUSES
		or any(not isinstance(name, str) or not name or name != name.strip() for name in names)
		or len(set(names)) != len(names)
	):
		issues["warehouses"] = "Select 1 to 10 different warehouses."
	elif "company" not in issues:
		try:
			for name in names:
				_warehouse(name, company)
		except (frappe.PermissionError, frappe.DoesNotExistError, frappe.ValidationError):
			issues["warehouses"] = "Every warehouse must be readable, enabled, a leaf, and in this company."
	if type(config.get("line_limit")) is not int or not 1 <= config["line_limit"] <= 40:
		issues["line_limit"] = "Choose a whole count-list size from 1 to 40 for this run."
	if type(config.get("recount_after_days")) is not int or not 1 <= config["recount_after_days"] <= 365:
		issues["recount_after_days"] = "Choose a whole recount interval from 1 to 365 days."
	if config.get("basis") != "current_records":
		issues["basis"] = "Confirm Current records only."
	if config.get("review_scope") != "count_plan_only":
		issues["review_scope"] = "Confirm Count plan only; approval does not change stock."
	if (
		not isinstance(config.get("policy_version"), str)
		or not 1 <= len(config["policy_version"].strip()) <= 128
	):
		issues["policy_version"] = "Enter the count-plan policy reference (1 to 128 characters)."
	return issues


def configuration(inst) -> dict:
	issues = configuration_issues(inst.config or "{}")
	if issues:
		frappe.throw(list(issues.values()), title="Complete count-plan configuration", as_list=True)
	config = frappe.parse_json(inst.config)
	reviewer = (inst.reviewer or "").strip()
	if not reviewer or not frappe.db.get_value("User", reviewer, "enabled"):
		frappe.throw("Set an enabled reviewer before starting a physical-count plan.")
	return {
		**{key: sorted(config[key]) if key == "warehouses" else config[key] for key in USER_CONFIG_KEYS},
		"plan_date": frappe.utils.today(),
		"reviewer": reviewer,
	}


def scope_from_config(config: dict) -> dict:
	if not isinstance(config, dict) or set(config) != set(LAUNCH_CONFIG_KEYS):
		frappe.throw("This count-plan run has no valid launch-time policy.")
	return {
		key: config[key]
		for key in (
			"company",
			"from_date",
			"to_date",
			"count_history_from",
			"warehouses",
			"basis",
			"plan_date",
		)
	}


def _numbers(rows, fields: set[str]) -> list[dict]:
	return [
		{key: str(value) if key in fields and value is not None else value for key, value in row.items()}
		for row in rows
	]


def collect(scope: dict, config: dict) -> dict:
	"""Read a bounded, permission-filtered source snapshot.

	The collector never compares a permission-filtered page with a direct database
	count. A missing readable row fails the associated source rather than being
	presented as a zero or a complete organisation-wide view.
	"""
	snapshot = {
		"schema": "cycle-count-source-v2",
		"scope": scope,
		"config": config,
		"complete": False,
		"datasets": {},
		"source_counts": {},
	}
	started, total = time.monotonic(), 0

	def rows(doctype, fields, filters, *, limit=MAX_SOURCE_ROWS, parent=None, order_by="name"):
		nonlocal total
		if time.monotonic() - started > MAX_SECONDS:
			raise EvidenceGap(
				"run_truncated_watermark", "The evidence collection time limit was reached. Reduce the scope."
			)
		if not frappe.has_permission(doctype, "read", parent_doctype=parent):
			raise frappe.PermissionError
		options = {"parent_doctype": parent} if parent else {}
		values = frappe.get_list(
			doctype,
			fields=list(fields),
			filters=filters,
			order_by=order_by,
			limit=limit + 1,
			**options,
		)
		if time.monotonic() - started > MAX_SECONDS:
			raise EvidenceGap(
				"run_truncated_watermark", "The evidence collection time limit was reached. Reduce the scope."
			)
		if len(values) > limit or total + len(values) > MAX_SOURCE_ROWS:
			raise EvidenceGap(
				"run_truncated_watermark",
				"A source row limit was reached. Reduce the warehouse, date, or history scope.",
			)
		if any(set(row) != set(fields) for row in values):
			raise frappe.PermissionError("Complete source rows and fields are required.")
		total += len(values)
		snapshot["source_counts"][doctype] = snapshot["source_counts"].get(doctype, 0) + len(values)
		return json.loads(frappe.as_json(values))

	try:
		company = frappe.get_doc("Company", scope["company"])
		company.check_permission("read")
		currency = company.default_currency
		company.apply_fieldlevel_read_permissions()
		if not currency or company.default_currency != currency:
			raise frappe.PermissionError("Company currency must be readable.")
		snapshot["currency"] = currency
		for name in scope["warehouses"]:
			_warehouse(name, scope["company"])

		bins = []
		for name in scope["warehouses"]:
			bins.extend(rows("Bin", BIN_FIELDS, {"warehouse": name}, limit=MAX_BIN_ROWS))
		if len({(row["warehouse"], row["item_code"]) for row in bins}) != len(bins):
			raise EvidenceGap(
				"unsupported_customisation",
				"Duplicate item and warehouse book rows cannot support a complete count plan.",
			)
		items = sorted({row["item_code"] for row in bins})
		catalog = rows("Item", ITEM_FIELDS, {"name": ["in", items]}) if items else []
		if len(catalog) != len(items):
			raise EvidenceGap(
				"permission_slice",
				"Some in-scope stock items are not completely readable to the execution user.",
			)

		for doctype in ("Stock Ledger Entry", "Stock Entry", "Jarvis Physical Count"):
			if not frappe.has_permission(doctype, "read"):
				raise frappe.PermissionError
		ledger = rows(
			"Stock Ledger Entry",
			LEDGER_FIELDS,
			{
				"company": scope["company"],
				"warehouse": ["in", scope["warehouses"]],
				"posting_date": ["between", [scope["from_date"], scope["to_date"]]],
				"actual_qty": ["<", 0],
				"is_cancelled": 0,
			},
		)
		entry_names = sorted({row["voucher_no"] for row in ledger if row["voucher_type"] == "Stock Entry"})
		entries = (
			rows("Stock Entry", ENTRY_FIELDS, {"name": ["in", entry_names], "company": scope["company"]})
			if entry_names
			else []
		)
		if len(entries) != len(entry_names):
			raise EvidenceGap(
				"permission_slice", "Some in-scope stock-entry movement records are not completely readable."
			)
		entry_by_name = {row["name"]: row for row in entries}
		outward_ledger = []
		for row in ledger:
			if row["voucher_type"] == "Stock Entry":
				entry = entry_by_name.get(row["voucher_no"])
				if entry and entry["docstatus"] == 1 and entry["purpose"] in OUTWARD_STOCK_ENTRY_PURPOSES:
					outward_ledger.append(row)
			elif row["voucher_type"] in OUTWARD_NON_ENTRY_VOUCHERS:
				outward_ledger.append(row)

		counts = rows(
			"Jarvis Physical Count",
			COUNT_FIELDS,
			{
				"company": scope["company"],
				"warehouse": ["in", scope["warehouses"]],
				"docstatus": 1,
				"counted_at": [">=", scope["count_history_from"]],
			},
			order_by="counted_at asc, name asc",
		)
		count_items = []
		for header in counts:
			items_for_header = rows(
				"Jarvis Physical Count Item",
				COUNT_ITEM_FIELDS,
				{"parent": header["name"]},
				parent="Jarvis Physical Count",
			)
			if not items_for_header:
				raise EvidenceGap(
					"unsupported_customisation", "A submitted physical count has no readable item rows."
				)
			count_items.extend(items_for_header)

		snapshot["datasets"] = {
			"bins": _numbers(bins, {"actual_qty", "stock_value", "valuation_rate"}),
			"items": catalog,
			"outward_ledger": _numbers(outward_ledger, {"actual_qty"}),
			"stock_entries": entries,
			"physical_counts": _numbers(counts, set()),
			"physical_count_items": _numbers(count_items, {"counted_qty"}),
		}
		snapshot["complete"] = True
	except frappe.PermissionError:
		snapshot.update(
			datasets={},
			reason_code="permission_slice",
			detail=(
				"The execution user cannot read the complete warehouse, item, movement, and physical-count "
				"evidence for this scope. Check read access and run again."
			),
		)
	except (frappe.DoesNotExistError, frappe.ValidationError):
		snapshot.update(
			datasets={},
			reason_code="configuration_missing",
			detail="A selected warehouse or required source is no longer available. Check the configuration and run again.",
		)
	except EvidenceGap as exc:
		snapshot.update(datasets={}, reason_code=exc.reason, detail=exc.detail)
	return _seal(snapshot)


def _seal(snapshot: dict) -> dict:
	snapshot["source_digest"] = digest(snapshot)
	return snapshot


def get_inputs(run):
	from jarvis.chat import operator_review

	return operator_review.get_inputs(run, operator_review.backend(AGENT))


def _unavailable(reason, detail):
	return {
		TOKEN: {
			"state": "not_evaluable",
			"reason_code": reason or "record_coverage_insufficient",
			"detail": detail or "Complete warehouse evidence is required.",
		}
	}


def envelope_failure(detail: str) -> dict:
	"""Return typed coverage when the private assessment did not read its file."""
	return _unavailable("record_coverage_insufficient", detail)


def validate_envelope(run, review_ready: bool, truncated: bool, rows_consumed) -> str | None:
	"""Check that a complete private file was evaluated in full.

	The private-input endpoint supplies one sealed snapshot, rather than a set of
	model-selected pages. A complete plan can therefore require the evaluator to
	report the exact source-row total. This check does not authenticate the
	evaluator; it prevents an accidental partial file read from staging a plan.
	"""
	if not review_ready:
		return None
	if truncated:
		return "The count-plan assessment reported truncated input. Re-run it with the complete private file."
	stored = frappe.parse_json(run.input_snapshot_json or "{}")
	counts = stored.get("source_counts") if isinstance(stored, dict) else None
	if not isinstance(counts, dict) or any(type(value) is not int or value < 0 for value in counts.values()):
		return "The captured count-plan source totals are invalid. Start a new run."
	if type(rows_consumed) is not int or rows_consumed != sum(counts.values()):
		return "The assessment did not read every captured count-plan source row. Re-run it with the complete private file."
	return None


def _number(value) -> Decimal:
	try:
		result = Decimal(str(value))
		if not result.is_finite():
			raise InvalidOperation
		return result
	except (InvalidOperation, ValueError, TypeError):
		frappe.throw("A quoted plan value is not a finite number.")


def is_countable_item(item) -> bool:
	"""Return whether an Item can still be included in a human count plan.

	ERPNext stores ``end_of_life`` as a future Date (often 2099-12-31), not a
	boolean. A truthiness check would reject every normal stock item.
	"""
	if not item or not item.get("is_stock_item") or item.get("disabled"):
		return False
	end_of_life = item.get("end_of_life")
	if not end_of_life:
		return True
	try:
		return frappe.utils.getdate(end_of_life) > _site_date()
	except Exception:
		return False


def _history_by_line(stored: dict) -> dict[tuple[str, str], str]:
	headers = {row["name"]: row for row in stored["datasets"]["physical_counts"]}
	history: dict[tuple[str, str], str] = {}
	for row in stored["datasets"]["physical_count_items"]:
		header = headers.get(row["parent"])
		if not header:
			frappe.throw("A captured physical-count item has no captured header.")
		key = (header["warehouse"], row["item_code"])
		counted_on = str(header["counted_at"])[:10]
		try:
			_date(counted_on, "counted_at")
		except ValueError:
			frappe.throw("A captured physical-count date is invalid.")
		history[key] = max(history.get(key, ""), counted_on)
	return history


def validate_output(run, findings, coverage, scope, integrity_digest):
	stored = frappe.parse_json(run.input_snapshot_json or "null")
	if not stored or scope != stored["scope"]:
		frappe.throw("Count-plan output must use this run's captured scope and evidence.")
	if integrity_digest != digest(
		{"input_digest": stored["source_digest"], "scope": scope, "findings": findings, "coverage": coverage}
	):
		frappe.throw("Count-plan output does not match its captured evidence and result digest.")
	if not isinstance(coverage, dict) or set(coverage) != {TOKEN}:
		frappe.throw("Count-plan coverage is missing or has unknown tokens.")
	if not stored["complete"]:
		return [], _unavailable(stored.get("reason_code"), stored.get("detail")), False
	if coverage[TOKEN].get("state") != "evaluated":
		if findings:
			frappe.throw("Incomplete count plans cannot stage proposals.")
		return [], coverage, False
	if (
		not isinstance(findings, list)
		or len(json.dumps(findings, ensure_ascii=True).encode()) > MAX_OUTPUT_BYTES
	):
		frappe.throw("The count plan is too large. Reduce its line limit or warehouse scope.")
	if len(findings) != len(scope["warehouses"]) or {row.get("ref_name") for row in findings} != set(
		scope["warehouses"]
	):
		frappe.throw("Return exactly one result for every configured warehouse.")
	bins = {(row["warehouse"], row["item_code"]): row for row in stored["datasets"]["bins"]}
	items = {row["name"]: row for row in stored["datasets"]["items"]}
	history = _history_by_line(stored)
	outward: dict[tuple[str, str], Decimal] = {}
	for row in stored["datasets"]["outward_ledger"]:
		key = (row["warehouse"], row["item_code"])
		outward[key] = outward.get(key, Decimal(0)) - _number(row["actual_qty"])
	try:
		cutoff = _date(stored["config"]["plan_date"], "plan_date") - timedelta(
			days=stored["config"]["recount_after_days"]
		)
	except (ValueError, TypeError, KeyError):
		frappe.throw("The captured count-plan cadence is invalid.")
	planned_total = 0
	seen = set()
	for finding in findings:
		if (
			not isinstance(finding, dict)
			or set(finding) != FINDING_KEYS
			or finding["token"] != TOKEN
			or finding["ref_doctype"] != "Warehouse"
			or finding["amount"] != 0
			or finding["result_class"] != "derived_candidate"
		):
			frappe.throw("Count plans must use the declared, non-financial proposal fields.")
		if (
			finding["severity"] not in {"blocker", "warning", "note"}
			or not isinstance(finding["confidence"], str)
			or not finding["confidence"].strip()
			or not isinstance(finding["match_basis"], str)
			or not finding["match_basis"].strip()
			or not isinstance(finding["false_positive_path"], str)
			or not finding["false_positive_path"].strip()
		):
			frappe.throw("Count plans must include complete human-review metadata.")
		if not isinstance(finding["note"], str) or any(
			term in finding["note"].lower() for term in ("variance", "shrinkage", "inventory adjustment")
		):
			frappe.throw("A count proposal cannot assert a variance or an inventory adjustment.")
		proposal = finding["proposal"]
		if not isinstance(proposal, dict) or set(proposal) != PROPOSAL_KEYS:
			frappe.throw("The count proposal has missing or unsupported fields.")
		if not isinstance(proposal["reason_code"], str) or not proposal["reason_code"].strip():
			frappe.throw("The count proposal needs a reason code.")
		warehouse = finding["ref_name"]
		population = sum(key[0] == warehouse for key in bins)
		if (
			any(
				type(proposal[key]) is not int or proposal[key] < 0
				for key in ("lines_considered", "lines_planned", "lines_rolled")
			)
			or proposal["lines_considered"] != population
			or not isinstance(proposal["lines"], list)
			or proposal["lines_planned"] != len(proposal["lines"])
		):
			frappe.throw("The count-list totals do not agree with the captured warehouse population.")
		if proposal["lines_planned"] + proposal["lines_rolled"] > population:
			frappe.throw("The count-list totals exceed the captured warehouse population.")
		outcome = proposal["outcome"]
		if outcome not in {"plan", "none_due", "ask", "deferred"} or (outcome == "plan") != bool(
			proposal["lines"]
		):
			frappe.throw("The proposal state and selected lines do not agree.")
		due = proposal["due_total"]
		if (
			(outcome == "ask" and due is not None)
			or (
				outcome != "ask"
				and (type(due) is not int or due != proposal["lines_planned"] + proposal["lines_rolled"])
			)
			or (outcome == "none_due" and due != 0)
			or (outcome == "deferred" and not due)
		):
			frappe.throw("The proposal's due and deferred totals do not agree.")
		for line in proposal["lines"]:
			if not isinstance(line, dict) or set(line) != LINE_KEYS:
				frappe.throw("The proposed line has missing or unsupported fields.")
			key = (warehouse, line["item_code"])
			if key not in bins or key in seen:
				frappe.throw("The proposal contains an unknown or duplicate stock line.")
			seen.add(key)
			item = items.get(line["item_code"])
			if not is_countable_item(item):
				frappe.throw("The proposal contains an unavailable stock item.")
			if line["stock_uom"] != item["stock_uom"]:
				frappe.throw("The proposed stock unit does not match the captured item.")
			if any(
				_number(line[field]) != _number(bins[key][field]) for field in ("actual_qty", "stock_value")
			) or _number(line["outward_qty"]) != outward.get(key, Decimal(0)):
				frappe.throw("The proposed values do not match the captured stock and movement evidence.")
			if line["last_physical_count_on"] != history.get(key):
				frappe.throw(
					"The proposed count history does not match the captured submitted physical counts."
				)
			if line["last_physical_count_on"]:
				try:
					if _date(line["last_physical_count_on"], "last_physical_count_on") > cutoff:
						frappe.throw(
							"The proposal includes an item that was counted within its configured cadence."
						)
				except ValueError:
					frappe.throw("The proposed physical-count date is invalid.")
			if type(line["capture_required"]) is not bool or line["capture_required"] != bool(
				item["has_batch_no"] or item["has_serial_no"]
			):
				frappe.throw("The proposal's batch or serial capture marker is wrong.")
			if line["abc"] not in {"A", "B", "C", ""} or line["velocity"] not in {
				"FAST",
				"MEDIUM",
				"SLOW",
				"",
			}:
				frappe.throw("The proposal has an unsupported classification label.")
		planned_total += proposal["lines_planned"]
	if planned_total > stored["config"]["line_limit"]:
		frappe.throw("The plan exceeds the configured total count-list size.")
	return findings, coverage, bool(planned_total)


def review_context(stored: dict, finding: dict) -> str:
	proposal = finding["proposal"]
	lines = [
		f"- {line['item_code']} ({line['stock_uom']}): {line['abc'] or 'unclassified'} / "
		f"{line['velocity'] or 'no movement bucket'}; current quantity {line['actual_qty']}; "
		f"current value {line['stock_value']}; last Jarvis physical count "
		f"{line['last_physical_count_on'] or 'not recorded since this feature was enabled'}"
		for line in proposal["lines"]
	]
	return "\n".join(
		[
			f"Warehouse: {finding['ref_name']}",
			f"Plan date: {stored['config']['plan_date']}",
			"Approval authorises a human count plan only. A later human count record does not post stock, "
			"submit a reconciliation, or adjust inventory.",
			*lines,
		]
	)


def stage_reviews(run, inst, findings):
	"""Only a plan with lines needs an approval request."""
	from jarvis.chat import operator_review

	planned = [row for row in findings if row["proposal"]["outcome"] == "plan"]
	return operator_review.stage_reviews(run, inst, planned, operator_review.backend(AGENT))


def check_review(doc, approve):
	from jarvis.chat import operator_review

	return operator_review.check_review(doc, approve, operator_review.backend(AGENT))
