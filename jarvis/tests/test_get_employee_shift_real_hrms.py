"""get_employee_shift against the real HRMS functions (no mock).

The tool passed a date STRING where HRMS calls ``for_timestamp.date()``, so
every real call raised AttributeError. The earlier test mocked the HRMS
function and asserted the string was passed, which pinned the bug. These call
the real thing, on fixtures they create themselves: no ``_Test Company``, no
setup wizard, no ERPNext / HRMS test helpers (CI's site has none of them).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, nowdate

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.get_employee_shift import get_employee_shift
from jarvis.tools.registry import dispatch

HAS_HRMS = "hrms" in frappe.get_installed_apps()
COMPANY = "Jarvis Shift Test Co"
DAY_SHIFT = "Jarvis Day"
NIGHT_SHIFT = "Jarvis Night"
EVENING_SHIFT = "Jarvis Evening"
NARROW_SHIFT = "Jarvis Narrow Margins"
SHIFT_TYPES = (DAY_SHIFT, NIGHT_SHIFT, EVENING_SHIFT, NARROW_SHIFT)
# A fixed day in the past: every test rolls back, so they cannot see each
# other's rows, and HRMS's expiry job treats it as long over.
DAY = date(2026, 6, 18)
BEFORE = DAY - timedelta(days=1)
AFTER = DAY + timedelta(days=1)


def _at(day: date, hhmm: str) -> str:
	return f"{day} {hhmm}:00"


def _company() -> str:
	if frappe.db.exists("Company", COMPANY):
		return COMPANY
	# Same pattern as test_company_scope.py: skip chart-of-accounts / warehouse /
	# tax-template creation. A site that never ran the setup wizard (CI) lacks the
	# fixtures those hooks link to (e.g. Warehouse Type "Transit").
	frappe.local.flags.ignore_chart_of_accounts = True
	try:
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": COMPANY,
				"abbr": "JSTC",
				"default_currency": "INR",
				"country": "India",
			}
		).insert(ignore_permissions=True)
	finally:
		frappe.local.flags.ignore_chart_of_accounts = False
	return COMPANY


def _gender() -> str:
	# Gender rows come from the setup wizard; a bare site has none.
	name = frappe.db.get_value("Gender", {}, "name")
	if name:
		return name
	return frappe.get_doc({"doctype": "Gender", "gender": "Other"}).insert(ignore_permissions=True).name


def _employee() -> str:
	return (
		frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "JarvisShift",
				"company": _company(),
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"gender": _gender(),
				"status": "Active",
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _shift_type(name: str, start: str, end: str, before: int = 60, after: int = 60) -> str:
	# Margins are set explicitly: their defaults are not part of what is tested.
	frappe.get_doc(
		{
			"doctype": "Shift Type",
			"name": name,
			"start_time": start,
			"end_time": end,
			"begin_check_in_before_shift_start_time": before,
			"allow_check_out_after_shift_end_time": after,
		}
	).insert(ignore_permissions=True)
	return name


def _assign(employee: str, shift_type: str, start_date, end_date, submit: bool = True):
	doc = frappe.get_doc(
		{
			"doctype": "Shift Assignment",
			"employee": employee,
			"company": COMPANY,
			"shift_type": shift_type,
			"start_date": start_date,
			"end_date": end_date,
		}
	).insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc


def _allow_two_shifts_a_day():
	# HRMS refuses a second assignment on a day unless this setting is on.
	frappe.db.set_single_value("HR Settings", "allow_multiple_shift_assignments", 1)


def _name(shift: dict | None) -> str | None:
	return shift["shift_type"]["name"] if shift else None


def _names(out: dict) -> list[str]:
	return [_name(s) for s in out["shifts"]]


class TestGetEmployeeShiftRealHRMS(FrappeTestCase):
	def setUp(self):
		if not HAS_HRMS:
			# CI installs hrms on purpose (ci.yml); there, its absence must fail
			# loudly instead of skipping every test in this module.
			if os.environ.get("CI"):
				self.fail("hrms is not installed on the CI site")
			self.skipTest("hrms is not installed on this site")
		self.employee = _employee()
		self.day = _shift_type(DAY_SHIFT, "09:00:00", "17:00:00")
		self.night = _shift_type(NIGHT_SHIFT, "22:00:00", "06:00:00")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		# The rollback removes the rows but not what HRMS cached about them
		# (get_cached_value on Shift Type, Employee.default_shift).
		for name in SHIFT_TYPES:
			frappe.clear_document_cache("Shift Type", name)
		if getattr(self, "employee", None):
			frappe.clear_document_cache("Employee", self.employee)

	def _default_shift(self, shift_type: str) -> None:
		frappe.db.set_value("Employee", self.employee, "default_shift", shift_type)

	# -- a date: "which shift(s) is the employee on that day" --------------------

	def test_date_only_does_not_raise_and_finds_the_day_shift(self):
		_assign(self.employee, self.day, DAY, DAY)
		out = get_employee_shift(self.employee, str(DAY))
		self.assertEqual(_name(out["shift"]), self.day)
		self.assertEqual(out["shift"]["start_datetime"], datetime(2026, 6, 18, 9, 0))
		self.assertEqual(out["shift"]["end_datetime"], datetime(2026, 6, 18, 17, 0))
		self.assertEqual(out["shifts"], [out["shift"]])
		self.assertEqual(out["employee"], self.employee)
		self.assertEqual(out["for_date"], "2026-06-18")
		# A date is a question about a day; no instant was asked about.
		self.assertNotIn("for_timestamp", out)

	def test_date_only_resolves_the_night_shift_that_starts_that_day(self):
		# No single instant answers this for a 22:00-06:00 shift: it covers
		# neither midnight, 09:00 nor noon of the day it is assigned to.
		_assign(self.employee, self.night, DAY, DAY)
		out = get_employee_shift(self.employee, str(DAY))
		self.assertEqual(_name(out["shift"]), self.night)
		self.assertEqual(out["shift"]["start_datetime"], datetime(2026, 6, 18, 22, 0))
		self.assertEqual(out["shift"]["end_datetime"], datetime(2026, 6, 19, 6, 0))

	def test_date_only_ignores_an_assignment_for_the_day_before(self):
		# Yesterday's night shift is still running at 02:00 today, but it is
		# yesterday's shift: it does not START today.
		_assign(self.employee, self.night, BEFORE, BEFORE)
		out = get_employee_shift(self.employee, str(DAY))
		self.assertIsNone(out["shift"])
		self.assertEqual(out["shifts"], [])

	def test_date_only_ignores_an_assignment_for_the_day_after(self):
		_assign(self.employee, self.day, AFTER, AFTER)
		out = get_employee_shift(self.employee, str(DAY))
		self.assertIsNone(out["shift"])
		self.assertEqual(out["shifts"], [])

	def test_open_ended_assignment_covers_a_later_date(self):
		_assign(self.employee, self.day, BEFORE - timedelta(days=30), None)
		out = get_employee_shift(self.employee, str(DAY))
		self.assertEqual(_name(out["shift"]), self.day)
		self.assertEqual(out["shift"]["start_datetime"], datetime(2026, 6, 18, 9, 0))
		self.assertEqual(_name(get_employee_shift(self.employee, _at(DAY, "10:00"))["shift"]), self.day)

	def test_two_shifts_on_one_day_are_both_listed_earliest_first(self):
		_allow_two_shifts_a_day()
		_assign(self.employee, self.night, DAY, DAY)
		_assign(self.employee, self.day, DAY, DAY)
		out = get_employee_shift(self.employee, str(DAY))
		self.assertEqual(_names(out), [self.day, self.night])
		self.assertEqual(_name(out["shift"]), self.day)

	def test_no_argument_means_today_not_this_instant(self):
		# "Today" is frozen once, for the tool's whole call, so the test cannot
		# straddle midnight; only the no-argument form of getdate is frozen.
		real_getdate = frappe.utils.getdate

		def frozen(*args, **kwargs):
			return DAY if not args and not kwargs else real_getdate(*args, **kwargs)

		_assign(self.employee, self.night, DAY, DAY)
		with patch("frappe.utils.getdate", side_effect=frozen):
			out = get_employee_shift(self.employee)
			blank = get_employee_shift(self.employee, "")
		# The night shift STARTS today, whether or not it covers the current time.
		self.assertEqual(_name(out["shift"]), self.night)
		self.assertEqual(out["shift"]["start_datetime"], datetime(2026, 6, 18, 22, 0))
		self.assertEqual(out["for_date"], "2026-06-18")
		self.assertNotIn("for_timestamp", out)
		self.assertEqual(blank, out)

	def test_cancelled_assignment_is_not_a_shift(self):
		_assign(self.employee, self.day, DAY, DAY).cancel()
		self.assertIsNone(get_employee_shift(self.employee, str(DAY))["shift"])
		self.assertIsNone(get_employee_shift(self.employee, _at(DAY, "10:00"))["shift"])

	def test_draft_assignment_is_not_a_shift(self):
		_assign(self.employee, self.day, DAY, DAY, submit=False)
		self.assertIsNone(get_employee_shift(self.employee, str(DAY))["shift"])
		self.assertIsNone(get_employee_shift(self.employee, _at(DAY, "10:00"))["shift"])

	def test_holidays_are_ignored(self):
		holiday_list = frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": "Jarvis Shift Holidays",
				"from_date": DAY - timedelta(days=10),
				"to_date": DAY + timedelta(days=10),
				"holidays": [{"holiday_date": DAY, "description": "Jarvis test holiday"}],
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("Employee", self.employee, "holiday_list", holiday_list.name)
		_assign(self.employee, self.day, DAY, DAY)
		self.assertEqual(_name(get_employee_shift(self.employee, str(DAY))["shift"]), self.day)
		self.assertEqual(_name(get_employee_shift(self.employee, _at(DAY, "10:00"))["shift"]), self.day)

	# -- past dates: assignments HRMS's daily job has since set to Inactive ------

	def test_a_past_assignment_expired_by_the_hrms_job_still_counts(self):
		from hrms.hr.doctype.shift_assignment.shift_assignment import (
			mark_expired_shift_assignments_as_inactive,
		)

		self._default_shift(self.day)
		assignment = _assign(self.employee, self.night, DAY, DAY)
		# Exactly what the daily scheduler does (hrms/hooks.py, scheduler_events).
		mark_expired_shift_assignments_as_inactive()
		self.assertEqual(frappe.db.get_value("Shift Assignment", assignment.name, "status"), "Inactive")
		self.assertEqual(_names(get_employee_shift(self.employee, str(DAY))), [self.night])
		self.assertEqual(_name(get_employee_shift(self.employee, _at(DAY, "23:00"))["shift"]), self.night)
		self.assertEqual(_name(get_employee_shift(self.employee, _at(AFTER, "02:00"))["shift"]), self.night)

	def test_an_assignment_switched_off_by_hand_before_its_end_does_not_count(self):
		# Its end has not passed, so the expiry job cannot have done this.
		future = getdate(add_days(nowdate(), 5))
		assignment = _assign(self.employee, self.day, future, future)
		assignment.status = "Inactive"
		assignment.save(ignore_permissions=True)
		self.assertIsNone(get_employee_shift(self.employee, str(future))["shift"])
		self.assertIsNone(get_employee_shift(self.employee, _at(future, "10:00"))["shift"])
		open_ended = _assign(self.employee, self.night, future, None)
		open_ended.status = "Inactive"
		open_ended.save(ignore_permissions=True)
		self.assertIsNone(get_employee_shift(self.employee, str(future))["shift"])

	def test_switched_off_by_hand_and_since_ended_counts_like_expired(self):
		# The one case the data cannot tell apart from the expiry job; the
		# docstring states it. Pinned so a change to the rule is deliberate.
		assignment = _assign(self.employee, self.day, DAY, DAY)
		assignment.status = "Inactive"
		assignment.save(ignore_permissions=True)
		self.assertEqual(_name(get_employee_shift(self.employee, str(DAY))["shift"]), self.day)

	# -- no shift at all: a clean empty result, never an error -------------------

	def test_no_assignment_and_no_default_shift_is_a_clean_empty_result(self):
		# consider_default_shift is left at its default (True) on purpose: the
		# employee has no default shift, and that must not raise either.
		for value in (str(DAY), _at(DAY, "10:00"), None):
			with self.subTest(for_date=value):
				out = get_employee_shift(self.employee, value)
				self.assertIsNone(out["shift"])
				self.assertEqual(out["shifts"], [])
				self.assertEqual(out["employee"], self.employee)

	# -- the default shift -------------------------------------------------------

	def test_default_shift_is_used_for_a_date_with_no_assignment(self):
		self._default_shift(self.day)
		out = get_employee_shift(self.employee, str(DAY))
		self.assertEqual(_name(out["shift"]), self.day)
		self.assertEqual(out["shift"]["start_datetime"], datetime(2026, 6, 18, 9, 0))
		off = get_employee_shift(self.employee, str(DAY), consider_default_shift=False)
		self.assertIsNone(off["shift"])
		self.assertEqual(off["shifts"], [])

	def test_an_assignment_that_day_wins_over_the_default_shift(self):
		self._default_shift(self.day)
		_assign(self.employee, self.night, DAY, DAY)
		self.assertEqual(_names(get_employee_shift(self.employee, str(DAY))), [self.night])

	def test_default_shift_at_an_instant_only_inside_its_window(self):
		self._default_shift(self.day)
		# 09:00-17:00 with a 60 minute margin each side: 08:00 to 18:00.
		for hhmm, expected in (("10:00", self.day), ("08:30", self.day), ("18:30", None), ("20:00", None)):
			with self.subTest(at=hhmm):
				self.assertEqual(_name(get_employee_shift(self.employee, _at(DAY, hhmm))["shift"]), expected)
		off = get_employee_shift(self.employee, _at(DAY, "10:00"), consider_default_shift=False)
		self.assertIsNone(off["shift"])

	def test_default_only_night_worker_has_no_shift_at_ten_in_the_morning(self):
		# HRMS's get_employee_shift returns the default shift's details at ANY
		# hour; here that was the 17th 22:00 to the 18th 06:00, over by 10:00.
		self._default_shift(self.night)
		at_ten = get_employee_shift(self.employee, _at(DAY, "10:00"))
		self.assertIsNone(at_ten["shift"])
		self.assertEqual(at_ten["shifts"], [])
		late = get_employee_shift(self.employee, _at(DAY, "23:00"))["shift"]
		self.assertEqual(late["start_datetime"], datetime(2026, 6, 18, 22, 0))
		early = get_employee_shift(self.employee, _at(DAY, "02:00"))["shift"]
		self.assertEqual(early["start_datetime"], datetime(2026, 6, 17, 22, 0))
		self.assertEqual(_names(get_employee_shift(self.employee, str(DAY))), [self.night])

	def test_default_shift_covers_an_instant_no_assignment_covers(self):
		# As HRMS's check-in logic (get_actual_start_end_datetime_of_shift) does:
		# the default is considered when no ASSIGNED shift covers the instant.
		self._default_shift(self.day)
		_assign(self.employee, self.night, DAY, DAY)
		self.assertEqual(_name(get_employee_shift(self.employee, _at(DAY, "10:00"))["shift"]), self.day)
		self.assertEqual(_name(get_employee_shift(self.employee, _at(DAY, "23:00"))["shift"]), self.night)

	def test_an_assigned_shift_covering_the_instant_hides_the_default(self):
		# Both windows contain 11:00; HRMS looks at the default only when no
		# assigned shift covers the instant, so the answer is the assignment alone.
		self._default_shift(self.day)
		late_morning = _shift_type(NARROW_SHIFT, "10:00:00", "14:00:00")
		_assign(self.employee, late_morning, DAY, DAY)
		self.assertEqual(_names(get_employee_shift(self.employee, _at(DAY, "11:00"))), [late_morning])

	# -- a datetime: "which shift covers this instant" ---------------------------

	def test_datetime_answers_which_shift_covers_that_instant(self):
		_assign(self.employee, self.day, DAY, DAY)
		out = get_employee_shift(self.employee, _at(DAY, "10:30"))
		self.assertEqual(_name(out["shift"]), self.day)
		self.assertEqual(out["shifts"], [out["shift"]])
		self.assertEqual(out["for_timestamp"], "2026-06-18 10:30:00")
		self.assertEqual(out["for_date"], "2026-06-18")
		outside = get_employee_shift(self.employee, _at(DAY, "20:00"))
		self.assertIsNone(outside["shift"])
		self.assertEqual(outside["shifts"], [])
		self.assertEqual(outside["for_timestamp"], "2026-06-18 20:00:00")

	def test_check_in_margins_are_part_of_the_window(self):
		_shift_type(NARROW_SHIFT, "09:00:00", "17:00:00", before=30, after=45)
		_assign(self.employee, NARROW_SHIFT, DAY, DAY)
		by_date = get_employee_shift(self.employee, str(DAY))["shift"]
		self.assertEqual(by_date["actual_start"], datetime(2026, 6, 18, 8, 30))
		self.assertEqual(by_date["actual_end"], datetime(2026, 6, 18, 17, 45))
		for hhmm, expected in (
			("08:29", None),
			("08:30", NARROW_SHIFT),
			("08:40", NARROW_SHIFT),
			("17:40", NARROW_SHIFT),
			("17:45", NARROW_SHIFT),
			("17:46", None),
		):
			with self.subTest(at=hhmm):
				self.assertEqual(_name(get_employee_shift(self.employee, _at(DAY, hhmm))["shift"]), expected)

	def test_datetime_after_midnight_finds_the_overnight_shift_of_the_day_before(self):
		_assign(self.employee, self.night, DAY, DAY)
		# 02:00 on the 19th is inside the shift assigned to (and starting on) the 18th.
		out = get_employee_shift(self.employee, _at(AFTER, "02:00"))
		self.assertEqual(_name(out["shift"]), self.night)
		self.assertEqual(out["shift"]["start_datetime"], datetime(2026, 6, 18, 22, 0))
		self.assertEqual(out["for_date"], "2026-06-19")
		# The same wall-clock time a day earlier belongs to no shift: the
		# assignment starts on the 18th, so nothing was running at 02:00 that day.
		self.assertIsNone(get_employee_shift(self.employee, _at(DAY, "02:00"))["shift"])
		# Either side of the day boundary, and the gap before the shift starts.
		self.assertEqual(_name(get_employee_shift(self.employee, f"{DAY} 23:59:59")["shift"]), self.night)
		self.assertEqual(_name(get_employee_shift(self.employee, _at(AFTER, "00:00"))["shift"]), self.night)
		self.assertIsNone(get_employee_shift(self.employee, _at(DAY, "12:00"))["shift"])
		# ...while the date question about the 19th says: no shift starts that day.
		self.assertIsNone(get_employee_shift(self.employee, str(AFTER))["shift"])

	def test_a_check_in_margin_reaching_back_past_midnight(self):
		# A 00:30 start with a 60 minute margin opens check-in at 23:30 the day before.
		early = _shift_type(NARROW_SHIFT, "00:30:00", "08:30:00")
		_assign(self.employee, early, AFTER, AFTER)
		out = get_employee_shift(self.employee, _at(DAY, "23:45"))
		self.assertEqual(out["shift"]["start_datetime"], datetime(2026, 6, 19, 0, 30))
		self.assertEqual(_names(get_employee_shift(self.employee, str(AFTER))), [early])

	def test_a_date_and_an_instant_inside_the_shift_describe_the_same_shift(self):
		assignment = _assign(self.employee, self.day, DAY, DAY)
		if frappe.get_meta("Shift Assignment").has_field("overtime_type"):
			# version-16: HRMS reports the ASSIGNMENT's overtime type, not the
			# Shift Type's. A raw value is enough; nothing here follows the link.
			frappe.db.set_value("Shift Assignment", assignment.name, "overtime_type", "Jarvis OT")
		by_date = get_employee_shift(self.employee, str(DAY))["shift"]
		by_instant = get_employee_shift(self.employee, _at(DAY, "10:30"))["shift"]
		self.assertEqual(by_date, by_instant)
		if "overtime_type" in by_date:
			self.assertEqual(by_date["overtime_type"], "Jarvis OT")

	def test_adjacent_shifts_have_their_margins_trimmed_on_both_paths(self):
		# Day 09:00-17:00 and Evening 17:00-01:00, 60 minute margins each: raw,
		# Day's check-out window (to 18:00) and Evening's check-in (from 16:00)
		# overlap. HRMS splits them at 17:00; both paths must agree.
		_allow_two_shifts_a_day()
		evening = _shift_type(EVENING_SHIFT, "17:00:00", "01:00:00")
		_assign(self.employee, self.day, DAY, DAY)
		_assign(self.employee, evening, DAY, DAY)
		day_shift, evening_shift = get_employee_shift(self.employee, str(DAY))["shifts"]
		self.assertEqual(day_shift["actual_end"], datetime(2026, 6, 18, 17, 0))
		self.assertEqual(evening_shift["actual_start"], datetime(2026, 6, 18, 17, 0))
		at_half_four = get_employee_shift(self.employee, _at(DAY, "16:30"))
		at_half_five = get_employee_shift(self.employee, _at(DAY, "17:30"))
		self.assertEqual(at_half_four["shifts"], [day_shift])
		self.assertEqual(at_half_five["shifts"], [evening_shift])

	# -- for_date parsing --------------------------------------------------------

	def test_a_date_is_accepted_as_a_date_object_or_iso_text(self):
		_assign(self.employee, self.day, DAY, DAY)
		for value in (DAY, "2026-06-18", " 2026-06-18 "):
			with self.subTest(day=value):
				out = get_employee_shift(self.employee, value)
				self.assertEqual((_name(out["shift"]), out["for_date"]), (self.day, "2026-06-18"))
				self.assertNotIn("for_timestamp", out)

	def test_a_datetime_is_accepted_in_each_iso_spelling(self):
		_assign(self.employee, self.day, DAY, DAY)
		for value in (
			datetime(2026, 6, 18, 10, 30),
			"2026-06-18 10:30",
			"2026-06-18T10:30:00",
			"2026-06-18 10:30:00.250000",
		):
			with self.subTest(instant=value):
				out = get_employee_shift(self.employee, value)
				self.assertEqual(out["for_timestamp"], "2026-06-18 10:30:00")
				self.assertEqual(_name(out["shift"]), self.day)

	def test_midnight_is_an_instant_not_a_day(self):
		_assign(self.employee, self.day, DAY, DAY)
		out = get_employee_shift(self.employee, "2026-06-18 00:00:00")
		self.assertEqual(out["for_timestamp"], "2026-06-18 00:00:00")
		self.assertIsNone(out["shift"])

	def test_bad_input_is_a_clean_error(self):
		# Nothing may be guessed at: day-first, zero, compact, week-date and
		# timezone-aware values, a bare time, hour 24, and non-string junk.
		for bad in (
			"not-a-date",
			"10:30:00",
			"18-06-2026",
			"06/18/2026",
			"0000-00-00",
			"2026-13-40",
			"20260618",
			"2026-W25-4",
			"2026-06-18T10:30:00Z",
			"2026-06-18 10:30:00+05:30",
			"2026-06-18 25:00:00",
			"2026-06-18 24:00",
			"2026-06-18 24:00:00",
			"2026-06-18 10",
			"today",
			datetime(2026, 6, 18, 10, 30, tzinfo=timezone.utc),
			20260618,
			0,
			False,
			["2026-06-18"],
		):
			with self.subTest(bad=bad), self.assertRaises(InvalidArgumentError):
				get_employee_shift(self.employee, bad)

	def test_bad_input_error_says_what_is_accepted_and_clips_the_value(self):
		with self.assertRaises(InvalidArgumentError) as caught:
			get_employee_shift(self.employee, "2026-06-18T10:30:00Z")
		message = str(caught.exception)
		self.assertIn("YYYY-MM-DD", message)
		self.assertIn("no timezone offset or Z", message)
		self.assertIn("'2026-06-18T10:30:00Z'", message)
		with self.assertRaises(InvalidArgumentError) as caught:
			get_employee_shift(self.employee, "x" * 5000)
		self.assertNotIn("x" * 61, str(caught.exception))

	# -- broken data and a missing app: clean errors, never AttributeError ------

	def test_an_assignment_whose_shift_type_is_gone_is_a_clean_error(self):
		assignment = _assign(self.employee, self.night, DAY, DAY)
		# Corrupt data only a raw delete makes: the link is still there.
		frappe.db.delete("Shift Type", {"name": self.night})
		frappe.clear_document_cache("Shift Type", self.night)
		for value in (str(DAY), _at(DAY, "23:00")):
			with self.subTest(for_date=value), self.assertRaises(InvalidArgumentError) as caught:
				get_employee_shift(self.employee, value)
			self.assertIn(assignment.name, str(caught.exception))
			self.assertIn(self.night, str(caught.exception))

	def test_a_default_shift_that_is_gone_is_a_clean_error(self):
		frappe.db.set_value("Employee", self.employee, "default_shift", "Jarvis Ghost Shift")
		for value in (str(DAY), _at(DAY, "10:00")):
			with self.subTest(for_date=value), self.assertRaises(InvalidArgumentError) as caught:
				get_employee_shift(self.employee, value)
			self.assertIn("Jarvis Ghost Shift", str(caught.exception))
		# Not considering the default means not reading it.
		self.assertIsNone(get_employee_shift(self.employee, str(DAY), consider_default_shift=False)["shift"])

	def test_hrms_not_installed_is_a_clean_error(self):
		with (
			patch("frappe.get_installed_apps", return_value=["frappe", "erpnext", "jarvis"]),
			self.assertRaises(InvalidArgumentError) as caught,
		):
			get_employee_shift(self.employee, str(DAY))
		self.assertIn("HRMS is not installed", str(caught.exception))

	# -- who may ask (unchanged by the fix; pinned so it stays that way) ---------

	def test_unknown_or_missing_employee_is_refused(self):
		for employee in ("", None, "HR-EMP-DOES-NOT-EXIST"):
			with self.subTest(employee=employee), self.assertRaises(InvalidArgumentError):
				get_employee_shift(employee, str(DAY))

	def test_a_non_string_employee_is_refused(self):
		# A dict would pass frappe.db.exists as a filter set and reach the query.
		for employee in ({"name": self.employee}, [self.employee], 1, "   "):
			with self.subTest(employee=employee), self.assertRaises(InvalidArgumentError):
				get_employee_shift(employee, str(DAY))

	def test_user_without_read_permission_on_the_employee_is_refused(self):
		_assign(self.employee, self.day, DAY, DAY)
		frappe.set_user("Guest")
		for value in (str(DAY), _at(DAY, "10:30")):
			with self.subTest(for_date=value), self.assertRaises(PermissionDeniedError):
				get_employee_shift(self.employee, value)

	# -- the tool layer ----------------------------------------------------------

	def test_dispatched_result_survives_the_tool_layer_serialiser(self):
		_assign(self.employee, self.night, DAY, DAY)
		out = dispatch("get_employee_shift", {"employee": self.employee, "for_date": "2026-06-18"})
		# Real HRMS details carry datetimes and a timedelta start_time.
		parsed = frappe.parse_json(frappe.as_json(out))
		self.assertEqual(parsed["shift"]["shift_type"]["name"], self.night)
		self.assertEqual(parsed["shift"]["start_datetime"], "2026-06-18 22:00:00")
		self.assertEqual(parsed["shift"]["shift_type"]["start_time"], "22:00:00")
		self.assertEqual(len(parsed["shifts"]), 1)
