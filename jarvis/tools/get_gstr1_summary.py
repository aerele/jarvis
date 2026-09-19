"""GSTR-1 return-side period totals for a books-vs-return tie-out.

Reads the FILED government return figures that india_compliance stores in the
``GST Return Log`` — which live only inside the gzip-compressed ``filed_summary``
File attachment, not in any queryable field, and behind no read-only IC API. This
tool does the read + the canonical Total-Liability reduction server-side (the
sandboxed evaluator + the model-driven delegate cannot), so an auditor can tie its
own books rollup against what was actually filed.

STRICTLY READ-ONLY. Its entire IC surface is ``frappe.db.exists`` / ``frappe.get_doc``
(never ``.insert``/``.save``), reading the stored ``filing_status`` / ``generation_status``
attributes, and ``log.get_json_for("filed_summary")``. It NEVER calls the mutating
"getters" (``get_gst_return_log`` inserts a log, ``get_return_status`` hits the GSTN
API + writes, ``get_gstr1_data`` / ``generate*`` write Files). (One benign exception
outside our control: ``get_json_for`` itself clears a dangling ``file_url`` via
``db_set`` if the physical File is missing — IC housekeeping on a restored/corrupt
row, never on a healthy read.)

Reduction replicates IC's own Total-Liability rollup (``gstr_1.js`` "Total Liability"):
sum every summary row by the two ``consider_*`` flags, per column — NO ``indent``
filter (the flagged indent-0 "Net Liability from Amendments" row and QRMP-key rows
MUST be included, with their already-signed amounts). Only ``filed_summary`` is used
(``books_summary`` is IC's own books rollup — books-vs-books, not a return tie-out).

Malformed arguments RAISE (caller bugs surface); data-absent / permission-sliced /
app-absent / unrecognized-shape return ``{"available": False, "reason": ...}`` so the
delegate degrades honestly to ``not_evaluable`` — never a false clean.
"""

from __future__ import annotations

import re

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._company_scope import is_company_permitted

RETURN_LOG = "GST Return Log"
_FILED_SUMMARY = "filed_summary"
# per-column reduction keys (IC amount keys carry an ``_amount`` suffix except taxable)
_TAX_KEYS = (
	("total_igst", "total_igst_amount"),
	("total_cgst", "total_cgst_amount"),
	("total_sgst", "total_sgst_amount"),
	("total_cess", "total_cess_amount"),
)
_AMOUNT_KEYS = ("total_taxable_value",) + tuple(ic for _, ic in _TAX_KEYS)
_PERIOD_RE = re.compile(r"^\d{6}$")  # MMYYYY


def _na(reason: str, **extra) -> dict:
	"""The honest 'no return-side tie-out available' envelope."""
	return {"available": False, "reason": reason, **extra}


def _flt(x) -> float:
	try:
		return float(x or 0)
	except (TypeError, ValueError):
		return 0.0


def _period_window(return_period: str) -> tuple[str, str]:
	"""MMYYYY -> (from_date, to_date) for a MONTHLY period (IC's own mapping)."""
	from frappe.utils import get_last_day, getdate

	mm, yyyy = return_period[:2], return_period[2:]
	start = getdate(f"{yyyy}-{mm}-01")
	return str(start), str(get_last_day(start))


def _schema_ok(rows) -> bool:
	"""Recognize IC's summary shape before reducing — refuse a renamed/foreign
	structure rather than silently reduce it to zero (a false clean)."""
	if not isinstance(rows, list) or not rows:
		return False
	if not any(isinstance(r, dict) and "consider_in_total_taxable_value" in r for r in rows):
		return False
	# every amount key must appear on at least one row (guards a renamed key -> 0)
	seen: set[str] = set()
	for r in rows:
		if isinstance(r, dict):
			seen.update(k for k in _AMOUNT_KEYS if k in r)
	return set(_AMOUNT_KEYS).issubset(seen)


def _reduce(rows) -> dict:
	"""IC's canonical Total-Liability reduction (gstr_1.js): sum rows by the two
	``consider_*`` flags, per column — no indent filter, amounts already signed."""
	totals = {
		"total_taxable_value": 0.0,
		"total_igst": 0.0,
		"total_cgst": 0.0,
		"total_sgst": 0.0,
		"total_cess": 0.0,
	}
	for r in rows:
		if not isinstance(r, dict):
			continue
		if r.get("consider_in_total_taxable_value"):
			totals["total_taxable_value"] += _flt(r.get("total_taxable_value"))
		if r.get("consider_in_total_tax"):
			for out_key, ic_key in _TAX_KEYS:
				totals[out_key] += _flt(r.get(ic_key))
	return {k: round(v, 2) for k, v in totals.items()}


