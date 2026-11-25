"""The one-pass bank read equals today's `get_balance_on` read.

Today's path is the oracle: each case reads the same ledger in the same test transaction once with the
switch off (today) and once with the switch on and the company served (one pass), and compares the whole
reply, or the exception class and refusal code.
"""

from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar import banks, books, compatibility, read_log, routing
from jarvis.payment_calendar.banks import BANK_FALLBACK_KEY, BANK_PATH_KEY, read_bank_balances
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


@contextmanager
def switch(mode, served=()):
	"""The reader switch as the bank read sees it; no setting is saved."""
	settings = routing.ReaderSettings(mode, tuple(served), ())
	with patch.object(routing, "reader_settings", return_value=settings):
		yield


def gl_queries(call):
	"""`call()` and the SQL text of every GL Entry query it ran."""
	seen, original = [], frappe.db.sql

	def spy(query, *args, **kwargs):
		if "tabGL Entry" in str(query):
			seen.append(" ".join(str(query).split()))
		return original(query, *args, **kwargs)

	with patch.object(frappe.db, "sql", spy):
		return call(), seen


def outcome(call):
	"""The reply, or the class and refusal code of the exception (the part a caller can act on)."""
	try:
		return ("ok", call())
	except Exception as exc:
		return ("raised", type(exc).__name__, getattr(exc, "code", None))


