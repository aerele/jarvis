"""Server-side canonical policy for Cycle-Count Planner output.

The delegate bundle owns its private input file, but the server owns the
approval boundary.  This module recomputes the deterministic proposal from
the already sealed ``Jarvis Agent Run.input_snapshot_json`` before an approval
can be staged.  It intentionally has no Frappe import and performs no I/O.

Keep this policy byte-for-byte compatible in behaviour with the shipped
Cycle-Count bundle.  Tests exercise shared contract vectors against both
implementations; a caller-supplied digest is correlation data, never proof of
the policy result.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

TOKEN = "ccp-plan-v1"
_CONFIG_KEYS = frozenset(
	{
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
		"plan_date",
		"reviewer",
	}
)
_SCOPE_KEYS = frozenset(
	{"company", "from_date", "to_date", "count_history_from", "warehouses", "basis", "plan_date"}
)
_SOURCE_DATASETS = {
	"bins": "Bin",
	"items": "Item",
	"ledger": "Stock Ledger Entry",
	"stock_entries": "Stock Entry",
	"physical_counts": "Jarvis Physical Count",
	"physical_count_items": "Jarvis Physical Count Item",
}
_LEDGER_KEYS = frozenset(
	{
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
	}
)
_STOCK_ENTRY_KEYS = frozenset({"name", "company", "purpose", "docstatus"})

# These rules remain an implementation detail.  The server needs the same
# deterministic evaluator solely to refuse a model-altered proposal; no rule
# body is sent through a tool, the catalog, or the review UI.
_OUTWARD_VOUCHER_TYPES = frozenset({"Delivery Note", "Sales Invoice", "POS Invoice"})
_EXCLUDED_VOUCHER_TYPES = frozenset(
	{"Stock Reconciliation", "Purchase Receipt", "Purchase Invoice", "Purchase Return"}
)
_OUTWARD_STOCK_ENTRY_PURPOSES = frozenset({"Material Issue", "Manufacture"})
_EXCLUDED_STOCK_ENTRY_PURPOSES = frozenset(
	{
		"Material Receipt",
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Repack",
		"Send to Subcontractor",
		"Disassemble",
	}
)

_ABC_A_CUM = Decimal("0.70")
_ABC_B_CUM = Decimal("0.90")
_VEL_FAST_MIN = Decimal("100")
_VEL_MED_MIN = Decimal("20")
_CADENCE = {
	("A", "FAST"): 30,
	("A", "MEDIUM"): 45,
	("A", "SLOW"): 60,
	("B", "FAST"): 60,
	("B", "MEDIUM"): 90,
	("B", "SLOW"): 120,
	("C", "FAST"): 120,
	("C", "MEDIUM"): 180,
	("C", "SLOW"): 270,
}
_CADENCE_VALUE_ONLY = {"A": 45, "B": 90, "C": 180}
_CADENCE_VELOCITY_ONLY = {"FAST": 45, "MEDIUM": 90, "SLOW": 180}


class PolicyError(ValueError):
	"""The sealed source cannot support a canonical count plan."""


def digest(value) -> str:
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
	).hexdigest()


def _number(value) -> Decimal:
	if value is None or isinstance(value, bool):
		raise PolicyError("A numeric source value is missing.")
	try:
		result = Decimal(str(value))
	except InvalidOperation as exc:
		raise PolicyError("A numeric source value is invalid.") from exc
	if not result.is_finite() or abs(result) > Decimal("1e18"):
		raise PolicyError("A numeric source value is out of range.")
	return result


def _number_text(value) -> str:
	return format(_number(value), "f")


def _date(value, field: str) -> date:
	if not isinstance(value, str):
		raise PolicyError(f"{field} needs an ISO date.")
	try:
		result = date.fromisoformat(value)
	except ValueError as exc:
		raise PolicyError(f"{field} needs an ISO date.") from exc
	if result.isoformat() != value:
		raise PolicyError(f"{field} needs an ISO date.")
	return result


def _count_date(value) -> str:
	if not isinstance(value, str) or len(value) < 10:
		raise PolicyError("A physical-count time is missing.")
	return _date(value[:10], "counted_at").isoformat()


def _unavailable(reason, detail) -> dict:
	return {
		TOKEN: {
			"state": "not_evaluable",
			"reason_code": reason or "record_coverage_insufficient",
			"detail": detail,
		}
	}


def _validate_policy(snapshot: dict) -> tuple[dict, dict]:
	scope = snapshot.get("scope")
	config = snapshot.get("config")
	if not isinstance(scope, dict) or set(scope) != _SCOPE_KEYS:
		raise PolicyError("The sealed scope is invalid.")
	if not isinstance(config, dict) or set(config) != _CONFIG_KEYS:
		raise PolicyError("The sealed configuration is invalid.")
	if not isinstance(scope["company"], str) or not scope["company"].strip():
		raise PolicyError("A company is required.")
	if scope["company"] != config["company"]:
		raise PolicyError("The company policy disagrees with the scope.")
	if scope["basis"] != "current_records" or config["basis"] != "current_records":
		raise PolicyError("Current-records scope is required.")
	if config["review_scope"] != "count_plan_only":
		raise PolicyError("Count-plan-only review scope is required.")
	if not isinstance(config["policy_version"], str) or not config["policy_version"].strip():
		raise PolicyError("A policy reference is required.")
	if not isinstance(config["reviewer"], str) or not config["reviewer"].strip():
		raise PolicyError("A named reviewer is required.")
	start = _date(scope["from_date"], "from_date")
	end = _date(scope["to_date"], "to_date")
	history_start = _date(scope["count_history_from"], "count_history_from")
	plan_date = _date(scope["plan_date"], "plan_date")
	if start > end or end > plan_date or history_start > plan_date:
		raise PolicyError("The date policy is invalid.")
	for field in ("from_date", "to_date", "count_history_from", "plan_date"):
		if scope[field] != config[field]:
			raise PolicyError("The configuration date policy disagrees with the scope.")
	warehouses = scope["warehouses"]
	if (
		not isinstance(warehouses, list)
		or not 1 <= len(warehouses) <= 10
		or any(not isinstance(name, str) or not name or name != name.strip() for name in warehouses)
		or len(set(warehouses)) != len(warehouses)
		or warehouses != sorted(warehouses)
		or config["warehouses"] != warehouses
	):
		raise PolicyError("The warehouse policy is invalid.")
	for field, lower, upper in (("line_limit", 1, 40), ("recount_after_days", 1, 365)):
		value = config[field]
		if type(value) is not int or not lower <= value <= upper:
			raise PolicyError(f"{field} is invalid.")
	return scope, config


def _history(data: dict, scope: dict) -> dict[tuple[str, str], str]:
	headers: dict[str, tuple[str, str]] = {}
	history_start = _date(scope["count_history_from"], "count_history_from")
	for row in data["physical_counts"]:
		if not isinstance(row, dict) or not isinstance(row.get("name"), str):
			raise PolicyError("A physical-count header is invalid.")
		name = row["name"]
		if (
			name in headers
			or row.get("company") != scope["company"]
			or row.get("warehouse") not in scope["warehouses"]
		):
			raise PolicyError("Physical-count headers do not match the scope.")
		counted_on = _count_date(row.get("counted_at"))
		if _date(counted_on, "counted_at") < history_start:
			raise PolicyError("Physical-count history is outside the configured start date.")
		headers[name] = (row["warehouse"], counted_on)
	history: dict[tuple[str, str], str] = {}
	for row in data["physical_count_items"]:
		if (
			not isinstance(row, dict)
			or row.get("parent") not in headers
			or not isinstance(row.get("item_code"), str)
		):
			raise PolicyError("A physical-count item is invalid.")
		_number(row.get("counted_qty"))
		warehouse, counted_on = headers[row["parent"]]
		key = (warehouse, row["item_code"])
		history[key] = max(history.get(key, ""), counted_on)
	return history


def _source_counts(data: dict, source_counts) -> int:
	if not isinstance(source_counts, dict):
		raise PolicyError("Source row counts are invalid.")
	if set(source_counts) - set(_SOURCE_DATASETS.values()):
		raise PolicyError("Source row counts contain an unknown dataset.")
	for dataset, doctype in _SOURCE_DATASETS.items():
		count = source_counts.get(doctype, 0)
		if type(count) is not int or count < 0 or count != len(data[dataset]):
			raise PolicyError("Source row counts do not match the captured datasets.")
	return sum(source_counts.values())


def _movement(data: dict, scope: dict, bin_by_key: dict[tuple[str, str], dict]):
	start = _date(scope["from_date"], "from_date")
	end = _date(scope["to_date"], "to_date")
	headers: dict[str, dict] = {}
	for row in data["stock_entries"]:
		if not isinstance(row, dict) or set(row) != _STOCK_ENTRY_KEYS:
			raise PolicyError("Stock Entry header evidence is invalid.")
		name = row.get("name")
		if (
			not isinstance(name, str)
			or not name
			or name in headers
			or row.get("company") != scope["company"]
			or row.get("docstatus") != 1
			or not isinstance(row.get("purpose"), str)
			or not row["purpose"]
		):
			raise PolicyError("Stock Entry header evidence is invalid.")
		headers[name] = row

	seen: set[str] = set()
	referenced_entries: set[str] = set()
	outward: dict[tuple[str, str], Decimal] = {}
	unknown: set[tuple[str, str]] = set()
	for row in data["ledger"]:
		if not isinstance(row, dict) or set(row) != _LEDGER_KEYS:
			raise PolicyError("Movement evidence is invalid.")
		name = row.get("name")
		item_code = row.get("item_code")
		warehouse = row.get("warehouse")
		voucher_type = row.get("voucher_type")
		voucher_no = row.get("voucher_no")
		if (
			not isinstance(name, str)
			or not name
			or name in seen
			or not isinstance(item_code, str)
			or not item_code
			or not isinstance(warehouse, str)
			or warehouse not in scope["warehouses"]
			or row.get("company") != scope["company"]
			or not isinstance(voucher_type, str)
			or not voucher_type
			or not isinstance(voucher_no, str)
			or not voucher_no
			or row.get("is_cancelled") != 0
		):
			raise PolicyError("Movement evidence is outside the selected scope.")
		seen.add(name)
		posting_date = _date(row.get("posting_date"), "posting_date")
		if posting_date < start or posting_date > end:
			raise PolicyError("Movement evidence is outside the selected date range.")
		if not isinstance(row.get("posting_time"), str) or not row["posting_time"]:
			raise PolicyError("Movement evidence has no posting time.")
		quantity = _number(row.get("actual_qty"))
		if quantity >= 0:
			raise PolicyError("Movement evidence must reduce stock.")
		key = (warehouse, item_code)
		if key not in bin_by_key:
			raise PolicyError("Movement evidence has no current Bin row.")

		classification = "unknown"
		if voucher_type in _OUTWARD_VOUCHER_TYPES:
			classification = "outward"
		elif voucher_type in _EXCLUDED_VOUCHER_TYPES:
			classification = "excluded"
		elif voucher_type == "Stock Entry":
			referenced_entries.add(voucher_no)
			header = headers.get(voucher_no)
			if not header:
				raise PolicyError("A Stock Entry movement has no verified parent header.")
			if header["purpose"] in _OUTWARD_STOCK_ENTRY_PURPOSES:
				classification = "outward"
			elif header["purpose"] in _EXCLUDED_STOCK_ENTRY_PURPOSES:
				classification = "excluded"
		if classification == "outward":
			outward[key] = outward.get(key, Decimal(0)) - quantity
		elif classification == "unknown":
			unknown.add(key)
	if set(headers) != referenced_entries:
		raise PolicyError("Stock Entry headers do not exactly match the movement evidence.")
	return outward, unknown


def _eligible(item: dict, plan_date: date) -> bool:
	if not bool(item.get("is_stock_item")) or bool(item.get("disabled")):
		return False
	end_of_life = item.get("end_of_life")
	if end_of_life in (None, ""):
		return True
	return _date(end_of_life, "end_of_life") > plan_date


def _planning_rows(data: dict, scope: dict) -> list[dict]:
	if not all(isinstance(data.get(name), list) for name in _SOURCE_DATASETS):
		raise PolicyError("A source dataset is missing.")
	item_by_name: dict[str, dict] = {}
	for item in data["items"]:
		if (
			not isinstance(item, dict)
			or not isinstance(item.get("name"), str)
			or item["name"] in item_by_name
		):
			raise PolicyError("Item evidence is invalid.")
		if not isinstance(item.get("stock_uom"), str) or not item["stock_uom"].strip():
			raise PolicyError("A stock unit is missing.")
		item_by_name[item["name"]] = item
	bin_by_key: dict[tuple[str, str], dict] = {}
	for row in data["bins"]:
		if not isinstance(row, dict) or not isinstance(row.get("item_code"), str):
			raise PolicyError("Bin evidence is invalid.")
		key = (row.get("warehouse"), row["item_code"])
		if key in bin_by_key or key[0] not in scope["warehouses"] or row["item_code"] not in item_by_name:
			raise PolicyError("Bin evidence is outside the selected scope.")
		_number(row.get("actual_qty"))
		_number(row.get("stock_value"))
		bin_by_key[key] = row
	outward, unknown_movement = _movement(data, scope, bin_by_key)
	history = _history(data, scope)
	plan_date = _date(scope["plan_date"], "plan_date")
	rows = []
	for (warehouse, item_code), bin_row in sorted(bin_by_key.items()):
		item = item_by_name[item_code]
		rows.append(
			{
				"warehouse": warehouse,
				"item_code": item_code,
				"stock_uom": item["stock_uom"],
				"eligible": _eligible(item, plan_date),
				"outward_qty": (
					None
					if (warehouse, item_code) in unknown_movement
					else _number_text(outward.get((warehouse, item_code), Decimal(0)))
				),
				"actual_qty": _number_text(bin_row["actual_qty"]),
				"stock_value": _number_text(bin_row["stock_value"]),
				"last_physical_count_on": history.get((warehouse, item_code)),
				"capture_required": bool(item.get("has_batch_no")) or bool(item.get("has_serial_no")),
			}
		)
	return rows


def _q2(value) -> Decimal:
	return _number(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _assign_abc(rows: list[dict]) -> dict[str, str]:
	values = {row["item_code"]: max(Decimal(0), _q2(row["stock_value"])) for row in rows}
	total = sum(values.values(), Decimal(0))
	if not total:
		return {}
	result: dict[str, str] = {}
	cumulative = Decimal(0)
	for index, code in enumerate(sorted(values, key=lambda name: (-values[name], name))):
		cumulative += values[code]
		fraction = cumulative / total
		result[code] = "A" if index == 0 or fraction <= _ABC_A_CUM else "B" if fraction <= _ABC_B_CUM else "C"
	return result


def _velocity_bucket(quantity) -> str:
	quantity = _number(quantity).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
	return "FAST" if quantity >= _VEL_FAST_MIN else "MEDIUM" if quantity >= _VEL_MED_MIN else "SLOW"


def _cadence_days(abc: str | None, velocity: str | None) -> int:
	if abc is not None and velocity is not None:
		return _CADENCE[(abc, velocity)]
	if abc is not None:
		return _CADENCE_VALUE_ONLY[abc]
	if velocity is not None:
		return _CADENCE_VELOCITY_ONLY[velocity]
	raise PolicyError("A count cadence needs a usable signal.")


def _base(population: list[dict], outcome="none_due", reason="no_count_due") -> dict:
	return {
		"outcome": outcome,
		"reason_code": reason,
		"lines_considered": len(population),
		"lines_planned": 0,
		"lines_rolled": 0,
		"due_total": None if outcome == "ask" else 0,
		"lines": [],
	}


def _plan(warehouses: list[str], rows: list[dict], plan_date: str, line_limit: int, recount_after_days: int):
	if type(line_limit) is not int or not 1 <= line_limit <= 40:
		raise PolicyError("Invalid count-list size.")
	if type(recount_after_days) is not int or not 1 <= recount_after_days <= 365:
		raise PolicyError("Invalid recount interval.")
	plan_day = _date(plan_date, "plan_date")
	result, candidates = {}, []
	for warehouse in sorted(warehouses):
		population = [row for row in rows if row["warehouse"] == warehouse]
		if any(
			not row["eligible"] and (_number(row["actual_qty"]) != 0 or _number(row["stock_value"]) != 0)
			for row in population
		):
			result[warehouse] = _base(population, "ask", "item_status_review")
			continue
		eligible = [row for row in population if row["eligible"]]
		if not eligible:
			result[warehouse] = _base(population)
			continue
		abc = _assign_abc(eligible)
		unknown_movement = any(row["outward_qty"] is None for row in eligible)
		movement_signal = any(
			row["outward_qty"] is not None and _number(row["outward_qty"]) > 0 for row in eligible
		)
		if not abc and not movement_signal:
			result[warehouse] = _base(population, "ask", "ranking_signal_unavailable")
			continue
		result[warehouse] = _base(
			population, reason="movement_unavailable" if unknown_movement else "cadence_not_due"
		)
		for row in eligible:
			code = row["item_code"]
			value_class = abc.get(code)
			velocity = (
				_velocity_bucket(row["outward_qty"])
				if movement_signal and row["outward_qty"] is not None
				else None
			)
			if value_class is None and velocity is None:
				continue
			last = row["last_physical_count_on"]
			age = (plan_day - _date(last, "last_physical_count_on")).days if last is not None else None
			if age is not None and age < 0:
				raise PolicyError("A physical count is newer than the plan date.")
			if age is not None and age <= max(_cadence_days(value_class, velocity), recount_after_days):
				continue
			candidates.append(
				{
					"warehouse": warehouse,
					"item_code": code,
					"stock_uom": row["stock_uom"],
					"abc": value_class or "",
					"velocity": velocity or "",
					"outward_qty": row["outward_qty"],
					"actual_qty": row["actual_qty"],
					"stock_value": row["stock_value"],
					"last_physical_count_on": last,
					"capture_required": row["capture_required"],
				}
			)
	candidates.sort(
		key=lambda row: (
			0 if _number(row["actual_qty"]) < 0 else 1,
			0 if row["last_physical_count_on"] is None else 1,
			-max(Decimal(0), _number(row["stock_value"])),
			row["warehouse"],
			row["item_code"],
		)
	)
	for index, candidate in enumerate(candidates):
		warehouse = candidate["warehouse"]
		proposal = result[warehouse]
		proposal["due_total"] += 1
		if index < line_limit:
			proposal["lines"].append({key: value for key, value in candidate.items() if key != "warehouse"})
		else:
			proposal["lines_rolled"] += 1
	for proposal in result.values():
		proposal["lines_planned"] = len(proposal["lines"])
		if proposal["lines_planned"]:
			proposal["outcome"] = "plan"
			if proposal["reason_code"] == "cadence_not_due":
				proposal["reason_code"] = "cadence_due"
		elif proposal["due_total"]:
			proposal["outcome"] = "deferred"
			proposal["reason_code"] = "cycle_budget"
	return result


def _finding(warehouse: str, proposal: dict) -> dict:
	return {
		"token": TOKEN,
		"ref_doctype": "Warehouse",
		"ref_name": warehouse,
		"amount": 0,
		"severity": "note",
		"result_class": "derived_candidate",
		"confidence": "medium",
		"match_basis": "Current stock, permitted outbound movement, and submitted physical-count history.",
		"false_positive_path": "A human can rescope, defer, or change this physical-count plan during review.",
		"note": (
			f"Human physical-count plan for {warehouse}. Approval permits a count plan only; "
			"no quantity is recorded and no stock is changed."
		),
		"proposal": proposal,
	}


def evaluate(snapshot: dict) -> dict:
	"""Return the one canonical complete-policy result for a sealed snapshot.

	``PolicyError`` means the app must finish the run as not evaluable.  This
	function never accepts or observes a delegate's output.
	"""
	with localcontext() as context:
		context.prec = 60
		try:
			if not isinstance(snapshot, dict):
				raise PolicyError("The source snapshot is invalid.")
			source = {
				key: value
				for key, value in snapshot.items()
				if key not in {"source_digest", "captured_at", "captured_by"}
			}
			supplied_digest = snapshot.get("source_digest")
			if not isinstance(supplied_digest, str) or supplied_digest != digest(source):
				raise PolicyError("The source digest does not match.")
			scope, config = _validate_policy(snapshot)
			if snapshot.get("complete") is not True:
				raise PolicyError("The source snapshot is not complete.")
			data = snapshot.get("datasets")
			if not isinstance(data, dict) or set(data) != set(_SOURCE_DATASETS):
				raise PolicyError("A source dataset is missing.")
			if not all(isinstance(data[key], list) for key in _SOURCE_DATASETS):
				raise PolicyError("A source dataset is invalid.")
			rows_consumed = _source_counts(data, snapshot.get("source_counts"))
			rows = _planning_rows(data, scope)
			proposals = _plan(
				scope["warehouses"],
				rows,
				config["plan_date"],
				config["line_limit"],
				config["recount_after_days"],
			)
			findings = [_finding(warehouse, proposals[warehouse]) for warehouse in scope["warehouses"]]
			coverage = {
				TOKEN: {
					"state": "evaluated",
					"detail": "Complete permission-bounded stock, movement, and submitted physical-count evidence was assessed.",
				}
			}
			return {
				"findings": findings,
				"coverage": coverage,
				"scope": scope,
				"rows_consumed": rows_consumed,
				"integrity_digest": digest(
					{
						"input_digest": supplied_digest,
						"scope": scope,
						"findings": findings,
						"coverage": coverage,
					}
				),
			}
		except PolicyError:
			raise
		except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError, RecursionError) as exc:
			raise PolicyError("The sealed count-plan source is invalid.") from exc
