"""Trusted server evaluator for submitted physical-count facts.

The server owns source collection.  This module accepts only already-collected,
permission-checked rows from :mod:`stock_variance_evaluation`; it has no file or
delegate input path.
"""

import copy
import hashlib
import json
from bisect import bisect_right
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

TOKEN = "sv-vtol-7b2c"
VERSION = "0.5.0"
MAX_ROWS = 20000
# The report includes every selected count line. Keep the private input bounded
# before rendering it, then apply the exact rendered-page bound below.
MAX_COUNT_LINES = 1500
DIMENSION_FIELDS = "name target_fieldname document_type apply_to_all_doctypes creation modified"
DIMENSION_SOURCE_KEYS = ("dimensions_global", "dimensions_scoped")
# A dimension that applies everywhere, or to one of these documents, can
# partition the selected count or its Stock Ledger Entry history. A dimension
# for an unrelated inventory document must not make this review unavailable.
RELEVANT_DIMENSION_DOCTYPES = (
	"Jarvis Physical Count",
	"Jarvis Physical Count Item",
	"Stock Ledger Entry",
)
FIELDS = {
	"company": ("Company", "name default_currency is_group creation modified"),
	"warehouse_root": (
		"Warehouse",
		"name company parent_warehouse is_group disabled lft rgt creation modified",
	),
	"warehouses": (
		"Warehouse",
		"name company parent_warehouse is_group disabled lft rgt creation modified",
	),
	"counts": (
		"Jarvis Physical Count",
		"name company warehouse docstatus counted_at creation modified",
	),
	"count_lines": (
		"Jarvis Physical Count Item",
		"name parent parenttype parentfield idx item_code counted_qty stock_uom batch_detail_recorded creation modified parent_modified",
	),
	"items": (
		"Item",
		"name is_stock_item has_variants variant_of has_batch_no has_serial_no stock_uom disabled creation modified",
	),
	"sle_entries": (
		"Stock Ledger Entry",
		"name company item_code warehouse posting_date posting_time posting_datetime qty_after_transaction valuation_rate stock_value stock_value_difference actual_qty voucher_type voucher_no docstatus is_cancelled is_adjustment_entry creation modified",
	),
	"repost_queue": (
		"Repost Item Valuation",
		"name company based_on item_code warehouse status docstatus voucher_type voucher_no posting_date posting_time repost_only_accounting_ledgers creation modified",
	),
	"repost_transactions": (
		"Repost Item Valuation",
		"name company based_on item_code warehouse status docstatus voucher_type voucher_no posting_date posting_time repost_only_accounting_ledgers creation modified",
	),
	"dimensions_global": ("Inventory Dimension", DIMENSION_FIELDS),
	"dimensions_scoped": ("Inventory Dimension", DIMENSION_FIELDS),
}
NUMBERS = set(
	"lft rgt idx counted_qty valuation_rate qty_after_transaction stock_value stock_value_difference actual_qty".split()
)
FLAGS = set(
	"is_group disabled is_stock_item has_variants has_batch_no has_serial_no is_cancelled is_adjustment_entry batch_detail_recorded apply_to_all_doctypes repost_only_accounting_ledgers".split()
)


class Gap(ValueError):
	def __init__(self, message, reason="record_coverage_insufficient", state="not_evaluable"):
		super().__init__(message)
		self.reason = reason
		self.state = state


def require(condition, message, reason="record_coverage_insufficient"):
	if not condition:
		raise Gap(message, reason)


def source_row_limit(key):
	return MAX_COUNT_LINES if key == "count_lines" else MAX_ROWS


def enforce_row_limit(key, rows):
	if len(rows) <= source_row_limit(key):
		return
	message = (
		f"More than {MAX_COUNT_LINES:,} count lines were selected. Narrow the warehouse scope or dates."
		if key == "count_lines"
		else "A source exceeds the row cap"
	)
	raise Gap(message, "run_truncated_watermark", "truncated")


def number(value):
	try:
		if value is None or isinstance(value, bool):
			raise ValueError
		d = Decimal(str(value))
		if not d.is_finite() or abs(d) > Decimal("1e15"):
			raise ValueError
		return d
	except (ValueError, TypeError, InvalidOperation):
		raise Gap("A quantity or rate is missing or invalid") from None


def day(value):
	try:
		return date.fromisoformat(value)
	except (ValueError, TypeError):
		raise Gap("A source date is invalid") from None


def clock(value):
	try:
		require(isinstance(value, str), "A source time is invalid")
		value = value.split(":", 1)[0].zfill(2) + ":" + value.split(":", 1)[1]
		t = time.fromisoformat(value)
		require(t.tzinfo is None, "A source time has a timezone")
		return t
	except (ValueError, TypeError, IndexError):
		raise Gap("A source time is invalid") from None


def stamp(value):
	try:
		d = datetime.fromisoformat(value)
		require(d.tzinfo is None, "A source timestamp has a timezone")
		return d
	except (ValueError, TypeError):
		raise Gap("A source timestamp is invalid") from None


def sha(value):
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
	).hexdigest()


