"""Server-owned Negative Stock trust receipts for the future Reorder gate.

The Negative Stock bundle owns its grade rules. This module does not recreate
them and does not read a dashboard. It turns a completed clean Negative Stock
run into a small immutable server receipt. A future native Reorder backend can
consume it only when the same server-stamped company, Warehouse root, effective
as-of date, run-as identity, permission profile, and source state still match.

The receipt contains hashes and counts only. It is not an inventory report and
it must never become a shared-chat data channel.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import frappe
from frappe.utils import get_datetime

AGENT = "negative-stock-valuation-auditor"
REORDER_AGENT = "reorder-replenishment-operator"
GRADE_TOKEN = "nsv-grade-e058"
REQUIRED_TOKENS = frozenset(
	{
		"nsv-grade-e058",
		"nsv-integ-2a6f",
		"nsv-neg-2e73",
		"nsv-spike-4b1c",
		"nsv-tieout-7d92",
		"nsv-zero-8c45",
	}
)
RECEIPT_KEY = "negative_stock_trust_receipt"
RECEIPT_SCHEMA = "negative-stock-trust-receipt-v1"
SOURCE_SCHEMA = "negative-stock-source-receipt-v1"
SUPPORTED_BUNDLE_VERSION = "0.3.1"
RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"
SESSION = "Jarvis Chat Session"
MAX_SOURCE_ROWS = 20_000


class ReceiptRefused(Exception):
	"""The narrow Reorder gate has no trusted matching receipt."""


def _digest(value) -> str:
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
	).hexdigest()


def _object(value, label: str) -> dict:
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (TypeError, ValueError) as exc:
			raise ReceiptRefused(f"{label} is not a JSON object") from exc
	if not isinstance(value, dict):
		raise ReceiptRefused(f"{label} is not a JSON object")
	return value


def _date(value, label: str) -> str:
	if not isinstance(value, str):
		raise ReceiptRefused(f"{label} is missing")
	try:
		parsed = date.fromisoformat(value)
	except ValueError as exc:
		raise ReceiptRefused(f"{label} is invalid") from exc
	if parsed.isoformat() != value:
		raise ReceiptRefused(f"{label} is invalid")
	return value


def _scope(value) -> dict:
	"""Project only the server dimensions that can bind a Reorder gate."""
	scope = _object(value, "run scope")
	company = scope.get("company")
	if not isinstance(company, str) or not company.strip():
		raise ReceiptRefused("run scope has no company")
	root = scope.get("record_scope")
	if (
		not isinstance(root, dict)
		or set(root) != {"doctype", "name"}
		or root.get("doctype") != "Warehouse"
		or not isinstance(root.get("name"), str)
		or not root["name"].strip()
	):
		raise ReceiptRefused("run scope has no server-bound Warehouse root")
	from_date = _date(scope.get("from_date"), "run from date")
	to_date = _date(scope.get("to_date"), "run to date")
	report_date = _date(scope.get("report_date"), "run report date")
	effective_as_of = min(to_date, report_date)
	if from_date > effective_as_of:
		raise ReceiptRefused("run date range is invalid")
	return {
		"company": company.strip(),
		"warehouse_root": root["name"].strip(),
		"from_date": from_date,
		"to_date": to_date,
		"report_date": report_date,
		"effective_as_of": effective_as_of,
	}


def _stored_scope(value) -> dict:
	"""Validate the compact scope form stored inside a receipt."""
	scope = _object(value, "receipt scope")
	if set(scope) != {"company", "warehouse_root", "from_date", "to_date", "report_date", "effective_as_of"}:
		raise ReceiptRefused("receipt scope is invalid")
	company = scope.get("company")
	root = scope.get("warehouse_root")
	if not isinstance(company, str) or not company.strip() or not isinstance(root, str) or not root.strip():
		raise ReceiptRefused("receipt scope is invalid")
	from_date = _date(scope.get("from_date"), "receipt from date")
	to_date = _date(scope.get("to_date"), "receipt to date")
	report_date = _date(scope.get("report_date"), "receipt report date")
	effective_as_of = _date(scope.get("effective_as_of"), "receipt effective as-of date")
	if from_date > effective_as_of or effective_as_of != min(to_date, report_date):
		raise ReceiptRefused("receipt scope is invalid")
	return {
		"company": company.strip(),
		"warehouse_root": root.strip(),
		"from_date": from_date,
		"to_date": to_date,
		"report_date": report_date,
		"effective_as_of": effective_as_of,
	}


def _profile_fingerprint(value) -> str:
	profile = _object(value, "permission profile")
	if not isinstance(profile.get("hash"), str) or len(profile["hash"]) != 64:
		raise ReceiptRefused("permission profile is incomplete")
	return _digest(profile)


def _user_fingerprint(user) -> str:
	if not isinstance(user, str) or not user.strip() or user == "Guest":
		raise ReceiptRefused("run-as identity is unavailable")
	return _digest({"run_as_user": user.strip()})


def _session_user(run) -> str:
	key = run.get("session_key")
	if not isinstance(key, str) or not key:
		raise ReceiptRefused("run session is unavailable")
	user = frappe.db.get_value(SESSION, key, "user")
	if not isinstance(user, str) or not user.strip() or user == "Guest":
		raise ReceiptRefused("run-as identity is unavailable")
	return user.strip()


def _live_profile_fingerprint(user: str) -> str:
	"""Use the scheduler's one profile implementation; do not recreate it here."""
	from jarvis.chat.agent_scheduler import _permission_profile

	return _profile_fingerprint(_permission_profile(user))


