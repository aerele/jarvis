import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._export.model import ExportModel
from jarvis.tools.get_list import _child_table_parents

# Server-side hard ceiling. ABOVE this we FAIL CLOSED (raise) rather than return
# a silently-partial file - an export must never look complete when it isn't.
# Sized as a memory/time backstop; streaming export of larger sets is a deferred
# follow-up (see the export DEFERRED backlog).
_HARD_ROW_CEILING = 100_000


def _default_fields(doctype: str) -> list[str]:
	"""Never bulk-export every field by default; a caller opts into wide fields.
	Default to name + the title field (if distinct)."""
	meta = frappe.get_meta(doctype)
	title_field = meta.title_field or "name"
	return ["name"] + ([title_field] if title_field != "name" else [])


def from_query(doctype, filters=None, fields=None, order_by=None, parent_doctype=None) -> ExportModel:
	"""Resolve doctype+filters to a canonical ExportModel, SERVER-SIDE and
	permission-checked. Record- and field-level permissions are inherited from
	``frappe.get_list`` (a permlevel-denied field comes back NULL, so column
	alignment holds). Rows never return to the model context (reference-not-rows).

	NOT capped for the model, but FAIL-CLOSED above ``_HARD_ROW_CEILING`` so an
	oversized export raises instead of silently truncating. ``total`` is the TRUE,
	permission-filtered source count (we fetch one past the ceiling: under it,
	``len(rows)`` IS the exact count)."""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
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
		as_list=True,
	)
	if len(rows) > _HARD_ROW_CEILING:
		raise InvalidArgumentError(
			f"{doctype} matches more than {_HARD_ROW_CEILING:,} rows for your permissions "
			f"- too many to export in full. Narrow the filter; streamed export of larger "
			f"sets is not yet available."
		)
	return ExportModel(
		columns=cols,
		rows=[list(r) for r in rows],
		total=len(rows),
		meta={"doctype": doctype},
	)
