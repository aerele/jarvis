"""Holiday touches: what one user sees today.

Real ERPNext and HRMS fixtures (Company, Employee, Holiday List, Holiday List Assignment);
no mocks for HRMS. Time is injected as UTC ``now``. Holiday cases skip without HRMS.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from itertools import count
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_system_timezone

from jarvis.holiday_touch import cache, report, resolve, settings, weekly_off

SETTINGS = "Jarvis Settings"
_MISSING = object()
USETT = "Jarvis User Settings"
HAS_HRMS = "hrms" in frappe.get_installed_apps()
IST = "Asia/Kolkata"
DIWALI = date(2026, 11, 8)  # a Sunday: the weekly-off collision date
_ids = count(1)
_COMPANIES = {"India": "Holiday Touch IN Co", "United States": "Holiday Touch US Co"}
_ABBR = {"India": "HTIN", "United States": "HTUS"}


def _now(on: date, hour: int = 6) -> datetime:
	# 06:00 UTC is 11:30 in Kolkata: the same calendar day there.
	return datetime(on.year, on.month, on.day, hour, 0, tzinfo=timezone.utc)


def _company(country: str) -> str:
	name = _COMPANIES[country]
	if frappe.db.exists("Company", name):
		return name
	frappe.local.flags.ignore_chart_of_accounts = True
	try:
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": name,
				"abbr": _ABBR[country],
				"default_currency": "INR" if country == "India" else "USD",
				"country": country,
			}
		).insert(ignore_permissions=True)
	finally:
		frappe.local.flags.ignore_chart_of_accounts = False
	return name


def _gender() -> str:
	name = frappe.db.get_value("Gender", {}, "name")
	if name:
		return name
	return frappe.get_doc({"doctype": "Gender", "gender": "Other"}).insert(ignore_permissions=True).name


def _user(roles=("Jarvis User",), tz=IST) -> str:
	email = f"holiday.touch.{next(_ids)}@example.com"
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": "Holi",
			"send_welcome_email": 0,
			"time_zone": tz,
			"roles": [{"role": r} for r in roles],
		}
	).insert(ignore_permissions=True)
	return email


def _employee(company: str, dob, user: str | None = None) -> str:
	return (
		frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": f"Holi{next(_ids)}",
				"company": company,
				"date_of_birth": dob,
				"date_of_joining": "2020-01-01",
				"gender": _gender(),
				"status": "Active",
				"user_id": user,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _holiday_list(rows, country=None, subdivision=None, allow_duplicates=False) -> str:
	"""``rows`` are (date, description, weekly_off). ``allow_duplicates`` skips ERPNext's
	"date is repeated" validation, to build the same-date rows the matcher must still handle."""
	doc = frappe.get_doc(
		{
			"doctype": "Holiday List",
			"holiday_list_name": f"Holiday Touch List {next(_ids)}",
			"from_date": "2025-12-01",
			"to_date": "2027-12-31",
			"country": country,
			"subdivision": subdivision,
			"holidays": [
				{"holiday_date": d, "description": text, "weekly_off": off} for d, text, off in rows
			],
		}
	)
	doc.flags.ignore_validate = allow_duplicates
	return doc.insert(ignore_permissions=True).name


def _assign(employee: str, company: str, holiday_list: str) -> None:
	frappe.get_doc(
		{
			"doctype": "Holiday List Assignment",
			"applicable_for": "Employee",
			"assigned_to": employee,
			"employee_company": company,
			"holiday_list": holiday_list,
			"from_date": "2025-12-01",
		}
	).insert(ignore_permissions=True).submit()


LOG_KINDS = ("answer", "holiday", "rotate", "policy_persist", "policy_malformed", "policy_designs")


def clear_log_markers() -> None:
	"""The hourly log markers outlive a test's rollback and ``clear_cache``: clear every kind."""
	for kind in LOG_KINDS:
		frappe.cache().delete_value(report.PREFIX + kind)


def flush_error_logs() -> None:
	"""Write the deferred Error Log rows now (the scheduler does this in production)."""
	frappe.deferred_insert.save_to_db()


def _set(field: str, value) -> None:
	frappe.db.set_single_value(SETTINGS, field, value)
	frappe.db.value_cache.pop(SETTINGS, None)


