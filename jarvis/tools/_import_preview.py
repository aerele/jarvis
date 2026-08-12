"""Read-only preview for a Frappe Data Import, computed on a TRANSIENT (un-inserted)
``Data Import`` document so nothing is persisted.

Shared by the ``preview_import`` tool and the ``run_import`` confirmation card, so the
card renders exactly what the tool showed. This module owns the whole safety core:

  - the FILE read is gated as the CALLER (Frappe's importer resolves a File by URL with
    no permission check - a private-file leak); we reuse ``read_file._resolve_file``,
    which enforces ``File`` ``has_permission("read")`` per candidate.
  - the import gate is REUSED, not re-implemented: ``DataImport.validate_doctype()`` runs
    the real BLOCKED_DOCTYPES / ``allow_import`` / ``has_permission(..., "import")`` checks
    on the transient doc (no hand-rolled copy that could drift from Frappe).
  - file size is bounded before the parse; the row count is bounded from the parse result.
  - the transient preview calls the ``ImportFile``-level ``get_data_for_import_preview()``
    (NOT the ``Importer`` wrapper that queries ``Data Import Log`` by ``name=None``).

It never inserts a ``Data Import`` row. It is read-only.
"""

from __future__ import annotations

import json

import frappe

from jarvis.chat._record_summary import fmt, is_secret
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.read_file import _resolve_file

_TABLE_FIELDTYPES = ("Table", "Table MultiSelect")

# Exact import-type literals Frappe's importer branches on. Anything else silently
# becomes upsert semantics in Frappe (importer.process_doc), so we validate up front.
IMPORT_TYPES = (
	"Insert New Records",
	"Update Existing Records",
	"Insert or Update Records",
)

# Frappe's own sentinel for a column the user chose not to import.
DONT_IMPORT = "Don't Import"

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_DEFAULT_MAX_ROWS = 10_000


def _max_bytes() -> int:
	return int(frappe.conf.get("jarvis_import_max_bytes") or _DEFAULT_MAX_BYTES)


def _max_rows() -> int:
	return int(frappe.conf.get("jarvis_import_max_rows") or _DEFAULT_MAX_ROWS)


