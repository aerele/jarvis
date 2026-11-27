"""Proof that the exact speed-ups change no output, and that the collect log reports the speed path."""

from functools import wraps
from time import monotonic
from types import SimpleNamespace
from unittest import TestCase, TestResult
from unittest.mock import MagicMock, patch

import frappe
from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

from jarvis.payment_calendar import compatibility, snapshot
from jarvis.payment_calendar import sources as obligation_sources
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.tests import test_payment_calendar_reconciliation as reconciliation_tests
from jarvis.tests import test_payment_calendar_sources as sources_tests
from jarvis.tests.payment_calendar_differential import TALLY, compare_with_oracle, no_oracle_compare


class TestCollectLogsFastPath(TestCase):
	def logged(self, fast_path):
		logger = MagicMock()
		sources = {"Payable": {"rows": [1, 2]}, "Receivable": None}
		with (
			patch.object(obligation_sources, "FAST_PATH", fast_path),
			patch.object(frappe, "logger", return_value=logger),
		):
			snapshot._log_collected(monotonic(), 3, sources)
		return logger.info.call_args.args[0]

	def test_collect_logs_fast_path(self):
		self.assertIn("payable_rows=2 receivable_rows=0 fast_path=True", self.logged(True))
		self.assertIn("fast_path=False", self.logged(False))


def result(rows=None, **changes):
	"""A minimal `read_obligations` result."""
	base = {
		"rows": rows if rows is not None else [{"id": "a", "voucher_no": "X", "remaining": "10"}],
		"excluded_rows": [],
		"notes": {},
		"canonical_total": "10",
		"complete_for_company": True,
		"scope_reasons": [],
	}
	return {**base, **changes}


def refusing(code):
	def refuse(*args, **kwargs):
		raise CalendarSourceUnavailable(code)

	return refuse


def returning(value):
	return lambda *args, **kwargs: value


class TestOracleComparator(TestCase):
	"""The comparator that wraps `read_obligations` must itself detect every kind of difference."""

	def compare(self, real, oracle):
		return compare_with_oracle(real, oracle)(
			"company", "Payable", "2026-01-01", "2026-01-01", "2026-02-01"
		)

	def test_equal_results_pass_and_the_real_result_is_returned(self):
		real = result()
		self.assertIs(self.compare(returning(real), returning(result())), real)

	def test_row_order_and_row_ids_are_ignored(self):
		first = {"id": "a", "voucher_no": "X", "remaining": "10"}
		second = {"id": "b", "voucher_no": "Y", "remaining": "5"}
		other_ids = [{**second, "id": "z"}, {**first, "id": "y"}]
		self.compare(returning(result([first, second])), returning(result(other_ids)))

	def test_any_other_difference_fails(self):
		changed = {
			"a row field": result([{"id": "a", "voucher_no": "X", "remaining": "11"}]),
			"the row count": result([]),
			"excluded rows": result(excluded_rows=[{"voucher_no": "X"}]),
			"notes": result(notes={"invoice_level": {"count": 1}}),
			"the total": result(canonical_total="11"),
			"completeness": result(complete_for_company=False),
			"scope reasons": result(scope_reasons=["filtered_obligations"]),
			"a field outside the plan list": result(precision=3),
		}
		for label, other in changed.items():
			with self.subTest(label), self.assertRaises(AssertionError):
				self.compare(returning(result()), returning(other))

	def test_excluded_rows_keep_their_order(self):
		first, second = {"voucher_no": "X"}, {"voucher_no": "Y"}
		with self.assertRaises(AssertionError):
			self.compare(
				returning(result(excluded_rows=[first, second])),
				returning(result(excluded_rows=[second, first])),
			)

	def test_the_same_refusal_code_is_raised_again(self):
		with self.assertRaises(CalendarSourceUnavailable) as raised:
			self.compare(refusing("too_large"), refusing("too_large"))
		self.assertEqual(raised.exception.code, "too_large")

	def test_a_different_refusal_code_fails(self):
		with self.assertRaises(AssertionError):
			self.compare(refusing("too_large"), refusing("reconciliation_mismatch"))

	def test_a_refusal_on_one_side_only_fails(self):
		with self.assertRaises(AssertionError):
			self.compare(refusing("too_large"), returning(result()))
		with self.assertRaises(AssertionError):
			self.compare(returning(result()), refusing("too_large"))

	def test_other_errors_must_match_in_type(self):
		def invalid(*args, **kwargs):
			raise frappe.ValidationError("Invalid calendar window.")

		with self.assertRaises(frappe.ValidationError):
			self.compare(invalid, invalid)
		with self.assertRaises(AssertionError):
			self.compare(invalid, refusing("too_large"))

	def test_the_multibook_and_too_large_precedence_change_is_the_only_accepted_pair(self):
		with patch.object(compatibility, "source_profile", return_value=SimpleNamespace(major="16")):
			with self.assertRaises(CalendarSourceUnavailable) as raised:
				self.compare(refusing("too_large"), refusing("multibook_unsupported"))
			self.assertEqual(raised.exception.code, "too_large")
			for real, oracle in (
				("multibook_unsupported", "too_large"),
				("too_large", "reconciliation_mismatch"),
				("reconciliation_mismatch", "multibook_unsupported"),
			):
				with self.subTest(real=real, oracle=oracle), self.assertRaises(AssertionError):
					self.compare(refusing(real), refusing(oracle))

	def test_the_accepted_pair_is_not_accepted_on_v15(self):
		with patch.object(compatibility, "source_profile", return_value=SimpleNamespace(major="15")):
			with self.assertRaises(AssertionError):
				self.compare(refusing("too_large"), refusing("multibook_unsupported"))


