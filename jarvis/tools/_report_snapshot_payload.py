"""What a report snapshot stores: the skip rules and the gzip payload (plan B.1). No Redis.

`create` runs in the web request that just produced the result, so every cap that can be
decided from counts or from the size `api` already measured is decided BEFORE anything is
serialised, and the gzip stops as soon as its output passes its cap."""

import json
import zlib

from frappe.utils.response import json_handler

from jarvis.tools import _private_output
from jarvis.tools._report_query_data import (
	MAX_CELLS,
	MAX_COLUMNS,
	MAX_EXPANDED,
	MAX_TOTAL_ROWS,
	validate_report,
)

MAX_ROWS = MAX_TOTAL_ROWS  # the limits of the query engine that reads the snapshot back
MAX_GZIP = 2 * 1024 * 1024
MAX_FILTER_BYTES = 2500
GZIP_CHUNK = 256 * 1024
GZIP_WBITS = 16 + zlib.MAX_WBITS  # a gzip container, as the Prepared Report file format


def skip_reason(data, measured=None):
	"""Why a result is not stored, or None. `disabled` is decided by the caller. Every check
	here reads counts, flags or `measured` (the size `api` measured), never the cells."""
	if not isinstance(data, dict):
		return "undecodable"
	if data.get("prepared_report"):
		return "prepared"
	rows = data.get("result")
	if not isinstance(rows, list):
		return "no_rows"
	if any(isinstance(row, dict) and "indent" in row for row in rows):
		return "tree"
	if _private_output.metadata_redacted():
		return "private"
	columns = data.get("columns")
	if not isinstance(columns, list):
		return "undecodable"
	if len(rows) > MAX_ROWS or len(columns) > MAX_COLUMNS or len(rows) * len(columns) > MAX_CELLS:
		return "caps"
	if measured is not None and measured > MAX_EXPANDED:
		return "caps"  # the serialised result is larger than the expanded cap
	return None


def build(data, filters):
	"""`(json_bytes, gzip_bytes)` or a skip reason."""
	if len(json.dumps(filters, default=json_handler).encode()) > MAX_FILTER_BYTES:
		return "caps"
	try:
		# The structural checks the query engine makes on read (`decode_report`), on the
		# parsed result: no gzip and JSON round trip in the web request.
		validate_report(
			data["columns"],
			data["result"],
			skip=data.get("skip_total_row"),
			has_skip=True,
		)
	except Exception:
		return "undecodable"
	json_bytes = json.dumps(
		{"columns": data["columns"], "result": data["result"], "skip_total_row": data.get("skip_total_row")},
		default=json_handler,
		sort_keys=True,
	).encode()
	if len(json_bytes) > MAX_EXPANDED:
		return "caps"
	content = _gzip(json_bytes)
	if content is None:
		return "caps"
	return json_bytes, content


def _gzip(json_bytes):
	"""The gzip of `json_bytes` (deterministic: mtime 0), or None as soon as its output
	passes `MAX_GZIP`; the rest of the input is then not compressed."""
	packer = zlib.compressobj(6, zlib.DEFLATED, GZIP_WBITS)
	parts, size = [], 0
	for start in range(0, len(json_bytes), GZIP_CHUNK):
		part = packer.compress(json_bytes[start : start + GZIP_CHUNK])
		parts.append(part)
		size += len(part)
		if size > MAX_GZIP:
			return None
	parts.append(packer.flush())
	content = b"".join(parts)
	return content if len(content) <= MAX_GZIP else None
