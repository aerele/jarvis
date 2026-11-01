"""LCV creation fetches the same omitted vendor amounts as ERPNext's form."""

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import nowdate

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tests import _erpnext_masters as masters
from jarvis.tools.create_doc import build_doc, create_doc
from jarvis.tools.preview_doc import preview_doc


class LandedCostFixtures(FrappeTestCase):
	requires_vendor_amounts = True

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("DocType", "Landed Cost Voucher"):
			raise unittest.SkipTest("ERPNext is not installed")
		if cls.requires_vendor_amounts and not hasattr(
			type(frappe.new_doc("Landed Cost Voucher")), "get_vendor_invoice_amount"
		):
			raise unittest.SkipTest("ERPNext does not have vendor-invoice cost fetching")
		frappe.set_user("Administrator")
		cls.company = masters.ensure_ledger_company()
		masters.ensure_fiscal_years(nowdate())
		masters.ensure_item_group_and_uom()
		cls.stock = cls.make_item("_JLCV Stock", 1, "84821020")
		cls.service = cls.make_item("_JLCV Service", 0, "996799")
		group = "_JLCV Suppliers"
		if not frappe.db.exists("Supplier Group", group):
			frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": group}).insert()
		cls.supplier = (
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "_JLCV Vendor",
					"supplier_group": group,
					"supplier_type": "Company",
					"country": "Maldives",
					"gst_category": "Overseas",
				}
			)
			.insert()
			.name
		)
		cls.reader = "lcv.defaults.tests@example.com"
		if not frappe.db.exists("User", cls.reader):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": cls.reader,
					"first_name": "LCV Defaults",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			).insert()
		frappe.get_doc("User", cls.reader).add_roles("Stock Manager", "Accounts User")
		masters.clear_cache_after_rollback(cls)

	@staticmethod
	def make_item(code, stock, hsn):
		values = {
			"doctype": "Item",
			"item_code": code,
			"item_group": masters.ITEM_GROUP,
			"stock_uom": masters.UOM,
			"is_stock_item": stock,
		}
		if frappe.get_meta("Item").has_field("gst_hsn_code"):
			values["gst_hsn_code"] = hsn
		return frappe.get_doc(values).insert().name

	@patch.object(frappe, "in_test", False)
	def setUp(self):
		frappe.set_user("Administrator")
		# Fixture PR/PI payment-term setup passes native date objects to an RPC
		# annotated for strings. Keep that workaround outside the tested tool calls.
		frappe.db.savepoint("lcv_defaults_test")
		company = frappe.get_doc("Company", self.company)
		self.expense = company.default_expense_account
		self.cost_center = company.cost_center
		warehouse = frappe.db.get_value("Warehouse", {"company": self.company, "warehouse_name": "Stores"})
		self.receipt = frappe.get_doc(
			{
				"doctype": "Purchase Receipt",
				"company": self.company,
				"supplier": self.supplier,
				"posting_date": nowdate(),
				"set_posting_time": 1,
				"items": [{"item_code": self.stock, "qty": 2, "rate": 50, "warehouse": warehouse}],
			}
		).insert()
		self.receipt.submit()
		self.invoice = self.make_invoice(120)
		frappe.set_user(self.reader)
		self.assertNotIn("System Manager", frappe.get_roles())

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="lcv_defaults_test")
		frappe.clear_cache()

	@patch.object(frappe, "in_test", False)
	def make_invoice(self, rate, currency="INR", conversion_rate=1):
		invoice = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": self.company,
				"supplier": self.supplier,
				"posting_date": nowdate(),
				"due_date": nowdate(),
				"currency": currency,
				"conversion_rate": conversion_rate,
				"bill_no": frappe.generate_hash(length=10),
				"items": [
					{
						"item_code": self.service,
						"qty": 1,
						"rate": rate,
						"expense_account": self.expense,
						"cost_center": self.cost_center,
					}
				],
			}
		).insert()
		invoice.submit()
		return invoice

	def values(self, invoices=None, amounts=None, charge=None):
		invoices = invoices or [self.invoice]
		voucher = frappe.new_doc("Landed Cost Voucher")
		voucher.company = self.company
		voucher.append(
			"purchase_receipts",
			{
				"receipt_document_type": "Purchase Receipt",
				"receipt_document": self.receipt.name,
			},
		)
		voucher.get_items_from_purchase_receipts()
		rows = [{"vendor_invoice": invoice.name} for invoice in invoices]
		if amounts is not None:
			for row, amount in zip(rows, amounts, strict=True):
				row["amount"] = amount
		return {
			"company": self.company,
			"posting_date": nowdate(),
			"distribute_charges_based_on": "Amount",
			"purchase_receipts": [
				{"receipt_document_type": "Purchase Receipt", "receipt_document": self.receipt.name}
			],
			"items": [
				{
					key: row.get(key)
					for key in (
						"item_code",
						"description",
						"qty",
						"rate",
						"amount",
						"receipt_document_type",
						"receipt_document",
						"purchase_receipt_item",
						"cost_center",
					)
				}
				for row in voucher.items
			],
			"taxes": [
				{
					"expense_account": self.expense,
					"cost_center": self.cost_center,
					"description": "Freight",
					"amount": charge if charge is not None else sum(i.base_total for i in invoices),
				}
			],
			"vendor_invoices": rows,
		}


