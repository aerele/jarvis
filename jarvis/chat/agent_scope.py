"""Generic, zero-IP scope / period resolution for agent runs (amendment A6).

Resolves an explicit
``{company, fiscal_year, from_date, to_date, prior_fy_start, prior_fy_end}``
from an installation's ``config`` JSON plus the site date, so the container
bundle NEVER infers "the current period" itself (a UTC container clock vs an IST
site picks the wrong fiscal year for ~5.5h every day — catastrophic at the
Mar-31 / Apr-1 boundary). ``_launch_audit`` stamps the result on the Run
(``scope_json``) and injects it verbatim into the run message; prior-FY
selection stays versioned bench code, never LLM prose.

It is DELIBERATELY generic: it carries no rule shape, only ordinary accounting
dimensions (company / fiscal year / period boundaries), so the delegate bundles
resolve their run scope from bench-injected, versioned values — never LLM prose.

The helper runs under the caller's identity (the run-as user in
``_launch_audit``), so ``frappe.defaults`` / erpnext's fiscal-year lookup are
resolved for that user.
"""

import hashlib
import json
import re

import frappe
from frappe import _

from jarvis.exceptions import InvalidArgumentError

INSTALLATION = "Jarvis Agent Installation"
FEFO_AGENT = "fefo-expiry-compliance-auditor"
_FEFO_OUTPUT_SCOPE_KEYS = ("company", "from_date", "to_date", "report_date")
_FEFO_SCOPE_CONTRACT_KEY = "fefo_scope_contract"
_FEFO_SCOPE_CONTRACT = "fefo-expiry-compliance-v0.4.2"
_FEFO_VISIBILITY_PROOF_SCHEMA = "fefo-native-visibility-proof-v1"
_FEFO_VISIBILITY_PROOF_KEYS = frozenset({"schema", "complete", "policy_sha256"})
# Every relation the fixed FEFO source plan reads.  A child table must be
# authorised through the one parent the plan uses; a direct child check is not
# meaningful in Frappe and must never turn into a false full-coverage verdict.
_FEFO_VISIBILITY_SURFACE = (
	("Company", None),
	("Warehouse", None),
	("Item", None),
	("Batch", None),
	("Stock Ledger Entry", None),
	("Serial and Batch Bundle", None),
	("Serial and Batch Entry", "Serial and Batch Bundle"),
	("Stock Entry", None),
	("Delivery Note", None),
	("Sales Invoice", None),
	("Repost Item Valuation", None),
	("Stock Closing Entry", None),
	("Stock Closing Balance", "Stock Closing Entry"),
	("Inventory Dimension", None),
)
# A Company User Permission is safe only when it can reach the source plan
# through these known canonical Company links.  Frappe applies a Company User
# Permission to *every* non-ignored Link -> Company field.  A custom Company
# link on Item, Batch, or another source would make the permission-woven
# source query a slice while Batch-Wise Balance History remains a raw query.
# Keep this list deliberately small and reject a new link until its raw and
# permission-woven behaviour is reviewed together.
_FEFO_COMPANY_LINK_ALLOWLIST = {
	"Warehouse": frozenset({"company"}),
	"Stock Ledger Entry": frozenset({"company"}),
	"Serial and Batch Bundle": frozenset({"company"}),
	"Stock Entry": frozenset({"company"}),
	"Delivery Note": frozenset({"company"}),
	"Sales Invoice": frozenset({"company"}),
	"Repost Item Valuation": frozenset({"company"}),
	"Stock Closing Entry": frozenset({"company"}),
	"Stock Closing Balance": frozenset({"company"}),
}
# Batch-Wise Balance History reads these fields with raw query-builder or
# get_all calls.  Normal query field-permission and mask checks do not protect
# these reads.  A "*" Stock Closing Balance select is represented by None and
# must prove every valid database column is public and unmasked.
_FEFO_NATIVE_REPORT_FIELDS = {
	"Warehouse": frozenset({"name", "lft", "rgt"}),
	"Item": frozenset({"name", "item_name", "description", "stock_uom", "item_group", "has_serial_no"}),
	"Stock Ledger Entry": frozenset(
		{
			"name",
			"item_code",
			"warehouse",
			"batch_no",
			"posting_date",
			"posting_time",
			"posting_datetime",
			"actual_qty",
			"qty_after_transaction",
			"stock_value_difference",
			"stock_value",
			"valuation_rate",
			"stock_uom",
			"docstatus",
			"is_cancelled",
			"has_batch_no",
			"serial_and_batch_bundle",
			"voucher_no",
			"company",
		}
	),
	"Serial and Batch Entry": frozenset({"parent", "batch_no", "qty", "stock_value_difference", "warehouse"}),
	"Stock Closing Entry": frozenset({"name", "to_date", "company", "docstatus", "status", "creation"}),
	"Stock Closing Balance": None,
	"Inventory Dimension": frozenset(
		{
			"name",
			"target_fieldname",
			"source_fieldname",
			"reference_document",
			"validate_negative_stock",
			"creation",
		}
	),
}
# This is the one server-owned copy of the FEFO query source plan.  It must
# stay in lock step with ``fefo_native_inputs.query_specs`` in the signed
# bundle.  The visibility proof compiles these specs with the real query tool
# before a run starts.  Thus an evaluator cannot claim complete coverage when
# one of its required select, predicate, join, order, or EXISTS fields is not
# readable for the run-as user.
_FEFO_QUERY_SOURCE_FIELDS = (
	("company", "Company", ("name", "default_currency", "is_group", "creation", "modified")),
	("warehouses", "Warehouse", ("name", "company", "is_group", "disabled", "creation", "modified")),
	(
		"items",
		"Item",
		(
			"name",
			"is_stock_item",
			"has_variants",
			"has_batch_no",
			"has_serial_no",
			"stock_uom",
			"creation",
			"modified",
		),
	),
	("batches", "Batch", ("name", "item", "expiry_date", "disabled", "creation", "modified")),
	(
		"ledger",
		"Stock Ledger Entry",
		(
			"name",
			"company",
			"item_code",
			"warehouse",
			"posting_date",
			"posting_datetime",
			"actual_qty",
			"qty_after_transaction",
			"stock_value_difference",
			"stock_value",
			"valuation_rate",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"batch_no",
			"serial_and_batch_bundle",
			"has_batch_no",
			"has_serial_no",
			"is_cancelled",
			"docstatus",
			"creation",
			"modified",
		),
	),
	(
		"bundles",
		"Serial and Batch Bundle",
		(
			"name",
			"company",
			"item_code",
			"warehouse",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"docstatus",
			"is_cancelled",
			"has_batch_no",
			"has_serial_no",
			"type_of_transaction",
			"total_qty",
			"creation",
			"modified",
		),
	),
	(
		"bundle_entries",
		"Serial and Batch Entry",
		(
			"name",
			"parent",
			"parenttype",
			"parentfield",
			"idx",
			"batch_no",
			"serial_no",
			"qty",
			"warehouse",
			"stock_value_difference",
			"creation",
			"modified",
		),
	),
	("stock_entries", "Stock Entry", ("name", "company", "purpose", "docstatus", "creation", "modified")),
	(
		"deliveries",
		"Delivery Note",
		("name", "company", "is_return", "is_internal_customer", "docstatus", "creation", "modified"),
	),
	(
		"invoices",
		"Sales Invoice",
		(
			"name",
			"company",
			"is_return",
			"is_internal_customer",
			"update_stock",
			"docstatus",
			"creation",
			"modified",
		),
	),
	(
		"reposts",
		"Repost Item Valuation",
		("name", "company", "warehouse", "status", "docstatus", "creation", "modified"),
	),
	("dimensions", "Inventory Dimension", ("name", "creation", "modified")),
	(
		"closings",
		"Stock Closing Entry",
		("name", "company", "docstatus", "from_date", "to_date", "creation", "modified"),
	),
)


