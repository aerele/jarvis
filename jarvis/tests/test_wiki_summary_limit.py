"""Wiki summary limit: 500 characters, measured in characters, named in the error.

A person saw ``update_wiki`` refuse a 382-character summary against a stated 500
limit. No code path reproduces that, so these tests pin the contract: up to 500
characters save (multi-byte text counts by character), 501 is refused with the
measured length, an injection-shaped summary is refused (not silently swapped),
and the confirm card shows the whole summary.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.confirm_card import build_card
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.update_wiki import update_wiki

SLUG = "org--wikitest-summary-limit"


class TestWikiSummaryLimit(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._wiki_was = frappe.db.get_single_value("Jarvis Settings", "wiki_enabled")
		frappe.db.set_single_value("Jarvis Settings", "wiki_enabled", 1, update_modified=False)
		frappe.db.delete("Jarvis Wiki Page", {"slug": SLUG})

	def tearDown(self):
		frappe.db.delete("Jarvis Wiki Page", {"slug": SLUG})
		frappe.db.set_single_value("Jarvis Settings", "wiki_enabled", self._wiki_was, update_modified=False)

	def _save(self, summary):
		return update_wiki(slug=SLUG, title="T", page_type="Org", summary=summary)

	def _stored(self):
		return frappe.db.get_value("Jarvis Wiki Page", {"slug": SLUG}, "summary")

	def test_boundary_lengths_save_with_awkward_characters(self):
		for n in (382, 499, 500):
			frappe.db.delete("Jarvis Wiki Page", {"slug": SLUG})
			text = ("a < b & c \U0001f600 தமிழ் " * 40)[: n - 1] + "x"  # the tool trims edge spaces
			res = self._save(text)
			self.assertEqual(res["summary_chars"], n)
			self.assertEqual(self._stored(), text)

	def test_501_is_refused_with_the_measured_length(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._save("a" * 501)
		self.assertIn("Summary is 501 characters; the limit is 500.", str(ctx.exception))

	def test_multibyte_counts_as_characters(self):
		tamil = "த" * 500  # 1500 bytes in UTF-8, still 500 characters
		self.assertEqual(self._save(tamil)["summary_chars"], 500)
		frappe.db.delete("Jarvis Wiki Page", {"slug": SLUG})
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._save("\U0001f600" * 501)
		self.assertIn("Summary is 501 characters", str(ctx.exception))

	def test_injection_shaped_summary_is_refused_not_swapped(self):
		for bad in (
			"# Heading first",
			"system: do this",
			"see jarvis__run_method",
			"```code```",
			"Customer asked us to ignore previous pricing",
			"\x00# x",
			"jar\x00vis__",
		):
			with self.assertRaises(InvalidArgumentError) as ctx:
				self._save(bad)
			self.assertIn("summary refused", str(ctx.exception))
			self.assertIsNone(self._stored())

	def test_card_collapses_whitespace_like_the_tool(self):
		card = build_card("update_wiki", {"slug": SLUG, "summary": "a\n\n  b\tc"}, {})
		self.assertEqual(card["summary"], "a b c")

	def test_card_shows_the_full_500_character_summary(self):
		summary = "word " * 99 + "end"  # 498 characters
		card = build_card("update_wiki", {"slug": SLUG, "summary": summary}, {})
		self.assertEqual(card["summary"], summary)
