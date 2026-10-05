"""Bank reconciliation review: sealed bank-line and voucher evidence for a named reviewer.

Matching predicates stay in the private bundle; the bench captures, checks and stages only."""

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import frappe

from jarvis.chat import purchase_match
from jarvis.chat.purchase_match import digest

AGENT = "bank-recon-operator"
TOKEN = "bank-recon-review-v1"
LABEL = "Bank reconciliation review"
REVIEW_QUESTION = (
	"Confirm or correct this proposal, then reconcile it yourself in the Bank Reconciliation Tool. "
	"Approving does not reconcile, allocate, create or post anything."
)
CONFIG_KEYS = (
	"company",
	"bank_account",
	"from_date",
	"to_date",
	"voucher_lookback_days",
	"policy_version",
	"review_scope",
)
MAX_LINES = 500
MAX_VOUCHERS = 2000
MAX_ACCOUNTS = 200
STATES = frozenset({"match_proposed", "ambiguous", "reversal_pair", "draft_proposal", "needs_decision"})


def _bank_account(name):
	if not isinstance(name, str) or not name:
		return None
	return frappe.db.get_value(
		"Bank Account", name, ["company", "is_company_account", "account", "disabled"], as_dict=True
	)


def configuration_defaults(company=None):
	"""Visible starter settings; never persisted or treated as policy approval."""
	result = purchase_match.configuration_defaults(company)
	defaults = result["defaults"]
	for key in ("price_tolerance_percent", "basis"):
		defaults.pop(key, None)
	last = date.fromisoformat(result["site_date"]).replace(day=1) - timedelta(days=1)
	defaults.update(
		policy_version="BANK-RECON-v1",
		review_scope="proposals_only",
		voucher_lookback_days=30,
		from_date=last.replace(day=1).isoformat(),
		to_date=last.isoformat(),
	)
	result["bank_accounts"] = []
	if defaults.get("company"):
		result["bank_accounts"] = frappe.get_list(
			"Bank Account",
			filters={"company": defaults["company"], "is_company_account": 1, "disabled": 0},
			fields=["name", "account"],
			order_by="name",
			limit_page_length=50,
		)
		if len(result["bank_accounts"]) == 1:
			defaults["bank_account"] = result["bank_accounts"][0]["name"]
	return result


def configuration_issues(raw):
	"""Field-level setup guidance shared by the UI and the launch guard."""
	try:
		config = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (ValueError, TypeError):
		config = None
	if not isinstance(config, dict):
		return {"config": "Configuration must be a JSON object."}
	issues = {}
	if not isinstance(config.get("company"), str) or not config["company"].strip():
		issues["company"] = "Select a company."
	name = config.get("bank_account")
	bank = _bank_account(name) if isinstance(name, str) and name else None
	if (
		not bank
		or bank.company != config.get("company")
		or not bank.is_company_account
		or not bank.account
		or bank.disabled
	):
		issues["bank_account"] = (
			"Select an enabled company bank account of this company, linked to a ledger account."
		)
	for field, label in (("from_date", "Statement from"), ("to_date", "Statement to")):
		try:
			if date.fromisoformat(config.get(field)).isoformat() != config[field]:
				raise ValueError
		except (ValueError, TypeError):
			issues[field] = f"{label}: choose a valid date."
	if not {"from_date", "to_date"}.intersection(issues):
		if config["to_date"] < config["from_date"]:
			issues["to_date"] = "Statement to must be on or after Statement from."
		elif config["to_date"] > frappe.utils.today():
			issues["to_date"] = "Statement to cannot be later than today's date on this site."
	lookback = config.get("voucher_lookback_days")
	if type(lookback) is not int or not 0 <= lookback <= 90:
		issues["voucher_lookback_days"] = "Choose a voucher lookback from 0 to 90 calendar days."
	policy = config.get("policy_version")
	if not isinstance(policy, str) or not 1 <= len(policy.strip()) <= 128:
		issues["policy_version"] = "Enter a bank reconciliation policy reference (1 to 128 characters)."
	if config.get("review_scope") != "proposals_only":
		issues["review_scope"] = "Confirm Proposals only; reconciling is not supported."
	return issues


def configuration(inst):
	issues = configuration_issues(inst.config or "{}")
	if issues:
		frappe.throw(list(issues.values()), title="Complete bank reconciliation settings", as_list=True)
	config = frappe.parse_json(inst.config)
	frappe.get_doc("Company", config["company"]).check_permission("read")
	bank = frappe.get_doc("Bank Account", config["bank_account"])
	bank.check_permission("read")
	frappe.get_doc("Account", bank.account).check_permission("read")
	return {key: config[key] for key in CONFIG_KEYS}


