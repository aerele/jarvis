"""Pure classification tests; no site, credentials, or running services needed."""

import json
import unittest
from pathlib import Path

from jarvis.chat.error_taxonomy import classify_error_text, headline_for


class TestErrorTaxonomy(unittest.TestCase):
	def test_error_cases(self):
		cases = json.loads((Path(__file__).parent / "fixtures/turn_errors.json").read_text())
		for case in cases:
			with self.subTest(error=case["error"]):
				self.assertEqual(classify_error_text(case["error"]), case["code"])

	def test_structured_error(self):
		self.assertEqual(classify_error_text({"error": {"type": "overloaded_error"}}), "service-unavailable")

	def test_headline_is_the_matched_rules_own(self):
		# For callers that must say why something failed away from the chat: the same
		# words the chat shows, never the payload.
		headline = headline_for({"error": {"type": "overloaded_error"}})
		self.assertTrue(headline)
		self.assertNotIn("{", headline)
		self.assertEqual(headline, headline_for('{"error": {"type": "overloaded_error"}}'))

	def test_text_no_rule_recognises_has_no_headline(self):
		self.assertIsNone(headline_for("zzz qqq"))
		self.assertIsNone(headline_for(""))
		self.assertIsNone(headline_for(None))
