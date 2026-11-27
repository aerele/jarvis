"""Payment Calendar S2 T4: `routing.read_side`, the router that sends a `too_large` side to the reader.

Unit cases use a fake today's read and a fake reader, so each routing rule is tested alone. No test
commits a mode: the settings are patched, or (one case) written inside the test transaction and rolled back.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_router
"""

import gc
import weakref
from contextlib import nullcontext
from time import monotonic
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from rq.timeouts import JobTimeoutException

from jarvis.payment_calendar import read_log, routing, snapshot, sources, store
from jarvis.payment_calendar.errors import READER_FALLBACK_REASONS, CalendarSourceUnavailable, ReaderFallback
from jarvis.tests.payment_calendar_majors import GATE_NO_V15_READER_PINS, interface_error, v16_only

COMPANY = "Router Test Company"
OTHER = "Another Router Company"
SIDE = "Receivable"
ARGS = (COMPANY, SIDE, "2026-11-20", "2026-11-20", "2026-12-20")
TODAY_RESULT = {"rows": [{"id": "today"}]}
READER_RESULT = {"rows": [{"id": "reader"}]}
KEY = "k" * 44
SLOW = {"deadline", "assemble_budget", "too_many_lines"}
DETERMINISTIC = {
	"control_total",
	"rekey_unresolved",
	"reader_error",
	"pin_drift",
	"unsupported_major",
	"eligibility_unknown",
}


def redis(*command):
	"""A raw Redis command; the router's keys are already made with the site prefix."""
	return frappe.cache.execute_command(*command)


def too_large(*args, **kwargs):
	raise CalendarSourceUnavailable("too_large")


