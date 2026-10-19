from typing import NamedTuple

import frappe
from frappe.utils import cint

from jarvis.exceptions import ExportTooSlowError, InvalidArgumentError, PermissionDeniedError
from jarvis.tools._export import masking
from jarvis.tools._export.model import ExportModel
from jarvis.tools.get_list import _child_table_parents, assert_child_query_fields_readable

# Server-side hard ceiling. ABOVE this we FAIL CLOSED (raise) rather than return
# a silently-partial file - an export must never look complete when it isn't.
# Kept well below a pure memory backstop ON PURPOSE: export_query runs
# SYNCHRONOUSLY inside the web request, and the agent plugin aborts every call_tool
# at 30s - a 100k wide-row fetch+render loses that race (the bench finishes and
# orphans a File the agent never receives, then retries) and can hold ~200-300MB in
# a shared worker. Larger sets go to a background export (jobs.py); 20k keeps the
# immediate path inside the budget. A site may LOWER the split with
# ``jarvis_export_immediate_row_limit`` (see ``immediate_row_limit``), never raise it.
_HARD_ROW_CEILING = 20_000

# The export SELECT's own statement limit, in seconds. The plugin aborts the whole
# call at 30 s and the rest of the export (render, save) runs after the SELECT, so
# the agent read limit (25 s) leaves no room for it: a slow query plus a 20k-row
# render passed 30 s, orphaned the File and made the agent retry the same heavy
# query. export_query's overall deadline (see there) is the other half of this.
_EXPORT_SELECT_LIMIT_S = 12


class NeedsBackground(InvalidArgumentError):
	"""The immediate row ceiling was exceeded, with all access gates satisfied."""


def immediate_row_limit() -> int:
	"""Rows an immediate export holds before it hands off to a background export.

	Lower-only site setting: the plugin aborts a call at 30 s, which is why 20,000
	exists, so a larger value is clamped to it. An unusable value (not a positive
	whole number, or a bool) means the default."""
	configured = frappe.conf.get("jarvis_export_immediate_row_limit")
	if isinstance(configured, bool):
		return _HARD_ROW_CEILING
	value = cint(configured)
	if value <= 0:
		return _HARD_ROW_CEILING
	return min(value, _HARD_ROW_CEILING)


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

	NOT capped for the model, but FAIL-CLOSED above ``immediate_row_limit()`` so an
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

	cap = immediate_row_limit()
	cols = list(fields) if fields else _default_fields(doctype)
	# Same core gap as get_list: a child's filter / sort / aggregate field is not
	# permlevel-checked against parent_doctype by the query engine.
	assert_child_query_fields_readable(doctype, parent_doctype, cols, filters, order_by)
	# Fetch one past the ceiling so we can FAIL CLOSED on an over-large export
	# instead of shipping a silently-truncated file. Under the ceiling, len(rows)
	# is the true permission-filtered total.
	rows = _get_list_bounded(
		doctype,
		filters=filters or {},
		fields=cols,
		order_by=order_by,
		parent_doctype=parent_doctype,
		limit=cap + 1,
		# get_list returns list-of-dicts by default (no as_list), which is what the
		# projection below keys on - the column-alignment fix.
	)
	if len(rows) > cap:
		raise NeedsBackground(
			f"{doctype} matches more than {cap:,} rows for your permissions, more than an immediate "
			f"export holds. Narrow the filter (e.g. by date or status), or split it by a filter into "
			f"a few exports."
		)
	# Project each row dict onto the REQUESTED columns. A field the caller lacks
	# permlevel-read on is dropped from the row by get_list (on v17), so keying by
	# column name - blank when absent - keeps every value under its own header on
	# every engine, instead of the shift a positional as_list would produce.
	# Keying is by plain fieldname (the tool's contract; fields=['*'] is rejected
	# and aggregate/aliased expressions are out of scope - they'd key as blank).
	return ExportModel(
		columns=cols,
		rows=[[row.get(c) for c in cols] for row in rows],
		total=len(rows),
		meta={"doctype": doctype, "headers": _unique_headers([_Column(c, doctype, None) for c in cols])},
	)


