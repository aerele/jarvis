"""Tests for jarvis._subscription_models - the subscription model catalogue."""

import json
import unittest

from jarvis import _subscription_models as cat
from jarvis.oauth.api import _coerce_subscription_model


class TestSubscriptionCatalogue(unittest.TestCase):
	def test_catalogue_values_are_json_serializable_lists(self):
		# The catalogue is a lazy Mapping (spec 6.3 keeps the public name), so it
		# must be dict()-wrapped before serialising. Asserting on the wrapped copy
		# preserves this test's original intent (values are JSON-safe lists) and
		# additionally locks the R9 rule: production serialises with orjson +
		# frappe's json_handler, which turns a BARE Mapping into a list of its
		# KEYS with no error raised.
		import orjson
		from frappe.utils.response import json_handler

		json.dumps(dict(cat.SUBSCRIPTION_MODELS))  # must not raise
		decoded = orjson.loads(orjson.dumps(dict(cat.SUBSCRIPTION_MODELS), default=json_handler))
		self.assertIsInstance(decoded, dict, "catalogue must serialise to a JSON object")
		for value in cat.SUBSCRIPTION_MODELS.values():
			self.assertIsInstance(value, list)

	def test_openai_entry_unchanged(self):
		self.assertEqual(cat.SUBSCRIPTION_MODELS["OpenAI"], ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"])
		self.assertEqual(cat.DEFAULT_MODEL["OpenAI"], "gpt-5.5")

	def test_coerce_falls_back_to_default_for_bogus_and_empty(self):
		self.assertEqual(_coerce_subscription_model("OpenAI", "nope"), "gpt-5.5")
		self.assertEqual(_coerce_subscription_model("OpenAI", ""), "gpt-5.5")

	def test_google_gemini_has_no_subscription_seed(self):
		# Google's chat subscription was removed 2026-08-19 (Google discontinued
		# consumer login-with-Google for Gemini 2026-06-18). Gemini stays
		# available via API key, which is served from the api_key-tier catalog,
		# not this subscription seed.
		self.assertNotIn("Google Gemini", cat._SEED_SUBSCRIPTION_MODELS)
		self.assertNotIn("Google Gemini", cat._SEED_DEFAULT_MODEL)
		# With no valid models and no default, coercion for the removed
		# provider falls all the way through to "".
		self.assertEqual(_coerce_subscription_model("Google Gemini", "gemini-2.5-pro"), "")