class TestLandedCostDefaults(LandedCostFixtures):
	def test_preview_and_save_fetch_company_currency_costs(self):
		frappe.set_user("Administrator")
		invoices = [self.invoice, self.make_invoice(80), self.make_invoice(2, "USD", 83.4)]
		frappe.set_user(self.reader)
		values = self.values(invoices)
		expected = sum(invoice.base_total for invoice in invoices)
		before = frappe.db.count("Landed Cost Voucher")
		preview = preview_doc("Landed Cost Voucher", values)
		self.assertTrue(preview["valid"], preview)
		self.assertAlmostEqual(preview["resolved"]["total_vendor_invoices_cost"], expected)
		self.assertEqual(frappe.db.count("Landed Cost Voucher"), before)
		self.assertFalse(self.invoice.reload().claimed_landed_cost_amount)
		saved = create_doc("Landed Cost Voucher", values)
		self.assertEqual(saved["docstatus"], 0)
		self.assertAlmostEqual(saved["total_vendor_invoices_cost"], expected)
		self.assertEqual(
			[row["amount"] for row in saved["vendor_invoices"]], [i.base_total for i in invoices]
		)

	def test_partial_native_claim_is_subtracted_on_the_next_create(self):
		first = create_doc("Landed Cost Voucher", self.values(amounts=[30], charge=30))
		frappe.get_doc("Landed Cost Voucher", first["name"]).submit()
		self.assertEqual(self.invoice.reload().claimed_landed_cost_amount, 30)
		saved = create_doc("Landed Cost Voucher", self.values(charge=90))
		self.assertEqual(saved["total_vendor_invoices_cost"], 90)

	def test_explicit_zero_null_blank_and_partial_are_not_fetched(self):
		controller = type(frappe.new_doc("Landed Cost Voucher"))
		for amount in (0, None, "", 25):
			with self.subTest(amount=amount), patch.object(controller, "get_vendor_invoice_amount") as fetch:
				saved = create_doc("Landed Cost Voucher", self.values(amounts=[amount]))
				fetch.assert_not_called()
				self.assertEqual(saved["total_vendor_invoices_cost"], amount or 0)

	def test_ineligible_or_missing_invoice_is_not_silently_zero(self):
		values = self.values()
		frappe.db.set_value(
			"Purchase Invoice", self.invoice.name, "claimed_landed_cost_amount", self.invoice.base_total
		)
		with self.assertRaises(InvalidArgumentError):
			create_doc("Landed Cost Voucher", values)
		values["vendor_invoices"][0]["vendor_invoice"] = "_JLCV Missing"
		with self.assertRaises(PermissionDeniedError):
			preview_doc("Landed Cost Voucher", values)

	def test_record_and_field_permission_failures_precede_native_fetch(self):
		controller = type(frappe.new_doc("Landed Cost Voucher"))
		values = self.values()
		with patch.object(controller, "get_vendor_invoice_amount") as fetch:
			with patch("jarvis.tools._client_defaults.frappe.has_permission", return_value=False):
				with self.assertRaises(PermissionDeniedError):
					build_doc("Landed Cost Voucher", values)
			with patch(
				"jarvis.tools._client_defaults._refuse_unreadable",
				side_effect=PermissionDeniedError("hidden"),
			):
				with self.assertRaises(PermissionDeniedError):
					build_doc("Landed Cost Voucher", values)
			fetch.assert_not_called()

	def test_real_field_permission_blocks_derived_money(self):
		values = self.values()
		field = frappe.get_meta("Purchase Invoice").get_field("base_total")
		with patch.object(field, "permlevel", 99), self.assertRaises(PermissionDeniedError):
			build_doc("Landed Cost Voucher", values)

	def test_real_supplier_user_permission_blocks_derived_money(self):
		values = self.values()
		frappe.set_user("Administrator")
		other = frappe.copy_doc(frappe.get_doc("Supplier", self.supplier))
		other.supplier_name = "_JLCV Allowed Supplier"
		other.insert()
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": self.reader,
				"allow": "Supplier",
				"for_value": other.name,
				"apply_to_all_doctypes": 0,
				"applicable_for": "Purchase Invoice",
			}
		).insert()
		frappe.clear_cache(user=self.reader)
		frappe.set_user(self.reader)
		self.assertFalse(frappe.has_permission("Purchase Invoice", "read", doc=self.invoice.name))
		with self.assertRaises(PermissionDeniedError):
			preview_doc("Landed Cost Voucher", values)

	def test_real_source_field_mask_blocks_derived_money(self):
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		meta = frappe.get_meta("Purchase Invoice")
		if not hasattr(meta, "get_masked_fields"):
			self.skipTest("Frappe does not support field masking")
		values = self.values()
		frappe.set_user("Administrator")
		make_property_setter("Purchase Invoice", "base_total", "mask", 1, "Check")
		frappe.clear_cache(doctype="Purchase Invoice")
		frappe.set_user(self.reader)
		self.assertIn(
			"base_total",
			[field.fieldname for field in frappe.get_meta("Purchase Invoice").get_masked_fields()],
		)
		controller = type(frappe.new_doc("Landed Cost Voucher"))
		with patch.object(controller, "get_vendor_invoice_amount") as fetch:
			with self.assertRaises(PermissionDeniedError):
				build_doc("Landed Cost Voucher", values)
			fetch.assert_not_called()

	def test_request_context_preview_and_create(self):
		from werkzeug.wrappers import Request

		request = getattr(frappe.local, "request", None)
		frappe.local.request = Request.from_values(method="POST")
		try:
			values = self.values()
			preview = preview_doc("Landed Cost Voucher", values)
			self.assertTrue(preview["valid"], preview)
			self.assertEqual(preview["resolved"]["total_vendor_invoices_cost"], 120)
			saved = create_doc("Landed Cost Voucher", values)
			self.assertEqual(saved["docstatus"], 0)
			self.assertEqual(saved["total_vendor_invoices_cost"], 120)
		finally:
			frappe.local.request = request

	def test_filter_shaped_links_are_refused_without_a_lookup(self):
		for name in ({"base_total": [">", 5000]}, [["base_total", ">", 5000]], {}, [], 5, True):
			values = self.values()
			values["vendor_invoices"][0]["vendor_invoice"] = name
			with self.subTest(name=name), patch("jarvis.tools._client_defaults.frappe.db.exists") as exists:
				with self.assertRaises(PermissionDeniedError):
					build_doc("Landed Cost Voucher", values)
				exists.assert_not_called()

	def test_explicit_amount_does_not_require_a_new_source_read(self):
		values = self.values(amounts=[25])
		original = frappe.has_permission

		def permission(doctype, *args, **kwargs):
			return False if doctype == "Purchase Invoice" else original(doctype, *args, **kwargs)

		with patch("jarvis.tools._client_defaults.frappe.has_permission", side_effect=permission):
			saved = create_doc("Landed Cost Voucher", values)
		self.assertEqual(saved["total_vendor_invoices_cost"], 25)

	def test_save_refreshes_an_omitted_default_after_preview(self):
		values = self.values(charge=90)
		preview = preview_doc("Landed Cost Voucher", values)
		self.assertEqual(preview["resolved"]["total_vendor_invoices_cost"], 120)
		self.assertNotIn("amount", values["vendor_invoices"][0])
		first = create_doc("Landed Cost Voucher", self.values(amounts=[30], charge=30))
		frappe.get_doc("Landed Cost Voucher", first["name"]).submit()
		saved = create_doc("Landed Cost Voucher", values)
		self.assertEqual(saved["total_vendor_invoices_cost"], 90)

	def test_a_draft_invoice_is_not_a_derivable_cost(self):
		frappe.set_user("Administrator")
		self.invoice.cancel()
		with patch.object(frappe, "in_test", False):
			amendment = frappe.copy_doc(self.invoice, ignore_no_copy=False)
			amendment.amended_from = self.invoice.name
			amendment.insert()
		frappe.set_user(self.reader)
		with self.assertRaises(InvalidArgumentError):
			create_doc("Landed Cost Voucher", self.values([amendment]))

	def test_native_fetch_failure_is_not_an_optional_version_fallback(self):
		values = self.values()
		controller = type(frappe.new_doc("Landed Cost Voucher"))
		with patch.object(controller, "get_vendor_invoice_amount", side_effect=frappe.QueryTimeoutError):
			with self.assertRaises(frappe.QueryTimeoutError):
				build_doc("Landed Cost Voucher", values)

	def test_batch_refusal_rolls_back_earlier_rows(self):
		before = frappe.db.count("Landed Cost Voucher")
		values = self.values()
		bad = self.values()
		bad["vendor_invoices"][0]["vendor_invoice"] = "_JLCV Missing"
		with self.assertRaises(PermissionDeniedError):
			create_doc(
				docs=[
					{"doctype": "Landed Cost Voucher", "values": values},
					{"doctype": "Landed Cost Voucher", "values": bad},
				]
			)
		self.assertEqual(frappe.db.count("Landed Cost Voucher"), before)

	def test_old_controller_without_fetch_keeps_its_existing_behavior(self):
		controller = type(frappe.new_doc("Landed Cost Voucher"))
		with patch.object(controller, "get_vendor_invoice_amount", None):
			self.assertFalse(build_doc("Landed Cost Voucher", self.values()).vendor_invoices[0].amount)
