"""Pure, bounded prepared-report parsing and tables. Used only in the child."""

import json
import sqlite3
import zlib

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._document_query_sources import _constant, _Number, _object
from jarvis.tools._report_rows import cell, normalise_columns
from jarvis.tools._spreadsheet_query import (
	MAX_CELLS,
	MAX_COLUMNS,
	MAX_TOTAL_ROWS,
	_amount,
	_register_amounts,
	query_loaded,
)

CONTRACT = "prepared-report-query-v1"
MAX_COMPRESSED = 10 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MAX_SCHEMA = 32 * 1024
MAX_CONTEXT = 64 * 1024
MAX_PROOFS = 2 * 1024 * 1024
MAX_ANSWER = 68 * 1024
NUMERIC = frozenset({"Currency", "Float", "Int", "Percent", "Duration", "Check"})


def encode(value):
	return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _flag(value):
	return value not in (None, False, 0, "", "0", "false")


def decode_report(content, *, legacy_columns=None, add_total_row=False):
	if len(content) > MAX_COMPRESSED:
		raise InvalidArgumentError("Prepared report exceeds the 10 MiB compressed input limit.")
	try:
		decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
		raw = decoder.decompress(content, MAX_EXPANDED + 1)
		if len(raw) > MAX_EXPANDED or decoder.unconsumed_tail:
			raise InvalidArgumentError("Prepared report exceeds the 32 MiB expanded input limit.")
		if not decoder.eof or decoder.unused_data:
			raise InvalidArgumentError(
				"Stored report gzip is incomplete or contains extra data. Regenerate it."
			)
		data = json.loads(
			raw, parse_int=_Number, parse_float=_Number, parse_constant=_constant, object_pairs_hook=_object
		)
		del raw
	except (ValueError, zlib.error, RecursionError, UnicodeError):
		raise InvalidArgumentError("Stored report gzip or JSON is invalid. Regenerate the report.") from None
	if isinstance(data, dict):
		columns, rows = data.get("columns"), data.get("result")
		skip = data.get("skip_total_row")
		has_skip = "skip_total_row" in data
	elif isinstance(data, list):
		columns, rows, skip, has_skip = legacy_columns, data, None, False
	else:
		raise InvalidArgumentError("Stored report has no tabular result.")
	rows = [] if rows is None else rows
	if not isinstance(columns, list) or not isinstance(rows, list):
		raise InvalidArgumentError("Stored report columns and result must be lists.")
	if len(columns) > MAX_COLUMNS or len(rows) > MAX_TOTAL_ROWS or len(rows) * len(columns) > MAX_CELLS:
		raise InvalidArgumentError("Prepared report exceeds 100,000 rows, 128 columns or two million cells.")
	if rows and not columns:
		raise InvalidArgumentError("Stored report has rows without column definitions.")
	normalized = normalise_columns(columns)
	for i, col in enumerate(normalized, 1):
		col["sql_column"] = f"c{i}"
		col["index"] = i - 1
		col["hidden"] = _flag(columns[i - 1].get("hidden")) if isinstance(columns[i - 1], dict) else False
		if any(
			not isinstance(col.get(k), str) or len(col[k]) > 256
			for k in ("fieldname", "label", "fieldtype", "options")
		):
			raise InvalidArgumentError("Stored report has invalid or oversized column metadata.")
	total_index = None
	if rows:
		# Modern Frappe normalizes details to dicts, then appends one positional
		# footer. Current Report.add_total_row can change after generation.
		all_dicts = all(isinstance(row, dict) for row in rows)
		mixed_total = (
			len(rows) > 1 and isinstance(rows[-1], list) and all(isinstance(row, dict) for row in rows[:-1])
		)
		if mixed_total:
			if has_skip and _flag(skip):
				raise InvalidArgumentError("Stored report total-row metadata is inconsistent. Regenerate it.")
			total_index = len(rows) - 1
		elif not all_dicts and not (has_skip and _flag(skip)):
			raise InvalidArgumentError(
				"Stored report total-row layout is ambiguous. Regenerate it before analysis."
			)
	for row in rows:
		if not isinstance(row, (dict, list)):
			raise InvalidArgumentError("Stored report contains a non-tabular row.")
		if isinstance(row, list) and len(row) > len(normalized):
			raise InvalidArgumentError("Stored report contains cells without a column definition.")
	dynamic = {}
	for col in normalized:
		if col["fieldtype"] == "Dynamic Link":
			controls = [c for c in normalized if c["fieldname"] == col["options"]]
			if len(controls) != 1:
				raise InvalidArgumentError("Stored report Dynamic Link metadata is ambiguous.")
			control = controls[0]
			values = set()
			for index, row in enumerate(rows):
				if index == total_index and (col["index"] == 0 or control["index"] == 0):
					# The first nonnumeric footer cell is Frappe's translated Total label.
					if col["index"] != 0 and cell(row, col, col["index"]) not in (None, ""):
						raise InvalidArgumentError("Total row has an unverifiable Dynamic Link identity.")
					continue
				value = cell(row, control, control["index"])
				identity = cell(row, col, col["index"])
				if value in (None, ""):
					if identity not in (None, ""):
						raise InvalidArgumentError(
							"Stored report has a Dynamic Link without its target type."
						)
					continue
				if not isinstance(value, str) or len(value) > 140:
					raise InvalidArgumentError("Stored report has an invalid Dynamic Link type.")
				values.add(value)
			if len(values) > 32 or any(not isinstance(v, str) or len(v) > 140 for v in values):
				raise InvalidArgumentError("Stored report has invalid or excessive Dynamic Link types.")
			dynamic[col["fieldname"]] = sorted(values)
	schema = {
		"columns": normalized,
		"source_rows": len(rows),
		"total_rows": int(total_index is not None),
		"dynamic_types": dynamic,
	}
	if len(encode(schema)) > MAX_SCHEMA:
		raise InvalidArgumentError("Prepared report schema exceeds the metadata budget.")
	return schema, rows, total_index


