"""The shape of a saved report's result, made uniform for the tools that turn it
into a file (``report_pdf``, ``export_query`` with ``report_name``).

``frappe.desk.query_report.run`` normalises columns to dicts, but a hand-written
Query Report and an older Prepared Report copy can still carry the legacy
``"Label:Fieldtype/Options:Width"`` string form, and rows arrive as dicts keyed by
fieldname or as positional lists (an appended total row can be a list among
dicts). Frappe's own ``build_xlsx_data`` does this too, but its signature differs
between v15 and v16, so the normalising lives here."""

from __future__ import annotations

import datetime

import frappe
from frappe.utils import get_datetime, getdate

# Fieldtypes whose stored value is a date the spreadsheet should hold as a date.
_DATE_FIELDTYPES = {"Date": getdate, "Datetime": get_datetime}


def normalise_columns(columns: list) -> list[dict]:
	"""Columns as dicts with at least ``fieldname``, ``label``, ``fieldtype``,
	``options`` and ``hidden``. Both the dict form and the legacy string form."""
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
					"hidden": bool(col.get("hidden")),
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
				"hidden": False,
			}
		)
	return out


def cell(row, col: dict, idx: int):
	"""One cell from a dict row (keyed by fieldname) or a positional row."""
	if isinstance(row, dict):
		return row.get(col["fieldname"])
	if isinstance(row, (list, tuple)):
		return row[idx] if idx < len(row) else None
	return None


def visible_columns(columns: list) -> list[tuple[int, dict]]:
	"""``(index, column)`` for every column a person sees. A ``hidden`` column is a
	helper the report script uses (an id behind a link); Desk's export drops it too."""
	return [(i, col) for i, col in enumerate(normalise_columns(columns)) if not col["hidden"]]


def as_file_value(value, col: dict):
	"""The value as a spreadsheet should hold it. A Prepared Report stores its rows
	as JSON, so a Date arrives as ``"2026-10-21"``: made a date again, so the
	spreadsheet column is a real date (xlsx) or an ISO text (csv). Anything that
	does not parse stays as it came."""
	if value is None or value == "":
		return value
	if isinstance(value, (datetime.date, datetime.datetime)):
		return value
	parse = _DATE_FIELDTYPES.get(col.get("fieldtype"))
	if parse is None or not isinstance(value, str):
		return value
	try:
		return parse(value)
	except Exception:
		return value