class RouterCase(TestCase):
	"""Today refuses `too_large`; the switch is `on` for COMPANY; the reader is a mock that serves."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.outcomes = []
		self.today = MagicMock(side_effect=too_large)
		self.reader = MagicMock(return_value=READER_RESULT)
		self.settings = MagicMock(return_value=routing.ReaderSettings("on", (COMPANY,), ()))
		self.probe = MagicMock(return_value=False)
		for patcher in (
			patch.dict(frappe.conf, {"encryption_key": KEY}),
			patch.object(routing, "reader_settings", self.settings),
			patch.object(routing.builtin_reader, "read_obligations", self.reader),
			patch.object(sources, "_ledger_rows_exceed", self.probe),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		self.clean()
		self.addCleanup(self.clean)

	def clean(self):
		for n in range(routing.READER_SLOTS):
			redis("DEL", routing.slot_key(n))
		redis("DEL", routing.memo_key(COMPANY, SIDE))
		for side in (SIDE, "Payable"):
			redis("DEL", routing.inflight_key(COMPANY, side))
		redis("DEL", routing.memo_key(COMPANY, "Payable"))

	def route(self, started=None, args=ARGS):
		return routing.read_side(
			self.today,
			*args,
			started=monotonic() if started is None else started,
			outcomes=self.outcomes,
		)

	def refusal(self, **kwargs):
		"""The `too_large` refusal a routed side ends in."""
		with self.assertRaises(CalendarSourceUnavailable) as raised:
			self.route(**kwargs)
		self.assertEqual(raised.exception.code, "too_large")
		return raised.exception

	def memo_set(self):
		return bool(redis("EXISTS", routing.memo_key(COMPANY, SIDE)))

	def slots_free(self):
		return not any(
			redis("EXISTS", routing.slot_key(n)) for n in range(routing.READER_SLOTS)
		) and not redis("EXISTS", routing.inflight_key(COMPANY, SIDE))

	def memo_ttl(self):
		return redis("TTL", routing.memo_key(COMPANY, SIDE))


class TestWhenTodayServesOrRefusesOtherwise(RouterCase):
	def test_a_poisoned_view_skips_the_next_side_and_counts_a_fresh_refusal(self):
		self.reader.side_effect = frappe.QueryDeadlockError("deadlock")
		with self.assertRaises(CalendarSourceUnavailable) as view_error:
			with snapshot.consistent_read():
				first = self.refusal()
				self.reader.reset_mock()
				second = self.refusal(args=(COMPANY, "Payable", *ARGS[2:]))
		self.assertEqual(view_error.exception.code, "source_changed")
		self.reader.assert_not_called()
		self.today.assert_called_once_with(*ARGS)
		self.settings.assert_called_once()
		self.probe.assert_called_once()
		self.assertIsNot(first, second)
		self.assertIsNone(second.__context__)
		self.assertEqual(self.outcomes[-1]["step"], "view_broken")
		with patch.object(read_log, "_count") as count, patch.object(frappe, "log_error") as log_error:
			read_log.reader_outcomes([self.outcomes[-1]], "2031-04-01")
		count.assert_called_once_with(read_log.company_digest(COMPANY), "2031-04-01", "view_broken")
		log_error.assert_not_called()
		self.reader.side_effect = None
		with snapshot.consistent_read():
			self.assertIs(self.route(args=(COMPANY, "Payable", *ARGS[2:])), READER_RESULT)

	def test_a_served_side_is_todays_result_and_nothing_else_is_read(self):
		self.today.side_effect = None
		self.today.return_value = TODAY_RESULT
		self.assertIs(self.route(), TODAY_RESULT)
		self.settings.assert_not_called()
		self.reader.assert_not_called()
		self.probe.assert_not_called()
		self.assertEqual([r["path"] for r in self.outcomes], ["today"])

	def test_todays_arguments_are_passed_through(self):
		self.today.side_effect = None
		self.today.return_value = TODAY_RESULT
		self.route()
		self.today.assert_called_once_with(*ARGS)

	def test_a_refusal_other_than_too_large_is_never_routed(self):
		for code in ("forbidden", "reconciliation_mismatch", "multiple_invoice_accounts", "timeout"):
			with self.subTest(code):
				error = CalendarSourceUnavailable(code)
				self.today.side_effect = error
				with self.assertRaises(CalendarSourceUnavailable) as raised:
					self.route()
				self.assertIs(raised.exception, error)
		self.settings.assert_not_called()
		self.reader.assert_not_called()

	def test_an_error_that_is_not_a_refusal_passes_through(self):
		self.today.side_effect = RuntimeError("boom")
		with self.assertRaises(RuntimeError):
			self.route()
		self.reader.assert_not_called()


class TestRoutingRules(RouterCase):
	def test_a_served_company_with_mode_on_gets_the_readers_result(self):
		self.assertIs(self.route(), READER_RESULT)
		record = self.outcomes[0]
		self.assertEqual((record["side"], record["path"], record["outcome"]), (SIDE, "reader", "served"))
		self.assertIsNone(record.get("step"))
		self.assertTrue(self.slots_free())
		self.assertFalse(self.memo_set())

	def test_the_reader_gets_the_arguments_a_stats_dict_and_a_deadline(self):
		before = monotonic()
		self.route()
		args, kwargs = self.reader.call_args
		self.assertEqual(args, ARGS)
		self.assertIsInstance(kwargs["stats"], dict)
		self.assertAlmostEqual(kwargs["deadline"] - before, routing.READER_DEADLINE_S, delta=2)

	def test_the_deadline_is_cut_to_what_is_left_of_the_view_cap(self):
		# A late view: 80 s gone of a 100 s cap, so 20 s are left, below the reader's own 30 s.
		with patch.object(routing, "VIEW_CAP_S", 100):
			self.route(started=monotonic() - 80)
		self.assertAlmostEqual(self.reader.call_args.kwargs["deadline"] - monotonic(), 20, delta=1)

	def test_an_early_view_leaves_the_readers_own_limit(self):
		with patch.object(routing, "VIEW_CAP_S", 100):
			self.route(started=monotonic() - 10)
		self.assertAlmostEqual(
			self.reader.call_args.kwargs["deadline"] - monotonic(), routing.READER_DEADLINE_S, delta=1
		)

	def test_the_timings_and_counts_of_the_reader_reach_the_record(self):
		def read(*args, stats, **kwargs):
			stats.update(timings_ms={"control": 1.0}, counts={"lines": 3})
			return READER_RESULT

		self.reader.side_effect = read
		self.route()
		record = self.outcomes[0]
		self.assertEqual((record["timings_ms"], record["counts"]), ({"control": 1.0}, {"lines": 3}))
		self.assertIn("today_ms", record)
		self.assertIn("reader_ms", record)

	def test_mode_off_or_shadow_or_a_shadow_only_company_or_another_company_is_refused(self):
		cases = {
			"off": routing.ReaderSettings("off", (COMPANY,), (COMPANY,)),
			"shadow": routing.ReaderSettings("shadow", (COMPANY,), (COMPANY,)),
			"shadow list only": routing.ReaderSettings("on", (OTHER,), (COMPANY,)),
			"not listed": routing.ReaderSettings("on", (OTHER,), ()),
			"empty": routing.ReaderSettings("on", (), ()),
		}
		for label, settings in cases.items():
			with self.subTest(label):
				self.settings.return_value = settings
				self.refusal()
		self.reader.assert_not_called()
		self.assertEqual({r["step"] for r in self.outcomes}, {"settings"})

	def test_an_ineligible_user_is_refused_by_the_reader_and_no_memo_is_set(self):
		self.reader.side_effect = ReaderFallback("ineligible", ("user_permission",))
		self.refusal()
		self.reader.assert_called_once()
		record = self.outcomes[0]
		self.assertEqual((record["fallback"], record["fallback_detail"]), ("ineligible", ["user_permission"]))
		self.assertFalse(self.memo_set())

	def test_each_refusing_step_refuses_without_calling_the_reader(self):
		def v15():
			return patch.object(routing, "source_profile", return_value=SimpleNamespace(major="15"))

		def no_key():
			return patch.dict(frappe.conf, {"encryption_key": ""})

		def memo():
			redis("SET", routing.memo_key(COMPANY, SIDE), "1", "EX", routing.MEMO_TTL_S)
			return nullcontext()

		def probe():
			self.probe.return_value = True
			return nullcontext()

		def in_flight():
			redis("SET", routing.inflight_key(COMPANY, SIDE), "other", "EX", 60)
			return nullcontext()

		def slots_taken():
			for n in range(routing.READER_SLOTS):
				redis("SET", routing.slot_key(n), "other", "EX", 60)
			return nullcontext()

		cases = (
			("v15", v15, monotonic()),
			("no_key", no_key, monotonic()),
			("budget", nullcontext, monotonic() - 91),
			("memo", memo, monotonic()),
			("probe", probe, monotonic()),
			("busy", in_flight, monotonic()),
			("busy", slots_taken, monotonic()),
		)
		for step, setup, started in cases:
			with self.subTest(step, setup=setup.__name__):
				self.clean()
				self.settings.reset_mock()
				self.probe.return_value = False
				self.outcomes.clear()
				with setup():
					self.refusal(started=started)
				self.reader.assert_not_called()
				self.assertEqual(self.outcomes[0]["step"], step)
				# The switch is read first, whatever refuses later.
				self.settings.assert_called_once_with(lists_when_off=False)
				if setup is slots_taken:
					# The two slots belong to someone else and stay; our own in-flight marker is released.
					self.assertTrue(redis("EXISTS", routing.slot_key(0)))
					self.assertFalse(redis("EXISTS", routing.inflight_key(COMPANY, SIDE)))

	def test_mode_off_stops_before_the_v15_and_no_key_steps_and_the_probe(self):
		self.settings.return_value = routing.ReaderSettings("off", (), ())
		with patch.object(routing, "source_profile", return_value=SimpleNamespace(major="15")):
			with patch.dict(frappe.conf, {"encryption_key": ""}):
				self.refusal()
		self.assertEqual(self.outcomes[0]["step"], "settings")
		self.probe.assert_not_called()
		self.reader.assert_not_called()

	def test_the_reader_cap_is_todays_ledger_guard_so_the_shadow_can_prove_every_served_company(self):
		from jarvis.payment_calendar import shadow

		self.assertEqual(routing.READER_LEDGER_CAP, sources.MAX_LEDGER_ROWS)
		self.assertEqual(routing.READER_LEDGER_CAP, shadow.SHADOW_LEDGER_CAP)

	def test_the_probe_runs_with_the_company_side_date_and_cap(self):
		self.route()
		self.probe.assert_called_once_with(COMPANY, SIDE, "2026-11-20", routing.READER_LEDGER_CAP)

	def test_a_free_second_slot_is_used_when_the_first_is_taken(self):
		redis("SET", routing.slot_key(0), "other", "EX", 60)
		seen = []
		self.reader.side_effect = lambda *a, **k: (
			seen.append(redis("EXISTS", routing.slot_key(1))),
			READER_RESULT,
		)[1]
		self.assertIs(self.route(), READER_RESULT)
		self.assertEqual(seen, [1])
		self.assertEqual(redis("GET", routing.slot_key(0)), b"other")

	def test_the_slot_and_the_in_flight_marker_outlive_the_longest_job(self):
		seen = []
		self.reader.side_effect = lambda *a, **k: (
			seen.append(
				(redis("TTL", routing.slot_key(0)), redis("TTL", routing.inflight_key(COMPANY, SIDE)))
			),
			READER_RESULT,
		)[1]
		self.route()
		self.assertGreaterEqual(routing.SLOT_TTL_S, store.RUNNING_TTL + 60)
		for ttl in seen[0]:
			self.assertAlmostEqual(ttl, routing.SLOT_TTL_S, delta=5)


class TestProbeErrors(RouterCase):
	def test_a_failed_statement_in_the_probe_refuses_with_a_fresh_too_large(self):
		for code in (1969, 1054):
			with self.subTest(code):
				self.outcomes.clear()
				self.probe.side_effect = frappe.db.OperationalError(code, "PRIVATE text")
				error = self.refusal()
				self.assertIsNone(error.__cause__)
				self.assertIsNone(error.__context__)
				self.assertEqual(self.outcomes[0]["step"], "probe")
				self.assertEqual(self.outcomes[0]["probe_error"], "OperationalError")
				self.assertNotIn("PRIVATE", repr(self.outcomes))
				self.reader.assert_not_called()
				self.assertTrue(self.slots_free())

	def test_a_job_deadline_in_the_probe_propagates_and_records_the_class(self):
		for error in (JobTimeoutException(), TimeoutError()):
			with self.subTest(type(error).__name__):
				self.outcomes.clear()
				self.probe.side_effect = error
				with self.assertRaises(type(error)) as raised:
					self.route()
				self.assertIs(raised.exception, error)
				self.assertEqual(self.outcomes[0]["reader_exception"], type(error).__name__)
				self.assertTrue(self.slots_free())
		self.reader.assert_not_called()

	@v16_only(GATE_NO_V15_READER_PINS)
	def test_a_lost_connection_in_the_probe_is_a_refused_probe_not_a_special_case(self):
		for error in (
			frappe.db.OperationalError(2013, "lost"),
			interface_error(),
		):
			with self.subTest(type(error).__name__):
				self.outcomes.clear()
				self.probe.side_effect = error
				self.refusal()
				self.assertEqual(self.outcomes[0]["step"], "probe")
				self.assertEqual(self.outcomes[0]["probe_error"], type(error).__name__)


class TestReaderOutcomes(RouterCase):
	def test_each_fallback_reason_is_refused_and_the_memo_follows_the_reason(self):
		for reason in sorted(READER_FALLBACK_REASONS):
			with self.subTest(reason):
				self.clean()
				self.outcomes.clear()
				self.reader.side_effect = ReaderFallback(reason)
				self.refusal()
				self.assertEqual(self.outcomes[0]["fallback"], reason)
				if reason in SLOW:
					self.assertAlmostEqual(self.memo_ttl(), routing.MEMO_TTL_S, delta=5)
				elif reason in DETERMINISTIC:
					self.assertAlmostEqual(self.memo_ttl(), routing.SHORT_MEMO_TTL_S, delta=5)
				else:
					self.assertFalse(self.memo_set(), reason)
				self.assertTrue(self.slots_free())

	def test_the_cause_of_a_fallback_is_recorded_as_a_class_only(self):
		def raise_with_cause(*args, **kwargs):
			try:
				raise KeyError("PRIVATE party text")
			except KeyError as error:
				raise ReaderFallback("reader_error") from error

		self.reader.side_effect = raise_with_cause
		self.refusal()
		self.assertEqual(self.outcomes[0]["cause"], "KeyError")
		self.assertNotIn("PRIVATE", repr(self.outcomes))

	def test_a_deadline_cut_short_by_a_late_view_sets_no_memo_but_an_uncut_one_does(self):
		self.reader.side_effect = ReaderFallback("deadline")
		with patch.object(routing, "VIEW_CAP_S", 100):
			self.refusal(started=monotonic() - 80)
		self.assertFalse(self.memo_set(), "the reader was not slow, the view was late")
		self.clean()
		with patch.object(routing, "VIEW_CAP_S", 100):
			self.refusal(started=monotonic() - 10)
		self.assertAlmostEqual(self.memo_ttl(), routing.MEMO_TTL_S, delta=5)

	def test_a_probe_that_says_too_big_sets_a_short_memo(self):
		self.probe.return_value = True
		self.refusal()
		self.assertAlmostEqual(self.memo_ttl(), routing.SHORT_MEMO_TTL_S, delta=5)
		self.assertTrue(self.slots_free())

	def test_a_reader_refusal_is_refused_and_sets_no_memo(self):
		self.reader.side_effect = CalendarSourceUnavailable("multiple_invoice_accounts")
		self.refusal()
		self.assertEqual(self.outcomes[0]["reader_refused"], "multiple_invoice_accounts")
		self.assertFalse(self.memo_set())

	def test_any_other_exception_is_refused_and_sets_the_slow_memo(self):
		self.reader.side_effect = RuntimeError("PRIVATE party text")
		self.refusal()
		self.assertEqual(self.outcomes[0]["reader_exception"], "RuntimeError")
		self.assertNotIn("PRIVATE", repr(self.outcomes))
		self.assertAlmostEqual(self.memo_ttl(), routing.MEMO_TTL_S, delta=5)
		self.assertTrue(self.slots_free())

	def test_a_failed_statement_in_the_reader_is_refused_as_a_reader_error(self):
		self.reader.side_effect = frappe.db.OperationalError(1054, "Unknown column")
		self.refusal()
		record = self.outcomes[0]
		# Both are kept: the fallback class and the exception class (and the cause).
		self.assertEqual(
			(record["fallback"], record["reader_exception"], record["cause"]),
			("reader_error", "OperationalError", "OperationalError"),
		)
		self.assertAlmostEqual(self.memo_ttl(), routing.MEMO_TTL_S, delta=5)

	def test_a_failed_statement_in_one_side_leaves_the_other_side_served(self):
		self.reader.side_effect = frappe.db.OperationalError(1054, "Unknown column")
		self.refusal()
		self.today.side_effect = None
		self.today.return_value = TODAY_RESULT
		self.assertIs(self.route(args=(COMPANY, "Payable", *ARGS[2:])), TODAY_RESULT)

	def test_only_a_job_deadline_propagates_and_it_leaves_the_record_and_the_memo(self):
		for error in (JobTimeoutException(), TimeoutError()):
			with self.subTest(type(error).__name__):
				self.clean()
				self.outcomes.clear()
				self.reader.side_effect = error
				with self.assertRaises(type(error)) as raised:
					self.route()
				self.assertIs(raised.exception, error)
				self.assertEqual(self.outcomes[0]["reader_exception"], type(error).__name__)
				self.assertTrue(self.slots_free())
				self.assertAlmostEqual(self.memo_ttl(), routing.MEMO_TTL_S, delta=5)

	@v16_only(GATE_NO_V15_READER_PINS)
	def test_a_lost_connection_in_the_reader_is_refused_like_any_other_error(self):
		"""No special case: the snapshot view's exit check fails the job when the connection is really gone."""
		for error in (
			interface_error(),
			frappe.db.OperationalError(2013, "lost"),
			frappe.QueryDeadlockError("deadlock"),
		):
			with self.subTest(type(error).__name__):
				self.clean()
				self.outcomes.clear()
				self.reader.side_effect = error
				self.refusal()
				self.assertEqual(self.outcomes[0]["reader_exception"], type(error).__name__)
				self.assertTrue(self.slots_free())

	def test_after_a_job_timeout_in_the_reader_the_next_read_skips_with_the_memo(self):
		self.reader.side_effect = JobTimeoutException()
		with self.assertRaises(JobTimeoutException):
			self.route()
		self.reader.reset_mock(side_effect=True)
		self.reader.return_value = READER_RESULT
		self.outcomes.clear()
		self.refusal()
		self.reader.assert_not_called()
		self.assertEqual(self.outcomes[0]["step"], "memo")

	def test_a_served_read_sets_no_memo_and_a_failed_one_does_not_block_the_next_after_it_expires(self):
		self.assertIs(self.route(), READER_RESULT)
		self.assertFalse(self.memo_set())
		self.reader.side_effect = [ReaderFallback("deadline"), READER_RESULT]
		self.refusal()
		self.assertTrue(self.memo_set())
		redis("DEL", routing.memo_key(COMPANY, SIDE))
		self.assertIs(self.route(), READER_RESULT)
		self.assertFalse(self.memo_set())

	def test_the_pessimistic_memo_is_set_before_the_reader_and_owned_by_the_in_flight_token(self):
		seen = []
		memo, marker = routing.memo_key(COMPANY, SIDE), routing.inflight_key(COMPANY, SIDE)
		self.reader.side_effect = lambda *a, **k: (
			seen.append((redis("GET", memo), redis("GET", marker), redis("TTL", memo))),
			READER_RESULT,
		)[1]
		self.route()
		value, token, ttl = seen[0]
		self.assertIsNotNone(value)
		self.assertEqual(value, token, "the memo carries the token of the job that owns the marker")
		self.assertAlmostEqual(ttl, routing.MEMO_TTL_S, delta=5)
		self.assertFalse(self.memo_set(), "a safe normal outcome deletes it")

	def test_a_memo_that_matches_the_live_marker_is_busy_and_one_without_a_marker_is_a_bad_read(self):
		memo, marker = routing.memo_key(COMPANY, SIDE), routing.inflight_key(COMPANY, SIDE)
		redis("SET", memo, "tok", "EX", 60)
		redis("SET", marker, "tok", "EX", 60)
		self.refusal()
		self.assertEqual(self.outcomes[0]["step"], "busy")
		self.assertTrue(self.memo_set(), "a busy answer leaves the running job's memo alone")
		redis("DEL", marker)
		self.outcomes.clear()
		self.refusal()
		self.assertEqual(self.outcomes[0]["step"], "memo")
		self.reader.assert_not_called()

	def test_a_read_killed_without_a_trace_leaves_the_memo_for_the_full_time(self):
		"""OOM or SIGKILL bypass every `except`; a BaseException stands in: nothing deletes the memo."""
		self.reader.side_effect = KeyboardInterrupt()
		with self.assertRaises(KeyboardInterrupt):
			self.route()
		self.assertTrue(self.memo_set())
		self.assertAlmostEqual(self.memo_ttl(), routing.MEMO_TTL_S, delta=5)
		self.assertTrue(self.slots_free(), "the marker is gone, as when it has expired")
		self.reader.reset_mock(side_effect=True)
		self.reader.return_value = READER_RESULT
		self.outcomes.clear()
		self.refusal()
		self.assertEqual(self.outcomes[0]["step"], "memo")
		self.reader.assert_not_called()

	def test_a_job_deletes_the_memo_only_while_it_is_still_its_own(self):
		memo = routing.memo_key(COMPANY, SIDE)

		def replace_then_serve(*args, **kwargs):
			redis("SET", memo, "1", "EX", 600)  # another job turned it into a bad-read memo
			return READER_RESULT

		self.reader.side_effect = replace_then_serve
		self.assertIs(self.route(), READER_RESULT)
		self.assertEqual(redis("GET", memo), b"1")

	def test_the_memo_is_checked_again_after_the_marker_is_claimed(self):
		"""Job A failed and released its marker between B's first memo check and B's claim."""
		claim = routing._claim
		calls = []

		def claim_after_a_failure(key, token):
			result = claim(key, token)
			if not calls:
				calls.append(True)
				redis("SET", routing.memo_key(COMPANY, SIDE), "1", "EX", routing.MEMO_TTL_S)
			return result

		with patch.object(routing, "_claim", claim_after_a_failure):
			self.refusal()
		self.assertEqual(self.outcomes[0]["step"], "memo")
		self.reader.assert_not_called()
		self.probe.assert_not_called()
		self.assertTrue(self.slots_free())

	def test_a_timeout_in_the_digest_is_not_swallowed(self):
		with patch.object(read_log, "company_digest", side_effect=JobTimeoutException()):
			with self.assertRaises(JobTimeoutException):
				routing._digest(COMPANY)
		with patch.object(read_log, "company_digest", side_effect=RuntimeError("no key")):
			self.assertIsNone(routing._digest(COMPANY))

	def test_a_second_read_while_one_runs_is_busy_and_remembers_nothing(self):
		inner = []

		def reader(*args, **kwargs):
			try:
				routing.read_side(self.today, *ARGS, started=monotonic(), outcomes=inner)
			except CalendarSourceUnavailable:
				pass
			return READER_RESULT

		self.reader.side_effect = reader
		self.assertIs(self.route(), READER_RESULT)
		self.assertEqual(inner[0]["step"], "busy")
		self.assertEqual(self.reader.call_count, 1)
		self.assertFalse(self.memo_set(), "a busy answer is not remembered")
		self.assertTrue(self.slots_free())

	def test_the_memo_is_per_company_and_side(self):
		self.reader.side_effect = ReaderFallback("deadline")
		self.refusal()
		self.reader.reset_mock(side_effect=True)
		self.reader.return_value = READER_RESULT
		self.assertIs(self.route(args=(COMPANY, "Payable", *ARGS[2:])), READER_RESULT)
		redis("DEL", routing.memo_key(COMPANY, "Payable"))


