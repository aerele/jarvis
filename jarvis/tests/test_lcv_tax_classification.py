"""Run tax-classification branches in CI without an installed IC app.

The IC fixture supplies recorded classifier outputs. It is not a tax-law test.
"""

import sys
import unittest
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import frappe

from jarvis.tools import _lcv_source_tax as source


class Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def precision(self, field):
		return 2


class TestTaxClassificationWithoutIC(unittest.TestCase):
	def invoice(self):
		meta = SimpleNamespace(has_field=lambda field: True)
		item = Doc(
			meta=meta,
			item_code="SERVICE",
			**{f"{kind}_amount": 18 if kind == "cgst" else 0 for kind in source.GST_TYPES},
		)
		tax = Doc(
			meta=meta,
			base_tax_amount_after_discount_amount=18,
			add_deduct_tax="Add",
			category="Total",
			gst_tax_type="cgst",
			account_head="INPUT-GST",
		)
		return Doc(
			name="TEST-PI",
			doctype="Purchase Invoice",
			meta=meta,
			taxes=[tax],
			items=[item],
			company="TEST-COMPANY",
			company_gstin="TEST-GSTIN",
			place_of_supply="TEST-POS",
			ineligibility_reason="",
			base_total_taxes_and_charges=18,
			is_reverse_charge=0,
			is_opening="No",
		)

	@contextmanager
	def fixture(self, *, ineligible=0, changed=False, posted=True):
		modules = {}
		for name in [
			"india_compliance",
			"india_compliance.gst_india",
			"india_compliance.gst_india.overrides",
			"india_compliance.gst_india.constants",
			"india_compliance.gst_india.overrides.ineligible_itc",
			"india_compliance.gst_india.overrides.purchase_invoice",
		]:
			module = ModuleType(name)
			module.__path__ = []
			modules[name] = module
		modules["india_compliance.gst_india.constants"].GST_TAX_TYPES = source.GST_TYPES

		class RecordedClassifier:
			def __init__(self, working):
				self.working = working

			def update_item_ineligibility(self):
				for row in self.working.items:
					row._ineligible_tax_amount = ineligible

		def recorded_reason(working, show_alert=False):
			if changed:
				working.ineligibility_reason = "changed-recorded-treatment"

		modules["india_compliance.gst_india.overrides.ineligible_itc"].PurchaseInvoice = RecordedClassifier
		modules[
			"india_compliance.gst_india.overrides.purchase_invoice"
		].set_ineligibility_reason = recorded_reason
		meta = SimpleNamespace()
		records = {
			("Company", "TEST-COMPANY"): Doc(meta=meta, default_currency="INR"),
			("Item", "SERVICE"): Doc(meta=meta),
			("Account", "INPUT-GST"): Doc(
				meta=meta, company="TEST-COMPANY", root_type="Asset", account_type="Tax"
			),
		}
		with (
			patch.dict(sys.modules, modules),
			patch.object(frappe, "session", frappe._dict(user="Administrator")),
			patch.object(
				frappe, "get_installed_apps", return_value=["frappe", "erpnext", "india_compliance"]
			),
			patch.object(source, "_read", side_effect=lambda doctype, name: records[(doctype, name)]),
			patch.object(source, "_posted_input_matches", return_value=posted) as ledger,
		):
			yield ledger

	def test_recorded_input_tax_is_excluded_and_original_is_unchanged(self):
		invoice = self.invoice()
		with self.fixture() as ledger:
			result = source._classify(invoice)
		self.assertEqual(result["treatment"], "excluded_per_recorded_treatment")
		self.assertEqual(result["gst_amount"], 18)
		self.assertEqual(result["currency"], "INR")
		self.assertFalse(hasattr(invoice.items[0], "_ineligible_tax_amount"))
		self.assertEqual(ledger.call_args.args[1], {"INPUT-GST": 18})

	def test_reverse_charge_and_opening_require_separate_reconciliation(self):
		for key, value in [("is_reverse_charge", 1), ("is_opening", "Yes")]:
			with self.subTest(key=key), self.fixture():
				invoice = self.invoice()
				setattr(invoice, key, value)
				self.assertEqual(source._classify(invoice)["code"], "source_tax_unsupported")

	def test_nonrecoverable_recorded_tax_is_an_input_gap(self):
		with self.fixture(ineligible=18):
			result = source._classify(self.invoice())
		self.assertEqual(result["code"], "nonrecoverable_gst_unsupported")
		self.assertEqual(result["ineligible_gst_amount"], 18)

	def test_changed_recorded_treatment_and_unverified_postings_are_not_ready(self):
		for options, expected in [
			({"changed": True}, "tax_treatment_changed"),
			({"posted": False}, "posted_treatment_unverified"),
		]:
			with self.subTest(options=options), self.fixture(**options):
				self.assertEqual(source._classify(self.invoice())["code"], expected)

	def test_item_amount_mismatch_is_not_accepted(self):
		invoice = self.invoice()
		invoice.items[0].cgst_amount = 17
		with self.fixture():
			self.assertEqual(source._classify(invoice)["code"], "tax_split_inconsistent")

	def test_missing_gst_identity_is_not_accepted(self):
		invoice = self.invoice()
		invoice.company_gstin = ""
		with self.fixture():
			self.assertEqual(source._classify(invoice)["code"], "tax_identity_unverified")

	def test_recorded_withholding_does_not_hide_input_gst(self):
		invoice = self.invoice()
		invoice.taxes.append(
			Doc(
				base_tax_amount_after_discount_amount=1,
				add_deduct_tax="Deduct",
				category="Total",
				account_head="TDS",
				gst_tax_type=None,
			)
		)
		invoice.base_total_taxes_and_charges = 17
		with self.fixture(), patch.object(source, "_tds_accounts", return_value={"TDS"}):
			self.assertEqual(source._classify(invoice)["treatment"], "excluded_per_recorded_treatment")