def build_import_preview(
	*,
	doctype: str,
	file_url: str | None = None,
	filename: str | None = None,
	import_type: str = "Insert New Records",
	mapping: dict | None = None,
) -> dict:
	"""Compute a read-only import preview on a transient Data Import doc.

	Returns ``{doctype, import_type, file, total_rows, total_records, columns, sample,
	warnings, importable, resolved_file_url, note}``. Raises ``InvalidArgumentError`` for
	a bad doctype/import_type/file or an oversized file, and ``frappe.PermissionError``
	for a caller who lacks import permission on the target doctype.

	NOTHING is persisted: no ``Data Import`` row is created.
	"""
	if not doctype or not isinstance(doctype, str):
		raise InvalidArgumentError("doctype is required")
	if import_type not in IMPORT_TYPES:
		raise InvalidArgumentError(f"import_type must be one of {IMPORT_TYPES}; got {import_type!r}")
	if mapping is not None and not isinstance(mapping, dict):
		raise InvalidArgumentError("mapping must be an object of {column: field} entries")

	# 1. Resolve + permission-gate the File AS THE CALLER. _resolve_file's file_url branch
	#    does NOT permission-check (only its filename branch does, per-candidate), so gate
	#    the read here - mirrors read_file.py:60 - else a raw file_url discloses a private
	#    file the caller can't read. Generic message: never confirm a private file's
	#    existence to a caller who cannot see it.
	file_doc = _resolve_file(file_url, filename)
	if not frappe.has_permission("File", "read", doc=file_doc.name):
		raise InvalidArgumentError("no matching file found")
	resolved_url = file_doc.file_url

	# 2. Byte bound BEFORE any parse (cheap metadata read; the importer would otherwise
	#    fully parse even a huge file just to show 10 rows). Fall back to the content
	#    length when file_size is missing so an oversized file is still bounded pre-parse.
	size = file_doc.file_size or len(file_doc.get_content() or b"")
	if size and size > _max_bytes():
		raise InvalidArgumentError(
			f"file is {size} bytes; the import limit is {_max_bytes()} bytes - "
			"use the Desk importer for larger files"
		)

	# 3. Transient (un-inserted) Data Import doc.
	doc = frappe.new_doc("Data Import")
	doc.reference_doctype = doctype
	doc.import_type = import_type
	doc.import_file = resolved_url

	# 4. Reuse Frappe's real gate. blocked-doctype / allow_import -> ValidationError
	#    (a doctype-level fact -> importable:false); missing import perm -> PermissionError
	#    (a caller fault -> propagate).
	try:
		doc.validate_doctype()
	except frappe.PermissionError:
		raise
	except frappe.ValidationError as e:
		return {
			"doctype": doctype,
			"import_type": import_type,
			"file": {"name": file_doc.file_name, "url": resolved_url},
			"importable": False,
			"reason": _clean(e),
			"resolved_file_url": resolved_url,
		}

	# 5. Apply a column mapping override (resolve header->index against the parsed columns).
	resolved_template_options = None
	index_map: dict = {}
	if mapping:
		index_map = _resolve_mapping(mapping, doc)
		doc.template_options = json.dumps({"column_to_field_map": index_map})
		resolved_template_options = doc.template_options

	# 6. Parse + preview via the ImportFile-level call (transient-safe). Any parse throw
	#    (empty / non-UTF8 / non-table / malformed) becomes a clean tool error.
	doc.set_delimiters_flag()
	try:
		importer = doc.get_importer()
		import_file = importer.import_file
		preview = import_file.get_data_for_import_preview()
	except InvalidArgumentError:
		raise
	except Exception as e:
		raise InvalidArgumentError(_clean(e)) from e

	# v16 only sets total_number_of_rows when the file exceeds the 10-row preview cap; for a
	# small file it is absent and the (untruncated) data holds every row. v17 always sets it.
	total_rows = preview.total_number_of_rows or len(preview.data or [])
	if total_rows > _max_rows():
		raise InvalidArgumentError(
			f"file has {total_rows} rows; the import limit is {_max_rows()} rows - "
			"use the Desk importer for larger files"
		)

	columns = _columns_view(preview, doctype)

	# Reject a mapping whose TARGET field did not resolve: Frappe would silently drop a
	# mis-typed target to an `info` warning and import the column as if unmapped.
	for idx_str, target in index_map.items():
		if target == DONT_IMPORT:
			continue
		col = next((c for c in columns if c["index"] == int(idx_str)), None)
		if col is None or not col["mapped"]:
			raise InvalidArgumentError(f"mapping target {target!r} did not match any field on {doctype}")

	sample = _sample_view(preview, columns, doctype)
	total_records = _count_records(importer)
	warnings = _classify_warnings(preview, columns, doctype, import_type, total_rows, total_records)

	return {
		"doctype": doctype,
		"import_type": import_type,
		"file": {"name": file_doc.file_name, "url": resolved_url},
		"total_rows": total_rows,
		"total_records": total_records,
		"submittable": bool(getattr(frappe.get_meta(doctype), "is_submittable", 0)),
		"columns": columns,
		"sample": sample,
		"warnings": warnings,
		"importable": True,
		"can_run": frappe.has_permission("Data Import", "create"),
		"resolved_file_url": resolved_url,
		"template_options": resolved_template_options,
		"note": ("read-only preview - nothing was created. Use run_import to import."),
	}


