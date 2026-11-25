"""The view-scoped Payment Ledger book memo (`books.book_memo`).

Inside a snapshot view the bank check and both side checks read one grouped Payment Ledger query, not
three scans. The memo applies only with the switch `on` and the company served (v16); every other case
runs today's queries. Direct tests use savepoint fixtures; the view tests use committed fixtures
(`consistent_read` rolls back first).

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_books
"""

from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar import books, compatibility, routing, snapshot
from jarvis.payment_calendar.access import authorize_scope
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.tests.payment_calendar_fixtures import (
	CalendarFixtures,
	require_calendar_test_site,
	sync_outstanding,
)

OTHER_BOOK = "Other Finance Book"


@contextmanager
def count_ple_book_queries():
	"""Every statement that reads finance books from the Payment Ledger (today's or the memo's)."""
	real, seen = frappe.db.sql, []

	def spy(query, *args, **kwargs):
		text = str(query)
		if "tabPayment Ledger Entry" in text and "finance_book" in text and "outstanding" not in text:
			seen.append(text)
		return real(query, *args, **kwargs)

	with patch.object(frappe.db, "sql", spy):
		yield seen


def settings_for(company, mode="on", served=True):
	return MagicMock(return_value=routing.ReaderSettings(mode, (company,) if served else (), ()))


class BookFixtures:
	"""Alternative-book rows per side, so a refusal names which check saw which rows."""

	def ple_book(self, side, book, suffix, offset=0):
		account = self.fx.payable if side == "Payable" else self.fx.receivable
		return self.fx.ple(
			suffix,
			"Journal Entry",
			self.fx.prefix + suffix,
			10,
			side,
			account=account,
			finance_book=book,
			posting_offset=offset,
		)

	def checks_outcome(self, settings):
		"""`run_checks` in a view under `settings`: the three refusal codes."""
		with patch.object(routing, "reader_settings", settings), books.book_memo():
			return self.run_checks()

	def run_checks(self, as_of=None):
		"""The three checks of one bank + two sides view; the refusal codes in order (None = passed)."""
		as_of = as_of or self.fx.today
		checks = (
			lambda: books.validate_finance_books(self.fx.company, [self.fx.bank], as_of),
			lambda: books.validate_obligation_books(self.fx.company, "Payable", as_of),
			lambda: books.validate_obligation_books(self.fx.company, "Receivable", as_of),
		)
		codes = []
		for check in checks:
			try:
				check()
				codes.append(None)
			except CalendarSourceUnavailable as exc:
				codes.append(exc.code)
		return codes