def authoritative_writeback_scope(run_doc, submitted_scope) -> dict:
	"""Return the launch-stamped scope after checking the evaluator echo.

	The evaluator receives the scope in its run message and returns the scope it
	used with its findings. That return value is useful as a consistency check,
	but it is not an authority to change a run. In particular, it must never
	select the company used for finding persistence or auto-resolution.

	An evaluator may return only the dimensions that apply to its data model. For
	example, a CRM evaluator does not bind a Company dimension. Each supplied key
	must therefore exist in, and exactly match, the server-stamped launch scope.
	A scoped run also requires a non-empty evaluator scope, so a missing result
	cannot silently bypass the check. An unscoped legacy run may return an empty
	scope, but it may not introduce a scope of its own.
	"""
	# Older unscoped launches leave scope_json unset. Treat that as an empty server
	# scope, never as permission for the evaluator to add a replacement scope.
	stamped = _scope_object(run_doc.get("scope_json"), label="server-stamped run scope", allow_missing=True)
	submitted = _scope_object(submitted_scope, label="evaluator scope", allow_missing=True)

	if not stamped:
		if submitted:
			raise InvalidArgumentError("evaluator scope cannot add dimensions to an unscoped run")
		return {}
	if not submitted:
		raise InvalidArgumentError("evaluator scope is required for a scoped run")

	for key, value in submitted.items():
		if key not in stamped:
			raise InvalidArgumentError(f"evaluator scope has unstamped dimension: {key}")
		if stamped[key] != value:
			raise InvalidArgumentError(f"evaluator scope does not match server-stamped {key}")

	# A fresh dict prevents later code from mutating the run's trusted stamp.
	return dict(stamped)


def _scope_object(value, *, label: str, allow_missing: bool = False) -> dict:
	"""Parse one scope object without the old silent fallback-to-empty behavior."""
	if value is None and allow_missing:
		return {}
	if isinstance(value, str):
		try:
			value = frappe.parse_json(value)
		except Exception:
			raise InvalidArgumentError(f"{label} must be a JSON object") from None
	if not isinstance(value, dict):
		raise InvalidArgumentError(f"{label} must be a JSON object")
	return value


def fefo_record_scope(value) -> dict | None:
	"""Return the exact optional FEFO Warehouse leaf declaration."""
	if (
		not isinstance(value, dict)
		or set(value) != {"doctype", "name"}
		or value.get("doctype") != "Warehouse"
		or not isinstance(value.get("name"), str)
		or not value["name"].strip()
		or value["name"] != value["name"].strip()
	):
		return None
	return {"doctype": "Warehouse", "name": value["name"]}


def _fefo_warehouse_boundary(value, company: str) -> dict:
	"""Validate one FEFO Warehouse leaf as the active run-as user."""
	boundary = fefo_record_scope(value)
	if boundary is None:
		raise InvalidArgumentError("FEFO record_scope must name one readable enabled Warehouse leaf")
	name = boundary["name"]
	try:
		warehouse = frappe.db.get_value("Warehouse", name, ["company", "is_group", "disabled"], as_dict=True)
		warehouse_readable = frappe.has_permission("Warehouse", "read", doc=name)
		company_readable = frappe.has_permission("Company", "read", doc=company)
	except Exception as exc:
		raise InvalidArgumentError("FEFO Warehouse scope could not be verified") from exc
	if (
		not warehouse
		or not warehouse_readable
		or not company_readable
		or warehouse.get("company") != company
		or warehouse.get("is_group") is None
		or warehouse.get("disabled") is None
		or frappe.utils.cint(warehouse.get("is_group"))
		or frappe.utils.cint(warehouse.get("disabled"))
	):
		raise InvalidArgumentError("FEFO record_scope must name one readable enabled Warehouse leaf")
	return boundary


def _fefo_warehouse_scope(config: dict, company: str) -> dict | None:
	"""Return an optional FEFO leaf and preserve absent as company-wide."""
	if not record_scope_requested(config):
		return None
	return _fefo_warehouse_boundary(config.get("record_scope"), company)


def fefo_output_scope(value) -> dict | None:
	"""Project the immutable FEFO writeback boundary, or reject it."""
	if not isinstance(value, dict):
		return None
	output = {}
	for key in _FEFO_OUTPUT_SCOPE_KEYS:
		item = value.get(key)
		if not isinstance(item, str) or not item.strip() or item != item.strip():
			return None
		output[key] = item
	if "record_scope" in value:
		boundary = fefo_record_scope(value.get("record_scope"))
		if boundary is None:
			return None
		output["record_scope"] = boundary
	try:
		dates = {key: frappe.utils.getdate(output[key]) for key in ("from_date", "to_date", "report_date")}
	except Exception:
		return None
	if any(str(dates[key]) != output[key] for key in dates) or dates["from_date"] > min(
		dates["to_date"], dates["report_date"]
	):
		return None
	return output


def _fefo_visibility_refusal() -> InvalidArgumentError:
	"""Return the deliberately non-enumerating FEFO visibility refusal."""
	return InvalidArgumentError("FEFO native visibility could not be verified")


