"""Permission-bounded AR evidence transport and unsent human-review artifacts.

The private agent bundle owns the assessment. This module owns scope, provenance,
freshness and permissions, and never mutates accounting or communication records.
"""

import json
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext

import frappe

from jarvis.chat import purchase_match
from jarvis.chat.purchase_match import digest

AGENT = "ar-collections-operator"
TOKEN = "ar-review-v1"
LABEL = "AR collections review"
REVIEW_QUESTION = "Acknowledge this review or request correction. Any reminder is an UNSENT draft; this never sends, posts, reconciles or writes off."
CONFIG_KEYS = (
	"company",
	"from_date",
	"to_date",
	"report_date",
	"basis",
	"review_scope",
	"policy_version",
	"settlement_hold_days",
)
MAX_DOCUMENTS = 500
MAX_LEDGER_ROWS = 10000
MAX_CHILD_ROWS = 10000
FIELDS = {
	"Company": ("name", "country", "default_currency", "modified"),
	"Sales Invoice": (
		"name",
		"modified",
		"company",
		"customer",
		"docstatus",
		"posting_date",
		"due_date",
		"is_return",
		"return_against",
		"amended_from",
		"currency",
		"party_account_currency",
		"debit_to",
		"outstanding_amount",
		"grand_total",
		"rounded_total",
		"payment_schedule",
	),
	"Payment Ledger Entry": (
		"name",
		"modified",
		"company",
		"posting_date",
		"account_type",
		"account",
		"party_type",
		"party",
		"voucher_type",
		"voucher_no",
		"against_voucher_type",
		"against_voucher_no",
		"account_currency",
		"amount_in_account_currency",
		"delinked",
		"finance_book",
	),
	"Payment Entry": ("name", "modified", "company", "docstatus", "posting_date", "party_type", "party"),
	"Journal Entry": ("name", "modified", "company", "docstatus", "posting_date", "accounts"),
	"Customer": ("name", "modified", "customer_name", "disabled"),
}
CHILD_FIELDS = {
	"payment_schedule": (
		"name",
		"due_date",
		"payment_term",
		"payment_amount",
		"outstanding",
		"paid_amount",
		"discounted_amount",
	),
	"accounts": ("name", "party_type", "party", "reference_type", "reference_name"),
}
INDIA_FIELDS = ("company_gstin", "billing_address_gstin", "gst_category", "irn", "einvoice_status")
NUMBERS = {
	"grand_total",
	"rounded_total",
	"outstanding_amount",
	"amount_in_account_currency",
	"payment_amount",
	"outstanding",
	"paid_amount",
	"discounted_amount",
}


def _server_number(value):
	"""Read a bounded Decimal from captured evidence, never defaulting bad data to zero."""
	if value is None or isinstance(value, bool):
		raise ValueError("Missing numeric evidence")
	result = Decimal(str(value))
	if not result.is_finite() or abs(result) > Decimal("1e18"):
		raise ValueError("Invalid numeric evidence")
	return result


def _server_date(value):
	if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
		raise ValueError("An ISO date is required")
	return value


def _server_index(rows):
	if not isinstance(rows, list):
		raise ValueError("Missing dataset")
	result = {}
	for row in rows:
		if not isinstance(row, dict) or not row.get("name") or row["name"] in result:
			raise ValueError("Missing or duplicate record identity")
		result[row["name"]] = row
	return result


def _server_pending_parties(payments, journals):
	parties = set()
	for payment in payments:
		if payment["docstatus"] == 0 and payment.get("party_type") == "Customer":
			parties.add(payment.get("party"))
	for journal in journals:
		if journal["docstatus"] != 0:
			continue
		for row in journal["accounts"]:
			if row.get("party_type") == "Customer":
				parties.add(row.get("party"))
	return parties


def _server_ledger_groups(rows, company):
	groups = defaultdict(list)
	for row in rows:
		if row.get("company") != company or row.get("party_type") != "Customer":
			raise ValueError("Ledger scope differs from the captured company/customer scope")
		if row.get("delinked") not in (0, 1):
			raise ValueError("Unknown ledger cancellation state")
		_server_date(row.get("posting_date"))
		_server_number(row.get("amount_in_account_currency"))
		identity = tuple(
			row.get(key)
			for key in (
				"party",
				"account",
				"account_currency",
				"against_voucher_type",
				"against_voucher_no",
			)
		)
		if not all(identity) or not row.get("voucher_type") or not row.get("voucher_no"):
			raise ValueError("Incomplete typed ledger identity")
		if not row["delinked"]:
			groups[identity].append(row)
	return groups


