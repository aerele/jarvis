"""Effective finance access and current permission proofs for every delivery."""

import json
from copy import deepcopy
from hashlib import sha256

import frappe
from frappe.utils.safe_exec import is_safe_exec_enabled

from jarvis.exceptions import PermissionDeniedError
from jarvis.payment_calendar.compatibility import has_native_field_masks, require_obligation_scope
from jarvis.payment_calendar.errors import CalendarSourceUnavailable, ScopeChanged
from jarvis.payment_calendar.permissions import (
	incomplete_scope_reasons,
	require_company,
	require_report,
	row_filter_columns,
	visible_names,
)
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.settings import get_settings
from jarvis.permissions import require_jarvis_access
from jarvis.tools._ledger_access import gl_dimension_doctypes

REFRESH = "Refresh the Payment Calendar snapshot."
# What the Accounts Payable / Receivable Link columns point at (not already listed in `_doctypes`): the report row
# filter applies user permissions on them, so a change to their permission rows must change the fingerprint.
REPORT_LINK_DOCTYPES = (
	"Contact",
	"Currency",
	"Customer Group",
	"DocType",
	"Payment Term",
	"Project",
	"Sales Person",
	"Supplier Group",
	"Territory",
)
# The scope keys that follow from the site day. A stored scope that differs only in these is stale, not changed.
_DAY_KEYS = ("anchor", "selectable_start", "selectable_end")
_WINDOW_KEYS = ("window_start", "window_end")
# A scope rebuild can fail like this when access or an account is gone; for a stored proof it is a changed scope.
_REVOKED = (frappe.ValidationError, frappe.PermissionError, PermissionDeniedError, frappe.DoesNotExistError)


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
			*REPORT_LINK_DOCTYPES,
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


# One name per key of ``fingerprint_data``; a test keeps the two equal.
FINGERPRINT_INPUTS = (
	"scope",
	"user",
	"roles",
	"role_definitions",
	"user_permissions",
	"doc_permissions",
	"custom_permissions",
	"reports",
	"report_roles",
	"custom_report_roles",
	"custom_role_children",
	"dimensions",
	"party_types",
	"properties",
	"custom_fields",
	"field_permissions",
	"shares",
	"company",
	"accounts",
	"floor",
	"defaults",
	"system",
	"fiscal_years",
	"fiscal_companies",
	"fetch",
	"hooks",
	"server_scripts",
	"server_script_enabled",
	"domains",
)


def policy_fingerprint(scope: dict) -> str:
	"""Hash actual rows, never a count/max-modified proxy or cached role names."""
	return fingerprint_hash(fingerprint_data(scope))


def fingerprint_hash(data: dict) -> str:
	return sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def fingerprint_data(scope: dict) -> dict:
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
				*(["mask"] if has_native_field_masks() else []),
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
		# get_list applies Permission Query scripts, so their text and state decide row access.
		"server_scripts": _rows(
			"Server Script",
			{"script_type": "Permission Query", "reference_doctype": ["in", types]},
			["name", "reference_doctype", "disabled", "script", "modified"],
		),
		"server_script_enabled": is_safe_exec_enabled(),
		"domains": _rows("Has Domain", {"parent": "Domain Settings"}, ["name", "domain"]),
	}
	return data


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
	if frappe.session.user == "Administrator" or not has_native_field_masks():
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
	"""Recheck every source row, also the excluded ones, before a result is published."""
	for side, source in result.get("sources", {}).items():
		if source is not None:
			_check_rows(
				scope, side, source["permission_columns"], source["rows"] + source.get("excluded_rows", [])
			)


def verify_returned_rows(scope: dict, side: str, permission_columns: list, rows: list, parties=()) -> None:
	"""Read-path check of the stored rows a reply returns, plus the party keys it lists.

	`parties` are (party_type, party) pairs. A hidden or changed row raises ScopeChanged
	for the whole read; an error from a hook or a lookup propagates as itself.
	"""
	_check_rows(scope, side, permission_columns, rows, parties)


def _check_rows(scope: dict, side: str, permission_columns: list, rows: list, parties=()) -> None:
	"""The one visibility rule shared by publish and every read."""
	if not rows and not parties:
		return
	report = require_report(scope["company"], side)
	if rows:
		_check_proofs(report, permission_columns, rows, scope["actor"])
		_check_vouchers(scope["company"], rows)
	_check_parties(
		{*(tuple(party) for party in parties), *((row["party_type"], row["party"]) for row in rows)}
	)


