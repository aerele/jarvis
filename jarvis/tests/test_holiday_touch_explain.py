"""Holiday touch ``explain``: the admin support view of why a user got (or missed) a touch.

Real ERPNext and HRMS fixtures through the helpers of ``test_holiday_touch_resolve``; time is
injected through ``api._now``. Holiday cases skip without HRMS.
"""

from __future__ import annotations

import json
import os
from datetime import date
from unittest import mock

import frappe

from jarvis.holiday_touch import api, resolve
from jarvis.tests.test_holiday_touch_resolve import (
	DIWALI,
	HAS_HRMS,
	IST,
	USETT,
	_Base,
	_now,
	_set,
	_user,
)

NOW = _now(DIWALI)  # 11:30 in Kolkata on 2026-11-08
BIRTHDAY = "1990-11-08"
VIEW_KEYS = {
	"user",
	"known",
	"zone",
	"today",
	"window",
	"birthday_today",
	"rows_status",
	"rows",
	"occasion",
	"chain",
	"answer",
	"answer_is_authoritative",
}


class _Case(_Base):
	def view(self, user=None, as_user="Administrator", now=NOW) -> dict:
		"""``api.explain`` called by ``as_user`` with the clock pinned to ``now``."""
		frappe.set_user(as_user)
		with mock.patch.object(api, "_now", return_value=now):
			return api.explain(user) if user else api.explain()

	def step(self, view: dict, name: str) -> dict:
		return next(s for s in view["chain"] if s["step"] == name)

	def needs_hrms(self):
		if not HAS_HRMS:
			self.skipTest("hrms is not installed on this site")


class TestEndpoint(_Case):
	def test_it_is_a_get_only_whitelisted_endpoint(self):
		self.assertIn(api.explain, frappe.whitelisted)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[api.explain], ["GET"])
		self.assertNotIn(api.explain, frappe.guest_methods)

	def test_an_admin_gets_the_view(self):
		user = self.person(dob=BIRTHDAY)
		view = self.view(user)
		self.assertEqual(set(view), VIEW_KEYS)
		self.assertEqual(
			(view["user"], view["known"], view["zone"], view["today"]), (user, True, IST, "2026-11-08")
		)
		self.assertEqual(view["answer"]["kind"], "birthday")

	def test_a_jarvis_admin_role_holder_gets_the_view(self):
		admin = _user(roles=("Jarvis User", "Jarvis Admin"))
		self.assertTrue(self.view(self.person(), as_user=admin)["known"])

	def test_the_user_defaults_to_the_session_user(self):
		self.assertEqual(self.view()["user"], "Administrator")

	def test_a_non_admin_is_denied_with_a_403(self):
		user = self.person()
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError) as ctx:
			api.explain(user)
		self.assertEqual(ctx.exception.http_status_code, 403)

	def test_a_guest_is_denied_with_a_403(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError) as ctx:
			api.explain("Administrator")
		self.assertEqual(ctx.exception.http_status_code, 403)

	def test_it_writes_nothing(self):
		user = self.person(dob=BIRTHDAY)
		before = frappe.db.count(USETT)
		with (
			mock.patch.object(frappe.db, "set_value") as set_value,
			mock.patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql,
		):
			self.view(user)
		set_value.assert_not_called()
		writes = [
			c
			for c in sql.call_args_list
			if str(c.args[0]).lstrip().lower().startswith(("insert", "update", "delete"))
		]
		self.assertEqual(writes, [])
		self.assertEqual(frappe.db.count(USETT), before)


class TestPrivacy(_Case):
	def test_the_date_of_birth_never_appears_even_on_the_birthday(self):
		user = self.person(dob=BIRTHDAY)
		view = self.view(user)
		self.assertIs(view["birthday_today"], True)
		text = json.dumps(view, default=str)
		for banned in ("1990", "date_of_birth", "dob", BIRTHDAY):
			self.assertNotIn(banned, text)

	def test_a_birthday_that_is_not_today_is_a_plain_no(self):
		view = self.view(self.person(dob="1990-05-05"))
		self.assertIs(view["birthday_today"], False)
		self.assertNotIn("1990", json.dumps(view, default=str))


class TestUnknownUser(_Case):
	def test_an_unknown_user_gets_a_clear_view_not_an_error(self):
		view = self.view("nobody@nowhere.test")
		self.assertEqual(view, {"user": "nobody@nowhere.test", "known": False, "summary": "unknown user"})

	def test_a_blank_or_odd_user_does_not_raise(self):
		for odd in ("   ", "a" * 500, "x'; drop table tabUser; --"):
			self.assertFalse(self.view(odd)["known"], odd)

	def test_a_fault_inside_the_view_is_an_error_view_not_a_raise(self):
		user = self.person()
		with (
			mock.patch.object(resolve, "occasion", side_effect=RuntimeError("boom")),
			mock.patch("frappe.log_error"),
		):
			view = self.view(user)
		self.assertEqual((view["known"], view["summary"]), (True, "explain failed"))


