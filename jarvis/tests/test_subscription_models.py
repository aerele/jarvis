"""Tests for jarvis._subscription_models - the subscription model catalogue."""

import json
import unittest

from jarvis import _subscription_models as cat
from jarvis.oauth.api import _coerce_subscription_model

GEMINI = "Google Gemini"
# Every id here MUST be compiled into the pinned cliproxy image. "gemini-3.1-flash"
# was listed until 2026-07-23 and never was: the image carries only the -image,
# -image-preview, -lite and -lite-preview variants, so a subscriber choosing it
# silently misrouted to the pool primary via Bifrost's catch-all instead of
# erroring. Replaced with the -lite variant, which is real.
# CORRECTED 2026-07-28: this note used to add that the api-key tier still offered
# "gemini-3.1-flash" because Google's own API served it. Measured against a live
# key, Google 404s it there too ("not found for API version v1beta") and it is
# absent from the 41 generateContent models the API lists, so the bare id is gone
# from BOTH tiers. This tier is image-bound on top of that.
EXPECTED_GEMINI = [
	"gemini-2.5-pro",
	"gemini-2.5-flash",
	"gemini-3.1-flash-lite",
]


class TestSubscriptionCatalogue(unittest.TestCase):
	def test_gemini_list_is_active_set(self):
		self.assertEqual(cat.SUBSCRIPTION_MODELS[GEMINI], EXPECTED_GEMINI)

	def test_gemini_default_is_in_list(self):
		self.assertEqual(cat.DEFAULT_MODEL[GEMINI], "gemini-2.5-pro")
		self.assertIn(cat.DEFAULT_MODEL[GEMINI], cat.SUBSCRIPTION_MODELS[GEMINI])

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

	def test_coerce_accepts_every_active_gemini_model(self):
		for model in EXPECTED_GEMINI:
			self.assertEqual(_coerce_subscription_model(GEMINI, model), model)

	def test_coerce_falls_back_to_default_for_bogus_and_empty(self):
		self.assertEqual(_coerce_subscription_model(GEMINI, "nope"), "gemini-2.5-pro")
		self.assertEqual(_coerce_subscription_model(GEMINI, ""), "gemini-2.5-pro")

	def test_openai_entry_unchanged(self):
		self.assertEqual(cat.SUBSCRIPTION_MODELS["OpenAI"], ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"])
		self.assertEqual(cat.DEFAULT_MODEL["OpenAI"], "gpt-5.5")
