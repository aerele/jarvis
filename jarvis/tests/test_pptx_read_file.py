"""read_file wiring for .pptx: plugin-contract port, truncation, preview and kill-switch gates."""

import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tests.test_docx_reading import p as docx_p
from jarvis.tests.test_docx_reading import package
from jarvis.tests.test_pptx_text import _shape, _slide, build
from jarvis.tools import _pptx_text
from jarvis.tools.read_file import read_file

CURSOR = r"[0-9]{1,2}:[0-9]{1,6}:[0-9]{1,8}"


def plugin_failures(data, args):
	"""Port of normalizeFileReading checks in jarvis-openclaw-plugin/src/file-reading.ts.

	Returns the names of failed checks (empty list means the plugin accepts the reply).
	"""
	coverage, nxt, rng = data.get("coverage") or {}, data.get("next_read") or {}, data.get("range") or {}
	cursor = args.get("cursor", "0:1:0")
	pos = [int(x) for x in cursor.split(":")]
	records = data["records"] if isinstance(data.get("records"), list) else []
	first = records[0] if records else {}
	valid_cursor = lambda c: isinstance(c, str) and re.fullmatch(CURSOR, c)  # noqa: E731
	checks = {
		"status": data.get("status") in ("complete", "window", "partial"),
		"coverage": isinstance(coverage.get("complete"), bool)
		and isinstance(coverage.get("window_complete"), bool)
		and isinstance(coverage.get("issues"), list),
		"records": isinstance(data.get("records"), list) and bool(data.get("source")),
		"version": bool(re.fullmatch("[a-f0-9]{64}", str(data.get("version"))))
		and ("version" not in args or data["version"] == args["version"]),
		"cursor_echo": bool(valid_cursor(data.get("cursor"))) and data["cursor"] == cursor,
		"position": rng.get("source") == pos[0]
		and rng.get("first") == pos[1]
		and isinstance(rng.get("last"), int),
		"empty_window": len(records) > 0 or data.get("end_of_file") is True,
		"fragment_offset": not records or first.get("offset", 0) == pos[2],
		"end": isinstance(data.get("end_of_file"), bool),
		"next_read": data.get("next_read") is None
		if data.get("end_of_file")
		else bool(valid_cursor(nxt.get("cursor")))
		and nxt["cursor"] != cursor
		and nxt.get("version") == data.get("version")
		and bool(nxt.get("file_url"))
		and nxt.get("sheet") == args.get("sheet"),
		"complete": coverage.get("complete") is (data.get("status") == "complete")
		and (
			not coverage.get("complete")
			or ("cursor" not in args and data.get("end_of_file") is True and coverage.get("window_complete"))
		),
	}
	return [name for name, ok in checks.items() if not ok]


def _read(data, name="deck.pptx", **args):
	doc = SimpleNamespace(name="f1", file_name=name, file_url=f"/private/files/{name}")
	with (
		patch("jarvis.tools.read_file.resolve_file", return_value=doc),
		patch.object(frappe, "has_permission", return_value=True),
		patch("jarvis.compat.file_bytes", return_value=data),
	):
		return read_file(**{"file_url": doc.file_url, **args})


def _walk(data, name="deck.pptx"):
	"""Follow next_read to the end, asserting the plugin accepts every window."""
	args, windows = {"read_mode": "file-cursor-v1"}, []
	for _ in range(60):
		reply = _read(data, name, **args)
		assert plugin_failures(reply, args) == [], (plugin_failures(reply, args), args, reply)
		windows.append(reply)
		if not reply["next_read"]:
			return windows
		args = {"read_mode": "file-cursor-v1", **{k: reply["next_read"][k] for k in ("cursor", "version")}}
	raise AssertionError("did not terminate")


def _deck(count, text="hello"):
	return build({f"s{i}.xml": (_slide(_shape(f"{text}{i}")), None) for i in range(count)})


class TestPptxContract(unittest.TestCase):
	def test_port_matches_real_docx_reply(self):
		windows = _walk(package(docx_p("Real docx reply")), "document.docx")
		self.assertEqual(plugin_failures(windows[0], {}), [])
		self.assertEqual(windows[0]["status"], "complete")

	def test_first_window(self):
		reply = _read(_deck(1), read_mode="file-cursor-v1")
		self.assertEqual(plugin_failures(reply, {}), [])
		self.assertEqual((reply["range"]["first"], reply["range"]["last"]), (1, 1))
		self.assertEqual(reply["status"], "complete")
		self.assertIn("hello0", reply["records"][0]["text"])

	def test_middle_and_last_windows(self):
		big = _deck(40, "y" * 1500)  # forces several 12k-char windows
		windows = _walk(big)
		self.assertGreater(len(windows), 2)
		self.assertEqual(windows[1]["status"], "window")
		self.assertEqual(windows[-1]["status"], "window")
		self.assertTrue(windows[-1]["end_of_file"])
		self.assertFalse(windows[-1]["coverage"]["complete"])
		texts = "".join(r["text"] for w in windows for r in w["records"])
		self.assertIn("Slide 40", texts)

	def test_oversized_slide_fragments(self):
		windows = _walk(_deck(1, "z" * 30000))
		self.assertGreater(len(windows), 2)
		self.assertIn("fragment", windows[1]["records"][0])
		self.assertTrue(windows[-1]["records"][-1]["fragment"].endswith("zzz0"))

	def test_capped_deck_is_partial(self):
		windows = _walk(_deck(_pptx_text.MAX_SLIDES + 1, "a"))
		last = windows[-1]
		self.assertEqual(last["status"], "partial")
		self.assertTrue(last["end_of_file"])
		self.assertFalse(last["coverage"]["complete"])
		self.assertFalse(last["coverage"]["window_complete"])
		self.assertEqual(last["stop_reason"]["code"], "slide_limit")

	def test_empty_deck(self):
		reply = _read(build({}), read_mode="file-cursor-v1")
		self.assertEqual(plugin_failures(reply, {}), [])

	def test_bad_cursor_and_version(self):
		data = _deck(2)
		with self.assertRaises(InvalidArgumentError):
			_read(data, read_mode="file-cursor-v1", cursor="0:9:0", version="0" * 64)
		version = _read(data, read_mode="file-cursor-v1")["version"]
		with self.assertRaises(InvalidArgumentError):
			_read(data, read_mode="file-cursor-v1", cursor="0:9:0", version=version)


class TestPptxGates(unittest.TestCase):
	def test_preview_has_no_cursor_contract(self):
		out = _read(_deck(2), preview=True, read_mode="file-cursor-v1")
		self.assertEqual(out["format"], "pptx")
		self.assertNotIn("next_read", out)
		self.assertNotIn("read_contract", out)

	def test_kill_switch_degrades_to_preview(self):
		with patch.object(frappe, "conf", {"jarvis_file_sections_enabled": False}):
			out = _read(_deck(2), read_mode="file-cursor-v1")
		self.assertEqual(out["format"], "pptx")
		self.assertNotIn("read_contract", out)

	def test_corrupt_file_is_invalid_argument(self):
		with self.assertRaises(InvalidArgumentError):
			_read(b"nope", read_mode="file-cursor-v1")
