"""Core fact handling for the server-owned Stock Variance evaluator."""

from unittest import TestCase
from unittest.mock import patch

from jarvis.chat import stock_variance_native as native

COMPANY = "Stock Variance Test Co"
WAREHOUSE = "Stock Variance Test Stores"
SCOPE = {
	"company": COMPANY,
	"record_scope": {"doctype": "Warehouse", "name": WAREHOUSE},
	"from_date": "2026-10-01",
	"to_date": "2026-10-31",
	"report_date": "2026-10-31",
}
CONFIG = {"stock_variance": {"reviewer": "reviewer@example.invalid"}}


def _count(name, counted_at, *, modified=None, status=1):
	return {
		"name": name,
		"company": COMPANY,
		"warehouse": WAREHOUSE,
		"docstatus": status,
		"counted_at": counted_at,
		"creation": "2026-10-01 08:00:00",
		"modified": modified or counted_at,
	}


def _line(name, parent, item, quantity, parent_modified):
	return {
		"name": name,
		"parent": parent,
		"parenttype": "Jarvis Physical Count",
		"parentfield": "items",
		"idx": 1,
		"item_code": item,
		"counted_qty": quantity,
		"stock_uom": "Nos",
		"batch_detail_recorded": 0,
		"creation": "2026-10-01 08:00:00",
		"modified": parent_modified,
		"parent_modified": parent_modified,
	}


def _item(name):
	return {
		"name": name,
		"is_stock_item": 1,
		"has_variants": 0,
		"variant_of": "",
		"has_batch_no": 0,
		"has_serial_no": 0,
		"stock_uom": "Nos",
		"disabled": 0,
		"creation": "2026-10-01 08:00:00",
		"modified": "2026-10-01 08:00:00",
	}


def _sle(name, item, moment, qty, rate):
	date, clock = moment.split(" ", 1)
	return {
		"name": name,
		"company": COMPANY,
		"item_code": item,
		"warehouse": WAREHOUSE,
		"posting_date": date,
		"posting_time": clock,
		"posting_datetime": moment,
		"qty_after_transaction": qty,
		"valuation_rate": rate,
		"stock_value": qty * rate,
		"stock_value_difference": 0,
		"actual_qty": 0,
		"voucher_type": "Stock Entry",
		"voucher_no": f"SE-{name}",
		"docstatus": 1,
		"is_cancelled": 0,
		"is_adjustment_entry": 0,
		"creation": moment,
		"modified": moment,
	}


def _rows():
	modified = "2026-10-10 12:00:00"
	warehouse = {
		"name": WAREHOUSE,
		"company": COMPANY,
		"parent_warehouse": "",
		"is_group": 0,
		"disabled": 0,
		"lft": 1,
		"rgt": 2,
		"creation": "2026-10-01 08:00:00",
		"modified": "2026-10-01 08:00:00",
	}
	rows = {key: [] for key in native.FIELDS}
	rows.update(
		company=[
			{
				"name": COMPANY,
				"default_currency": "INR",
				"is_group": 0,
				"creation": "2026-10-01 08:00:00",
				"modified": "2026-10-01 08:00:00",
			}
		],
		warehouse_root=[warehouse],
		warehouses=[warehouse],
		counts=[_count("PC-COUNT", "2026-10-10 10:00:00", modified=modified)],
		count_lines=[_line("PCL-COUNT", "PC-COUNT", "SKU-1", 4, modified)],
		items=[_item("SKU-1")],
		sle_entries=[
			_sle("SLE-BEFORE", "SKU-1", "2026-10-10 09:00:00", 10, 20),
			_sle("SLE-AFTER", "SKU-1", "2026-10-10 11:00:00", 4, 30),
		],
	)
	return rows


def _evaluate(rows):
	with patch.object(native, "check_sources", return_value=rows):
		return native.evaluate_native(SCOPE, CONFIG, {"source_receipts": []})


