"""Generate a real .xlsx workbook from tabular data and stash it as a private
File so the chat can hand the customer a download.

The agent gathers rows (typically via get_list / run_report) and passes them
here; we build the workbook through ``jarvis.compat.xlsx_bytes`` and save the
bytes as a private File. That shim exists because ``frappe.utils.xlsxutils``
was rewritten from openpyxl to xlsxwriter in Frappe 16, so the engine behind
Frappe's own report export differs per major. The chat surface then renders a
download card from the returned ``file_url``.

Two shapes:
  * single sheet — pass ``rows`` (+ optional ``title`` / ``columns``);
  * multi-tab workbook — pass ``sheets`` = a list of
    ``{"title", "rows", "columns"?}``; ``title`` then names the file. This is
    the only way to hand the user a real multi-tab workbook as a download —
    building one by hand via ``exec`` leaves the file stranded in the container.
"""

import datetime
import re

import frappe

from jarvis import compat
from jarvis._xlsx_charts import validate_charts
from jarvis.exceptions import InvalidArgumentError, NoDataError

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# Dates reach this tool as ISO text (get_list/run_report rows go through JSON),
# which Excel stores as text: no date sorting, no date filters. Only these two
# strict shapes are read as dates, never looser ones: ERPNext names like
# "2026-00012", codes like "00123" and phone numbers must stay text.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?$")
_INVALID_SHEET_NAME_RE = re.compile(r"[\[\]:*?/\\]")


def export_excel(
	rows: list | None = None,
	title: str | None = None,
	columns: list | None = None,
	sheets: list | None = None,
	charts: list | None = None,
) -> dict:
	"""Build an .xlsx and return ``{file_url, filename, title, mime_type,
	size_bytes, name}``.

	Single sheet: ``rows`` is a list of dicts (columns = first row's keys, or
	the given ``columns``) or a list of lists (first row is the header unless
	``columns`` is given); ``title`` names the sheet + file.

	Multi-tab: pass ``sheets`` = ``[{"title": str, "rows": [...], "columns":
	[...]?}, ...]`` — one tab per entry (empty tabs are skipped). ``title``
	names the workbook file. When ``sheets`` is given, ``rows`` is ignored.

	``charts`` adds native editable column/bar/line/pie charts. Each specifies
	``type``, ``categories`` (header name), ``values`` (numeric header names),
	and optional ``title``. With ``sheets``, put charts inside each sheet.
	"""
	sheet_charts = []
	if sheets is not None:
		if charts is not None:
			raise InvalidArgumentError("Put charts inside each sheet when using sheets")
		if not isinstance(sheets, list) or not sheets:
			raise InvalidArgumentError("sheets must be a non-empty list of {title, rows}.")
		sheet_data: list[tuple[str, list]] = []
		used: set[str] = set()
		for i, spec in enumerate(sheets):
			if not isinstance(spec, dict):
				raise InvalidArgumentError(
					"each sheet must be an object with 'rows' (+ optional 'title', 'columns')."
				)
			try:
				data = _normalize(spec.get("rows"), spec.get("columns"))
			except NoDataError:
				if spec.get("charts") is not None and spec.get("charts") != []:
					raise InvalidArgumentError("A sheet with charts needs data") from None
				continue  # skip an empty tab, keep the rest of the workbook
			sheet_data.append((_unique_sheet_name(spec.get("title") or f"Sheet{i + 1}", used), data))
			sheet_charts.append(validate_charts(spec.get("charts"), data))
		# Every tab was empty → nothing to hand back (same rule as single-sheet).
		if not sheet_data:
			raise NoDataError("No data to prepare for Excel.")
	else:
		sheet_data = [(_unique_sheet_name(title or "Sheet1", set()), _normalize(rows, columns))]
		sheet_charts.append(validate_charts(charts, sheet_data[0][1]))

	content = _workbook_bytes(sheet_data, charts=sheet_charts)

	from frappe.utils.file_manager import save_file

	safe = (title or "export").replace(" ", "-").replace("/", "-")[:60] or "export"
	fdoc = save_file(f"{safe}.xlsx", content, None, None, is_private=1)
	return {
		"file_url": fdoc.file_url,
		"filename": fdoc.file_name,
		"title": title or "Export",  # clean title for the chat artifact card
		"mime_type": _XLSX_MIME,
		"size_bytes": int(fdoc.file_size or len(content)),
		"name": fdoc.name,
		"chart_count": sum(len(items) for items in sheet_charts),
	}