class TestRefusalsAreFresh(RouterCase):
	def assert_fresh(self, error):
		self.assertEqual(error.code, "too_large")
		self.assertIsNone(error.__cause__)
		self.assertIsNone(error.__context__)
		self.assertTrue(error.__suppress_context__)

	def test_a_refusal_has_no_cause_and_no_context_whatever_refused(self):
		reasons = {
			"no reader": lambda: setattr(
				self.settings, "return_value", routing.ReaderSettings("off", (), ())
			),
			"fallback": lambda: setattr(self.reader, "side_effect", ReaderFallback("deadline")),
			"reader refusal": lambda: setattr(
				self.reader, "side_effect", CalendarSourceUnavailable("multiple_invoice_accounts")
			),
			"exception": lambda: setattr(self.reader, "side_effect", RuntimeError("boom")),
			"statement error": lambda: setattr(
				self.reader, "side_effect", frappe.db.OperationalError(1054, "column")
			),
		}
		for label, setup in reasons.items():
			with self.subTest(label):
				self.clean()
				self.reader.reset_mock(side_effect=True)
				self.reader.return_value = READER_RESULT
				self.settings.return_value = routing.ReaderSettings("on", (COMPANY,), ())
				setup()
				self.assert_fresh(self.refusal())

	def test_no_frame_of_todays_engine_or_the_readers_is_reachable_from_a_refusal(self):
		class Engine:
			pass

		alive = []

		def today(*args):
			engine = Engine()
			alive.append(weakref.ref(engine))
			raise CalendarSourceUnavailable("too_large")

		def reader(*args, **kwargs):
			engine = Engine()
			alive.append(weakref.ref(engine))
			raise ReaderFallback("deadline")

		self.today.side_effect = today
		self.reader.side_effect = reader
		error = self.refusal()
		gc.collect()
		self.assertEqual(len(alive), 2)
		self.assertEqual([ref() for ref in alive], [None, None])
		self.assertIsNotNone(error)


