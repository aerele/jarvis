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
from jarvis.payment_calendar.errors import (
	READER_FALLBACK_REASONS,
	CalendarSourceUnavailable,
	ReaderFallback,
)


class TestPaymentCalendarCompatibility(TestCase):
	def test_installed_pair_matches_the_audited_allocation_contract(self):
		sources._check_contract()

	def test_installed_bank_helper_matches_an_audited_finance_book_contract(self):
		from erpnext.accounts.utils import get_balance_on

		bank_balance_arguments(get_balance_on)

	def test_earlier_audited_source_revisions_stay_accepted(self):
		profiles = compatibility._PROFILES
		self.assertIn(
			"da22739f601983c4070b44a73bdefb683286145aefc89c2374f684aed32bd0f3",
			profiles["16"].payment_schedule_hashes,
		)
		self.assertIn(
			"fb85c419791fe15830f0d5794782897914a4946ef75c0c90972c65145ce07a77",
			profiles["15"].payment_schedule_hashes,
		)
		self.assertEqual(len(profiles["16"].payment_schedule_hashes), 2)
		self.assertEqual(len(profiles["15"].payment_schedule_hashes), 3)

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

	def test_reader_pins_match_installed_source(self):
		"""Deliberate tripwire: any edit of a pinned upstream source needs a fresh audit of the reader."""
		drifted = compatibility.reader_pin_drift()
		self.assertEqual(
			drifted,
			[],
			"Reader pins drifted; audit each and update _READER_PINS:\n"
			+ "\n".join(f"{name} {compatibility.qualified_source_hash(name)}" for name in drifted),
		)

	def test_reader_pins_cover_the_audited_upstream_names(self):
		pinned = set(compatibility._READER_PINS["16"])
		rpr = "erpnext.accounts.report.accounts_receivable.accounts_receivable.ReceivablePayableReport."
		for method in (
			"get_data",
			"get_invoice_details",
			"init_voucher_balance",
			"get_voucher_balance",
			"update_voucher_balance",
			"prepare_conditions",
			"add_common_filters",
			"exclude_employee_transaction",
			"prepare_ple_query",
			"build_data",
			"get_return_entries",
			"fetch_ple_in_buffered_cursor",
			"fetch_ple_in_unbuffered_cursor",
			"build_delivery_note_map",
		):
			self.assertIn(rpr + method, pinned)
		self.assertIn("erpnext.accounts.utils.update_voucher_outstanding", pinned)
		self.assertIn("erpnext.accounts.utils.build_qb_match_conditions", pinned)
		self.assertIn("frappe.desk.query_report.get_filtered_data", pinned)
		restriction = "erpnext.stock.doctype.company_restriction.company_restriction."
		for function in (
			"get_permission_query_conditions",
			"has_permission",
			"get_inherited_permission_query_conditions",
			"has_inherited_permission",
		):
			self.assertIn(restriction + function, pinned)

	def test_qualified_source_hash_resolves_functions_and_methods(self):
		from hashlib import sha256
		from inspect import getsource

		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport
		from erpnext.accounts.utils import update_voucher_outstanding

		qualified = "erpnext.accounts.utils.update_voucher_outstanding"
		self.assertEqual(
			compatibility.qualified_source_hash(qualified),
			sha256(getsource(update_voucher_outstanding).encode()).hexdigest(),
		)
		method = (
			"erpnext.accounts.report.accounts_receivable.accounts_receivable.ReceivablePayableReport.get_data"
		)
		self.assertEqual(
			compatibility.qualified_source_hash(method),
			sha256(getsource(ReceivablePayableReport.get_data).encode()).hexdigest(),
		)

	def test_qualified_source_hash_is_none_for_anything_unreadable(self):
		for qualified in (
			"jarvis_no_such_module.function",
			"erpnext.accounts.utils.jarvis_no_such_function",
			"erpnext.accounts.report.accounts_receivable.accounts_receivable.NoClass.get_data",
			"erpnext.accounts.utils.__name__",
		):
			with self.subTest(qualified=qualified):
				self.assertIsNone(compatibility.qualified_source_hash(qualified))
		with patch.object(compatibility, "getsource", side_effect=OSError):
			self.assertIsNone(
				compatibility.qualified_source_hash("erpnext.accounts.utils.update_voucher_outstanding")
			)

	def test_reader_pin_drift_lists_unknown_and_unreadable_sources(self):
		name = "erpnext.accounts.utils.update_voucher_outstanding"
		pins = {"16": {**compatibility._READER_PINS["16"], name: ("unknown",)}}
		with patch.object(compatibility, "_READER_PINS", pins):
			self.assertEqual(compatibility.reader_pin_drift(), [name])
			self.assertEqual(compatibility.reader_pin_drift([name]), [name])
			self.assertEqual(
				compatibility.reader_pin_drift(["erpnext.accounts.utils.no_pin"]),
				["erpnext.accounts.utils.no_pin"],
			)
		with patch.object(compatibility, "getsource", side_effect=OSError):
			self.assertEqual(compatibility.reader_pin_drift(), sorted(compatibility._READER_PINS["16"]))

	def test_reader_pins_have_no_v15_entry_so_v15_drifts_everything(self):
		self.assertNotIn("15", compatibility._READER_PINS)
		with patch.object(erpnext, "__version__", "15.0.0"):
			self.assertEqual(compatibility.reader_pin_drift(), sorted(compatibility._READER_PINS["16"]))

	def test_reader_fallback_reasons_and_refusal_codes(self):
		self.assertEqual(
			READER_FALLBACK_REASONS,
			{
				"ineligible",
				"eligibility_unknown",
				"unsupported_major",
				"pin_drift",
				"sales_person_scope",
				"control_total",
				"reader_error",
				"rekey_unresolved",
				"too_many_lines",
			},
		)
		codes = {
			"too_many_lines": "too_large",
			"control_total": "reconciliation_mismatch",
			"pin_drift": "source_contract_unavailable",
			"unsupported_major": "source_contract_unavailable",
		}
		for reason in READER_FALLBACK_REASONS:
			with self.subTest(reason=reason):
				error = ReaderFallback(reason)
				self.assertEqual(error.reason, reason)
				self.assertEqual(error.refusal_code, codes.get(reason))
				self.assertNotIn(reason, str(error))
				self.assertNotIsInstance(error, CalendarSourceUnavailable)
		with self.assertRaises(ValueError):
			ReaderFallback("not a reason")
		self.assertEqual(ReaderFallback("pin_drift").detail, ())
		self.assertEqual(ReaderFallback("pin_drift", ("a.b", "c.d")).detail, ("a.b", "c.d"))
		for bad in (["a.b"], ("a.b", 1), "a.b", ({"party": "x"},)):
			with self.subTest(bad=bad), self.assertRaises(ValueError):
				ReaderFallback("pin_drift", bad)
		detailed = ReaderFallback("ineligible", ("user_permission",))
		self.assertNotIn("user_permission", str(detailed), "the message stays fixed")
		self.assertEqual(str(ReaderFallback("ineligible")), str(ReaderFallback("pin_drift")))


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
