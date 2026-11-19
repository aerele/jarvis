"""Proof that the built-in reader returns today's result for every scenario of the sources, reconciliation and
voucher-type modules, and that the comparator itself detects every kind of difference.

`rerun_under_reader(module)` builds a `Reader<Name>` class per test class of a module. Each scenario's
`read_obligations` calls go through `compare_reader_with_today`: the reader runs, today's read (the real
oracle) runs, and any difference fails the scenario. Today's read is never patched here
(`sources.read_obligations` is the oracle); only the names the three modules bound at import are.
"""

import dis
import inspect
from unittest import TestCase, TestResult
from unittest.mock import patch

from jarvis.payment_calendar import builtin_reader
from jarvis.payment_calendar import sources as today_sources
from jarvis.payment_calendar.errors import READER_FALLBACK_REASONS, CalendarSourceUnavailable, ReaderFallback
from jarvis.tests import test_payment_calendar_reconciliation as reconciliation_tests
from jarvis.tests import test_payment_calendar_sources as sources_tests
from jarvis.tests import test_payment_calendar_voucher_types as voucher_tests
from jarvis.tests.payment_calendar_differential import (
	READER_TALLY,
	compare_reader_with_today,
	first_line_unread,
	no_reader_compare,
)
from jarvis.tests.payment_calendar_fixtures import sync_outstanding

REAL_TODAY = today_sources.read_obligations
MODULES = (sources_tests, reconciliation_tests, voucher_tests)

# Scenarios whose reader outcome is a declared fallback: test name -> the ReaderFallback reason. Each one is
# a scenario that changes the very condition the reader's gate checks (the user, the major, the pins), so the
# fallback is the correct outcome and today's read then serves it. `control_total` is never declared here: a
# control-total fallback on a scenario would be a reader defect.
EXPECTED_FALLBACKS: dict[str, str] = {
	# A restricted user (user permissions, cost-center scope, sales person, party permissions): the reader
	# reads without the report's per-user narrowing, so OC2 declines and today's read serves it.
	"test_restricted_customer_does_not_leak_excluded_exchange_residue": "ineligible",
	"test_restricted_supplier_does_not_leak_fallback_counts_or_totals": "ineligible",
	"test_cost_center_scope_hides_settled_rows_and_denies_unscoped_bank_cash": "ineligible",
	"test_invoice_permission_filters_the_settled_overlay_too": "ineligible",
	"test_sales_person_restriction_does_not_turn_skipped_invoices_into_settled_rows": "ineligible",
	"test_supplier_permission_cannot_leak_a_fully_paid_other_supplier": "ineligible",
	# The scenario replaces a pinned upstream source: the reader's pins no longer match what it audited.
	"test_a_renamed_accumulator_refuses_the_read": "pin_drift",
}

# Scenarios the reader differential cannot run, test name -> reason. Only a scenario that patches internals
# of today's `sources` that the reader never calls, or reads the engine back through a spy.
READER_OPT_OUTS: dict[str, str] = {
	"test_native_clone_with_shared_caches": (
		"bounds the queries of ONE read; the comparator runs the reader and today's read, which doubles the count"
	),
}


# Scenarios that insert real invoices (the headers are right) and assert that a read leaves the invoice
# unchanged: they run without the header upkeep (`sync_outstanding` updates outstanding_amount by design).
NO_HEADER_UPKEEP = {
	"test_foreign_rounding_does_not_change_native_reports_or_documents",
	"test_ordinary_reports_and_financial_documents_are_unchanged",
}

# The real runs, by scenario id. Filled only by a `Reader*` scenario that ran the real reader beside today's
# read (a comparison or a declared fallback); comparator unit tests and wiring tests never add to them.
REAL_SCENARIOS: dict[str, int] = {}
REAL_FALLBACKS: dict[str, int] = {}


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


def falling_back(reason):
	def fall(*args, **kwargs):
		raise ReaderFallback(reason)

	return fall


def returning(value):
	return lambda *args, **kwargs: value


def calls(counter):
	"""A `today` that counts its calls and refuses `too_large` until its guards are lifted."""

	def today(*args, **kwargs):
		counter.append(today_sources.MAX_VOUCHERS)
		if today_sources.MAX_VOUCHERS < 10**9:
			raise CalendarSourceUnavailable("too_large")
		return result()

	return today


