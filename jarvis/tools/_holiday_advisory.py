"""Non-blocking holiday advisory for employee-date writes.

When a write touches a curated HR doctype that pairs an Employee with an
activity date, we look that date up against the employee's holiday list
(ERPNext resolution: Employee.holiday_list -> Company.default_holiday_list)
and, if it lands on a holiday or weekly-off, attach a human-readable warning
to the tool result. It NEVER blocks and NEVER raises into the write path: any
failure (no erpnext, no holiday list, bad data) returns no advisory.
"""

from __future__ import annotations

import frappe
from frappe.utils import getdate

try:
	# The official resolver: Employee.holiday_list, else the Company default.
	from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
except ImportError:  # erpnext not installed on this bench -> feature is a no-op
	get_holiday_list_for_employee = None

# doctype -> {employee field, list of (start_field, end_field_or_None) spans}.
# A None end_field means a single date; a pair means an inclusive range. Only
# doctypes where a date genuinely means "a day this employee is expected to be
# doing something" — deliberately NOT payroll periods, comp-off (working a
# holiday is the point), or identity dates (DOB/DOJ/relieving).
CURATED_MAP: dict[str, dict] = {
	"Attendance": {"employee": "employee", "spans": [("attendance_date", None)]},
	"Attendance Request": {"employee": "employee", "spans": [("from_date", "to_date")]},
	"Employee Checkin": {"employee": "employee", "spans": [("time", None)]},
	"Shift Assignment": {"employee": "employee", "spans": [("start_date", "end_date")]},
	"Leave Application": {"employee": "employee", "spans": [("from_date", "to_date")]},
}


def _who(doc, employee: str) -> str:
	return doc.get("employee_name") or employee


def _advisory_line(who: str, holiday: dict) -> str:
	kind = "weekly off" if holiday.get("weekly_off") else "holiday"
	desc = holiday.get("description") or kind
	return f"{who}: {holiday['holiday_date']} is a {kind} ({desc}) on their holiday list."


def advisories_for_doc(doc) -> list[str]:
	"""Advisory strings for a just-written doc, or [] (never raises)."""
	mark = len(frappe.local.message_log) if getattr(frappe.local, "message_log", None) else 0
	try:
		spec = CURATED_MAP.get(getattr(doc, "doctype", None))
		if not spec or get_holiday_list_for_employee is None:
			return []
		employee = doc.get(spec["employee"])
		if not employee:
			return []
		who = _who(doc, employee)
		out: list[str] = []
		for start_field, end_field in spec["spans"]:
			start_raw = doc.get(start_field)
			if not start_raw:
				continue
			start = getdate(start_raw)
			end = getdate(doc.get(end_field)) if end_field and doc.get(end_field) else start
			if end < start:
				start, end = end, start
			# Resolve the holiday list AS OF the activity date, not today. Lists are
			# yearly and (under HRMS) assigned per period via Holiday List Assignment,
			# so a backdated attendance or a mid-year reassignment must use the list
			# in effect THEN. ERPNext's vanilla resolver accepts and ignores as_on.
			holiday_list = get_holiday_list_for_employee(employee, raise_exception=False, as_on=start)
			if not holiday_list:
				continue
			holidays = frappe.get_all(
				"Holiday",
				filters={"parent": holiday_list, "holiday_date": ("between", [start, end])},
				fields=["holiday_date", "description", "weekly_off"],
				order_by="holiday_date",
			)
			out.extend(_advisory_line(who, h) for h in holidays)
		return out
	except Exception:
		# Advisory must never break a write. Drop only the messages THIS call pushed
		# (e.g. a frappe.throw in the swallowed path) back to the pre-call mark, never
		# the successful write's own msgprints.
		log = getattr(frappe.local, "message_log", None)
		if log is not None and len(log) > mark:
			del log[mark:]
		return []


def attach(result: dict, doc) -> dict:
	"""Append holiday advisories to a write-tool result under ``warnings``.

	Non-mutating on the no-advisory path (no ``warnings`` key added), so a doc
	dict without holidays is byte-identical to today's output.
	"""
	advisories = advisories_for_doc(doc)
	if advisories:
		# Never assume ``warnings`` is absent or a list: a curated doctype could
		# carry a Custom Field literally named ``warnings`` (its value lands in
		# ``as_dict()``). setdefault().extend() would then AttributeError and break
		# the write — the one invariant we must not pierce. Extend a list, else set.
		existing = result.get("warnings")
		if isinstance(existing, list):
			existing.extend(advisories)
		else:
			result["warnings"] = advisories
	return result
