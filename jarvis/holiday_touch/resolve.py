"""What the holiday touch shows one user today.

Two layers, split so the first can be cached:

* ``occasion(user, now)``: the cacheable part. It reads Employee and Holiday rows only and
  ignores every switch. Shape: ``{kind, design, occasion, year, scene_first_day, reason}``
  plus, only when the winner is a birthday and a holiday also matched, ``fallback`` (the
  same shape for the holiday). ``reason`` is internal (it says why an occasion is ``none``, for support) and never leaves ``answer``.
* ``answer(user, now)``: the payload the client gets. It applies the switches, the personal
  switch, the policy mirror, white label and ``may_play`` on every call, then picks the
  first of ``[winner, fallback]`` that the birthday switch and the disabled designs allow.

A date of birth is compared, never stored, returned or logged. ``safe_answer`` is the only
entry point a request path should call: it never raises.

The India-only date guards (Republic Day, Independence Day, ...) use the COMPANY's country,
not the user's or the Holiday List's: the feature is about the company whose country is
India, and a user's own country is not recorded anywhere reliable.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_system_timezone, getdate

from jarvis.holiday_touch import cache, cadence, force, pick, settings
from jarvis.holiday_touch.match import match_occasion
from jarvis.holiday_touch.names import OCCASIONS
from jarvis.holiday_touch.report import report
from jarvis.holiday_touch.weekly_off import calendar_name, country_code_for
from jarvis.permissions import has_jarvis_access

# Active Employees of one company sharing a date of birth: that many or more is a dummy
# date (a form default, an import placeholder), not a real birthday.
_PLACEHOLDER_MIN = 5
_none = pick.none


# -- public ---------------------------------------------------------------------------


def safe_answer(user: str, now: datetime | None = None) -> dict:
	"""``answer`` that never raises: any error is logged and becomes ``none``."""
	try:
		return answer(user, now)
	except Exception:
		report("answer", "Holiday touch: answer failed")
		return _none_payload(user, now)


def answer(user: str, now: datetime | None = None) -> dict:
	"""The payload for ``user`` at ``now`` (UTC; injected by tests)."""
	now = _utc(now)
	zone = zone_for(user)
	today = now.astimezone(ZoneInfo(zone)).date()
	payload = pick.none_payload(zone, today)
	if not user or user == "Guest":
		return payload
	site = settings.site()
	if not settings.on(site, "holiday_touches_enabled"):
		return payload
	row = settings.user_row(user)
	if settings.personal_off(row) or settings.on(site, "holiday_policy_disabled"):
		return payload
	forced = force.forced_occasion(now)  # developer_mode only: skips the cache and the cadence
	if forced is not None and has_jarvis_access(user):
		occ = forced
	else:
		occ = cache.occasion_cached(user, zone, now, lambda: occasion(user, now, zone))
	chosen = _choose(
		occ,
		settings.on(site, "birthday_greetings_enabled"),
		settings.blocked_designs(site),
	)
	if chosen is None:
		return payload
	white = settings.white_label(site)
	scene = chosen["kind"] in ("day", "birthday") and chosen["scene_first_day"] and not white
	payload.update(
		kind=chosen["kind"],
		design=chosen["design"],
		occasion=chosen["occasion"],
		year=chosen["year"],
		worn=not white,
		scene=scene,
		may_play=(forced is not None and occ is forced)
		or cadence.may_play(now, settings.shown_times(row), zone),
	)
	return payload


def occasion(user: str, now: datetime | None = None, zone: str | None = None) -> dict:
	"""The cacheable occasion for ``user`` (see the module docstring). ``zone`` is the user's
	already validated zone (``zone_for``); it is read here when the caller has not."""
	if not user or user == "Guest":
		return _none("guest")
	if not has_jarvis_access(user):
		return _none("no_access")
	if not frappe.db.table_exists("Employee"):
		return _none("no_employee_doctype")
	employee = _employee(user)
	if not employee:
		return _none("no_employee")
	today = _utc(now).astimezone(ZoneInfo(zone or zone_for(user))).date()
	holiday = _holiday_candidate(employee, today)
	state = _birthday_state(employee, today)
	if state == "yes":
		win = pick.hit("birthday", "birthday", "birthday", None, True, "birthday")
		if holiday["kind"] != "none":
			win["fallback"] = holiday
		return win
	if state == "placeholder" and holiday["kind"] == "none":
		return _none("placeholder_dob")
	return holiday


def zone_for(user: str | None) -> str:
	"""``User.time_zone`` when it is a valid IANA zone, else the system zone when that is
	valid, else UTC. Never use ``get_datetime_in_timezone``: it returns UTC for an unknown
	zone, silently."""
	for read in (lambda: _user_zone(user), get_system_timezone):
		try:
			zone = read()
			ZoneInfo(zone)
			return zone
		except Exception:
			continue
	return "UTC"


def _user_zone(user: str | None):
	return frappe.db.get_value("User", user, "time_zone") if user else None


# -- choosing -------------------------------------------------------------------------


def _choose(occ: dict, birthday_on: bool, blocked: frozenset) -> dict | None:
	for cand in (occ, occ.get("fallback")):
		if not cand or cand["kind"] == "none":
			continue
		if cand["kind"] == "birthday" and not birthday_on:
			continue
		if cand["design"] in blocked:
			continue
		return cand
	return None


# -- employee and birthday ------------------------------------------------------------


def _employee(user: str):
	return frappe.db.get_value(
		"Employee",
		{"user_id": user, "status": "Active"},
		["name", "company", "date_of_birth"],
		order_by="modified desc",
		as_dict=True,
	)


def _birthday_state(employee, today: date) -> str:
	"""``yes``, ``placeholder`` (a dummy date shared by 5+ colleagues) or ``no``."""
	if not employee.date_of_birth:
		return "no"
	dob = getdate(employee.date_of_birth)
	if not _is_today(dob, today):
		return "no"
	placeholder = cache.placeholder_dates(
		employee.company, today, lambda: _placeholder_dates(employee.company)
	)
	return "placeholder" if dob in placeholder else "yes"


def _is_today(dob: date, today: date) -> bool:
	if (dob.month, dob.day) == (today.month, today.day):
		return True
	# 29 Feb is celebrated on 28 Feb in a non-leap year.
	return (
		(dob.month, dob.day) == (2, 29)
		and (today.month, today.day) == (2, 28)
		and not calendar.isleap(today.year)
	)


def _placeholder_dates(company: str) -> set[date]:
	"""Dates shared by 5+ Active Employees of the company. Dates only: no counts, no names."""
	rows = frappe.db.sql(
		"""select date_of_birth from `tabEmployee`
		where company = %s and status = 'Active' and date_of_birth is not null
		group by date_of_birth having count(*) >= %s""",
		(company, _PLACEHOLDER_MIN),
	)
	return {getdate(r[0]) for r in rows}


# -- holidays -------------------------------------------------------------------------


def _holiday_candidate(employee, today: date) -> dict:
	"""The holiday for ``today``, or a ``none`` with a reason. A failure here never costs the
	birthday: it is logged once an hour and counts as "no holiday" (reason ``holiday_error``).
	Reasons: ``no_list`` (no Holiday List resolves), ``no_rows`` (a list, but no row within
	the window), ``no_match`` (rows in the window, none naming an occasion for today)."""
	try:
		if "hrms" not in frappe.get_installed_apps():
			return _none("no_hrms")
		span = timedelta(days=pick.SPAN)
		rows = _holiday_rows(employee.name, today - span, today + span)
		if rows is None:
			return _none("no_list")
		if not rows:
			return _none("no_rows")
		return pick.pick(_matches(rows, employee.company), today)
	except Exception:
		report("holiday", "Holiday touch: holiday lookup failed")
		return _none("holiday_error")


def _holiday_rows(employee: str, start: date, end: date):
	"""Rows ``{holiday_date, description, weekly_off, parent}``; None when no list resolves.

	HRMS 16 rows carry ``weekly_off`` and ``parent``. It resolves the list(s) in effect as
	date ranges and reads the rows in one query (what ``get_holidays_for_employee`` does); an
	empty range list is how "no holiday list at all" is told apart from "a list, no rows here".
	When that internal module is missing or has another signature, the public
	``get_holidays_for_employee`` is used (no list gives no rows, reason ``no_rows``).
	HRMS 15 rows carry neither ``weekly_off`` nor ``parent``, so there (no Holiday List
	Assignment doctype) the list is resolved through ERPNext and its Holiday rows are read
	directly."""
	if frappe.db.exists("DocType", "Holiday List Assignment"):
		try:
			from hrms.utils.holiday_list import get_holiday_list_ranges_for_employee, get_holidays_in_ranges

			ranges = get_holiday_list_ranges_for_employee(employee, start, end, raise_exception=False)
			return get_holidays_in_ranges(ranges, skip_weekly_offs=False) if ranges else None
		except (ImportError, AttributeError, TypeError):
			from hrms.hr.utils import get_holidays_for_employee

			return get_holidays_for_employee(
				employee, start, end, raise_exception=False, only_non_weekly=False
			)
	from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee

	holiday_list = get_holiday_list_for_employee(employee, raise_exception=False)
	if not holiday_list:
		return None
	return frappe.get_all(
		"Holiday",
		filters={
			"parent": holiday_list,
			"parenttype": "Holiday List",
			"holiday_date": ["between", [start, end]],
		},
		fields=["holiday_date", "description", "weekly_off", "parent"],
		order_by="holiday_date",
	)


def _matches(rows, company: str) -> dict:
	"""``{date: {occasion: via}}`` for the rows that name an occasion."""
	found = {}
	for hit in _row_matches(rows, company):
		if hit["occasion"]:
			found.setdefault(hit["on"], {})[hit["occasion"]] = hit["via"]
	return found


def _row_matches(rows, company: str) -> list[dict]:
	"""One ``{on, description, occasion, via}`` per row, in row order; ``occasion`` and
	``via`` are None for a row that names none. The one place a row is matched."""
	country = frappe.get_cached_value("Company", company, "country")
	lists, hits = {}, []
	for row in rows:
		on = getdate(row.get("holiday_date"))
		occ, via = match_occasion(row.get("description"), on, country), "match"
		if occ is None and row.get("weekly_off"):
			occ, via = _calendar_occasion(row.get("parent"), on, country, lists), "weekly_off_calendar"
		hits.append(
			{"on": on, "description": row.get("description"), "occasion": occ, "via": via if occ else None}
		)
	return hits


def _calendar_occasion(parent, on: date, company_country, lists: dict):
	"""What the country's own calendar calls this weekly-off date (ERPNext's "Get Local
	Holidays" skips a date that already has a row, so a festival on a Sunday stays "Sunday").
	The row's list country and subdivision win; without a country, the Company's ISO code and
	no subdivision."""
	if parent not in lists:
		lists[parent] = _list_region(parent)
	country, subdivision = lists[parent]
	code = country_code_for(country or company_country)
	name = calendar_name(on, code, subdivision if country else None)
	return match_occasion(name, on, company_country) if name else None


def _list_region(parent):
	"""``(country, subdivision)`` of a Holiday List; ``(None, None)`` when there is no list or
	the lookup fails: this only refines a best-effort lookup, it must not cost the answer."""
	try:
		region = frappe.db.get_value("Holiday List", parent, ["country", "subdivision"]) if parent else None
	except Exception:
		region = None
	return region or (None, None)


# -- support view ---------------------------------------------------------------------


def window_view(user: str, now: datetime | None = None, zone: str | None = None) -> dict:
	"""What ``occasion`` reads for ``user``, for the admin support view (``explain``).

	``{employee, birthday_today, rows_status, rows}``. ``birthday_today`` is a bool: the date
	of birth itself never leaves this module. ``rows`` are the Holiday rows within ``SPAN``
	days of today as ``{date, description, occasion, design, via}``; ``rows_status`` is ``ok``,
	``no_employee``, ``no_hrms``, ``no_list`` or ``error`` (a lookup fault is a status, not a
	raise). Writes nothing."""
	employee = _employee(user) if frappe.db.table_exists("Employee") else None
	if not employee:
		return _view(False, False, "no_employee", [])
	today = _utc(now).astimezone(ZoneInfo(zone or zone_for(user))).date()
	birthday = _birthday_state(employee, today) == "yes"
	status, rows = _window_rows(employee, today)
	return _view(True, birthday, status, rows)


def _view(employee: bool, birthday: bool, status: str, rows: list) -> dict:
	return {"employee": employee, "birthday_today": birthday, "rows_status": status, "rows": rows}


def _window_rows(employee, today: date) -> tuple[str, list]:
	try:
		if "hrms" not in frappe.get_installed_apps():
			return "no_hrms", []
		span = timedelta(days=pick.SPAN)
		rows = _holiday_rows(employee.name, today - span, today + span)
		if rows is None:
			return "no_list", []
		return "ok", [_row_view(hit) for hit in _row_matches(rows, employee.company)]
	except Exception:
		report("explain_rows", "Holiday touch: explain rows lookup failed")
		return "error", []


def _row_view(hit: dict) -> dict:
	occ = hit["occasion"]
	return {
		"date": hit["on"].isoformat(),
		"description": hit["description"],
		"occasion": occ,
		"design": OCCASIONS[occ] if occ else None,
		"via": hit["via"],
	}


# -- small helpers --------------------------------------------------------------------


def _utc(now: datetime | None) -> datetime:
	if now is None:
		return datetime.now(timezone.utc)
	if now.tzinfo is None:
		return now.replace(tzinfo=timezone.utc)
	return now.astimezone(timezone.utc)


def _none_payload(user, now) -> dict:
	try:
		zone = zone_for(user)
		today = _utc(now).astimezone(ZoneInfo(zone)).date()
	except Exception:
		zone, today = "UTC", _utc(now).date()
	return pick.none_payload(zone, today)
