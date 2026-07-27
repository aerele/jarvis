from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client
from jarvis.exceptions import AdminUnreachableError

_PAYLOAD = [
	{
		"provider_id": "openai",
		"label": "OpenAI",
		"default_base_url": "https://api.openai.com/v1",
		"renderer_id": "openai-codex",
		"auth_profile_id": "openai",
		"supports_api_key": True,
		"supports_subscription": True,
		"needs_base_url": False,
		"is_local": False,
		"models": [
			{
				"model_id": "gpt-5.5",
				"label": "gpt-5.5",
				"tier": "subscription",
				"is_default": True,
				"sort_order": 0,
			}
		],
	}
]


def _clear_model_catalog_cache():
	"""Drop both Redis keys get_model_catalog uses.

	Must run in tearDown as well as setUp. These are keys in the REAL per-site
	cache, and the success key has a 6 hour TTL, so a test that leaves the fake
	_PAYLOAD cached hands it to any later test (in this file or another module in
	the same worker) that calls get_model_catalog without mocking. Clearing only
	on the way in protects this class and poisons everyone after it.
	"""
	frappe.cache().delete_value(admin_client._MODEL_CATALOG_CACHE_KEY)
	frappe.cache().delete_value(admin_client._MODEL_CATALOG_FAIL_KEY)


class TestGetModelCatalog(FrappeTestCase):
	def setUp(self):
		_clear_model_catalog_cache()
		self.addCleanup(_clear_model_catalog_cache)

	def test_fetches_and_caches_admin_catalog(self):
		with patch.object(admin_client, "_post_guest", return_value=_PAYLOAD) as gp:
			out = admin_client.get_model_catalog()
		self.assertEqual(out, _PAYLOAD)
		gp.assert_called_once()
		self.assertIn("get_provider_catalog", gp.call_args.kwargs.get("path", ""))
		with patch.object(admin_client, "_post_guest", side_effect=AssertionError("must use cache")):
			self.assertEqual(admin_client.get_model_catalog(), _PAYLOAD)

	def test_cache_hit_short_circuits_network(self):
		frappe.cache().set_value(
			admin_client._MODEL_CATALOG_CACHE_KEY,
			_PAYLOAD,
			expires_in_sec=admin_client._MODEL_CATALOG_TTL_S,
		)
		with patch.object(admin_client, "_post_guest") as m:
			result = admin_client.get_model_catalog()
		self.assertEqual(result, _PAYLOAD)
		m.assert_not_called()

	def test_falls_back_to_bundled_when_admin_down_and_cache_empty(self):
		from jarvis._model_catalog import BUNDLED_MODEL_CATALOG

		with patch.object(admin_client, "_post_guest", side_effect=AdminUnreachableError("down")):
			out = admin_client.get_model_catalog()
		self.assertEqual(out, BUNDLED_MODEL_CATALOG)
		self.assertTrue(all("provider_id" in e and "models" in e for e in out))

	def test_never_raises_on_any_exception(self):
		from jarvis._model_catalog import BUNDLED_MODEL_CATALOG

		with patch.object(admin_client, "_post_guest", side_effect=ValueError("scheme-less url")):
			out = admin_client.get_model_catalog()
		self.assertEqual(out, BUNDLED_MODEL_CATALOG)

	def test_uses_a_short_timeout_not_the_150s_default(self):
		# R3: this runs inside send_message. DEFAULT_TIMEOUT_S is 150.
		with patch.object(admin_client, "_post_guest", return_value=_PAYLOAD) as gp:
			admin_client.get_model_catalog()
		self.assertLessEqual(gp.call_args.kwargs.get("timeout_s", 150), 10)

	def test_a_failure_is_cached_so_the_hot_path_stops_retrying(self):
		# R3: without negative caching a hanging admin costs EVERY chat send a
		# full timeout. One failure must suppress the next call entirely.
		frappe.cache().delete_value(admin_client._MODEL_CATALOG_FAIL_KEY)
		with patch.object(admin_client, "_post_guest", side_effect=AdminUnreachableError("down")):
			admin_client.get_model_catalog()
		with patch.object(admin_client, "_post_guest", side_effect=AssertionError("must not retry")) as gp:
			out = admin_client.get_model_catalog()
		gp.assert_not_called()
		from jarvis._model_catalog import BUNDLED_MODEL_CATALOG

		self.assertEqual(out, BUNDLED_MODEL_CATALOG)

	def test_an_empty_payload_also_trips_the_failure_marker(self):
		frappe.cache().delete_value(admin_client._MODEL_CATALOG_FAIL_KEY)
		with patch.object(admin_client, "_post_guest", return_value=[]):
			admin_client.get_model_catalog()
		self.assertTrue(frappe.cache().get_value(admin_client._MODEL_CATALOG_FAIL_KEY))