def _resolve_filed_log(company: str, return_period: str, gstin: str | None, return_type: str):
	"""The single Filed GSTR-1 log for (company, period[, gstin]), read-only.

	Uses get_all/get_doc ONLY — never ``get_gst_return_log`` (which inserts). Returns
	the loaded doc, or an ``_na`` dict when absent / ambiguous / not filed."""
	filters = {
		"company": company,
		"return_period": return_period,
		"return_type": ["in", ["GSTR1", "GSTR-1", return_type]],
	}
	if gstin:
		filters["gstin"] = gstin
	names = frappe.get_all(RETURN_LOG, filters=filters, pluck="name", ignore_permissions=True)
	if not names:
		return _na("no_log")
	if len(names) > 1 and not gstin:
		# a multi-registration company has one log per GSTIN — never blend them.
		return _na("ambiguous_gstin", gstins_found=len(names))
	# deterministic pick: the Filed one (latest by name for a stable tie-break)
	docs = [frappe.get_doc(RETURN_LOG, n) for n in sorted(names)]
	filed = [d for d in docs if (d.get("filing_status") or "").strip().lower() == "filed"]
	if not filed:
		return _na("not_filed")
	return filed[-1]


def get_gstr1_summary(
	company: str,
	return_period: str,
	gstin: str | None = None,
	return_type: str = "GSTR1",
) -> dict:
	"""Return the FILED GSTR-1 period totals for a books-vs-return tie-out.

	``return_period`` is ``MMYYYY`` (e.g. ``"122026"``). On success returns
	``{available: True, source: "filed_summary", log_name, gstin, return_period,
	return_type, from_date, to_date, filing_status, generation_status,
	total_taxable_value, total_igst, total_cgst, total_sgst, total_cess}``. Otherwise
	``{available: False, reason}`` — the delegate then leaves the tie-out un-fed and
	RET-5 stays ``not_evaluable``. Raises ``InvalidArgumentError`` on malformed args.
	"""
	# --- arg validation: RAISE (caller bugs must not hide behind not-available) ---
	company = (company or "").strip()
	return_period = (return_period or "").strip()
	if not company:
		raise InvalidArgumentError("company is required")
	if not _PERIOD_RE.match(return_period):
		raise InvalidArgumentError(f"return_period must be MMYYYY (e.g. '122026'); got {return_period!r}")
	if not frappe.db.exists("Company", company):
		raise InvalidArgumentError(f"unknown Company: {company}")

	# --- everything else: fail-closed to an honest not-available envelope ---
	try:
		if "india_compliance" not in frappe.get_installed_apps():
			return _na("app_absent")
		if not is_company_permitted(company):  # Company User-Permission slice
			return _na("permission_denied")
		# doctype-level read ROLE gate BEFORE any resolution, so a caller without GST
		# Return Log read can't learn a log's existence / GSTIN count / filed-status for a
		# named company+period. The per-record check below still enforces the record slice.
		if not frappe.has_permission(RETURN_LOG, ptype="read"):
			return _na("permission_denied")

		resolved = _resolve_filed_log(company, return_period, (gstin or "").strip() or None, return_type)
		if isinstance(resolved, dict):  # an _na(...) envelope
			return resolved
		log = resolved

		# per-RECORD read perm under the run-as identity (enforces the Company/GSTIN User
		# Permission slice on THIS log — the doctype-level check above would not).
		if not frappe.has_permission(RETURN_LOG, ptype="read", doc=log):
			return _na("permission_denied")

		# QRMP quarterly deferred (D2): the log covers a quarter but the auditor sums a
		# month -> windows mismatch. Degrade honestly rather than tie the wrong windows.
		if (log.get("filing_preference") or "").strip().lower() == "quarterly":
			return _na("window_mismatch", filing_preference="Quarterly")

		if not log.get(_FILED_SUMMARY):
			return _na("no_filed_summary")
		rows = log.get_json_for(_FILED_SUMMARY)
		if not _schema_ok(rows):
			return _na("schema_unrecognized")

		from_date, to_date = _period_window(return_period)
		result = {
			"available": True,
			"source": _FILED_SUMMARY,
			"log_name": log.name,
			"gstin": log.get("gstin"),
			"return_period": return_period,
			"return_type": (log.get("return_type") or return_type),
			"from_date": from_date,
			"to_date": to_date,
			"filing_status": log.get("filing_status"),
			"generation_status": log.get("generation_status"),
			**_reduce(rows),
		}
	except InvalidArgumentError:
		raise
	except Exception:
		# never raise an unexpected fault to the delegate: fail-closed + a bench-side
		# breadcrumb (out of model context) so a broken RET-5 is diagnosable.
		frappe.log_error(title="get_gstr1_summary failed", message=frappe.get_traceback())
		return _na("error")

	# structured, model-invisible telemetry so a silently-wrong RET-5 is diagnosable.
	frappe.logger("jarvis.gstr1_summary").info(
		{
			k: result.get(k)
			for k in ("log_name", "source", "filing_status", "generation_status", "return_period")
		}
	)
	return result
