"""Weight allocation uses posted receipt measurements, never quantities as a proxy."""

from unittest.mock import patch

import frappe

from jarvis.exceptions import PermissionDeniedError
from jarvis.tests.test_landed_cost_defaults import LandedCostFixtures
from jarvis.tools.preview_landed_cost_voucher import preview_landed_cost_voucher


class TestLandedCostWeightPreview(LandedCostFixtures):
	@patch.object(frappe, "in_test", False)
	def weighted_receipt(self, weights=(15, 70), quantities=(2, 1), rates=(50, 100), boxed=False):
		frappe.set_user("Administrator")
		# Minimal CI sites do not install the full standard UOM catalogue.
		for unit in ("Kg", "Gram", "Nos", "Box"):
			if not frappe.db.exists("UOM", unit):
				if not frappe.db.exists("UOM Category", "Mass"):
					frappe.get_doc({"doctype": "UOM Category", "category_name": "Mass"}).insert()
				frappe.get_doc(
					{
						"doctype": "UOM",
						"uom_name": unit,
						"category": "Mass" if unit in {"Kg", "Gram"} else None,
					}
				).insert()
		rows = []
		for idx, (weight, qty, rate) in enumerate(zip(weights, quantities, rates, strict=True)):
			code = self.make_item(f"_JLCV Weight {idx}", 1, "84821020")
			item = frappe.get_doc("Item", code)
			item.weight_per_unit, item.weight_uom = weight, "Kg"
			if boxed and idx == 0:
				item.append("uoms", {"uom": "Box", "conversion_factor": 10})
			item.save()
			rows.append(
				{
					"item_code": code,
					"qty": qty,
					"rate": rate,
					"warehouse": self.receipt.items[0].warehouse,
					**({"uom": "Box", "conversion_factor": 10} if boxed and idx == 0 else {}),
				}
			)
		doc = frappe.get_doc(
			{
				"doctype": "Purchase Receipt",
				"company": self.company,
				"supplier": self.supplier,
				"posting_date": self.receipt.posting_date,
				"set_posting_time": 1,
				"items": rows,
			}
		).insert()
		doc.submit()
		frappe.set_user(self.reader)
		return doc

	def prepare(self, receipt, **charge):
		return preview_landed_cost_voucher(
			self.company,
			[{"doctype": receipt.doctype, "name": receipt.name}],
			charges=[{"purchase_invoice": self.invoice.name, "basis": "Weight", **charge}],
		)

	def test_recorded_weights_drive_allocation_and_original_values_survive(self):
		receipt = self.weighted_receipt()
		self.assertEqual([r.total_weight for r in receipt.items], [30, 70])
		before = frappe.get_doc(receipt.doctype, receipt.name).as_dict()
		count = frappe.db.count("Landed Cost Voucher")
		result = self.prepare(receipt)
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual([r["applicable_charges"] for r in result["items"]], [36, 84])
		self.assertEqual([r["qty"] for r in result["draft_values"]["items"]], [2, 1])
		self.assertEqual([r["amount"] for r in result["draft_values"]["items"]], [100, 100])
		self.assertEqual(result["draft_values"]["distribute_charges_based_on"], "Distribute Manually")
		self.assertEqual(result["charges"][0]["weight_uom"], "Kg")
		self.assertEqual([r["total_weight"] for r in result["charges"][0]["weight_evidence"]], [30, 70])
		self.assertEqual(before, frappe.get_doc(receipt.doctype, receipt.name).as_dict())
		self.assertEqual(count, frappe.db.count("Landed Cost Voucher"))

	def test_mixed_mass_units_require_explicit_resolution(self):
		receipt = self.weighted_receipt()
		frappe.db.set_value("Purchase Receipt Item", receipt.items[1].name, "weight_uom", "Gram")
		result = self.prepare(receipt)
		self.assertEqual(result["status"], "needs_input")
		self.assertEqual(result["issues"][0]["code"], "weight_basis")
		self.assertNotIn("draft_values", result)

	def test_missing_or_inconsistent_posted_weights_do_not_use_item_defaults(self):
		receipt = self.weighted_receipt()
		for values in (
			{"total_weight": 0},
			{"weight_per_unit": 0},
			{"weight_uom": ""},
			{"stock_uom": ""},
			{"total_weight": -30},
			{"total_weight": 31},
			{"weight_uom": "Nos"},
		):
			with self.subTest(values=values):
				frappe.db.set_value("Purchase Receipt Item", receipt.items[0].name, values)
				result = self.prepare(receipt)
				self.assertEqual(result["status"], "needs_input")
				self.assertEqual(result["issues"][0]["code"], "weight_basis")
				self.assertEqual(result["issues"][0]["receipt_item"], receipt.items[0].name)
				self.assertNotIn("draft_values", result)
				frappe.db.set_value(
					"Purchase Receipt Item",
					receipt.items[0].name,
					{
						"total_weight": 30,
						"weight_per_unit": 15,
						"weight_uom": "Kg",
						"stock_uom": receipt.items[0].stock_uom,
					},
				)

	def test_stock_quantity_conversion_is_used_without_changing_purchase_quantity(self):
		receipt = self.weighted_receipt(weights=(2, 10), quantities=(1, 1), rates=(100, 100), boxed=True)
		self.assertEqual(receipt.items[0].stock_qty, 10)
		self.assertEqual([r.total_weight for r in receipt.items], [20, 10])
		result = self.prepare(receipt)
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual([r["applicable_charges"] for r in result["items"]], [80, 40])
		self.assertEqual([r["qty"] for r in result["draft_values"]["items"]], [1, 1])

	def test_current_item_weight_cannot_rewrite_a_posted_receipt_basis(self):
		receipt = self.weighted_receipt()
		frappe.db.set_value("Item", receipt.items[0].item_code, "weight_per_unit", 999)
		result = self.prepare(receipt)
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual([r["applicable_charges"] for r in result["items"]], [36, 84])

	def test_native_rounding_reconciles_three_equal_weights(self):
		receipt = self.weighted_receipt(weights=(1, 1, 1), quantities=(1, 1, 1), rates=(10, 10, 10))
		result = self.prepare(receipt, items=[{"name": self.invoice.items[0].name, "amount": 100}])
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual([r["applicable_charges"] for r in result["items"]], [33.33, 33.33, 33.34])
		self.assertEqual(result["totals"]["allocated"], 100)

	def test_explicit_scope_can_exclude_a_row_without_weight(self):
		receipt = self.weighted_receipt()
		frappe.db.set_value("Purchase Receipt Item", receipt.items[1].name, "total_weight", 0)
		result = self.prepare(receipt, receipt_items=[receipt.items[0].name])
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual([r["applicable_charges"] for r in result["items"]], [120, 0])

	def test_weight_unit_mask_blocks_ratio_and_unit_disclosure(self):
		if not callable(getattr(frappe.get_meta("Purchase Invoice"), "get_masked_fields", None)):
			self.skipTest("Frappe v15 has no native field-masking feature")
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		receipt = self.weighted_receipt()
		frappe.set_user("Administrator")
		make_property_setter("Purchase Receipt Item", "weight_uom", "mask", 1, "Check")
		frappe.clear_cache(doctype="Purchase Receipt Item")
		frappe.set_user(self.reader)
		with self.assertRaises(PermissionDeniedError):
			self.prepare(receipt)

	def test_small_fractional_weights_are_not_rounded_as_money(self):
		receipt = self.weighted_receipt(weights=(0.004, 0.004), quantities=(1, 1), rates=(10, 10))
		result = self.prepare(receipt, items=[{"name": self.invoice.items[0].name, "amount": 100}])
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual([r["applicable_charges"] for r in result["items"]], [50, 50])

	def test_unequal_fractional_weights_preserve_the_actual_ratio(self):
		receipt = self.weighted_receipt(weights=(0.125, 0.375), quantities=(1, 1), rates=(10, 10))
		result = self.prepare(receipt, items=[{"name": self.invoice.items[0].name, "amount": 100}])
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual([r["applicable_charges"] for r in result["items"]], [25, 75])

	def test_extreme_weight_range_refuses_approximate_native_arithmetic(self):
		receipt = self.weighted_receipt(weights=(10000000000, 0.000000001), quantities=(1, 1), rates=(10, 10))
		result = self.prepare(receipt)
		self.assertEqual(result["status"], "needs_input")
		self.assertEqual(result["issues"][0]["code"], "weight_basis")
		self.assertNotIn("draft_values", result)

	def test_weight_and_amount_groups_request_separate_v15_vouchers(self):
		receipt = self.weighted_receipt()
		frappe.set_user("Administrator")
		other = self.make_invoice(80)
		frappe.set_user(self.reader)
		result = preview_landed_cost_voucher(
			self.company,
			[{"doctype": receipt.doctype, "name": receipt.name}],
			charges=[
				{"purchase_invoice": self.invoice.name, "basis": "Weight"},
				{"purchase_invoice": other.name, "basis": "Amount"},
			],
		)
		self.assertEqual(result["status"], "needs_input", result)
		self.assertEqual(result["issues"][0]["code"], "single_charge_per_manual_voucher")
		self.assertNotIn("draft_values", result)

	def test_unclassified_weight_unit_requires_manual_allocations(self):
		receipt = self.weighted_receipt()
		for row in receipt.items:
			frappe.db.set_value("Purchase Receipt Item", row.name, "weight_uom", "Box")
		result = self.prepare(receipt)
		self.assertEqual(result["status"], "needs_input", result)
		self.assertNotIn("draft_values", result)

	def test_native_rounding_integer_bound_accepts_the_edge_and_refuses_overflow(self):
		receipt = self.weighted_receipt(
			weights=(45035.996273704, 45035.996273704), quantities=(1, 1), rates=(10, 10)
		)
		for weight, status in ((45035.996273704, "ready"), (45035.996273705, "needs_input")):
			with self.subTest(weight=weight):
				for row in receipt.items:
					frappe.db.set_value(
						"Purchase Receipt Item", row.name, {"weight_per_unit": weight, "total_weight": weight}
					)
				result = self.prepare(receipt)
				self.assertEqual(result["status"], status, result)
				if status == "ready":
					self.assertEqual([r["applicable_charges"] for r in result["items"]], [60, 60])
				else:
					self.assertEqual(result["issues"][0]["code"], "weight_basis")
					self.assertNotIn("draft_values", result)

	def test_stock_updating_invoice_uses_its_own_recorded_weights(self):
		receipt = self.weighted_receipt()
		frappe.set_user("Administrator")
		with patch.object(frappe, "in_test", False):
			invoice = frappe.get_doc(
				{
					"doctype": "Purchase Invoice",
					"company": self.company,
					"supplier": self.supplier,
					"update_stock": 1,
					"posting_date": receipt.posting_date,
					"set_posting_time": 1,
					"bill_no": "Weight stock invoice",
					"bill_date": receipt.posting_date,
					"items": [
						{"item_code": r.item_code, "qty": r.qty, "rate": r.rate, "warehouse": r.warehouse}
						for r in receipt.items
					],
				}
			).insert()
			invoice.submit()
		frappe.set_user(self.reader)
		result = self.prepare(invoice)
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual([r["applicable_charges"] for r in result["items"]], [36, 84])
		self.assertTrue(
			all(r["receipt_document_type"] == "Purchase Invoice" for r in result["draft_values"]["items"])
		)

	def test_native_field_precision_keeps_fractional_recorded_weights_consistent(self):
		receipt = self.weighted_receipt(weights=(0.123456789, 0.2), quantities=(2.5, 1), rates=(10, 10))
		recorded = frappe.get_doc(receipt.doctype, receipt.name)
		self.assertEqual(recorded.items[0].weight_per_unit, 0.123)
		self.assertEqual(recorded.items[0].total_weight, 0.308)
		result = self.prepare(recorded, items=[{"name": self.invoice.items[0].name, "amount": 100}])
		self.assertEqual(result["status"], "ready", result)
		self.assertEqual([r["applicable_charges"] for r in result["items"]], [60.63, 39.37])

	def test_disabled_mass_unit_is_an_actionable_gap(self):
		receipt = self.weighted_receipt()
		frappe.db.set_value("UOM", "Kg", "enabled", 0)
		result = self.prepare(receipt)
		self.assertEqual(result["status"], "needs_input")
		self.assertEqual(result["issues"][0]["receipt_item"], receipt.items[0].name)
		self.assertEqual(result["issues"][0]["weight_uom"], "Kg")
		self.assertNotIn("draft_values", result)