def semantic(value):
	if isinstance(value, bool) or value is None:
		return value
	if isinstance(value, (int, float, Decimal)):
		return {"$numeric": format(number(value).normalize(), "f")}
	if isinstance(value, dict):
		return {k: semantic(v) for k, v in value.items()}
	if isinstance(value, list):
		values = value
		if values and all(isinstance(v, dict) and isinstance(v.get("name"), str) for v in values):
			values = sorted(values, key=lambda v: v["name"])
		elif values and all(isinstance(v, dict) and isinstance(v.get("source"), str) for v in values):
			values = sorted(values, key=lambda v: v["source"])
		return [semantic(v) for v in values]
	return value


def policy(scope, config):
	require(
		isinstance(scope, dict) and isinstance(config, dict),
		"the launcher scope and stock review settings",
		"configuration_missing",
	)
	require(
		set(config) == {"stock_variance"},
		"the declared stock review settings",
		"configuration_missing",
	)
	c = config["stock_variance"]
	require(
		isinstance(c, dict) and not set(c) - {"reviewer", "test_declaration"},
		"the supported stock review settings",
		"configuration_missing",
	)
	require(
		isinstance(c.get("reviewer"), str) and 0 < len(c["reviewer"].strip()) <= 140,
		"the stock reviewer",
		"configuration_missing",
	)
	require(
		isinstance(c.get("test_declaration", ""), str) and len(c.get("test_declaration", "")) <= 500,
		"the plain test notice",
		"configuration_missing",
	)
	require(
		isinstance(scope.get("company"), str) and bool(scope["company"]),
		"the review company",
		"configuration_missing",
	)
	r = scope.get("record_scope")
	require(
		isinstance(r, dict)
		and r.get("doctype") == "Warehouse"
		and isinstance(r.get("name"), str)
		and bool(r["name"]),
		"a Warehouse record scope",
		"configuration_missing",
	)
	start, end, report = (day(scope.get(k)) for k in ["from_date", "to_date", "report_date"])
	cutoff = min(end, report)
	require(
		start <= cutoff,
		"the review dates through the report cutoff",
		"configuration_missing",
	)
	return {
		"company": scope["company"],
		"root": r["name"],
		"start": start.isoformat(),
		"end": end.isoformat(),
		"cutoff": cutoff.isoformat(),
		"reviewer": c["reviewer"],
		"test_declaration": c.get("test_declaration", ""),
	}


def pred(field, op, value):
	return {"field": field, "op": op, "value": value}


def exists(dt, alias, where, joins=None):
	s = {"from": dt, "alias": alias, "where": where}
	if joins:
		s["joins"] = joins
	return {"op": "exists", "value": s}


def warehouse_predicate(reference, p, prefix, *, active_leaf=True):
	"""Match a selected Warehouse subtree, optionally only active leaves.

	The warehouse metadata pull needs every descendant to validate the nested-set
	tree. Every fact source, however, must stop at enabled leaf warehouses. This
	keeps a disabled descendant out of the captured source, rather than merely
	excluding it after it consumes a source row or page budget.
	"""
	w, root = prefix + "w", prefix + "root"
	where = [
		pred(w + ".name", "=", {"$field": reference}),
		pred(w + ".company", "=", p["company"]),
		pred(root + ".company", "=", p["company"]),
		pred(root + ".name", "=", p["root"]),
		pred(w + ".lft", ">=", {"$field": root + ".lft"}),
		pred(w + ".rgt", "<=", {"$field": root + ".rgt"}),
	]
	if active_leaf:
		where.extend(
			[
				pred(root + ".disabled", "=", 0),
				pred(w + ".is_group", "=", 0),
				pred(w + ".disabled", "=", 0),
			]
		)
	return exists(
		"Warehouse",
		w,
		where,
		[
			{
				"doctype": "Warehouse",
				"alias": root,
				"type": "inner",
				"on": {root + ".company": w + ".company"},
			}
		],
	)


def parent_filters(alias, p, *, through_cutoff=False):
	end = p["cutoff"] if through_cutoff else p["end"]
	return [
		pred(alias + ".company", "=", p["company"]),
		pred(alias + ".docstatus", "=", 1),
		pred(alias + ".counted_at", ">=", p["start"] + " 00:00:00"),
		pred(alias + ".counted_at", "<=", end + " 23:59:59.999999"),
	]


def parent_where(alias, p, prefix, *, through_cutoff=False):
	child = prefix + "c"
	return [
		*parent_filters(alias, p, through_cutoff=through_cutoff),
		warehouse_predicate(alias + ".warehouse", p, prefix + "w"),
		exists(
			"Jarvis Physical Count Item",
			child,
			[
				pred(child + ".parent", "=", {"$field": alias + ".name"}),
				pred(child + ".parenttype", "=", "Jarvis Physical Count"),
				pred(child + ".parentfield", "=", "items"),
			],
		),
	]