class TestSlotRelease(RouterCase):
	def test_a_slot_whose_token_was_replaced_is_not_deleted(self):
		def replace(*args, **kwargs):
			for n in range(routing.READER_SLOTS):
				if redis("EXISTS", routing.slot_key(n)):
					redis("SET", routing.slot_key(n), "another-token", "EX", 60)
			return READER_RESULT

		self.reader.side_effect = replace
		self.route()
		self.assertEqual(redis("GET", routing.slot_key(0)), b"another-token")

	def test_the_slot_is_released_after_a_served_read_a_refusal_and_a_raise(self):
		for effect in (None, ReaderFallback("pin_drift"), RuntimeError("boom"), JobTimeoutException()):
			with self.subTest(repr(effect)):
				self.clean()
				self.reader.reset_mock(side_effect=True)
				self.reader.return_value = READER_RESULT
				if effect is not None:
					self.reader.side_effect = effect
				try:
					self.route()
				except (CalendarSourceUnavailable, JobTimeoutException):
					pass
				self.assertTrue(self.slots_free())

	def test_the_keys_live_under_the_persistent_prefix(self):
		self.assertEqual(routing.slot_key(0), frappe.cache.make_key("jarvis:pcal:reader:slot:0"))
		memo = routing.memo_key(COMPANY, SIDE).decode()
		self.assertIn("jarvis:pcal:reader:memo:", memo)
		self.assertNotIn(COMPANY, memo)
		with patch.dict(frappe.conf, {"encryption_key": KEY}):
			self.assertIn(read_log.company_digest(COMPANY), memo)

	def test_a_job_timeout_in_the_release_or_in_remembering_propagates(self):
		real_command = frappe.cache.execute_command

		def kill_release(*args, **kwargs):
			raise JobTimeoutException()

		def kill_memo(*args, **kwargs):
			# The memo is the only plain SET (the slot and in-flight claims use NX).
			if args[0] == "SET" and "NX" not in args:
				raise JobTimeoutException()
			return real_command(*args, **kwargs)

		self.clean()
		with patch.object(frappe.cache, "eval", kill_release), self.assertRaises(JobTimeoutException):
			self.route()
		self.clean()
		self.reader.side_effect = ReaderFallback("deadline")
		with patch.object(frappe.cache, "execute_command", kill_memo), self.assertRaises(JobTimeoutException):
			self.route()

	def test_a_cache_failure_before_the_reader_refuses_and_leaves_no_slot(self):
		with patch.object(routing, "_take_slot", side_effect=RuntimeError("redis down")):
			self.refusal()
		self.reader.assert_not_called()
		self.assertEqual(self.outcomes[0]["step"], "cache")


