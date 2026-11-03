"""Holiday touches: switches, backfill patch and the server-owned shown-state field.

The Jarvis Settings switches are Check fields on a Single. ``get_single_value``
coerces an unset Check to 0, so the v2_29 patch must seed real "1" rows on
existing sites, exactly once, without ever clobbering an admin's explicit 0.
"""

from __future__ import annotations

import json
from unittest.mock import patch as mock_patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint

from jarvis.chat import usage
from jarvis.holiday_touch import settings
from jarvis.learning.roles import _seed_personalise_settings_defaults as seed_defaults
from jarvis.patches.v2_29_backfill_holiday_touch_switches import execute as backfill
from jarvis.tests.test_user_settings import USER_A, USETT, _UsageTestBase

SETTINGS = "Jarvis Settings"
SWITCHES = ("holiday_touches_enabled", "birthday_greetings_enabled")
_SEL = "select value from tabSingles where doctype=%s and field=%s"


def _row(field):
	return frappe.db.sql(_SEL, (SETTINGS, field))


def _drop_rows():
	for field in SWITCHES:
		frappe.db.sql("delete from tabSingles where doctype=%s and field=%s", (SETTINGS, field))


class TestHolidaySwitchBackfill(FrappeTestCase):
	def tearDown(self):
		for field in SWITCHES:
			frappe.db.set_single_value(SETTINGS, field, 1)
		frappe.db.rollback()

	def test_absent_rows_are_seeded_to_one(self):
		_drop_rows()
		self.assertEqual(_row("holiday_touches_enabled"), ())
		backfill()
		for field in SWITCHES:
			self.assertEqual(cint(_row(field)[0][0]), 1)
			self.assertEqual(cint(frappe.db.get_single_value(SETTINGS, field)), 1)

	def test_rerun_keeps_an_explicit_zero(self):
		_drop_rows()
		backfill()
		frappe.db.set_single_value(SETTINGS, "birthday_greetings_enabled", 0)
		backfill()
		self.assertEqual(cint(_row("birthday_greetings_enabled")[0][0]), 0)
		self.assertEqual(cint(_row("holiday_touches_enabled")[0][0]), 1)

	def test_seed_does_not_touch_modified(self):
		_drop_rows()
		before = frappe.db.sql(
			"select value from tabSingles where doctype=%s and field='modified'", (SETTINGS,)
		)
		backfill()
		after = frappe.db.sql(
			"select value from tabSingles where doctype=%s and field='modified'", (SETTINGS,)
		)
		self.assertEqual(before, after)

	def test_full_settings_save_keeps_switches_on_after_backfill(self):
		_drop_rows()
		backfill()
		# A committed, keyless model row left by another module would fail validate_models;
		# clearing it here is inside the test transaction and tearDown rolls it back.
		doc = frappe.get_single(SETTINGS)
		doc.set("models", [])
		doc.save(ignore_permissions=True)
		for field in SWITCHES:
			self.assertEqual(cint(_row(field)[0][0]), 1)

	def test_patch_is_registered_post_model_sync(self):
		text = frappe.get_app_path("jarvis", "patches.txt")
		with open(text) as fh:
			body = fh.read()
		post = body.split("[post_model_sync]", 1)[1]
		self.assertIn("jarvis.patches.v2_29_backfill_holiday_touch_switches", post)


def _fresh_install_state():
	"""What a fresh install leaves: other Jarvis Settings rows exist, the two switches do not."""
	frappe.db.set_single_value(SETTINGS, "agent_name", "Jarvis")
	_drop_rows()
	frappe.db.value_cache.pop(SETTINGS, None)


class TestFreshInstallSwitches(FrappeTestCase):
	"""``after_install`` writes other Single rows, so the field default never reaches the
	database: the switches must read ON with no row, and the seeder must create the rows."""

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.value_cache.pop(SETTINGS, None)

	def test_absent_switch_rows_read_on(self):
		_fresh_install_state()
		site = settings.site()
		for field in SWITCHES:
			self.assertTrue(settings.on(site, field), field)

	def test_an_explicit_zero_reads_off(self):
		_fresh_install_state()
		frappe.db.set_single_value(SETTINGS, "holiday_touches_enabled", 0)
		site = settings.site()
		self.assertFalse(settings.on(site, "holiday_touches_enabled"))
		self.assertTrue(settings.on(site, "birthday_greetings_enabled"))

	def test_other_switches_still_read_off_when_absent(self):
		_fresh_install_state()
		frappe.db.sql("delete from tabSingles where doctype=%s and field='holiday_policy_disabled'", SETTINGS)
		self.assertFalse(settings.on(settings.site(), "holiday_policy_disabled"))

	def test_the_seeder_creates_both_rows_as_one(self):
		_fresh_install_state()
		self.assertTrue(seed_defaults())
		for field in SWITCHES:
			self.assertEqual(cint(_row(field)[0][0]), 1, field)

	def test_a_full_save_after_the_seeder_keeps_both_on(self):
		_fresh_install_state()
		seed_defaults()
		doc = frappe.get_single(SETTINGS)
		doc.set("models", [])  # see test_full_settings_save_keeps_switches_on_after_backfill
		doc.save(ignore_permissions=True)
		for field in SWITCHES:
			self.assertEqual(cint(_row(field)[0][0]), 1, field)
		self.assertTrue(settings.on(settings.site(), "holiday_touches_enabled"))

	def test_an_explicit_zero_survives_the_seeder(self):
		_fresh_install_state()
		frappe.db.set_single_value(SETTINGS, "birthday_greetings_enabled", 0)
		seed_defaults()
		self.assertEqual(cint(_row("birthday_greetings_enabled")[0][0]), 0)
		self.assertEqual(cint(_row("holiday_touches_enabled")[0][0]), 1)
		self.assertFalse(settings.on(settings.site(), "birthday_greetings_enabled"))

	def test_a_field_missing_from_the_meta_still_reads_off(self):
		_fresh_install_state()
		real = frappe.get_meta

		def meta(doctype, *args, **kwargs):
			if doctype == SETTINGS:
				return type("M", (), {"has_field": lambda self, f: False})()
			return real(doctype, *args, **kwargs)

		with mock_patch("frappe.get_meta", side_effect=meta):
			self.assertFalse(settings.on(settings.site(), "holiday_touches_enabled"))


