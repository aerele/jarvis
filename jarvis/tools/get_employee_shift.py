"""Shift(s) an employee is on for a day, or at an instant.

Built on ``hrms.hr.doctype.shift_assignment.shift_assignment``: HRMS computes
each shift's start, end and check-in window; this tool decides which
assignments applied. "No shift" is an empty result, never an error. Returns
HRMS's shift details as plain dicts.

The agent uses this for attendance / OT / roster questions where
the answer depends on shift start + end times that vary day-to-day.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import frappe

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Hour 24 is refused here: Python 3.14's fromisoformat reads "24:00" as the next
# midnight, 3.11 rejects it, and the answer must not depend on the interpreter.
_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[ T](?:[01]\d|2[0-3]):\d{2}(?::\d{2}(?:\.\d{6})?)?")
_ONE_DAY = timedelta(days=1)


def get_employee_shift(
	employee: str,
	for_date: str | None = None,
	consider_default_shift: bool = True,
) -> dict:
	"""Return ``{shift, shifts, employee, for_date}``, plus ``for_timestamp``
	when ``for_date`` carried a time.

	``for_date`` is a date (``YYYY-MM-DD``, default today) or a naive,
	site-local datetime (``YYYY-MM-DD HH:MM[:SS]``). They ask different questions:

	- a date: every shift that STARTS on that day, ordered by check-in window.
	  A night shift belongs to the day it starts on, so the one that began the
	  evening before is not listed. The default shift is used only when no
	  assignment covers the day.
	- a datetime: the shift whose window (check-in / check-out margins included)
	  contains that instant, so a night worker asked about at 09:00 has none.
	  The default shift is used when no ASSIGNED shift covers the instant: with
	  a Night assignment on D and a Day default, D 10:00 gives the Day shift
	  although the date question for D gives Night. Which shift covers an
	  instant agrees with HRMS get_employee_shift (what auto-attendance uses).
	  HRMS's check-in lookup can differ in one case: it also trims an assigned
	  shift's margin against a different default shift's occurrence next to it
	  (an Evening 17:00 assignment and a Day default: check-in gives Day at
	  16:30, this tool gives Evening, whose window opens at 16:00).

	Holidays and leave are ignored (HRMS ignores them here too). Margins of
	shifts listed together are trimmed so they do not overlap, as HRMS does.

	Which assignments count: submitted ones that were Active on the day. HRMS's
	daily job sets every assignment that ended before yesterday to Inactive, so
	an Inactive assignment that ended before yesterday still counts, and one
	ending later (or never) that is Inactive was switched off by hand and does
	not. The one case the data cannot tell apart: an assignment switched off by
	hand and since ended before yesterday counts as if the job had expired it.
	That happens in practice: HRMS refuses to cancel an assignment with linked
	check-ins or attendance, so HR sets it Inactive instead, and an Inactive
	assignment skips overlap validation, so its replacement may overlap it;
	once both have ended, both are listed.

	Each shift is HRMS's shift-details dict: ``{shift_type: {name, start_time,
	end_time, ...}, start_datetime, end_datetime, actual_start, actual_end}``,
	``actual_*`` being the check-in window. ``shift`` is the first of
	``shifts``, or None.
	"""
	# A dict or list would pass frappe.db.exists as a filter set and reach the
	# query below; only a name may.
	if not isinstance(employee, str) or not employee.strip():
		raise InvalidArgumentError("employee is required (an Employee name)")
	if "hrms" not in frappe.get_installed_apps():
		raise InvalidArgumentError("HRMS is not installed on this site")
	if not frappe.db.exists("Employee", employee):
		raise InvalidArgumentError(f"unknown Employee: {employee}")
	if not frappe.has_permission("Employee", "read", doc=employee):
		raise PermissionDeniedError(f"no read permission on Employee {employee}")

	from hrms.hr.doctype.shift_assignment import shift_assignment as hrms_shift

	# One "today" per call: it is both the default day and the expiry cut-off.
	today = frappe.utils.getdate()
	day, instant = _resolve(for_date, today)
	default = frappe.db.get_value("Employee", employee, "default_shift") if consider_default_shift else None
	shifts = _Shifts(hrms_shift, employee, today)
	if instant is None:
		found = shifts.assigned_on(day, day) or shifts.default_on(default, day, day)
		found = _ordered(hrms_shift, found)
	else:
		# The instant's own day, the evening before (a night shift still
		# running) and the day after (a check-in margin reaching back past
		# midnight), as HRMS's own lookup does.
		first, last = day - _ONE_DAY, day + _ONE_DAY
		found = _covering(hrms_shift, shifts.assigned_on(first, last), instant)
		if not found:
			found = _covering(hrms_shift, shifts.default_on(default, first, last), instant)
	# HRMS returns plain dicts (frappe._dict), never Documents: no .as_dict().
	found = [dict(s) for s in found]
	out = {
		"shift": found[0] if found else None,
		"shifts": found,
		"employee": employee,
		"for_date": str(day),
	}
	if instant is not None:
		out["for_timestamp"] = instant.isoformat(sep=" ", timespec="seconds")
	return out


def _resolve(for_date, today: date) -> tuple[date, datetime | None]:
	"""``(day, instant or None)``.

	Nothing, or a bare date, is a question about a working DAY (the tool's
	contract: for_date defaults to today), which no single instant answers: a
	22:00-06:00 shift covers neither 09:00 nor noon. A value carrying a time is
	a question about that instant.
	"""
	if for_date is None or (isinstance(for_date, str) and not for_date.strip()):
		return today, None
	if isinstance(for_date, datetime):
		if for_date.tzinfo is None:
			return for_date.date(), for_date
	elif isinstance(for_date, date):
		return for_date, None
	elif isinstance(for_date, str):
		# Strict ISO only, by shape first: a lenient parser reads "03-10-2026"
		# month-first and "0000-00-00" as no date at all, and fromisoformat alone
		# takes "20261003" as midnight (an instant, not a day) and timezone
		# offsets that HRMS's naive comparisons cannot handle.
		text = for_date.strip()
		try:
			if _DATE.fullmatch(text):
				return date.fromisoformat(text), None
			if _DATETIME.fullmatch(text):
				instant = datetime.fromisoformat(text.replace("T", " "))
				return instant.date(), instant
		except ValueError:
			pass
	shown = repr(for_date)
	if len(shown) > 60:
		shown = shown[:57] + "..."
	raise InvalidArgumentError(
		"for_date must be a date YYYY-MM-DD, or a naive site-local datetime YYYY-MM-DD HH:MM[:SS]"
		f" with no timezone offset or Z; got {shown}"
	)


class _Shifts:
	"""Shift occurrences for one employee, as HRMS computes them."""

	def __init__(self, hrms_shift, employee: str, today: date):
		self.hrms_shift = hrms_shift
		self.employee = employee
		self.today = today

	def assigned_on(self, first: date, last: date) -> list:
		"""The assigned shifts starting on each day from ``first`` to ``last``."""
		found = []
		for assignment in self._assignments(first, last):
			for day in _days(max(first, assignment.start_date), min(last, assignment.end_date or last)):
				details = self._occurrence(assignment.shift_type, day, f"Shift Assignment {assignment.name}")
				if "overtime_type" in assignment:
					# version-16: as get_valid_shifts_for_time does, the assignment's
					# overtime type replaces the Shift Type's.
					details.overtime_type = assignment.overtime_type or None
				found.append(details)
		return found

	def default_on(self, default: str | None, first: date, last: date) -> list:
		if not default:
			return []
		source = f"the default shift of Employee {self.employee}"
		return [self._occurrence(default, day, source) for day in _days(first, last)]

	def _assignments(self, first: date, last: date) -> list:
		"""Submitted assignments that applied on some day from ``first`` to ``last``.

		Not HRMS's get_shifts_for_date: that reads status "Active" only, and
		mark_expired_shift_assignments_as_inactive (daily) sets every assignment
		whose end_date is before yesterday to "Inactive", so every past date
		would lose the assignment that applied then.
		"""
		sa = frappe.qb.DocType("Shift Assignment")
		fields = [sa.name, sa.shift_type, sa.start_date, sa.end_date]
		if frappe.get_meta("Shift Assignment").has_field("overtime_type"):
			fields.append(sa.overtime_type)
		expired_by_the_job = sa.end_date.isnotnull() & (sa.end_date < self.today - _ONE_DAY)
		return (
			frappe.qb.from_(sa)
			.select(*fields)
			.where(
				(sa.employee == self.employee)
				& (sa.docstatus == 1)
				& ((sa.status == "Active") | expired_by_the_job)
				& (sa.start_date <= last)
				& (sa.end_date.isnull() | (sa.end_date >= first))
			)
			.run(as_dict=True)
		)

	def _occurrence(self, shift_type: str, day: date, source: str):
		"""The ``shift_type`` shift that starts on ``day``."""
		timing = self.hrms_shift.get_shift_type(shift_type)
		if not timing:
			raise InvalidArgumentError(f"{source} names Shift Type {shift_type!r}, which does not exist")
		# Asked at the shift's own start time, HRMS places the shift on ``day``;
		# asked at any fixed hour it may pick the previous evening's night shift.
		return self.hrms_shift.get_shift_details(
			shift_type, datetime.combine(day, datetime.min.time()) + timing.start_time
		)


def _days(first: date, last: date) -> list[date]:
	return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def _ordered(hrms_shift, shifts: list) -> list:
	"""Ordered by check-in window, with overlapping margins trimmed as HRMS does."""
	shifts = sorted(shifts, key=lambda details: details.actual_start)
	hrms_shift._adjust_overlapping_shifts(shifts)
	return shifts


def _covering(hrms_shift, shifts: list, instant: datetime) -> list:
	return [s for s in _ordered(hrms_shift, shifts) if s.actual_start <= instant <= s.actual_end]
