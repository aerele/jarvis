"""Per-leave-type dashboard snapshot for an employee as of a date.

Wraps ``hrms.hr.doctype.leave_application.leave_application.get_leave_details``.
This is the same multi-row payload HRMS's employee dashboard renders -
each leave type with allocated / used / pending / expired / balance
broken out. Critical for "what's my full leave situation?" questions
the LLM would otherwise answer with a single approximation.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)


def get_leave_details(
	employee: str,
	date: str | None = None,
) -> dict:
	"""Return ``{leave_details, employee, date}`` where leave_details
	is the multi-row HRMS payload (one entry per leave type with
	allocated / used / pending / expired / balance).
	"""
	if not employee:
		raise InvalidArgumentError("employee is required")
	if not frappe.db.exists("Employee", employee):
		raise InvalidArgumentError(f"unknown Employee: {employee}")
	if not frappe.has_permission("Employee", "read", doc=employee):
		raise PermissionDeniedError(f"no read permission on Employee {employee}")

	from hrms.hr.doctype.leave_application.leave_application import (
		get_leave_details as _gld,
	)

	date_to_use = date or frappe.utils.today()
	details = _gld(employee=employee, date=date_to_use)
	return {
		"leave_details": details,
		"employee": employee,
		"date": date_to_use,
	}