def _normalize(rows, columns) -> list:
	"""``rows`` (list of dicts or list of lists) → ``[header] + body`` 2D list.

	Raises ``NoDataError`` for an empty / contentless set (no columns, no data
	rows, or every cell blank) and ``InvalidArgumentError`` for a malformed one
	— the same guards the single-sheet path has always applied, now shared by
	every tab so one blank tab can't ship a user a workbook that opens empty.
	"""
	if rows is None or rows == []:
		raise NoDataError("No data to prepare for Excel.")
	if not isinstance(rows, list):
		raise InvalidArgumentError("rows must be a list of dicts or a list of lists")

	first = rows[0]
	if isinstance(first, dict):
		if not all(isinstance(r, dict) for r in rows):
			raise InvalidArgumentError("All rows must be dicts when the first row is a dict")
		header = list(columns) if columns else list(first.keys())
		body = [[_cell(r.get(c)) for c in header] for r in rows]
	elif isinstance(first, (list, tuple)):
		if not all(isinstance(r, (list, tuple)) for r in rows):
			raise InvalidArgumentError("All rows must be lists when the first row is a list")
		if columns:
			header, body = list(columns), [list(r) for r in rows]
		else:
			# Without an explicit `columns`, the first row is the header and the
			# rest is the body.
			header, body = list(first), [list(r) for r in rows[1:]]
	else:
		raise InvalidArgumentError("rows must be a list of dicts or a list of lists")

	if not header or not any(_nonempty(c) for r in body for c in r):
		raise NoDataError("No data to prepare for Excel.")
	_dates_from_iso_text(len(header), body)
	return [header] + body


def _dates_from_iso_text(width: int, body: list) -> None:
	"""In place: turn a column of ISO date / datetime text into real dates, so
	the workbook formats, sorts and filters them as dates. All or nothing per
	column: one non-date value (or an impossible date like 2026-13-40) leaves
	the whole column as text, so a mixed column is never half converted."""
	for c in range(width):
		values = [r[c] for r in body if c < len(r) and _nonempty(r[c])]
		if not values or not all(isinstance(v, str) for v in values):
			continue
		if all(_ISO_DATE_RE.match(v.strip()) for v in values):
			parse = datetime.date.fromisoformat
		elif all(_ISO_DATETIME_RE.match(v.strip()) for v in values):
			parse = datetime.datetime.fromisoformat
		else:
			continue
		try:
			parsed = {v: parse(v.strip()) for v in values}
		except ValueError:
			continue
		for r in body:
			if c < len(r) and _nonempty(r[c]):
				r[c] = parsed[r[c]]


def _workbook_bytes(sheet_data: list[tuple[str, list]], *, charts=None) -> bytes:
	"""One workbook, a sheet per (name, data).

	``frappe.utils.xlsxutils`` was rewritten from openpyxl to xlsxwriter in
	Frappe 16, so the two majors need different builders; ``jarvis.compat``
	probes for the 16 API and picks one. Building this inline against the 16 API
	made every export raise ``ModuleNotFoundError: xlsxwriter`` on a 15 bench.
	"""
	return compat.xlsx_bytes(sheet_data, charts=charts)


def _unique_sheet_name(raw, used: set[str]) -> str:
	"""Excel sheet names are ≤31 chars and must be unique; de-dupe collisions
	after sanitizing and truncating, before either workbook engine sees them."""
	base = _INVALID_SHEET_NAME_RE.sub(" ", str(raw)).strip(" '")[:31].rstrip(" '") or "Sheet"
	name, n = base, 2
	while name.lower() in used:
		suffix = f"-{n}"
		name = base[: 31 - len(suffix)] + suffix
		n += 1
	used.add(name.lower())
	return name


def _cell(v):
	if v is None:
		return ""
	if isinstance(v, (dict, list)):
		return frappe.as_json(v)
	return v


def _nonempty(v):
	"""A cell counts as data unless it's None or blank/whitespace. 0 and False
	are real values, so they count."""
	return v is not None and str(v).strip() != ""