def _config_fingerprint(run) -> str:
	"""Require #2338's immutable delivered engagement snapshot.

	The launch message receives this exact object. Requiring the snapshot makes a
	missing deployment or a mutable-installation fallback hold Reorder instead of
	turning an unproven configuration into a clean receipt.
	"""
	config = _object(run.get("engagement_config_json"), "Negative Stock engagement configuration")
	namespace = config.get("negative_stock")
	if not isinstance(namespace, dict) or "record_scope" in namespace:
		raise ReceiptRefused("Negative Stock engagement configuration is not safely delivered")
	return _digest(config)


def capture_launch(run) -> dict:
	"""Capture launch-only facts before terminal writeback tears down the session."""
	if run.get("agent") != AGENT:
		raise ReceiptRefused("not a Negative Stock run")
	user = _session_user(run)
	profile = _profile_fingerprint(run.get("permission_profile"))
	if _live_profile_fingerprint(user) != profile:
		raise ReceiptRefused("run-as permission profile changed before writeback")
	return {
		"run": run.name,
		"scope": _scope(run.get("scope_json")),
		"run_as_user": user,
		"run_as_fingerprint": _user_fingerprint(user),
		"permission_profile_fingerprint": profile,
		"engagement_config_fingerprint": _config_fingerprint(run),
	}


def _exact_tokens(value, expected: frozenset[str], label: str) -> None:
	if not isinstance(value, list) or any(not isinstance(token, str) for token in value):
		raise ReceiptRefused(f"{label} is incomplete")
	if len(value) != len(expected) or set(value) != expected:
		raise ReceiptRefused(f"{label} does not match the supported Negative Stock bundle")


