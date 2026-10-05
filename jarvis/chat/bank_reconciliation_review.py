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
	frappe.get_doc("Bank Account", config["bank_account"]).check_permission("read")
	return {key: config[key] for key in CONFIG_KEYS}


def scope_from_config(config):
	start = date.fromisoformat(config["from_date"])
	return {
		"company": config["company"],
		"bank_account": config["bank_account"],
		"bank_gl": _bank_account(config["bank_account"]).account,
		"from_date": config["from_date"],
		"to_date": config["to_date"],
		"voucher_from": (start - timedelta(days=config["voucher_lookback_days"])).isoformat(),
	}