class _Base(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		weekly_off._year_map.cache_clear()
		# The cache outlives frappe.clear_cache() and a test's rollback: start every test on a
		# fresh generation and a clear hourly log marker.
		cache.rotate()
		clear_log_markers()
		for field in ("holiday_touches_enabled", "birthday_greetings_enabled"):
			_set(field, 1)
		for field in ("holiday_policy_disabled", "holiday_policy_disabled_designs", "brand_logo"):
			_set(field, None)
		_set("agent_name", "Jarvis")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.value_cache.pop(SETTINGS, None)
		clear_log_markers()  # a test must not mute the site's real logs
		for name in _COMPANIES.values():
			frappe.clear_document_cache("Company", name)

	def person(
		self,
		rows=(),
		country="India",
		dob="1990-05-05",
		list_country=None,
		subdivision=None,
		tz=IST,
		allow_duplicates=False,
	):
		"""A Jarvis user with an Active Employee and (when ``rows``) a Holiday List."""
		company = _company(country)
		user = _user(tz=tz)
		employee = _employee(company, dob, user)
		if rows:
			_assign(employee, company, _holiday_list(rows, list_country, subdivision, allow_duplicates))
		return user

	def kinds(self, user, days):
		return [(d.day, (a := resolve.answer(user, _now(d)))["kind"], a.get("scene")) for d in days]


def _days(first, last, month=11, year=2026):
	return [date(year, month, n) for n in range(first, last + 1)]


class TestZoneAndToday(_Base):
	def test_valid_zone_is_used_and_sent(self):
		user = _user(tz="Pacific/Pago_Pago")
		self.assertEqual(resolve.zone_for(user), "Pacific/Pago_Pago")

	def test_invalid_or_blank_zone_falls_back_to_system_zone(self):
		user = _user()
		for bad in ("Not/AZone", "", "../etc", "America"):
			frappe.db.set_value("User", user, "time_zone", bad)
			self.assertEqual(resolve.zone_for(user), get_system_timezone(), bad)

	def test_zones_straddling_midnight_give_different_dates(self):
		west, east = _user(tz="Pacific/Pago_Pago"), _user(tz="Pacific/Kiritimati")
		now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
		self.assertEqual(resolve.answer(west, now)["date"], "2026-11-08")
		payload = resolve.answer(east, now)
		self.assertEqual((payload["date"], payload["tz"]), ("2026-11-09", "Pacific/Kiritimati"))

	def test_naive_now_is_read_as_utc(self):
		out = resolve.answer(_user(tz="UTC"), datetime(2026, 1, 2, 23, 0))
		self.assertEqual(out["date"], "2026-01-02")

	def test_an_invalid_system_zone_falls_back_to_utc(self):
		user = _user()
		frappe.db.set_value("User", user, "time_zone", "Not/AZone")
		for bad in ("Also/Bad", "", None):
			with mock.patch.object(resolve, "get_system_timezone", return_value=bad):
				self.assertEqual(resolve.zone_for(user), "UTC", bad)
				self.assertEqual(resolve.answer(user, _now(DIWALI))["tz"], "UTC", bad)
		with mock.patch.object(resolve, "get_system_timezone", side_effect=RuntimeError("no zone")):
			self.assertEqual(resolve.zone_for(user), "UTC")

	def test_a_valid_system_zone_is_used_when_the_user_zone_is_bad(self):
		user = _user()
		frappe.db.set_value("User", user, "time_zone", "Not/AZone")
		with mock.patch.object(resolve, "get_system_timezone", return_value="Asia/Tokyo"):
			self.assertEqual(resolve.zone_for(user), "Asia/Tokyo")


class TestHolidays(_Base):
	def setUp(self):
		if not HAS_HRMS:
			if os.environ.get("CI"):
				self.fail("hrms is not installed on the CI site")
			self.skipTest("hrms is not installed on this site")
		super().setUp()

	def test_leadup_is_days_one_to_three_only(self):
		user = self.person([(DIWALI, "Diwali", 0)])
		got = {d: k for d, k, _ in self.kinds(user, _days(3, 9))}
		self.assertEqual(
			got, {3: "none", 4: "none", 5: "leadup", 6: "leadup", 7: "leadup", 8: "day", 9: "none"}
		)

	def test_day_payload_has_the_contract_shape(self):
		user = self.person([(DIWALI, "Diwali", 0)])
		self.assertEqual(
			resolve.answer(user, _now(DIWALI)),
			{
				"v": 1,
				"date": "2026-11-08",
				"tz": IST,
				"kind": "day",
				"design": "diwali",
				"occasion": "diwali",
				"year": None,
				"worn": True,
				"scene": True,
				"may_play": True,
			},
		)

	def test_none_payload_is_just_the_four_keys(self):
		user = self.person([(DIWALI, "Diwali", 0)])
		self.assertEqual(
			resolve.answer(user, _now(date(2026, 11, 1))),
			{"v": 1, "date": "2026-11-01", "tz": IST, "kind": "none"},
		)

	def test_republic_keeps_its_own_occasion_with_the_independence_design(self):
		user = self.person([(date(2026, 1, 26), "Republic Day", 0)])
		out = resolve.answer(user, _now(date(2026, 1, 26)))
		self.assertEqual((out["design"], out["occasion"]), ("independence", "republic"))

	def test_new_year_carries_the_year_of_the_matched_date(self):
		user = self.person([(date(2027, 1, 1), "New Year's Day", 0)])
		self.assertEqual(resolve.answer(user, _now(date(2027, 1, 1)))["year"], 2027)
		lead = resolve.answer(user, _now(date(2026, 12, 30)))
		self.assertEqual((lead["kind"], lead["year"]), ("leadup", 2027))

	def test_weekly_off_sunday_row_with_an_india_list_is_diwali(self):
		user = self.person([(DIWALI, "Sunday", 1)], list_country="India")
		out = resolve.answer(user, _now(DIWALI))
		self.assertEqual((out["kind"], out["design"]), ("day", "diwali"))

	def test_same_weekly_off_row_for_a_us_company_is_none(self):
		user = self.person([(DIWALI, "Sunday", 1)], country="United States", list_country="United States")
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")

	def test_list_without_a_country_uses_the_company_country(self):
		user = self.person([(DIWALI, "Sunday", 1)], country="India")
		self.assertEqual(resolve.answer(user, _now(DIWALI))["design"], "diwali")

	def test_a_sunday_row_that_is_not_a_weekly_off_is_none(self):
		user = self.person([(DIWALI, "Sunday", 0)], list_country="India")
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")

	def test_tamil_nadu_list_gives_pongal_on_a_weekly_off_14_january(self):
		user = self.person([(date(2026, 1, 14), "Sunday", 1)], list_country="India", subdivision="TN")
		out = resolve.answer(user, _now(date(2026, 1, 14)))
		self.assertEqual((out["kind"], out["design"]), ("day", "pongal"))

	def test_a_failing_holidays_package_is_silently_none(self):
		user = self.person([(DIWALI, "Sunday", 1)], list_country="India")
		with mock.patch.object(weekly_off, "_year_map", side_effect=ImportError("no holidays")):
			self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")

	def test_a_hindi_or_marathi_request_still_gives_diwali(self):
		user = self.person([(DIWALI, "Sunday", 1)], list_country="India")
		for lang in ("hi", "mr"):
			frappe.local.lang = lang
			try:
				self.assertEqual(resolve.answer(user, _now(DIWALI))["design"], "diwali", lang)
			finally:
				frappe.local.lang = "en"

	def test_maharashtra_diwali_run(self):
		# Diwali on the Sunday weekly-off, Bali Pratipada two days later.
		rows = [(DIWALI, "Sunday", 1), (date(2026, 11, 10), "Diwali (Bali Pratipada)", 0)]
		user = self.person(rows, list_country="India", subdivision="MH")
		self.assertEqual(
			self.kinds(user, _days(4, 11)),
			[
				(4, "none", None),
				(5, "leadup", False),
				(6, "leadup", False),
				(7, "leadup", False),
				(8, "day", True),
				(9, "none", None),  # lookback: no "in advance" right after the day
				(10, "day", False),  # a second day of the run: words, no scene
				(11, "none", None),
			],
		)

	def test_lookback_is_three_days_for_the_leadup(self):
		# Diwali rows on 6 and 10 Nov: 9 Nov is a lead-up to the 10th, but the 6th was 3 days
		# back, so it is not wished "in advance". A 2-day lookback would show a lead-up.
		rows = [(date(2026, 11, 6), "Diwali", 0), (date(2026, 11, 10), "Diwali", 0)]
		user = self.person(rows)
		self.assertEqual(resolve.answer(user, _now(date(2026, 11, 9)))["kind"], "none")
		self.assertEqual(resolve.occasion(user, _now(date(2026, 11, 9)))["reason"], "lookback")

	def test_first_day_flag_looks_back_three_days(self):
		# A Diwali row 3 days before: the day itself is a later day of the run, no scene.
		rows = [(date(2026, 11, 7), "Diwali", 0), (date(2026, 11, 10), "Diwali", 0)]
		user = self.person(rows)
		out = resolve.answer(user, _now(date(2026, 11, 10)))
		self.assertEqual((out["kind"], out["scene"]), ("day", False))

	def test_employee_with_no_holiday_list_has_the_no_list_reason(self):
		user = self.person()
		self.assertEqual(resolve.occasion(user, _now(DIWALI))["reason"], "no_list")

	def test_a_list_with_no_row_in_the_window_is_no_rows(self):
		user = self.person([(date(2026, 3, 3), "Diwali", 0)])
		self.assertEqual(resolve.occasion(user, _now(DIWALI))["reason"], "no_rows")

	def test_rows_in_the_window_that_name_no_occasion_are_no_match(self):
		user = self.person([(DIWALI, "Company Offsite", 0)])
		self.assertEqual(resolve.occasion(user, _now(DIWALI))["reason"], "no_match")

	def test_multi_day_run_plays_the_scene_on_the_first_day_only(self):
		rows = [(date(2026, 1, 14), "Pongal", 0), (date(2026, 1, 15), "Mattu Pongal", 0)]
		user = self.person(rows)
		got = self.kinds(user, [date(2026, 1, 14), date(2026, 1, 15)])
		self.assertEqual(got, [(14, "day", True), (15, "day", False)])

	def test_two_rows_on_one_date_naming_different_designs_are_a_tie(self):
		# Two separate Holiday rows on 8 Nov, a Diwali one and an Eid one (ERPNext refuses a
		# repeated date inside one list, so the validation is skipped to build them): the
		# matcher must still give none, for the day and for the lead-up two days before.
		rows = [(DIWALI, "Diwali", 0), (DIWALI, "Eid ul Fitr", 0)]
		user = self.person(rows, allow_duplicates=True)
		for on in (DIWALI, date(2026, 11, 6)):
			out = resolve.occasion(user, _now(on))
			self.assertEqual((out["kind"], out["reason"]), ("none", "tie"), on)
			self.assertEqual(resolve.answer(user, _now(on))["kind"], "none", on)

	def test_same_date_tie_gives_none_on_the_day_and_for_the_leadup(self):
		user = self.person([(DIWALI, "Diwali and Eid ul Fitr", 0)])
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")
		self.assertEqual(resolve.answer(user, _now(date(2026, 11, 6)))["kind"], "none")

	def test_nearest_leadup_wins(self):
		rows = [(date(2026, 11, 8), "Diwali", 0), (date(2026, 11, 6), "Eid ul Fitr", 0)]
		user = self.person(rows)
		out = resolve.answer(user, _now(date(2026, 11, 5)))
		self.assertEqual((out["kind"], out["design"]), ("leadup", "eid"))

	def test_no_holiday_list_is_none(self):
		self.assertEqual(resolve.answer(self.person(), _now(DIWALI))["kind"], "none")

	def test_hrms_15_adapter_reads_the_list_and_keeps_weekly_off_and_parent(self):
		user = self.person([(DIWALI, "Sunday", 1)], list_country="India")
		real = frappe.db.exists

		def no_assignments(doctype, *args, **kwargs):
			if doctype == "DocType" and args and args[0] == "Holiday List Assignment":
				return None
			return real(doctype, *args, **kwargs)

		with mock.patch.object(frappe.db, "exists", side_effect=no_assignments):
			out = resolve.answer(user, _now(DIWALI))
		self.assertEqual((out["kind"], out["design"]), ("day", "diwali"))

	def _public_stand_in(self):
		"""``get_holidays_for_employee`` built from the real internal calls, captured before a
		test breaks them (the real public function imports the same internal module)."""
		from hrms.utils import holiday_list as internal

		ranges_for, rows_in = internal.get_holiday_list_ranges_for_employee, internal.get_holidays_in_ranges

		def public(employee, start, end, raise_exception=True, only_non_weekly=False):
			return rows_in(
				ranges_for(employee, start, end, raise_exception), skip_weekly_offs=only_non_weekly
			)

		return mock.MagicMock(side_effect=public)

	def _assert_public_fallback_used(self, user, public):
		with mock.patch("hrms.hr.utils.get_holidays_for_employee", public):
			out = resolve.answer(user, _now(DIWALI))
		self.assertEqual((out["kind"], out["design"]), ("day", "diwali"))
		self.assertEqual(public.call_args.kwargs, {"raise_exception": False, "only_non_weekly": False})

	def test_a_missing_internal_hrms_module_falls_back_to_the_public_function(self):
		user = self.person([(DIWALI, "Sunday", 1)], list_country="India")
		public = self._public_stand_in()
		with mock.patch.dict("sys.modules", {"hrms.utils.holiday_list": None}):
			self._assert_public_fallback_used(user, public)

	def test_an_internal_signature_change_falls_back_to_the_public_function(self):
		user = self.person([(DIWALI, "Sunday", 1)], list_country="India")
		public = self._public_stand_in()
		with mock.patch("hrms.utils.holiday_list.get_holidays_in_ranges", side_effect=TypeError("sig")):
			self._assert_public_fallback_used(user, public)

	def test_a_failing_holiday_lookup_keeps_the_birthday_and_logs_once(self):
		user = self.person(dob="1990-11-08")
		other = self.person(dob="1990-11-08")
		title = "Holiday touch: holiday lookup failed"
		flush_error_logs()
		before = frappe.db.count("Error Log", {"method": title})
		with mock.patch.object(resolve, "_holiday_rows", side_effect=RuntimeError("boom")):
			out = resolve.answer(user, _now(DIWALI))
			resolve.answer(other, _now(DIWALI))
		flush_error_logs()
		self.assertEqual((out["kind"], out["design"]), ("birthday", "birthday"))
		self.assertEqual(frappe.db.count("Error Log", {"method": title}), before + 1)

	def test_a_failing_holiday_list_region_lookup_keeps_the_weekly_off_answer(self):
		user = self.person([(DIWALI, "Sunday", 1)])  # a list with no country: the Company's (India) is used
		real = frappe.db.get_value

		def flaky(doctype, *args, **kwargs):
			if doctype == "Holiday List":
				raise RuntimeError("db hiccup")
			return real(doctype, *args, **kwargs)

		with mock.patch.object(frappe.db, "get_value", side_effect=flaky):
			self.assertEqual(resolve._list_region("any"), (None, None))
			out = resolve.occasion(user, _now(DIWALI))
		self.assertEqual((out["kind"], out["design"]), ("day", "diwali"))


class TestBirthday(_Base):
	def test_birthday_today_in_the_users_zone(self):
		user = self.person(dob="1990-11-08")
		out = resolve.answer(user, _now(DIWALI))
		self.assertEqual(
			(out["kind"], out["design"], out["occasion"], out["scene"]),
			("birthday", "birthday", "birthday", True),
		)
		self.assertEqual(resolve.answer(user, _now(date(2026, 11, 9)))["kind"], "none")

	def test_29_feb_is_celebrated_on_28_feb_in_a_non_leap_year(self):
		user = self.person(dob="1992-02-29")
		self.assertEqual(resolve.answer(user, _now(date(2026, 2, 28)))["kind"], "birthday")
		self.assertEqual(resolve.answer(user, _now(date(2028, 2, 29)))["kind"], "birthday")
		self.assertEqual(resolve.answer(user, _now(date(2028, 2, 28)))["kind"], "none")

	def test_five_sharing_a_date_is_a_placeholder(self):
		company = _company("India")
		for _ in range(4):
			_employee(company, "1990-11-08")
		user = self.person(dob="1990-11-08")
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")
		self.assertEqual(resolve.occasion(user, _now(DIWALI))["reason"], "placeholder_dob")

	def test_four_sharing_a_date_is_a_birthday(self):
		company = _company("India")
		for _ in range(3):
			_employee(company, "1990-11-08")
		user = self.person(dob="1990-11-08")
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "birthday")

	def test_a_changed_birth_date_lifts_the_placeholder_for_the_others(self):
		# Five Active Employees share 8 Nov, so it is a dummy date and nobody gets a birthday.
		# One of them corrects their date; the other four now share it, which is a real
		# birthday. The first answer was cached: the save's rotation must reach the others.
		company = _company("India")
		others = [_employee(company, "1990-11-08") for _ in range(4)]
		user = self.person(dob="1990-11-08")
		frappe.db.after_commit.run()  # an earlier commit: the setup's own rotations are done
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")
		self.assertEqual(resolve.occasion(user, _now(DIWALI))["reason"], "placeholder_dob")
		doc = frappe.get_doc("Employee", others[0])
		doc.date_of_birth = "1985-01-01"
		doc.save(ignore_permissions=True)
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")  # not committed yet
		frappe.db.after_commit.run()
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "birthday")

	def test_placeholder_set_holds_dates_only(self):
		company = _company("India")
		for _ in range(5):
			_employee(company, "1990-11-08")
		self.assertEqual(resolve._placeholder_dates(company), {date(1990, 11, 8)})

	def test_inactive_employee_is_none(self):
		user = self.person(dob="1990-11-08")
		frappe.db.set_value("Employee", {"user_id": user}, "status", "Inactive")
		out = resolve.occasion(user, _now(DIWALI))
		self.assertEqual((out["kind"], out["reason"]), ("none", "no_employee"))

	def test_user_without_an_employee_is_none(self):
		out = resolve.occasion(_user(), _now(DIWALI))
		self.assertEqual((out["kind"], out["reason"]), ("none", "no_employee"))

	def test_latest_modified_active_employee_wins(self):
		company = _company("India")
		user = _user()
		old = _employee(company, "1990-01-01")
		new = _employee(company, "1990-11-08")
		for name, modified in ((old, "2026-01-01 10:00:00"), (new, "2026-02-01 10:00:00")):
			frappe.db.set_value("Employee", name, {"user_id": user}, update_modified=False)
			frappe.db.set_value("Employee", name, "modified", modified, update_modified=False)
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "birthday")

	def test_no_employee_doctype_is_none_without_an_error_log(self):
		user = self.person(dob="1990-11-08")
		with (
			mock.patch.object(frappe.db, "table_exists", return_value=False),
			mock.patch("frappe.log_error") as log,
		):
			out = resolve.safe_answer(user, _now(DIWALI))
			self.assertEqual(resolve.occasion(user, _now(DIWALI))["reason"], "no_employee_doctype")
		self.assertEqual(out["kind"], "none")
		log.assert_not_called()

	def test_without_hrms_birthdays_still_work_and_holidays_give_none(self):
		bday = self.person(dob="1990-11-08")
		other = self.person([(DIWALI, "Diwali", 0)]) if HAS_HRMS else self.person()
		with mock.patch("frappe.get_installed_apps", return_value=["frappe", "erpnext", "jarvis"]):
			self.assertEqual(resolve.answer(bday, _now(DIWALI))["kind"], "birthday")
			self.assertEqual(resolve.answer(other, _now(DIWALI))["kind"], "none")
			self.assertEqual(resolve.occasion(other, _now(DIWALI))["reason"], "no_hrms")

	def test_no_jarvis_access_is_none_and_runs_no_employee_query(self):
		user = self.person(dob="1990-11-08")
		frappe.db.delete("Has Role", {"parent": user, "role": "Jarvis User"})
		frappe.clear_cache(user=user)
		real, tables = frappe.db.get_value, []

		def spy(doctype, *args, **kwargs):
			tables.append(doctype)
			return real(doctype, *args, **kwargs)

		with mock.patch.object(frappe.db, "get_value", side_effect=spy):
			out = resolve.occasion(user, _now(DIWALI))
			payload = resolve.answer(user, _now(DIWALI))
		self.assertEqual((out["kind"], out["reason"]), ("none", "no_access"))
		self.assertEqual(payload["kind"], "none")
		self.assertNotIn("Employee", tables)

	def test_guest_is_none(self):
		for who in ("Guest", None, ""):
			self.assertEqual(resolve.occasion(who, _now(DIWALI))["kind"], "none")
			self.assertEqual(resolve.answer(who, _now(DIWALI))["kind"], "none")