def _complete(run) -> None:
	if run.get("agent") != AGENT:
		raise ReceiptRefused("receipt is not from Negative Stock")
	if run.get("status") != "completed" or run.get("result_state") != "evaluated_clean":
		raise ReceiptRefused("Negative Stock run was not complete and clean")
	if run.get("bundle_version") != SUPPORTED_BUNDLE_VERSION:
		raise ReceiptRefused("Negative Stock bundle version is not supported by this gate")
	if not run.get("started_at") or not run.get("finished_at"):
		raise ReceiptRefused("Negative Stock run timing is incomplete")
	coverage = _object(run.get("coverage_json"), "Negative Stock coverage")
	if (
		coverage.get("result_state") != "evaluated_clean"
		or coverage.get("truncated") is not False
		or coverage.get("watermark_drift") is not False
		or coverage.get("scoped_visibility") is not False
		or coverage.get("row_shortfall") is not False
		or coverage.get("dropped") != []
		or coverage.get("not_evaluable") != []
		or coverage.get("not_evaluable_scoped") != []
	):
		raise ReceiptRefused("Negative Stock coverage is incomplete")
	_exact_tokens(coverage.get("required_tokens"), REQUIRED_TOKENS, "Negative Stock required coverage")
	entries = coverage.get("coverage")
	if not isinstance(entries, dict) or set(entries) != REQUIRED_TOKENS:
		raise ReceiptRefused("Negative Stock coverage entries are incomplete")
	if any(
		not isinstance(entries[token], dict) or entries[token].get("state") != "evaluated"
		for token in REQUIRED_TOKENS
	):
		raise ReceiptRefused("Negative Stock coverage entries are not all evaluated")
	if frappe.db.exists(
		FINDING,
		{"agent": AGENT, "rule_id": GRADE_TOKEN, "last_seen_run": run.name},
	):
		raise ReceiptRefused("Negative Stock grade did not clear")
	integrity = run.get("integrity_digest")
	if not isinstance(integrity, str) or len(integrity) != 64:
		raise ReceiptRefused("Negative Stock integrity digest is unavailable")
	try:
		int(integrity, 16)
	except ValueError as exc:
		raise ReceiptRefused("Negative Stock integrity digest is invalid") from exc


def _rows(label: str, query: str, values=()) -> list[dict]:
	"""Read identity + modification time only, with a hard complete-source bound."""
	rows = frappe.db.sql(query, values, as_dict=True)
	if len(rows) > MAX_SOURCE_ROWS:
		raise ReceiptRefused(f"{label} source is too large for a trust receipt")
	result = []
	for row in rows:
		name, modified = row.get("name"), row.get("modified")
		if not isinstance(name, str) or not name or modified is None:
			raise ReceiptRefused(f"{label} source identity is incomplete")
		result.append({"name": name, "modified": str(modified)})
	return result


def _marks_stable(rows: list[dict], started_at) -> None:
	try:
		started = get_datetime(started_at)
	except Exception as exc:
		raise ReceiptRefused("Negative Stock run start time is invalid") from exc
	for row in rows:
		try:
			modified = get_datetime(row["modified"])
		except Exception as exc:
			raise ReceiptRefused("source modification time is invalid") from exc
		# Equal timestamps are unsafe. MariaDB visible timestamp precision can hide an
		# update made after the run started in the same tick.
		if modified >= started:
			raise ReceiptRefused("source changed during or after the Negative Stock run")


