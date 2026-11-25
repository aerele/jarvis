"""Contracts against ERPNext's real allocations; fixtures never leave test_jarvis."""

import re
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import wraps
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe
from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport
from frappe.utils import add_days, getdate

from jarvis.payment_calendar import compatibility, read_log, sources
from jarvis.payment_calendar.compatibility import source_profile
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.reconciliation import InvoiceReconciler
from jarvis.payment_calendar.sources import read_obligations
from jarvis.tests.payment_calendar_differential import no_oracle_compare
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site
from jarvis.tests.payment_calendar_oracle_1a import oracle_read_obligations

IN_LIST = re.compile(r"\bIN \(([^)]*)\)", re.IGNORECASE)


class TestPaymentCalendarSources(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_case")
		frappe.clear_document_cache("Accounts Settings")
		self.fx = CalendarFixtures()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_case")
		frappe.clear_document_cache("Accounts Settings")

	def read(self, side="Payable", **kwargs):
		return read_obligations(
			self.fx.company, side, self.fx.today, self.fx.today, add_days(self.fx.today, 40), **kwargs
		)

	def test_unpaid_and_partial_terms_reconcile_without_duplication(self):
		self.fx.invoice()
		partial = self.fx.invoice(paid=30)
		result = self.read()
		self.assertEqual(Decimal(result["canonical_total"]), Decimal("170"))
		terms = [r for r in result["rows"] if r["voucher_no"] == partial]
		self.assertEqual([Decimal(r["remaining"]) for r in terms], [Decimal("10"), Decimal("60")])
		self.assertEqual(sum(Decimal(r["remaining"]) for r in result["rows"]), Decimal("170"))
		self.assertTrue(result["complete_for_company"])

	def test_fully_settled_invoice_is_retained_with_zero_remaining(self):
		invoice = self.fx.invoice(paid=100)
		result = self.read()
		terms = [r for r in result["rows"] if r["voucher_no"] == invoice]
		self.assertEqual(len(terms), 2)
		self.assertEqual(sum(Decimal(r["original"]) for r in terms), Decimal("100"))
		self.assertEqual([Decimal(r["remaining"]) for r in terms], [Decimal("0"), Decimal("0")])
		self.assertTrue(all(r["settlement"] in ("Paid", "Settled") for r in terms))

	def test_paid_term_of_an_open_invoice_is_retained(self):
		invoice = self.fx.invoice(paid=40)
		result = self.read()
		terms = [r for r in result["rows"] if r["voucher_no"] == invoice]
		self.assertEqual([Decimal(r["remaining"]) for r in terms], [Decimal("0"), Decimal("60")])
		self.assertEqual(Decimal(result["canonical_total"]), Decimal("60"))

	def test_float_payment_residue_does_not_duplicate_a_settled_term(self):
		invoice = self.fx.invoice((0.8, 1.2), paid=0.1)
		self.fx.ple("SecondPayment", "Payment Entry", invoice, -0.7)
		rows = [r for r in self.read()["rows"] if r["voucher_no"] == invoice]
		self.assertEqual(len(rows), 2)
		self.assertEqual(sum(Decimal(r["original"]) for r in rows), Decimal("2.00"))
		self.assertEqual(sorted(Decimal(r["remaining"]) for r in rows), [Decimal("0.00"), Decimal("1.20")])

	def test_no_schedule_uses_invoice_due_date_for_open_and_settled(self):
		for paid in (0, 100):
			invoice = self.fx.invoice((100,), paid=paid, schedules=False)
			rows = [r for r in self.read()["rows"] if r["voucher_no"] == invoice]
			self.assertEqual(len(rows), 1)
			self.assertEqual(rows[0]["due_date"], add_days(self.fx.today, 20))
			self.assertEqual(Decimal(rows[0]["remaining"]), Decimal(100 - paid))

	def test_receivables_use_the_same_canonical_term_semantics(self):
		invoice = self.fx.invoice(paid=25, side="Receivable")
		result = self.read("Receivable")
		self.assertEqual(result["currency"], "INR")
		self.assertEqual(Decimal(result["canonical_total"]), Decimal("75"))
		self.assertTrue(all(r["party_type"] == "Customer" for r in result["rows"]))
		self.assertEqual(
			[Decimal(r["remaining"]) for r in result["rows"] if r["voucher_no"] == invoice],
			[Decimal("15"), Decimal("60")],
		)

	def test_future_postings_are_excluded_without_hiding_future_due_dates(self):
		present = self.fx.invoice()
		future = self.fx.invoice(offset=1)
		rows = self.read()["rows"]
		self.assertTrue(any(r["voucher_no"] == present for r in rows))
		self.assertFalse(any(r["voucher_no"] == future for r in rows))

	def test_credit_note_settles_terms_without_becoming_a_cash_receipt(self):
		invoice = self.fx.invoice(paid=50, credit=50)
		terms = [r for r in self.read()["rows"] if r["voucher_no"] == invoice]
		self.assertEqual(sum(Decimal(r["remaining"]) for r in terms), Decimal("0"))
		self.assertTrue(any(r["settlement"] == "Settled" for r in terms))
		self.assertEqual(sum(Decimal(r["credit"]) for r in terms), Decimal("50"))

	def test_overpayment_keeps_a_signed_reconciliation_row(self):
		invoice = self.fx.invoice(paid=120)
		result = self.read()
		self.assertEqual(Decimal(result["canonical_total"]), Decimal("-20"))
		self.assertEqual(sum(Decimal(r["remaining"]) for r in result["rows"]), Decimal("-20"))
		credits = [r for r in result["rows"] if r["kind"] == "adjustment"]
		self.assertTrue(any(r["voucher_no"] == invoice and Decimal(r["remaining"]) == -20 for r in credits))

	def test_new_source_does_not_change_ordinary_report_output(self):
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		self.fx.invoice(paid=30)
		filters = {
			"company": self.fx.company,
			"report_date": self.fx.today,
			"based_on_payment_terms": 1,
			"ageing_based_on": "Due Date",
			"range": "30,60,90,120",
			"group_by_party": 0,
			"ignore_accounts": 0,
		}
		args = {"account_type": "Payable", "naming_by": ["Buying Settings", "supp_master_name"]}
		before = ReceivablePayableReport(filters).run(args)[1]
		self.read()
		after = ReceivablePayableReport(filters).run(args)[1]
		self.assertEqual(before, after)

	def test_disabled_report_is_refused_before_source_execution(self):
		with patch("frappe.desk.query_report.get_report_doc", side_effect=frappe.PermissionError):
			with self.assertRaises(frappe.PermissionError):
				self.read()

	def test_missing_private_contract_is_refused(self):
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		with patch.object(ReceivablePayableReport, "get_payment_terms", None):
			with self.assertRaises(frappe.ValidationError):
				self.read()

	def test_paid_overlay_is_bounded_to_the_visible_due_window(self):
		self.fx.invoice(paid=100)
		result = read_obligations(
			self.fx.company,
			"Payable",
			self.fx.today,
			add_days(self.fx.today, 50),
			add_days(self.fx.today, 80),
		)
		self.assertEqual(result["rows"], [])
		self.assertEqual(Decimal(result["canonical_total"]), 0)

	def test_multiple_payable_accounts_do_not_duplicate_one_invoice_schedule(self):
		invoice = self.fx.invoice(paid=50)
		other = self.fx.account("Payable2", "Payable", "Liability")
		self.fx.ple("OtherAccount", "Purchase Invoice", invoice, 50, account=other)
		with self.assertRaises(frappe.ValidationError):
			self.read()

	def test_supplier_permission_cannot_leak_a_fully_paid_other_supplier(self):
		visible = self.fx.invoice()
		other = self.fx.insert(
			"Supplier", "OtherSupplier", supplier_name="Hidden supplier", supplier_type="Company", disabled=0
		)
		hidden = self.fx.invoice(paid=100, supplier=other)
		user = self._restricted_user("Supplier", self.fx.supplier)
		frappe.set_user(user)
		result = self.read()
		self.assertFalse(result["complete_for_company"])
		self.assertTrue(any(r["voucher_no"] == visible for r in result["rows"]))
		self.assertFalse(any(r["party"] == other or r["voucher_no"] == hidden for r in result["rows"]))
		self.assertEqual(Decimal(result["canonical_total"]), 100)

	def test_invoice_permission_filters_the_settled_overlay_too(self):
		visible = self.fx.invoice()
		hidden = self.fx.invoice(paid=100)
		user = self._restricted_user("Purchase Invoice", visible)
		frappe.set_user(user)
		result = self.read()
		self.assertFalse(result["complete_for_company"])
		self.assertTrue(any(r["voucher_no"] == visible for r in result["rows"]))
		self.assertFalse(any(r["voucher_no"] == hidden for r in result["rows"]))

	def test_scheduled_discount_is_separate_from_allocated_payment(self):
		invoice = self.fx.invoice((100,), paid=100, schedule_paid={0: 90}, discounts={0: 10})
		rows = [r for r in self.read()["rows"] if r["voucher_no"] == invoice]
		self.assertEqual(len(rows), 1)
		self.assertEqual(Decimal(rows[0]["discount"]), 10)
		self.assertEqual(Decimal(rows[0]["paid"]), 90)
		self.assertEqual(Decimal(rows[0]["remaining"]), 0)
		self.assertEqual(rows[0]["settlement"], "Settled")

	def test_foreign_account_residual_that_canonical_report_drops_is_unavailable(self):
		self.fx.invoice((100,), paid=100)
		frappe.db.set_value(
			"Payment Ledger Entry", self.fx.prefix + "PaymentPLE1", "amount_in_account_currency", -95
		)
		with self.assertRaises(frappe.ValidationError):
			self.read()

	def test_sales_person_restriction_does_not_turn_skipped_invoices_into_settled_rows(self):
		self.fx.invoice(paid=100, side="Receivable")
		person = self.fx.insert(
			"Sales Person",
			"SalesPerson",
			sales_person_name="Scoped sales person",
			is_group=0,
			lft=1,
			rgt=2,
			enabled=1,
		)
		frappe.set_user(self._restricted_user("Sales Person", person))
		if source_profile().major == "15":
			with (
				patch("jarvis.payment_calendar.sources._report_class") as report,
				self.assertRaises(CalendarSourceUnavailable) as error,
			):
				self.read("Receivable")
			self.assertEqual(error.exception.code, "restricted_obligations")
			report.assert_not_called()
			self.assertFalse(self.read("Payable")["complete_for_company"])
			return
		result = self.read("Receivable")
		self.assertEqual(result["rows"], [])
		self.assertFalse(result["complete_for_company"])

	def test_zero_payment_placeholders_do_not_exhaust_the_invoice_limit(self):
		invoice = self.fx.invoice((100,))
		for index, amount in enumerate((1, -1, 1, -1)):
			self.fx.ple("History" + str(index), "Payment Entry", invoice, amount)
		with patch("jarvis.payment_calendar.sources.MAX_VOUCHERS", 1):
			self.assertEqual(Decimal(self.read()["canonical_total"]), 100)

	def test_obligations_refuse_a_non_default_book_without_reading_bank_accounts(self):
		self.fx.invoice()
		frappe.db.set_value(
			"Payment Ledger Entry", self.fx.prefix + "InvoicePLE1", "finance_book", "Alternative book"
		)
		with self.assertRaises(frappe.ValidationError):
			self.read()

	def test_default_blank_and_null_books_have_identical_obligation_semantics(self):
		default = self.fx.insert("Finance Book", "Book", finance_book_name="Calendar default")
		frappe.db.set_value("Company", self.fx.company, "default_finance_book", default)
		for index, book in enumerate((None, "", default), 1):
			self.fx.invoice((100,))
			frappe.db.set_value(
				"Payment Ledger Entry", self.fx.prefix + "InvoicePLE" + str(index), "finance_book", book
			)
		self.assertEqual(Decimal(self.read()["canonical_total"]), 300)

	def test_settled_schedule_drift_is_unavailable_instead_of_losing_a_paid_day(self):
		self.fx.invoice(paid=100, advance=20)
		with self.assertRaises(frappe.ValidationError):
			self.read()

	def test_allocated_advance_uses_the_actual_net_payment_schedule(self):
		invoice = self.fx.invoice((30, 50), gross_total=100, paid=100, advance=20)
		rows = [r for r in self.read()["rows"] if r["voucher_no"] == invoice]
		self.assertEqual(sum(Decimal(r["original"]) for r in rows), 80)
		self.assertEqual(sum(Decimal(r["remaining"]) for r in rows), 0)
		self.assertTrue(all(r["settlement"] == "Settled" for r in rows))

	def test_unallocated_advance_is_a_signed_adjustment_not_new_cash(self):
		self.fx.insert(
			"Payment Ledger Entry",
			"UnallocatedAdvance",
			company=self.fx.company,
			posting_date=self.fx.today,
			account_type="Payable",
			account=self.fx.payable,
			account_currency="INR",
			party_type="Supplier",
			party=self.fx.supplier,
			voucher_type="Payment Entry",
			voucher_no=self.fx.prefix + "Advance",
			against_voucher_type="Payment Entry",
			against_voucher_no=self.fx.prefix + "Advance",
			amount=-25,
			amount_in_account_currency=-25,
			delinked=0,
		)
		result = self.read()
		self.assertEqual(Decimal(result["canonical_total"]), -25)
		self.assertEqual(result["rows"][0]["kind"], "adjustment")
		self.assertEqual(Decimal(result["rows"][0]["remaining"]), -25)

	def test_missing_dates_are_not_silently_replaced_with_today(self):
		with self.assertRaises(frappe.ValidationError):
			read_obligations(self.fx.company, "Payable", None, self.fx.today, self.fx.today)

	def test_real_disabled_report_is_refused_before_engine_creation(self):
		old = frappe.db.get_value("Report", "Accounts Payable", "disabled")
		frappe.db.set_value("Report", "Accounts Payable", "disabled", 1)
		try:
			with patch(
				"jarvis.payment_calendar.sources._report_class",
				side_effect=AssertionError("Engine must not run"),
			):
				with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
					self.read()
		finally:
			frappe.db.set_value("Report", "Accounts Payable", "disabled", old)

	def test_missing_runtime_attribute_has_the_explicit_contract_error(self):
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		from jarvis.payment_calendar.sources import CalendarSourceUnavailable

		self.fx.invoice()
		with patch.object(
			ReceivablePayableReport, "get_chart_data", lambda report: delattr(report, "columns")
		):
			with self.assertRaises(CalendarSourceUnavailable):
				self.read()

	def test_invalid_upstream_fetch_setting_never_becomes_no_dues(self):
		from jarvis.payment_calendar.sources import CalendarSourceUnavailable

		self.fx.invoice()
		frappe.db.set_single_value("Accounts Settings", "receivable_payable_fetch_method", "Unknown Cursor")
		frappe.clear_document_cache("Accounts Settings")
		with self.assertRaises(CalendarSourceUnavailable):
			self.read()

	def test_missing_standard_party_mapping_is_refused_before_the_report(self):
		from jarvis.payment_calendar.sources import CalendarSourceUnavailable

		self.fx.invoice()
		frappe.db.delete("Party Type", {"name": "Supplier"})
		with patch(
			"jarvis.payment_calendar.sources._report_class",
			side_effect=AssertionError("Unscoped report must not run"),
		):
			with self.assertRaises(CalendarSourceUnavailable):
				self.read()

	def test_missing_payable_account_is_not_replaced_by_receivables(self):
		from jarvis.payment_calendar.sources import CalendarSourceUnavailable

		self.fx.invoice(side="Receivable")
		frappe.db.delete("Account", {"name": self.fx.payable})
		with patch(
			"jarvis.payment_calendar.sources._report_class",
			side_effect=AssertionError("Unscoped report must not run"),
		):
			with self.assertRaises(CalendarSourceUnavailable):
				self.read()

	def test_payables_and_receivables_cannot_cross_company_or_side(self):
		self.fx.invoice((100,))
		self.fx.invoice((200,), side="Receivable")
		other = CalendarFixtures()
		other.invoice((300,))
		self.assertEqual(Decimal(self.read()["canonical_total"]), 100)
		self.assertEqual(Decimal(self.read("Receivable")["canonical_total"]), 200)

	def test_unbuffered_source_preserves_the_same_allocation(self):
		self.fx.invoice(paid=30)
		buffered = self.read()
		frappe.db.set_single_value(
			"Accounts Settings", "receivable_payable_fetch_method", "UnBuffered Cursor"
		)
		frappe.clear_document_cache("Accounts Settings")
		self.assertEqual(
			frappe.db.get_single_value("Accounts Settings", "receivable_payable_fetch_method", cache=False),
			"UnBuffered Cursor",
		)
		unbuffered = self.read()
		self.assertEqual(unbuffered, buffered)

	def test_cost_center_scope_hides_settled_rows_and_denies_unscoped_bank_cash(self):
		from jarvis.exceptions import PermissionDeniedError
		from jarvis.payment_calendar.banks import read_bank_balances

		allowed = self.fx.insert(
			"Cost Center",
			"AllowedCC",
			cost_center_name="Calendar allowed",
			company=self.fx.company,
			is_group=0,
			lft=1,
			rgt=2,
		)
		other = self.fx.insert(
			"Cost Center",
			"OtherCC",
			cost_center_name="Calendar hidden",
			company=self.fx.company,
			is_group=0,
			lft=3,
			rgt=4,
		)
		visible, hidden = self.fx.invoice(), self.fx.invoice(paid=100)
		for invoice, center in ((visible, allowed), (hidden, other)):
			frappe.db.set_value("Purchase Invoice", invoice, "cost_center", center)
			frappe.db.set_value(
				"Payment Ledger Entry",
				{"company": self.fx.company, "against_voucher_no": invoice},
				"cost_center",
				center,
			)
		frappe.set_user(self._restricted_user("Cost Center", allowed))
		result = self.read()
		self.assertFalse(result["complete_for_company"])
		self.assertTrue(any(r["voucher_no"] == visible for r in result["rows"]))
		self.assertFalse(any(r["voucher_no"] == hidden for r in result["rows"]))
		with self.assertRaises(PermissionDeniedError):
			read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)

	def _restricted_user(self, doctype, name):
		email = self.fx.prefix.lower() + "@example.com"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Calendar test",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
				"roles": [{"role": "Accounts Manager"}],
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": email,
				"allow": doctype,
				"for_value": name,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=email)
		return email


