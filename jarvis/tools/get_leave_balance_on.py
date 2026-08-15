"""Leave balance for an employee + leave type as of a date.

Wraps ``hrms.hr.doctype.leave_application.leave_application.get_leave_balance_on``.
That helper is HRMS's canonical "what's the source of truth for this
balance" computation: it walks Leave Allocation + Leave Ledger Entry
records across the policy period, applies carry-forward / expiry
rules, and returns a single number. The LLM consistently miscomputes
when forced to derive from raw allocations.

Read-only. Underlying helper enforces Leave Application read perm
internally; we add a boundary Employee existence check so the agent
gets a clean InvalidArgumentError rather than a stack trace from a
typo'd employee id.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)


def get_leave_balance_on(
	employee: str,
	leave_type: str,
	date: str | None = None,
	consider_all_leaves_in_the_allocation_period: bool = False,
) -> dict:
	"""Return ``{balance, employee, leave_type, date}`` where balance
	is the available leave balance for ``employee`` + ``leave_type`` on
	``date`` (defaults to today).
	"""
	if not employee:
		raise InvalidArgumentError("employee is required")
	if not leave_type:
		raise InvalidArgumentError("leave_type is required")
	if not frappe.db.exists("Employee", employee):
		raise InvalidArgumentError(f"unknown Employee: {employee}")
	if not frappe.db.exists("Leave Type", leave_type):
		raise InvalidArgumentError(f"unknown Leave Type: {leave_type}")
	if not frappe.has_permission("Employee", "read", doc=employee):
		raise PermissionDeniedError(f"no read permission on Employee {employee}")

	from hrms.hr.doctype.leave_application.leave_application import (
		get_leave_balance_on as _gb,
	)

	date_to_use = date or frappe.utils.today()
	balance = _gb(
		employee=employee,
		leave_type=leave_type,
		date=date_to_use,
		consider_all_leaves_in_the_allocation_period=bool(
			consider_all_leaves_in_the_allocation_period,
		),
	)
	return {
		"balance": float(balance or 0),
		"employee": employee,
		"leave_type": leave_type,
		"date": date_to_use,
	}
