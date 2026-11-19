"""Who may use the built-in reader (OC2): only users for whom ERPNext's report narrows nothing.

The reader reads the ledger without the report's per-user narrowing (match conditions, permission
aware invoice details, Sales Person scope). It is correct only when that narrowing removes nothing
for the user, so every rule here fails closed: any doubt, error or unknown input is ineligible.
"""

import frappe
from frappe.core.doctype.server_script.server_script_utils import get_server_script_map
from frappe.core.doctype.user_permission.user_permission import get_user_permissions
from frappe.model import get_permitted_fields
from frappe.permissions import get_allowed_docs_for_doctype

from jarvis import permission_hooks
from jarvis.payment_calendar import compatibility, permissions
from jarvis.payment_calendar.errors import TIMEOUTS, ReaderFallback
from jarvis.tools._ledger_access import gl_dimension_doctypes

GUARDED_DOCTYPES = (
	"Sales Invoice",
	"Purchase Invoice",
	"Payment Entry",
	"Journal Entry",
	"Payment Ledger Entry",
	"GL Entry",
	"Account",
	"Customer",
	"Supplier",
	"Employee",
	"*",
)

_COMPANY_RESTRICTION = "erpnext.stock.doctype.company_restriction.company_restriction."

# The same ERPNext functions as permission_hooks.AUDITED_WILDCARD_HOOKS, plus the doctype-specific pair.
AUDITED_HOOKS = {
	"permission_query_conditions": frozenset(
		{
			_COMPANY_RESTRICTION + "get_permission_query_conditions",
			_COMPANY_RESTRICTION + "get_inherited_permission_query_conditions",
		}
	),
	"has_permission": frozenset(
		{
			_COMPANY_RESTRICTION + "has_permission",
			_COMPANY_RESTRICTION + "has_inherited_permission",
		}
	),
}

_AUDITED_SOURCE_NAMES = tuple(sorted(path for paths in AUDITED_HOOKS.values() for path in paths))

_HEADER_DOCTYPES = ("Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry")

# Every field the reader reads on the four voucher doctypes: discovery, invoice details, the
# reconciler's header read and future payments (accounts_receivable.py:472-533, :691-765).
_PARTY_INVOICE_FIELDS = {
	"name",
	"docstatus",
	"company",
	"posting_date",
	"due_date",
	"is_return",
	"return_against",
	"update_outstanding_for_self",
	"outstanding_amount",
	"currency",
	"party_account_currency",
	"conversion_rate",
	"grand_total",
	"rounded_total",
	"base_grand_total",
	"base_rounded_total",
	"total_advance",
	"write_off_amount",
}
READER_FIELDS = {
	"Sales Invoice": _PARTY_INVOICE_FIELDS | {"customer", "debit_to", "po_no", "sales_partner"},
	"Purchase Invoice": _PARTY_INVOICE_FIELDS | {"supplier", "credit_to", "bill_no", "bill_date"},
	"Journal Entry": {
		"name",
		"docstatus",
		"company",
		"posting_date",
		"voucher_type",
		"due_date",
		"bill_no",
		"bill_date",
		"cheque_no",
	},
	"Payment Entry": {
		"name",
		"docstatus",
		"posting_date",
		"party",
		"party_type",
		"payment_type",
		"reference_no",
		"source_exchange_rate",
		"target_exchange_rate",
	},
}


def ineligibility_reasons(
	*,
	company: str,
	scope_reasons: list[str],
	user_permissions: dict[str, list],
	allowed_companies: set[str] | None,
	restriction_configured: bool,
	hooks: dict[str, dict[str, list[str]]],
	server_script_queries: frozenset[str],
	audited_sources_ok: bool,
	linked_doctypes: frozenset[str],
	column_match_filters: dict,
	masked_report_fields: bool,
) -> list[str]:
	"""Sorted reasons the reader must not serve this user; an empty list means eligible.

	Pure: no frappe calls. `masked_report_fields` is True when any field the reader reads on the
	four voucher doctypes is masked or permlevel-restricted for the user. `server_script_queries` is the
	set of doctypes that have an enabled Server Script permission query: one on a guarded doctype
	narrows what the report reads, and nothing audits its code.
	"""
	reasons = set()
	if scope_reasons:
		reasons.add("incomplete_scope")
	# A User Permission on a doctype nothing links to narrows nothing; Company is judged below.
	if any(doctype != "Company" and doctype in linked_doctypes for doctype in user_permissions):
		reasons.add("user_permission")
	if allowed_companies is not None:
		if company not in allowed_companies:
			reasons.add("company_permission")
		# A Company permission that allows this company narrows nothing, unless a restriction applies.
		if restriction_configured:
			reasons.add("company_restriction")
	if _has_unaudited_hook(hooks) or (not audited_sources_ok and _has_audited_hook(hooks)):
		reasons.add("permission_hook")
	if server_script_queries & set(GUARDED_DOCTYPES):
		reasons.add("permission_hook")
	if column_match_filters or masked_report_fields:
		reasons.add("column_filter")
	return sorted(reasons)