def query_specs(scope, config):
	p = policy(scope, config)
	specs = {}
	for key, (dt, fields) in FIELDS.items():
		q = {
			"from": dt,
			"alias": "r",
			"select": ["r." + f for f in fields.split() if f != "parent_modified"],
			"where": [],
			"order_by": [{"field": "r.name", "dir": "asc"}],
			"limit": 200,
		}
		if key == "company":
			q["where"] = [pred("r.name", "=", p["company"])]
		elif key in ["warehouse_root", "warehouses"]:
			q["where"] = [pred("r.company", "=", p["company"])]
			if key == "warehouse_root":
				q["where"].append(pred("r.name", "=", p["root"]))
			else:
				# The selected root is a record boundary, not only an input to
				# later evaluator checks.  Do not collect sibling warehouse
				# metadata from the same company.
				q["where"].append(warehouse_predicate("r.name", p, "wh", active_leaf=False))
		elif key == "counts":
			q["where"] = parent_where("r", p, "pc", through_cutoff=True)
		elif key == "count_lines":
			q["joins"] = [
				{
					"doctype": "Jarvis Physical Count",
					"alias": "p",
					"type": "inner",
					"on": {"r.parent": "p.name"},
				}
			]
			q["where"] = parent_where("p", p, "lc", through_cutoff=True) + [
				pred("r.parenttype", "=", "Jarvis Physical Count"),
				pred("r.parentfield", "=", "items"),
			]
			q["select"].append({"expr": {"field": "p.modified"}, "as": "parent_modified"})
		elif key == "items":
			q["where"] = [
				exists(
					"Jarvis Physical Count Item",
					"ic",
					[
						pred("ic.item_code", "=", {"$field": "r.name"}),
						pred("ic.parenttype", "=", "Jarvis Physical Count"),
						pred("ic.parentfield", "=", "items"),
						*parent_filters("ip", p, through_cutoff=True),
						pred("iw.company", "=", p["company"]),
						pred("ir.disabled", "=", 0),
						pred("iw.is_group", "=", 0),
						pred("iw.disabled", "=", 0),
						pred("ir.name", "=", p["root"]),
						pred("iw.lft", ">=", {"$field": "ir.lft"}),
						pred("iw.rgt", "<=", {"$field": "ir.rgt"}),
					],
					[
						{
							"doctype": "Jarvis Physical Count",
							"alias": "ip",
							"type": "inner",
							"on": {"ic.parent": "ip.name"},
						},
						{
							"doctype": "Warehouse",
							"alias": "iw",
							"type": "inner",
							"on": {"iw.name": "ip.warehouse"},
						},
						{
							"doctype": "Warehouse",
							"alias": "ir",
							"type": "inner",
							"on": {"ir.company": "iw.company"},
						},
					],
				)
			]
		elif key == "sle_entries":
			q["where"] = [
				pred("r.company", "=", p["company"]),
				pred("r.is_cancelled", "=", 0),
				pred("r.posting_datetime", "<=", p["cutoff"] + " 23:59:59.999999"),
				warehouse_predicate("r.warehouse", p, "sw"),
				exists(
					"Jarvis Physical Count Item",
					"sc",
					[
						pred("sc.item_code", "=", {"$field": "r.item_code"}),
						pred("sc.parenttype", "=", "Jarvis Physical Count"),
						pred("sc.parentfield", "=", "items"),
						*parent_filters("sp", p, through_cutoff=True),
						pred("sp.warehouse", "=", {"$field": "r.warehouse"}),
					],
					[
						{
							"doctype": "Jarvis Physical Count",
							"alias": "sp",
							"type": "inner",
							"on": {"sc.parent": "sp.name"},
						}
					],
				),
			]
		elif key == "repost_queue":
			q["where"] = [
				pred("r.company", "=", p["company"]),
				pred("r.docstatus", "=", 1),
				pred("r.status", "in", ["Queued", "In Progress", "Failed"]),
				pred("r.based_on", "=", "Item and Warehouse"),
				pred("r.repost_only_accounting_ledgers", "=", 0),
				pred("r.posting_date", "<=", p["cutoff"]),
				exists(
					"Jarvis Physical Count Item",
					"rc",
					[
						pred("rc.item_code", "=", {"$field": "r.item_code"}),
						pred("rc.parenttype", "=", "Jarvis Physical Count"),
						pred("rc.parentfield", "=", "items"),
						pred("rp.warehouse", "=", {"$field": "r.warehouse"}),
						*parent_filters("rp", p, through_cutoff=True),
						pred("rqw.company", "=", p["company"]),
						pred("rqw.is_group", "=", 0),
						pred("rqw.disabled", "=", 0),
						pred("rqroot.company", "=", p["company"]),
						pred("rqroot.disabled", "=", 0),
						pred("rqroot.name", "=", p["root"]),
						pred("rqw.lft", ">=", {"$field": "rqroot.lft"}),
						pred("rqw.rgt", "<=", {"$field": "rqroot.rgt"}),
					],
					[
						{
							"doctype": "Jarvis Physical Count",
							"alias": "rp",
							"type": "inner",
							"on": {"rc.parent": "rp.name"},
						},
						{
							"doctype": "Warehouse",
							"alias": "rqw",
							"type": "inner",
							"on": {"rqw.name": "rp.warehouse"},
						},
						{
							"doctype": "Warehouse",
							"alias": "rqroot",
							"type": "inner",
							"on": {"rqroot.company": "rqw.company"},
						},
					],
				),
			]
		elif key == "repost_transactions":
			q["where"] = [
				pred("r.company", "=", p["company"]),
				pred("r.docstatus", "=", 1),
				pred("r.status", "in", ["Queued", "In Progress", "Failed"]),
				pred("r.based_on", "=", "Transaction"),
				pred("r.repost_only_accounting_ledgers", "=", 0),
				pred("r.posting_date", "<=", p["cutoff"]),
				exists(
					"Stock Ledger Entry",
					"rs",
					[
						pred("rs.company", "=", {"$field": "r.company"}),
						pred("rs.voucher_type", "=", {"$field": "r.voucher_type"}),
						pred("rs.voucher_no", "=", {"$field": "r.voucher_no"}),
						pred("rs.is_cancelled", "=", 0),
						pred("rs.docstatus", "<", 2),
						pred(
							"rs.posting_datetime",
							"<=",
							p["cutoff"] + " 23:59:59.999999",
						),
						pred("rt.parenttype", "=", "Jarvis Physical Count"),
						pred("rt.parentfield", "=", "items"),
						*parent_filters("rp", p, through_cutoff=True),
						pred("rp.warehouse", "=", {"$field": "rs.warehouse"}),
						pred("rtw.company", "=", p["company"]),
						pred("rtw.is_group", "=", 0),
						pred("rtw.disabled", "=", 0),
						pred("rtroot.company", "=", p["company"]),
						pred("rtroot.disabled", "=", 0),
						pred("rtroot.name", "=", p["root"]),
						pred("rtw.lft", ">=", {"$field": "rtroot.lft"}),
						pred("rtw.rgt", "<=", {"$field": "rtroot.rgt"}),
					],
					[
						{
							"doctype": "Jarvis Physical Count Item",
							"alias": "rt",
							"type": "inner",
							"on": {"rt.item_code": "rs.item_code"},
						},
						{
							"doctype": "Jarvis Physical Count",
							"alias": "rp",
							"type": "inner",
							"on": {"rt.parent": "rp.name"},
						},
						{
							"doctype": "Warehouse",
							"alias": "rtw",
							"type": "inner",
							"on": {"rtw.name": "rs.warehouse"},
						},
						{
							"doctype": "Warehouse",
							"alias": "rtroot",
							"type": "inner",
							"on": {"rtroot.company": "rtw.company"},
						},
					],
				),
			]
		elif key == "dimensions_global":
			q["where"] = [pred("r.apply_to_all_doctypes", "=", 1)]
		elif key == "dimensions_scoped":
			q["where"] = [
				pred("r.apply_to_all_doctypes", "=", 0),
				pred("r.document_type", "in", list(RELEVANT_DIMENSION_DOCTYPES)),
			]
		specs[key] = q
	return specs