def _source_state(scope: dict, started_at) -> dict:
	"""Hash complete source identity without storing source rows.

	Every family is a superset of the Negative Stock evaluator's selected rows.
	An unrelated company change can hold Reorder. That is intentional: a new audit
	is safer than a proposal based on a mixed snapshot.
	"""
	root = frappe.db.get_value(
		"Warehouse", scope["warehouse_root"], ["name", "company", "lft", "rgt"], as_dict=True
	)
	if (
		not root
		or root.get("company") != scope["company"]
		or root.get("lft") is None
		or root.get("rgt") is None
	):
		raise ReceiptRefused("server-bound Warehouse root no longer matches the company")
	company = scope["company"]
	as_of = scope["effective_as_of"]
	tables = {
		"Account": _rows(
			"Account",
			"select name, modified from `tabAccount` where company=%s order by name asc limit %s",
			(company, MAX_SOURCE_ROWS + 1),
		),
		"Bin": _rows(
			"Bin",
			"""select b.name, b.modified from `tabBin` b
			   inner join `tabWarehouse` w on w.name=b.warehouse
			   where w.company=%s order by b.name asc limit %s""",
			(company, MAX_SOURCE_ROWS + 1),
		),
		"Company": _rows(
			"Company",
			"select name, modified from `tabCompany` where name=%s order by name asc limit %s",
			(company, 2),
		),
		"GL Entry": _rows(
			"GL Entry",
			"""select name, modified from `tabGL Entry`
			   where company=%s and posting_date <= %s order by name asc limit %s""",
			(company, as_of, MAX_SOURCE_ROWS + 1),
		),
		"Item": _rows(
			"Item", "select name, modified from `tabItem` order by name asc limit %s", (MAX_SOURCE_ROWS + 1,)
		),
		"Landed Cost Purchase Receipt": _rows(
			"Landed Cost Purchase Receipt",
			"""select r.name, r.modified from `tabLanded Cost Purchase Receipt` r
			   inner join `tabLanded Cost Voucher` p on p.name=r.parent
			   where p.company=%s order by r.name asc limit %s""",
			(company, MAX_SOURCE_ROWS + 1),
		),
		"Landed Cost Voucher": _rows(
			"Landed Cost Voucher",
			"select name, modified from `tabLanded Cost Voucher` where company=%s order by name asc limit %s",
			(company, MAX_SOURCE_ROWS + 1),
		),
		"Repost Item Valuation": _rows(
			"Repost Item Valuation",
			"select name, modified from `tabRepost Item Valuation` where company=%s order by name asc limit %s",
			(company, MAX_SOURCE_ROWS + 1),
		),
		"Stock Entry": _rows(
			"Stock Entry",
			"select name, modified from `tabStock Entry` where company=%s order by name asc limit %s",
			(company, MAX_SOURCE_ROWS + 1),
		),
		"Stock Ledger Entry": _rows(
			"Stock Ledger Entry",
			"""select name, modified from `tabStock Ledger Entry`
			   where company=%s and posting_date <= %s order by name asc limit %s""",
			(company, as_of, MAX_SOURCE_ROWS + 1),
		),
		"Stock Settings": _rows(
			"Stock Settings",
			"select name, modified from `tabStock Settings` order by name asc limit %s",
			(2,),
		),
		"Warehouse": _rows(
			"Warehouse",
			"select name, modified from `tabWarehouse` where company=%s order by name asc limit %s",
			(company, MAX_SOURCE_ROWS + 1),
		),
	}
	if len(tables["Company"]) != 1 or len(tables["Stock Settings"]) != 1:
		raise ReceiptRefused("required Negative Stock source is unavailable")

	total = sum(len(rows) for rows in tables.values())
	if total > MAX_SOURCE_ROWS:
		raise ReceiptRefused("Negative Stock source is too large for a trust receipt")
	for rows in tables.values():
		_marks_stable(rows, started_at)
	state = {
		"schema": SOURCE_SCHEMA,
		"scope": scope,
		"tables": {
			name: {"count": len(rows), "digest": _digest(rows)} for name, rows in sorted(tables.items())
		},
	}
	state["digest"] = _digest(state)
	return state


def _receipt_body(run, capture: dict, source: dict) -> dict:
	return {
		"schema": RECEIPT_SCHEMA,
		"negative_run": run.name,
		"negative_bundle_version": run.get("bundle_version"),
		"negative_integrity_digest": run.get("integrity_digest"),
		"negative_engagement_config_digest": capture["engagement_config_fingerprint"],
		"scope": capture["scope"],
		"run_as_fingerprint": capture["run_as_fingerprint"],
		"permission_profile_fingerprint": capture["permission_profile_fingerprint"],
		"source": source,
	}


_RECEIPT_BODY_KEYS = frozenset(
	{
		"schema",
		"negative_run",
		"negative_bundle_version",
		"negative_integrity_digest",
		"negative_engagement_config_digest",
		"scope",
		"run_as_fingerprint",
		"permission_profile_fingerprint",
		"source",
	}
)