def _fefo_refresh_access(user: str) -> None:
	"""Discard cached access state before a completeness decision.

	The evaluator source query is permission-woven while the native history report
	is not.  A cached role or User Permission answer can therefore create a false
	full-coverage verdict after an administrator narrows access.  Refresh both
	inputs before every proof; any refresh failure is a refusal, never an old
	answer.
	"""
	try:
		account = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
		if (
			not account
			or not frappe.utils.cint(account.get("enabled"))
			or account.get("user_type") != "System User"
			or user in {"", "Guest"}
		):
			raise _fefo_visibility_refusal()
		frappe.cache.hdel("roles", user)
		frappe.cache.hdel("user_permissions", user)
		frappe.clear_document_cache("User", user)
		# The source compiler reads this Single through ``get_cached_doc``.  A
		# stale allowlist can make launch proof and later query execution disagree.
		frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")
		# Frappe caches masks per DocType, parent route, and user.  Rechecking a
		# proof after a Property Setter or Mask permission change must not reuse
		# an old unmasked answer.  Clear both bare and canonical-parent routes;
		# only the latter is meaningful for Serial and Batch Entry.
		for doctype, parent in _FEFO_VISIBILITY_SURFACE:
			for parenttype in {None, parent}:
				frappe.cache.delete_value(f"masked_fields::{doctype}::{parenttype or ''}::{user}")
		frappe.local.role_permissions = {}
		if hasattr(frappe.local, "system_settings"):
			del frappe.local.system_settings
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_company_permission_links_are_safe() -> list[dict]:
	"""Return the reviewed Company-link shape for a Company User Permission.

	The normal source tool uses Frappe query permissions.  A Company User
	Permission can therefore add a predicate on *any* non-ignored Company Link,
	including an administrator's custom field.  The native report does not weave
	that predicate.  Only the explicit fields below have a reviewed equivalent in
	the FEFO plan; everything else is a partial-coverage refusal.
	"""
	try:
		result = []
		for doctype, _parent in _FEFO_VISIBILITY_SURFACE:
			# Company itself is a scope authorization check, not an evaluator
			# source or native-report cohort.  Its hierarchical parent_company
			# link must not turn the narrow Company-only exception into a false
			# rejection.
			if doctype == "Company":
				continue
			meta = frappe.get_meta(doctype, cached=False)
			if not meta:
				raise _fefo_visibility_refusal()
			links = meta.get_link_fields()
			if not isinstance(links, (list, tuple)):
				raise _fefo_visibility_refusal()
			allowed = _FEFO_COMPANY_LINK_ALLOWLIST.get(doctype, frozenset())
			company_links = []
			for field in links:
				if not hasattr(field, "get"):
					raise _fefo_visibility_refusal()
				if field.get("fieldtype") != "Link":
					raise _fefo_visibility_refusal()
				if field.get("options") != "Company" or frappe.utils.cint(
					field.get("ignore_user_permissions")
				):
					continue
				fieldname = str(field.get("fieldname") or "").strip()
				if not fieldname or fieldname not in allowed:
					raise _fefo_visibility_refusal()
				company_links.append(fieldname)
			result.append({"doctype": doctype, "company_links": sorted(company_links)})
		return result
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_company_only_user_permissions(user: str, company: str) -> list[dict]:
	"""Return the sole narrow User Permission state that keeps FEFO complete.

	A Company-only grant for the exact stamped Company does not omit records
	within that Company.  Every other User Permission, including an
	``applicable_for`` variant, can make Frappe's source query a slice while the
	native report remains a full query.  Check raw rows and Frappe's effective
	projection because the latter omits ``apply_to_all_doctypes``.
	"""
	try:
		rows = frappe.get_all(
			"User Permission",
			filters={"user": user},
			fields=[
				"allow",
				"for_value",
				"apply_to_all_doctypes",
				"applicable_for",
				"hide_descendants",
				"is_default",
			],
			order_by="name asc",
			limit_page_length=2,
		)
		if len(rows) > 1:
			raise _fefo_visibility_refusal()
		canonical = [
			{
				"allow": str(row.get("allow") or ""),
				"for_value": str(row.get("for_value") or ""),
				"apply_to_all_doctypes": frappe.utils.cint(row.get("apply_to_all_doctypes")),
				"applicable_for": str(row.get("applicable_for") or "").strip(),
				"hide_descendants": frappe.utils.cint(row.get("hide_descendants")),
				"is_default": frappe.utils.cint(row.get("is_default")),
			}
			for row in rows
		]
		effective = frappe.permissions.get_user_permissions(user)
		if not isinstance(effective, dict):
			raise _fefo_visibility_refusal()
		if not canonical:
			if effective:
				raise _fefo_visibility_refusal()
			return canonical
		row = canonical[0]
		if (
			row["allow"] != "Company"
			or row["for_value"] != company
			or row["apply_to_all_doctypes"] != 1
			or row["applicable_for"]
			or row["hide_descendants"]
		):
			raise _fefo_visibility_refusal()
		if set(effective) != {"Company"}:
			raise _fefo_visibility_refusal()
		entries = effective.get("Company")
		if not isinstance(entries, (list, tuple)) or len(entries) != 1:
			raise _fefo_visibility_refusal()
		entry = entries[0]
		if (
			not isinstance(entry, dict)
			or entry.get("doc") != company
			or str(entry.get("applicable_for") or "").strip()
			or frappe.utils.cint(entry.get("hide_descendants"))
		):
			raise _fefo_visibility_refusal()
		# This must happen only for the narrow Company exception.  With no User
		# Permission, a Company Link cannot add a hidden row predicate.
		_fefo_company_permission_links_are_safe()
		return canonical
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_assert_no_record_slices() -> None:
	"""Refuse every hook or script that can narrow one native source.

	Permission Query hooks, ``has_permission`` hooks, and Permission Query Server
	Scripts have arbitrary record predicates.  They cannot prove that the raw
	Batch-Wise Balance History query sees the same cohort, so their mere presence
	makes this run partial.  Check both doctype and wildcard registrations.
	"""
	try:
		doctypes = {doctype for doctype, _parent in _FEFO_VISIBILITY_SURFACE}
		for hook_name in ("permission_query_conditions", "has_permission"):
			hooks = frappe.get_hooks(hook_name, {})
			if not isinstance(hooks, dict):
				raise _fefo_visibility_refusal()
			if any(hooks.get(key) for key in {"*", *doctypes}):
				raise _fefo_visibility_refusal()
		from frappe.core.doctype.server_script.server_script_utils import get_server_script_map

		script_map = get_server_script_map()
		if not isinstance(script_map, dict):
			raise _fefo_visibility_refusal()
		permission_queries = script_map.get("permission_query", {})
		if permission_queries is None:
			permission_queries = {}
		if not isinstance(permission_queries, dict):
			raise _fefo_visibility_refusal()
		if any(permission_queries.get(key) for key in {"*", *doctypes}):
			raise _fefo_visibility_refusal()
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_role_access(meta, user: str) -> dict:
	"""Return a known non-owner read grant, or refuse it.

	The structured query tool requires ``read`` for every FEFO source.  A
	select-only role can inspect a search surface but cannot run the canonical
	source plan, so it must never receive a complete native-visibility proof.
	"""
	from frappe.model.db_query import requires_owner_constraint
	from frappe.permissions import get_role_permissions

	try:
		permissions = get_role_permissions(meta, user=user)
		if (
			not isinstance(permissions, dict)
			or not permissions.get("read")
			or requires_owner_constraint(permissions)
		):
			raise _fefo_visibility_refusal()
		return {
			"read": bool(permissions.get("read")),
			"select": bool(permissions.get("select")),
		}
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_parent_path(child_meta, parent_meta, child: str, parent: str, user: str) -> str:
	"""Prove the one parent relation the native FEFO plan uses.

	``Serial and Batch Entry`` is a real child table and can only use
	``Serial and Batch Bundle.entries``.  ``Stock Closing Balance`` is a normal
	DocType but FEFO uses it only through its fixed ``stock_closing_entry`` link.
	The explicit relation check stops a custom or unrelated parent from serving as
	a permission substitute.
	"""
	try:
		if frappe.utils.cint(child_meta.get("istable")):
			fields = [
				field
				for field in (parent_meta.get("fields") or [])
				if field.get("fieldtype") in {"Table", "Table MultiSelect"} and field.get("options") == child
			]
			if child != "Serial and Batch Entry" or not any(
				field.get("fieldname") == "entries" for field in fields
			):
				raise _fefo_visibility_refusal()
			levels = set(parent_meta.get_permlevel_access("read", user=user) or [])
			if not any(frappe.utils.cint(field.get("permlevel")) in levels for field in fields):
				raise _fefo_visibility_refusal()
			# A child DocType has no independent DocPerm.  Ask through the verified
			# parent path, never as a bare child (which Frappe correctly answers false).
			if not frappe.has_permission(child, "read", user=user, parent_doctype=parent, print_logs=False):
				raise _fefo_visibility_refusal()
			return "table:entries"
		link = next(
			(
				field
				for field in (child_meta.get("fields") or [])
				if field.get("fieldname") == "stock_closing_entry"
				and field.get("fieldtype") == "Link"
				and field.get("options") == parent
			),
			None,
		)
		if child != "Stock Closing Balance" or parent != "Stock Closing Entry" or link is None:
			raise _fefo_visibility_refusal()
		return "link:stock_closing_entry"
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_source_permissions(user: str) -> list[dict]:
	"""Return the effective non-slicing access matrix for native FEFO sources."""
	try:
		metas = {}

		def meta(doctype: str):
			if doctype not in metas:
				metas[doctype] = frappe.get_meta(doctype, cached=False)
			if not metas[doctype]:
				raise _fefo_visibility_refusal()
			return metas[doctype]

		matrix = []
		for doctype, parent in _FEFO_VISIBILITY_SURFACE:
			source_meta = meta(doctype)
			if not parent:
				access = _fefo_role_access(source_meta, user)
				matrix.append({"doctype": doctype, "parent": "", **access})
				continue
			parent_meta = meta(parent)
			path = _fefo_parent_path(source_meta, parent_meta, doctype, parent, user)
			parent_access = _fefo_role_access(parent_meta, user)
			if frappe.utils.cint(source_meta.get("istable")):
				# The verified table route is the effective child permission surface.
				matrix.append({"doctype": doctype, "parent": parent, "path": path, **parent_access})
			else:
				# Stock Closing Balance has its own DocPerm, but its report rows are
				# meaningful only through the checked closing-entry relation.
				source_access = _fefo_role_access(source_meta, user)
				matrix.append(
					{
						"doctype": doctype,
						"parent": parent,
						"path": path,
						"read": source_access["read"],
						"select": source_access["select"],
						"parent_read": parent_access["read"],
						"parent_select": parent_access["select"],
					}
				)
		return matrix
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_inventory_dimension_targets() -> list[str]:
	"""Return every dynamic SLE field that StockClosing can raw-select.

	StockClosing appends every active Inventory Dimension target to its raw SLE
	query.  The configuration table is itself read without normal record or field
	permission weaving, so malformed rows and an unavailable lookup fail closed.
	"""
	try:
		rows = frappe.get_all(
			"Inventory Dimension",
			fields=[
				"name",
				"target_fieldname",
				"source_fieldname",
				"reference_document",
				"validate_negative_stock",
			],
			order_by="creation asc",
			limit_page_length=0,
		)
		if not isinstance(rows, (list, tuple)):
			raise _fefo_visibility_refusal()
		targets = set()
		for row in rows:
			if not isinstance(row, dict):
				raise _fefo_visibility_refusal()
			target = row.get("target_fieldname")
			if not isinstance(target, str) or not target.strip() or target != target.strip():
				raise _fefo_visibility_refusal()
			targets.add(target)
		return sorted(targets)
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_native_report_fields() -> list[dict]:
	"""Prove that every raw FEFO report field is public and unmasked.

	``Batch-Wise Balance History`` uses query-builder and ``get_all`` calls that
	do not apply the normal field permission or mask layer.  The safe equivalent
	guard is intentionally stricter: every field it reads must be a known
	permlevel-0 field without a mask.  A privileged permlevel-1 role is still a
	non-public custom policy and must make the report partial until a reviewed
	native path can enforce it.
	"""
	try:
		from frappe.model import child_table_fields, default_fields

		matrix = []
		dimension_targets = (
			_fefo_inventory_dimension_targets() if "Stock Ledger Entry" in _FEFO_NATIVE_REPORT_FIELDS else []
		)
		for doctype, configured_fields in _FEFO_NATIVE_REPORT_FIELDS.items():
			meta = frappe.get_meta(doctype, cached=False)
			if not meta:
				raise _fefo_visibility_refusal()
			valid_columns = meta.get_valid_columns()
			if not isinstance(valid_columns, (list, tuple)) or not all(
				isinstance(field, str) and field for field in valid_columns
			):
				raise _fefo_visibility_refusal()
			valid = set(valid_columns)
			fields = valid if configured_fields is None else set(configured_fields)
			if doctype == "Stock Ledger Entry":
				fields.update(dimension_targets)
			if not fields or not fields.issubset(valid):
				raise _fefo_visibility_refusal()
			for fieldname in fields:
				field = meta.get_field(fieldname)
				if field is None:
					# Frappe default / child-table columns have no DocField and are
					# always permlevel 0.  Any other missing metadata is unknown.
					if fieldname not in {*default_fields, *child_table_fields}:
						raise _fefo_visibility_refusal()
					continue
				if not hasattr(field, "get"):
					raise _fefo_visibility_refusal()
				if frappe.utils.cint(field.get("permlevel")) != 0 or field.get("mask"):
					raise _fefo_visibility_refusal()
			matrix.append({"doctype": doctype, "fields": sorted(fields)})
		return matrix
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_canonical_query_specs(scope: dict) -> dict[str, dict]:
	"""Return the exact signed-bundle FEFO query plan for this stamped scope.

	This factory is deliberately private.  It does not launch a query or return
	data.  It gives the server one immutable plan that can be compiled under the
	run-as identity before dispatch and again before source/read/writeback work.
	The query compiler then supplies the same DocType, field, child-parent, and
	record-gate checks that the delegate's ``jarvis__query`` calls receive.
	"""
	# Callers have already checked the run-owned marker.  Keep this helper able
	# to consume the canonical output projection that ``fefo_stamped_scope``
	# returns, rather than requiring the marker a second time.
	scope = fefo_output_scope(scope)
	if scope is None:
		raise _fefo_visibility_refusal()
	try:
		company = scope["company"]
		cutoff = _fefo_cutoff(scope)
		boundary = scope.get("record_scope")
		warehouse = boundary["name"] if boundary else None

		def predicate(field: str, op: str, value) -> dict:
			return {"field": field, "op": op, "value": value}

		def ledger_where(alias: str) -> list[dict]:
			where = [
				predicate(f"{alias}.company", "=", company),
				predicate(f"{alias}.posting_date", "<=", cutoff),
				predicate(f"{alias}.docstatus", "<", 2),
				predicate(f"{alias}.is_cancelled", "=", 0),
			]
			if warehouse:
				where.append(predicate(f"{alias}.warehouse", "=", warehouse))
			return where

		def ledger_exists(field: str, reference: str, voucher_type: str | None = None) -> dict:
			where = [*ledger_where("s"), predicate(f"s.{field}", "=", {"$field": reference})]
			if voucher_type:
				where.append(predicate("s.voucher_type", "=", voucher_type))
			return {"op": "exists", "value": {"from": "Stock Ledger Entry", "alias": "s", "where": where}}

		out = {}
		for key, doctype, fields in _FEFO_QUERY_SOURCE_FIELDS:
			# Inventory Dimension has no reviewed Warehouse relation.  The bundle
			# omits it for a leaf review rather than widening this query.
			if warehouse and key == "dimensions":
				continue
			spec = {
				"from": doctype,
				"alias": "r",
				"select": [f"r.{field}" for field in fields],
				"where": [],
				"order_by": [{"field": "r.name", "dir": "asc"}],
				"limit": 200,
			}
			if key == "company":
				spec["where"] = [predicate("r.name", "=", company)]
			elif key == "ledger":
				spec["where"] = ledger_where("r")
			elif key == "warehouses":
				spec["where"] = [predicate("r.company", "=", company)]
				if warehouse:
					spec["where"].append(predicate("r.name", "=", warehouse))
			elif key == "items":
				spec["where"] = [ledger_exists("item_code", "r.name")]
			elif key == "batches":
				spec["where"] = [ledger_exists("item_code", "r.item")]
			elif key == "bundles":
				spec["where"] = [ledger_exists("serial_and_batch_bundle", "r.name")]
			elif key == "bundle_entries":
				spec["joins"] = [
					{
						"doctype": "Serial and Batch Bundle",
						"alias": "b",
						"type": "inner",
						"on": {"r.parent": "b.name"},
					}
				]
				spec["select"].append({"expr": {"field": "b.modified"}, "as": "parent_modified"})
				spec["where"] = [
					predicate("r.parenttype", "=", "Serial and Batch Bundle"),
					predicate("r.parentfield", "=", "entries"),
					ledger_exists("serial_and_batch_bundle", "b.name"),
				]
			elif key in {"stock_entries", "deliveries", "invoices"}:
				spec["where"] = [ledger_exists("voucher_no", "r.name", doctype)]
			elif key == "reposts":
				spec["where"] = [
					predicate("r.company", "=", company),
					predicate("r.docstatus", "=", 1),
					predicate("r.status", "not in", ["Completed", "Skipped"]),
				]
				if warehouse:
					spec["where"].append(predicate("r.warehouse", "=", warehouse))
			elif key == "closings":
				spec["where"] = [
					predicate("r.company", "=", company),
					predicate("r.docstatus", "=", 1),
					predicate("r.to_date", "<=", cutoff),
				]
				if warehouse:
					spec["where"].append(
						{
							"op": "exists",
							"value": {
								"from": "Stock Closing Balance",
								"alias": "b",
								"where": [
									predicate("b.stock_closing_entry", "=", {"$field": "r.name"}),
									predicate("b.company", "=", company),
									predicate("b.warehouse", "=", warehouse),
								],
							},
						}
					)
			out[key] = spec
		return out
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_query_field_references(spec: dict) -> list[tuple[str, str, str | None]]:
	"""Return every physical canonical query field and its mask parent route.

	``query._compile`` owns field ACL validation.  This walker only finds fields
	for the companion mask check.  It follows the same actual spec object,
	including joins and correlated EXISTS references, so an omitted field cannot
	become an unproved source input.
	"""
	from jarvis.tools._doctype_name import canonical_doctype

	try:
		refs: set[tuple[str, str, str | None]] = set()

		def add(ref, aliases: dict[str, str]) -> None:
			if not isinstance(ref, str) or ref.count(".") != 1:
				return
			alias, field = ref.split(".", 1)
			doctype = aliases.get(alias)
			if not doctype or not field:
				raise _fefo_visibility_refusal()
			parent = "Serial and Batch Bundle" if doctype == "Serial and Batch Entry" else None
			refs.add((doctype, field, parent))

		def expression(value, aliases: dict[str, str]) -> None:
			if isinstance(value, str):
				add(value, aliases)
			elif isinstance(value, dict):
				if set(value) == {"field"}:
					add(value["field"], aliases)
				else:
					for item in value.values():
						expression(item, aliases)
			elif isinstance(value, (list, tuple)):
				for item in value:
					expression(item, aliases)

		def walk(node: dict, outer_aliases: dict[str, str]) -> None:
			if not isinstance(node, dict):
				raise _fefo_visibility_refusal()
			aliases = dict(outer_aliases)
			from_doctype = canonical_doctype(node.get("from"))
			alias = node.get("alias") or node.get("from")
			if not isinstance(from_doctype, str) or not isinstance(alias, str) or alias in aliases:
				raise _fefo_visibility_refusal()
			aliases[alias] = from_doctype
			for join in node.get("joins") or []:
				if not isinstance(join, dict):
					raise _fefo_visibility_refusal()
				join_doctype = canonical_doctype(join.get("doctype"))
				join_alias = join.get("alias")
				if (
					not isinstance(join_doctype, str)
					or not isinstance(join_alias, str)
					or join_alias in aliases
				):
					raise _fefo_visibility_refusal()
				aliases[join_alias] = join_doctype
				for lhs, rhs in (join.get("on") or {}).items():
					add(lhs, aliases)
					add(rhs, aliases)
			for selected in node.get("select") or []:
				if isinstance(selected, str):
					add(selected, aliases)
				elif isinstance(selected, dict):
					expression(selected.get("expr") if "expr" in selected else selected.get("field"), aliases)
				else:
					raise _fefo_visibility_refusal()
			for clause in ("where", "having"):
				for predicate in node.get(clause) or []:
					if not isinstance(predicate, dict):
						raise _fefo_visibility_refusal()
					if predicate.get("op") in {"exists", "not exists"}:
						walk(predicate.get("value"), aliases)
						continue
					expression(predicate.get("field"), aliases)
					value = predicate.get("value")
					if isinstance(value, dict) and set(value) == {"$field"}:
						add(value["$field"], aliases)
			for item in [*(node.get("group_by") or []), *(node.get("order_by") or [])]:
				field = item.get("field") if isinstance(item, dict) else item
				add(field, aliases)

		walk(spec, {})
		return sorted(refs)
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_assert_canonical_query_fields_unmasked(spec: dict) -> list[dict]:
	"""Refuse canonical source fields that the query tool would mask.

	The query compiler checks field ACLs.  Its result layer masks plain selected
	fields after a source query, so an expiry evaluator could otherwise compare
	changed values and still report full coverage.  Use Frappe's current mask
	calculation, with the one verified child-parent route, before any source row
	can be read.
	"""
	try:
		from jarvis import compat
		from jarvis.tools._export import masking

		if masking.mask_field_value is None or frappe.session.user == "Administrator":
			return []
		masked_by_surface = {}
		references = _fefo_query_field_references(spec)
		for doctype, fieldname, parent in references:
			key = (doctype, parent)
			if key not in masked_by_surface:
				meta = frappe.get_meta(doctype, cached=False)
				masked = compat.masked_fields_of(meta, parent)
				if not isinstance(masked, (list, tuple)):
					raise _fefo_visibility_refusal()
				masked_by_surface[key] = set()
				for field in masked:
					if not hasattr(field, "get"):
						raise _fefo_visibility_refusal()
					masked_name = str(field.get("fieldname") or "").strip()
					if not masked_name:
						raise _fefo_visibility_refusal()
					masked_by_surface[key].add(masked_name)
			if fieldname in masked_by_surface[key]:
				raise _fefo_visibility_refusal()
		return [
			{"doctype": doctype, "field": fieldname, "parent": parent or ""}
			for doctype, fieldname, parent in references
		]
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_canonical_query_access(scope: dict) -> list[dict]:
	"""Compile every FEFO source request under the exact query-tool gates."""
	try:
		from jarvis.tools import query as query_tool

		matrix = []
		for source, spec in _fefo_canonical_query_specs(scope).items():
			# ``_compile`` builds only a query object.  It never calls ``run``.
			# It is the source of truth for DocType read, field ACL, child-parent,
			# query allowlist, and record-condition validation.
			query_tool._compile(spec)
			fields = _fefo_assert_canonical_query_fields_unmasked(spec)
			matrix.append({"source": source, "fields": fields})
		return matrix
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_query_allowlist(scope: dict) -> dict:
	"""Prove that fresh query settings admit every canonical FEFO DocType."""
	try:
		from jarvis.tools import query as query_tool

		required = set()
		for spec in _fefo_canonical_query_specs(scope).values():
			required.update(query_tool._collect_doctypes(spec))
		if not required or not all(isinstance(doctype, str) and doctype for doctype in required):
			raise _fefo_visibility_refusal()
		raw = frappe.db.get_single_value("Jarvis Settings", "run_query_doctype_allowlist")
		if raw is None:
			raw = ""
		if not isinstance(raw, str):
			raise _fefo_visibility_refusal()
		normalized = raw.strip()
		if not normalized:
			return {"mode": "unset", "required": sorted(required), "sha256": hashlib.sha256(b"").hexdigest()}
		allowed = {part.strip() for part in re.split(r"[,\n]", normalized) if part.strip()}
		if not required.issubset(allowed):
			raise _fefo_visibility_refusal()
		return {
			"mode": "configured",
			"required": sorted(required),
			"sha256": hashlib.sha256("\n".join(sorted(allowed)).encode()).hexdigest(),
		}
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_native_report_access() -> dict:
	"""Prove the exact native report can run for the current run-as user."""
	try:
		from frappe.desk.query_report import get_report_doc

		report = get_report_doc("Batch-Wise Balance History")
		if (
			not report
			or str(report.get("name") or "") != "Batch-Wise Balance History"
			or str(report.get("ref_doctype") or "") != "Stock Ledger Entry"
			or str(report.get("report_type") or "") != "Script Report"
			or str(report.get("is_standard") or "") != "Yes"
			or str(report.get("module") or "") != "Stock"
			or frappe.utils.cint(report.get("disabled"))
			or not frappe.has_permission("Stock Ledger Entry", "report", print_logs=False)
		):
			raise _fefo_visibility_refusal()
		return {
			"name": "Batch-Wise Balance History",
			"ref_doctype": "Stock Ledger Entry",
			"report_type": str(report.get("report_type") or ""),
			"is_standard": "Yes",
			"module": "Stock",
			"modified": str(report.get("modified") or ""),
		}
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def _fefo_visibility_proof_value(value) -> dict | None:
	"""Parse the opaque server-only proof without accepting extra fields."""
	try:
		if isinstance(value, str):
			value = json.loads(value)
	except Exception:
		return None
	if (
		not isinstance(value, dict)
		or set(value) != _FEFO_VISIBILITY_PROOF_KEYS
		or value.get("schema") != _FEFO_VISIBILITY_PROOF_SCHEMA
		or value.get("complete") is not True
		or not isinstance(value.get("policy_sha256"), str)
		or not re.fullmatch(r"[0-9a-f]{64}", value["policy_sha256"])
	):
		return None
	return {
		"schema": _FEFO_VISIBILITY_PROOF_SCHEMA,
		"complete": True,
		"policy_sha256": value["policy_sha256"],
	}


