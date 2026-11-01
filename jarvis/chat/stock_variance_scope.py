"""Validate the Stock Variance install scope before a run can start.

The private evaluator receives only the declared ``stock_variance`` namespace.
The Warehouse boundary is a bench-owned run scope, so this module keeps the
two contracts separate while enforcing the link between the selected company
and the Warehouse tree.
"""

from __future__ import annotations

import frappe

from jarvis.permissions import is_valid_unattended_owner

AGENT = "stock-variance-auditor"
RECORD_SCOPE_KEYS = frozenset({"doctype", "name"})
STOCK_VARIANCE_KEYS = frozenset({"reviewer", "test_declaration"})


def is_stock_variance(agent: str | None) -> bool:
	return agent == AGENT


def configuration_issues(raw, *, require_complete: bool = True) -> dict[str, str]:
	"""Return safe, field-level configuration faults for the current user.

	A disabled installation may keep an empty draft. Once a value is supplied,
	it is checked at save time. Enablement and every launch call this with
	``require_complete=True`` so no run can fall back to an all-company scope.
	"""
	config = _parse(raw)
	if config is None:
		return {"config": "Configuration must be a JSON object."}

	issues: dict[str, str] = {}
	company = config.get("company")
	company_ok = False
	if company is None:
		if require_complete:
			issues["company"] = "Select a readable company."
	elif not _valid_text(company):
		issues["company"] = "Select a readable company."
	else:
		try:
			_validate_company(company)
			company_ok = True
		except (frappe.PermissionError, frappe.DoesNotExistError, frappe.ValidationError):
			issues["company"] = "Select a readable company."

	record_supplied = "record_scope" in config
	record = config.get("record_scope")
	if not record_supplied:
		if require_complete:
			issues["record_scope"] = "Select an enabled Warehouse group or leaf."
	elif not _valid_record_scope(record):
		issues["record_scope"] = "Select an enabled Warehouse group or leaf."
	elif not company_ok:
		issues["record_scope"] = "Select a company before choosing a Warehouse scope."
	else:
		try:
			_validate_warehouse_root(record["name"], company)
		except (frappe.PermissionError, frappe.DoesNotExistError, frappe.ValidationError, ValueError):
			issues["record_scope"] = "Select an enabled Warehouse group or leaf in the selected company."

	settings_supplied = "stock_variance" in config
	settings = config.get("stock_variance")
	if not settings_supplied:
		if require_complete:
			issues["stock_variance.reviewer"] = "Select an enabled stock reviewer."
	elif not isinstance(settings, dict) or set(settings) - STOCK_VARIANCE_KEYS:
		issues["stock_variance"] = "Use only the supported stock review settings."
	else:
		reviewer = settings.get("reviewer")
		if not _valid_text(reviewer) or not is_valid_unattended_owner(reviewer):
			if require_complete or reviewer is not None:
				issues["stock_variance.reviewer"] = "Select an enabled stock reviewer."
		declaration = settings.get("test_declaration", "")
		if not isinstance(declaration, str) or len(declaration) > 500:
			issues["stock_variance.test_declaration"] = "Keep the test notice to 500 characters or less."

	return issues


def validate_saved_configuration(raw) -> dict:
	"""Reject malformed supplied values but allow a disabled setup draft."""
	issues = configuration_issues(raw, require_complete=False)
	if issues:
		frappe.throw(list(issues.values()), title="Check Stock Variance configuration", as_list=True)
	return _parse(raw) or {}


def require_configuration(raw) -> dict:
	"""Require a complete, readable Warehouse scope for enablement and launch."""
	issues = configuration_issues(raw, require_complete=True)
	if issues:
		frappe.throw(list(issues.values()), title="Complete Stock Variance configuration", as_list=True)
	return _parse(raw) or {}


def _parse(raw) -> dict | None:
	try:
		value = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (TypeError, ValueError):
		return None
	return value if isinstance(value, dict) else None


def _valid_text(value) -> bool:
	return isinstance(value, str) and bool(value) and value == value.strip()


def _valid_record_scope(record) -> bool:
	return (
		isinstance(record, dict)
		and set(record) == RECORD_SCOPE_KEYS
		and record.get("doctype") == "Warehouse"
		and _valid_text(record.get("name"))
	)


def _validate_company(name: str) -> None:
	"""Check one readable leaf Company before its Warehouse tree is used.

	The native evaluator requires a leaf Company row. Check the same condition
	before an installation can be enabled or launched, so a group Company cannot
	be saved as a scope that will later fail in the evaluator.
	"""
	company = frappe.get_doc("Company", name)
	company.check_permission("read")
	is_group = company.get("is_group")
	company.apply_fieldlevel_read_permissions()
	if is_group is None or company.get("is_group") != is_group:
		raise frappe.PermissionError("Company group status is not readable.")
	if frappe.utils.cint(is_group):
		raise frappe.ValidationError("Company scope must be a leaf Company.")


def _validate_warehouse_root(name: str, company: str) -> None:
	"""Check one readable enabled root and one readable enabled leaf below it."""
	warehouse = frappe.get_doc("Warehouse", name)
	warehouse.check_permission("read")
	fields = ("name", "company", "is_group", "disabled", "lft", "rgt")
	before = {field: warehouse.get(field) for field in fields}
	warehouse.apply_fieldlevel_read_permissions()
	if before != {field: warehouse.get(field) for field in fields}:
		raise frappe.PermissionError("Required Warehouse fields are not readable.")
	if warehouse.company != company or frappe.utils.cint(warehouse.disabled):
		raise frappe.ValidationError("Warehouse is outside the selected company or disabled.")
	try:
		left, right = int(warehouse.lft), int(warehouse.rgt)
	except (TypeError, ValueError) as exc:
		raise ValueError("Warehouse tree bounds are invalid.") from exc
	if left >= right:
		raise ValueError("Warehouse tree bounds are invalid.")
	leaf = frappe.get_list(
		"Warehouse",
		filters={
			"company": company,
			"disabled": 0,
			"is_group": 0,
			"lft": (">=", left),
			"rgt": ("<=", right),
		},
		pluck="name",
		limit=1,
	)
	if not leaf:
		raise frappe.ValidationError("Warehouse scope has no readable enabled leaf.")
