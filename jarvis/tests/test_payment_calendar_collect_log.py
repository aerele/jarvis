"""The collect log line: today's text with the switch off, plus the bank path, time and fallback cause when the bank is routed."""

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from jarvis.payment_calendar import banks, snapshot
from jarvis.payment_calendar import sources as obligation_sources
from jarvis.payment_calendar.access import authorize_scope
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site

SOURCES = {"Payable": {"rows": [1, 2]}, "Receivable": None}
TODAY_LINE = "collect complete duration_ms=7 projections_ms=3 payable_rows=2 receivable_rows=0 fast_path=True"


def clear_bank_route():
	"""The request leaves no bank path and no fallback cause."""
	for key in (banks.BANK_PATH_KEY, banks.BANK_FALLBACK_KEY):
		if hasattr(frappe.local, key):
			delattr(frappe.local, key)


def logged(*args, **kwargs):
	logger = MagicMock()
	with (
		patch.object(obligation_sources, "FAST_PATH", True),
		patch.object(snapshot, "elapsed_ms", return_value=7),
		patch.object(frappe, "logger", return_value=logger),
	):
		snapshot._log_collected(0.0, 3, SOURCES, *args, **kwargs)
	return logger.info.call_args.args[0]


class TestLogCollectedLine(TestCase):
	def test_without_bank_fields_the_line_is_byte_identical_to_today(self):
		self.assertEqual(logged(), TODAY_LINE)
		self.assertEqual(logged(None), TODAY_LINE)

	def test_bank_fields_follow_the_speed_path_flag(self):
		self.assertEqual(
			logged({"path": "one_pass", "ms": 12}), f"{TODAY_LINE} bank_path=one_pass bank_ms=12"
		)
		self.assertEqual(logged({"path": "today", "ms": 5}), f"{TODAY_LINE} bank_path=today bank_ms=5")

	def test_a_fallback_cause_follows_the_time(self):
		bank = {"path": "today", "ms": 5, "fallback": "unpinned"}
		self.assertEqual(logged(bank), f"{TODAY_LINE} bank_path=today bank_ms=5 bank_fallback=unpinned")


class TestBankLog(TestCase):
	"""`_bank_log` turns what the bank read left in the request into the two log fields, or into nothing."""

	def setUp(self):
		clear_bank_route()
		self.addCleanup(clear_bank_route)

	def test_a_read_that_left_no_path_logs_nothing(self):
		self.assertIsNone(snapshot._bank_log(0.0))

	def test_a_known_path_logs_its_name_and_the_elapsed_ms(self):
		for path in ("one_pass", "today"):
			setattr(frappe.local, banks.BANK_PATH_KEY, path)
			with patch.object(snapshot, "elapsed_ms", return_value=9):
				self.assertEqual(snapshot._bank_log(0.0), {"path": path, "ms": 9})

	def test_any_other_value_is_not_logged(self):
		setattr(frappe.local, banks.BANK_PATH_KEY, "Some Company Ltd")
		self.assertIsNone(snapshot._bank_log(0.0))

	def test_the_cause_of_a_today_path_is_logged_when_it_is_a_known_one(self):
		setattr(frappe.local, banks.BANK_PATH_KEY, "today")
		for cause in banks.FALLBACK_CAUSES:
			setattr(frappe.local, banks.BANK_FALLBACK_KEY, cause)
			with patch.object(snapshot, "elapsed_ms", return_value=9):
				self.assertEqual(snapshot._bank_log(0.0), {"path": "today", "ms": 9, "fallback": cause})

	def test_an_unknown_cause_or_a_cause_on_the_one_pass_is_not_logged(self):
		for path, cause in (("today", "Some Company Ltd"), ("one_pass", "unpinned")):
			setattr(frappe.local, banks.BANK_PATH_KEY, path)
			setattr(frappe.local, banks.BANK_FALLBACK_KEY, cause)
			with patch.object(snapshot, "elapsed_ms", return_value=9):
				self.assertEqual(snapshot._bank_log(0.0), {"path": path, "ms": 9})


class TestBankFallbackCounter(TestCase):
	"""A served company that read today's path is counted per cause in a site day counter: the production signal."""

	def count(self, bank):
		with (
			patch.object(snapshot.read_log, "site_count") as counter,
			patch.object(snapshot.routing, "_today", return_value="2026-01-02"),
		):
			snapshot._count_bank_fallback(bank)
		return counter

	def test_a_cause_is_counted_by_name_and_day(self):
		self.count({"path": "today", "ms": 1, "fallback": "missing_index"}).assert_called_once_with(
			"bank_today:missing_index", "2026-01-02"
		)

	def test_nothing_is_counted_without_a_cause(self):
		for bank in (None, {"path": "one_pass", "ms": 1}, {"path": "today", "ms": 1}):
			self.count(bank).assert_not_called()

	def test_a_failing_counter_never_fails_the_collect(self):
		bank = {"path": "today", "ms": 1, "fallback": "v15"}
		with patch.object(snapshot.read_log, "site_count", side_effect=RuntimeError("redis down")):
			snapshot._count_bank_fallback(bank)

	def test_a_job_timeout_still_propagates(self):
		bank = {"path": "today", "ms": 1, "fallback": "v15"}
		with (
			patch.object(snapshot.read_log, "site_count", side_effect=TimeoutError),
			self.assertRaises(TimeoutError),
		):
			snapshot._count_bank_fallback(bank)


