"""Tests for export_document: composed content → downloadable PDF / HTML / PNG.

save_file is mocked so we inspect the exact bytes the tool produced (real
render engines still run — get_pdf / md_to_html / pypdfium2).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, NoDataError
from jarvis.tools.export_document import export_document

_MD = "# Report\n\n| Metric | Value |\n|---|---|\n| Revenue | 1000 |\n\n- point one\n"


def _saved(mock):
	"""(filename, bytes) export_document handed save_file."""
	return mock.call_args.args[0], mock.call_args.args[1]


def _pdf_backend_ok() -> bool:
	"""Frappe's HTML→PDF backend (wkhtmltopdf) may be absent in a minimal env;
	the pdf/png tests skip rather than fail when it can't render."""
	try:
		from frappe.utils.pdf import get_pdf

		return bool(get_pdf("<p>x</p>"))
	except Exception:
		return False


def _png_backend_ok() -> bool:
	if not _pdf_backend_ok():
		return False
	try:
		import PIL
		import pypdfium2

		return True
	except Exception:
		return False


class TestExportDocumentGuards(FrappeTestCase):
	def test_rejects_empty_content(self):
		with self.assertRaises(NoDataError):
			export_document("")
		with self.assertRaises(NoDataError):
			export_document("   \n  ")

	def test_rejects_unknown_format(self):
		with self.assertRaises(InvalidArgumentError):
			export_document(_MD, format="docx")


class TestExportDocumentFormats(FrappeTestCase):
	def _mock(self):
		m = patch("frappe.utils.file_manager.save_file").start()
		self.addCleanup(patch.stopall)
		m.return_value = frappe._dict(
			file_url="/private/files/doc.out", file_name="doc.out", file_size=1, name="F-DOC"
		)
		return m

	def test_pdf(self):
		if not _pdf_backend_ok():
			self.skipTest("no HTML→PDF backend (wkhtmltopdf) in this environment")
		m = self._mock()
		out = export_document(_MD, format="pdf", title="My Report")
		fname, payload = _saved(m)
		self.assertEqual(out["mime_type"], "application/pdf")
		self.assertTrue(fname.endswith(".pdf"))
		self.assertEqual(payload[:4], b"%PDF")

	def test_html_is_standalone_and_renders_markdown(self):
		m = self._mock()
		out = export_document(_MD, format="html", title="My Report")
		fname, payload = _saved(m)
		text = payload.decode("utf-8")
		self.assertEqual(out["mime_type"], "text/html")
		self.assertTrue(fname.endswith(".html"))
		self.assertIn("<!doctype html>", text.lower())
		self.assertIn("<title>My Report</title>", text)
		self.assertIn("<table", text)  # markdown table rendered
		self.assertIn("Revenue", text)

	def test_png(self):
		if not _png_backend_ok():
			self.skipTest("no PDF→PNG rendering stack (wkhtmltopdf / pypdfium2 / PIL)")
		m = self._mock()
		out = export_document(_MD, format="png", title="My Report")
		fname, payload = _saved(m)
		self.assertEqual(out["mime_type"], "image/png")
		self.assertTrue(fname.endswith(".png"))
		self.assertEqual(payload[:4], b"\x89PNG")

	def test_raw_html_benign_tags_survive_sanitization(self):
		"""content_is_html=True skips markdown conversion, not sanitization —
		benign tags with no dangerous attributes pass through unchanged."""
		m = self._mock()
		export_document("<h1>Raw</h1><p>kept verbatim</p>", format="html", content_is_html=True)
		_, payload = _saved(m)
		text = payload.decode("utf-8")
		self.assertIn("<h1>Raw</h1>", text)  # not markdown-escaped
		self.assertIn("kept verbatim", text)

	def test_default_format_is_pdf(self):
		if not _pdf_backend_ok():
			self.skipTest("no HTML→PDF backend (wkhtmltopdf) in this environment")
		m = self._mock()
		out = export_document(_MD, title="d")
		self.assertEqual(out["mime_type"], "application/pdf")
		self.assertEqual(_saved(m)[1][:4], b"%PDF")


class TestExportDocumentSanitization(FrappeTestCase):
	"""content is agent-composed (effectively LLM-controlled) text. Two threats,
	both must be closed on BOTH the Markdown branch and the content_is_html=True
	raw-HTML branch: XSS in the standalone HTML, and SSRF-via-render — wkhtmltopdf
	fetches any <img>/<link>/SVG <image> it finds, server-side, at render time."""

	def _mock(self):
		m = patch("frappe.utils.file_manager.save_file").start()
		self.addCleanup(patch.stopall)
		m.return_value = frappe._dict(
			file_url="/private/files/doc.out", file_name="doc.out", file_size=1, name="F-DOC"
		)
		return m

	def _rendered_html(self, content: str, *, content_is_html: bool) -> str:
		m = self._mock()
		export_document(content, format="html", content_is_html=content_is_html)
		return _saved(m)[1].decode("utf-8")

	def test_script_tag_stripped_markdown_branch(self):
		text = self._rendered_html("hello <script>alert(1)</script> world", content_is_html=False)
		self.assertNotIn("<script", text)
		self.assertNotIn("alert(1)", text.replace("&#39;", "'"))  # not present as live JS either way

	def test_script_tag_stripped_raw_html_branch(self):
		text = self._rendered_html("<p>hi</p><script>alert(1)</script>", content_is_html=True)
		self.assertNotIn("<script", text)

	def test_img_tag_stripped_both_branches(self):
		"""The SSRF vector this whole change exists to close: an <img src> makes
		wkhtmltopdf issue a server-side fetch at render time (cloud metadata
		endpoints, internal network probing, file:// local reads)."""
		md_text = self._rendered_html("![x](http://169.254.169.254/latest/meta-data/)", content_is_html=False)
		self.assertNotIn("<img", md_text)
		html_text = self._rendered_html('<img src="file:///etc/passwd">', content_is_html=True)
		self.assertNotIn("<img", html_text)

	def test_svg_image_href_stripped(self):
		text = self._rendered_html(
			'<svg><image href="http://attacker.example/beacon"/></svg>', content_is_html=True
		)
		self.assertNotIn("<svg", text)
		self.assertNotIn("<image", text)

	def test_link_and_meta_stripped(self):
		text = self._rendered_html(
			'<link rel="stylesheet" href="http://attacker.example/x.css">'
			'<meta http-equiv="refresh" content="0;url=http://attacker.example">',
			content_is_html=True,
		)
		self.assertNotIn("<link", text)
		self.assertNotIn("http-equiv", text)

	def test_onerror_attribute_and_javascript_href_stripped(self):
		text = self._rendered_html(
			'<a href="javascript:alert(1)" onclick="alert(2)">click</a>', content_is_html=True
		)
		self.assertNotIn("javascript:", text)
		self.assertNotIn("onclick", text)

	def test_benign_markdown_formatting_still_renders(self):
		text = self._rendered_html("# Title\n\n**bold** and *italic*\n\n- one\n- two", content_is_html=False)
		self.assertIn("<h1", text)  # md_to_html's header-ids extra adds an id attribute
		self.assertIn("Title", text)
		self.assertIn("<strong>bold</strong>", text)
		self.assertIn("<li>one</li>", text)

	def test_content_length_cap(self):
		with self.assertRaises(InvalidArgumentError):
			export_document("x" * 200_001, format="html")
		# exactly at the cap is accepted
		m = self._mock()
		export_document("x" * 200_000, format="html")
		self.assertTrue(m.called)
