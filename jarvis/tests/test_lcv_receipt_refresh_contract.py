"""Receipt summaries refresh after edits and before source-tax validation."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jarvis.tools.update_doc import _update_one


class TestReceiptRefreshOrder(unittest.TestCase):
	def test_changed_receipt_is_refetched_before_the_tax_gate_and_save(self):
		class Draft:
			doctype = "Landed Cost Voucher"
			docstatus = 0
			meta = SimpleNamespace(get_table_fields=lambda: [])
			purchase_receipts = [{"receipt_document": "old", "grand_total": 10}]
			taxes = []

			def get(self, field):
				return getattr(self, field, None)

			def set(self, field, value):
				setattr(self, field, value)

			def save(self):
				self.saved = dict(self.purchase_receipts[0])

		doc = Draft()

		def fetch(target):
			self.assertEqual(target.purchase_receipts[0]["receipt_document"], "new")
			target.purchase_receipts[0].update(
				supplier="new-supplier", posting_date="2026-10-31", grand_total=20
			)

		def tax_gate(target, **kwargs):
			self.assertEqual(target.purchase_receipts[0]["grand_total"], 20)

		with (
			patch("frappe.get_doc", return_value=doc),
			patch("frappe.has_permission", return_value=True),
			patch("jarvis.tools.update_doc.enforce_update"),
			patch("jarvis.tools.update_doc.check_values", side_effect=lambda doctype, changes: changes),
			patch("jarvis.tools.update_doc.cast_numbers"),
			patch("jarvis.tools._lcv_source_tax._guard_fields"),
			patch("jarvis.tools._lcv_source_tax.source_names", return_value=[]),
			patch("jarvis.tools._lcv_source_tax.gate_lcv", side_effect=tax_gate),
			patch("jarvis.tools._client_defaults.landed_cost_receipt_details", side_effect=fetch) as refresh,
		):
			_update_one(
				"Landed Cost Voucher",
				"DRAFT",
				{
					"purchase_receipts": [
						{
							"receipt_document": "new",
							"supplier": "old-supplier",
							"posting_date": "2026-10-01",
							"grand_total": 10,
						}
					]
				},
			)
		refresh.assert_called_once_with(doc)
		self.assertEqual(
			doc.saved,
			{
				"receipt_document": "new",
				"supplier": "new-supplier",
				"posting_date": "2026-10-31",
				"grand_total": 20,
			},
		)
