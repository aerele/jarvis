"""Payment Calendar S2 T4: `snapshot.collect` sends a `too_large` side through `routing.read_side`.

Committed fixtures (`consistent_read` rolls back first). The switch is patched, never saved: no test
commits a mode. "Above the guard" is `sources.MAX_VOUCHERS = 0`, or `sources.MAX_LEDGER_ROWS = 0`.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_collect_routing
"""

from time import monotonic
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from jarvis.payment_calendar import read_log, routing, snapshot, sources, store
from jarvis.payment_calendar.access import authorize_scope
from jarvis.payment_calendar.errors import CalendarSourceUnavailable, ReaderFallback
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.tests.payment_calendar_differential import _assert_equal
from jarvis.tests.payment_calendar_fixtures import (
	CalendarFixtures,
	require_calendar_test_site,
	sync_outstanding,
)

SIDES = ("Payable", "Receivable")
KEY = "k" * 44
LIFTED = 10**9


class CollectRoutingCase(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		self.fx = CalendarFixtures()
		self.fx.invoice((100, 50), side="Payable", paid=30)
		self.fx.invoice((200,), side="Receivable", paid=20)
		self.fx.fiscal_year()
		self.fx.gl(1000)
		sync_outstanding(self.fx.company)
		frappe.db.commit()
		self.scope = normalize_scope(self.fx.company, [self.fx.bank])
		self.fingerprint = authorize_scope(self.scope)["fingerprint"]
		self.settings = MagicMock(return_value=routing.ReaderSettings("on", (self.fx.company,), ()))
		for patcher in (
			patch.dict(frappe.conf, {"encryption_key": KEY}),
			patch.object(routing, "reader_settings", self.settings),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		self.clean_keys()
		self.addCleanup(self.clean_keys)

	def tearDown(self):
		frappe.db.rollback()
		for doctype, name in reversed(self.fx.created):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		frappe.cache.hdel("fiscal_years", self.fx.company)
		frappe.clear_document_cache("Company", self.fx.company)

	def clean_keys(self):
		for number in range(routing.READER_SLOTS):
			frappe.cache.execute_command("DEL", routing.slot_key(number))
		for side in SIDES:
			frappe.cache.execute_command("DEL", routing.memo_key(self.fx.company, side))
			frappe.cache.execute_command("DEL", routing.inflight_key(self.fx.company, side))
		# The day counters of every date the tests wrote (a collect test without a date seam writes today's).
		digest = read_log.company_digest(self.fx.company)
		for key in frappe.cache.execute_command("KEYS", frappe.cache.make_key(f"jarvis:pcal:*{digest}*")):
			frappe.cache.execute_command("DEL", key)

	def collect(self):
		return snapshot.collect(self.scope, self.fingerprint)

	def todays_unguarded(self, side):
		"""Today's read with the guards lifted: the oracle the reader must equal, ids included."""
		scope = self.scope
		with patch.object(sources, "MAX_VOUCHERS", LIFTED), patch.object(sources, "MAX_LEDGER_ROWS", LIFTED):
			return sources.read_obligations(
				scope["company"], side, scope["anchor"], scope["window_start"], scope["window_end"]
			)


class TestBelowTheGuard(CollectRoutingCase):
	def test_a_served_side_reads_one_setting_per_view_and_never_calls_the_reader(self):
		for mode in ("off", "shadow", "on"):
			with self.subTest(mode):
				self.settings.return_value = routing.ReaderSettings(mode, (self.fx.company,), ())
				self.settings.reset_mock()
				with patch.object(routing.builtin_reader, "read_obligations") as reader:
					result = self.collect()
				# The bank one-pass check and the book memo share one settings read per view, in every mode.
				self.assertEqual(self.settings.call_count, 1)
				reader.assert_not_called()
				self.assertEqual(result["unavailable"], {})
				for side in SIDES:
					_assert_equal(result["sources"][side]["rows"], self.todays_unguarded(side)["rows"], side)

	def test_collect_hands_the_router_todays_read_the_start_time_and_an_outcomes_list(self):
		with patch.object(routing, "read_side", wraps=routing.read_side) as router:
			before = monotonic()
			self.collect()
		self.assertEqual(router.call_count, 2)
		self.assertEqual([call.args[2] for call in router.call_args_list], list(SIDES))
		outcomes = router.call_args.kwargs["outcomes"]
		for call in router.call_args_list:
			self.assertIs(call.args[0], snapshot.read_obligations)
			self.assertIs(call.kwargs["outcomes"], outcomes)
			self.assertTrue(before - 1 <= call.kwargs["started"] <= monotonic())
		self.assertEqual([record["side"] for record in outcomes], list(SIDES))
		self.assertEqual({record["path"] for record in outcomes}, {"today"})


class TestAboveTheGuard(CollectRoutingCase):
	def above_the_guard(self, kind):
		return patch.object(sources, "MAX_VOUCHERS" if kind == "vouchers" else "MAX_LEDGER_ROWS", 0)

	def test_a_served_eligible_user_gets_the_reader_equal_to_todays_unguarded_read(self):
		for kind in ("vouchers", "ledger rows"):
			with self.subTest(kind):
				with self.above_the_guard(kind):
					result = self.collect()
				self.assertEqual(result["unavailable"], {})
				for side in SIDES:
					expected = self.todays_unguarded(side)
					self.assertTrue(expected["rows"])
					# Ids encode the position of each row, so equal rows and ids mean the same read.
					self.assertEqual(
						[row["id"] for row in result["sources"][side]["rows"]],
						[row["id"] for row in expected["rows"]],
					)
					_assert_equal(result["sources"][side]["rows"], expected["rows"], f"{kind} {side}")

	def test_off_and_shadow_and_a_company_not_listed_give_todays_too_large(self):
		cases = {
			"off": routing.ReaderSettings("off", (self.fx.company,), ()),
			"shadow": routing.ReaderSettings("shadow", (self.fx.company,), (self.fx.company,)),
			"shadow list only": routing.ReaderSettings("on", (), (self.fx.company,)),
		}
		for label, settings in cases.items():
			with self.subTest(label):
				self.settings.return_value = settings
				with (
					self.above_the_guard("vouchers"),
					patch.object(routing.builtin_reader, "read_obligations") as reader,
				):
					result = self.collect()
				reader.assert_not_called()
				self.assertEqual(result["unavailable"], {side: "too_large" for side in SIDES})
				self.assertTrue(all(result["sources"][side] is None for side in SIDES))

	def test_a_reader_that_cannot_serve_leaves_todays_too_large_for_that_side_only(self):
		real = routing.builtin_reader.read_obligations

		def read(company, side, *args, **kwargs):
			if side == "Receivable":
				raise frappe.db.OperationalError(1054, "Unknown column")
			return real(company, side, *args, **kwargs)

		with self.above_the_guard("vouchers"), patch.object(routing.builtin_reader, "read_obligations", read):
			result = self.collect()
		self.assertEqual(result["unavailable"], {"Receivable": "too_large"})
		self.assertTrue(result["sources"]["Payable"]["rows"])

	def test_a_failed_statement_in_the_probe_leaves_too_large_and_the_other_side_served(self):
		real, calls = sources._ledger_rows_exceed, {}

		def probe(company, side, anchor, limit):
			# The router's probe has today's own cap now: it is the second probe of a side (today's read goes first).
			calls[side] = calls.get(side, 0) + 1
			if side == "Receivable" and calls[side] == 2:
				raise frappe.db.OperationalError(1969, "Query execution was interrupted")
			return real(company, side, anchor, limit)

		with (
			self.above_the_guard("vouchers"),
			patch.object(sources, "_ledger_rows_exceed", probe),
		):
			result = self.collect()
		self.assertEqual(result["unavailable"], {"Receivable": "too_large"})
		self.assertTrue(result["sources"]["Payable"]["rows"])

	@staticmethod
	def rolled_back_then(error):
		"""A side effect that does what the server does on a deadlock: a real ROLLBACK, then the error.

		Autocommit is left as it is (off), so the next statement opens a NEW transaction: that is the case the
		view's own exit check cannot see.
		"""
		connection = frappe.db._conn

		def run(*args, **kwargs):
			connection.rollback()
			raise error

		return run

	def view_errors(self):
		return (
			frappe.QueryDeadlockError("deadlock"),
			frappe.QueryTimeoutError("lock wait"),
			frappe.db.OperationalError(1213, "deadlock"),
			frappe.db.OperationalError(4031, "idle"),
			frappe.db.OperationalError(2013, "lost"),
			frappe.db.InterfaceError("closed"),
		)

	def assert_view_fails(self, error, patcher):
		"""The router swallows `error` (a refusal), and the job still fails: the view is marked broken."""
		self.clean_keys()  # the previous error left a memo, which would keep the reader from running
		with self.above_the_guard("vouchers"), patcher(self.rolled_back_then(error)):
			with self.assertRaises(CalendarSourceUnavailable) as raised:
				self.collect()
		self.assertEqual(raised.exception.code, "source_changed", repr(error))

	def test_a_reader_that_loses_the_view_to_a_rollback_fails_the_job_with_normal_autocommit(self):
		for error in self.view_errors():
			with self.subTest(type(error).__name__):
				self.assert_view_fails(
					error,
					lambda kill: patch(
						"jarvis.payment_calendar.builtin_reader.read_obligations", side_effect=kill
					),
				)

	def test_a_probe_that_loses_the_view_to_a_rollback_fails_the_job_with_normal_autocommit(self):
		real = sources._ledger_rows_exceed

		def patcher(kill):
			calls = {}

			def probe(company, side, anchor, limit):
				calls[side] = calls.get(side, 0) + 1
				if calls[side] == 2:  # the router's probe; today's read goes first
					kill()
				return real(company, side, anchor, limit)

			return patch.object(sources, "_ledger_rows_exceed", probe)

		for error in self.view_errors():
			with self.subTest(type(error).__name__):
				self.assert_view_fails(error, patcher)

	def test_an_eligibility_read_that_loses_the_view_to_a_rollback_fails_the_job(self):
		for error in self.view_errors():
			with self.subTest(type(error).__name__):
				self.assert_view_fails(
					error,
					lambda kill: patch(
						"jarvis.payment_calendar.eligibility.get_user_permissions", side_effect=kill
					),
				)

	def test_a_statement_error_does_not_break_the_view_and_payable_is_still_served(self):
		for code in (1054, 1969):
			with self.subTest(code):
				real = routing.builtin_reader.read_obligations

				def read(company, side, *args, code=code, **kwargs):
					if side == "Receivable":
						raise frappe.db.OperationalError(code, "statement failed")
					return real(company, side, *args, **kwargs)

				with (
					self.above_the_guard("vouchers"),
					patch.object(routing.builtin_reader, "read_obligations", read),
				):
					result = self.collect()
				self.assertEqual(result["unavailable"], {"Receivable": "too_large"})
				self.assertTrue(result["sources"]["Payable"]["rows"])

	def test_a_non_too_large_refusal_is_not_routed(self):
		refusal = CalendarSourceUnavailable("multiple_invoice_accounts")
		with (
			patch.object(snapshot, "read_obligations", side_effect=refusal),
			patch.object(routing.builtin_reader, "read_obligations") as reader,
		):
			result = self.collect()
		reader.assert_not_called()
		self.assertEqual(result["unavailable"], {side: "multiple_invoice_accounts" for side in SIDES})


class TestOutcomesInCollect(CollectRoutingCase):
	"""The records of the routed sides are logged after the view, also when the job then fails."""

	DATE = "2031-04-02"

	def setUp(self):
		super().setUp()
		self.log_error, self.in_view = MagicMock(), []

		def log_error(*args, **kwargs):
			self.in_view.append(snapshot.view_is_open())

		self.log_error.side_effect = log_error
		for patcher in (
			patch.object(routing, "_today", return_value=self.DATE),
			patch.object(frappe, "log_error", self.log_error),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		self.digest = read_log.company_digest(self.fx.company)
		self.addCleanup(self.clean_outcome_keys)

	def clean_outcome_keys(self):
		pattern = frappe.cache.make_key(f"jarvis:pcal:*{self.digest}*")
		for key in frappe.cache.execute_command("KEYS", pattern):
			frappe.cache.execute_command("DEL", key)

	def counts(self):
		return read_log.day_counts(self.digest, self.DATE)

	def above_the_guard(self):
		return patch.object(sources, "MAX_VOUCHERS", 0)

	def fail_reader(self, side="Receivable", reason="pin_drift"):
		real = routing.builtin_reader.read_obligations

		def read(company, current, *args, **kwargs):
			if current == side:
				raise ReaderFallback(reason, ("compat.source",))
			return real(company, current, *args, **kwargs)

		return patch.object(routing.builtin_reader, "read_obligations", read)

	def test_a_fallback_job_still_publishes_payable_and_counts_the_fallback(self):
		with self.above_the_guard(), self.fail_reader():
			result = self.collect()
		self.assertEqual(result["unavailable"], {"Receivable": "too_large"})
		self.assertTrue(result["sources"]["Payable"]["rows"])
		store.split_result(result, self.scope)
		self.assertEqual(self.counts(), {"fallback:pin_drift": 1, "served": 1})

	def test_log_error_runs_only_after_the_view(self):
		with self.above_the_guard(), self.fail_reader():
			self.collect()
		self.assertEqual(self.in_view, [False])

	def test_a_raise_after_the_reader_still_writes_the_record(self):
		contract = CalendarSourceUnavailable("source_contract_unavailable")
		with (
			self.above_the_guard(),
			patch.object(snapshot, "check_source_contract", side_effect=contract),
			self.assertRaises(CalendarSourceUnavailable),
		):
			self.collect()
		self.assertEqual(self.counts(), {"served": 2})

	def test_a_failure_before_the_sides_ends_cleanly_with_no_record(self):
		with (
			self.above_the_guard(),
			patch.object(snapshot, "read_bank_balances", side_effect=RuntimeError("boom")),
			self.assertRaises(RuntimeError),
		):
			self.collect()
		self.assertEqual(self.counts(), {})

	def test_a_side_today_serves_is_not_counted(self):
		self.collect()
		self.assertEqual(self.counts(), {})
		self.log_error.assert_not_called()

	def test_mode_off_writes_no_reader_line_below_or_above_the_guard(self):
		self.settings.return_value = routing.ReaderSettings("off", (self.fx.company,), ())
		with patch.object(frappe, "logger", return_value=MagicMock()) as logger:
			self.collect()
			with self.above_the_guard():
				self.collect()
		infos = [call.args[0] for call in logger.return_value.info.call_args_list]
		self.assertEqual([line for line in infos if line.startswith("reader_outcome")], [])
		self.assertEqual(self.counts(), {})

	def test_a_failing_outcome_writer_never_changes_the_result(self):
		with (
			self.above_the_guard(),
			patch.object(read_log, "reader_outcomes", side_effect=RuntimeError("log down")),
		):
			result = self.collect()
		self.assertEqual(result["unavailable"], {})

	def test_a_failing_build_lookup_only_blanks_the_build_field(self):
		seen = []
		with (
			self.above_the_guard(),
			patch.object(routing, "reader_build", side_effect=OSError("disk")),
			patch.object(read_log, "reader_outcomes", side_effect=lambda records, date: seen.extend(records)),
		):
			self.collect()
		self.assertEqual(len(seen), 2, "the records are still written")
		self.assertEqual([item["reader_build"] for item in seen], [None, None])
		self.assertTrue(all(isinstance(item["view_ms"], int) for item in seen))

	def test_the_view_length_and_build_reach_the_records(self):
		seen = []
		with (
			self.above_the_guard(),
			patch.object(read_log, "reader_outcomes", side_effect=lambda records, date: seen.extend(records)),
		):
			self.collect()
		self.assertEqual(len(seen), 2)
		for item in seen:
			self.assertIsInstance(item["view_ms"], int)
			self.assertRegex(item["reader_build"], r"^[0-9a-f]{12}$")
