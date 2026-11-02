"""Server-owned private inputs for the payroll deposit-status delegate.

The delegate receives one sealed snapshot. It cannot select a query, widen an
Account scope, or replace the launch configuration. Raw source rows exist only
in that private response. The Run stores only digests and count/watermark
receipts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from calendar import monthrange
from copy import deepcopy
from datetime import date
from decimal import Decimal

import frappe
from frappe.utils.response import json_handler

from jarvis.chat import coverage_reasons as cr
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import query as query_tool

AGENT = "statutory-payroll-deposit-status"
CONTRACT = "payroll_deposit_snapshot_v1"
RECEIPT_CONTRACT = "payroll_deposit_snapshot_receipt_v2"
LAUNCH_CONTRACT = "payroll_deposit_launch_v1"
WRITEBACK_CONTRACT = "payroll_deposit_writeback_v3"
RUN = "Jarvis Agent Run"

SOURCE_ORDER = (
	"accounts",
	"company",
	"journal_entries",
	"journal_lines",
	"journal_reversals",
	"payment_entries",
	"payment_deductions",
	"payment_taxes",
)
SOURCE_CAP = 200
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024

_DEPOSIT_KEYS = frozenset(
	{
		"account_group",
		"liability_accounts",
		"vouchers",
		"settlement_as_of",
		"reviewer",
		"reviewer_capacity",
		"test_declaration",
	}
)
_SCOPE_CONFIG_KEYS = ("company", "fiscal_year", "from_date", "to_date")
_MONTH = re.compile(r"^\d{4}-\d{2}$")
_WRITEBACK_TOKENS = frozenset({"pf_esi_deposit_due_date", "s7q_interest_table", "s14b_damages_table"})
_WRITEBACK_FINDING_KEYS = frozenset({"token", "ref_doctype", "ref_name"})
_MISSING = object()


def _fail(message: str = "Payroll deposit inputs cannot be captured safely.") -> None:
	raise InvalidArgumentError(message)


def canonical_json(value) -> str:
	"""Return the stable JSON representation used by all snapshot digests."""
	try:
		return json.dumps(
			value,
			default=json_handler,
			sort_keys=True,
			ensure_ascii=False,
			allow_nan=False,
			separators=(",", ":"),
		)
	except (TypeError, ValueError, OverflowError) as exc:
		raise InvalidArgumentError("Payroll deposit inputs cannot be serialized safely.") from exc


def canonical_digest(value) -> str:
	return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalise(value):
	"""Convert Frappe values to the same JSON values sent privately."""
	try:
		return json.loads(canonical_json(value))
	except (TypeError, ValueError) as exc:
		raise InvalidArgumentError("Payroll deposit inputs contain an unsupported value.") from exc


def _semantic(value):
	"""Normalise numbers without changing the actual private source values."""
	if isinstance(value, bool) or value is None:
		return value
	if isinstance(value, (int, float, Decimal)):
		try:
			return {"$numeric": format(Decimal(str(value)).normalize(), "f")}
		except Exception as exc:
			raise InvalidArgumentError("Payroll deposit inputs contain an invalid numeric value.") from exc
	if isinstance(value, dict):
		return {key: _semantic(item) for key, item in value.items()}
	if isinstance(value, list):
		return [_semantic(item) for item in value]
	return value


def semantic_digest(rows: list) -> str:
	return canonical_digest(_semantic(rows))


def _parse_json(value, message: str) -> dict:
	try:
		parsed = frappe.parse_json(value) if isinstance(value, str) else value
	except Exception as exc:
		raise InvalidArgumentError(message) from exc
	if not isinstance(parsed, dict):
		raise InvalidArgumentError(message)
	return _normalise(parsed)


def _date(value) -> date:
	try:
		return date.fromisoformat(str(value))
	except (TypeError, ValueError) as exc:
		raise InvalidArgumentError("Payroll deposit configuration has an invalid date.") from exc


def _valid_deposit(deposit) -> dict:
	if not isinstance(deposit, dict) or set(deposit) - _DEPOSIT_KEYS:
		_fail("Payroll deposit configuration is missing or contains unsupported fields.")
	value = _normalise(deposit)
	if (
		not isinstance(value.get("reviewer"), str)
		or not value["reviewer"].strip()
		or not value.get("reviewer_capacity")
	):
		_fail("Payroll deposit configuration requires a reviewer with capacity.")
	if not isinstance(value.get("account_group"), str) or not value["account_group"].strip():
		_fail("Payroll deposit configuration requires an Account group.")
	if not isinstance(value.get("settlement_as_of"), str) or not value["settlement_as_of"].strip():
		_fail("Payroll deposit configuration requires a booking evidence cutoff.")
	_date(value["settlement_as_of"])
	mapping = value.get("liability_accounts")
	if (
		not isinstance(mapping, dict)
		or not mapping
		or any(
			not isinstance(account, str) or not account.strip() or scheme not in {"pf", "esi"}
			for account, scheme in mapping.items()
		)
	):
		_fail("Payroll deposit configuration requires a PF or ESI Account mapping.")
	vouchers = value.get("vouchers")
	if not isinstance(vouchers, list) or not vouchers:
		_fail("Payroll deposit configuration requires declared vouchers.")
	seen = set()
	for voucher in vouchers:
		if (
			not isinstance(voucher, dict)
			or set(voucher) != {"doctype", "name", "liability_month"}
			or voucher.get("doctype") not in {"Journal Entry", "Payment Entry"}
			or not isinstance(voucher.get("name"), str)
			or not voucher["name"].strip()
			or not isinstance(voucher.get("liability_month"), str)
			or not _MONTH.fullmatch(voucher["liability_month"])
		):
			_fail("Payroll deposit configuration has an invalid declared voucher.")
		try:
			date.fromisoformat(voucher["liability_month"] + "-01")
		except ValueError:
			_fail("Payroll deposit configuration has an invalid declared voucher.")
		key = (voucher["doctype"], voucher["name"])
		if key in seen:
			_fail("Payroll deposit configuration repeats a declared voucher.")
		seen.add(key)
	return {"deposit": value}


def launch_config(installation) -> dict:
	"""Capture the only installation values this agent may use for a run."""
	try:
		full = frappe.parse_json(installation.get("config") or "{}")
	except Exception as exc:
		raise InvalidArgumentError("Payroll deposit configuration is invalid.") from exc
	if not isinstance(full, dict):
		_fail("Payroll deposit configuration is invalid.")
	config = _valid_deposit(full.get("deposit"))
	scope_config = {key: _normalise(full[key]) for key in _SCOPE_CONFIG_KEYS if key in full}
	return {
		"contract": LAUNCH_CONTRACT,
		"config": config,
		"config_digest": canonical_digest(config),
		"scope_config": scope_config,
		"scope_config_digest": canonical_digest(scope_config),
	}


def load_launch_config(value) -> dict:
	"""Read and verify the immutable launch configuration from the Run."""
	launch = _parse_json(value, "Payroll deposit launch configuration is missing or malformed.")
	if (
		set(launch)
		!= {
			"contract",
			"config",
			"config_digest",
			"scope_config",
			"scope_config_digest",
		}
		or launch.get("contract") != LAUNCH_CONTRACT
	):
		_fail("Payroll deposit launch configuration is missing or malformed.")
	config = _valid_deposit(launch.get("config", {}).get("deposit"))
	if launch.get("config") != config or not isinstance(launch.get("scope_config"), dict):
		_fail("Payroll deposit launch configuration is missing or malformed.")
	if not isinstance(launch.get("config_digest"), str) or not hmac.compare_digest(
		launch["config_digest"], canonical_digest(config)
	):
		_fail("Payroll deposit launch configuration changed after the run started.")
	if not isinstance(launch.get("scope_config_digest"), str) or not hmac.compare_digest(
		launch["scope_config_digest"], canonical_digest(launch["scope_config"])
	):
		_fail("Payroll deposit launch configuration changed after the run started.")
	return launch


def scope_config(launch: dict) -> dict:
	"""Return the frozen input for ``agent_scope.resolve_scope``."""
	return {**deepcopy(launch["scope_config"]), **deepcopy(launch["config"])}


def prompt_config(launch: dict) -> dict:
	"""Return only the frozen delegate config namespace, never installation data."""
	return deepcopy(launch["config"])


def _validate_scope(scope: dict, config: dict) -> None:
	if not isinstance(scope, dict) or any(
		not isinstance(scope.get(key), str) or not scope[key].strip()
		for key in ("company", "from_date", "to_date", "report_date")
	):
		_fail("Payroll deposit launch scope is missing or malformed.")
	deposit = config["deposit"]
	boundary = scope.get("record_scope")
	if boundary != {"doctype": "Account", "name": deposit["account_group"]}:
		_fail("Payroll deposit Account scope does not match the launch configuration.")
	start, end, report_date = (_date(scope[key]) for key in ("from_date", "to_date", "report_date"))
	month_end = date(start.year, start.month, monthrange(start.year, start.month)[1])
	if start.day != 1 or end != month_end or report_date < end:
		_fail("Payroll deposit launch scope is not one completed liability month.")
	if min(_date(deposit["settlement_as_of"]), report_date) < end:
		_fail("Payroll deposit booking evidence cutoff is before the liability month ends.")


def stamp_launch(run_name: str, launch: dict, scope: dict) -> None:
	"""Store a row-free receipt for the exact config and scope at launch."""
	launch = load_launch_config(launch)
	scope = _normalise(scope)
	_validate_scope(scope, launch["config"])
	current = frappe.db.get_value(RUN, run_name, "input_snapshot_json")
	if current:
		_fail("Payroll deposit input receipt already exists for this run.")
	receipt = {
		"contract": RECEIPT_CONTRACT,
		"launch": {
			"scope": scope,
			"scope_digest": canonical_digest(scope),
			"config_digest": launch["config_digest"],
			"scope_config_digest": launch["scope_config_digest"],
		},
		"source_snapshot": None,
	}
	frappe.db.set_value(RUN, run_name, "input_snapshot_json", canonical_json(receipt), update_modified=False)


def _load_receipt(run) -> tuple[dict, dict, dict]:
	launch = load_launch_config(run.get("assessment_config_json"))
	receipt = _parse_json(
		run.get("input_snapshot_json"), "Payroll deposit input receipt is missing or malformed."
	)
	if (
		set(receipt) != {"contract", "launch", "source_snapshot"}
		or receipt.get("contract") != RECEIPT_CONTRACT
	):
		_fail("Payroll deposit input receipt is missing or malformed.")
	launch_receipt = receipt.get("launch")
	if not isinstance(launch_receipt, dict) or set(launch_receipt) != {
		"scope",
		"scope_digest",
		"config_digest",
		"scope_config_digest",
	}:
		_fail("Payroll deposit input receipt is missing or malformed.")
	scope = launch_receipt.get("scope")
	if not isinstance(scope, dict):
		_fail("Payroll deposit input receipt is missing or malformed.")
	_validate_scope(scope, launch["config"])
	if not isinstance(launch_receipt.get("scope_digest"), str) or not hmac.compare_digest(
		launch_receipt["scope_digest"], canonical_digest(scope)
	):
		_fail("Payroll deposit launch scope changed after the run started.")
	for key in ("config_digest", "scope_config_digest"):
		if not isinstance(launch_receipt.get(key), str) or not hmac.compare_digest(
			launch_receipt[key], launch[key]
		):
			_fail("Payroll deposit launch configuration changed after the run started.")
	current_scope = _parse_json(run.get("scope_json"), "Payroll deposit run scope is missing or malformed.")
	if current_scope != scope:
		_fail("Payroll deposit run scope changed after the run started.")
	return launch, receipt, scope


_SOURCE_FIELDS = {
	"company": {"name", "default_currency"},
	"accounts": {"name", "company", "parent_account", "is_group", "disabled", "account_currency"},
	"journal_entries": {
		"name",
		"company",
		"posting_date",
		"docstatus",
		"voucher_type",
		"is_opening",
		"multi_currency",
		"reversal_of",
		"reference_present",
	},
	"journal_lines": {
		"name",
		"parent",
		"parenttype",
		"account",
		"account_currency",
		"debit",
		"credit",
	},
	"journal_reversals": {"name", "company", "posting_date", "docstatus", "reversal_of"},
	"payment_entries": {
		"name",
		"company",
		"posting_date",
		"docstatus",
		"payment_type",
		"paid_from",
		"paid_to",
		"paid_from_account_currency",
		"paid_to_account_currency",
		"paid_amount",
		"received_amount",
		"base_paid_amount",
		"base_received_amount",
		"difference_amount",
		"book_advance_payments_in_separate_party_account",
		"reference_present",
	},
	"payment_deductions": {"name", "parent"},
	"payment_taxes": {"name", "parent"},
}


def _condition(field, value, op="=") -> dict:
	return {"field": field, "op": op, "value": value}


def _reference_present(field: str) -> dict:
	return {
		"expr": {
			"fn": "case",
			"args": [
				{"when": _condition(field, "", "!="), "then": {"literal": 1}},
				{"else": {"literal": 0}},
			],
		},
		"as": "reference_present",
	}


def source_specs(scope: dict, config: dict) -> dict:
	"""Return the fixed server-owned query plan for all eight source datasets."""
	_validate_scope(scope, config)
	deposit = config["deposit"]
	company = scope["company"]
	root = deposit["account_group"]
	mapping = deposit["liability_accounts"]
	jes = [row["name"] for row in deposit["vouchers"] if row["doctype"] == "Journal Entry"] or [
		"__NO_DECLARED_JOURNAL__"
	]
	pes = [row["name"] for row in deposit["vouchers"] if row["doctype"] == "Payment Entry"] or [
		"__NO_DECLARED_PAYMENT__"
	]

	def add(doctype, alias, fields, where, joins=None):
		spec = {
			"from": doctype,
			"alias": alias,
			"select": [
				alias + "." + field if isinstance(field, str) and "." not in field else field
				for field in fields
			],
			"where": where,
			"order_by": [{"field": alias + ".name", "dir": "asc"}],
			"limit": SOURCE_CAP,
		}
		if joins:
			spec["joins"] = joins
		return spec

	return {
		"company": add("Company", "c", ["name", "default_currency"], [_condition("c.name", company)]),
		"accounts": add(
			"Account",
			"a",
			["name", "company", "parent_account", "is_group", "disabled", "account_currency"],
			[_condition("a.company", company), _condition("a.name", [root, *mapping], "in")],
		),
		"journal_entries": add(
			"Journal Entry",
			"j",
			[
				"name",
				"company",
				"posting_date",
				"docstatus",
				"voucher_type",
				"is_opening",
				"multi_currency",
				"reversal_of",
				_reference_present("j.cheque_no"),
			],
			[_condition("j.company", company), _condition("j.name", jes, "in")],
		),
		"journal_lines": add(
			"Journal Entry Account",
			"l",
			["name", "parent", "parenttype", "account", "account_currency", "debit", "credit"],
			[
				_condition("j.company", company),
				_condition("j.name", jes, "in"),
				_condition("l.parenttype", "Journal Entry"),
				_condition("l.account", list(mapping), "in"),
			],
			[{"type": "inner", "doctype": "Journal Entry", "alias": "j", "on": {"l.parent": "j.name"}}],
		),
		"journal_reversals": add(
			"Journal Entry",
			"j",
			["name", "company", "posting_date", "docstatus", "reversal_of"],
			[_condition("j.company", company), _condition("j.reversal_of", jes, "in")],
		),
		"payment_entries": add(
			"Payment Entry",
			"p",
			[
				"name",
				"company",
				"posting_date",
				"docstatus",
				"payment_type",
				"paid_from",
				"paid_to",
				"paid_from_account_currency",
				"paid_to_account_currency",
				"paid_amount",
				"received_amount",
				"base_paid_amount",
				"base_received_amount",
				"difference_amount",
				"book_advance_payments_in_separate_party_account",
				_reference_present("p.reference_no"),
			],
			[_condition("p.company", company), _condition("p.name", pes, "in")],
		),
		"payment_deductions": add(
			"Payment Entry Deduction",
			"d",
			["name", "parent"],
			[
				_condition("p.company", company),
				_condition("p.name", pes, "in"),
				_condition("d.parenttype", "Payment Entry"),
			],
			[{"type": "inner", "doctype": "Payment Entry", "alias": "p", "on": {"d.parent": "p.name"}}],
		),
		"payment_taxes": add(
			"Advance Taxes and Charges",
			"d",
			["name", "parent"],
			[
				_condition("p.company", company),
				_condition("p.name", pes, "in"),
				_condition("d.parenttype", "Payment Entry"),
			],
			[{"type": "inner", "doctype": "Payment Entry", "alias": "p", "on": {"d.parent": "p.name"}}],
		),
	}


def _aggregate_spec(spec: dict) -> dict:
	result = {
		"from": spec["from"],
		"alias": spec["alias"],
		"select": [
			{"agg": "count", "field": "*", "as": "count"},
			{"agg": "max", "field": spec["alias"] + ".modified", "as": "modified"},
		],
		"where": deepcopy(spec["where"]),
		"limit": 1,
	}
	if spec.get("joins"):
		result["joins"] = deepcopy(spec["joins"])
	return result


def _count_watermark(spec: dict) -> dict:
	try:
		result = query_tool._query_private_server(_aggregate_spec(spec), confirm_large=True)
		rows = result.get("rows") if isinstance(result, dict) else None
		if result.get("more_rows") or result.get("partial") or not isinstance(rows, list) or len(rows) != 1:
			_fail()
		row = _normalise(rows[0])
		if not isinstance(row, dict) or set(row) != {"count", "modified"}:
			_fail()
		count = row.get("count")
		if isinstance(count, bool) or not isinstance(count, (int, float, str, Decimal)):
			_fail()
		count_int = int(count)
		if count_int < 0 or Decimal(str(count)) != Decimal(count_int):
			_fail()
		return {"count": count_int, "modified": row.get("modified")}
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise InvalidArgumentError("Payroll deposit source receipt could not be read safely.") from exc


def _source_rows(key: str, spec: dict, expected_count: int) -> list:
	if expected_count > SOURCE_CAP:
		_fail("Payroll deposit source exceeds its fixed review cap.")
	try:
		result = query_tool._query_private_server(spec, confirm_large=True)
		rows = result.get("rows") if isinstance(result, dict) else None
		if result.get("more_rows") or result.get("partial") or not isinstance(rows, list):
			_fail()
		rows = _normalise(rows)
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise InvalidArgumentError("Payroll deposit source data could not be read safely.") from exc
	if len(rows) != expected_count:
		_fail("Payroll deposit source is incomplete.")
	return _validate_source_rows(key, rows)


def _validate_source_rows(key: str, rows: list) -> list:
	if not isinstance(rows, list) or len(rows) > SOURCE_CAP:
		_fail("Payroll deposit source is incomplete.")
	names = []
	for row in rows:
		if not isinstance(row, dict) or set(row) != _SOURCE_FIELDS[key]:
			_fail("Payroll deposit source has an invalid shape.")
		name = row.get("name")
		if not isinstance(name, str) or not name:
			_fail("Payroll deposit source has an invalid record identity.")
		names.append(name)
	if len(names) != len(set(names)):
		_fail("Payroll deposit source has repeated record identities.")
	return rows


def _validate_account_rows(rows: list, scope: dict, config: dict) -> None:
	deposit = config["deposit"]
	by_name = {row["name"]: row for row in rows}
	expected = {deposit["account_group"], *deposit["liability_accounts"]}
	if set(by_name) != expected:
		_fail("Payroll deposit Account scope is incomplete.")
	root = by_name[deposit["account_group"]]
	if (
		root.get("company") != scope["company"]
		or not frappe.utils.cint(root.get("is_group"))
		or frappe.utils.cint(root.get("disabled"))
	):
		_fail("Payroll deposit Account scope is invalid.")
	for account in deposit["liability_accounts"]:
		row = by_name[account]
		if (
			row.get("company") != scope["company"]
			or row.get("parent_account") != root["name"]
			or frappe.utils.cint(row.get("is_group"))
			or frappe.utils.cint(row.get("disabled"))
		):
			_fail("Payroll deposit liability Account scope is invalid.")


def _validate_declared_membership(sources: dict, config: dict) -> None:
	"""Reject a source row outside the launch-stamped voucher declaration."""
	vouchers = config["deposit"]["vouchers"]
	journals = {row["name"] for row in vouchers if row["doctype"] == "Journal Entry"}
	payments = {row["name"] for row in vouchers if row["doctype"] == "Payment Entry"}
	if not {row["name"] for row in sources["journal_entries"]} <= journals:
		_fail("Payroll deposit journal source is outside the declared voucher scope.")
	if not {row["name"] for row in sources["payment_entries"]} <= payments:
		_fail("Payroll deposit payment source is outside the declared voucher scope.")
	if not {row["parent"] for row in sources["journal_lines"]} <= journals:
		_fail("Payroll deposit journal-line source is outside the declared voucher scope.")
	if not {row["reversal_of"] for row in sources["journal_reversals"]} <= journals:
		_fail("Payroll deposit reversal source is outside the declared voucher scope.")
	for key in ("payment_deductions", "payment_taxes"):
		if not {row["parent"] for row in sources[key]} <= payments:
			_fail("Payroll deposit payment-detail source is outside the declared voucher scope.")


def _metadata(snapshot: dict) -> dict:
	return {
		"snapshot_id": snapshot["snapshot_id"],
		"snapshot_digest": snapshot["snapshot_digest"],
		"config_digest": snapshot["config_digest"],
		"scope_digest": canonical_digest(snapshot["scope"]),
		"source_receipts": deepcopy(snapshot["source_receipts"]),
	}


def _validate_snapshot_metadata(value) -> dict:
	if not isinstance(value, dict) or set(value) != {
		"snapshot_id",
		"snapshot_digest",
		"config_digest",
		"scope_digest",
		"source_receipts",
	}:
		_fail("Payroll deposit source receipt is malformed.")
	for key in ("snapshot_id", "snapshot_digest", "config_digest", "scope_digest"):
		if not isinstance(value.get(key), str) or not value[key]:
			_fail("Payroll deposit source receipt is malformed.")
	receipts = value.get("source_receipts")
	if not isinstance(receipts, list) or [
		row.get("source") if isinstance(row, dict) else None for row in receipts
	] != list(SOURCE_ORDER):
		_fail("Payroll deposit source receipt is malformed.")
	for row in receipts:
		if set(row) != {"source", "before", "data", "after"}:
			_fail("Payroll deposit source receipt is malformed.")
		for checkpoint in ("before", "after"):
			if not isinstance(row[checkpoint], dict) or set(row[checkpoint]) != {"count", "modified"}:
				_fail("Payroll deposit source receipt is malformed.")
			if (
				isinstance(row[checkpoint].get("count"), bool)
				or not isinstance(row[checkpoint].get("count"), int)
				or row[checkpoint]["count"] < 0
				or not isinstance(row[checkpoint].get("modified"), (str, type(None)))
			):
				_fail("Payroll deposit source receipt is malformed.")
		if not isinstance(row["data"], dict) or set(row["data"]) != {"rows", "semantic_sha256"}:
			_fail("Payroll deposit source receipt is malformed.")
		if (
			isinstance(row["data"].get("rows"), bool)
			or not isinstance(row["data"].get("rows"), int)
			or row["data"]["rows"] < 0
			or not isinstance(row["data"].get("semantic_sha256"), str)
			or not re.fullmatch(r"[0-9a-f]{64}", row["data"]["semantic_sha256"])
		):
			_fail("Payroll deposit source receipt is malformed.")
	return value


def validate_snapshot(snapshot) -> dict:
	"""Verify the complete private object before it crosses the sealed boundary."""
	if (
		not isinstance(snapshot, dict)
		or set(snapshot)
		!= {
			"contract",
			"snapshot_id",
			"snapshot_digest",
			"scope",
			"config",
			"config_digest",
			"sources",
			"source_receipts",
		}
		or snapshot.get("contract") != CONTRACT
	):
		_fail("Payroll deposit private snapshot is malformed.")
	if not isinstance(snapshot.get("snapshot_id"), str) or not snapshot["snapshot_id"]:
		_fail("Payroll deposit private snapshot is malformed.")
	config = _valid_deposit(snapshot.get("config", {}).get("deposit"))
	if snapshot.get("config") != config or not hmac.compare_digest(
		str(snapshot.get("config_digest") or ""), canonical_digest(config)
	):
		_fail("Payroll deposit private snapshot configuration is invalid.")
	_validate_scope(snapshot.get("scope"), config)
	sources = snapshot.get("sources")
	if not isinstance(sources, dict) or set(sources) != set(SOURCE_ORDER):
		_fail("Payroll deposit private snapshot sources are incomplete.")
	metadata = _validate_snapshot_metadata(
		{
			"snapshot_id": snapshot["snapshot_id"],
			"snapshot_digest": snapshot["snapshot_digest"],
			"config_digest": snapshot["config_digest"],
			"scope_digest": canonical_digest(snapshot["scope"]),
			"source_receipts": snapshot["source_receipts"],
		}
	)
	for receipt in metadata["source_receipts"]:
		key = receipt["source"]
		rows = sources.get(key)
		if not isinstance(rows, list) or receipt["before"] != receipt["after"]:
			_fail("Payroll deposit private source receipt changed during capture.")
		_validate_source_rows(key, rows)
		if receipt["before"].get("count") != len(rows) or receipt["data"].get("rows") != len(rows):
			_fail("Payroll deposit private source receipt is incomplete.")
		if not hmac.compare_digest(str(receipt["data"].get("semantic_sha256") or ""), semantic_digest(rows)):
			_fail("Payroll deposit private source receipt does not match its data.")
	_validate_account_rows(sources["accounts"], snapshot["scope"], config)
	_validate_declared_membership(sources, config)
	if len(sources["company"]) != 1 or sources["company"][0].get("name") != snapshot["scope"]["company"]:
		_fail("Payroll deposit private Company scope is incomplete.")
	without_digest = {key: value for key, value in snapshot.items() if key != "snapshot_digest"}
	if not hmac.compare_digest(str(snapshot.get("snapshot_digest") or ""), canonical_digest(without_digest)):
		_fail("Payroll deposit private snapshot digest is invalid.")
	return snapshot


def capture(run) -> dict:
	"""Read all eight fixed sources and return the sealed private snapshot."""
	if run.get("agent") != AGENT or run.get("status") != "running":
		_fail("Payroll deposit inputs require its active delegate run.")
	launch, receipt, scope = _load_receipt(run)
	if receipt.get("source_snapshot") is not None:
		_fail("Payroll deposit inputs were already captured for this run.")
	specs = source_specs(scope, launch["config"])
	if set(specs) != set(SOURCE_ORDER):
		_fail()
	sources, receipts = {}, []
	for key in SOURCE_ORDER:
		spec = specs[key]
		before = _count_watermark(spec)
		rows = _source_rows(key, spec, before["count"])
		after = _count_watermark(spec)
		if before != after:
			_fail("Payroll deposit sources changed while the snapshot was captured.")
		sources[key] = rows
		receipts.append(
			{
				"source": key,
				"before": before,
				"data": {"rows": len(rows), "semantic_sha256": semantic_digest(rows)},
				"after": after,
			}
		)
	_validate_account_rows(sources["accounts"], scope, launch["config"])
	_validate_declared_membership(sources, launch["config"])
	if len(sources["company"]) != 1 or sources["company"][0].get("name") != scope["company"]:
		_fail("Payroll deposit Company scope is incomplete.")
	snapshot = {
		"contract": CONTRACT,
		"snapshot_id": frappe.generate_hash(length=32),
		"scope": deepcopy(scope),
		"config": deepcopy(launch["config"]),
		"config_digest": launch["config_digest"],
		"sources": sources,
		"source_receipts": receipts,
	}
	snapshot["snapshot_digest"] = canonical_digest(snapshot)
	validate_snapshot(snapshot)
	if len(canonical_json(snapshot).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
		_fail("Payroll deposit private snapshot exceeds its byte cap.")
	metadata = _metadata(snapshot)
	_validate_snapshot_metadata(metadata)
	receipt["source_snapshot"] = metadata
	# This is metadata only. Raw rows never enter the Run, a step, an audit row,
	# or a transcript.
	frappe.db.set_value(RUN, run.name, "input_snapshot_json", canonical_json(receipt), update_modified=False)
	return snapshot


def _declared_voucher_refs(launch: dict) -> set[tuple[str, str]]:
	return {(row["doctype"], row["name"]) for row in launch["config"]["deposit"]["vouchers"]}


def _safe_findings(findings, launch: dict) -> list[dict]:
	"""Project compact Deposit references into fixed server-owned findings.

	The bundle may identify a declared voucher and token, but it must never place
	row values or authored explanation text into a durable record.  This routine
	accepts no extensible fields and builds every persisted text field itself.
	"""
	if not isinstance(findings, list):
		_fail("Payroll deposit writeback findings are malformed.")
	declared = _declared_voucher_refs(launch)
	seen = set()
	projected = []
	for row in findings:
		if not isinstance(row, dict) or set(row) != _WRITEBACK_FINDING_KEYS:
			_fail("Payroll deposit writeback findings are malformed.")
		token = row.get("token")
		ref_doctype = row.get("ref_doctype")
		ref_name = row.get("ref_name")
		if (
			token != "pf_esi_deposit_due_date"
			or not isinstance(ref_doctype, str)
			or not isinstance(ref_name, str)
			or not ref_name
			or (ref_doctype, ref_name) not in declared
		):
			_fail("Payroll deposit writeback findings are outside the sealed voucher scope.")
		ref = (ref_doctype, ref_name)
		if ref in seen:
			_fail("Payroll deposit writeback repeats a declared voucher reference.")
		seen.add(ref)
		projected.append(
			{
				"token": token,
				"ref_doctype": ref_doctype,
				"ref_name": ref_name,
				"amount": None,
				"severity": "warning",
				"note": "Potential payroll deposit timing issue. Review the declared voucher and payment evidence.",
				"result_class": "derived_candidate",
				"confidence": "low",
				"match_basis": "Reference was checked against the sealed payroll deposit input.",
				"false_positive_path": "Check the declared voucher, payment evidence, and an approved exception.",
			}
		)
	# The evaluator has no ordering authority.  A semantically identical result
	# therefore receives the same server receipt even when the worker emits its
	# references in a different order.
	return sorted(projected, key=lambda row: (row["token"], row["ref_doctype"], row["ref_name"]))


def _safe_coverage(coverage) -> dict:
	"""Validate the compact closed coverage grammar without accepting detail text."""
	if not isinstance(coverage, dict) or set(coverage) != _WRITEBACK_TOKENS:
		_fail("Payroll deposit writeback coverage is malformed.")
	projected = {}
	for token in _WRITEBACK_TOKENS:
		entry = coverage.get(token)
		if not isinstance(entry, dict):
			_fail("Payroll deposit writeback coverage is malformed.")
		state = entry.get("state")
		if not isinstance(state, str):
			_fail("Payroll deposit writeback coverage is malformed.")
		if state == "evaluated" and set(entry) == {"state"}:
			projected[token] = {"state": state}
			continue
		reason_code = entry.get("reason_code")
		if (
			state in {"not_evaluable", "truncated"}
			and set(entry) == {"state", "reason_code"}
			and isinstance(reason_code, str)
			and reason_code in cr.REASON_CODES
		):
			projected[token] = {"state": state, "reason_code": reason_code}
			continue
		_fail("Payroll deposit writeback coverage is malformed.")
	return projected


def validate_writeback(
	run,
	*,
	findings,
	coverage,
	scope,
	truncated,
	rows_consumed,
	input_snapshot_id,
	input_snapshot_digest,
	integrity_digest: str | None = _MISSING,
	dashboard=None,
	report_sha256=None,
	canvas_ref=None,
) -> dict:
	"""Validate and safely project the Deposit v3 handoff.

	There is no delegate dashboard in this contract.  A generic server fallback
	dashboard is rendered only after this function has accepted the compact,
	row-free projection and the writeback has persisted it.  V3 accepts exactly
	the seven compact handoff fields.  A delegate integrity digest is rejected;
	the server creates the only persisted digest.
	"""
	if run.get("agent") != AGENT or run.get("status") != "running":
		_fail("Payroll deposit writeback requires its active delegate run.")
	if dashboard is not None or report_sha256 is not None or canvas_ref is not None:
		_fail("Payroll deposit writeback does not accept delegate dashboard output.")
	launch, receipt, launch_scope = _load_receipt(run)
	metadata = _validate_snapshot_metadata(receipt.get("source_snapshot"))
	if (
		not isinstance(input_snapshot_id, str)
		or not isinstance(input_snapshot_digest, str)
		or not hmac.compare_digest(input_snapshot_id, metadata["snapshot_id"])
		or not hmac.compare_digest(input_snapshot_digest, metadata["snapshot_digest"])
	):
		_fail("Payroll deposit writeback does not match the server input snapshot.")
	if scope != launch_scope:
		_fail("Payroll deposit writeback scope does not match the launch scope.")
	if not hmac.compare_digest(metadata["config_digest"], launch["config_digest"]) or not hmac.compare_digest(
		metadata["scope_digest"], canonical_digest(launch_scope)
	):
		_fail("Payroll deposit input receipt changed after capture.")
	if type(truncated) is not bool:
		_fail("Payroll deposit writeback truncation state is malformed.")
	if isinstance(rows_consumed, bool) or not isinstance(rows_consumed, int):
		_fail("Payroll deposit writeback row count is malformed.")
	expected_rows = sum(row["data"]["rows"] for row in metadata["source_receipts"])
	if rows_consumed != expected_rows:
		_fail("Payroll deposit writeback row count does not match the sealed input.")
	if integrity_digest is not _MISSING:
		_fail("Payroll deposit writeback does not accept a delegate integrity digest.")
	safe_findings = _safe_findings(findings, launch)
	safe_coverage = _safe_coverage(coverage)
	safe_scope = deepcopy(launch_scope)
	server_integrity_digest = canonical_digest(
		{
			"contract": WRITEBACK_CONTRACT,
			"input_snapshot_id": metadata["snapshot_id"],
			"input_snapshot_digest": metadata["snapshot_digest"],
			"scope": safe_scope,
			"findings": safe_findings,
			"coverage": safe_coverage,
			"truncated": truncated,
			"rows_consumed": expected_rows,
		}
	)
	return {
		"findings": safe_findings,
		"coverage": safe_coverage,
		"scope": safe_scope,
		"truncated": truncated,
		"rows_consumed": expected_rows,
		"integrity_digest": server_integrity_digest,
	}