def fefo_visibility_proof(value) -> dict:
	"""Build an opaque, run-as visibility proof for the FEFO native source set.

	The digest intentionally contains no Warehouse cohort, role, User Permission,
	or hook detail.  It stays in a hidden Run field and is recomputed by server
	guards.  The bundle receives only the normal immutable scope and contract
	marker, so it cannot turn a policy fact into evaluator-controlled input.
	"""
	scope = fefo_stamped_scope(value)
	if scope is None:
		raise _fefo_visibility_refusal()
	user = str(frappe.session.user or "").strip()
	_fefo_refresh_access(user)
	try:
		company_access = _fefo_role_access(frappe.get_meta("Company", cached=False), user)
		if not frappe.has_permission("Company", "read", doc=scope["company"], user=user, print_logs=False):
			raise _fefo_visibility_refusal()
		if scope.get("record_scope"):
			if _fefo_warehouse_boundary(scope["record_scope"], scope["company"]) != scope["record_scope"]:
				raise _fefo_visibility_refusal()
		user_permissions = _fefo_company_only_user_permissions(user, scope["company"])
		_fefo_assert_no_record_slices()
		sources = _fefo_source_permissions(user)
		query_allowlist = _fefo_query_allowlist(scope)
		query_fields = _fefo_canonical_query_access(scope)
		native_report = _fefo_native_report_access()
		report_fields = _fefo_native_report_fields()
		policy = {
			"contract": _FEFO_SCOPE_CONTRACT,
			"scope": scope,
			"user": user,
			"roles": sorted(frappe.get_roles(user)),
			"company_access": company_access,
			"user_permissions": user_permissions,
			"strict_user_permissions": frappe.utils.cint(
				frappe.get_system_settings("apply_strict_user_permissions")
			),
			"sources": sources,
			"query_allowlist": query_allowlist,
			"query_fields": query_fields,
			"native_report": native_report,
			"report_fields": report_fields,
		}
		digest = hashlib.sha256(
			json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
		).hexdigest()
		return {
			"schema": _FEFO_VISIBILITY_PROOF_SCHEMA,
			"complete": True,
			"policy_sha256": digest,
		}
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise _fefo_visibility_refusal() from exc


