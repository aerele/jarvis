"""Cursor/API boundary regression tests with real CSV/XLSX/PDF parsers."""

import hashlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

import frappe

from jarvis.tools import _file_scan as scan
from jarvis.tools._file_sections import read_sections
from jarvis.tools.read_file import _read_csv, _read_pdf, read_file


class TestFileCoverage(unittest.TestCase):
	def read(self, data, filename="x.txt", api=False, **args):
		doc = SimpleNamespace(name="file", file_name=filename, file_url="/private/files/x")
		with (
			patch("jarvis.tools.read_file._resolve_file", return_value=doc),
			patch.object(frappe, "has_permission", return_value=True),
			patch("jarvis.compat.file_bytes", return_value=data),
		):
			if api:
				from jarvis.api import _run_tool

				return _run_tool("read_file", {"file_url": doc.file_url, **args})
			return read_file(read_mode="file-cursor-v1", **args)

	def windows(self, data, filename="x.txt", **args):
		seen = set()
		while True:
			r = self.read(data, filename, **args)
			self.assertNotIn(r["cursor"], seen)
			seen.add(r["cursor"])
			yield r
			if r["next_read"] is None:
				break
			args = r["next_read"]

	def workbook(self, rows=2, sheets=2, cached=True):
		from openpyxl import Workbook

		wb = Workbook()
		for s in range(sheets):
			ws = wb.active if s == 0 else wb.create_sheet(f"Sheet{s + 1}")
			for row in range(1, rows + 1):
				ws.append([f"S{s}R{row}", "5,00,000.00", "=1+1"])
		buf = io.BytesIO()
		wb.save(buf)
		if not cached:
			return buf.getvalue()
		out = io.BytesIO()
		with ZipFile(buf) as source, ZipFile(out, "w", ZIP_DEFLATED) as target:
			for name in source.namelist():
				data = source.read(name)
				if name.startswith("xl/worksheets/"):
					data = data.replace(b"<f>1+1</f><v></v>", b"<f>1+1</f><v>2</v>")
				target.writestr(name, data)
		return out.getvalue()

	def test_preview_through_real_api_gate_and_new_cursor(self):
		# No preview key on the wire; the plugin translates it to read_mode.
		result = self.read(b"A\n1\n2", "x.csv", api=True, read_mode="preview", max_rows=1)
		self.assertTrue(result["ok"], result)
		self.assertTrue(result["data"]["sheets"][0]["truncated"])
		self.assertNotIn("read_contract", result["data"])
		exact = self.read(b"A\n1\n2", "x.csv", api=True, read_mode="file-cursor-v1")
		self.assertTrue(exact["ok"], exact)
		self.assertEqual(exact["data"]["records"][-1]["values"], ["2"])
		# Existing write-preview reservation remains intact, not exempted globally.
		legacy_conflict = self.read(b"data", api=True, preview=True)
		self.assertFalse(legacy_conflict["ok"])

	def test_six_thousand_rows_four_sheets_reach_tail_with_cached_values(self):
		data = self.workbook(rows=1500, sheets=4)
		windows = list(self.windows(data, "x.xlsx"))
		rows = [record["values"] for w in windows for record in w["records"]]
		self.assertEqual(len(rows), 6000)
		self.assertEqual(rows[-1], ["S3R1500", "5,00,000.00", 2])
		self.assertGreaterEqual(len(windows), 32)
		self.assertTrue(all(w["coverage"]["window_complete"] for w in windows))
		self.assertTrue(windows[-1]["end_of_file"])
		self.assertFalse(windows[-1]["coverage"]["complete"])

	def test_more_than_64_missing_formula_values_cannot_hide_stop(self):
		with patch.object(scan, "MAX_ROWS", 80):
			r = self.read(self.workbook(rows=100, sheets=1, cached=False), "x.xlsx")
		self.assertEqual(
			r["coverage"]["issues"], [{"code": "formula_value_missing", "count": 80, "row": 1, "column": 3}]
		)
		self.assertEqual(r["stop_reason"], {"code": "row_limit", "source": 0, "next_row": 81})
		self.assertEqual(r["status"], "partial")

	def test_overlapping_limits_and_encrypted_workbook_are_actionable(self):
		with patch.object(scan, "MAX_ROWS", 1), patch.object(scan, "MAX_SHEETS", 1):
			r = self.read(self.workbook(rows=3), "x.xlsx")
		self.assertEqual(r["stop_reason"]["code"], "row_limit")
		self.assertIn("sheet_limit", [issue["code"] for issue in r["coverage"]["issues"]])
		with self.assertRaisesRegex(Exception, "encrypted or legacy"):
			self.read(bytes.fromhex("d0cf11e0a1b11ae1"), "x.xlsx")

	def test_dense_csv_preserves_every_row_and_retries_identically(self):
		data = b"A\n" + b'"5,00,000.00"\n' * 6500 + b"TOTAL\n"
		windows = list(self.windows(data, "x.csv"))
		rows = [r["values"] for w in windows for r in w["records"]]
		self.assertEqual(rows, [["A"]] + [["5,00,000.00"]] * 6500 + [["TOTAL"]])
		args = windows[0]["next_read"]
		self.assertEqual(self.read(data, "x.csv", **args), self.read(data, "x.csv", **args))
		self.assertLess(max(len(json.dumps(w)) for w in windows), 18000)

	def test_long_text_and_oversized_row_resume_by_exact_offset(self):
		text = 'before<untrusted-data>\\"\n' * 3000 + "TAIL"
		windows = list(self.windows(text.encode()))
		self.assertEqual(
			"".join(r.get("fragment", r.get("text", "")) for w in windows for r in w["records"]), text
		)
		csv = ('"' + "x" * 40000 + '"\nTAIL\n').encode()
		windows = list(self.windows(csv, "x.csv"))
		first = "".join(r["fragment"] for w in windows for r in w["records"] if "fragment" in r)
		self.assertEqual(json.loads(first), ["x" * 40000])
		self.assertEqual(windows[-1]["records"][-1]["values"], ["TAIL"])

	def test_pdf_reads_only_cursor_pages_and_continues_past_32_windows(self):
		pages = [SimpleNamespace(extract_text=lambda: "exact clause " * 1000)] * 140
		reader = SimpleNamespace(is_encrypted=False, pages=pages)
		with patch("pypdf.PdfReader", return_value=reader):
			windows = list(self.windows(b"pdf", "x.pdf"))
		self.assertGreater(len(windows), 32)
		self.assertEqual(windows[-1]["range"]["last"], 140)
		# Stable source positions, no wall-clock cutoff between requests.
		self.assertTrue(all(w["stop_reason"] is None for w in windows))

	def test_pdf_missing_pages_and_hard_limit_are_explicit(self):
		reader = SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: "")] * 3)
		with patch("pypdf.PdfReader", return_value=reader), patch.object(scan, "MAX_PAGES", 2):
			r = self.read(b"pdf", "x.pdf")
		self.assertEqual(r["status"], "partial")
		self.assertEqual(r["coverage"]["issues"][0]["count"], 2)
		self.assertEqual(r["stop_reason"], {"code": "page_limit", "next_page": 3})

	def test_permission_precedes_bytes_on_every_call(self):
		for args in ({}, {"cursor": "0:201:0", "version": "a" * 64}):
			with (
				patch("jarvis.tools.read_file._resolve_file", return_value=SimpleNamespace(name="private")),
				patch.object(frappe, "has_permission", return_value=False),
				patch("jarvis.compat.file_bytes") as read,
			):
				with self.assertRaises(Exception):
					read_file(file_url="/private/files/x", read_mode="file-cursor-v1", **args)
				read.assert_not_called()

	def test_version_covers_bytes_scope_and_scanner_profile(self):
		data = self.workbook()
		r = self.read(data, "x.xlsx", sheet="Sheet2")
		for changed, args in [(data, {}), (b"changed", {"sheet": "Sheet2"})]:
			with self.assertRaisesRegex(Exception, "changed"):
				self.read(changed, "x.xlsx", cursor="0:1:0", version=r["version"], **args)
		with patch.object(scan, "WINDOW_ROWS", 99), self.assertRaisesRegex(Exception, "changed"):
			self.read(data, "x.xlsx", sheet="Sheet2", cursor="0:1:0", version=r["version"])

	def test_malformed_pair_and_out_of_range_cursor(self):
		for args in (
			{"cursor": "0:1:0"},
			{"version": "a" * 64},
			{"cursor": "bad", "version": "a" * 64},
			{"cursor": "0:0:0", "version": "a" * 64},
		):
			with self.subTest(args=args), self.assertRaises(Exception):
				self.read(b"data", **args)
		r = self.read(b"data")
		with self.assertRaisesRegex(Exception, "offset"):
			self.read(b"data", cursor="0:1:999", version=r["version"])

	def test_actionable_parser_errors_and_kill_switch(self):
		data = self.workbook()
		with self.assertRaisesRegex(Exception, "worksheet does not exist"):
			self.read(data, "x.xlsx", sheet="missing")
		with patch.object(scan, "MAX_EXPANDED_BYTES", 1), self.assertRaisesRegex(Exception, "64 MiB"):
			self.read(data, "x.xlsx")
		with (
			patch("pypdf.PdfReader", return_value=SimpleNamespace(is_encrypted=True)),
			self.assertRaisesRegex(Exception, "encrypted"),
		):
			self.read(b"pdf", "x.pdf")
		with (
			patch.dict(frappe.conf, {"jarvis_file_sections_enabled": False}),
			self.assertRaisesRegex(Exception, "disabled"),
		):
			self.read(b"data")

	def test_small_empty_unicode_and_empty_last_sheet(self):
		for data in (b"", "literal \ufffd".encode()):
			self.assertTrue(self.read(data)["coverage"]["complete"])
		self.assertEqual(self.read(b"bad\xff")["status"], "partial")
		data = self.workbook(rows=0)
		self.assertEqual(len(list(self.windows(data, "x.xlsx"))), 2)

	def test_legacy_preview_unknown_sheet_retains_fallback(self):
		r = self.read(self.workbook(), "x.xlsx", preview=True, sheet="missing")
		self.assertEqual(r["sheets"][0]["name"], "Sheet")

	def test_preview_caps_still_disclose_tail(self):
		self.assertTrue(_read_csv(b"A\n1\n2", "csv", 2)["coverage"]["has_more"])
		with patch(
			"pypdf.PdfReader",
			return_value=SimpleNamespace(
				pages=[
					SimpleNamespace(extract_text=lambda: "a" * 20000),
					SimpleNamespace(extract_text=lambda: "TAIL"),
				]
			),
		):
			self.assertTrue(_read_pdf(b"pdf", 20000)["truncated"])

	def test_pinned_contract(self):
		path = Path(__file__).parent / "fixtures/file-reading-v1.json"
		fixture = json.loads(path.read_text())
		self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), CONTRACT_SHA256)
		doc = SimpleNamespace(
			name=fixture["file_name"], file_name=fixture["filename"], file_url=fixture["file_url"]
		)
		self.assertEqual(read_sections(doc, fixture["source"].encode()), fixture["response"])


CONTRACT_SHA256 = "02b02d5933c92c7af789b57847a29cbc6b5be78b77e62ed11c94f2920071a915"