class TestReaderComparator(TestCase):
	"""The comparator that wraps the reader must itself detect every kind of difference."""

	ARGS = ("company", "Payable", "2026-01-01", "2026-01-01", "2026-02-01")

	def compare(self, reader, today, **options):
		options.setdefault("sync", None)
		return compare_reader_with_today(reader, today, **options)(*self.ARGS)

	def test_equal_results_pass_are_counted_and_the_reader_result_is_returned(self):
		reader_result = result()
		before = dict(READER_TALLY)
		self.assertIs(self.compare(returning(reader_result), returning(result())), reader_result)
		self.assertEqual(READER_TALLY["compared"], before["compared"] + 1)
		self.assertEqual(READER_TALLY["refusals"], before["refusals"])

	def test_row_order_is_ignored(self):
		first = {"id": "a", "voucher_no": "X", "remaining": "10"}
		second = {"id": "b", "voucher_no": "Y", "remaining": "5"}
		self.compare(returning(result([first, second])), returning(result([second, first])))

	def test_any_field_difference_fails(self):
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

	def test_a_refusal_on_one_side_only_fails(self):
		with self.assertRaises(AssertionError):
			self.compare(refusing("too_large"), returning(result()))
		with self.assertRaises(AssertionError):
			self.compare(returning(result()), refusing("too_large"))

	def test_the_same_refusal_is_raised_again_and_a_different_code_fails(self):
		with self.assertRaises(CalendarSourceUnavailable) as raised:
			self.compare(refusing("unknown_code"), refusing("unknown_code"))
		self.assertEqual(raised.exception.code, "unknown_code")
		with self.assertRaises(AssertionError):
			self.compare(refusing("unknown_code"), refusing("reconciliation_mismatch"))

	def test_the_1a_accepted_refusal_pair_is_not_accepted_here(self):
		with self.assertRaises(AssertionError):
			self.compare(refusing("too_large"), refusing("multibook_unsupported"))

	def test_an_undeclared_fallback_fails(self):
		with self.assertRaises(AssertionError):
			self.compare(falling_back("ineligible"), returning(result()))
		with self.assertRaises(AssertionError):
			self.compare(falling_back("ineligible"), returning(result()), expected_fallback="pin_drift")

	def test_a_declared_fallback_returns_todays_outcome_and_is_counted(self):
		today_result = result(canonical_total="99")
		before = READER_TALLY["fallbacks"]["ineligible"]
		served = self.compare(
			falling_back("ineligible"), returning(today_result), expected_fallback="ineligible"
		)
		self.assertIs(served, today_result)
		self.assertEqual(READER_TALLY["fallbacks"]["ineligible"], before + 1)
		with self.assertRaises(CalendarSourceUnavailable):
			self.compare(falling_back("ineligible"), refusing("too_large"), expected_fallback="ineligible")

	def test_a_declared_fallback_that_does_not_happen_fails(self):
		with self.assertRaises(AssertionError):
			self.compare(returning(result()), returning(result()), expected_fallback="ineligible")

	def test_a_too_large_today_is_compared_with_the_lifted_read_and_re_raised(self):
		seen = []
		before = READER_TALLY["lifted"]
		with self.assertRaises(CalendarSourceUnavailable) as raised:
			self.compare(returning(result()), calls(seen))
		self.assertEqual(raised.exception.code, "too_large")
		self.assertEqual(len(seen), 2)
		self.assertGreaterEqual(seen[1], 10**12, "the second run of today's read has its guards lifted")
		self.assertEqual(READER_TALLY["lifted"], before + 1)
		# The lifted comparison fails on a reader difference.
		with self.assertRaises(AssertionError):
			self.compare(returning(result(canonical_total="11")), calls([]))
		# The guards are restored afterwards.
		self.assertLess(today_sources.MAX_VOUCHERS, 10**9)

	def test_ids_differ_only_in_the_accepted_case(self):
		moved = result([{"id": "b", "voucher_no": "X", "remaining": "10"}])
		with self.assertRaises(AssertionError):
			self.compare(returning(moved), returning(result()), explain_ids=lambda *args: False)
		before = READER_TALLY["id_only"]
		self.assertIs(
			self.compare(returning(moved), returning(result()), explain_ids=lambda *args: True), moved
		)
		self.assertEqual(READER_TALLY["id_only"], before + 1)

	def test_the_explanation_gets_the_keys_of_the_rows_whose_ids_differ(self):
		first = {"id": "a", "account": "A", "voucher_type": "T", "voucher_no": "X", "party": "P"}
		second = {"id": "b", "account": "A", "voucher_type": "T", "voucher_no": "Y", "party": "P"}
		moved = [first, {**second, "id": "c"}]
		asked = []

		def explain(args, kwargs, read_names, keys):
			asked.append(keys)
			return True

		self.compare(returning(result(moved)), returning(result([first, second])), explain_ids=explain)
		self.assertEqual(asked, [{("A", "T", "Y", "P")}])

	def test_equal_ids_do_not_ask_for_an_explanation(self):
		def explain(*args):
			raise AssertionError("asked")

		self.compare(returning(result()), returning(result()), explain_ids=explain)

	def test_an_id_difference_never_excuses_an_amount_difference(self):
		moved = result([{"id": "b", "voucher_no": "X", "remaining": "11"}])
		with self.assertRaises(AssertionError):
			self.compare(returning(moved), returning(result()), explain_ids=lambda *args: True)

	def test_swapped_ids_on_two_rows_are_a_difference(self):
		first = {"id": "a", "voucher_no": "X", "remaining": "10"}
		second = {"id": "b", "voucher_no": "Y", "remaining": "5"}
		swapped = [{**first, "id": "b"}, {**second, "id": "a"}]
		with self.assertRaises(AssertionError):
			self.compare(
				returning(result(swapped)),
				returning(result([first, second])),
				explain_ids=lambda *args: False,
			)

	def test_the_sync_runs_first_and_receives_the_company(self):
		order = []

		def reader(*args, **kwargs):
			order.append("reader")
			return result()

		compare_reader_with_today(reader, returning(result()), sync=order.append)(*self.ARGS)
		self.assertEqual(order, ["company", "reader"])


