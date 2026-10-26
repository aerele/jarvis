"""Effective finance access and current permission proofs for every delivery."""

import json
from copy import deepcopy
from hashlib import sha256

import frappe

from jarvis.exceptions import PermissionDeniedError
from jarvis.payment_calendar.banks import validate_bank_accounts
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.permissions import (
	incomplete_scope_reasons,
	require_company,
	require_report,
	visible_names,
)
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.settings import get_settings
from jarvis.permissions import require_jarvis_access
from jarvis.tools._ledger_access import gl_dimension_doctypes


class ScopeChanged(frappe.PermissionError):
	code = "scope_changed"


def refresh_actor_access() -> str:
	actor = frappe.session.user
	user = frappe.db.get_value("User", actor, ["enabled", "user_type"], as_dict=True)
	if not user or not user.enabled or user.user_type != "System User" or actor == "Guest":
		raise frappe.PermissionError("Payment Calendar access is unavailable.")
	frappe.cache.hdel("roles", actor)
	frappe.cache.hdel("user_permissions", actor)
	frappe.clear_document_cache("User", actor)
	frappe.local.role_permissions = {}
	require_jarvis_access()
	return actor


def _doctypes():
	configured = frappe.get_all(
		"Party Type", filters={"account_type": ["in", ["Payable", "Receivable"]]}, pluck="name"
	)
	return sorted(
		{
			"Company",
			"Account",
			"GL Entry",
			"Payment Ledger Entry",
			"Purchase Invoice",
			"Sales Invoice",
			"Journal Entry",
			"Payment Entry",
			"Supplier",
			"Customer",
			"Report",
			"Payment Schedule",
			"Purchase Taxes and Charges",
			*gl_dimension_doctypes(),
			*(dt for dt in configured if frappe.db.exists("DocType", dt)),
		}
	)


def _rows(doctype, filters=None, fields=None, or_filters=None):
	return frappe.get_all(
		doctype,
		filters=filters or {},
		or_filters=or_filters,
		fields=fields or ["*"],
		order_by="name",
		limit_page_length=0,
	)


def _hook_identity(value):
	if callable(value):
		return value.__module__ + "." + value.__qualname__
	if isinstance(value, dict):
		return {key: _hook_identity(item) for key, item in value.items()}
	if isinstance(value, (list, tuple)):
		return [_hook_identity(item) for item in value]
	return value


def policy_fingerprint(scope: dict) -> str:
	"""Hash actual rows, never a count/max-modified proxy or cached role names."""
	actor, types = scope["actor"], _doctypes()
	custom_roles = _rows("Custom Role", {"report": ["in", ["Accounts Payable", "Accounts Receivable"]]})
	data = {
		"scope": scope,
		"user": frappe.db.get_value(
			"User", actor, ["creation", "enabled", "user_type", "role_profile_name"], as_dict=True
		),
		"roles": _rows("Has Role", {"parent": actor, "parenttype": "User"}),
		"role_definitions": _rows(
			"Role", fields=["name", "modified", "disabled", "desk_access", "restrict_to_domain"]
		),
		"user_permissions": _rows("User Permission", {"user": actor}),
		"doc_permissions": _rows("DocPerm", {"parent": ["in", types]}),
		"custom_permissions": _rows("Custom DocPerm", {"parent": ["in", types]}),
		"reports": _rows(
			"Report",
			{"name": ["in", ["Accounts Payable", "Accounts Receivable"]]},
			[
				"name",
				"modified",
				"disabled",
				"ref_doctype",
				"module",
				"report_type",
				"is_standard",
				"prepared_report",
			],
		),
		"report_roles": _rows(
			"Has Role",
			{"parenttype": "Report", "parent": ["in", ["Accounts Payable", "Accounts Receivable"]]},
		),
		"custom_report_roles": custom_roles,
		"custom_role_children": _rows(
			"Has Role", {"parenttype": "Custom Role", "parent": ["in", [row.name for row in custom_roles]]}
		)
		if custom_roles
		else [],
		"dimensions": _rows("Accounting Dimension"),
		"party_types": _rows("Party Type", fields=["name", "modified", "account_type"]),
		"properties": _rows("Property Setter", {"doc_type": ["in", types]}),
		"custom_fields": _rows("Custom Field", {"dt": ["in", types]}),
		"field_permissions": _rows(
			"DocField",
			{"parent": ["in", types]},
			[
				"name",
				"parent",
				"fieldname",
				"fieldtype",
				"options",
				"permlevel",
				"mask",
				"ignore_user_permissions",
			],
		),
		"shares": _rows(
			"DocShare", {"share_doctype": ["in", types]}, or_filters={"user": actor, "everyone": 1}
		),
		"company": frappe.db.get_value(
			"Company", scope["company"], ["name", "default_currency", "default_finance_book"], as_dict=True
		),
		"accounts": _rows(
			"Account",
			{"name": ["in", scope["accounts"]]},
			["name", "company", "disabled", "is_group", "account_type", "account_currency", "modified"],
		)
		if scope["accounts"]
		else [],
		"floor": frappe.db.get_value(
			"Jarvis Payment Calendar Settings",
			{"company": scope["company"]},
			["name", "currency", "safety_floor", "revision"],
			as_dict=True,
		),
		"defaults": _rows(
			"DefaultValue", {"defkey": ["in", ["currency_precision", "number_format", "time_zone"]]}
		),
		"system": {
			field: frappe.db.get_single_value("System Settings", field, cache=False)
			for field in ("apply_strict_user_permissions", "time_zone", "rounding_method")
		},
		"fiscal_years": _rows(
			"Fiscal Year", fields=["name", "year_start_date", "year_end_date", "disabled", "modified"]
		),
		"fiscal_companies": _rows("Fiscal Year Company"),
		"fetch": frappe.db.get_single_value(
			"Accounts Settings", "receivable_payable_fetch_method", cache=False
		),
		"hooks": {
			key: _hook_identity(frappe.get_hooks(key))
			for key in ("permission_query_conditions", "has_permission")
		},
	}
	return sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


