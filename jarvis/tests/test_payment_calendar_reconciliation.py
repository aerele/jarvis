"""Regressions for the populated-site failures, using the actual ERPNext engine."""

from copy import deepcopy
from decimal import Decimal
from unittest import TestCase

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar.access import ScopeChanged, validate_result_visibility
from jarvis.payment_calendar.compatibility import has_native_field_masks
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.projection import project_snapshot
from jarvis.payment_calendar.sources import read_obligations
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarReconciliation(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_reconciliation")
		self.fx = CalendarFixtures()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_reconciliation")

	def read(self, side="Payable"):
		return read_obligations(
			self.fx.company, side, self.fx.today, self.fx.today, add_days(self.fx.today, 40)
		)

	def withholding(self, paid=0, amounts=(90,)):
		invoice = self.fx.invoice(amounts, gross_total=100, paid=paid)
		frappe.db.set_value(
			"Purchase Invoice",
			invoice,
			{"grand_total": 90, "base_grand_total": 90, "apply_tds": 1},
		)
		self.fx.insert(
			"Purchase Taxes and Charges",
			"TDS" + str(self.fx.serial),
			parent=invoice,
			parenttype="Purchase Invoice",
			parentfield="taxes",
			is_tax_withholding_account=1,
			base_tax_amount_after_discount_amount=10,
		)
		# The real PI controller adds an invoice self-reference for the withholding
		# payment, in addition to the gross party entry (no cash moves).
		self.fx.ple(
			"TDS_PLE" + str(self.fx.serial),
			"Purchase Invoice",
			invoice,
			-10,
			voucher_no=invoice,
		)
		return invoice

	def foreign(self, *, paid=1600, advance=20, gross=8000):
		invoice = self.fx.invoice((gross,), side="Receivable", gross_total=gross, advance=advance)
		suffix = str(self.fx.serial)
		frappe.db.set_value("Account", self.fx.receivable, "account_currency", "USD")
		frappe.db.set_value(
			"Sales Invoice",
			invoice,
			{
				"currency": "USD",
				"party_account_currency": "USD",
				"conversion_rate": 80,
				"grand_total": gross / 80,
				"base_grand_total": gross,
			},
		)
		doc = frappe.get_doc("Sales Invoice", invoice)
		doc.payment_schedule[0].invoice_portion = 100
		# Generate the net schedule using the real accounts controller, including
		# its foreign-currency advance conversion, rather than a mocked allocator.
		doc.set_payment_schedule()
		for term in doc.payment_schedule:
			term.db_update()
		frappe.db.set_value(
			"Payment Ledger Entry",
			self.fx.prefix + "InvoicePLE" + suffix,
			{
				"account_currency": "USD",
				"amount_in_account_currency": gross / 80,
			},
		)
		if paid:
			self.fx.ple(
				"FXPayment" + suffix,
				"Payment Entry",
				invoice,
				-paid,
				"Receivable",
				self.fx.customer,
				self.fx.receivable,
			)
			frappe.db.set_value(
				"Payment Ledger Entry",
				self.fx.prefix + "FXPayment" + suffix,
				{
					"account_currency": "USD",
					"amount_in_account_currency": -paid / 80,
				},
			)
		return invoice

	def foreign_rounding(self, side="Payable", *, ledger=5733.78, scheduled=5733.79, paid=0):
		invoice = self.fx.invoice((scheduled,), side=side, gross_total=ledger, paid=paid)
		doctype = "Purchase Invoice" if side == "Payable" else "Sales Invoice"
		account = self.fx.payable if side == "Payable" else self.fx.receivable
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
				"base_grand_total": ledger,
				"base_rounded_total": scheduled,
			},
		)
		doc = frappe.get_doc(doctype, invoice)
		doc.payment_schedule[0].invoice_portion = 100
		doc.set_payment_schedule()
		for term in doc.payment_schedule:
			term.db_update()
		frappe.db.set_value(
			"Payment Ledger Entry",
			self.fx.prefix + "InvoicePLE" + str(self.fx.serial),
			{
				"account_currency": "USD",
				"amount_in_account_currency": 61.72,
			},
		)
		if paid:
			frappe.db.set_value(
				"Payment Ledger Entry",
				self.fx.prefix + "PaymentPLE" + str(self.fx.serial),
				{
					"account_currency": "USD",
					"amount_in_account_currency": -paid / 92.9,
				},
			)
		return invoice

	def test_foreign_rounding_uses_exact_native_ledger_balance_on_both_sides(self):
		for side in ("Payable", "Receivable"):
			with self.subTest(side=side):
				invoice = self.foreign_rounding(side)
				result = self.read(side)
				rows = [r for r in result["rows"] if r["voucher_no"] == invoice]
				self.assertEqual(len(rows), 1)
				self.assertEqual(Decimal(rows[0]["remaining"]), Decimal("5733.78"))
				self.assertEqual(rows[0]["due_date"], add_days(self.fx.today, 10))
				self.assertEqual(rows[0]["allocation_reason"], "foreign_rounding")
				self.assertEqual(rows[0]["allocation_basis"], "invoice")
				self.assertIsNone(rows[0]["paid"])

	def test_foreign_rounding_retains_partial_payment_ledger_residual(self):
		invoice = self.foreign_rounding(paid=929)
		rows = [r for r in self.read()["rows"] if r["voucher_no"] == invoice]
		self.assertEqual(Decimal(rows[0]["remaining"]), Decimal("4804.78"))
		self.assertEqual(rows[0]["settlement"], "Partially settled")

	def test_foreign_rounding_handles_the_opposite_minor_unit_direction(self):
		invoice = self.foreign_rounding(ledger=5733.80)
		rows = [r for r in self.read()["rows"] if r["voucher_no"] == invoice]
		self.assertEqual(Decimal(rows[0]["remaining"]), Decimal("5733.80"))
		self.assertEqual(rows[0]["allocation_reason"], "foreign_rounding")

	def test_foreign_rounding_preserves_a_fully_settled_overlay_without_new_dues(self):
		invoice = self.foreign_rounding(paid=5733.78)
		frappe.db.set_value(
			"Payment Ledger Entry",
			self.fx.prefix + "PaymentPLE" + str(self.fx.serial),
			"amount_in_account_currency",
			-61.72,
		)
		rows = [r for r in self.read()["rows"] if r["voucher_no"] == invoice]
		self.assertTrue(rows)
		self.assertEqual(sum(Decimal(r["remaining"]) for r in rows), 0)

	def test_foreign_rounding_cannot_excuse_an_unexplained_larger_difference(self):
		self.foreign_rounding(ledger=5733.50)
		with self.assertRaises(CalendarSourceUnavailable):
			self.read()

	def test_foreign_rounding_cannot_excuse_a_wrong_schedule(self):
		invoice = self.foreign_rounding()
		frappe.db.set_value("Payment Schedule", {"parent": invoice}, "base_payment_amount", 5733.77)
		with self.assertRaises(CalendarSourceUnavailable):
			self.read()

	def test_foreign_rounding_cannot_excuse_missing_currency_or_multi_term_splits(self):
		invoice = self.foreign_rounding()
		frappe.db.set_value("Purchase Invoice", invoice, "party_account_currency", None)
		with self.assertRaises(CalendarSourceUnavailable):
			self.read()
		frappe.db.set_value("Purchase Invoice", invoice, "party_account_currency", "USD")
		self.fx.insert(
			"Payment Schedule",
			"UnexpectedSplit",
			parent=invoice,
			parenttype="Purchase Invoice",
			parentfield="payment_schedule",
			payment_amount=0,
			base_payment_amount=0,
			due_date=add_days(self.fx.today, 20),
		)
		with self.assertRaises(CalendarSourceUnavailable):
			self.read()

	def test_foreign_rounding_does_not_change_native_reports_or_documents(self):
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		invoice = self.foreign_rounding()

		def ordinary():
			return deepcopy(
				ReceivablePayableReport(
					{
						"company": self.fx.company,
						"report_date": self.fx.today,
						"based_on_payment_terms": 0,
						"range": "30,60,90,120",
					}
				).run({"account_type": "Payable", "naming_by": ["Buying Settings", "supp_master_name"]})
			)

		before = ordinary()
		doc = frappe.get_doc("Purchase Invoice", invoice).as_dict()
		self.read()
		self.assertEqual(ordinary(), before)
		self.assertEqual(frappe.get_doc("Purchase Invoice", invoice).as_dict(), doc)

	def test_withholding_uses_native_invoice_balance_and_preserves_the_only_due_date(self):
		invoice = self.withholding()
		result = self.read()
		rows = [r for r in result["rows"] if r["voucher_no"] == invoice]
		self.assertEqual(len(rows), 1)
		self.assertEqual(Decimal(rows[0]["remaining"]), 90)
		self.assertEqual(rows[0]["due_date"], add_days(self.fx.today, 10))
		self.assertEqual(rows[0]["allocation_basis"], "invoice")
		self.assertIsNone(rows[0]["paid"])
		self.assertEqual(rows[0]["settlement"], "Partially settled")
		self.assertEqual(Decimal(result["canonical_total"]), 90)

	def test_partial_and_fully_settled_withholding_never_create_extra_obligations(self):
		for paid in (40, 90):
			invoice = self.withholding(paid=paid)
			rows = [r for r in self.read()["rows"] if r["voucher_no"] == invoice]
			self.assertEqual(sum(Decimal(r["remaining"]) for r in rows), Decimal(90 - paid))
			self.assertFalse(any(r["settlement"] == "Paid" for r in rows))

	def test_withholding_and_rounding_use_the_native_net_payable(self):
		invoice = self.fx.invoice((106,), gross_total=105.5)
		frappe.db.set_value(
			"Purchase Invoice",
			invoice,
			{
				"grand_total": 100.5,
				"base_grand_total": 100.5,
				"rounded_total": 100,
				"base_rounded_total": 100,
				"base_rounding_adjustment": -0.5,
				"apply_tds": 1,
			},
		)
		self.fx.insert(
			"Purchase Taxes and Charges",
			"RoundTDS",
			parent=invoice,
			parenttype="Purchase Invoice",
			parentfield="taxes",
			is_tax_withholding_account=1,
			base_tax_amount_after_discount_amount=5.5,
		)
		self.fx.ple("RoundTDS_PLE", "Purchase Invoice", invoice, -5.5, voucher_no=invoice)
		rows = [r for r in self.read()["rows"] if r["voucher_no"] == invoice]
		self.assertEqual(len(rows), 1)
		self.assertEqual(Decimal(rows[0]["remaining"]), 100)
		self.assertEqual(rows[0]["allocation_basis"], "invoice")

	def test_unexplained_amount_is_not_excused_by_a_withholding_flag(self):
		invoice = self.withholding()
		frappe.db.set_value("Payment Schedule", {"parent": invoice}, "base_payment_amount", 50)
		with self.assertRaises(CalendarSourceUnavailable):
			self.read()

	def test_unexplained_amount_is_not_excused_by_a_foreign_advance(self):
		invoice = self.foreign()
		frappe.db.set_value("Payment Schedule", {"parent": invoice}, "base_payment_amount", 6300)
		with self.assertRaises(CalendarSourceUnavailable):
			self.read("Receivable")

	def test_restricted_supplier_does_not_leak_fallback_counts_or_totals(self):
		visible, hidden = self.withholding(), self.withholding()
		other = self.fx.insert(
			"Supplier", "HiddenSupplier", supplier_name="Hidden supplier", supplier_type="Company", disabled=0
		)
		frappe.db.set_value("Purchase Invoice", hidden, "supplier", other)
		frappe.db.set_value(
			"Payment Ledger Entry", {"company": self.fx.company, "against_voucher_no": hidden}, "party", other
		)
		user = self.fx.user()
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": "Supplier",
				"for_value": self.fx.supplier,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)
		frappe.set_user(user)
		result = self.read()
		self.assertEqual([r["voucher_no"] for r in result["rows"]], [visible])
		self.assertEqual(result["notes"]["invoice_level"]["count"], 1)
		self.assertEqual(Decimal(result["canonical_total"]), 90)

	def test_restricted_customer_does_not_leak_excluded_exchange_residue(self):
		hidden = self.foreign(paid=8000, advance=0, gross=8000.01)
		frappe.db.set_value(
			"Payment Ledger Entry", self.fx.prefix + "FXPayment1", "amount_in_account_currency", -100.000125
		)
		other = self.fx.insert(
			"Customer", "HiddenCustomer", customer_name="Hidden customer", customer_type="Company", disabled=0
		)
		frappe.db.set_value("Sales Invoice", hidden, "customer", other)
		frappe.db.set_value(
			"Payment Ledger Entry", {"company": self.fx.company, "against_voucher_no": hidden}, "party", other
		)
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
		result = self.read("Receivable")
		self.assertEqual(result["excluded_rows"], [])
		self.assertNotIn("currency_residue_excluded", result["notes"])

	def test_foreign_advance_reuses_company_currency_ledger_residual(self):
		invoice = self.foreign()
		result = self.read("Receivable")
		rows = [r for r in result["rows"] if r["voucher_no"] == invoice]
		self.assertEqual(len(rows), 1)
		self.assertEqual(Decimal(rows[0]["remaining"]), 6400)
		self.assertEqual(rows[0]["allocation_basis"], "invoice")
		self.assertEqual(rows[0]["due_date"], add_days(self.fx.today, 10))
		self.assertEqual(Decimal(result["canonical_total"]), 6400)

	def test_verified_settled_fx_leftover_is_disclosed_without_blocking_forecast_colours(self):
		invoice = self.foreign(paid=8000, advance=0, gross=8000.01)
		# Settlement is complete in the party currency; only a company-currency
		# cent remains, the exact suppression used by the native report.
		frappe.db.set_value(
			"Payment Ledger Entry", self.fx.prefix + "FXPayment1", "amount_in_account_currency", -100.000125
		)
		visible_account = self.fx.account("OtherReceivable", "Receivable", "Asset")
		visible = self.fx.invoice((25,), side="Receivable", account=visible_account)
		result = self.read("Receivable")
		self.assertTrue(any(r["voucher_no"] == visible for r in result["rows"]))
		self.assertFalse(any(r["voucher_no"] == invoice for r in result["rows"]))
		self.assertFalse(result["complete_for_company"])
		self.assertEqual(result["notes"]["currency_residue_excluded"]["count"], 1)
		self.assertEqual(Decimal(result["notes"]["currency_residue_excluded"]["amount"]), Decimal(".01"))
		self.assertEqual(result["excluded_rows"][0]["voucher_no"], invoice)
		empty = {"rows": [], "canonical_total": "0", "complete_for_company": True}
		projection = project_snapshot(
			empty, result, "1000", "100", self.fx.today, self.fx.today, add_days(self.fx.today, 40)
		)
		self.assertTrue(projection["forecast_available"])
		self.assertTrue(all(day["risk"] is not None for day in projection["days"].values()))
		self.assertEqual(projection["source_notes"]["Receivable"], result["notes"])

	def test_unknown_single_term_mismatch_and_multiple_terms_still_refuse(self):
		self.fx.invoice((70,), gross_total=100)
		with self.assertRaises(CalendarSourceUnavailable):
			self.read()

	def test_withholding_does_not_guess_a_mismatched_multi_term_split(self):
		self.withholding(amounts=(40, 50))
		with self.assertRaises(CalendarSourceUnavailable):
			self.read()

	def test_excluded_fx_proofs_are_rechecked_on_delivery(self):
		invoice = self.foreign(paid=8000, advance=0, gross=8000.01)
		frappe.db.set_value(
			"Payment Ledger Entry", self.fx.prefix + "FXPayment1", "amount_in_account_currency", -100.000125
		)
		result = self.read("Receivable")
		self.assertTrue(result["excluded_rows"])
		frappe.db.set_value("Sales Invoice", invoice, "docstatus", 2)
		with self.assertRaises(ScopeChanged):
			validate_result_visibility(
				{"company": self.fx.company, "actor": "Administrator"}, {"sources": {"Receivable": result}}
			)

	def test_new_classification_fields_cannot_bypass_field_masks(self):
		from jarvis.payment_calendar.access import _check_masks

		if not has_native_field_masks():
			self.skipTest("This framework has no native field-mask permissions")
		user = self.fx.user()
		fields = {
			"Purchase Invoice": [
				"rounded_total",
				"base_rounded_total",
				"party_account_currency",
				"write_off_amount",
			],
			"Sales Invoice": [
				"rounded_total",
				"base_rounded_total",
				"party_account_currency",
				"write_off_amount",
			],
			"Payment Schedule": ["due_date", "base_payment_amount"],
			"Purchase Taxes and Charges": [
				"is_tax_withholding_account",
				"base_tax_amount_after_discount_amount",
			],
		}
		for doctype, names in fields.items():
			for field in names:
				with self.subTest(doctype=doctype, field=field):
					frappe.set_user("Administrator")
					prop = frappe.get_doc(
						{
							"doctype": "Property Setter",
							"doctype_or_field": "DocField",
							"doc_type": doctype,
							"field_name": field,
							"property": "mask",
							"property_type": "Check",
							"value": "1",
						}
					).insert(ignore_permissions=True)
					frappe.clear_cache(doctype=doctype)
					frappe.set_user(user)
					with self.assertRaises(CalendarSourceUnavailable):
						_check_masks({"Payable": True, "Receivable": True})
					frappe.set_user("Administrator")
					frappe.delete_doc("Property Setter", prop.name, ignore_permissions=True)
					frappe.clear_cache(doctype=doctype)

	def test_negative_fx_leftover_keeps_its_sign_and_is_not_an_expected_receipt(self):
		invoice = self.foreign(paid=8000.01, advance=0, gross=8000)
		frappe.db.set_value(
			"Payment Ledger Entry", self.fx.prefix + "FXPayment1", "amount_in_account_currency", -100
		)
		result = self.read("Receivable")
		self.assertEqual(result["rows"], [])
		self.assertEqual(result["excluded_rows"][0]["voucher_no"], invoice)
		self.assertEqual(Decimal(result["notes"]["currency_residue_excluded"]["amount"]), Decimal("-.01"))

	def test_missing_party_currency_cannot_be_classified_as_settled_foreign_currency(self):
		invoice = self.foreign(paid=8000, advance=0, gross=8000.01)
		frappe.db.set_value(
			"Payment Ledger Entry", self.fx.prefix + "FXPayment1", "amount_in_account_currency", -100.000125
		)
		frappe.db.set_value("Sales Invoice", invoice, "party_account_currency", None)
		with self.assertRaises(CalendarSourceUnavailable):
			self.read("Receivable")

	def test_foreign_advance_does_not_mutate_either_ordinary_report_mode(self):
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		self.foreign()

		def ordinary(terms):
			return deepcopy(
				ReceivablePayableReport(
					{
						"company": self.fx.company,
						"report_date": self.fx.today,
						"based_on_payment_terms": terms,
						"range": "30,60,90,120",
					}
				).run({"account_type": "Receivable", "naming_by": ["Selling Settings", "cust_master_name"]})
			)

		before = [ordinary(terms) for terms in (0, 1)]
		self.read("Receivable")
		self.assertEqual([ordinary(terms) for terms in (0, 1)], before)

	def test_ordinary_reports_and_financial_documents_are_unchanged(self):
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		invoice = self.withholding()

		def ordinary():
			return deepcopy(
				ReceivablePayableReport(
					{
						"company": self.fx.company,
						"report_date": self.fx.today,
						"based_on_payment_terms": 1,
						"range": "30,60,90,120",
					}
				).run({"account_type": "Payable", "naming_by": ["Buying Settings", "supp_master_name"]})
			)

		before = ordinary()
		doc_before = frappe.get_doc("Purchase Invoice", invoice).as_dict()
		self.read()
		self.assertEqual(ordinary(), before)
		self.assertEqual(frappe.get_doc("Purchase Invoice", invoice).as_dict(), doc_before)