def fingerprint_specs(scope, config):
	out = copy.deepcopy(query_specs(scope, config))
	for key, q in out.items():
		q["select"] = [
			{"agg": "count", "field": "*", "as": "n"},
			{"agg": "max", "field": "r.modified", "as": "modified"},
			{"agg": "max", "field": "r.creation", "as": "creation"},
		]
		if key == "count_lines":
			q["select"].append({"agg": "max", "field": "p.modified", "as": "parent_modified"})
		q.pop("order_by")
		q["limit"] = 1
	return out


def fingerprint(key, rows):
	out = {
		"n": len(rows),
		"modified": max((r["modified"] for r in rows), key=stamp, default=None),
		"creation": max((r["creation"] for r in rows), key=stamp, default=None),
	}
	if key == "count_lines":
		out["parent_modified"] = max((r["parent_modified"] for r in rows), key=stamp, default=None)
	return out


def checked_rows(key, rows):
	require(isinstance(rows, list), "A required source is missing")
	enforce_row_limit(key, rows)
	names = set()
	expected = set(FIELDS[key][1].split())
	for r in rows:
		require(
			isinstance(r, dict) and set(r) == expected,
			"A source has missing or extra fields",
		)
		require(
			isinstance(r.get("name"), str) and bool(r["name"]) and r["name"] not in names,
			"A source has missing or repeated record names",
		)
		names.add(r["name"])
		stamp(r["creation"])
		stamp(r["modified"])
		for k, v in r.items():
			if k in NUMBERS:
				number(v)
			if k in FLAGS:
				require(type(v) is int and v in [0, 1], "A native flag is invalid")
			if k == "docstatus":
				require(
					type(v) is int and v in [0, 1, 2],
					"A native document state is invalid",
				)
		if "posting_date" in r and r["posting_date"] is not None:
			day(r["posting_date"])
		if "posting_time" in r:
			clock(r["posting_time"])
		if "posting_datetime" in r:
			stamp(r["posting_datetime"])
		if "counted_at" in r:
			stamp(r["counted_at"])
	return sorted(rows, key=lambda r: r["name"])


def _fp(value):
	require(
		isinstance(value, dict)
		and set(value)
		in [
			{"n", "modified", "creation"},
			{"n", "modified", "creation", "parent_modified"},
		],
		"A source fingerprint is invalid",
	)
	require(type(value["n"]) is int and value["n"] >= 0, "A source count is invalid")
	return {k: (stamp(v) if v is not None else None) if k != "n" else v for k, v in value.items()}


