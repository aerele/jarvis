import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client, catalog_store
from jarvis.catalog_store import MODELS, SNAPSHOT_DT
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
	"""Drop the model catalog's Redis copy so reads fall through to the snapshot.

	Runs in tearDown as well as setUp: the key lives in the REAL per-site cache, so
	a test that leaves a fake payload cached hands it to later tests in any module.
	"""
	frappe.cache().delete_value(MODELS.cache_key)
	frappe.cache().delete_value(f"{MODELS.cache_key}:failed")


def _snapshot_models() -> list:
	return json.loads(frappe.db.get_single_value(SNAPSHOT_DT, "model_catalog") or "[]")


class TestCatalogRead(FrappeTestCase):
	"""read() is the hot path: it must never call the admin or write the DB."""

	def setUp(self):
		_clear_model_catalog_cache()
		self.addCleanup(_clear_model_catalog_cache)

	def test_redis_hit_is_returned_without_touching_the_admin(self):
		frappe.cache().set_value(MODELS.cache_key, _PAYLOAD, expires_in_sec=60)
		with patch.object(admin_client, "_post_guest", side_effect=AssertionError("no admin")):
			self.assertEqual(admin_client.get_model_catalog(), _PAYLOAD)

	def test_redis_miss_reads_the_snapshot_and_refills_redis(self):
		with patch.object(admin_client, "_post_guest", side_effect=AssertionError("no admin")):
			out = admin_client.get_model_catalog()
		self.assertTrue(out)
		self.assertEqual(out, _snapshot_models())
		self.assertEqual(frappe.cache().get_value(MODELS.cache_key), out)

	def test_no_redis_and_no_snapshot_is_an_empty_catalog(self):
		# Only a site that never reached the admin gets here; callers treat [] as
		# "catalog unavailable".
		with patch.object(MODELS, "_load_snapshot", return_value=[]):
			self.assertEqual(admin_client.get_model_catalog(), [])

	def test_a_redis_failure_still_serves_the_snapshot(self):
		# send_message calls this: a Redis blip must not become a 500. The guard
		# logs through frappe.logger, since log_error needs a healthy cache and DB.
		class _BoomCache:
			def get_value(self, *a, **k):
				raise ConnectionError("redis gone")

			set_value = get_value

		with patch.object(catalog_store.frappe, "cache", return_value=_BoomCache()):
			# Redis is down but the snapshot is fine: the snapshot is still served.
			self.assertEqual(admin_client.get_model_catalog(), _snapshot_models())
		with (
			patch.object(catalog_store.frappe, "cache", return_value=_BoomCache()),
			patch.object(MODELS, "_load_snapshot", side_effect=RuntimeError("db gone")),
		):
			self.assertEqual(admin_client.get_model_catalog(), [])