class TestSwitchesAndPolicy(_Base):
	def setUp(self):
		super().setUp()
		self.user = self.person(dob="1990-05-05")

	def _holiday_user(self, **kw):
		if not HAS_HRMS:
			self.skipTest("hrms is not installed on this site")
		return self.person([(DIWALI, "Diwali", 0)], **kw)

	def test_each_switch_turns_everything_off_on_the_next_read(self):
		user = self._holiday_user()
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "day")
		_set("holiday_touches_enabled", 0)
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")
		_set("holiday_touches_enabled", 1)
		_set("holiday_policy_disabled", 1)
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")
		_set("holiday_policy_disabled", 0)
		frappe.get_doc({"doctype": USETT, "user": user, "show_holiday_touches": 0}).insert(
			ignore_permissions=True
		)
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")

	def test_site_switch_off_also_stops_birthdays(self):
		user = self.person(dob="1990-11-08")
		_set("holiday_touches_enabled", 0)
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")

	def test_birthday_switch_affects_only_birthdays(self):
		_set("birthday_greetings_enabled", 0)
		bday = self.person(dob="1990-11-08")
		self.assertEqual(resolve.answer(bday, _now(DIWALI))["kind"], "none")
		user = self._holiday_user()
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "day")

	def test_birthday_wins_over_a_holiday_and_the_holiday_shows_when_it_is_off(self):
		user = self._holiday_user(dob="1990-11-08")
		self.assertEqual(resolve.answer(user, _now(DIWALI))["design"], "birthday")
		_set("birthday_greetings_enabled", 0)
		self.assertEqual(resolve.answer(user, _now(DIWALI))["design"], "diwali")

	def test_placeholder_birthday_falls_through_to_the_holiday(self):
		company = _company("India")
		for _ in range(4):
			_employee(company, "1990-11-08")
		user = self._holiday_user(dob="1990-11-08")
		self.assertEqual(resolve.answer(user, _now(DIWALI))["design"], "diwali")

	def test_a_missing_settings_row_means_defaults(self):
		user = self._holiday_user()
		self.assertFalse(frappe.db.exists(USETT, user))
		self.assertTrue(resolve.answer(user, _now(DIWALI))["may_play"])

	def test_may_play_follows_the_stored_showings(self):
		user = self._holiday_user()
		shown = {"date": "2026-11-08", "times": ["2026-11-08T04:30:00+00:00"]}
		frappe.get_doc({"doctype": USETT, "user": user, "holiday_touch_shown": json.dumps(shown)}).insert(
			ignore_permissions=True
		)
		self.assertFalse(resolve.answer(user, _now(DIWALI))["may_play"])
		self.assertTrue(resolve.answer(user, _now(DIWALI, hour=10))["may_play"])  # 15:30 IST, 3h+ later

	def test_unreadable_shown_state_is_ignored(self):
		user = self._holiday_user()
		frappe.get_doc({"doctype": USETT, "user": user, "holiday_touch_shown": "{not json"}).insert(
			ignore_permissions=True
		)
		self.assertTrue(resolve.answer(user, _now(DIWALI))["may_play"])

	def test_policy_disabled_designs(self):
		user = self._holiday_user()
		_set("holiday_policy_disabled_designs", json.dumps(["eid", "diwali"]))
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")
		_set("holiday_policy_disabled_designs", json.dumps(["eid"]))
		self.assertEqual(resolve.answer(user, _now(DIWALI))["design"], "diwali")

	def test_malformed_disabled_designs_block_nothing(self):
		user = self._holiday_user()
		for raw in ("{not json", '"diwali"', "[1, null]"):
			_set("holiday_policy_disabled_designs", raw)
			self.assertEqual(resolve.answer(user, _now(DIWALI))["design"], "diwali", raw)

	def test_a_deeply_nested_disabled_designs_value_blocks_nothing(self):
		user = self.person(dob="1990-11-08")
		deep = "[" * 200000
		self.assertEqual(settings.blocked_designs({"holiday_policy_disabled_designs": deep}), frozenset())
		_set("holiday_policy_disabled_designs", deep)
		self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "birthday")

	def test_policy_disabled_is_read_like_the_other_switches(self):
		user = self.person(dob="1990-11-08")
		for raw, kind in ((1, "none"), ("0", "birthday"), (None, "birthday")):
			_set("holiday_policy_disabled", raw)
			self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], kind, raw)

	def test_one_answer_reads_the_site_settings_and_the_user_zone_once(self):
		user = self.person(dob="1990-11-08")
		with (
			mock.patch.object(frappe.db, "get_singles_dict", wraps=frappe.db.get_singles_dict) as singles,
			mock.patch.object(frappe.db, "get_value", wraps=frappe.db.get_value) as get_value,
		):
			self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "birthday")
		self.assertEqual(singles.call_count, 1)
		zone_reads = [
			c for c in get_value.call_args_list if c.args[:1] == ("User",) and "time_zone" in c.args
		]
		self.assertEqual(len(zone_reads), 1, zone_reads)

	def test_a_disabled_birthday_design_falls_back_to_the_holiday(self):
		user = self._holiday_user(dob="1990-11-08")
		_set("holiday_policy_disabled_designs", json.dumps(["birthday"]))
		self.assertEqual(resolve.answer(user, _now(DIWALI))["design"], "diwali")

	def test_white_label_keeps_the_words_but_drops_worn_and_scene(self):
		user = self._holiday_user()
		for field, value in (("brand_logo", "/files/logo.png"), ("agent_name", "Acme Assistant")):
			_set("brand_logo", None)
			_set("agent_name", "Jarvis")
			_set(field, value)
			out = resolve.answer(user, _now(DIWALI))
			self.assertEqual((out["kind"], out["design"]), ("day", "diwali"), field)
			self.assertEqual((out["worn"], out["scene"]), (False, False), field)

	def test_default_or_blank_agent_name_is_not_white_label(self):
		user = self._holiday_user()
		for name in ("Jarvis", " Jarvis ", "", None):
			_set("agent_name", name)
			out = resolve.answer(user, _now(DIWALI))
			self.assertEqual((out["worn"], out["scene"]), (True, True), repr(name))

	def test_white_label_birthday_has_no_scene(self):
		_set("agent_name", "Acme Assistant")
		user = self.person(dob="1990-11-08")
		out = resolve.answer(user, _now(DIWALI))
		self.assertEqual((out["kind"], out["worn"], out["scene"]), ("birthday", False, False))

	def test_leadup_never_has_a_scene(self):
		user = self._holiday_user()
		out = resolve.answer(user, _now(date(2026, 11, 6)))
		self.assertEqual((out["kind"], out["worn"], out["scene"]), ("leadup", True, False))