def _server_terms(invoice, residual, cutoff, quantum):
	terms = invoice.get("payment_schedule")
	if not isinstance(terms, list):
		raise ValueError("Payment schedule was not fetched")
	_server_index(terms)
	if len(terms) <= 1:
		due = _server_date(terms[0].get("due_date") if terms else invoice.get("due_date"))
		return [(due, residual)] if due < cutoff else []
	if invoice.get("currency") != invoice.get("party_account_currency"):
		raise ValueError("Instalment currency differs from the receivable account currency")
	total = Decimal(0)
	overdue = []
	for term in terms:
		due = _server_date(term.get("due_date"))
		outstanding = _server_number(term.get("outstanding"))
		expected = (
			_server_number(term.get("payment_amount"))
			- _server_number(term.get("paid_amount"))
			- _server_number(term.get("discounted_amount"))
		)
		if outstanding < 0 or abs(expected - outstanding) > quantum / 2:
			raise ValueError("Instalment allocation needs manual reconciliation")
		total += outstanding
		if outstanding > 0 and due < cutoff:
			overdue.append((due, outstanding))
	if abs(total - residual) > quantum / 2:
		raise ValueError("Instalments do not reconcile to the receivable balance")
	return overdue


def _server_terminal_state(invoice, snapshot, groups, credits, pending, customers, returns, vouchers):
	"""Return only a server-proven no-action state, otherwise ``None``.

	A private evaluator can produce a hold or a review candidate, but it must not
	decide that an invoice is settled or still within terms. Those states suppress
	the Approval Request path, so the bench proves them from the sealed evidence.
	"""
	scope, config = snapshot["scope"], snapshot["config"]
	cutoff = scope["report_date"]
	party, account, currency = (
		invoice.get(key) for key in ("customer", "debit_to", "party_account_currency")
	)
	if not all((party, account, currency)) or party not in customers:
		raise ValueError("Customer or receivable account evidence is missing")
	precision = invoice.get("outstanding_precision")
	if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 9:
		raise ValueError("Source currency precision is missing")
	quantum = Decimal(1).scaleb(-precision)
	identity = (party, account, currency, "Sales Invoice", invoice["name"])
	rows = groups.get(identity, [])
	if not rows:
		raise ValueError("No active invoice ledger evidence")
	for row in rows:
		voucher = vouchers.get((row["voucher_type"], row["voucher_no"]))
		if row.get("account_type") != "Receivable" or not voucher or voucher.get("docstatus") != 1:
			raise ValueError("Active ledger source is not a submitted receivables voucher")
	if any(key[-2:] == identity[-2:] and key != identity for key in groups):
		raise ValueError("Invoice ledger party, account or currency identities disagree")
	if any(row.get("finance_book") for row in rows):
		raise ValueError("Finance-book-specific balances need a separately scoped review")
	current = sum((_server_number(row["amount_in_account_currency"]) for row in rows), Decimal(0))
	header_balance = _server_number(invoice.get("outstanding_amount"))
	if header_balance.quantize(quantum) != header_balance or abs(current - header_balance) > quantum / 2:
		raise ValueError("Ledger and current invoice outstanding disagree")
	if (
		invoice.get("currency") == currency
		and header_balance
		> max(_server_number(invoice.get("grand_total")), _server_number(invoice.get("rounded_total")))
		+ quantum / 2
	):
		raise ValueError("Recorded receivable exceeds the invoice total")
	eligible = [row for row in rows if row["posting_date"] <= cutoff]
	if len(eligible) != len(rows):
		return None
	if header_balance <= 0:
		return "settled"
	if customers[party].get("disabled"):
		return None
	jurisdiction = snapshot.get("jurisdiction") or {}
	if (
		jurisdiction.get("india_compliance_installed")
		and jurisdiction.get("company_country") == "India"
		and (invoice.get("india") or {}).get("einvoice_status")
		in ("Cancelled", "Manually Cancelled", "Pending Cancellation")
	):
		return None
	if party in credits or party in pending or invoice["name"] in returns:
		return None
	if scope["report_date"] != snapshot.get("site_date"):
		return None
	settlements = [
		row["posting_date"] for row in eligible if _server_number(row["amount_in_account_currency"]) < 0
	]
	if (
		settlements
		and (date.fromisoformat(cutoff) - date.fromisoformat(max(settlements))).days
		<= config["settlement_hold_days"]
	):
		return None
	return "within_terms" if not _server_terms(invoice, header_balance, cutoff, quantum) else None


