"""Holiday touch delivery.

``get_today`` and the ``www/jarvis.py`` boot key return the same ``resolve.safe_answer``.
``answer_cases.json`` is the frozen contract the desktop client is built against: every case
must equal what ``answer()`` really produces, so it cannot drift from the server.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import os
from datetime import date
from pathlib import Path
from unittest import mock

import frappe

from jarvis.holiday_touch import api, resolve
from jarvis.tests.test_holiday_touch_resolve import DIWALI, HAS_HRMS, IST, USETT, _Base, _now, _set
from jarvis.www import jarvis as www_desktop

CASES_FILE = Path(api.__file__).with_name("answer_cases.json")
NOW = _now(DIWALI)  # 11:30 in Kolkata on 2026-11-08
PERSON_KEYS = {"v", "date", "tz", "kind", "design", "occasion", "year", "worn", "scene", "may_play"}


def _boot(user: str) -> dict:
	"""The page's boot dict for ``user``: access, admin and support probes pinned, no commit."""
	frappe.set_user(user)
	ctx = frappe._dict()
	with (
		mock.patch.object(www_desktop, "has_jarvis_access", return_value=True),
		mock.patch.object(www_desktop, "has_jarvis_admin_access", return_value=False),
		mock.patch.object(www_desktop, "is_skill_reviewer", return_value=False),
		mock.patch.object(www_desktop, "support_scope", return_value=None),
		mock.patch.object(www_desktop, "_support_state", return_value=www_desktop.SUPPORT_OFF),
		mock.patch.object(frappe.db, "commit"),  # get_context commits; a test must not
	):
		www_desktop.get_context(ctx)
	return ctx.boot


class TestGetToday(_Base):
	def test_it_is_a_get_only_whitelisted_endpoint_with_no_arguments(self):
		self.assertIn(api.get_today, frappe.whitelisted)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[api.get_today], ["GET"])
		self.assertEqual(api.get_today.__code__.co_argcount, 0)
		self.assertFalse(api.get_today in frappe.guest_methods)

	def test_a_guest_is_denied_with_a_403(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError) as ctx:
			api.get_today()
		self.assertEqual(ctx.exception.http_status_code, 403)

	def test_a_user_without_jarvis_access_is_denied(self):
		# The repo's pattern (require_jarvis_access) is to raise, not to answer none.
		user = self.person()
		frappe.db.delete("Has Role", {"parent": user, "role": "Jarvis User"})
		frappe.clear_cache(user=user)
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			api.get_today()

	def test_it_answers_for_the_session_user_only(self):
		user = self.person(dob="1990-11-08")
		other = self.person(dob="1990-05-05")
		frappe.set_user(other)
		with mock.patch.object(resolve, "_utc", return_value=NOW):
			self.assertEqual(api.get_today()["kind"], "none")
		frappe.set_user(user)
		with mock.patch.object(resolve, "_utc", return_value=NOW):
			self.assertEqual(api.get_today()["kind"], "birthday")

	def test_it_returns_the_same_payload_as_the_boot_key(self):
		user = self.person(dob="1990-11-08")
		with mock.patch.object(resolve, "_utc", return_value=NOW):
			frappe.set_user(user)
			endpoint = api.get_today()
			boot = _boot(user)["holiday_touch"]
		self.assertEqual(endpoint["kind"], "birthday")
		self.assertEqual(endpoint, boot)

	def test_a_fault_inside_the_answer_is_a_none_payload_not_an_error(self):
		user = self.person(dob="1990-11-08")
		frappe.set_user(user)
		with (
			mock.patch.object(resolve, "occasion", side_effect=RuntimeError("boom")),
			mock.patch("frappe.log_error"),
		):
			self.assertEqual(api.get_today()["kind"], "none")