def check_sources(scope, config, data):
	errors = data.get("_source_errors", {})
	if errors:
		count_errors = [errors[key] for key in ["counts", "count_lines"] if key in errors]
		truncated_count_error = next((error for error in count_errors if error["state"] == "truncated"), None)
		if truncated_count_error:
			raise Gap(
				truncated_count_error["detail"],
				truncated_count_error["reason_code"],
				"truncated",
			)
		if count_errors:
			raise Gap(
				"The submitted physical-count header or item source is unavailable. The required DocType may not be installed or readable.",
				"permission_slice",
			)
		e = next(
			(v for v in errors.values() if v["state"] == "truncated"),
			next(iter(errors.values())),
		)
		raise Gap(e["detail"], e["reason_code"], e["state"])
	rows = {}
	for key in FIELDS:
		if key in ["counts", "count_lines"] and not isinstance(data.get(key), list):
			raise Gap(
				"The submitted physical-count header or item source is unavailable",
				"permission_slice",
			)
		rows[key] = checked_rows(key, data.get(key))
	receipts = data.get("source_receipts")
	require(
		isinstance(receipts, list) and len(receipts) == len(FIELDS),
		"Source receipts are missing or repeated",
	)
	rm = {r.get("source"): r for r in receipts if isinstance(r, dict)}
	require(set(rm) == set(FIELDS), "Source receipts do not cover every query")
	for key, spec in query_specs(scope, config).items():
		r = rm[key]
		require(
			set(r) == {"source", "query_sha256", "before", "after"} and r["query_sha256"] == sha(spec),
			"A source receipt does not match its query",
		)
		a, b = _fp(r["before"]), _fp(r["after"])
		require(a == b, "The source changed during the read", "source_stale")
		if a["n"] > len(rows[key]):
			raise Gap(
				"The source file has fewer rows than the stable source count",
				"run_truncated_watermark",
				"truncated",
			)
		require(
			a == _fp(fingerprint(key, rows[key])),
			"Source rows do not match their count and timestamps",
			"source_stale",
		)
	return rows


def combined_source_rows(rows, keys):
	"""Recombine split source pulls without expanding their logical row cap."""
	result = [row for key in keys for row in rows[key]]
	require(
		len({row["name"] for row in result}) == len(result),
		"A split source repeats a record",
		"source_stale",
	)
	enforce_row_limit("split_source", result)
	return result


def warehouse_set(rows, p):
	require(
		len(rows["warehouse_root"]) == 1,
		"the selected warehouse",
		"configuration_missing",
	)
	root = rows["warehouse_root"][0]
	require(
		root["name"] == p["root"] and root["company"] == p["company"],
		"The warehouse root is outside the scope",
	)
	require(not root["disabled"], "The selected warehouse is disabled", "source_stale")
	warehouses = {w["name"]: w for w in rows["warehouses"]}
	require(warehouses.get(p["root"]) == root, "The warehouse root changed", "source_stale")
	require(
		all(w["company"] == p["company"] for w in warehouses.values()),
		"Warehouse metadata crosses companies",
	)
	for w in warehouses.values():
		require(
			number(w["lft"]) == int(number(w["lft"]))
			and number(w["rgt"]) == int(number(w["rgt"]))
			and number(w["lft"]) < number(w["rgt"]),
			"The warehouse tree is invalid",
		)
	inside = {k: w for k, w in warehouses.items() if root["lft"] <= w["lft"] and w["rgt"] <= root["rgt"]}
	require(
		root["rgt"] - root["lft"] + 1 == 2 * len(inside),
		"Warehouse permissions or metadata hide part of the selected tree",
	)
	positions = [v for w in inside.values() for v in [w["lft"], w["rgt"]]]
	require(len(set(positions)) == len(positions), "Warehouse bounds overlap")
	stack = []
	for w in sorted(inside.values(), key=lambda w: w["lft"]):
		while stack and w["lft"] > stack[-1]["rgt"]:
			stack.pop()
		if stack:
			require(
				w["rgt"] < stack[-1]["rgt"]
				and w["parent_warehouse"] == stack[-1]["name"]
				and stack[-1]["is_group"] == 1,
				"A warehouse parent or interval is inconsistent",
			)
		else:
			require(w["name"] == p["root"], "The warehouse tree has more than one root")
		stack.append(w)
	leaves = {k for k, w in inside.items() if not w["is_group"] and not w["disabled"]}
	require(bool(leaves), "a leaf warehouse in the selected scope", "configuration_missing")
	require(
		all(w["is_group"] or w["rgt"] == w["lft"] + 1 for w in inside.values()),
		"A leaf warehouse has descendants",
	)
	return leaves