def _get_list_bounded(*args, **kwargs):
	from jarvis import _failure_kind
	from jarvis.api import statement_time_limit

	try:
		with statement_time_limit(_EXPORT_SELECT_LIMIT_S):
			return frappe.get_list(*args, **kwargs)
	except Exception as exc:
		if _failure_kind.too_slow(exc):
			raise ExportTooSlowError("This export's query exceeded the immediate time limit.") from None
		raise


def from_spec(spec) -> ExportModel:
	"""Resolve a ``query`` spec (joins, aggregates, EXISTS / NOT EXISTS) to an
	ExportModel, SERVER-SIDE. The spec goes through the query tool's own compiler,
	so it gets exactly the same gates as ``query``: DocType read on every table it
	touches, the per-site allowlist, the field-level (permlevel) ACL on every
	column, and the record-level permission weave. On top, an export needs the
	Export DocPerm on each table its columns come from: FROM and joins, and for a
	child table the owning parent its rows are scoped to (``query`` resolves it).
	A column masked for the user (Frappe 16 ``mask``) is masked as ``get_list``
	would, and a computed column over one is refused (``_export.masking``).

	Every row is fetched (not the model-facing ``query`` caps), FAIL-CLOSED above
	``immediate_row_limit()`` like ``from_query``, and the SELECT is stopped at
	``_EXPORT_SELECT_LIMIT_S`` so it cannot outlive the tool call. A spec ``limit``
	below the ceiling still applies (a deliberate "top 50"); when it actually cut
	the result, ``meta["limited_by_spec"]`` holds it so the caller can say so. An
	``offset`` is refused."""
	from jarvis.tools.query import compile_spec_for_export

	cap = immediate_row_limit()
	q, tables, export_doctypes, child_parents = compile_spec_for_export(spec, cap + 1)
	for dt in export_doctypes:
		if not frappe.permissions.can_export(dt):
			raise PermissionDeniedError(f"no export permission on {dt}")
	columns = _spec_columns(spec, tables)
	masked = masking.masked_fields(tables, child_parents)
	masking.refuse_masked_expressions(columns, q, masked)
	rows, limited_by_spec = _cut_to_spec_limit(spec, _run_bounded(q))
	if len(rows) > cap:
		raise NeedsBackground(
			f"this query matches more than {cap:,} rows for your permissions, more than an immediate "
			f"export holds. Narrow it (e.g. by date or status), or split it by a filter into a few "
			f"exports."
		)
	masking.mask_rows(columns, rows, masked)
	from_dt = next(iter(tables.values()))
	meta = {"doctype": from_dt, "headers": _unique_headers(columns)}
	if limited_by_spec:
		meta["limited_by_spec"] = limited_by_spec
	return ExportModel(
		columns=[c.key for c in columns],
		rows=[[row.get(c.key) for c in columns] for row in rows],
		total=len(rows),
		meta=meta,
	)


def _cut_to_spec_limit(spec: dict, rows: list) -> tuple[list, int | None]:
	"""``(rows, limit)``: the query fetched one row past the spec's ``limit``, so a
	row beyond it proves the limit cut the result. Drop that probe row and return the
	limit; ``None`` when the spec's limit did not cut anything (or there is none)."""
	limit = spec.get("limit")
	if limit is None or len(rows) <= limit:
		return rows, None
	return rows[:limit], limit