def _classify_warnings(preview, columns, doctype, import_type, total_rows, total_records) -> dict:
	"""Split warnings into ``blocking`` (import would create nothing / fail every row) and
	``advisory`` (allowed, but the user should know). Blocking = Frappe's non-``info``
	warnings (invalid Link/Select values, bad dates, malformed rows) PLUS the app-level
	guards (near-zero mapping, stricter parent-scoped mandatory, Update/Upsert without an
	ID column).
	"""
	blocking: list[dict] = []

	# Frappe-derived blocking warnings, classified version-agnostically: a warning that is
	# not "info" blocks the import. This is EXACTLY Frappe v16's own import_data rule
	# (importer.py: `[w for w in warnings if w.get("type") != "info"]`); on v17 it is
	# slightly more conservative than value_mapping.get_blocking_warnings, which is correct
	# for a pre-confirmation refusal (an invalid value should block until fixed). Crucially
	# it avoids the v17-ONLY `frappe...data_import.value_mapping` import, which does not
	# exist on v16 (CI/prod) and would crash the whole tool layer at registry load.
	for w in preview.warnings or []:
		if isinstance(w, dict) and w.get("type") != "info":
			blocking.append(
				{
					"type": "invalid_value",
					"column": _col_header(columns, w.get("col")),
					"message": _clean_msg(w.get("message")),
				}
			)

	mapped = [c for c in columns if c["mapped"]]

	# Guard: near-zero mapping - the file doesn't look like this doctype.
	if not mapped:
		blocking.append(
			{
				"type": "no_mapping",
				"message": (
					f"No columns in the file matched any field on {doctype} - "
					f"this file does not look like a {doctype} import."
				),
			}
		)

	# Guard: stricter parent-scoped mandatory (empirical default check).
	blocking += _mandatory_blocks(doctype, columns)

	# Guard: Update / Upsert needs an ID column, else it silently bulk-inserts duplicates.
	if import_type in ("Update Existing Records", "Insert or Update Records"):
		if not _has_id_column(columns, doctype):
			blocking.append(
				{
					"type": "missing_id",
					"message": (
						f"{import_type} needs a column mapped to the record ID (name), "
						"otherwise every row would be inserted as a new record."
					),
				}
			)

	# Advisory: unrecognised columns are ignored (never blocks - a sparse file is legit).
	unmapped = [c["header"] for c in columns if not c["mapped"]]
	advisory: list[dict] = []
	if unmapped:
		advisory.append(
			{
				"type": "unmapped_columns",
				"columns": unmapped,
				"message": (
					f"{len(unmapped)} of {len(columns)} columns were not recognised "
					f"and will be ignored: {', '.join(unmapped)}"
				),
			}
		)

	# Advisory: a flat parent-repeated file FANS OUT into one record per row.
	if any(c["mapped"] and c["child"] for c in columns) and total_records == total_rows and total_rows > 1:
		advisory.append(
			{
				"type": "fan_out",
				"message": (
					f"This creates {total_records} separate {doctype} records, one per row. "
					"If you meant one record with many child rows, the file must be grouped "
					"first (blank the parent columns on continuation rows)."
				),
			}
		)

	return {"blocking": blocking, "advisory": advisory}


def _mandatory_blocks(doctype: str, columns: list[dict]) -> list[dict]:
	"""Block if a mandatory PARENT field Frappe cannot auto-fill is left unmapped, or a
	mandatory Table field has no mapped child columns. Child-ROW mandatory fields are NOT
	pre-checked (Frappe enforces them per-payload at insert; pre-checking false-positives
	on child defaults/fetch-from).

	"Auto-fillable" is measured EMPIRICALLY: a fresh ``new_doc`` already carries a value
	(default / naming series / hook), or the field is read-only / fetched from a link.
	"""
	blocks: list[dict] = []
	meta = frappe.get_meta(doctype)
	fresh = frappe.new_doc(doctype)
	mapped_parent = {c["field"] for c in columns if c["mapped"] and not c["child"]}
	mapped_child_doctypes = {c["child_doctype"] for c in columns if c["mapped"] and c["child"]}

	for df in meta.fields:
		if not df.reqd:
			continue
		if df.fieldtype in _TABLE_FIELDTYPES:
			# A mandatory child table with no mapped child columns -> every parent insert
			# fails on the empty mandatory table.
			if df.options not in mapped_child_doctypes:
				blocks.append(
					{
						"type": "missing_mandatory",
						"field": df.fieldname,
						"message": f"Required table '{df.label}' has no columns mapped to it.",
					}
				)
			continue
		if df.fieldname in mapped_parent:
			continue
		if df.read_only or df.fetch_from or df.default:
			continue
		if fresh.get(df.fieldname):
			continue  # auto-filled by a default / naming series / hook
		blocks.append(
			{
				"type": "missing_mandatory",
				"field": df.fieldname,
				"message": f"Required field '{df.label}' isn't mapped to any column.",
			}
		)
	return blocks


def _has_id_column(columns: list[dict], doctype: str) -> bool:
	"""True if a mapped column feeds the record ID (the ``name`` field or the autoname
	``field:`` target)."""
	id_fields = {"name"}
	autoname = frappe.get_meta(doctype).autoname or ""
	if autoname.startswith("field:"):
		id_fields.add(autoname.split(":", 1)[1])
	return any(c["mapped"] and c["field"] in id_fields for c in columns)