class TestRealSettings(TestCase):
	"""`reader_settings` is not patched: the three values live in this test's transaction only."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		previous = frappe.conf.get("encryption_key")

		def restore_key():
			if previous is None:
				frappe.conf.pop("encryption_key", None)
			else:
				frappe.conf["encryption_key"] = previous

		cls.addClassCleanup(restore_key)
		frappe.set_user("Administrator")
		frappe.reload_doc("jarvis", "doctype", "jarvis_settings")
		frappe.clear_cache(doctype=routing.SETTINGS_DOCTYPE)

	def setUp(self):
		# Frappe replaces local.conf at class cleanup, so set the key on the current config.
		frappe.conf["encryption_key"] = KEY

	def tearDown(self):
		frappe.db.rollback()
		for n in range(routing.READER_SLOTS):
			redis("DEL", routing.slot_key(n))
		redis("DEL", routing.memo_key(COMPANY, SIDE))

	def raw(self, field, value):
		table = "`tabSingles`"
		frappe.db.sql(f"DELETE FROM {table} WHERE doctype=%s AND field=%s", (routing.SETTINGS_DOCTYPE, field))
		frappe.db.sql(
			f"INSERT INTO {table} (doctype, field, value) VALUES (%s, %s, %s)",
			(routing.SETTINGS_DOCTYPE, field, value),
		)

	def route(self, reader):
		with (
			patch.dict(frappe.conf, {"encryption_key": KEY}),
			patch.object(routing.builtin_reader, "read_obligations", reader),
			patch.object(sources, "_ledger_rows_exceed", return_value=False),
		):
			return routing.read_side(too_large, *ARGS, started=monotonic(), outcomes=[])

	def test_on_with_the_company_listed_routes_and_off_does_not(self):
		reader = MagicMock(return_value=READER_RESULT)
		self.raw(routing.MODE_FIELD, "on")
		self.raw(routing.SERVED_FIELD, f"{OTHER}\n{COMPANY}")
		self.assertIs(self.route(reader), READER_RESULT)
		self.raw(routing.MODE_FIELD, "off")
		with self.assertRaises(CalendarSourceUnavailable):
			self.route(reader)
		self.assertEqual(reader.call_count, 1)


class TestMayHaveRolledBack(TestCase):
	"""Which driver errors may have ended the snapshot transaction (the router then breaks the view)."""

	def test_the_classification(self):
		rows = [
			("deadlock", frappe.QueryDeadlockError("x"), True),
			("lock wait", frappe.QueryTimeoutError("x"), True),
			("interface error", interface_error("x"), True),
		] + [
			(f"operational {code}", frappe.db.OperationalError(code, "x"), True)
			for code in (1213, 1205, 2006, 2013, 2055, 1927, 4031)
		]
		rows += [
			("unknown column", frappe.db.OperationalError(1054, "x"), False),
			("statement timeout", frappe.db.OperationalError(1969, "x"), False),
			("operational without a code", frappe.db.OperationalError(), False),
			("programming error", frappe.db.ProgrammingError(1146, "x"), False),
			("not a database error", RuntimeError(1213), False),
			("a job deadline", TimeoutError(), False),
			("nothing", None, False),
		]
		for label, error, expected in rows:
			with self.subTest(label):
				self.assertIs(routing._may_have_rolled_back(error), expected)

	def test_internal_error_with_a_rollback_code_counts(self):
		self.assertTrue(routing._may_have_rolled_back(frappe.db.InternalError(1213, "x")))

	def test_a_driver_namespace_without_an_interface_error_attribute_is_still_classified(self):
		"""A driver with no `InterfaceError` attribute: the interface check is its own `is_interface_error`."""
		operational = frappe.db.OperationalError(1213, "x")
		statement = frappe.db.OperationalError(1054, "x")
		driver = SimpleNamespace(
			OperationalError=frappe.db.OperationalError,
			InternalError=frappe.db.InternalError,
			is_interface_error=lambda error: False,
		)
		with patch.object(frappe, "db", driver):
			self.assertTrue(routing._may_have_rolled_back(operational))
			self.assertFalse(routing._may_have_rolled_back(statement))
			self.assertTrue(routing._may_have_rolled_back(frappe.QueryDeadlockError("x")))
