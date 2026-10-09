"""AP review transport, evidence custody and human-only disposition.

Matching predicates live in the private bundle. The bench controls scope,
permissions, completeness, input provenance and artifact writeback, never posting.
"""

import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation

import frappe

AGENT = "ap-3way-match-operator"
TOKEN = "ap3-match-v1"
LABEL = "AP document review"
REVIEW_QUESTION = "Acknowledge this document review or request correction. This does NOT submit, pay or authorise posting the invoice."
MAX_DOCUMENTS = 500
MAX_LINES = 10000
DATASETS = {"invoices": "Purchase Invoice", "orders": "Purchase Order", "receipts": "Purchase Receipt"}
HEADER_FIELDS = (
	"name",
	"modified",
	"company",
	"supplier",
	"currency",
	"docstatus",
	"status",
	"posting_date",
	"transaction_date",
	"bill_no",
	"bill_date",
	"is_return",
	"return_against",
	"amended_from",
	"update_stock",
)
ITEM_FIELDS = (
	"name",
	"item_code",
	"qty",
	"stock_qty",
	"stock_uom",
	"conversion_factor",
	"net_rate",
	"net_amount",
	"purchase_order",
	"po_detail",
	"purchase_receipt",
	"pr_detail",
	"purchase_order_item",
)
NUMBERS = {"qty", "stock_qty", "conversion_factor", "net_rate", "net_amount"}


def digest(value):
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
	).hexdigest()


def _fiscal_year(name, company):
	document = frappe.get_doc("Fiscal Year", name)
	document.check_permission("read")
	fields = ("disabled", "year_start_date", "year_end_date", "companies")
	before = frappe.as_json({field: document.get(field) for field in fields})
	document.apply_fieldlevel_read_permissions()
	if before != frappe.as_json({field: document.get(field) for field in fields}):
		raise frappe.PermissionError
	companies = [row.company for row in document.get("companies") or []]
	if document.disabled or (companies and company not in companies):
		return None
	return {
		"name": document.name,
		"from_date": str(document.year_start_date),
		"to_date": str(document.year_end_date),
	}


def configuration_defaults(company=None):
	"""Visible starter settings, never persisted or treated as policy approval."""
	result = {
		"defaults": {
			"policy_version": "AP-MATCH-v1",
			"price_tolerance_percent": 0,
			"basis": "current_records",
			"review_scope": "document_matching_only",
		},
		"site_date": frappe.utils.today(),
		"fiscal_years": [],
		"message": "",
	}
	explicit_company = bool(company)
	if not company:
		company = frappe.defaults.get_user_default("Company")
		if not isinstance(company, str) or not company:
			companies = frappe.get_list("Company", pluck="name", limit_page_length=2)
			company = companies[0] if len(companies) == 1 else None
	if not company:
		result["message"] = "Select a company to load its fiscal years and date suggestions."
		return result
	try:
		frappe.get_doc("Company", company).check_permission("read")
	except (frappe.PermissionError, frappe.DoesNotExistError):
		if explicit_company:
			raise
		result["message"] = "Select a readable company to load its fiscal years."
		return result
	result["defaults"]["company"] = company
	try:
		names = frappe.get_list(
			"Fiscal Year",
			filters={"disabled": 0},
			pluck="name",
			order_by="year_start_date desc",
			limit_page_length=201,
		)
		if len(names) > 200:
			result["message"] = "Too many fiscal years to suggest a period; enter explicit dates."
			return result
		result["fiscal_years"] = [
			fiscal for name in names if (fiscal := _fiscal_year(name, company)) is not None
		]
	except (frappe.PermissionError, frappe.DoesNotExistError):
		result["message"] = "Fiscal years are not readable. Check permissions or enter explicit dates."
		return result
	current = [
		fiscal
		for fiscal in result["fiscal_years"]
		if fiscal["from_date"] <= result["site_date"] <= fiscal["to_date"]
	]
	if len(current) != 1:
		result["message"] = "No unique current fiscal year is available. Choose a fiscal year or enter dates."
	return result


