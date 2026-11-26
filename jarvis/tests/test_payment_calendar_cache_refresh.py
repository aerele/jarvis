"""Payment Calendar S4b B4: `cache_refresh` guards, preflight, the reader build and the bank generation.

The job itself (claim, publish, loop, failures) is in `test_payment_calendar_cache_run`. These tests never
commit: the reader fixtures are transactional and the view is replaced by a plain context.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_refresh
"""

import contextlib
import resource
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar import builtin_reader, cache_refresh, cache_state, cache_store, snapshot, sources
from jarvis.payment_calendar.errors import CalendarSourceUnavailable, ReaderFallback
from jarvis.tests.payment_calendar_fixtures import (
	CalendarFixtures,
	require_calendar_test_site,
	sync_outstanding,
)


def real_statement_timeout() -> Exception:
	"""The error MariaDB raises when `max_statement_time` stops a statement (1969), taken from the server itself."""
	try:
		with frappe.db.execution_timeout(1):
			frappe.db.sql("select sleep(3)")
	except Exception as exc:
		return exc
	raise AssertionError("the server did not stop the statement")


class Clock:
	"""A settable clock for `cache_refresh._clock`."""

	def __init__(self, now=1000.0):
		self.now = now

	def __call__(self):
		return self.now


class TestFailureClass(TestCase):
	def test_each_error_has_its_class(self):
		cases = [
			(ReaderFallback("deadline"), "history_too_large"),
			(ReaderFallback("too_many_lines"), "history_too_large"),
			(ReaderFallback("assemble_budget"), "history_too_large"),
			(ReaderFallback("pin_drift"), "reader_fallback:pin_drift"),
			(MemoryError(), "history_too_large"),
			(real_statement_timeout(), "history_too_large"),
			(frappe.QueryTimeoutError(), "error:QueryTimeoutError"),
			(CalendarSourceUnavailable("too_large"), "refused:too_large"),
			(cache_refresh.LeaseLost(), "lease_lost"),
			(ValueError("Acme Ltd"), "error:ValueError"),
		]
		for error, expected in cases:
			with self.subTest(error=type(error).__name__, expected=expected):
				self.assertEqual(cache_refresh.failure_class(error), expected)

	def test_the_server_really_raises_1969_and_it_is_not_a_frappe_query_timeout(self):
		error = real_statement_timeout()
		self.assertIsInstance(error, frappe.db.OperationalError)
		self.assertEqual(error.args[0], 1969)
		self.assertNotIsInstance(error, frappe.QueryTimeoutError)

	def test_a_lock_wait_is_a_plain_error_and_a_driver_error_is_not_a_statement_timeout(self):
		self.assertEqual(
			cache_refresh.failure_class(frappe.db.OperationalError(1205, "x")), "error:OperationalError"
		)
		self.assertEqual(
			cache_refresh.failure_class(frappe.db.OperationalError(1054, "x")), "error:OperationalError"
		)

	def test_the_class_never_carries_the_message(self):
		self.assertNotIn("Acme", cache_refresh.failure_class(KeyError("Acme Ltd")))


class TestViewClock(TestCase):
	def setUp(self):
		self.clock = Clock()
		patcher = patch.object(cache_refresh, "_clock", self.clock)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_a_statement_may_start_while_a_full_statement_cap_remains(self):
		view = cache_refresh.ViewClock(240, None, "t", False)
		self.clock.now += 240 - cache_state.STATEMENT_CAP_S
		view.check()

	def test_no_statement_starts_with_less_than_the_statement_cap_left(self):
		view = cache_refresh.ViewClock(240, None, "t", False)
		self.clock.now += 240 - cache_state.STATEMENT_CAP_S + 1
		with self.assertRaises(ReaderFallback) as raised:
			view.check()
		self.assertEqual(raised.exception.reason, "deadline")

	def test_the_slot_renews_at_most_every_renew_interval(self):
		view = cache_refresh.ViewClock(10_000, "key", "tok", True)
		with patch.object(cache_state, "renew_slot", return_value=True) as renew:
			view.check()
			self.clock.now += cache_refresh.SLOT_RENEW_EVERY_S - 1
			view.check()
			self.assertEqual(renew.call_count, 0)
			self.clock.now += 1
			view.check()
			self.assertEqual(renew.call_args.args, ("key", "tok"))
			self.assertEqual(renew.call_args.kwargs, {"night": True})
			view.check()
			self.assertEqual(renew.call_count, 1)

	def test_a_slot_that_is_no_longer_ours_stops_the_view(self):
		view = cache_refresh.ViewClock(10_000, "key", "tok", False)
		self.clock.now += cache_refresh.SLOT_RENEW_EVERY_S
		with (
			patch.object(cache_state, "renew_slot", return_value=False),
			self.assertRaises(cache_refresh.LeaseLost),
		):
			view.check()

	def test_no_slot_means_no_renew(self):
		view = cache_refresh.ViewClock(10_000, None, "tok", False)
		self.clock.now += 10 * cache_refresh.SLOT_RENEW_EVERY_S
		with patch.object(cache_state, "renew_slot") as renew:
			view.check()
		renew.assert_not_called()


