"""Real gzip, SQLite and whole-source report tests, without a site connection."""

import gzip
import json
import unittest
from unittest.mock import patch

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _report_query_data as data


def source(rows=6000, *, total=True):
	columns = [
		{"fieldname": "party_type", "label": "Party Type", "fieldtype": "Link", "options": "DocType"},
		{"fieldname": "party", "label": "Customer", "fieldtype": "Dynamic Link", "options": "party_type"},
		{"fieldname": "outstanding", "label": "Outstanding", "fieldtype": "Currency"},
		{"fieldname": "currency", "label": "Currency", "fieldtype": "Link", "options": "Currency"},
		{"fieldname": "private", "label": "Private", "fieldtype": "Data", "hidden": 1},
	]
	values = [
		{
			"party_type": "Customer",
			"party": f"CUST-{i:05}",
			"outstanding": "0.10",
			"currency": "INR",
			"private": "NEVER-EXPOSE",
		}
		for i in range(1, rows + 1)
	]
	if total:
		values.append(["Total", "", f"{rows / 10:.2f}", "INR", ""])
	return gzip.compress(json.dumps({"columns": columns, "result": values, "skip_total_row": 0}).encode())


class TestReportQueryData(unittest.TestCase):
	def query(self, content, sql=None, *, context=None, total=True, max_rows=100):
		schema, rows, total_index = data.decode_report(content, add_total_row=total)
		context = context or {}
		db, manifest, errors, blocked, proof = data.build_tables(schema, rows, total_index, context)
		try:
			return data.query_report_tables(
				db, manifest, schema, context, sql=sql, max_rows=max_rows, errors=errors, blocked=blocked
			), proof
		finally:
			db.close()

	def test_reads_all_6000_detail_rows_and_separates_total(self):
		result, _ = self.query(source(), "SELECT COUNT(*), decimal_sum(c3), max(_row) FROM report_rows")
		self.assertEqual(result["rows"], [[6000, "600.00", 6000]])
		self.assertEqual(result["source_rows"], 6001)
		self.assertEqual(result["data_rows"], 6000)
		self.assertEqual(result["total_rows"], 1)
		self.assertTrue(result["coverage"]["complete"])
		inspection, _ = self.query(source(1))
		self.assertEqual(
			len(inspection["tables"][0]["sample"][0]), len(inspection["tables"][0]["columns"]) + 1
		)
		self.assertEqual(inspection["tables"][0]["sample"][0][1], "data")
		tail, _ = self.query(source(), "SELECT c2 FROM report_rows WHERE _row = 6000")
		self.assertEqual(tail["rows"], [["CUST-06000"]])
		totals, _ = self.query(source(), "SELECT _row, _kind, c3, c4 FROM report_totals")
		self.assertEqual(totals["rows"], [[6001, "total", "600.00", None]])

	def test_proofs_are_complete_even_for_filtered_and_limited_answers(self):
		result, proof = self.query(
			source(1500), "SELECT c2 FROM report_rows WHERE _row = 1500", context={"proof_fields": [0, 1]}
		)
		self.assertEqual(len(proof["rows"]), 1500)
		self.assertEqual(proof["rows"][-1], ["Customer", "CUST-01500"])
		self.assertEqual(result["rows"], [["CUST-01500"]])
		result, _ = self.query(source(1500), "SELECT c2 FROM report_rows ORDER BY _row", max_rows=2)
		self.assertTrue(result["result_truncated"])
		self.assertEqual(result["source_rows"], 1501)

	def test_exact_numeric_filter_and_comparison_latch(self):
		content = gzip.compress(
			b'{"columns":[{"fieldname":"amount","label":"Amount","fieldtype":"Currency"}],"result":[[99.999999999999999999],[100],[100.000000000000000001]],"skip_total_row":1}'
		)
		result, _ = self.query(
			content, "SELECT _row, c1 FROM report_rows WHERE c1 > 100 ORDER BY c1", total=False
		)
		self.assertEqual(result["rows"], [[3, "100.000000000000000001"]])
		result, _ = self.query(
			content,
			"SELECT decimal_sum(c1), decimal_cmp('100.000000000000000001', '100') FROM report_rows",
			total=False,
		)
		self.assertEqual(result["rows"], [["300.000000000000000000", 1]])
		for sql in (
			"SELECT * FROM report_rows WHERE c1 = 'abc'",
			"SELECT * FROM report_rows WHERE c1 < 'NaN'",
		):
			with self.subTest(sql=sql), self.assertRaisesRegex(InvalidArgumentError, "comparison"):
				self.query(content, sql, total=False)

	def test_masks_never_enter_query_table_and_no_oracle(self):
		context = {"masked_columns": ["c3"]}
		for sql in (
			"SELECT c3 FROM report_rows",
			"SELECT COUNT(*) FROM report_rows WHERE c3 > 100",
			"SELECT c2 FROM report_rows ORDER BY c3",
			"SELECT decimal_sum(c3) FROM report_totals",
		):
			with self.subTest(sql=sql), self.assertRaises(InvalidArgumentError):
				self.query(source(3), sql, context=context)
		result, _ = self.query(source(3), context=context)
		self.assertEqual(result["tables"][0]["sample"][0][4], "XXXXXXXX")
		self.assertNotIn("NEVER-EXPOSE", json.dumps(result))
		with self.assertRaises(InvalidArgumentError):
			self.query(source(3), "SELECT c5 FROM report_rows")

	def test_empty_legacy_and_invalid_shapes(self):
		result, _ = self.query(source(0, total=False), "SELECT COUNT(*) FROM report_rows", total=False)
		self.assertEqual(result["rows"], [[0]])
		schema, rows, index = data.decode_report(
			gzip.compress(
				b'{"columns":["Party:Data:120","Amount:Currency:100"],"result":[["A",0.1],["B",0.2]],"skip_total_row":1}'
			)
		)
		self.assertEqual(schema["source_rows"], 2)
		self.assertEqual(rows[0][1], "0.1")
		self.assertIsNone(index)
		with self.assertRaisesRegex(InvalidArgumentError, "ambiguous"):
			data.decode_report(
				gzip.compress(b'[["A",0.1],["B",0.2]]'),
				legacy_columns=["Party:Data:120", "Amount:Currency:100"],
			)
		for body in (b'{"columns":[],"result":[1]}', b'{"columns":[],"result":{}}', b"null"):
			with self.subTest(body=body), self.assertRaises(InvalidArgumentError):
				data.decode_report(gzip.compress(body))

	def test_corrupt_bombs_limits_and_no_partial_totals(self):
		for content in (b"bad gzip", source(3)[:-4], source(3) + b"extra"):
			with self.subTest(content=content[:8]), self.assertRaises(InvalidArgumentError):
				data.decode_report(content)
		with (
			patch.object(data, "MAX_EXPANDED", 100),
			self.assertRaisesRegex(InvalidArgumentError, "expanded"),
		):
			data.decode_report(source(30))
		with (
			patch.object(data, "MAX_PROOFS", 30),
			self.assertRaisesRegex(InvalidArgumentError, "proof budget"),
		):
			self.query(source(5), context={"proof_fields": [1]})
		with patch.object(data, "MAX_TOTAL_ROWS", 4), self.assertRaisesRegex(InvalidArgumentError, "rows"):
			self.query(source(5))

	def test_total_links_and_ambiguous_layouts(self):
		body = json.loads(gzip.decompress(source(2)))
		body["columns"].append({"fieldname": "customer", "fieldtype": "Link", "options": "Customer"})
		body["result"][-1].append("FOOTER-CUSTOMER")
		_, proof = self.query(gzip.compress(json.dumps(body).encode()), context={"proof_fields": [5]})
		self.assertIn(["FOOTER-CUSTOMER"], proof["rows"])
		body["result"][-1] = {"outstanding": "0.20"}
		result, _ = self.query(gzip.compress(json.dumps(body).encode()))
		self.assertEqual(result["data_rows"], 3)
		self.assertEqual(result["total_rows"], 0)
		body = json.loads(gzip.decompress(source(2)))
		body["result"][0]["party_type"] = ["Customer"]
		with self.assertRaisesRegex(InvalidArgumentError, "Dynamic Link"):
			self.query(gzip.compress(json.dumps(body).encode()))

	def test_stored_footer_ignores_current_report_total_setting(self):
		result, _ = self.query(source(2), "SELECT COUNT(*), decimal_sum(c3) FROM report_rows", total=False)
		self.assertEqual(result["rows"], [[2, "0.20"]])
		# A report generated without a footer remains all detail after enabling totals.
		result, proof = self.query(
			source(2, total=False),
			"SELECT COUNT(*) FROM report_rows",
			total=True,
			context={"proof_fields": [0, 1]},
		)
		self.assertEqual(result["rows"], [[2]])
		self.assertEqual(len(proof["rows"]), 2)
		self.assertEqual(proof["rows"][-1][1], "CUST-00002")

	def test_native_float_residuals_and_blank_numeric_cells(self):
		content = gzip.compress(
			b'{"columns":[{"fieldname":"amount","fieldtype":"Currency"}],"result":[{"amount":5.551115123125783e-17},{"amount":""},{"amount":null},{"amount":0.10}],"skip_total_row":1}'
		)
		result, _ = self.query(
			content,
			"SELECT decimal_sum(c1), count(c1), decimal_cmp(decimal_sum(c1), '0.10000000000000005551115123125783') FROM report_rows",
			total=False,
		)
		self.assertEqual(result["rows"], [["0.10000000000000005551115123125783", 2, 0]])
		result, _ = self.query(
			content, "SELECT _row FROM report_rows WHERE c1 > '0' ORDER BY c1", total=False
		)
		self.assertEqual(result["rows"], [[1], [4]])
		result, _ = self.query(
			content, "SELECT _row FROM report_rows WHERE c1 IS NULL ORDER BY _row", total=False
		)
		self.assertEqual(result["rows"], [[2], [3]])

	def test_currency_groups_credit_notes_and_repeated_party(self):
		body = json.loads(gzip.decompress(source(3)))
		for row, party, amount, currency in zip(
			body["result"][:-1],
			["REPEAT", "REPEAT", "OTHER"],
			["100.25", "-50.50", "7.00"],
			["INR", "INR", "USD"],
			strict=True,
		):
			row.update(party=party, outstanding=amount, currency=currency)
		content = gzip.compress(json.dumps(body).encode())
		result, _ = self.query(
			content,
			"SELECT c4, COUNT(*), COUNT(DISTINCT c2), decimal_sum(c3) FROM report_rows GROUP BY c4 ORDER BY c4",
		)
		self.assertEqual(result["rows"], [["INR", 2, 1, "49.75"], ["USD", 1, 1, "7.00"]])

	def test_sql_sandbox_is_shared(self):
		for sql in (
			"DELETE FROM report_rows",
			"ATTACH '/tmp/forbidden.db' AS other",
			"PRAGMA database_list",
			"SELECT * FROM sqlite_master",
			"SELECT load_extension('bad')",
			"SELECT sum(c3) FROM report_rows",
			"SELECT 1; SELECT 2",
		):
			with self.subTest(sql=sql), self.assertRaises(InvalidArgumentError):
				self.query(source(2), sql)
