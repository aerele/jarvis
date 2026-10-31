"""Owner/report/saved-scope gates and bounded access proofs for stored reports."""

import hashlib
import json
import math
from contextlib import contextmanager

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions
from frappe.desk.query_report import (
	get_filtered_data,
	get_report_doc,
	get_user_match_filters,
	validate_filters_permissions,
)
from frappe.permissions import get_role_permissions

from jarvis import compat
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._report_query_data import CONTRACT, MAX_COMPRESSED, encode
from jarvis.tools._report_scope import _assert_company_readable
from jarvis.tools.get_report_filters import get_report_filters

MISSING = "No prepared report is available for this identifier."
SCOPE_CHANGED = "Access to the stored report has changed. Generate a fresh report in Desk before querying it."


def _canonical(value):
	return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def _masked_fields(meta):
	# Frappe 15 has no native field masks. Probe the capability, not its version.
	return compat.masked_fields_of(meta) if callable(getattr(meta, "get_masked_fields", None)) else []


def _denied_columns(meta, columns, user, aliases=None):
	"""Match stored report fieldnames to the current source DocType's read levels."""
	if user == "Administrator":
		return set()
	fields = {df.fieldname for df in meta.fields}
	permitted = set(meta.get_permitted_fieldnames(user=user, permission_type="read"))
	aliases = aliases or {}
	return {
		col["sql_column"]
		for col in columns
		if aliases.get(col["fieldname"], col["fieldname"]) in fields - permitted
	}


@contextmanager
def _db_budget(deadline):
	from jarvis.api import statement_time_limit

	with statement_time_limit(max(1, math.ceil(deadline.remaining()))):
		yield
	deadline.check()


def _visible_names(doctype, names, deadline):
	"""Bound each query and each document hook by the remaining request budget."""
	try:
		with _db_budget(deadline):
			allowed = set(
				frappe.get_list(
					doctype, filters={"name": ["in", sorted(names)]}, pluck="name", limit_page_length=0
				)
			)
		hooks = frappe.get_hooks("has_permission")
		if hooks.get(doctype) or hooks.get("*"):
			ptype = "select" if frappe.only_has_select_perm(doctype) else "read"
			visible = set()
			for name in allowed:
				with _db_budget(deadline):
					if frappe.has_permission(doctype, ptype, doc=name):
						visible.add(name)
			return visible
		return allowed
	except (frappe.PermissionError, frappe.DoesNotExistError):
		return set()


def _filter_values(value):
	"""The names a stored filter value pins (a string, or a list for a MultiSelectList);
	empty when it is unset or any entry is not a non-empty string."""
	values = [value] if isinstance(value, str) else list(value) if isinstance(value, list | tuple) else []
	return values if all(isinstance(v, str) and v for v in values) else []


def _pinned_restrictions(linked_matches, filter_defs, stored_filters, ref_links, group_values):
	"""The restricted doctypes the stored run was pinned inside. A doctype is pinned only
	when EVERY link to it on the report's DocType that User Permissions apply through
	(``ref_links``: fieldname -> options) has a Link filter of the same fieldname whose
	stored value is set, is one the user may use (``linked_matches``: their User
	Permissions for it, tree descendants included) and names no tree group node
	(``group_values``), since a group value can widen the rows to descendants a
	hide-descendants permission excludes. Anything else is not credited (fail closed)."""
	pinned = set()
	for doctype, matches in linked_matches.items():
		permitted = {permission.get("doc") for permission in matches or []}
		# User Permissions apply through every such link on the DocType, so each one
		# needs its own pinned filter.
		required = {fieldname for fieldname, options in ref_links.items() if options == doctype}
		pins = [
			definition
			for definition in filter_defs
			if definition.get("fieldtype") == "Link"
			and definition.get("options") == doctype
			and definition.get("fieldname") in required
		]
		if not required or required - {definition.get("fieldname") for definition in pins}:
			continue
		for definition in pins:
			values = _filter_values(stored_filters.get(definition.get("fieldname")))
			if not values or not permitted.issuperset(values) or group_values(doctype, values):
				break
		else:
			pinned.add(doctype)
	return pinned