def fefo_stamped_scope(value) -> dict | None:
	"""Return a current server-stamped FEFO projection, never a legacy scope."""
	if not isinstance(value, dict) or value.get(_FEFO_SCOPE_CONTRACT_KEY) != _FEFO_SCOPE_CONTRACT:
		return None
	return fefo_output_scope(value)


def fefo_stamped_scope_as_user(value, run_as_user: str, visibility_proof=None) -> dict | None:
	"""Revalidate an immutable FEFO boundary as the bound run-as user.

	This helper takes a Run scope, never a mutable Installation configuration.
	It is shared by source-tool and writeback guards so an Administrator worker
	cannot make an unreadable leaf look safe for the actual delegate user.  The
	third argument is the hidden Run proof, not evaluator input; an older v0.4.1
	Run without one fails closed.
	"""
	scope = fefo_stamped_scope(value)
	stored_proof = _fefo_visibility_proof_value(visibility_proof)
	if scope is None or stored_proof is None:
		return None
	user = str(run_as_user or "").strip()
	if not user or user == "Guest":
		return None
	from jarvis._session import impersonate

	try:
		with impersonate(user):
			if (
				scope.get("record_scope")
				and _fefo_warehouse_boundary(scope["record_scope"], scope["company"]) != scope["record_scope"]
			):
				return None
			if fefo_visibility_proof(value) != stored_proof:
				return None
	except Exception:
		return None
	return scope