class TestCatalogRefresh(FrappeTestCase):
	def setUp(self):
		_clear_model_catalog_cache()
		self.addCleanup(_clear_model_catalog_cache)
		frappe.db.savepoint("catalog_refresh_test")
		self.addCleanup(frappe.db.rollback, save_point="catalog_refresh_test")

	def test_a_successful_fetch_updates_redis_and_the_snapshot(self):
		with patch.object(admin_client, "_post_guest", return_value=_PAYLOAD) as gp:
			out = MODELS.refresh()
		self.assertEqual(out, _PAYLOAD)
		self.assertIn("get_provider_catalog", gp.call_args.kwargs["path"])
		self.assertEqual(frappe.cache().get_value(MODELS.cache_key), _PAYLOAD)
		self.assertEqual(_snapshot_models(), _PAYLOAD)
		self.assertTrue(frappe.db.get_single_value(SNAPSHOT_DT, "model_catalog_fetched_at"))

	def test_a_dict_envelope_is_unwrapped(self):
		with patch.object(admin_client, "_post_guest", return_value={"data": _PAYLOAD}):
			self.assertEqual(MODELS.refresh(), _PAYLOAD)

	def test_uses_a_short_timeout(self):
		with patch.object(admin_client, "_post_guest", return_value=_PAYLOAD) as gp:
			MODELS.refresh()
		self.assertLessEqual(gp.call_args.kwargs["timeout_s"], 10)

	def test_admin_down_keeps_serving_the_snapshot(self):
		before = _snapshot_models()
		with patch.object(admin_client, "_post_guest", side_effect=AdminUnreachableError("down")):
			out = MODELS.refresh()
		self.assertEqual(out, before)
		self.assertEqual(_snapshot_models(), before)

	def test_a_failed_fetch_backs_off_but_a_forced_retry_does_not(self):
		with patch.object(admin_client, "_post_guest", side_effect=AdminUnreachableError("down")):
			MODELS.refresh()
		with patch.object(admin_client, "_post_guest", side_effect=AssertionError("backing off")):
			self.assertEqual(MODELS.refresh(), _snapshot_models())
		with patch.object(admin_client, "_post_guest", return_value=_PAYLOAD) as gp:
			self.assertEqual(MODELS.refresh(force=True), _PAYLOAD)
		gp.assert_called_once()

	def test_any_exception_is_contained(self):
		# A scheme-less admin URL raises requests.MissingSchema, not an Admin* error.
		with patch.object(admin_client, "_post_guest", side_effect=ValueError("scheme-less url")):
			self.assertEqual(MODELS.refresh(), _snapshot_models())

	def test_an_empty_answer_never_blanks_the_snapshot(self):
		before = _snapshot_models()
		with patch.object(admin_client, "_post_guest", return_value=[]):
			self.assertEqual(MODELS.refresh(), before)
		self.assertEqual(_snapshot_models(), before)

	def test_an_unchanged_catalog_is_not_rewritten(self):
		with patch.object(admin_client, "_post_guest", return_value=_PAYLOAD):
			MODELS.refresh()
		with (
			patch.object(admin_client, "_post_guest", return_value=_PAYLOAD),
			patch.object(catalog_store.frappe.db, "set_single_value") as write,
		):
			MODELS.refresh()
		(values,) = [c.args[1] for c in write.call_args_list]
		self.assertEqual(list(values), ["model_catalog_fetched_at"])

	def test_refresh_all_covers_models_and_presets(self):
		with (
			patch.object(catalog_store.MODELS, "refresh") as models,
			patch.object(catalog_store.PRESETS, "refresh") as presets,
		):
			catalog_store.refresh_all()
		models.assert_called_once()
		presets.assert_called_once()