def _group_values(doctype, values):
	"""The tree group nodes among ``values``; none for a non-tree doctype. Any error
	counts every value as a group, so the restriction stays uncredited."""
	try:
		if not frappe.get_meta(doctype).is_tree:
			return set()
		# get_all, not get_list: this reads only the is_group flag of values already in
		# the user's permitted set, to decide a gate. A permission-filtered list could
		# hide a group row and make it pass as a leaf (fail open).
		return set(
			frappe.get_all(
				doctype,
				filters={"name": ["in", list(values)], "is_group": 1},
				pluck="name",
				limit_page_length=0,
			)
		)
	except Exception:
		return set(values)


class ReportSource:
	def __init__(self, name, *, version=None):
		self.name = name
		self.user = frappe.session.user
		self.expected_version = version or None
		self.version = None
		self.context = None
		self.schema = None
		self.report = None
		self.file = None
		self.dynamic = []
		self.proof_columns = []
		self.proof_targets = set()
		self.framework_filter = False
		self.filter_defs = []
		self.meta = self._metadata()
		if self.meta.status != "Completed":
			raise InvalidArgumentError(
				f"The selected prepared report is {self.meta.status}. Query this run after it is completed."
			)
		try:
			self.filters = json.loads(self.meta.filters or "{}")
		except (ValueError, TypeError):
			raise InvalidArgumentError("Stored report filters are invalid. Regenerate the report.") from None
		if not isinstance(self.filters, dict):
			raise InvalidArgumentError("Stored report filters must be an object.")
		self._gate()
		files = frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": "Prepared Report",
				"attached_to_name": name,
				"is_private": 1,
				"is_folder": 0,
			},
			fields=["name", "file_name", "file_size", "content_hash", "modified"],
		)
		files = [f for f in files if (f.file_name or "").lower().endswith(".gz")]
		if len(files) != 1:
			raise InvalidArgumentError(
				"The prepared report needs exactly one private gzip attachment. Regenerate it."
			)
		self.file = files[0]
		if int(self.file.file_size or 0) > MAX_COMPRESSED:
			raise InvalidArgumentError("Prepared report exceeds the 10 MiB compressed input limit.")

	def _metadata(self):
		fields = [
			"name",
			"owner",
			"report_name",
			"status",
			"filters",
			"report_end_time",
			"creation",
			"modified",
		]
		if frappe.get_meta("Prepared Report").has_field("columns"):
			fields.append("columns")
		meta = frappe.db.get_value(
			"Prepared Report", {"name": self.name, "owner": self.user}, fields, as_dict=True
		)
		if not meta:
			raise PermissionDeniedError(MISSING)
		return meta

	def _gate(self):
		try:
			try:
				self.report = get_report_doc(self.meta.report_name)
			except frappe.ValidationError:
				raise InvalidArgumentError(
					"The selected report is unavailable. Choose an enabled report."
				) from None
			metadata = get_report_filters(self.meta.report_name)
			self.filter_defs = metadata.get("filters") or []
			validate_filters_permissions(
				self.meta.report_name, self.filters, self.user, js_filters=metadata.get("filters") or []
			)
			company = self.filters.get("company")
			if company:
				companies = [company] if isinstance(company, str) else company
				if not isinstance(companies, list) or not all(isinstance(c, str) and c for c in companies):
					raise InvalidArgumentError("Stored report company scope is invalid.")
				for company in companies:
					_assert_company_readable(company, supplied=True)
			# Standard AR/AP reports need a stored company and date. Never apply today's defaults.
			if self.report.name in {
				"Accounts Receivable",
				"Accounts Receivable Summary",
				"Accounts Payable",
				"Accounts Payable Summary",
			}:
				if not self.filters.get("company") or not self.filters.get("report_date"):
					raise InvalidArgumentError(
						"The saved accounting report lacks its company or report date. Regenerate it."
					)
		except (frappe.PermissionError, frappe.ValidationError):
			raise PermissionDeniedError(
				"You do not have permission to read this report or its saved scope."
			) from None

	def read(self):
		content = compat.file_bytes(frappe.get_cached_doc("File", self.file.name))
		if len(content) > MAX_COMPRESSED:
			raise InvalidArgumentError("Prepared report exceeds the 10 MiB compressed input limit.")
		self.source_sha = hashlib.sha256(content).hexdigest()
		legacy = self.meta.get("columns")
		if isinstance(legacy, str):
			if len(legacy.encode()) > 32 * 1024:
				raise InvalidArgumentError("Stored legacy report columns exceed the metadata budget.")
			try:
				legacy = json.loads(legacy)
			except ValueError:
				raise InvalidArgumentError("Stored legacy report columns are invalid.") from None
		return content, {
			"legacy_columns": legacy or [],
			"add_total_row": bool(self.report.get("add_total_row")),
		}

	def authorize(self, schema, deadline):
		with _db_budget(deadline):
			return self._authorize(schema, deadline)

	def _authorize(self, schema, deadline):
		if (
			not isinstance(schema, dict)
			or not isinstance(schema.get("columns"), list)
			or len(schema["columns"]) > 128
		):
			raise InvalidArgumentError("Report worker returned an invalid schema.")
		self.schema = schema
		columns = schema["columns"]
		deadline.check()
		masked = set()
		ref_meta = frappe.get_meta(self.report.ref_doctype)
		denied = _denied_columns(ref_meta, columns, self.user)
		mask_names = {df.fieldname for df in _masked_fields(ref_meta)}
		masked.update(c["sql_column"] for c in columns if c["fieldname"] in mask_names)
		# Known aliases in the standard AR/AP reports must not reveal newly masked party names.
		if self.report.name in {"Accounts Receivable", "Accounts Receivable Summary"}:
			party_meta = frappe.get_meta("Customer")
			denied.update(_denied_columns(party_meta, columns, self.user, {"party_name": "customer_name"}))
			party_masks = {df.fieldname for df in _masked_fields(party_meta)}
			if "customer_name" in party_masks:
				masked.update(
					c["sql_column"] for c in columns if c["fieldname"] in {"party_name", "customer_name"}
				)
		elif self.report.name in {"Accounts Payable", "Accounts Payable Summary"}:
			party_meta = frappe.get_meta("Supplier")
			denied.update(_denied_columns(party_meta, columns, self.user, {"party_name": "supplier_name"}))
			party_masks = {df.fieldname for df in _masked_fields(party_meta)}
			if "supplier_name" in party_masks:
				masked.update(
					c["sql_column"] for c in columns if c["fieldname"] in {"party_name", "supplier_name"}
				)
		static = {c["options"] for c in columns if c["fieldtype"] == "Link" and c["options"]}
		dynamic_types = set()
		for col in columns:
			if col["fieldtype"] == "Dynamic Link":
				types = schema.get("dynamic_types", {}).get(col["fieldname"], [])
				if (
					not isinstance(types, list)
					or len(types) > 32
					or not all(isinstance(t, str) and len(t) <= 140 for t in types)
				):
					raise InvalidArgumentError("Report worker returned invalid Dynamic Link metadata.")
				dynamic_types.update(types)
				self.dynamic.append(col)
		match_filters = (
			get_user_match_filters({dt: None for dt in static | dynamic_types}, self.user)
			if self.user != "Administrator"
			else {}
		)
		hooks = frappe.get_hooks("permission_query_conditions") or {}
		doc_hooks = frappe.get_hooks("has_permission") or {}
		# Match restrictions on the source DocType's links, not only links the
		# report happened to include. Otherwise fresh inspection can bless old
		# rows after a Branch/Cost Center restriction was added.
		linked_types = {
			df.options for df in ref_meta.get_link_fields() if not df.get("ignore_user_permissions")
		}
		user_permissions = get_user_permissions(self.user)
		linked_matches = {
			dt: [
				permission
				for permission in user_permissions.get(dt, [])
				if permission.get("applicable_for") in (None, "", self.report.ref_doctype)
			]
			for dt in linked_types
		}
		# A run made with a permitted value for the restricted doctype (Company on Accounts
		# Receivable) is inside the user's restriction without a column for it.
		ref_links = {
			df.fieldname: df.options
			for df in ref_meta.get_link_fields()
			if not df.get("ignore_user_permissions")
		}
		pinned = _pinned_restrictions(
			linked_matches, self.filter_defs, self.filters, ref_links, _group_values
		)
		unrepresented_restrictions = any(
			linked_matches.get(dt) and dt not in static | dynamic_types | pinned for dt in linked_types
		)
		for dt in static | dynamic_types:
			deadline.check()
			if self.user == "Administrator":
				continue
			perms = get_role_permissions(frappe.get_meta(dt), self.user)
			if (
				match_filters.get(dt)
				or perms.get("if_owner", {}).get("read")
				or hooks.get(dt)
				or hooks.get("*")
				or doc_hooks.get(dt)
				or doc_hooks.get("*")
			):
				self.proof_targets.add(dt)
		ref_perms = get_role_permissions(ref_meta, self.user)
		if self.user != "Administrator" and (
			unrepresented_restrictions
			or ref_perms.get("if_owner", {}).get("report")
			or ref_perms.get("if_owner", {}).get("read")
			or hooks.get(self.report.ref_doctype)
			or hooks.get("*")
			or doc_hooks.get(self.report.ref_doctype)
			or doc_hooks.get("*")
		):
			if not any(c["fieldtype"] == "Link" and c["options"] == self.report.ref_doctype for c in columns):
				raise PermissionDeniedError(
					"This report's custom or owner-only row access cannot be verified from its saved columns."
				)
			self.proof_targets.add(self.report.ref_doctype)
		fields = set()
		self.framework_filter = bool({dt for dt in static if match_filters.get(dt)})
		for col in columns:
			if col["fieldtype"] == "Link" and (col["options"] in self.proof_targets or self.framework_filter):
				fields.add(col["index"])
			elif col["fieldtype"] == "Dynamic Link" and self.proof_targets.intersection(
				schema.get("dynamic_types", {}).get(col["fieldname"], [])
			):
				fields.add(col["index"])
				fields.update(c["index"] for c in columns if c["fieldname"] == col["options"])
		self.proof_columns = [columns[i] for i in sorted(fields)]
		if len({c["fieldname"] for c in self.proof_columns}) != len(self.proof_columns):
			raise PermissionDeniedError("Stored report permission columns are ambiguous. Regenerate it.")
		if any(c["sql_column"] in masked | denied for c in self.proof_columns):
			raise PermissionDeniedError(
				"Stored report access depends on an unreadable identity. Use a report with permitted identity columns."
			)
		self.context = {
			"masked_columns": sorted(masked),
			"denied_columns": sorted(denied),
			"proof_fields": sorted(fields),
		}
		policy = {
			"roles": sorted(frappe.get_roles(self.user)),
			"user_permissions": user_permissions,
			"match_filters": match_filters,
			"targets": sorted(self.proof_targets),
			"masked": sorted(masked),
			"denied": sorted(denied),
			"linked_match_filters": linked_matches,
		}
		self.version = hashlib.sha256(
			_canonical(
				[
					CONTRACT,
					frappe.local.site,
					self.user,
					dict(self.meta),
					self.source_sha,
					self.report.ref_doctype,
					str(self.report.get("modified")),
					schema,
					policy,
				]
			).encode()
		).hexdigest()
		if self.expected_version is not None and self.expected_version != self.version:
			raise InvalidArgumentError(
				"Prepared report, scope or access changed. Inspect this run again before querying."
			)
		return self.context

	def validate(self, schema, context, proofs, deadline):
		if (
			not isinstance(proofs, dict)
			or proofs.get("fields") != context["proof_fields"]
			or not isinstance(proofs.get("rows"), list)
		):
			raise InvalidArgumentError("Report worker returned invalid access proofs.")
		rows = []
		for values in proofs["rows"]:
			if not isinstance(values, list) or len(values) != len(self.proof_columns):
				raise InvalidArgumentError("Report worker returned invalid access references.")
			rows.append(
				frappe._dict(
					{col["fieldname"]: value for col, value in zip(self.proof_columns, values, strict=True)}
				)
			)
		if schema["source_rows"] > schema["total_rows"] and self.proof_columns and not rows:
			raise PermissionDeniedError(SCOPE_CHANGED)
		if self.framework_filter:
			for offset in range(0, len(rows), 200):
				deadline.check()
				batch = rows[offset : offset + 200]
				try:
					with _db_budget(deadline):
						filtered = get_filtered_data(
							self.report.ref_doctype,
							self.proof_columns,
							[frappe._dict(r) for r in batch],
							self.user,
						)
				except (KeyError, TypeError, AttributeError, frappe.PermissionError):
					raise PermissionDeniedError(SCOPE_CHANGED) from None
				if len(filtered) != len(batch):
					raise PermissionDeniedError(SCOPE_CHANGED)
		groups = {}
		for col in self.proof_columns:
			for row in rows:
				target = (
					col["options"]
					if col["fieldtype"] == "Link"
					else row.get(col["options"])
					if col["fieldtype"] == "Dynamic Link"
					else None
				)
				value = row.get(col["fieldname"])
				if target in self.proof_targets and value:
					groups.setdefault(target, set()).add(value)
		for doctype, names in groups.items():
			ordered = sorted(names)
			for offset in range(0, len(ordered), 500):
				deadline.check()
				batch = set(ordered[offset : offset + 500])
				if _visible_names(doctype, batch, deadline) != batch:
					raise PermissionDeniedError(SCOPE_CHANGED)
		deadline.check()
		self.confirm()

	def confirm(self):
		current = self._metadata()
		if _canonical(dict(current)) != _canonical(dict(self.meta)) or current.status != "Completed":
			raise InvalidArgumentError("Prepared report changed or was removed. Inspect it again.")
		current_file = frappe.db.get_value(
			"File",
			self.file.name,
			["content_hash", "modified", "attached_to_doctype", "attached_to_name", "is_private"],
			as_dict=True,
		)
		if (
			not current_file
			or current_file.content_hash != self.file.content_hash
			or str(current_file.modified) != str(self.file.modified)
			or current_file.attached_to_doctype != "Prepared Report"
			or current_file.attached_to_name != self.name
			or not current_file.is_private
		):
			raise InvalidArgumentError("Prepared report attachment changed or was removed. Inspect it again.")

	def receipt(self):
		filters = (
			self.filters
			if not self.context.get("masked_columns") and not self.context.get("denied_columns")
			else {
				k: v
				for k, v in self.filters.items()
				if k in {"company", "report_date", "from_date", "to_date"}
			}
		)
		result = {
			"prepared_report": self.name,
			"report_name": self.meta.report_name,
			"as_of": str(self.meta.report_end_time or self.meta.creation),
			"filters": filters,
			"scope_digest": hashlib.sha256(_canonical(self.filters).encode()).hexdigest(),
			"version": self.version,
		}
		if len(encode(result)) > 4000:
			raise InvalidArgumentError("Saved report scope exceeds the metadata budget. Narrow the report.")
		return result
