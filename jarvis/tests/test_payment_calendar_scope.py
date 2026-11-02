import json
from unittest import TestCase

import frappe
from frappe.utils import add_months, get_first_day, get_last_day, nowdate

from jarvis.payment_calendar import scope
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarScope(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_scope")
		self.fx = CalendarFixtures()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_scope")

	def test_scope_uses_site_date_and_complete_bounded_history_and_future_window(self):
		result = scope.normalize_scope(self.fx.company, [self.fx.bank])
		anchor = nowdate()
		self.assertEqual(result["anchor"], anchor)
		self.assertEqual(result["selectable_start"], str(get_first_day(add_months(anchor, -12))))
		self.assertEqual(result["selectable_end"], str(get_last_day(add_months(anchor, 11))))
		self.assertEqual(result["window_start"], result["selectable_start"])
		self.assertEqual(result["window_end"], frappe.utils.add_days(result["selectable_end"], 30))
		self.assertEqual(result["actor"], frappe.session.user)

	def test_account_ids_are_canonical_sorted_and_deduplicated(self):
		other = self.fx.account("OtherCash", "Cash", "Asset")
		result = scope.normalize_scope(self.fx.company, [self.fx.bank.lower(), other, self.fx.bank])
		self.assertEqual(result["accounts"], sorted([self.fx.bank, other]))

	def test_encoded_account_array_is_supported_without_filter_shaped_values(self):
		self.assertEqual(
			scope.normalize_scope(self.fx.company, json.dumps([self.fx.bank]))["accounts"], [self.fx.bank]
		)
		for value in ({"name": ["like", "%"]}, "{}", [True], [["like", "%"]], [None]):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				scope.normalize_scope(self.fx.company, value)

	def test_no_accounts_is_an_explicit_dues_only_scope(self):
		self.assertEqual(scope.normalize_scope(self.fx.company, [])["accounts"], [])

	def test_more_than_fifty_requested_accounts_is_rejected_before_document_probes(self):
		with self.assertRaises(frappe.ValidationError):
			scope.normalize_scope(self.fx.company, [f"BANK-{i}" for i in range(51)])

	def test_company_filter_objects_are_not_treated_as_company_ids(self):
		for value in (None, [], {"name": ["like", "%"]}, True, ""):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				scope.normalize_scope(value, [])

	def test_an_account_of_another_company_is_refused(self):
		other = CalendarFixtures()
		with self.assertRaises(frappe.ValidationError):
			scope.normalize_scope(self.fx.company, [other.bank])

	def test_company_restriction_is_checked_before_accepting_a_scope(self):
		other = CalendarFixtures()
		user = self.fx.user()
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": "Company",
				"for_value": other.company,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			scope.normalize_scope(self.fx.company, [])