def scope_from_config(config):
	bank = _bank_account(config["bank_account"])
	if not bank:
		frappe.throw("The selected bank account is no longer available.")
	start = date.fromisoformat(config["from_date"])
	return {
		"company": config["company"],
		"bank_account": config["bank_account"],
		"bank_gl": bank.account,
		"from_date": config["from_date"],
		"to_date": config["to_date"],
		"voucher_from": (start - timedelta(days=config["voucher_lookback_days"])).isoformat(),
	}


LINE_FIELDS = (
	"name",
	"date",
	"docstatus",
	"status",
	"deposit",
	"withdrawal",
	"unallocated_amount",
	"currency",
	"description",
	"reference_number",
	"transaction_id",
	"bank_party_name",
	"bank_party_account_number",
	"party_type",
	"party",
	"modified",
)
_AMOUNTS = ("deposit", "withdrawal", "unallocated_amount")


def _count(doctype, filters):
	return frappe.db.count(doctype, filters=filters)


def _names(doctype, filters, limit):
	if not frappe.has_permission(doctype, "read"):
		raise frappe.PermissionError
	names = frappe.get_list(
		doctype, filters=filters, pluck="name", order_by="name", limit_page_length=limit + 1
	)
	if len(names) > limit:
		raise OverflowError
	if len(set(names)) != len(names) or len(names) != _count(doctype, filters):
		raise frappe.PermissionError
	return names


def _read(doctype, name):
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	return doc


def _plain(value):
	return json.loads(frappe.as_json(value))


def _line_row(doc):
	row = {field: doc.get(field) for field in LINE_FIELDS}
	for field in _AMOUNTS:
		row[field] = str(Decimal(str(row[field] or 0)))
	return _plain(row)


def _links(doctype, names):
	"""Voucher -> submitted Bank Transactions it is allocated to (cancelled/draft BTs do not lock it)."""
	if not names:
		return {}
	rows = frappe.get_all(
		"Bank Transaction Payments",
		filters={
			"parenttype": "Bank Transaction",
			"payment_document": doctype,
			"payment_entry": ["in", names],
		},
		fields=["parent", "payment_entry"],
	)
	live = set(
		frappe.get_all(
			"Bank Transaction",
			filters={"name": ["in", sorted({r.parent for r in rows}) or [""]], "docstatus": 1},
			pluck="name",
		)
	)
	links = {}
	for r in rows:
		if r.parent in live:
			links.setdefault(r.payment_entry, []).append(r.parent)
	return {key: sorted(value) for key, value in links.items()}


def _payment_vouchers(scope):
	gl, window = scope["bank_gl"], ["between", [scope["voucher_from"], scope["to_date"]]]
	base = {"company": scope["company"], "docstatus": 1, "posting_date": window}
	names = sorted(
		set(_names("Payment Entry", {**base, "paid_from": gl}, MAX_VOUCHERS))
		| set(_names("Payment Entry", {**base, "paid_to": gl}, MAX_VOUCHERS))
	)
	if len(names) > MAX_VOUCHERS:
		raise OverflowError
	rows = []
	for name in names:
		doc = _read("Payment Entry", name)
		out = doc.paid_from == gl
		rows.append(
			{
				"doctype": "Payment Entry",
				"name": doc.name,
				"posting_date": doc.posting_date,
				"direction": "out" if out else "in",
				"amount": str(Decimal(str(doc.paid_amount if out else doc.received_amount))),
				"party_type": doc.party_type,
				"party": doc.party,
				"party_name": doc.party_name,
				"reference": doc.reference_no,
				"reference_date": doc.reference_date,
				"clearance_date": doc.clearance_date,
				"modified": doc.modified,
			}
		)
	return rows