def _registered(hooks: dict, kind: str, doctype: str) -> list[str] | None:
	"""Hook paths for one kind and doctype; None when the shape is unknown."""
	by_doctype = hooks.get(kind, {})
	if not isinstance(by_doctype, dict):
		return None
	paths = by_doctype.get(doctype, [])
	if isinstance(paths, str):
		return [paths]
	if isinstance(paths, (list, tuple)) and all(isinstance(path, str) for path in paths):
		return list(paths)
	return None


def _hook_paths(hooks):
	"""Every registered path on a guarded doctype as (kind, path); a bad shape yields (kind, None)."""
	if not isinstance(hooks, dict):
		yield "unknown", None
		return
	for kind in AUDITED_HOOKS:
		for doctype in GUARDED_DOCTYPES:
			paths = _registered(hooks, kind, doctype)
			if paths is None:
				yield kind, None
				continue
			for path in paths:
				yield kind, path


def _has_unaudited_hook(hooks) -> bool:
	return any(path is None or path not in AUDITED_HOOKS.get(kind, ()) for kind, path in _hook_paths(hooks))


def _has_audited_hook(hooks) -> bool:
	return any(path is not None and path in AUDITED_HOOKS.get(kind, ()) for kind, path in _hook_paths(hooks))


def linked_doctypes(columns: list[dict], party_types: list[str]) -> frozenset[str]:
	"""Doctypes whose User Permissions can narrow what the reader reads.

	Link targets of the report columns, of the Payment Ledger Entry fields and of the header
	fields of the four voucher doctypes (Warehouse is linked from Sales Invoice), the party
	types, the voucher doctypes themselves (a Dynamic Link target), Payment Ledger Entry itself
	(build_qb_match_conditions narrows by a User Permission on a ledger entry name), Sales Person
	(the report's Sales Person scope) and the GL dimensions.
	"""
	linked = {column["options"] for column in columns if _is_link_column(column)}
	for doctype in ("Payment Ledger Entry", *_HEADER_DOCTYPES):
		linked.update(df.options for df in frappe.get_meta(doctype).get_link_fields() if df.options)
	linked.update(party_types)
	linked.update(_HEADER_DOCTYPES)
	linked.update(("Payment Ledger Entry", "Sales Person"))
	linked.update(gl_dimension_doctypes())
	return frozenset(linked)


def _is_link_column(column) -> bool:
	return column.get("fieldtype") == "Link" and isinstance(column.get("options"), str)


def reader_fields_restricted() -> bool:
	"""True when any field the reader reads is masked or permlevel-restricted for the session user.

	Frappe filters these for the session user in query.py:1448 (permlevel) and db_query.py:250
	(mask), and Meta.get_masked_fields() has no user argument, so this checks the session user
	only. The reader serves the requesting user, who is the session user.
	"""
	for doctype, fields in READER_FIELDS.items():
		masked = {df.fieldname for df in frappe.get_meta(doctype).get_masked_fields()}
		if masked & fields:
			return True
		if not fields <= set(get_permitted_fields(doctype, permission_type="read")):
			return True
	return False


def reader_ineligibility(
	company: str, account_type: str, columns: list[dict], *, audited_sources_ok: bool | None = None
) -> list[str]:
	"""Live inputs for `ineligibility_reasons`; any error makes the user ineligible (fail closed).

	`audited_sources_ok` is True when the caller has already proved every reader pin (the four audited
	hook sources are among them); None checks the four here.

	An error is `ReaderFallback("eligibility_unknown")` with the error as `__cause__`, so the class of the
	failure stays recoverable. A job deadline is not an eligibility failure and passes through untouched.
	"""
	try:
		user = frappe.session.user
		user_permissions = get_user_permissions(user)
		company_permissions = user_permissions.get("Company")
		allowed = (
			set(get_allowed_docs_for_doctype(company_permissions, "Company")) if company_permissions else None
		)
		party_types = frappe.get_all("Party Type", filters={"account_type": account_type}, pluck="name")
		link_doctypes = sorted({c["options"] for c in columns if _is_link_column(c)})
		return ineligibility_reasons(
			company=company,
			scope_reasons=permissions.incomplete_scope_reasons(),
			user_permissions=user_permissions,
			allowed_companies=allowed,
			restriction_configured=permission_hooks._restriction_configured(),
			hooks={kind: frappe.get_hooks(kind, {}) for kind in AUDITED_HOOKS},
			server_script_queries=frozenset(get_server_script_map()["permission_query"]),
			audited_sources_ok=(
				compatibility.reader_pin_drift(_AUDITED_SOURCE_NAMES) == []
				if audited_sources_ok is None
				else audited_sources_ok
			),
			linked_doctypes=linked_doctypes(columns, party_types),
			column_match_filters=_column_match_filters(link_doctypes, user),
			masked_report_fields=reader_fields_restricted(),
		)
	except TIMEOUTS:
		raise
	except Exception as exc:
		raise ReaderFallback("eligibility_unknown") from exc


def _column_match_filters(doctypes: list[str], user: str) -> dict:
	from frappe.desk.query_report import get_user_match_filters

	return get_user_match_filters(doctypes, user)
