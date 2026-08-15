"""Render a saved Frappe Report to a PDF and store it as a private File so the
chat surface can hand the user a download link.

This is the *report* analogue of ``download_pdf``. ``download_pdf`` only handles a
single document (``doctype`` + ``name``) via ``frappe.get_print`` + a Print
Format. A report has neither a document nor a print format — it is ``columns`` +
a rowset — so this executes the report, builds an HTML table from the result, and
renders it with ``frappe.utils.pdf.get_pdf``. The saved File and the return
envelope are byte-identical to ``download_pdf``'s, so the chat renders the same
download card with **no frontend change**.

Without this tool the model has no way to satisfy "give me this report as a PDF":
it improvises with the container's ``browser``/``exec`` tools and writes the PDF
onto the container filesystem, where it never becomes a Frappe File and never
surfaces in chat ("Done, I generated the PDF" — but nothing to download).

Permission: report execution goes through the same ``frappe.desk.query_report.run``
call ``run_report`` uses, so Frappe applies report-level permission and
``validate_filters_permissions`` and raises ``frappe.PermissionError``, which we
translate to ``PermissionDeniedError`` — one exception contract across tools.

Frappe 15/16: ``query_report.run`` / ``get_pdf`` / ``save_file`` are stable across
both majors (``download_pdf`` uses ``get_print`` + ``save_file`` with no
``jarvis.compat`` shim), so none is needed here.
"""

import re

import frappe
from frappe.desk.query_report import run as frappe_run_report

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError

# Above this many columns a portrait page clips the table, so default to landscape.
_LANDSCAPE_COL_THRESHOLD = 6
_ORIENTATIONS = ("portrait", "landscape")
# Fieldtypes whose values are right-aligned numbers in the rendered table.
_NUMERIC_FIELDTYPES = {"Currency", "Float", "Int", "Percent"}


def report_pdf(
	report_name: str,
	filters: dict | None = None,
	orientation: str | None = None,
	title: str | None = None,
) -> dict:
	"""Execute ``report_name`` with ``filters``, render its columns+rows to a PDF,
	store it as a private File, and return the ``download_pdf`` envelope
	``{file_url, filename, title, mime_type, size_bytes, name}`` so the chat
	surfaces an identical download link.

	``orientation`` is ``"portrait"`` / ``"landscape"``; omitted picks landscape
	for wide reports. ``title`` overrides the heading (defaults to the report name).
	"""
	if not report_name:
		raise InvalidArgumentError("report_name is required")
	if not frappe.db.exists("Report", report_name):
		raise InvalidArgumentError(f"unknown Report: {report_name}")

	if orientation is not None:
		orientation = str(orientation).strip().lower()
		if orientation not in _ORIENTATIONS:
			raise InvalidArgumentError(f"orientation must be one of {_ORIENTATIONS}, got {orientation!r}")

	# A Report Builder report is a saved list view, not a columns+rows result this
	# path can render; refuse clearly rather than emit an empty or garbled PDF.
	report_type = frappe.db.get_value("Report", report_name, "report_type")
	if report_type == "Report Builder":
		raise InvalidArgumentError(
			f"{report_name} is a Report Builder report — export it from its list view. "
			"report_pdf renders Query Report and Script Report results."
		)

	# Same execution path as run_report: Frappe enforces report + filter
	# permissions and raises PermissionError. ignore_prepared_report=True forces
	# LIVE data rather than a queued async stub for prepared reports.
	try:
		res = frappe_run_report(
			report_name=report_name,
			filters=filters or {},
			ignore_prepared_report=True,
		)
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"no permission to run report {report_name}") from e
	except Exception as e:
		# The report itself failed to run: a missing/bad filter (Frappe raises
		# ``TypeError: format requires a mapping`` when a ``%(name)s`` filter has no
		# value), invalid report SQL, or a mandatory input the report needs. From
		# this tool's contract that is a bad-input outcome, not a 500. Log the full
		# traceback so a genuine infrastructure fault stays visible, then surface a
		# clean, deliberate error rather than swallowing it.
		frappe.log_error(
			title=f"report_pdf: running report {report_name} failed",
			message=frappe.get_traceback(),
		)
		raise InvalidArgumentError(f"could not run report {report_name}: {e}") from e

	columns = _normalise_columns(res.get("columns") or [])
	if not columns:
		raise InvalidArgumentError(
			f"report {report_name} returned no columns to render "
			"(it may require filters, or be a layout with no tabular result)"
		)
	rows = res.get("result") or []
	# When add_total_row is set, query_report.run has already appended the total as
	# the LAST row of `result` (frappe/desk/query_report.py add_total_row); we only
	# style it, never recompute it.
	has_total_row = bool(res.get("add_total_row")) and bool(rows)

	report_title = (title or "").strip() or report_name
	auto_landscape = len(columns) > _LANDSCAPE_COL_THRESHOLD
	landscape = orientation == "landscape" or (orientation is None and auto_landscape)

	html = _render_html(report_title, filters or {}, columns, rows, has_total_row)

	from frappe.utils.pdf import get_pdf

	pdf_bytes = get_pdf(html, options={"orientation": "Landscape" if landscape else "Portrait"})
	if not pdf_bytes:
		raise InvalidArgumentError(f"PDF generation for report {report_name} produced no content")

	from frappe.exceptions import ValidationError

	from jarvis.tools._export import save_export_file

	# Owner-only File (dt/dn default to None -> unattached, so has_permission falls
	# through to owner-only). This PDF is rendered under the CALLING user's record-
	# and field-filtered permissions, so it must NOT be attached to the shared
	# Report doc: File.has_permission would then defer to Report read, letting
	# anyone who can read the Report download a row-filtered artifact (e.g. a
	# Salary Register). See the export-architecture plan-check, finding C5.
	try:
		return save_export_file(
			f"{_safe_filename(report_title)}.pdf",
			pdf_bytes,
			title=report_title,
			mime_type="application/pdf",
		)
	except ValidationError as e:
		# A File-save constraint (e.g. a rejected filename) is a bad-input outcome,
		# not an opaque 500.
		raise InvalidArgumentError(f"could not store the report PDF: {e}") from e


