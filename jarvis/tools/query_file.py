"""Permission-checked, full-source analysis of uploaded spreadsheet files."""

import hashlib
import json

import frappe

from jarvis import compat
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._spreadsheet_process import run_query
from jarvis.tools._spreadsheet_query import revision_for
from jarvis.tools.read_file import _resolve_file


def query_file(
	file_url: str | None = None,
	filename: str | None = None,
	sql: str | None = None,
	sheets: list | None = None,
	max_rows: int = 100,
	version: str | None = None,
) -> dict:
	"""Inspect a spreadsheet/document, or run a read-only SELECT over its full source.
	Omit sql to discover stable tables/columns, source counts and header samples.
	Pin the returned version for follow-ups. All rows are loaded before aggregation;
	only the displayed result is bounded. No application records or caches are written.
	"""
	fdoc = _resolve_file(file_url or None, filename or None)
	if not frappe.has_permission("File", "read", doc=fdoc.name):
		raise PermissionDeniedError("no matching file found")
	content = compat.file_bytes(fdoc)
	# Canonical scope makes reordered selections equivalent and pins identity too.
	if sheets is not None and (
		not isinstance(sheets, list) or any(not isinstance(name, str) for name in sheets)
	):
		raise InvalidArgumentError("sheets must be a list of worksheet names or null.")
	digest = hashlib.sha256(
		json.dumps(
			[revision_for(fdoc.file_name), fdoc.name, fdoc.file_name, sorted(sheets) if sheets else sheets]
		).encode()
		+ b"\0"
		+ content
	).hexdigest()
	if version not in (None, "") and version != digest:
		raise InvalidArgumentError("File or worksheet scope changed. Inspect the file again without version.")
	return {
		"file_id": fdoc.name,
		"filename": fdoc.file_name,
		"file_url": fdoc.file_url,
		"version": digest,
		**run_query(content, fdoc.file_name, sql=sql, sheets=sheets, max_rows=max_rows),
	}