def speed_path(name):
	"""Skip a fast-path test when the installed ERPNext source of `name` is not pinned."""

	def decorate(test):
		@wraps(test)
		def wrapper(self):
			if not compatibility.upstream_matches(name):
				self.skipTest("speed path inactive on this ERPNext source")
			return test(self)

		return wrapper

	return decorate


requires_speed_path = speed_path("get_payment_terms")


class EngineCase(TestCase):
	"""A real `CalendarReport` over fixtures that roll back; reads are asserted below the public API."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_case")
		self.fx = CalendarFixtures()

	def tearDown(self):
		frappe.db.rollback(save_point="calendar_case")

	def engine(self, report_date=None):
		filters = {
			"company": self.fx.company,
			"report_date": report_date or self.fx.today,
			"based_on_payment_terms": 1,
			"ageing_based_on": "Due Date",
			"range": "30,60,90,120",
			"group_by_party": 0,
			"ignore_accounts": 0,
			"in_party_currency": 0,
			"finance_book": None,
		}
		return sources._report_class()(filters, self.fx.today, add_days(self.fx.today, 40))

	def run_engine(self, side="Payable"):
		engine = self.engine()
		engine.filters.update(show_future_payments=0, for_revaluation_journals=0)
		naming = (
			["Buying Settings", "supp_master_name"]
			if side == "Payable"
			else ["Selling Settings", "cust_master_name"]
		)
		engine.run({"account_type": side, "naming_by": naming})
		return engine


class TestBatchedPaymentTerms(EngineCase):
	def row(self, name, doctype="Purchase Invoice", *, invoiced=100, paid=30):
		return frappe._dict(
			voucher_type=doctype,
			voucher_no=name,
			party=self.fx.supplier,
			party_account=self.fx.payable,
			invoiced=invoiced,
			paid=paid,
			credit_note=0,
		)

	def register(self, engine, rows):
		engine.voucher_balance = {(r.party_account, r.voucher_type, r.voucher_no, r.party): r for r in rows}

	def upstream_terms(self, engine, row):
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		expected = deepcopy(row)
		ReceivablePayableReport.get_payment_terms(engine, expected)
		return expected

	def fixture_rows(self):
		fx = self.fx
		multi = fx.invoice((40, 60))
		single = fx.invoice((100,))
		frappe.db.set_value("Payment Schedule", {"parent": single}, "payment_term", "Single term")
		advance = fx.invoice((100,), advance=20)
		foreign = fx.invoice((100,), advance=20)
		frappe.db.set_value(
			"Purchase Invoice",
			foreign,
			{"currency": "USD", "party_account_currency": "USD", "conversion_rate": 80},
		)
		credit_note = fx.invoice((100,), schedules=False)
		frappe.db.set_value("Purchase Invoice", credit_note, "is_return", 1)
		returned = fx.invoice((40, 60))
		frappe.db.set_value("Purchase Invoice", returned, "is_return", 1)
		receivable = fx.invoice((100,), side="Receivable")
		rows = [self.row(n) for n in (multi, single, advance, foreign, credit_note, returned)]
		return rows + [self.row(receivable, "Sales Invoice")]

	@requires_speed_path
	def test_batched_terms_equal_upstream_per_invoice(self):
		engine = self.engine()
		rows = self.fixture_rows()
		self.register(engine, rows)
		with_terms = 0
		for row in rows:
			expected = self.upstream_terms(engine, row)
			actual = deepcopy(row)
			engine.get_payment_terms(actual)
			with_terms += bool(expected.payment_terms)
			self.assertEqual(actual.payment_terms, expected.payment_terms, row.voucher_no)
			self.assertEqual(actual.paid, expected.paid, row.voucher_no)
		# Credit note and return invoice have no terms; the other five do.
		self.assertEqual(with_terms, 5)

	@requires_speed_path
	def test_batched_terms_follow_historical_order(self):
		invoice = self.fx.invoice((40, 60), schedule_paid={1: 50})
		row = self.row(invoice)
		for report_date, expected_order in (
			(add_days(self.fx.today, -1), (10, 20)),
			(self.fx.today, (20, 10)),
		):
			engine = self.engine(report_date)
			actual = deepcopy(row)
			engine.get_payment_terms(actual)
			expected = [getdate(add_days(self.fx.today, days)) for days in expected_order]
			self.assertEqual([t.due_date for t in actual.payment_terms], expected)

	@requires_speed_path
	def test_batched_terms_idx_breaks_ties(self):
		invoice = self.fx.invoice((40, 60))
		same_day = add_days(self.fx.today, 10)
		frappe.db.set_value("Payment Schedule", {"parent": invoice}, "due_date", same_day)
		frappe.db.set_value("Payment Schedule", {"parent": invoice, "payment_amount": 40}, "idx", 2)
		frappe.db.set_value("Payment Schedule", {"parent": invoice, "payment_amount": 60}, "idx", 1)
		actual = self.row(invoice)
		self.engine().get_payment_terms(actual)
		self.assertEqual([t.invoiced for t in actual.payment_terms], [60, 40])

	@requires_speed_path
	def test_schedule_parent_in_another_case_maps_back_on_mariadb(self):
		if frappe.db.db_type != "mariadb":
			self.skipTest("only MariaDB matches the IN list case-insensitively")
		invoice = self.fx.invoice((40, 60))
		cased = invoice.swapcase()
		self.assertNotEqual(cased, invoice)
		row = self.row(cased)
		engine = self.engine()
		self.register(engine, [row])
		expected = self.upstream_terms(engine, row)
		self.assertTrue(expected.payment_terms)
		actual = deepcopy(row)
		engine.get_payment_terms(actual)
		self.assertEqual(actual.payment_terms, expected.payment_terms)
		self.assertEqual(len(engine._term_cache[("Purchase Invoice", cased)]), 2)

	@requires_speed_path
	def test_single_term_shortcut_matches_upstream_including_row_and_aliasing(self):
		invoice = self.fx.invoice((100,), advance=20, schedule_paid={0: 30})
		frappe.db.set_value("Payment Schedule", {"parent": invoice}, "payment_term", "Single term")
		engine = self.engine()
		row = self.row(invoice)
		self.register(engine, [row])
		expected = self.upstream_terms(engine, row)
		actual = deepcopy(row)
		engine.get_payment_terms(actual)
		self.assertEqual(len(actual.payment_terms), 1)
		# Every row field read later (paid, invoiced, ...) and the term itself equal upstream's.
		self.assertEqual(dict(actual), dict(expected))
		# Aliasing as upstream: the lone term is its own object, so editing it never edits the row.
		self.assertIsNot(actual.payment_terms[0], actual)
		self.assertIsNot(expected.payment_terms[0], expected)
		for result in (actual, expected):
			result.payment_terms[0].outstanding = -1
		self.assertEqual(actual.get("outstanding"), expected.get("outstanding"))

	def test_allocation_stores_a_copy_of_the_terms(self):
		invoice = self.fx.invoice((40, 60))
		engine = self.engine()
		row = self.row(invoice)
		self.register(engine, [row])
		engine.allocate_outstanding_based_on_payment_terms(row)
		stored = engine.terms[sources._key(row)]
		self.assertEqual(len(stored), 2)
		self.assertEqual(stored, row.payment_terms)
		for kept, term in zip(stored, row.payment_terms, strict=True):
			self.assertIsNot(kept, term)
		before = deepcopy(stored)
		row.payment_terms[0].outstanding = 12345
		row.payment_terms[0].due_date = None
		self.assertEqual(engine.terms[sources._key(row)], before)

	@requires_speed_path
	def test_batched_terms_query_count_is_per_chunk(self):
		real = [self.fx.invoice((40, 60)) for _ in range(3)]
		ghosts = [self.fx.prefix + "Ghost" + str(i) for i in range(1197)]
		rows = [self.row(name) for name in real + ghosts]
		engine = self.engine()
		self.register(engine, rows)
		queries, reads = [], []
		sql, get_value = frappe.db.sql, frappe.get_value

		def spy_sql(query, *args, **kwargs):
			queries.append(str(query))
			return sql(query, *args, **kwargs)

		def spy_get_value(doctype, *args, **kwargs):
			reads.append(doctype)
			return get_value(doctype, *args, **kwargs)

		with patch.object(frappe.db, "sql", spy_sql), patch.object(frappe, "get_value", spy_get_value):
			for row in rows:
				engine.get_payment_terms(row)
		schedule_queries = [q for q in queries if "Payment Schedule" in q]
		self.assertEqual(len(schedule_queries), 3)
		self.assertEqual(reads.count("Company"), 1)
		largest = max(len(names.split(",")) for q in schedule_queries for names in IN_LIST.findall(q))
		self.assertLessEqual(largest, 500)

	@requires_speed_path
	def test_prefetch_includes_return_invoices_but_not_journals(self):
		invoice = self.fx.invoice((40, 60))
		returned = self.fx.invoice((40, 60))
		frappe.db.set_value("Purchase Invoice", returned, "is_return", 1)
		journal = self.fx.prefix + "Journal"
		rows = [self.row(invoice), self.row(returned), self.row(journal, "Journal Entry")]
		engine = self.engine()
		self.register(engine, rows)
		engine.get_payment_terms(deepcopy(rows[0]))
		self.assertEqual(engine._term_cache[("Purchase Invoice", returned)], [])
		# The prefetch queries Purchase Invoice only; a journal must not be cached under any type.
		self.assertNotIn(journal, {name for _, name in engine._term_cache})
		self.assertEqual({doctype for doctype, _ in engine._term_cache}, {"Purchase Invoice"})

	def test_unknown_source_falls_back(self):
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		if source_profile().major != "16":
			self.skipTest("the fallback warning is v16 only")
		invoice = self.fx.invoice((40, 60))
		engine = self.engine()
		sources._report_class()
		self.addCleanup(self.reset_fast_path)
		self.reset_fast_path()
		upstream, calls = ReceivablePayableReport.get_payment_terms, []

		@wraps(upstream)  # inspect.getsource follows __wrapped__, so the installed hash stays readable
		def spy(*args):
			calls.append(args)
			return upstream(*args)

		with (
			patch.object(compatibility, "upstream_matches", return_value=False),
			patch.object(ReceivablePayableReport, "get_payment_terms", spy),
			patch.object(frappe, "logger") as logger,
		):
			engine.get_payment_terms(self.row(invoice))
			engine.get_payment_terms(self.row(invoice))
		self.assertEqual(len(calls), 2)
		logger.return_value.error.assert_called_once()
		line = logger.return_value.error.call_args.args[0]
		self.assertIn("get_payment_terms", line)
		self.assertIn(compatibility.installed_hash("get_payment_terms"), line)
		self.assertIs(sources.FAST_PATH, False)

	def reset_fast_path(self):
		sources.FAST_PATH = True
		sources._warned_fallbacks.clear()

	def test_pinned_hashes_match_installed_source(self):
		# Deliberate tripwire: it fails on a drifted v16 source so the pin gets audited and bumped.
		if source_profile().major != "16":
			self.skipTest("pins exist for v16 only")
		for name in ("get_payment_terms",):
			actual = compatibility.installed_hash(name)
			self.assertIn(
				actual,
				compatibility._SPEED_PINS["16"].get(name, ()),
				f"ERPNext {name} changed (hash {actual}): audit the diff and add the hash to _SPEED_PINS",
			)

	def test_upstream_matches_is_false_for_an_unknown_major_or_hash(self):
		pins = {"16": {"get_payment_terms": (compatibility.installed_hash("get_payment_terms"),)}}
		with patch.object(compatibility, "_SPEED_PINS", pins):
			with patch("erpnext.__version__", "16.0.0"):
				self.assertTrue(compatibility.upstream_matches("get_payment_terms"))
				self.assertFalse(compatibility.upstream_matches("get_unpinned_method"))
			with patch("erpnext.__version__", "17.0.0"):
				self.assertFalse(compatibility.upstream_matches("get_payment_terms"))
		with patch.object(compatibility, "_SPEED_PINS", {"16": {"get_payment_terms": ("unknown",)}}):
			with patch("erpnext.__version__", "16.0.0"):
				self.assertFalse(compatibility.upstream_matches("get_payment_terms"))

	def test_upstream_matches_is_false_when_the_source_is_unreadable(self):
		compatibility._hashes.clear()
		self.addCleanup(compatibility._hashes.clear)
		pins = {"16": {"get_payment_terms": ("anything",)}}
		with (
			patch.object(compatibility, "_SPEED_PINS", pins),
			patch("erpnext.__version__", "16.0.0"),
			patch.object(compatibility, "getsource", side_effect=OSError),
		):
			self.assertFalse(compatibility.upstream_matches("get_payment_terms"))
			self.assertIsNone(compatibility.installed_hash("get_payment_terms"))


class TestSpeedFallbackLog(EngineCase):
	"""The fallback line must reach an ERROR-level log (production logs at ERROR) and never raise into a read."""

	def setUp(self):
		super().setUp()
		self.addCleanup(self.reset_fast_path)
		self.reset_fast_path()

	def reset_fast_path(self):
		sources.FAST_PATH = True
		sources._warned_fallbacks.clear()

	def test_speed_fallback_logs_one_error_line(self):
		with patch.object(frappe, "logger") as logger:
			read_log.speed_fallback("get_payment_terms", "abc123", "16.55.0")
		logger.assert_called_once_with("payment_calendar")
		logger.return_value.error.assert_called_once_with(
			"speed_fallback method=get_payment_terms hash=abc123 erpnext=16.55.0"
		)
		logger.return_value.warning.assert_not_called()

	def test_speed_fallback_logs_none_for_an_unreadable_hash(self):
		with patch.object(frappe, "logger") as logger:
			read_log.speed_fallback("get_payment_terms", None, "16.55.0")
		line = logger.return_value.error.call_args.args[0]
		self.assertEqual(line, "speed_fallback method=get_payment_terms hash=none erpnext=16.55.0")

	def test_speed_fallback_never_raises(self):
		with patch.object(frappe, "logger", side_effect=RuntimeError("logging is down")):
			read_log.speed_fallback("get_payment_terms", "abc123", "16.55.0")

	def test_a_failing_hash_lookup_or_logger_never_reaches_the_read(self):
		if source_profile().major != "16":
			self.skipTest("the fallback line is v16 only")
		failures = {
			"hash lookup": patch.object(
				compatibility, "installed_hash", side_effect=RuntimeError("no source")
			),
			"logger": patch.object(read_log, "speed_fallback", side_effect=RuntimeError("no logger")),
		}
		for label, failure in failures.items():
			self.reset_fast_path()
			with self.subTest(label), failure:
				sources._speed_fallback("get_payment_terms")
			self.assertIs(sources.FAST_PATH, False)

	def test_the_line_is_logged_once_per_method_and_only_on_v16(self):
		with patch.object(frappe, "logger") as logger, patch("erpnext.__version__", "16.55.0"):
			sources._speed_fallback("get_payment_terms")
			sources._speed_fallback("get_payment_terms")
			sources._speed_fallback("get_other_method")
		self.assertEqual(logger.return_value.error.call_count, 2)
		self.reset_fast_path()
		with patch.object(frappe, "logger") as logger, patch("erpnext.__version__", "15.125.0"):
			sources._speed_fallback("get_payment_terms")
		logger.return_value.error.assert_not_called()
		self.assertIs(sources.FAST_PATH, False)


class TestUnreadableHashIsNotCached(EngineCase):
	def test_a_none_hash_is_recomputed_not_cached(self):
		compatibility._hashes.clear()
		self.addCleanup(compatibility._hashes.clear)
		with patch.object(compatibility, "getsource", side_effect=OSError):
			self.assertIsNone(compatibility._upstream_hash("get_payment_terms", "16"))
		self.assertEqual(compatibility._hashes, {})
		real = compatibility.installed_hash("get_payment_terms")
		self.assertEqual(compatibility._upstream_hash("get_payment_terms", "16"), real)
		self.assertEqual(compatibility._hashes, {("get_payment_terms", "16"): real})


class TestVoucherBalanceGate(EngineCase):
	"""build_data refuses unless ERPNext still creates the accumulator the batched reads scan."""

	def test_an_engine_that_never_built_the_accumulator_refuses(self):
		engine = self.engine()
		self.assertIsNone(engine.voucher_balance)
		with self.assertRaises(CalendarSourceUnavailable):
			engine.build_data()

	@no_oracle_compare("patches the upstream class both readers inherit, so the oracle would not refuse")
	def test_a_renamed_accumulator_refuses_the_read(self):
		self.fx.invoice((40, 60))

		def get_data_with_renamed_accumulator(report):
			# What upstream's get_data sets up, except that the accumulator has another name.
			report.data, report.invoice_details, report.err_journals = [], {}, []
			report.accumulator = {}
			report.build_data()

		with patch.object(ReceivablePayableReport, "get_data", get_data_with_renamed_accumulator):
			with self.assertRaises(CalendarSourceUnavailable):
				read_obligations(
					self.fx.company, "Payable", self.fx.today, self.fx.today, add_days(self.fx.today, 40)
				)


class TestLedgerOrder(EngineCase):
	def tied_lines(self):
		"""Two self-referencing lines of one journal, same day and party; the greater name goes in first."""
		journal = self.fx.prefix + "Journal"
		for suffix, project in (("TieB", "Project B"), ("TieA", "Project A")):
			self.fx.insert(
				"Payment Ledger Entry",
				suffix,
				company=self.fx.company,
				posting_date=self.fx.today,
				account_type="Payable",
				account=self.fx.payable,
				account_currency="INR",
				party_type="Supplier",
				party=self.fx.supplier,
				voucher_type="Journal Entry",
				voucher_no=journal,
				against_voucher_type="Journal Entry",
				against_voucher_no=journal,
				amount=50,
				amount_in_account_currency=50,
				delinked=0,
				project=project,
			)
		return (self.fx.payable, "Journal Entry", journal, self.fx.supplier)

	def order_keys(self, engine):
		order = str(engine.ple_query).rsplit("ORDER BY", 1)[1]
		return order.replace('"', "").replace("`", "").replace(" ", "").split(",")

	def test_tied_ledger_rows_follow_name(self):
		if source_profile().major != "16":
			self.skipTest("the name order key is v16 only")
		key = self.tied_lines()
		engine = self.run_engine()
		self.assertEqual(self.order_keys(engine)[-1], "name")
		self.assertEqual(engine.voucher_balance[key].project, "Project B")

	def test_v15_profile_keeps_upstream_order(self):
		self.tied_lines()
		with patch.object(sources, "source_profile", return_value=SimpleNamespace(major="15")):
			engine = self.run_engine()
		self.assertNotIn("name", self.order_keys(engine))


class TestBatchedPartyDetails(EngineCase):
	def party_engine(self, side, parties):
		engine = self.engine()
		engine.account_type, engine.party_details = side, {}
		doctype = "Purchase Invoice" if side == "Payable" else "Sales Invoice"
		account = self.fx.payable if side == "Payable" else self.fx.receivable
		engine.voucher_balance = {
			(account, doctype, f"V{index}", party): frappe._dict(
				voucher_type=doctype, voucher_no=f"V{index}", party=party, party_account=account
			)
			for index, party in enumerate(parties)
		}
		return engine

	def spy_queries(self):
		queries, sql = [], frappe.db.sql

		def spy(query, *args, **kwargs):
			queries.append(str(query))
			return sql(query, *args, **kwargs)

		return queries, patch.object(frappe.db, "sql", spy)

	def seeded(self, side, parties):
		engine = self.party_engine(side, parties)
		engine._seed_party_details()
		return engine

	def upstream_details(self, side, parties):
		"""Upstream's own lazy lookups, one party at a time, on a fresh engine."""
		engine = self.party_engine(side, parties)
		details = [ReceivablePayableReport.get_party_details(engine, party) for party in parties]
		return engine, details

	def test_seeded_party_details_equal_upstream(self):
		fx = self.fx
		employee = fx.insert("Employee", "Employee", employee_name="Only employee")
		missing = fx.prefix + "Missing"
		cases = {
			"Payable": [fx.supplier, employee, missing, fx.supplier, fx.customer],
			"Receivable": [fx.customer, fx.supplier, missing, employee],
		}
		for side, parties in cases.items():
			with self.subTest(side):
				seeded = self.seeded(side, parties)
				upstream, expected = self.upstream_details(side, parties)
				queries, spy = self.spy_queries()
				with spy:
					# The inherited upstream method must now only read the dict.
					actual = [seeded.get_party_details(party) for party in parties]
				self.assertEqual(queries, [])
				self.assertEqual(actual, expected)
				# One live party; Payable also has no Employee lookup, so the employee is None there.
				self.assertEqual(actual.count(None), len(parties) - 1 - (side == "Payable" and 1), side)
				self.assertEqual(seeded.party_details, upstream.party_details)

	def test_seed_stores_none_for_a_missing_party(self):
		missing = self.fx.prefix + "Missing"
		seeded = self.seeded("Payable", [missing])
		self.assertIn(missing, seeded.party_details)
		self.assertIsNone(seeded.party_details[missing])

	def test_seed_does_not_overwrite_an_existing_entry(self):
		engine = self.party_engine("Payable", [self.fx.supplier])
		engine.party_details[self.fx.supplier] = frappe._dict(supplier_name="kept")
		engine._seed_party_details()
		self.assertEqual(engine.party_details[self.fx.supplier].supplier_name, "kept")

	def test_party_details_match_case_insensitively_on_mariadb(self):
		if frappe.db.db_type != "mariadb":
			self.skipTest("only MariaDB compares party ids case-insensitively")
		cased = self.fx.supplier.swapcase()
		self.assertNotEqual(cased, self.fx.supplier)
		seeded = self.seeded("Payable", [cased])
		_, (expected,) = self.upstream_details("Payable", [cased])
		self.assertIsNotNone(expected)
		self.assertEqual(seeded.party_details[cased], expected)
		self.assertEqual(seeded.get_party_details(cased), expected)

	def test_party_details_query_count_is_per_chunk(self):
		ghosts = [self.fx.prefix + "Ghost" + str(i) for i in range(599)]
		parties = [self.fx.supplier, *ghosts]
		engine = self.party_engine("Payable", parties)
		queries, spy = self.spy_queries()
		with spy:
			engine._seed_party_details()
		self.assertEqual(len([q for q in queries if "tabSupplier" in q]), 2)
		with spy:
			first = [engine.get_party_details(party) for party in parties]
			repeated = [engine.get_party_details(party) for party in parties]
		self.assertEqual(first, repeated)
		self.assertEqual(len([q for q in queries if "tabSupplier" in q]), 2, "reading the seed queried again")

	def test_query_count_per_side_through_a_read(self):
		if source_profile().major != "16":
			self.skipTest("the party seed is v16 only")
		for side in ("Payable", "Receivable"):
			with self.subTest(side):
				self.fx.invoice((40, 60), side=side)
				queries, spy = self.spy_queries()
				with spy:
					engine = self.run_engine(side)
				party_table = "tabSupplier" if side == "Payable" else "tabCustomer"
				lookups = [q for q in queries if party_table in q]
				self.assertEqual(len(lookups), 1, "one chunk query for the side, no per-party lookup")
				self.assertIn("`name` IN", lookups[0].replace('"', "`"))
				self.assertTrue(engine.party_details)

	def test_seed_runs_after_set_defaults_and_only_on_v16(self):
		self.fx.invoice((40, 60))
		for major, seeds in (("16", 1), ("15", 0)):
			with self.subTest(major):
				seed = sources._report_class()._seed_party_details
				with (
					patch.object(sources, "source_profile", return_value=SimpleNamespace(major=major)),
					patch.object(
						sources._report_class(), "_seed_party_details", autospec=True, side_effect=seed
					) as spy,
				):
					engine = self.run_engine()
				self.assertEqual(spy.call_count, seeds)
				self.assertTrue(engine.party_details)

	def withholding(self):
		invoice = self.fx.invoice((90,), gross_total=100)
		frappe.db.set_value(
			"Purchase Invoice", invoice, {"grand_total": 90, "base_grand_total": 90, "apply_tds": 1}
		)
		self.fx.insert(
			"Purchase Taxes and Charges",
			"TDS",
			parent=invoice,
			parenttype="Purchase Invoice",
			parentfield="taxes",
			is_tax_withholding_account=1,
			base_tax_amount_after_discount_amount=10,
		)
		self.fx.ple("TDS_PLE", "Purchase Invoice", invoice, -10, voucher_no=invoice)
		return invoice

	@requires_speed_path  # the shared term cache is filled by the batched override only
	@no_oracle_compare("reads the real engine back from the native spy, and compares with the oracle itself")
	def test_native_clone_with_shared_caches(self):
		repaired = self.withholding()
		plain = self.fx.invoice((40, 60), paid=30)
		args = (self.fx.company, "Payable", self.fx.today, self.fx.today, add_days(self.fx.today, 40))
		queries, spy = self.spy_queries()
		with (
			spy,
			patch.object(
				InvoiceReconciler, "native", autospec=True, side_effect=InvoiceReconciler.native
			) as native,
		):
			actual = read_obligations(*args)
		expected = oracle_read_obligations(*args)
		self.assertTrue(native.called)
		engine = native.call_args.args[0].engine
		# The clone shares the seeded dict, so the repair rebuild adds no party lookup of its own.
		self.assertTrue(engine.party_details)
		self.assertLessEqual(
			len([q for q in queries if "tabSupplier" in q and "`name` IN" in q.replace('"', "`")]), 2
		)
		self.assertEqual(
			len([q for q in queries if "tabSupplier" in q and "`name` IN" not in q.replace('"', "`")]), 0
		)
		self.assertTrue(engine._term_cache)
		self.assertEqual({r["voucher_no"] for r in actual["rows"]}, {repaired, plain})
		for result in (actual, expected):
			for row in result["rows"]:
				row.pop("id")
		self.assertEqual(actual, expected)


