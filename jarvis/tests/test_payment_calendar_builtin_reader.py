"""Builtin reader contracts against ERPNext's real allocations; fixtures never leave test_jarvis."""

import ast
import itertools
import json
import random
import re
from contextlib import ExitStack
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from inspect import getsource
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, nowdate
from pypika import Order

from jarvis.payment_calendar import (
	builtin_reader,
	compatibility,
	eligibility,
	permissions,
	snapshot,
	sources,
)
from jarvis.payment_calendar.errors import CalendarSourceUnavailable, ReaderFallback
from jarvis.tests import _erpnext_masters as masters
from jarvis.tests.payment_calendar_differential import _assert_equal, _canonical
from jarvis.tests.payment_calendar_fixtures import (
	CalendarFixtures,
	require_calendar_test_site,
	sync_outstanding,
)
from jarvis.tests.payment_calendar_majors import (
	GATE_NO_META_CURRENCY_PRECISION,
	GATE_NO_V15_READER_PINS,
	v16_only,
)


class TestFixtureUpkeep(TestCase):
	"""sync_outstanding keeps raw-insert invoice headers in step with the ledger, the way ERPNext does."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("builtin_reader_case")
		frappe.clear_document_cache("Accounts Settings")
		self.fx = CalendarFixtures()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="builtin_reader_case")
		frappe.clear_document_cache("Accounts Settings")
		frappe.clear_cache()

	def header(self, doctype, name):
		return flt(frappe.db.get_value(doctype, name, "outstanding_amount"))

	def read(self, side="Payable"):
		return sources.read_obligations(
			self.fx.company,
			side,
			self.fx.today,
			self.fx.today,
			add_days(self.fx.today, 40),
		)

	@staticmethod
	def service_item(code):
		if frappe.db.exists("Item", code):
			return code
		masters.ensure_item_group_and_uom()
		values = {
			"doctype": "Item",
			"item_code": code,
			"item_group": masters.ITEM_GROUP,
			"stock_uom": masters.UOM,
			"is_stock_item": 0,
		}
		# india_compliance makes the HSN code mandatory on a site that has it installed.
		if frappe.get_meta("Item").has_field("gst_hsn_code"):
			values["gst_hsn_code"] = "01011010"
		return frappe.get_doc(values).insert(ignore_permissions=True).name

	def real_invoice_and_payment(self, billed=100, paid=30):
		"""A real submitted Sales Invoice and Payment Entry, so ERPNext itself maintains the header."""
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		company = masters.ensure_ledger_company()
		masters.ensure_fiscal_years(nowdate())
		customer = masters.ensure_customer("_J2A Upkeep Customer")
		item = self.service_item("_J2A Upkeep Service")
		invoice = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": company,
				"customer": customer,
				"posting_date": nowdate(),
				"currency": "INR",
				"conversion_rate": 1,
				"selling_price_list": masters.ensure_selling_price_list(),
				"price_list_currency": "INR",
				"plc_conversion_rate": 1,
				"set_posting_time": 1,
				"due_date": add_days(nowdate(), 20),
				"items": [{"item_code": item, "qty": 1, "rate": billed}],
			}
		).insert(ignore_permissions=True)
		invoice.submit()
		cash = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Cash", "is_group": 0}, "name"
		)
		payment = get_payment_entry("Sales Invoice", invoice.name, party_amount=paid, bank_account=cash)
		payment.reference_no = "upkeep"
		payment.reference_date = nowdate()
		payment.insert(ignore_permissions=True)
		payment.submit()
		return company, invoice.name

	def test_sync_matches_erpnext_upkeep_on_real_documents(self):
		company, invoice = self.real_invoice_and_payment()
		self.assertEqual(self.header("Sales Invoice", invoice), 70)
		self.assertEqual(sync_outstanding(company), 0)
		self.assertEqual(self.header("Sales Invoice", invoice), 70)

	def test_sync_sets_fixture_headers_from_the_ledger(self):
		invoice = self.fx.invoice((100,), side="Receivable", paid=30)
		self.assertEqual(self.header("Sales Invoice", invoice), 0)
		self.assertEqual(sync_outstanding(self.fx.company), 1)
		self.assertEqual(self.header("Sales Invoice", invoice), 70)
		# A payment line dated after today still counts: ERPNext's header has no date filter.
		self.fx.ple(
			"FuturePaymentPLE",
			"Payment Entry",
			invoice,
			-20,
			"Receivable",
			self.fx.customer,
			self.fx.receivable,
			posting_offset=15,
		)
		self.assertEqual(sync_outstanding(self.fx.company), 1)
		self.assertEqual(self.header("Sales Invoice", invoice), 50)
		self.assertEqual(sync_outstanding(self.fx.company), 0)

	def test_sync_covers_both_sides_and_leaves_ledgerless_invoices_alone(self):
		payable = self.fx.invoice((60,), side="Payable", paid=10)
		receivable = self.fx.invoice((40,), side="Receivable")
		ledgerless = self.fx.insert(
			"Sales Invoice",
			"Ledgerless",
			company=self.fx.company,
			customer=self.fx.customer,
			posting_date=self.fx.today,
			docstatus=1,
			is_return=0,
			debit_to=self.fx.receivable,
			outstanding_amount=7,
		)
		self.assertEqual(sync_outstanding(self.fx.company), 2)
		self.assertEqual(self.header("Purchase Invoice", payable), 50)
		self.assertEqual(self.header("Sales Invoice", receivable), 40)
		# ERPNext skips an invoice with no ledger rows (erpnext/accounts/utils.py:2205): so does the helper.
		self.assertEqual(self.header("Sales Invoice", ledgerless), 7)

	def test_sync_ignores_other_companies_and_non_submitted_invoices(self):
		draft = self.fx.invoice((100,), side="Receivable")
		frappe.db.set_value("Sales Invoice", draft, "docstatus", 0)
		self.assertEqual(sync_outstanding(self.fx.company), 0)
		self.assertEqual(self.header("Sales Invoice", draft), 0)
		frappe.db.set_value("Sales Invoice", draft, "docstatus", 1)
		self.assertEqual(sync_outstanding(self.fx.prefix + "Other"), 0)
		self.assertEqual(self.header("Sales Invoice", draft), 0)

	def test_sync_does_not_change_todays_read(self):
		self.fx.invoice((40, 60), side="Payable", paid=30)
		self.fx.invoice((50,), side="Receivable", credit=10)
		for side in ("Payable", "Receivable"):
			before = self.read(side)
			sync_outstanding(self.fx.company)
			self.assertEqual(self.read(side), before)
		self.assertEqual(Decimal(self.read("Payable")["canonical_total"]), Decimal("70"))

	@v16_only(GATE_NO_META_CURRENCY_PRECISION)
	def test_sync_rounds_with_the_precision_of_the_invoice_currency(self):
		real = frappe.get_system_settings

		def settings(key):
			return 1 if key == "use_number_format_from_currency" else real(key)

		# The company default (INR, 2 places) would keep 100.46; the invoice's own currency has none.
		yen = self.fx.invoice((100.456,), side="Receivable")
		frappe.db.set_value("Sales Invoice", yen, "party_account_currency", "JPY")
		rupee = self.fx.invoice((100.456,), side="Receivable")
		real_get_value = frappe.db.get_value
		formats = {"JPY": "#,###", "INR": "#,##,###.##"}

		def get_value(doctype, *args, **kwargs):
			# The two currency formats this test is about, whatever the site's Currency rows say.
			if doctype == "Currency" and args and args[0] in formats and "number_format" in args[1:2]:
				return formats[args[0]]
			return real_get_value(doctype, *args, **kwargs)

		# No explicit global currency precision either: it would override the currency format.
		with (
			patch.object(frappe, "get_system_settings", settings),
			patch.object(frappe.model.meta, "get_currency_precision", return_value=None),
			patch.object(frappe.db, "get_value", get_value),
		):
			self.assertEqual(sync_outstanding(self.fx.company), 2)
		self.assertEqual(self.header("Sales Invoice", yen), 100)
		self.assertEqual(self.header("Sales Invoice", rupee), 100.46)

	def test_sync_keeps_a_fractional_amount_at_the_field_precision(self):
		invoice = self.fx.invoice((10.005, 20.0049), side="Payable", paid=0.0001)
		sync_outstanding(self.fx.company)
		self.assertEqual(self.header("Purchase Invoice", invoice), flt(30.0099, 2))
		self.assertNotEqual(self.header("Purchase Invoice", invoice), 30.0099)

	def test_sync_filters_by_party_and_by_account(self):
		"""A line against an invoice under another party or account is no part of that invoice's balance."""
		other_party = self.fx.insert(
			"Customer", "Customer2", customer_name=self.fx.prefix + "2", customer_type="Company", disabled=0
		)
		other_account = self.fx.account("Receivable2", "Receivable", "Asset")
		first = self.fx.invoice((100,), side="Receivable")
		second = self.fx.invoice((100,), side="Receivable", supplier=other_party, account=other_account)
		# Ledger lines of the same invoice under another party, and under another account.
		self.fx.ple(
			"OtherPartyLine", "Sales Invoice", second, 50, "Receivable", self.fx.customer, other_account
		)
		self.fx.ple(
			"OtherAccountLine", "Sales Invoice", second, 40, "Receivable", other_party, self.fx.receivable
		)
		self.fx.ple(
			"RightParty", "Payment Entry", first, -10, "Receivable", self.fx.customer, self.fx.receivable
		)
		sync_outstanding(self.fx.company)
		self.assertEqual(self.header("Sales Invoice", first), 90)
		self.assertEqual(self.header("Sales Invoice", second), 100)


PARTY_TYPES = {"Receivable": "Customer", "Payable": "Supplier"}
INVOICE_TYPES = {"Receivable": "Sales Invoice", "Payable": "Purchase Invoice"}
SIDES = ("Receivable", "Payable")
# A statement that returns ledger rows must carry one of these narrowing criteria.
NARROWING = re.compile(
	r"`against_voucher_no`\s+IN\b|`against_voucher_type`\s*=|`against_voucher_type`\s+IS NULL|`voucher_no`\s+IN\b"
)
# sha256 of the source of the two copies of the settled-in-window rule (see the test that uses it).
# Re-audited when `_settled_in_window` began returning the selected keys in candidate order (a list) instead of
# the set: the selection rule is unchanged, so `_due_in_window` still finds the same candidates.
SETTLED_RULE_PINS = {
	"sources._settled_in_window": "0421064fbcfd968c1af46bc60e3df72be67645d92f712fd3a54844ae97721b73",
	"builtin_reader._due_in_window": "0a62b0e1116b09ed8708382d33a6869779b192c64b4c2ec6a02baa73138ba528",
}
SIDES_ACCOUNT_FIELD = {"Receivable": "debit_to", "Payable": "credit_to"}
HEADER_TABLE = re.compile(r"FROM `tab(Sales Invoice|Purchase Invoice|Journal Entry|Payment Entry)`")


class ReaderCase(TestCase):
	"""Fixtures on test_jarvis in a savepoint, and the reader run through the engine seam."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("builtin_reader_case")
		frappe.clear_document_cache("Accounts Settings")
		self.fx = CalendarFixtures()
		# (doctype, name) -> stored outstanding_amount, applied after sync_outstanding to isolate one rule
		self.pinned_headers = {}

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="builtin_reader_case")
		frappe.clear_document_cache("Accounts Settings")
		frappe.clear_cache()

	def window(self, window=None):
		return window or (self.fx.today, add_days(self.fx.today, 40))

	def parties(self, side):
		if side == "Receivable":
			return self.fx.customer, self.fx.receivable
		return self.fx.supplier, self.fx.payable

	def sync(self):
		sync_outstanding(self.fx.company)
		for (doctype, name), value in self.pinned_headers.items():
			frappe.db.set_value(doctype, name, "outstanding_amount", value)

	def engine(self, side, window=None):
		start, end = self.window(window)
		report, start, end, anchor, values = sources._require_inputs(
			self.fx.company, side, self.fx.today, start, end
		)
		engine = sources._run_engine(
			builtin_reader._builtin_class(),
			sources._engine_filters(self.fx.company, anchor),
			side,
			start,
			end,
		)
		return report, start, end, engine, values

	def reader_result(self, side, window=None):
		report, start, end, engine, values = self.engine(side, window)
		return sources._assemble(engine, report, side, start, end, values)

	def today_result(self, side, window=None):
		start, end = self.window(window)
		with patch.object(sources, "MAX_LEDGER_ROWS", 10**9), patch.object(sources, "MAX_VOUCHERS", 10**9):
			return sources.read_obligations(self.fx.company, side, self.fx.today, start, end)

	def assert_reader_equals_today(self, side, window=None):
		self.sync()
		expected = self.today_result(side, window)
		actual = self.reader_result(side, window)
		_assert_equal(
			_canonical(actual), _canonical(expected), f"{side}: the reader differs from today's read"
		)
		return actual

	def assert_ids_and_order_equal_today(self, side):
		"""Ids encode the position of each row, so equal ids and order mean the same line order."""
		self.sync()
		actual, expected = self.reader_result(side), self.today_result(side)
		self.assertTrue(actual["rows"])
		self.assertEqual([row["id"] for row in actual["rows"]], [row["id"] for row in expected["rows"]])
		_assert_equal(actual["rows"], expected["rows"], f"{side}: rows differ from today's, ids included")

	def both_sides(self, scenario, window=None):
		for side in SIDES:
			with self.subTest(side):
				scenario(side)
				self.assert_reader_equals_today(side, window)

	def raw(self, suffix, voucher_type, voucher_no, against_type, against_no, amount, side, **extra):
		"""One ledger line with any voucher and against type; the side's party and account unless overridden."""
		party, account = self.parties(side)
		values = {
			"company": self.fx.company,
			"posting_date": self.fx.today,
			"account_type": side,
			"account": account,
			"account_currency": "INR",
			"party_type": PARTY_TYPES[side],
			"party": party,
			"voucher_type": voucher_type,
			"voucher_no": voucher_no,
			"against_voucher_type": against_type,
			"against_voucher_no": against_no,
			"amount": amount,
			"amount_in_account_currency": amount,
			"delinked": 0,
		}
		values.update(extra)
		return self.fx.insert("Payment Ledger Entry", suffix, **values)

	def invoice(self, side, amounts=(100,), **kwargs):
		return self.fx.invoice(amounts, side=side, **kwargs)

	def pay(self, suffix, invoice, amount, side, **kwargs):
		party, account = self.parties(side)
		return self.fx.ple(suffix, "Payment Entry", invoice, amount, side, party, account, **kwargs)

	def spy_queries(self):
		queries, sql = [], frappe.db.sql

		def spy(query, *args, **kwargs):
			queries.append(str(query))
			return sql(query, *args, **kwargs)

		return queries, patch.object(frappe.db, "sql", spy)

	def settled(self, side, *, term=10, header=20, paid_at=-5, schedule=True, amount=100):
		"""A fully paid invoice: payment on day `paid_at`, one term due on `term`, header due on `header`."""
		invoice = self.invoice(side, (amount,), schedules=schedule, offset=-30)
		self.pay("Settle" + str(self.fx.serial), invoice, -amount, side, posting_offset=paid_at)
		doctype = INVOICE_TYPES[side]
		frappe.db.set_value(doctype, invoice, "due_date", add_days(self.fx.today, header))
		if schedule:
			frappe.db.set_value(
				"Payment Schedule",
				f"{self.fx.prefix}Term{self.fx.serial}_0",
				"due_date",
				add_days(self.fx.today, term),
			)
		return invoice

	def vouchers(self, result):
		return {row["voucher_no"] for row in result["rows"]}

	def assert_same_refusal(self, side, code, window=None):
		self.sync()
		with self.assertRaises(CalendarSourceUnavailable) as today:
			self.today_result(side, window)
		with self.assertRaises(CalendarSourceUnavailable) as reader:
			self.reader_result(side, window)
		self.assertEqual((today.exception.code, reader.exception.code), (code, code))

	def foreign_invoice(self, side, *, gross=8000.01, paid=8000, rate=80, account_paid=None):
		"""A foreign-currency invoice settled in its own currency; the company-currency residual is gross - paid.

		`account_paid` sets the payment line's account-currency amount (default: the full invoice amount).
		"""
		invoice = self.invoice(side, (gross,), gross_total=gross)
		doctype, account = INVOICE_TYPES[side], self.parties(side)[1]
		serial = str(self.fx.serial)
		frappe.db.set_value("Account", account, "account_currency", "USD")
		frappe.db.set_value(
			doctype,
			invoice,
			{
				"currency": "USD",
				"party_account_currency": "USD",
				"conversion_rate": rate,
				"grand_total": gross / rate,
				"base_grand_total": gross,
			},
		)
		doc = frappe.get_doc(doctype, invoice)
		doc.payment_schedule[0].invoice_portion = 100
		doc.set_payment_schedule()
		for term in doc.payment_schedule:
			term.db_update()
		frappe.db.set_value(
			"Payment Ledger Entry",
			self.fx.prefix + "InvoicePLE" + serial,
			{"account_currency": "USD", "amount_in_account_currency": gross / rate},
		)
		self.pay("FXPay" + serial, invoice, -paid, side)
		frappe.db.set_value(
			"Payment Ledger Entry",
			self.fx.prefix + "FXPay" + serial,
			{
				"account_currency": "USD",
				"amount_in_account_currency": -(gross / rate if account_paid is None else account_paid),
			},
		)
		return invoice

	def two_account_invoice(self, side, *, paid):
		"""An invoice with own lines on two accounts of the side (a second account with its own payment)."""
		party, account = self.parties(side)
		other = self.fx.account("Second" + side, side, "Asset" if side == "Receivable" else "Liability")
		doctype = INVOICE_TYPES[side]
		invoice = self.invoice(side, (100,), paid=100 if paid else 0, offset=-30)
		self.fx.ple("OwnB" + side, doctype, invoice, 50, side, party, other, posting_offset=-30)
		if paid:
			self.fx.ple("PayB" + side, "Payment Entry", invoice, -50, side, party, other, posting_offset=-5)
		return invoice

	def second_receivable(self):
		"""A second customer and a second receivable account of the same company."""
		party = self.fx.insert(
			"Customer", "Customer2", customer_name=self.fx.prefix + "2", customer_type="Company", disabled=0
		)
		return party, self.fx.account("Receivable2", "Receivable", "Asset")