class TestReasons(_Case):
	def reason(self, view) -> str:
		return view["occasion"]["reason"]

	def test_a_user_with_no_employee(self):
		view = self.view(_user())
		self.assertEqual(self.reason(view), "no_employee")
		self.assertEqual(view["rows_status"], "no_employee")
		self.assertEqual(view["rows"], [])

	def test_a_user_with_no_jarvis_access(self):
		user = self.person()
		frappe.db.delete("Has Role", {"parent": user, "role": "Jarvis User"})
		frappe.clear_cache(user=user)
		self.assertEqual(self.reason(self.view(user)), "no_access")

	def test_an_employee_with_no_holiday_list(self):
		self.needs_hrms()
		view = self.view(self.person())
		self.assertEqual(self.reason(view), "no_list")
		self.assertEqual(view["rows_status"], "no_list")

	def test_rows_that_name_no_occasion(self):
		self.needs_hrms()
		view = self.view(self.person([(DIWALI, "Company Offsite", 0)]))
		self.assertEqual(self.reason(view), "no_match")
		self.assertEqual(view["rows_status"], "ok")

	def test_two_designs_on_one_date_are_a_tie(self):
		self.needs_hrms()
		rows = [(DIWALI, "Diwali", 0), (DIWALI, "Eid ul Fitr", 0)]
		view = self.view(self.person(rows, allow_duplicates=True))
		self.assertEqual(self.reason(view), "tie")
		self.assertEqual(view["answer"]["kind"], "none")

	def test_a_match_carries_how_it_was_found(self):
		self.needs_hrms()
		view = self.view(self.person([(DIWALI, "Diwali", 0)]))
		self.assertEqual(view["occasion"]["kind"], "day")
		self.assertEqual(self.reason(view), "match")
		self.assertEqual(view["answer"]["kind"], "day")

	def test_the_site_switch_off(self):
		user = self.person(dob=BIRTHDAY)
		_set("holiday_touches_enabled", 0)
		view = self.view(user)
		self.assertEqual(
			self.step(view, "site_switch"), {"step": "site_switch", "value": "off", "effect": "blocks"}
		)
		self.assertEqual(view["answer"]["kind"], "none")

	def test_the_personal_switch_off(self):
		user = self.person(dob=BIRTHDAY)
		frappe.get_doc({"doctype": USETT, "user": user, "show_holiday_touches": 0}).insert(
			ignore_permissions=True
		)
		view = self.view(user)
		self.assertEqual(self.step(view, "personal_switch")["effect"], "blocks")
		self.assertEqual(view["answer"]["kind"], "none")

	def test_the_operator_policy_off(self):
		user = self.person(dob=BIRTHDAY)
		_set("holiday_policy_disabled", 1)
		view = self.view(user)
		self.assertEqual(self.step(view, "policy_switch")["effect"], "blocks")
		self.assertEqual(view["answer"]["kind"], "none")

	def test_a_disabled_design_blocks_the_birthday(self):
		user = self.person(dob=BIRTHDAY)
		_set("holiday_policy_disabled_designs", json.dumps(["birthday"]))
		view = self.view(user)
		self.assertEqual(self.step(view, "blocked_designs")["effect"], "blocks")
		self.assertEqual(view["answer"]["kind"], "none")

	def test_the_birthday_switch_off(self):
		user = self.person(dob=BIRTHDAY)
		_set("birthday_greetings_enabled", 0)
		view = self.view(user)
		self.assertEqual(self.step(view, "birthday_switch")["effect"], "blocks")
		self.assertEqual(view["answer"]["kind"], "none")

	def test_white_label_is_reported(self):
		user = self.person(dob=BIRTHDAY)
		_set("brand_logo", "/files/logo.png")
		view = self.view(user)
		self.assertEqual(self.step(view, "white_label")["value"], "yes")
		self.assertIs(view["answer"]["scene"], False)

	def test_a_forced_occasion_is_reported(self):
		user = self.person()
		with mock.patch.dict(frappe.conf, {"developer_mode": 1, "jarvis_holiday_touch_force": "diwali:day"}):
			view = self.view(user)
		self.assertEqual(self.step(view, "force")["effect"], "changes")
		self.assertEqual(view["answer"]["kind"], "day")

	def test_the_cadence_is_reported(self):
		user = self.person(dob=BIRTHDAY)
		self.assertEqual(self.step(self.view(user), "cadence")["value"], "may play")
		state = {"date": "2026-11-08", "times": [NOW.isoformat()]}
		frappe.get_doc({"doctype": USETT, "user": user, "holiday_touch_shown": json.dumps(state)}).insert(
			ignore_permissions=True
		)
		self.assertEqual(self.step(self.view(user), "cadence")["value"], "already shown")

	def test_a_forced_occasion_bypasses_the_cadence(self):
		user = self.person()
		state = {"date": "2026-11-08", "times": [NOW.isoformat()]}
		frappe.get_doc({"doctype": USETT, "user": user, "holiday_touch_shown": json.dumps(state)}).insert(
			ignore_permissions=True
		)
		with mock.patch.dict(frappe.conf, {"developer_mode": 1, "jarvis_holiday_touch_force": "diwali:day"}):
			view = self.view(user)
		self.assertEqual(
			self.step(view, "cadence"),
			{"step": "cadence", "value": "force bypasses the cadence", "effect": "none"},
		)
		self.assertIs(view["answer"]["may_play"], True)

	def test_the_view_says_the_answer_is_authoritative(self):
		self.assertIs(self.view(self.person())["answer_is_authoritative"], True)

	def test_the_chain_steps_come_in_the_order_answer_applies_them(self):
		names = [s["step"] for s in self.view(self.person())["chain"]]
		self.assertEqual(
			names,
			[
				"site_switch",
				"personal_switch",
				"policy_switch",
				"force",
				"occasion",
				"birthday_switch",
				"blocked_designs",
				"white_label",
				"cadence",
			],
		)


