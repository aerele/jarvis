"""Shift assigned to an employee on a given date.

Wraps ``hrms.hr.doctype.shift_assignment.shift_assignment.get_employee_shift``.
Walks Shift Assignment records (date-bounded) + the employee's default
shift in priority order; falls back to "no shift assigned" without
raising. Returns the resolved shift details as a plain dict.

The agent uses this for attendance / OT / roster questions where
the answer depends on shift start + end times that vary day-to-day.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)


def get_employee_shift(
	employee: str,
	for_date: str | None = None,
	consider_default_shift: bool = True,
) -> dict:
	"""Return ``{shift, employee, for_date}`` where ``shift`` is the
	resolved Shift Type dict (or None if none assigned that day).
	"""
	if not employee:
		raise InvalidArgumentError("employee is required")
	if not frappe.db.exists("Employee", employee):
		raise InvalidArgumentError(f"unknown Employee: {employee}")
	if not frappe.has_permission("Employee", "read", doc=employee):
		raise PermissionDeniedError(f"no read permission on Employee {employee}")

	from hrms.hr.doctype.shift_assignment.shift_assignment import (
		get_employee_shift as _ges,
	)

	date_to_use = for_date or frappe.utils.today()
	shift = _ges(
		employee=employee,
		for_timestamp=date_to_use,
		consider_default_shift=bool(consider_default_shift),
	)
	# Helper always returns a plain dict (`shift_details or {}`) - never a
	# Document, never None. Collapse the falsy "nothing found" {} to None
	# for a cleaner envelope instead of calling a nonexistent .as_dict().
	return {
		"shift": dict(shift) if shift else None,
		"employee": employee,
		"for_date": date_to_use,
	}