class BankOnePassCase(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_bank_one_pass")
		self.fx = CalendarFixtures()
		self.fx.fiscal_year()
		frappe.cache().hdel("fiscal_years", self.fx.company)
		self.year = self.fx.today[:4]

	def tearDown(self):
		frappe.local.form_dict = frappe._dict()
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_bank_one_pass")
		frappe.cache().hdel("fiscal_years", self.fx.company)

	def read(self, accounts=None, as_of=None):
		accounts = accounts or [self.fx.bank]
		return lambda: read_bank_balances(self.fx.company, accounts, as_of or self.fx.today)

	def today_path(self, accounts=None, as_of=None):
		with switch("off"):
			return outcome(self.read(accounts, as_of))

	def one_pass(self, accounts=None, as_of=None):
		with switch("on", (self.fx.company,)):
			return outcome(self.read(accounts, as_of))

	def assert_same(self, accounts=None, as_of=None):
		"""Both paths give the same reply or the same refusal; returns it."""
		today, one_pass = self.today_path(accounts, as_of), self.one_pass(accounts, as_of)
		self.assertEqual(one_pass, today)
		return today

	def gl_row(self, amount, *, posting_date=None, account=None, **values):
		name = self.fx.gl(amount, account=account)
		changes = {"posting_date": posting_date, **values} if posting_date else values
		for field, value in changes.items():
			frappe.db.set_value("GL Entry", name, field, value)
		return name

	def second_bank(self):
		return self.fx.account("Bank2", "Bank", "Asset")


class TestOnePassMatrix(BankOnePassCase):
	"""The one-pass read equals `get_balance_on` for every date, book, currency and ledger state."""

	def test_as_of_dates(self):
		# A day in the middle of the fiscal year, so tomorrow and Dec 31 are always inside it.
		self.fx.today = f"{self.year}-06-15"
		bank2 = self.second_bank()
		self.gl_row(100, posting_date=f"{self.year}-01-01")
		self.gl_row(-30, posting_date=self.fx.today)
		self.gl_row(7, posting_date=add_days(self.fx.today, 1))
		self.gl_row(500, posting_date=f"{self.year}-12-31", account=bank2)
		self.gl_row(11, posting_date=self.fx.today, account=bank2)
		for as_of in (self.fx.today, add_days(self.fx.today, 1), f"{self.year}-01-01", f"{self.year}-12-31"):
			with self.subTest(as_of=as_of):
				result = self.assert_same([self.fx.bank, bank2], as_of)
				self.assertEqual(result[0], "ok")
		# The row of tomorrow is not part of today's balance; the reply is not a refusal.
		balances = {
			row["account"]: row["balance"] for row in self.one_pass([self.fx.bank, bank2])[1]["accounts"]
		}
		self.assertEqual(balances, {self.fx.bank: "70.00", bank2: "11.00"})

	def test_a_date_before_every_fiscal_year_refuses_alike(self):
		self.gl_row(100)
		result = self.assert_same(as_of="1950-06-01")
		self.assertEqual(result[0], "raised")

	def test_an_account_without_rows_is_zero(self):
		bank2 = self.second_bank()
		self.gl_row(40)
		result = self.assert_same([self.fx.bank, bank2])
		self.assertEqual([row["balance"] for row in result[1]["accounts"]], ["40.00", "0.00"])

	def test_cancelled_rows_and_opening_rows(self):
		self.gl_row(100)
		self.gl_row(900, is_cancelled=1)
		self.gl_row(250, is_opening="Yes")
		result = self.assert_same()
		self.assertEqual(result[1]["total"], "350.00")

	def test_rows_of_another_company_or_account_are_not_counted(self):
		other = CalendarFixtures()
		self.gl_row(100)
		self.gl_row(777, company=other.company)
		self.fx.gl(55, account=self.fx.payable)
		result = self.assert_same()
		self.assertEqual(result[1]["total"], "100.00")

	def test_default_blank_and_null_books_count_and_an_alternative_book_refuses(self):
		default = self.fx.insert("Finance Book", "Book", finance_book_name="Calendar default")
		frappe.db.set_value("Company", self.fx.company, "default_finance_book", default)
		frappe.clear_document_cache("Company", self.fx.company)
		for book in (None, "", default):
			self.fx.gl(100, finance_book=book)
		self.assertEqual(self.assert_same()[1]["total"], "300.00")
		self.fx.gl(1, finance_book="Other Finance Book")
		refused = self.assert_same()
		self.assertEqual(refused, ("raised", "CalendarSourceUnavailable", "multibook_unsupported"))

	def test_without_a_default_book_only_blank_and_null_count(self):
		self.fx.gl(100, finance_book=None)
		self.fx.gl(100, finance_book="")
		self.assertEqual(self.assert_same()[1]["total"], "200.00")
		self.fx.gl(5, finance_book="Other Finance Book")
		self.assertEqual(
			self.assert_same()[0:3], ("raised", "CalendarSourceUnavailable", "multibook_unsupported")
		)

	def test_a_default_book_set_after_rows_exist(self):
		later = self.fx.insert("Finance Book", "Later", finance_book_name="Later default")
		self.fx.gl(100, finance_book=later)
		self.assertEqual(self.assert_same()[2], "multibook_unsupported")
		frappe.db.set_value("Company", self.fx.company, "default_finance_book", later)
		frappe.clear_document_cache("Company", self.fx.company)
		self.assertEqual(self.assert_same()[1]["total"], "100.00")

	def test_an_alternative_book_only_in_the_payment_ledger_refuses_alike(self):
		self.gl_row(100)
		invoice = self.fx.invoice()
		self.fx.ple("OtherBook", "Purchase Invoice", invoice, 5, finance_book="Other Finance Book")
		self.assertEqual(self.assert_same(), ("raised", "CalendarSourceUnavailable", "multibook_unsupported"))

	def test_a_delinked_payment_ledger_row_in_another_book_does_not_refuse(self):
		self.gl_row(100)
		invoice = self.fx.invoice()
		ple = self.fx.ple("Delinked", "Purchase Invoice", invoice, 5, finance_book="Other Finance Book")
		frappe.db.set_value("Payment Ledger Entry", ple, "delinked", 1)
		self.assertEqual(self.assert_same()[1]["total"], "100.00")

	def test_the_book_refusal_comes_before_the_account_permission_check(self):
		from frappe.model.document import Document

		self.fx.gl(1, finance_book="Other Finance Book")
		calls = []
		with (
			switch("on", (self.fx.company,)),
			patch.object(Document, "check_permission", lambda self, *a, **k: calls.append(self.name)),
		):
			result = outcome(self.read())
		self.assertEqual(result, ("raised", "CalendarSourceUnavailable", "multibook_unsupported"))
		self.assertEqual(calls, [])

	def test_foreign_currency_account_uses_the_company_currency_columns(self):
		frappe.db.set_value("Account", self.fx.bank, "account_currency", "USD")
		name = self.gl_row(8300)
		frappe.db.set_value("GL Entry", name, {"debit_in_account_currency": 100, "account_currency": "USD"})
		self.assertEqual(self.assert_same()[1]["total"], "8300.00")

	def test_sub_precision_digits_are_rounded_per_row_like_today(self):
		# Three rows of 0.004: each rounds to 0.00, the unrounded sum would be 0.01.
		for _ in range(3):
			self.gl_row(0, debit=0.004)
		self.assertEqual(self.assert_same()[1]["total"], "0.00")
		# Credits round per row too, and rows round before they are summed.
		for _ in range(3):
			self.gl_row(0, credit=0.006)
		self.assertEqual(self.assert_same()[1]["total"], "-0.03")

	def test_a_three_decimal_company_currency(self):
		for _ in range(3):
			self.gl_row(0, debit=0.0004)
		self.gl_row(0, debit=1.2345)
		with patch("erpnext.accounts.utils.get_currency_precision", return_value=3):
			result = self.assert_same()
		self.assertEqual(result[1]["precision"], 3)
		self.assertEqual(result[1]["total"], "1.235")  # the database rounds 1.2345 half up

	def test_precision_zero_rounds_rows_to_integers_and_shows_two_places(self):
		for _ in range(3):
			self.gl_row(0, debit=0.4)
		with patch("erpnext.accounts.utils.get_currency_precision", return_value=0):
			result = self.assert_same()
		self.assertEqual(result[1]["total"], "0.00")
		self.assertEqual(result[1]["precision"], 2)


class TestOnePassSelection(BankOnePassCase):
	def test_duplicates_collapse_and_case_variants_name_one_account(self):
		self.gl_row(100)
		result = self.assert_same([self.fx.bank, self.fx.bank, self.fx.bank.lower()])
		self.assertEqual(len(result[1]["accounts"]), 1)

	def test_disabled_group_and_wrong_type_accounts_refuse_alike(self):
		for field, value in (("disabled", 1), ("is_group", 1), ("account_type", "Receivable")):
			old = frappe.db.get_value("Account", self.fx.bank, field)
			frappe.db.set_value("Account", self.fx.bank, field, value)
			with self.subTest(field=field):
				self.assertEqual(self.assert_same()[0], "raised")
			frappe.db.set_value("Account", self.fx.bank, field, old)

	def test_fifty_accounts_are_one_gl_query_and_fifty_one_are_refused(self):
		banks = [self.fx.account(f"Many{i}", "Bank", "Asset") for i in range(50)]
		for index, name in enumerate(banks[:5]):
			self.gl_row(index + 1, account=name)
		with switch("on", (self.fx.company,)):
			result, queries = gl_queries(self.read(banks))
		self.assertEqual(len(result["accounts"]), 50)
		self.assertEqual(len(queries), 1)
		self.assertIn("GROUP BY account, finance_book", queries[0])
		self.assertEqual(result, self.today_path(banks)[1])
		self.assertEqual(self.assert_same(banks + [self.fx.bank])[0], "raised")

	def test_an_account_the_user_cannot_read_refuses_alike(self):
		user = self.fx.user("Sales User")
		self.gl_row(100)
		frappe.set_user(user)
		today, one_pass = self.today_path(), self.one_pass()
		self.assertEqual(today[0], "raised")
		self.assertEqual(one_pass, today)

	def test_no_fiscal_year_refuses_before_any_ledger_read(self):
		from erpnext.accounts.utils import FiscalYearError

		self.gl_row(100)
		for mode, served in (("off", ()), ("on", (self.fx.company,))):
			with (
				self.subTest(mode=mode),
				switch(mode, served),
				patch("erpnext.accounts.utils.get_fiscal_year", side_effect=FiscalYearError),
			):
				result, queries = gl_queries(lambda: outcome(self.read()))
			self.assertEqual(result, ("raised", "CalendarSourceUnavailable", "fiscal_year_unavailable"))
			self.assertEqual(queries, [])

	def test_every_account_gets_a_read_permission_check(self):
		from frappe.model.document import Document

		bank2 = self.second_bank()
		self.gl_row(1)
		seen = []

		def record(document, permtype=None, *args, **kwargs):
			seen.append((document.doctype, document.name, permtype))

		with switch("on", (self.fx.company,)), patch.object(Document, "check_permission", record):
			read_bank_balances(self.fx.company, [self.fx.bank, bank2], self.fx.today)
		self.assertEqual(
			[entry for entry in seen if entry[0] == "Account"],
			[("Account", self.fx.bank, "read"), ("Account", bank2, "read")],
		)

	def test_a_denied_account_check_is_raised_not_swallowed(self):
		from frappe.model.document import Document

		self.gl_row(1)

		def deny(document, *args, **kwargs):
			raise frappe.PermissionError("denied")

		with switch("on", (self.fx.company,)), patch.object(Document, "check_permission", deny):
			with self.assertRaises(frappe.PermissionError):
				read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)

	def test_request_fields_do_not_change_the_one_pass_read(self):
		self.gl_row(100)
		frappe.local.form_dict = frappe._dict(date="1990-01-01", account="forged", cost_center="forged")
		self.assertEqual(self.assert_same()[1]["total"], "100.00")


