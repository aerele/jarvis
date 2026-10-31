"""LCV receipt summaries must match the values fetched by the ERP form (#787)."""

import copy
from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tests.test_landed_cost_defaults import LandedCostFixtures
from jarvis.tools._client_defaults import landed_cost_receipt_details
from jarvis.tools.create_doc import build_doc, create_doc
from jarvis.tools.preview_doc import preview_doc
from jarvis.tools.preview_landed_cost_voucher import preview_landed_cost_voucher
from jarvis.tools.update_doc import update_doc


class TestLandedCostReceiptDetails(LandedCostFixtures):
	requires_vendor_amounts = False

	def receipt_values(self):
		values = self.values(amounts=[120])
		values["posting_date"] = add_days(nowdate(), 1)
		if not frappe.get_meta("Landed Cost Voucher").has_field("vendor_invoices"):
			values.pop("vendor_invoices")
		return values

	def assert_receipt_summary(self, row, source=None):
		source = source or self.receipt
		voucher = frappe.new_doc("Landed Cost Voucher")
		if callable(getattr(type(voucher), "get_receipt_document_details", None)):
			native = voucher.get_receipt_document_details(source.doctype, source.name)
		else:
			# v15 Desk has three add_fetch declarations instead of a controller method.
			native = {
				"supplier": source.supplier,
				"posting_date": source.posting_date,
				"grand_total": source.base_grand_total,
			}
		for field in ("supplier", "posting_date", "grand_total"):
			with self.subTest(field=field):
				self.assertEqual(str(row.get(field)), str(native.get(field)))

	def test_create_matches_manual_fetch_even_with_blank_or_zero_input(self):
		values = self.receipt_values()
		values["purchase_receipts"][0].update(supplier="", posting_date=None, grand_total=0)
		original = copy.deepcopy(values)
		saved = create_doc("Landed Cost Voucher", values)
		self.assert_receipt_summary(saved["purchase_receipts"][0])
		stored = frappe.get_doc("Landed Cost Voucher", saved["name"])
		self.assert_receipt_summary(stored.purchase_receipts[0])
		self.assertEqual(stored.docstatus, 0)
		self.assertEqual(stored.total_taxes_and_charges, 120)
		# Frappe already adds child doctype tags to input dictionaries. The fetch
		# must not copy source values back into the caller's proposal.
		for table in ("purchase_receipts", "items", "taxes", "vendor_invoices"):
			for row in values.get(table, []):
				row.pop("doctype", None)
		self.assertEqual(values, original)

	def test_prepared_payload_has_receipt_details_without_saving(self):
		before = frappe.db.count("Landed Cost Voucher")
		result = preview_landed_cost_voucher(
			company=self.company,
			receipts=[{"doctype": "Purchase Receipt", "name": self.receipt.name}],
			charges=[{"purchase_invoice": self.invoice.name, "basis": "Amount"}],
		)
		self.assertEqual(result["status"], "ready", result)
		self.assert_receipt_summary(result["draft_values"]["purchase_receipts"][0])
		self.assertEqual(frappe.db.count("Landed Cost Voucher"), before)
		saved = create_doc("Landed Cost Voucher", result["draft_values"])
		self.assert_receipt_summary(saved["purchase_receipts"][0])

	def test_receipt_table_edit_repairs_legacy_summary_and_preserves_row(self):
		legacy = frappe.get_doc({"doctype": "Landed Cost Voucher", **self.receipt_values()}).insert()
		row_name = legacy.purchase_receipts[0].name
		self.assertFalse(legacy.purchase_receipts[0].grand_total)
		items = [(r.name, r.applicable_charges) for r in legacy.items]
		saved = update_doc("Landed Cost Voucher", legacy.name, {"purchase_receipts": [{"name": row_name}]})
		self.assert_receipt_summary(saved["purchase_receipts"][0])
		self.assertEqual(saved["purchase_receipts"][0]["name"], row_name)
		self.assertEqual([(r["name"], r["applicable_charges"]) for r in saved["items"]], items)

	def make_receipt(self, *, rate=50, currency="INR", conversion_rate=1, tax=0, discount=0):
		with patch.dict(frappe.flags, in_test=False):
			frappe.set_user("Administrator")
			receipt = frappe.get_doc(
				{
					"doctype": "Purchase Receipt",
					"company": self.company,
					"supplier": self.supplier,
					"posting_date": nowdate(),
					"set_posting_time": 1,
					"currency": currency,
					"conversion_rate": conversion_rate,
					"apply_discount_on": "Grand Total",
					"additional_discount_percentage": discount,
					"items": [
						{
							"item_code": self.stock,
							"qty": 2,
							"rate": rate,
							"is_free_item": int(rate == 0),
							"warehouse": self.receipt.items[0].warehouse,
							"allow_zero_valuation_rate": 1,
						}
					],
					"taxes": [
						{
							"charge_type": "Actual",
							"account_head": self.expense,
							"description": "Receipt charge",
							"tax_amount": tax,
						}
					]
					if tax
					else [],
				}
			).insert()
			receipt.submit()
		frappe.set_user(self.reader)
		return receipt

	def test_foreign_receipt_uses_company_currency_total_including_tax(self):
		receipt = self.make_receipt(currency="USD", conversion_rate=83.4, tax=12)
		values = self.receipt_values()
		values["purchase_receipts"][0]["receipt_document"] = receipt.name
		values["items"] = []
		saved = create_doc("Landed Cost Voucher", values)
		self.assert_receipt_summary(saved["purchase_receipts"][0], receipt)
		self.assertNotEqual(receipt.base_grand_total, receipt.grand_total)
		self.assertNotEqual(receipt.base_grand_total, receipt.base_net_total)

	def test_zero_total_is_not_treated_as_a_failed_fetch(self):
		receipt = self.make_receipt(discount=100)
		values = self.receipt_values()
		values["purchase_receipts"][0]["receipt_document"] = receipt.name
		values["items"] = []
		# Build isolates reference fetching from the native zero-allocation rule.
		doc = build_doc("Landed Cost Voucher", values)
		self.assert_receipt_summary(doc.purchase_receipts[0], receipt)
		self.assertEqual(doc.purchase_receipts[0].grand_total, 0)

	def test_multiple_receipts_keep_their_own_summary_and_allocation(self):
		other = self.make_receipt(rate=100)
		values = self.receipt_values()
		values["purchase_receipts"].append(
			{"receipt_document_type": "Purchase Receipt", "receipt_document": other.name}
		)
		values["items"] = []
		saved = create_doc("Landed Cost Voucher", values)
		self.assert_receipt_summary(saved["purchase_receipts"][0])
		self.assert_receipt_summary(saved["purchase_receipts"][1], other)
		self.assertEqual([r["applicable_charges"] for r in saved["items"]], [40, 80])

	def test_v15_fetch_fallback_reads_native_pr_and_pi_fields(self):
		controller = type(frappe.new_doc("Landed Cost Voucher"))
		for source in (self.receipt, self.invoice):
			with (
				self.subTest(doctype=source.doctype),
				patch.object(controller, "get_receipt_document_details", None, create=True),
			):
				doc = frappe.new_doc("Landed Cost Voucher")
				doc.append(
					"purchase_receipts",
					{"receipt_document_type": source.doctype, "receipt_document": source.name},
				)
				landed_cost_receipt_details(doc)
				self.assert_receipt_summary(doc.purchase_receipts[0], source)

	def test_unrelated_update_does_not_fetch_new_receipt_details(self):
		legacy = frappe.get_doc({"doctype": "Landed Cost Voucher", **self.receipt_values()}).insert()
		with patch("jarvis.tools._client_defaults.landed_cost_receipt_details") as fetch:
			update_doc("Landed Cost Voucher", legacy.name, {"posting_date": add_days(nowdate(), 2)})
		fetch.assert_not_called()

	def test_submitted_voucher_does_not_refresh_reference_fields(self):
		doc = frappe.new_doc("Landed Cost Voucher")
		doc.docstatus = 1
		row = doc.append(
			"purchase_receipts",
			{
				"receipt_document_type": "Purchase Receipt",
				"receipt_document": self.receipt.name,
				"grand_total": 5,
			},
		)
		with patch("jarvis.tools._client_defaults._require_source_read") as fetch:
			landed_cost_receipt_details(doc)
		fetch.assert_not_called()
		self.assertEqual(row.grand_total, 5)

	def test_record_permission_blocks_fetch_for_create_and_generic_preview(self):
		controller = type(frappe.new_doc("Landed Cost Voucher"))
		original = frappe.has_permission

		def permitted(doctype, *args, **kwargs):
			return False if doctype == "Purchase Receipt" else original(doctype, *args, **kwargs)

		for call in (build_doc, preview_doc):
			with (
				self.subTest(call=call.__name__),
				patch.object(controller, "get_receipt_document_details", create=True) as fetch,
			):
				with (
					patch("frappe.has_permission", side_effect=permitted),
					self.assertRaises(PermissionDeniedError),
				):
					call("Landed Cost Voucher", self.receipt_values())
				fetch.assert_not_called()

	def test_source_summary_field_permissions_are_checked_before_fetch(self):
		controller = type(frappe.new_doc("Landed Cost Voucher"))
		for name in ("supplier", "posting_date", "base_grand_total"):
			field = frappe.get_meta("Purchase Receipt").get_field(name)
			with self.subTest(field=name), patch.object(field, "permlevel", 99):
				with patch.object(controller, "get_receipt_document_details", create=True) as fetch:
					with self.assertRaises(PermissionDeniedError):
						build_doc("Landed Cost Voucher", self.receipt_values())
					fetch.assert_not_called()

	def test_filtered_receipt_reference_never_becomes_a_database_lookup(self):
		values = self.receipt_values()
		for invalid in ({"name": ["like", "%"]}, ["like", "%"]):
			values["purchase_receipts"][0]["receipt_document"] = invalid
			with self.subTest(value=invalid), patch("frappe.db.exists") as lookup:
				with self.assertRaises(InvalidArgumentError):
					build_doc("Landed Cost Voucher", values)
				lookup.assert_not_called()

	def test_incomplete_receipt_rows_remain_available_for_mandatory_field_checks(self):
		for name in (None, "", " "):
			with self.subTest(name=name):
				values = self.receipt_values()
				values["purchase_receipts"][0].update(receipt_document=name, grand_total=999)
				with patch("frappe.db.exists") as lookup:
					doc = build_doc("Landed Cost Voucher", values)
				lookup.assert_not_called()
				self.assertIsNone(doc.purchase_receipts[0].grand_total)
				self.assertEqual(doc.purchase_receipts[0].receipt_document, name)

	def test_collect_mode_reaches_native_validation_for_a_partial_receipt(self):
		from jarvis.chat.held_writes import collect_missing

		values = self.receipt_values()
		values["purchase_receipts"][0].pop("receipt_document")
		controller = type(frappe.new_doc("Landed Cost Voucher"))
		original = controller.validate
		seen = []

		def validate(doc):
			seen.append(doc.purchase_receipts[0].receipt_document)
			original(doc)

		with patch.object(controller, "validate", validate):
			missing = collect_missing(
				"create_doc", [{"doctype": "Landed Cost Voucher", "op": "create", "values": values}]
			)
		self.assertEqual(seen, [None])
		# LCV's native submitted-receipt validation still blocks this incomplete
		# input. The fetch must not replace it with a hard argument refusal.
		self.assertEqual(missing, [])

	def test_missing_native_result_or_deleted_record_fails_without_messages(self):
		controller = type(frappe.new_doc("Landed Cost Voucher"))

		def deleted(*args, **kwargs):
			frappe.msgprint("private source message")
			raise frappe.DoesNotExistError("private source message")

		for outcome in (None, deleted):
			with self.subTest(outcome=outcome):
				before = list(frappe.local.message_log or [])
				with patch.object(
					controller,
					"get_receipt_document_details",
					side_effect=outcome if callable(outcome) else None,
					return_value=None,
					create=True,
				):
					with self.assertRaises(PermissionDeniedError):
						build_doc("Landed Cost Voucher", self.receipt_values())
				self.assertEqual(frappe.local.message_log or [], before)

	def test_native_fetch_timeout_is_not_replaced_with_zero_or_fallback(self):
		controller = type(frappe.new_doc("Landed Cost Voucher"))
		with patch.object(
			controller, "get_receipt_document_details", side_effect=frappe.QueryTimeoutError, create=True
		):
			with self.assertRaises(frappe.QueryTimeoutError):
				build_doc("Landed Cost Voucher", self.receipt_values())

	def test_batch_create_rolls_back_when_a_receipt_cannot_be_read(self):
		before = frappe.db.count("Landed Cost Voucher")
		bad = self.receipt_values()
		bad["purchase_receipts"][0]["receipt_document"] = "_JLCV missing receipt"
		with self.assertRaises(PermissionDeniedError):
			create_doc(
				docs=[
					{"doctype": "Landed Cost Voucher", "values": self.receipt_values()},
					{"doctype": "Landed Cost Voucher", "values": bad},
				]
			)
		self.assertEqual(frappe.db.count("Landed Cost Voucher"), before)

	def test_batch_update_rolls_back_earlier_summary_refresh(self):
		legacy = frappe.get_doc({"doctype": "Landed Cost Voucher", **self.receipt_values()}).insert()
		row = legacy.purchase_receipts[0]
		with self.assertRaises(PermissionDeniedError):
			update_doc(
				"Landed Cost Voucher",
				updates=[
					{"name": legacy.name, "changes": {"purchase_receipts": [{"name": row.name}]}},
					{
						"name": legacy.name,
						"changes": {
							"purchase_receipts": [
								{"name": row.name, "receipt_document": "_JLCV missing receipt"}
							]
						},
					},
				],
			)
		self.assertFalse(legacy.reload().purchase_receipts[0].grand_total)

	def test_changing_a_receipt_link_refreshes_the_summary(self):
		saved = create_doc("Landed Cost Voucher", self.receipt_values())
		other = self.make_receipt(rate=150)
		row = saved["purchase_receipts"][0]
		updated = update_doc(
			"Landed Cost Voucher",
			saved["name"],
			{
				"purchase_receipts": [
					{"name": row["name"], "receipt_document": other.name, "grand_total": row["grand_total"]}
				],
				"items": [
					{
						"name": r["name"],
						"receipt_document": other.name,
						"purchase_receipt_item": other.items[0].name,
						"rate": other.items[0].base_rate,
						"amount": other.items[0].base_amount,
					}
					for r in saved["items"]
				],
			},
		)
		self.assert_receipt_summary(updated["purchase_receipts"][0], other)
		self.assertEqual(updated["purchase_receipts"][0]["name"], row["name"])

	def test_stock_updating_purchase_invoice_gets_its_native_summary(self):
		with patch.dict(frappe.flags, in_test=False):
			frappe.set_user("Administrator")
			invoice = frappe.get_doc(
				{
					"doctype": "Purchase Invoice",
					"company": self.company,
					"supplier": self.supplier,
					"posting_date": nowdate(),
					"due_date": nowdate(),
					"set_posting_time": 1,
					"update_stock": 1,
					"bill_no": frappe.generate_hash(length=10),
					"items": [
						{
							"item_code": self.stock,
							"qty": 2,
							"rate": 50,
							"warehouse": self.receipt.items[0].warehouse,
						}
					],
				}
			).insert()
			invoice.submit()
		frappe.set_user(self.reader)
		values = self.receipt_values()
		values["purchase_receipts"] = [
			{"receipt_document_type": "Purchase Invoice", "receipt_document": invoice.name}
		]
		values["items"] = []
		saved = create_doc("Landed Cost Voucher", values)
		self.assert_receipt_summary(saved["purchase_receipts"][0], invoice)

	def test_native_extended_receipt_types_use_their_own_amount_fields(self):
		controller = type(frappe.new_doc("Landed Cost Voucher"))
		if not callable(getattr(controller, "get_receipt_document_details", None)):
			self.skipTest("These receipt types require the newer native interface")
		for doctype, field in (("Stock Entry", "total_incoming_value"), ("Subcontracting Receipt", "total")):
			with self.subTest(doctype=doctype):
				# Header-only fixtures test the native reference fetch, not stock posting.
				frappe.set_user("Administrator")
				source = frappe.new_doc(doctype)
				source.name = "_JLCV787 " + frappe.generate_hash(length=10)
				source.company = self.company
				source.posting_date = nowdate()
				source.supplier = self.supplier
				source.set(field, 321)
				source.db_insert()
				frappe.set_user(self.reader)
				doc = frappe.new_doc("Landed Cost Voucher")
				row = doc.append(
					"purchase_receipts",
					{
						"receipt_document_type": doctype,
						"receipt_document": source.name,
						"supplier": self.supplier,
						"posting_date": "2001-01-01",
						"grand_total": 999,
					},
				)
				landed_cost_receipt_details(doc)
				self.assertEqual(row.grand_total, 321)
				if doctype == "Stock Entry":
					self.assertIsNone(row.supplier)
					self.assertIsNone(row.posting_date)
				else:
					self.assertEqual(row.supplier, self.supplier)
				# An older/custom interface with no native fetch must not invent values.
				with patch.object(controller, "get_receipt_document_details", None):
					landed_cost_receipt_details(doc)
				self.assertIsNone(row.grand_total)
				self.assertIsNone(row.supplier)
				self.assertIsNone(row.posting_date)

	def test_real_supplier_user_permission_blocks_receipt_fetch(self):
		frappe.set_user("Administrator")
		other = frappe.copy_doc(frappe.get_doc("Supplier", self.supplier))
		other.supplier_name = "_JLCV787 Allowed Supplier"
		other.insert()
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": self.reader,
				"allow": "Supplier",
				"for_value": other.name,
				"apply_to_all_doctypes": 0,
				"applicable_for": "Purchase Receipt",
			}
		).insert()
		frappe.clear_cache(user=self.reader)
		frappe.set_user(self.reader)
		self.assertFalse(frappe.has_permission("Purchase Receipt", "read", doc=self.receipt.name))
		with self.assertRaises(PermissionDeniedError):
			build_doc("Landed Cost Voucher", self.receipt_values())

	def test_source_mask_blocks_generic_and_source_backed_previews(self):
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		if not hasattr(frappe.get_meta("Purchase Receipt"), "get_masked_fields"):
			self.skipTest("This framework does not support field masking")
		frappe.set_user("Administrator")
		make_property_setter("Purchase Receipt", "supplier", "mask", 1, "Check")
		frappe.clear_cache(doctype="Purchase Receipt")
		frappe.set_user(self.reader)
		self.assertIn(
			"supplier", [f.fieldname for f in frappe.get_meta("Purchase Receipt").get_masked_fields()]
		)
		with self.assertRaises(PermissionDeniedError):
			preview_doc("Landed Cost Voucher", self.receipt_values())
		with self.assertRaises(PermissionDeniedError):
			preview_landed_cost_voucher(
				company=self.company,
				receipts=[{"doctype": "Purchase Receipt", "name": self.receipt.name}],
				charges=[{"purchase_invoice": self.invoice.name, "basis": "Amount"}],
			)

	def test_generic_preview_runs_the_same_fetch_then_rolls_back(self):
		controller = type(frappe.new_doc("Landed Cost Voucher"))
		original = controller.validate
		seen = []

		def validate(doc):
			seen.append(doc.purchase_receipts[0].as_dict())
			original(doc)

		before = frappe.db.count("Landed Cost Voucher")
		with patch.object(controller, "validate", validate):
			result = preview_doc("Landed Cost Voucher", self.receipt_values())
		self.assertTrue(result["valid"], result)
		self.assert_receipt_summary(seen[0])
		self.assertEqual(frappe.db.count("Landed Cost Voucher"), before)
