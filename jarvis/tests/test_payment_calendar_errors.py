"""The fixed vocabulary of failure codes and the only text a failure log may carry."""

from unittest import TestCase

import frappe

from jarvis.payment_calendar import errors
from jarvis.payment_calendar.errors import CalendarSourceUnavailable, ScopeChanged
from jarvis.payment_calendar.projection import ProjectionError


class TestSafeCode(TestCase):
	def test_a_known_code_passes_and_anything_else_takes_the_default(self):
		self.assertEqual(errors.safe_code(CalendarSourceUnavailable("too_large")), "too_large")
		self.assertEqual(errors.safe_code(ScopeChanged()), "scope_changed")
		self.assertEqual(errors.safe_code(CalendarSourceUnavailable("PRIVATE party text")), None)
		self.assertEqual(errors.safe_code(CalendarSourceUnavailable("PRIVATE"), "fallback"), "fallback")
		self.assertEqual(errors.safe_code(ValueError("too_large"), "fallback"), "fallback")

	def test_a_projection_error_uses_the_same_vocabulary(self):
		code = next(iter(errors.SAFE_CODES))
		self.assertEqual(errors.safe_code(ProjectionError(code)), code)
		self.assertIsNone(errors.safe_code(ProjectionError("PRIVATE text")))


class TestSafeDetail(TestCase):
	def test_a_key_error_names_its_keys_when_they_are_fixed_names(self):
		self.assertEqual(
			errors.safe_detail(KeyError("horizon", "projection.month_totals")),
			"missing key 'horizon', 'projection.month_totals'",
		)

	def test_a_key_that_may_come_from_data_is_never_logged(self):
		for key in ("ACME Ltd", "supplier 1", "PUR-INV-0001", "party-name", "", 42, ("a", "b")):
			with self.subTest(key=key):
				self.assertIsNone(errors.safe_detail(KeyError(key)))

	def test_one_data_derived_key_hides_the_whole_list(self):
		self.assertIsNone(errors.safe_detail(KeyError("horizon", "ACME Ltd")))

	def test_a_fixed_code_is_logged_and_other_text_is_not(self):
		self.assertEqual(errors.safe_detail(CalendarSourceUnavailable("too_large")), "too_large")
		self.assertIsNone(errors.safe_detail(frappe.ValidationError("PRIVATE")))
		self.assertIsNone(errors.safe_detail(CalendarSourceUnavailable("PRIVATE")))