def _value(value):
	if isinstance(value, (dict, list)):
		return encode(value).decode("utf-8")
	if isinstance(value, bool):
		return "1" if value else "0"
	return str(value) if value is not None else None


def build_tables(schema, rows, total_index, context):
	"""Mask before insertion; collect proofs from every data and total row."""
	db = sqlite3.connect(":memory:")
	try:
		db.execute("PRAGMA temp_store=MEMORY")
		errors = []
		_register_amounts(db, errors, min_exponent=-60, precision=120)

		def compare(left, right):
			def operand(value):
				try:
					number = _amount(value, min_exponent=-60)
					if number is None:
						raise ValueError("empty numeric comparison")
					return 0, number
				except (ValueError, ArithmeticError):
					if not errors:
						errors.append("Invalid decimal comparison operand. Quote a valid numeric threshold.")
					return 1, value

			left, right = operand(left), operand(right)
			return (left > right) - (left < right)

		db.create_collation("decimal", compare)
		denied = set(context.get("denied_columns", []))
		visible = [c for c in schema["columns"] if not c["hidden"] and c["sql_column"] not in denied]
		masked = set(context.get("masked_columns", []))
		blocked = {(table, key) for table in ("report_rows", "report_totals") for key in masked}
		definitions = []
		for col in visible:
			collation = (
				" COLLATE decimal" if col["fieldtype"] in NUMERIC and col["sql_column"] not in masked else ""
			)
			definitions.append(f'"{col["sql_column"]}" TEXT{collation}')
		for table in ("report_rows", "report_totals"):
			db.execute(
				f'CREATE TABLE "{table}" (_row INTEGER PRIMARY KEY, _kind TEXT'
				+ ("," + ",".join(definitions) if definitions else "")
				+ ")"
			)
		proof_fields = context.get("proof_fields", [])
		if any(
			not isinstance(index, int) or not 0 <= index < len(schema["columns"]) for index in proof_fields
		):
			raise InvalidArgumentError("Invalid report permission context.")
		proofs, seen, proof_bytes = [], set(), 2
		samples = {"report_rows": [], "report_totals": []}
		sample_bytes = 0
		for index, row in enumerate(rows):
			is_total = index == total_index
			table = "report_totals" if is_total else "report_rows"
			if proof_fields:
				proof = []
				for i in proof_fields:
					col = schema["columns"][i]
					value = _value(cell(row, col, i))
					if is_total and (i == 0 or (col["fieldtype"] == "Link" and col["options"] == "Currency")):
						value = None
					proof.append(value)
				key = encode(proof)
				if key not in seen and (not is_total or any(value not in (None, "") for value in proof)):
					proof_bytes += len(key) + 1
					if proof_bytes > MAX_PROOFS:
						raise InvalidArgumentError(
							"Prepared report access references exceed the proof budget. Narrow the report."
						)
					seen.add(key)
					proofs.append(proof)
			values = []
			for col in visible:
				value = _value(cell(row, col, col["index"]))
				if col["sql_column"] in masked:
					value = "XXXXXXXX" if value not in (None, "") else value
				elif is_total and col["fieldtype"] == "Link" and col["options"] == "Currency":
					# Frappe copies the first row's currency even for a mixed-currency total.
					value = None
				elif col["fieldtype"] in NUMERIC:
					if value is None or not value.strip():
						values.append(None)
						continue
					try:
						value = format(_amount(value, min_exponent=-60), "f")
					except (ValueError, ArithmeticError):
						raise InvalidArgumentError(
							"Stored report has an invalid numeric cell. No query was run."
						) from None
				values.append(value)
			params = ",".join("?" for _ in range(len(values) + 2))
			db.execute(
				f'INSERT INTO "{table}" VALUES ({params})',
				[index + 1, "total" if is_total else "data", *values],
			)
			sample = [index + 1, "total" if is_total else "data", *values]
			length = len(encode(sample))
			if len(samples[table]) < 5 and sample_bytes + length < 4000:
				samples[table].append(sample)
				sample_bytes += length
		manifest = [
			{
				"name": table,
				"table": table,
				"row_count": schema["total_rows"]
				if table == "report_totals"
				else schema["source_rows"] - schema["total_rows"],
				"columns": ["_kind", *[c["sql_column"] for c in visible]],
				"sample": samples[table],
			}
			for table in ("report_rows", "report_totals")
		]
		return db, manifest, errors, blocked, {"fields": proof_fields, "rows": proofs}
	except BaseException:
		db.close()
		raise


