"""LCV claim ceilings and compatibility do not require India Compliance in CI."""

import builtins
from unittest.mock import patch

import frappe

from jarvis.tests.test_landed_cost_defaults import LandedCostFixtures
from jarvis.tools import _lcv_source_tax as source
from jarvis.tools.create_doc import build_doc

NATIVE = "erpnext.stock.doctype.landed_cost_voucher.landed_cost_voucher"


class TestLCVCompatibility(LandedCostFixtures):
	def test_missing_native_claim_helper_uses_submitted_child_rows(self):
		doc = build_doc("Landed Cost Voucher", self.values(amounts=[80]))
		doc.name = "COMPAT-CURRENT"
		for status, amount, parent in [
			(1, 30, "COMPAT-PRIOR"),
			(0, 900, "COMPAT-DRAFT"),
			(2, 900, "COMPAT-CANCELLED"),
			(1, 900, "COMPAT-CURRENT"),
		]:
			row = frappe.get_doc(
				{
					"doctype": "Landed Cost Vendor Invoice",
					"name": frappe.generate_hash(length=12),
					"parent": parent,
					"parenttype": "Landed Cost Voucher",
					"parentfield": "vendor_invoices",
					"vendor_invoice": self.invoice.name,
					"amount": amount,
					"docstatus": status,
				}
			)
			row.db_insert()
		actual_import = builtins.__import__

		def old_version(name, globals=None, locals=None, fromlist=(), level=0):
			if name == NATIVE and "get_claimed_landed_cost" in fromlist:
				raise ImportError("simulated older ERPNext helper")
			return actual_import(name, globals, locals, fromlist, level)

		with (
			patch("builtins.__import__", side_effect=old_version),
			patch.object(
				source,
				"_classify",
				return_value={
					"purchase_invoice": self.invoice.name,
					"treatment": "excluded_per_recorded_treatment",
				},
			),
		):
			self.assertEqual(source.gate_lcv(doc, lock=True)["status"], "ready")
			doc.vendor_invoices[0].amount = 100
			with self.assertRaises(source.LCVSourceTaxNeedsInput) as caught:
				source.gate_lcv(doc)
			self.assertEqual(caught.exception.assessment["issues"][0]["code"], "source_net_claim_exceeded")

	def test_untaxed_invoice_discount_is_not_capitalized(self):
		frappe.set_user("Administrator")
		invoice = frappe.copy_doc(self.invoice)
		invoice.docstatus = 0
		invoice.bill_no = frappe.generate_hash(length=12)
		invoice.apply_discount_on = "Net Total"
		invoice.additional_discount_percentage = 10
		invoice.insert()
		invoice.submit()
		self.assertLess(invoice.base_net_total, invoice.base_total)
		self.invoice = invoice
		frappe.set_user(self.reader)
		doc = build_doc("Landed Cost Voucher", self.values(amounts=[invoice.base_total]))
		with self.assertRaises(source.LCVSourceTaxNeedsInput) as caught:
			source.gate_lcv(doc)
		self.assertEqual(caught.exception.assessment["issues"][0]["code"], "source_net_claim_exceeded")

	def test_unexpected_classifier_fault_has_a_safe_input_gap(self):
		doc = build_doc("Landed Cost Voucher", self.values())
		with (
			patch.object(source, "_classify", side_effect=RuntimeError("PRIVATE-TAX-VALUE")),
			patch("frappe.logger") as logger,
		):
			with self.assertRaises(source.LCVSourceTaxNeedsInput) as caught:
				source.gate_lcv(doc)
		self.assertEqual(caught.exception.assessment["issues"][0]["code"], "tax_classifier_unavailable")
		self.assertNotIn("PRIVATE-TAX-VALUE", str(caught.exception))
		self.assertNotIn("PRIVATE-TAX-VALUE", str(logger.mock_calls))

	def test_custom_rpc_save_uses_the_native_document_hook_and_scope_resets(self):
		from jarvis.tools.run_method import run_method

		frappe.clear_cache()
		before = frappe.db.count("Landed Cost Voucher")
		values = self.values()

		@frappe.whitelist()
		def custom_save():
			return frappe.get_doc({"doctype": "Landed Cost Voucher", **values}).insert().name

		get_attr = frappe.get_attr
		failure = source.LCVSourceTaxNeedsInput(
			[{"code": "test_source_gap", "message": "Test source needs review."}]
		)
		with (
			patch(
				"frappe.get_attr",
				side_effect=lambda path: custom_save if path == "tests.lcv_custom_save" else get_attr(path),
			),
			patch("jarvis.tools.run_method.get_server_script_map", return_value={}),
			patch.object(source, "gate_lcv", side_effect=failure) as gate,
		):
			with self.assertRaises(source.LCVSourceTaxNeedsInput):
				run_method("tests.lcv_custom_save")
			gate.assert_called()
		self.assertEqual(frappe.db.count("Landed Cost Voucher"), before)
		with patch.object(source, "gate_lcv", side_effect=failure) as gate:
			# The failed RPC must not leave its scope on an ordinary Desk insert.
			frappe.get_doc({"doctype": "Landed Cost Voucher", **values}).insert()
			gate.assert_not_called()

	def test_permission_and_database_retry_errors_are_not_classifier_gaps(self):
		doc = build_doc("Landed Cost Voucher", self.values())
		for error in (source.PermissionDeniedError, frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			with (
				self.subTest(error=error.__name__),
				patch.object(source, "_classify", side_effect=error("Test refusal")),
			):
				with self.assertRaises(error):
					source.gate_lcv(doc)

	def test_raw_controller_read_helpers_are_refused(self):
		from jarvis.tools.run_method import run_method

		frappe.set_user("Administrator")
		doc = frappe.get_doc({"doctype": "Landed Cost Voucher", **self.values()}).insert()
		frappe.set_user(self.reader)
		for method, args in [
			("get_items_from_purchase_receipts", {}),
			("get_vendor_invoice_amount", {"vendor_invoice": self.invoice.name}),
		]:
			with self.subTest(method=method), self.assertRaises(source.PermissionDeniedError):
				run_method(method, args, doctype="Landed Cost Voucher", name=doc.name)

	def test_linked_ledger_adjustment_invalidates_original_posting_proof(self):
		expected = {self.expense: self.invoice.base_net_total}
		self.assertTrue(source._posted_input_matches(self.invoice, expected, 2))
		name = frappe.db.get_value(
			"GL Entry",
			{"voucher_type": "Purchase Invoice", "voucher_no": self.invoice.name, "account": self.expense},
		)
		row = frappe.copy_doc(frappe.get_doc("GL Entry", name))
		row.name = frappe.generate_hash(length=12)
		row.voucher_type = "Journal Entry"
		row.voucher_no = "SYNTHETIC-LCV-ADJUSTMENT"
		row.against_voucher_type = "Purchase Invoice"
		row.against_voucher = self.invoice.name
		# A native row fixture checks the proof query. It does not submit a journal.
		row.db_insert()
		self.assertFalse(source._posted_input_matches(self.invoice, expected, 2))
