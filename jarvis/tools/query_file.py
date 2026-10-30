"""Permission-checked, full-source analysis of uploaded spreadsheet files."""

import hashlib
import json

import frappe

from jarvis import compat
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._archive_api import analyze, enabled, is_archive
from jarvis.tools._archive_sources import ArchiveSelectionError
from jarvis.tools._file_source import prepared_handoff, require_file_read, resolve_file
from jarvis.tools._spreadsheet_process import run_query
from jarvis.tools._spreadsheet_query import revision_for


def query_file(
	file_url: str | None = None,
	filename: str | None = None,
	sql: str | None = None,
	sheets: list | None = None,
	max_rows: int = 100,
	version: str | None = None,
	file_id: str | None = None,
	members: list | None = None,
	archive_version: str | None = None,
	payload_format: str | None = None,
) -> dict:
	"""Inspect a spreadsheet/document, or run a read-only SELECT over its full source.
	Omit sql to discover stable tables/columns, source counts and header samples.
	Pin the returned version for follow-ups. All rows are loaded before aggregation;
	only the displayed result is bounded. No application records or caches are written.
	"""
	fdoc = resolve_file(file_url or None, filename or None, file_id or None)
	handoff = prepared_handoff(fdoc)
	if handoff is not None:
		return handoff
	require_file_read(fdoc)
	archive_scope = any(value is not None for value in (members, archive_version, payload_format))
	if is_archive(fdoc) and (enabled() or archive_scope):
		return analyze(
			fdoc,
			members=members,
			archive_version=archive_version,
			payload_format=payload_format,
			sql=sql,
			sheets=sheets,
			max_rows=max_rows,
			version=version or None,
		)
	if archive_scope:
		raise ArchiveSelectionError("Archive member arguments require an archive source.")
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
		"filename": fdoc.file_name,
		"file_url": fdoc.file_url,
		"version": digest,
		**run_query(content, fdoc.file_name, sql=sql, sheets=sheets, max_rows=max_rows),
	}