def _check_proofs(report, permission_columns, rows, actor) -> None:
	"""The report's own row filter must still keep every stored proof."""
	from frappe.desk.query_report import get_filtered_data

	# Only a damaged proof or a changed row shape is a scope change. A hook or lookup
	# error must stay an error, so nothing else is caught here.
	try:
		proofs = [frappe._dict(deepcopy(row["_permission"])) for row in rows]
		# Link-to-DocType columns would make get_filtered_data read DocType and refuse
		# users who are not System Managers (see row_filter_columns).
		filtered = get_filtered_data(
			report.ref_doctype, row_filter_columns(permission_columns), proofs, actor
		)
	except (KeyError, TypeError, AttributeError, ValueError) as exc:
		raise ScopeChanged(REFRESH, "proof") from exc
	if len(filtered) != len(rows):
		raise ScopeChanged(REFRESH, "proof")


def _check_vouchers(company: str, rows) -> None:
	for doctype in {row["voucher_type"] for row in rows}:
		names = {row["voucher_no"] for row in rows if row["voucher_type"] == doctype}
		filters = {"docstatus": 1}
		if frappe.get_meta(doctype).has_field("company"):
			filters["company"] = company
		if visible_names(doctype, names, filters) != names:
			raise ScopeChanged(REFRESH, "voucher")


def _check_parties(party_keys) -> None:
	for party_type in {key[0] for key in party_keys}:
		names = {name for kind, name in party_keys if kind == party_type}
		if visible_names(party_type, names) != names:
			raise ScopeChanged(REFRESH, "party")


def authorize_scope(
	scope: dict,
	expected_fingerprint: str | None = None,
	result: dict | None = None,
	*,
	check_accounts: bool = True,
	actor_refreshed: bool = False,
	sides: tuple = (),
) -> dict:
	"""Prove the scope for the current actor.

	A read passes `actor_refreshed=True` after its own `refresh_actor_access`, `sides` (the
	obligation sides it returns) and, except for the one reply that shows bank balances,
	`check_accounts=False`. A published `result` brings its own sides. A read (`actor_refreshed`) gets no
	`floor` and no `scope_reasons`; only a full authorization looks them up.
	"""
	actor = frappe.session.user if actor_refreshed else refresh_actor_access()
	if scope.get("actor") != actor:
		raise frappe.PermissionError("Payment Calendar access is unavailable.")
	# Refresh metadata for a new authorization proof. Existing proofs are first
	# compared with live database policy rows; normal Frappe caches remain intact.
	if expected_fingerprint is None:
		for doctype in _doctypes():
			frappe.get_meta(doctype, cached=False)
		frappe.local.role_permissions = {}
	_require_current_scope(scope, expected_fingerprint, check_accounts)
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
	fingerprint = policy_fingerprint(scope)
	if expected_fingerprint is not None and fingerprint != expected_fingerprint:
		raise ScopeChanged(REFRESH, "fingerprint")
	_check_masks(reports)
	for side in dict.fromkeys([*sides, *_present_sides(result)]):
		require_obligation_scope(side)
	if result is not None:
		validate_result_visibility(scope, result)
	proof = {
		"actor": actor,
		"fingerprint": fingerprint,
		"reports": reports,
		"currency": company.default_currency,
	}
	if actor_refreshed:
		# A read ignores the floor and the scope reasons (the floor row is fingerprinted): skip both lookups.
		return proof
	reasons = incomplete_scope_reasons()
	if not reports["Receivable"]:
		reasons = sorted({*reasons, "receivables_unavailable"})
	return {**proof, "floor": get_settings(scope["company"]), "scope_reasons": reasons}


def _require_current_scope(scope, expected_fingerprint, check_accounts) -> None:
	"""The stored scope must be what the server would build now (same company, accounts, day)."""
	try:
		current = normalize_scope(
			scope["company"], scope["accounts"], month=scope.get("month"), check_accounts=check_accounts
		)
	except _REVOKED as exc:
		# A proof made earlier no longer holds (a disabled scope account, a company or account no longer permitted).
		if expected_fingerprint is None:
			raise
		raise ScopeChanged(REFRESH) from exc
	if current != scope:
		raise ScopeChanged(REFRESH, "anchor" if _only_the_day_differs(scope, current) else "scope")


def _only_the_day_differs(scope, current) -> bool:
	"""True when the two scopes differ in the site day and what follows from it, and in nothing else."""
	keys = (*_DAY_KEYS, *(() if "month" in scope else _WINDOW_KEYS))

	def rest(value):
		return {key: item for key, item in value.items() if key not in keys}

	return scope.get("anchor") != current["anchor"] and rest(scope) == rest(current)


def _present_sides(result: dict | None):
	if result is None:
		return []
	return [side for side, source in result.get("sources", {}).items() if source is not None]