def configuration_issues(raw):
	"""Field-level setup guidance shared by the UI and the launch guard."""
	try:
		config = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (ValueError, TypeError):
		return {"config": "Configuration must be a valid JSON object."}
	if not isinstance(config, dict):
		return {"config": "Configuration must be a JSON object."}
	issues = {}
	if not isinstance(config.get("company"), str) or not config["company"].strip():
		issues["company"] = "Select a company."
	for field, label in (
		("from_date", "Period from"),
		("to_date", "Period to"),
		("report_date", "Evidence cutoff"),
	):
		try:
			if date.fromisoformat(config.get(field)).isoformat() != config[field]:
				raise ValueError
		except (ValueError, TypeError):
			issues[field] = f"{label}: choose a valid date."
	if "from_date" not in issues:
		if "to_date" not in issues and config["to_date"] < config["from_date"]:
			issues["to_date"] = "Period to must be on or after Period from."
		if "report_date" not in issues and config["report_date"] < config["from_date"]:
			issues["report_date"] = "Evidence cutoff must be on or after Period from."
	if "report_date" not in issues and config["report_date"] > frappe.utils.today():
		issues["report_date"] = "Evidence cutoff cannot be later than today's date on this site."
	if config.get("fiscal_year") not in (None, "") and "company" not in issues:
		fiscal = None
		try:
			if isinstance(config["fiscal_year"], str):
				frappe.get_doc("Company", config["company"]).check_permission("read")
				fiscal = _fiscal_year(config["fiscal_year"], config["company"])
		except (frappe.PermissionError, frappe.DoesNotExistError):
			pass
		if not fiscal:
			issues["fiscal_year"] = "Select an active, readable fiscal year applicable to this company."
		else:
			for field, label in (("from_date", "Period from"), ("to_date", "Period to")):
				if field not in issues and not fiscal["from_date"] <= config[field] <= fiscal["to_date"]:
					issues[field] = (
						f"{label} must be within the selected fiscal year; clear Fiscal year for a custom cross-year period."
					)
	policy = config.get("policy_version")
	if not isinstance(policy, str) or not 1 <= len(policy.strip()) <= 128:
		issues["policy_version"] = "Enter an AP matching policy reference (1 to 128 characters)."
	try:
		tolerance = Decimal(str(config.get("price_tolerance_percent")))
		if not tolerance.is_finite() or not 0 <= tolerance <= 100:
			raise ValueError
	except (ValueError, InvalidOperation):
		issues["price_tolerance_percent"] = "Enter a net-price tolerance from 0 to 100%."
	if config.get("basis") != "current_records":
		issues["basis"] = "Select Current records only as the evidence basis."
	if config.get("review_scope") != "document_matching_only":
		issues["review_scope"] = "Confirm Document matching only as the review scope."
	return issues


def configuration(inst):
	raw = inst.config or "{}"
	issues = configuration_issues(raw)
	if issues:
		frappe.throw(list(issues.values()), title="Complete AP configuration", as_list=True)
	config = frappe.parse_json(raw)
	company = frappe.get_doc("Company", config["company"])
	company.check_permission("read")
	validated = {
		key: config[key]
		for key in (
			"company",
			"from_date",
			"to_date",
			"report_date",
			"basis",
			"review_scope",
			"policy_version",
			"price_tolerance_percent",
		)
	}
	if config.get("fiscal_year"):
		validated["fiscal_year"] = config["fiscal_year"]
	return validated


def scope_from_config(config):
	scope = {key: config[key] for key in ("company", "from_date", "to_date", "report_date", "basis")}
	if config.get("fiscal_year"):
		scope["fiscal_year"] = config["fiscal_year"]
	return scope


def _project(doc):
	doc.check_permission("read")
	header_before = {field: doc.get(field) for field in HEADER_FIELDS}
	items_before = [{field: item.get(field) for field in ITEM_FIELDS} for item in doc.get("items") or []]
	doc.apply_fieldlevel_read_permissions()
	if header_before != {field: doc.get(field) for field in HEADER_FIELDS} or items_before != [
		{field: item.get(field) for field in ITEM_FIELDS} for item in doc.get("items") or []
	]:
		raise frappe.PermissionError
	projected = {
		field: doc.get(field)
		for field in HEADER_FIELDS
		if doc.meta.has_field(field) or field in ("name", "modified", "docstatus")
	}
	projected["items"] = []
	for item in doc.get("items") or []:
		row = {
			field: item.get(field) for field in ITEM_FIELDS if item.meta.has_field(field) or field == "name"
		}
		for field in NUMBERS:
			if row.get(field) is not None:
				row[field] = str(row[field])
		row["stock_qty_precision"] = item.precision("stock_qty")
		row["net_amount_precision"] = item.precision("net_amount")
		projected["items"].append(row)
	return json.loads(frappe.as_json(projected))