class TestUnreadFirstLine(TestCase):
	"""The accepted difference, decided from the full ledger and the lines the reader read."""

	@staticmethod
	def line(name, key, date):
		return {"name": name, "key": key, "posting_date": date}

	def test_all_lines_read_means_no_accepted_difference(self):
		lines = [self.line("a", "K", 1), self.line("b", "K", 2), self.line("c", "L", 1)]
		self.assertFalse(first_line_unread(lines, {"a", "b", "c"}))

	def test_an_unread_first_line_of_a_read_key_is_the_accepted_case(self):
		lines = [self.line("a", "K", 1), self.line("b", "K", 2)]
		self.assertTrue(first_line_unread(lines, {"b"}))

	def test_an_unread_line_after_the_first_read_line_is_not(self):
		lines = [self.line("a", "K", 1), self.line("b", "K", 2)]
		self.assertFalse(first_line_unread(lines, {"a"}))

	def test_a_key_with_no_read_line_is_not(self):
		lines = [self.line("a", "K", 1), self.line("b", "L", 2)]
		self.assertFalse(first_line_unread(lines, {"b"}))

	def test_only_the_keys_of_the_rows_with_a_changed_id_can_excuse_it(self):
		lines = [
			self.line("a", "K", 1),
			self.line("b", "K", 2),
			self.line("c", "L", 1),
			self.line("d", "L", 2),
		]
		read = {"b", "c", "d"}
		self.assertTrue(first_line_unread(lines, read), "K qualifies")
		self.assertTrue(first_line_unread(lines, read, {"K"}))
		self.assertFalse(
			first_line_unread(lines, read, {"L"}), "L has its first line read: K cannot excuse it"
		)
		self.assertFalse(first_line_unread(lines, read, set()))

	def test_a_tie_on_the_date_counts_because_the_collation_decides(self):
		lines = [self.line("a", "K", 1), self.line("b", "K", 1)]
		self.assertTrue(first_line_unread(lines, {"b"}))