class TestCollectWritesBankFields(TestCase):
	"""The real collect on the test site: the line a view writes with the bank read stubbed or left alone."""

	def setUp(self):
		require_calendar_test_site()
		clear_bank_route()
		frappe.set_user("Administrator")
		self.fx = CalendarFixtures()
		self.fx.invoice((100,))
		self.fx.invoice((100,), side="Receivable")
		self.fx.fiscal_year()
		self.fx.gl(1000)
		frappe.db.commit()
		self.addCleanup(self.cleanup)
		self.scope = normalize_scope(self.fx.company, [self.fx.bank])

	def cleanup(self):
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
		clear_bank_route()

	def collect_message(self):
		fingerprint = authorize_scope(self.scope)["fingerprint"]
		logger = MagicMock()
		with patch.object(frappe, "logger", return_value=logger):
			snapshot.collect(self.scope, fingerprint)
		return logger.info.call_args.args[0]

	def test_with_the_switch_off_the_line_has_no_bank_field(self):
		message = self.collect_message()
		self.assertRegex(message, r"^collect complete duration_ms=\d+ projections_ms=\d+ payable_rows=\d+ ")
		self.assertRegex(message, r" receivable_rows=\d+ fast_path=(True|False)$")

	def test_a_path_left_by_an_earlier_read_is_not_logged_when_this_read_leaves_none(self):
		setattr(frappe.local, banks.BANK_PATH_KEY, "one_pass")
		self.assertNotIn("bank_path", self.collect_message())

	def test_the_path_the_bank_read_leaves_is_logged_with_a_time_and_no_name(self):
		real = snapshot.read_bank_balances

		def read(*args):
			result = real(*args)
			setattr(frappe.local, banks.BANK_PATH_KEY, "one_pass")
			return result

		with patch.object(snapshot, "read_bank_balances", side_effect=read):
			message = self.collect_message()
		self.assertRegex(message, r" fast_path=(True|False) bank_path=one_pass bank_ms=\d+$")
		for private in (self.fx.company, self.fx.bank, self.scope["actor"]):
			self.assertNotIn(str(private), message)

	def test_the_time_is_the_bank_read_alone_not_the_work_before_it(self):
		clock = {"now": 1000.0}
		real_authorize = snapshot.authorize_scope

		def authorize(*args, **kwargs):
			clock["now"] += 1  # a second of work before the bank read
			return real_authorize(*args, **kwargs)

		def read(*args):
			clock["now"] += 0.25
			setattr(frappe.local, banks.BANK_PATH_KEY, "one_pass")
			return {"total": "0.00", "accounts": [], "currency": "INR", "precision": 2}

		with (
			patch.object(snapshot, "monotonic", lambda: clock["now"]),
			patch.object(snapshot, "authorize_scope", authorize),
			patch.object(snapshot, "read_bank_balances", side_effect=read),
			patch.object(snapshot, "check_source_contract"),
		):
			message = self.collect_message()
		self.assertIn(" bank_path=one_pass bank_ms=250", message)

	def test_a_served_company_on_todays_path_logs_the_cause_and_counts_it(self):
		def read(*args):
			setattr(frappe.local, banks.BANK_PATH_KEY, "today")
			setattr(frappe.local, banks.BANK_FALLBACK_KEY, "unpinned")
			return {"total": "0.00", "accounts": [], "currency": "INR", "precision": 2}

		with (
			patch.object(snapshot, "read_bank_balances", side_effect=read),
			patch.object(snapshot, "check_source_contract"),
			patch.object(snapshot.read_log, "site_count") as counter,
		):
			message = self.collect_message()
		self.assertRegex(message, r" bank_path=today bank_ms=\d+ bank_fallback=unpinned$")
		counter.assert_called_once_with("bank_today:unpinned", snapshot.routing._today())

	def test_a_refused_bank_read_still_logs_the_path_it_took(self):
		def refuse(*args):
			setattr(frappe.local, banks.BANK_PATH_KEY, "today")
			raise CalendarSourceUnavailable("multibook_unsupported")

		with patch.object(snapshot, "read_bank_balances", side_effect=refuse):
			message = self.collect_message()
		self.assertRegex(message, r" bank_path=today bank_ms=\d+$")

	def test_a_scope_without_accounts_logs_no_bank_field(self):
		setattr(frappe.local, banks.BANK_PATH_KEY, "one_pass")
		self.scope = normalize_scope(self.fx.company, [])
		self.assertNotIn("bank_path", self.collect_message())
