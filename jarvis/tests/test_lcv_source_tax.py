"""Source GST cannot be cleared by an allocation or a different Jarvis write path.

Recorded GST fields in these fixtures model legacy submitted invoices. The tax
charges and their GL are posted by native ERPNext; classification uses the real
installed India Compliance when available. No real site is needed in CI.
"""

from unittest.mock import patch

import frappe

from jarvis.exceptions import PermissionDeniedError
from jarvis.lcv import SOURCE_INVOICE
from jarvis.tests import test_landed_cost_preview as preview_tests
from jarvis.tests.test_landed_cost_defaults import LandedCostFixtures
from jarvis.tools._lcv_source_tax import LCVSourceTaxNeedsInput, gate_lcv
from jarvis.tools.create_doc import create_doc
from jarvis.tools.preview_doc import preview_doc
from jarvis.tools.submit_doc import submit_doc
from jarvis.tools.update_doc import update_doc

LCV = "Landed Cost Voucher"


class TestLCVSourceTax(LandedCostFixtures):
	prepare = preview_tests.TestLandedCostPreview.prepare
	snapshot = preview_tests.TestLandedCostPreview.snapshot

	def require_ic(self):
		if "india_compliance" not in frappe.get_installed_apps():
			self.skipTest("India Compliance integration requires its installed schema and native classifier")

	@patch.object(frappe, "in_test", False)
	def taxed_invoice(self, *, restricted=False, item_flag=False, currency="INR", fx=1, tax=18):
		frappe.set_user("Administrator")
		parent = frappe.db.get_value(
			"Account", {"company": self.company, "root_type": "Asset", "is_group": 1}
		)
		account = (
			frappe.get_doc(
				{
					"doctype": "Account",
					"company": self.company,
					"account_name": "_JLCV Tax " + frappe.generate_hash(length=7),
					"parent_account": parent,
					"account_type": "Tax",
					"account_currency": "INR",
				}
			)
			.insert()
			.name
		)
		pi = frappe.copy_doc(self.invoice)
		pi.docstatus = 0
		pi.bill_no = frappe.generate_hash(length=10)
		pi.currency, pi.conversion_rate = currency, fx
		pi.disable_rounded_total = 1
		pi.set(
			"taxes",
			[
				{
					"charge_type": "Actual",
					"account_head": account,
					"tax_amount": tax,
					"description": "Recorded tax",
					"cost_center": self.cost_center,
					"category": "Total",
					"add_deduct_tax": "Add",
				}
			],
		)
		pi.insert()
		pi.submit()
		if "india_compliance" in frappe.get_installed_apps() and frappe.get_meta(
			"Purchase Invoice"
		).has_field("ineligibility_reason"):
			from india_compliance.gst_india.overrides.purchase_invoice import set_ineligibility_reason

			pi.company_gstin = "33AAACU9603R1ZW"
			pi.place_of_supply = "19-West Bengal" if restricted else "33-Tamil Nadu"
			pi.items[0].is_ineligible_for_itc = int(item_flag)
			set_ineligibility_reason(pi, show_alert=False)
			frappe.db.set_value(
				"Purchase Invoice",
				pi.name,
				{
					"company_gstin": pi.company_gstin,
					"place_of_supply": pi.place_of_supply,
					"ineligibility_reason": pi.ineligibility_reason,
				},
			)
			frappe.db.set_value("Purchase Taxes and Charges", pi.taxes[0].name, "gst_tax_type", "cgst")
			frappe.db.set_value(
				"Purchase Invoice Item",
				pi.items[0].name,
				{
					"cgst_amount": pi.taxes[0].base_tax_amount_after_discount_amount,
					"is_ineligible_for_itc": int(item_flag),
				},
			)
		pi.reload()
		frappe.set_user(self.reader)
		return pi

	def legacy_draft(self):
		# A native Desk/old-version draft must be reviewed again by new Jarvis code.
		frappe.set_user("Administrator")
		doc = frappe.get_doc({"doctype": LCV, **self.values(amounts=[self.invoice.base_total])}).insert()
		frappe.set_user(self.reader)
		return doc

	def test_pos_restriction_with_zero_item_flags_blocks_without_effects(self):
		self.require_ic()
		self.invoice = self.taxed_invoice(restricted=True)
		self.assertEqual(self.invoice.items[0].is_ineligible_for_itc, 0)
		before = self.snapshot()
		result = self.prepare()
		self.assertEqual(result["status"], "needs_input", result)
		self.assertEqual(result["issues"][0]["code"], "nonrecoverable_gst_unsupported")
		self.assertEqual(result["issues"][0]["ineligible_gst_amount"], 18)
		self.assertNotIn("draft_values", result)
		self.assertEqual(before, self.snapshot())

	def test_partial_selection_is_not_claimed_as_attributable_tax(self):
		self.require_ic()
		self.invoice = self.taxed_invoice(restricted=True)
		result = self.prepare(
			charges=[
				{
					"purchase_invoice": self.invoice.name,
					"basis": "Manual",
					"items": [{"name": self.invoice.items[0].name, "amount": 20}],
					"allocations": [
						{
							"receipt_document_type": "Purchase Receipt",
							"receipt_document": self.receipt.name,
							"receipt_item": self.receipt.items[0].name,
							"amount": 20,
						}
					],
				}
			]
		)
		self.assertEqual(result["status"], "needs_input", result)
		self.assertEqual(result["issues"][0]["amount_scope"], "invoice")
		self.assertNotIn("draft_values", result)

	def test_item_flag_alone_is_not_ignored(self):
		self.require_ic()
		self.invoice = self.taxed_invoice(item_flag=True)
		self.assertEqual(self.prepare()["issues"][0]["code"], "nonrecoverable_gst_unsupported")

	def test_recorded_input_gst_stays_outside_cost(self):
		self.require_ic()
		self.invoice = self.taxed_invoice()
		result = self.prepare()
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual(result["source_tax"]["invoices"][0]["treatment"], "excluded_per_recorded_treatment")
		self.assertEqual(result["totals"]["charges"], self.invoice.base_net_total)
		saved = create_doc(LCV, result["draft_values"])
		self.assertEqual(saved["taxes"][0]["base_amount"], self.invoice.base_net_total)

	def test_offset_input_tax_debit_is_not_proof_of_credit(self):
		self.require_ic()
		self.invoice = self.taxed_invoice()
		frappe.db.sql(
			"update `tabGL Entry` set credit=debit where voucher_type='Purchase Invoice' and voucher_no=%s and account=%s",
			(self.invoice.name, self.invoice.taxes[0].account_head),
		)
		self.assertEqual(self.prepare()["issues"][0]["code"], "posted_treatment_unverified")

	def test_missing_item_split_cannot_be_treated_as_zero_tax(self):
		self.require_ic()
		self.invoice = self.taxed_invoice()
		frappe.db.set_value("Purchase Invoice Item", self.invoice.items[0].name, "cgst_amount", 0)
		self.assertEqual(self.prepare()["issues"][0]["code"], "tax_split_inconsistent")

	def test_edited_pos_does_not_reuse_stale_reason(self):
		self.require_ic()
		self.invoice = self.taxed_invoice()
		frappe.db.set_value("Purchase Invoice", self.invoice.name, "place_of_supply", "19-West Bengal")
		self.assertEqual(self.prepare()["issues"][0]["code"], "tax_treatment_changed")

	def test_fx_uses_company_currency_once(self):
		self.require_ic()
		self.invoice = self.taxed_invoice(restricted=True, currency="USD", fx=83.4, tax=0.18)
		result = self.prepare()
		self.assertEqual(result["issues"][0]["ineligible_gst_amount"], 15.01)
		self.assertEqual(result["issues"][0]["currency"], "INR")

	def test_generic_preview_and_create_cannot_bypass_tax(self):
		self.require_ic()
		self.invoice = self.taxed_invoice(restricted=True)
		values = self.values(amounts=[self.invoice.base_total])
		before = self.snapshot()
		result = preview_doc(LCV, values)
		self.assertFalse(result["valid"], result)
		self.assertEqual(result["status"], "needs_input")
		self.assertNotIn("resolved", result)
		with self.assertRaises(LCVSourceTaxNeedsInput):
			create_doc(LCV, values)
		self.assertEqual(before, self.snapshot())

	def test_saved_draft_update_and_submit_recheck_source(self):
		self.require_ic()
		self.invoice = self.taxed_invoice(restricted=True)
		doc = self.legacy_draft()
		before = self.snapshot()
		result = self.prepare(existing_draft=doc.name, charges=[])
		self.assertEqual(result["status"], "needs_input", result)
		self.assertNotIn("suggested_action", result)
		with self.assertRaises(LCVSourceTaxNeedsInput):
			update_doc(LCV, doc.name, {"posting_date": doc.posting_date})
		with self.assertRaises(LCVSourceTaxNeedsInput):
			submit_doc(LCV, doc.name)
		self.assertEqual(doc.reload().docstatus, 0)
		self.assertEqual(before, self.snapshot())

	def test_removing_links_cannot_erase_the_source_check(self):
		doc = self.legacy_draft()
		with self.assertRaises(LCVSourceTaxNeedsInput) as caught:
			update_doc(LCV, doc.name, {"taxes": [{"name": doc.taxes[0].name, SOURCE_INVOICE: ""}]})
		self.assertEqual(caught.exception.assessment["issues"][0]["code"], "vendor_link_removed")
		self.assertEqual(doc.reload().taxes[0].get(SOURCE_INVOICE), self.invoice.name)

	def test_taxed_invoice_without_classifier_is_unresolved(self):
		self.invoice = self.taxed_invoice()
		apps = [a for a in frappe.get_installed_apps() if a != "india_compliance"]
		with patch("jarvis.tools._lcv_source_tax.frappe.get_installed_apps", return_value=apps):
			result = self.prepare()
		self.assertEqual(result["issues"][0]["code"], "tax_classifier_unavailable")

	def test_untaxed_invoice_does_not_require_india_compliance(self):
		apps = [a for a in frappe.get_installed_apps() if a != "india_compliance"]
		with patch("jarvis.tools._lcv_source_tax.frappe.get_installed_apps", return_value=apps):
			self.assertEqual(self.prepare()["status"], "ready")

	def test_hidden_header_cannot_leak_tax_through_result(self):
		self.require_ic()
		self.invoice = self.taxed_invoice(restricted=True)
		meta = frappe.get_meta("Purchase Invoice")
		with patch.object(meta.get_field("ineligibility_reason"), "permlevel", 99):
			with self.assertRaises(PermissionDeniedError) as caught:
				self.prepare()
		self.assertNotIn(self.invoice.name, str(caught.exception))
		self.assertNotIn("18", str(caught.exception))

	def test_unreadable_ledger_cannot_prove_input_credit(self):
		self.require_ic()
		self.invoice = self.taxed_invoice()
		original = frappe.has_permission

		def allowed(doctype, *args, **kwargs):
			return False if doctype == "GL Entry" else original(doctype, *args, **kwargs)

		with patch.object(frappe, "has_permission", side_effect=allowed):
			result = self.prepare()
		self.assertEqual(result["issues"][0]["code"], "posted_treatment_unreadable")
		self.assertNotIn("ineligible_gst_amount", result["issues"][0])

	def test_cancelled_source_invalidates_prior_preview(self):
		prepared = self.prepare()
		self.assertEqual(prepared["status"], "ready")
		frappe.db.set_value("Purchase Invoice", self.invoice.name, "docstatus", 2)
		with self.assertRaises(LCVSourceTaxNeedsInput):
			create_doc(LCV, prepared["draft_values"])

	def test_batch_rolls_back_an_earlier_valid_create(self):
		self.require_ic()
		good = self.values(amounts=[self.invoice.base_total])
		self.invoice = self.taxed_invoice(restricted=True)
		bad = self.values(amounts=[self.invoice.base_total])
		before = self.snapshot()
		with self.assertRaises(Exception):
			create_doc(docs=[{"doctype": LCV, "values": good}, {"doctype": LCV, "values": bad}])
		self.assertEqual(before, self.snapshot())

	def test_manual_unlinked_costs_keep_native_scope(self):
		values = self.values()
		values["taxes"][0].pop(SOURCE_INVOICE)
		result = preview_doc(LCV, values)
		self.assertTrue(result["valid"], result)
		self.assertEqual(result["source_tax"]["status"], "not_applicable")

	def test_missing_tax_rows_do_not_erase_recorded_tax(self):
		frappe.db.set_value("Purchase Invoice", self.invoice.name, "base_total_taxes_and_charges", 18)
		self.assertEqual(self.prepare()["issues"][0]["code"], "tax_split_inconsistent")

	def test_workflow_single_and_batch_stop_before_transition(self):
		self.require_ic()
		from jarvis.tools.apply_workflow_action import apply_workflow_action

		self.invoice = self.taxed_invoice(restricted=True)
		doc = self.legacy_draft()
		with (
			patch("jarvis.tools.apply_workflow_action.get_workflow_name", return_value="LCV approval"),
			patch(
				"jarvis.tools.apply_workflow_action.get_workflow",
				return_value=frappe._dict(states=[frappe._dict(state="Submitted", doc_status=1)]),
			),
			patch(
				"jarvis.tools.apply_workflow_action.get_transitions",
				return_value=[frappe._dict(action="Approve", next_state="Submitted")],
			),
			patch("jarvis.tools.apply_workflow_action.apply_workflow") as apply,
		):
			with self.assertRaises(LCVSourceTaxNeedsInput):
				apply_workflow_action(LCV, doc.name, "Approve")
			with self.assertRaises(LCVSourceTaxNeedsInput):
				apply_workflow_action(LCV, action="Approve", names=[doc.name])
			apply.assert_not_called()
		self.assertEqual(doc.reload().docstatus, 0)

	def test_amend_does_not_create_another_unresolved_draft(self):
		self.require_ic()
		from jarvis.tools.amend_doc import amend_doc

		self.invoice = self.taxed_invoice(restricted=True)
		doc = self.legacy_draft()
		frappe.db.set_value(LCV, doc.name, "docstatus", 2)
		before = self.snapshot()
		with self.assertRaises(LCVSourceTaxNeedsInput):
			amend_doc(LCV, doc.name)
		self.assertEqual(before, self.snapshot())

	def test_controller_write_cannot_bypass_tax(self):
		self.require_ic()
		from jarvis.tools.run_method import _run_doc_method

		self.invoice = self.taxed_invoice(restricted=True)
		doc = self.legacy_draft()
		controller = type(doc)
		called = []

		@frappe.whitelist()
		def save_lcv(self):
			called.append(self.name)
			self.save()

		with patch.object(controller, "test_save_lcv", save_lcv, create=True):
			with self.assertRaises(LCVSourceTaxNeedsInput):
				_run_doc_method("test_save_lcv", LCV, doc.name, {})
		self.assertEqual(called, [])

	def test_held_sheet_marks_tax_gap_as_invalid(self):
		self.require_ic()
		from jarvis.chat import held_sheets

		self.invoice = self.taxed_invoice(restricted=True)
		record = held_sheets._record({"op": "create", "doctype": LCV, "values": self.values()})
		before = self.snapshot()
		problems = held_sheets._dry_run([record], [0])
		self.assertEqual(problems[0][0], "invalid", problems)
		self.assertIsInstance(problems[0][1], LCVSourceTaxNeedsInput)
		self.assertEqual(before, self.snapshot())

	def test_confirmed_write_rechecks_source_and_reports_gap(self):
		self.require_ic()
		from jarvis import api

		prepared = self.prepare()
		self.assertEqual(prepared["status"], "ready")
		# A different submitted source is supplied after the initial preview.
		self.invoice = self.taxed_invoice(restricted=True)
		values = self.values(amounts=[self.invoice.base_total])
		before = self.snapshot()
		result = api.dispatch_confirmed("create_doc", {"doctype": LCV, "values": values})
		self.assertFalse(result["ok"], result)
		self.assertIn("nonrecoverable GST", result["error"]["message"])
		self.assertEqual(before, self.snapshot())

	def test_rejection_is_available_when_source_tax_needs_input(self):
		self.require_ic()
		from jarvis.tools.apply_workflow_action import apply_workflow_action

		self.invoice = self.taxed_invoice(restricted=True)
		doc = self.legacy_draft()
		with (
			patch("jarvis.tools.apply_workflow_action.get_workflow_name", return_value="LCV approval"),
			patch(
				"jarvis.tools.apply_workflow_action.get_workflow",
				return_value=frappe._dict(states=[frappe._dict(state="Rejected", doc_status=0)]),
			),
			patch(
				"jarvis.tools.apply_workflow_action.get_transitions",
				return_value=[frappe._dict(action="Reject", next_state="Rejected")],
			),
			patch("jarvis.tools.apply_workflow_action.apply_workflow", return_value=doc) as apply,
		):
			apply_workflow_action(LCV, doc.name, "Reject")
			apply.assert_called_once()

	def test_recoverable_source_claim_cannot_use_discount_as_extra_cost(self):
		self.require_ic()
		self.invoice = self.taxed_invoice()
		frappe.db.set_value("Purchase Invoice", self.invoice.name, "base_net_total", 100)
		result = preview_doc(LCV, self.values(amounts=[120]))
		self.assertFalse(result["valid"], result)
		self.assertEqual(result["issues"][0]["code"], "source_net_claim_exceeded")

	@patch.object(frappe, "in_test", False)
	def test_structured_withholding_stays_outside_cost(self):
		frappe.set_user("Administrator")
		parent = frappe.db.get_value(
			"Account", {"company": self.company, "root_type": "Liability", "is_group": 1}
		)
		account = frappe.get_doc(
			{
				"doctype": "Account",
				"company": self.company,
				"account_name": "_JLCV TDS " + frappe.generate_hash(length=7),
				"parent_account": parent,
				"account_type": "Tax",
			}
		).insert()
		category = frappe.get_doc(
			{
				"doctype": "Tax Withholding Category",
				"name": "_JLCV TDS " + frappe.generate_hash(length=7),
				"category_name": "LCV withholding",
				"rates": [{"from_date": "2020-01-01", "to_date": "2030-12-31", "tax_withholding_rate": 2}],
				"accounts": [{"company": self.company, "account": account.name}],
			}
		).insert()
		pi = frappe.copy_doc(self.invoice)
		pi.docstatus = 0
		pi.bill_no = frappe.generate_hash(length=10)
		pi.set(
			"taxes",
			[
				{
					"charge_type": "Actual",
					"account_head": account.name,
					"tax_amount": 2,
					"description": "TDS",
					"category": "Total",
					"add_deduct_tax": "Deduct",
					"cost_center": self.cost_center,
				}
			],
		)
		pi.insert()
		pi.submit()
		if pi.items[0].meta.has_field("tax_withholding_category"):
			frappe.db.set_value(
				"Purchase Invoice Item",
				pi.items[0].name,
				{"apply_tds": 1, "tax_withholding_category": category.name},
			)
		else:
			frappe.db.set_value(
				"Purchase Invoice", pi.name, {"apply_tds": 1, "tax_withholding_category": category.name}
			)
		self.invoice = pi.reload()
		frappe.set_user(self.reader)
		result = self.prepare()
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual(result["totals"]["charges"], self.invoice.base_net_total)
		self.assertEqual(result["source_tax"]["invoices"][0]["treatment"], "no_tax")

	def test_import_preview_cannot_clear_lcv_tax_checks(self):
		from jarvis.tools._import_preview import build_import_preview

		with patch("jarvis.tools._import_preview._import_file") as read_file:
			result = build_import_preview(doctype=LCV, file_url="/private/files/lcv.csv")
			self.assertFalse(result["importable"], result)
			self.assertIn("source tax", result["reason"])
			read_file.assert_not_called()

	def test_import_execution_cannot_start_an_unguarded_lcv_job(self):
		from jarvis.exceptions import InvalidArgumentError
		from jarvis.tools.run_import import run_import

		before = frappe.db.count("Data Import")
		with (
			patch.dict(frappe.conf, {"jarvis_import_disabled": 0}),
			patch("jarvis.tools.run_import.frappe.db.commit") as commit,
		):
			with self.assertRaises(InvalidArgumentError):
				run_import(LCV, file_url="/private/files/lcv.csv", submit_after_import=True)
			commit.assert_not_called()
		self.assertEqual(frappe.db.count("Data Import"), before)

	def test_workflow_reload_cannot_switch_to_an_unchecked_target(self):
		from jarvis.tools.apply_workflow_action import _lcv_tax_gate

		doc = self.legacy_draft()
		frappe.db.set_value(LCV, doc.name, "modified", "2099-01-01 00:00:00", update_modified=False)
		with self.assertRaises(frappe.TimestampMismatchError):
			_lcv_tax_gate(doc, "Approve")