class TestProviderWiringMatchesTheCatalog(FrappeTestCase):
	"""pool_serialize keeps two provider facts as reviewed code. Drift from the
	admin seed (mirrored by the fixture) must fail here, not in a customer's save."""

	def test_renderer_and_base_url_tables_match_the_fixture(self):
		from jarvis.jarvis import pool_serialize as ps
		from jarvis.tests.fixtures.model_catalog import MODEL_CATALOG

		with_renderer = {p["provider_id"] for p in MODEL_CATALOG if (p.get("renderer_id") or "").strip()}
		self.assertEqual(set(ps._RENDERER_UPSTREAMS), with_renderer)
		by_id = {p["provider_id"]: p.get("default_base_url") for p in MODEL_CATALOG}
		for provider, url in ps._BASE_URL_BACKFILL.items():
			self.assertEqual(url, by_id[provider])


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
		from jarvis._subscription_models import SUBSCRIPTION_MODELS
		from jarvis.tests.fixtures.model_catalog import MODEL_CATALOG

		expected = {
			p.get("subscription_label") or p["label"]
			for p in MODEL_CATALOG
			if any(m["tier"] == "subscription" for m in p["models"])
		}
		self.assertEqual(expected, {"OpenAI", "Anthropic", "xAI Grok", "Kimi (Moonshot)"})
		self.assertEqual(set(SUBSCRIPTION_MODELS), expected)

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
				"provider_id": "openai",
				"label": "OpenAI",
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
			},
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
			},
		]
		with patch.object(admin_client, "get_model_catalog", return_value=payload):
			_clear_sub_model_cache()
			self.assertIn("OpenAI", _subscription_models.SUBSCRIPTION_MODELS)
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
		import json

		from frappe.utils.response import json_handler

		from jarvis.chat.api import get_chat_ui_settings
		from jarvis.tests import dumps_like_response

		with patch.object(admin_client, "get_model_catalog", return_value=_PAYLOAD):
			out = get_chat_ui_settings()
		for key in ("subscription_models", "default_models", "api_key_models"):
			self.assertIsInstance(out[key], dict, f"{key} must be a real dict before serialisation")
			decoded = json.loads(dumps_like_response(out[key], json_handler))
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
		# The endpoint fetches the admin live first; CI has no admin to answer.
		refresh = patch.object(catalog_store.MODELS, "refresh", return_value=_PAYLOAD)
		refresh.start()
		self.addCleanup(refresh.stop)

	def test_reports_availability_and_catalog_base_urls(self):
		from jarvis.chat.api import get_model_catalog_ui

		with patch.object(catalog_store.MODELS, "refresh", return_value=_PAYLOAD):
			out = get_model_catalog_ui()
		self.assertTrue(out["catalog_available"])
		self.assertEqual(out["provider_base_urls"], {"OpenAI": "https://api.openai.com/v1"})

	def test_an_unavailable_catalog_is_reported_not_hidden(self):
		from jarvis.chat.api import get_model_catalog_ui

		with patch.object(catalog_store.MODELS, "refresh", return_value=[]):
			out = get_model_catalog_ui()
		self.assertFalse(out["catalog_available"])
		self.assertEqual(out["subscription_models"], {})

	def test_returns_the_three_catalog_slices_as_json_objects(self):
		import json

		from frappe.utils.response import json_handler

		from jarvis.chat.api import get_model_catalog_ui
		from jarvis.tests import dumps_like_response

		with patch.object(catalog_store.MODELS, "refresh", return_value=_PAYLOAD):
			out = get_model_catalog_ui()
		for key in ("api_key_models", "subscription_models", "default_models"):
			self.assertIn(key, out)
			# R9: a bare Mapping would serialise to its KEYS with no error.
			self.assertIsInstance(out[key], dict)
			self.assertIsInstance(json.loads(dumps_like_response(out[key], json_handler)), dict)

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
		with patch.object(catalog_store.MODELS, "refresh", return_value=payload):
			out = get_model_catalog_ui()
		self.assertIn("Kimi (Moonshot)", out["subscription_models"])
		self.assertNotIn("Moonshot (Kimi)", out["subscription_models"])

	def test_connect_providers_gate_on_auth_profile_id_not_supports_subscription(self):
		# R7: supports_subscription is true for xai and moonshot too (cliproxy
		# really does serve their models), but only a provider with a real
		# auth_profile_id supports the paste-back connect card. Gating on
		# supports_subscription would render a connect button that can never
		# succeed. Google used to be the other auth_profile_id-bearing example
		# here; its chat subscription was removed 2026-08-19 (Google
		# discontinued login-with-Google for Gemini), so its auth_profile_id is
		# now empty too and it must be excluded exactly like xai.
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
			{
				"provider_id": "google",
				"label": "Google Gemini",
				"auth_profile_id": "",
				"supports_subscription": False,
				"models": [
					{
						"model_id": "gemini-3.6-flash",
						"label": "gemini-3.6-flash",
						"tier": "api_key",
						"is_default": True,
						"sort_order": 0,
					}
				],
			},
		]
		with patch.object(catalog_store.MODELS, "refresh", return_value=payload):
			out = get_model_catalog_ui()
		providers = {p["provider"] for p in out["subscription_connect_providers"]}
		self.assertIn("OpenAI", providers)
		self.assertNotIn("xAI Grok", providers)
		self.assertNotIn("Google Gemini", providers)

	def test_lists_come_from_the_fetched_catalog_even_if_caching_failed(self):
		# Review finding: the flag came from refresh() while the lists re-read
		# Redis/the snapshot, so a failed cache write gave "available" + empty lists.
		from jarvis.chat.api import get_model_catalog_ui

		with patch.object(admin_client, "get_model_catalog", return_value=[]):
			out = get_model_catalog_ui()
		self.assertTrue(out["catalog_available"])
		self.assertEqual(out["subscription_models"], {"OpenAI": ["gpt-5.5"]})
		self.assertEqual(out["default_models"], {"OpenAI": "gpt-5.5"})
		self.assertEqual([p["provider"] for p in out["subscription_connect_providers"]], ["OpenAI"])


class TestCatalogRefreshIsScheduled(FrappeTestCase):
	def test_refresh_runs_hourly_after_migrate_and_install(self):
		from jarvis import hooks

		self.assertIn("jarvis.catalog_store.refresh_all", hooks.scheduler_events["hourly"])
		self.assertIn("jarvis.catalog_store.enqueue_refresh_all", hooks.after_migrate)
		self.assertEqual(hooks.before_tests, "jarvis.tests.catalog_seed.seed_catalog_snapshot")
		with open(frappe.get_app_path("jarvis", "install.py")) as f:
			self.assertIn("enqueue_refresh_all()", f.read())
