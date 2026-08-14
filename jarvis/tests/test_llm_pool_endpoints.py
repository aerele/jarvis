from unittest.mock import patch

import frappe

from jarvis import admin_client, onboarding
from jarvis.tests.test_settings_on_update import _reset_settings
from jarvis.tests.test_unified_llm_config import _RT3SettingsTestCase

_CATALOG = [
	{
		"key": "cost-saver",
		"label": "Cost-saver",
		"kind": "cross_vendor",
		"blurb": "",
		"enabled": True,
		"vendors": ["openai"],
		"models": [],
	}
]


class TestSaveLlmPool(_RT3SettingsTestCase):
	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		s = frappe.get_single("Jarvis Settings")
		s.db_set("preset", "", update_modified=False)
		s.db_set("routing_mode", "failover", update_modified=False)
		s.db_set("proxy_active", 0, update_modified=False)
		# jarvis#715's non-retroactivity gate keys the lone-subscription direct
		# leg off "has this workspace ever synced through the pool leg" - clear
		# both apply markers so these tests see a fresh-connect workspace
		# regardless of what an earlier test in this (shared, DB-polluting)
		# site left behind.
		s.db_set("llm_pool_synced_at", None, update_modified=False)
		s.db_set("llm_direct_synced_at", None, update_modified=False)
		frappe.db.commit()

	def test_two_models_writes_rows_and_routes_to_the_pool(self):
		models = [
			{
				"provider": "openai",
				"model": "gpt-5.5",
				"api_key": "sk-a",
				"base_url": "",
				"tier": "strong",
				"order": 0,
			},
			{
				"provider": "openai",
				"model": "gpt-5.4",
				"api_key": "sk-b",
				"base_url": "",
				"tier": "strong",
				"order": 1,
			},
		]
		pool_calls = []
		with (
			patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: pool_calls.append(kw) or {"action": "pool_update"},
			),
			patch("jarvis.admin_client.post_update_llm_creds") as creds,
		):
			out = onboarding.save_llm_pool(frappe.as_json(models), preset=None, routing_mode="failover")
		self.assertTrue(pool_calls, "the /llm-pool path must fire for >=2 models")
		creds.assert_not_called()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(len(s.get("models")), 2)
		self.assertEqual(s.models[0].get_password("api_key"), "sk-a")
		# Pool, but an agent-DIRECT one: two BYO api keys get no sidecar, so
		# proxy_active stays 0 while the config still syncs through /llm-pool.
		self.assertEqual(int(s.proxy_active or 0), 0)
		self.assertEqual(s.routing_mode, "failover")
		self.assertIn("last_sync_status", out)

	def test_one_model_no_preset_is_direct(self):
		models = [
			{
				"provider": "openai",
				"model": "gpt-5.5",
				"api_key": "sk-x",
				"base_url": "",
				"tier": "strong",
				"order": 0,
			}
		]
		with (
			patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}),
			patch("jarvis.admin_client.post_update_llm_pool") as pool,
		):
			onboarding.save_llm_pool(frappe.as_json(models))
		pool.assert_not_called()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(int(s.proxy_active or 0), 0)

	def _lone_subscription_models(self, upstream="openai", provider="openai", blob=None):
		return [
			{
				# jarvis#756: the SPA's own validatePool gate refuses a
				# provider-less subscription row client-side, so a real save
				# always carries this - on_update mirrors it verbatim into
				# self.llm_provider, which admin-v2's subscription_connect
				# (jarvis_admin_v2 branch feat/subscription-connect-relay)
				# requires.
				"provider": provider,
				"model": "gpt-5.5",
				"tier": "strong",
				"order": 0,
				"subscription": {
					"rotation": "sticky",
					"accounts": [
						{
							"upstream": upstream,
							"account_ref": "SUB_deadbeef",
							"label": "me@x.com",
							"oauth_blob": blob or ('{"provider":"%s","refresh_token":"rt"}' % provider),
						},
					],
				},
			}
		]

	def test_one_renderable_subscription_model_is_direct(self):
		"""jarvis#715: a FRESH lone subscription on a provider agent serves
		NATIVELY (openai) needs no sidecar at all - it takes the direct
		leg, one subscription_connect call carrying its own oauth blob, not
		/llm-pool."""
		with (
			patch("jarvis.admin_client.post_update_llm_pool") as pool,
			patch(
				"jarvis.admin_client.post_subscription_connect",
				return_value={"action": "restart", "status": "applied"},
			) as connect,
		):
			onboarding.save_llm_pool(
				frappe.as_json(self._lone_subscription_models()), preset=None, routing_mode="failover"
			)
		pool.assert_not_called()
		connect.assert_called_once()
		provider, _blob, llm_provider = connect.call_args.args
		self.assertEqual(provider, "openai")
		self.assertEqual(llm_provider, "openai")
		self.assertEqual(connect.call_args.kwargs.get("model"), "gpt-5.5")
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(int(s.proxy_active or 0), 0)

	def test_lone_direct_subscription_resolves_its_own_agent_provider(self):
		"""jarvis#755 critical fix: turn_handler's provider resolution used to
		gate on ``llm_auth_mode == "oauth"`` only, so every message from a
		subscription-direct tenant resolved provider=None - agent then either
		502s ("No API key found for provider ...") or silently mis-routes.
		The lone account's own oauth_blob already carries the agent-provider id
		under its "provider" key (the same key ``_direct_subscription_blob``
		relies on), so the turn dispatcher must read it from there."""
		from jarvis.chat.turn_handler import _resolve_model_and_provider

		with patch("jarvis.admin_client.post_subscription_connect", return_value={"action": "restart"}):
			onboarding.save_llm_pool(
				frappe.as_json(self._lone_subscription_models()), preset=None, routing_mode="failover"
			)
		conv = frappe._dict(model_override="")
		effective_model, provider = _resolve_model_and_provider(conv)
		self.assertEqual(provider, "openai")
		self.assertEqual(effective_model, "gpt-5.5")

	def test_lone_direct_subscription_with_a_malformed_blob_resolves_no_provider(self):
		"""A blob missing its "provider" key (or otherwise unreadable) must
		degrade to None, never raise or crash the turn - see
		``_direct_subscription_blob``'s own guard for the write side of this
		same invariant."""
		from jarvis.chat.turn_handler import _resolve_model_and_provider

		with patch("jarvis.admin_client.post_subscription_connect", return_value={"action": "restart"}):
			onboarding.save_llm_pool(
				frappe.as_json(self._lone_subscription_models(blob='{"refresh_token":"rt"}')),
				preset=None,
				routing_mode="failover",
			)
		conv = frappe._dict(model_override="")
		_, provider = _resolve_model_and_provider(conv)
		self.assertIsNone(provider)

	def test_direct_subscription_connect_validation_error_surfaces_specifically(self):
		"""jarvis#755 review: _sync_via_admin had no except clause for
		AdminValidationError, so a concrete rejection reason fell through to
		the generic "failed: unexpected error; see Error Log" backstop -
		exactly the failure this exception exists to name. Mirrors the
		pool-sync twin's own handling of the same exception (jarvis_settings.py
		~1975).

		Also the DISCRIMINATOR for the deploy-window fallback (see
		test_subscription_connect_method_not_found_falls_back_to_two_calls
		below): a plain business rejection from the new endpoint is NOT the
		"no such method" signal and must surface directly, never trigger the
		old two-call fallback."""
		from jarvis import admin_client

		with (
			patch(
				"jarvis.admin_client.post_subscription_connect",
				side_effect=admin_client.AdminValidationError("missing or malformed oauth_blob"),
			) as connect,
			patch("jarvis.admin_client.post_push_oauth_blob") as blob,
			patch("jarvis.admin_client.post_update_llm_creds") as creds,
		):
			onboarding.save_llm_pool(
				frappe.as_json(self._lone_subscription_models()), preset=None, routing_mode="failover"
			)
		connect.assert_called_once()
		blob.assert_not_called()
		creds.assert_not_called()
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(s.last_sync_status.startswith("failed: validation:"))
		self.assertIn("missing or malformed oauth_blob", s.last_sync_status)

	def test_subscription_connect_method_not_found_falls_back_to_two_calls(self):
		"""TODO(delete-me after admin-v2 subscription_connect deploys) deploy-window
		coverage: an admin build that has not shipped ``subscription_connect``
		yet answers with Frappe's own "no such whitelisted method" rejection
		(handler.py's ``execute_cmd`` -> ``get_attr`` failure, wrapped in
		``frappe.throw``). That must fall back to the old push-blob-then-creds
		sequence, in that order, rather than surface as a broken subscription
		connect. The blob push now goes through ``self._push_direct_subscription_blob``
		(the same wrapper the pre-collapse code used) rather than an inlined
		call, but the observable effect - a ``post_push_oauth_blob`` call before
		``post_update_llm_creds`` - is unchanged, so the assertions below still
		verify it."""
		from jarvis import admin_client

		calls = []
		method_not_found = admin_client.AdminValidationError(
			"Failed to get method for command jarvis_admin_v2.api.tenant.subscription_connect "
			"with module 'jarvis_admin_v2.api.tenant' has no attribute 'subscription_connect'",
			exc_type="ValidationError",
		)
		with (
			patch("jarvis.admin_client.post_subscription_connect", side_effect=method_not_found) as connect,
			patch(
				"jarvis.admin_client.post_push_oauth_blob",
				side_effect=lambda provider, blob: calls.append(("blob", provider, blob)) or {},
			) as blob,
			patch(
				"jarvis.admin_client.post_update_llm_creds",
				side_effect=lambda **kw: calls.append(("creds", kw))
				or {"action": "restart", "status": "applied"},
			) as creds,
		):
			onboarding.save_llm_pool(
				frappe.as_json(self._lone_subscription_models()), preset=None, routing_mode="failover"
			)
		connect.assert_called_once()
		blob.assert_called_once()
		creds.assert_called_once()
		self.assertEqual([c[0] for c in calls], ["blob", "creds"], "blob must push before creds")
		self.assertEqual(calls[0][1], "openai")
		self.assertEqual(calls[1][1].get("auth_mode"), "oauth")
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(s.last_sync_status.startswith("ok"), f"expected ok, got {s.last_sync_status!r}")

	def test_blank_llm_provider_is_a_clean_local_rejection(self):
		"""admin's subscription_connect REQUIRES llm_provider and 400s
		(ProviderMismatch) on a blank one. A provider-less subscription row is
		refused client-side by the SPA's own validatePool gate (jarvis#756),
		but nothing enforces that server-side, so a non-SPA caller of
		save_llm_pool can still reach this leg with one. That must surface as
		a clean local rejection - never an admin round trip, never the
		method-not-found fallback."""
		models = self._lone_subscription_models()
		del models[0]["provider"]
		with (
			patch("jarvis.admin_client.post_subscription_connect") as connect,
			patch("jarvis.admin_client.post_push_oauth_blob") as blob,
			patch("jarvis.admin_client.post_update_llm_creds") as creds,
		):
			onboarding.save_llm_pool(frappe.as_json(models), preset=None, routing_mode="failover")
		connect.assert_not_called()
		blob.assert_not_called()
		creds.assert_not_called()
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(s.last_sync_status.startswith("failed: validation:"))
		self.assertIn("missing llm_provider", s.last_sync_status)

	def test_one_unrenderable_subscription_model_is_still_proxy(self):
		"""Kimi has no agent-native auth flow → still needs cliproxy → the
		proxy pool path, NOT the direct llm-creds path (#200 review #1)."""
		pool_calls = []
		with (
			patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: pool_calls.append(kw) or {"action": "pool_update"},
			),
			patch("jarvis.admin_client.post_push_oauth_blob") as blob,
			patch("jarvis.admin_client.post_update_llm_creds") as creds,
		):
			onboarding.save_llm_pool(
				frappe.as_json(self._lone_subscription_models(upstream="kimi", provider="kimi")),
				preset=None,
				routing_mode="failover",
			)
		self.assertTrue(pool_calls, "a lone Kimi subscription must still route to the proxy pool path")
		blob.assert_not_called()
		creds.assert_not_called()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(int(s.proxy_active or 0), 1)
		# The account's blob reaches the pool push (served via cliproxy).
		self.assertEqual(
			pool_calls[0]["oauth_blobs"].get("SUB_deadbeef"), {"provider": "kimi", "refresh_token": "rt"}
		)

	def test_legacy_preset_value_normalized_by_patch(self):
		"""A legacy capitalized Select value is mapped to its lowercase catalog
		key so the next save_llm_pool doesn't raise 'unknown preset'. #200 #12."""
		from jarvis.patches.v1_6_normalize_llm_preset_value import execute as normalize_preset

		s = frappe.get_single("Jarvis Settings")
		s.db_set("preset", "Balanced", update_modified=False)
		normalize_preset()
		s.reload()
		self.assertEqual(s.preset, "balanced")

	def _two_models(self):
		return [
			{
				"provider": "openai",
				"model": "gpt-5.5",
				"api_key": "sk-a",
				"base_url": "",
				"tier": "strong",
				"order": 0,
			},
			{
				"provider": "openai",
				"model": "gpt-5.4",
				"api_key": "sk-b",
				"base_url": "",
				"tier": "strong",
				"order": 1,
			},
		]

	def test_pool_sync_retries_transient_unreachable_then_succeeds(self):
		"""A transient admin/agent 502 (AdminUnreachableError) on the first push
		is retried; the second succeeds → status 'ok'. #onboarding-hardening."""
		calls = []

		def _flaky(**kw):
			calls.append(kw)
			if len(calls) == 1:
				raise admin_client.AdminUnreachableError("admin returned a 502: agent_error")
			return {"action": "pool_update"}

		with (
			patch("jarvis.admin_client.post_update_llm_pool", side_effect=_flaky),
			patch("jarvis.admin_client.post_update_llm_creds"),
		):
			onboarding.save_llm_pool(frappe.as_json(self._two_models()), preset=None, routing_mode="failover")
		self.assertEqual(len(calls), 2, "should retry once after a transient unreachable")
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(s.last_sync_status.startswith("ok"), f"expected ok, got {s.last_sync_status!r}")

	def test_pool_sync_gives_up_after_bounded_retries(self):
		"""Persistent unreachable → the SHORT synchronous descriptor-obtain does ONE
		call and hands off (plan-05 D2 F2/F3), then the async worker runs its bounded
		retry budget (no infinite loop) and F2 convergence takes over: an
		unreachable/timeout is NOT a lost apply (admin persists desired-first and
		reconciles it), so the outcome is PENDING, not a terminal 'failed'. A
		get_connection probe that is not yet Ready leaves the pending marker for the
		*/5 safety net."""
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import _POOL_SYNC_RETRIES

		with (
			patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=admin_client.AdminUnreachableError("down"),
			) as m,
			patch("jarvis.admin_client.post_update_llm_creds"),
			patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Configuring"}),
		):
			onboarding.save_llm_pool(frappe.as_json(self._two_models()), preset=None, routing_mode="failover")
		# 1 short synchronous obtain (unreachable) + the async worker's bounded retries.
		self.assertEqual(m.call_count, 1 + _POOL_SYNC_RETRIES)
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(
			(s.last_sync_status or "").startswith("pending: admin applying"),
			f"unreachable must converge to pending, not failed; got {s.last_sync_status!r}",
		)

	def test_preset_validated_against_catalog(self):
		models = [
			{
				"provider": "openai",
				"model": "gpt-5.5",
				"api_key": "sk-x",
				"base_url": "",
				"tier": "strong",
				"order": 0,
			}
		]
		with patch("jarvis.admin_client.get_preset_catalog", return_value=_CATALOG):
			with self.assertRaises(frappe.ValidationError):
				onboarding.save_llm_pool(frappe.as_json(models), preset="does-not-exist")

	def test_non_failover_routing_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			onboarding.save_llm_pool(frappe.as_json([{"model": "m"}]), routing_mode="dynamic")

	def test_blank_key_surfaces_validation_error_from_pipeline(self):
		models = [
			{"provider": "openai", "model": "gpt-5.5", "api_key": "", "order": 0},
			{"provider": "openai", "model": "gpt-5.4", "api_key": "sk-b", "order": 1},
		]
		with patch("jarvis.admin_client.post_update_llm_pool") as pool:
			with self.assertRaises(frappe.ValidationError):
				onboarding.save_llm_pool(frappe.as_json(models))
		pool.assert_not_called()  # on_update validate_models throws before enqueue

	def test_glm_zai_round_trips_as_first_class_provider(self):
		"""Regression test for the bug where a GLM / Z.ai row permanently stored
		(and re-rendered) as "OpenAI-Compatible": saving "GLM / Z.ai" must
		round-trip through save_llm_pool -> Jarvis Settings -> get_llm_config
		as its own "zai" id, not collapse into a different provider's id.
		model + base_url already survived the old bug; provider is the fix.
		The wire payload's separate collapse (zai -> openai_compat, so Bifrost
		- which has no native zai provider - still gets a working config) is
		covered by test_unified_llm_config.py's TestProviderNormalization and
		is unaffected by this test."""
		models = [
			{
				"provider": "GLM / Z.ai",
				"model": "glm-4.6",
				"api_key": "zk",
				"base_url": "https://api.z.ai/api/paas/v4",
				"tier": "strong",
				"order": 0,
			}
		]
		with (
			patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}),
			patch("jarvis.admin_client.post_update_llm_pool") as pool,
		):
			onboarding.save_llm_pool(frappe.as_json(models))
		pool.assert_not_called()  # single model, no preset -> DIRECT path
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			s.models[0].provider,
			"zai",
			"GLM / Z.ai must be stored as its own id, not collapsed to openai_compat",
		)
		self.assertEqual(s.models[0].base_url, "https://api.z.ai/api/paas/v4")
		cfg = onboarding.get_llm_config()
		self.assertEqual(cfg["models"][0]["provider"], "zai")
		self.assertEqual(cfg["models"][0]["base_url"], "https://api.z.ai/api/paas/v4")

	def test_glm_coding_plan_round_trips_as_its_own_distinct_provider(self):
		"""Same round-trip guarantee as the standard GLM row, for the Coding
		Plan variant added after live discovery that a Coding Plan key
		reports "insufficient balance" on the pay-as-you-go endpoint. Must
		store/read back as "zai_coding" - never collapsed onto "zai" (the
		two are separate z.ai products with separate balances)."""
		models = [
			{
				"provider": "GLM / Z.ai (Coding Plan)",
				"model": "glm-4.6",
				"api_key": "zck",
				"base_url": "https://api.z.ai/api/coding/paas/v4",
				"tier": "strong",
				"order": 0,
			}
		]
		with (
			patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}),
			patch("jarvis.admin_client.post_update_llm_pool") as pool,
		):
			onboarding.save_llm_pool(frappe.as_json(models))
		pool.assert_not_called()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.models[0].provider, "zai_coding")
		self.assertEqual(s.models[0].base_url, "https://api.z.ai/api/coding/paas/v4")
		cfg = onboarding.get_llm_config()
		self.assertEqual(cfg["models"][0]["provider"], "zai_coding")
		self.assertEqual(cfg["models"][0]["base_url"], "https://api.z.ai/api/coding/paas/v4")


