"""PREPARE loads all permissions/metadata. STREAM only transforms rows and writes disk."""

import csv
import datetime
import itertools
import zlib
from dataclasses import dataclass
from pathlib import Path

import frappe

from jarvis._xlsx_highlights import add_xlsxwriter_highlights, validate_highlights
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._export import masking, renderers, resolvers
from jarvis.tools._export.model import ExportModel
from jarvis.tools._export.safety import escape_formula


class ExportLimit(Exception):
	def __init__(self, status, message):
		self.status = status
		super().__init__(message)


@dataclass
class Prepared:
	sql: str
	params: dict | None
	columns: list
	headers: list
	masked: dict
	fieldtypes: list
	rules: list
	date_format: str
	time_format: str
	limit: int | None
	number_format: str = "#,##0.00"
	spool_cap: int = 1024 * 1024 * 1024
	# The DocType the rows come from (a spec's FROM table); the default title of the export.
	doctype: str = ""


def _fieldtype(item, tables):
	"""Infer expression output types from the already validated spec, without rows."""
	if isinstance(item, str):
		alias, _, name = item.rpartition(".")
		dt = tables.get(alias or next(iter(tables)))
		df = frappe.get_meta(dt).get_field(name) if dt else None
		return df.fieldtype if df else {"creation": "Datetime", "modified": "Datetime"}.get(name, "Data")
	if not isinstance(item, dict):
		return "Float" if isinstance(item, (int, float)) else "Data"
	if item.get("agg") == "count":
		return "Int"
	if item.get("agg") in {"avg", "sum"}:
		return "Float"
	if "field" in item:
		return _fieldtype(item["field"], tables)
	if "expr" in item:
		return _fieldtype(item["expr"], tables)
	if "literal" in item:
		return _fieldtype(item["literal"], {}) if not isinstance(item["literal"], str) else "Data"
	if "op" in item:
		return "Float"
	function = str(item.get("fn", "")).lower()
	if function == "date_trunc":
		return "Date"
	if function == "date_add":
		return _fieldtype((item.get("args") or [None])[0], tables)
	if function in {"date_part", "length"}:
		return "Int"
	if function in {"round", "abs", "ceil", "floor", "add", "sub", "mul", "div", "neg"}:
		return "Float"
	if function in {"coalesce", "ifnull"}:
		return _fieldtype((item.get("args") or [None])[0], tables)
	if function == "case":
		kinds = {_fieldtype(arg.get("then", arg.get("else")), tables) for arg in item.get("args", [])}
		return next(iter(kinds)) if len(kinds) == 1 else "Data"
	return "Data"


def _filter_sql(doctype, parent, **kwargs):
	"""Use the installed list compiler, adding explicit parent record scope on v15."""
	from jarvis.tools.query import _child_record_scope

	scope = _child_record_scope(frappe.qb.DocType(doctype), doctype, parent) if parent else None
	try:
		from frappe.model.qb_query import DatabaseQuery
	except ModuleNotFoundError as exc:
		if exc.name != "frappe.model.qb_query":
			raise
		from frappe.model.db_query import DatabaseQuery as LegacyQuery

		class DatabaseQuery(LegacyQuery):
			def prepare_args(self):
				args = super().prepare_args()
				if scope is not None:
					condition = scope.get_sql(quote_char="`", with_namespace=True)
					args.conditions = (
						f"({args.conditions}) AND ({condition})" if args.conditions else condition
					)
				return args

		# Legacy SQL already contains escaped values. None deliberately disables
		# PyMySQL's percent interpolation, just as frappe.db.sql(query) does.
		sql = DatabaseQuery(doctype).execute(**kwargs, run=False)
		if not isinstance(sql, str):
			raise InvalidArgumentError("This DocType does not support background streaming exports.")
		return sql, None
	query = DatabaseQuery(doctype).execute(**kwargs, run=False)
	if not callable(getattr(query, "walk", None)):
		raise InvalidArgumentError("This DocType does not support background streaming exports.")
	if scope is not None:
		query = query.where(scope)
	return query.walk()