def _receipt(value) -> dict:
	payload = _object(value, "Negative Stock receipt")
	if set(payload) != {RECEIPT_KEY}:
		raise ReceiptRefused("Negative Stock receipt storage is invalid")
	receipt = payload[RECEIPT_KEY]
	if not isinstance(receipt, dict):
		raise ReceiptRefused("Negative Stock receipt is invalid")
	provided = receipt.get("digest")
	body = {key: value for key, value in receipt.items() if key != "digest"}
	if (
		set(body) != _RECEIPT_BODY_KEYS
		or receipt.get("schema") != RECEIPT_SCHEMA
		or not isinstance(provided, str)
		or provided != _digest(body)
	):
		raise ReceiptRefused("Negative Stock receipt integrity check failed")
	if not isinstance(receipt.get("negative_run"), str) or not receipt["negative_run"]:
		raise ReceiptRefused("Negative Stock receipt run identity is invalid")
	if receipt.get("negative_bundle_version") != SUPPORTED_BUNDLE_VERSION:
		raise ReceiptRefused("Negative Stock receipt bundle identity is invalid")
	if (
		not isinstance(receipt.get("negative_integrity_digest"), str)
		or len(receipt["negative_integrity_digest"]) != 64
	):
		raise ReceiptRefused("Negative Stock receipt evaluator identity is invalid")
	if not all(
		isinstance(receipt.get(key), str) and len(receipt[key]) == 64
		for key in (
			"negative_engagement_config_digest",
			"run_as_fingerprint",
			"permission_profile_fingerprint",
		)
	):
		raise ReceiptRefused("Negative Stock receipt identity is invalid")
	if _stored_scope(receipt.get("scope")) != receipt.get("scope"):
		raise ReceiptRefused("Negative Stock receipt scope is invalid")
	source = receipt.get("source")
	if (
		not isinstance(source, dict)
		or source.get("schema") != SOURCE_SCHEMA
		or source.get("scope") != receipt["scope"]
	):
		raise ReceiptRefused("Negative Stock receipt source is invalid")
	source_digest = source.get("digest")
	source_body = {key: item for key, item in source.items() if key != "digest"}
	if not isinstance(source_digest, str) or source_digest != _digest(source_body):
		raise ReceiptRefused("Negative Stock receipt source integrity check failed")
	return receipt


def _capture(capture: dict) -> dict:
	if not isinstance(capture, dict):
		raise ReceiptRefused("Negative Stock launch capture is invalid")
	if not isinstance(capture.get("run"), str) or not capture["run"]:
		raise ReceiptRefused("Negative Stock launch capture is invalid")
	if _stored_scope(capture.get("scope")) != capture.get("scope"):
		raise ReceiptRefused("Negative Stock launch scope is invalid")
	user = capture.get("run_as_user")
	if _user_fingerprint(user) != capture.get("run_as_fingerprint"):
		raise ReceiptRefused("Negative Stock launch identity is invalid")
	if not all(
		isinstance(capture.get(key), str) and len(capture[key]) == 64
		for key in ("permission_profile_fingerprint", "engagement_config_fingerprint")
	):
		raise ReceiptRefused("Negative Stock launch capture is invalid")
	return capture