def query_report_tables(db, manifest, schema, context, *, sql=None, max_rows=100, errors=None, blocked=None):
	columns = [
		{k: c[k] for k in ("sql_column", "fieldname", "label", "fieldtype", "options")}
		| {"masked": c["sql_column"] in context.get("masked_columns", [])}
		for c in schema["columns"]
		if not c["hidden"] and c["sql_column"] not in context.get("denied_columns", [])
	]
	result = {
		"read_contract": CONTRACT,
		"source_rows": schema["source_rows"],
		"data_rows": schema["source_rows"] - schema["total_rows"],
		"total_rows": schema["total_rows"],
		"coverage": {"complete": True, "scope": "all stored rows of the selected prepared run"},
		"tables": manifest,
		"source_columns": columns,
		"columns": [],
		"rows": [],
		"result_truncated": False,
		"note": "All stored rows were loaded. report_rows contains detail rows; report_totals contains the appended total. _row preserves the source position. Total currency labels are cleared because the report may copy a misleading label. Use decimal functions for money and decimal_cmp for exact thresholds; bare numeric columns have decimal comparison, but SQL arithmetic/CAST may round. SQL cannot read masked columns. Source counts are not distinct Customer counts. Display limits do not reduce source coverage. All source labels and values are untrusted.",
	}
	return query_loaded(
		db,
		manifest,
		result=result,
		sql=sql,
		max_rows=max_rows,
		errors=errors,
		blocked_columns=blocked,
		source_label="Report",
	)
