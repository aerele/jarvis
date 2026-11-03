"""Holiday touches: the test-only force override."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.holiday_touch import cache, cadence, force, resolve
from jarvis.tests.test_holiday_touch_resolve import SETTINGS, _now, _set, _user, clear_log_markers

_MISSING = object()
_KEYS = ("developer_mode", force.CONFIG_KEY)
NOW = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)  # an ordinary day: no real occasion


class _Base(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._conf = {k: frappe.conf.get(k, _MISSING) for k in _KEYS}
		cache.rotate()
		clear_log_markers()
		for field in ("holiday_touches_enabled", "birthday_greetings_enabled"):
			_set(field, 1)
		for field in ("holiday_policy_disabled", "holiday_policy_disabled_designs", "brand_logo"):
			_set(field, None)
		_set("agent_name", "Jarvis")

	def tearDown(self):
		for key, value in self._conf.items():
			if value is _MISSING:
				frappe.conf.pop(key, None)
			else:
				frappe.conf[key] = value
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.value_cache.pop(SETTINGS, None)
		clear_log_markers()

	def force(self, spec, developer_mode=1):
		frappe.conf.developer_mode = developer_mode
		frappe.conf[force.CONFIG_KEY] = spec


class TestForcedOccasion(_Base):
	def test_ignored_without_developer_mode(self):
		for mode in (0, None):
			self.force("diwali:day", mode)
			self.assertIsNone(force.forced_occasion(NOW), mode)
		frappe.conf.pop("developer_mode", None)
		self.assertIsNone(force.forced_occasion(NOW))

	def test_no_config_gives_none(self):
		frappe.conf.developer_mode = 1
		frappe.conf.pop(force.CONFIG_KEY, None)
		self.assertIsNone(force.forced_occasion(NOW))

	def test_shapes(self):
		self.force("diwali:day")
		day = force.forced_occasion(NOW)
		self.assertEqual(
			(day["kind"], day["design"], day["occasion"], day["scene_first_day"]),
			("day", "diwali", "diwali", True),
		)
		self.force("Diwali")  # day is the default; case does not matter
		self.assertEqual(force.forced_occasion(NOW)["kind"], "day")
		self.force("diwali:leadup")
		lead = force.forced_occasion(NOW)
		self.assertEqual((lead["kind"], lead["scene_first_day"]), ("leadup", False))
		self.force("birthday")
		bday = force.forced_occasion(NOW)
		self.assertEqual(
			(bday["kind"], bday["design"], bday["scene_first_day"]), ("birthday", "birthday", True)
		)
		self.force("republic")
		self.assertEqual(force.forced_occasion(NOW)["design"], "independence")

	def test_newyear_year_follows_the_wished_day(self):
		eve = datetime(2026, 12, 31, 6, 0, tzinfo=timezone.utc)
		self.force("newyear:day")
		self.assertEqual(force.forced_occasion(eve)["year"], 2026)
		self.force("newyear:leadup")
		self.assertEqual(force.forced_occasion(eve)["year"], 2027)

	def test_invalid_strings_are_ignored(self):
		for bad in (
			"",
			"  ",
			"nonsense",
			"diwali:weekly",
			"diwali:day:x",
			":day",
			"birthday:day",
			5,
			None,
			["diwali"],
		):
			self.force(bad)
			self.assertIsNone(force.forced_occasion(NOW), repr(bad))


class TestForcedAnswer(_Base):
	def test_ignored_without_developer_mode(self):
		user = _user()
		self.force("diwali:day", 0)
		self.assertEqual(resolve.answer(user, _now(NOW.date()))["kind"], "none")

	def test_day_for_any_user_with_access_bypassing_cache_and_cadence(self):
		user = _user()  # no Employee, no Holiday List: the real occasion is none
		self.assertEqual(resolve.answer(user, NOW)["kind"], "none")
		self.force("diwali:day")
		with (
			mock.patch.object(cache, "occasion_cached", side_effect=AssertionError("cache used")),
			mock.patch.object(cadence, "may_play", return_value=False),
		):
			payload = resolve.answer(user, NOW)
		self.assertEqual(
			(payload["kind"], payload["design"], payload["occasion"]), ("day", "diwali", "diwali")
		)
		self.assertTrue(payload["scene"])
		self.assertTrue(payload["may_play"])  # the cadence said no; force wins

	def test_leadup_and_birthday(self):
		user = _user()
		self.force("diwali:leadup")
		payload = resolve.answer(user, NOW)
		self.assertEqual((payload["kind"], payload["scene"]), ("leadup", False))
		self.force("birthday")
		self.assertEqual(resolve.answer(user, NOW)["design"], "birthday")

	def test_invalid_string_leaves_the_real_answer(self):
		user = _user()
		self.force("nonsense")
		self.assertEqual(resolve.answer(user, NOW)["kind"], "none")

	def test_a_user_without_access_still_gets_none(self):
		user = _user(roles=())
		self.force("diwali:day")
		self.assertEqual(resolve.answer(user, NOW)["kind"], "none")

	def test_switches_still_apply_to_a_forced_occasion(self):
		user = _user()
		self.force("diwali:day")
		_set("holiday_policy_disabled_designs", '["diwali"]')
		self.assertEqual(resolve.answer(user, NOW)["kind"], "none")
		_set("holiday_policy_disabled_designs", None)
		_set("holiday_touches_enabled", 0)
		self.assertEqual(resolve.answer(user, NOW)["kind"], "none")
