"""get_report_filters - return a saved report's filter fields at runtime.

A report's filters live in its client script (``frappe.query_reports[name].
filters``), not in the DB doc - the ``Report.filters`` child table is
populated only for a few UI-built reports. So we read the script via
``frappe.desk.query_report.get_script`` and parse the filter literals out of
it, letting the agent build a correct ``run_report`` filters dict without
hardcoded per-report keys.
"""

import re

import frappe
from frappe.desk.query_report import get_script

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError

_FIELDNAME_RE = re.compile(r"""fieldname\s*:\s*["']([^"']+)["']""")


def get_report_filters(report_name: str) -> dict:
	"""Return the filter fields a saved Frappe Report accepts.

	Result: ``{report_name, report_type, ref_doctype, filters:[{fieldname,
	label, fieldtype, options, reqd}], required:[fieldname...], note}``.
	Filters come from the report's client script, so dynamically-added or
	conditional filters may be absent (best-effort); ``required`` is the
	reliable subset to always supply to run_report.
	"""
	if not report_name:
		raise InvalidArgumentError("report_name is required")
	if not frappe.db.exists("Report", report_name):
		raise InvalidArgumentError(f"unknown Report: {report_name}")

	report_type, ref_doctype = frappe.db.get_value("Report", report_name, ["report_type", "ref_doctype"])
	try:
		script = get_script(report_name)
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"no permission to read report {report_name}") from e

	# Prefer the doc-declared filters (UI-built reports); otherwise parse the
	# client script (the case for virtually all standard ERPNext reports).
	raw = script.get("filters") or _parse_js_filters(script.get("script") or "")
	filters = [_normalize(f) for f in raw if (f or {}).get("fieldname")]
	return {
		"report_name": report_name,
		"report_type": report_type,
		"ref_doctype": ref_doctype,
		"filters": filters,
		"required": [f["fieldname"] for f in filters if f["reqd"]],
		"note": (
			"Filters parsed from the report script; dynamically-added or "
			"conditional filters may be missing. Always supply `required`."
		),
	}


def _normalize(f: dict) -> dict:
	opts = f.get("options")
	return {
		"fieldname": f.get("fieldname"),
		"label": f.get("label"),
		"fieldtype": f.get("fieldtype"),
		"options": opts if isinstance(opts, str) else None,
		"reqd": bool(f.get("reqd") or f.get("mandatory")),
	}


def _parse_js_filters(js: str) -> list[dict]:
	"""Best-effort parse of the ``filters: [ ... ]`` literal in a report's .js.

	Segment by ``fieldname:`` boundaries rather than brace-matching - filter
	entries embed ``get_data``/``on_change`` function bodies with nested
	braces, so brace-matching is unreliable.
	"""
	arr = _extract_filters_array(js)
	if not arr:
		return []
	out = []
	matches = list(_FIELDNAME_RE.finditer(arr))
	for idx, m in enumerate(matches):
		end = matches[idx + 1].start() if idx + 1 < len(matches) else len(arr)
		seg = arr[m.start() : end]
		out.append(
			{
				"fieldname": m.group(1),
				"fieldtype": _grab(r"""fieldtype\s*:\s*["']([^"']+)["']""", seg),
				"options": _grab(r"""options\s*:\s*["']([^"']+)["']""", seg),
				"label": (
					_grab(r"""label\s*:\s*__\(\s*["']([^"']+)["']""", seg)
					or _grab(r"""label\s*:\s*["']([^"']+)["']""", seg)
				),
				"reqd": bool(re.search(r"\breqd\s*:\s*1\b", seg)),
			}
		)
	return out


def _grab(pattern: str, text: str):
	m = re.search(pattern, text)
	return m.group(1) if m else None


def _extract_filters_array(js: str) -> str:
	"""Slice the ``filters: [ ... ]`` array out of the report .js by counting
	brackets (so column/formatter fieldnames elsewhere don't leak in)."""
	m = re.search(r"filters\s*:\s*\[", js)
	if not m:
		return ""
	i = m.end() - 1  # index of the opening '['
	depth = 0
	for j in range(i, len(js)):
		c = js[j]
		if c == "[":
			depth += 1
		elif c == "]":
			depth -= 1
			if depth == 0:
				return js[i : j + 1]
	return js[i:]