class TestChoiceEffects(_Case):
	"""A birthday on the Diwali date: the birthday wins and Diwali is its fallback."""

	def setUp(self):
		self.needs_hrms()
		super().setUp()
		self.user = self.person([(DIWALI, "Diwali", 0)], dob=BIRTHDAY)

	def test_the_baseline_is_the_birthday(self):
		view = self.view(self.user)
		self.assertEqual(view["occasion"]["fallback"]["kind"], "day")
		self.assertEqual(view["answer"]["kind"], "birthday")

	def test_a_blocked_fallback_design_does_not_block_the_birthday(self):
		_set("holiday_policy_disabled_designs", json.dumps(["diwali"]))
		view = self.view(self.user)
		self.assertEqual(self.step(view, "blocked_designs")["effect"], "none")
		self.assertEqual(view["answer"]["kind"], "birthday")

	def test_a_blocked_birthday_design_changes_to_the_fallback(self):
		_set("holiday_policy_disabled_designs", json.dumps(["birthday"]))
		view = self.view(self.user)
		self.assertEqual(self.step(view, "blocked_designs")["effect"], "changes")
		self.assertEqual(view["answer"]["kind"], "day")

	def test_both_designs_blocked_blocks(self):
		_set("holiday_policy_disabled_designs", json.dumps(["birthday", "diwali"]))
		view = self.view(self.user)
		self.assertEqual(self.step(view, "blocked_designs")["effect"], "blocks")
		self.assertEqual(view["answer"]["kind"], "none")

	def test_the_birthday_switch_off_changes_to_the_fallback(self):
		_set("birthday_greetings_enabled", 0)
		view = self.view(self.user)
		self.assertEqual(self.step(view, "birthday_switch")["effect"], "changes")
		self.assertEqual(view["answer"]["kind"], "day")

	def test_the_birthday_switch_off_with_a_blocked_fallback_blocks(self):
		_set("birthday_greetings_enabled", 0)
		_set("holiday_policy_disabled_designs", json.dumps(["diwali"]))
		view = self.view(self.user)
		self.assertEqual(self.step(view, "birthday_switch")["effect"], "blocks")
		self.assertEqual(view["answer"]["kind"], "none")


class TestRows(_Case):
	def setUp(self):
		self.needs_hrms()
		super().setUp()

	def test_each_row_in_the_window_shows_its_date_description_and_match(self):
		rows = [
			(date(2026, 11, 6), "Company Offsite", 0),
			(DIWALI, "Diwali", 0),
			(date(2026, 11, 10), "Eid ul Fitr", 0),
		]
		view = self.view(self.person(rows))
		self.assertEqual(view["window"], {"from": "2026-11-05", "to": "2026-11-11"})
		got = [(r["date"], r["description"], r["occasion"], r["design"], r["via"]) for r in view["rows"]]
		self.assertEqual(
			got,
			[
				("2026-11-06", "Company Offsite", None, None, None),
				("2026-11-08", "Diwali", "diwali", "diwali", "match"),
				("2026-11-10", "Eid ul Fitr", "eid", "eid", "match"),
			],
		)

	def test_a_weekly_off_row_matched_through_the_country_calendar_says_so(self):
		# 8 Nov 2026 is a Sunday: a weekly-off row the India calendar names Diwali.
		view = self.view(self.person([(DIWALI, "Sunday", 1)], list_country="India"))
		(row,) = view["rows"]
		self.assertEqual((row["description"], row["via"]), ("Sunday", "weekly_off_calendar"))
		self.assertEqual(row["occasion"], "diwali")

	def test_a_row_failure_is_reported_in_the_status_not_raised(self):
		user = self.person([(DIWALI, "Diwali", 0)])
		with (
			mock.patch.object(resolve, "_holiday_rows", side_effect=RuntimeError("boom")),
			mock.patch("frappe.log_error"),
		):
			view = self.view(user)
		self.assertEqual((view["rows_status"], view["rows"]), ("error", []))
		self.assertEqual(view["occasion"]["reason"], "holiday_error")

	def test_the_view_answer_is_the_payload_the_user_gets_from_get_today(self):
		user = self.person([(DIWALI, "Diwali", 0), (date(2026, 11, 10), "Company Offsite", 0)])
		answer = self.view(user)["answer"]
		frappe.set_user(user)
		with mock.patch.object(resolve, "_utc", return_value=NOW):
			self.assertEqual(api.get_today(), answer)
		self.assertEqual(answer["kind"], "day")
