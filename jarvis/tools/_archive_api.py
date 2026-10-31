"""Archive feature gate, source versions and bounded tool result contracts."""

import hashlib
import json
import re

import frappe

from jarvis.exceptions import FeatureDisabledError, InvalidArgumentError
from jarvis.tools._archive_process import run_archive
from jarvis.tools._archive_sources import (
	REVISION,
	ArchiveModeError,
	ArchiveSelectionError,
	ArchiveSourceChangedError,
)
from jarvis.tools._file_source import read_bounded
from jarvis.tools._report_query_process import Deadline


def is_archive(doc):
	return (doc.file_name or "").lower().endswith((".zip", ".gz"))


def enabled():
	return bool(frappe.conf.get("jarvis_archive_reading_enabled", False))


def _hash(value):
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
	).hexdigest()


def _token(value):
	return isinstance(value, str) and re.fullmatch("[a-f0-9]{64}", value)


def _source(doc, archive_version, deadline):
	if not enabled():
		raise FeatureDisabledError(
			"Archive reading is disabled on this site. Do not retry through another reader."
		)
	content = read_bounded(doc)
	deadline.check()
	token = _hash(
		[
			REVISION,
			frappe.local.site,
			frappe.session.user,
			doc.name,
			doc.file_name,
			hashlib.sha256(content).hexdigest(),
		]
	)
	if archive_version is not None and (not _token(archive_version) or archive_version != token):
		raise ArchiveSourceChangedError(
			"The archive or access context changed. Inspect this exact attachment again."
		)
	return content, token


def _members(value):
	if (
		not isinstance(value, list)
		or not 1 <= len(value) <= 32
		or any(not isinstance(v, str) or not re.fullmatch(r"m[1-9][0-9]?", v) for v in value)
		or len(set(value)) != len(value)
	):
		raise ArchiveSelectionError("Select unique member IDs from the archive inventory.")
	return sorted(value, key=lambda v: int(v[1:]))


def analyze(
	doc,
	*,
	members=None,
	archive_version=None,
	payload_format=None,
	sql=None,
	sheets=None,
	max_rows=100,
	version=None,
	member=None,
	sheet=None,
	cursor=None,
	read=False,
	read_mode=None,
	deadline=None,
):
	deadline = deadline or Deadline()
	if read and read_mode != "file-cursor-v1":
		raise ArchiveModeError(
			"Archive reading requires exact member mode. Preview cannot read an archive member."
		)
	if read and not frappe.conf.get("jarvis_file_sections_enabled", True):
		raise FeatureDisabledError("Exact file sections are disabled. No archive content was read.")
	if sql is not None and (not isinstance(sql, str) or not sql.strip() or len(sql) > 12000):
		raise InvalidArgumentError("Use one read-only SELECT of at most 12,000 characters.")
	if type(max_rows) is not int or not 1 <= max_rows <= 200:
		raise InvalidArgumentError("max_rows must be an integer from 1 to 200.")
	if sheets is not None and (
		not isinstance(sheets, list)
		or not sheets
		or len(sheets) > 32
		or any(not isinstance(s, str) or not s or len(s) > 128 for s in sheets)
		or len(set(sheets)) != len(sheets)
	):
		raise ArchiveSelectionError("Use a nonempty list of unique worksheet names.")
	if payload_format is not None and (
		not isinstance(payload_format, str)
		or payload_format not in {"xlsx", "csv", "tsv", "json", "md", "markdown", "txt"}
	):
		raise ArchiveSelectionError("Choose a supported GZ payload format.")
	content, current = _source(doc, archive_version, deadline)
	identity = {
		"file_id": doc.name,
		"file_url": doc.file_url,
		"filename": doc.file_name,
		"archive_version": current,
	}
	selection = [member] if read and member is not None else members
	if selection is None:
		if (
			sql is not None
			or version is not None
			or cursor is not None
			or sheets is not None
			or sheet is not None
		):
			raise ArchiveSelectionError(
				"Inspect the archive inventory and select members before reading content."
			)
		return {
			**run_archive(
				content, doc.file_name, deadline=deadline, operation="manifest", payload_format=payload_format
			),
			**identity,
		}
	selected = _members(selection)
	if archive_version is None:
		raise ArchiveSourceChangedError("Pin the archive_version from inventory before selecting members.")
	view = _hash(
		[
			current,
			"read" if read else "query",
			selected,
			payload_format,
			sheet if read else sorted(sheets) if sheets else sheets,
			REVISION,
		]
	)
	if version is not None and (not _token(version) or version != view):
		raise ArchiveSourceChangedError(
			"The selected archive view changed. Inspect the same member scope again."
		)
	if sql is not None and version is None:
		raise ArchiveSourceChangedError("Inspect the selected view first and pin its version before SQL.")
	if read:
		if (cursor is None) != (version is None):
			raise ArchiveSourceChangedError("Cursor and member-view version must be supplied together.")
		scan = run_archive(
			content,
			doc.file_name,
			deadline=deadline,
			operation="read",
			member=member,
			sheet=sheet,
			cursor=cursor,
			payload_format=payload_format,
		)
		member_count = scan.pop("archive_member_count")
		next_cursor = scan.pop("next_cursor")
		issues = scan.pop("issues")
		window_complete = not issues and scan["stop_reason"] is None
		complete = cursor is None and next_cursor is None and window_complete
		next_read = None
		if next_cursor is not None:
			next_read = {
				"file_id": doc.name,
				"file_url": doc.file_url,
				"member": member,
				"archive_version": current,
				"version": view,
				"cursor": next_cursor,
			}
			if sheet is not None:
				next_read["sheet"] = sheet
			if payload_format is not None:
				next_read["payload_format"] = payload_format
		return {
			**scan,
			**identity,
			"read_contract": "archive-cursor-v1",
			"member": member,
			"unread_member_count": member_count - 1,
			"archive_complete": complete and member_count == 1 and sheet is None,
			"worksheet_scope": [sheet] if sheet is not None else None,
			"version": view,
			"cursor": cursor or "0:1:0",
			"status": "partial" if not window_complete else "complete" if complete else "window",
			"end_of_file": next_cursor is None,
			"coverage": {"complete": complete, "window_complete": window_complete, "issues": issues},
			"next_read": next_read,
			"note": "Exact window of one archive member. Preserve archive and member versions. Follow next_read within the task budget; the final window alone does not prove prior windows or other members were read.",
		}
	result = run_archive(
		content,
		doc.file_name,
		deadline=deadline,
		operation="query",
		members=selected,
		sheets=sheets,
		sql=sql,
		max_rows=max_rows,
		payload_format=payload_format,
	)
	return {**result, **identity, "version": view}