def _run_bounded(q) -> list:
	"""Run the export SELECT under a per-statement limit: the agent read limit
	(``api._agent_read_timeout_s``) or ``_EXPORT_SELECT_LIMIT_S``, whichever is
	smaller. A site that switched the agent limit off (0) keeps no limit here; the
	overall deadline in ``export_query`` still keeps the call under the plugin abort.
	Exports are write tools, so the agent path leaves them unlimited; a spec export
	is an agent-built query, and past the plugin's call timeout nobody would receive
	the file. The query runs through ``q.run`` (its values parameterised), never as a
	rendered SQL string. A query stopped at the limit is refused with what to do
	instead."""
	from jarvis import _failure_kind
	from jarvis.api import _agent_read_timeout_s, statement_time_limit

	agent_limit = _agent_read_timeout_s()
	limit = min(agent_limit, _EXPORT_SELECT_LIMIT_S) if agent_limit else 0
	try:
		with statement_time_limit(limit):
			return q.run(as_dict=True)
	except Exception as e:
		if _failure_kind.too_slow(e):
			raise ExportTooSlowError(
				f"this export's query ran past the {limit}-second limit, so the database stopped "
				f"it. Narrow it (a date range or a status filter), or split it into a few exports."
			) from None
		raise


class _Column(NamedTuple):
	key: str  # the row key (field name, or the `as` name of a computed column)
	doctype: str | None  # the DocType a plain column belongs to
	label: str | None  # a fixed header (computed columns), else the field's label
	alias: str | None = None  # the spec alias of a plain column's table; None when computed


def _spec_columns(spec: dict, tables: dict) -> list[_Column]:
	"""One ``_Column`` per select entry, in order. A plain column keys by its field
	name and takes the field's label; a computed one keys and is labelled by its
	``as`` name, which an export requires (without it the column's name is
	whatever SQL makes of the expression). Two columns with the same key would
	overwrite each other in the row, so that is refused with the fix."""
	from_alias = next(iter(tables), None)
	out = []
	for item in spec.get("select") or ["name"]:
		if isinstance(item, str):
			alias, _, field = item.rpartition(".")
			alias = alias or from_alias
			out.append(_Column(field, tables.get(alias, tables[from_alias]), None, alias))
		elif isinstance(item, dict) and item.get("as"):
			out.append(_Column(item["as"], None, _humanize(item["as"])))
		else:
			raise InvalidArgumentError(
				"give every computed column (an aggregate or expression) an 'as' name in an export"
			)
	keys = [c.key for c in out]
	clash = sorted({k for k in keys if keys.count(k) > 1})
	if clash:
		raise InvalidArgumentError(
			f"two columns are both named {clash[0]!r}: rename one with "
			f'{{"expr": {{"field": "<alias>.{clash[0]}"}}, "as": "<new_name>"}}'
		)
	return out


def _humanize(name: str) -> str:
	return " ".join(w.capitalize() for w in str(name).replace("_", " ").split()) or str(name)


def _field_label(doctype: str | None, fieldname: str) -> str:
	"""What a person calls the column: the field's label ("ID" for the record name),
	else the field name made readable."""
	if fieldname == "name":
		return "ID"
	label = None
	if doctype:
		try:
			label = frappe.get_meta(doctype).get_label(fieldname)
		except Exception:
			label = None
	# Meta.get_label answers "No Label" for a field it does not know (a standard
	# column such as creation): read that as no label, like an empty one.
	if not label or label == fieldname or label == "No Label":
		return _humanize(fieldname)
	return label


def _unique_headers(columns: list[_Column]) -> list[str]:
	"""Header text per column. A label two columns share (two "ID"s, or "Status" on
	the order and on its invoice) is told apart by its DocType: "Supplier ID",
	"Purchase Invoice ID"; a label still shared after that keeps its field name."""
	labels = [c.label or _field_label(c.doctype, c.key) for c in columns]
	headers = []
	for column, label in zip(columns, labels, strict=True):
		if labels.count(label) > 1 and column.doctype:
			label = f"{column.doctype} {label}"
		headers.append(label)
	out = []
	for column, header in zip(columns, headers, strict=True):
		out.append(f"{header} ({column.key})" if headers.count(header) > 1 else header)
	return out