class TestBoot(_Base):
	def test_the_boot_key_is_present_and_json_serialisable(self):
		user = self.person(dob="1990-11-08")
		with mock.patch.object(resolve, "_utc", return_value=NOW):
			boot = _boot(user)
		self.assertEqual(json.loads(json.dumps(boot))["holiday_touch"]["kind"], "birthday")

	def test_the_payload_never_carries_the_dob_or_the_description(self):
		secret = "Quokka Special Festival Note"
		rows = [(DIWALI, f"Diwali {secret}", 0)] if HAS_HRMS else ()
		user = self.person(rows, dob="1990-11-08")
		with mock.patch.object(resolve, "_utc", return_value=NOW):
			texts = [json.dumps(_boot(user)["holiday_touch"])]
			frappe.set_user(user)
			texts.append(json.dumps(api.get_today()))
		for text in texts:
			for banned in ("1990", "date_of_birth", "Quokka", secret, "fallback", "reason"):
				self.assertNotIn(banned, text)

	def test_a_failing_answer_still_renders_the_page_with_none(self):
		user = self.person(dob="1990-11-08")
		for target in ("answer", "occasion", "safe_answer"):
			with (
				self.subTest(target),
				mock.patch.object(resolve, target, side_effect=RuntimeError("boom")),
				mock.patch("frappe.log_error"),
			):
				boot = _boot(user)
			self.assertEqual(boot["holiday_touch"]["kind"], "none")
			self.assertEqual(boot["holiday_touch"]["v"], 1)
			json.dumps(boot)
			self.assertIn("csrf_token", boot)  # the rest of the boot is intact
			self.assertIn("announcement", boot)

	def test_when_even_safe_answer_raises_the_boot_falls_back_to_the_none_payload(self):
		user = self.person(dob="1990-11-08")  # an IST user: the fallback zone is UTC
		with mock.patch.object(resolve, "safe_answer", side_effect=RuntimeError("boom")):
			boot = _boot(user)
		self.assertEqual(set(boot["holiday_touch"]), {"v", "date", "tz", "kind"})
		self.assertEqual((boot["holiday_touch"]["tz"], boot["holiday_touch"]["kind"]), ("UTC", "none"))
		date.fromisoformat(boot["holiday_touch"]["date"])

	def test_a_broken_holiday_touch_package_does_not_break_the_page_boot(self):
		# Every import of a holiday_touch module raises, as a broken package would. A fresh
		# copy of the page module is loaded under that: a top-level import of any of them
		# would fail the load, and the boot's own import is the lazy one in its try block.
		user = self.person(dob="1990-11-08")
		frappe.set_user(user)
		real_import = builtins.__import__

		def broken_import(name, *args, **kwargs):
			if name.startswith("jarvis.holiday_touch"):
				raise ImportError(f"broken: {name}")
			return real_import(name, *args, **kwargs)

		with mock.patch.object(builtins, "__import__", broken_import):
			spec = importlib.util.spec_from_file_location("jarvis_www_fresh_copy", www_desktop.__file__)
			fresh = importlib.util.module_from_spec(spec)
			spec.loader.exec_module(fresh)
			ctx = frappe._dict()
			with (
				mock.patch.object(fresh, "has_jarvis_access", return_value=True),
				mock.patch.object(fresh, "has_jarvis_admin_access", return_value=False),
				mock.patch.object(fresh, "is_skill_reviewer", return_value=False),
				mock.patch.object(fresh, "support_scope", return_value=None),
				mock.patch.object(fresh, "_support_state", return_value=fresh.SUPPORT_OFF),
				mock.patch.object(frappe.db, "commit"),
			):
				fresh.get_context(ctx)
		holiday = ctx.boot["holiday_touch"]
		self.assertEqual(set(holiday), {"v", "date", "tz", "kind"})
		self.assertEqual((holiday["tz"], holiday["kind"]), ("UTC", "none"))
		self.assertIn("csrf_token", ctx.boot)


class TestAnswerCases(_Base):
	"""The frozen contract: every named case equals a real ``answer()``."""

	def setUp(self):
		super().setUp()
		self.cases = {c["name"]: c["payload"] for c in json.loads(CASES_FILE.read_text())}

	def needs_hrms(self):
		if not HAS_HRMS:
			if os.environ.get("CI"):
				self.fail("hrms is not installed on the CI site")
			self.skipTest("hrms is not installed on this site")

	def check(self, name, user, now):
		real = resolve.answer(user, now)
		case = self.cases[name]
		self.assertEqual(set(real), set(case), name)
		for key, value in case.items():
			self.assertIs(type(real[key]), type(value), f"{name}.{key}")
		self.assertEqual(real, case, name)

	def test_the_file_lists_the_named_cases_with_the_frozen_key_sets(self):
		self.assertEqual(
			list(self.cases),
			[
				"none",
				"leadup_diwali",
				"day_diwali",
				"day_second_day",
				"day_may_not_play",
				"day_republic",
				"birthday",
				"white_label_day",
				"newyear",
			],
		)
		for name, payload in self.cases.items():
			self.assertEqual(set(payload), {"v", "date", "tz", "kind"} if name == "none" else PERSON_KEYS)
			self.assertEqual(payload["v"], 1)
			json.dumps(payload)
			self.assertNotIn("reason", payload)

	def test_none(self):
		self.check("none", self.person(), NOW)

	def test_birthday(self):
		self.check("birthday", self.person(dob="1990-11-08"), NOW)

	def test_leadup_diwali(self):
		self.needs_hrms()
		self.check("leadup_diwali", self.person([(DIWALI, "Diwali", 0)]), _now(date(2026, 11, 6)))

	def test_day_diwali(self):
		self.needs_hrms()
		self.check("day_diwali", self.person([(DIWALI, "Diwali", 0)]), NOW)

	def test_day_second_day(self):
		self.needs_hrms()
		rows = [(date(2026, 11, 7), "Diwali", 0), (date(2026, 11, 10), "Diwali", 0)]
		self.check("day_second_day", self.person(rows), _now(date(2026, 11, 10)))

	def test_day_may_not_play(self):
		self.needs_hrms()
		user = self.person([(DIWALI, "Diwali", 0)])
		shown = {"date": "2026-11-08", "times": ["2026-11-08T04:30:00+00:00"]}  # 1h30 before NOW
		frappe.get_doc({"doctype": USETT, "user": user, "holiday_touch_shown": json.dumps(shown)}).insert(
			ignore_permissions=True
		)
		self.check("day_may_not_play", user, NOW)

	def test_day_republic(self):
		self.needs_hrms()
		on = date(2026, 1, 26)
		self.check("day_republic", self.person([(on, "Republic Day", 0)]), _now(on))

	def test_white_label_day(self):
		self.needs_hrms()
		user = self.person([(DIWALI, "Diwali", 0)])
		_set("brand_logo", "/files/logo.png")
		self.check("white_label_day", user, NOW)

	def test_newyear_carries_the_year(self):
		self.needs_hrms()
		on = date(2027, 1, 1)
		self.check("newyear", self.person([(on, "New Year's Day", 0)]), _now(on))