class TestStockVarianceNative(TestCase):
	def test_uses_book_quantity_and_rate_at_the_saved_count_time(self):
		result = _evaluate(_rows())

		self.assertEqual(result["run_state"], "evaluated_clean")
		line = result["findings"][0]["lines"][0]
		# The post-count SLE would match the observed quantity. It must not be used.
		self.assertEqual(line["system_qty"], 10.0)
		self.assertEqual(line["book_rate"], 20.0)
		self.assertEqual(line["variance_qty"], -6.0)
		self.assertEqual(line["amount"], 120.0)

	def test_submitted_correction_is_evaluated_without_a_cancelled_predecessor(self):
		rows = _rows()
		modified = "2026-10-10 12:00:00"
		rows["counts"] = [_count("PC-CORRECTED", "2026-10-10 10:00:00", modified=modified)]
		rows["count_lines"] = [_line("PCL-CORRECTED", "PC-CORRECTED", "SKU-1", 4, modified)]

		result = _evaluate(rows)
		header_where = native.query_specs(SCOPE, CONFIG)["counts"]["where"]

		self.assertIn({"field": "r.docstatus", "op": "=", "value": 1}, header_where)
		self.assertEqual([row["name"] for row in result["count_results"]], ["PC-CORRECTED"])
		self.assertEqual([row["ref_name"] for row in result["findings"]], ["PC-CORRECTED"])

	def test_a_cancelled_predecessor_fails_closed_if_it_reaches_the_evaluator(self):
		rows = _rows()
		rows["counts"].append(_count("PC-CANCELLED", "2026-10-10 10:00:00", status=2))

		result = _evaluate(rows)

		self.assertEqual(result["run_state"], "not_evaluable")
		self.assertEqual(result["findings"], [])
		self.assertEqual(result["coverage"][native.TOKEN]["reason_code"], "record_coverage_insufficient")

	def test_source_stale_and_permission_loss_are_not_clean(self):
		for reason in ("source_stale", "permission_slice"):
			with self.subTest(reason=reason):
				with patch.object(
					native, "check_sources", side_effect=native.Gap("source unavailable", reason)
				):
					result = native.evaluate_native(SCOPE, CONFIG, {"source_receipts": []})

				self.assertEqual(result["run_state"], "not_evaluable")
				self.assertEqual(result["findings"], [])
				self.assertEqual(result["coverage"][native.TOKEN]["reason_code"], reason)

	def test_an_unsettled_repost_keeps_the_completed_subset_partial(self):
		rows = _rows()
		clean_modified = "2026-10-11 12:00:00"
		rows["counts"].append(_count("PC-CLEAN", "2026-10-11 10:00:00", modified=clean_modified))
		rows["count_lines"].append(_line("PCL-CLEAN", "PC-CLEAN", "SKU-2", 10, clean_modified))
		rows["items"].append(_item("SKU-2"))
		rows["sle_entries"].append(_sle("SLE-CLEAN", "SKU-2", "2026-10-11 09:00:00", 10, 20))
		rows["repost_queue"] = [
			{
				"name": "RIV-COUNT",
				"company": COMPANY,
				"based_on": "Item and Warehouse",
				"item_code": "SKU-1",
				"warehouse": WAREHOUSE,
				"status": "Queued",
				"docstatus": 1,
				"voucher_type": "",
				"voucher_no": "",
				"posting_date": "2026-10-10",
				"posting_time": "08:00:00",
				"repost_only_accounting_ledgers": 0,
				"creation": "2026-10-10 08:00:00",
				"modified": "2026-10-10 08:00:00",
			}
		]

		result = _evaluate(rows)

		self.assertEqual(result["run_state"], "partial")
		self.assertEqual(result["coverage"][native.TOKEN]["state"], "evaluated")
		self.assertEqual(result["incomplete"]["reason_code"], "source_stale")
		self.assertEqual([row["name"] for row in result["count_results"]], ["PC-CLEAN"])
		self.assertEqual([row["name"] for row in result["excluded_counts"]], ["PC-COUNT"])

	def test_no_stock_reconciliation_source_is_declared(self):
		self.assertNotIn("Stock Reconciliation", {doctype for doctype, _fields in native.FIELDS.values()})
		self.assertNotIn("Stock Reconciliation", native.RELEVANT_DIMENSION_DOCTYPES)