@v16_only(GATE_NO_V15_READER_PINS)
class TestBuiltinReaderCore(ReaderCase):
	def test_open_partial_invoice(self):
		self.both_sides(lambda side: self.invoice(side, (40, 60), paid=30))

	def test_overpaid_invoice_gives_an_adjustment_row(self):
		self.both_sides(lambda side: self.invoice(side, (100,), paid=130))

	def test_credit_note_against_an_open_invoice(self):
		self.both_sides(lambda side: self.invoice(side, (100,), credit=10))

	def test_return_with_its_own_outstanding_off_and_a_refund_line_against_it(self):
		"""The refund folds onto the original; the return's own header says nothing, so only the original is open."""

		def scenario(side):
			invoice = self.invoice(side, (100,), paid=30, credit=10)
			ret = invoice.replace("Invoice", "Return", 1)
			self.pay("Refund" + side, ret, 10, side)
			self.pinned_headers[(INVOICE_TYPES[side], ret)] = 0

		self.both_sides(scenario)

	def test_fully_paid_by_a_future_dated_payment_is_open_as_of_today(self):
		invoices = {}
		for side in SIDES:
			invoices[side] = self.invoice(side, (100,))
			self.pay("FuturePayment" + side, invoices[side], -100, side, posting_offset=15)
		sync_outstanding(self.fx.company)
		for side in SIDES:
			with self.subTest(side):
				header = frappe.db.get_value(INVOICE_TYPES[side], invoices[side], "outstanding_amount")
				self.assertEqual(
					header, 0, "the header counts the later payment, so only the ledger says open"
				)
				result = self.assert_reader_equals_today(side)
				self.assertEqual(Decimal(result["canonical_total"]), 100)

	def test_a_payment_against_an_invoice_posted_after_the_anchor_is_its_own_row(self):
		def scenario(side):
			invoice = self.invoice(side, (100,), offset=5)
			self.pay("EarlyPayment" + side, invoice, -40, side)

		self.both_sides(scenario)

	def test_a_payment_against_a_cancelled_invoice_is_its_own_row(self):
		def scenario(side):
			invoice = self.invoice(side, (100,), paid=40)
			serial = self.fx.serial
			frappe.db.set_value(INVOICE_TYPES[side], invoice, "docstatus", 2)
			frappe.db.set_value("Payment Ledger Entry", f"{self.fx.prefix}InvoicePLE{serial}", "delinked", 1)

		self.both_sides(scenario)

	def test_unallocated_payment_entry(self):
		def scenario(side):
			entry = self.fx.prefix + "Advance" + side
			self.raw("Unallocated" + side, "Payment Entry", entry, "Payment Entry", entry, -50, side)

		self.both_sides(scenario)

	def test_journal_entry_without_reference_uses_its_bill_date(self):
		def scenario(side):
			entry = self.fx.prefix + "JE" + side
			self.raw("JeLine" + side, "Journal Entry", entry, "Journal Entry", entry, 70, side)
			frappe.db.set_value(
				"Journal Entry", entry, {"bill_no": "B-1", "due_date": add_days(self.fx.today, 15)}
			)

		self.both_sides(scenario)

	def test_journal_entry_with_a_blank_bill_number_is_booked_like_today(self):
		"""A bill number of spaces is a bill number (truthy for today's read); `<> ''` would drop it (PAD SPACE)."""

		def scenario(side):
			entry = self.fx.prefix + "JEBlank" + side
			self.raw("JeBlank" + side, "Journal Entry", entry, "Journal Entry", entry, 70, side)
			frappe.db.set_value(
				"Journal Entry", entry, {"bill_no": " ", "due_date": add_days(self.fx.today, 15)}
			)

		self.both_sides(scenario)

	def test_journal_entry_against_a_journal_entry(self):
		def scenario(side):
			first, second = self.fx.prefix + "JEa" + side, self.fx.prefix + "JEb" + side
			self.raw("JeOwn" + side, "Journal Entry", first, "Journal Entry", first, 90, side)
			self.raw("JePay" + side, "Journal Entry", second, "Journal Entry", first, -35, side)

		self.both_sides(scenario)

	def test_sales_order_advance_takes_cost_center_and_project(self):
		def scenario(side):
			order = "Purchase Order" if side == "Payable" else "Sales Order"
			entry = self.fx.prefix + "OrderAdvance" + side
			self.raw(
				"OrderAdvance" + side,
				"Payment Entry",
				entry,
				order,
				self.fx.prefix + "Order" + side,
				-60,
				side,
				project=self.fx.prefix + "Project",
				cost_center=self.fx.prefix + "Centre",
			)

		self.both_sides(scenario)

	def test_employee_and_null_party_type_lines_follow_the_report_on_each_side(self):
		def scenario(side):
			self.invoice(side, (100,))
			employee = self.fx.insert("Employee", "Employee" + side, employee_name="Only employee")
			for kind, party_type, party, amount in (
				("EmployeeLine", "Employee", employee, -30),
				("NullTypeLine", None, self.parties(side)[0], -20),
			):
				entry = self.fx.prefix + kind + "PE" + side
				self.raw(
					kind + side,
					"Payment Entry",
					entry,
					"Payment Entry",
					entry,
					amount,
					side,
					party_type=party_type,
					party=party,
				)

		self.both_sides(scenario)

	def test_a_null_against_type_line_is_read(self):
		def scenario(side):
			entry = self.fx.prefix + "NoAgainst" + side
			self.raw("NoAgainst" + side, "Payment Entry", entry, None, None, -20, side)

		self.both_sides(scenario)

	def test_a_null_against_line_and_an_order_advance_in_one_read(self):
		"""A NULL against number keys to itself; the probe must not sort it next to the order's name."""

		def scenario(side):
			order = "Purchase Order" if side == "Payable" else "Sales Order"
			entry = self.fx.prefix + "NullAgainst" + side
			self.raw("NullAgainst" + side, "Payment Entry", entry, None, None, -20, side)
			advance = self.fx.prefix + "AdvanceWithNull" + side
			self.raw(
				"AdvanceWithNull" + side,
				"Payment Entry",
				advance,
				order,
				self.fx.prefix + "Order" + side,
				-60,
				side,
			)

		self.both_sides(scenario)

	COLLATION_NAMES = ("Éclair", "Zeta", "X&Y", "A_1", "A1")

	def test_parties_sort_by_the_database_collation_like_today(self):
		"""Accents and punctuation: a Python casefold puts "Éclair" after "Zeta", the database before "X&Y"."""
		for side in SIDES:
			with self.subTest(side):
				for index, name in enumerate(self.COLLATION_NAMES):
					kind = "Customer" if side == "Receivable" else "Supplier"
					party = self.fx.insert(
						kind,
						name + side,
						**{f"{kind.lower()}_name": self.fx.prefix + name},
						**(
							{"customer_type": "Company", "disabled": 0}
							if kind == "Customer"
							else {"supplier_type": "Company", "disabled": 0}
						),
					)
					self.invoice(side, (10 + index,), supplier=party)
				self.assert_ids_and_order_equal_today(side)

	def test_lines_of_one_party_and_date_sort_by_the_database_collation_like_today(self):
		"""The line name is the last sort key: journals on one party and date come in the database's order."""
		for side in SIDES:
			with self.subTest(side):
				for index, name in enumerate(self.COLLATION_NAMES):
					entry = self.fx.prefix + "Journal" + name + side
					self.raw(name + side, "Journal Entry", entry, "Journal Entry", entry, 10 + index, side)
				self.assert_ids_and_order_equal_today(side)

	def test_a_voucher_whose_first_line_is_against_an_unread_closed_invoice_keeps_its_place(self):
		"""One date, one party: PE1's first line pays a closed invoice the reader never discovers, its second line
		is unallocated; PE2 has one unallocated line. Today sees PE1 first (its first line has the lower name).
		Row ids follow that order, so the reader must see PE1 first too, though it reads only PE1's second line.
		"""

		def scenario(side):
			invoice = self.invoice(side, (100,), schedules=False, offset=-400)
			doctype = INVOICE_TYPES[side]
			# Closed and due long before the window: nothing selects the invoice, so none of its lines is read.
			frappe.db.set_value(
				doctype, invoice, "due_date", add_days(self.fx.today, -380), update_modified=False
			)
			first, second = (self.fx.prefix + name + side for name in ("PE1", "PE2"))
			self.raw("IdA" + side, "Payment Entry", first, doctype, invoice, -100, side)
			self.raw("IdB" + side, "Payment Entry", second, "Payment Entry", second, -30, side)
			self.raw("IdC" + side, "Payment Entry", first, "Payment Entry", first, -50, side)

		for side in SIDES:
			with self.subTest(side):
				scenario(side)
				self.assert_ids_and_order_equal_today(side)

	def test_a_tied_line_order_follows_the_report(self):
		"""Own lines of one journal on one date and party: the last by name decides the project."""

		def scenario(side):
			entry = self.fx.prefix + "Tie" + side
			for suffix, amount, project in (
				("COwn", 30, "Gamma"),
				("AOwn", 20, "Alpha"),
				("BOwn", 40, "Beta"),
			):
				self.raw(
					suffix + side,
					"Journal Entry",
					entry,
					"Journal Entry",
					entry,
					amount,
					side,
					project=project,
				)

		self.both_sides(scenario)

	def test_wrong_stored_outstanding_falls_back(self):
		"""Outside the due window only the header finds an open invoice: a header forced to 0 hides it."""
		invoice = self.invoice("Receivable", (100,))
		sync_outstanding(self.fx.company)
		frappe.db.set_value("Sales Invoice", invoice, "outstanding_amount", 0)
		with self.assertRaises(ReaderFallback) as raised:
			self.reader_result("Receivable", (self.fx.today, self.fx.today))
		self.assertEqual(raised.exception.reason, "control_total")

	def test_a_nonzero_unread_line_falls_back(self):
		"""A payment line against an invoice that does not exist lands on the payment's own key, unread."""
		self.invoice("Receivable", (100,), paid=100)
		self.pay("Ghost", self.fx.prefix + "GhostInvoice", -30, "Receivable")
		sync_outstanding(self.fx.company)
		window = (self.fx.today, self.fx.today)
		with self.assertRaises(ReaderFallback) as raised:
			self.reader_result("Receivable", window)
		self.assertEqual(raised.exception.reason, "control_total")
		result = self.today_result("Receivable", window)
		self.assertEqual(Decimal(result["canonical_total"]), -30, "today's read does show the line")

	def test_control_total_matches_boundaries(self):
		matches = builtin_reader.control_total_matches
		# Strictly less than half a unit of the last place: exactly half a unit does not match.
		rows = ((2, "0.0049", "0.005", "0.0051"), (3, "0.00049", "0.0005", "0.00051"))
		for precision, inside, half, outside in rows:
			with self.subTest(precision):
				self.assertTrue(matches(Decimal(inside), Decimal(0), precision))
				self.assertTrue(matches(Decimal(0), Decimal(inside), precision))
				self.assertFalse(matches(Decimal(half), Decimal(0), precision))
				self.assertFalse(matches(Decimal(0), Decimal(half), precision))
				self.assertFalse(matches(Decimal(outside), Decimal(0), precision))
				self.assertFalse(matches(Decimal(0), Decimal(outside), precision))

	def test_control_total_compares_decimals_not_floats(self):
		big, base = Decimal("10000000000.00500005"), Decimal(10**10)
		# As floats the two differ by less than half a cent and would match.
		self.assertLessEqual(abs(float(big) - float(base)), 0.005)
		self.assertFalse(builtin_reader.control_total_matches(big, base, 2))

	def test_ledger_control_reads_the_sum_exactly(self):
		exact = Decimal("12345678901.123456789")
		entry = self.fx.prefix + "HugePE"
		name = self.raw("Huge", "Payment Entry", entry, "Payment Entry", entry, 1, "Payable")
		# The document layer rounds to a float; the column itself holds nine decimals.
		frappe.db.sql("UPDATE `tabPayment Ledger Entry` SET amount = %s WHERE name = %s", (str(exact), name))
		control = builtin_reader.ledger_control(
			self.fx.company, "Payable", date.fromisoformat(self.fx.today), [self.fx.payable]
		)
		self.assertEqual(control.total, exact, "a float cannot hold these digits")

	def test_ledger_control_has_the_reports_predicate(self):
		today = date.fromisoformat(self.fx.today)
		invoice = self.invoice("Receivable", (100,))
		entry = self.fx.prefix + "ControlPE"
		# Each line fails exactly one part of the report's predicate.
		for suffix, extra in (
			("OtherCompany", {"company": self.fx.prefix + "Other"}),
			("OtherAccount", {"account": self.fx.bank}),
			("Delinked", {"delinked": 1}),
			("Later", {"posting_date": add_days(self.fx.today, 3)}),
			("Employee", {"party_type": "Employee"}),
			("NullType", {"party_type": None}),
		):
			self.raw(
				"Control" + suffix,
				"Payment Entry",
				entry + suffix,
				"Sales Invoice",
				invoice,
				-1,
				"Receivable",
				**extra,
			)
		receivable = builtin_reader.ledger_control(self.fx.company, "Receivable", today, [self.fx.receivable])
		self.assertEqual(receivable.total, Decimal(100))
		self.assertEqual(receivable.pairs, {("Sales Invoice", "Sales Invoice"): 1})
		# Payable keeps Employee, and the SQL `!=` rule does not apply there: a NULL party type counts.
		for suffix, party_type in (("Employee", "Employee"), ("NullType", None)):
			name = entry + "P" + suffix
			self.raw(
				"Payable" + suffix,
				"Payment Entry",
				name,
				"Payment Entry",
				name,
				-2,
				"Payable",
				party_type=party_type,
			)
		payable = builtin_reader.ledger_control(self.fx.company, "Payable", today, [self.fx.payable])
		self.assertEqual(payable.total, Decimal(-4))
		self.assertEqual(payable.pairs, {("Payment Entry", "Payment Entry"): 2})

	def test_ledger_control_names_the_type_pairs_including_a_null_one(self):
		self.invoice("Receivable", (100,), paid=10)
		entry = self.fx.prefix + "PairPE"
		self.raw("Pair", "Payment Entry", entry, None, None, -5, "Receivable")
		control = builtin_reader.ledger_control(
			self.fx.company, "Receivable", date.fromisoformat(self.fx.today), [self.fx.receivable]
		)
		self.assertEqual(control.total, Decimal(85))
		self.assertEqual(
			control.pairs,
			{
				("Sales Invoice", "Sales Invoice"): 1,
				("Payment Entry", "Sales Invoice"): 1,
				("Payment Entry", None): 1,
			},
		)

	def rekey_scenario(self, side):
		"""A payment against a journal whose own lines (against a closed invoice) are unread and net to zero."""
		doctype = INVOICE_TYPES[side]
		closed = self.invoice(side, (100,), paid=100)
		journal, payment = self.fx.prefix + "RekeyJE" + side, self.fx.prefix + "RekeyPE" + side
		self.raw("RekeyA" + side, "Journal Entry", journal, doctype, closed, 70, side)
		self.raw("RekeyB" + side, "Journal Entry", journal, doctype, closed, -70, side)
		self.raw("RekeyPay" + side, "Payment Entry", payment, "Journal Entry", journal, -40, side)

	def test_a_generic_line_whose_target_key_has_unread_lines_is_rekeyed(self):
		"""The control total passes here (the unread lines net to zero); only the probe gets the row right."""
		self.both_sides(self.rekey_scenario, (self.fx.today, self.fx.today))

	def test_the_rekey_probe_is_bounded(self):
		self.rekey_scenario("Receivable")
		sync_outstanding(self.fx.company)
		window = (self.fx.today, self.fx.today)
		self.assertTrue(self.reader_result("Receivable", window))
		with patch.object(builtin_reader, "MAX_REKEY_ROUNDS", 0), self.assertRaises(ReaderFallback) as raised:
			self.reader_result("Receivable", window)
		self.assertEqual(raised.exception.reason, "rekey_unresolved")

	def test_return_closure_runs_in_both_directions(self):
		report_class = builtin_reader._builtin_class()
		report = report_class.__new__(report_class)
		report.return_entries = frappe._dict({"R1": "X1", "R2": "X2", "R3": None, "R4": "R2"})
		self.assertEqual(report._close_returns({"R1"}), {"R1", "X1"}, "a return needs its original")
		self.assertEqual(report._close_returns({"X1"}), {"X1", "R1"}, "an original needs its returns")
		self.assertEqual(report._close_returns({"R3"}), {"R3"})
		self.assertEqual(report._close_returns({"X2"}), {"X2", "R2", "R4"}, "to a fixed point")
		self.assertEqual(report._close_returns(set()), set())

	def test_discovery_closes_returns_last_over_the_union_of_the_other_sets(self):
		"""A return of a cancelled original: the original is found as an orphan, the return only by closure."""
		invoice = self.invoice("Receivable", (100,), credit=10)
		ret = invoice.replace("Invoice", "Return", 1)
		frappe.db.set_value("Sales Invoice", invoice, "docstatus", 2)
		frappe.db.set_value(
			"Payment Ledger Entry", f"{self.fx.prefix}InvoicePLE{self.fx.serial}", "delinked", 1
		)
		sync_outstanding(self.fx.company)
		frappe.db.set_value("Sales Invoice", ret, "outstanding_amount", 0)
		_, _, _, engine, _ = self.engine("Receivable")
		control = builtin_reader.ledger_control(
			self.fx.company, "Receivable", engine.filters.report_date, engine.side_accounts
		)
		discovery = engine._discover(control)
		self.assertEqual(discovery.orphans, {invoice})
		self.assertEqual(discovery.open, frozenset())
		self.assertEqual(discovery.returns, {ret})
		self.assertEqual(engine.reader_stats["counts"]["returns"], 1)

	def test_the_header_of_an_open_invoice_is_discovery_only(self):
		"""Open by header: the figures still come from the ledger lines, never from the header."""
		invoice = self.invoice("Payable", (40, 60), paid=30)
		sync_outstanding(self.fx.company)
		frappe.db.set_value("Purchase Invoice", invoice, "outstanding_amount", 999)
		self.assertEqual(_canonical(self.reader_result("Payable")), _canonical(self.today_result("Payable")))

	def test_no_ledger_line_is_read_twice(self):
		"""The probe finds lines of a closed invoice that the invoice read then asks for again: once each."""
		self.rekey_scenario("Receivable")
		invoice = self.invoice("Receivable", (100,), paid=30, credit=10)
		self.pay("LaterPayment", invoice, -5, "Receivable", posting_offset=4)
		sync_outstanding(self.fx.company)
		seen = []
		report_class = builtin_reader._builtin_class()
		init = report_class.init_voucher_balance

		def record(report, ple):
			seen.append(ple.name)
			return init(report, ple)

		with patch.object(report_class, "init_voucher_balance", record):
			_, _, _, engine, _ = self.engine("Receivable", (self.fx.today, self.fx.today))
		self.assertEqual(len(seen), len(set(seen)))
		self.assertEqual(len(seen), engine.reader_stats["counts"]["lines"])
		self.assertGreaterEqual(len(seen), 8)
		self.assertGreaterEqual(engine.reader_stats["counts"]["rekey_probed"], 1)

	def test_the_lines_of_a_closed_invoice_are_never_read(self):
		"""The point of the reader: history that nets to zero and cannot reach a row stays in the database."""
		closed = self.invoice("Receivable", (100,), paid=100)
		self.invoice("Receivable", (50,), paid=20)
		sync_outstanding(self.fx.company)
		seen = []
		report_class = builtin_reader._builtin_class()
		init = report_class.init_voucher_balance

		def record(report, ple):
			seen.append(ple.against_voucher_no)
			return init(report, ple)

		with patch.object(report_class, "init_voucher_balance", record):
			self.engine("Receivable", (self.fx.today, self.fx.today))
		self.assertTrue(seen)
		self.assertNotIn(closed, seen)

	def test_plain_name_ranks_equal_the_database_ranks(self):
		"""The casefold shortcut for names of numerals and ASCII letters is the collation's own order."""
		alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
		rng = random.Random(7)
		names = sorted({"".join(rng.choices(alphabet, k=rng.randint(1, 4))) for _ in range(1500)})
		collation = builtin_reader.column_collations(("name",))["name"]
		self.assertRegex(collation, builtin_reader.LETTER_COLLATION)
		plain = builtin_reader.plain_ranks(names, collation)
		database = builtin_reader.collation_ranks([names], collation)[0]
		for first, second in itertools.combinations(names, 2):
			self.assertEqual(
				(plain[first] > plain[second]) - (plain[first] < plain[second]),
				(database[first] > database[second]) - (database[first] < database[second]),
				(first, second),
			)

	def test_plain_name_ranks_stand_down_for_other_names_and_collations(self):
		self.assertIsNone(builtin_reader.plain_ranks(["abc", "a-b"], "utf8mb4_unicode_ci"))
		self.assertIsNone(builtin_reader.plain_ranks(["abc", "éa"], "utf8mb4_unicode_ci"))
		self.assertIsNone(builtin_reader.plain_ranks(["abc", "ABD"], "utf8mb4_bin"))
		self.assertIsNone(builtin_reader.plain_ranks(["abc", "ABD"], None))
		self.assertEqual(
			builtin_reader.plain_ranks(["b", "A", "a"], "utf8mb4_unicode_ci"), {"b": 1, "A": 0, "a": 0}
		)

	def test_merged_lines_follow_the_reports_order(self):
		def line(day, party, name):
			return SimpleNamespace(posting_date=date(2026, 1, day), party=party, name=name)

		lines = [
			line(2, "b", "x"),
			line(1, "B", "z"),
			line(1, "a", "y"),
			line(1, None, "q"),
			line(1, "a", "X"),
		]
		ordered = builtin_reader.sort_like_the_report(lines)
		self.assertEqual(
			[(row.posting_date.day, row.party, row.name) for row in ordered],
			[(1, None, "q"), (1, "a", "X"), (1, "a", "y"), (1, "B", "z"), (2, "b", "x")],
		)

	def test_reader_reads_no_unbounded_ledger_rows(self):
		for side in SIDES:
			with self.subTest(side):
				invoice = self.invoice(side, (40, 60), paid=30, credit=5)
				self.pay("Later" + side, invoice, -5, side, posting_offset=4)
				entry = self.fx.prefix + "SpyPE" + side
				self.raw("Spy" + side, "Payment Entry", entry, "Payment Entry", entry, -9, side)
				sync_outstanding(self.fx.company)
				queries, spy = self.spy_queries()
				with spy:
					self.reader_result(side)
				ledger = [q for q in queries if "tabPayment Ledger Entry" in q]
				aggregates = [q for q in ledger if "GROUP BY" in q]
				# The control total and the multi-account aggregate; the FX one only with a foreign account.
				self.assertEqual(len(aggregates), 2, "one aggregate per purpose")
				self.assertEqual(len([q for q in aggregates if "CAST(" in q]), 1)
				self.assertEqual(len([q for q in aggregates if "COUNT(DISTINCT" in q]), 1)
				rows = [q for q in ledger if q not in aggregates]
				self.assertTrue(rows)
				for query in rows:
					self.assertRegex(query, NARROWING, "a ledger read without a narrowing criterion")

	def test_a_reader_bug_is_a_fallback_not_a_refusal(self):
		self.invoice("Payable")
		sync_outstanding(self.fx.company)
		report_class = builtin_reader._builtin_class()
		for name in ("_discover", "_narrow_lines", "_narrow_invoice_details", "_check_control_total"):
			for error in (AttributeError, KeyError, TypeError, ArithmeticError, InvalidOperation):
				with self.subTest(name, error=error.__name__):
					with patch.object(report_class, name, side_effect=error("bug")):
						with self.assertRaises(ReaderFallback) as raised:
							self.reader_result("Payable")
					self.assertEqual(raised.exception.reason, "reader_error")
					self.assertNotIsInstance(raised.exception, CalendarSourceUnavailable)
		with patch.object(builtin_reader, "ledger_control", side_effect=TypeError("bug")):
			with self.assertRaises(ReaderFallback) as raised:
				self.reader_result("Payable")
		self.assertEqual(raised.exception.reason, "reader_error")

	def test_an_upstream_error_keeps_todays_mapping(self):
		self.invoice("Payable")
		sync_outstanding(self.fx.company)
		report_class = builtin_reader._builtin_class()
		for name in ("init_voucher_balance", "update_voucher_balance", "build_data"):
			with self.subTest(name):
				with patch.object(report_class, name, side_effect=TypeError("bug")):
					with self.assertRaises(CalendarSourceUnavailable):
						self.reader_result("Payable")

	def test_an_unreadable_pinned_source_is_a_fallback(self):
		for patched, error in (
			("import_module", RuntimeError("module code failed")),
			("qualified_source_hash", ValueError("bad")),
		):
			with self.subTest(patched):
				with patch.object(compatibility, patched, side_effect=error):
					with self.assertRaises(ReaderFallback) as raised:
						builtin_reader.require_supported_source()
				self.assertEqual(raised.exception.reason, "pin_drift")
		with patch.object(compatibility, "reader_pin_drift", return_value=["a.b"]):
			with self.assertRaises(ReaderFallback) as raised:
				builtin_reader.require_supported_source()
		self.assertEqual(raised.exception.reason, "pin_drift")
		with patch.object(builtin_reader, "source_profile", return_value=SimpleNamespace(major="15")):
			with self.assertRaises(ReaderFallback) as raised:
				builtin_reader.require_supported_source()
		self.assertEqual(raised.exception.reason, "unsupported_major")
		builtin_reader.require_supported_source()

	def test_every_field_the_reader_reads_is_covered_by_the_eligibility_check(self):
		for doctype, fields in builtin_reader.READER_READS.items():
			with self.subTest(doctype):
				self.assertLessEqual(
					fields, eligibility.READER_FIELDS[doctype], "a field the masking check misses"
				)

	def test_the_columns_a_read_selects_are_declared_in_both_places(self):
		"""The SQL a real read sends to the voucher tables names only declared and eligibility-covered fields."""
		seen = set()
		for side in SIDES:
			self.invoice(side, (100,), paid=10, credit=5)
			entry = self.fx.prefix + "FieldPE" + side
			self.raw("Field" + side, "Journal Entry", entry, "Journal Entry", entry, 10, side)
			frappe.db.set_value("Journal Entry", entry, "bill_no", "B")
			sync_outstanding(self.fx.company)
			queries, spy = self.spy_queries()
			with spy:
				self.reader_result(side)
			for query in queries:
				match = HEADER_TABLE.search(query)
				if not match or " JOIN " in query:
					continue
				doctype = match.group(1)
				seen.add(doctype)
				columns = set(re.findall(r"`([a-z_0-9]+)`", query.split(" ORDER BY ")[0]))
				self.assertLessEqual(columns, builtin_reader.READER_READS[doctype], query)
				self.assertLessEqual(columns, eligibility.READER_FIELDS[doctype], query)
		self.assertEqual(seen, {"Sales Invoice", "Purchase Invoice", "Journal Entry"})

	def test_an_ineligible_user_falls_back_before_any_ledger_read(self):
		self.invoice("Receivable")
		user = self.fx.user()
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": "Customer",
				"for_value": self.fx.customer,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)
		frappe.set_user(user)
		queries, spy = self.spy_queries()
		with spy, self.assertRaises(ReaderFallback) as raised:
			self.reader_result("Receivable")
		self.assertEqual(raised.exception.reason, "ineligible")
		self.assertEqual([q for q in queries if "tabPayment Ledger Entry" in q], [])

	def test_an_unknown_eligibility_state_falls_back_with_its_cause(self):
		self.invoice("Receivable")
		with patch.object(eligibility, "get_user_permissions", side_effect=RuntimeError("boom")):
			with self.assertRaises(ReaderFallback) as raised:
				self.reader_result("Receivable")
		self.assertEqual(raised.exception.reason, "eligibility_unknown")
		self.assertIsInstance(raised.exception.__cause__, RuntimeError)

	def test_a_job_deadline_in_the_eligibility_check_is_not_swallowed(self):
		from rq.timeouts import JobTimeoutException

		self.invoice("Receivable")
		for error in (TimeoutError, JobTimeoutException):
			with self.subTest(error.__name__):
				with patch.object(eligibility, "get_user_permissions", side_effect=error()):
					with self.assertRaises(error):
						self.reader_result("Receivable")
				with patch.object(compatibility, "reader_pin_drift", side_effect=error()):
					with self.assertRaises(error):
						builtin_reader.require_supported_source()

	def test_a_sales_person_scope_falls_back(self):
		self.invoice("Receivable")

		def scope(report):
			report.sales_person_records = frappe._dict()

		report_class = builtin_reader._builtin_class()
		with patch.object(report_class, "get_sales_invoices_or_customers_based_on_sales_person", scope):
			with self.assertRaises(ReaderFallback) as raised:
				self.reader_result("Receivable")
		self.assertEqual(raised.exception.reason, "sales_person_scope")

	def test_amounts_near_ten_billion_pass_the_control_total(self):
		"""Float sums of such amounts drift by micro-units: far below half a unit, so the check still holds."""

		def scenario(side):
			self.invoice(side, (12345678901.23, 9876543210.11), paid=1234567890.12)
			self.invoice(side, (11111111111.11,), paid=3333333333.33, credit=777777777.77)

		self.both_sides(scenario)

	def test_the_reader_never_refuses_too_large(self):
		self.invoice("Payable", (40, 60), paid=30)
		sync_outstanding(self.fx.company)
		with patch.object(sources, "MAX_VOUCHERS", 0):
			self.assertTrue(self.reader_result("Payable")["rows"])