class TestRefreshCaps(TestCase):
	def test_the_caps_are_raised_inside_and_restored_after(self):
		before = {
			name: getattr(builtin_reader, name)
			for name in ("GENERIC_LINE_CAP", "TOTAL_LINE_CAP", "RANK_PARTY_CAP", "READER_MAX_ROWS")
		}
		view = cache_refresh.ViewClock(240, None, "t", False)
		with cache_refresh.refresh_caps(view):
			self.assertEqual(builtin_reader.TOTAL_LINE_CAP, cache_refresh.REFRESH_TOTAL_LINE_CAP)
			self.assertEqual(builtin_reader.GENERIC_LINE_CAP, cache_refresh.REFRESH_GENERIC_LINE_CAP)
			self.assertEqual(builtin_reader.RANK_PARTY_CAP, cache_refresh.REFRESH_RANK_PARTY_CAP)
			self.assertEqual(builtin_reader.READER_MAX_ROWS, cache_refresh.REFRESH_MAX_ROWS)
			self.assertGreater(builtin_reader.TOTAL_LINE_CAP, before["TOTAL_LINE_CAP"])
		self.assertEqual(before, {name: getattr(builtin_reader, name) for name in before})

	def test_a_build_above_150000_rows_is_refused_and_150000_is_not(self):
		def engine(rows):
			return SimpleNamespace(
				data=[object()] * rows, excluded=[], terms={}, reader_stats={"counts": {}, "timings_ms": {}}
			)

		with cache_refresh.refresh_caps(cache_refresh.ViewClock(240, None, "t", False)):
			self.assertEqual(builtin_reader.READER_MAX_ROWS, 150_000)
			builtin_reader._check_row_cap(engine(150_000))
			with self.assertRaises(ReaderFallback) as raised:
				builtin_reader._check_row_cap(engine(150_001))
		self.assertEqual(raised.exception.reason, "assemble_budget")

	def test_the_caps_are_restored_after_an_error(self):
		before = builtin_reader.TOTAL_LINE_CAP
		with (
			self.assertRaises(ValueError),
			cache_refresh.refresh_caps(cache_refresh.ViewClock(240, None, "t", False)),
		):
			raise ValueError
		self.assertEqual(builtin_reader.TOTAL_LINE_CAP, before)

	def test_every_database_statement_checks_the_clock_and_the_original_comes_back(self):
		original = frappe.db.sql
		clock = MagicMock()
		with cache_refresh.refresh_caps(clock):
			frappe.db.sql("SELECT 1")
		clock.check.assert_called_once_with()
		self.assertEqual(frappe.db.sql, original)

	def test_a_statement_past_the_view_cap_is_not_started(self):
		now = Clock()
		with patch.object(cache_refresh, "_clock", now):
			view = cache_refresh.ViewClock(240, None, "t", False)
			with cache_refresh.refresh_caps(view):
				frappe.db.sql("SELECT 1")
				now.now += 200
				with self.assertRaises(ReaderFallback):
					frappe.db.sql("SELECT 2")

	def test_the_engine_factory_is_the_refresh_engine_inside_and_the_reader_s_outside(self):
		outside = builtin_reader._builtin_class
		with cache_refresh.refresh_caps(cache_refresh.ViewClock(240, None, "t", False)):
			self.assertIs(builtin_reader._builtin_class, cache_refresh._engine_class)
			self.assertTrue(issubclass(cache_refresh._engine_class(), outside()))
		self.assertIs(builtin_reader._builtin_class, outside)


class TestEstimate(TestCase):
	def test_the_t0_constants_are_pinned(self):
		self.assertEqual(cache_refresh.BYTES_PER_LINE, 3072)
		self.assertEqual(cache_refresh.LINES_PER_INVOICE, 10)
		self.assertEqual(cache_refresh.REFRESH_MAX_ROWS, 150_000)
		self.assertEqual(cache_refresh.REFRESH_GENERIC_LINE_CAP, 349_525)

	def test_the_estimate_is_3072_bytes_per_line_and_10_lines_per_invoice(self):
		self.assertEqual(cache_refresh.estimate_bytes(100, 10), 200 * 3072)
		self.assertEqual(cache_refresh.estimate_bytes(0, 1), 10 * 3072)
		self.assertEqual(cache_refresh.estimate_bytes(1, 0), 3072)

	def test_the_estimate_counts_generic_lines_and_invoice_lines(self):
		expected = (100 + 10 * cache_refresh.LINES_PER_INVOICE) * cache_refresh.BYTES_PER_LINE
		self.assertEqual(cache_refresh.estimate_bytes(100, 10), expected)

	def test_an_estimate_above_the_cap_ends_the_read_before_the_fetch(self):
		engine = cache_refresh._engine_class().__new__(cache_refresh._engine_class())
		engine.reader_stats = {"counts": {"generic_lines": 3}}
		discovery = SimpleNamespace(invoices=lambda: frozenset(range(5)))
		base = cache_refresh._READER_CLASS()
		with patch.object(base, "_discover", return_value=discovery):
			with patch.object(cache_refresh, "estimate_bytes", return_value=3 * 1024**3):
				with self.assertRaises(ReaderFallback) as raised:
					engine._discover(None)
			self.assertEqual(raised.exception.reason, "too_many_lines")
			with patch.object(cache_refresh, "estimate_bytes", return_value=1024):
				self.assertIs(engine._discover(None), discovery)

	def test_the_estimate_is_checked_with_the_counted_generic_lines_and_invoices(self):
		engine = cache_refresh._engine_class().__new__(cache_refresh._engine_class())
		engine.reader_stats = {"counts": {"generic_lines": 7}}
		discovery = SimpleNamespace(invoices=lambda: frozenset(range(5)))
		base = cache_refresh._READER_CLASS()
		with patch.object(base, "_discover", return_value=discovery):
			with patch.object(cache_refresh, "estimate_bytes", return_value=0) as estimate:
				engine._discover(None)
		estimate.assert_called_once_with(7, 5)


class TestAddressSpace(TestCase):
	def test_the_limit_is_the_baseline_plus_the_extra_and_it_is_restored(self):
		before = resource.getrlimit(resource.RLIMIT_AS)
		with cache_refresh.address_space_cap(512):
			soft = resource.getrlimit(resource.RLIMIT_AS)[0]
			baseline = cache_refresh._address_space_bytes()
			self.assertLessEqual(soft, baseline + 512 * 1024 * 1024 + 64 * 1024 * 1024)
			self.assertGreater(soft, baseline)
		self.assertEqual(resource.getrlimit(resource.RLIMIT_AS), before)

	def test_a_fetch_over_the_cap_is_a_memory_error_and_the_limit_is_restored(self):
		before = resource.getrlimit(resource.RLIMIT_AS)
		with self.assertRaises(MemoryError), cache_refresh.address_space_cap(64):
			hold = bytearray(256 * 1024 * 1024)
			hold[0] = 1
		self.assertEqual(resource.getrlimit(resource.RLIMIT_AS), before)
		self.assertEqual(cache_refresh.failure_class(MemoryError()), "history_too_large")

	def test_the_limit_is_restored_after_any_error(self):
		before = resource.getrlimit(resource.RLIMIT_AS)
		with self.assertRaises(KeyError), cache_refresh.address_space_cap(64):
			raise KeyError
		self.assertEqual(resource.getrlimit(resource.RLIMIT_AS), before)