def _rounded(v):
	return float(number(v).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def _equal(a, b):
	return number(a).quantize(Decimal(".000000001")) == number(b).quantize(Decimal(".000000001"))


def finding_note(lines, count_time, currency):
	short = sum(line["direction"] == "short" for line in lines)
	over = sum(line["direction"] == "over" for line in lines)
	total = _rounded(sum((number(line["amount"]) for line in lines), Decimal(0)))
	return "\n".join(
		[
			f"{len(lines)} material stock count differences need review.",
			"",
			"Count time: " + count_time,
			f"Short lines: {short}; over lines: {over}.",
			f"Sum of absolute values: {currency} {total:,.2f}.",
			"Line detail is in the saved Stock Count Review dashboard.",
			"",
			"The parent amount is the sum of absolute values. It does not prove a cause, loss or required stock adjustment.",
		]
	)


def partial_coverage(errors):
	priority = (
		"unsupported_customisation",
		"app_absent_or_ineligible",
		"permission_slice",
		"source_stale",
		"record_coverage_insufficient",
		"external_evidence_absent",
	)
	reasons = {error.reason for error in errors}
	reason = next((candidate for candidate in priority if candidate in reasons), None)
	return {
		"state": "not_evaluable",
		"reason_code": reason or "record_coverage_insufficient",
		"detail": (
			"One or more selected physical counts could not be evaluated. "
			"Review the excluded count documents and run the check again."
		),
	}


def incomplete_marker(errors):
	coverage = partial_coverage(errors)
	return {
		"reason_code": coverage["reason_code"],
		"detail": coverage["detail"],
	}


def repost_started_at(repost):
	return datetime.combine(day(repost["posting_date"]), clock(repost["posting_time"]))


def unsettled_reposts_by_pair(rows, count_pairs):
	"""Map relevant unresolved reposts to the counted item/warehouse pairs.

	A direct repost owns its item and warehouse. A transaction repost owns every
	selected Stock Ledger Entry pair for its voucher. Accounting-only reposts do
	not change stock-ledger evidence. The timestamp is checked per physical count
	below because a repost that starts after a saved count cannot invalidate that
	count's reconstructed book balance.
	"""
	by_pair = {}

	def append(pair, repost):
		if pair in count_pairs:
			by_pair.setdefault(pair, []).append(repost)

	for repost in rows["repost_queue"]:
		if repost["repost_only_accounting_ledgers"] == 1:
			continue
		require(
			repost["based_on"] == "Item and Warehouse",
			"A direct stock valuation repost has an unsupported source",
			"unsupported_customisation",
		)
		append((repost["item_code"], repost["warehouse"]), repost)

	for repost in rows["repost_transactions"]:
		if repost["repost_only_accounting_ledgers"] == 1:
			continue
		require(
			repost["based_on"] == "Transaction",
			"A transaction stock valuation repost has an unsupported source",
			"unsupported_customisation",
		)
		for sle in rows["sle_entries"]:
			if sle["voucher_type"] == repost["voucher_type"] and sle["voucher_no"] == repost["voucher_no"]:
				append((sle["item_code"], sle["warehouse"]), repost)
	return by_pair


def evaluate_native(scope, config, data):
	result = {
		"agent": "stock-variance-auditor",
		"evaluator_version": VERSION,
		"scope": {
			k: scope.get(k)
			for k in [
				"company",
				"from_date",
				"to_date",
				"report_date",
				"record_scope",
				"prior_period_from",
				"prior_period_to",
			]
			if k in scope
		}
		if isinstance(scope, dict)
		else {},
		"run_state": "not_evaluable",
		"incomplete": None,
		"truncated": False,
		"findings": [],
		"coverage": {},
		"count_results": [],
		"excluded_counts": [],
		"cross_references": [],
		"rows_consumed": 0,
		"source_hashes": {},
		"source_counts": {},
		"query_hashes": {},
		"reviewer": None,
		"currency": None,
		"test_declaration": "",
	}
	try:
		p = policy(scope, config)
		result.update(reviewer=p["reviewer"], test_declaration=p["test_declaration"])
		result["scope"]["effective_cutoff"] = p["cutoff"]
		require(isinstance(data, dict), "Native source inputs are missing")
		rows = check_sources(scope, config, data)
		result["source_counts"] = {k: len(v) for k, v in rows.items()}
		result["rows_consumed"] = sum(result["source_counts"].values())
		leaves = warehouse_set(rows, p)
		result["scope"]["warehouse_set"] = sorted(leaves)
		require(
			len(rows["company"]) == 1
			and rows["company"][0]["name"] == p["company"]
			and not rows["company"][0]["is_group"],
			"Company metadata does not match the scope",
		)
		result["currency"] = rows["company"][0]["default_currency"]
		require(
			result["currency"] == "INR",
			"This count-sizing review currently supports INR books",
			"app_absent_or_ineligible",
		)
		# The two source queries include only dimensions that can partition the
		# selected count or its Stock Ledger Entry history. This evaluator does not
		# receive their field values, so it cannot establish a dimension-safe book
		# quantity when either query returns a row.
		require(
			not combined_source_rows(rows, DIMENSION_SOURCE_KEYS),
			"Inventory dimensions on Stock Ledger Entry need a separate count adapter",
			"unsupported_customisation",
		)
		parents = {r["name"]: r for r in rows["counts"]}
		reviewable_parents = {
			name: parent
			for name, parent in parents.items()
			if stamp(parent["counted_at"]).date() <= day(p["cutoff"])
		}
		items = {r["name"]: r for r in rows["items"]}
		lines = {k: [] for k in reviewable_parents}
		require(
			all(r["company"] == p["company"] for r in parents.values()),
			"Count documents cross companies",
		)
		require(
			all(
				r["docstatus"] == 1 and day(p["start"]) <= stamp(r["counted_at"]).date() <= day(p["end"])
				for r in parents.values()
			),
			"A count is outside the declared submitted-fact source query",
		)
		for line in rows["count_lines"]:
			require(
				line["parent"] in reviewable_parents
				and line["parenttype"] == "Jarvis Physical Count"
				and line["parentfield"] == "items",
				"A count line is outside the report cutoff or has no valid native parent",
			)
			lines[line["parent"]].append(line)
		sle_by_key = {}
		for s in rows["sle_entries"]:
			require(
				s["company"] == p["company"]
				and s["warehouse"] in leaves
				and s["is_cancelled"] == 0
				and s["docstatus"] < 2,
				"A ledger row is outside the selected source",
			)
			require(
				stamp(s["posting_datetime"]).date() <= day(p["cutoff"]),
				"A future ledger row entered the source",
			)
			require(
				bool(s["voucher_type"]) and bool(s["voucher_no"]),
				"A ledger reference is missing",
			)
			sle_by_key.setdefault((s["item_code"], s["warehouse"]), []).append(s)
		for values in sle_by_key.values():
			values.sort(
				key=lambda s: (
					stamp(s["posting_datetime"]),
					stamp(s["creation"]),
					s["name"],
				)
			)
		sle_times = {
			key: [stamp(s["posting_datetime"]) for s in values] for key, values in sle_by_key.items()
		}
		require(
			all(parent["warehouse"] in leaves for parent in parents.values()),
			"A count is outside the selected warehouse scope",
			"unsupported_customisation",
		)
		count_pairs = {
			(line["item_code"], parents[line["parent"]]["warehouse"]) for line in rows["count_lines"]
		}
		reposts_by_pair = unsettled_reposts_by_pair(rows, count_pairs)
		evaluated = 0
		excluded_errors = []
		for name, parent in sorted(parents.items()):
			if parent["docstatus"] != 1:
				result["excluded_counts"].append(
					{
						"name": name,
						"reason": "Not a submitted physical count",
						"counted_at": parent["counted_at"],
					}
				)
				continue
			if stamp(parent["counted_at"]).date() > day(p["cutoff"]):
				result["excluded_counts"].append(
					{
						"name": name,
						"reason": "After the report cutoff",
						"counted_at": parent["counted_at"],
					}
				)
				continue
			try:
				require(
					day(p["start"]) <= stamp(parent["counted_at"]).date(),
					"A count precedes the selected review window",
				)
				parent_lines = lines[name]
				require(parent_lines, "A selected count has no complete lines")
				seen_items = set()
				for line in parent_lines:
					require(
						stamp(line["parent_modified"]) == stamp(parent["modified"]),
						"The count parent changed",
						"source_stale",
					)
					require(line["item_code"] not in seen_items, "A count repeats an item")
					seen_items.add(line["item_code"])
				when = stamp(parent["counted_at"])
				require(
					not any(
						repost_started_at(repost) <= when
						for line in parent_lines
						for repost in reposts_by_pair.get((line["item_code"], parent["warehouse"]), [])
					),
					"A stock valuation repost is unsettled for this count",
					"source_stale",
				)
				observations = []
				count_cross_references = []
				base = Decimal(0)
				for line in sorted(parent_lines, key=lambda x: (x["idx"], x["name"])):
					item = items.get(line["item_code"])
					require(item is not None, "A counted Item is missing")
					require(
						item["is_stock_item"]
						and not item["has_variants"]
						and not item["variant_of"]
						and not item["has_serial_no"]
						and not item["has_batch_no"],
						"Batch, serial, template or non-stock Items need a separate adapter",
						"unsupported_customisation",
					)
					require(
						line["stock_uom"] == item["stock_uom"] and bool(item["stock_uom"]),
						"Count and Item stock units differ",
					)
					require(
						line["batch_detail_recorded"] == 0,
						"A physical count with batch or serial detail needs a separate adapter",
						"unsupported_customisation",
					)
					require(
						number(line["counted_qty"]) >= 0,
						"A physical count line has an invalid quantity",
					)
					key = (line["item_code"], parent["warehouse"])
					values = sle_by_key.get(key, [])
					index = bisect_right(sle_times.get(key, []), when)
					last = values[index - 1] if index else None
					if index > 1:
						require(
							(
								stamp(values[index - 2]["posting_datetime"]),
								stamp(values[index - 2]["creation"]),
							)
							!= (
								stamp(last["posting_datetime"]),
								stamp(last["creation"]),
							),
							"The latest ledger order is ambiguous",
							"source_stale",
						)
					qty = number(last["qty_after_transaction"]) if last else Decimal(0)
					rate = number(last["valuation_rate"]) if last else Decimal(0)
					counted = number(line["counted_qty"])
					gap = counted - qty
					require(
						not gap or rate > 0,
						"A count difference has no positive book rate. Its value cannot be sized.",
						"record_coverage_insufficient",
					)
					if qty < 0:
						count_cross_references.append(
							{
								"owner": "negative-stock-valuation-auditor",
								"item_code": line["item_code"],
								"warehouse": parent["warehouse"],
								"ref_doctype": "Stock Ledger Entry",
								"ref_name": last["name"],
								"condition": "negative_book_quantity",
							}
						)
					amount = abs(gap) * max(rate, Decimal(0))
					base += abs(qty * rate)
					observations.append(
						{
							"row_name": line["name"],
							"item_code": line["item_code"],
							"warehouse": parent["warehouse"],
							"stock_uom": line["stock_uom"],
							"counted_qty": float(counted),
							"system_qty": float(qty),
							"book_rate": float(rate),
							"variance_qty": float(gap),
							"signed_value": _rounded(gap * rate),
							"amount": _rounded(amount),
							"direction": "short" if gap < 0 else "over" if gap > 0 else "matched",
							"book_sle": last["name"] if last else None,
							"_amount": amount,
						}
					)
				tolerance = max(
					Decimal(100),
					((base * Decimal(".005")) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP) * 100,
				)
				for x in observations:
					x["material"] = x.pop("_amount") > tolerance
				material = sorted(
					[x for x in observations if x["material"]],
					key=lambda x: (
						-number(x["amount"]),
						x["item_code"],
						x["warehouse"],
						x["row_name"],
					),
				)
				summary = {
					"name": name,
					"counted_at": parent["counted_at"],
					"count_time": when.isoformat(sep=" "),
					"lines": observations,
					"material_lines": len(material),
					"matched_lines": sum(x["variance_qty"] == 0 for x in observations),
					"below_threshold_lines": sum(
						x["variance_qty"] != 0 and not x["material"] for x in observations
					),
				}
				result["count_results"].append(summary)
				result["cross_references"].extend(count_cross_references)
				if material:
					result["findings"].append(
						{
							"check_id": "SV-VAR",
							"token": TOKEN,
							"ref_doctype": "Jarvis Physical Count",
							"ref_name": name,
							"result_class": "observed_fact",
							"severity": "warning",
							"amount": _rounded(sum((number(x["amount"]) for x in material), Decimal(0))),
							"lines": material,
							"line_count": len(material),
							"lines_sha256": sha(semantic(material)),
							"count_time": summary["count_time"],
							"title": f"{len(material)} stock count differences need review",
							"note": finding_note(material, summary["count_time"], result["currency"]),
						}
					)
				evaluated += 1
			except Gap as error:
				excluded_errors.append(error)
				result["excluded_counts"].append(
					{
						"name": name,
						"reason": str(error),
						"counted_at": parent["counted_at"],
					}
				)
		if excluded_errors:
			if evaluated:
				result["coverage"] = {TOKEN: {"state": "evaluated", "reason_code": None, "detail": None}}
				result["run_state"] = "partial"
				result["incomplete"] = incomplete_marker(excluded_errors)
			else:
				result["coverage"] = {TOKEN: partial_coverage(excluded_errors)}
				result["run_state"] = "not_evaluable"
		else:
			require(
				evaluated > 0,
				"No submitted physical count is available in the selected scope",
				"external_evidence_absent",
			)
			result["coverage"] = {TOKEN: {"state": "evaluated", "reason_code": None, "detail": None}}
			result["run_state"] = "evaluated_clean"
		result["query_hashes"] = {k: sha(q) for k, q in query_specs(scope, config).items()}
		result["source_hashes"] = {
			k: data.get("_byte_receipts", {}).get(k, {"semantic_sha256": sha(semantic(v))})
			for k, v in rows.items()
		}
		result["receipts_hash"] = data.get("_byte_receipts", {}).get(
			"source_receipts",
			{"semantic_sha256": sha(semantic(data["source_receipts"]))},
		)
		# The Jarvis Dashboard DocType has a 1,000,000-character cap. Check the
		# exact self-authored page before the agent can claim a clean result.
		from jarvis.chat.stock_variance_render import (
			INTEGRITY_DIGEST_PLACEHOLDER,
			MAX_DASHBOARD_CHARS,
			render,
		)

		preflight = dict(result, integrity_digest=INTEGRITY_DIGEST_PLACEHOLDER)
		if len(render(preflight)) > MAX_DASHBOARD_CHARS:
			raise Gap(
				"The selected physical-count report exceeds the safe dashboard size. Narrow the warehouse scope or dates.",
				"run_truncated_watermark",
				"truncated",
			)
	except (Gap, InvalidOperation, TypeError, KeyError) as e:
		reason = getattr(e, "reason", "record_coverage_insufficient")
		state = getattr(e, "state", "not_evaluable")
		result.update(
			findings=[],
			count_results=[],
			excluded_counts=[],
			cross_references=[],
			incomplete=None,
			run_state="partial" if state == "truncated" else "not_evaluable",
			truncated=state == "truncated",
			coverage={
				TOKEN: {
					"state": state,
					"reason_code": reason,
					"detail": str(e) if isinstance(e, Gap) else "A required native source is invalid",
				}
			},
		)
	result["integrity_digest"] = sha(result)
	return result
