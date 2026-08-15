"""Holidays for an employee in a date range.

Wraps ``hrms.hr.utils.get_holidays_for_employee``. Resolves the right
Holiday List (employee-specific override, then department default,
then company default) and returns the holiday rows that apply -
including the holiday list owner, name, and date.

The agent uses this when asked "is X a holiday for me?" or "how many
holidays in January?". A get_list against Holiday alone wouldn't
know which Holiday List applies to this employee.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)


def get_holidays_for_employee(
	employee: str,
	start_date: str,
	end_date: str,
	only_non_weekly: bool = False,
) -> dict:
	"""Return ``{holidays, employee, start_date, end_date}`` where
	``holidays`` is the list of {holiday_date, description, weekly_off}
	rows that apply to ``employee`` between the dates.
	"""
	if not employee:
		raise InvalidArgumentError("employee is required")
	if not start_date:
		raise InvalidArgumentError("start_date is required")
	if not end_date:
		raise InvalidArgumentError("end_date is required")
	if not frappe.db.exists("Employee", employee):
		raise InvalidArgumentError(f"unknown Employee: {employee}")
	if not frappe.has_permission("Employee", "read", doc=employee):
		raise PermissionDeniedError(f"no read permission on Employee {employee}")

	from hrms.hr.utils import get_holidays_for_employee as _ghe

	# raise_exception=False so a missing holiday list returns [] rather
	# than throwing - the agent envelope wants empty rather than 500.
	holidays = _ghe(
		employee=employee,
		start_date=start_date,
		end_date=end_date,
		raise_exception=False,
		only_non_weekly=bool(only_non_weekly),
	)
	return {
		"holidays": holidays or [],
		"employee": employee,
		"start_date": start_date,
		"end_date": end_date,
	}
