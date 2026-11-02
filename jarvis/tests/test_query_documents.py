"""Document queries exercise real parsers, source provenance and complete-source SQL."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._spreadsheet_process import run_query
from jarvis.tools._spreadsheet_query import query_spreadsheet


class TestDocumentQuery(unittest.TestCase):
	def query(self, content, filename, sql=None, **kwargs):
		if isinstance(content, str):
			content = content.encode()
		return query_spreadsheet(content, filename, sql=sql, **kwargs)

	def test_json_nested_records_keep_array_boundaries_and_exact_numbers(self):
		content = (
			'{"payments":[{"supplier":"A","amount":0.10,"items":[{"amount":999}]},'
			'{"supplier":"A","amount":0.20},{"supplier":"B","amount":-1.25}],"amount":10000}'
		)
		result = self.query(
			content,
			"payments.json",
			"SELECT supplier.value, COUNT(*), decimal_sum(amount.value) "
			"FROM json_nodes record "
			"JOIN json_nodes supplier ON supplier.parent = record.path AND supplier.key = 'supplier' "
			"JOIN json_nodes amount ON amount.parent = record.path AND amount.key = 'amount' "
			"WHERE record.parent = '/payments' AND record.kind = 'object' GROUP BY supplier.value ORDER BY 1",
		)
		self.assertEqual(result["rows"], [["A", 2, "0.30"], ["B", 1, "-1.25"]])
		self.assertEqual(result["read_contract"], "document-query-v1")
		self.assertEqual(result["source_unit"], "JSON nodes")
		self.assertTrue(result["coverage"]["complete"])

	def test_json_types_empty_containers_pointers_and_large_numbers(self):
		result = self.query(
			'{"a/b~c":[null,"",true,false,[],{},9007199254740993,1.2300,1e2,"001"]}',
			"types.json",
			"SELECT path, parent, key, kind, value, depth FROM json_nodes ORDER BY _row",
		)
		rows = result["rows"]
		self.assertEqual(rows[0], ["", None, None, "object", None, 0])
		self.assertEqual(rows[1], ["/a~1b~0c", "", "a/b~c", "array", None, 1])
		self.assertEqual(
			[row[3:5] for row in rows[2:]],
			[
				["null", None],
				["string", ""],
				["boolean", "true"],
				["boolean", "false"],
				["array", None],
				["object", None],
				["number", "9007199254740993"],
				["number", "1.2300"],
				["number", "1e2"],
				["string", "001"],
			],
		)
		self.assertEqual(rows[-1][0], "/a~1b~0c/9")

	def test_json_scalar_roots_and_empty_containers(self):
		for value, kind, scalar in [
			("[]", "array", None),
			("{}", "object", None),
			("null", "null", None),
			('"text"', "string", "text"),
			("12.00", "number", "12.00"),
		]:
			with self.subTest(value=value):
				result = self.query(value, "root.json", "SELECT path, kind, value FROM json_nodes")
				self.assertEqual(result["rows"], [["", kind, scalar]])
				self.assertEqual(result["source_rows"], 1)

	def test_json_invalid_data_never_returns_partial_totals(self):
		for value, message in [
			('{"a":1,"a":2}', "duplicate"),
			('{"a":NaN}', "NaN"),
			("[Infinity]", "Infinity"),
			('{"a":', "malformed"),
			("[1,]", "malformed"),
			("[" * 66 + "0" + "]" * 66, "depth"),
			('"\\ud800"', "Unicode"),
		]:
			with self.subTest(value=value), self.assertRaisesRegex(InvalidArgumentError, message):
				self.query(value, "bad.json", "SELECT COUNT(*) FROM json_nodes")

	def test_json_source_limit_counts_nodes_and_display_limit_only_counts_results(self):
		result = self.query(
			"[1,2,3,4]", "values.json", "SELECT path FROM json_nodes ORDER BY _row", max_rows=2
		)
		self.assertEqual(result["source_rows"], 5)
		self.assertEqual(result["rows"], [[""], ["/0"]])
		self.assertTrue(result["result_truncated"])
		self.assertTrue(result["coverage"]["complete"])
		with patch("jarvis.tools._spreadsheet_query.MAX_TOTAL_ROWS", 4):
			with self.assertRaisesRegex(InvalidArgumentError, "rows"):
				self.query("[1,2,3,4]", "values.json", "SELECT COUNT(*) FROM json_nodes")

	def test_text_tail_search_and_exact_line_endings(self):
		content = "first\r\nsecond\rthird\n" + "".join(f"line-{n}\n" for n in range(4, 1501)) + "TAIL Ω"
		result = self.query(
			content, "notes.txt", "SELECT _row, text FROM text_lines WHERE text LIKE '%TAIL%'"
		)
		self.assertEqual(result["rows"], [[1501, "TAIL Ω"]])
		self.assertEqual(result["source_rows"], 1501)
		result = self.query(
			"first\r\nsecond\rthird\nlast", "notes.txt", "SELECT text, ending FROM text_lines ORDER BY _row"
		)
		self.assertEqual(
			"".join(text + ending for text, ending in result["rows"]), "first\r\nsecond\rthird\nlast"
		)

	def test_empty_text_and_unicode_separators(self):
		result = self.query("", "empty.txt", "SELECT COUNT(*) FROM text_lines")
		self.assertEqual(result["rows"], [[0]])
		self.assertTrue(result["coverage"]["complete"])
		result = self.query("a\u2028b\n", "unicode.txt", "SELECT text, ending FROM text_lines ORDER BY _row")
		self.assertEqual(result["rows"], [["a\u2028b", "\n"]])

	def test_markdown_table_full_total_header_and_line_provenance(self):
		content = "# Payments\n\n| Reference | Amount |\n| --- | ---: |\n" + "".join(
			f"| INV-{i} | 0.10 |\n" for i in range(1, 1501)
		)
		result = self.query(
			content, "report.md", "SELECT COUNT(*), decimal_sum(B), max(_row) FROM md_table1 WHERE _row > 3"
		)
		self.assertEqual(result["rows"], [[1500, "150.00", 1504]])
		self.assertEqual(result["source_rows"], 3005)
		tail = self.query(content, "report.md", "SELECT _row, A FROM md_table1 WHERE A = 'INV-1500'")
		self.assertEqual(tail["rows"], [[1504, "INV-1500"]])

	def test_markdown_escaped_pipes_multiple_tables_and_code_fences(self):
		content = (
			"# Bank\n\n| Name | Amount |\n| --- | --- |\n| A\\|B | 0.20 |\n\n"
			"```text\n# Fake heading\n| Ignore | Amount |\n| --- | --- |\n| X | 999 |\n```\n\n"
			"## Fees\n\nFee | Amount\n--- | ---\nCharge | (0.10)\n"
		)
		result = self.query(content, "report.markdown")
		self.assertEqual([s["table"] for s in result["sheets"]], ["text_lines", "md_table1", "md_table2"])
		result = self.query(content, "report.markdown", "SELECT A, B FROM md_table1 WHERE _row > 3")
		self.assertEqual(result["rows"], [["A|B", "0.20"]])
		result = self.query(
			content, "report.markdown", "SELECT section FROM text_lines WHERE text = 'Charge | (0.10)'"
		)
		self.assertEqual(result["rows"], [["Bank / Fees"]])

	def test_markdown_indented_code_and_unequal_rows(self):
		result = self.query("    | a | b |\n    | --- | --- |\n    | 1 | 2 |\n", "code.md")
		self.assertEqual([s["table"] for s in result["sheets"]], ["text_lines"])
		result = self.query(
			"|a|b|\n|---|---|\n|1|2|3|\n", "bad.md", "SELECT text FROM text_lines WHERE _row=3"
		)
		self.assertEqual(result["rows"], [["|1|2|3|"]])
		self.assertEqual([s["table"] for s in result["sheets"]], ["text_lines"])
		self.assertEqual(result["warnings"], [{"code": "malformed_markdown_table", "omitted_tables": 1}])
		self.assertFalse(result["coverage"]["derived_tables_complete"])
		content = "# C#\n|a|b|\n|---|---|\n|1|2|\n``` language|info\n# Fake\n```\nend"
		result = self.query(content, "code.md", "SELECT text, section FROM text_lines WHERE text = 'end'")
		self.assertEqual(result["rows"], [["end", "C#"]])

	def test_document_encoding_scope_and_oversized_result_row(self):
		for filename in ("x.json", "x.md", "x.txt"):
			with self.subTest(filename=filename):
				with self.assertRaisesRegex(InvalidArgumentError, "UTF-8"):
					self.query(b"\xff", filename)
				with self.assertRaisesRegex(InvalidArgumentError, "sheets"):
					self.query("{}", filename, sheets=["Sheet1"])
		long = "x" * 20000 + "END"
		with self.assertRaisesRegex(InvalidArgumentError, "One result row"):
			self.query(long, "long.txt", "SELECT text FROM text_lines")
		self.assertEqual(
			self.query(long, "long.txt", "SELECT substr(text, -3) FROM text_lines")["rows"], [["END"]]
		)

	def test_document_sql_remains_read_only(self):
		for sql in (
			"DELETE FROM json_nodes",
			"ATTACH DATABASE '/tmp/no-document.db' AS other",
			"SELECT * FROM sqlite_master",
			"SELECT readfile('/etc/passwd')",
			"SELECT sum(value) FROM json_nodes",
		):
			with self.subTest(sql=sql), self.assertRaises(InvalidArgumentError):
				self.query("[1,2]", "data.json", sql)

	def test_large_attachment_hint_routes_documents_without_inlining_source(self):
		import frappe

		with patch.object(frappe, "conf", frappe._dict()):
			from jarvis.chat.turn_handler import _prepare_attachments
		for filename in ("records.json", "report.md", "report.markdown", "notes.txt"):
			doc = SimpleNamespace(name="file", file_name=filename, file_url="/private/files/" + filename)
			with (
				self.subTest(filename=filename),
				patch("frappe.get_doc", return_value=doc),
				patch("jarvis.tools._file_source.resolve_file", return_value=doc),
				patch("frappe.has_permission", return_value=True),
				patch("jarvis.compat.file_bytes", return_value=b"PRIVATE-DOCUMENT-CONTENT\n" * 1000),
			):
				message, images = _prepare_attachments(
					"Find the last payment.", [{"file_url": "/private/files/" + filename}], False
				)
				self.assertIn("jarvis__query_file", message)
				self.assertIn("original _row line numbers", message)
				self.assertNotIn("PRIVATE-DOCUMENT-CONTENT", message)
				self.assertEqual(images, [])

	def test_small_documents_remain_fenced_inline_input(self):
		import frappe

		with patch.object(frappe, "conf", frappe._dict()):
			from jarvis.chat.turn_handler import _prepare_attachments
		for filename in ("records.json", "report.md", "report.markdown", "notes.txt"):
			doc = SimpleNamespace(name="file", file_name=filename, file_url="/private/files/" + filename)
			with (
				self.subTest(filename=filename),
				patch("frappe.get_doc", return_value=doc),
				patch("jarvis.tools._file_source.resolve_file", return_value=doc),
				patch("frappe.has_permission", return_value=True),
				patch("jarvis.compat.file_bytes", return_value=b"SMALL-DOCUMENT-CONTENT"),
			):
				message, images = _prepare_attachments(
					"Read this.", [{"file_url": "/private/files/" + filename}], False
				)
				self.assertIn("SMALL-DOCUMENT-CONTENT", message)
				self.assertIn("<untrusted-data", message)
				self.assertEqual(images, [])

	def test_extra_derived_tables_do_not_hide_raw_document_lines(self):
		content = ("|a|b|\n|---|---|\n|1|2|\n\n" * 32) + "TAIL-RECORD"
		result = self.query(content, "tables.md", "SELECT text FROM text_lines WHERE text='TAIL-RECORD'")
		self.assertEqual(result["rows"], [["TAIL-RECORD"]])
		self.assertEqual(len(result["sheets"]), 32)
		self.assertEqual(result["warnings"], [{"code": "markdown_table_limit", "omitted_tables": 1}])
		self.assertFalse(result["coverage"]["derived_tables_complete"])

	def test_real_child_processes_large_json_markdown_and_text(self):
		json_data = (
			'{"payments":[' + ",".join(f'{{"id":"P-{i}","amount":0.10}}' for i in range(20000)) + "]}"
		).encode()
		result = run_query(
			json_data,
			"payments.json",
			sql="SELECT COUNT(*), decimal_sum(value) FROM json_nodes WHERE key = 'amount'",
		)
		self.assertEqual(result["rows"], [[20000, "2000.00"]])
		self.assertEqual(result["source_rows"], 60002)
		self.assertTrue(result["coverage"]["complete"])
		markdown = ("|Ref|Amount|\n|---|---|\n" + "|A|0.10|\n" * 1500).encode()
		self.assertEqual(
			run_query(
				markdown, "payments.md", sql="SELECT COUNT(*), decimal_sum(B) FROM md_table1 WHERE _row > 1"
			)["rows"],
			[[1500, "150.00"]],
		)
		text = ("line\n" * 5000 + "last-record").encode()
		self.assertEqual(
			run_query(text, "notes.txt", sql="SELECT _row, text FROM text_lines WHERE text = 'last-record'")[
				"rows"
			],
			[[5001, "last-record"]],
		)


if __name__ == "__main__":
	unittest.main()