class TestBookMemoDirect(BookFixtures, TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("pcal_books")
		self.fx = CalendarFixtures()
		self.settings = settings_for(self.fx.company)
		patcher = patch.object(routing, "reader_settings", self.settings)
		patcher.start()
		self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="pcal_books")

	def test_one_ple_query_serves_the_bank_and_both_sides(self):
		with books.book_memo(), count_ple_book_queries() as seen:
			self.assertEqual(self.run_checks(), [None, None, None])
		self.assertEqual(len(seen), 1)

	def test_the_bank_payment_ledger_check_alone_shares_the_memo_with_the_sides(self):
		"""The one-pass bank read has no GL book gate; it calls this check on its own."""
		self.ple_book("Receivable", OTHER_BOOK, "Alt")
		with books.book_memo(), count_ple_book_queries() as seen:
			with self.assertRaises(CalendarSourceUnavailable) as refused:
				books.validate_bank_ple_books(self.fx.company, self.fx.today)
			books.validate_obligation_books(self.fx.company, "Payable", self.fx.today)
		self.assertEqual(refused.exception.code, "multibook_unsupported")
		self.assertEqual(len(seen), 1)

	def test_without_the_memo_each_check_scans_the_ledger(self):
		with count_ple_book_queries() as seen:
			self.assertEqual(self.run_checks(), [None, None, None])
		self.assertEqual(len(seen), 3)

	def test_the_memo_is_gone_after_the_view_and_after_an_exception(self):
		with books.book_memo():
			self.run_checks()
		with count_ple_book_queries() as seen:
			self.run_checks()
		self.assertEqual(len(seen), 3)
		with self.assertRaises(ValueError), books.book_memo():
			self.run_checks()
			raise ValueError
		with count_ple_book_queries() as seen:
			self.run_checks()
		self.assertEqual(len(seen), 3)

	def test_a_memo_does_not_survive_into_the_next_memo(self):
		with books.book_memo():
			self.run_checks()
		self.ple_book("Receivable", OTHER_BOOK, "Late")
		with books.book_memo():
			self.assertEqual(self.run_checks(), ["multibook_unsupported", None, "multibook_unsupported"])

	def test_the_memo_is_keyed_by_the_accounting_date(self):
		self.ple_book("Receivable", OTHER_BOOK, "Future", offset=5)
		with books.book_memo(), count_ple_book_queries() as seen:
			self.assertEqual(self.run_checks(add_days(self.fx.today, 4)), [None, None, None])
			self.assertEqual(len(seen), 1)
			self.assertEqual(
				self.run_checks(add_days(self.fx.today, 5)),
				["multibook_unsupported", None, "multibook_unsupported"],
			)
			self.assertEqual(len(seen), 2)
			self.run_checks(add_days(self.fx.today, 4))
			self.assertEqual(len(seen), 2)

	def test_the_memo_is_keyed_by_the_company(self):
		other = CalendarFixtures()
		self.settings.return_value = routing.ReaderSettings("on", (self.fx.company, other.company), ())
		other.ple(
			"Alt",
			"Journal Entry",
			other.prefix + "Alt",
			10,
			"Receivable",
			account=other.receivable,
			finance_book=OTHER_BOOK,
		)
		with books.book_memo():
			self.assertEqual(self.run_checks(), [None, None, None])
			with self.assertRaises(CalendarSourceUnavailable):
				books.validate_obligation_books(other.company, "Receivable", self.fx.today)
			books.validate_obligation_books(other.company, "Payable", self.fx.today)

	def test_a_side_check_sees_only_its_own_account_type_and_the_bank_sees_all(self):
		self.ple_book("Receivable", OTHER_BOOK, "Alt")
		with books.book_memo():
			self.assertEqual(self.run_checks(), ["multibook_unsupported", None, "multibook_unsupported"])

	def test_every_mix_of_books_gives_todays_answer(self):
		default = self.fx.insert("Finance Book", "Book", finance_book_name="Calendar default")
		frappe.db.set_value("Company", self.fx.company, "default_finance_book", default)
		frappe.clear_document_cache("Company", self.fx.company)
		for side, book in (("Payable", None), ("Receivable", ""), ("Payable", default)):
			self.ple_book(side, book, f"Ok{side}{book}")
		with books.book_memo():
			self.assertEqual(self.run_checks(), [None, None, None])
		self.ple_book("Payable", OTHER_BOOK, "Alt")
		today_codes = self.run_checks()
		with books.book_memo():
			self.assertEqual(self.run_checks(), today_codes)
		self.assertEqual(today_codes, ["multibook_unsupported", "multibook_unsupported", None])

	def test_an_account_type_in_another_case_is_refused_as_today(self):
		"""The database compares `account_type` without case and trailing spaces; the memo must too."""
		for index, stored in enumerate(("receivable", "RECEIVABLE", "Receivable ")):
			with self.subTest(stored=stored):
				row = self.ple_book("Receivable", OTHER_BOOK, f"Case{index}")
				frappe.db.set_value("Payment Ledger Entry", row, "account_type", stored)
				today = self.checks_outcome(settings_for(self.fx.company, "off"))
				memo = self.checks_outcome(settings_for(self.fx.company))
				self.assertEqual(memo, today)
				self.assertEqual(memo[2], "multibook_unsupported")
				frappe.db.set_value("Payment Ledger Entry", row, "delinked", 1)

	def test_the_direct_groups_never_come_from_the_memo(self):
		self.ple_book("Payable", None, "Seen")
		with books.book_memo() as _, patch.object(routing, "reader_settings", self.settings):
			books.validate_bank_ple_books(self.fx.company, self.fx.today)  # fills the memo
			self.ple_book("Payable", OTHER_BOOK, "Late")
			memo = books._active_memo().groups(self.fx.company, self.fx.today)
			direct = books.ple_book_groups_direct(self.fx.company, self.fx.today)
		self.assertNotIn(("Payable", OTHER_BOOK), memo)
		self.assertIn(("Payable", OTHER_BOOK), direct)

	def test_a_delinked_row_in_another_book_is_ignored_as_today(self):
		name = self.ple_book("Receivable", OTHER_BOOK, "Delinked")
		frappe.db.set_value("Payment Ledger Entry", name, "delinked", 1)
		with books.book_memo():
			self.assertEqual(self.run_checks(), [None, None, None])

	def test_the_gl_check_of_the_bank_is_not_memoised(self):
		self.fx.gl(10, finance_book=OTHER_BOOK)
		with books.book_memo():
			self.assertEqual(self.run_checks(), ["multibook_unsupported", None, None])

	def test_a_failed_query_is_not_remembered(self):
		self.ple_book("Payable", OTHER_BOOK, "Alt")
		real = frappe.db.sql
		calls = []

		def flaky(query, *args, **kwargs):
			if "tabPayment Ledger Entry" in str(query) and not calls:
				calls.append(1)
				raise frappe.QueryTimeoutError
			return real(query, *args, **kwargs)

		with books.book_memo(), patch.object(frappe.db, "sql", flaky):
			with self.assertRaises(frappe.QueryTimeoutError):
				books.validate_obligation_books(self.fx.company, "Payable", self.fx.today)
			with self.assertRaises(CalendarSourceUnavailable):
				books.validate_obligation_books(self.fx.company, "Payable", self.fx.today)