def collect(scope, config):
	"""Bounded, full-company evidence; hidden rows invalidate absence conclusions."""
	snapshot = {"scope": scope, "config": config, "datasets": {}, "complete": False}
	line_count = 0
	try:
		company = frappe.get_doc("Company", scope["company"])
		company.check_permission("read")
		for key, doctype in DATASETS.items():
			if not frappe.has_permission(doctype, "read"):
				raise frappe.PermissionError
			filters = {"company": scope["company"]}
			names = frappe.get_list(
				doctype, filters=filters, pluck="name", order_by="name", limit_page_length=MAX_DOCUMENTS + 1
			)
			if len(names) > MAX_DOCUMENTS:
				snapshot.update(
					reason_code="run_truncated_watermark",
					detail="Company evidence exceeds the bounded review capacity; no completeness conclusion is available.",
				)
				break
			if len(set(names)) != len(names) or len(names) != frappe.db.count(doctype, filters=filters):
				raise frappe.PermissionError
			rows = []
			for name in names:
				rows.append(_project(frappe.get_doc(doctype, name)))
				line_count += len(rows[-1]["items"])
				if line_count > MAX_LINES:
					snapshot.update(
						reason_code="run_truncated_watermark",
						detail="The source-line limit was reached; no complete match is asserted.",
					)
					return _seal(snapshot)
			snapshot["datasets"][key] = rows
		else:
			snapshot["complete"] = True
	except frappe.PermissionError:
		snapshot["datasets"] = {}
		snapshot.update(
			reason_code="permission_slice",
			detail="Complete company evidence is not readable by this run-as user.",
		)
	return _seal(snapshot)


def _seal(snapshot):
	snapshot["source_digest"] = digest(snapshot)
	return snapshot


def get_inputs(run):
	from jarvis.chat import operator_review

	return operator_review.get_inputs(run, operator_review.backend(AGENT))


def validate_output(run, findings, coverage, scope, integrity_digest):
	"""Fail closed on omitted drafts, stale inputs, altered scope or unbound results."""
	stored = frappe.parse_json(run.input_snapshot_json or "null")
	if not stored or scope != stored["scope"]:
		frappe.throw("AP output must reference the captured launch scope and evidence.")
	expected = digest(
		{"input_digest": stored["source_digest"], "scope": scope, "findings": findings, "coverage": coverage}
	)
	if integrity_digest != expected:
		frappe.throw("AP result digest does not match its captured inputs and output.")
	if set(coverage) != {TOKEN}:
		frappe.throw("AP output must include its complete matching coverage.")
	if not stored["complete"]:
		return [], _unavailable(stored.get("reason_code"), stored.get("detail")), False
	cutoff = min(scope["to_date"], scope["report_date"])
	names = {
		row["name"]
		for row in stored["datasets"]["invoices"]
		if row["docstatus"] == 0 and scope["from_date"] <= row["posting_date"] <= cutoff
	}
	refs = [row.get("ref_name") for row in findings if isinstance(row, dict)]
	if len(refs) != len(findings) or set(refs) != names or len(refs) != len(names):
		frappe.throw("AP output must contain exactly one review result per captured draft.")
	if any(
		row.get("ref_doctype") != "Purchase Invoice"
		or row.get("token") != TOKEN
		or row.get("result_class") != "derived_candidate"
		or row.get("amount") != 0
		for row in findings
	):
		frappe.throw("AP results are document-review candidates, not financial outcomes.")
	current = collect(stored["scope"], stored["config"])
	if current["source_digest"] != stored["source_digest"]:
		return (
			[],
			_unavailable(
				"run_truncated_watermark",
				"AP evidence or permissions changed after capture. Re-run before reviewing any proposals.",
			),
			False,
		)
	return findings, coverage, True


def _unavailable(reason, detail):
	return {
		TOKEN: {
			"state": "not_evaluable",
			"reason_code": reason or "record_coverage_insufficient",
			"detail": detail or "Complete evidence is required.",
		}
	}


def stage_reviews(run, inst, findings):
	from jarvis.chat import operator_review

	return operator_review.stage_reviews(run, inst, findings, operator_review.backend(AGENT))


def may_review(doc):
	from jarvis.chat.operator_review import may_review as allowed

	return allowed(doc)


def check_review(doc, approve):
	from jarvis.chat import operator_review

	return operator_review.check_review(doc, approve, operator_review.backend(AGENT))


def review_context(stored, finding):
	policy = stored["config"]
	scope = stored["scope"]
	return (
		f"Company: {scope['company']}. Draft posting window: {scope['from_date']} to {scope['to_date']}. "
		f"Cutoff: {scope['report_date']}. Current records only. "
		f"Policy: {policy['policy_version']}; configured net-price tolerance: {policy['price_tolerance_percent']}%. "
		"Quantity demand must not exceed the linked order or accepted receipt. "
		"Review this policy and the evidence; configuration alone is not approval.\n\n" + finding["note"]
	)
