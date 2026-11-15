"""Tests for jarvis._subscription_models - the subscription model catalogue."""

import json
import unittest

from jarvis import _subscription_models as cat
from jarvis.oauth.api import _coerce_subscription_model


class TestSubscriptionCatalogue(unittest.TestCase):
	def setUp(self):
		# These read the real (fixture-seeded) catalog; drop any request-cached
		# rows an earlier test left behind.
		from jarvis.tests.test_model_catalog import _clear_model_catalog_cache, _clear_sub_model_cache

		for clear in (_clear_model_catalog_cache, _clear_sub_model_cache):
			clear()
			self.addCleanup(clear)

	def test_catalogue_values_are_json_serializable_lists(self):
		# The catalogue is a lazy Mapping (spec 6.3 keeps the public name), so it
		# must be dict()-wrapped before serialising. Asserting on the wrapped copy
		# preserves this test's original intent (values are JSON-safe lists) and
		# additionally locks the R9 rule: production serialises with orjson +
		# frappe's json_handler, which turns a BARE Mapping into a list of its
		# KEYS with no error raised.
		from frappe.utils.response import json_handler

		from jarvis.tests import dumps_like_response

		json.dumps(dict(cat.SUBSCRIPTION_MODELS))  # must not raise
		decoded = json.loads(dumps_like_response(dict(cat.SUBSCRIPTION_MODELS), json_handler))
		self.assertIsInstance(decoded, dict, "catalogue must serialise to a JSON object")
		for value in cat.SUBSCRIPTION_MODELS.values():
			self.assertIsInstance(value, list)

	def test_openai_subscription_tier_carries_the_codex_catalog_ids(self):
		# The Codex channel serves the suffixed gpt-5.6 ids and gpt-6-astra (bare
		# "gpt-5.6" is an API-only alias); gpt-5.4 / gpt-5.4-mini were retired
		# upstream and fail inside cliproxy. Read through the app, not a literal.
		self.assertEqual(
			cat.SUBSCRIPTION_MODELS["OpenAI"],
			["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-6-astra", "gpt-5.5"],
		)
		self.assertEqual(cat.DEFAULT_MODEL["OpenAI"], "gpt-5.6-terra")

	def test_a_catalog_without_subscription_models_gives_empty_mappings(self):
		# The admin is the only source: no fallback copy fills the gap.
		from unittest.mock import patch

		from jarvis import admin_client
		from jarvis.tests.test_model_catalog import _clear_sub_model_cache

		self.addCleanup(_clear_sub_model_cache)
		api_key_only = [
			{"provider_id": "openai", "label": "OpenAI", "models": [{"model_id": "gpt-5", "tier": "api_key"}]}
		]
		with patch.object(admin_client, "get_model_catalog", return_value=api_key_only):
			_clear_sub_model_cache()
			self.assertEqual(dict(cat.SUBSCRIPTION_MODELS), {})
			self.assertEqual(dict(cat.DEFAULT_MODEL), {})

	def test_coerce_falls_back_to_default_for_bogus_and_empty(self):
		self.assertEqual(_coerce_subscription_model("OpenAI", "nope"), "gpt-5.6-terra")
		self.assertEqual(_coerce_subscription_model("OpenAI", ""), "gpt-5.6-terra")

	def test_signin_refuses_with_an_envelope_when_the_catalog_is_unavailable(self):
		# Never start a sign-in toward a blank model, and never raise out of the
		# sign-in endpoints: they answer with the {code, message} envelope.
		from unittest.mock import patch

		from jarvis import admin_client
		from jarvis.oauth.api import _catalog_unavailable
		from jarvis.tests.test_model_catalog import _clear_sub_model_cache

		self.addCleanup(_clear_sub_model_cache)
		with patch.object(admin_client, "get_model_catalog", return_value=[]):
			_clear_sub_model_cache()
			self.assertEqual(_coerce_subscription_model("OpenAI", "gpt-5.6-terra"), "")
			err = _catalog_unavailable()
		self.assertEqual(err["error"]["code"], "catalog_unavailable")
		_clear_sub_model_cache()
		self.assertIsNone(_catalog_unavailable())

	def test_google_gemini_has_no_subscription_tier(self):
		# Google's chat subscription was removed 2026-08-19 (Google discontinued
		# consumer login-with-Google for Gemini 2026-06-18). Gemini stays
		# available via API key, which is served from the api_key-tier catalog.
		self.assertNotIn("Google Gemini", cat.SUBSCRIPTION_MODELS)
		self.assertNotIn("Google Gemini", cat.DEFAULT_MODEL)
		# With no valid models and no default, coercion for the removed
		# provider falls all the way through to "".
		self.assertEqual(_coerce_subscription_model("Google Gemini", "gemini-2.5-pro"), "")