class TestBookMemoSwitch(BookFixtures, TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("pcal_books")
		self.fx = CalendarFixtures()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="pcal_books")

	def queries_with(self, settings):
		with (
			patch.object(routing, "reader_settings", settings),
			books.book_memo(),
			count_ple_book_queries() as seen,
		):
			self.assertEqual(self.run_checks(), [None, None, None])
		return len(seen)

	def test_off_shadow_and_an_unlisted_company_run_todays_queries(self):
		for mode, served in (("off", True), ("shadow", True), ("on", False)):
			with self.subTest(mode=mode, served=served):
				self.assertEqual(self.queries_with(settings_for(self.fx.company, mode, served)), 3)

	def test_on_and_listed_uses_the_memo(self):
		self.assertEqual(self.queries_with(settings_for(self.fx.company)), 1)

	def test_the_switch_is_read_once_per_view(self):
		settings = settings_for(self.fx.company)
		self.queries_with(settings)
		settings.assert_called_once_with(lists_when_off=False)

	def test_view_settings_are_read_once_per_view_and_shared_with_the_memo(self):
		"""The bank one-pass switch and the memo take the same read."""
		settings = settings_for(self.fx.company)
		with patch.object(routing, "reader_settings", settings), books.book_memo():
			first, second = books.view_settings(), books.view_settings()
			self.run_checks()
		self.assertIs(first, second)
		self.assertEqual(first.mode, "on")
		settings.assert_called_once_with(lists_when_off=False)

	def test_view_settings_outside_a_view_are_read_fresh(self):
		settings = settings_for(self.fx.company)
		with patch.object(routing, "reader_settings", settings):
			books.view_settings()
			books.view_settings()
		self.assertEqual(settings.call_count, 2)

	def test_no_settings_are_read_outside_a_view(self):
		settings = settings_for(self.fx.company)
		with patch.object(routing, "reader_settings", settings):
			self.run_checks()
		settings.assert_not_called()

	def test_a_v15_source_does_not_read_the_switch_for_the_memo(self):
		settings = settings_for(self.fx.company)
		with (
			patch.object(books, "source_profile", return_value=compatibility._PROFILES["15"]),
			patch.object(routing, "reader_settings", settings),
			books.book_memo(),
		):
			self.run_checks()
		settings.assert_not_called()

	def test_a_v15_source_runs_todays_queries(self):
		with patch.object(books, "source_profile", return_value=compatibility._PROFILES["15"]):
			self.assertEqual(self.queries_with(settings_for(self.fx.company)), 3)

	def test_an_unknown_source_runs_todays_queries(self):
		with patch.object(books, "source_profile", side_effect=CalendarSourceUnavailable()):
			self.assertEqual(self.queries_with(settings_for(self.fx.company)), 3)


class ViewCase(TestCase):
	"""Committed fixtures: `consistent_read` rolls back first. No tests of its own."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		self.fx = CalendarFixtures()
		frappe.db.commit()
		self.settings = settings_for(self.fx.company)
		patcher = patch.object(routing, "reader_settings", self.settings)
		patcher.start()
		self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.db.rollback()
		for doctype, name in reversed(self.fx.created):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		frappe.clear_document_cache("Company", self.fx.company)


class TestBookMemoInTheView(ViewCase):
	"""`consistent_read` opens the memo and removes it on exit, also after an error."""

	def checks(self):
		books.validate_finance_books(self.fx.company, [self.fx.bank], self.fx.today)
		books.validate_obligation_books(self.fx.company, "Payable", self.fx.today)
		books.validate_obligation_books(self.fx.company, "Receivable", self.fx.today)

	def test_the_view_opens_the_memo(self):
		with snapshot.consistent_read(), count_ple_book_queries() as seen:
			self.checks()
		self.assertEqual(len(seen), 1)

	def test_the_memo_is_gone_after_the_view(self):
		with snapshot.consistent_read():
			self.checks()
		with count_ple_book_queries() as seen:
			self.checks()
		self.assertEqual(len(seen), 3)

	def test_the_memo_is_gone_after_an_error_in_the_view(self):
		with self.assertRaises(ValueError), snapshot.consistent_read():
			self.checks()
			raise ValueError
		with count_ple_book_queries() as seen:
			self.checks()
		self.assertEqual(len(seen), 3)


class TestBookMemoInCollect(ViewCase):
	"""A whole `snapshot.collect` (bank + both sides) reads the Payment Ledger books once, only when `on`."""

	def setUp(self):
		super().setUp()
		self.fx.invoice((100, 50), side="Payable", paid=30)
		self.fx.invoice((200,), side="Receivable", paid=20)
		self.fx.fiscal_year()
		self.fx.gl(1000)
		sync_outstanding(self.fx.company)
		frappe.db.commit()
		scope = normalize_scope(self.fx.company, [self.fx.bank])
		fingerprint = authorize_scope(scope)["fingerprint"]
		self.collect = lambda: snapshot.collect(scope, fingerprint)

	def test_collect_reads_the_books_once_when_on_and_three_times_otherwise(self):
		for mode, expected in (("on", 1), ("shadow", 3), ("off", 3)):
			with self.subTest(mode=mode):
				self.settings.return_value = routing.ReaderSettings(mode, (self.fx.company,), ())
				with count_ple_book_queries() as seen:
					result = self.collect()
				self.assertEqual(result["unavailable"], {})
				self.assertEqual(len(seen), expected, seen)
