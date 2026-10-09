"""Versioned cursor windows; each call uses current authorized File bytes."""

import hashlib
import json
import re
import time

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._file_scan import parse_cursor, scanner_profile
from jarvis.tools._file_scan_process import scan_file

CONTRACT = "file-cursor-v1"


def read_sections(fdoc, content, *, sheet=None, cursor=None, version=None):
	if not frappe.conf.get("jarvis_file_sections_enabled", True):
		raise InvalidArgumentError(
			"Exact file reading is disabled on this site. Request a bounded preview instead."
		)
	parse_cursor(cursor)
	if (cursor is None) != (version is None):
		raise InvalidArgumentError("Cursor and version must be supplied together.")
	digest = hashlib.sha256(
		json.dumps(
			[CONTRACT, scanner_profile(), fdoc.name, fdoc.file_name, sheet], ensure_ascii=False
		).encode()
		+ b"\0"
		+ content
	).hexdigest()
	if version is not None and (
		not isinstance(version, str) or not re.fullmatch("[a-f0-9]{64}", version) or version != digest
	):
		raise InvalidArgumentError("File, worksheet scope or reader changed. Restart without cursor/version.")
	started = time.monotonic()
	try:
		scan = scan_file(content, fdoc.file_name, sheet, cursor)
	except InvalidArgumentError:
		frappe.logger("jarvis.file_read").warning(
			"status=failed reason=reader_rejected cursor=%s version=%s", cursor or "0:1:0", digest[:12]
		)
		raise
	except Exception:
		frappe.logger("jarvis.file_read").warning("status=failed reason=invalid_format")
		raise InvalidArgumentError(
			"File format could not be parsed. Re-save it as an unlocked PDF, XLSX or UTF-8 text/CSV."
		) from None
	next_cursor = scan.pop("next_cursor")
	next_read = None
	if next_cursor is not None:
		next_read = {"file_url": fdoc.file_url, "cursor": next_cursor, "version": digest}
		if sheet is not None:
			next_read["sheet"] = sheet
	window_complete = not scan["issues"] and scan["stop_reason"] is None
	complete = cursor is None and next_cursor is None and window_complete
	status = "partial" if not window_complete else "complete" if complete else "window"
	frappe.logger("jarvis.file_read").info(
		"status=%s elapsed_ms=%d first=%d last=%d cursor=%s version=%s",
		status,
		int((time.monotonic() - started) * 1000),
		scan["range"]["first"],
		scan["range"]["last"],
		cursor or "0:1:0",
		digest[:12],
	)
	issues = scan.pop("issues")
	return {
		**scan,
		"read_contract": CONTRACT,
		"status": status,
		"version": digest,
		"cursor": cursor or "0:1:0",
		"end_of_file": next_cursor is None,
		"coverage": {"complete": complete, "window_complete": window_complete, "issues": issues},
		"next_read": next_read,
		"note": (
			"This is one exact extraction window, not computed business totals. Follow next_read only within the task budget. "
			"Stop after at most 20 windows or 100,000 returned characters and disclose unread ranges. "
			"Retain all coverage issues and stop_reason; a final window does not prove prior windows were read. "
			"Fragments of the same unit must be joined by offset before decoding json-row."
		),
	}
