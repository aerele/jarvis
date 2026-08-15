import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._export.model import ExportModel
from jarvis.tools.get_list import _child_table_parents

# Server-side hard ceiling. ABOVE this we FAIL CLOSED (raise) rather than return
# a silently-partial file - an export must never look complete when it isn't.
# Kept well below a pure memory backstop ON PURPOSE: export_query runs
# SYNCHRONOUSLY inside the web request, and the agent plugin aborts every call_tool
# at 30s - a 100k wide-row fetch+render loses that race (the bench finishes and
# orphans a File the agent never receives, then retries) and can hold ~200-300MB in
# a shared worker. Streaming/async for larger sets is a deferred follow-up (see the
# export DEFERRED backlog); until then, 20k keeps the sync path inside the budget.
_HARD_ROW_CEILING = 20_000


def _default_fields(doctype: str) -> list[str]:
	"""Never bulk-export every field by default; a caller opts into wide fields.
	Default to name + the title field (if distinct)."""
	meta = frappe.get_meta(doctype)
	title_field = meta.title_field or "name"
	return ["name"] + ([title_field] if title_field != "name" else [])


def from_query(doctype, filters=None, fields=None, order_by=None, parent_doctype=None) -> ExportModel:
	"""Resolve doctype+filters to a canonical ExportModel, SERVER-SIDE and
	permission-checked. Record- and field-level permissions are inherited from
	``frappe.get_list``; a permlevel-denied field is DROPPED from the result on the
	v17 query engine (v16 nulls it in place), so we fetch ``as_dict`` and PROJECT
	onto the requested columns - a dropped field becomes a blank cell under its own
	header and columns stay aligned on every engine. Also gated on the Export
	DocPerm (``can_export``), matching Frappe's own query->file export. Rows never
	return to the model context (reference-not-rows).

	NOT capped for the model, but FAIL-CLOSED above ``_HARD_ROW_CEILING`` so an
	oversized export raises instead of silently truncating. ``total`` is the TRUE,
	permission-filtered source count (we fetch one past the ceiling: under it,
	``len(rows)`` IS the exact count)."""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not frappe.db.exists("DocType", doctype):
		raise InvalidArgumentError(f"unknown DocType: {doctype}")
	# A stray scalar `fields="name"` would otherwise iterate into characters; the
	# wire schema enforces an array, so this is defense-in-depth.
	if isinstance(fields, str):
		fields = [fields]
	if fields is not None and "*" in fields:
		raise InvalidArgumentError("fields=['*'] is not supported for export - name the columns you want.")

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
		# parent_doctype only makes sense for a child table; drop a stray value so
		# it never turns a normal query into a "DocType <x> not found" error.
		parent_doctype = None

	if not frappe.has_permission(doctype, ptype="read", parent_doctype=parent_doctype):
		raise PermissionDeniedError(f"no read permission on {doctype}")
	# Honor Frappe's Export DocPerm (like frappe.desk.reportview.export_query): a
	# bulk file export is governed by the Export permission, not just read (System
	# Manager bypasses). export_query lifts the row cap, so a read-but-not-Export
	# user must not gain a bulk-exfil path. For a child table the Export perm lives
	# on the owning parent, so check that.
	export_dt = parent_doctype or doctype
	if not frappe.permissions.can_export(export_dt):
		raise PermissionDeniedError(f"no export permission on {export_dt}")

	cols = list(fields) if fields else _default_fields(doctype)
	# Fetch one past the ceiling so we can FAIL CLOSED on an over-large export
	# instead of shipping a silently-truncated file. Under the ceiling, len(rows)
	# is the true permission-filtered total.
	rows = frappe.get_list(
		doctype,
		filters=filters or {},
		fields=cols,
		order_by=order_by,
		parent_doctype=parent_doctype,
		limit=_HARD_ROW_CEILING + 1,
		# get_list returns list-of-dicts by default (no as_list), which is what the
		# projection below keys on - the column-alignment fix.
	)
	if len(rows) > _HARD_ROW_CEILING:
		raise InvalidArgumentError(
			f"{doctype} matches more than {_HARD_ROW_CEILING:,} rows for your permissions "
			f"- too many to export in full. Narrow the filter (e.g. by date or status); "
			f"streamed export of larger sets is not yet available."
		)
	# Project each row dict onto the REQUESTED columns. A field the caller lacks
	# permlevel-read on is dropped from the row by get_list (on v17), so keying by
	# column name - blank when absent - keeps every value under its own header on
	# every engine, instead of the shift a positional as_list would produce.
	return ExportModel(
		columns=cols,
		rows=[[row.get(c) for c in cols] for row in rows],
		total=len(rows),
		meta={"doctype": doctype},
	)
