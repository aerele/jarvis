"""Unit tests for jarvis._text.plaintext_to_html - the shared plain-text ->
newline-preserving-HTML conversion applied wherever the agent's composed text
lands in an HTML-rendered field (email Communication content, Comment content).
"""

from __future__ import annotations

from frappe.tests.utils import FrappeTestCase

from jarvis._text import plaintext_to_html


class TestPlaintextToHtml(FrappeTestCase):
	def test_newline_becomes_br(self):
		self.assertEqual(plaintext_to_html("a\nb"), "a<br>\nb")

	def test_blank_line_survives_as_two_breaks(self):
		out = plaintext_to_html("Para one.\n\nPara two.")
		self.assertEqual(out.count("<br>"), 2)
		self.assertIn("Para one.", out)
		self.assertIn("Para two.", out)

	def test_html_special_chars_are_escaped(self):
		# a stray < / & / an <email> must render literally, never as markup
		out = plaintext_to_html("a < b & c <x@y.com>")
		self.assertIn("&lt;", out)
		self.assertIn("&amp;", out)
		self.assertIn("&lt;x@y.com&gt;", out)
		self.assertNotIn("<x@y.com>", out)

	def test_empty_and_none_are_safe(self):
		self.assertEqual(plaintext_to_html(""), "")
		self.assertEqual(plaintext_to_html(None), "")

	def test_plain_single_line_unchanged(self):
		self.assertEqual(plaintext_to_html("hello world"), "hello world")
