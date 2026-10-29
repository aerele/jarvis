"""Contracts against ERPNext's real allocations; fixtures never leave test_jarvis."""

from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar.compatibility import source_profile
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.sources import read_obligations
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


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

	def test_doctype_link_columns_never_reach_the_user_match_filter(self):
		# ERPNext links Party Type / Voucher Type to DocType; Frappe v15's match
		# filter would then demand DocType read from every non-System Manager.
		from frappe.desk.query_report import get_filtered_data

		self.fx.invoice()
		with patch("frappe.desk.query_report.get_filtered_data", wraps=get_filtered_data) as filtered:
			self.read()
		columns = [col for call in filtered.call_args_list for col in call.args[1]]
		self.assertTrue(columns)
		self.assertFalse(
			[c for c in columns if c.get("fieldtype") == "Link" and c.get("options") == "DocType"]
		)

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