_MASK_FIELDS = {
	"GL Entry": {"company", "account", "posting_date", "debit", "credit"},
	"Payment Ledger Entry": {
		"company",
		"account",
		"posting_date",
		"party",
		"amount",
		"amount_in_account_currency",
	},
	"Purchase Invoice": {
		"posting_date",
		"due_date",
		"supplier",
		"currency",
		"credit_to",
		"grand_total",
		"rounded_total",
		"base_rounded_total",
		"party_account_currency",
		"write_off_amount",
		"base_grand_total",
		"outstanding_amount",
		"total_advance",
		"conversion_rate",
		"cost_center",
		"project",
	},
	"Sales Invoice": {
		"posting_date",
		"due_date",
		"customer",
		"currency",
		"debit_to",
		"grand_total",
		"rounded_total",
		"base_rounded_total",
		"party_account_currency",
		"write_off_amount",
		"base_grand_total",
		"outstanding_amount",
		"total_advance",
		"conversion_rate",
		"cost_center",
		"project",
	},
	"Payment Schedule": {"due_date", "base_payment_amount"},
	"Purchase Taxes and Charges": {"is_tax_withholding_account", "base_tax_amount_after_discount_amount"},
	"Supplier": {"supplier_name"},
	"Customer": {"customer_name"},
	"Company": {"default_currency", "default_finance_book"},
	"Account": {"company", "account_currency", "account_type"},
}


def _check_masks(reports):
	if frappe.session.user == "Administrator":
		return
	for doctype, fields in _MASK_FIELDS.items():
		if doctype in ("Sales Invoice", "Customer") and not reports["Receivable"]:
			continue
		meta = frappe.get_meta(doctype)
		levels = set(meta.get_permlevel_access("mask"))
		if any(
			df.get("mask") and df.fieldname in fields and df.permlevel not in levels for df in meta.fields
		):
			raise CalendarSourceUnavailable("masked_source_unsupported")


def validate_result_visibility(scope: dict, result: dict) -> None:
	"""Recheck every source row before returning even aggregate calendar amounts."""
	from frappe.desk.query_report import get_filtered_data

	for side, source in result.get("sources", {}).items():
		if source is None:
			continue
		report = require_report(scope["company"], side)
		rows = source["rows"] + source.get("excluded_rows", [])
		try:
			proofs = [frappe._dict(deepcopy(row["_permission"])) for row in rows]
			filtered = get_filtered_data(
				report.ref_doctype, source["permission_columns"], proofs, scope["actor"]
			)
		except (KeyError, TypeError, AttributeError) as exc:
			raise ScopeChanged("Refresh the Payment Calendar snapshot.") from exc
		if len(filtered) != len(rows):
			raise ScopeChanged("Refresh the Payment Calendar snapshot.")
		for doctype in {row["voucher_type"] for row in rows}:
			names = {row["voucher_no"] for row in rows if row["voucher_type"] == doctype}
			filters = {"docstatus": 1}
			if frappe.get_meta(doctype).has_field("company"):
				filters["company"] = scope["company"]
			if visible_names(doctype, names, filters) != names:
				raise ScopeChanged("Refresh the Payment Calendar snapshot.")
		for doctype in {row["party_type"] for row in rows}:
			names = {row["party"] for row in rows if row["party_type"] == doctype}
			if visible_names(doctype, names) != names:
				raise ScopeChanged("Refresh the Payment Calendar snapshot.")


def authorize_scope(scope: dict, expected_fingerprint: str | None = None, result: dict | None = None) -> dict:
	actor = refresh_actor_access()
	if scope.get("actor") != actor:
		raise frappe.PermissionError("Payment Calendar access is unavailable.")
	# Refresh metadata for a new authorization proof. Existing proofs are first
	# compared with live database policy rows; normal Frappe caches remain intact.
	if expected_fingerprint is None:
		for doctype in _doctypes():
			frappe.get_meta(doctype, cached=False)
		frappe.local.role_permissions = {}
	if normalize_scope(scope["company"], scope["accounts"]) != scope:
		raise ScopeChanged("Refresh the Payment Calendar snapshot.")
	company = require_company(scope["company"])
	if not company.default_currency or not frappe.db.exists("Currency", company.default_currency):
		raise CalendarSourceUnavailable("source_configuration_unavailable")
	try:
		require_report(scope["company"], "Payable")
	except (frappe.PermissionError, frappe.ValidationError, PermissionDeniedError) as exc:
		raise frappe.PermissionError("Accounts Payable report access is required.") from exc
	reports = {"Payable": True, "Receivable": True}
	try:
		require_report(scope["company"], "Receivable")
	except (frappe.PermissionError, frappe.ValidationError, PermissionDeniedError):
		reports["Receivable"] = False
	if scope["accounts"]:
		validate_bank_accounts(scope["company"], scope["accounts"])
	fingerprint = policy_fingerprint(scope)
	if expected_fingerprint is not None and fingerprint != expected_fingerprint:
		raise ScopeChanged("Refresh the Payment Calendar snapshot.")
	_check_masks(reports)
	if result is not None:
		validate_result_visibility(scope, result)
	reasons = incomplete_scope_reasons()
	if not reports["Receivable"]:
		reasons = sorted({*reasons, "receivables_unavailable"})
	return {
		"actor": actor,
		"fingerprint": fingerprint,
		"reports": reports,
		"floor": get_settings(scope["company"]),
		"scope_reasons": reasons,
		"currency": company.default_currency,
	}
