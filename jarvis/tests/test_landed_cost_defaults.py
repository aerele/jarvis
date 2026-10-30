"""Native v15 receipt and service-invoice fixtures for landed-cost tools."""

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import nowdate

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.lcv import SOURCE_INVOICE
from jarvis.tests import _erpnext_masters as masters
from jarvis.tools.create_doc import build_doc, create_doc
from jarvis.tools.preview_doc import preview_doc


class LandedCostFixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("DocType", "Landed Cost Voucher"):
			raise unittest.SkipTest("ERPNext is not installed")
		frappe.set_user("Administrator")
		cls.company = masters.ensure_ledger_company()
		masters.ensure_fiscal_years(nowdate())
		masters.ensure_item_group_and_uom()
		# Real multi-connection probes commit their own fixtures. Give each test
		# class separate stock so those earlier ledgers cannot affect this suite.
		suffix = frappe.generate_hash(length=7)
		cls.stock = cls.make_item("_JLCV Stock " + suffix, 1, "84821020")
		cls.service = cls.make_item("_JLCV Service " + suffix, 0, "996799")
		group = "_JLCV Suppliers"
		if not frappe.db.exists("Supplier Group", group):
			frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": group}).insert()
		cls.supplier = (
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "_JLCV Vendor " + suffix,
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
					"description": "Freight",
					"amount": charge
					if charge is not None and len(invoices) == 1
					else (amounts[idx] if amounts is not None else invoice.base_net_total),
					SOURCE_INVOICE: invoice.name,
				}
				for idx, invoice in enumerate(invoices)
			],
		}

	def claimed_cost(self):
		from jarvis.tools._lcv_source_tax import _prior_claims

		return sum((_prior_claims(self.invoice.name, None, False) or {}).values())
