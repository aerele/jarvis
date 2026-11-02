"""The weekly-off lookup against the real `holidays` package."""

import sys
import unittest
from datetime import date
from unittest import mock

import frappe

from jarvis.holiday_touch import weekly_off
from jarvis.holiday_touch.match import match_occasion
from jarvis.holiday_touch.weekly_off import calendar_name, country_code_for


def _occasion(name, on_date, country="India"):
	return match_occasion(name, on_date, country)


class TestCalendarName(unittest.TestCase):
	def setUp(self):
		weekly_off._year_map.cache_clear()
		self.addCleanup(weekly_off._year_map.cache_clear)

	def test_india_diwali_on_a_sunday(self):
		on = date(2026, 11, 8)
		name = calendar_name(on, "IN")
		self.assertTrue(name)
		self.assertEqual(_occasion(name, on), "diwali")

	def test_lowercase_country_code_is_accepted(self):
		on = date(2026, 11, 8)
		self.assertEqual(calendar_name(on, "in"), calendar_name(on, "IN"))
		self.assertEqual(_occasion(calendar_name(on, "in"), on), "diwali")

	def test_other_country_has_no_indian_festival(self):
		self.assertIsNone(calendar_name(date(2026, 11, 8), "US"))

	def test_tamil_nadu_pongal(self):
		on = date(2026, 1, 14)
		self.assertEqual(_occasion(calendar_name(on, "IN", "TN"), on), "pongal")

	def test_kerala_onam_beats_the_masked_milad(self):
		on = date(2026, 8, 26)
		self.assertEqual(_occasion(calendar_name(on, "IN", "KL"), on), "onam")

	def test_maharashtra_bali_pratipada_is_diwali(self):
		on = date(2026, 11, 10)
		name = calendar_name(on, "IN", "MH")
		self.assertIn("Bali Pratipada", name)
		self.assertEqual(_occasion(name, on), "diwali")

	def test_a_date_with_no_holiday_gives_none(self):
		self.assertIsNone(calendar_name(date(2026, 6, 10), "IN"))

	def test_language_is_pinned_to_english_whatever_the_site_language(self):
		saved = getattr(frappe.local, "lang", None)
		self.addCleanup(setattr, frappe.local, "lang", saved)
		on = date(2026, 11, 8)
		for lang in ("hi", "mr"):
			with self.subTest(lang=lang):
				weekly_off._year_map.cache_clear()
				frappe.local.lang = lang
				self.assertEqual(_occasion(calendar_name(on, "IN"), on), "diwali")

	def test_language_argument_is_en_in_for_india_and_en_us_elsewhere(self):
		import holidays

		real = holidays.country_holidays
		with mock.patch.object(holidays, "country_holidays", side_effect=real) as spy:
			calendar_name(date(2026, 11, 8), "IN")
			calendar_name(date(2026, 11, 8), "US")
		self.assertEqual([c.kwargs["language"] for c in spy.call_args_list], ["en_IN", "en_US"])

	def test_a_rejected_language_is_retried_without_it(self):
		import holidays

		real = holidays.country_holidays

		def fussy(code, **kw):
			if "language" in kw:
				raise ValueError("no such language")
			return real(code, **kw)

		on = date(2026, 11, 8)
		with mock.patch.object(holidays, "country_holidays", side_effect=fussy):
			self.assertEqual(_occasion(calendar_name(on, "IN"), on), "diwali")

	def test_import_failure_gives_none_without_raising(self):
		with mock.patch.dict(sys.modules, {"holidays": None}):
			self.assertIsNone(calendar_name(date(2026, 11, 8), "IN"))

	def test_any_error_gives_none(self):
		import holidays

		with mock.patch.object(holidays, "country_holidays", side_effect=RuntimeError("boom")):
			self.assertIsNone(calendar_name(date(2026, 11, 8), "IN"))

	def test_an_unknown_country_gives_none(self):
		self.assertIsNone(calendar_name(date(2026, 11, 8), "ZZ"))
		self.assertIsNone(calendar_name(date(2026, 11, 8), "ZZ", "XX"))

	def test_an_invalid_subdivision_is_retried_without_it(self):
		on = date(2026, 11, 8)
		name = calendar_name(on, "IN", "XX")  # Diwali is national: the country calendar still names it
		self.assertTrue(name)
		self.assertEqual(name, calendar_name(on, "IN"))
		self.assertEqual(_occasion(name, on), "diwali")

	def test_a_valid_subdivision_is_not_dropped(self):
		import holidays

		real = holidays.country_holidays
		with mock.patch.object(holidays, "country_holidays", side_effect=real) as spy:
			calendar_name(date(2026, 1, 14), "IN", "TN")
		self.assertEqual([c.kwargs["subdiv"] for c in spy.call_args_list], ["TN"])

	def test_missing_country_gives_none(self):
		self.assertIsNone(calendar_name(date(2026, 11, 8), None))
		self.assertIsNone(calendar_name(date(2026, 11, 8), ""))

	def test_the_year_map_is_built_once_per_country_subdivision_and_year(self):
		import holidays

		real = holidays.country_holidays
		with mock.patch.object(holidays, "country_holidays", side_effect=real) as spy:
			calendar_name(date(2026, 11, 8), "IN")
			calendar_name(date(2026, 11, 10), "IN")
			calendar_name(date(2026, 11, 10), "IN", "MH")
			calendar_name(date(2027, 1, 1), "IN")
		self.assertEqual(spy.call_count, 3)


class TestCountryCodeFor(unittest.TestCase):
	def test_india(self):
		self.assertEqual(country_code_for("India"), "IN")

	def test_unknown_blank_and_none(self):
		self.assertIsNone(country_code_for("Atlantis"))
		self.assertIsNone(country_code_for(""))
		self.assertIsNone(country_code_for(None))

	def test_code_is_uppercased(self):
		with mock.patch.object(frappe, "get_cached_value", return_value="in"):
			self.assertEqual(country_code_for("India"), "IN")

	def test_lookup_error_gives_none(self):
		with mock.patch.object(frappe, "get_cached_value", side_effect=RuntimeError("db down")):
			self.assertIsNone(country_code_for("India"))