class TestRepairRebuild(EngineCase):
	"""build_data over synthetic rows: a fake upstream build and a fake reconciler isolate the rebuild."""

	def synthetic(self, repairs=300, normal=300):
		"""Interleaved normal and repair keys; each repair key is True in the returned map."""
		engine = self.engine()
		engine.currency_precision, engine.err_journals = 2, ["JV-1", "JV-2"]
		engine.account_type, engine.party_details = "Payable", {}
		engine.voucher_balance, engine.invoice_details = {}, {}
		keys = {}
		for index in range(max(normal, repairs)):
			for repair, count in ((False, normal), (True, repairs)):
				if index >= count:
					continue
				name = f"INV{'R' if repair else 'N'}{index:04d}"
				key = ("Creditors", "Purchase Invoice", name, "Supplier")
				engine.voucher_balance[key] = frappe._dict(
					voucher_type="Purchase Invoice",
					voucher_no=name,
					party="Supplier",
					party_account="Creditors",
					invoiced=100,
					paid=0,
					credit_note=0,
				)
				keys[key] = repair
		return engine, keys

	def run_build(self, engine, keys, spy=None, excluded=()):
		"""Run build_data. The fake upstream lists the rows in reverse, with a stale row per repair
		key, and gives the unrepaired keys their terms. `spy(report)` runs after it. The fake
		reconciler records each repair and the data list it saw; it reports the keys in `excluded`
		as excluded_fx, which stay in `data`. Returns (repaired, data seen)."""
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		repaired, seen = {}, []

		def upstream_build(report):
			report.data = [
				frappe._dict(
					party_account=k[0],
					voucher_type=k[1],
					voucher_no=k[2],
					party=k[3],
					outstanding=50 if repair else 100,
				)
				for k, repair in reversed(list(keys.items()))
			]
			for key, repair in keys.items():
				if not repair:
					report.terms[key] = []
			if spy:
				spy(report)

		class Reconciler:
			def __init__(self, report, money, key):
				self.key = key

			def excluded_fx(self, row, present):
				return self.key(row) in excluded

			def native(self, row, force=False):
				return []

			def repair(self, row, expected, *, settled=False):
				replacement = frappe._dict(
					party_account=row.party_account,
					voucher_type=row.voucher_type,
					voucher_no=row.voucher_no,
					party=row.party,
					outstanding=expected,
					repaired=True,
				)
				repaired[self.key(row)] = replacement
				seen.append(engine.data)
				return replacement

		with (
			patch.object(sources, "InvoiceReconciler", Reconciler),
			patch.object(ReceivablePayableReport, "build_data", upstream_build),
		):
			engine.build_data()
		return repaired, seen

	@staticmethod
	def per_key_rebuild(data, replacements):
		"""Today's loop: filter the whole list once for every repaired key."""
		for key, replacement in replacements.items():
			data = [r for r in data if sources._key(r) != key] + [replacement]
		return data

	def test_many_repairs_keep_todays_order(self):
		engine, keys = self.synthetic()
		original = []
		repaired, _ = self.run_build(engine, keys, lambda report: original.extend(report.data))
		self.assertEqual(len(repaired), sum(keys.values()))
		expected = self.per_key_rebuild(original, repaired)
		self.assertEqual([sources._key(r) for r in engine.data], [sources._key(r) for r in expected])
		self.assertEqual([r.get("repaired") for r in engine.data], [r.get("repaired") for r in expected])
		for key in repaired:
			self.assertEqual(engine.terms[key], [])

	def test_excluded_fx_rows_keep_their_place_among_survivors(self):
		engine, keys = self.synthetic(repairs=40, normal=40)
		# Every fourth repair key is excluded_fx: it stays in data, unrepaired, at its own position.
		excluded = {
			key for index, key in enumerate(k for k, repair in keys.items() if repair) if index % 4 == 0
		}
		original = []
		repaired, _ = self.run_build(
			engine, keys, lambda report: original.extend(report.data), excluded=excluded
		)
		self.assertEqual(set(repaired), {k for k, repair in keys.items() if repair} - excluded)
		expected = self.per_key_rebuild(original, repaired)
		self.assertEqual([sources._key(r) for r in engine.data], [sources._key(r) for r in expected])
		self.assertEqual([r.get("repaired") for r in engine.data], [r.get("repaired") for r in expected])
		survivors = [sources._key(r) for r in engine.data if not r.get("repaired")]
		self.assertEqual(
			[k for k in survivors if k in excluded],
			[sources._key(r) for r in original if sources._key(r) in excluded],
		)
		self.assertTrue(excluded)
		for key in excluded:
			self.assertIn(key, survivors)

	def test_repair_rebuild_is_one_pass(self):
		engine, keys = self.synthetic(repairs=3000, normal=0)
		original = []
		_, seen = self.run_build(engine, keys, lambda report: original.append(report.data))
		self.assertEqual(len(seen), 3000)
		# No repair saw a rebuilt list; the single rebuild came after the loop.
		self.assertTrue(all(data is original[0] for data in seen))
		self.assertIsNot(engine.data, original[0])

	def test_v15_profile_rebuilds_per_key(self):
		engine, keys = self.synthetic(repairs=20, normal=0)
		original = []
		with patch.object(sources, "source_profile", return_value=SimpleNamespace(major="15")):
			_, seen = self.run_build(engine, keys, lambda report: original.append(report.data))
		# Today's loop rebuilds the list after every repair: each repair sees a new list.
		self.assertIs(seen[0], original[0])
		self.assertEqual(len({id(data) for data in seen}), 20)
		self.assertEqual(len(engine.data), 20)

	def test_allocation_reads_the_version_gate_once_per_build(self):
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		engine, keys = self.synthetic(repairs=0, normal=3)
		rows = [
			frappe._dict(party_account=k[0], voucher_type=k[1], voucher_no=k[2], party=k[3]) for k in keys
		]
		for row in rows:
			row.payment_terms = []
		profile = patch.object(sources, "source_profile", return_value=SimpleNamespace(major="16"))
		with (
			patch.object(ReceivablePayableReport, "allocate_outstanding_based_on_payment_terms"),
			profile as gate,
		):
			engine.allocate_outstanding_based_on_payment_terms(rows[0])
			self.assertEqual(gate.call_count, 1)  # before build_data: falls back to the call
			engine._fast = True
			for row in rows:
				engine.allocate_outstanding_based_on_payment_terms(row)
			self.assertEqual(gate.call_count, 1)

	def test_a_missing_err_journals_attribute_is_tolerated(self):
		engine, keys = self.synthetic(repairs=1, normal=1)
		del engine.err_journals
		self.run_build(engine, keys)
		self.assertFalse(hasattr(engine, "err_journals"))

	def test_err_journals_become_a_set(self):
		engine, keys = self.synthetic(repairs=1, normal=1)
		self.run_build(engine, keys)
		self.assertEqual(engine.err_journals, {"JV-1", "JV-2"})
		engine, keys = self.synthetic(repairs=1, normal=1)
		with patch.object(sources, "source_profile", return_value=SimpleNamespace(major="15")):
			self.run_build(engine, keys)
		self.assertEqual(engine.err_journals, ["JV-1", "JV-2"])

	def test_pre_entries_hold_scalars_only(self):
		self.fx.invoice((40, 60), paid=30)
		self.fx.invoice((100,))
		engine = self.run_engine()
		self.assertTrue(engine.pre)
		scalar = (str, int, float, Decimal, date, datetime, time, timedelta, bool, type(None))
		for key, entry in engine.pre.items():
			for field, value in entry.items():
				self.assertIsInstance(value, scalar, f"{key} {field}")

	def test_metadata_due_date_pop_does_not_leak(self):
		engine, keys = self.synthetic(repairs=1, normal=1)
		name = next(iter(engine.voucher_balance.values())).voucher_no
		engine.invoice_details = {name: frappe._dict(due_date=self.fx.today, bill_no="B1")}

		def pop_due_date(report):
			report.set_invoice_details(
				frappe._dict(voucher_type="Purchase Invoice", voucher_no=name, due_date=1)
			)

		self.run_build(engine, keys, pop_due_date)
		self.assertNotIn("due_date", engine.invoice_details[name])
		self.assertIsInstance(engine.metadata[name], frappe._dict)
		self.assertEqual(engine.metadata[name].due_date, self.fx.today)