def server_terminal_states(snapshot):
	"""Return invoice states that may suppress an Approval Request.

	The result is intentionally conservative. Any malformed, unreadable, changed,
	or ambiguous source returns no terminal state. A delegate can then only submit a
	hold or review candidate, never hide the invoice as settled or within terms.
	"""
	try:
		with localcontext() as context:
			context.prec = 48
			sealed = {
				key: value
				for key, value in snapshot.items()
				if key not in ("source_digest", "captured_at", "captured_by")
			}
			if snapshot.get("source_digest") != digest(sealed):
				return {}
			scope, config = snapshot["scope"], snapshot["config"]
			start, end, cutoff = (
				_server_date(scope.get(key)) for key in ("from_date", "to_date", "report_date")
			)
			if (
				not scope.get("company")
				or scope.get("basis") != "current_records"
				or not start <= cutoff <= end
				or cutoff > _server_date(snapshot.get("site_date"))
				or config.get("review_scope") != "review_and_drafts_only"
				or not isinstance(config.get("policy_version"), str)
				or not config["policy_version"].strip()
				or type(config.get("settlement_hold_days")) is not int
				or not 0 <= config["settlement_hold_days"] <= 30
				or snapshot.get("complete") is not True
			):
				return {}
			indexed = {
				key: _server_index(snapshot["datasets"].get(key))
				for key in ("invoices", "ledger", "payments", "journals", "customers")
			}
			for key in ("invoices", "payments", "journals"):
				if any(row.get("company") != scope["company"] for row in indexed[key].values()):
					raise ValueError("Source company differs from scope")
			for invoice in indexed["invoices"].values():
				_server_date(invoice.get("posting_date"))
				if invoice.get("docstatus") not in (0, 1, 2) or invoice.get("is_return") not in (0, 1):
					raise ValueError("Unknown invoice state")
			groups = _server_ledger_groups(indexed["ledger"].values(), scope["company"])
			if any(key[3] == "Sales Invoice" and key[4] not in indexed["invoices"] for key in groups):
				raise ValueError("An active invoice ledger reference has no captured invoice")
			credits = {
				key[0]
				for key, rows in groups.items()
				if sum((_server_number(row["amount_in_account_currency"]) for row in rows), Decimal(0)) < 0
			}
			credits.update(
				row.get("customer")
				for row in indexed["invoices"].values()
				if row.get("is_return") and row["docstatus"] != 2 and not row.get("return_against")
			)
			pending = _server_pending_parties(indexed["payments"].values(), indexed["journals"].values())
			returns = {
				row.get("return_against")
				for row in indexed["invoices"].values()
				if row["docstatus"] != 2 and row.get("is_return")
			}
			vouchers = {
				(doctype, name): row
				for key, doctype in (
					("invoices", "Sales Invoice"),
					("payments", "Payment Entry"),
					("journals", "Journal Entry"),
				)
				for name, row in indexed[key].items()
			}
			return {
				invoice["name"]: state
				for invoice in indexed["invoices"].values()
				if invoice["docstatus"] == 1
				and not invoice.get("is_return")
				and invoice["posting_date"] <= cutoff
				and (
					state := _server_terminal_state(
						invoice,
						snapshot,
						groups,
						credits,
						pending,
						indexed["customers"],
						returns,
						vouchers,
					)
				)
			}
	except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation):
		return {}


def configuration_defaults(company=None):
	result = purchase_match.configuration_defaults(company)
	result["defaults"].pop("price_tolerance_percent", None)
	result["defaults"].update(
		policy_version="AR-REVIEW-v1", settlement_hold_days=3, review_scope="review_and_drafts_only"
	)
	return result


def configuration_issues(raw):
	try:
		config = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (ValueError, TypeError):
		config = None
	if not isinstance(config, dict):
		return {"config": "Configuration must be a JSON object."}
	issues = purchase_match.configuration_issues(
		{
			**config,
			"price_tolerance_percent": 0,
			"review_scope": "document_matching_only",
		}
	)
	if "policy_version" in issues:
		issues["policy_version"] = "Enter an AR review policy reference (1 to 128 characters)."
	if not {"from_date", "to_date", "report_date"}.intersection(issues):
		if not config["from_date"] <= config["report_date"] <= config["to_date"]:
			issues["report_date"] = (
				"Evidence cutoff must be within the review period. Older open invoices remain included."
			)
	if type(config.get("settlement_hold_days")) is not int or not 0 <= config["settlement_hold_days"] <= 30:
		issues["settlement_hold_days"] = "Choose a settlement hold from 0 to 30 calendar days."
	if config.get("review_scope") != "review_and_drafts_only":
		issues["review_scope"] = "Confirm Review and drafts only; sending is not supported."
	return issues