class TestOnePassRouting(BankOnePassCase):
	"""Only `on` plus a served company takes the one-pass path; anything else is today's read."""

	def path(self, mode, served):
		with switch(mode, served):
			result, queries = gl_queries(self.read())
		return result, any("GROUP BY account, finance_book" in query for query in queries)

	def test_only_on_with_the_company_served_uses_one_pass(self):
		self.gl_row(100)
		company = self.fx.company
		cases = (
			("off", (company,), False),
			("shadow", (company,), False),
			("on", (), False),
			("on", ("Another Company",), False),
			("on", (company,), True),
		)
		replies = set()
		for mode, served, expected in cases:
			with self.subTest(mode=mode, served=served):
				result, one_pass = self.path(mode, served)
				self.assertEqual(one_pass, expected)
				replies.add(repr(result))
		self.assertEqual(len(replies), 1)

	def test_off_reads_the_ledger_the_way_today_does(self):
		self.gl_row(100)
		with switch("off"):
			_, queries = gl_queries(self.read())
		# The book gate and one balance read: at least two GL queries, none of them grouped.
		self.assertGreaterEqual(len(queries), 2)

	def test_on_reads_the_ledger_once(self):
		self.gl_row(100)
		with switch("on", (self.fx.company,)):
			_, queries = gl_queries(self.read())
		self.assertEqual(len(queries), 1)

	def test_the_switch_is_read_once_per_view(self):
		self.gl_row(100)
		settings = routing.ReaderSettings("on", (self.fx.company,), ())
		with patch.object(routing, "reader_settings", return_value=settings) as read:
			with books.book_memo():
				self.read()()
				self.read()()
			self.assertEqual(read.call_count, 1)
			self.read()()  # outside a view the switch is read fresh
			self.assertEqual(read.call_count, 2)

	def test_a_settings_read_failure_gives_todays_path(self):
		self.gl_row(100)
		with patch.object(routing, "_read", side_effect=RuntimeError("settings unavailable")):
			result, queries = gl_queries(self.read())
		self.assertEqual(result["total"], "100.00")
		self.assertFalse(any("GROUP BY account, finance_book" in query for query in queries))

	def test_a_served_company_is_matched_exactly(self):
		self.gl_row(100)
		for served in (self.fx.company.lower(), self.fx.company + " "):
			with self.subTest(served=served):
				self.assertFalse(self.path("on", (served,))[1])