def _fefo_run_as_user(run_doc) -> str | None:
	"""Read the immutable live run session identity without an owner fallback."""
	key = str(run_doc.get("session_key") or "").strip()
	if not key:
		return None
	try:
		user = frappe.db.get_value("Jarvis Chat Session", {"session_key": key}, "user")
	except Exception:
		return None
	user = str(user or "").strip()
	return user if user and user != "Guest" else None


def fefo_run_visibility_is_current(run_doc) -> bool:
	"""Return true only when this Run's opaque FEFO proof still matches now."""
	if not run_doc or run_doc.get("agent") != FEFO_AGENT:
		return False
	try:
		scope = json.loads(run_doc.get("scope_json") or "{}")
		user = _fefo_run_as_user(run_doc)
		return bool(
			user
			and fefo_stamped_scope_as_user(
				scope,
				user,
				run_doc.get("fefo_visibility_proof_json"),
			)
		)
	except Exception:
		return False


def assert_fefo_run_artifact_scope(run_doc) -> None:
	"""Block FEFO dashboard/artifact persistence after any proof drift."""
	if run_doc and run_doc.get("agent") == FEFO_AGENT and not fefo_run_visibility_is_current(run_doc):
		raise InvalidArgumentError("FEFO visibility proof is stale or unavailable")


