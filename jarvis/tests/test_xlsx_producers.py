"""Offline, pinned producer exports through both real source-reader children."""

import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import frappe

from jarvis.tools._file_scan_process import scan_file
from jarvis.tools._spreadsheet_process import run_query

FIXTURES = Path(__file__).parent / "fixtures" / "xlsx_producers"


class TestXlsxProducers(unittest.TestCase):
	def test_real_producer_exports_read_with_archives_disabled(self):
		manifest = json.loads((FIXTURES / "manifest.json").read_text())
		self.assertEqual(len(manifest), 9)
		with patch.object(frappe, "conf", frappe._dict(jarvis_archive_reading_enabled=False)):
			for fixture in manifest:
				with self.subTest(fixture=fixture["filename"]):
					content = (FIXTURES / fixture["filename"]).read_bytes()
					self.assertEqual(hashlib.sha256(content).hexdigest(), fixture["sha256"])
					query = run_query(content, fixture["filename"])
					self.assertEqual(query["source_rows"], fixture["source_rows"])
					self.assertTrue(query["coverage"]["complete"])
					scan = scan_file(content, fixture["filename"])
					self.assertEqual(len(scan["records"]), fixture["scan_records"])
