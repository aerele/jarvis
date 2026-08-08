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

import frappe

from jarvis import compat
from jarvis.exceptions import InvalidArgumentError, NoDataError

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def export_excel(
	rows: list | None = None,
	title: str | None = None,
	columns: list | None = None,
	sheets: list | None = None,
) -> dict:
	"""Build an .xlsx and return ``{file_url, filename, title, mime_type,
	size_bytes, name}``.

	Single sheet: ``rows`` is a list of dicts (columns = first row's keys, or
	the given ``columns``) or a list of lists (first row is the header unless
	``columns`` is given); ``title`` names the sheet + file.

	Multi-tab: pass ``sheets`` = ``[{"title": str, "rows": [...], "columns":
	[...]?}, ...]`` — one tab per entry (empty tabs are skipped). ``title``
	names the workbook file. When ``sheets`` is given, ``rows`` is ignored.
	"""
	if sheets is not None:
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
				continue  # skip an empty tab, keep the rest of the workbook
			sheet_data.append((_unique_sheet_name(spec.get("title") or f"Sheet{i + 1}", used), data))
		# Every tab was empty → nothing to hand back (same rule as single-sheet).
		if not sheet_data:
			raise NoDataError("No data to prepare for Excel.")
	else:
		sheet_data = [((title or "Sheet1")[:31], _normalize(rows, columns))]

	content = _workbook_bytes(sheet_data)

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
	}


def _normalize(rows, columns) -> list:
	"""``rows`` (list of dicts or list of lists) → ``[header] + body`` 2D list.

	Raises ``NoDataError`` for an empty / contentless set (no columns, no data
	rows, or every cell blank) and ``InvalidArgumentError`` for a malformed one
	— the same guards the single-sheet path has always applied, now shared by
	every tab so one blank tab can't ship a user a workbook that opens empty.
	"""
	if not isinstance(rows, list) or not rows:
		raise NoDataError("No data to prepare for Excel.")

	first = rows[0]
	if isinstance(first, dict):
		header = list(columns) if columns else list(first.keys())
		body = [[_cell(r.get(c)) for c in header] for r in rows if isinstance(r, dict)]
	elif isinstance(first, (list, tuple)):
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
	return [header] + body


def _workbook_bytes(sheet_data: list[tuple[str, list]]) -> bytes:
	"""One workbook, a sheet per (name, data).

	``frappe.utils.xlsxutils`` was rewritten from openpyxl to xlsxwriter in
	Frappe 16, so the two majors need different builders; ``jarvis.compat``
	probes for the 16 API and picks one. Building this inline against the 16 API
	made every export raise ``ModuleNotFoundError: xlsxwriter`` on a 15 bench.
	"""
	return compat.xlsx_bytes(sheet_data)


def _unique_sheet_name(raw, used: set[str]) -> str:
	"""Excel sheet names are ≤31 chars and must be unique; de-dupe collisions
	(after truncation) so a workbook with two 'Summary' tabs doesn't blow up."""
	base = (str(raw) or "Sheet")[:31]
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