def prepare(source, row_cap, highlights=None):
	"""Compile fresh under the caller, including every Export gate, before streaming."""
	from jarvis.tools.query import compile_spec_for_export

	if "spec" in source:
		spec = source["spec"]
		q, tables, export_doctypes, parents = compile_spec_for_export(spec, row_cap + 1)
		for dt in export_doctypes:
			if not frappe.permissions.can_export(dt):
				raise PermissionDeniedError(f"no export permission on {dt}")
		columns = resolvers._spec_columns(spec, tables)
		types = [_fieldtype(item, tables) for item in spec.get("select") or ["name"]]
		masked = masking.masked_fields(tables, parents)
		masking.refuse_masked_expressions(columns, q, masked)
		sql, params = q.walk()
		limit = spec.get("limit")
		source_doctype = next(iter(tables.values()))
	else:
		from jarvis.tools.get_list import assert_child_query_fields_readable

		dt = source["doctype"]
		parent = source.get("parent_doctype") if frappe.get_meta(dt).istable else None
		if frappe.get_meta(dt).istable and not parent:
			raise InvalidArgumentError("A child-table export requires parent_doctype.")
		if not frappe.has_permission(dt, "read", parent_doctype=parent):
			raise PermissionDeniedError(f"no read permission on {dt}")
		if not frappe.permissions.can_export(parent or dt):
			raise PermissionDeniedError(f"no export permission on {parent or dt}")
		fields = source.get("fields") or resolvers._default_fields(dt)
		if isinstance(fields, str):
			fields = [fields]
		# This path is for plain fields; expression queries use the structured compiler.
		if any(not isinstance(f, str) or not f.replace("_", "").isalnum() for f in fields):
			raise InvalidArgumentError("Name plain fields for a filter export; use spec for expressions.")
		assert_child_query_fields_readable(dt, parent, fields, source.get("filters"), source.get("order_by"))
		sql, params = _filter_sql(
			dt,
			parent,
			fields=fields,
			filters=source.get("filters") or {},
			order_by=source.get("order_by"),
			parent_doctype=parent,
			limit_page_length=row_cap + 1,
		)
		columns = [resolvers._Column(f, dt, None, "base") for f in fields]
		types = [_fieldtype(f, {"base": dt}) for f in fields]
		masked = masking.masked_fields({"base": dt}, {"base": parent})
		limit = None
		source_doctype = dt
	labels = resolvers._unique_headers(columns)
	model = ExportModel(columns=[c.key for c in columns], rows=[], total=0, meta={"headers": labels})
	from frappe.utils.xlsxutils import ILLEGAL_CHARACTERS_RE, handle_html

	headers = [ILLEGAL_CHARACTERS_RE.sub("", renderers._xlsx_cell(h, model, handle_html)) for h in labels]
	# Validate rules using metadata representatives, never scan the result set.
	probe = [datetime.date(2000, 1, 1) if t in {"Date", "Datetime", "Time"} else None for t in types]
	rules = validate_highlights(renderers._rules_by_header(highlights, model, labels), [headers, probe])
	return Prepared(
		sql,
		params,
		columns,
		headers,
		masked,
		types,
		rules,
		frappe.get_system_settings("date_format") or "yyyy-mm-dd",
		frappe.get_system_settings("time_format") or "hh:mm:ss",
		limit,
		"#,##0." + "0" * min(12, max(0, int(frappe.get_system_settings("float_precision") or 2))),
		int(frappe.conf.get("jarvis_export_spool_max_mb", 1024)) * 1024 * 1024,
		source_doctype,
	)


class _Range:
	"""The shared highlight renderer needs only a header and a final row count."""

	def __init__(self, headers, rows):
		self.headers, self.rows = headers, rows

	def __len__(self):
		return self.rows + 1

	def __getitem__(self, index):
		if index == 0:
			return self.headers
		raise IndexError(index)


