from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar.banks import read_bank_balances
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarBanks(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_bank")
		self.fx = CalendarFixtures()
		self.fx.fiscal_year()
		frappe.cache().hdel("fiscal_years", self.fx.company)

	def tearDown(self):
		frappe.local.form_dict = frappe._dict()
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_bank")
		frappe.cache().hdel("fiscal_years", self.fx.company)

	def test_signed_balance_and_duplicates_do_not_double_count(self):
		self.fx.gl(-125)
		result = read_bank_balances(self.fx.company, [self.fx.bank, self.fx.bank], self.fx.today)
		self.assertEqual(Decimal(result["total"]), Decimal("-125"))
		self.assertEqual(len(result["accounts"]), 1)
		self.assertEqual(result["currency"], "INR")

	def test_missing_accounting_date_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			read_bank_balances(self.fx.company, [self.fx.bank], None)

	def test_foreign_currency_account_returns_company_currency_book_value(self):
		frappe.db.set_value("Account", self.fx.bank, "account_currency", "USD")
		self.fx.gl(8300)
		result = read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)
		self.assertEqual(Decimal(result["total"]), Decimal("8300"))

	def test_request_fields_do_not_change_the_canonical_bank_call(self):
		self.fx.gl(100)
		forged = frappe._dict(date="1990-01-01", cost_center="forged", party_type="Supplier", party="forged")
		frappe.local.form_dict = forged
		result = read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)
		self.assertEqual(Decimal(result["total"]), 100)
		self.assertIs(frappe.local.form_dict, forged)

	def test_group_disabled_wrong_company_and_non_cash_accounts_are_refused(self):
		for field, value in (("is_group", 1), ("disabled", 1), ("account_type", "Receivable")):
			old = frappe.db.get_value("Account", self.fx.bank, field)
			frappe.db.set_value("Account", self.fx.bank, field, value)
			with self.subTest(field=field), self.assertRaises(frappe.ValidationError):
				read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)
			frappe.db.set_value("Account", self.fx.bank, field, old)
		other = CalendarFixtures()
		with self.assertRaises(frappe.ValidationError):
			read_bank_balances(self.fx.company, [other.bank], self.fx.today)

	def test_real_zero_is_valid_but_missing_fiscal_coverage_is_unavailable(self):
		from erpnext.accounts.utils import FiscalYearError

		self.assertEqual(
			Decimal(read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)["total"]), 0
		)
		with patch("erpnext.accounts.utils.get_fiscal_year", side_effect=FiscalYearError):
			with self.assertRaises(frappe.ValidationError):
				read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)

	def test_no_accounts_does_not_become_zero_cash(self):
		with self.assertRaises(frappe.ValidationError):
			read_bank_balances(self.fx.company, [], self.fx.today)

	def test_non_default_finance_book_is_refused(self):
		self.fx.gl(100, finance_book="Other Finance Book")
		with self.assertRaises(frappe.ValidationError):
			read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)

	def test_missing_real_fiscal_year_is_unavailable_not_zero(self):
		# Only this transaction sees disabled fixture years; tearDown rolls it back.
		frappe.db.set_value("Fiscal Year", {"disabled": 0}, "disabled", 1)
		frappe.cache().hdel("fiscal_years", self.fx.company)
		with self.assertRaises(frappe.ValidationError):
			read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)