class TestBundledCatalogMirrorsAdminSeed(FrappeTestCase):
	def test_bundle_covers_every_provider_and_model(self):
		# R4: the bundle IS what CI and every outage sees. If it drifts from the
		# admin seed, tests pass locally (admin reachable) and fail on CI.
		from jarvis._model_catalog import BUNDLED_MODEL_CATALOG

		self.assertEqual(len(BUNDLED_MODEL_CATALOG), 15)
		by_id = {p["provider_id"]: p for p in BUNDLED_MODEL_CATALOG}
		# Spot-check the entries the older trimmed draft dropped.
		for pid in ("mistral", "groq", "together", "deepseek", "openrouter", "zai", "zai_coding"):
			self.assertIn(pid, by_id, f"bundle is missing {pid}")
		self.assertEqual(by_id["moonshot"]["subscription_label"], "Kimi (Moonshot)")
		self.assertEqual(by_id["google"]["catalog_id"], "gemini")

	def test_bundle_preserves_the_full_subscription_lists(self):
		# test_subscription_models.py asserts every active gemini model coerces.
		from jarvis._model_catalog import BUNDLED_MODEL_CATALOG

		g = next(p for p in BUNDLED_MODEL_CATALOG if p["provider_id"] == "google")
		subs = [m["model_id"] for m in g["models"] if m["tier"] == "subscription"]
		self.assertEqual(subs, ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.1-flash-lite"])

	def test_the_two_gemini_tiers_differ_on_purpose(self):
		# Not a typo and not drift. "gemini-3.1-flash" is absent from the pinned
		# cliproxy image, so it cannot be served on the subscription tier, but
		# Google's own API does serve it so the api-key tier keeps it. If a future
		# regeneration ever collapses these to the same list, one tier is wrong.
		from jarvis._model_catalog import BUNDLED_MODEL_CATALOG

		g = next(p for p in BUNDLED_MODEL_CATALOG if p["provider_id"] == "google")
		api = [m["model_id"] for m in g["models"] if m["tier"] == "api_key"]
		subs = [m["model_id"] for m in g["models"] if m["tier"] == "subscription"]
		self.assertIn("gemini-3.1-flash", api)
		self.assertNotIn("gemini-3.1-flash", subs)
		self.assertIn("gemini-3.1-flash-lite", subs)


class TestGetModelCatalogNeverRaises(FrappeTestCase):
	"""The chat hot path (send_message) assumes this cannot raise."""

	def setUp(self):
		_clear_model_catalog_cache()
		self.addCleanup(_clear_model_catalog_cache)

	def test_a_redis_read_failure_degrades_instead_of_raising(self):
		# The cache calls sit outside the inner try. Before this was guarded, a
		# transient Redis blip during a cache READ propagated out of
		# get_model_catalog into send_message, turning a hiccup into a 500 on
		# every chat send.
		#
		# Patching frappe.cache also breaks anything else that touches the cache,
		# which is the point: the guard must survive an unhealthy cache. It is
		# also why the guard logs via frappe.logger() and not frappe.log_error --
		# the latter writes an Error Log DOCUMENT and would raise from inside the
		# handler here, defeating the guard entirely. This test caught exactly
		# that.
		from jarvis._model_catalog import BUNDLED_MODEL_CATALOG

		class _BoomCache:
			def get_value(self, *a, **k):
				raise ConnectionError("redis gone")

			def set_value(self, *a, **k):
				raise ConnectionError("redis gone")

		with patch.object(admin_client.frappe, "cache", return_value=_BoomCache()):
			out = admin_client.get_model_catalog()
		self.assertEqual(out, BUNDLED_MODEL_CATALOG)

	def test_a_redis_write_failure_still_returns_the_payload_or_bundle(self):
		class _ReadOnlyCache:
			def get_value(self, *a, **k):
				return None

			def set_value(self, *a, **k):
				raise ConnectionError("redis read-only")

		with patch.object(admin_client.frappe, "cache", return_value=_ReadOnlyCache()):
			with patch.object(admin_client, "_post_guest", return_value=_PAYLOAD):
				out = admin_client.get_model_catalog()
		# The write failed, but the caller still gets a usable catalog.
		self.assertTrue(isinstance(out, list) and out)


def _clear_sub_model_cache():
	"""frappe.local survives the whole test process; FrappeTestCase does not
	reset it. A test that leaves patched rows cached poisons every later test,
	including tests in OTHER files (CI's parallel runner sorts files
	alphabetically)."""
	if hasattr(frappe.local, "_jarvis_sub_models"):
		delattr(frappe.local, "_jarvis_sub_models")


class TestSubscriptionModelsMappings(FrappeTestCase):
	def setUp(self):
		_clear_model_catalog_cache()
		_clear_sub_model_cache()
		self.addCleanup(_clear_model_catalog_cache)
		self.addCleanup(_clear_sub_model_cache)

	def test_public_names_are_still_mappings(self):
		from jarvis._subscription_models import DEFAULT_MODEL, SUBSCRIPTION_MODELS

		for m in (SUBSCRIPTION_MODELS, DEFAULT_MODEL):
			self.assertTrue(hasattr(m, "get"))
			self.assertTrue(hasattr(m, "items"))
			self.assertGreater(len(m), 0)

	def test_kimi_uses_the_subscription_label_not_the_api_key_label(self):
		# R1. oauth/api.py:_coerce_subscription_model looks up by this exact key.
		from jarvis._subscription_models import DEFAULT_MODEL, SUBSCRIPTION_MODELS

		self.assertIn("Kimi (Moonshot)", SUBSCRIPTION_MODELS)
		self.assertNotIn("Moonshot (Kimi)", SUBSCRIPTION_MODELS)
		self.assertEqual(DEFAULT_MODEL["Kimi (Moonshot)"], "kimi-k2.7-code")

	def test_keys_exactly_match_todays_hardcoded_catalogue(self):
		# The pinned regression the reviewer asked for: whatever the catalog says,
		# the KEY SET must not move, or oauth/api.py and the desk tab break.
		from jarvis._subscription_models import _SEED_SUBSCRIPTION_MODELS, SUBSCRIPTION_MODELS

		self.assertEqual(set(SUBSCRIPTION_MODELS), set(_SEED_SUBSCRIPTION_MODELS))

	def test_reads_values_from_the_catalog(self):
		from jarvis import _subscription_models

		payload = [
			{
				"provider_id": "openai",
				"label": "OpenAI",
				"subscription_label": "OpenAI",
				"supports_subscription": True,
				"models": [
					{
						"model_id": "gpt-9.9",
						"label": "gpt-9.9",
						"tier": "subscription",
						"is_default": True,
						"sort_order": 0,
					}
				],
			}
		]
		with patch.object(admin_client, "get_model_catalog", return_value=payload):
			_clear_sub_model_cache()
			self.assertEqual(_subscription_models.SUBSCRIPTION_MODELS["OpenAI"], ["gpt-9.9"])
			self.assertEqual(_subscription_models.DEFAULT_MODEL["OpenAI"], "gpt-9.9")

	def test_api_key_only_provider_is_excluded(self):
		from jarvis import _subscription_models

		payload = [
			{
				"provider_id": "anthropic",
				"label": "Anthropic",
				"supports_subscription": False,
				"models": [
					{
						"model_id": "claude-opus-4-8",
						"label": "claude-opus-4-8",
						"tier": "api_key",
						"is_default": True,
						"sort_order": 0,
					}
				],
			}
		]
		with patch.object(admin_client, "get_model_catalog", return_value=payload):
			_clear_sub_model_cache()
			self.assertNotIn("Anthropic", _subscription_models.SUBSCRIPTION_MODELS)

	def test_default_falls_back_to_first_row_when_none_flagged(self):
		from jarvis import _subscription_models

		payload = [
			{
				"provider_id": "openai",
				"label": "OpenAI",
				"supports_subscription": True,
				"models": [
					{
						"model_id": "gpt-5.4",
						"label": "gpt-5.4",
						"tier": "subscription",
						"is_default": False,
						"sort_order": 0,
					}
				],
			}
		]
		with patch.object(admin_client, "get_model_catalog", return_value=payload):
			_clear_sub_model_cache()
			self.assertEqual(_subscription_models.DEFAULT_MODEL["OpenAI"], "gpt-5.4")


class TestChatUiSettingsServesApiKeyModels(FrappeTestCase):
	def setUp(self):
		_clear_model_catalog_cache()
		self.addCleanup(_clear_model_catalog_cache)

	def test_response_carries_api_key_models(self):
		from jarvis.chat.api import get_chat_ui_settings

		payload = [
			{
				"provider_id": "anthropic",
				"label": "Anthropic",
				"supports_subscription": False,
				"models": [
					{
						"model_id": "claude-opus-4-8",
						"label": "claude-opus-4-8",
						"tier": "api_key",
						"is_default": True,
						"sort_order": 0,
					},
					{
						"model_id": "claude-sonnet-4-6",
						"label": "claude-sonnet-4-6",
						"tier": "api_key",
						"is_default": False,
						"sort_order": 1,
					},
				],
			}
		]
		with patch.object(admin_client, "get_model_catalog", return_value=payload):
			out = get_chat_ui_settings()
		self.assertIn("api_key_models", out)
		self.assertEqual(
			[m["model_id"] for m in out["api_key_models"]["Anthropic"]],
			["claude-opus-4-8", "claude-sonnet-4-6"],
		)

	def test_subscription_models_serialises_as_a_JSON_OBJECT_not_an_array(self):
		# R9 regression lock. A collections.abc.Mapping is Iterable, and frappe's
		# json_handler (frappe/utils/response.py:238) tests Iterable BEFORE dict,
		# so an unwrapped Mapping silently serialises to its KEYS: the response
		# would carry ["OpenAI", ...] instead of {"OpenAI": [...], ...} with no
		# error raised. chat/api.py must wrap these in dict().
		import orjson
		from frappe.utils.response import json_handler

		from jarvis.chat.api import get_chat_ui_settings

		with patch.object(admin_client, "get_model_catalog", return_value=_PAYLOAD):
			out = get_chat_ui_settings()
		for key in ("subscription_models", "default_models", "api_key_models"):
			self.assertIsInstance(out[key], dict, f"{key} must be a real dict before serialisation")
			decoded = orjson.loads(orjson.dumps(out[key], default=json_handler))
			self.assertIsInstance(decoded, dict, f"{key} serialised to a non-object")

	def test_api_key_models_excludes_subscription_rows(self):
		from jarvis.chat.api import get_chat_ui_settings

		with patch.object(admin_client, "get_model_catalog", return_value=_PAYLOAD):
			out = get_chat_ui_settings()
		# _PAYLOAD's only model is subscription-tier, so OpenAI has no api-key rows.
		self.assertEqual(out["api_key_models"].get("OpenAI", []), [])


class TestModelCatalogUiEndpoint(FrappeTestCase):
	def setUp(self):
		_clear_model_catalog_cache()
		_clear_sub_model_cache()
		self.addCleanup(_clear_model_catalog_cache)
		self.addCleanup(_clear_sub_model_cache)

	def test_returns_the_three_catalog_slices_as_json_objects(self):
		import orjson
		from frappe.utils.response import json_handler

		from jarvis.chat.api import get_model_catalog_ui

		with patch.object(admin_client, "get_model_catalog", return_value=_PAYLOAD):
			out = get_model_catalog_ui()
		for key in ("api_key_models", "subscription_models", "default_models"):
			self.assertIn(key, out)
			# R9: a bare Mapping would serialise to its KEYS with no error.
			self.assertIsInstance(out[key], dict)
			self.assertIsInstance(orjson.loads(orjson.dumps(out[key], default=json_handler)), dict)

	def test_kimi_is_keyed_by_its_subscription_label(self):
		from jarvis.chat.api import get_model_catalog_ui

		payload = [
			{
				"provider_id": "moonshot",
				"label": "Moonshot (Kimi)",
				"subscription_label": "Kimi (Moonshot)",
				"supports_subscription": True,
				"models": [
					{
						"model_id": "kimi-k2.7-code",
						"label": "kimi-k2.7-code",
						"tier": "subscription",
						"is_default": True,
						"sort_order": 0,
					}
				],
			}
		]
		with patch.object(admin_client, "get_model_catalog", return_value=payload):
			out = get_model_catalog_ui()
		self.assertIn("Kimi (Moonshot)", out["subscription_models"])
		self.assertNotIn("Moonshot (Kimi)", out["subscription_models"])

	def test_connect_providers_gate_on_auth_profile_id_not_supports_subscription(self):
		# R7: supports_subscription is true for xai and moonshot too (cliproxy
		# really does serve their models), but only openai/google support the
		# paste-back connect card. Gating on supports_subscription would render
		# a connect button that can never succeed.
		from jarvis.chat.api import get_model_catalog_ui

		payload = [
			{
				"provider_id": "openai",
				"label": "OpenAI",
				"auth_profile_id": "openai",
				"supports_subscription": True,
				"models": [
					{
						"model_id": "gpt-5.5",
						"label": "gpt-5.5",
						"tier": "subscription",
						"is_default": True,
						"sort_order": 0,
					}
				],
			},
			{
				"provider_id": "xai",
				"label": "xAI Grok",
				"auth_profile_id": "",
				"supports_subscription": True,
				"models": [
					{
						"model_id": "grok-4.3",
						"label": "grok-4.3",
						"tier": "subscription",
						"is_default": True,
						"sort_order": 0,
					}
				],
			},
		]
		with patch.object(admin_client, "get_model_catalog", return_value=payload):
			out = get_model_catalog_ui()
		providers = {p["provider"] for p in out["subscription_connect_providers"]}
		self.assertIn("OpenAI", providers)
		self.assertNotIn("xAI Grok", providers)
