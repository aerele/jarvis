"""Pure classification tests; no site, credentials, or running services needed."""

import json
import unittest
from pathlib import Path

from jarvis.chat.error_taxonomy import (
	_RULES,
	SUBSCRIPTION_LABELS,
	classify_error_text,
	headline_for,
	subscription_upstream_from_error,
)


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


class TestSubscriptionExpiredRule(unittest.TestCase):
	def _upstream_cases(self):
		return json.loads(
			(Path(__file__).parent / "fixtures/subscription_upstreams.json").read_text(encoding="utf-8")
		)

	def test_rule_sits_before_models_exhausted_and_authentication(self):
		codes = [code for code, _, _ in _RULES]
		self.assertIn("subscription-expired", codes)
		self.assertLess(codes.index("subscription-expired"), codes.index("models-exhausted"))
		self.assertLess(codes.index("subscription-expired"), codes.index("authentication"))

	def test_prod_string_and_all_models_failed_form(self):
		cases = self._upstream_cases()
		self.assertEqual(classify_error_text(cases[0]["error"]), "subscription-expired")
		self.assertEqual(classify_error_text(cases[1]["error"]), "subscription-expired")

	def test_non_subscription_authentication_stays_authentication(self):
		self.assertEqual(
			classify_error_text("OpenAI API error: 401 Unauthorized. Incorrect API key provided: sk-xxxx"),
			"authentication",
		)
		self.assertEqual(
			classify_error_text("MCP connector google_drive failed: invalid_grant"), "authentication"
		)
		self.assertEqual(
			classify_error_text(
				"All models failed: Anthropic 401 Unauthorized; OpenAI 503 Service Unavailable"
			),
			"models-exhausted",
		)

	def test_upstream_fixtures_match_the_js_side(self):
		for case in self._upstream_cases():
			with self.subTest(error=case["error"][:60]):
				self.assertEqual(subscription_upstream_from_error(case["error"]), case["upstream"])

	def test_labels_are_the_apps_own(self):
		self.assertEqual(
			SUBSCRIPTION_LABELS,
			{"openai": "OpenAI", "anthropic": "Anthropic", "xai": "xAI Grok", "kimi": "Kimi (Moonshot)"},
		)

	def test_headline_for_fills_the_provider_and_never_leaks_the_placeholder(self):
		cases = self._upstream_cases()
		self.assertEqual(headline_for(cases[0]["error"]), "OpenAI sign-in expired")
		self.assertEqual(headline_for(cases[5]["error"]), "Kimi (Moonshot) sign-in expired")
		# A strong signal that names no upstream still has no placeholder in it.
		self.assertEqual(headline_for("auth_unavailable: unauthorized"), "Sign-in expired")