class TestOnePassPin(BankOnePassCase):
	def helper(self):
		from erpnext.accounts.utils import get_balance_on

		return get_balance_on

	def test_the_installed_helper_is_pinned(self):
		self.assertTrue(compatibility.bank_one_pass_pinned(self.helper()))

	def test_only_the_two_include_default_sources_are_pinned(self):
		self.assertEqual(len(compatibility._BANK_HELPER_HASHES), 2)
		# The 16.50 source has another default-book rule: it keeps today's read.
		self.assertNotIn(
			"7af005912349b570c56e0fdd4306bab43721cb620a0e418ae25d38c359ebe3c6",
			compatibility._BANK_HELPER_HASHES,
		)
		for hashes in (
			("7af005912349b570c56e0fdd4306bab43721cb620a0e418ae25d38c359ebe3c6",),
			("0" * 64,),
			(),
		):
			with self.subTest(hashes=hashes), patch.object(compatibility, "_BANK_HELPER_HASHES", hashes):
				self.assertFalse(compatibility.bank_one_pass_pinned(self.helper()))

	def test_an_unreadable_or_unreviewed_source_is_not_pinned(self):
		for error in (OSError, TypeError):
			with self.subTest(error=error), patch.object(compatibility, "getsource", side_effect=error):
				self.assertFalse(compatibility.bank_one_pass_pinned(self.helper()))
		with patch.object(compatibility, "getsource", return_value="unreviewed finance-book filter"):
			self.assertFalse(compatibility.bank_one_pass_pinned(self.helper()))

	def test_version_15_is_not_pinned(self):
		import erpnext

		with (
			patch.object(frappe, "__version__", "15.0.0"),
			patch.object(erpnext, "__version__", "15.0.0"),
		):
			self.assertFalse(compatibility.bank_one_pass_pinned(self.helper()))

	def test_an_unsupported_framework_pair_is_not_pinned(self):
		import erpnext

		with patch.object(erpnext, "__version__", "17.0.0"):
			with self.assertRaises(CalendarSourceUnavailable):
				compatibility.source_profile()
			self.assertFalse(compatibility.bank_one_pass_pinned(self.helper()))

	def test_an_unpinned_source_keeps_todays_read_on_a_served_company(self):
		self.gl_row(100)
		with (
			patch.object(banks, "bank_one_pass_blocker", return_value="unpinned"),
			switch("on", (self.fx.company,)),
		):
			result, queries = gl_queries(self.read())
		self.assertEqual(result["total"], "100.00")
		self.assertFalse(any("GROUP BY account, finance_book" in query for query in queries))