class TestQueryHooks(TestCase):
	def hooks(self, query=None, scripts=None):
		return (
			patch.object(cache_refresh, "effective_hooks", return_value=query or {}),
			patch.object(
				cache_refresh, "get_server_script_map", return_value={"permission_query": scripts or {}}
			),
		)

	def offenders(self, doctypes, query=None, scripts=None):
		first, second = self.hooks(query, scripts)
		with first, second:
			return cache_refresh.query_hook_offenders(doctypes)

	def test_no_hook_no_offender(self):
		self.assertEqual(self.offenders(["Sales Invoice", "Customer"]), [])

	def test_an_unaudited_star_hook_is_an_offender(self):
		self.assertEqual(self.offenders(["Sales Invoice"], {"*": ["x.y"]}), ["*"])

	def test_a_query_hook_on_a_checked_doctype_is_an_offender(self):
		self.assertEqual(
			self.offenders(["Sales Invoice", "Expense Claim"], {"Expense Claim": ["x.y"]}), ["Expense Claim"]
		)

	def test_a_server_script_query_on_a_checked_doctype_is_an_offender(self):
		self.assertEqual(self.offenders(["Employee"], None, {"Employee": "script"}), ["Employee"])

	AUDITED = "erpnext.stock.doctype.company_restriction.company_restriction.get_permission_query_conditions"

	def test_an_audited_hook_on_a_checked_doctype_is_not_an_offender(self):
		with patch.object(cache_refresh.cache_eligibility, "audited_sources_ok", return_value=True):
			found = self.offenders(
				["Customer", "Supplier"], {"Customer": [self.AUDITED], "Supplier": self.AUDITED}
			)
		self.assertEqual(found, [])

	def test_an_audited_hook_next_to_an_unaudited_one_is_an_offender(self):
		with patch.object(cache_refresh.cache_eligibility, "audited_sources_ok", return_value=True):
			found = self.offenders(["Customer"], {"Customer": [self.AUDITED, "x.y"]})
		self.assertEqual(found, ["Customer"])

	def test_an_unaudited_hook_on_customer_is_an_offender(self):
		self.assertEqual(self.offenders(["Customer"], {"Customer": ["x.y"]}), ["Customer"])

	def test_an_audited_hook_whose_source_drifted_is_an_offender(self):
		with patch.object(cache_refresh.cache_eligibility, "audited_sources_ok", return_value=False):
			found = self.offenders(["Customer"], {"Customer": [self.AUDITED]})
		self.assertEqual(found, ["Customer"])

	def test_a_server_script_query_on_a_doctype_with_an_audited_hook_is_still_an_offender(self):
		with patch.object(cache_refresh.cache_eligibility, "audited_sources_ok", return_value=True):
			found = self.offenders(["Customer"], {"Customer": [self.AUDITED]}, {"Customer": "s"})
		self.assertEqual(found, ["Customer"])

	def test_the_audited_hook_path_is_part_of_the_fingerprint_and_a_change_fails_verify(self):
		def fingerprint(path):
			first, second = self.hooks({"Customer": [path]})
			with first, second, patch.object(frappe, "get_hooks", return_value={}):
				return cache_refresh.hook_fingerprint(["Customer"])

		base = fingerprint(self.AUDITED)
		self.assertNotEqual(base, fingerprint(self.AUDITED + "_changed"))
		self.assertNotEqual(base, fingerprint("x.y"))

	def test_a_hook_on_a_doctype_that_is_not_checked_is_not_an_offender(self):
		self.assertEqual(self.offenders(["Sales Invoice"], {"Item": ["x.y"]}, {"Item": "s"}), [])

	def test_the_star_hook_is_read_through_effective_hooks_as_administrator(self):
		with patch.object(cache_refresh, "effective_hooks", return_value={}) as hooks:
			with patch.object(cache_refresh, "get_server_script_map", return_value={"permission_query": {}}):
				cache_refresh.query_hook_offenders(["Account"])
		hooks.assert_called_once_with("permission_query_conditions", user="Administrator")

	def test_the_fingerprint_changes_with_a_hook_or_a_script_on_a_checked_doctype_only(self):
		def fingerprint(query=None, scripts=None):
			first, second = self.hooks(query, scripts)
			with first, second, patch.object(frappe, "get_hooks", return_value={}):
				return cache_refresh.hook_fingerprint(["Account"])

		base = fingerprint()
		self.assertEqual(base, fingerprint())
		self.assertNotEqual(base, fingerprint({"Account": ["x.y"]}))
		self.assertNotEqual(base, fingerprint({"*": ["x.y"]}))
		self.assertNotEqual(base, fingerprint(None, {"Account": "s"}))
		self.assertEqual(base, fingerprint({"Item": ["x.y"]}))


