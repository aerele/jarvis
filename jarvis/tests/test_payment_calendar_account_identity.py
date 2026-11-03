from unittest import TestCase

import frappe

from jarvis.payment_calendar.banks import read_bank_balances
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarAccountIdentity(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_account_identity")
		self.fx = CalendarFixtures()
		self.fx.fiscal_year()
		frappe.cache.hdel("fiscal_years", self.fx.company)

	def tearDown(self):
		frappe.db.rollback(save_point="calendar_account_identity")
		frappe.cache.hdel("fiscal_years", self.fx.company)

	def test_case_aliases_cannot_double_count_one_bank_account(self):
		self.fx.gl(100)
		result = read_bank_balances(self.fx.company, [self.fx.bank, self.fx.bank.lower()], self.fx.today)
		self.assertEqual(len(result["accounts"]), 1)
		self.assertEqual(result["accounts"][0]["account"], self.fx.bank)
		self.assertEqual(result["total"], "100.00")