def write(prepared, rows, path, fmt, row_cap, size_cap, check, progress):
	"""Bounded memory; no Frappe/metadata/permission calls after entering this function.

	Measure compressed XML while spooling, then check the completed ZIP. A separate
	configurable disk budget bounds XML scratch space. CSV checks actual bytes.
	"""
	from frappe.utils.xlsxutils import ILLEGAL_CHARACTERS_RE, handle_html

	model = ExportModel(columns=[c.key for c in prepared.columns], rows=[], total=0, meta={})
	count = 0
	limited = False

	def transformed():
		nonlocal count, limited
		for row in rows:
			if prepared.limit is not None and count >= prepared.limit and prepared.limit <= row_cap:
				limited = True
				break
			if count >= row_cap:
				raise ExportLimit(
					"too large", f"More than {row_cap:,} rows match. Narrow or split the filters."
				)
			masking.mask_rows(prepared.columns, [row], prepared.masked)
			values = [row.get(c.key) for c in prepared.columns]
			if fmt == "xlsx":
				values = [renderers._xlsx_cell(v, model, handle_html) for v in values]
				values = [ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v for v in values]
			else:
				values = [escape_formula(v) for v in values]
			count += 1
			if count % 1000 == 0:
				check()
			yield values

	iterator = transformed()
	check()
	if fmt == "csv":
		# Server-created private attempt path; no caller-supplied filename reaches this writer.
		# nosemgrep: frappe-security-file-traversal
		with open(path, "w", encoding="utf-8", newline="") as output:
			writer = csv.writer(output)
			writer.writerow([escape_formula(h) for h in prepared.headers])
			for values in iterator:
				writer.writerow(values)
				if count % 1000 == 0:
					progress(count)
					output.flush()
					if output.tell() > size_cap:
						raise ExportLimit(
							"too large",
							"The export exceeds the file-size limit. Narrow or split the filters.",
						)
	else:
		import xlsxwriter

		# xlsxwriter keeps only one worksheet row and spools XML beside the .part.
		book = xlsxwriter.Workbook(
			str(path),
			{
				"constant_memory": True,
				"tmpdir": str(Path(path).parent),
				"strings_to_formulas": False,
				"strings_to_urls": False,
			},
		)
		try:
			sheet = book.add_worksheet("Export")
			compressor = zlib.compressobj(wbits=-15)
			compressed_size, spool_offset = 0, 0
			first = list(itertools.islice(iterator, 1000))
			bold = book.add_format({"bold": True, "bg_color": "#E8EDF2"})
			formats = {
				"Date": book.add_format({"num_format": prepared.date_format}),
				"Datetime": book.add_format(
					{"num_format": prepared.date_format + " " + prepared.time_format}
				),
				"Time": book.add_format({"num_format": prepared.time_format}),
				"Currency": book.add_format({"num_format": "#,##0.00"}),
				"Float": book.add_format({"num_format": prepared.number_format}),
				"Int": book.add_format({"num_format": "0"}),
			}
			for i, header in enumerate(prepared.headers):
				width = max([len(str(header))] + [len(str(r[i] or "")) for r in first])
				sheet.set_column(i, i, min(60, max(12, width + 2)))
				sheet.write(0, i, header, bold)
			sheet.freeze_panes(1, 0)
			for n, values in enumerate(itertools.chain(first, iterator), 1):
				for i, value in enumerate(values):
					sheet.write(n, i, value, formats.get(prepared.fieldtypes[i]))
				if n % 1000 == 0:
					progress(n)
					check()
					sheet.row_data_fh.flush()
					if sheet.row_data_fh.tell() > prepared.spool_cap:
						raise ExportLimit(
							"too large",
							"The workbook exceeds the export disk budget. Narrow or split the filters.",
						)
					# xlsxwriter generated this spool inside the private attempt directory.
					# nosemgrep: frappe-security-file-traversal
					with open(sheet.row_data_filename, "rb") as spool:
						spool.seek(spool_offset)
						while block := spool.read(64 * 1024):
							compressed_size += len(compressor.compress(block))
						spool_offset = spool.tell()
					if compressed_size > size_cap:
						raise ExportLimit(
							"too large",
							"The workbook exceeds the file-size limit. Narrow or split the filters.",
						)
			add_xlsxwriter_highlights(book, sheet, _Range(prepared.headers, count), prepared.rules)
		finally:
			book.close()
	check()
	if Path(path).stat().st_size > size_cap:
		raise ExportLimit("too large", "The export exceeds the file-size limit. Narrow or split the filters.")
	progress(count)
	return {
		"rows_written": count,
		"limited_by_spec": prepared.limit if limited else 0,
		"cells_truncated": bool(model.meta.get("cells_truncated")),
	}