def persist(capture: dict) -> dict | None:
	"""Persist one immutable receipt after the normal negative-run finalization.

	No receipt is safer than a weak receipt. A refusal does not change the audit
	result. It only keeps the future Reorder stage unavailable.
	"""
	try:
		capture = _capture(capture)
		locked = frappe.db.get_value(
			RUN, capture["run"], ["name", "assessment_output_json"], as_dict=True, for_update=True
		)
		if not locked:
			raise ReceiptRefused("Negative Stock run no longer exists")
		run = frappe.get_doc(RUN, capture["run"])
		_complete(run)
		if _scope(run.get("scope_json")) != capture["scope"]:
			raise ReceiptRefused("run scope changed after launch")
		if _profile_fingerprint(run.get("permission_profile")) != capture["permission_profile_fingerprint"]:
			raise ReceiptRefused("run permission profile changed after launch")
		if _config_fingerprint(run) != capture["engagement_config_fingerprint"]:
			raise ReceiptRefused("run engagement configuration changed after launch")
		if _live_profile_fingerprint(capture["run_as_user"]) != capture["permission_profile_fingerprint"]:
			raise ReceiptRefused("run-as permission profile changed during the Negative Stock run")
		source = _source_state(capture["scope"], run.get("started_at"))
		body = _receipt_body(run, capture, source)
		receipt = {**body, "digest": _digest(body)}
		existing = locked.get("assessment_output_json")
		if existing:
			# The row lock makes this a compare-or-set operation. A retry can observe
			# identical bytes, but no second path can replace a terminal receipt.
			if _receipt(existing) != receipt:
				raise ReceiptRefused("an immutable receipt already exists for this run")
			return receipt
		frappe.db.set_value(
			RUN,
			run.name,
			"assessment_output_json",
			frappe.as_json({RECEIPT_KEY: receipt}),
			update_modified=False,
		)
		return receipt
	except (ReceiptRefused, frappe.DoesNotExistError, frappe.ValidationError):
		return None
	except Exception:
		# A receipt error must never turn a useful Negative Stock audit into a failed
		# run. It remains unavailable for Reorder until a later clean run yields proof.
		frappe.logger("jarvis.negative_stock_receipt").warning(
			{
				"event": "negative_stock_receipt_refused",
				"run": capture.get("run") if isinstance(capture, dict) else None,
			},
			exc_info=True,
		)
		return None


def verify_for_reorder(negative_run_name: str, reorder_run) -> dict:
	"""Verify every cross-run precondition before a future native Reorder action.

	This internal function accepts no receipt bytes, company, date, or Warehouse
	arguments from a delegate. It loads the immutable receipt bytes from the
	Negative Stock Run, then reads the Reorder Run's server-stamped scope itself.
	"""
	if reorder_run.get("agent") != REORDER_AGENT or reorder_run.get("status") != "running":
		raise ReceiptRefused("Reorder run is not an active server-bound run")
	if not isinstance(negative_run_name, str) or not negative_run_name:
		raise ReceiptRefused("Negative Stock run identity is unavailable")
	reorder_scope = _scope(reorder_run.get("scope_json"))
	try:
		negative = frappe.get_doc(RUN, negative_run_name)
	except frappe.DoesNotExistError as exc:
		raise ReceiptRefused("Negative Stock run no longer exists") from exc
	receipt = _receipt(frappe.db.get_value(RUN, negative.name, "assessment_output_json"))
	if receipt["negative_run"] != negative.name or receipt["scope"] != reorder_scope:
		raise ReceiptRefused("Negative Stock receipt does not match the Reorder scope")
	_complete(negative)
	if _scope(negative.get("scope_json")) != reorder_scope:
		raise ReceiptRefused("Negative Stock run scope no longer matches its receipt")
	if negative.get("bundle_version") != receipt["negative_bundle_version"]:
		raise ReceiptRefused("Negative Stock bundle identity changed")
	if negative.get("integrity_digest") != receipt["negative_integrity_digest"]:
		raise ReceiptRefused("Negative Stock integrity identity changed")
	if _config_fingerprint(negative) != receipt["negative_engagement_config_digest"]:
		raise ReceiptRefused("Negative Stock engagement configuration identity changed")

	reorder_user = _session_user(reorder_run)
	reorder_identity = _user_fingerprint(reorder_user)
	reorder_profile = _profile_fingerprint(reorder_run.get("permission_profile"))
	if (
		reorder_identity != receipt["run_as_fingerprint"]
		or reorder_profile != receipt["permission_profile_fingerprint"]
	):
		raise ReceiptRefused("Reorder run-as identity or permission profile does not match Negative Stock")
	if _live_profile_fingerprint(reorder_user) != reorder_profile:
		raise ReceiptRefused("Reorder run-as permission profile changed")
	current_source = _source_state(reorder_scope, negative.get("started_at"))
	if receipt.get("source") != current_source:
		raise ReceiptRefused("Negative Stock source is stale")
	return receipt
