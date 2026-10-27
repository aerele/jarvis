"""Version boundaries fail closed before accounting sources are consumed.

These tests use no database. The source suites separately exercise the real
allocator, cache paths and ledger balances on each installed framework pair.
"""

from unittest import TestCase
from unittest.mock import patch

import erpnext
import frappe

from jarvis.payment_calendar import compatibility, reconciliation, snapshot, sources
from jarvis.payment_calendar.compatibility import (
	bank_balance_arguments,
	require_obligation_scope,
	source_profile,
)
from jarvis.payment_calendar.errors import CalendarSourceUnavailable


class TestPaymentCalendarCompatibility(TestCase):
	def test_installed_pair_matches_the_audited_allocation_contract(self):
		sources._check_contract()

	def test_installed_bank_helper_matches_an_audited_finance_book_contract(self):
		from erpnext.accounts.utils import get_balance_on

		bank_balance_arguments(get_balance_on)

	def test_matching_supported_framework_pairs_reach_the_allocation_contract(self):
		for major in ("15", "16"):
			with (
				self.subTest(major=major),
				patch.object(frappe, "__version__", major + ".0.0"),
				patch.object(erpnext, "__version__", major + ".0.0"),
				patch.object(sources, "check_allocation_contract") as check,
			):
				sources._check_contract()
				check.assert_called_once_with()

	def test_unknown_and_mixed_framework_pairs_are_rejected(self):
		for framework, accounting in (("14", "14"), ("17", "17"), ("15", "16"), ("16", "15")):
			with (
				self.subTest(framework=framework, accounting=accounting),
				patch.object(frappe, "__version__", framework + ".0.0"),
				patch.object(erpnext, "__version__", accounting + ".0.0"),
				patch.object(sources, "check_allocation_contract"),
				self.assertRaises(CalendarSourceUnavailable),
			):
				sources._check_contract()

	def test_report_signature_drift_is_rejected_on_both_supported_versions(self):
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		for major in ("15", "16"):
			with (
				self.subTest(major=major),
				patch.object(frappe, "__version__", major + ".0.0"),
				patch.object(erpnext, "__version__", major + ".0.0"),
				patch.object(sources, "check_allocation_contract"),
				patch.object(ReceivablePayableReport, "get_payment_terms", lambda self, row, changed: None),
				self.assertRaises(CalendarSourceUnavailable),
			):
				sources._check_contract()

	def test_unrecognized_classifier_source_is_rejected_on_both_versions(self):
		for major in ("15", "16"):
			with (
				self.subTest(major=major),
				patch.object(frappe, "__version__", major + ".0.0"),
				patch.object(erpnext, "__version__", major + ".0.0"),
				patch.object(reconciliation, "getsource", return_value="unreviewed allocation formula"),
				self.assertRaises(CalendarSourceUnavailable),
			):
				reconciliation.check_allocation_contract()

	def test_unknown_bank_helper_cannot_choose_finance_book_arguments(self):
		for major in ("15", "16"):
			with (
				self.subTest(major=major),
				patch.object(frappe, "__version__", major + ".0.0"),
				patch.object(erpnext, "__version__", major + ".0.0"),
				patch.object(compatibility, "getsource", return_value="unreviewed finance-book filter"),
				self.assertRaises(CalendarSourceUnavailable),
			):
				bank_balance_arguments(object())

	def test_sales_person_restriction_only_refuses_the_unsupported_v15_receivable_source(self):
		for major in ("15", "16"):
			for side in ("Payable", "Receivable"):
				with (
					self.subTest(major=major, side=side),
					patch.object(frappe, "__version__", major + ".0.0"),
					patch.object(erpnext, "__version__", major + ".0.0"),
					patch.object(compatibility, "get_user_permissions", return_value={"Sales Person": [{}]}),
				):
					if major == "15" and side == "Receivable":
						with self.assertRaises(CalendarSourceUnavailable) as error:
							require_obligation_scope(side)
						self.assertEqual(error.exception.code, "restricted_obligations")
					else:
						require_obligation_scope(side)
					with patch.object(compatibility, "get_user_permissions", return_value={}):
						require_obligation_scope(side)

	def test_cache_misses_do_not_read_or_replace_shared_financial_values(self):
		for major in ("15", "16"):
			with (
				self.subTest(major=major),
				patch.object(frappe, "__version__", major + ".0.0"),
				patch.object(erpnext, "__version__", major + ".0.0"),
			):
				cache, client = _Cache(), _Cache()
				cache.hset("defaults", "__default", "stale defaults")
				cache.hset("defaults", "another user", "private selection")
				cache.hset("fiscal_years", "company", "stale year")
				cache.hset("roles", "actor", "old roles")
				cache.set_value("document_cache::Company::company", "stale company")
				client.set_value("defaults::__default", "stale defaults")
				with (
					patch.object(frappe, "cache", cache),
					patch.object(frappe, "client_cache", client if major == "16" else None, create=True),
				):
					with self.assertRaisesRegex(RuntimeError, "reader failed"):
						with snapshot._source_caches() as hits:
							if major == "15":
								self.assertIsNone(cache.hget("defaults", "__default"))
								cache.hset("defaults", "__default", "fresh defaults")
							else:
								self.assertIsNone(client.get_value("defaults::__default"))
								client.set_value("defaults::__default", "fresh defaults")
							self.assertIn(source_profile().defaults_cache_hit, hits)
							self.assertEqual(cache.hget("defaults", "another user"), "private selection")
							self.assertIsNone(cache.get_value("document_cache::Company::company"))
							cache.set_value("document_cache::Company::company", "fresh company")
							self.assertIsNone(cache.hget("fiscal_years", "company"))
							cache.hset("fiscal_years", "company", "fresh year")
							self.assertEqual(
								cache.hget("roles", "actor", lambda: "current roles"), "current roles"
							)
							cache.hset("roles", "actor", "updated roles")
							self.assertEqual(cache.hget("roles", "actor"), "updated roles")
							raise RuntimeError("reader failed")
					self.assertEqual(cache.hget("defaults", "__default"), "stale defaults")
					self.assertEqual(client.get_value("defaults::__default"), "stale defaults")
					self.assertEqual(cache.get_value("document_cache::Company::company"), "stale company")
					self.assertEqual(cache.hget("fiscal_years", "company"), "stale year")
					self.assertEqual(cache.hget("roles", "actor"), "old roles")
					self.assertFalse({"get_value", "set_value", "hget", "hset"}.intersection(vars(cache)))


class _Cache:
	"""Only the cache operations exercised by the financial read window."""

	def __init__(self):
		self.values = {}
		self.hashes = {}

	def get_value(self, key):
		return self.values.get(key)

	def set_value(self, key, value):
		self.values[key] = value

	def hget(self, name, key, generator=None, shared=False):
		return self.hashes.get((name, key))

	def hset(self, name, key, value):
		self.hashes[(name, key)] = value