class TestOnePassPlanHint(BankOnePassCase):
	"""The `IGNORE INDEX` hint is not worth a lost figure: a missing index reads today's way."""

	def run_with_sql(self, query):
		with (
			switch("on", (self.fx.company,)),
			patch.object(banks, "_ONE_PASS_SQL", query),
			patch.object(banks, "today_amounts", wraps=banks.today_amounts) as today,
		):
			result = outcome(self.read())
		return result, today.call_count

	def test_a_missing_index_in_the_hint_falls_back_to_todays_read(self):
		self.gl_row(100)
		self.gl_row(-30, account=self.second_bank())
		hinted = banks._ONE_PASS_SQL.replace("IGNORE INDEX (company)", "IGNORE INDEX (no_such_index)")
		self.assertNotEqual(hinted, banks._ONE_PASS_SQL)
		result, today_calls = self.run_with_sql(hinted)
		self.assertEqual(today_calls, 1)
		self.assertEqual(result, self.today_path())
		self.assertEqual(result[1]["total"], "100.00")

	def test_the_installed_hint_names_an_existing_index(self):
		self.gl_row(100)
		result, today_calls = self.run_with_sql(banks._ONE_PASS_SQL)
		self.assertEqual(today_calls, 0)
		self.assertEqual(result[1]["total"], "100.00")

	def test_any_other_database_error_propagates(self):
		self.gl_row(100)
		broken = banks._ONE_PASS_SQL.replace("SELECT account,", "SELECT no_such_column,")
		result, today_calls = self.run_with_sql(broken)
		self.assertEqual(today_calls, 0)
		self.assertEqual(result[0], "raised")
		self.assertNotEqual(result[1], "CalendarSourceUnavailable")

	def test_a_refusal_is_not_hidden_by_the_fallback(self):
		self.fx.gl(1, finance_book="Other Finance Book")
		hinted = banks._ONE_PASS_SQL.replace("IGNORE INDEX (company)", "IGNORE INDEX (no_such_index)")
		result, _ = self.run_with_sql(hinted)
		self.assertEqual(result, ("raised", "CalendarSourceUnavailable", "multibook_unsupported"))


class TestBankPathKey(BankOnePassCase):
	"""`frappe.local.<BANK_PATH_KEY>` tells the collect log which path read the bank."""

	def tearDown(self):
		if hasattr(frappe.local, BANK_PATH_KEY):
			delattr(frappe.local, BANK_PATH_KEY)
		super().tearDown()

	def path_after(self, mode="on", served=None, *, pinned=True):
		"""The recorded path (None when unset) after one read, and the outcome of that read."""
		served = (self.fx.company,) if served is None else served
		blocker = None if pinned else "unpinned"
		with switch(mode, served), patch.object(banks, "bank_one_pass_blocker", return_value=blocker):
			result = outcome(self.read())
		return getattr(frappe.local, BANK_PATH_KEY, None), result

	def test_the_key_name_is_fixed(self):
		self.assertEqual(BANK_PATH_KEY, "payment_calendar_bank_path")

	def test_one_pass_is_recorded(self):
		self.gl_row(100)
		path, result = self.path_after()
		self.assertEqual((path, result[0]), ("one_pass", "ok"))

	def test_an_unpinned_source_on_a_served_company_records_today(self):
		self.gl_row(100)
		path, result = self.path_after(pinned=False)
		self.assertEqual((path, result[0]), ("today", "ok"))

	def test_the_missing_index_fallback_records_today(self):
		self.gl_row(100)
		hinted = banks._ONE_PASS_SQL.replace("IGNORE INDEX (company)", "IGNORE INDEX (no_such_index)")
		with patch.object(banks, "_ONE_PASS_SQL", hinted):
			path, result = self.path_after()
		self.assertEqual((path, result[0]), ("today", "ok"))

	def test_a_refused_read_still_records_its_path(self):
		from erpnext.accounts.utils import FiscalYearError

		# A book refusal after the one-pass query, the same refusal on today's path, and a refusal before any read.
		self.fx.gl(1, finance_book="Other Finance Book")
		self.assertEqual(self.path_after()[0], "one_pass")
		self.assertEqual(self.path_after(pinned=False)[0], "today")
		with patch("erpnext.accounts.utils.get_fiscal_year", side_effect=FiscalYearError):
			path, result = self.path_after()
		self.assertEqual((path, result[0]), ("one_pass", "raised"))

	def test_nothing_is_recorded_when_off_shadow_or_not_served(self):
		self.gl_row(100)
		for mode, served in (("off", None), ("shadow", None), ("on", ("Another Company",)), ("on", ())):
			with self.subTest(mode=mode, served=served):
				self.assertEqual(self.path_after(mode, served)[0], None)

	def test_a_value_from_an_earlier_read_does_not_survive_an_unrouted_read(self):
		self.gl_row(100)
		self.assertEqual(self.path_after()[0], "one_pass")
		self.assertIsNone(self.path_after("off")[0])
		self.assertEqual(self.path_after()[0], "one_pass")
		self.assertIsNone(self.path_after("on", ())[0])

	def test_a_failure_before_the_path_is_known_leaves_no_earlier_value(self):
		self.gl_row(100)
		self.assertEqual(self.path_after()[0], "one_pass")
		with patch.object(books, "view_settings", side_effect=RuntimeError("switch unavailable")):
			with self.assertRaises(RuntimeError):
				self.read()()
		self.assertFalse(hasattr(frappe.local, BANK_PATH_KEY))


