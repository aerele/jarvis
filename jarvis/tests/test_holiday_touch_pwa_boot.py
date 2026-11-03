"""The ``/jarvis-mobile`` shell carries the same holiday answer as the desktop boot.

``www/jarvis_mobile.py`` puts ``holiday_touch`` in ``context.boot`` for a signed-in user, a
plain "none" for a Guest (the resolver is not even called), and a "none" on any fault: the
PWA shell must always render.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
from datetime import date
from unittest import mock

import frappe

from jarvis.holiday_touch import resolve
from jarvis.tests.test_holiday_touch_resolve import DIWALI, _Base, _now
from jarvis.www import jarvis_mobile as www_mobile

NOW = _now(DIWALI)  # 11:30 in Kolkata on 2026-11-08


def _boot(user: str, module=www_mobile) -> dict:
	"""The shell's boot dict for ``user``: the access probe pinned, no commit."""
	frappe.set_user(user)
	ctx = frappe._dict()
	with (
		mock.patch.object(module, "has_jarvis_access", return_value=True),
		mock.patch.object(module, "has_jarvis_admin_access", return_value=False),
		mock.patch.object(frappe.db, "commit"),  # get_context commits; a test must not
	):
		module.get_context(ctx)
	return ctx.boot


class TestMobileBoot(_Base):
	def test_a_signed_in_user_gets_the_same_payload_as_the_resolver(self):
		user = self.person(dob="1990-11-08")
		with mock.patch.object(resolve, "_utc", return_value=NOW):
			boot = _boot(user)
			expected = resolve.safe_answer(user)
		self.assertEqual(boot["holiday_touch"]["kind"], "birthday")
		self.assertEqual(boot["holiday_touch"], expected)
		json.dumps(boot)  # jinjaBootData renders it with |tojson

	def test_a_guest_gets_none_without_the_resolver_being_called(self):
		with mock.patch.object(resolve, "safe_answer") as safe_answer:
			boot = _boot("Guest")
		safe_answer.assert_not_called()
		holiday = boot["holiday_touch"]
		self.assertEqual(set(holiday), {"v", "date", "tz", "kind"})
		self.assertEqual((holiday["v"], holiday["tz"], holiday["kind"]), (1, "UTC", "none"))
		date.fromisoformat(holiday["date"])

	def test_a_resolver_fault_is_a_none_answer_and_the_rest_of_the_boot_is_intact(self):
		user = self.person(dob="1990-11-08")
		with mock.patch.object(resolve, "safe_answer", side_effect=RuntimeError("boom")):
			boot = _boot(user)
		holiday = boot["holiday_touch"]
		self.assertEqual(set(holiday), {"v", "date", "tz", "kind"})
		self.assertEqual((holiday["tz"], holiday["kind"]), ("UTC", "none"))
		self.assertIn("csrf_token", boot)
		self.assertIn("announcement", boot)

	def test_a_broken_holiday_touch_package_does_not_break_the_shell(self):
		# Every import of a holiday_touch module raises, as a broken package would. A fresh
		# copy of the page module is loaded under that, so a top-level import of any of them
		# would fail the load; the boot's own import is the lazy one inside its try block.
		user = self.person(dob="1990-11-08")
		real_import = builtins.__import__

		def broken_import(name, *args, **kwargs):
			if name.startswith("jarvis.holiday_touch"):
				raise ImportError(f"broken: {name}")
			return real_import(name, *args, **kwargs)

		with mock.patch.object(builtins, "__import__", broken_import):
			spec = importlib.util.spec_from_file_location("jarvis_www_mobile_fresh", www_mobile.__file__)
			fresh = importlib.util.module_from_spec(spec)
			spec.loader.exec_module(fresh)
			boot = _boot(user, fresh)
		self.assertEqual(boot["holiday_touch"]["kind"], "none")
		self.assertIn("csrf_token", boot)