class TestSizeProbe(EngineCase):
	"""The bounded ledger-size probe that runs ahead of the full-history finance-book check."""

	def ledger_count(self):
		return frappe.db.count(
			"Payment Ledger Entry",
			{
				"company": self.fx.company,
				"account_type": "Payable",
				"delinked": 0,
				"posting_date": ["<=", self.fx.today],
			},
		)

	def read(self):
		return read_obligations(
			self.fx.company, "Payable", self.fx.today, self.fx.today, add_days(self.fx.today, 40)
		)

	def test_size_probe_refuses_above_limit(self):
		self.fx.invoice((40, 60), paid=30)
		count = self.ledger_count()
		self.assertGreater(count, 1)
		with patch.object(sources, "MAX_LEDGER_ROWS", count - 1):
			with self.assertRaises(CalendarSourceUnavailable) as raised:
				self.read()
		self.assertEqual(raised.exception.code, "too_large")
		with patch.object(sources, "MAX_LEDGER_ROWS", count):
			self.assertTrue(self.read()["rows"])

	def test_size_probe_counts_the_exact_boundary(self):
		self.fx.invoice((100,))
		count = self.ledger_count()
		anchor = self.fx.today
		self.assertIs(sources._ledger_rows_exceed(self.fx.company, "Payable", anchor, count), False)
		self.assertIs(sources._ledger_rows_exceed(self.fx.company, "Payable", anchor, count - 1), True)

	def test_size_probe_ignores_delinked_other_side_and_later_rows(self):
		invoice = self.fx.invoice((100,))
		count = self.ledger_count()
		self.fx.ple("Delinked", "Payment Entry", invoice, -10)
		frappe.db.set_value("Payment Ledger Entry", self.fx.prefix + "Delinked", "delinked", 1)
		self.fx.ple("Later", "Payment Entry", invoice, -10, posting_offset=5)
		self.assertIs(sources._ledger_rows_exceed(self.fx.company, "Payable", self.fx.today, count), False)
		self.assertIs(sources._ledger_rows_exceed(self.fx.company, "Receivable", self.fx.today, 0), False)

	def test_size_probe_uses_limit_plus_one(self):
		self.fx.invoice((100,))
		real_sql = frappe.db.sql
		statements = []

		def spy(query, *args, **kwargs):
			statements.append(str(query))
			return real_sql(query, *args, **kwargs)

		with patch.object(frappe.db, "sql", spy):
			sources._ledger_rows_exceed(self.fx.company, "Payable", self.fx.today, 5)
		probes = [s for s in statements if "Payment Ledger Entry" in s]
		self.assertEqual(len(probes), 1)
		self.assertRegex(probes[0], r"LIMIT 6\b")
		self.assertNotIn("ORDER BY", probes[0])
		self.assertIn("COUNT", probes[0].upper())

	def multibook_and_too_large(self):
		self.fx.invoice((100,))
		frappe.db.set_value(
			"Payment Ledger Entry", self.fx.prefix + "InvoicePLE1", "finance_book", "Alternative book"
		)

	def refusal_code(self):
		with patch.object(sources, "MAX_LEDGER_ROWS", 0):
			with self.assertRaises(CalendarSourceUnavailable) as raised:
				self.read()
		return raised.exception.code

	def test_size_probe_runs_before_books_check(self):
		if source_profile().major != "16":
			self.skipTest("the probe-first order is v16 only")
		self.multibook_and_too_large()
		self.assertEqual(self.refusal_code(), "too_large")

	def test_v15_profile_checks_books_before_size(self):
		self.multibook_and_too_large()
		with patch.object(sources, "source_profile", return_value=SimpleNamespace(major="15")):
			self.assertEqual(self.refusal_code(), "multibook_unsupported")