def assert_fefo_message_artifact_scope(message_name: str) -> None:
	"""Gate a canvas save when its assistant message belongs to a FEFO Run.

	A Run has a dedicated conversation.  Treat an ambiguous FEFO association as a
	refusal instead of guessing which proof owns the artifact.  Other agents keep
	their existing canvas path unchanged.
	"""
	try:
		message = frappe.db.get_value(
			"Jarvis Chat Message", message_name, ["conversation", "role"], as_dict=True
		)
		if not message:
			raise InvalidArgumentError("assistant message is unavailable for canvas persistence")
		conversation = str(message.get("conversation") or "").strip()
		if not conversation:
			return
		runs = frappe.get_all(
			"Jarvis Agent Run",
			filters={"conversation": conversation, "agent": FEFO_AGENT},
			fields=["name", "agent", "session_key", "scope_json", "fefo_visibility_proof_json"],
			limit_page_length=2,
		)
		if not runs:
			return
		if len(runs) != 1:
			raise InvalidArgumentError("FEFO canvas provenance is ambiguous")
		assert_fefo_run_artifact_scope(runs[0])
	except InvalidArgumentError:
		raise
	except Exception as exc:
		raise InvalidArgumentError("FEFO canvas provenance could not be verified") from exc


def active_fefo_scope() -> dict | None:
	"""Return the active FEFO Run scope after server-side revalidation."""
	from jarvis.tools._agent_run_ctx import get_session_key

	session_key = get_session_key()
	if not session_key:
		return None
	run = frappe.db.get_value(
		"Jarvis Agent Run",
		{"session_key": session_key, "status": "running"},
		["agent", "scope_json", "fefo_visibility_proof_json"],
		as_dict=True,
	)
	if not run or run.get("agent") != FEFO_AGENT:
		return None
	run_as_user = frappe.db.get_value("Jarvis Chat Session", {"session_key": session_key}, "user")
	try:
		scope = frappe.parse_json(run.get("scope_json") or "{}")
	except Exception as exc:
		raise InvalidArgumentError("FEFO run scope is missing or malformed") from exc
	trusted = fefo_stamped_scope_as_user(scope, run_as_user, run.get("fefo_visibility_proof_json"))
	if trusted is None:
		raise InvalidArgumentError("FEFO visibility proof is stale or unavailable")
	return trusted


_FEFO_SOURCE_DOCTYPES = frozenset(
	{
		"Company",
		"Warehouse",
		"Item",
		"Batch",
		"Stock Ledger Entry",
		"Serial and Batch Bundle",
		"Serial and Batch Entry",
		"Stock Entry",
		"Delivery Note",
		"Sales Invoice",
		"Repost Item Valuation",
		"Stock Closing Entry",
		"Inventory Dimension",
	}
)
_FEFO_LEAF_UNSCOPABLE_DOCTYPES = frozenset({"Inventory Dimension"})


def _fefo_equal(where, field: str, value) -> bool:
	return _fefo_predicate(where, field, "=", value)


def _fefo_predicate(where, field: str, op: str, value) -> bool:
	return any(
		isinstance(predicate, dict)
		and predicate.get("op") == op
		and predicate.get("field") == field
		and predicate.get("value") == value
		for predicate in (where or [])
	)


def _fefo_cutoff(scope: dict) -> str:
	return min(scope["to_date"], scope["report_date"])


def _fefo_main_equal(spec: dict, suffix: str, value) -> bool:
	alias = spec.get("alias")
	return (
		isinstance(alias, str) and bool(alias) and _fefo_equal(spec.get("where"), f"{alias}.{suffix}", value)
	)


def _fefo_sle_exists(
	spec: dict, field: str, reference: str, scope: dict, *, voucher_type: str | None = None
) -> bool:
	for predicate in spec.get("where") or []:
		if not isinstance(predicate, dict) or predicate.get("op") != "exists":
			continue
		child = predicate.get("value")
		if (
			not isinstance(child, dict)
			or child.get("from") != "Stock Ledger Entry"
			or child.get("alias") != "s"
		):
			continue
		where = child.get("where")
		if not _fefo_equal(where, "s.company", scope["company"]):
			continue
		if not _fefo_predicate(where, "s.posting_date", "<=", _fefo_cutoff(scope)):
			continue
		if not _fefo_predicate(where, "s.docstatus", "<", 2) or not _fefo_equal(where, "s.is_cancelled", 0):
			continue
		if not _fefo_equal(where, f"s.{field}", {"$field": reference}):
			continue
		if voucher_type and not _fefo_equal(where, "s.voucher_type", voucher_type):
			continue
		boundary = scope.get("record_scope")
		if boundary and not _fefo_equal(where, "s.warehouse", boundary["name"]):
			continue
		return True
	return False


def _fefo_closing_balance_exists(spec: dict, scope: dict) -> bool:
	for predicate in spec.get("where") or []:
		if not isinstance(predicate, dict) or predicate.get("op") != "exists":
			continue
		child = predicate.get("value")
		if (
			not isinstance(child, dict)
			or child.get("from") != "Stock Closing Balance"
			or child.get("alias") != "b"
		):
			continue
		where = child.get("where")
		if (
			_fefo_equal(where, "b.stock_closing_entry", {"$field": "r.name"})
			and _fefo_equal(where, "b.company", scope["company"])
			and _fefo_equal(where, "b.warehouse", scope["record_scope"]["name"])
		):
			return True
	return False


def _fefo_only_expected_joins(spec: dict, doctype: str) -> bool:
	joins = spec.get("joins") or []
	if doctype != "Serial and Batch Entry":
		return not joins
	return [
		{
			"doctype": "Serial and Batch Bundle",
			"alias": "b",
			"type": "inner",
			"on": {"r.parent": "b.name"},
		}
	] == joins


def _fefo_native_source_is_fenced(spec: dict, doctype: str, scope: dict) -> bool:
	if not _fefo_only_expected_joins(spec, doctype):
		return False
	boundary = scope.get("record_scope")
	if doctype == "Company":
		return _fefo_main_equal(spec, "name", scope["company"])
	if doctype == "Inventory Dimension":
		return boundary is None
	if doctype == "Warehouse":
		return _fefo_main_equal(spec, "company", scope["company"]) and (
			boundary is None or _fefo_main_equal(spec, "name", boundary["name"])
		)
	if doctype == "Stock Ledger Entry":
		return (
			_fefo_main_equal(spec, "company", scope["company"])
			and _fefo_predicate(
				spec.get("where"), f"{spec.get('alias')}.posting_date", "<=", _fefo_cutoff(scope)
			)
			and _fefo_predicate(spec.get("where"), f"{spec.get('alias')}.docstatus", "<", 2)
			and _fefo_equal(spec.get("where"), f"{spec.get('alias')}.is_cancelled", 0)
			and (boundary is None or _fefo_main_equal(spec, "warehouse", boundary["name"]))
		)
	if doctype == "Repost Item Valuation":
		return _fefo_main_equal(spec, "company", scope["company"]) and (
			boundary is None or _fefo_main_equal(spec, "warehouse", boundary["name"])
		)
	if doctype == "Stock Closing Entry":
		return (
			_fefo_main_equal(spec, "company", scope["company"])
			and _fefo_equal(spec.get("where"), f"{spec.get('alias')}.docstatus", 1)
			and _fefo_predicate(spec.get("where"), f"{spec.get('alias')}.to_date", "<=", _fefo_cutoff(scope))
			and (boundary is None or _fefo_closing_balance_exists(spec, scope))
		)
	if doctype == "Item":
		return _fefo_sle_exists(spec, "item_code", "r.name", scope)
	if doctype == "Batch":
		return _fefo_sle_exists(spec, "item_code", "r.item", scope)
	if doctype == "Serial and Batch Bundle":
		return _fefo_sle_exists(spec, "serial_and_batch_bundle", "r.name", scope)
	if doctype == "Serial and Batch Entry":
		return _fefo_sle_exists(spec, "serial_and_batch_bundle", "b.name", scope)
	if doctype in {"Stock Entry", "Delivery Note", "Sales Invoice"}:
		return _fefo_sle_exists(spec, "voucher_no", "r.name", scope, voucher_type=doctype)
	return False


