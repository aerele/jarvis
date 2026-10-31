"""Execute the legacy claim SQL on native fixture rows without India Compliance."""

import unittest

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

from jarvis.tools import _lcv_legacy as legacy


class TestLegacyClaimSql(unittest.TestCase):
	def test_native_sum_exclusions_lock_and_invalid_claims(self):
		if not str(frappe.local.site).startswith(("test_", "test.", "jarvis-ci.")):
			self.skipTest("Requires a connected disposable site")
		field_name = f"{legacy.TAX_DOCTYPE}-{legacy.SOURCE_FIELD}"
		created_field = not frappe.db.exists("Custom Field", field_name)
		parents = []
		try:
			if created_field:
				create_custom_field(
					legacy.TAX_DOCTYPE,
					{
						"fieldname": legacy.SOURCE_FIELD,
						"label": "Legacy Test Source",
						"fieldtype": "Link",
						"options": "Purchase Invoice",
					},
				)
			frappe.clear_cache(doctype=legacy.TAX_DOCTYPE)
			invoice = "LEGACY-SQL-" + frappe.generate_hash(length=12)

			def parent(status, amount, native=False):
				name = "LEGACY-SQL-" + frappe.generate_hash(length=12)
				# Parent-only fixtures exercise the stored query, not accounting submission.
				frappe.get_doc(
					{"doctype": "Landed Cost Voucher", "name": name, "docstatus": status}
				).db_insert()
				parents.append(name)
				frappe.get_doc(
					{
						"doctype": legacy.TAX_DOCTYPE,
						"name": frappe.generate_hash(length=12),
						"parent": name,
						"parenttype": "Landed Cost Voucher",
						"parentfield": "taxes",
						"base_amount": amount,
						legacy.SOURCE_FIELD: invoice,
					}
				).db_insert()
				if native:
					frappe.get_doc(
						{
							"doctype": "Landed Cost Vendor Invoice",
							"name": frappe.generate_hash(length=12),
							"parent": name,
							"parenttype": "Landed Cost Voucher",
							"parentfield": "vendor_invoices",
							"vendor_invoice": invoice,
							"amount": amount,
							"docstatus": status,
						}
					).db_insert()
				return name

			counted = parent(1, 30)
			parent(0, 900)
			parent(2, 900)
			parent(1, 900, native=True)
			self.assertEqual(legacy.claimed_cost(invoice, None), 30)
			self.assertEqual(legacy.claimed_cost(invoice, None, lock=True), 30)
			self.assertEqual(legacy.claimed_cost(invoice, counted), 0)
			parent(1, -2)
			self.assertIsNone(legacy.claimed_cost(invoice, None))
		finally:
			for doctype, field in [
				(legacy.TAX_DOCTYPE, "parent"),
				("Landed Cost Vendor Invoice", "parent"),
				("Landed Cost Voucher", "name"),
			]:
				if parents:
					frappe.db.delete(doctype, {field: ["in", parents]})
			if created_field and frappe.db.exists("Custom Field", field_name):
				frappe.delete_doc("Custom Field", field_name, force=True, ignore_permissions=True)
			frappe.db.commit()