@v16_only(GATE_NO_V15_READER_PINS)
class TestBuiltinReaderDiscovery(ReaderCase):
	"""Settled-in-window, FX-residue and multi-account discovery: every case reads what today's read reads."""

	def test_settled_before_window_start_with_term_in_window_is_found(self):
		"""The spike's miss: paid five days before the window opens, a term due inside it."""
		for side in SIDES:
			with self.subTest(side):
				invoice = self.settled(side)
				result = self.assert_reader_equals_today(side)
				self.assertIn(invoice, self.vouchers(result))

	def test_settled_invoice_due_in_window_by_its_header_only(self):
		for side in SIDES:
			with self.subTest(side):
				invoice = self.settled(side, schedule=False, header=15)
				result = self.assert_reader_equals_today(side)
				self.assertIn(invoice, self.vouchers(result))

	def test_settled_invoice_with_the_term_in_the_window_and_the_header_outside(self):
		for side in SIDES:
			with self.subTest(side):
				invoice = self.settled(side, term=12, header=90)
				result = self.assert_reader_equals_today(side)
				self.assertIn(invoice, self.vouchers(result))

	def test_settled_invoice_with_the_term_outside_the_window_and_the_header_inside(self):
		for side in SIDES:
			with self.subTest(side):
				self.settled(side, term=90, header=12)
				self.assert_reader_equals_today(side)

	def test_a_settled_invoice_whose_header_account_is_not_its_ledger_account_is_found(self):
		"""Today's rule looks at the header's dates and terms, never its account: discovery must not either."""
		for side in SIDES:
			with self.subTest(side):
				invoice = self.settled(side)
				elsewhere = self.fx.account("Hdr" + side, "Cash", "Asset")
				frappe.db.set_value(INVOICE_TYPES[side], invoice, SIDES_ACCOUNT_FIELD[side], elsewhere)
				result = self.assert_reader_equals_today(side)
				self.assertIn(invoice, self.vouchers(result))

	def test_window_ends_are_inclusive_for_terms_and_for_headers(self):
		"""One day inside each end is found, one day outside is not; a term and a header decide separately."""
		start, end = 10, 20
		window = (add_days(self.fx.today, start), add_days(self.fx.today, end))
		for side in SIDES:
			with self.subTest(side):
				inside = {
					self.settled(side, term=start, header=90),
					self.settled(side, term=end, header=90),
					self.settled(side, schedule=False, header=start),
					self.settled(side, schedule=False, header=end),
				}
				for day in (start - 1, end + 1):
					self.settled(side, term=day, header=90)
					self.settled(side, schedule=False, header=day)
				result = self.assert_reader_equals_today(side, window)
				self.assertEqual(self.vouchers(result), inside)

	def test_settled_foreign_rounding_single_term_invoice_is_repaired_at_invoice_level(self):
		for side in SIDES:
			with self.subTest(side):
				invoice = self.invoice(side, (5733.79,), gross_total=5733.78, paid=5733.78)
				doctype, account = INVOICE_TYPES[side], self.parties(side)[1]
				frappe.db.set_value("Account", account, "account_currency", "USD")
				frappe.db.set_value(
					doctype,
					invoice,
					{
						"currency": "USD",
						"party_account_currency": "USD",
						"conversion_rate": 92.9,
						"grand_total": 61.72,
						"rounded_total": 61.72,
						"base_grand_total": 5733.78,
						"base_rounded_total": 5733.79,
					},
				)
				doc = frappe.get_doc(doctype, invoice)
				doc.payment_schedule[0].invoice_portion = 100
				doc.set_payment_schedule()
				for term in doc.payment_schedule:
					term.db_update()
				serial = str(self.fx.serial)
				frappe.db.set_value(
					"Payment Ledger Entry",
					self.fx.prefix + "InvoicePLE" + serial,
					{"account_currency": "USD", "amount_in_account_currency": 61.72},
				)
				frappe.db.set_value(
					"Payment Ledger Entry",
					self.fx.prefix + "PaymentPLE" + serial,
					{"account_currency": "USD", "amount_in_account_currency": -5733.78 / 92.9},
				)
				result = self.assert_reader_equals_today(side)
				self.assertEqual([row["allocation_basis"] for row in result["rows"]], ["invoice"])

	def test_settled_multi_term_schedule_that_misses_the_ledger_is_refused_on_both(self):
		"""Terms that outrun the ledger: a closed invoice with an open term is refused by today's overlay, and here."""
		for side in SIDES:
			with self.subTest(side):
				self.invoice(side, (80, 70), gross_total=100, paid=100)
				self.assert_same_refusal(side, "settlement_schedule_mismatch")

	def test_a_settled_fx_leftover_is_excluded_and_disclosed(self):
		"""Foreign account at zero, company-currency residual 0.15: found by the FX read, outside the window."""
		for side in SIDES:
			with self.subTest(side):
				invoice = self.foreign_invoice(side, gross=8000.15, paid=8000)
				result = self.assert_reader_equals_today(side, (self.fx.today, self.fx.today))
				self.assertEqual([row["voucher_no"] for row in result["excluded_rows"]], [invoice])
				self.assertEqual(result["excluded_rows"][0]["remaining"], "0.15")
				self.assertEqual(result["notes"]["currency_residue_excluded"]["count"], 1)
				self.assertFalse(result["complete_for_company"])
				self.assertIn("currency_residue_excluded", result["scope_reasons"])

	def test_an_account_currency_residual_with_a_zero_company_residual_is_refused_like_today(self):
		"""Company currency nets to 0 and the header says 0, but 50 of the 100 USD is unpaid: today refuses."""
		for side in SIDES:
			with self.subTest(side):
				invoice = self.foreign_invoice(side, gross=8000, paid=8000, account_paid=50)
				# A header that was never updated for the account-currency line.
				self.pinned_headers[(INVOICE_TYPES[side], invoice)] = 0
				self.assert_same_refusal(
					side, "currency_reconciliation_unavailable", (self.fx.today, self.fx.today)
				)

	def refund_through_a_return(self, side, *, original_residue=0, refund=500.15):
		"""A settled foreign invoice and a return against it whose refund leaves the company-currency residue.

		The return's own line and the refund both name the return; ERPNext folds them onto the original's key.
		Their company-currency sum is `refund - 500`, their account-currency sum is 0. `original_residue` adds a
		company-currency residue on the original's own lines (the offset test makes the two cancel).
		"""
		original = self.foreign_invoice(side, gross=8000 + original_residue, paid=8000)
		doctype = INVOICE_TYPES[side]
		party = self.parties(side)[0]
		returned = self.fx.insert(
			doctype,
			"RefundReturn" + side,
			company=self.fx.company,
			**{PARTY_TYPES[side].lower(): party},
			posting_date=self.fx.today,
			docstatus=1,
			is_return=1,
			return_against=original,
			update_outstanding_for_self=0,
		)
		foreign = {"account_currency": "USD"}
		self.raw(
			"ReturnOwn" + side,
			doctype,
			returned,
			doctype,
			returned,
			-500,
			side,
			amount_in_account_currency=-500 / 80,
			**foreign,
		)
		self.raw(
			"RefundPay" + side,
			"Payment Entry",
			self.fx.prefix + "RefundPE" + side,
			doctype,
			returned,
			refund,
			side,
			amount_in_account_currency=500 / 80,
			**foreign,
		)
		return original, returned

	def test_an_fx_residue_that_exists_only_through_a_return_is_found(self):
		"""D7 groups by `against_voucher_no`, the return's name, while ERPNext sums the return onto the original.

		Safe because a non-zero sum of the folded groups needs a non-zero group, and `_close_returns` then reads
		the other side: the residue is found through the return and the original's lines are read with it.
		"""
		for side in SIDES:
			with self.subTest(side):
				original, returned = self.refund_through_a_return(side)
				self.sync()
				window = (self.fx.today, self.fx.today)
				expected = self.today_result(side, window)
				_, _, _, engine, _ = self.engine(side, window)
				actual = self.reader_result(side, window)
				_assert_equal(_canonical(actual), _canonical(expected), f"{side}: through a return")
				self.assertEqual([row["voucher_no"] for row in actual["excluded_rows"]], [original])
				self.assertEqual(actual["excluded_rows"][0]["remaining"], "0.15")
				# The return, not the original, carried the residue into discovery; the original came by closure.
				self.assertGreaterEqual(engine.reader_stats["counts"]["fx"], 1)

	def test_fx_residues_that_cancel_across_a_return_are_read_and_equal_today(self):
		"""The original's own residue and the return's residue offset: the folded key is clean, the read is exact."""
		for side in SIDES:
			with self.subTest(side):
				self.refund_through_a_return(side, original_residue=0.15, refund=499.85)
				self.assert_reader_equals_today(side, (self.fx.today, self.fx.today))

	def test_a_negative_fx_leftover_keeps_its_sign(self):
		for side in SIDES:
			with self.subTest(side):
				invoice = self.foreign_invoice(side, gross=8000, paid=8000.15)
				result = self.assert_reader_equals_today(side, (self.fx.today, self.fx.today))
				self.assertEqual([row["voucher_no"] for row in result["excluded_rows"]], [invoice])
				self.assertEqual(result["excluded_rows"][0]["remaining"], "-0.15")
				self.assertEqual(result["notes"]["currency_residue_excluded"]["amount"], "-0.15")

	def test_a_native_account_is_never_searched_for_fx_residues(self):
		self.invoice("Receivable", (100,), paid=100)
		sync_outstanding(self.fx.company)
		queries, spy = self.spy_queries()
		with spy:
			_, _, _, engine, _ = self.engine("Receivable", (self.fx.today, self.fx.today))
		self.assertEqual(engine.reader_stats["counts"]["fx"], 0)
		self.assertFalse([q for q in queries if "tabPayment Ledger Entry" in q and "HAVING SUM" in q])

	def test_a_closed_invoice_outside_the_window_on_two_accounts_is_refused(self):
		for side in SIDES:
			with self.subTest(side):
				self.two_account_invoice(side, paid=True)
				self.assert_same_refusal(side, "multiple_invoice_accounts", (self.fx.today, self.fx.today))

	def test_an_open_invoice_on_two_accounts_is_refused(self):
		for side in SIDES:
			with self.subTest(side):
				self.two_account_invoice(side, paid=False)
				self.assert_same_refusal(side, "multiple_invoice_accounts")

	def test_a_payment_on_another_account_against_a_closed_invoice_is_read_by_the_multi_account_read(self):
		"""The control-total fallback this once needed is gone: the invoice has lines on two keys, so all are read."""
		party, account = self.second_receivable()
		closed = self.invoice("Receivable", (100,), paid=100)
		self.fx.ple("Elsewhere", "Payment Entry", closed, -30, "Receivable", party, account)
		result = self.assert_reader_equals_today("Receivable", (self.fx.today, self.fx.today))
		self.assertEqual(Decimal(result["canonical_total"]), -30)

	def test_a_second_key_dated_on_the_anchor_counts_for_the_multi_account_read(self):
		"""The anchor day is inside the report's ledger: its only second-account line is dated that day."""
		for side in SIDES:
			with self.subTest(side):
				party, _ = self.parties(side)
				other = self.fx.account(
					"Anchor" + side, side, "Asset" if side == "Receivable" else "Liability"
				)
				invoice = self.invoice(side, (100,), paid=100, offset=-30)
				self.fx.ple("OwnOnAnchor" + side, INVOICE_TYPES[side], invoice, 50, side, party, other)
				self.assert_same_refusal(side, "multiple_invoice_accounts", (self.fx.today, self.fx.today))

	def test_two_keys_with_a_null_party_count_as_two(self):
		"""COUNT(DISTINCT account, party) skips a NULL party; the report keys such a line on (account, NULL)."""
		for side in SIDES:
			with self.subTest(side):
				party, account = self.parties(side)
				invoice = self.invoice(side, (100,), paid=100, offset=-30)
				self.fx.ple(
					"NullParty" + side,
					INVOICE_TYPES[side],
					invoice,
					50,
					side,
					party,
					account,
					posting_offset=-30,
				)
				frappe.db.set_value("Payment Ledger Entry", f"{self.fx.prefix}NullParty{side}", "party", None)
				self.fx.ple("NullPartyPay" + side, "Payment Entry", invoice, -50, side, party, account)
				frappe.db.set_value(
					"Payment Ledger Entry", f"{self.fx.prefix}NullPartyPay{side}", "party", None
				)
				self.assert_same_refusal(side, "multiple_invoice_accounts", (self.fx.today, self.fx.today))

	def test_one_account_and_one_party_is_not_a_multi_account_invoice(self):
		invoice = self.invoice("Receivable", (100,), paid=100, offset=-30)
		self.pay("Second", invoice, -10, "Receivable")
		self.sync()
		_, _, _, engine, _ = self.engine("Receivable", (self.fx.today, self.fx.today))
		self.assertEqual(engine.reader_stats["counts"]["multi"], 0)

	def test_a_missing_index_drops_the_hint_and_reads_the_same_figure(self):
		"""The hints are speed-ups only: without the index the reader asks for none and the figure is unchanged."""
		party, account = self.second_receivable()
		closed = self.invoice("Receivable", (100,), paid=100)
		self.fx.ple("Elsewhere", "Payment Entry", closed, -30, "Receivable", party, account)
		self.invoice("Receivable", (70,))
		queries, spy = self.spy_queries()
		hints_only = lambda table, index: index in builtin_reader.LEDGER_INDEXES  # noqa: E731
		with spy, patch.object(builtin_reader, "_index_exists", side_effect=hints_only):
			result = self.assert_reader_equals_today("Receivable", (self.fx.today, self.fx.today))
		self.assertTrue(queries)
		self.assertFalse([query for query in queries if "FORCE INDEX" in query.upper()])
		self.assertEqual(Decimal(result["canonical_total"]), Decimal(70) - 30)

	def test_the_hints_are_used_when_the_indexes_exist(self):
		self.invoice("Receivable", (70,))
		queries, spy = self.spy_queries()
		with spy, patch.object(builtin_reader, "_INDEXES", {}):
			self.assert_reader_equals_today("Receivable", (self.fx.today, self.fx.today))
		hinted = " ".join(query for query in queries if "FORCE INDEX" in query.upper())
		self.assertIn("PRIMARY", hinted)
		self.assertRegex(hinted, r"FORCE INDEX \(`?account`?\)")

	def test_a_missing_key_error_from_a_hinted_statement_is_a_fallback_never_a_figure(self):
		"""A stale answer of the index check: MariaDB 1176 on a hinted statement maps to `reader_error`."""
		party, account = self.second_receivable()
		closed = self.invoice("Receivable", (100,), paid=100)
		self.fx.ple("Elsewhere", "Payment Entry", closed, -30, "Receivable", party, account)
		self.sync()
		sql = frappe.db.sql

		def broken(query, *args, **kwargs):
			if "FORCE INDEX" in str(query).upper():
				raise frappe.db.OperationalError(
					1176, "Key 'account' doesn't exist in table 'tabPayment Ledger Entry'"
				)
			return sql(query, *args, **kwargs)

		with (
			patch.object(builtin_reader, "_index_exists", return_value=True),
			patch.object(frappe.db, "sql", broken),
			self.assertRaises(ReaderFallback) as raised,
		):
			self.reader_result("Receivable", (self.fx.today, self.fx.today))
		self.assertEqual(raised.exception.reason, "reader_error")

	def test_another_database_error_is_not_mapped_to_a_fallback(self):
		self.invoice("Receivable", (70,))
		self.sync()
		sql = frappe.db.sql

		def broken(query, *args, **kwargs):
			if "FORCE INDEX" in str(query).upper():
				raise frappe.db.OperationalError(1205, "Lock wait timeout exceeded")
			return sql(query, *args, **kwargs)

		with (
			patch.object(builtin_reader, "_index_exists", return_value=True),
			patch.object(frappe.db, "sql", broken),
			self.assertRaises(frappe.db.OperationalError),
		):
			self.reader_result("Receivable", (self.fx.today, self.fx.today))

	def test_the_index_check_asks_information_schema_once_per_process(self):
		with patch.object(builtin_reader, "_INDEXES", {}):
			queries, spy = self.spy_queries()
			with spy:
				self.assertTrue(builtin_reader._index_exists("tabPayment Ledger Entry", "account"))
				self.assertTrue(builtin_reader._index_exists("tabPayment Ledger Entry", "account"))
				self.assertFalse(builtin_reader._index_exists("tabPayment Ledger Entry", "no_such_index"))
		self.assertEqual(len([q for q in queries if "information_schema" in q]), 2)

	def test_due_window_discovery_query_count_is_bounded(self):
		"""120 settled invoices due in the window, chunks of 50: two discovery statements, ceil(n / 50) line reads."""
		count, chunk = 120, 50
		for _ in range(count):
			self.invoice("Receivable", (10,), schedules=False, paid=10, offset=-30)
		sync_outstanding(self.fx.company)
		queries, spy = self.spy_queries()
		with spy, patch.object(builtin_reader, "CHUNK", chunk):
			_, _, _, engine, _ = self.engine("Receivable")
		self.assertEqual(engine.reader_stats["counts"]["due_in_window"], count)
		self.assertEqual(engine.reader_stats["counts"]["open"], 0)
		# The invoice scope (`docstatus`, `posting_date`) tells discovery from today's own schedule read.
		discovery = [q for q in queries if "BETWEEN" in q and "`docstatus`" in q and "`posting_date`" in q]
		self.assertLessEqual(len(discovery), 2)
		line_reads = [
			q
			for q in queries
			if "tabPayment Ledger Entry" in q and re.search(r"`against_voucher_no`\s+IN", q)
		]
		self.assertEqual(len(line_reads), -(-count // chunk))


@v16_only(GATE_NO_V15_READER_PINS)
class TestBuiltinReaderLimits(ReaderCase):
	"""Memory caps, chunked statements, the ledger index and collation checks, and the error mapping."""

	def mixed_ledger(self, side):
		"""An invoice with a payment, and one generic line (a payment entry against itself)."""
		self.invoice(side, (100,), paid=30)
		entry = self.fx.prefix + "CapPE" + side
		self.raw("Cap" + side, "Payment Entry", entry, "Payment Entry", entry, -5, side)
		self.sync()

	def line_counts(self, side):
		_, _, _, engine, _ = self.engine(side)
		counts = engine.reader_stats["counts"]
		return counts["ledger_lines"], counts["generic_lines"]

	def test_the_generic_line_cap_is_exclusive_and_falls_back_with_a_reason(self):
		for side in SIDES:
			with self.subTest(side):
				self.mixed_ledger(side)
				total, generic = self.line_counts(side)
				self.assertEqual((total, generic), (3, 1))
				with patch.object(builtin_reader, "GENERIC_LINE_CAP", generic):
					self.assertTrue(self.reader_result(side)["rows"])
				with patch.object(builtin_reader, "GENERIC_LINE_CAP", generic - 1):
					with self.assertRaises(ReaderFallback) as raised:
						self.reader_result(side)
				self.assertEqual(raised.exception.reason, "too_many_lines")

	def test_the_total_line_cap_is_exclusive_and_falls_back_with_a_reason(self):
		for side in SIDES:
			with self.subTest(side):
				self.mixed_ledger(side)
				total, _ = self.line_counts(side)
				with patch.object(builtin_reader, "TOTAL_LINE_CAP", total):
					self.assertTrue(self.reader_result(side)["rows"])
				with patch.object(builtin_reader, "TOTAL_LINE_CAP", total - 1):
					with self.assertRaises(ReaderFallback) as raised:
						self.reader_result(side)
				self.assertEqual(raised.exception.reason, "too_many_lines")

	def test_a_line_cap_fallback_reads_no_ledger_line(self):
		self.mixed_ledger("Payable")
		queries, spy = self.spy_queries()
		with spy, patch.object(builtin_reader, "TOTAL_LINE_CAP", 0), self.assertRaises(ReaderFallback):
			self.reader_result("Payable")
		self.assertEqual([q for q in queries if "tabPayment Ledger Entry" in q and "GROUP BY" not in q], [])

	def test_the_party_rank_cap_is_exclusive(self):
		def line(day, party, name):
			return SimpleNamespace(posting_date=date(2026, 1, day), party=party, name=name)

		lines = [line(1, "a", "x"), line(1, "b", "y"), line(2, "c", "z")]
		with patch.object(builtin_reader, "RANK_PARTY_CAP", 3):
			self.assertEqual(len(builtin_reader.sort_like_the_report(lines)), 3)
		with patch.object(builtin_reader, "RANK_PARTY_CAP", 2):
			with self.assertRaises(ReaderFallback) as raised:
				builtin_reader.sort_like_the_report(lines)
		self.assertEqual(raised.exception.reason, "too_many_lines")

	def test_the_party_rank_cap_applies_to_a_real_read(self):
		self.invoice("Payable", (100,), paid=30)
		self.sync()
		with patch.object(builtin_reader, "RANK_PARTY_CAP", 0), self.assertRaises(ReaderFallback) as raised:
			self.reader_result("Payable")
		self.assertEqual(raised.exception.reason, "too_many_lines")

	def chunked_scenario(self, side):
		"""5 open invoices and 7 journals with bill numbers; lines tie on (date, party) in three groups."""
		for index in range(5):
			self.invoice(side, (50 + index,), paid=10)
		for index, day in enumerate((0, 0, -1, -1, -2, -2, -2)):
			entry = self.fx.prefix + f"ChunkJE{index}" + side
			self.raw(
				f"Chunk{index}" + side,
				"Journal Entry",
				entry,
				"Journal Entry",
				entry,
				10 + index,
				side,
				posting_date=add_days(self.fx.today, day),
			)
			frappe.db.set_value(
				"Journal Entry", entry, {"bill_no": f"B-{index}", "due_date": add_days(self.fx.today, 15)}
			)

	def test_chunked_detail_and_rank_statements_read_what_today_reads(self):
		"""DETAIL_CHUNK=2 and RANK_CHUNK=3 split every name list and every tie group of the rank statements."""
		for side in SIDES:
			with self.subTest(side):
				self.chunked_scenario(side)
				queries, spy = self.spy_queries()
				with (
					spy,
					patch.object(builtin_reader, "DETAIL_CHUNK", 2),
					patch.object(builtin_reader, "RANK_CHUNK", 3),
					patch.object(builtin_reader, "plain_ranks", return_value=None),
				):
					self.assert_ids_and_order_equal_today(side)
				ranked = [q for q in queries if "DENSE_RANK" in q]
				details = [q for q in queries if "FROM `tabJournal Entry`" in q and "`name` IN" in q]
				self.assertGreaterEqual(len(ranked), 3, "the tie groups did not split over statements")
				self.assertGreaterEqual(len(details), 4, "7 journals in chunks of 2")

	def test_ranks_of_groups_over_the_chunk_size_match_the_database_one_group_at_a_time(self):
		"""A group is never split: a group of 4 under RANK_CHUNK=3 is ranked alone, groups of 1-2 are batched."""
		collation = builtin_reader.column_collations(("name",))["name"]
		groups = [["b", "A", "a", "B"], ["z"], ["y", "x"], ["m", "M"]]
		expected = [builtin_reader.collation_ranks([group], collation)[0] for group in groups]
		with patch.object(builtin_reader, "RANK_CHUNK", 3):
			self.assertEqual(builtin_reader.collation_ranks(groups, collation), expected)

	def test_a_missing_composite_ledger_index_is_a_fallback(self):
		self.invoice("Payable", (100,))
		self.sync()
		for missing in builtin_reader.LEDGER_INDEXES:
			with self.subTest(missing):

				def exists(table, index, missing=missing):
					return index != missing

				with patch.object(builtin_reader, "_index_exists", side_effect=exists):
					with self.assertRaises(ReaderFallback) as raised:
						self.reader_result("Payable")
				self.assertEqual(raised.exception.reason, "reader_error")
				self.assertEqual(raised.exception.detail, ("ledger_index_missing",))

	def test_the_composite_ledger_indexes_exist_on_the_test_site(self):
		for index in builtin_reader.LEDGER_INDEXES:
			self.assertTrue(builtin_reader._index_exists(builtin_reader.LEDGER_TABLE, index), index)

	def test_column_collations_are_read_once_per_site_and_column(self):
		with patch.object(builtin_reader, "_COLLATIONS", {}):
			queries, spy = self.spy_queries()
			with spy:
				first = builtin_reader.column_collations(("party", "name"))
				again = builtin_reader.column_collations(("party", "name"))
				builtin_reader.column_collations(("name",))
				builtin_reader.column_collations(("party", "account"))
		self.assertEqual(first, again)
		self.assertEqual(len([q for q in queries if "information_schema" in q]), 2, "a new column asks once")

	def test_an_unsafe_collation_name_never_reaches_a_statement(self):
		queries, spy = self.spy_queries()
		with spy, self.assertRaises(ReaderFallback) as raised:
			builtin_reader.collation_ranks([["a", "b"]], "utf8mb4_bin; DROP TABLE x")
		self.assertEqual(raised.exception.reason, "reader_error")
		self.assertEqual(raised.exception.detail, ("unsafe_collation",))
		self.assertEqual(queries, [])
		with patch.object(builtin_reader, "_COLLATIONS", {}):
			with patch.object(frappe.db, "sql", return_value=[("party", "bad name")]):
				with self.assertRaises(ReaderFallback):
					builtin_reader.column_collations(("party",))
			self.assertEqual(builtin_reader._COLLATIONS, {}, "an unsafe name is not remembered")

	def test_a_decimal_error_in_reader_code_is_a_fallback_and_a_database_error_is_not(self):
		self.invoice("Payable")
		self.sync()
		with patch.object(builtin_reader, "ledger_control", side_effect=InvalidOperation("bad amount")):
			with self.assertRaises(ReaderFallback) as raised:
				self.reader_result("Payable")
		self.assertEqual(raised.exception.reason, "reader_error")
		self.assertIsInstance(raised.exception.__cause__, InvalidOperation)
		lock = frappe.db.OperationalError(1205, "Lock wait timeout exceeded")
		with patch.object(builtin_reader, "ledger_control", side_effect=lock):
			with self.assertRaises(frappe.db.OperationalError):
				self.reader_result("Payable")

	def test_a_company_without_side_accounts_is_a_fallback(self):
		report_class = builtin_reader._builtin_class()
		report = report_class.__new__(report_class)
		report.account_type, report.filters = "Receivable", frappe._dict(company=self.fx.company)
		with patch.object(frappe, "get_all", return_value=[]), self.assertRaises(ReaderFallback) as raised:
			report._side_accounts()
		self.assertEqual(raised.exception.reason, "reader_error")
		self.assertEqual(raised.exception.detail, ("no_side_accounts",))

	def test_the_multi_account_read_refuses_with_a_detail_off_mariadb(self):
		report_class = builtin_reader._builtin_class()
		report = report_class.__new__(report_class)
		with patch.object(frappe.db, "db_type", "postgres"), self.assertRaises(ReaderFallback) as raised:
			report._multi_account()
		self.assertEqual(
			(raised.exception.reason, raised.exception.detail), ("reader_error", ("not_mariadb",))
		)

	def test_the_rows_a_missing_key_builds_follow_the_filters(self):
		"""`_creates_row` mirrors upstream; two of its arms are dead under the engine's fixed filters."""
		report_class = builtin_reader._builtin_class()
		report = report_class.__new__(report_class)
		for against, filters, expected in (
			("Payroll Entry", {}, True),
			("Employee Advance", {}, False),
			("Employee Advance", {"handle_employee_advances": 1}, True),
			("Exchange Rate Revaluation", {}, False),
			("Exchange Rate Revaluation", {"for_revaluation_journals": 1}, True),
			("Journal Entry", {"handle_employee_advances": 1, "for_revaluation_journals": 1}, False),
		):
			with self.subTest(against=against, filters=filters):
				report.filters = frappe._dict(filters)
				self.assertEqual(
					bool(report._creates_row(SimpleNamespace(against_voucher_type=against))), expected
				)
		fixed = sources._engine_filters(self.fx.company, self.fx.today)
		self.assertFalse(fixed["for_revaluation_journals"])
		self.assertNotIn("handle_employee_advances", fixed)


@v16_only(GATE_NO_V15_READER_PINS)
class TestBuiltinReaderForks(ReaderCase):
	"""The reader copies four rules of today's read; each copy is checked against its original."""

	def test_the_reports_ledger_order_is_the_order_the_reader_sorts_by(self):
		_, _, _, engine, _ = self.engine("Payable")
		terms = [(field.name, order) for field, order in engine.ple_query._orderbys]
		self.assertEqual([name for name, _ in terms], list(builtin_reader.REPORT_ORDER))
		self.assertTrue(all(order in (None, Order.asc) for _, order in terms), "every key ascending")
		# The reader's own copy has none: it sorts once, in Python (see `_unordered_query`).
		self.assertEqual(engine._unordered_query()._orderbys, [])

	def test_sort_like_the_report_orders_by_each_report_key_in_turn(self):
		def line(day, party, name):
			return SimpleNamespace(posting_date=date(2026, 1, day), party=party, name=name)

		lines = [line(2, "a", "a"), line(1, "b", "a"), line(1, "a", "b"), line(1, "a", "a")]
		ordered = builtin_reader.sort_like_the_report(lines)
		keys = [(row.posting_date.day, row.party, row.name) for row in ordered]
		self.assertEqual(keys, [(1, "a", "a"), (1, "a", "b"), (1, "b", "a"), (2, "a", "a")])
		self.assertEqual(builtin_reader.REPORT_ORDER, ("posting_date", "party", "name"))

	def test_the_settled_rule_and_its_discovery_copy_are_pinned_together(self):
		"""`_due_in_window` finds the candidates of `_settled_in_window`: a change to either needs both audited."""
		report_class = builtin_reader._builtin_class()
		today = sources._report_class()
		found = {
			"sources._settled_in_window": sha256(getsource(today._settled_in_window).encode()).hexdigest(),
			"builtin_reader._due_in_window": sha256(
				getsource(report_class._due_in_window).encode()
			).hexdigest(),
		}
		self.assertEqual(
			found,
			SETTLED_RULE_PINS,
			"The settled-in-window rule or its discovery copy changed: audit both, then update the pins.",
		)

	def test_the_multi_account_statement_has_the_reports_ledger_predicate(self):
		for side, employee in (("Receivable", True), ("Payable", False)):
			with self.subTest(side):
				self.invoice(side, (100,), paid=10)
				self.sync()
				queries, spy = self.spy_queries()
				with spy:
					self.reader_result(side)
				(multi,) = [q for q in queries if "COUNT(DISTINCT" in q]
				squeezed = multi.replace(" ", "")
				for needle in ("`company`=", "`delinked`=0", "`against_voucher_type`=", "`posting_date`<="):
					self.assertIn(needle, squeezed)
				self.assertEqual(
					"`party_type`<>" in squeezed, employee, "the Employee criterion is Receivable's"
				)

	def test_the_multi_account_set_equals_the_two_key_invoices_of_the_ledger(self):
		"""Row-set check against a Python recount of the same predicate over the raw ledger rows."""
		for side in SIDES:
			with self.subTest(side):
				self.two_account_invoice(side, paid=True)
				self.invoice(side, (100,), paid=10)
				self.sync()
				# The refusal of a two-key invoice is lifted here to reach the engine and its read.
				with patch.object(
					builtin_reader._builtin_class(), "has_multiple_invoice_accounts", return_value=False
				):
					_, _, _, engine, _ = self.engine(side, (self.fx.today, self.fx.today))
				rows = frappe.get_all(
					"Payment Ledger Entry",
					filters={
						"company": self.fx.company,
						"account": ["in", engine.side_accounts],
						"delinked": 0,
						"against_voucher_type": engine.invoice_type,
					},
					fields=["against_voucher_no", "account", "party"],
				)
				keys = {}
				for row in rows:
					keys.setdefault(row.against_voucher_no, set()).add((row.account, row.party))
				expected = frozenset(name for name, found in keys.items() if len(found) > 1)
				self.assertTrue(expected)
				self.assertEqual(engine._multi_account(), expected)


@v16_only(GATE_NO_V15_READER_PINS)
class TestBuiltinReaderEntry(ReaderCase):
	"""The public entry: today's inputs, today's refusals, no size guard, and a fallback that is never a refusal."""

	def entry(self, side, window=None, as_of=None, **kwargs):
		start, end = self.window(window)
		return builtin_reader.read_obligations(
			self.fx.company, side, as_of if as_of is not None else self.fx.today, start, end, **kwargs
		)

	def today_as_of(self, side, window=None, as_of=None):
		start, end = self.window(window)
		as_of = as_of if as_of is not None else self.fx.today
		with patch.object(sources, "MAX_LEDGER_ROWS", 10**9), patch.object(sources, "MAX_VOUCHERS", 10**9):
			return sources.read_obligations(self.fx.company, side, as_of, start, end)

	def outcome(self, call):
		"""The exception a call raises, or the result it returns."""
		try:
			return call()
		except Exception as exc:  # the comparison of what was raised is the point of this helper
			return exc

	def assert_parity(self, side, expected, *, window=None, as_of=None):
		"""Reader and today raise the same type and the same refusal code; `expected` names that code or type."""
		self.sync()
		today = self.outcome(lambda: self.today_as_of(side, window, as_of))
		reader = self.outcome(lambda: self.entry(side, window, as_of))
		self.assertIsInstance(today, Exception, "today's read did not refuse")
		self.assertIsInstance(reader, Exception, "the reader did not refuse")
		self.assertNotIsInstance(reader, ReaderFallback)
		self.assertIs(type(reader), type(today))
		if isinstance(expected, str):
			self.assertEqual((today.code, reader.code), (expected, expected))
		else:
			self.assertIsInstance(today, expected)

	def refusal_rows(self):
		"""name -> (expected, setup(side), window, as_of, patchers); a setup builds the row's fixtures."""
		today = self.fx.today

		def with_invoice(side):
			self.invoice(side, (100,))

		def multibook(side):
			self.invoice(side, (100,))
			serial = self.fx.serial
			frappe.db.set_value(
				"Payment Ledger Entry",
				f"{self.fx.prefix}InvoicePLE{serial}",
				"finance_book",
				"Alternative book",
			)

		def foreign_account_residual(side):
			# Real documents and ledger lines, no patch of shared code: only the reader's narrower read set can
			# miss this invoice (company currency nets to 0, the stale header says 0, 50 of 100 USD is unpaid).
			invoice = self.foreign_invoice(side, gross=8000, paid=8000, account_paid=50)
			self.pinned_headers[(INVOICE_TYPES[side], invoice)] = 0

		def bad_fetch_method(side):
			with_invoice(side)
			frappe.db.set_single_value(
				"Accounts Settings", "receivable_payable_fetch_method", "Unknown Cursor"
			)
			frappe.clear_document_cache("Accounts Settings")

		def no_party_mapping(side):
			with_invoice(side)
			frappe.db.delete("Party Type", {"name": PARTY_TYPES[side]})

		def no_side_account(side):
			with_invoice(side)
			frappe.db.delete("Account", {"company": self.fx.company, "account_type": side})

		def report_class():
			from erpnext.accounts.report.accounts_receivable.accounts_receivable import (
				ReceivablePayableReport,
			)

			return ReceivablePayableReport

		def skewed_repair():
			from jarvis.payment_calendar.reconciliation import InvoiceReconciler

			real = InvoiceReconciler.repair

			def repair(self, *args, **kwargs):
				row = real(self, *args, **kwargs)
				row.outstanding += 1
				return row

			return patch.object(InvoiceReconciler, "repair", repair)

		return {
			"missing as_of": (frappe.ValidationError, with_invoice, None, "", []),
			"window start after end": (
				frappe.ValidationError,
				with_invoice,
				(add_days(today, 5), today),
				None,
				[],
			),
			"non-standard report": (
				frappe.PermissionError,
				with_invoice,
				None,
				None,
				[
					lambda: patch(
						"jarvis.payment_calendar.permissions.is_standard_accounts_report", return_value=False
					)
				],
			),
			"contract signature drift": (
				"source_contract_unavailable",
				with_invoice,
				None,
				None,
				[lambda: patch.object(report_class(), "build_data", lambda self, extra: None)],
			),
			"party type mapping missing": (
				"source_configuration_unavailable",
				no_party_mapping,
				None,
				None,
				[],
			),
			"no side account": ("source_configuration_unavailable", no_side_account, None, None, []),
			"multibook": ("multibook_unsupported", multibook, None, None, []),
			"multiple invoice accounts": (
				"multiple_invoice_accounts",
				lambda side: self.two_account_invoice(side, paid=True),
				None,
				None,
				[],
			),
			"currency reconciliation unavailable": (
				"currency_reconciliation_unavailable",
				with_invoice,
				None,
				None,
				[
					lambda: patch.object(
						sources._report_class(),
						"allocate_outstanding_based_on_payment_terms",
						lambda self, row: None,
					)
				],
			),
			"foreign account residual with a zero company residual and a zero header": (
				"currency_reconciliation_unavailable",
				foreign_account_residual,
				(today, today),
				None,
				[],
			),
			"settlement schedule mismatch": (
				"settlement_schedule_mismatch",
				lambda side: self.invoice(side, (80, 70), gross_total=100, paid=100),
				None,
				None,
				[],
			),
			"reconciliation mismatch": (
				"reconciliation_mismatch",
				lambda side: self.invoice(side, (80, 70), gross_total=100),
				None,
				None,
				[skewed_repair],
			),
			"invalid source amount": (
				"invalid_source_amount",
				with_invoice,
				None,
				None,
				[
					lambda: patch.object(
						sources, "money", side_effect=CalendarSourceUnavailable("invalid_source_amount")
					)
				],
			),
			"unsupported fetch method": (
				"source_configuration_unavailable",
				bad_fetch_method,
				None,
				None,
				[],
			),
		}

	def test_every_refusal_of_today_is_the_readers_refusal(self):
		for name, (expected, setup, window, as_of, patchers) in self.refusal_rows().items():
			for side in SIDES:
				with self.subTest(name, side=side):
					frappe.db.savepoint("refusal_row")
					try:
						setup(side)
						with ExitStack() as stack:
							for patcher in patchers:
								stack.enter_context(patcher())
							self.assert_parity(side, expected, window=window, as_of=as_of)
					finally:
						frappe.db.rollback(save_point="refusal_row")
						self.pinned_headers.clear()
						frappe.clear_document_cache("Accounts Settings")
						frappe.clear_cache()

	def test_an_unknown_account_type_is_refused_like_today(self):
		today = self.outcome(lambda: self.today_as_of("Bank"))
		reader = self.outcome(lambda: self.entry("Bank"))
		self.assertIsInstance(today, frappe.ValidationError)
		self.assertIs(type(reader), type(today))
		self.assertEqual(str(reader), str(today))

	def test_bad_input_is_refused_before_the_readers_own_gates(self):
		"""Today's input gates run first: bad input is today's refusal even when the major is unsupported."""
		with patch.object(builtin_reader, "source_profile", return_value=SimpleNamespace(major="15")):
			with self.assertRaises(frappe.ValidationError):
				self.entry("Receivable", window=(add_days(self.fx.today, 5), self.fx.today))

	def details_patched(self, change):
		"""The reader's invoice details with `change(details, invoice)` applied after the reader built them."""
		report_class = builtin_reader._builtin_class()
		real = report_class._narrow_invoice_details

		def narrow(engine, lines):
			details = real(engine, lines)
			change(details)
			return details

		return patch.object(report_class, "_narrow_invoice_details", narrow)

	def test_a_missing_invoice_details_entry_is_read_as_empty_by_the_upstream_code(self):
		"""The residual boundary (module docstring): upstream reads a missing entry as `{}`; nothing raises."""
		for side in SIDES:
			with self.subTest(side):
				invoice = self.invoice(side, (100,), paid=30)
				self.sync()
				with self.details_patched(lambda details: details.pop(invoice, None)):
					result = self.outcome(lambda: self.entry(side))
				self.assertNotIsInstance(result, Exception)
				self.assertEqual([row["remaining"] for row in result["rows"]], ["70.00"])

	def test_a_malformed_invoice_details_entry_is_todays_refusal_not_a_fallback(self):
		"""The residual boundary: a shape bug in reader-built input, hit inside upstream code, is a refusal."""
		for side in SIDES:
			with self.subTest(side):
				invoice = self.invoice(side, (100,), paid=30)
				self.sync()
				with self.details_patched(lambda details: details.update({invoice: None})):
					result = self.outcome(lambda: self.entry(side))
				self.assertIs(type(result), CalendarSourceUnavailable)
				self.assertNotIsInstance(result, ReaderFallback)

	def test_an_upstream_programming_error_is_todays_refusal_not_a_fallback(self):
		"""AttributeError / KeyError / TypeError from ERPNext or shared code map as in today's read."""
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		self.invoice("Receivable", (100,))
		self.sync()
		for error in (AttributeError("x"), KeyError("x"), TypeError("x")):
			with self.subTest(type(error).__name__):
				with patch.object(
					ReceivablePayableReport, "get_exchange_rate_revaluations", side_effect=error
				):
					today = self.outcome(lambda: self.today_as_of("Receivable"))
					reader = self.outcome(lambda: self.entry("Receivable"))
				self.assertIsInstance(today, CalendarSourceUnavailable)
				self.assertIs(type(reader), CalendarSourceUnavailable)
				self.assertEqual(reader.code, today.code)

	def test_an_error_in_the_shared_entry_code_is_todays_refusal_too(self):
		self.invoice("Receivable", (100,))
		self.sync()
		boom = TypeError("x")
		with (
			patch.object(sources, "validate_obligation_books", side_effect=boom),
			patch.object(builtin_reader, "validate_obligation_books", side_effect=boom),
		):
			today = self.outcome(lambda: self.today_as_of("Receivable"))
			reader = self.outcome(lambda: self.entry("Receivable"))
		self.assertIsInstance(today, CalendarSourceUnavailable)
		self.assertIs(type(reader), CalendarSourceUnavailable)
		self.assertEqual(reader.code, today.code)

	def test_a_bug_in_reader_only_code_is_a_fallback_not_a_refusal(self):
		self.invoice("Receivable", (100,))
		self.invoice("Receivable", (50,))
		self.sync()
		for error in (AttributeError("x"), KeyError("x"), TypeError("x")):
			for target in ("ledger_control", "sort_like_the_report"):
				with self.subTest(type(error).__name__, target=target):
					with patch.object(builtin_reader, target, side_effect=error):
						reader = self.outcome(lambda: self.entry("Receivable"))
					self.assertIsInstance(reader, ReaderFallback)
					self.assertEqual(reader.reason, "reader_error")
					self.assertNotIsInstance(reader, CalendarSourceUnavailable)

	def test_reader_never_refuses_too_large(self):
		for side in SIDES:
			with self.subTest(side):
				self.invoice(side, (40, 60), paid=30)
				self.invoice(side, (10,))
				self.sync()
				expected = self.today_as_of(side)
				with (
					patch.object(sources, "MAX_VOUCHERS", 0),
					patch.object(sources, "MAX_LEDGER_ROWS", 0),
				):
					start, end = self.window()
					with self.assertRaises(CalendarSourceUnavailable) as today:
						sources.read_obligations(self.fx.company, side, self.fx.today, start, end)
					self.assertEqual(today.exception.code, "too_large")
					actual = self.entry(side)
				_assert_equal(_canonical(actual), _canonical(expected), f"{side}: guards lifted")

	def mixed(self, side):
		"""Terms, an overlay, an invoice-level row, an excluded FX residue, a journal entry and a payment."""
		self.invoice(side, (40, 60), paid=30)
		self.settled(side)
		self.foreign_invoice(side, gross=8000.15, paid=8000)
		entry = self.fx.prefix + "MixedJE" + side
		self.raw("MixedJe" + side, "Journal Entry", entry, "Journal Entry", entry, 70, side)
		frappe.db.set_value(
			"Journal Entry", entry, {"bill_no": "B-1", "due_date": add_days(self.fx.today, 15)}
		)
		advance = self.fx.prefix + "MixedAdvance" + side
		self.raw("MixedPay" + side, "Payment Entry", advance, "Payment Entry", advance, -50, side)

	def test_result_contract_matches_today(self):
		def shape(value):
			if isinstance(value, dict):
				return {key: shape(item) for key, item in value.items()}
			if isinstance(value, list):
				return [shape(item) for item in value]
			return type(value).__name__

		def order(row):
			return (row["voucher_type"], row["voucher_no"], row["term"], row["due_date"] or "")

		for side in SIDES:
			with self.subTest(side):
				self.mixed(side)
				self.sync()
				window = (self.fx.today, add_days(self.fx.today, 40))
				expected, actual = self.today_as_of(side, window), self.entry(side, window)
				self.assertEqual(list(actual), list(expected))
				self.assertTrue(actual["rows"] and actual["excluded_rows"])
				for key in expected:
					if key != "rows":
						self.assertEqual(shape(actual[key]), shape(expected[key]), key)
				for row, today_row in zip(
					sorted(actual["rows"], key=order), sorted(expected["rows"], key=order), strict=True
				):
					self.assertEqual(list(row), list(today_row))
					self.assertEqual(shape(row), shape(today_row))
				_assert_equal(_canonical(actual), _canonical(expected), f"{side}: content")

	def test_row_ids_are_unique_and_deterministic(self):
		for side in SIDES:
			with self.subTest(side):
				self.mixed(side)
				self.sync()
				window = (self.fx.today, add_days(self.fx.today, 40))
				first, second = self.entry(side, window), self.entry(side, window)
				ids = [row["id"] for row in first["rows"]]
				self.assertEqual(ids, [row["id"] for row in second["rows"]])
				self.assertEqual(len(set(ids)), len(ids))
				for row in first["rows"]:
					key = (row["account"], row["voucher_type"], row["voucher_no"], row["party"])
					formulas = {
						sha256(f"{side}:{key}:{index}".encode()).hexdigest()[:24]
						for index in range(len(ids) + len(first["excluded_rows"]) + 5)
					}
					self.assertIn(row["id"], formulas)

	def test_unsupported_major_and_pin_drift_fall_back(self):
		self.invoice("Receivable", (100,))
		self.sync()
		with patch.object(builtin_reader, "source_profile", return_value=SimpleNamespace(major="15")):
			with self.assertRaises(ReaderFallback) as raised:
				self.entry("Receivable")
		self.assertEqual(raised.exception.reason, "unsupported_major")
		with patch.object(compatibility, "reader_pin_drift", return_value=["sources.x"]):
			with self.assertRaises(ReaderFallback) as raised:
				self.entry("Receivable")
		self.assertEqual(raised.exception.reason, "pin_drift")

	def test_stats_are_filled_and_carry_no_names(self):
		for side in SIDES:
			with self.subTest(side):
				self.mixed(side)
				self.sync()
				stats = {}
				self.entry(side, (self.fx.today, self.fx.today), stats=stats)
				self.assertEqual(set(stats), {"counts", "timings_ms", "control"})
				self.assertTrue(stats["control"]["passed"])
				self.assertGreater(stats["counts"]["lines"], 0)
				self.assertIn("control", stats["timings_ms"])
				text = json.dumps(stats)
				self.assertNotIn(self.fx.prefix, text)
				self.assertNotIn(self.fx.company, text)

	def test_the_pin_check_runs_once_per_read_and_is_timed(self):
		self.invoice("Receivable", (100,))
		self.sync()
		stats = {}
		with patch.object(compatibility, "reader_pin_drift", wraps=compatibility.reader_pin_drift) as pins:
			self.entry("Receivable", stats=stats)
		self.assertEqual(pins.call_count, 1, "the read and its eligibility check share one pin check")
		self.assertIn("pins", stats["timings_ms"])
		self.assertNotIn("fallback", stats)

	def test_a_fallback_reports_its_reason_and_fixed_detail_in_the_stats(self):
		self.invoice("Receivable", (100,))
		self.sync()
		cases = (
			(
				"pin_drift",
				["sources.x", "sources.y"],
				compatibility,
				"reader_pin_drift",
				["sources.x", "sources.y"],
			),
			(
				"ineligible",
				["column_filter", "user_permission"],
				builtin_reader,
				"reader_ineligibility",
				["column_filter", "user_permission"],
			),
		)
		for reason, detail, module, name, returned in cases:
			with self.subTest(reason):
				stats = {}
				with patch.object(module, name, return_value=returned):
					with self.assertRaises(ReaderFallback) as raised:
						self.entry("Receivable", stats=stats)
				self.assertEqual(raised.exception.reason, reason)
				self.assertEqual(raised.exception.detail, tuple(detail))
				self.assertEqual((stats["fallback"], stats["fallback_detail"]), (reason, detail))
				self.assertTrue(stats["timings_ms"], "timings survive the fallback")

	def test_a_fallback_before_the_engine_exists_is_reported_in_the_stats_too(self):
		self.invoice("Receivable", (100,))
		self.sync()
		stats = {}
		early = ReaderFallback("reader_error")
		with patch.object(builtin_reader, "validate_obligation_books", side_effect=early):
			with self.assertRaises(ReaderFallback):
				self.entry("Receivable", stats=stats)
		self.assertEqual((stats["fallback"], stats["fallback_detail"]), ("reader_error", []))

	def test_stats_survive_a_fallback(self):
		"""A control-total fallback still reports what it measured (for the caller's log)."""
		self.invoice("Receivable", (100,))
		self.sync()
		stats = {}
		with patch.object(builtin_reader, "control_total_matches", return_value=False):
			with self.assertRaises(ReaderFallback) as raised:
				self.entry("Receivable", stats=stats)
		self.assertEqual(raised.exception.reason, "control_total")
		self.assertFalse(stats["control"]["passed"])

	def test_keys_the_caller_put_in_the_stats_dict_survive_the_read(self):
		self.invoice("Receivable", (100,))
		self.sync()
		for fails in (False, True):
			with self.subTest(fallback=fails):
				stats = {"label": "caller's", "counts": {"stale": 1}}
				with patch.object(builtin_reader, "control_total_matches", return_value=not fails):
					self.outcome(lambda: self.entry("Receivable", stats=stats))
				self.assertEqual(stats["label"], "caller's")
				self.assertNotIn("stale", stats["counts"])
				expected = {"label", "counts", "timings_ms", "control"} | (
					{"fallback", "fallback_detail"} if fails else set()
				)
				self.assertEqual(set(stats), expected)
				self.assertEqual(stats["control"]["passed"], not fails)


@v16_only(GATE_NO_V15_READER_PINS)
class TestPinCheckErrors(ReaderCase):
	"""Any error of the pin check is a pin drift; a lost view is not special (the view's exit check fails the job)."""

	def test_any_failed_statement_in_the_pin_check_is_a_pin_drift(self):
		for error in (
			frappe.db.OperationalError(1054, "Unknown column"),
			frappe.db.OperationalError(2013, "lost"),
		):
			with self.subTest(error.args[0]):
				with patch.object(compatibility, "reader_pin_drift", side_effect=error):
					with self.assertRaises(ReaderFallback) as raised:
						builtin_reader.require_supported_source()
				self.assertEqual(raised.exception.reason, "pin_drift")
				self.assertIs(raised.exception.__cause__, error)


class FakeClock:
	"""A `monotonic` that moves only when a test says so."""

	def __init__(self, now=0.0):
		self.now = now

	def __call__(self):
		return self.now


@v16_only(GATE_NO_V15_READER_PINS)
class TestReaderDeadline(ReaderCase):
	"""The reader's own time limit: a cooperative check that ends in a fallback, never a `TimeoutError`."""

	def entry(self, side, **kwargs):
		start, end = self.window()
		return builtin_reader.read_obligations(self.fx.company, side, self.fx.today, start, end, **kwargs)

	def test_a_past_deadline_falls_back_before_the_ledger_control(self):
		self.invoice("Receivable")
		self.sync()
		stats = {}
		with patch.object(builtin_reader, "ledger_control") as control:
			with self.assertRaises(ReaderFallback) as raised:
				self.entry("Receivable", stats=stats, deadline=monotonic() - 1)
		control.assert_not_called()
		self.assertEqual(raised.exception.reason, "deadline")
		self.assertNotIsInstance(raised.exception, TimeoutError)
		self.assertEqual(raised.exception.refusal_code, "too_large")
		self.assertEqual(stats["fallback"], "deadline")

	def test_no_deadline_and_a_far_deadline_change_nothing(self):
		for side in SIDES:
			with self.subTest(side):
				self.invoice(side, (100,), paid=30)
				self.sync()
				expected = self.today_result(side)
				for deadline in (None, monotonic() + 3600):
					with self.subTest(deadline=deadline):
						actual = self.entry(side, deadline=deadline)
						_assert_equal(
							_canonical(actual), _canonical(expected), "the deadline changed the read"
						)
						self.assertEqual(
							[r["id"] for r in actual["rows"]], [r["id"] for r in expected["rows"]]
						)

	def test_a_deadline_crossed_in_a_phase_stops_at_the_next_timed_step(self):
		self.invoice("Receivable")
		self.sync()
		clock = FakeClock()
		control = builtin_reader.ledger_control

		def slow_control(*args, **kwargs):
			result = control(*args, **kwargs)
			clock.now = 100
			return result

		report_class = builtin_reader._builtin_class()
		with (
			patch.object(builtin_reader, "monotonic", clock),
			patch.object(builtin_reader, "ledger_control", slow_control),
			patch.object(report_class, "_discover") as discover,
			self.assertRaises(ReaderFallback) as raised,
		):
			self.entry("Receivable", deadline=50)
		discover.assert_not_called()
		self.assertEqual(raised.exception.reason, "deadline")

	def test_a_deadline_crossed_in_the_invoice_line_loop_stops_the_next_chunk(self):
		for _ in range(3):
			self.invoice("Receivable", (100,))
		self.sync()
		clock, reads = FakeClock(), []
		report_class = builtin_reader._builtin_class()
		read = report_class._read

		def counting_read(engine, criterion):
			reads.append(criterion)
			result = read(engine, criterion)
			clock.now = 100
			return result

		with (
			patch.object(builtin_reader, "monotonic", clock),
			patch.object(builtin_reader, "CHUNK", 1),
			patch.object(report_class, "_read", counting_read),
			self.assertRaises(ReaderFallback) as raised,
		):
			self.entry("Receivable", deadline=50)
		self.assertEqual(raised.exception.reason, "deadline")
		self.assertEqual(len(reads), 1, "a chunk was read after the deadline")

	def test_a_deadline_crossed_in_the_invoice_detail_loop_stops_the_next_chunk(self):
		for _ in range(3):
			self.invoice("Receivable", (100,))
		self.sync()
		clock, details = FakeClock(), []
		sql = frappe.db.sql

		def spy(query, *args, **kwargs):
			result = sql(query, *args, **kwargs)
			if "`po_no`" in str(query) and "`tabSales Invoice`" in str(query):
				details.append(str(query))
				clock.now = 100
			return result

		with (
			patch.object(builtin_reader, "monotonic", clock),
			patch.object(builtin_reader, "DETAIL_CHUNK", 1),
			patch.object(frappe.db, "sql", spy),
			self.assertRaises(ReaderFallback) as raised,
		):
			self.entry("Receivable", deadline=50)
		self.assertEqual(raised.exception.reason, "deadline")
		self.assertEqual(len(details), 1, "a detail chunk was read after the deadline")


def fake_engine(data=0, excluded=0, terms=0):
	"""An engine with only what the row cap reads."""
	return SimpleNamespace(
		data=[object()] * data,
		excluded=[object()] * excluded,
		terms={("k", index): [object()] for index in range(terms)},
		reader_stats={"counts": {}, "timings_ms": {}},
	)


class TestRowCap(TestCase):
	def test_the_cap_counts_data_excluded_rows_and_terms_and_is_exclusive(self):
		engine = fake_engine(data=2, excluded=2, terms=2)
		with patch.object(builtin_reader, "READER_MAX_ROWS", 6):
			builtin_reader._check_row_cap(engine)
		self.assertEqual(engine.reader_stats["counts"]["assemble_rows"], 6)
		with patch.object(builtin_reader, "READER_MAX_ROWS", 5), self.assertRaises(ReaderFallback) as raised:
			builtin_reader._check_row_cap(engine)
		self.assertEqual(raised.exception.reason, "assemble_budget")

	def test_there_is_no_estimator_left(self):
		for name in (
			"_check_assemble_budget",
			"_measured_hook_ms",
			"HOOKED_CHECK_MS",
			"PLAIN_CHECK_MS",
			"HOOK_COST_MARGIN",
			"CALIBRATION_CHECKS",
			"CALIBRATION_S",
		):
			self.assertFalse(hasattr(builtin_reader, name), name)


@v16_only(GATE_NO_V15_READER_PINS)
class TestPermissionDeadline(ReaderCase):
	"""`sources._assemble` is held to the reader's time inside its per-document permission checks."""

	def entry(self, side="Receivable", **kwargs):
		start, end = self.window()
		return builtin_reader.read_obligations(self.fx.company, side, self.fx.today, start, end, **kwargs)

	def setUp(self):
		super().setUp()
		for _ in range(3):
			self.invoice("Receivable", (100,))
		self.sync()
		self.set_hooks({"*": ["app.hooks.allow"]})

	def set_hooks(self, hooks):
		"""The registered has_permission hooks, as the wrapper and `visible_names` both see them."""
		real = permissions.effective_hooks

		def effective(name, user=None):
			# Only the has_permission kind is replaced: the scope rule reads the query hooks through the same function.
			return hooks if name == "has_permission" else real(name, user)

		for target in (builtin_reader, permissions):
			patcher = patch.object(target, "effective_hooks", effective)
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_a_slow_hook_stops_at_the_hard_stop_plus_one_check(self):
		def slow(*args, **kwargs):
			sleep(0.05)
			return True

		stop = 0.3
		started = monotonic()
		with (
			patch("frappe.has_permission", slow),
			patch.object(builtin_reader, "ASSEMBLE_GRACE_S", stop),
			self.assertRaises(ReaderFallback) as raised,
		):
			# The reader's own deadline is already behind us once its read is done: only the grace is left.
			self.entry(deadline=started)
		self.assertEqual(raised.exception.reason, "deadline")
		self.assertNotIsInstance(raised.exception, TimeoutError)
		self.assertLess(monotonic() - started, 30, "the whole read is bounded")

	def test_the_stop_is_exact_on_a_fake_clock(self):
		for _ in range(6):
			self.invoice("Receivable", (100,))
		self.sync()
		clock, calls = FakeClock(48.0), []

		def check(*args, **kwargs):
			calls.append(clock.now)
			clock.now += 1.0
			return True

		with (
			patch("frappe.has_permission", check),
			patch.object(builtin_reader, "monotonic", clock),
			patch.object(builtin_reader, "ASSEMBLE_GRACE_S", 2),
			self.assertRaises(ReaderFallback) as raised,
		):
			self.entry(deadline=50)
		self.assertEqual(raised.exception.reason, "deadline")
		# The hard stop is 50 + 2: the checks at 48, 49, 50 and 51 run, the one at 52 is refused.
		self.assertEqual(calls, [48.0, 49.0, 50.0, 51.0])

	def test_without_a_hook_nothing_is_wrapped(self):
		seen = []
		real = sources._assemble

		def spy(*args, **kwargs):
			seen.append(frappe.has_permission)
			return real(*args, **kwargs)

		original = frappe.has_permission
		self.set_hooks({})
		with patch.object(sources, "_assemble", spy):
			self.entry(deadline=monotonic() + 60)
		self.assertEqual(seen, [original], "no wrapper cost without a hook")

	def test_with_a_hook_the_wrapper_is_installed_only_during_assemble_and_removed_after(self):
		seen = []
		real = sources._assemble

		def spy(*args, **kwargs):
			seen.append(frappe.has_permission)
			return real(*args, **kwargs)

		original = frappe.has_permission
		with patch.object(sources, "_assemble", spy):
			self.entry(deadline=monotonic() + 60)
		self.assertNotEqual(seen[0], original)
		self.assertIs(frappe.has_permission, original)

	def test_the_wrapper_is_removed_when_the_read_stops(self):
		original = frappe.has_permission
		with self.assertRaises(ReaderFallback):
			self.entry(deadline=monotonic() - 100)
		self.assertIs(frappe.has_permission, original)

	def test_when_in_time_visible_names_runs_for_every_row_and_the_result_is_todays(self):
		"""I1: the filter is never skipped while time lasts."""
		calls = []
		real = permissions.visible_names

		def spy(doctype, names, filters=None):
			calls.append((doctype, tuple(sorted(set(names)))))
			return real(doctype, names, filters)

		expected = self.today_result("Receivable")
		with patch.object(sources, "visible_names", spy):
			actual = self.entry(deadline=monotonic() + 60)
		_assert_equal(_canonical(actual), _canonical(expected), "the deadline wrapper changed the read")
		vouchers = {row["voucher_no"] for row in actual["rows"]}
		checked = {name for doctype, names in calls if doctype == "Sales Invoice" for name in names}
		self.assertLessEqual(vouchers, checked)
		self.assertTrue(calls)

	def test_a_past_stop_raises_in_the_permission_step_after_the_read_completed(self):
		real = sources._assemble
		inside = []
		clock = FakeClock()

		def spy(*args, **kwargs):
			inside.append(True)
			return real(*args, **kwargs)

		# The reader's own steps see a far deadline; only the checks inside `_assemble` see the clock jump.
		def jump(*args, **kwargs):
			if inside:
				clock.now = 10_000
			return True

		with (
			patch.object(builtin_reader, "monotonic", clock),
			patch("frappe.has_permission", jump),
			patch.object(sources, "_assemble", spy),
			self.assertRaises(ReaderFallback) as raised,
		):
			self.entry(deadline=50)
		self.assertEqual(raised.exception.reason, "deadline")
		self.assertTrue(inside, "the read got as far as the permission filter")


@v16_only(GATE_NO_V15_READER_PINS)
class TestRowCapInTheRead(ReaderCase):
	"""The cap sits between the engine and `sources._assemble`."""

	def entry(self, side, **kwargs):
		start, end = self.window()
		return builtin_reader.read_obligations(self.fx.company, side, self.fx.today, start, end, **kwargs)

	def test_a_refused_cap_never_reaches_assemble(self):
		self.invoice("Receivable", (100,))
		self.sync()
		stats = {}
		with (
			patch.object(builtin_reader, "READER_MAX_ROWS", 0),
			patch.object(sources, "_assemble", wraps=sources._assemble) as assemble,
			self.assertRaises(ReaderFallback) as raised,
		):
			self.entry("Receivable", stats=stats, deadline=monotonic() + 30)
		assemble.assert_not_called()
		self.assertEqual(raised.exception.reason, "assemble_budget")
		self.assertEqual(stats["fallback"], "assemble_budget")

	def test_within_the_cap_the_read_is_assembled_and_equals_today(self):
		self.invoice("Receivable", (100,), paid=30)
		self.sync()
		expected = self.today_result("Receivable")
		with patch.object(sources, "_assemble", wraps=sources._assemble) as assemble:
			actual = self.entry("Receivable", deadline=monotonic() + 30)
		assemble.assert_called_once()
		_assert_equal(_canonical(actual), _canonical(expected), "the cap changed the read")

	def test_without_a_deadline_there_is_no_cap(self):
		self.invoice("Receivable", (100,))
		self.sync()
		with patch.object(builtin_reader, "READER_MAX_ROWS", 0):
			self.assertTrue(self.entry("Receivable")["rows"])


@v16_only(GATE_NO_V15_READER_PINS)
class TestReaderTransactions(TestCase):
	"""Committed fixtures: `consistent_read` rolls back first, so savepoint fixtures would vanish."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		self.fx = CalendarFixtures()
		for side in SIDES:
			self.fx.invoice((40, 60), side=side, paid=30, credit=5)
		sync_outstanding(self.fx.company)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for doctype, name in reversed(self.fx.created):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		frappe.clear_cache()

	def test_reader_takes_no_transaction_control(self):
		for side in SIDES:
			with self.subTest(side):
				with snapshot.consistent_read():
					queries, sql = [], frappe.db.sql

					def spy(query, *args, **kwargs):
						queries.append(str(query))
						return sql(query, *args, **kwargs)

					report, start, end, anchor, values = sources._require_inputs(
						self.fx.company, side, self.fx.today, self.fx.today, add_days(self.fx.today, 40)
					)
					with patch.object(frappe.db, "sql", spy):
						engine = sources._run_engine(
							builtin_reader._builtin_class(),
							sources._engine_filters(self.fx.company, anchor),
							side,
							start,
							end,
						)
						result = sources._assemble(engine, report, side, start, end, values)
				self.assertTrue(result["rows"])
				self.assertTrue(queries)
				for query in queries:
					self.assertRegex(
						query.lstrip(), r"(?i)^(select|with)\b", "a statement that is not a read"
					)


@v16_only(GATE_NO_V15_READER_PINS)
class TestReaderInsideCollectView(TestCase):
	"""Committed fixtures like `TestReaderTransactions`: `consistent_read` rolls back before it starts."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		self.fx = CalendarFixtures()
		for side in SIDES:
			self.fx.invoice((100,), side=side)
		self.fx.fiscal_year()
		self.fx.gl(1000)
		sync_outstanding(self.fx.company)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for doctype, name in reversed(self.fx.created):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		frappe.cache.hdel("fiscal_years", self.fx.company)
		frappe.clear_document_cache("Company", self.fx.company)
		frappe.clear_cache()

	def contract_failure(self, read):
		"""None when collect's source contract holds for `read` inside the fixed view, else the refusal code."""
		from erpnext.accounts.utils import get_currency_precision

		from jarvis.payment_calendar.banks import read_bank_balances

		window = (self.fx.today, add_days(self.fx.today, 40))
		with snapshot.consistent_read() as hits:
			bank = read_bank_balances(self.fx.company, [self.fx.bank], self.fx.today)
			sides = {side: read(self.fx.company, side, self.fx.today, *window) for side in SIDES}
			precision = get_currency_precision() or 2
			currency = frappe.get_cached_value("Company", self.fx.company, "default_currency")
			try:
				snapshot.check_source_contract(
					hits, sides, bank, currency, precision, compatibility.source_profile()
				)
			except CalendarSourceUnavailable as refusal:
				return refusal.code
		return None

	def test_reader_passes_collects_source_contract(self):
		# The premise: today's read meets the contract in the same view, so a refusal below is the reader's.
		self.assertIsNone(self.contract_failure(sources.read_obligations))
		self.assertIsNone(self.contract_failure(builtin_reader.read_obligations))


# Names of the modules that only the S2 router and the shadow may use (T8 pins the rest of the wiring).
UNWIRED = ("builtin_reader", "eligibility")
DOTTED_PATH = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+")


def wiring_references(source: str, modules: tuple[str, ...] = UNWIRED) -> list[str]:
	"""Where `source` imports or names an unwired module: an import, or a dotted-path string.

	A dotted-path string is how hooks.py and `frappe.enqueue` call code without importing it; a docstring or
	a prose string (with spaces) is not one.
	"""
	found = []
	for node in ast.walk(ast.parse(source)):
		if isinstance(node, ast.Import):
			names = [alias.name for alias in node.names]
		elif isinstance(node, ast.ImportFrom):
			module = node.module or ""
			names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
		elif isinstance(node, ast.Constant) and isinstance(node.value, str):
			names = [node.value] if DOTTED_PATH.fullmatch(node.value) else []
		else:
			continue
		found += [name for name in names if set(name.split(".")) & set(modules)]
	return found


# The payment-calendar modules that carry the S2 wiring; `snapshot` may import `routing` and nothing else of them.
READER_WIRING = ("routing", "shadow", "builtin_reader", "eligibility")


def imported_wiring_modules(source: str) -> set[str]:
	"""The READER_WIRING modules that `source` imports (not strings: those are how hooks and jobs name code)."""
	found = set()
	for node in ast.walk(ast.parse(source)):
		if isinstance(node, ast.Import):
			names = [alias.name for alias in node.names]
		elif isinstance(node, ast.ImportFrom):
			module = node.module or ""
			names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
			if node.level:
				names += [alias.name for alias in node.names]
		else:
			continue
		found |= {part for name in names for part in name.split(".")} & set(READER_WIRING)
	return found


def scheduler_entries(value) -> list[str]:
	"""Every string under `scheduler_events` that names a payment-calendar module."""
	if isinstance(value, str):
		return [value] if "payment_calendar" in value else []
	if isinstance(value, dict):
		value = list(value.values())
	return (
		[name for item in value for name in scheduler_entries(item)]
		if isinstance(value, (list, tuple))
		else []
	)


class TestReaderCallers(TestCase):
	def test_no_runtime_module_imports_or_names_the_reader(self):
		app = Path(builtin_reader.__file__).resolve().parents[1]
		own = {Path(builtin_reader.__file__).resolve(), Path(eligibility.__file__).resolve()}
		scanned, offenders = 0, {}
		for path in sorted(app.rglob("*.py")):
			path = path.resolve()
			if "tests" in path.relative_to(app).parts or path in own:
				continue
			scanned += 1
			found = wiring_references(path.read_text())
			if found:
				offenders[str(path.relative_to(app))] = found
		self.assertGreater(scanned, 100)
		self.assertTrue((app / "hooks.py").exists())
		# S2: the router (`routing`) and the shadow name the reader. S4b: the per-company cache builds from the
		# reader's engine and checks the same eligibility, so its three builder modules are the only other callers.
		cache_builders = {
			"payment_calendar/cache_eligibility.py",
			"payment_calendar/cache_refresh.py",
			"payment_calendar/cache_shadow.py",
		}
		self.assertEqual(
			set(offenders) - {"payment_calendar/routing.py", "payment_calendar/shadow.py"} - cache_builders,
			set(),
		)
		self.assertIn("payment_calendar/routing.py", offenders)

	def test_only_hooks_names_the_shadow_and_nothing_imports_it(self):
		app = Path(builtin_reader.__file__).resolve().parents[1]
		shadow_file = Path(app / "payment_calendar" / "shadow.py").resolve()
		offenders = {}
		for path in sorted(app.rglob("*.py")):
			path = path.resolve()
			if "tests" in path.relative_to(app).parts or path == shadow_file:
				continue
			found = wiring_references(path.read_text(), ("shadow",))
			if found:
				offenders[str(path.relative_to(app))] = found
		# S4b: `cache_shadow` imports the shadow for its `lifted_guards`; no other module may.
		self.assertEqual(
			offenders,
			{
				"hooks.py": ["jarvis.payment_calendar.shadow.dispatch"],
				"payment_calendar/cache_shadow.py": ["jarvis.payment_calendar.shadow"],
			},
		)

	def test_hooks_run_the_shadow_from_exactly_one_scheduler_entry_and_name_nothing_else_of_the_reader(self):
		from jarvis import hooks

		source = (Path(hooks.__file__)).read_text()
		self.assertEqual(wiring_references(source, ("builtin_reader", "eligibility", "routing")), [])
		entries = scheduler_entries(hooks.scheduler_events)
		self.assertEqual(
			[entry for entry in entries if entry.startswith("jarvis.payment_calendar.shadow")],
			["jarvis.payment_calendar.shadow.dispatch"],
		)
		# S4b adds one more payment-calendar entry: the cache's own tick. It names no part of the reader.
		self.assertEqual(
			sorted(entry for entry in entries if entry.startswith("jarvis.payment_calendar.")),
			["jarvis.payment_calendar.cache.tick", "jarvis.payment_calendar.shadow.dispatch"],
		)
		crons = [
			cron
			for cron, methods in hooks.scheduler_events["cron"].items()
			if "jarvis.payment_calendar.shadow.dispatch" in methods
		]
		self.assertEqual(crons, ["*/15 2-5 * * *"])
		self.assertEqual(source.count("payment_calendar.shadow"), 1)

	def test_snapshot_imports_the_router_and_none_of_the_other_wiring(self):
		app = Path(builtin_reader.__file__).resolve().parents[1]
		self.assertEqual(
			imported_wiring_modules((app / "payment_calendar" / "snapshot.py").read_text()), {"routing"}
		)
		# The router never imports the shadow: the shadow imports the router.
		self.assertNotIn(
			"shadow", imported_wiring_modules((app / "payment_calendar" / "routing.py").read_text())
		)

	def test_the_import_and_hook_scans_see_each_way_to_wire(self):
		imports = {
			"from-import": (
				"from jarvis.payment_calendar import routing, builtin_reader",
				{"routing", "builtin_reader"},
			),
			"from-module": ("from jarvis.payment_calendar.shadow import dispatch", {"shadow"}),
			"import": ("import jarvis.payment_calendar.eligibility", {"eligibility"}),
			"relative": ("from . import builtin_reader", {"builtin_reader"}),
			"nested": ("def f():\n\tfrom jarvis.payment_calendar import shadow", {"shadow"}),
			"none": ("from jarvis.payment_calendar import sources", set()),
		}
		for label, (source, expected) in imports.items():
			with self.subTest(label):
				self.assertEqual(imported_wiring_modules(source), expected)
		events = {
			"cron": {
				"* * * * *": ["jarvis.payment_calendar.shadow.dispatch", "x.y"],
				"daily": ["jarvis.payment_calendar.other.f"],
			}
		}
		self.assertEqual(
			scheduler_entries(events),
			["jarvis.payment_calendar.shadow.dispatch", "jarvis.payment_calendar.other.f"],
		)

	def test_the_scan_sees_every_way_to_wire_a_module(self):
		wired = {
			"import": "import jarvis.payment_calendar.builtin_reader",
			"from-import": "from jarvis.payment_calendar import eligibility",
			"from-module": "from jarvis.payment_calendar.builtin_reader import read_obligations",
			"relative": "from . import builtin_reader",
			"relative-module": "from .eligibility import reader_ineligibility",
			"hook": "scheduler_events = {'daily': ['jarvis.payment_calendar.builtin_reader.read_obligations']}",
			"enqueue": "frappe.enqueue('jarvis.payment_calendar.eligibility.reader_ineligibility')",
			"module-name": "importlib.import_module('jarvis.payment_calendar.builtin_reader')",
		}
		for label, source in wired.items():
			with self.subTest(label):
				self.assertTrue(wiring_references(source), source)
		quiet = (
			"from jarvis.payment_calendar import sources",
			'"""The built-in reader (builtin_reader) is not wired."""',
			"message = 'see the eligibility rules'",
		)
		for source in quiet:
			with self.subTest(source):
				self.assertEqual(wiring_references(source), [])