class RunUnderReader:
	"""Re-runs a scenario with every `read_obligations` call compared to today's read."""

	def setUp(self):
		scenario = getattr(self, self._testMethodName)
		opted_out = getattr(scenario, "no_reader_compare", None) or READER_OPT_OUTS.get(self._testMethodName)
		if not opted_out:
			comparator = self.counted(
				compare_reader_with_today(
					builtin_reader.read_obligations,
					REAL_TODAY,
					sync=None if self._testMethodName in NO_HEADER_UPKEEP else sync_outstanding,
					expected_fallback=EXPECTED_FALLBACKS.get(self._testMethodName),
				)
			)
			# Each module bound the name at import, so each binding is replaced; never `sources` itself.
			for module in MODULES:
				patcher = patch.object(module, "read_obligations", comparator)
				patcher.start()
				self.addCleanup(patcher.stop)
		super().setUp()

	def counted(self, comparator):
		"""`comparator`, recording each call of this scenario that compared or declared a fallback."""

		def run(*args, **kwargs):
			reads, fallbacks = _reads_done(), _fallbacks_done()
			try:
				return comparator(*args, **kwargs)
			finally:
				if _reads_done() > reads:
					REAL_SCENARIOS[self.id()] = REAL_SCENARIOS.get(self.id(), 0) + 1
				if _fallbacks_done() > fallbacks:
					REAL_FALLBACKS[self.id()] = REAL_FALLBACKS.get(self.id(), 0) + 1

		return run


def _fallbacks_done():
	return sum(READER_TALLY["fallbacks"].values())


def _reads_done():
	"""Comparisons plus declared fallbacks so far (a comparison that fails adds nothing)."""
	return READER_TALLY["compared"] + READER_TALLY["lifted"] + _fallbacks_done()


READER_CASES = {}


def rerun_under_reader(module):
	"""`Reader<Name>` per test class of `module`: every scenario, none hand-picked."""
	for name, case in vars(module).items():
		is_case = isinstance(case, type) and issubclass(case, TestCase) and case.__module__ == module.__name__
		if is_case and any(attribute.startswith("test") for attribute in dir(case)):
			READER_CASES[name] = type(f"Reader{name}", (RunUnderReader, case), {"__module__": __name__})


for tests in MODULES:
	rerun_under_reader(tests)
globals().update({case.__name__: case for case in READER_CASES.values()})


class TestReaderDifferentialIsWired(TestCase):
	"""A re-run scenario must really pass through the comparator, or the proof would be empty."""

	def tallied(self, case, *keys):
		before = {key: READER_TALLY[key] for key in keys}
		outcome, real, real_fallbacks = TestResult(), dict(REAL_SCENARIOS), dict(REAL_FALLBACKS)
		case.run(outcome)
		# A wiring run is not the scenario's own run.
		REAL_SCENARIOS.clear()
		REAL_SCENARIOS.update(real)
		REAL_FALLBACKS.clear()
		REAL_FALLBACKS.update(real_fallbacks)
		self.assertTrue(outcome.wasSuccessful(), outcome.failures + outcome.errors)
		return {key: READER_TALLY[key] - before[key] for key in keys}

	def test_a_sources_scenario_is_compared(self):
		case = READER_CASES["TestPaymentCalendarSources"](
			"test_unpaid_and_partial_terms_reconcile_without_duplication"
		)
		self.assertEqual(self.tallied(case, "compared")["compared"], 1)

	def test_a_reconciliation_scenario_is_compared(self):
		case = READER_CASES["TestPaymentCalendarReconciliation"](
			"test_withholding_uses_native_invoice_balance_and_preserves_the_only_due_date"
		)
		self.assertEqual(self.tallied(case, "compared")["compared"], 1)

	def test_a_voucher_type_scenario_is_compared(self):
		case = READER_CASES["TestVoucherTypeScenarios"]("test_write_off_settles_the_rest_of_a_paid_invoice")
		self.assertGreaterEqual(self.tallied(case, "compared")["compared"], 1)

	def test_a_declared_fallback_scenario_adds_a_fallback_and_passes(self):
		name = "test_unpaid_and_partial_terms_reconcile_without_duplication"
		case = READER_CASES["TestPaymentCalendarSources"](name)
		with (
			patch.dict(EXPECTED_FALLBACKS, {name: "ineligible"}),
			patch.object(builtin_reader, "reader_ineligibility", return_value=["user_permissions"]),
		):
			before = READER_TALLY["fallbacks"]["ineligible"]
			tallied = self.tallied(case, "compared")
		self.assertEqual(tallied["compared"], 0)
		self.assertEqual(READER_TALLY["fallbacks"]["ineligible"], before + 1)

	def test_an_opted_out_scenario_adds_nothing(self):
		class OptedOut(READER_CASES["TestPaymentCalendarSources"]):
			@no_reader_compare("wiring test")
			def test_marked(self):
				self.fx.invoice()
				self.read()

		self.assertEqual(self.tallied(OptedOut("test_marked"), "compared")["compared"], 0)


