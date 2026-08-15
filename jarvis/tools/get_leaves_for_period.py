"""Aggregate leave days an employee consumed in a period.

Wraps ``hrms.hr.doctype.leave_application.leave_application.get_leaves_for_period``.
Returns total leave days taken between two dates for a given leave
type, with holidays / weekly-off / half-days correctly netted out.

The "naive" version (count days in approved Leave Application rows
between dates) is what the LLM keeps trying; it overcounts because
it doesn't know about the leave_type's include_holiday setting or
the holiday list applied to the employee.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)


def get_leaves_for_period(
	employee: str,
	leave_type: str,
	from_date: str,
	to_date: str,
) -> dict:
	"""Return ``{days, employee, leave_type, from_date, to_date}`` for
	leave days consumed in the inclusive range.
	"""
	if not employee:
		raise InvalidArgumentError("employee is required")
	if not leave_type:
		raise InvalidArgumentError("leave_type is required")
	if not from_date:
		raise InvalidArgumentError("from_date is required")
	if not to_date:
		raise InvalidArgumentError("to_date is required")
	if not frappe.db.exists("Employee", employee):
		raise InvalidArgumentError(f"unknown Employee: {employee}")
	if not frappe.db.exists("Leave Type", leave_type):
		raise InvalidArgumentError(f"unknown Leave Type: {leave_type}")
	if not frappe.has_permission("Employee", "read", doc=employee):
		raise PermissionDeniedError(f"no read permission on Employee {employee}")

	from hrms.hr.doctype.leave_application.leave_application import (
		get_leaves_for_period as _gfp,
	)

	days = _gfp(
		employee=employee,
		leave_type=leave_type,
		from_date=from_date,
		to_date=to_date,
	)
	return {
		"days": float(days or 0),
		"employee": employee,
		"leave_type": leave_type,
		"from_date": from_date,
		"to_date": to_date,
	}