class TestGetLlmConfig(_RT3SettingsTestCase):
	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()

	def test_reports_models_preset_routing_and_proxy_without_secrets(self):
		models = [
			{"provider": "openai", "model": "gpt-5.5", "api_key": "sk-a", "order": 0},
			{"provider": "openai", "model": "gpt-5.4", "api_key": "sk-b", "order": 1},
		]
		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			onboarding.save_llm_pool(frappe.as_json(models), routing_mode="failover")
		cfg = onboarding.get_llm_config()
		self.assertEqual(len(cfg["models"]), 2)
		self.assertEqual(cfg["models"][0]["model"], "gpt-5.5")
		self.assertTrue(cfg["models"][0]["has_key"])
		self.assertNotIn("api_key", cfg["models"][0])
		self.assertNotIn("sk-a", frappe.as_json(cfg))
		self.assertEqual(cfg["routing_mode"], "failover")
		# Two BYO api keys render agent-direct: no Bifrost/cliproxy sidecar.
		self.assertFalse(cfg["proxy_active"])


class TestBackfillGlmZaiProviderIdPatch(_RT3SettingsTestCase):
	"""v2_01_backfill_glm_zai_provider_id: existing rows that were collapsed
	into provider="openai_compat" by the old storage-time normalize_provider
	bug (see pool_serialize._PROVIDER_ALIASES) must be flipped back to the
	first-class "zai" (pay-as-you-go) or "zai_coding" (Coding Plan) id
	depending on which Z.ai endpoint their base_url actually names. A genuine
	openai_compat row (any other custom endpoint) must be left untouched."""

	def setUp(self):
		super().setUp()
		self._clear_models()

	def _insert_model_row(self, *, provider, base_url, model="m", order=0):
		"""Insert a model row directly (bypassing Jarvis Settings.save()), the
		same way the patch will encounter it: already-persisted config, not a
		fresh in-memory row. Mirrors v1_seed_llm_models's insert-not-save
		pattern so this test never triggers on_update / validate_models /
		any admin network call."""
		row = frappe.get_doc(
			{
				"doctype": "Jarvis LLM Pool Model",
				"parent": "Jarvis Settings",
				"parenttype": "Jarvis Settings",
				"parentfield": "models",
				"provider": provider,
				"model": model,
				"base_url": base_url,
				"credential_type": "api_key",
				"tier": "strong",
				"order": order,
				"enabled": 1,
				"api_key": "sk-test",
			}
		)
		row.insert(ignore_permissions=True)
		frappe.db.commit()
		return row.name

	def _run_patch(self):
		import importlib

		from jarvis.patches import v2_01_backfill_glm_zai_provider_id

		importlib.reload(v2_01_backfill_glm_zai_provider_id)
		v2_01_backfill_glm_zai_provider_id.execute()

	def test_collapsed_glm_row_is_flipped_to_zai(self):
		name = self._insert_model_row(
			provider="openai_compat",
			base_url="https://api.z.ai/api/paas/v4",
			model="glm-4.6",
		)
		self._run_patch()
		self.assertEqual(frappe.db.get_value("Jarvis LLM Pool Model", name, "provider"), "zai")

	def test_collapsed_glm_coding_plan_endpoint_is_flipped_to_zai_coding(self):
		"""The coding-plan Z.ai endpoint (same host, different path) is a DIFFERENT
		product from pay-as-you-go and must backfill to its own "zai_coding" id,
		not "zai" - the two have separate balances and a coding-plan key rejected
		on the pay-as-you-go endpoint is exactly the trap this distinction exists
		to avoid re-creating during backfill."""
		name = self._insert_model_row(
			provider="openai_compat",
			base_url="https://api.z.ai/api/coding/paas/v4",
			model="glm-4.6",
		)
		self._run_patch()
		self.assertEqual(frappe.db.get_value("Jarvis LLM Pool Model", name, "provider"), "zai_coding")

	def test_genuine_openai_compat_row_is_left_untouched(self):
		"""A real OpenAI-Compatible shim (not Z.ai) must NOT be reclassified."""
		name = self._insert_model_row(
			provider="openai_compat",
			base_url="https://my-claude-cli-gateway.example.com/v1",
			model="claude-sonnet-4-6",
		)
		self._run_patch()
		self.assertEqual(frappe.db.get_value("Jarvis LLM Pool Model", name, "provider"), "openai_compat")

	def test_already_zai_row_is_a_no_op(self):
		"""A row already migrated (e.g. a second patch run) is idempotent."""
		name = self._insert_model_row(
			provider="zai",
			base_url="https://api.z.ai/api/paas/v4",
			model="glm-4.6",
		)
		self._run_patch()
		self.assertEqual(frappe.db.get_value("Jarvis LLM Pool Model", name, "provider"), "zai")

	def test_already_zai_coding_row_is_a_no_op(self):
		"""Same idempotency guarantee for the coding-plan id."""
		name = self._insert_model_row(
			provider="zai_coding",
			base_url="https://api.z.ai/api/coding/paas/v4",
			model="glm-4.6",
		)
		self._run_patch()
		self.assertEqual(frappe.db.get_value("Jarvis LLM Pool Model", name, "provider"), "zai_coding")

	def test_non_openai_compat_provider_is_untouched(self):
		"""Only rows currently stored as openai_compat are candidates; an
		unrelated provider's row must never be inspected/rewritten."""
		name = self._insert_model_row(
			provider="openai",
			base_url="https://api.openai.com/v1",
			model="gpt-4o",
		)
		self._run_patch()
		self.assertEqual(frappe.db.get_value("Jarvis LLM Pool Model", name, "provider"), "openai")


