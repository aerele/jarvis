"""Source-backed landed-cost preparation against real ERP documents."""

from unittest.mock import patch

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tests.test_landed_cost_defaults import LandedCostFixtures
from jarvis.tools.create_doc import create_doc
from jarvis.tools.preview_doc import preview_doc
from jarvis.tools.preview_landed_cost_voucher import preview_landed_cost_voucher


class TestLandedCostPreview(LandedCostFixtures):
	def prepare(self, **overrides):
		args = {
			"company": self.company,
			"receipts": [{"doctype": "Purchase Receipt", "name": self.receipt.name}],
			"charges": [{"purchase_invoice": self.invoice.name, "basis": "Amount"}],
		}
		args.update(overrides)
		return preview_landed_cost_voucher(**args)

	def snapshot(self):
		return {
			"counts": {
				dt: frappe.db.count(dt)
				for dt in (
					"Landed Cost Voucher",
					"GL Entry",
					"Stock Ledger Entry",
					"Repost Item Valuation",
					"Version",
				)
			},
			"receipt": frappe.get_doc("Purchase Receipt", self.receipt.name).as_dict(),
			"bins": frappe.get_all("Bin", filters={"item_code": self.stock}, fields=["*"]),
			"claims": frappe.db.get_value(
				"Purchase Invoice", self.invoice.name, "claimed_landed_cost_amount"
			),
			"series": frappe.db.sql("select name, current from tabSeries order by name"),
		}

	def test_native_allocation_and_create_payload_without_preview_side_effects(self):
		before = self.snapshot()
		result = self.prepare()
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual(before, self.snapshot())
		self.assertEqual(result["totals"]["charges"], 120)
		self.assertEqual(result["totals"]["allocated"], 120)
		self.assertEqual(
			result["draft_values"]["items"][0]["purchase_receipt_item"], self.receipt.items[0].name
		)
		self.assertFalse(self.invoice.reload().claimed_landed_cost_amount)
		saved = create_doc("Landed Cost Voucher", result["draft_values"])
		self.assertEqual(saved["docstatus"], 0)
		self.assertEqual(saved["total_vendor_invoices_cost"], 120)
		self.assertEqual(saved["items"][0]["applicable_charges"], 120)

	def test_duplicate_invoice_is_not_allocated_twice(self):
		result = self.prepare(charges=[{"purchase_invoice": self.invoice.name}] * 2)
		self.assertEqual(result["status"], "needs_input")
		self.assertNotIn("draft_values", result)

	def test_missing_bill_and_unsupported_weight_are_explicit_gaps(self):
		for charges in (
			[{"amount": 120, "description": "Unbooked freight"}],
			[{"purchase_invoice": self.invoice.name, "basis": "Weight"}],
		):
			with self.subTest(charges=charges):
				result = self.prepare(charges=charges)
				self.assertEqual(result["status"], "needs_input")
				self.assertNotIn("draft_values", result)

	def test_filtered_link_value_cannot_become_a_database_query(self):
		with self.assertRaises(InvalidArgumentError):
			self.prepare(receipts=[{"doctype": "Purchase Receipt", "name": ["like", "%"]}])

	def test_permission_denial_before_reading_source_money(self):
		original = frappe.has_permission

		def allowed(doctype, *args, **kwargs):
			return False if doctype == "Purchase Invoice" else original(doctype, *args, **kwargs)

		with patch.object(frappe, "has_permission", side_effect=allowed):
			with self.assertRaises(PermissionDeniedError):
				self.prepare()

	def test_saved_draft_is_reconciled_after_an_edit(self):
		prepared = self.prepare()
		saved = create_doc("Landed Cost Voucher", prepared["draft_values"])
		frappe.db.set_value("Landed Cost Item", saved["items"][0]["name"], "applicable_charges", 121)
		before = self.snapshot()
		result = self.prepare(existing_draft=saved["name"], charges=[])
		self.assertEqual(result["status"], "needs_input")
		self.assertEqual(before, self.snapshot())
		self.assertEqual(
			frappe.get_doc("Landed Cost Voucher", saved["name"]).items[0].applicable_charges, 121
		)

	def test_submitted_receipt_is_required(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("Purchase Receipt", self.receipt.name, "docstatus", 0)
		frappe.set_user(self.reader)
		result = self.prepare()
		self.assertEqual(result["status"], "needs_input")
		self.assertNotIn("draft_values", result)

	def test_partial_cost_stays_explicit_when_draft_is_saved(self):
		result = self.prepare(
			charges=[
				{
					"purchase_invoice": self.invoice.name,
					"basis": "Amount",
					"items": [{"name": self.invoice.items[0].name, "amount": 30}],
				}
			]
		)
		self.assertEqual(result["status"], "ready", result)
		saved = create_doc("Landed Cost Voucher", result["draft_values"])
		self.assertEqual(saved["total_taxes_and_charges"], 30)
		self.assertEqual(saved["vendor_invoices"][0]["amount"], 30)

	def test_recorded_foreign_currency_cost_is_not_converted_twice(self):
		frappe.set_user("Administrator")
		invoice = self.make_invoice(2, "USD", 83.4)
		frappe.set_user(self.reader)
		result = self.prepare(charges=[{"purchase_invoice": invoice.name, "basis": "Amount"}])
		self.assertEqual(result["status"], "ready", result)
		self.assertAlmostEqual(result["totals"]["charges"], 166.8)
		self.assertEqual(result["draft_values"]["taxes"][0]["exchange_rate"], 1)
		self.assertAlmostEqual(result["draft_values"]["taxes"][0]["amount"], 166.8)

	def test_wrong_manual_sum_is_an_input_gap_without_a_saved_voucher(self):
		before = self.snapshot()
		result = self.prepare(
			charges=[
				{
					"purchase_invoice": self.invoice.name,
					"basis": "Manual",
					"allocations": [{"receipt_item": self.receipt.items[0].name, "amount": 121}],
				}
			]
		)
		self.assertEqual(result["status"], "needs_input")
		self.assertNotIn("draft_values", result)
		self.assertEqual(before, self.snapshot())

	def test_masked_receipt_amounts_cannot_leak_through_allocation_ratios(self):
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		frappe.set_user("Administrator")
		make_property_setter("Purchase Receipt Item", "base_net_amount", "mask", 1, "Check")
		frappe.clear_cache(doctype="Purchase Receipt Item")
		frappe.set_user(self.reader)
		with self.assertRaises(PermissionDeniedError):
			self.prepare()

	def test_unknown_financial_options_are_not_silently_ignored(self):
		with self.assertRaises(InvalidArgumentError):
			self.prepare(
				charges=[{"purchase_invoice": self.invoice.name, "basis": "Amount", "capitalize": False}]
			)

	def test_explicit_manual_allocation_reconciles(self):
		result = self.prepare(
			charges=[
				{
					"purchase_invoice": self.invoice.name,
					"basis": "Manual",
					"allocations": [{"receipt_item": self.receipt.items[0].name, "amount": 120}],
				}
			]
		)
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual(result["items"][0]["applicable_charges"], 120)

	def test_saved_draft_is_reused_with_charge_reconciliation(self):
		result = self.prepare()
		saved = create_doc("Landed Cost Voucher", result["draft_values"])
		before = self.snapshot()
		reconciled = self.prepare(existing_draft=saved["name"], charges=[])
		self.assertEqual(reconciled["status"], "ready", reconciled)
		self.assertEqual(reconciled["suggested_action"], "reuse")
		self.assertEqual(reconciled["charges"][0]["amount"], 120)
		self.assertNotIn("draft_values", reconciled)
		self.assertEqual(before, self.snapshot())
		with self.assertRaises(InvalidArgumentError):
			self.prepare(existing_draft=saved["name"])

	def test_masked_text_and_dimensions_cannot_reach_the_draft(self):
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		for doctype, field in (
			("Purchase Invoice Item", "description"),
			("Purchase Receipt Item", "project"),
		):
			with self.subTest(field=field):
				frappe.set_user("Administrator")
				make_property_setter(doctype, field, "mask", 1, "Check")
				frappe.clear_cache(doctype=doctype)
				frappe.set_user(self.reader)
				with self.assertRaises(PermissionDeniedError):
					self.prepare()
				frappe.set_user("Administrator")
				make_property_setter(doctype, field, "mask", 0, "Check")
				frappe.clear_cache(doctype=doctype)
				frappe.set_user(self.reader)

	def test_restricted_source_description_is_not_emitted(self):
		field = frappe.get_meta("Purchase Invoice Item").get_field("description")
		with patch.object(field, "permlevel", 99), self.assertRaises(PermissionDeniedError):
			self.prepare()

	def test_cost_precision_and_date_are_canonical(self):
		result = self.prepare(
			posting_date="27-10-2026",
			charges=[
				{
					"purchase_invoice": self.invoice.name,
					"basis": "Amount",
					"items": [{"name": self.invoice.items[0].name, "amount": 30.001}],
				}
			],
		)
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual(result["draft_values"]["posting_date"], "2026-10-27")
		self.assertEqual(result["totals"]["charges"], 30)
		self.assertTrue(any("rounded" in message for message in result["warnings"]))
		result = self.prepare(
			charges=[
				{
					"purchase_invoice": self.invoice.name,
					"basis": "Amount",
					"items": [{"name": self.invoice.items[0].name, "amount": 0.00001}],
				}
			]
		)
		self.assertEqual(result["status"], "needs_input")
		self.assertNotIn("draft_values", result)

	def test_missing_recorded_rate_is_an_explicit_gap(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("Purchase Invoice", self.invoice.name, "conversion_rate", 0)
		frappe.set_user(self.reader)
		result = self.prepare()
		self.assertEqual(result["status"], "needs_input")
		self.assertEqual(result["issues"][0]["code"], "recorded_fx")

	def test_native_manual_submit_check_is_stricter_than_generic_draft_preview(self):
		values = self.prepare()["draft_values"]
		values["items"][0]["applicable_charges"] += 1
		before = self.snapshot()
		self.assertTrue(preview_doc("Landed Cost Voucher", values)["valid"])
		voucher = frappe.get_doc({"doctype": "Landed Cost Voucher", **values})
		voucher.run_method("validate")
		with self.assertRaises(frappe.ValidationError):
			voucher.validate_applicable_charges_for_item()
		self.assertEqual(before, self.snapshot())

	def test_native_submission_matches_estimate_and_cumulative_claims_survive_cancel(self):
		first = self.prepare(
			charges=[
				{
					"purchase_invoice": self.invoice.name,
					"basis": "Amount",
					"items": [{"name": self.invoice.items[0].name, "amount": 30}],
				}
			]
		)
		voucher = frappe.get_doc(
			"Landed Cost Voucher", create_doc("Landed Cost Voucher", first["draft_values"])["name"]
		)
		voucher.submit()
		self.assertAlmostEqual(
			self.receipt.reload().items[0].valuation_rate,
			first["items"][0]["estimated_receipt_valuation_rate"],
		)
		self.assertEqual(self.invoice.reload().claimed_landed_cost_amount, 30)
		second = self.prepare(
			charges=[
				{
					"purchase_invoice": self.invoice.name,
					"basis": "Amount",
					"items": [{"name": self.invoice.items[0].name, "amount": 90}],
				}
			]
		)
		self.assertEqual(second["status"], "ready", second)
		other = frappe.get_doc(
			"Landed Cost Voucher", create_doc("Landed Cost Voucher", second["draft_values"])["name"]
		)
		other.submit()
		self.assertEqual(self.invoice.reload().claimed_landed_cost_amount, 120)
		blocked = self.prepare()
		self.assertEqual(blocked["status"], "needs_input")
		other.cancel()
		self.assertEqual(self.invoice.reload().claimed_landed_cost_amount, 30)
		self.assertEqual(
			self.prepare(
				charges=[
					{
						"purchase_invoice": self.invoice.name,
						"basis": "Amount",
						"items": [{"name": self.invoice.items[0].name, "amount": 90}],
					}
				]
			)["status"],
			"ready",
		)

	def test_complete_payload_is_never_truncated_into_an_executable_draft(self):
		with patch("jarvis.tools.preview_landed_cost_voucher.MAX_RESULT_CHARS", 1000):
			result = self.prepare()
		self.assertEqual(result["status"], "needs_input")
		self.assertEqual(result["issues"][0]["code"], "result_too_large")
		self.assertNotIn("draft_values", result)

	def test_existing_receipt_dimension_is_preserved(self):
		frappe.set_user("Administrator")
		parent = frappe.db.get_value("Cost Center", {"company": self.company, "is_group": 1}, "name")
		center = frappe.get_doc(
			{
				"doctype": "Cost Center",
				"cost_center_name": "_JLCV Receiving",
				"company": self.company,
				"parent_cost_center": parent,
			}
		).insert()
		frappe.db.set_value("Purchase Receipt Item", self.receipt.items[0].name, "cost_center", center.name)
		frappe.set_user(self.reader)
		result = self.prepare()
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual(result["draft_values"]["items"][0]["cost_center"], center.name)
		self.assertEqual(
			result["items"][0]["cost_components"],
			[{"source": self.invoice.name, "kind": "service", "basis": "Amount", "amount": 120}],
		)

	def test_foreign_expense_account_uses_the_booked_rate(self):
		frappe.set_user("Administrator")
		account = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "_JLCV USD Freight",
				"company": self.company,
				"parent_account": frappe.db.get_value("Account", self.expense, "parent_account"),
				"account_currency": "USD",
				"is_group": 0,
			}
		).insert()
		original = self.expense
		try:
			self.expense = account.name
			invoice = self.make_invoice(2, "USD", 83.4)
		finally:
			self.expense = original
		frappe.set_user(self.reader)
		with patch("erpnext.controllers.taxes_and_totals.get_exchange_rate", return_value=99) as current_rate:
			result = self.prepare(charges=[{"purchase_invoice": invoice.name, "basis": "Amount"}])
			current_rate.assert_not_called()
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual(result["draft_values"]["taxes"][0]["amount"], 2)
		self.assertEqual(result["draft_values"]["taxes"][0]["exchange_rate"], 83.4)
		self.assertAlmostEqual(result["totals"]["charges"], 166.8)