def configuration(inst):
	issues = configuration_issues(inst.config or "{}")
	if issues:
		frappe.throw(list(issues.values()), title="Complete AR configuration", as_list=True)
	config = frappe.parse_json(inst.config)
	frappe.get_doc("Company", config["company"]).check_permission("read")
	result = {key: config[key] for key in CONFIG_KEYS}
	if config.get("fiscal_year"):
		result["fiscal_year"] = config["fiscal_year"]
	return result


def scope_from_config(config):
	return {**purchase_match.scope_from_config(config), "include_opening_receivables": True}


def _projection(document, india):
	fields = FIELDS[document.doctype]
	result = {}
	for field in fields:
		value = document.get(field)
		if field in CHILD_FIELDS:
			result[field] = [
				{
					key: str(row.get(key)) if key in NUMBERS and row.get(key) is not None else row.get(key)
					for key in CHILD_FIELDS[field]
				}
				for row in value or []
			]
		else:
			result[field] = str(value) if field in NUMBERS and value is not None else value
	if document.doctype == "Sales Invoice":
		result["outstanding_precision"] = document.precision("outstanding_amount")
		if india:
			result["india"] = {key: document.get(key) for key in INDIA_FIELDS if document.meta.has_field(key)}
	return result


def _project(document, india=False):
	document.check_permission("read")
	before = frappe.as_json(_projection(document, india))
	document.apply_fieldlevel_read_permissions()
	after = frappe.as_json(_projection(document, india))
	if before != after:
		raise frappe.PermissionError
	return json.loads(after)


def _names(doctype, filters, limit):
	if not frappe.has_permission(doctype, "read"):
		raise frappe.PermissionError
	names = frappe.get_list(
		doctype, filters=filters, pluck="name", order_by="name", limit_page_length=limit + 1
	)
	if len(names) > limit:
		raise OverflowError
	if len(set(names)) != len(names) or len(names) != frappe.db.count(doctype, filters=filters):
		raise frappe.PermissionError
	return names


def collect(scope, config):
	snapshot = {
		"scope": scope,
		"config": config,
		"datasets": {},
		"complete": False,
		"site_date": frappe.utils.today(),
	}
	try:
		company = _project(frappe.get_doc("Company", scope["company"]))
		snapshot["company"] = company
		if scope.get("fiscal_year"):
			fiscal = purchase_match._fiscal_year(scope["fiscal_year"], scope["company"])
			if not fiscal or any(
				not fiscal["from_date"] <= scope[field] <= fiscal["to_date"]
				for field in ("from_date", "to_date")
			):
				raise ValueError("Fiscal-year applicability changed")
			snapshot["fiscal_year"] = fiscal
		snapshot["jurisdiction"] = {
			"india_compliance_installed": "india_compliance" in frappe.get_installed_apps(),
			"company_country": company["country"],
			"accounting_framework": "not_determined",
		}
		india = snapshot["jurisdiction"]["india_compliance_installed"] and company["country"] == "India"
		filters = {"company": scope["company"]}
		child_count = 0
		for key, doctype, extra, limit in (
			("invoices", "Sales Invoice", {}, MAX_DOCUMENTS),
			("ledger", "Payment Ledger Entry", {"party_type": "Customer"}, MAX_LEDGER_ROWS),
			("payments", "Payment Entry", {"party_type": "Customer", "docstatus": ["!=", 2]}, MAX_DOCUMENTS),
			("journals", "Journal Entry", {"docstatus": ["!=", 2]}, MAX_DOCUMENTS),
		):
			rows = []
			for name in _names(doctype, {**filters, **extra}, limit):
				row = _project(frappe.get_doc(doctype, name), india=india)
				child_count += sum(len(row.get(field) or []) for field in CHILD_FIELDS)
				if child_count > MAX_CHILD_ROWS:
					raise OverflowError
				rows.append(row)
			snapshot["datasets"][key] = rows
		parties = {row["customer"] for row in snapshot["datasets"]["invoices"]} | {
			row["party"] for row in snapshot["datasets"]["ledger"]
		}
		if not all(isinstance(party, str) and party for party in parties):
			raise ValueError("Missing customer identity")
		snapshot["datasets"]["customers"] = [
			_project(frappe.get_doc("Customer", party)) for party in sorted(parties)
		]
		snapshot["complete"] = True
	except frappe.PermissionError:
		snapshot["datasets"] = {}
		snapshot.update(
			reason_code="permission_slice",
			detail="Complete company AR evidence is not readable by this user.",
		)
	except OverflowError:
		snapshot["datasets"] = {}
		snapshot.update(
			reason_code="run_truncated_watermark",
			detail="AR evidence exceeds the bounded review capacity; no full-book conclusion is available.",
		)
	except (frappe.DoesNotExistError, ValueError):
		snapshot["datasets"] = {}
		snapshot.update(
			reason_code="record_coverage_insufficient",
			detail="Source records changed or are incomplete; retry after reconciliation.",
		)
	snapshot["source_digest"] = digest(snapshot)
	return snapshot