def _safe_filename(title: str) -> str:
	"""A File-doctype-safe base name derived from the title.

	A report name or a caller-supplied ``title`` can carry markup, slashes and
	other characters that Frappe's File sanitiser rejects or rewrites — notably it
	re-introduces a ``/`` from a closing HTML tag (``</b>``), which then raises
	``ValidationError: File name cannot have /`` deep inside ``save_file``. Keep
	only filename-safe characters, cap the length, and never return empty.
	"""
	base = re.sub(r"[^A-Za-z0-9._-]+", "-", title or "").strip("-.")
	return base[:80] or "report"


def _normalise_columns(columns: list) -> list[dict]:
	"""Return columns as dicts with at least ``fieldname``, ``label``,
	``fieldtype``, ``options``.

	``query_report.run`` normalises columns to dicts, but a hand-written Query
	Report can still emit the legacy ``"Label:Fieldtype/Options:Width"`` string
	form, so both are handled.
	"""
	out: list[dict] = []
	for i, col in enumerate(columns):
		if isinstance(col, dict):
			fieldname = col.get("fieldname") or col.get("label") or f"col_{i}"
			out.append(
				{
					"fieldname": fieldname,
					"label": col.get("label") or fieldname,
					"fieldtype": col.get("fieldtype") or "Data",
					"options": col.get("options") or "",
				}
			)
			continue
		# Legacy string column: "Label:Fieldtype/Options:Width"
		parts = str(col).split(":")
		label = parts[0].strip() or f"col_{i}"
		fieldtype, options = "Data", ""
		if len(parts) > 1 and parts[1]:
			ft = parts[1].split("/")
			fieldtype = ft[0].strip() or "Data"
			options = ft[1].strip() if len(ft) > 1 else ""
		out.append(
			{
				"fieldname": frappe.scrub(label),
				"label": label,
				"fieldtype": fieldtype,
				"options": options,
			}
		)
	return out


def _cell(row, col: dict, idx: int):
	"""Read one cell, tolerating both dict rows (keyed by fieldname) and
	list/tuple rows (positional) — Frappe returns either depending on report type,
	and an appended total row can be a list among dict rows."""
	if isinstance(row, dict):
		return row.get(col["fieldname"])
	if isinstance(row, (list, tuple)):
		return row[idx] if idx < len(row) else None
	return None


def _format_cell(value, col: dict) -> str:
	"""HTML-escaped, fieldtype-aware rendering so the PDF matches the on-screen
	table. Falls back to the raw string if ``frappe.format`` cannot render it —
	a formatting hiccup must never abort the whole PDF."""
	if value is None or value == "":
		return ""
	try:
		formatted = frappe.format(value, {"fieldtype": col["fieldtype"], "options": col["options"]})
	except Exception:
		formatted = value
	return frappe.utils.escape_html(str(formatted))


def _render_html(title: str, filters: dict, columns: list[dict], rows: list, has_total_row: bool) -> str:
	"""Build a self-contained HTML document: escaped title, a one-line filters
	summary, and the data table. All dynamic text is escaped — report data is
	untrusted and must never inject markup into the PDF."""
	esc = frappe.utils.escape_html
	filter_bits = [
		f"{esc(str(k))}: {esc(str(v))}" for k, v in (filters or {}).items() if v not in (None, "", [])
	]
	filters_line = " · ".join(filter_bits)

	head = "".join(f"<th>{esc(c['label'])}</th>" for c in columns)

	body_parts = []
	last = len(rows) - 1
	for r_i, row in enumerate(rows):
		is_total = has_total_row and r_i == last
		tds = []
		for c_i, col in enumerate(columns):
			align = "right" if col["fieldtype"] in _NUMERIC_FIELDTYPES else "left"
			tds.append(f'<td style="text-align:{align}">{_format_cell(_cell(row, col, c_i), col)}</td>')
		cls = ' class="total"' if is_total else ""
		body_parts.append(f"<tr{cls}>{''.join(tds)}</tr>")

	empty_note = "" if rows else '<p class="empty">No data for these filters.</p>'
	table = (
		""
		if not rows
		else (f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_parts)}</tbody></table>")
	)
	filters_html = f'<p class="filters">{filters_line}</p>' if filters_line else ""

	return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 11px; color: #1a1a1a; margin: 18px; }}
  h1 {{ font-size: 16px; margin: 0 0 4px; }}
  .filters {{ color: #555; margin: 0 0 12px; font-size: 10px; }}
  .empty {{ color: #777; font-style: italic; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 4px 6px; vertical-align: top; }}
  thead th {{ background: #f2f2f2; text-align: left; font-weight: 600; }}
  tr.total td {{ font-weight: 700; background: #fafafa; }}
</style></head><body>
  <h1>{esc(title)}</h1>
  {filters_html}
  {table}{empty_note}
</body></html>"""