class TestDifferentialIsWired(TestCase):
	"""A re-run scenario must really pass through the comparator, or the proof would be empty."""

	def compared_by(self, case):
		before = TALLY["compared"]
		outcome = TestResult()
		case.run(outcome)
		self.assertTrue(outcome.wasSuccessful(), outcome.failures + outcome.errors)
		return TALLY["compared"] - before

	def test_a_sources_scenario_is_compared(self):
		case = ORACLE_CASES["TestPaymentCalendarSources"](
			"test_unpaid_and_partial_terms_reconcile_without_duplication"
		)
		self.assertEqual(self.compared_by(case), 1)

	def test_a_reconciliation_scenario_is_compared(self):
		case = ORACLE_CASES["TestPaymentCalendarReconciliation"](
			"test_withholding_uses_native_invoice_balance_and_preserves_the_only_due_date"
		)
		self.assertEqual(self.compared_by(case), 1)

	def test_a_fallback_scenario_is_compared_and_runs_upstream(self):
		case = ORACLE_FALLBACK_CASES["TestPaymentCalendarSources"](
			"test_unpaid_and_partial_terms_reconcile_without_duplication"
		)
		upstream = ReceivablePayableReport.get_payment_terms
		calls = []

		@wraps(upstream)  # inspect.getsource follows __wrapped__, so the installed hash stays readable
		def spy(*args):
			calls.append(args)
			return upstream(*args)

		batched = obligation_sources._report_class()
		with (
			patch.object(ReceivablePayableReport, "get_payment_terms", spy),
			patch.object(
				batched, "_schedule_rows", autospec=True, side_effect=batched._schedule_rows
			) as fast,
		):
			self.assertEqual(self.compared_by(case), 1)
		self.assertTrue(calls, "the fallback family did not reach the upstream get_payment_terms")
		self.assertFalse(fast.called, "the fallback family ran the batched schedule query")
		self.assertIs(obligation_sources.FAST_PATH, True, "the fallback family left FAST_PATH off")
		self.assertEqual(obligation_sources._warned_fallbacks, set())

	def test_a_fast_path_only_scenario_is_skipped_in_the_fallback_family(self):
		case = ORACLE_FALLBACK_CASES["TestBatchedPaymentTerms"]("test_" + FAST_PATH_ONLY_SCENARIO)
		outcome = TestResult()
		case.run(outcome)
		self.assertEqual(len(outcome.skipped), 1, outcome.skipped)
		self.assertIn("speed path inactive", outcome.skipped[0][1])
		self.assertTrue(outcome.wasSuccessful())

	def test_an_opted_out_scenario_is_not_compared(self):
		class OptedOut(ORACLE_CASES["TestPaymentCalendarSources"]):
			@no_oracle_compare("wiring test")
			def test_marked(self):
				self.fx.invoice()
				self.read()

		self.assertEqual(self.compared_by(OptedOut("test_marked")), 0)


class RunUnderOracle:
	"""Re-runs a scenario with every `read_obligations` call compared to the frozen oracle."""

	def setUp(self):
		scenario = getattr(self, self._testMethodName)
		if not getattr(scenario, "no_oracle_compare", None):
			comparator = compare_with_oracle(REAL_READ_OBLIGATIONS)
			# Both modules bound the name at import, so each binding is replaced, not only the source module.
			for module in (obligation_sources, sources_tests, reconciliation_tests):
				patcher = patch.object(module, "read_obligations", comparator)
				patcher.start()
				self.addCleanup(patcher.stop)
		super().setUp()


class RunUnderOracleFallback(RunUnderOracle):
	"""The same comparison with every speed override forced onto its upstream fallback.

	Fast-path-only scenarios (`speed_path`) skip here: they assert the batching, which is inactive by design.
	"""

	def setUp(self):
		if self._testMethodName in TESTS_OF_UPSTREAM_MATCHES:
			self.skipTest("asserts the real `upstream_matches`, which this family replaces")
		patcher = patch.object(compatibility, "upstream_matches", return_value=False)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(self.restore_fast_path)
		super().setUp()

	def restore_fast_path(self):
		obligation_sources.FAST_PATH = True
		obligation_sources._warned_fallbacks.clear()


REAL_READ_OBLIGATIONS = obligation_sources.read_obligations
FAST_PATH_ONLY_SCENARIO = "batched_terms_equal_upstream_per_invoice"
TESTS_OF_UPSTREAM_MATCHES = (
	"test_upstream_matches_is_false_for_an_unknown_major_or_hash",
	"test_upstream_matches_is_false_when_the_source_is_unreadable",
)
ORACLE_CASES = {}
ORACLE_FALLBACK_CASES = {}


def rerun_under_oracle(module):
	"""`Oracle<Name>` and `OracleFallback<Name>` per test class of `module`: every scenario, none hand-picked."""
	for name, case in vars(module).items():
		is_case = isinstance(case, type) and issubclass(case, TestCase) and case.__module__ == module.__name__
		if is_case and any(attribute.startswith("test") for attribute in dir(case)):
			for prefix, mixin, family in (
				("Oracle", RunUnderOracle, ORACLE_CASES),
				("OracleFallback", RunUnderOracleFallback, ORACLE_FALLBACK_CASES),
			):
				family[name] = type(f"{prefix}{name}", (mixin, case), {"__module__": __name__})


for tests in (sources_tests, reconciliation_tests):
	rerun_under_oracle(tests)
globals().update({case.__name__: case for case in (*ORACLE_CASES.values(), *ORACLE_FALLBACK_CASES.values())})