class TestPreflight(TestCase):
	"""One reason per check; each check alone turned on, everything else clear."""

	def setUp(self):
		self.company = "PCAL-B4-Company"
		self.real_control = cache_refresh._control
		self.patches = {
			"_scheduler_inactive": patch.object(cache_refresh, "_scheduler_inactive", return_value=False),
			"_tdr_running": patch.object(cache_refresh, "_tdr_running", return_value=False),
			"_free_disk_bytes": patch.object(cache_refresh, "_free_disk_bytes", return_value=10**15),
			"_needed_disk_bytes": patch.object(cache_refresh, "_needed_disk_bytes", return_value=5 * 1024**3),
			"_control": patch.object(
				cache_refresh,
				"_control",
				return_value=cache_refresh.Control(frozenset({"Sales Invoice", "Credit"}), 1234),
			),
			"query_hook_offenders": patch.object(cache_refresh, "query_hook_offenders", return_value=[]),
			"company_creation": patch.object(cache_store, "company_creation", return_value="created"),
			"hook_fingerprint": patch.object(cache_refresh, "hook_fingerprint", return_value="fp"),
			"party_types": patch.object(frappe, "get_all", return_value=["Customer", "Supplier"]),
			"key": patch.dict(frappe.conf, {"encryption_key": frappe.conf.get("encryption_key") or "k"}),
			"major": patch.object(cache_refresh, "source_profile", return_value=SimpleNamespace(major="16")),
		}
		self.mocks = {name: patcher.start() for name, patcher in self.patches.items()}
		for patcher in self.patches.values():
			self.addCleanup(patcher.stop)

	def flags(self, **changes):
		for name, value in changes.items():
			self.mocks[name].return_value = value

	def test_all_clear_gives_the_fingerprint_and_the_checked_doctypes(self):
		found = cache_refresh.preflight(self.company, "Receivable")
		self.assertIsNone(found.skip)
		self.assertEqual(found.fingerprint, "fp")
		self.assertIn("Sales Invoice", found.checked)
		self.assertIn("Credit", found.checked)
		self.assertTrue(set(cache_refresh.HOOKED_DOCTYPES) <= found.checked)
		self.assertIn("Customer", found.checked)

	def test_the_bank_side_checks_account_and_gl_entry_only(self):
		self.mocks["_control"].side_effect = self.real_control
		found = cache_refresh.preflight(self.company, "Bank")
		self.assertEqual(found.checked, frozenset({"Account", "GL Entry"}))

	def test_the_preflight_carries_the_ledger_line_total_of_the_control_pairs(self):
		self.assertEqual(cache_refresh.preflight(self.company, "Receivable").lines, 1234)

	def test_the_bank_side_has_no_control_lines(self):
		self.mocks["_control"].side_effect = self.real_control
		self.assertEqual(cache_refresh.preflight(self.company, "Bank").lines, 0)

	def test_a_known_result_keeps_its_ledger_lines(self):
		first = cache_refresh.preflight(self.company, "Receivable")
		self.assertEqual(cache_refresh.preflight(self.company, "Receivable", known=first).lines, 1234)

	def test_a_scheduler_that_is_not_running_skips(self):
		self.flags(_scheduler_inactive=True)
		self.assertEqual(cache_refresh.preflight(self.company, "Receivable").skip, "scheduler_inactive")

	def test_a_migration_skips(self):
		with patch.dict(frappe.flags, {"in_migrate": True}):
			self.assertEqual(cache_refresh.preflight(self.company, "Receivable").skip, "scheduler_inactive")

	def test_a_transaction_deletion_in_progress_skips(self):
		self.flags(_tdr_running=True)
		self.assertEqual(cache_refresh.preflight(self.company, "Receivable").skip, "deletion_running")

	def test_a_missing_company_skips(self):
		self.flags(company_creation=None)
		self.assertEqual(cache_refresh.preflight(self.company, "Receivable").skip, "company_gone")

	def test_v15_skips(self):
		self.flags(major=SimpleNamespace(major="15"))
		self.assertEqual(cache_refresh.preflight(self.company, "Receivable").skip, "unsupported_major")

	def test_a_site_without_an_encryption_key_skips(self):
		with patch.dict(frappe.conf, {"encryption_key": ""}):
			self.assertEqual(cache_refresh.preflight(self.company, "Receivable").skip, "no_encryption_key")

	def test_too_little_disk_skips(self):
		self.flags(_free_disk_bytes=10)
		self.assertEqual(cache_refresh.preflight(self.company, "Receivable").skip, "no_disk")

	def test_a_query_hook_is_not_buildable(self):
		self.flags(query_hook_offenders=["Expense Claim"])
		found = cache_refresh.preflight(self.company, "Receivable")
		self.assertEqual(found.skip, "not_buildable:query_hook")

	def test_the_hook_check_covers_the_control_pair_types_and_every_party_type(self):
		cache_refresh.preflight(self.company, "Receivable")
		checked = self.mocks["query_hook_offenders"].call_args.args[0]
		self.assertTrue(
			{"Sales Invoice", "Credit", "Customer", "Supplier", *cache_refresh.HOOKED_DOCTYPES}
			<= set(checked)
		)

	def test_a_known_result_reuses_its_control_pairs(self):
		first = cache_refresh.preflight(self.company, "Receivable")
		self.mocks["_control"].reset_mock()
		again = cache_refresh.preflight(self.company, "Receivable", known=first)
		self.mocks["_control"].assert_not_called()
		self.assertEqual(again.checked, first.checked)

	def test_the_slot_is_renewed_before_and_after_the_aggregate(self):
		order = []
		self.mocks["_control"].side_effect = lambda *a: order.append("control") or cache_refresh.Control()
		found = cache_refresh.preflight(
			self.company, "Receivable", renew=lambda: order.append("renew") or True
		)
		self.assertIsNone(found.skip)
		self.assertEqual(order, ["renew", "control", "renew"])

	def test_a_slot_that_is_gone_skips_with_lease_lost(self):
		self.assertEqual(
			cache_refresh.preflight(self.company, "Receivable", renew=lambda: False).skip, "lease_lost"
		)

	def test_a_known_result_does_not_renew(self):
		first = cache_refresh.preflight(self.company, "Receivable")
		renew = MagicMock(return_value=True)
		cache_refresh.preflight(self.company, "Receivable", known=first, renew=renew)
		renew.assert_not_called()


class TestDiskNeeded(TestCase):
	def test_the_floor_wins_for_a_small_generation_and_three_times_the_bytes_for_a_large_one(self):
		with patch.object(frappe.db, "sql", return_value=[(100,)]):
			self.assertEqual(
				cache_refresh._needed_disk_bytes("c", "Receivable"), cache_refresh.FREE_DISK_FLOOR_BYTES
			)
		with patch.object(frappe.db, "sql", return_value=[(4 * 1024**3,)]):
			self.assertEqual(cache_refresh._needed_disk_bytes("c", "Receivable"), 12 * 1024**3)
		with patch.object(frappe.db, "sql", return_value=[]):
			self.assertEqual(
				cache_refresh._needed_disk_bytes("c", "Receivable"), cache_refresh.FREE_DISK_FLOOR_BYTES
			)


