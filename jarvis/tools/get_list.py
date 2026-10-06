import re

import frappe

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
	ResultTooLargeError,
)

MAX_LIMIT = 1000

# Row guard: refuse a result over this size unless the caller passes
# ``confirm_large=True``. Sits below ``MAX_LIMIT`` (the hard ceiling on
# Frappe's ``limit`` parameter) and above the default ``limit=20`` so
# narrow queries fit silently and only the agent's wide ``limit=N``
# calls trigger the guard. See ``ResultTooLargeError`` for the
# 2026-06-22 outage that motivated this.
ROW_GUARD = 200

_CHILD_TABLE_FIELDTYPES = ("Table", "Table MultiSelect")

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _idents(text) -> set:
	"""Every identifier in a SQL-ish fragment (``rate desc``, ``max(rate)``,
	```tabX`.`rate```), backticks dropped."""
	return set(_IDENT.findall(str(text).replace("`", "")))


def _filter_fieldnames(filters) -> set:
	"""The field each filter condition tests, from Frappe's filter shapes:
	``{field: value}``, ``[[field, op, value]]``, ``[[doctype, field, op, value]]``,
	a single ``[field, op, value]``, or the same as a JSON string. Only the field
	position is read, so a filter VALUE that happens to spell a field name is not
	mistaken for a reference."""
	if isinstance(filters, str):
		try:
			filters = frappe.parse_json(filters)
		except Exception:
			return set()
	refs = []
	if isinstance(filters, dict):
		refs = list(filters)
	elif isinstance(filters, list | tuple) and filters:
		conditions = filters if isinstance(filters[0], list | tuple | dict) else [filters]
		for cond in conditions:
			if isinstance(cond, dict):
				refs += list(cond)
			elif isinstance(cond, list | tuple) and cond:
				refs.append(cond[1] if len(cond) >= 4 else cond[0])
	names = set()
	for ref in refs:
		if isinstance(ref, str) and ref.strip():
			names.add(ref.replace("`", "").rsplit(".", 1)[-1].strip())
	return names


def _aggregate_fieldnames(fields) -> set:
	"""Identifiers inside aggregate / function select entries (``{"MAX": "rate"}``,
	``"max(rate)"``, ``"sum(rate) as total"``). A plain field name is left to
	Frappe, which drops a permlevel-denied SELECT field on its own."""
	names = set()
	for entry in fields or []:
		if isinstance(entry, dict):
			for value in entry.values():
				names |= _idents(value)
		elif isinstance(entry, str) and "(" in entry:
			names |= _idents(entry)
	return names


def assert_child_query_fields_readable(doctype, parent_doctype, fields, filters, order_by) -> None:
	"""Refuse a child-table filter / sort / aggregate on a field the caller cannot
	read through ``parent_doctype``.

	Frappe's query engine checks a child's SELECT fields against
	``parent_doctype``, but passes NO parent to the field check for filters,
	``order_by`` and aggregate arguments - and a child DocType has no permissions
	of its own, so that check is skipped. ``get_list("Delivery Note Item",
	fields=[{"MAX": "rate"}], parent_doctype="Delivery Note")`` therefore returned
	a rate the caller cannot see, and filtering or sorting on it leaked it too.
	Until core passes the parent there, enforce it here."""
	if not parent_doctype or frappe.session.user == "Administrator":
		return
	meta = frappe.get_meta(doctype)
	if not meta.istable:
		return
	levels = set(meta.get_permlevel_access(permission_type="read", parenttype=parent_doctype))
	denied = {df.fieldname for df in meta.fields if df.permlevel and df.permlevel not in levels}
	if not denied:
		return
	referenced = _filter_fieldnames(filters) | _idents(order_by or "") | _aggregate_fieldnames(fields)
	hit = sorted(referenced & denied)
	if hit:
		raise PermissionDeniedError(
			f"no read permission on {doctype} field(s) {', '.join(hit)} through {parent_doctype}, "
			"so they cannot be filtered, sorted or aggregated on"
		)