class TestExactSum(BankOnePassCase):
	"""Group sums are added as exact text, so the total equals the database's one SUM at any size."""

	def default_book(self):
		book = self.fx.insert("Finance Book", "Book", finance_book_name="Calendar default")
		frappe.db.set_value("Company", self.fx.company, "default_finance_book", book)
		frappe.clear_document_cache("Company", self.fx.company)
		return book

	def random_books(self, rnd, default, scale):
		frappe.db.sql("delete from `tabGL Entry` where account=%s", self.fx.bank)
		for book in (None, "", default):
			for _ in range(rnd.randint(1, 3)):
				row = self.fx.gl(0, finance_book=book)
				frappe.db.set_value(
					"GL Entry",
					row,
					{"debit": round(rnd.uniform(0, scale), 9), "credit": round(rnd.uniform(0, scale), 9)},
				)

	def test_large_balances_at_every_precision_equal_todays_total(self):
		import random

		default, rnd, mismatches = self.default_book(), random.Random(7), []
		for precision in (3, 4, 6, 9):
			for _ in range(12):
				self.random_books(rnd, default, rnd.choice([1e9, 1e11, 1e12]))
				with patch("erpnext.accounts.utils.get_currency_precision", return_value=precision):
					today, one_pass = self.today_path(), self.one_pass()
				if today != one_pass:
					mismatches.append((precision, today, one_pass))
		self.assertEqual(mismatches, [])

	def test_a_total_near_1e13_at_three_decimals_with_every_book_group(self):
		# A column holds at most 999,999,999,999.999999999; three rows per book put the total near 1e13.
		default = self.default_book()
		for book, debit, credit in (
			(None, 999999999999.999, 0.001),
			("", 0.001, 499999999999.5),
			(default, 500000000000.001, 0.0004),
		):
			for _ in range(3):
				row = self.fx.gl(0, finance_book=book)
				frappe.db.set_value("GL Entry", row, {"debit": debit, "credit": credit})
		with patch("erpnext.accounts.utils.get_currency_precision", return_value=3):
			today, one_pass = self.today_path(), self.one_pass()
		self.assertEqual(today[0], "ok")
		self.assertEqual(one_pass, today)


class TestCanonicalAccountLabels(BankOnePassCase):
	"""The ledger compares account names with a case, accent and trailing-space insensitive collation."""

	def relabel(self, label):
		self.gl_row(100)
		frappe.db.sql("update `tabGL Entry` set account=%s where account=%s", (label, self.fx.bank))

	def test_a_label_in_another_case(self):
		self.relabel(self.fx.bank.lower())
		self.assertEqual(self.assert_same()[1]["total"], "100.00")

	def test_a_label_with_a_trailing_space(self):
		self.relabel(self.fx.bank + " ")
		self.assertEqual(self.assert_same()[1]["total"], "100.00")

	def test_a_label_without_the_accent(self):
		bank = self.fx.account("Banké", "Bank", "Asset")
		self.gl_row(100, account=bank)
		frappe.db.sql("update `tabGL Entry` set account=%s where account=%s", (bank.replace("é", "e"), bank))
		self.assertEqual(self.assert_same([bank])[1]["total"], "100.00")

	def test_labels_of_one_account_add_up(self):
		self.gl_row(100)
		self.gl_row(20)
		frappe.db.sql(
			"update `tabGL Entry` set account=%s where account=%s and credit=0 and debit=20",
			(self.fx.bank.upper(), self.fx.bank),
		)
		self.assertEqual(self.assert_same()[1]["total"], "120.00")

	def test_an_unresolvable_label_falls_back_to_todays_read(self):
		self.gl_row(100)
		real = frappe.db.get_value

		def lookup(doctype, name, *args, **kwargs):
			return (
				None
				if doctype == "Account" and isinstance(name, str) and name != self.fx.bank
				else real(doctype, name, *args, **kwargs)
			)

		self.relabel(self.fx.bank.lower())
		with (
			switch("on", (self.fx.company,)),
			patch.object(frappe.db, "get_value", lookup),
			patch.object(banks, "today_amounts", wraps=banks.today_amounts) as today,
		):
			result = outcome(self.read())
		self.assertEqual((result[0], today.call_count), ("ok", 1))
		self.assertEqual(getattr(frappe.local, BANK_FALLBACK_KEY, None), "account_label")


class TestStaleCompanyDefault(BankOnePassCase):
	def test_the_summed_books_follow_the_cached_default_like_get_balance_on(self):
		book = self.fx.insert("Finance Book", "Book", finance_book_name="Calendar default")
		frappe.get_cached_value("Company", self.fx.company, "default_finance_book")
		self.fx.gl(100, finance_book=book)
		self.fx.gl(7)
		# A raw write leaves the document cache stale: the gate sees the new default, the sum the cached one.
		frappe.db.sql("update tabCompany set default_finance_book=%s where name=%s", (book, self.fx.company))
		today = self.today_path()
		self.assertEqual(today[1]["total"], "7.00")
		self.assertEqual(self.one_pass(), today)


