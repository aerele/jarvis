"""Upgrade protection without requiring a legacy custom field on fresh CI sites."""

from unittest.mock import patch

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tests.test_landed_cost_defaults import LandedCostFixtures
from jarvis.tools import _lcv_legacy
from jarvis.tools._lcv_source_tax import LCVSourceTaxNeedsInput, gate_lcv
from jarvis.tools.create_doc import build_doc, create_doc
from jarvis.tools.preview_import import preview_import
from jarvis.tools.run_import import run_import
from jarvis.tools.update_doc import update_doc


class TestLCVLegacy(LandedCostFixtures):
	def test_tax_child_import_cannot_replace_source_provenance(self):
		with patch("jarvis.tools._import_preview._import_file") as read:
			result = preview_import(_lcv_legacy.TAX_DOCTYPE, filename="unused.csv")
		self.assertFalse(result["importable"])
		read.assert_not_called()
		before = frappe.db.count("Data Import")
		with self.assertRaises(InvalidArgumentError):
			run_import(_lcv_legacy.TAX_DOCTYPE, filename="unused.csv")
		self.assertEqual(frappe.db.count("Data Import"), before)

	def test_fresh_site_does_not_query_a_missing_legacy_column(self):
		meta = frappe.get_meta(_lcv_legacy.TAX_DOCTYPE)
		original = meta.get_field

		def field(name):
			return None if name == _lcv_legacy.SOURCE_FIELD else original(name)

		with (
			patch.object(meta, "get_field", side_effect=field),
			patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql,
		):
			self.assertEqual(_lcv_legacy.claimed_cost(self.invoice.name, None), 0)
		self.assertFalse(any(_lcv_legacy.SOURCE_FIELD in str(call) for call in sql.call_args_list))

	def test_legacy_draft_requires_new_preview_without_inserting(self):
		doc = build_doc("Landed Cost Voucher", self.values())
		before = frappe.db.count("Landed Cost Voucher")
		with patch.object(_lcv_legacy, "source_names", return_value={self.invoice.name}):
			with self.assertRaises(LCVSourceTaxNeedsInput) as caught:
				gate_lcv(doc)
		self.assertEqual(caught.exception.assessment["issues"][0]["code"], "legacy_source_link")
		self.assertEqual(frappe.db.count("Landed Cost Voucher"), before)

	def test_previous_legacy_claim_reduces_native_invoice_balance(self):
		doc = build_doc("Landed Cost Voucher", self.values(amounts=[100], charge=100))
		with patch.object(_lcv_legacy, "claimed_cost", return_value=30):
			with self.assertRaises(LCVSourceTaxNeedsInput) as caught:
				gate_lcv(doc)
		self.assertEqual(caught.exception.assessment["issues"][0]["code"], "source_net_claim_exceeded")
		with patch.object(_lcv_legacy, "claimed_cost", return_value=20):
			self.assertEqual(gate_lcv(doc)["status"], "ready")

	def test_overflow_never_proves_available_cost(self):
		doc = build_doc("Landed Cost Voucher", self.values())
		with patch.object(_lcv_legacy, "claimed_cost", return_value=None):
			with self.assertRaises(LCVSourceTaxNeedsInput) as caught:
				gate_lcv(doc)
		self.assertEqual(caught.exception.assessment["issues"][0]["code"], "legacy_claims_unresolved")

	def test_update_cannot_erase_legacy_source_while_costs_remain(self):
		values = self.values()
		values["vendor_invoices"] = []
		saved = create_doc("Landed Cost Voucher", values)
		with patch.object(_lcv_legacy, "source_names", side_effect=[{self.invoice.name}, set()]):
			with self.assertRaises(LCVSourceTaxNeedsInput):
				update_doc("Landed Cost Voucher", saved["name"], {"posting_date": saved["posting_date"]})