class TestPrivacyAndFailSafe(_Base):
	def test_payload_never_carries_the_dob_or_the_description(self):
		secret = "Quokka Special Festival Note"
		user = self.person([(DIWALI, f"Diwali {secret}", 0)] if HAS_HRMS else (), dob="1990-11-08")
		for on in (date(2026, 11, 5), DIWALI):
			text = json.dumps(resolve.answer(user, _now(on)))
			for banned in ("1990", "date_of_birth", "Quokka", secret, "fallback", "reason"):
				self.assertNotIn(banned, text)

	def test_safe_answer_returns_none_when_occasion_raises(self):
		user = self.person(dob="1990-11-08")
		with mock.patch.object(resolve, "occasion", side_effect=RuntimeError("boom")):
			self.assertEqual(
				resolve.safe_answer(user, _now(DIWALI)),
				{"v": 1, "date": "2026-11-08", "tz": IST, "kind": "none"},
			)

	def test_error_is_logged_with_title_and_a_message_without_locals(self):
		user = self.person(dob="1990-11-08")
		with (
			mock.patch.object(resolve, "_birthday_state", side_effect=RuntimeError("boom")),
			mock.patch("frappe.log_error") as log,
		):
			resolve.safe_answer(user, _now(DIWALI))
		kwargs = log.call_args.kwargs
		self.assertEqual(kwargs["title"], "Holiday touch: answer failed")
		self.assertIn("RuntimeError", kwargs["message"])
		self.assertNotIn("1990-11-08", kwargs["message"])

	def test_error_creates_an_error_log_row(self):
		user = self.person(dob="1990-11-08")
		title = "Holiday touch: answer failed"
		flush_error_logs()
		before = frappe.db.count("Error Log", {"method": title})
		with mock.patch.object(resolve, "occasion", side_effect=RuntimeError("boom")):
			resolve.safe_answer(user, _now(DIWALI))
		flush_error_logs()
		self.assertEqual(frappe.db.count("Error Log", {"method": title}), before + 1)

	def test_in_developer_mode_the_error_log_row_still_holds_no_birth_date(self):
		# Frappe appends the frame's local variables to a traceback in developer mode. The
		# failing frame (_birthday_state) holds the birth date in a local, so the logged
		# traceback must be written without locals.
		secret = "1987-03-21"
		user = self.person(dob=secret)
		title = "Holiday touch: answer failed"
		conf = frappe.conf.get("developer_mode", _MISSING)
		frappe.conf.developer_mode = 1
		try:
			with mock.patch.object(resolve, "_is_today", side_effect=RuntimeError("boom")):
				resolve.safe_answer(user, _now(date(2026, 3, 21)))
			flush_error_logs()
		finally:
			if conf is _MISSING:
				frappe.conf.pop("developer_mode", None)
			else:
				frappe.conf.developer_mode = conf
		row = frappe.get_all(
			"Error Log", {"method": title}, ["error", "method"], order_by="creation desc", limit=1
		)[0]
		self.assertIn("RuntimeError: boom", row.error)
		for banned in (secret, "1987", "date_of_birth"):
			self.assertNotIn(banned, row.error)

	def test_the_none_payload_has_one_shape_everywhere(self):
		from jarvis.holiday_touch import pick

		user = self.person(dob="1990-11-08")
		expected = pick.none_payload(IST, DIWALI)
		self.assertEqual(expected, {"v": 1, "date": "2026-11-08", "tz": IST, "kind": "none"})
		with mock.patch.object(resolve, "occasion", side_effect=RuntimeError("boom")):
			self.assertEqual(resolve.safe_answer(user, _now(DIWALI)), expected)
		guest = resolve.answer("Guest", _now(DIWALI))
		self.assertEqual((set(guest), guest["kind"]), ({"v", "date", "tz", "kind"}, "none"))

	def test_even_a_failing_logger_cannot_break_safe_answer(self):
		user = self.person()
		with (
			mock.patch.object(resolve, "occasion", side_effect=RuntimeError("boom")),
			mock.patch("frappe.log_error", side_effect=RuntimeError("log down")),
		):
			self.assertEqual(resolve.safe_answer(user, _now(DIWALI))["kind"], "none")

	def test_pre_migrate_switches_that_cannot_be_read_mean_none(self):
		user = self.person(dob="1990-11-08")
		with mock.patch.object(frappe.db, "get_singles_dict", side_effect=Exception("no table")):
			self.assertEqual(resolve.safe_answer(user, _now(DIWALI))["kind"], "none")

	def _meta_without_fields(self, *doctypes):
		real = frappe.get_meta

		def meta(doctype, *args, **kwargs):
			if doctype in doctypes:
				return mock.Mock(has_field=lambda field: False)
			return real(doctype, *args, **kwargs)

		return mock.patch("frappe.get_meta", side_effect=meta)

	def test_a_missing_switch_field_means_none_and_leaves_no_message(self):
		user = self.person(dob="1990-11-08")
		before = list(frappe.local.message_log)
		with self._meta_without_fields(SETTINGS, USETT):
			self.assertEqual(resolve.answer(user, _now(DIWALI))["kind"], "none")
		self.assertEqual(frappe.local.message_log, before)

	def test_missing_user_settings_fields_mean_defaults_and_leave_no_message(self):
		user = self.person(dob="1990-11-08")
		before = list(frappe.local.message_log)
		with self._meta_without_fields(USETT):
			out = resolve.answer(user, _now(DIWALI))
		self.assertEqual((out["kind"], out["may_play"]), ("birthday", True))
		self.assertEqual(frappe.local.message_log, before)