def _journal_vouchers(scope):
	gl = scope["bank_gl"]
	if not frappe.has_permission("Journal Entry", "read"):
		raise frappe.PermissionError
	filters = [
		["Journal Entry", "company", "=", scope["company"]],
		["Journal Entry", "docstatus", "=", 1],
		["Journal Entry", "posting_date", "between", [scope["voucher_from"], scope["to_date"]]],
		["Journal Entry Account", "account", "=", gl],
	]
	names = sorted(
		set(
			frappe.get_list(
				"Journal Entry",
				filters=filters,
				pluck="name",
				distinct=True,
				order_by="name",
				limit_page_length=MAX_VOUCHERS + 1,
			)
		)
	)
	if len(names) > MAX_VOUCHERS:
		raise OverflowError
	total = frappe.db.sql(
		"""select count(distinct je.name) from `tabJournal Entry` je
		join `tabJournal Entry Account` jea on jea.parent = je.name and jea.parenttype = 'Journal Entry'
		where je.company = %s and je.docstatus = 1 and je.posting_date between %s and %s and jea.account = %s""",
		(scope["company"], scope["voucher_from"], scope["to_date"], gl),
	)[0][0]
	if total != len(names):
		raise frappe.PermissionError
	rows = []
	for name in names:
		doc = _read("Journal Entry", name)
		net = sum(
			(Decimal(str(r.debit_in_account_currency or 0)) - Decimal(str(r.credit_in_account_currency or 0)))
			for r in doc.accounts
			if r.account == gl
		)
		if not net:
			continue
		party = next((r for r in doc.accounts if r.party), None)
		rows.append(
			{
				"doctype": "Journal Entry",
				"name": doc.name,
				"posting_date": doc.posting_date,
				"direction": "in" if net > 0 else "out",
				"amount": str(abs(net)),
				"party_type": party.party_type if party else None,
				"party": party.party if party else None,
				"party_name": None,
				"reference": doc.cheque_no,
				"reference_date": doc.cheque_date,
				"clearance_date": doc.clearance_date,
				"modified": doc.modified,
			}
		)
	return rows


def _accounts(company):
	filters = {"company": company, "is_group": 0, "root_type": ["in", ["Expense", "Income"]]}
	or_filters = [
		["account_name", "like", f"%{word}%"] for word in ("charge", "fee", "interest", "commission")
	]
	rows = frappe.get_list(
		"Account",
		filters=filters,
		or_filters=or_filters,
		fields=["name", "account_name", "root_type"],
		order_by="name",
		limit_page_length=MAX_ACCOUNTS + 1,
	)
	if len(rows) > MAX_ACCOUNTS:
		raise OverflowError
	if len(rows) != len(frappe.get_all("Account", filters=filters, or_filters=or_filters, pluck="name")):
		raise frappe.PermissionError
	return rows


def collect(scope, config):
	"""Bounded, permission-checked evidence for one bank account; hidden rows void completeness."""
	snapshot = {
		"scope": scope,
		"config": config,
		"datasets": {},
		"complete": False,
		"site_date": frappe.utils.today(),
	}
	try:
		_read("Company", scope["company"])
		bank = _read("Bank Account", scope["bank_account"])
		if (
			bank.company != scope["company"]
			or bank.account != scope["bank_gl"]
			or not bank.is_company_account
		):
			raise ValueError("Bank account changed")
		filters = {
			"bank_account": scope["bank_account"],
			"date": ["between", [scope["from_date"], scope["to_date"]]],
			"docstatus": ["in", [0, 1]],
		}
		lines = [
			_line_row(_read("Bank Transaction", n)) for n in _names("Bank Transaction", filters, MAX_LINES)
		]
		vouchers = _payment_vouchers(scope) + _journal_vouchers(scope)
		if len(vouchers) > MAX_VOUCHERS:
			raise OverflowError
		for doctype in ("Payment Entry", "Journal Entry"):
			links = _links(doctype, [v["name"] for v in vouchers if v["doctype"] == doctype])
			for v in vouchers:
				if v["doctype"] == doctype:
					v["linked_bank_lines"] = links.get(v["name"], [])
		snapshot["datasets"] = {
			"bank_lines": lines,
			"vouchers": _plain(sorted(vouchers, key=lambda v: (v["doctype"], v["name"]))),
			"accounts": _plain(_accounts(scope["company"])),
		}
		snapshot["complete"] = True
	except frappe.PermissionError:
		snapshot["datasets"] = {}
		snapshot.update(
			reason_code="permission_slice", detail="Complete bank evidence is not readable by this user."
		)
	except OverflowError:
		snapshot["datasets"] = {}
		snapshot.update(
			reason_code="run_truncated_watermark",
			detail="Bank evidence exceeds the bounded review capacity; no reconciliation conclusion is available.",
		)
	except (frappe.DoesNotExistError, ValueError, InvalidOperation):
		snapshot["datasets"] = {}
		snapshot.update(
			reason_code="record_coverage_insufficient",
			detail="Source records changed or are incomplete; retry the run.",
		)
	snapshot["source_digest"] = digest(snapshot)
	return snapshot


def get_inputs(run):
	from jarvis.chat import operator_review

	return operator_review.get_inputs(run, operator_review.backend(AGENT))
