"""read_file wiring for .pptx: windows, truncation signal, preview and kill-switch gates."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jarvis.tests.test_pptx_text import _shape, _slide, build
from jarvis.tools import _pptx_text
from jarvis.tools.read_file import read_file


def _read(data, **args):
	doc = SimpleNamespace(name="pptx1", file_name="deck.pptx", file_url="/private/files/deck.pptx")
	with (
		patch("jarvis.tools.read_file.resolve_file", return_value=doc),
		patch.object(frappe, "has_permission", return_value=True),
		patch("jarvis.compat.file_bytes", return_value=data),
	):
		return read_file(**{"file_url": doc.file_url, **args})


def _deck(count=2, text="hello"):
	return build({f"s{i}.xml": (_slide(_shape(f"{text}{i}")), None) for i in range(count)})


class TestPptxReadFile(unittest.TestCase):
	def test_cursor_windows_reach_end(self):
		data = _deck(1, "x" * 50)
		first = _read(data, read_mode="file-cursor-v1", max_chars=20)
		self.assertFalse(first["end_of_file"])
		nxt = first["next_read"]
		last = _read(
			data, read_mode="file-cursor-v1", max_chars=1000, cursor=nxt["cursor"], version=nxt["version"]
		)
		self.assertTrue(last["end_of_file"])
		self.assertIsNone(last["next_read"])

	def test_truncated_source_is_not_reported_complete(self):
		data = _deck(_pptx_text.MAX_SLIDES + 1, "a")
		out = _read(data, read_mode="file-cursor-v1", max_chars=40000)
		while out["next_read"]:
			nxt = out["next_read"]
			out = _read(
				data,
				read_mode="file-cursor-v1",
				max_chars=40000,
				cursor=nxt["cursor"],
				version=nxt["version"],
			)
		self.assertEqual(out["status"], "partial")
		self.assertFalse(out["coverage"]["complete"])
		self.assertFalse(out["coverage"]["window_complete"])
		self.assertTrue(out["truncated"])

	def test_preview_has_no_cursor_contract(self):
		out = _read(_deck(), preview=True, read_mode="file-cursor-v1")
		self.assertEqual(out["format"], "pptx")
		self.assertNotIn("next_read", out)
		self.assertNotIn("read_contract", out)

	def test_kill_switch_degrades_to_preview(self):
		with patch.object(frappe, "conf", {"jarvis_file_sections_enabled": False}):
			out = _read(_deck(), read_mode="file-cursor-v1")
		self.assertEqual(out["format"], "pptx")
		self.assertNotIn("next_read", out)
		self.assertNotIn("read_contract", out)

	def test_corrupt_file_is_invalid_argument(self):
		from jarvis.exceptions import InvalidArgumentError

		with self.assertRaises(InvalidArgumentError):
			_read(b"nope", read_mode="file-cursor-v1")