class TestFallbackCauses(BankOnePassCase):
	"""Every served read that does not take the one pass says why: a key for the log line and one ERROR line."""

	def setUp(self):
		super().setUp()
		banks._logged_fallbacks.clear()
		self.addCleanup(banks._logged_fallbacks.clear)
		self.addCleanup(
			lambda: hasattr(frappe.local, BANK_FALLBACK_KEY) and delattr(frappe.local, BANK_FALLBACK_KEY)
		)
		self.gl_row(100)

	def served_read(self, blocker=None, sql=None):
		with (
			switch("on", (self.fx.company,)),
			patch.object(banks, "bank_one_pass_blocker", return_value=blocker),
			patch.object(banks, "_ONE_PASS_SQL", sql or banks._ONE_PASS_SQL),
			patch.object(read_log, "speed_fallback") as logged,
		):
			result = outcome(self.read())
		return result, getattr(frappe.local, BANK_FALLBACK_KEY, None), logged

	def test_an_unpinned_or_v15_source(self):
		for cause in ("unpinned", "v15"):
			with self.subTest(cause=cause):
				result, key, logged = self.served_read(blocker=cause)
				self.assertEqual((result[0], key), ("ok", cause))
				self.assertEqual(logged.call_args.args[0], f"bank_one_pass:{cause}")

	def test_a_missing_index(self):
		hinted = banks._ONE_PASS_SQL.replace("IGNORE INDEX (company)", "IGNORE INDEX (no_such_index)")
		result, key, logged = self.served_read(sql=hinted)
		self.assertEqual((result[0], key), ("ok", "missing_index"))
		self.assertEqual(logged.call_args.args[0], "bank_one_pass:missing_index")

	def test_the_line_is_logged_once_per_process_and_cause(self):
		_, _, first = self.served_read(blocker="unpinned")
		_, _, again = self.served_read(blocker="unpinned")
		_, _, other = self.served_read(blocker="v15")
		self.assertEqual((first.call_count, again.call_count, other.call_count), (1, 0, 1))

	def test_the_line_names_no_company_or_account(self):
		with patch.object(frappe, "logger") as logger:
			self.served_read(blocker="unpinned")
			banks._logged_fallbacks.clear()
			with (
				switch("on", (self.fx.company,)),
				patch.object(banks, "bank_one_pass_blocker", return_value="unpinned"),
			):
				self.read()()
		lines = [call.args[0] for call in logger.return_value.error.call_args_list]
		self.assertTrue(lines)
		for line in lines:
			self.assertNotIn(self.fx.prefix, line)

	def test_the_one_pass_and_the_unrouted_reads_leave_no_cause(self):
		self.assertEqual(self.served_read()[1:2], (None,))
		for mode, served in (("off", ()), ("shadow", (self.fx.company,)), ("on", ())):
			with self.subTest(mode=mode, served=served):
				with switch(mode, served), patch.object(read_log, "speed_fallback") as logged:
					self.read()()
				self.assertFalse(hasattr(frappe.local, BANK_FALLBACK_KEY))
				logged.assert_not_called()

	def test_a_cause_from_an_earlier_read_does_not_survive(self):
		self.served_read(blocker="unpinned")
		self.assertEqual(self.served_read()[1], None)

	def test_the_keys_are_reset_on_the_first_line_even_for_a_refused_request(self):
		setattr(frappe.local, BANK_PATH_KEY, "one_pass")
		setattr(frappe.local, BANK_FALLBACK_KEY, "unpinned")
		with self.assertRaises(frappe.ValidationError):
			read_bank_balances(self.fx.company, [self.fx.bank], None)
		self.assertFalse(hasattr(frappe.local, BANK_PATH_KEY))
		self.assertFalse(hasattr(frappe.local, BANK_FALLBACK_KEY))


