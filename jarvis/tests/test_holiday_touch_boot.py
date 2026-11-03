"""The Desk boot key for the holiday touch (``jarvis.holiday_touch.boot.extend_bootinfo``).

``extend_bootinfo`` runs after Frappe reads the cached ``boot_session`` blob, on every Desk
load, so the key is fresh even when the rest of the boot came from the cache.
"""

from __future__ import annotations

import json
from unittest import mock

import frappe
import frappe.sessions

from jarvis.holiday_touch import boot, resolve
from jarvis.tests.test_holiday_touch_resolve import DIWALI, _Base, _now, _set

NOW = _now(DIWALI)  # 11:30 in Kolkata on 2026-11-08
KEY = "jarvis_holiday_touch"


def _bootinfo(has_access=True):
	return frappe._dict(jarvis_has_access=has_access)


def _count_queries(call) -> list[str]:
	"""The SQL text of every query ``call`` runs."""
	seen = []
	real = frappe.db.sql

	def spy(query, *args, **kwargs):
		seen.append(str(query))
		return real(query, *args, **kwargs)

	with mock.patch.object(frappe.db, "sql", spy):
		call()
	return seen


class TestExtendBootinfo(_Base):
	def _birthday_user(self):
		user = self.person(dob="1990-11-08")
		frappe.set_user(user)
		return user

	def test_the_key_carries_the_contract_for_a_user_with_access(self):
		self._birthday_user()
		info = _bootinfo()
		with mock.patch.object(resolve, "_utc", return_value=NOW):
			boot.extend_bootinfo(bootinfo=info)
		self.assertEqual(info[KEY]["v"], 1)
		self.assertEqual(
			(info[KEY]["kind"], info[KEY]["design"], info[KEY]["scene"]), ("birthday", "birthday", True)
		)
		json.dumps(info[KEY])

	def test_no_key_without_access(self):
		self._birthday_user()
		for info in (_bootinfo(False), frappe._dict()):
			with mock.patch.object(resolve, "_utc", return_value=NOW):
				boot.extend_bootinfo(bootinfo=info)
			self.assertNotIn(KEY, info)

	def test_no_key_for_guest(self):
		frappe.set_user("Guest")
		info = _bootinfo()
		boot.extend_bootinfo(bootinfo=info)
		self.assertNotIn(KEY, info)

	def test_no_access_costs_no_query_at_all(self):
		self._birthday_user()
		info = _bootinfo(False)
		self.assertEqual(_count_queries(lambda: boot.extend_bootinfo(bootinfo=info)), [])

	def test_a_warm_boot_runs_few_queries_and_none_on_employee_or_holiday(self):
		self._birthday_user()
		with mock.patch.object(resolve, "_utc", return_value=NOW):
			boot.extend_bootinfo(bootinfo=_bootinfo())  # warms the occasion cache
			queries = _count_queries(lambda: boot.extend_bootinfo(bootinfo=_bootinfo()))
		self.assertLessEqual(len(queries), 4, queries)
		for sql in queries:
			self.assertNotIn("tabEmployee", sql)
			self.assertNotIn("tabHoliday", sql)

	def test_a_raising_answer_leaves_the_boot_intact_and_is_logged_once_under_its_own_kind(self):
		self._birthday_user()
		info = frappe._dict(jarvis_has_access=True, csrf_token="kept")
		with (
			mock.patch.object(resolve, "safe_answer", side_effect=RuntimeError("boom")),
			mock.patch("frappe.log_error") as log,
		):
			boot.extend_bootinfo(bootinfo=info)
			boot.extend_bootinfo(bootinfo=info)  # the second fault inside the hour is muted
		self.assertNotIn(KEY, info)
		self.assertEqual(info.csrf_token, "kept")
		self.assertEqual(log.call_count, 1)
		self.assertIn("boot", log.call_args.kwargs["title"].lower())

	def test_a_disabled_design_is_a_none_answer(self):
		self._birthday_user()
		_set("holiday_policy_disabled_designs", '["birthday"]')
		info = _bootinfo()
		with mock.patch.object(resolve, "_utc", return_value=NOW):
			boot.extend_bootinfo(bootinfo=info)
		self.assertEqual(info[KEY]["kind"], "none")


class TestCachedBoot(_Base):
	"""Frappe serves the rest of the boot from the cache; the key must still be current."""

	def setUp(self):
		super().setUp()
		# sessions.get() reads request-only state; a test run has no request.
		patch = mock.patch.object(frappe.local, "request", None, create=True)
		patch.start()
		self.addCleanup(patch.stop)

	def tearDown(self):
		frappe.cache().hdel("bootinfo", frappe.session.user)
		super().tearDown()

	def test_a_cached_boot_still_sees_the_new_answer_after_the_site_switch_flips(self):
		frappe.set_user(self.person(dob="1990-11-08"))
		with mock.patch.object(resolve, "_utc", return_value=NOW):
			first = frappe.sessions.get()
			self.assertFalse(first.get("from_cache"))
			self.assertEqual(first[KEY]["kind"], "birthday")
			_set("holiday_touches_enabled", 0)
			frappe.local.cache = {}  # the per-request cache; the site cache stays warm
			second = frappe.sessions.get()
		self.assertTrue(second.get("from_cache"))
		self.assertEqual(second[KEY]["kind"], "none")