class TestShownTimes(FrappeTestCase):
	def _row(self, shown):
		return {"holiday_touch_shown": shown}

	def test_readable_times_are_returned(self):
		raw = json.dumps({"date": "2026-11-08", "times": ["2026-11-08T04:30:00+00:00", "junk"]})
		self.assertEqual(len(settings.shown_times(self._row(raw))), 1)

	def test_a_long_list_is_capped_to_the_last_ten(self):
		times = [f"2026-11-{n:02d}T04:30:00+00:00" for n in range(1, 29)]
		found = settings.shown_times(self._row(json.dumps({"times": times})))
		self.assertEqual(len(found), 10)
		self.assertEqual(found[-1].day, 28)
		self.assertEqual(found[0].day, 19)

	def test_deeply_nested_json_is_junk_not_an_error(self):
		deep = '{"times": ' + "[" * 200000 + "]" * 200000 + "}"
		self.assertEqual(settings.shown_times(self._row(deep)), [])
		self.assertEqual(settings.shown_times(self._row("[" * 200000)), [])

	def test_other_junk_is_ignored(self):
		for raw in ("{not json", "null", '{"times": "x"}', '{"times": 5}', "[]", ""):
			self.assertEqual(settings.shown_times(self._row(raw)), [], raw)
		self.assertEqual(settings.shown_times(None), [])


class TestHolidayUserSettingsFields(_UsageTestBase):
	def test_existing_rows_read_show_holiday_touches_one(self):
		# A row that predates the column: insert it without naming the column, the way an
		# ALTER ... NOT NULL DEFAULT 1 migrate leaves every old row.
		frappe.db.sql(
			"insert into `tabJarvis User Settings` (name, creation, modified, modified_by, owner, user)"
			" values (%s, now(), now(), 'Administrator', %s, %s)",
			("legacy-holiday@example.test", USER_A, "legacy-holiday@example.test"),
		)
		try:
			value = frappe.db.sql(
				"select show_holiday_touches from `tabJarvis User Settings` where name=%s",
				("legacy-holiday@example.test",),
			)[0][0]
			self.assertEqual(cint(value), 1)
		finally:
			frappe.db.sql(
				"delete from `tabJarvis User Settings` where name=%s", ("legacy-holiday@example.test",)
			)

	def test_new_row_defaults_on_and_shown_state_empty(self):
		doc = usage.get_or_create_user_settings(USER_A)
		self.assertEqual(cint(doc.show_holiday_touches), 1)
		self.assertFalse(doc.holiday_touch_shown)

	def test_owner_cannot_write_holiday_touch_shown_by_document_save(self):
		# Frappe does not raise for a permlevel-1 write by a user with only permlevel-0
		# access: Document.validate_higher_perm_levels (frappe/model/document.py) resets the
		# field to the stored value (reset_values_if_no_permlevel_access). The write is
		# silently dropped, not rejected.
		usage.get_or_create_user_settings(USER_A)  # owner = USER_A, so the if_owner grant holds
		frappe.set_user(USER_A)
		doc = frappe.get_doc(USETT, USER_A)
		doc.holiday_touch_shown = '{"date": "2026-12-25", "times": ["2026-12-25T01:00:00Z"]}'
		doc.show_holiday_touches = 0  # a permlevel-0 field in the same save still lands
		doc.save()
		frappe.set_user("Administrator")
		stored = frappe.db.get_value(
			USETT, USER_A, ["holiday_touch_shown", "show_holiday_touches"], as_dict=True
		)
		self.assertFalse(stored.holiday_touch_shown)
		self.assertEqual(cint(stored.show_holiday_touches), 0)
