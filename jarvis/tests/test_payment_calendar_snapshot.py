"""Real source transactions, including independent committed accounting changes."""

from threading import Thread
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar import snapshot
from jarvis.payment_calendar.access import ScopeChanged, authorize_scope
from jarvis.payment_calendar.banks import read_bank_balances
from jarvis.payment_calendar.compatibility import source_profile
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.settings import get_settings, update_safety_floor
from jarvis.payment_calendar.sources import read_obligations
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarSnapshot(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		self.fx = CalendarFixtures()
		self.ap = self.fx.invoice((100,))
		self.ar = self.fx.invoice((100,), side="Receivable")
		self.fx.fiscal_year()
		self.fx.gl(1000)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()
		for name in frappe.get_all(
			"Jarvis Payment Calendar Settings", filters={"company": self.fx.company}, pluck="name"
		):
			frappe.db.delete("Version", {"ref_doctype": "Jarvis Payment Calendar Settings", "docname": name})
			frappe.db.delete("Jarvis Payment Calendar Settings", {"name": name})
		for doctype, name in reversed(self.fx.created):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		frappe.cache.hdel("fiscal_years", self.fx.company)
		frappe.clear_document_cache("Company", self.fx.company)

	def commit_independently(self, callback):
		site, sites = frappe.local.site, frappe.local.sites_path
		errors = []

		def writer():
			try:
				frappe.init(site=site, sites_path=sites)
				frappe.connect()
				frappe.set_user("Administrator")
				callback()
				frappe.db.commit()
			except Exception as exc:
				errors.append(type(exc).__name__)
			finally:
				frappe.destroy()

		thread = Thread(target=writer)
		thread.start()
		thread.join(30)
		self.assertFalse(thread.is_alive())
		self.assertEqual(errors, [])

	def test_independent_cancellation_cannot_mix_old_payable_with_new_cash(self):
		def cancel():
			frappe.db.set_value("Payment Ledger Entry", self.fx.prefix + "InvoicePLE1", "delinked", 1)
			frappe.db.set_value("Purchase Invoice", self.ap, "docstatus", 2)
			self.fx.gl(100)

		with snapshot.consistent_read():
			self.assertEqual(self.read("Payable"), "100.00")
			self.commit_independently(cancel)
			self.assertEqual(self.read("Payable"), "100.00")
			self.assertEqual(
				read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)["total"], "1000.00"
			)
		self.assertEqual(self.read("Payable"), "0")
		self.assertEqual(
			read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)["total"], "1100.00"
		)

	def test_independent_floor_changes_keep_the_in_view_revision_fixed(self):
		update_safety_floor(self.fx.company, "20", 0)
		frappe.db.commit()
		with snapshot.consistent_read():
			self.assertEqual(get_settings(self.fx.company)["revision"], 1)
			self.commit_independently(lambda: update_safety_floor(self.fx.company, "30", 1))
			self.assertEqual(get_settings(self.fx.company)["safety_floor"], "20.00")
		self.assertEqual(get_settings(self.fx.company)["revision"], 2)

	def test_collect_refuses_a_floor_revision_committed_during_the_read(self):
		update_safety_floor(self.fx.company, "20", 0)
		frappe.db.commit()
		scope = normalize_scope(self.fx.company, [self.fx.bank])
		proof = authorize_scope(scope)

		def bank(*args):
			result = read_bank_balances(*args)
			self.commit_independently(lambda: update_safety_floor(self.fx.company, "30", 1))
			return result

		with patch.object(snapshot, "read_bank_balances", side_effect=bank), self.assertRaises(ScopeChanged):
			snapshot.collect(scope, proof["fingerprint"])

	def test_stale_shared_company_cache_is_neither_used_nor_overwritten(self):
		doc = frappe.get_doc("Company", self.fx.company)
		doc.default_currency = "USD"
		key = frappe.get_document_cache_key("Company", self.fx.company)
		frappe.cache.set_value(key, doc, expires_in_sec=60)
		with snapshot.consistent_read():
			self.assertEqual(frappe.get_cached_value("Company", self.fx.company, "default_currency"), "INR")
		self.assertEqual(frappe.cache.get_value(key).default_currency, "USD")

	def test_stale_precision_cache_is_bypassed_without_publishing_old_defaults(self):
		from erpnext.accounts.utils import get_currency_precision

		profile = source_profile()
		if profile.major == "15":

			def get():
				return frappe.cache.hget("defaults", "__default")

			def set_value(value):
				frappe.cache.hset("defaults", "__default", value)

			def delete():
				frappe.cache.hdel("defaults", "__default")
		else:

			def get():
				return frappe.client_cache.get_value("defaults::__default")

			def set_value(value):
				frappe.client_cache.set_value("defaults::__default", value)

			def delete():
				frappe.client_cache.delete_value("defaults::__default")

		original = get()
		try:
			set_value(frappe._dict(currency_precision="7"))
			with snapshot.consistent_read() as hits:
				self.assertNotEqual(get_currency_precision(), 7)
				self.assertIn(profile.defaults_cache_hit, hits)
			self.assertEqual(get().currency_precision, "7")
		finally:
			delete()
			if original is not None:
				set_value(original)

	def test_stale_fiscal_year_cache_is_bypassed_without_publishing_the_read_view(self):
		from erpnext.accounts.utils import get_fiscal_year

		original = frappe.cache.hget("fiscal_years", self.fx.company)
		stale = [
			frappe._dict(name="stale fiscal year", year_start_date="1990-01-01", year_end_date="1990-12-31")
		]
		try:
			frappe.cache.hset("fiscal_years", self.fx.company, stale)
			with snapshot.consistent_read() as hits:
				self.assertNotEqual(
					get_fiscal_year(self.fx.today, company=self.fx.company)[0], "stale fiscal year"
				)
				self.assertIn(("hget", "fiscal_years"), hits)
			self.assertEqual(frappe.cache.hget("fiscal_years", self.fx.company), stale)
		finally:
			frappe.cache.hdel("fiscal_years", self.fx.company)
			if original is not None:
				frappe.cache.hset("fiscal_years", self.fx.company, original)

	def read(self, side):
		return read_obligations(
			self.fx.company, side, self.fx.today, self.fx.today, add_days(self.fx.today, 40)
		)["canonical_total"]

	def test_independent_payments_do_not_mix_bank_ap_and_ar_views(self):
		site, sites = frappe.local.site, frappe.local.sites_path
		errors = []

		def writer():
			try:
				frappe.init(site=site, sites_path=sites)
				frappe.connect()
				frappe.set_user("Administrator")
				self.fx.ple("IndependentAP", "Payment Entry", self.ap, -30)
				self.fx.ple(
					"IndependentAR",
					"Payment Entry",
					self.ar,
					-40,
					side="Receivable",
					party=self.fx.customer,
					account=self.fx.receivable,
				)
				self.fx.gl(-30)
				self.fx.gl(40)
				frappe.db.commit()
			except Exception as exc:
				errors.append(type(exc).__name__)
			finally:
				frappe.destroy()

		with snapshot.consistent_read():
			connection = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
			before = read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)["total"]
			thread = Thread(target=writer)
			thread.start()
			thread.join(30)
			self.assertFalse(thread.is_alive())
			self.assertEqual(errors, [])
			self.assertEqual(
				(before, self.read("Payable"), self.read("Receivable")), ("1000.00", "100.00", "100.00")
			)
			self.assertEqual(frappe.db.sql("SELECT CONNECTION_ID()")[0][0], connection)
		self.assertEqual(self.read("Payable"), "70.00")
		self.assertEqual(self.read("Receivable"), "60.00")
		self.assertEqual(
			read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)["total"], "1010.00"
		)

	def test_source_write_is_refused_and_nothing_is_changed(self):
		with self.assertRaises(Exception), snapshot.consistent_read():
			frappe.db.set_value("Company", self.fx.company, "abbr", "CHANGED")
		self.assertNotEqual(frappe.db.get_value("Company", self.fx.company, "abbr"), "CHANGED")

	def test_helper_commit_breaks_the_contract(self):
		with self.assertRaises(CalendarSourceUnavailable), snapshot.consistent_read():
			frappe.db.commit()

	def test_helper_rollback_breaks_the_contract(self):
		with self.assertRaises(CalendarSourceUnavailable), snapshot.consistent_read():
			frappe.db.rollback()

	def test_raw_transaction_control_breaks_the_contract(self):
		with self.assertRaises(CalendarSourceUnavailable), snapshot.consistent_read():
			frappe.db.sql("COMMIT")

	def test_cached_company_cannot_supply_another_currency(self):
		frappe.get_cached_doc("Company", self.fx.company)
		frappe.db.set_value("Company", self.fx.company, "default_currency", "USD")
		frappe.db.commit()
		with snapshot.consistent_read():
			self.assertEqual(frappe.get_cached_value("Company", self.fx.company, "default_currency"), "USD")

	def test_cached_helpers_are_restored_on_failure(self):
		original = frappe.get_cached_value
		with self.assertRaises(ValueError), snapshot.consistent_read():
			raise ValueError("test")
		self.assertIs(frappe.get_cached_value, original)