def get_inputs(run):
	from jarvis.chat import operator_review

	return operator_review.get_inputs(run, operator_review.backend(AGENT))


def _unavailable(reason, detail):
	return {TOKEN: {"state": "not_evaluable", "reason_code": reason, "detail": detail}}


def validate_output(run, findings, coverage, scope, integrity_digest):
	stored = frappe.parse_json(run.input_snapshot_json or "null")
	if not stored or scope != stored["scope"]:
		frappe.throw(
			"AR output must retain the captured fiscal context, cutoff and opening-receivables scope."
		)
	if integrity_digest != digest(
		{"input_digest": stored["source_digest"], "scope": scope, "findings": findings, "coverage": coverage}
	):
		frappe.throw("AR result digest does not match captured evidence and output.")
	if set(coverage) != {TOKEN} or not isinstance(coverage[TOKEN], dict):
		frappe.throw("AR output must include its declared coverage.")
	if not stored["complete"]:
		return [], _unavailable(stored["reason_code"], stored["detail"]), False
	if not findings and coverage[TOKEN].get("state") == "not_evaluable":
		return [], coverage, False
	names = {
		row["name"]
		for row in stored["datasets"]["invoices"]
		if row["docstatus"] == 1 and not row["is_return"] and row["posting_date"] <= scope["report_date"]
	}
	refs = [row.get("ref_name") for row in findings if isinstance(row, dict)]
	if len(refs) != len(findings) or set(refs) != names or len(refs) != len(names):
		frappe.throw(
			"AR output must account for every submitted invoice through cutoff, including opening receivables."
		)
	terminal_states = server_terminal_states(stored)
	for row in findings:
		if (
			row.get("token") != TOKEN
			or row.get("ref_doctype") != "Sales Invoice"
			or row.get("amount") != 0
			or row.get("result_class") != "derived_candidate"
			or not isinstance(row.get("note"), str)
			or row.get("review_state") not in ("hold", "settled", "within_terms", "reminder_candidate")
		):
			frappe.throw("AR results must be explicit review candidates, never financial outcomes.")
		if row.get("reminder_draft") and (
			row["review_state"] != "reminder_candidate" or scope["report_date"] != stored["site_date"]
		):
			frappe.throw("Historical or held assessments cannot carry reminder drafts.")
		expected_state = terminal_states.get(row["ref_name"])
		if expected_state:
			if row["review_state"] != expected_state:
				frappe.throw("Only server evidence may mark an invoice settled or within terms.")
		elif row["review_state"] in ("settled", "within_terms"):
			frappe.throw("Only server evidence may mark an invoice settled or within terms.")
	current = collect(stored["scope"], stored["config"])
	if current["source_digest"] != stored["source_digest"]:
		return (
			[],
			_unavailable(
				"run_truncated_watermark",
				"AR evidence, site date or permissions changed after capture; rerun before review.",
			),
			False,
		)
	return findings, coverage, True


def review_context(stored, finding):
	return (
		f"Company: {stored['scope']['company']}. Cutoff: {stored['scope']['report_date']}. "
		f"Policy: {stored['config']['policy_version']}. Calendar-day settlement hold: {stored['config']['settlement_hold_days']}. "
		"Includes opening receivables. Current records only. No sending, Dunning creation, fees or accounting changes. "
		"Verify disputes, receipts, contact details and applicable tax treatment before any manual contact.\n\n"
		+ finding["note"]
	)


def stage_reviews(run, inst, findings):
	from jarvis.chat import operator_review

	return operator_review.stage_reviews(
		run,
		inst,
		[row for row in findings if row.get("review_state") in ("hold", "reminder_candidate")],
		operator_review.backend(AGENT),
	)