def _col_header(columns: list[dict], col_number) -> str | None:
	# Frappe warnings carry a 1-based ``col``; our columns view is 0-based.
	try:
		idx = int(col_number) - 1
	except (TypeError, ValueError):
		return None
	for c in columns:
		if c["index"] == idx:
			return c["header"]
	return None


def _clean_msg(msg) -> str:
	from frappe.utils import strip_html_tags

	try:
		return strip_html_tags(str(msg)) if msg else ""
	except Exception:
		return str(msg) if msg else ""


def _resolve_mapping(mapping: dict, doc) -> dict:
	"""Turn a user ``{header|index: fieldname|"Don't Import"}`` map into Frappe's
	``column_to_field_map`` (0-based stringified column index -> header key).

	A key that matches no column raises rather than being silently ignored (Frappe would
	drop a mis-keyed override to a harmless ``info`` warning).
	"""
	# Parse once without the override to learn the file's header order.
	doc.set_delimiters_flag()
	base_importer = doc.get_importer()
	headers = [c.header_title for c in base_importer.import_file.header.columns]
	by_header = {h: i for i, h in enumerate(headers)}

	index_map: dict[str, str] = {}
	for key, value in mapping.items():
		if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
			idx = int(key)
			if idx < 0 or idx >= len(headers):
				raise InvalidArgumentError(
					f"mapping column index {idx} is out of range (file has {len(headers)} columns)"
				)
		elif key in by_header:
			idx = by_header[key]
		else:
			raise InvalidArgumentError(f"mapping key {key!r} matches no column header in the file")
		index_map[str(idx)] = value
	return index_map


def _columns_view(preview, reference_doctype: str) -> list[dict]:
	"""One row per real file column (the synthetic 'Sr. No' at index 0 is dropped).

	A child-table column is identified by ``df.parent != reference_doctype`` (its df.parent
	is the CHILD doctype). ``mapped`` is False for a column Frappe could not match.
	"""
	out = []
	# preview.columns[0] is the synthetic Sr. No; real columns follow, 0-based from here.
	for idx, col in enumerate(preview.columns[1:]):
		df = col.get("df")
		mapped = bool(df) and not col.get("skip_import")
		child = bool(df) and df.get("parent") and df.get("parent") != reference_doctype
		out.append(
			{
				"index": idx,
				"header": col.get("header_title"),
				"field": df.get("fieldname") if df else None,
				"label": df.get("label") if df else None,
				"fieldtype": df.get("fieldtype") if df else None,
				"reqd": bool(df.get("reqd")) if df else False,
				"mapped": mapped,
				"child": bool(child),
				"child_doctype": df.get("parent") if child else None,
			}
		)
	return out


def _sample_view(preview, columns: list[dict], reference_doctype: str) -> dict:
	"""Up to 10 sample rows, with secret-mapped columns masked to ``[hidden]``.

	Cell j aligns with ``columns[j]`` (both drop the synthetic Sr. No column).
	"""
	# Which columns map to a secret field -> mask those cells.
	meta = frappe.get_meta(reference_doctype)
	secret_idx = set()
	for c in columns:
		field = c.get("field")
		if not field:
			continue
		# For a parent field, check against the parent meta; a secret-NAMED field is
		# caught regardless of meta.
		child_meta = frappe.get_meta(c["child_doctype"]) if c.get("child") else meta
		if is_secret(child_meta, field):
			secret_idx.add(c["index"])

	rows = []
	for data_row in preview.data:
		cells = data_row[1:]  # drop the leading row_number
		masked = ["[hidden]" if i in secret_idx else fmt(v) for i, v in enumerate(cells)]
		rows.append({"cells": masked})
	return {"columns": [c["header"] for c in columns], "rows": rows}


def _count_records(importer) -> int:
	"""Number of parent DOCUMENTS the file produces (= payload_count), which differs
	from the sheet-row count exactly when child rows are grouped under a parent.
	"""
	try:
		return len(importer.import_file.get_payloads_for_import())
	except Exception:
		return 0


def _clean(e: Exception) -> str:
	from frappe.utils import strip_html_tags

	msg = str(e) or type(e).__name__
	try:
		return strip_html_tags(msg)
	except Exception:
		return msg