class TestSharedPieces(BankOnePassCase):
	def test_the_account_cap_is_one_constant(self):
		self.assertEqual(banks.MAX_BANK_ACCOUNTS, 50)
		names = [self.fx.bank] * 3
		with patch.object(banks, "MAX_BANK_ACCOUNTS", 2), self.assertRaises(frappe.ValidationError):
			banks.validate_bank_accounts(self.fx.company, names)
		self.assertEqual(banks.validate_bank_accounts(self.fx.company, names), [self.fx.bank])

	def test_one_missing_index_constant(self):
		from jarvis.payment_calendar import builtin_reader

		self.assertEqual(banks.MISSING_INDEX_ERROR, 1176)
		self.assertIs(banks.MISSING_INDEX_ERROR, builtin_reader.MISSING_KEY)

	def test_allowed_books_is_the_blank_null_default_rule(self):
		self.assertEqual(books.allowed_books("Any Co", None), {None, ""})
		self.assertEqual(books.allowed_books("Any Co", "Default"), {None, "", "Default"})

	def test_the_helper_hashes_are_one_constant_for_both_readers(self):
		from erpnext.accounts.utils import get_balance_on

		self.assertTrue(compatibility.bank_one_pass_pinned(get_balance_on))
		self.assertEqual(
			compatibility.bank_balance_arguments(get_balance_on)["include_default_fb_balances"], True
		)
		with patch.object(compatibility, "_BANK_HELPER_HASHES", ()):
			self.assertFalse(compatibility.bank_one_pass_pinned(get_balance_on))
			with self.assertRaises(CalendarSourceUnavailable):
				compatibility.bank_balance_arguments(get_balance_on)

	def test_bank_read_inputs(self):
		inputs = banks.bank_read_inputs(self.fx.company, self.fx.today)
		self.assertEqual((inputs[0], inputs.row_precision, inputs.default_book), (True, 2, None))
		self.assertEqual(inputs.money_precision, 2)
		with patch("erpnext.accounts.utils.get_currency_precision", return_value=0):
			inputs = banks.bank_read_inputs(self.fx.company, self.fx.today)
		self.assertEqual((inputs.row_precision, inputs.money_precision), (0, 2))

	def test_bank_read_inputs_default_book_is_the_cached_value_like_get_balance_on(self):
		book = self.fx.insert("Finance Book", "Book", finance_book_name="Calendar default")
		frappe.get_cached_value("Company", self.fx.company, "default_finance_book")
		frappe.db.sql("update tabCompany set default_finance_book=%s where name=%s", (book, self.fx.company))
		self.assertIsNone(banks.bank_read_inputs(self.fx.company, self.fx.today).default_book)

	def test_bank_read_inputs_refuses_without_a_fiscal_year(self):
		with self.assertRaises(CalendarSourceUnavailable) as raised:
			banks.bank_read_inputs(self.fx.company, "1950-06-01")
		self.assertEqual(raised.exception.code, "fiscal_year_unavailable")

	def test_the_amount_readers_are_public(self):
		self.gl_row(100)
		args = (self.fx.company, [self.fx.bank], self.fx.today)
		from erpnext.accounts.utils import get_balance_on

		self.assertEqual(banks.today_amounts(*args, get_balance_on), {self.fx.bank: 100.0})
		self.assertEqual(banks.one_pass_amounts(*args, 2, None), {self.fx.bank: 100.0})


class TestPinHardening(BankOnePassCase):
	def helper(self):
		from erpnext.accounts.utils import get_balance_on

		return get_balance_on

	def test_a_wrapper_with_the_audited_source_text_is_not_pinned(self):
		import functools

		inner = self.helper()

		@functools.wraps(inner)
		def wrapper(*args, **kwargs):
			return inner(*args, **kwargs)

		self.assertEqual(compatibility.bank_one_pass_blocker(wrapper), "unpinned")

	def test_a_function_from_another_module_is_not_pinned(self):
		import types

		clone = types.FunctionType(self.helper().__code__, {}, "get_balance_on")
		clone.__module__ = "some_other_app.utils"
		with patch.object(compatibility, "getsource", return_value=compatibility.getsource(self.helper())):
			self.assertEqual(compatibility.bank_one_pass_blocker(clone), "unpinned")

	def test_the_blocker_names_v15_and_unpinned(self):
		import erpnext

		self.assertIsNone(compatibility.bank_one_pass_blocker(self.helper()))
		with patch.object(compatibility, "getsource", return_value="unreviewed"):
			self.assertEqual(compatibility.bank_one_pass_blocker(self.helper()), "unpinned")
		with patch.object(frappe, "__version__", "15.0.0"), patch.object(erpnext, "__version__", "15.0.0"):
			self.assertEqual(compatibility.bank_one_pass_blocker(self.helper()), "v15")


class TestSettingsReadInsideAView(BankOnePassCase):
	def tearDown(self):
		# The view test below ends the test transaction, and the savepoint with it.
		frappe.db.rollback()
		frappe.set_user("Administrator")

	def test_a_failing_settings_read_gives_todays_read_and_no_path(self):
		self.gl_row(100)
		with (
			books.book_memo(),
			patch.object(routing, "_read", side_effect=RuntimeError("settings unavailable")),
		):
			result = self.read()()
		self.assertEqual(result["total"], "100.00")
		self.assertFalse(hasattr(frappe.local, BANK_PATH_KEY))

	def test_a_rolled_back_settings_read_breaks_the_view(self):
		from MySQLdb import OperationalError

		from jarvis.payment_calendar import snapshot

		# Every view now reads the switch, also when it is off: a deadlock in that read ends the view.
		frappe.db.rollback()
		with self.assertRaises(CalendarSourceUnavailable):
			with snapshot.consistent_read():
				with patch.object(routing, "_read", side_effect=OperationalError(1213, "deadlock")):
					self.assertEqual(books.view_settings().mode, "off")
				self.assertTrue(snapshot.view_is_broken())