class TestDeclaredLists(TestCase):
	def test_declared_lists_name_existing_tests_with_reasons(self):
		tests = {
			name
			for module in MODULES
			for case in vars(module).values()
			if isinstance(case, type) and issubclass(case, TestCase) and case.__module__ == module.__name__
			for name in dir(case)
			if name.startswith("test")
		}
		for label, declared in (
			("EXPECTED_FALLBACKS", EXPECTED_FALLBACKS),
			("READER_OPT_OUTS", READER_OPT_OUTS),
		):
			for name, reason in declared.items():
				with self.subTest(label, name=name):
					self.assertIn(name, tests)
					self.assertTrue(reason)
		for name, reason in EXPECTED_FALLBACKS.items():
			with self.subTest("fallback reason", name=name):
				self.assertIn(reason, READER_FALLBACK_REASONS)
				self.assertNotEqual(reason, "control_total")


def _code_objects(code):
	yield code
	for constant in code.co_consts:
		if inspect.iscode(constant):
			yield from _code_objects(constant)


def reads_through_the_binding(case, function, seen=None):
	"""True when `function` (a scenario, or a helper it reaches through `self`) loads the global name
	`read_obligations`: the module binding the family replaces. A call to `builtin_reader.read_obligations`
	(an attribute) never counts, since a scenario that calls it compares by itself."""
	seen = set() if seen is None else seen
	seen.add(function)
	for code in _code_objects(function.__code__):
		for instruction in dis.get_instructions(code):
			if instruction.opname == "LOAD_GLOBAL" and instruction.argval == "read_obligations":
				return True
			helper = (
				getattr(case, instruction.argval, None)
				if instruction.opname in ("LOAD_ATTR", "LOAD_METHOD")
				else None
			)
			if (
				inspect.isfunction(helper)
				and helper not in seen
				and reads_through_the_binding(case, helper, seen)
			):
				return True
	return False


def reader_scenarios():
	"""The scenarios of the generated `Reader*` classes that must run the reader beside today's read: those
	that reach the replaced `read_obligations` binding and are not opted out. Derived from the classes."""
	return {
		f"{case.__module__}.{case.__qualname__}.{name}"
		for case in READER_CASES.values()
		for name in dir(case)
		if name.startswith("test")
		and not getattr(getattr(case, name), "no_reader_compare", None)
		and name not in READER_OPT_OUTS
		and reads_through_the_binding(case, getattr(case, name))
	}


class TestTallyOfTheWholeFamily(TestCase):
	"""Runs after the `Reader*` classes (the loader orders classes by name): the family cannot pass empty."""

	def test_every_test_class_of_the_modules_has_a_reader_class(self):
		expected = {
			name
			for module in MODULES
			for name, case in vars(module).items()
			if isinstance(case, type)
			and issubclass(case, TestCase)
			and case.__module__ == module.__name__
			and any(attribute.startswith("test") for attribute in dir(case))
		}
		self.assertTrue(expected)
		self.assertEqual(set(READER_CASES), expected)

	def test_every_reader_scenario_ran_the_reader_beside_todays_read(self):
		expected = reader_scenarios()
		print(
			f"\nREADER_TALLY {dict(READER_TALLY)} real_scenarios={len(REAL_SCENARIOS)} "
			f"of {len(expected)} real_fallback_scenarios={len(REAL_FALLBACKS)}"
		)
		self.assertTrue(expected, "no Reader* scenario exists")
		self.assertGreater(READER_TALLY["compared"], 0, "no read was compared: run the whole module")
		missing = sorted(expected - set(REAL_SCENARIOS))
		self.assertGreaterEqual(
			len(REAL_SCENARIOS),
			len(expected),
			f"{len(missing)} Reader* scenarios did not run the reader (run the whole module): {missing[:5]}",
		)

	def test_no_fallback_was_a_control_total_and_only_declared_scenarios_fell_back(self):
		self.assertNotIn("control_total", READER_TALLY["fallbacks"])
		self.assertLessEqual(set(READER_TALLY["fallbacks"]), set(EXPECTED_FALLBACKS.values()))
		declared = {test for test in EXPECTED_FALLBACKS}
		fell_back = {scenario.rsplit(".", 1)[1] for scenario in REAL_FALLBACKS}
		self.assertLessEqual(fell_back, declared)