def assert_active_fefo_query_scope(spec) -> None:
	"""Fence native FEFO query requests to the immutable launch scope."""
	scope = active_fefo_scope()
	if scope is None:
		return
	if not isinstance(spec, dict) or not isinstance(spec.get("from"), str):
		raise InvalidArgumentError("FEFO native source request is malformed")
	from jarvis.tools._doctype_name import canonical_doctype

	doctype = canonical_doctype(spec["from"])
	if doctype not in _FEFO_SOURCE_DOCTYPES:
		raise InvalidArgumentError("FEFO native source request is outside the approved source set")
	boundary = scope.get("record_scope")
	if boundary and doctype in _FEFO_LEAF_UNSCOPABLE_DOCTYPES:
		raise InvalidArgumentError("FEFO source is unavailable for a Warehouse-scoped review")
	if not _fefo_native_source_is_fenced(spec, doctype, scope):
		if boundary:
			raise InvalidArgumentError("FEFO native source request is outside the stamped leaf")
		raise InvalidArgumentError("FEFO native source request is outside the stamped scope")


def assert_active_fefo_report_scope(report_name: str, filters: dict) -> None:
	"""Fence the FEFO native report request to the immutable launch scope."""
	scope = active_fefo_scope()
	if scope is None:
		return
	if report_name != "Batch-Wise Balance History" or not isinstance(filters, dict):
		raise InvalidArgumentError("FEFO report request is outside the approved source set")
	expected = {
		"company": scope["company"],
		"from_date": scope["from_date"],
		"to_date": min(scope["to_date"], scope["report_date"]),
	}
	if scope.get("record_scope"):
		expected["warehouse"] = scope["record_scope"]["name"]
	if filters != expected:
		raise InvalidArgumentError("FEFO native report request is outside the stamped scope")


def _configuration(config) -> dict:
	"""Return a configuration object and preserve the legacy malformed fallback."""
	if isinstance(config, dict):
		return config
	if config:
		try:
			parsed = frappe.parse_json(config)
			if isinstance(parsed, dict):
				return parsed
		except Exception:
			pass
	return {}


def record_scope_requested(config) -> bool:
	"""True when a tenant supplied the record-scope namespace explicitly."""
	return "record_scope" in _configuration(config)


def assert_record_scope_supported(supports_record_scope: bool, config) -> None:
	"""Refuse an unsupported boundary before configuration or launch has effects."""
	if record_scope_requested(config) and supports_record_scope is not True:
		frappe.throw(
			_("This agent does not support record-scoped runs. Remove record_scope from its configuration.")
		)


def resolve_scope(installation, *, allow_record_scope: bool = False) -> dict:
	"""Resolve the explicit run scope for an installation."""
	if isinstance(installation, str):
		installation = frappe.get_doc(INSTALLATION, installation)
	# Statutory Payroll is a batch-bound monthly audit, not a generic accounting
	# period scan.  Its selected Payroll Entry is validated as the run-as user and
	# is the sole source for company, period, preceding month and report cutoff.
	# Do this before parsing generic config so a stored company/fiscal-year/range
	# can never widen or replace the batch boundary.
	from jarvis.chat.statutory_payroll_scope import is_statutory_payroll
	from jarvis.chat.statutory_payroll_scope import resolve_scope as statutory_scope

	if is_statutory_payroll(installation.get("agent")):
		return statutory_scope(installation.get("config") or "{}")

	cfg = _configuration(installation.get("config"))
	agent_slug = str(installation.get("agent") or installation.get("agent_slug") or "").strip()
	if record_scope_requested(cfg) and not allow_record_scope:
		raise InvalidArgumentError("record_scope is not supported by this agent")

	def _s(key):
		val = cfg.get(key)
		return str(val).strip() if val is not None and str(val).strip() else None

	scope = _resolve(_s("company"), _s("fiscal_year"), _s("from_date"), _s("to_date"))
	if agent_slug == FEFO_AGENT:
		boundary = _fefo_warehouse_scope(cfg, scope["company"])
		if boundary is not None:
			scope["record_scope"] = boundary
		scope[_FEFO_SCOPE_CONTRACT_KEY] = _FEFO_SCOPE_CONTRACT
		# This is launch preflight only.  The opaque proof itself is recomputed and
		# stored in a hidden Run field after the Run exists; it never enters the
		# prompt, bundle input, or model-controlled scope object.
		fefo_visibility_proof(scope)
		return scope
	if not record_scope_requested(cfg):
		return scope

	record = cfg["record_scope"]
	if not record:
		return scope
	if not isinstance(record, dict) or not all(record.get(k) for k in ("doctype", "name")):
		raise InvalidArgumentError("record_scope requires a doctype and name")
	doc = frappe.get_doc(record["doctype"], record["name"])
	doc.check_permission("read")
	if doc.get("company") and doc.company != scope["company"]:
		raise InvalidArgumentError("record_scope must belong to the selected company")
	scope["record_scope"] = {key: str(record[key]) for key in ("doctype", "name")}
	return scope


def _resolve(company, fiscal_year, from_date, to_date) -> dict:
	if not company:
		company = frappe.defaults.get_user_default("Company") or frappe.db.get_default("company")
	if not company:
		companies = frappe.get_all("Company", pluck="name", limit=2)
		if len(companies) == 1:
			company = companies[0]
	if not company:
		raise InvalidArgumentError("company is required (could not infer a single default Company)")
	if not frappe.db.exists("Company", company):
		raise InvalidArgumentError(f"unknown Company: {company}")

	if fiscal_year:
		fy = frappe.db.get_value(
			"Fiscal Year", fiscal_year, ["year_start_date", "year_end_date"], as_dict=True
		)
		if not fy:
			raise InvalidArgumentError(f"unknown Fiscal Year: {fiscal_year}")
		from_date, to_date = str(fy.year_start_date), str(fy.year_end_date)

	if not (from_date and to_date):
		from erpnext.accounts.utils import get_fiscal_year as _gfy

		try:
			fy = _gfy(frappe.utils.today(), company=company, as_dict=True)
			from_date, to_date = str(fy.year_start_date), str(fy.year_end_date)
			fiscal_year = fy.name
		except Exception:
			raise InvalidArgumentError("from_date and to_date (or fiscal_year) are required")

	prior = _prior_fiscal_year(from_date)
	return {
		"company": company,
		"fiscal_year": fiscal_year,
		"from_date": from_date,
		"to_date": to_date,
		"prior_fy_start": prior["py_start"] if prior else None,
		"prior_fy_end": prior["py_end"] if prior else None,
		"report_date": frappe.utils.today(),
	}


def _prior_fiscal_year(from_date) -> dict | None:
	"""Return the enabled fiscal year immediately before ``from_date``."""
	rows = frappe.db.sql(
		"""select name, year_start_date, year_end_date from `tabFiscal Year`
           where year_end_date < %(from_date)s and disabled = 0
           order by year_end_date desc limit 1""",
		{"from_date": from_date},
		as_dict=True,
	)
	if not rows:
		return None
	fy = rows[0]
	return {"name": fy.name, "py_start": str(fy.year_start_date), "py_end": str(fy.year_end_date)}