class TestCoalesceSubscriptionModels(_RT3SettingsTestCase):
	"""jarvis#575: a second account of a provider the tenant already uses arrived
	as a SECOND MODEL ROW naming the same model, and every subscription model
	renders through one shared Bifrost provider entry, so llm_proxy.validate
	rejected the whole spec with duplicate_subscription_model. The customer only
	learned that after completing a full OAuth sign-in.

	Pure list transforms. Nothing here touches the DB.
	"""

	@staticmethod
	def _sub(model, *accounts):
		return {
			"model": model,
			"credential_type": "subscription",
			"subscription": {"accounts": list(accounts)},
		}

	@staticmethod
	def _acct(ref, upstream="openai", blob='{"t":1}'):
		return {"account_ref": ref, "upstream": upstream, "oauth_blob": blob, "label": ref}

	def test_two_rows_of_one_model_fold_into_one_with_both_accounts(self):
		"""The reported repro: add a second ChatGPT account."""
		out = onboarding._coalesce_subscription_models(
			[
				self._sub("gpt-5.5", self._acct("ACC_1")),
				self._sub("gpt-5.5", self._acct("ACC_2")),
			]
		)
		self.assertEqual(len(out), 1)
		refs = [a["account_ref"] for a in out[0]["subscription"]["accounts"]]
		self.assertEqual(refs, ["ACC_1", "ACC_2"])

	def test_a_repeated_account_ref_is_not_duplicated(self):
		"""validate_models rejects a duplicate account_ref, so folding must not make one."""
		out = onboarding._coalesce_subscription_models(
			[self._sub("gpt-5.5", self._acct("ACC_1")), self._sub("gpt-5.5", self._acct("ACC_1"))]
		)
		self.assertEqual(len(out[0]["subscription"]["accounts"]), 1)

	def test_a_reloaded_blank_copy_gains_the_posted_credential(self):
		"""A row reloaded from the DB carries no blob; the freshly connected one does."""
		out = onboarding._coalesce_subscription_models(
			[
				self._sub("gpt-5.5", self._acct("ACC_1", blob="")),
				self._sub("gpt-5.5", self._acct("ACC_1", blob='{"real":1}')),
			]
		)
		accounts = out[0]["subscription"]["accounts"]
		self.assertEqual(len(accounts), 1)
		self.assertEqual(accounts[0]["oauth_blob"], '{"real":1}')

	def test_different_models_are_not_folded(self):
		out = onboarding._coalesce_subscription_models(
			[self._sub("gpt-5.5", self._acct("ACC_1")), self._sub("grok-4.3", self._acct("ACC_2"))]
		)
		self.assertEqual(len(out), 2)

	def test_rows_of_different_upstreams_are_not_folded(self):
		"""Folding across upstreams would manufacture the mixed-upstream row
		validate_models rejects, so the upstream is part of the fold key."""
		out = onboarding._coalesce_subscription_models(
			[
				self._sub("shared-name", self._acct("ACC_1", upstream="openai")),
				self._sub("shared-name", self._acct("ACC_2", upstream="xai")),
			]
		)
		self.assertEqual(len(out), 2)

	def test_api_key_rows_pass_through_untouched(self):
		"""validate scopes their duplicate check to (provider, model) and they have
		no accounts to merge, so they must not be folded."""
		rows = [
			{"model": "glm-4.7", "provider": "zai_coding", "credential_type": "api_key"},
			{"model": "glm-4.7", "provider": "openai_compat", "credential_type": "api_key"},
		]
		self.assertEqual(onboarding._coalesce_subscription_models(rows), rows)

	def test_the_input_list_is_not_mutated(self):
		"""save_llm_pool re-reads its own argument, so folding must copy."""
		a = self._sub("gpt-5.5", self._acct("ACC_1"))
		b = self._sub("gpt-5.5", self._acct("ACC_2"))
		onboarding._coalesce_subscription_models([a, b])
		self.assertEqual(len(a["subscription"]["accounts"]), 1)
		self.assertEqual(len(b["subscription"]["accounts"]), 1)
