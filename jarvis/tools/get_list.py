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