def _child_table_parents(doctype: str) -> list[str]:
	"""DocTypes that own ``doctype`` as a child table (via a Table field).

	Frappe derives a child DocType's read permission from its parent, so when a
	caller queries a child table without a parent we surface the candidate
	parents to point them at ``parent_doctype``. Reads schema metadata via
	``frappe.get_all`` (permissions bypassed) - ``DocField`` is itself a child
	table, so ``get_list`` here would hit the very wall we are explaining.
	Checks both standard fields (``DocField``) and ``Custom Field`` (``dt``).
	"""
	parents = frappe.get_all(
		"DocField",
		filters={"fieldtype": ["in", _CHILD_TABLE_FIELDTYPES], "options": doctype},
		pluck="parent",
	)
	parents += frappe.get_all(
		"Custom Field",
		filters={"fieldtype": ["in", _CHILD_TABLE_FIELDTYPES], "options": doctype},
		pluck="dt",
	)
	return list(dict.fromkeys(parents))


def _readable_child_parents(doctype: str) -> list[str]:
	"""Owning parents of child ``doctype`` the current user can read it THROUGH.

	A child (istable) DocType carries no permissions of its own; Frappe derives
	its read access from a parent via ``has_permission(child, parent_doctype=P)``.
	This factors the F1 readable-parent comprehension - ``_child_table_parents``
	filtered to the parents the caller actually has that derived read on,
	order-preserving. Shared by ``query.py``'s step-3 DocType gate, its field-ACL
	resolver (``_permitted_read_fields``) and its record-level scoping-parent
	resolver, so all three agree on which parents a child is reachable through.
	"""
	return [
		p
		for p in _child_table_parents(doctype)
		if frappe.has_permission(doctype, ptype="read", parent_doctype=p)
	]


def get_list(
	doctype: str,
	fields: list[str] | None = None,
	filters: dict | list | None = None,
	order_by: str | None = None,
	limit: int = 20,
	confirm_large: bool = False,
	parent_doctype: str | None = None,
) -> list[dict]:
	"""List documents with filters.

	Frappe's get_list applies per-user record permissions automatically.
	We additionally enforce DocType-level read permission and cap the limit.

	Child (Table) DocTypes have no permissions of their own - Frappe derives
	their access from a parent DocType. To read child rows (e.g. a Timesheet's
	``time_logs``) pass ``parent_doctype`` (the owning DocType) and usually
	filter by ``parent``; permission is then derived from the parent. Calling
	get_list on a child DocType without ``parent_doctype`` raises
	``InvalidArgumentError``. Note child tables lack parent-only fields (e.g.
	employee/date live on ``Timesheet``, not ``Timesheet Detail``) - to filter
	or aggregate by those, query the parent with a join via the ``query`` tool
	or use ``run_report``.

	Row guard: results above ``ROW_GUARD`` (200) rows raise
	``ResultTooLargeError`` unless ``confirm_large=True``. The agent
	should respond by narrowing the filter, aggregating the question
	via ``query``, or - for genuine export workflows - retrying
	with ``confirm_large=True``.
	"""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if limit <= 0 or limit > MAX_LIMIT:
		raise InvalidArgumentError(f"limit must be between 1 and {MAX_LIMIT}")

	if frappe.get_meta(doctype).istable:
		if not parent_doctype:
			parents = _child_table_parents(doctype)
			hint = f" (e.g. parent_doctype='{parents[0]}')" if parents else ""
			raise InvalidArgumentError(
				f"'{doctype}' is a child table; pass parent_doctype{hint} and filter by "
				f"parent, or query its parent with a join via the `query` tool / use "
				f"run_report. Child tables lack parent-only fields like employee/date."
			)
	else:
		# parent_doctype only makes sense for child tables; drop a stray value so
		# it never turns a normal query into a "DocType <x> not found" error.
		parent_doctype = None

	if not frappe.has_permission(doctype, ptype="read", parent_doctype=parent_doctype):
		raise PermissionDeniedError(f"no read permission on {doctype}")
	assert_child_query_fields_readable(doctype, parent_doctype, fields, filters, order_by)

	rows = frappe.get_list(
		doctype,
		fields=fields or ["name"],
		filters=filters or {},
		order_by=order_by,
		limit=limit,
		parent_doctype=parent_doctype,
	)
	if len(rows) > ROW_GUARD and not confirm_large:
		raise ResultTooLargeError(
			row_count=len(rows),
			limit=ROW_GUARD,
			tool="get_list",
		)
	return rows