class TestExtractAssertion(TestCase):
	def engine(self, terms, residuals):
		return SimpleNamespace(
			currency_precision=2,
			terms=terms,
			pre={"k": object()},
			ledger_residuals=residuals,
			data=[],
			excluded=[],
		)

	def test_a_settled_candidate_in_terms_is_a_reader_error(self):
		engine = self.engine({"k": [object()]}, {"k": Decimal(0)})
		with self.assertRaises(ReaderFallback) as raised:
			cache_refresh.extract_engine(engine, "Receivable")
		self.assertEqual(raised.exception.reason, "reader_error")

	def test_an_open_key_with_terms_is_fine(self):
		engine = self.engine({"k": [object()]}, {"k": Decimal("50")})
		engine.is_invoice = lambda row: False
		found = cache_refresh.extract_engine(engine, "Receivable")
		self.assertEqual(found.residue, [])
		self.assertEqual(found.vouchers, {"Sales Invoice"})
		self.assertEqual(found.parties, {"Customer"})

	def test_a_repaired_key_with_empty_terms_is_fine(self):
		engine = self.engine({"k": []}, {"k": Decimal(0)})
		engine.is_invoice = lambda row: False
		cache_refresh.extract_engine(engine, "Payable")


class ReaderCase(TestCase):
	"""A fixture company and the reader called through `_read_side`, with the view's own checks replaced."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("cache_refresh_case")
		self.fx = CalendarFixtures()
		self.fx.fiscal_year()
		frappe.cache().hdel("fiscal_years", self.fx.company)
		for name in ("require_defaults_cache_hit", "check_source_contract"):
			patcher = patch.object(snapshot, name)
			patcher.start()
			self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.local.form_dict = frappe._dict()
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="cache_refresh_case")
		frappe.cache().hdel("fiscal_years", self.fx.company)

	def read(self, side="Payable"):
		sync_outstanding(self.fx.company)
		clock = cache_refresh.ViewClock(10_000, None, "t", False)
		with cache_refresh.refresh_caps(clock):
			return cache_refresh._read_side(
				self.fx.company, side, frappe.utils.getdate(self.fx.today), set(), clock
			)

	@staticmethod
	def fields(build):
		return [dict(zip(cache_store.ROW_FIELDS, row, strict=True)) for row in build.rows]


class TestReadSide(ReaderCase):
	def test_the_open_terms_are_stored_with_their_remaining_amounts(self):
		invoice = self.fx.invoice((40, 60), paid=30)
		rows = [row for row in self.fields(self.read()) if row["voucher_no"] == invoice]
		self.assertEqual(
			sorted(Decimal(row["remaining"]) for row in rows if not row["residue"]),
			[Decimal("10"), Decimal("60")],
		)
		self.assertTrue(all(row["residue"] == 0 for row in rows))

	def test_the_payload_meets_the_b8_contract(self):
		self.fx.invoice((40, 60), paid=30)
		build = self.read()
		cache_store.check_payload("Payable", self.fx.company, build.payload)
		self.assertEqual(build.payload["open_set_window"], cache_store.OPEN_SET_WINDOW)
		self.assertEqual(build.payload["canonical_total"], "70.00")
		self.assertEqual(build.payload["currency"], "INR")
		self.assertIn("default_finance_book", build.payload)
		self.assertIn("Purchase Invoice", build.payload["voucher_doctypes"])
		self.assertIn("Supplier", build.payload["party_doctypes"])
		self.assertEqual(build.payload["voucher_doctypes"], ["Purchase Invoice"])

	def test_the_rows_fit_the_store_and_the_ids_are_dropped(self):
		self.fx.invoice((40, 60), paid=30)
		build = self.read()
		for row in build.rows:
			self.assertEqual(len(row), len(cache_store.ROW_FIELDS))
		list(cache_store.encode_rows("Payable", build.rows))
		self.assertNotIn("id", cache_store.ROW_FIELDS)

	def test_the_reader_runs_with_the_open_set_window_and_the_view_deadline(self):
		self.fx.invoice((100,), paid=0)
		sync_outstanding(self.fx.company)
		clock = cache_refresh.ViewClock(10_000, None, "t", False)
		with patch.object(builtin_reader, "read_obligations", wraps=builtin_reader.read_obligations) as read:
			with cache_refresh.refresh_caps(clock):
				cache_refresh._read_side(
					self.fx.company, "Payable", frappe.utils.getdate(self.fx.today), set(), clock
				)
		args, kwargs = read.call_args
		self.assertEqual(args[3:5], ("1900-01-01", "1900-01-01"))
		self.assertIsNotNone(kwargs["deadline"])

	def build_with_header(self, invoice, header=50):
		"""Build after the header of `invoice` was set open although its ledger is settled."""
		sync_outstanding(self.fx.company)
		frappe.db.set_value("Purchase Invoice", invoice, "outstanding_amount", header)
		clock = cache_refresh.ViewClock(10_000, None, "t", False)
		with cache_refresh.refresh_caps(clock):
			return cache_refresh._read_side(
				self.fx.company, "Payable", frappe.utils.getdate(self.fx.today), set(), clock
			)

	def residue_of(self, build, invoice):
		rows = [row for row in self.fields(build) if row["voucher_no"] == invoice and row["residue"]]
		return sorted(rows, key=lambda row: row["due_date"] or "")

	def test_a_float_residue_term_is_one_zero_row_with_its_own_due_date(self):
		invoice = self.fx.invoice((0.8, 1.2), paid=0.1)
		self.fx.ple("SecondPayment", "Payment Entry", invoice, -0.7)
		build = self.read()
		rows = [row for row in self.fields(build) if row["voucher_no"] == invoice]
		self.assertEqual([Decimal(row["remaining"]) for row in rows if not row["residue"]], [Decimal("1.20")])
		residue = self.residue_of(build, invoice)
		self.assertEqual(len(residue), 1)
		self.assertEqual(Decimal(residue[0]["remaining"]), 0)
		self.assertEqual(residue[0]["due_date"], add_days(self.fx.today, 10))
		self.assertEqual((residue[0]["hide_in_window"], residue[0]["window_refusal"]), (0, 0))
		self.assertEqual(json_list(residue[0]["window_due_dates"]), [add_days(self.fx.today, 10)])

	def test_the_paid_term_of_a_part_paid_invoice_is_a_zero_row_with_its_own_date(self):
		invoice = self.fx.invoice((40, 60), paid=40)
		build = self.read()
		residue = self.residue_of(build, invoice)
		self.assertEqual(len(residue), 1)
		self.assertEqual((Decimal(residue[0]["original"]), Decimal(residue[0]["remaining"])), (40, 0))
		self.assertEqual(residue[0]["due_date"], add_days(self.fx.today, 10))
		self.assertEqual(json_list(residue[0]["window_due_dates"]), [add_days(self.fx.today, 10)])

	def test_a_settled_candidate_has_one_zero_row_for_each_term_with_the_selection_dates(self):
		invoice = self.fx.invoice((40, 60), paid=100)
		residue = self.residue_of(self.build_with_header(invoice), invoice)
		self.assertEqual(
			[row["due_date"] for row in residue], [add_days(self.fx.today, 10), add_days(self.fx.today, 20)]
		)
		self.assertEqual([Decimal(row["original"]) for row in residue], [40, 60])
		dates = [add_days(self.fx.today, 10), add_days(self.fx.today, 20)]
		self.assertTrue(all(json_list(row["window_due_dates"]) == dates for row in residue))
		self.assertTrue(all(row["residue"] == 1 and row["hide_in_window"] == 0 for row in residue))

	def test_a_settled_candidate_with_one_term_has_its_schedule_and_header_dates(self):
		invoice = self.fx.invoice((100,), paid=100)
		residue = self.residue_of(self.build_with_header(invoice), invoice)
		self.assertEqual(len(residue), 1)
		self.assertEqual(residue[0]["due_date"], add_days(self.fx.today, 10))
		self.assertEqual(
			json_list(residue[0]["window_due_dates"]),
			[add_days(self.fx.today, 10), add_days(self.fx.today, 20)],
		)

	def test_a_settled_candidate_with_no_schedule_is_one_row_dated_by_its_header_and_never_hidden(self):
		invoice = self.fx.invoice((100,), paid=100, schedules=False)
		residue = self.residue_of(self.build_with_header(invoice), invoice)
		self.assertEqual(len(residue), 1)
		self.assertEqual(residue[0]["due_date"], add_days(self.fx.today, 20))
		self.assertEqual(json_list(residue[0]["window_due_dates"]), [add_days(self.fx.today, 20)])
		self.assertEqual(residue[0]["hide_in_window"], 0)

	def test_the_open_rows_do_not_change_with_the_residue_replay(self):
		invoice = self.fx.invoice((40, 60), paid=40)
		build = self.read()
		opened = [row for row in self.fields(build) if row["voucher_no"] == invoice and not row["residue"]]
		self.assertEqual([Decimal(row["remaining"]) for row in opened], [Decimal("60")])
		self.assertEqual(build.payload["canonical_total"], "60.00")

	def test_the_receivable_side_reads_customers(self):
		self.fx.invoice((100,), side="Receivable", paid=25)
		build = self.read("Receivable")
		self.assertEqual(build.payload["canonical_total"], "75.00")
		self.assertIn("Customer", build.payload["party_doctypes"])
		self.assertIn("Sales Invoice", build.payload["voucher_doctypes"])


class TestReadBank(ReaderCase):
	def read_bank(self):
		return cache_refresh._read_bank(self.fx.company, frappe.utils.getdate(self.fx.today), set())

	def test_every_enabled_bank_account_has_a_row_with_its_balance(self):
		second = self.fx.account("Bank2", "Bank", "Asset")
		self.fx.gl(500)
		self.fx.gl(-125, account=second)
		build = self.read_bank()
		rows = {row["account"]: Decimal(row["remaining"]) for row in self.fields(build)}
		self.assertEqual(rows, {self.fx.bank: Decimal("500"), second: Decimal("-125")})
		self.assertEqual(build.payload["currency"], "INR")
		self.assertEqual(build.payload["precision"], 2)
		self.assertEqual(build.doctypes, frozenset({"Account", "GL Entry"}))

	def test_the_bank_gl_rows_read_are_counted_for_the_size_gate(self):
		second = self.fx.account("Bank2", "Bank", "Asset")
		self.fx.gl(500)
		self.fx.gl(100)
		self.fx.gl(-125, account=second)
		self.assertEqual(self.read_bank().stats.ledger_lines, 3)

	def test_a_cancelled_gl_row_a_later_one_and_another_account_are_not_counted(self):
		self.fx.gl(500)
		cancelled = self.fx.gl(100)
		later = self.fx.gl(10)
		frappe.db.set_value("GL Entry", cancelled, "is_cancelled", 1)
		frappe.db.set_value("GL Entry", later, "posting_date", add_days(self.fx.today, 3))
		self.fx.gl(10, account=self.fx.receivable)
		self.assertEqual(self.read_bank().stats.ledger_lines, 1)

	def test_a_company_with_no_bank_account_counts_no_gl_rows(self):
		frappe.db.set_value("Account", self.fx.bank, "disabled", 1)
		self.assertEqual(self.read_bank().stats.ledger_lines, 0)

	def test_a_disabled_or_group_account_is_not_listed(self):
		frappe.db.set_value("Account", self.fx.bank, "disabled", 1)
		self.assertEqual(self.fields(self.read_bank()), [])

	def test_an_account_with_gl_rows_in_a_foreign_book_is_flagged(self):
		other = self.fx.account("Bank3", "Bank", "Asset")
		self.fx.gl(100)
		self.fx.gl(10, account=other, finance_book="Other Book")
		payload = self.read_bank().payload
		self.assertEqual(payload["multibook_accounts"], [other])
		self.assertFalse(payload["ple_book_flag"])

	def test_the_company_ple_book_flag(self):
		self.fx.gl(100)
		self.fx.ple("BookedPLE", "Purchase Invoice", "X", 10, finance_book="Other Book")
		self.assertTrue(self.read_bank().payload["ple_book_flag"])

	def test_a_missing_fiscal_year_is_refused(self):
		frappe.db.set_value("Fiscal Year", self.fx.prefix, "disabled", 1)
		frappe.cache().hdel("fiscal_years", self.fx.company)
		with patch(
			"erpnext.accounts.utils.get_fiscal_year",
			side_effect=__import__("erpnext.accounts.utils", fromlist=["x"]).FiscalYearError,
		):
			with self.assertRaises(CalendarSourceUnavailable) as raised:
				self.read_bank()
		self.assertEqual(raised.exception.code, "fiscal_year_unavailable")


def json_list(text):
	import json

	return json.loads(text) if isinstance(text, str) else list(text)


class TestStaticChecks(TestCase):
	def test_a_transaction_deletion_that_is_queued_or_running_blocks(self):
		with patch.object(frappe.db, "exists", return_value="TDR-1") as exists:
			self.assertTrue(cache_refresh._tdr_running())
		self.assertEqual(
			exists.call_args.args, ("Transaction Deletion Record", {"status": ["in", ["Queued", "Running"]]})
		)
		with patch.object(frappe.db, "exists", return_value=None):
			self.assertFalse(cache_refresh._tdr_running())

	def test_an_unreadable_deletion_state_blocks_and_a_deadline_passes_through(self):
		with patch.object(frappe.db, "exists", side_effect=RuntimeError):
			self.assertTrue(cache_refresh._tdr_running())
		with patch.object(frappe.db, "exists", side_effect=TimeoutError), self.assertRaises(TimeoutError):
			cache_refresh._tdr_running()

	def test_the_scheduler_state_is_frappes_without_its_printing(self):
		with patch("frappe.utils.scheduler.is_scheduler_inactive", return_value=True) as inactive:
			self.assertTrue(cache_refresh._scheduler_inactive())
		inactive.assert_called_once_with(verbose=False)


class TestReplayCandidates(TestCase):
	"""`replay_candidates` on a fake engine: the refusal, which a fixture cannot reach cheaply."""

	def engine(self, terms):
		row = frappe._dict(
			voucher_type="Purchase Invoice",
			voucher_no="PI-1",
			party_type="Supplier",
			party="S",
			party_account="Creditors",
			posting_date="2026-11-24",
			due_date=None,
			payment_term=None,
			invoiced=Decimal(100),
			paid=Decimal(100),
			credit_note=Decimal(0),
		)
		key = ("Creditors", "Purchase Invoice", "PI-1", "S")
		engine = SimpleNamespace(
			currency_precision=2,
			terms={},
			excluded=[],
			pre={key: row},
			is_invoice=lambda row: True,
			_schedule_rows=lambda doctype, name: [frappe._dict(due_date="2026-12-01")],
			metadata={"PI-1": {"due_date": "2026-12-05"}},
			set_party_details=lambda row: None,
			columns=[],
			key=key,
		)

		def allocate(row):
			row.payment_terms = terms
			engine.terms[key] = list(terms)

		engine.allocate_outstanding_based_on_payment_terms = allocate
		return engine

	def replay(self, terms, repair=None):
		reconciler = MagicMock()
		reconciler.return_value.repair.side_effect = repair
		engine = self.engine(terms)
		with patch.object(cache_refresh, "InvoiceReconciler", reconciler):
			return engine, cache_refresh.replay_candidates(engine, "Payable"), reconciler

	def test_a_refused_candidate_is_one_marker_row_and_leaves_no_terms(self):
		engine, replay, _ = self.replay(
			[frappe._dict(outstanding=5)], CalendarSourceUnavailable("settlement_schedule_mismatch")
		)
		self.assertEqual(len(replay.markers), 1)
		marker = replay.markers[0]
		self.assertEqual((marker["window_refusal"], marker["residue"], marker["due_date"]), (1, 1, None))
		self.assertEqual(marker["window_due_dates"], ["2026-12-01", "2026-12-05"])
		self.assertEqual(Decimal(marker["remaining"]), 0)
		self.assertNotIn("hide_in_window", marker)
		self.assertEqual(engine.terms, {})
		self.assertEqual(replay.dates, {})

	def test_a_non_zero_allocated_term_that_repairs_stays_as_the_repaired_term(self):
		engine, replay, reconciler = self.replay([frappe._dict(outstanding=5)], lambda *a, **k: "repaired")
		self.assertEqual(engine.terms[engine.key], ["repaired"])
		self.assertEqual(replay.markers, [])
		self.assertEqual(reconciler.return_value.repair.call_args.kwargs, {"settled": True})

	def test_zero_terms_stay_in_the_engine_and_the_reconciler_is_not_built(self):
		terms = [frappe._dict(outstanding=0)]
		engine, replay, reconciler = self.replay(terms)
		self.assertEqual(engine.terms[engine.key], terms)
		reconciler.assert_not_called()
		self.assertEqual(replay.dates, {("Purchase Invoice", "PI-1"): ["2026-12-01", "2026-12-05"]})

	def test_an_invoice_with_no_payment_terms_is_its_own_single_term_with_outstanding_zero(self):
		engine, replay, _ = self.replay([])
		(term,) = engine.terms[engine.key]
		self.assertEqual(term.outstanding, 0)
		self.assertEqual(term.voucher_no, "PI-1")
		self.assertIn(("Purchase Invoice", "PI-1"), replay.dates)

	def test_an_invoice_that_is_in_terms_or_open_or_excluded_is_not_a_candidate(self):
		for change in ("terms", "open", "excluded"):
			with self.subTest(change=change):
				engine = self.engine([])
				row = engine.pre[engine.key]
				if change == "terms":
					engine.terms[engine.key] = [object()]
				elif change == "open":
					row.paid = Decimal(40)
				else:
					engine.excluded = [row]
				replay = cache_refresh.replay_candidates(engine, "Payable")
				self.assertEqual((replay.dates, replay.markers), ({}, []))


class TestResidueRows(TestCase):
	def row(self, **fields):
		base = {
			"id": "x",
			"kind": "obligation",
			"voucher_type": "Purchase Invoice",
			"voucher_no": "PI-1",
			"party_type": "Supplier",
			"party": "S",
			"party_name": "S",
			"account": "Creditors",
			"posting_date": "2026-11-24",
			"due_date": "2026-12-01",
			"term": "",
			"original": "40.00",
			"paid": "40.00",
			"credit": "0.00",
			"discount": "0.00",
			"remaining": "0.00",
			"settlement": "Settled",
			"_permission": {"party": "S", "due_date": "2026-12-01"},
		}
		return {**base, **fields}

	def result(self, *rows):
		return {"rows": list(rows)}

	def test_only_the_zero_rows_that_the_open_set_read_lacks_are_stored(self):
		open_row = self.row(remaining="60.00", original="60.00", due_date="2026-12-11")
		zero = self.row()
		found = cache_refresh.residue_rows(self.result(open_row), self.result(open_row, zero), {})
		self.assertEqual(len(found), 1)
		self.assertEqual(found[0]["due_date"], "2026-12-01")
		self.assertEqual((found[0]["residue"], found[0]["window_due_dates"]), (1, ["2026-12-01"]))
		self.assertNotIn("id", found[0])
		self.assertNotIn("_permission", found[0])
		self.assertEqual(found[0]["permission"], {"party": "S", "due_date": "2026-12-01"})

	def test_identical_zero_rows_are_counted_once_each(self):
		zero = self.row()
		self.assertEqual(len(cache_refresh.residue_rows(self.result(), self.result(zero, dict(zero)), {})), 2)
		self.assertEqual(
			len(cache_refresh.residue_rows(self.result(zero), self.result(zero, dict(zero)), {})), 1
		)

	def test_a_new_non_zero_row_is_not_a_residue_row(self):
		changed = self.row(remaining="5.00")
		self.assertEqual(cache_refresh.residue_rows(self.result(), self.result(changed), {}), [])

	def test_a_row_of_a_settled_candidate_carries_the_selection_dates(self):
		dates = {("Purchase Invoice", "PI-1"): ["2026-12-01", "2026-12-05"]}
		found = cache_refresh.residue_rows(self.result(), self.result(self.row()), dates)
		self.assertEqual(found[0]["window_due_dates"], ["2026-12-01", "2026-12-05"])

	def test_the_ids_do_not_make_two_rows_differ(self):
		zero = self.row()
		other = self.row(id="y")
		self.assertEqual(cache_refresh.residue_rows(self.result(zero), self.result(other), {}), [])


class TestExcludedRows(TestCase):
	def test_the_excluded_fx_rows_are_stored_with_the_result_rows(self):
		excluded = {
			"voucher_type": "Sales Invoice",
			"voucher_no": "SI-9",
			"party_type": "Customer",
			"party": "C",
			"remaining": "0.40",
			"kind": "settled_fx",
			"account_remaining": "0.00",
			"_permission": {"party": "C"},
		}
		result = {
			"rows": [],
			"excluded_rows": [excluded],
			"notes": {},
			"canonical_total": "0.00",
			"currency": "INR",
			"precision": 2,
			"complete_for_company": True,
			"scope_reasons": [],
			"permission_columns": [],
		}
		engine = SimpleNamespace(
			currency_precision=2, terms={}, pre={}, ledger_residuals={}, data=[], excluded=[]
		)
		engine.excluded = [frappe._dict(voucher_type="Sales Invoice", party_type="Customer")]
		clock = cache_refresh.ViewClock(10_000, None, "t", False)

		def read(*args, **kwargs):
			return sources._assemble(engine, None, "Receivable", None, None, None)

		with (
			patch.object(builtin_reader, "read_obligations", side_effect=read),
			patch.object(sources, "_assemble", return_value=result),
			patch.object(snapshot, "require_defaults_cache_hit"),
			patch.object(snapshot, "check_source_contract"),
			patch.object(
				cache_refresh, "require_company", return_value=SimpleNamespace(default_finance_book=None)
			),
		):
			build = cache_refresh._read_side(
				"Acme", "Receivable", frappe.utils.getdate("2026-11-24"), set(), clock
			)
		fields = [dict(zip(cache_store.ROW_FIELDS, row, strict=True)) for row in build.rows]
		self.assertEqual(
			[(row["kind"], row["voucher_no"], row["remaining"]) for row in fields],
			[("settled_fx", "SI-9", "0.40")],
		)
		self.assertEqual(fields[0]["account_remaining"], "0.00")


class TestControlAggregate(TestCase):
	def test_the_control_aggregate_runs_under_the_statement_cap(self):
		events = []

		@contextlib.contextmanager
		def cap(seconds):
			events.append(("cap_on", seconds))
			yield
			events.append("cap_off")

		def control(*args):
			events.append("control")
			return SimpleNamespace(pairs={("Sales Invoice", None): 3, ("Payment Entry", "Sales Invoice"): 4})

		with (
			patch.object(frappe, "get_all", return_value=["Debtors"]),
			patch.object(frappe.db, "execution_timeout", cap),
			patch.object(builtin_reader, "ledger_control", side_effect=control),
		):
			found = cache_refresh._control("Acme", "Receivable")
		self.assertEqual(events, [("cap_on", cache_state.STATEMENT_CAP_S), "control", "cap_off"])
		self.assertEqual(found.types, frozenset({"Sales Invoice", "Payment Entry"}))

	def test_the_ledger_lines_are_the_sum_of_the_pair_counts(self):
		control = SimpleNamespace(pairs={("Sales Invoice", None): 3, ("Payment Entry", "Sales Invoice"): 4})
		with (
			patch.object(frappe, "get_all", return_value=["Debtors"]),
			patch.object(builtin_reader, "ledger_control", return_value=control),
		):
			self.assertEqual(cache_refresh._control("Acme", "Receivable").lines, 7)

	def test_a_company_with_no_account_of_the_side_has_no_lines(self):
		with patch.object(frappe, "get_all", return_value=[]):
			self.assertEqual(cache_refresh._control("Acme", "Receivable"), cache_refresh.Control())
