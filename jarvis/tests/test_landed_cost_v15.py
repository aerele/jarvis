"""v15 native posting limits, persisted invoice claims and migration lifecycle."""

from unittest.mock import patch

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.lcv import SOURCE_INVOICE, TAX_DOCTYPE, setup_source_link, source_link_available
from jarvis.tests import test_landed_cost_preview as preview_tests
from jarvis.tests import test_landed_cost_weight_preview as weight_tests
from jarvis.tests.test_landed_cost_defaults import LandedCostFixtures
from jarvis.tools._lcv_source_tax import LCVSourceTaxNeedsInput, gate_lcv
from jarvis.tools.amend_doc import amend_doc
from jarvis.tools.cancel_doc import cancel_doc
from jarvis.tools.create_doc import build_doc, create_doc
from jarvis.tools.preview_landed_cost_voucher import preview_landed_cost_voucher
from jarvis.tools.submit_doc import submit_doc
from jarvis.tools.update_doc import update_doc

LCV = "Landed Cost Voucher"


class TestLandedCostV15(LandedCostFixtures):
	prepare = preview_tests.TestLandedCostPreview.prepare
	snapshot = preview_tests.TestLandedCostPreview.snapshot
	weighted_receipt = weight_tests.TestLandedCostWeightPreview.weighted_receipt

	def make_other_invoice(self, amount=80):
		frappe.set_user("Administrator")
		invoice = self.make_invoice(amount)
		frappe.set_user(self.reader)
		return invoice

	def test_preview_create_review_submit_keeps_link_and_posts_once(self):
		result = self.prepare()
		self.assertEqual(result["status"], "ready", result)
		self.assertNotIn("vendor_invoices", result["draft_values"])
		saved = create_doc(LCV, result["draft_values"])
		self.assertEqual(saved["taxes"][0][SOURCE_INVOICE], self.invoice.name)
		review = self.prepare(existing_draft=saved["name"], charges=[])
		self.assertEqual(review["status"], "ready", review)
		self.assertEqual(review["suggested_action"], "reuse")
		submit_doc(LCV, saved["name"])
		self.assertEqual(self.claimed_cost(), 120)
		credits = frappe.db.sql(
			"select sum(credit-debit) from `tabGL Entry` where voucher_type='Purchase Receipt' and voucher_no=%s and account=%s and is_cancelled=0",
			(self.receipt.name, self.expense),
		)[0][0]
		self.assertAlmostEqual(float(credits), 120, places=2)

	def test_native_multi_invoice_amount_posts_balanced_costs(self):
		other = self.make_other_invoice(80.02)
		self.invoice = self.make_other_invoice(100.01)
		receipt = self.weighted_receipt(weights=(1, 2), quantities=(3, 7), rates=(13, 17))
		before_stock = frappe.db.sql(
			"select sum(stock_value_difference) from `tabStock Ledger Entry` where voucher_no=%s and is_cancelled=0",
			receipt.name,
		)[0][0]
		result = self.prepare(
			receipts=[{"doctype": "Purchase Receipt", "name": receipt.name}],
			charges=[
				{"purchase_invoice": invoice.name, "basis": "Amount"} for invoice in (self.invoice, other)
			],
		)
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual(result["draft_values"]["distribute_charges_based_on"], "Amount")
		self.assertEqual(len(result["draft_values"]["taxes"]), 2)
		saved = create_doc(LCV, result["draft_values"])
		submit_doc(LCV, saved["name"])
		entries = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Purchase Receipt", "voucher_no": receipt.name, "is_cancelled": 0},
			fields=["debit", "credit", "account"],
		)
		self.assertAlmostEqual(sum(row.debit - row.credit for row in entries), 0, places=2)
		self.assertAlmostEqual(
			sum(row.credit - row.debit for row in entries if row.account == self.expense), 180.03, places=2
		)
		after_stock = frappe.db.sql(
			"select sum(stock_value_difference) from `tabStock Ledger Entry` where voucher_no=%s and is_cancelled=0",
			receipt.name,
		)[0][0]
		self.assertAlmostEqual(float(after_stock - before_stock), 180.03, places=2)

	def test_multi_account_qty_credits_each_source_account_once(self):
		frappe.set_user("Administrator")
		account = frappe.get_doc(
			{
				"doctype": "Account",
				"company": self.company,
				"account_name": "_JLCV Forwarding " + frappe.generate_hash(length=6),
				"parent_account": frappe.db.get_value("Account", self.expense, "parent_account"),
				"account_currency": "INR",
				"is_group": 0,
			}
		).insert()
		original = self.expense
		try:
			self.expense = account.name
			other = self.make_invoice(80.02)
		finally:
			self.expense = original
		frappe.set_user(self.reader)
		receipt = self.weighted_receipt(weights=(1, 1, 1), quantities=(1, 1, 1), rates=(11, 13, 17))
		result = self.prepare(
			receipts=[{"doctype": "Purchase Receipt", "name": receipt.name}],
			charges=[{"purchase_invoice": invoice.name, "basis": "Qty"} for invoice in (self.invoice, other)],
		)
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual(result["draft_values"]["distribute_charges_based_on"], "Qty")
		saved = create_doc(LCV, result["draft_values"])
		submit_doc(LCV, saved["name"])
		for expense, amount in ((self.expense, 120), (account.name, 80.02)):
			credit = frappe.db.sql(
				"select sum(credit-debit) from `tabGL Entry` where voucher_type='Purchase Receipt' and voucher_no=%s and account=%s and is_cancelled=0",
				(receipt.name, expense),
			)[0][0]
			self.assertAlmostEqual(float(credit), amount, places=2)

	def test_mixed_basis_sources_request_separate_vouchers(self):
		other = self.make_other_invoice()
		before = self.snapshot()
		result = self.prepare(
			charges=[
				{"purchase_invoice": self.invoice.name, "basis": "Amount"},
				{"purchase_invoice": other.name, "basis": "Qty"},
			]
		)
		self.assertEqual(result["issues"][0]["code"], "single_charge_per_manual_voucher")
		self.assertNotIn("draft_values", result)
		self.assertEqual(before, self.snapshot())

	def test_desk_claim_counts_and_cancel_frees_it(self):
		frappe.set_user("Administrator")
		doc = frappe.get_doc({"doctype": LCV, **self.values(amounts=[150])}).insert()
		doc.submit()  # Native Desk deliberately has no Jarvis cap.
		frappe.set_user(self.reader)
		result = self.prepare()
		self.assertEqual(result["issues"][0]["code"], "source_net_claim_exceeded")
		cancel_doc(LCV, doc.name)
		self.assertEqual(self.prepare()["status"], "ready")

	def test_current_claim_does_not_trust_stale_base_amount(self):
		doc = create_doc(LCV, self.values(amounts=[120]))
		frappe.db.set_value(TAX_DOCTYPE, doc["taxes"][0]["name"], {"amount": 121, "base_amount": 0})
		with self.assertRaises(LCVSourceTaxNeedsInput) as caught:
			submit_doc(LCV, doc["name"])
		self.assertEqual(caught.exception.assessment["issues"][0]["code"], "source_net_claim_exceeded")
		self.assertEqual(frappe.get_doc(LCV, doc["name"]).docstatus, 0)

	def test_row_replacement_cannot_remove_the_source(self):
		doc = create_doc(LCV, self.values())
		with self.assertRaises((LCVSourceTaxNeedsInput, InvalidArgumentError)):
			update_doc(
				LCV,
				doc["name"],
				{"taxes": [{"description": "Replacement", "expense_account": self.expense, "amount": 120}]},
			)

	def test_missing_setup_has_no_executable_draft(self):
		with patch("jarvis.tools.preview_landed_cost_voucher.source_link_available", return_value=False):
			result = self.prepare()
		self.assertEqual(result["issues"][0]["code"], "source_link_unavailable")
		self.assertNotIn("draft_values", result)

	def test_native_vendor_schema_requires_matching_backend(self):
		doc = build_doc(LCV, self.values())
		original = doc.meta.has_field
		with patch.object(
			doc.meta, "has_field", side_effect=lambda name: name == "vendor_invoices" or original(name)
		):
			with self.assertRaises(LCVSourceTaxNeedsInput) as caught:
				gate_lcv(doc)
		self.assertEqual(caught.exception.assessment["issues"][0]["code"], "capability_unavailable")

	def test_schema_conflict_preserves_field_and_blocks_preview(self):
		frappe.set_user("Administrator")
		before = frappe.get_doc("Custom Field", f"{TAX_DOCTYPE}-{SOURCE_INVOICE}").as_dict()
		meta = frappe.get_meta(TAX_DOCTYPE)
		field = meta.get_field(SOURCE_INVOICE)
		original = frappe.get_meta

		def metadata(doctype, *args, **kwargs):
			return meta if doctype == TAX_DOCTYPE else original(doctype, *args, **kwargs)

		# set_user clears request caches. Keep the conflicting schema visible to
		# both the administrator migration and the accountant's next preview.
		with patch.object(field, "fieldtype", "Data"), patch.object(frappe, "get_meta", side_effect=metadata):
			setup_source_link()  # A customer collision must not abort bench migrate.
			self.assertFalse(source_link_available())
			frappe.set_user(self.reader)
			result = self.prepare()
		self.assertEqual(result["issues"][0]["code"], "source_link_unavailable")
		self.assertNotIn("draft_values", result)
		self.assertEqual(before, frappe.get_doc("Custom Field", f"{TAX_DOCTYPE}-{SOURCE_INVOICE}").as_dict())

	def test_no_copy_drift_cannot_remove_links_during_amendment(self):
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		saved = create_doc(LCV, self.values())
		submit_doc(LCV, saved["name"])
		cancel_doc(LCV, saved["name"])
		before = frappe.db.count(LCV)
		frappe.set_user("Administrator")
		try:
			make_property_setter(TAX_DOCTYPE, SOURCE_INVOICE, "no_copy", 1, "Check")
			frappe.clear_cache(doctype=TAX_DOCTYPE)
			frappe.set_user(self.reader)
			with self.assertRaises(LCVSourceTaxNeedsInput):
				amend_doc(LCV, saved["name"])
			self.assertEqual(frappe.db.count(LCV), before)
		finally:
			frappe.set_user("Administrator")
			make_property_setter(TAX_DOCTYPE, SOURCE_INVOICE, "no_copy", 0, "Check")
			frappe.clear_cache(doctype=TAX_DOCTYPE)

	def test_clearing_one_of_two_links_to_same_invoice_is_blocked(self):
		values = self.values()
		values["taxes"][0]["amount"] = 40
		values["taxes"].append({**values["taxes"][0], "amount": 80})
		saved = create_doc(LCV, values)
		rows = [{"name": row["name"]} for row in saved["taxes"]]
		rows[0][SOURCE_INVOICE] = ""
		with self.assertRaises(LCVSourceTaxNeedsInput) as caught:
			update_doc(LCV, saved["name"], {"taxes": rows})
		self.assertEqual(caught.exception.assessment["issues"][0]["code"], "vendor_link_removed")
		self.assertTrue(all(row.get(SOURCE_INVOICE) for row in frappe.get_doc(LCV, saved["name"]).taxes))

	def test_confirm_card_amount_change_preserves_unmentioned_source_link(self):
		saved = create_doc(LCV, self.values())
		updated = update_doc(
			LCV, saved["name"], {"taxes": [{"name": saved["taxes"][0]["name"], "amount": 100}]}
		)
		self.assertEqual(updated["taxes"][0][SOURCE_INVOICE], self.invoice.name)
		self.assertEqual(updated["total_taxes_and_charges"], 100)

	def test_setup_is_idempotent_and_preserves_voucher_fields(self):
		doc = create_doc(LCV, self.values())
		before = frappe.get_doc(LCV, doc["name"]).as_dict()
		field_name = f"{TAX_DOCTYPE}-{SOURCE_INVOICE}"
		field = frappe.get_doc("Custom Field", field_name).as_dict()
		frappe.set_user("Administrator")
		setup_source_link()
		setup_source_link()
		self.assertEqual(before, frappe.get_doc(LCV, doc["name"]).as_dict())
		self.assertEqual(field, frappe.get_doc("Custom Field", field_name).as_dict())
		indexes = frappe.db.sql(f"show index from `tab{TAX_DOCTYPE}` where Column_name=%s", SOURCE_INVOICE)
		self.assertTrue(indexes)

	def test_foreign_link_without_recorded_rate_cannot_use_date_lookup(self):
		# Existing INR accounts always use rate 1; a supplied conflicting rate is refused.
		values = self.values()
		values["taxes"][0]["exchange_rate"] = 2
		with self.assertRaises(LCVSourceTaxNeedsInput) as caught:
			create_doc(LCV, values)
		self.assertEqual(caught.exception.assessment["issues"][0]["code"], "linked_charge_unsupported")
