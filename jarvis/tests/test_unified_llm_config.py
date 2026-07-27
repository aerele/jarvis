"""Tests for Task 1: Unified LLM config — Jarvis Settings schema assertions.

Verifies:
- Jarvis Settings has models (Table), preset (Select), proxy_active (Check, read_only),
  proxy_recommended (Check, read_only) fields
- Legacy llm_model, llm_api_key, llm_provider, llm_base_url, llm_auth_mode are read_only
- Standalone Jarvis LLM Pool doctype no longer exists
- Pool Model provider + base_url fields have depends_on on credential_type=='api_key'
- Subscription Account upstream options do NOT contain 'anthropic'
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase


def _field_map(meta):
	"""Return a dict of fieldname -> field dict from a Meta object."""
	return {f.fieldname: f for f in meta.fields}


class TestUnifiedLLMConfigSchema(FrappeTestCase):
	"""Schema-level assertions for the unified LLM config design."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.settings_meta = frappe.get_meta("Jarvis Settings")
		cls.settings_fields = _field_map(cls.settings_meta)
		cls.pool_model_meta = frappe.get_meta("Jarvis LLM Pool Model")
		cls.pool_model_fields = _field_map(cls.pool_model_meta)
		cls.sub_account_meta = frappe.get_meta("Jarvis LLM Pool Subscription Account")
		cls.sub_account_fields = _field_map(cls.sub_account_meta)

	# ------------------------------------------------------------------ #
	# Jarvis Settings — new fields
	# ------------------------------------------------------------------ #

	def test_settings_has_models_table_field(self):
		"""Jarvis Settings must have a 'models' Table field pointing to Jarvis LLM Pool Model."""
		self.assertIn("models", self.settings_fields, "Jarvis Settings must have a 'models' field")
		f = self.settings_fields["models"]
		self.assertEqual(f.fieldtype, "Table", "models field must be of type Table")
		self.assertEqual(
			f.options, "Jarvis LLM Pool Model", "models Table must link to 'Jarvis LLM Pool Model'"
		)

	def test_settings_preset_is_free_form(self):
		self.assertIn("preset", self.settings_fields)
		self.assertEqual(
			self.settings_fields["preset"].fieldtype,
			"Data",
			"preset must be Data (validated against fetched catalog keys, not a Select)",
		)

	def test_settings_has_proxy_active_check_read_only(self):
		"""Jarvis Settings must have 'proxy_active' Check field that is read_only."""
		self.assertIn(
			"proxy_active", self.settings_fields, "Jarvis Settings must have a 'proxy_active' field"
		)
		f = self.settings_fields["proxy_active"]
		self.assertEqual(f.fieldtype, "Check", "proxy_active must be of type Check")
		self.assertEqual(f.read_only, 1, "proxy_active must be read_only=1")

	def test_settings_has_proxy_recommended_check_read_only(self):
		"""Jarvis Settings must have 'proxy_recommended' Check field that is read_only."""
		self.assertIn(
			"proxy_recommended", self.settings_fields, "Jarvis Settings must have a 'proxy_recommended' field"
		)
		f = self.settings_fields["proxy_recommended"]
		self.assertEqual(f.fieldtype, "Check", "proxy_recommended must be of type Check")
		self.assertEqual(f.read_only, 1, "proxy_recommended must be read_only=1")

	# ------------------------------------------------------------------ #
	# Jarvis Settings — legacy fields become read_only
	# ------------------------------------------------------------------ #

	def test_llm_model_is_read_only(self):
		"""Legacy llm_model field must be read_only=1 (derived mirror)."""
		self.assertIn("llm_model", self.settings_fields)
		self.assertEqual(self.settings_fields["llm_model"].read_only, 1, "llm_model must be read_only=1")

	def test_llm_api_key_is_read_only(self):
		"""Legacy llm_api_key field must be read_only=1 (derived mirror)."""
		self.assertIn("llm_api_key", self.settings_fields)
		self.assertEqual(self.settings_fields["llm_api_key"].read_only, 1, "llm_api_key must be read_only=1")

	def test_llm_provider_is_read_only(self):
		"""Legacy llm_provider field must be read_only=1 (derived mirror)."""
		self.assertIn("llm_provider", self.settings_fields)
		self.assertEqual(
			self.settings_fields["llm_provider"].read_only, 1, "llm_provider must be read_only=1"
		)

	def test_llm_base_url_is_read_only(self):
		"""Legacy llm_base_url field must be read_only=1 (derived mirror)."""
		self.assertIn("llm_base_url", self.settings_fields)
		self.assertEqual(
			self.settings_fields["llm_base_url"].read_only, 1, "llm_base_url must be read_only=1"
		)

	def test_llm_auth_mode_is_read_only(self):
		"""Legacy llm_auth_mode field must be read_only=1 (derived mirror)."""
		self.assertIn("llm_auth_mode", self.settings_fields)
		self.assertEqual(
			self.settings_fields["llm_auth_mode"].read_only, 1, "llm_auth_mode must be read_only=1"
		)

	# ------------------------------------------------------------------ #
	# Standalone Jarvis LLM Pool doctype must NOT exist
	# ------------------------------------------------------------------ #

	def test_standalone_llm_pool_doctype_dropped(self):
		"""The standalone 'Jarvis LLM Pool' Single doctype must not exist."""
		exists = frappe.db.exists("DocType", "Jarvis LLM Pool")
		self.assertFalse(exists, "Jarvis LLM Pool standalone doctype must be dropped")

	# ------------------------------------------------------------------ #
	# Pool Model — provider + base_url gated on credential_type
	# ------------------------------------------------------------------ #

	def test_pool_model_provider_has_depends_on_credential_type(self):
		"""Pool Model 'provider' field must have depends_on gating on credential_type=='api_key'."""
		self.assertIn("provider", self.pool_model_fields)
		depends_on = self.pool_model_fields["provider"].depends_on or ""
		self.assertEqual(
			depends_on,
			"eval:doc.credential_type=='api_key'",
			"provider field depends_on must be exactly eval:doc.credential_type=='api_key'",
		)

	def test_pool_model_base_url_has_depends_on_credential_type(self):
		"""Pool Model 'base_url' field must have depends_on gating on credential_type=='api_key'."""
		self.assertIn("base_url", self.pool_model_fields)
		depends_on = self.pool_model_fields["base_url"].depends_on or ""
		self.assertEqual(
			depends_on,
			"eval:doc.credential_type=='api_key'",
			"base_url field depends_on must be exactly eval:doc.credential_type=='api_key'",
		)

	# ------------------------------------------------------------------ #
	# Subscription Account — upstream must not include 'anthropic'
	# ------------------------------------------------------------------ #

	def test_subscription_account_upstream_no_anthropic(self):
		"""Subscription Account 'upstream' options must NOT contain 'anthropic' (ToS-banned)."""
		self.assertIn("upstream", self.sub_account_fields)
		options = self.sub_account_fields["upstream"].options or ""
		option_list = [o.strip() for o in options.split("\n") if o.strip()]
		self.assertNotIn(
			"anthropic", option_list, "upstream options must not include 'anthropic' (ToS violation)"
		)


# ---------------------------------------------------------------------------
# Task 2: pool_serialize re-sourced from settings.models + hygiene tests
# ---------------------------------------------------------------------------


def _make_settings_with_models(models_rows):
	"""Build a minimal in-memory settings-like object without saving to DB.

	Uses frappe._dict so that _get_password adapter works (hasattr(doc, 'get') path).
	models_rows is a list of frappe._dict; accounts inside subscription models
	are also frappe._dict objects.
	"""
	settings = frappe._dict(
		models=models_rows,
		preset=None,
		routing_mode="dynamic",
	)
	return settings


def _api_key_model(**kwargs):
	defaults = frappe._dict(
		model="gpt-4o",
		tier="strong",
		order=0,
		enabled=1,
		credential_type="api_key",
		api_key="sk-test-key",
		provider="openai_compat",
		base_url="https://api.openai.com",
		rotation=None,
		accounts=[],
	)
	defaults.update(kwargs)
	return defaults


def _subscription_model(**kwargs):
	defaults = frappe._dict(
		model="gpt-5.5",
		tier="cheap",
		order=1,
		enabled=1,
		credential_type="subscription",
		api_key=None,
		provider="",
		base_url="",
		rotation="round_robin",
		accounts=[],
	)
	defaults.update(kwargs)
	return defaults


def _account(
	upstream="openai", account_ref="ACC_001", label="test@example.com", oauth_blob='{"token":"abc"}'
):
	return frappe._dict(
		upstream=upstream,
		account_ref=account_ref,
		label=label,
		oauth_blob=oauth_blob,
	)


class TestProviderNormalization(FrappeTestCase):
	"""Phase-1 API-key expansion: normalize_provider maps new provider labels."""

	def test_xai_grok_normalizes_to_xai(self):
		from jarvis.jarvis.pool_serialize import normalize_provider

		# xAI Grok is a native Bifrost provider; its label normalizes to "xai".
		self.assertEqual(normalize_provider("xAI Grok"), "xai")

	def test_glm_zai_normalizes_to_zai(self):
		from jarvis.jarvis.pool_serialize import normalize_provider

		# GLM / Z.ai is a first-class STORED provider id, same as every other
		# alias: a dropdown label round-trips to its OWN canonical id, not a
		# different provider's. (It has no native Bifrost provider, so it
		# rides the openai-compatible custom-endpoint transport - but that
		# collapse happens only at wire-serialization time, in
		# build_pool_payload; see test_xai_and_glm_serialize_into_pool_payload
		# below.) Regression test for the bug where storage-time collapsing
		# onto "openai_compat" made a saved GLM row permanently render as
		# "OpenAI-Compatible" everywhere the pool was displayed.
		self.assertEqual(normalize_provider("GLM / Z.ai"), "zai")
		# Passthrough: a row already stored as "zai" re-normalizes to itself.
		self.assertEqual(normalize_provider("zai"), "zai")

	def test_existing_labels_unchanged(self):
		from jarvis.jarvis.pool_serialize import normalize_provider

		self.assertEqual(normalize_provider("Groq"), "groq")
		self.assertEqual(normalize_provider("Moonshot (Kimi)"), "moonshot")
		self.assertEqual(normalize_provider("OpenAI-Compatible"), "openai_compat")

	def test_xai_and_glm_serialize_into_pool_payload(self):
		# The actual feature: build_pool_payload emits xAI as the native "xai"
		# provider and GLM/Z.ai as an openai_compat custom provider (base_url
		# kept). This is the wire-payload guarantee: it must stay exactly
		# "openai_compat" even though normalize_provider now returns "zai" for
		# GLM/Z.ai - the collapse moved into build_pool_payload's
		# _wire_provider(), not away entirely. A GLM row's stored provider
		# ("GLM / Z.ai" label, or the persisted "zai" id) must both serialize
		# identically here so the Bifrost payload never changes shape.
		from jarvis.jarvis.pool_serialize import build_pool_payload

		xai = _api_key_model(
			provider="xAI Grok", model="grok-4.5", base_url="https://api.x.ai/v1", api_key="xk", order=0
		)
		glm = _api_key_model(
			provider="GLM / Z.ai",
			model="glm-4.6",
			base_url="https://api.z.ai/api/paas/v4",
			api_key="zk",
			order=1,
		)
		settings = _make_settings_with_models([xai, glm])
		spec, _api_keys, _ = build_pool_payload(settings)
		prov = {m["model"]: m.get("provider") for m in spec["models"]}
		base = {m["model"]: m.get("base_url") for m in spec["models"]}
		self.assertEqual(prov["grok-4.5"], "xai")  # native passthrough
		self.assertEqual(prov["glm-4.6"], "openai_compat")  # GLM rides openai_compat
		self.assertEqual(base["glm-4.6"], "https://api.z.ai/api/paas/v4")

	def test_glm_zai_already_stored_as_id_serializes_same_as_label(self):
		# A row already persisted with the new first-class "zai" id (the
		# normal post-fix state) must serialize onto the wire IDENTICALLY to
		# one that still carries the raw "GLM / Z.ai" label - build_pool_payload
		# re-normalizes before collapsing, so storage shape never leaks into
		# the Bifrost payload.
		from jarvis.jarvis.pool_serialize import build_pool_payload

		glm = _api_key_model(
			provider="zai", model="glm-4.6", base_url="https://api.z.ai/api/paas/v4", api_key="zk", order=0
		)
		settings = _make_settings_with_models([glm])
		spec, _api_keys, _ = build_pool_payload(settings)
		self.assertEqual(spec["models"][0]["provider"], "openai_compat")
		self.assertEqual(spec["models"][0]["base_url"], "https://api.z.ai/api/paas/v4")

	def test_validate_models_requires_base_url_for_zai(self):
		# Defense in depth: the same base_url requirement that already gated
		# openai_compat/vllm must cover zai now that it is a distinct stored
		# id, or a GLM row could be saved with no endpoint and every turn on
		# it would fail. (The frontend already blocks this client-side; this
		# is the server-side backstop the frontend validator mirrors.)
		from jarvis.jarvis.pool_serialize import validate_models

		glm_no_url = _api_key_model(provider="zai", model="glm-4.6", base_url="", api_key="zk", order=0)
		settings = _make_settings_with_models([glm_no_url])
		errors = validate_models(settings)
		self.assertTrue(
			any("requires a base_url" in e for e in errors),
			f"expected a base_url error for a zai row with no base_url, got: {errors}",
		)

	# ------------------------------------------------------------------ #
	# GLM Coding Plan ("zai_coding"): a SEPARATE z.ai product from
	# pay-as-you-go "zai" above, added after live discovery that a Coding
	# Plan key authenticates fine but reports "insufficient balance" (z.ai
	# error 1113) on the pay-as-you-go endpoint - a different balance, a
	# different endpoint, so a distinct stored id (never collapsed onto
	# "zai"). Both ride the same openai-compatible wire transport.
	# ------------------------------------------------------------------ #

	def test_glm_coding_plan_normalizes_to_zai_coding(self):
		from jarvis.jarvis.pool_serialize import normalize_provider

		self.assertEqual(normalize_provider("GLM / Z.ai (Coding Plan)"), "zai_coding")
		# Passthrough, same as every other canonical id.
		self.assertEqual(normalize_provider("zai_coding"), "zai_coding")
		# The two GLM ids are DISTINCT - never collapse into each other at
		# the storage layer (only their wire transport happens to coincide).
		self.assertNotEqual(
			normalize_provider("GLM / Z.ai (Coding Plan)"),
			normalize_provider("GLM / Z.ai"),
		)

	def test_glm_coding_plan_serializes_to_openai_compat_with_its_own_base_url(self):
		# Same wire-collapse guarantee as the standard GLM row, but the
		# coding-plan row must keep ITS OWN base_url (the coding endpoint),
		# not the pay-as-you-go one - the whole point of the two ids existing
		# separately is that they point at different URLs.
		from jarvis.jarvis.pool_serialize import build_pool_payload

		glm_coding = _api_key_model(
			provider="GLM / Z.ai (Coding Plan)",
			model="glm-4.6",
			base_url="https://api.z.ai/api/coding/paas/v4",
			api_key="zk",
			order=0,
		)
		settings = _make_settings_with_models([glm_coding])
		spec, _api_keys, _ = build_pool_payload(settings)
		self.assertEqual(spec["models"][0]["provider"], "openai_compat")
		self.assertEqual(spec["models"][0]["base_url"], "https://api.z.ai/api/coding/paas/v4")

	def test_standard_zai_wire_payload_unaffected_by_the_coding_plan_sibling(self):
		# Regression lock: adding "zai_coding" must NOT change one byte of the
		# standard "zai" row's wire output. Same assertions as
		# test_xai_and_glm_serialize_into_pool_payload, re-run in a pool that
		# ALSO contains a coding-plan row, to catch any cross-talk between the
		# two _WIRE_PROVIDER_OVERRIDES entries.
		from jarvis.jarvis.pool_serialize import build_pool_payload

		glm = _api_key_model(
			provider="GLM / Z.ai",
			model="glm-4.6",
			base_url="https://api.z.ai/api/paas/v4",
			api_key="zk",
			order=0,
		)
		glm_coding = _api_key_model(
			provider="GLM / Z.ai (Coding Plan)",
			model="glm-4.6-coding",
			base_url="https://api.z.ai/api/coding/paas/v4",
			api_key="zck",
			order=1,
		)
		settings = _make_settings_with_models([glm, glm_coding])
		spec, api_keys, _ = build_pool_payload(settings)
		prov = {m["model"]: m.get("provider") for m in spec["models"]}
		base = {m["model"]: m.get("base_url") for m in spec["models"]}
		self.assertEqual(prov["glm-4.6"], "openai_compat")
		self.assertEqual(base["glm-4.6"], "https://api.z.ai/api/paas/v4")
		self.assertEqual(prov["glm-4.6-coding"], "openai_compat")
		self.assertEqual(base["glm-4.6-coding"], "https://api.z.ai/api/coding/paas/v4")
		# Each row keeps its own api_key under its own key_ref - no collision.
		self.assertEqual(len(api_keys), 2)

	def test_validate_models_requires_base_url_for_zai_coding(self):
		from jarvis.jarvis.pool_serialize import validate_models

		glm_coding_no_url = _api_key_model(
			provider="zai_coding", model="glm-4.6", base_url="", api_key="zk", order=0
		)
		settings = _make_settings_with_models([glm_coding_no_url])
		errors = validate_models(settings)
		self.assertTrue(
			any("requires a base_url" in e for e in errors),
			f"expected a base_url error for a zai_coding row with no base_url, got: {errors}",
		)


class TestPoolSerializeFromSettings(FrappeTestCase):
	"""Task 2: Direct-call tests for build_pool_payload / validate_models / compute_proxy_active."""

	# ------------------------------------------------------------------ #
	# Imports
	# ------------------------------------------------------------------ #

	def _imports(self):
		from jarvis.jarvis.pool_serialize import (
			build_pool_payload,
			compute_proxy_active,
			validate_models,
		)

		return build_pool_payload, validate_models, compute_proxy_active

	# ------------------------------------------------------------------ #
	# (a) Secrets out of spec
	# ------------------------------------------------------------------ #

	def test_secrets_not_in_spec(self):
		"""API key values must NOT appear in the serialized spec dict."""
		build_pool_payload, _, _ = self._imports()
		m = _api_key_model(api_key="super-secret-key-xyz")
		settings = _make_settings_with_models([m])
		spec, api_keys, _ = build_pool_payload(settings)
		# secret must not appear anywhere in spec
		self.assertNotIn("super-secret-key-xyz", str(spec))
		# secret must be in api_keys under the key_ref
		self.assertTrue(any(v == "super-secret-key-xyz" for v in api_keys.values()))

	# ------------------------------------------------------------------ #
	# (b) Subscription entry omits provider/base_url
	# ------------------------------------------------------------------ #

	def test_subscription_model_omits_provider_and_base_url(self):
		"""Subscription models must NOT emit provider or base_url in the spec."""
		build_pool_payload, _, _ = self._imports()
		acc = _account()
		m = _subscription_model(accounts=[acc], provider="openai_compat", base_url="https://api.openai.com")
		settings = _make_settings_with_models([m])
		spec, _, _ = build_pool_payload(settings)
		entry = spec["models"][0]
		self.assertNotIn("provider", entry, "subscription entry must not emit provider")
		self.assertNotIn("base_url", entry, "subscription entry must not emit base_url")

	# ------------------------------------------------------------------ #
	# (c) Mixed upstream across accounts → validate_models error
	# ------------------------------------------------------------------ #

	def test_mixed_upstream_across_accounts_is_a_validate_error(self):
		"""Accounts with different upstreams in one subscription model → validate_models error."""
		_, validate_models, _ = self._imports()
		acc1 = _account(upstream="openai", account_ref="ACC_001")
		acc2 = _account(upstream="google", account_ref="ACC_002")
		m = _subscription_model(accounts=[acc1, acc2])
		settings = _make_settings_with_models([m])
		errors = validate_models(settings)
		self.assertTrue(
			any("upstream" in e.lower() or "mixed" in e.lower() or "consistent" in e.lower() for e in errors),
			f"Expected upstream consistency error, got: {errors}",
		)

	def test_unsupported_upstream_is_a_validate_error(self):
		"""A typo'd/unsupported subscription upstream is rejected (enum guard lost
		in the JSON migration, re-enforced). #200 review #6."""
		_, validate_models, _ = self._imports()
		m = _subscription_model(accounts=[_account(upstream="gogle", account_ref="ACC_001")])
		settings = _make_settings_with_models([m])
		errors = validate_models(settings)
		self.assertTrue(
			any("unsupported upstream" in e.lower() for e in errors),
			f"Expected an unsupported-upstream error, got: {errors}",
		)

	def test_supported_upstreams_pass_validation(self):
		"""openai + google are still accepted (no false positive from the guard)."""
		_, validate_models, _ = self._imports()
		for up in ("openai", "google"):
			m = _subscription_model(accounts=[_account(upstream=up, account_ref=f"ACC_{up}")])
			settings = _make_settings_with_models([m])
			errors = validate_models(settings)
			self.assertFalse(
				any("unsupported upstream" in e.lower() for e in errors),
				f"upstream={up!r} must be accepted, got: {errors}",
			)

	# ------------------------------------------------------------------ #
	# (d) Duplicate account_ref → validate_models error
	# ------------------------------------------------------------------ #

	def test_duplicate_account_ref_is_a_validate_error(self):
		"""Same account_ref used in two different models → validate_models error."""
		_, validate_models, _ = self._imports()
		acc1 = _account(upstream="openai", account_ref="DUPE_REF")
		acc2 = _account(upstream="openai", account_ref="DUPE_REF")
		m1 = _subscription_model(accounts=[acc1], model="gpt-5.5", order=0)
		m2 = _subscription_model(accounts=[acc2], model="gpt-4o-mini", tier="strong", order=1)
		settings = _make_settings_with_models([m1, m2])
		errors = validate_models(settings)
		self.assertTrue(
			any("duplicate" in e.lower() or "account_ref" in e.lower() for e in errors),
			f"Expected duplicate account_ref error, got: {errors}",
		)

	# ------------------------------------------------------------------ #
	# (e) Blank api_key on enabled api_key model → validate_models error
	# ------------------------------------------------------------------ #

	def test_blank_api_key_on_enabled_model_is_a_validate_error(self):
		"""Enabled api_key model with empty key → validate_models error (no dangling key_ref)."""
		_, validate_models, _ = self._imports()
		m = _api_key_model(api_key="", enabled=1)
		settings = _make_settings_with_models([m])
		errors = validate_models(settings)
		self.assertTrue(
			any("api_key" in e.lower() or "key" in e.lower() or "blank" in e.lower() for e in errors),
			f"Expected blank api_key error, got: {errors}",
		)

	# ------------------------------------------------------------------ #
	# (f) Empty subscription accounts → validate_models error
	# ------------------------------------------------------------------ #

	def test_empty_accounts_on_enabled_subscription_model_is_a_validate_error(self):
		"""Enabled subscription model with no accounts → validate_models error."""
		_, validate_models, _ = self._imports()
		m = _subscription_model(accounts=[], enabled=1)
		settings = _make_settings_with_models([m])
		errors = validate_models(settings)
		self.assertTrue(
			any("account" in e.lower() or "empty" in e.lower() for e in errors),
			f"Expected empty accounts error, got: {errors}",
		)

	# ------------------------------------------------------------------ #
	# (g) Malformed oauth_blob → validate_models error STRING (no raise)
	# ------------------------------------------------------------------ #

	def test_malformed_oauth_blob_returns_error_string_not_raise(self):
		"""Malformed JSON in oauth_blob must produce a validate_models error string — NOT raise."""
		build_pool_payload, validate_models, _ = self._imports()
		acc = _account(oauth_blob="{not valid json!!!")
		m = _subscription_model(accounts=[acc])
		settings = _make_settings_with_models([m])

		# validate_models must NOT raise — must return list with an error
		try:
			errors = validate_models(settings)
		except Exception as exc:
			self.fail(f"validate_models raised an exception on malformed oauth_blob: {exc}")
		self.assertTrue(
			any(
				"oauth" in e.lower() or "json" in e.lower() or "blob" in e.lower() or "malformed" in e.lower()
				for e in errors
			),
			f"Expected malformed oauth_blob error string, got: {errors}",
		)

		# build_pool_payload must also NOT raise
		try:
			build_pool_payload(settings)
		except Exception as exc:
			self.fail(f"build_pool_payload raised on malformed oauth_blob: {exc}")

	# ------------------------------------------------------------------ #
	# (h) compute_proxy_active
	# ------------------------------------------------------------------ #

	def test_compute_proxy_active_true_when_two_enabled_models(self):
		"""compute_proxy_active is True when ≥2 models are enabled."""
		_, _, compute_proxy_active = self._imports()
		m1 = _api_key_model(enabled=1)
		m2 = _api_key_model(model="gpt-4-turbo", enabled=1, order=1)
		settings = _make_settings_with_models([m1, m2])
		self.assertTrue(compute_proxy_active(settings))

	def test_compute_proxy_active_true_when_preset_set(self):
		"""compute_proxy_active is True when preset is set (even with only 1 model)."""
		_, _, compute_proxy_active = self._imports()
		m = _api_key_model(enabled=1)
		settings = _make_settings_with_models([m])
		settings.preset = "Cost-saver"
		self.assertTrue(compute_proxy_active(settings))

	def test_compute_proxy_active_false_when_one_model_no_preset(self):
		"""compute_proxy_active is False for exactly 1 enabled model and no preset."""
		_, _, compute_proxy_active = self._imports()
		m = _api_key_model(enabled=1)
		settings = _make_settings_with_models([m])
		settings.preset = None
		self.assertFalse(compute_proxy_active(settings))

	# ------------------------------------------------------------------ #
	# No dangling key_ref: build_pool_payload only emits key_ref+api_keys together
	# ------------------------------------------------------------------ #

	def test_no_dangling_key_ref_when_api_key_is_blank(self):
		"""A model with blank api_key must not emit a key_ref pointing to nothing."""
		build_pool_payload, _, _ = self._imports()
		m = _api_key_model(api_key="", enabled=1)
		settings = _make_settings_with_models([m])
		spec, api_keys, _ = build_pool_payload(settings)
		# The blank-key model must not have key_ref at all
		for entry in spec.get("models", []):
			self.assertNotIn("key_ref", entry, "blank api_key model must not emit key_ref in spec")

	# ------------------------------------------------------------------ #
	# anthropic upstream → validate_models error
	# ------------------------------------------------------------------ #

	def test_anthropic_upstream_in_subscription_is_a_validate_error(self):
		"""Subscription account with upstream='anthropic' → validate_models error (ToS)."""
		_, validate_models, _ = self._imports()
		acc = _account(upstream="anthropic")
		m = _subscription_model(accounts=[acc])
		settings = _make_settings_with_models([m])
		errors = validate_models(settings)
		self.assertTrue(
			any("anthropic" in e.lower() or "tos" in e.lower() or "upstream" in e.lower() for e in errors),
			f"Expected anthropic upstream ToS error, got: {errors}",
		)

	# ------------------------------------------------------------------ #
	# (i) Blank account_ref on subscription account
	# ------------------------------------------------------------------ #

	def test_blank_account_ref_on_subscription_is_a_validate_error(self):
		"""Enabled subscription account with blank account_ref → validate_models error."""
		_, validate_models, _ = self._imports()
		acc = _account(account_ref="")  # blank account_ref
		m = _subscription_model(accounts=[acc])
		settings = _make_settings_with_models([m])
		errors = validate_models(settings)
		self.assertTrue(
			any("account_ref" in e.lower() or "missing" in e.lower() for e in errors),
			f"Expected blank account_ref error, got: {errors}",
		)

	def test_blank_account_ref_does_not_write_empty_string_oauth_key(self):
		"""Enabled subscription account with blank account_ref must not produce oauth_blobs['']."""
		build_pool_payload, _, _ = self._imports()
		acc = _account(account_ref="", oauth_blob='{"token":"secret"}')
		m = _subscription_model(accounts=[acc])
		settings = _make_settings_with_models([m])
		_, _, oauth_blobs = build_pool_payload(settings)
		self.assertNotIn(
			"", oauth_blobs, "blank account_ref must NOT produce an empty-string key in oauth_blobs"
		)


# ---------------------------------------------------------------------------
# Task 3: E2E serialize → llm_proxy.validate tests
# (ported from test_jarvis_llm_pool.py — re-sourced from Jarvis Settings)
# ---------------------------------------------------------------------------

try:
	import llm_proxy as _llm_proxy_mod

	_HAS_LLM_PROXY = True
except ImportError:
	_HAS_LLM_PROXY = False


class TestPoolSerializeE2E(FrappeTestCase):
	"""E2E tests: build_pool_payload(settings) → PoolSpec → llm_proxy.validate → zero issues."""

	def _imports(self):
		from jarvis.jarvis.pool_serialize import build_pool_payload

		return build_pool_payload

	def _make_settings_dynamic_both_tiers(self):
		"""Settings with 1 strong + 1 cheap api_key model — routing_mode must stay dynamic."""
		strong = _api_key_model(
			model="gpt-4o",
			tier="strong",
			order=0,
			enabled=1,
			api_key="sk-strong",
			provider="openai_compat",
			base_url="https://api.openai.com",
		)
		cheap = _api_key_model(
			model="gpt-3.5-turbo",
			tier="cheap",
			order=1,
			enabled=1,
			api_key="sk-cheap",
			provider="openai_compat",
			base_url="https://api.openai.com",
		)
		settings = _make_settings_with_models([strong, cheap])
		return settings

	def _make_settings_two_strong(self):
		"""Settings with 2 strong api_key models — no cheap tier → routing_mode must fall back to failover."""
		m1 = _api_key_model(
			model="gpt-4o",
			tier="strong",
			order=0,
			enabled=1,
			api_key="sk-a",
			provider="openai_compat",
			base_url="https://api.openai.com",
		)
		m2 = _api_key_model(
			model="gpt-4-turbo",
			tier="strong",
			order=1,
			enabled=1,
			api_key="sk-b",
			provider="openai_compat",
			base_url="https://api.openai.com",
		)
		settings = _make_settings_with_models([m1, m2])
		return settings

	def test_e2e_dynamic_pool_with_both_tiers_validates_clean(self):
		"""1 cheap + 1 strong → routing_mode stays 'dynamic', classifier present, validate() clean."""
		import unittest

		if not _HAS_LLM_PROXY:
			raise unittest.SkipTest("llm_proxy not installed")

		from llm_proxy.schema import PoolSpec
		from llm_proxy.validate import validate

		build_pool_payload = self._imports()
		settings = self._make_settings_dynamic_both_tiers()
		spec, _, _ = build_pool_payload(settings)

		self.assertEqual(
			spec["routing_mode"], "dynamic", f"expected dynamic routing_mode, got {spec['routing_mode']}"
		)
		self.assertIn("classifier", spec, "classifier key must be present for dynamic routing")

		pool_spec = PoolSpec(**spec)
		issues = validate(pool_spec)
		self.assertEqual(issues, [], f"Expected zero validate issues, got: {issues}")

	def test_e2e_two_strong_pool_falls_back_to_failover_and_validates_clean(self):
		"""2 strong models (no cheap) → routing_mode falls back to 'failover', validate() clean."""
		import unittest

		if not _HAS_LLM_PROXY:
			raise unittest.SkipTest("llm_proxy not installed")

		from llm_proxy.schema import PoolSpec
		from llm_proxy.validate import validate

		build_pool_payload = self._imports()
		settings = self._make_settings_two_strong()
		spec, _, _ = build_pool_payload(settings)

		self.assertEqual(
			spec["routing_mode"], "failover", f"expected failover fallback, got {spec['routing_mode']}"
		)
		self.assertNotIn("classifier", spec, "classifier must NOT be emitted for failover mode")

		pool_spec = PoolSpec(**spec)
		issues = validate(pool_spec)
		self.assertEqual(issues, [], f"Expected zero validate issues, got: {issues}")


# ---------------------------------------------------------------------------
# Task 3 (RT3): unified on_update — direct-vs-proxy routing, legacy mirror,
# proxy_active/proxy_recommended derived fields, validate_models gate
# ---------------------------------------------------------------------------

# Import the snapshot/restore base class from the existing test module.
from jarvis.tests.test_settings_on_update import (
	_reset_settings,
	_SettingsSingletonTestCase,
)


def frappe_patch(target, **kwargs):
	"""Convenience wrapper around unittest.mock.patch for jarvis admin_client calls."""
	from unittest.mock import patch

	return patch(target, **kwargs)


def _add_model_row(
	settings,
	*,
	provider="openai_compat",
	model="gpt-4o",
	tier="strong",
	order=0,
	enabled=1,
	credential_type="api_key",
	api_key="sk-test-pool-key",
	base_url="https://api.openai.com",
	accounts=None,
):
	"""Append an in-memory model row to settings.models using frappe.new_doc."""
	row = frappe.new_doc("Jarvis LLM Pool Model")
	row.provider = provider
	row.model = model
	row.tier = tier
	row.order = order
	row.enabled = enabled
	row.credential_type = credential_type
	row.api_key = api_key
	row.base_url = base_url
	if accounts:
		row.accounts = accounts
	settings.append("models", row)


class _RT3SettingsTestCase(_SettingsSingletonTestCase):
	"""Extends snapshot/restore with models-table cleanup."""

	# Extra plain fields specific to RT3 that we snapshot/restore.
	_RT3_PLAIN_FIELDS = ("proxy_active", "proxy_recommended", "preset")

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		cls._rt3_snapshot = {f: settings.get(f) for f in cls._RT3_PLAIN_FIELDS}

	@classmethod
	def tearDownClass(cls):
		try:
			settings = frappe.get_single("Jarvis Settings")
			for field, value in cls._rt3_snapshot.items():
				settings.db_set(field, value or "", update_modified=False)
			# Remove any models rows added by tests
			frappe.db.delete(
				"Jarvis LLM Pool Model", {"parenttype": "Jarvis Settings", "parent": "Jarvis Settings"}
			)
			frappe.db.commit()
		finally:
			super().tearDownClass()

	def _clear_models(self):
		frappe.db.delete(
			"Jarvis LLM Pool Model", {"parenttype": "Jarvis Settings", "parent": "Jarvis Settings"}
		)
		frappe.db.commit()


class TestRT3UnifiedOnUpdateRouting(_RT3SettingsTestCase):
	"""Task 3 (RT3): Verifies that on_update routes to the correct sync path
	based on the models table contents and preset.

	(a) 1 model, no preset → single-model creds path, proxy_active==0,
	    proxy_recommended==1
	(b) 2 models → proxy pool path, proxy_active==1, legacy llm_model mirrors
	    models[0]
	(c) validate_models errors → frappe.throw (ValidationError), NO sync called
	(d) 1 model + preset → proxy path, proxy_active==1
	"""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("proxy_recommended", 0, update_modified=False)
		frappe.db.commit()

	# ------------------------------------------------------------------ #
	# (a) 1 model, no preset → single-model creds path (direct)
	# ------------------------------------------------------------------ #

	def test_one_model_no_preset_routes_to_single_model_path(self):
		"""1 enabled model + no preset → single-model creds path (_enqueued_sync_via_admin),
		proxy_active==0, proxy_recommended==1."""
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			api_key="sk-direct-key",
			base_url="https://api.openai.com",
		)
		settings.preset = ""
		# Trigger a structural change so the single-model path fires.
		settings.llm_model = "gpt-4o"

		pool_sync_called = []

		with (
			frappe_patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: pool_sync_called.append(kw) or {},
			),
			frappe_patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}),
		):
			settings.save()

		# proxy path must NOT have been called
		self.assertEqual(
			pool_sync_called, [], "proxy pool path must NOT be called when 1 model and no preset"
		)

		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(int(settings.proxy_active or 0), 0, "proxy_active must be 0 for 1 model / no preset")
		self.assertEqual(
			int(settings.proxy_recommended or 0),
			1,
			"proxy_recommended must be 1 (nudge to add a model/preset)",
		)

	# ------------------------------------------------------------------ #
	# (b) 2 models → proxy path, proxy_active==1, legacy mirror
	# ------------------------------------------------------------------ #

	def test_two_models_routes_to_proxy_path(self):
		"""2 enabled models → proxy pool path, proxy_active==1,
		legacy llm_model mirrors models[0].model, ALL legacy fields mirrored,
		AND get_password('llm_api_key') returns models[0]'s key (encrypted path)."""
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			tier="strong",
			order=0,
			api_key="sk-pool-key-1",
			base_url="https://api.openai.com",
		)
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-3.5-turbo",
			tier="cheap",
			order=1,
			api_key="sk-pool-key-2",
			base_url="https://api.openai.com",
		)
		settings.preset = ""

		pool_sync_called = []

		with (
			frappe_patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: pool_sync_called.append(kw) or {"action": "pool_update"},
			),
			frappe_patch("jarvis.admin_client.post_update_llm_creds") as mock_creds,
		):
			settings.save()

		self.assertTrue(len(pool_sync_called) >= 1, "proxy pool path MUST be called when ≥2 enabled models")
		mock_creds.assert_not_called()

		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(int(settings.proxy_active or 0), 1, "proxy_active must be 1 for ≥2 models")
		# All legacy fields must mirror models[0].
		self.assertEqual(settings.llm_model, "gpt-4o", "legacy llm_model must mirror models[0].model")
		self.assertEqual(
			settings.llm_provider, "openai_compat", "legacy llm_provider must mirror models[0].provider"
		)
		self.assertEqual(
			settings.llm_base_url,
			"https://api.openai.com",
			"legacy llm_base_url must mirror models[0].base_url",
		)
		self.assertEqual(
			settings.llm_auth_mode, "api_key", "legacy llm_auth_mode must mirror models[0].credential_type"
		)
		# api_key must be stored encrypted, not plaintext — readable via get_password.
		recovered_key = settings.get_password("llm_api_key", raise_exception=False)
		self.assertEqual(
			recovered_key,
			"sk-pool-key-1",
			"get_password('llm_api_key') must return models[0]'s key after mirror",
		)

	# ------------------------------------------------------------------ #
	# (c) validate_models errors → ValidationError, NO sync called
	# ------------------------------------------------------------------ #

	def test_validate_models_errors_raise_before_sync(self):
		"""A model with blank api_key → validate_models returns errors →
		frappe.throw (ValidationError), no sync enqueued."""
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			tier="strong",
			order=0,
			api_key="",  # blank → validation error
			base_url="https://api.openai.com",
		)
		settings.preset = ""

		pool_sync_called = []
		creds_sync_called = []

		with (
			frappe_patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: pool_sync_called.append(kw),
			),
			frappe_patch(
				"jarvis.admin_client.post_update_llm_creds",
				side_effect=lambda **kw: creds_sync_called.append(kw),
			),
		):
			with self.assertRaises(
				frappe.ValidationError,
				msg="frappe.throw must surface a clean ValidationError on blank api_key",
			):
				settings.save()

		self.assertEqual(pool_sync_called, [], "pool sync must NOT be called when validate_models has errors")
		self.assertEqual(
			creds_sync_called, [], "creds sync must NOT be called when validate_models has errors"
		)

	# ------------------------------------------------------------------ #
	# (d) 1 model + preset → proxy path, proxy_active==1
	# ------------------------------------------------------------------ #

	def test_one_model_with_preset_routes_to_proxy_path(self):
		"""1 model + preset → proxy path (proxy implied by preset), proxy_active==1."""
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			tier="strong",
			order=0,
			api_key="sk-preset-key",
			base_url="https://api.openai.com",
		)
		settings.preset = "Cost-saver"

		pool_sync_called = []

		with (
			frappe_patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: pool_sync_called.append(kw) or {"action": "pool_update"},
			),
			frappe_patch("jarvis.admin_client.post_update_llm_creds") as mock_creds,
		):
			settings.save()

		self.assertTrue(len(pool_sync_called) >= 1, "proxy pool path MUST be called when 1 model + preset")
		mock_creds.assert_not_called()

		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(int(settings.proxy_active or 0), 1, "proxy_active must be 1 when preset is set")


class TestRT3LegacyNoModelsBackcompat(_RT3SettingsTestCase):
	"""Task 3 (RT3): Back-compat — tenant with NO models rows + no preset must
	route through the EXISTING single-model classify/sync path unchanged.
	"""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("proxy_recommended", 0, update_modified=False)
		frappe.db.commit()

	def test_no_models_no_preset_still_uses_single_model_path(self):
		"""Legacy tenant (no models table, no preset) saves exactly as today:
		_classify_llm_change → _sync_via_admin → /llm-creds."""
		settings = frappe.get_single("Jarvis Settings")
		# No models rows; trigger a single-model structural change.
		settings.llm_model = "kimi-k2.5"

		creds_sync_called = []
		pool_sync_called = []

		with (
			frappe_patch(
				"jarvis.admin_client.post_update_llm_creds",
				side_effect=lambda **kw: creds_sync_called.append(kw) or {"action": "restart"},
			),
			frappe_patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: pool_sync_called.append(kw),
			),
		):
			settings.save()

		self.assertEqual(pool_sync_called, [], "proxy pool path must NOT be called when no models rows")
		self.assertTrue(
			len(creds_sync_called) >= 1, "single-model creds path MUST be called for legacy tenant"
		)


class TestRT3ProxyToDirectTransition(_RT3SettingsTestCase):
	"""Fix 2: proxy→direct transition test.

	A tenant that had ≥2 models (proxy_active=1) then drops to 1 model
	(no preset) must have proxy_active reset to 0 and proxy_recommended
	set to 1, and the single-model creds path must be taken (not proxy).
	"""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		# Simulate stale proxy_active=1 from a prior 2-model save.
		settings.db_set("proxy_active", 1, update_modified=False)
		settings.db_set("proxy_recommended", 0, update_modified=False)
		frappe.db.commit()

	def test_proxy_to_direct_resets_flags_and_takes_single_model_path(self):
		"""Start with proxy_active=1 (stale), save with 1 model + no preset:
		→ proxy_active==0, proxy_recommended==1, single-model creds path taken."""
		settings = frappe.get_single("Jarvis Settings")
		# Confirm the stale proxy_active state.
		self.assertEqual(int(settings.proxy_active or 0), 1, "pre-condition: proxy_active must start at 1")

		# Now configure a single model (no preset) — should trigger direct path.
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			tier="strong",
			order=0,
			api_key="sk-single-after-pool",
			base_url="https://api.openai.com",
		)
		settings.preset = ""

		pool_sync_called = []
		creds_sync_called = []

		with (
			frappe_patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: pool_sync_called.append(kw),
			),
			frappe_patch(
				"jarvis.admin_client.post_update_llm_creds",
				side_effect=lambda **kw: creds_sync_called.append(kw) or {"action": "restart"},
			),
		):
			settings.save()

		# Proxy path must NOT have been called.
		self.assertEqual(
			pool_sync_called, [], "proxy pool path must NOT be called after transition to 1 model/no preset"
		)

		# Single-model path must have been taken (structural change fires restart).
		self.assertTrue(
			len(creds_sync_called) >= 1,
			"single-model creds path MUST be called after proxy→direct transition",
		)

		# Flags must be reset.
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			int(settings.proxy_active or 0),
			0,
			"proxy_active must be reset to 0 after transition to 1 model/no preset",
		)
		self.assertEqual(
			int(settings.proxy_recommended or 0),
			1,
			"proxy_recommended must be 1 when exactly 1 model is present",
		)


# ---------------------------------------------------------------------------
# Task 4 (RT4): sync hardening — worker re-reads at run time (dedup-safe)
# ---------------------------------------------------------------------------

from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
	_enqueued_sync_via_admin_pool,
)


class TestRT4PoolSyncReReadsAtRunTime(_RT3SettingsTestCase):
	"""Task 4 (RT4): The pool-sync worker must rebuild the payload from the
	CURRENT Jarvis Settings at run time rather than using the snapshot
	passed as job args.  This makes a fixed-job_id + deduplicate=True safe:
	a correction saved while the first job is still queued is naturally
	included when the job eventually executes.
	"""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("proxy_recommended", 0, update_modified=False)
		frappe.db.commit()

	# ------------------------------------------------------------------
	# (a) Worker pushes CURRENT settings, not a stale snapshot
	# ------------------------------------------------------------------

	def test_worker_pushes_current_settings_not_stale_snapshot(self):
		"""Simulate a 'correction' scenario:

		1. Save settings with model set A (model="gpt-4o").
		2. Before the queued job runs, update the DB to model set B
		   (model="gpt-3.5-turbo") — simulating a second save while the
		   first job is still pending.
		3. Run the worker directly (inline) with NO args.
		4. Assert the spec pushed to admin reflects set B (current DB),
		   not set A (original snapshot).
		"""
		from unittest.mock import patch

		# Step 1: save with model set A (2 models so proxy_active fires).
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			tier="strong",
			order=0,
			api_key="sk-set-a",
			base_url="https://api.openai.com",
		)
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-3.5-turbo",
			tier="cheap",
			order=1,
			api_key="sk-set-a-2",
			base_url="https://api.openai.com",
		)

		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()

		# Step 2: simulate a correction — update model set in DB to set B
		# without triggering another enqueue (direct DB manipulation).
		frappe.db.delete(
			"Jarvis LLM Pool Model", {"parenttype": "Jarvis Settings", "parent": "Jarvis Settings"}
		)
		frappe.db.commit()

		# Insert set B: only one corrected model (strong only, different model name).
		fresh = frappe.get_single("Jarvis Settings")
		_add_model_row(
			fresh,
			provider="openai_compat",
			model="claude-3-5-sonnet",
			tier="strong",
			order=0,
			api_key="sk-set-b",
			base_url="https://api.anthropic.com",
		)
		_add_model_row(
			fresh,
			provider="openai_compat",
			model="claude-3-haiku",
			tier="cheap",
			order=1,
			api_key="sk-set-b-2",
			base_url="https://api.anthropic.com",
		)
		# Directly save model rows via the document's child table mechanism.
		for row in fresh.models:
			row.parent = "Jarvis Settings"
			row.parenttype = "Jarvis Settings"
			row.parentfield = "models"
			row.insert()
		frappe.db.commit()

		# Step 3: run the worker directly (no snapshot args — worker re-reads).
		pushed_specs = []

		with patch(
			"jarvis.admin_client.post_update_llm_pool",
			side_effect=lambda **kw: pushed_specs.append(kw) or {"action": "pool_update"},
		):
			_enqueued_sync_via_admin_pool()

		# Step 4: assert set B was pushed (model names from set B).
		self.assertTrue(pushed_specs, "post_update_llm_pool must have been called by the worker")
		pushed_models = pushed_specs[0]["spec"]["models"]
		pushed_model_names = {m["model"] for m in pushed_models}
		self.assertIn(
			"claude-3-5-sonnet",
			pushed_model_names,
			"worker must push CURRENT model set B (claude-3-5-sonnet), not stale set A (gpt-4o)",
		)
		self.assertNotIn("gpt-4o", pushed_model_names, "worker must NOT push stale snapshot model gpt-4o")

	# ------------------------------------------------------------------
	# (b) AdminRateLimitedError → terminal failure status (pool path)
	# ------------------------------------------------------------------

	def test_pool_sync_rate_limit_writes_terminal_failure_status(self):
		"""AdminRateLimitedError from post_update_llm_pool must write
		last_sync_status starting with 'failed: rate-limited' — matching
		the single-model path wording — so the UI poller stops spinning."""
		from unittest.mock import patch

		from jarvis.exceptions import AdminRateLimitedError

		# Save with 2 models so proxy_active=1 and last_sync_status is set to pending.
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			tier="strong",
			order=0,
			api_key="sk-rl-key-1",
			base_url="https://api.openai.com",
		)
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-3.5-turbo",
			tier="cheap",
			order=1,
			api_key="sk-rl-key-2",
			base_url="https://api.openai.com",
		)

		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()

		# Now run the worker with a rate-limited admin response.
		err = AdminRateLimitedError("too many requests", retry_after_seconds=90)
		with patch("jarvis.admin_client.post_update_llm_pool", side_effect=err):
			_enqueued_sync_via_admin_pool()

		settings = frappe.get_single("Jarvis Settings")
		status = settings.last_sync_status or ""
		self.assertTrue(
			status.startswith("failed: rate-limited"),
			f"Expected last_sync_status to start with 'failed: rate-limited', got: {status!r}",
		)


# ---------------------------------------------------------------------------
# Task 5 (RT5): onboarding writes models[0] (table is the source of truth)
# ---------------------------------------------------------------------------


class TestRT5OnboardingWritesModelsRow(_RT3SettingsTestCase):
	"""Task 5 (RT5): save_llm_creds with auth_mode='api_key' must upsert
	Jarvis Settings.models[0] (table is source of truth) so that:

	(a) models[0] carries provider / model / base_url / credential_type='api_key'
	(b) get_password on the row returns the api_key (encrypted via on_update mirror)
	(c) legacy llm_provider / llm_model / llm_auth_mode are populated by the mirror
	(d) is_ready_for_chat() returns ready=True after save_llm_creds (api_key path)
	(e) auth_mode='oauth' is left on the legacy direct path (models table untouched)
	"""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		# Seed admin creds so is_ready_for_chat passes the signup gate.
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("jarvis_admin_api_key", "test-admin-key", update_modified=False)
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("proxy_recommended", 0, update_modified=False)
		frappe.db.commit()

	# ------------------------------------------------------------------
	# (a)+(b)+(c) models[0] row created with correct shape + encrypted key
	# ------------------------------------------------------------------

	def test_api_key_onboarding_upserts_models_row(self):
		"""save_llm_creds(auth_mode='api_key') must create models[0] with
		provider / model / base_url / credential_type='api_key' and the
		api_key retrievable via get_password on the row."""
		from unittest.mock import patch

		with patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}):
			from jarvis import onboarding

			onboarding.save_llm_creds(
				provider="openai_compat",
				model="gpt-4o",
				api_key="sk-onboard-test-key",
				base_url="https://api.openai.com",
				auth_mode="api_key",
			)

		settings = frappe.get_single("Jarvis Settings")
		models = settings.get("models") or []
		self.assertTrue(len(models) >= 1, "models table must have at least one row after api_key onboarding")

		row = models[0]
		self.assertEqual(
			row.provider, "openai_compat", "models[0].provider must match the onboarding provider"
		)
		self.assertEqual(row.model, "gpt-4o", "models[0].model must match the onboarding model")
		self.assertEqual(
			row.base_url, "https://api.openai.com", "models[0].base_url must match the onboarding base_url"
		)
		self.assertEqual(row.credential_type, "api_key", "models[0].credential_type must be 'api_key'")
		self.assertEqual(int(row.enabled or 0), 1, "models[0].enabled must be 1")
		self.assertEqual(int(row.order or 0), 0, "models[0].order must be 0")

		# api_key must be stored encrypted, readable via get_password.
		recovered = row.get_password("api_key", raise_exception=False)
		self.assertEqual(
			recovered,
			"sk-onboard-test-key",
			"models[0].get_password('api_key') must return the onboarding key",
		)

	# ------------------------------------------------------------------
	# (c) Legacy mirror fields are populated by on_update
	# ------------------------------------------------------------------

	def test_api_key_onboarding_mirrors_legacy_fields(self):
		"""After save_llm_creds(api_key), on_update must mirror models[0]
		into the legacy llm_provider / llm_model / llm_auth_mode / llm_api_key
		fields so existing downstream readers (chat worker, is_ready_for_chat)
		continue to work unchanged."""
		from unittest.mock import patch

		with patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}):
			from jarvis import onboarding

			onboarding.save_llm_creds(
				provider="openai_compat",
				model="gpt-4o-mini",
				api_key="sk-mirror-test",
				base_url="https://api.openai.com",
				auth_mode="api_key",
			)

		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			settings.llm_provider, "openai_compat", "llm_provider must be mirrored from models[0]"
		)
		self.assertEqual(settings.llm_model, "gpt-4o-mini", "llm_model must be mirrored from models[0]")
		self.assertEqual(
			settings.llm_auth_mode, "api_key", "llm_auth_mode must be mirrored as 'api_key' from models[0]"
		)
		recovered_key = settings.get_password("llm_api_key", raise_exception=False)
		self.assertEqual(
			recovered_key,
			"sk-mirror-test",
			"get_password('llm_api_key') must return the onboarding key via mirror",
		)

	# ------------------------------------------------------------------
	# (d) is_ready_for_chat returns ready after api_key onboarding
	# ------------------------------------------------------------------

	def test_api_key_onboarding_leaves_is_ready_for_chat_passing(self):
		"""is_ready_for_chat must return ready=True after a successful (confirmed,
		status=applied) api_key onboarding via save_llm_creds."""
		from unittest.mock import patch

		with patch(
			"jarvis.admin_client.post_update_llm_creds",
			return_value={"action": "restart", "status": "applied"},
		):
			from jarvis import onboarding

			onboarding.save_llm_creds(
				provider="openai_compat",
				model="gpt-4o",
				api_key="sk-readiness-test",
				base_url="https://api.openai.com",
				auth_mode="api_key",
			)

		from jarvis.account import is_ready_for_chat

		result = is_ready_for_chat()
		self.assertTrue(
			result.get("ready"),
			f"is_ready_for_chat must return ready=True after api_key onboarding; got: {result}",
		)
		self.assertIsNone(
			result.get("reason"), f"reason must be None when ready; got: {result.get('reason')!r}"
		)

	def test_api_key_first_apply_still_applying_is_not_ready(self):
		"""Round-4 review R4-P0-6 / P1-10: a FIRST direct apply that admin returns
		as status="applying" (busy lock / timeout / CAS refusal) must NOT open
		chat — local key/provider/model presence alone is config intent, not proof
		the container received the creds. Gated on the durable llm_direct_synced_at
		marker, which is stamped only on a confirmed status=applied."""
		from unittest.mock import patch

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_direct_synced_at", None, update_modified=False)
		frappe.db.commit()
		# get_connection must be pinned not-Ready: the applying path now runs the
		# in-job convergence probe, and an unmocked call could reach a live admin.
		with (
			patch(
				"jarvis.admin_client.post_update_llm_creds",
				return_value={"action": "restart", "status": "applying"},
			),
			patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Configuring"}),
		):
			from jarvis import onboarding

			onboarding.save_llm_creds(
				provider="openai_compat",
				model="gpt-4o",
				api_key="sk-never-confirmed",
				base_url="https://api.openai.com",
				auth_mode="api_key",
			)
		from jarvis.account import is_ready_for_chat

		settings = frappe.get_single("Jarvis Settings")
		self.assertIsNone(settings.llm_direct_synced_at, "an 'applying' first apply is not confirmed")
		result = is_ready_for_chat()
		self.assertFalse(
			result.get("ready"), f"a never-confirmed direct tenant must not be chat-ready; got: {result}"
		)
		self.assertEqual(result.get("reason"), "llm_provisioning")

	def test_api_key_first_apply_applying_then_ready_converges_and_stamps_marker(self):
		"""The direct analogue of the pool convergence test: an 'applying' first
		direct apply whose in-job probe finds admin already Ready must stamp
		llm_direct_synced_at (via _stamp_converged_ok) and open chat — without
		this a converged-via-reconcile direct tenant would be stranded at
		llm_provisioning forever (an 'ok' status stops every later reconcile)."""
		from unittest.mock import patch

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_direct_synced_at", None, update_modified=False)
		frappe.db.commit()
		with (
			patch(
				"jarvis.admin_client.post_update_llm_creds",
				return_value={"action": "restart", "status": "applying"},
			),
			patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Ready"}),
		):
			from jarvis import onboarding

			onboarding.save_llm_creds(
				provider="openai_compat",
				model="gpt-4o",
				api_key="sk-converges",
				base_url="https://api.openai.com",
				auth_mode="api_key",
			)
		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue(
			settings.llm_direct_synced_at,
			"convergence to chat_readiness Ready must stamp llm_direct_synced_at",
		)
		self.assertTrue(
			(settings.last_sync_status or "").startswith("ok"),
			f"a converged apply must read ok; got {settings.last_sync_status!r}",
		)
		from jarvis.account import is_ready_for_chat

		self.assertTrue(is_ready_for_chat().get("ready"), "a converged first direct apply must open chat")

	def test_established_direct_tenant_stays_ready_through_resave_pending(self):
		"""An already-confirmed direct tenant (marker set) keeps chatting on its
		previous key while a re-save is transiently 'applying'."""
		from jarvis.account import is_ready_for_chat

		settings = frappe.get_single("Jarvis Settings")
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			_PENDING_APPLYING_STATUS,
		)

		settings.db_set(
			{
				"llm_auth_mode": "api_key",
				"llm_provider": "openai_compat",
				"llm_model": "gpt-4o",
				"proxy_active": 0,
				"llm_direct_synced_at": frappe.utils.now(),
				"last_sync_status": _PENDING_APPLYING_STATUS,
			},
			update_modified=False,
		)
		settings.db_set("llm_api_key", "sk-established")
		frappe.db.commit()
		self.assertTrue(
			is_ready_for_chat().get("ready"),
			"an established direct tenant must stay ready during a pending re-save",
		)

	# ------------------------------------------------------------------
	# (e) OAuth mode leaves the models table untouched
	# ------------------------------------------------------------------

	def test_oauth_mode_does_not_write_models_row(self):
		"""save_llm_creds(auth_mode='oauth') must NOT write to the models
		table — direct-OAuth single-model uses the legacy field path."""
		from unittest.mock import patch

		# Pre-condition: no models rows.
		settings = frappe.get_single("Jarvis Settings")
		initial_count = len(settings.get("models") or [])
		# The comment above states this pre-condition; assert it rather than
		# assume it - if rows already existed the assertions below could pass
		# for the wrong reason.
		self.assertEqual(initial_count, 0)

		with patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}):
			from jarvis import onboarding

			onboarding.save_llm_creds(
				provider="OpenAI",
				model="gpt-4o",
				api_key="",
				base_url="",
				auth_mode="oauth",
			)

		settings = frappe.get_single("Jarvis Settings")
		final_count = len(settings.get("models") or [])
		self.assertEqual(final_count, 0, "oauth mode must leave the models table empty (0 rows)")

	# ------------------------------------------------------------------
	# (f) OAuth mode with multi-model pool raises error (data-loss guard)
	# ------------------------------------------------------------------

	def test_oauth_mode_with_multi_model_pool_raises_error(self):
		"""save_llm_creds(auth_mode='oauth') with ≥2 enabled models must raise
		frappe.ValidationError and NOT clear the models table (data-loss guard)."""
		from unittest.mock import patch

		# Pre-condition: configure a multi-model pool with 2 enabled models.
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			tier="strong",
			order=0,
			enabled=1,
			api_key="sk-pool-key-1",
			base_url="https://api.openai.com",
		)
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-3.5-turbo",
			tier="cheap",
			order=1,
			enabled=1,
			api_key="sk-pool-key-2",
			base_url="https://api.openai.com",
		)
		settings.save()

		# Confirm 2 models exist before attempting OAuth switch.
		settings = frappe.get_single("Jarvis Settings")
		pre_count = len(settings.get("models") or [])
		self.assertEqual(pre_count, 2, "pre-condition: must have 2 enabled models")

		# Attempt to switch to OAuth with the multi-model pool in place.
		# This must raise ValidationError and NOT clear the models table.
		with patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}):
			from jarvis import onboarding

			with self.assertRaises(frappe.ValidationError) as ctx:
				onboarding.save_llm_creds(
					provider="OpenAI",
					model="gpt-4o",
					api_key="",
					base_url="",
					auth_mode="oauth",
				)

		# Assert the error message is clear about the guard.
		error_msg = str(ctx.exception)
		self.assertIn(
			"multi-model", error_msg.lower(), f"Error message must mention multi-model; got: {error_msg}"
		)
		self.assertIn(
			"remove", error_msg.lower(), f"Error message must mention removing models; got: {error_msg}"
		)

		# Assert the models table was NOT cleared.
		settings = frappe.get_single("Jarvis Settings")
		post_count = len(settings.get("models") or [])
		self.assertEqual(post_count, 2, "models table must NOT be cleared when guard is triggered")


# ---------------------------------------------------------------------------
# FT1: Migration patch tests — v1_seed_llm_models.py
#
# TDD RED→GREEN tests for the deploy-blocker fix:
#   (a) oauth-mode tenant → execute() must NOT raise, models stays empty
#   (b) blank-key api_key tenant → execute() must NOT raise, models stays empty
#   (c) api_key tenant WITH a key → execute() seeds models[0]
#   (d) no admin/network sync is enqueued during migrate
# ---------------------------------------------------------------------------


class TestFT1MigrationPatch(_RT3SettingsTestCase):
	"""FT1: Verify v1_seed_llm_models.execute() is safe for all credential modes.

	The patch must:
	- NOT raise for oauth/subscription tenants (legacy path, models stays empty)
	- NOT raise for blank-key api_key tenants (models stays empty)
	- Seed models[0] only for api_key tenants with a non-blank key
	- NOT enqueue an admin sync (no network calls during bench migrate)
	"""

	def setUp(self):
		super().setUp()
		self._clear_models()
		# Clean state: no models, no preset
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("proxy_recommended", 0, update_modified=False)
		settings.db_set("llm_provider", "", update_modified=False)
		settings.db_set("llm_model", "", update_modified=False)
		settings.db_set("llm_base_url", "", update_modified=False)
		settings.db_set("llm_auth_mode", "", update_modified=False)
		# Clear llm_api_key from both __Auth (encrypted) and tabSingles (plaintext).
		# tabSingles may store a non-masked value if legacy code wrote it directly;
		# db_set("", ...) clears it so get_password() returns "" in the next test.
		settings.db_set("llm_api_key", "", update_modified=False)
		from frappe.utils.password import remove_encrypted_password

		try:
			remove_encrypted_password("Jarvis Settings", "Jarvis Settings", "llm_api_key")
		except Exception:
			pass
		frappe.db.commit()

	def _set_legacy_fields(
		self,
		*,
		llm_model,
		llm_auth_mode,
		llm_api_key=None,
		llm_provider="OpenAI",
		llm_base_url="https://api.openai.com",
	):
		"""Seed the legacy single-model fields (without triggering on_update)."""
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_model", llm_model, update_modified=False)
		settings.db_set("llm_auth_mode", llm_auth_mode, update_modified=False)
		settings.db_set("llm_provider", llm_provider, update_modified=False)
		settings.db_set("llm_base_url", llm_base_url, update_modified=False)
		if llm_api_key:
			from frappe.utils.password import set_encrypted_password

			set_encrypted_password(
				"Jarvis Settings",
				"Jarvis Settings",
				llm_api_key,
				"llm_api_key",
			)
		frappe.db.commit()

	# ------------------------------------------------------------------ #
	# (a) oauth-mode tenant → execute() must NOT raise, models stays empty
	# ------------------------------------------------------------------ #

	def test_oauth_tenant_patch_does_not_raise_and_leaves_models_empty(self):
		"""FT1(a): oauth-mode tenant: execute() must NOT raise, models stays empty."""
		from unittest.mock import patch as mock_patch

		self._set_legacy_fields(
			llm_model="gpt-4o",
			llm_auth_mode="oauth",
			llm_api_key=None,
		)

		with (
			mock_patch("jarvis.admin_client.post_update_llm_creds") as mock_creds,
			mock_patch("jarvis.admin_client.post_update_llm_pool") as mock_pool,
		):
			try:
				import importlib

				from jarvis.patches import v1_seed_llm_models

				importlib.reload(v1_seed_llm_models)
				v1_seed_llm_models.execute()
			except Exception as exc:
				self.fail(f"v1_seed_llm_models.execute() raised for oauth tenant: {exc}")

		settings = frappe.get_single("Jarvis Settings")
		models = settings.get("models") or []
		self.assertEqual(len(models), 0, "oauth tenant: models must stay empty after migration patch")

		# No admin sync must be enqueued
		mock_creds.assert_not_called()
		mock_pool.assert_not_called()

	# ------------------------------------------------------------------ #
	# (b) subscription-mode tenant → execute() must NOT raise, models empty
	# ------------------------------------------------------------------ #

	def test_subscription_tenant_patch_does_not_raise_and_leaves_models_empty(self):
		"""FT1(a-sub): subscription-mode tenant: execute() must NOT raise, models stays empty."""
		from unittest.mock import patch as mock_patch

		self._set_legacy_fields(
			llm_model="gpt-4o",
			llm_auth_mode="subscription",
			llm_api_key=None,
		)

		with (
			mock_patch("jarvis.admin_client.post_update_llm_creds") as mock_creds,
			mock_patch("jarvis.admin_client.post_update_llm_pool") as mock_pool,
		):
			try:
				import importlib

				from jarvis.patches import v1_seed_llm_models

				importlib.reload(v1_seed_llm_models)
				v1_seed_llm_models.execute()
			except Exception as exc:
				self.fail(f"v1_seed_llm_models.execute() raised for subscription tenant: {exc}")

		settings = frappe.get_single("Jarvis Settings")
		models = settings.get("models") or []
		self.assertEqual(len(models), 0, "subscription tenant: models must stay empty after migration patch")

		mock_creds.assert_not_called()
		mock_pool.assert_not_called()

	# ------------------------------------------------------------------ #
	# (b) blank-key api_key tenant → execute() must NOT raise, models empty
	# ------------------------------------------------------------------ #

	def test_blank_key_api_key_tenant_patch_does_not_raise_and_leaves_models_empty(self):
		"""FT1(b): blank-key api_key tenant: execute() must NOT raise, models stays empty."""
		from unittest.mock import patch as mock_patch

		# api_key mode but NO key set (llm_api_key blank/absent)
		self._set_legacy_fields(
			llm_model="gpt-4o",
			llm_auth_mode="api_key",
			llm_api_key=None,  # blank
		)

		with (
			mock_patch("jarvis.admin_client.post_update_llm_creds") as mock_creds,
			mock_patch("jarvis.admin_client.post_update_llm_pool") as mock_pool,
		):
			try:
				import importlib

				from jarvis.patches import v1_seed_llm_models

				importlib.reload(v1_seed_llm_models)
				v1_seed_llm_models.execute()
			except Exception as exc:
				self.fail(f"v1_seed_llm_models.execute() raised for blank-key api_key tenant: {exc}")

		settings = frappe.get_single("Jarvis Settings")
		models = settings.get("models") or []
		self.assertEqual(
			len(models), 0, "blank-key api_key tenant: models must stay empty after migration patch"
		)

		mock_creds.assert_not_called()
		mock_pool.assert_not_called()

	# ------------------------------------------------------------------ #
	# (c) api_key tenant WITH key → execute() seeds models[0]
	# ------------------------------------------------------------------ #

	def test_api_key_tenant_with_key_patch_seeds_models_row(self):
		"""FT1(c): api_key tenant with non-blank key: execute() seeds models[0]."""
		from unittest.mock import patch as mock_patch

		self._set_legacy_fields(
			llm_model="gpt-4o",
			llm_auth_mode="api_key",
			llm_api_key="sk-migrate-test-key-xyz",
			llm_provider="OpenAI",
			llm_base_url="https://api.openai.com",
		)

		with (
			mock_patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}),
			mock_patch("jarvis.admin_client.post_update_llm_pool") as mock_pool,
		):
			import importlib

			from jarvis.patches import v1_seed_llm_models

			importlib.reload(v1_seed_llm_models)
			v1_seed_llm_models.execute()

		settings = frappe.get_single("Jarvis Settings")
		models = settings.get("models") or []
		self.assertEqual(
			len(models), 1, "api_key tenant with key: must have exactly 1 model row after migration"
		)

		row = models[0]
		self.assertEqual(row.credential_type, "api_key", "seeded row credential_type must be 'api_key'")
		self.assertEqual(row.model, "gpt-4o", "seeded row model must match legacy llm_model")
		self.assertEqual(int(row.enabled or 0), 1, "seeded row must be enabled")

		# Pool sync must NOT have been called (1 model → single-model path, not proxy)
		mock_pool.assert_not_called()

	# ------------------------------------------------------------------ #
	# (c-idempotent) second execute() with existing models → no-op
	# ------------------------------------------------------------------ #

	def test_api_key_patch_is_idempotent_when_models_already_seeded(self):
		"""FT1(c-idempotent): second execute() when models already exist → no-op (no duplicate rows)."""
		from unittest.mock import patch as mock_patch

		self._set_legacy_fields(
			llm_model="gpt-4o",
			llm_auth_mode="api_key",
			llm_api_key="sk-idempotent-key",
		)

		with mock_patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}):
			import importlib

			from jarvis.patches import v1_seed_llm_models

			importlib.reload(v1_seed_llm_models)
			v1_seed_llm_models.execute()

		# Run again — must not add a second row
		with mock_patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}):
			importlib.reload(v1_seed_llm_models)
			v1_seed_llm_models.execute()

		settings = frappe.get_single("Jarvis Settings")
		models = settings.get("models") or []
		self.assertEqual(len(models), 1, "idempotent: second execute() must NOT add a duplicate row")

	# ------------------------------------------------------------------ #
	# (d) no admin sync enqueued during migrate
	# ------------------------------------------------------------------ #

	def test_migrate_does_not_enqueue_admin_sync_for_api_key_tenant(self):
		"""FT1(d): During migrate, execute() must NOT enqueue an admin sync call.

		The in_llm_migrate flag (or equivalent) must suppress the sync enqueue
		so that bench migrate does not make per-tenant admin/network calls.
		"""
		from unittest.mock import patch as mock_patch

		self._set_legacy_fields(
			llm_model="gpt-4o",
			llm_auth_mode="api_key",
			llm_api_key="sk-no-sync-test",
			llm_provider="OpenAI",
			llm_base_url="https://api.openai.com",
		)

		enqueued_calls = []

		def capture_enqueue(*args, **kwargs):
			# Capture any enqueue calls
			method = args[0] if args else kwargs.get("method", "")
			if "sync" in str(method).lower() or "admin" in str(method).lower():
				enqueued_calls.append({"method": method, "kwargs": kwargs})
			# Still call original but prevent actual network calls
			return None

		with (
			mock_patch("frappe.enqueue", side_effect=capture_enqueue),
			mock_patch("jarvis.admin_client.post_update_llm_creds") as mock_creds,
			mock_patch("jarvis.admin_client.post_update_llm_pool") as mock_pool,
		):
			import importlib

			from jarvis.patches import v1_seed_llm_models

			importlib.reload(v1_seed_llm_models)
			v1_seed_llm_models.execute()

		# No admin sync must be enqueued during migration
		self.assertEqual(
			enqueued_calls,
			[],
			f"execute() must NOT enqueue any admin sync during migrate; got: {enqueued_calls}",
		)
		mock_creds.assert_not_called()
		mock_pool.assert_not_called()


# ---------------------------------------------------------------------------
# FT2: validate-vs-mirror timing
# ---------------------------------------------------------------------------


class TestFT2ValidateMirrorTiming(_RT3SettingsTestCase):
	"""FT2: before_validate mirrors models[0] so validate() sees fresh values."""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("proxy_recommended", 0, update_modified=False)
		settings.db_set("jarvis_admin_api_key", "test-admin-key-ft2", update_modified=False)
		frappe.db.commit()

	def test_table_api_key_rotation_enqueues_reload(self):
		"""Changing only the table row's api_key → a credential sync (reload or restart) is enqueued.

		'reload' calls post_rotate_llm_secret; 'restart' calls post_update_llm_creds.
		Either is acceptable — what matters is that an admin sync fires.
		"""
		from unittest.mock import patch

		# First, set up with initial key.
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			api_key="sk-initial-key",
			base_url="https://api.openai.com",
		)
		settings.preset = ""

		with (
			patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}),
			patch("jarvis.admin_client.post_rotate_llm_secret", return_value={"action": "reload"}),
		):
			settings.save()

		frappe.db.commit()

		# Now rotate the key only in the table row (not in the legacy field).
		settings = frappe.get_single("Jarvis Settings")
		# Update the model row's api_key
		for row in settings.models:
			if row.model == "gpt-4o":
				row.api_key = "sk-rotated-key"
		# Do NOT touch legacy llm_api_key — before_validate should mirror it.

		creds_calls = []
		rotate_calls = []
		with (
			patch(
				"jarvis.admin_client.post_update_llm_creds",
				side_effect=lambda **kw: creds_calls.append(kw) or {"action": "restart"},
			),
			patch(
				"jarvis.admin_client.post_rotate_llm_secret",
				side_effect=lambda **kw: rotate_calls.append(kw) or {"action": "reload"},
			),
		):
			settings.save()

		# Some admin call (reload or restart) must have been made.
		total_calls = len(creds_calls) + len(rotate_calls)
		self.assertTrue(
			total_calls >= 1,
			"A credential sync (reload or restart) must be enqueued when table api_key rotates",
		)

	def test_fresh_tenant_no_preseeded_key_can_save(self):
		"""Fresh tenant: no llm_api_key in DB, save with model row having api_key → no ValidationError."""
		from unittest.mock import patch

		# Ensure no llm_api_key in DB.
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_api_key", "", update_modified=False)
		from frappe.utils.password import remove_encrypted_password

		try:
			remove_encrypted_password("Jarvis Settings", "Jarvis Settings", "llm_api_key")
		except Exception:
			pass
		frappe.db.commit()

		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			api_key="sk-fresh-tenant-key",
			base_url="https://api.openai.com",
		)
		settings.preset = ""

		with patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}):
			try:
				settings.save()
			except frappe.ValidationError as exc:
				self.fail(f"Fresh tenant save raised ValidationError unexpectedly: {exc}")

	def test_proxy_active_pool_is_ready_for_chat(self):
		"""proxy_active=1 + an APPLIED pool (status ok) → ready.

		proxy_active alone no longer suffices: it is config intent, committed
		at save time BEFORE the async sync runs. Evidence of a successful
		apply (llm_pool_synced_at, or an 'ok' status for tenants provisioned
		before that marker existed) is what opens the gate.
		"""
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("proxy_active", 1, update_modified=False)
		settings.db_set("llm_oauth_connected_at", None, update_modified=False)
		settings.db_set("llm_pool_synced_at", frappe.utils.now(), update_modified=False)
		frappe.db.commit()

		from jarvis.account import is_ready_for_chat

		result = is_ready_for_chat()
		self.assertTrue(
			result.get("ready"),
			f"an applied pool must make is_ready_for_chat return ready=True; got: {result}",
		)


# ---------------------------------------------------------------------------
# FT3: empty/invalid pool guards
# ---------------------------------------------------------------------------


class TestFT3EmptyPoolGuards(_RT3SettingsTestCase):
	"""FT3: Guards against empty/stale-preset pool pushes."""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("proxy_recommended", 0, update_modified=False)
		frappe.db.commit()

	def test_preset_with_zero_enabled_models_raises_validation(self):
		"""preset set + all models disabled → ValidationError with 'at least 1' in message."""
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			api_key="sk-disabled-key",
			base_url="https://api.openai.com",
			enabled=0,
		)  # disabled!
		settings.preset = "Cost-saver"

		with self.assertRaises(frappe.ValidationError) as ctx:
			settings.save()

		error_msg = str(ctx.exception)
		self.assertIn("at least 1", error_msg.lower(), f"Error must mention 'at least 1'; got: {error_msg}")

	def test_oauth_save_llm_creds_clears_preset(self):
		"""save_llm_creds(auth_mode='oauth') clears preset."""
		from unittest.mock import patch

		# Set a preset first.
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "Cost-saver", update_modified=False)
		frappe.db.commit()

		with patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}):
			from jarvis import onboarding

			onboarding.save_llm_creds(
				provider="OpenAI",
				model="gpt-4o",
				api_key="",
				base_url="",
				auth_mode="oauth",
			)

		settings = frappe.get_single("Jarvis Settings")
		preset_val = settings.get("preset") or ""
		self.assertEqual(
			preset_val, "", f"preset must be cleared after oauth save_llm_creds; got: {preset_val!r}"
		)

	def test_pool_worker_skips_push_when_no_longer_proxy_valid(self):
		"""Worker re-validates at run time: if no models left → skips push, sets 'skipped' status."""
		from unittest.mock import patch

		# Set up proxy-valid state (2 models).
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			tier="strong",
			order=0,
			api_key="sk-worker-key-1",
			base_url="https://api.openai.com",
		)
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-3.5-turbo",
			tier="cheap",
			order=1,
			api_key="sk-worker-key-2",
			base_url="https://api.openai.com",
		)
		settings.preset = ""

		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()

		frappe.db.commit()

		# Now delete models directly so worker sees 0 models.
		frappe.db.delete(
			"Jarvis LLM Pool Model", {"parenttype": "Jarvis Settings", "parent": "Jarvis Settings"}
		)
		frappe.db.commit()

		# Run worker — it should skip, NOT call post_update_llm_pool.
		pool_sync_called = []
		with patch(
			"jarvis.admin_client.post_update_llm_pool",
			side_effect=lambda **kw: pool_sync_called.append(kw) or {"action": "pool_update"},
		):
			_enqueued_sync_via_admin_pool()

		self.assertEqual(
			pool_sync_called,
			[],
			"post_update_llm_pool must NOT be called when models deleted before worker runs",
		)

		settings = frappe.get_single("Jarvis Settings")
		status = settings.last_sync_status or ""
		self.assertIn("skipped", status.lower(), f"last_sync_status must contain 'skipped'; got: {status!r}")


# ---------------------------------------------------------------------------
# FT4: password masking + blob shape
# ---------------------------------------------------------------------------


class TestFT4PasswordMaskingAndBlobShape(FrappeTestCase):
	"""FT4: Password masking and oauth_blob shape validation."""

	def test_resave_db_backed_subscription_tenant_does_not_raise_malformed_blob(self):
		"""Re-reading a DB-backed subscription account's oauth_blob (masked) and
		running validate_models must NOT produce a 'malformed' error."""
		from jarvis.jarvis.pool_serialize import validate_models

		# Build in-memory account with oauth_blob set (simulating a DB-backed row
		# where the field is readable via _get_password which returns the real value).
		acc = frappe._dict(
			upstream="openai",
			account_ref="ACC_MASKED_001",
			label="test@example.com",
			oauth_blob='{"token": "real_value", "refresh": "abc"}',
		)
		m = _subscription_model(accounts=[acc])
		settings = _make_settings_with_models([m])

		# Should not produce a 'malformed' error.
		errors = validate_models(settings)
		malformed_errors = [e for e in errors if "malformed" in e.lower()]
		self.assertEqual(
			malformed_errors, [], f"No malformed errors expected for valid oauth_blob; got: {errors}"
		)

	def test_non_dict_blob_is_rejected(self):
		"""oauth_blob that parses to a list → validate_models returns error containing 'dict' or 'object'."""
		from jarvis.jarvis.pool_serialize import validate_models

		acc = frappe._dict(
			upstream="openai",
			account_ref="ACC_LIST_BLOB",
			label="test@example.com",
			oauth_blob="[1, 2, 3]",  # list, not dict
		)
		m = _subscription_model(accounts=[acc])
		settings = _make_settings_with_models([m])

		errors = validate_models(settings)
		self.assertTrue(
			any("dict" in e.lower() or "object" in e.lower() for e in errors),
			f"Expected error about non-dict blob; got: {errors}",
		)

	def test_get_password_does_not_collapse_decrypt_error_to_blank(self):
		"""_get_password must propagate when the field is non-empty but get_password raises."""
		from unittest.mock import MagicMock

		from jarvis.jarvis.pool_serialize import _get_password

		# Simulate a doc whose get_password raises even though the field has content.
		doc = MagicMock()
		doc.get_password.side_effect = Exception("decryption failed")
		doc.oauth_blob = "masked_value"  # non-empty field
		doc.get = MagicMock(return_value="masked_value")

		with self.assertRaises(Exception):
			_get_password(doc, "oauth_blob")


# ---------------------------------------------------------------------------
# FT5: chat-worker pool-awareness + routing_mode + worker error
# ---------------------------------------------------------------------------


class TestFT5ChatWorkerPoolAwareness(_RT3SettingsTestCase):
	"""FT5: Chat worker pool-awareness and routing_mode field."""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("proxy_recommended", 0, update_modified=False)
		frappe.db.commit()

	def _make_conv(self, model_override=""):
		"""Make a minimal conversation-like object."""
		return frappe._dict(model_override=model_override)

	def _set_proxy_active(self, models_list):
		"""Set proxy_active=1 and persist model rows to DB."""
		from unittest.mock import patch

		settings = frappe.get_single("Jarvis Settings")
		for m in models_list:
			_add_model_row(settings, **m)
		# Save with admin calls mocked so the model rows are persisted to DB.
		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()
		# Force proxy_active=1 in DB (in case 2 models triggered it already;
		# this handles the case where tests add only models via this helper
		# and proxy_active should be 1 for the pool routing assertion).
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "proxy_active", 1, update_modified=False)
		frappe.db.commit()

	def _pool_of_two(self):
		self._set_proxy_active(
			[
				{
					"provider": "openai_compat",
					"model": "gpt-4o",
					"tier": "strong",
					"order": 0,
					"api_key": "sk-p1",
				},
				{
					"provider": "openai_compat",
					"model": "gpt-3.5-turbo",
					"tier": "cheap",
					"order": 1,
					"api_key": "sk-p2",
				},
			]
		)

	def test_pool_mode_resolver_still_returns_empty_for_the_one_shot_callers(self):
		"""_resolve_model_and_provider's contract is UNCHANGED: "" means "no opinion".

		Two callers depend on that and must not be disturbed by the session-pin fix --
		the auto-title job (chat/title.py) and pattern-polish (learning/polish.py) run
		throwaway one-shot gateway turns and pass this straight through as an INLINE
		modelOverride, where "" correctly means "omit the field". Handing them a concrete
		"jarvis-pool" would push an explicit override down a path they never exercised,
		and both swallow their exceptions -- so a rejection would degrade silently into
		crude titles and unpolished patterns.
		"""
		from jarvis.chat.worker import _resolve_model_and_provider

		self._pool_of_two()
		for override in ("", "a-model-the-customer-deleted"):
			conv = self._make_conv(model_override=override)
			effective_model, provider = _resolve_model_and_provider(conv)
			self.assertEqual(
				effective_model,
				"",
				f"override={override!r} must resolve to '' (no opinion); got: {effective_model!r}",
			)
			self.assertIsNone(provider)

	def test_session_model_for_pool_mode_with_no_pin_names_the_pool(self):
		"""The SESSION patch must NAME the pool, not send nothing.

		handle_chat_send only issues sessions.patch when the model is truthy, and openclaw
		remembers a session's model across turns -- so "" ("send nothing") meant a
		conversation that had ONCE been pinned was never walked back. Selecting "Auto"
		wrote model_override="" and flipped the pill, while openclaw went right on calling
		the old model, forever. Clearing a pin has to be an explicit instruction, not the
		absence of one. (jarvis#299)
		"""
		from jarvis.chat.worker import POOL_VIRTUAL_MODEL, _session_model_for

		self._pool_of_two()
		conv = self._make_conv(model_override="")
		model, provider = _session_model_for(conv)
		self.assertEqual(model, POOL_VIRTUAL_MODEL)
		self.assertTrue(
			bool(model), "must be truthy, or handle_chat_send skips sessions.patch and the stale pin sticks"
		)
		self.assertIsNone(provider)

	def test_session_model_for_stale_pin_resets_to_the_pool(self):
		"""A pin naming a model the customer has since REMOVED from the pool resets to
		the pool rather than leaking a dead model id through to openclaw."""
		from jarvis.chat.worker import POOL_VIRTUAL_MODEL, _session_model_for

		self._pool_of_two()
		conv = self._make_conv(model_override="a-model-the-customer-deleted")
		model, provider = _session_model_for(conv)
		self.assertEqual(model, POOL_VIRTUAL_MODEL)
		self.assertIsNone(provider)

	def test_session_model_for_honours_a_valid_pin(self):
		from jarvis.chat.worker import _session_model_for

		self._pool_of_two()
		conv = self._make_conv(model_override="gpt-3.5-turbo")
		model, provider = _session_model_for(conv)
		self.assertEqual(model, "gpt-3.5-turbo")
		self.assertIsNone(provider)

	def test_pool_mode_validates_override_against_enabled_models(self):
		"""proxy_active=1, model_override in enabled models → override returned."""
		from jarvis.chat.worker import _resolve_model_and_provider

		self._set_proxy_active(
			[
				{
					"provider": "openai_compat",
					"model": "gpt-4o",
					"tier": "strong",
					"order": 0,
					"api_key": "sk-p1",
				},
				{
					"provider": "openai_compat",
					"model": "gpt-3.5-turbo",
					"tier": "cheap",
					"order": 1,
					"api_key": "sk-p2",
				},
			]
		)

		conv = self._make_conv(model_override="gpt-4o")
		effective_model, _ = _resolve_model_and_provider(conv)
		self.assertEqual(
			effective_model, "gpt-4o", f"Valid pool override must be accepted; got: {effective_model!r}"
		)

	def test_worker_catches_admin_validation_error(self):
		"""AdminValidationError from pool sync → last_sync_status contains 'validation'."""
		from unittest.mock import patch

		from jarvis.exceptions import AdminValidationError

		# Set up 2 models so proxy path is taken.
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			tier="strong",
			order=0,
			api_key="sk-av-key-1",
			base_url="https://api.openai.com",
		)
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-3.5-turbo",
			tier="cheap",
			order=1,
			api_key="sk-av-key-2",
			base_url="https://api.openai.com",
		)

		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()

		err = AdminValidationError("pool model config rejected by admin")
		with patch("jarvis.admin_client.post_update_llm_pool", side_effect=err):
			_enqueued_sync_via_admin_pool()

		settings = frappe.get_single("Jarvis Settings")
		status = settings.last_sync_status or ""
		self.assertIn("validation", status.lower(), f"Expected 'validation' in status; got: {status!r}")

	def test_routing_mode_field_exists_in_settings_schema(self):
		"""Jarvis Settings must have routing_mode Select field."""
		meta = frappe.get_meta("Jarvis Settings")
		fields = {f.fieldname: f for f in meta.fields}
		self.assertIn("routing_mode", fields, "Jarvis Settings must have 'routing_mode' field")
		f = fields["routing_mode"]
		self.assertEqual(f.fieldtype, "Select", "routing_mode must be of type Select")


# ---------------------------------------------------------------------------
# FT4b: Real DB-backed validate_models tests (never-raises contract)
# ---------------------------------------------------------------------------


class TestFT4bValidateModelsNeverRaises(_RT3SettingsTestCase):
	"""FT4b: Real-DB and mock-decrypt tests verifying the 'never raises' contract
	of validate_models when reading oauth_blob / api_key via get_password.
	"""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("proxy_recommended", 0, update_modified=False)
		frappe.db.commit()

	def test_real_db_backed_subscription_resave_does_not_produce_malformed_error(self):
		"""FT4b DB-backed path: save a Jarvis Settings with a real subscription model +
		account whose oauth_blob is encrypted in the DB.  Re-loading the doc and
		running validate_models on the reloaded doc must NOT produce a 'malformed'
		error and must NOT raise.

		This exercises the real DB + get_password path (not the frappe._dict fallback
		used by test_resave_db_backed_subscription_tenant_does_not_raise_malformed_blob).
		"""
		from unittest.mock import patch

		# Step 1: build and save settings with a subscription model + account.
		settings = frappe.get_single("Jarvis Settings")

		# Subscription model with one account whose oauth_blob will be encrypted.
		sub_row = frappe.new_doc("Jarvis LLM Pool Model")
		sub_row.model = "gpt-5.5-db-backed"
		sub_row.tier = "cheap"
		sub_row.order = 99
		sub_row.enabled = 1
		sub_row.credential_type = "subscription"

		acc_row = frappe.new_doc("Jarvis LLM Pool Subscription Account")
		acc_row.upstream = "openai"
		acc_row.account_ref = "ACC_DB_BACKED_FT4B_001"
		acc_row.label = "db-backed-ft4b@example.com"
		acc_row.oauth_blob = '{"token": "real_token", "refresh": "real_refresh"}'
		sub_row.append("accounts", acc_row)

		settings.append("models", sub_row)

		# A second strong api_key model so proxy_active triggers (≥2 models).
		strong_row = frappe.new_doc("Jarvis LLM Pool Model")
		strong_row.model = "gpt-4o-db-backed"
		strong_row.tier = "strong"
		strong_row.order = 98
		strong_row.enabled = 1
		strong_row.credential_type = "api_key"
		strong_row.api_key = "sk-db-backed-ft4b-key"
		strong_row.provider = "openai_compat"
		strong_row.base_url = "https://api.openai.com"
		settings.append("models", strong_row)

		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()

		frappe.db.commit()

		# Step 2: reload from DB — oauth_blob will be masked "****" in-memory
		# but readable via row.get_password("oauth_blob") from __Auth table.
		from jarvis.jarvis.pool_serialize import validate_models

		reloaded = frappe.get_single("Jarvis Settings")

		try:
			errors = validate_models(reloaded)
		except Exception as exc:
			self.fail(f"validate_models raised on reloaded DB-backed doc: {exc}")

		malformed_errors = [e for e in errors if "malformed" in e.lower()]
		self.assertEqual(
			malformed_errors,
			[],
			f"Re-running validate_models on a reloaded DB-backed subscription doc must NOT "
			f"report 'malformed'; got errors: {errors}",
		)

	def test_validate_models_decrypt_error_returns_clean_error_string_not_raise(self):
		"""FT4b decrypt-error path: if get_password raises for oauth_blob on a DB-backed
		account row, validate_models must return a clean error string containing
		'cannot read' or 'decryption error' and must NOT propagate the exception.

		Mocks get_password to raise so the test is independent of real key material.
		"""
		from unittest.mock import MagicMock

		from jarvis.jarvis.pool_serialize import validate_models

		# Build an account object that looks like a real child-doc row
		# (has get_password callable) but whose get_password always raises.
		acc = MagicMock()
		acc.get_password = MagicMock(side_effect=Exception("simulated decryption failure"))
		# Non-empty field so _get_password knows the field is set and re-raises.
		acc.oauth_blob = "***masked***"
		acc.get = MagicMock(return_value="***masked***")
		acc.account_ref = "ACC_DECRYPT_ERR_FT4B"
		acc.upstream = "openai"
		acc.label = "decrypt-err@example.com"

		m = _subscription_model(accounts=[acc])
		settings = _make_settings_with_models([m])

		# validate_models must NOT raise.
		try:
			errors = validate_models(settings)
		except Exception as exc:
			self.fail(f"validate_models must never raise; got exception on decrypt error: {exc}")

		# Must return a clean, human-readable error string about the failure.
		self.assertTrue(
			any("decryption error" in e.lower() or "cannot read" in e.lower() for e in errors),
			f"Expected a clean 'decryption error' / 'cannot read' error string; got: {errors}",
		)


# ---------------------------------------------------------------------------
# JARVIS-2026-07-08 onboarding-audit fixes: durable sync status + honest
# pool readiness. (Faults (a)/(c) of the incident + the split-brain gate.)
# ---------------------------------------------------------------------------

from unittest.mock import patch


class TestOnboardingAuditFixes(_RT3SettingsTestCase):
	"""Pins the incident-class behaviors:

	1. A NON-Admin* exception in the pool sync worker (rq JobTimeoutException,
	   programmer error, ...) leaves a terminal 'failed:' status that SURVIVES
	   the frappe.db.rollback() Frappe's execute_job performs on any job
	   exception - previously the finally-backstop write was uncommitted and
	   rolled back with it, pinning the UI poller on 'pending:' forever.
	2. A successful pool sync stamps llm_pool_synced_at.
	3. is_ready_for_chat treats proxy_active as intent, not provisioning
	   success: a never-applied pool is NOT ready (reason
	   llm_pool_provisioning); an ever-applied pool stays ready through a
	   later re-save's transient pending/failed.
	4. The sync jobs' RQ envelopes (300s) exceed the admin HTTP budgets they
	   wrap (120s pool / 90s single-model + lock waits + retries).
	"""

	def _seed_pool(self):
		"""Two api_key models -> proxy_active=1, sync job enqueued (mocked ok)."""
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			tier="strong",
			order=0,
			api_key="sk-audit-key-1",
			base_url="https://api.openai.com",
		)
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-3.5-turbo",
			tier="cheap",
			order=1,
			api_key="sk-audit-key-2",
			base_url="https://api.openai.com",
		)
		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()

	@staticmethod
	def _as_background_job():
		"""Simulate execute_job's worker context: _commit_terminal_sync_status
		commits ONLY when frappe.local.job is set (the real-worker signal), so
		durability tests must fake it - and everything else in the suite runs
		WITHOUT it, preserving FrappeTestCase transaction isolation."""
		from contextlib import contextmanager

		@contextmanager
		def _ctx():
			prior = getattr(frappe.local, "job", None)
			frappe.local.job = frappe._dict(method="test")
			try:
				yield
			finally:
				frappe.local.job = prior

		return _ctx()

	def test_unexpected_pool_sync_error_status_survives_rollback(self):
		"""RuntimeError (stand-in for rq JobTimeoutException - both take the
		non-Admin* path) -> finally writes 'failed: unexpected error' AND
		commits it, so execute_job's rollback can't undo it."""
		self._seed_pool()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("last_sync_status", "pending: provisioning container (pool)", update_modified=False)
		frappe.db.commit()

		with (
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._post_pool_with_retry",
				side_effect=RuntimeError("job timeout simulation"),
			),
			self._as_background_job(),
		):
			with self.assertRaises(RuntimeError):
				_enqueued_sync_via_admin_pool()

		# Simulate Frappe's execute_job exception handler.
		frappe.db.rollback()

		status = frappe.db.get_single_value("Jarvis Settings", "last_sync_status") or ""
		self.assertTrue(
			status.startswith("failed: unexpected error"),
			"the terminal failure status must survive the job-runner rollback "
			f"(was the finally-backstop committed?); got: {status!r}",
		)

	def test_pool_sync_ok_stamps_llm_pool_synced_at(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_pool_synced_at", None, update_modified=False)
		frappe.db.commit()

		self._seed_pool()
		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			_enqueued_sync_via_admin_pool()

		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue(settings.llm_pool_synced_at, "a successful pool apply must stamp llm_pool_synced_at")
		self.assertTrue(
			(settings.last_sync_status or "").startswith("ok"),
			f"expected ok status, got: {settings.last_sync_status!r}",
		)

	def test_pending_applying_flips_to_ok_when_admin_reports_ready(self):
		"""Round-4 review R4-P0-6: a pending-applying sync converges once admin's
		read-only serving verdict (get_connection -> chat_readiness) reports Ready.
		The poller flips the durable marker + status to ok, and stays pending while
		admin is not Ready."""
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			_PENDING_APPLYING_STATUS,
		)
		from jarvis.onboarding import get_llm_sync_status

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set(
			{
				"proxy_active": 1,
				"llm_pool_synced_at": None,
				"last_sync_status": _PENDING_APPLYING_STATUS,
			},
			update_modified=False,
		)
		frappe.db.commit()
		# Admin not yet Ready -> stays pending, no marker.
		with patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Configuring"}):
			out = get_llm_sync_status()
		self.assertTrue(out["last_sync_status"].startswith("pending"))
		self.assertIsNone(frappe.get_single("Jarvis Settings").llm_pool_synced_at)
		# Admin now Ready -> flip to ok + stamp the durable marker.
		with patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Ready"}):
			out = get_llm_sync_status()
		self.assertTrue(
			out["last_sync_status"].startswith("ok"),
			f"expected ok after admin Ready, got: {out['last_sync_status']!r}",
		)
		self.assertTrue(frappe.get_single("Jarvis Settings").llm_pool_synced_at)

	def test_pending_applying_stays_pending_when_admin_unreachable(self):
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			_PENDING_APPLYING_STATUS,
		)
		from jarvis.onboarding import get_llm_sync_status

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set(
			{
				"proxy_active": 1,
				"llm_pool_synced_at": None,
				"last_sync_status": _PENDING_APPLYING_STATUS,
			},
			update_modified=False,
		)
		frappe.db.commit()
		with patch("jarvis.admin_client.get_connection", side_effect=RuntimeError("admin down")):
			out = get_llm_sync_status()
		self.assertTrue(
			out["last_sync_status"].startswith("pending"),
			"an admin error must leave the status pending, never crash the poller",
		)

	def test_pool_sync_blocked_is_failed_no_stamp(self):
		"""status="blocked" (subscription pool with no OAuth blobs) is terminal
		failed — never a first-success."""
		self._seed_pool()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_pool_synced_at", None, update_modified=False)
		frappe.db.commit()
		with patch(
			"jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool", "status": "blocked"}
		):
			_enqueued_sync_via_admin_pool()
		settings = frappe.get_single("Jarvis Settings")
		self.assertIsNone(settings.llm_pool_synced_at)
		self.assertTrue(
			(settings.last_sync_status or "").startswith("failed"),
			f"expected failed, got: {settings.last_sync_status!r}",
		)

	# -- is_ready_for_chat gate ------------------------------------------

	def _set_pool_gate_state(self, *, synced_at, status):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("proxy_active", 1, update_modified=False)
		settings.db_set("llm_pool_synced_at", synced_at, update_modified=False)
		settings.db_set("last_sync_status", status, update_modified=False)
		frappe.db.commit()

	def test_never_applied_pool_pending_is_not_ready(self):
		from jarvis.account import is_ready_for_chat

		self._set_pool_gate_state(synced_at=None, status="pending: provisioning container (pool)")
		result = is_ready_for_chat()
		self.assertFalse(result.get("ready"), f"a never-applied pool must not be chat-ready; got: {result}")
		self.assertEqual(result.get("reason"), "llm_pool_provisioning")

	def test_never_applied_pool_failed_is_not_ready(self):
		from jarvis.account import is_ready_for_chat

		self._set_pool_gate_state(synced_at=None, status="failed: unexpected error; see Error Log")
		result = is_ready_for_chat()
		self.assertFalse(result.get("ready"))
		self.assertEqual(result.get("reason"), "llm_pool_provisioning")

	def test_ever_applied_pool_stays_ready_through_resave_pending(self):
		"""A tenant whose pool applied once keeps chatting on the container's
		previous config while a re-save is mid-sync - must NOT be bounced to
		onboarding over a transient pending/failed."""
		from jarvis.account import is_ready_for_chat

		self._set_pool_gate_state(
			synced_at=frappe.utils.now(), status="pending: provisioning container (pool)"
		)
		self.assertTrue(is_ready_for_chat().get("ready"))
		self._set_pool_gate_state(
			synced_at=frappe.utils.now(), status="failed: rate-limited; retry_after=90s"
		)
		self.assertTrue(is_ready_for_chat().get("ready"))

	def test_legacy_ok_status_alone_does_not_open_the_gate(self):
		"""last_sync_status is SHARED with the single-model sync: a stale
		'ok (reload via admin)' from a queued legacy creds job must not make
		a never-applied pool look ready. Only the marker counts."""
		from jarvis.account import is_ready_for_chat

		self._set_pool_gate_state(synced_at=None, status="ok (reload via admin)")
		result = is_ready_for_chat()
		self.assertFalse(result.get("ready"))
		self.assertEqual(result.get("reason"), "llm_pool_provisioning")

	def test_backfill_patch_grandfathers_pre_marker_tenants(self):
		"""Tenants provisioned before llm_pool_synced_at existed are
		grandfathered by patch v1_10 (marker backfilled from last_sync_at
		when the pool demonstrably applied), NOT by a status heuristic in
		the gate."""
		from jarvis.account import is_ready_for_chat
		from jarvis.patches.v1_10_backfill_llm_pool_synced_at import execute

		self._set_pool_gate_state(synced_at=None, status="ok (pool_update via admin)")
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("last_sync_at", frappe.utils.now(), update_modified=False)
		frappe.db.commit()

		execute()

		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue(settings.llm_pool_synced_at, "patch must backfill the marker for an applied pool")
		self.assertTrue(is_ready_for_chat().get("ready"))

	def test_backfill_patch_stamps_even_non_ok_pool_tenants(self):
		"""Grandfathering is unconditional for pre-marker pool tenants: an
		established tenant whose LATEST re-save is transiently pending/failed
		at migrate time (container still serving the applied pool) was
		chat-ready before the deploy and must stay so after it."""
		from jarvis.patches.v1_10_backfill_llm_pool_synced_at import execute

		self._set_pool_gate_state(synced_at=None, status="failed: admin unreachable: transient")
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("last_sync_at", frappe.utils.now(), update_modified=False)
		frappe.db.commit()
		execute()
		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue(
			settings.llm_pool_synced_at, "pre-marker pool tenants must be grandfathered unconditionally"
		)

	def test_backfill_patch_skips_never_synced_pools(self):
		"""A pre-marker pool tenant whose sync NEVER completed (last_sync_at
		empty) has a demonstrably never-applied pool - stamping would
		permanently disarm the llm_pool_provisioning gate for a tenant whose
		chat has never worked."""
		from jarvis.patches.v1_10_backfill_llm_pool_synced_at import execute

		self._set_pool_gate_state(synced_at=None, status="failed: admin unreachable: since creation")
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("last_sync_at", None, update_modified=False)
		frappe.db.commit()
		execute()
		settings = frappe.get_single("Jarvis Settings")
		self.assertFalse(settings.llm_pool_synced_at, "never-synced pools must not be grandfathered")

	def test_backfill_patch_skips_non_pool_tenants(self):
		from jarvis.patches.v1_10_backfill_llm_pool_synced_at import execute

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("llm_pool_synced_at", None, update_modified=False)
		frappe.db.commit()
		execute()
		settings = frappe.get_single("Jarvis Settings")
		self.assertFalse(settings.llm_pool_synced_at, "direct tenants must not be stamped")

	def test_direct_backfill_grandfathers_serving_direct_tenants(self):
		"""Round-4 review R4-P0-6: a pre-marker DIRECT api-key tenant with local
		creds that has EVER synced is grandfathered (llm_direct_synced_at
		backfilled from last_sync_at) so the new gate doesn't bounce it to
		onboarding mid-upgrade; a never-synced one is not stamped."""
		from jarvis.patches.v2_00_backfill_llm_direct_synced_at import execute

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set(
			{
				"proxy_active": 0,
				"llm_auth_mode": "api_key",
				"llm_provider": "openai_compat",
				"llm_model": "gpt-4o",
				"llm_direct_synced_at": None,
				"last_sync_at": frappe.utils.now(),
			},
			update_modified=False,
		)
		settings.db_set("llm_api_key", "sk-grandfathered")
		frappe.db.commit()
		execute()
		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue(
			settings.llm_direct_synced_at, "a serving pre-marker direct tenant must be grandfathered"
		)

	def test_direct_backfill_skips_never_synced(self):
		from jarvis.patches.v2_00_backfill_llm_direct_synced_at import execute

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set(
			{
				"proxy_active": 0,
				"llm_auth_mode": "api_key",
				"llm_provider": "openai_compat",
				"llm_model": "gpt-4o",
				"llm_direct_synced_at": None,
				"last_sync_at": None,
			},
			update_modified=False,
		)
		settings.db_set("llm_api_key", "sk-never-synced")
		frappe.db.commit()
		execute()
		settings = frappe.get_single("Jarvis Settings")
		self.assertFalse(
			settings.llm_direct_synced_at, "a never-synced direct tenant must not be grandfathered"
		)

	def test_direct_backfill_skips_pool_tenants(self):
		from jarvis.patches.v2_00_backfill_llm_direct_synced_at import execute

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set({"proxy_active": 1, "llm_direct_synced_at": None}, update_modified=False)
		frappe.db.commit()
		execute()
		settings = frappe.get_single("Jarvis Settings")
		self.assertFalse(
			settings.llm_direct_synced_at, "pool tenants gate on llm_pool_synced_at, not this marker"
		)

	def test_pool_sync_ok_status_survives_rollback(self):
		"""The OK terminal write (status + marker) must be committed too: an
		rq SIGALRM can fire AFTER the ok db_set but before job end, and the
		job-runner rollback would otherwise revert a SUCCESSFUL apply back
		to 'pending:' forever."""
		self._seed_pool()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("last_sync_status", "pending: provisioning container (pool)", update_modified=False)
		settings.db_set("llm_pool_synced_at", None, update_modified=False)
		frappe.db.commit()

		with (
			patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}),
			self._as_background_job(),
		):
			_enqueued_sync_via_admin_pool()
		frappe.db.rollback()

		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue(
			(settings.last_sync_status or "").startswith("ok"),
			f"ok status must survive rollback; got {settings.last_sync_status!r}",
		)
		self.assertTrue(settings.llm_pool_synced_at, "llm_pool_synced_at must survive rollback")

	def test_lock_loss_schedules_one_retry_then_terminal(self):
		"""Losing the sync lock must not strand a fresh tenant on a terminal
		'failed: skipped' - first loss re-enqueues one retry under its own
		job_id; only the retry's loss is terminal."""
		from contextlib import contextmanager

		@contextmanager
		def _never_acquired(*_a, **_kw):
			yield False

		captured = []
		with (
			patch("jarvis._redis_lock.redis_lock", _never_acquired),
			patch("frappe.enqueue", side_effect=lambda *a, **kw: captured.append(kw)),
		):
			_enqueued_sync_via_admin_pool(retry_left=1)
		status = frappe.db.get_single_value("Jarvis Settings", "last_sync_status") or ""
		self.assertTrue(
			status.startswith("pending: waiting for a concurrent sync"),
			f"first lock loss must stay pending-with-retry; got {status!r}",
		)
		self.assertEqual(len(captured), 1, "exactly one retry must be enqueued")
		# Per-level prefix + per-chain random suffix (two overlapping chains
		# at the same level must not dedup-collide and drop one).
		self.assertTrue(
			(captured[0].get("job_id") or "").startswith("jarvis_settings_sync:pool:retry:0:"),
			f"unexpected retry job_id: {captured[0].get('job_id')!r}",
		)
		self.assertEqual(captured[0].get("retry_left"), 0)

		captured.clear()
		with (
			patch("jarvis._redis_lock.redis_lock", _never_acquired),
			patch("frappe.enqueue", side_effect=lambda *a, **kw: captured.append(kw)),
		):
			_enqueued_sync_via_admin_pool(retry_left=0)
		status = frappe.db.get_single_value("Jarvis Settings", "last_sync_status") or ""
		self.assertTrue(
			status.startswith("failed: skipped"), f"retry exhaustion must be terminal; got {status!r}"
		)
		self.assertFalse(captured, "no further retries after exhaustion")

	# -- RQ envelope budgets ----------------------------------------------

	def test_sync_enqueues_share_the_budget_constant(self):
		"""BOTH sync enqueues (pool + single-model) must use
		ADMIN_SYNC_RQ_TIMEOUT_S - and the constant itself must clear the
		worst-case work it wraps (~430s: 60s lock wait + 3x120s pool POST
		attempts + sleeps). Tuning either enqueue away from the shared
		budget re-arms the JARVIS-2026-07-08 stuck-pending trigger."""
		from unittest.mock import patch as _patch

		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			ADMIN_SYNC_LOCK_RETRIES,
			ADMIN_SYNC_LOCK_TIMEOUT_S,
			ADMIN_SYNC_PRIMARY_LOCK_WAIT_S,
			ADMIN_SYNC_RETRY_LOCK_WAIT_S,
			ADMIN_SYNC_RQ_TIMEOUT_S,
		)

		self.assertGreaterEqual(
			ADMIN_SYNC_RQ_TIMEOUT_S, 440, "RQ envelope must exceed worst-case sync work (~430s)"
		)
		self.assertGreaterEqual(
			ADMIN_SYNC_LOCK_TIMEOUT_S,
			ADMIN_SYNC_RQ_TIMEOUT_S,
			"lock TTL must cover a holder running to its SIGALRM",
		)
		# A dead (SIGKILLed) holder blocks for the full TTL; the retry chain's
		# cumulative wait must outlive it or a fresh tenant's first sync can
		# exhaust every attempt against a corpse.
		cumulative_wait = (
			ADMIN_SYNC_PRIMARY_LOCK_WAIT_S + ADMIN_SYNC_LOCK_RETRIES * ADMIN_SYNC_RETRY_LOCK_WAIT_S
		)
		self.assertGreater(
			cumulative_wait,
			ADMIN_SYNC_LOCK_TIMEOUT_S,
			"retry-chain cumulative lock wait must outlive a dead holder's TTL",
		)
		# And the per-attempt wait must not eat the work budget: wait + POST
		# work (~425s: 2x150 pool attempts + 5s sleep + ~120s in-job convergence
		# poll, lock wait excluded) must fit the envelope. Tuning _POOL_SYNC_RETRIES,
		# the pool HTTP timeout, or _POOL_CONVERGE_DEADLINE_S moves this figure.
		self.assertLessEqual(
			ADMIN_SYNC_RETRY_LOCK_WAIT_S + 425,
			ADMIN_SYNC_RQ_TIMEOUT_S,
			"retry lock wait + worst-case POST+convergence work must fit the RQ envelope",
		)

		settings = frappe.get_single("Jarvis Settings")
		captured = []

		def _capture_enqueue(*args, **kwargs):
			captured.append(kwargs)

		with patch("frappe.enqueue", side_effect=_capture_enqueue):
			settings.db_set("last_sync_status", "", update_modified=False)
			settings._enqueue_pool_sync()
			with _patch.object(type(settings), "_classify_llm_change", return_value="restart"):
				settings._on_update_single_model_legacy()
		self.assertEqual(len(captured), 2)
		for kw in captured:
			self.assertEqual(kw.get("timeout"), ADMIN_SYNC_RQ_TIMEOUT_S)

	# -- Apply-warning propagation (subscription_status + warnings) -------
	#
	# The fleet-agent's PUT /v1/containers/{name}/llm-pool response carries
	# subscription_status + warnings alongside action/result. Persisted into
	# last_subscription_status / last_sync_warnings so the SPA (via
	# onboarding.get_llm_sync_status) can surface a subscription that loaded
	# but failed an upstream probe, instead of showing a blanket "ok".

	def test_pool_sync_persists_subscription_status_and_warnings(self):
		"""A successful apply that reports subscription_status/warnings must
		persist both, and last_sync_status must still start with the
		literal "ok" - _pool_sync_is_redundant()'s dedup gate depends on
		that exact prefix, so warnings must never be encoded into it."""
		self._seed_pool()
		warnings = [
			{
				"code": "subscription_unverified",
				"message": "cliproxy loaded the credential but the upstream rejected a 1-token probe",
			}
		]
		with patch(
			"jarvis.admin_client.post_update_llm_pool",
			return_value={"action": "pool_update", "subscription_status": "unverified", "warnings": warnings},
		):
			_enqueued_sync_via_admin_pool()

		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.last_subscription_status, "unverified")
		self.assertEqual(json.loads(settings.last_sync_warnings), warnings)
		self.assertTrue(
			(settings.last_sync_status or "").startswith("ok"),
			f"warned-but-applied sync must still read 'ok ...' for the "
			f"dedup gate; got {settings.last_sync_status!r}",
		)

	def test_noop_reapply_does_not_clobber_the_last_real_verdict(self):
		"""A NO-OP apply (contract 1.10 `unchanged: true`) must LEAVE the previous
		verdict alone.

		The no-op path is side-effect-free by contract, so the fleet deliberately
		runs no probe there (a 1-token completion is a side effect) and reports
		subscription_status "unchecked" with warnings []. Persisting those would
		DISCARD the last real apply's verdict:

		  * a healthy "verified" silently decays to "unchecked" -- reads as a
		    regression the customer never caused (this is exactly what was seen
		    live: a redundant re-save turned a working ChatGPT subscription into
		    "unchecked"), and
		  * far worse, a genuine `model_unreachable` / `subscription_unverified`
		    warning is CLEARED, so a dead model looks healthy again after any
		    redundant re-save.

		Nothing about the running pool changed, so the previous verdict still
		describes it exactly.
		"""
		self._seed_pool()
		warnings = [
			{
				"code": "model_unreachable",
				"message": "claude-sonnet-4-6: the upstream rejected a 1-token probe",
			}
		]
		# 1) a REAL apply lands a definitive verdict + a real warning
		with patch(
			"jarvis.admin_client.post_update_llm_pool",
			return_value={"action": "pool-applied", "subscription_status": "verified", "warnings": warnings},
		):
			_enqueued_sync_via_admin_pool()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.last_subscription_status, "verified")
		self.assertEqual(json.loads(settings.last_sync_warnings), warnings)

		# 2) a redundant re-save -> fleet short-circuits: unchanged, no probe
		with patch(
			"jarvis.admin_client.post_update_llm_pool",
			return_value={
				"action": "pool-applied",
				"unchanged": True,
				"subscription_status": "unchecked",
				"warnings": [],
			},
		):
			_enqueued_sync_via_admin_pool()

		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			settings.last_subscription_status,
			"verified",
			"a no-op must not downgrade a verified subscription to 'unchecked'",
		)
		self.assertEqual(
			json.loads(settings.last_sync_warnings),
			warnings,
			"a no-op must not CLEAR a real warning -- that hides a dead model",
		)
		# the sync itself still succeeded, and the dedup gate's "ok" prefix holds
		self.assertTrue((settings.last_sync_status or "").startswith("ok"))

	def test_pool_sync_contract_1_9_response_defaults_to_empty(self):
		"""A fleet on the pre-warnings contract (1.9) reports neither key -
		must default to "" / "[]" without raising (result is never assumed
		to carry these keys)."""
		self._seed_pool()
		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			_enqueued_sync_via_admin_pool()

		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.last_subscription_status, "")
		self.assertEqual(settings.last_sync_warnings, "[]")

	def test_failed_pool_sync_clears_stale_subscription_status_and_warnings(self):
		"""A genuinely-terminal FAILED sync (auth/validation) must clear both
		fields even when a PRIOR successful apply had set them - a stale
		'verified' status must not linger next to a 'failed:' status the poller
		reads as broken.

		NB (F2): an AdminUnreachableError/timeout is NO LONGER a terminal failure
		- it converges to 'pending' and is reconciled, so this test now uses a
		genuine terminal error (AdminAuthError) to exercise the failed-clearing
		path. The unreachable->pending path is covered by
		test_pool_sync_unreachable_records_pending_not_failed."""
		from jarvis.exceptions import AdminAuthError

		self._seed_pool()
		with patch(
			"jarvis.admin_client.post_update_llm_pool",
			return_value={"action": "pool_update", "subscription_status": "verified", "warnings": []},
		):
			_enqueued_sync_via_admin_pool()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.last_subscription_status, "verified")

		with patch("jarvis.admin_client.post_update_llm_pool", side_effect=AdminAuthError("bad token")):
			_enqueued_sync_via_admin_pool()

		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue((settings.last_sync_status or "").startswith("failed:"))
		self.assertEqual(
			settings.last_subscription_status, "", "a failed sync must clear the stale subscription_status"
		)
		self.assertEqual(settings.last_sync_warnings, "[]", "a failed sync must clear stale warnings")

	def test_redundant_skip_leaves_subscription_status_and_warnings_untouched(self):
		"""The pre-enqueue redundant-sync skip (_pool_sync_is_redundant) must
		leave subscription_status/warnings untouched, exactly like it leaves
		last_sync_status untouched - the container's last real apply is
		still the truth, nothing was pushed to it this time."""
		self._seed_pool()
		with patch(
			"jarvis.admin_client.post_update_llm_pool",
			return_value={
				"action": "pool_update",
				"subscription_status": "verified",
				"warnings": [{"code": "x", "message": "y"}],
			},
		):
			_enqueued_sync_via_admin_pool()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.last_subscription_status, "verified")
		status_before = settings.last_subscription_status
		warnings_before = settings.last_sync_warnings

		# An unrelated re-save (no pool-relevant change, last sync "ok")
		# must skip the enqueue entirely (_pool_sync_is_redundant) and
		# leave both fields exactly as the last real apply left them.
		# Mirrors TestPoolSyncChangeDetection's mock seam (patch
		# _enqueue_pool_sync itself, not the admin call it would make).
		with patch.object(type(settings), "_enqueue_pool_sync") as mock_enqueue:
			settings.save()
		mock_enqueue.assert_not_called()

		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.last_subscription_status, status_before)
		self.assertEqual(settings.last_sync_warnings, warnings_before)


class TestConvergenceReconcile(_RT3SettingsTestCase):
	"""Audit F2 (livelock) + C5 (applying-is-not-failure) + F5 (retry
	classification) + F1 (timeout ladder).

	An admin apply that is accepted-but-still-converging ("applying") or times
	out is recorded as PENDING (never terminal "failed"), and the bench converges
	to the durable success markers by polling admin get_connection's
	chat_readiness. Critically: llm_pool_synced_at (is_ready_for_chat's
	evidence-of-successful-apply gate) is stamped ONLY on a confirmed apply or a
	convergence "Ready" - never off an "applying" 200.
	"""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("proxy_recommended", 0, update_modified=False)
		settings.db_set("llm_pool_synced_at", None, update_modified=False)
		settings.db_set("last_sync_status", "", update_modified=False)
		frappe.db.commit()

	def _seed_pool(self):
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			tier="strong",
			order=0,
			api_key="sk-conv-1",
			base_url="https://api.openai.com",
		)
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-3.5-turbo",
			tier="cheap",
			order=1,
			api_key="sk-conv-2",
			base_url="https://api.openai.com",
		)
		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()
		# A clean baseline for the applying/timeout assertions below.
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_pool_synced_at", None, update_modified=False)
		frappe.db.commit()

	def _seed_admin_creds(self):
		"""Write an admin api_key into __Auth so reconcile_pending_llm_sync does
		not short-circuit on "un-onboarded". Deliberately does NOT commit: the
		__Auth row must stay inside the test transaction so FrappeTestCase's
		rollback cleans it up (a committed password write escapes rollback and
		pollutes the shared singleton for every later test - e.g. is_onboarded).
		Call it AFTER the plain-field commit so no later commit flushes it."""
		from frappe.utils.password import set_encrypted_password

		set_encrypted_password("Jarvis Settings", "Jarvis Settings", "test-admin-key", "jarvis_admin_api_key")

	# -- C5/P1-7: "applying" is pending, and must NOT stamp the marker --------

	def test_applying_result_records_pending_not_failed_and_no_marker(self):
		self._seed_pool()
		with (
			patch(
				"jarvis.admin_client.post_update_llm_pool",
				return_value={"action": "pool_update", "status": "applying"},
			),
			patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Configuring"}),
		):
			_enqueued_sync_via_admin_pool()
		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue(
			(settings.last_sync_status or "").startswith("pending: admin applying"),
			f"an 'applying' apply must be pending, not failed; got {settings.last_sync_status!r}",
		)
		self.assertFalse(
			settings.llm_pool_synced_at,
			"llm_pool_synced_at must NOT be stamped off an 'applying' 200 - it is "
			"evidence of a SUCCESSFUL apply and gates chat readiness",
		)

	def test_applying_then_ready_converges_and_stamps_marker(self):
		self._seed_pool()
		with (
			patch(
				"jarvis.admin_client.post_update_llm_pool",
				return_value={"action": "pool_update", "status": "applying"},
			),
			patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Ready"}),
		):
			_enqueued_sync_via_admin_pool()
		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue(
			(settings.last_sync_status or "").startswith("ok"),
			f"a converged apply must read ok; got {settings.last_sync_status!r}",
		)
		self.assertTrue(
			settings.llm_pool_synced_at, "convergence to chat_readiness Ready must stamp llm_pool_synced_at"
		)

	# -- F2: timeout/unreachable is pending+converge, never a lost apply -----

	def test_unreachable_then_ready_converges_and_stamps_marker(self):
		from jarvis.exceptions import AdminUnreachableError

		self._seed_pool()
		with (
			patch(
				"jarvis.admin_client.post_update_llm_pool", side_effect=AdminUnreachableError("read timeout")
			),
			patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Ready"}),
		):
			_enqueued_sync_via_admin_pool()
		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue((settings.last_sync_status or "").startswith("ok"))
		self.assertTrue(
			settings.llm_pool_synced_at,
			"an unreachable apply that the container actually landed must "
			"converge to ok+marker, not livelock at failed",
		)

	def test_unreachable_not_ready_records_pending_not_failed(self):
		from jarvis.exceptions import AdminUnreachableError

		self._seed_pool()
		with (
			patch(
				"jarvis.admin_client.post_update_llm_pool", side_effect=AdminUnreachableError("read timeout")
			),
			patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Configuring"}),
		):
			_enqueued_sync_via_admin_pool()
		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue(
			(settings.last_sync_status or "").startswith("pending: admin applying"),
			f"unreachable must record pending for the reconcile, not failed; "
			f"got {settings.last_sync_status!r}",
		)
		self.assertFalse(settings.llm_pool_synced_at)

	# -- This fix (2026-07-23 out-of-quota trace): a DEFINITIVE rejection ----
	# wrapped in the same AdminUnreachableError class as a genuine timeout
	# must be recognised and recorded TERMINAL, not converged/pending.

	def test_admin_customer_facing_reason_extracts_the_wrapped_sentence(self):
		"""Unit-level: admin_client wraps a 5xx envelope message as "admin
		returned a {status} error: {msg}" - the extractor must strip that
		wrapper and return admin's own "Your ..." sentence underneath."""
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			_admin_customer_facing_reason,
		)

		self.assertEqual(
			_admin_customer_facing_reason(
				"admin returned a 502 error: Your OpenAI account has reached "
				"its usage limit. It resets in about 27 hours."
			),
			"Your OpenAI account has reached its usage limit. It resets in about 27 hours.",
		)

	def test_admin_customer_facing_reason_accepts_an_unwrapped_sentence(self):
		"""Not every caller goes through the "admin returned a NNN error: "
		wrapper (e.g. a direct AdminUnreachableError("Your ...") in a test, or
		a future call site) - the sentence alone must still be recognised."""
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			_admin_customer_facing_reason,
		)

		self.assertEqual(
			_admin_customer_facing_reason("Your OpenAI subscription was rejected. Reconnect the account."),
			"Your OpenAI subscription was rejected. Reconnect the account.",
		)

	def test_admin_customer_facing_reason_rejects_generic_technical_text(self):
		"""A real network failure / raw diagnostic must NOT be mistaken for a
		customer-facing sentence - only admin's own "Your ..." convention
		qualifies. Never take the converge-skip path for these."""
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			_admin_customer_facing_reason,
		)

		self.assertEqual(_admin_customer_facing_reason("read timeout"), "")
		self.assertEqual(
			_admin_customer_facing_reason("admin is unreachable; check network / service status"), ""
		)
		self.assertEqual(
			_admin_customer_facing_reason(
				"admin returned a 502 error: provision_failed: subscription-only "
				"pool has no usable route: cliproxy_listening=True"
			),
			"",
		)
		self.assertEqual(_admin_customer_facing_reason(""), "")
		self.assertEqual(_admin_customer_facing_reason(None), "")

	def test_unreachable_with_customer_facing_reason_records_failed_not_pending(self):
		"""The remaining gap this fix closes. admin's fleet layer raises
		errors.ProvisionError on a subscription-only pool's first activation
		when the probe comes back exhausted (fleet's hard gate) - that never
		converges (there is no reconcile that can make a quota-exhausted
		account "Ready" before its own reset time), so it must be recorded as
		a terminal "failed: <admin's own sentence>" instead of livelocking in
		"pending:" via the F2 converge path. get_connection must never even
		be consulted for this case."""
		from jarvis.exceptions import AdminUnreachableError

		self._seed_pool()
		msg = (
			"admin returned a 502 error: Your OpenAI account has reached its "
			"usage limit. It resets in about 27 hours."
		)
		with (
			patch("jarvis.admin_client.post_update_llm_pool", side_effect=AdminUnreachableError(msg)),
			patch("jarvis.admin_client.get_connection") as get_conn,
		):
			_enqueued_sync_via_admin_pool()
			get_conn.assert_not_called()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			settings.last_sync_status,
			"failed: Your OpenAI account has reached its usage limit. It resets in about 27 hours.",
		)
		self.assertFalse(settings.llm_pool_synced_at)
		self.assertEqual(settings.last_subscription_status, "", "must clear stale subscription_status")

	# -- Scheduled safety net (reconcile_pending_llm_sync) -------------------

	def test_reconcile_stamps_marker_when_admin_reports_ready(self):
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			reconcile_pending_llm_sync,
		)

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("proxy_active", 1, update_modified=False)
		settings.db_set("llm_pool_synced_at", None, update_modified=False)
		settings.db_set("last_sync_status", "pending: admin applying config", update_modified=False)
		frappe.db.commit()
		self._seed_admin_creds()  # after the commit; stays in the rolled-back txn
		with patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Ready"}):
			reconcile_pending_llm_sync()
		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue(
			settings.llm_pool_synced_at, "the */5 reconcile must stamp the marker once admin reports Ready"
		)
		self.assertTrue((settings.last_sync_status or "").startswith("ok"))

	def test_reconcile_noop_when_not_ready(self):
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			reconcile_pending_llm_sync,
		)

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("proxy_active", 1, update_modified=False)
		settings.db_set("llm_pool_synced_at", None, update_modified=False)
		settings.db_set("last_sync_status", "pending: admin applying config", update_modified=False)
		frappe.db.commit()
		self._seed_admin_creds()  # after the commit; stays in the rolled-back txn
		with patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Configuring"}):
			reconcile_pending_llm_sync()
		settings = frappe.get_single("Jarvis Settings")
		self.assertFalse(settings.llm_pool_synced_at)
		self.assertTrue((settings.last_sync_status or "").startswith("pending:"))

	def test_reconcile_stamps_direct_marker_for_unproven_direct_tenant(self):
		"""Round-4 R4-P0-6 direct analogue of the unproven-pool re-probe: a
		single-model tenant whose FIRST apply never stamped llm_direct_synced_at
		(stuck at pending/failed) is re-probed, and admin Ready stamps the direct
		marker so is_ready_for_chat stops reporting llm_provisioning."""
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			reconcile_pending_llm_sync,
		)

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set(
			{
				"proxy_active": 0,
				"llm_auth_mode": "api_key",
				"llm_provider": "openai_compat",
				"llm_model": "gpt-4o",
				"llm_direct_synced_at": None,
				"last_sync_status": "failed: admin briefly unreachable",
			},
			update_modified=False,
		)
		frappe.db.commit()
		self._seed_admin_creds()  # after the commit; stays in the rolled-back txn
		with patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Ready"}):
			reconcile_pending_llm_sync()
		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue(
			settings.llm_direct_synced_at,
			"the */5 reconcile must stamp the DIRECT marker once admin reports Ready",
		)
		self.assertTrue((settings.last_sync_status or "").startswith("ok"))

	def test_reconcile_noop_on_healthy_tenant(self):
		"""Inert on a healthy fleet: an 'ok' tenant is never probed or re-stamped."""
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			reconcile_pending_llm_sync,
		)

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("proxy_active", 1, update_modified=False)
		settings.db_set("llm_pool_synced_at", frappe.utils.now(), update_modified=False)
		settings.db_set("last_sync_status", "ok (pool_update via admin)", update_modified=False)
		frappe.db.commit()
		self._seed_admin_creds()  # after the commit; stays in the rolled-back txn
		with patch("jarvis.admin_client.get_connection") as m:
			reconcile_pending_llm_sync()
		m.assert_not_called()

	# -- F5: a re-save after a NON-ok sync forces a full restart apply -------

	def test_classify_forces_restart_when_last_sync_not_ok(self):
		s = frappe.get_single("Jarvis Settings")
		s.llm_provider = "OpenAI"
		s.llm_model = "gpt-4o"
		s.llm_base_url = ""
		s.flags.force_admin_sync = False
		s.flags.llm_auth_mode_changed = False
		s.flags.llm_api_key_changed = False
		before = frappe._dict(llm_provider="OpenAI", llm_model="gpt-4o", llm_base_url="")

		s.last_sync_status = "failed: admin unreachable: x"
		with patch.object(type(s), "get_doc_before_save", return_value=before):
			self.assertEqual(
				s._classify_llm_change(),
				"restart",
				"a re-save of identical creds after a FAILED sync must re-run the full apply (F5), not no-op",
			)

		# Mirror image: an unchanged save after an OK sync stays a no-op.
		s.last_sync_status = "ok (restart via admin)"
		with patch.object(type(s), "get_doc_before_save", return_value=before):
			self.assertIsNone(
				s._classify_llm_change(), "an unchanged re-save after an OK sync must remain a no-op"
			)

	# -- F1: the timeout ladder + both apply legs ride DEFAULT_TIMEOUT_S -----

	def test_default_timeout_is_150_and_apply_legs_use_it(self):
		from jarvis import admin_client

		self.assertEqual(
			admin_client.DEFAULT_TIMEOUT_S,
			150,
			"bench->admin budget must sit above the 100s admin->agent leg",
		)
		captured = []

		def _cap(path, body, *, timeout_s=admin_client.DEFAULT_TIMEOUT_S):
			captured.append(timeout_s)
			return {}

		with patch("jarvis.admin_client._post", side_effect=_cap):
			admin_client.post_update_llm_pool(spec={}, api_keys={}, oauth_blobs={})
			admin_client.post_update_llm_creds(provider="p", model="m", base_url="", api_key="k")
		self.assertEqual(captured, [150, 150], "both interactive apply legs must ride DEFAULT_TIMEOUT_S=150")


# ---------------------------------------------------------------------------
# Pool-sync change detection: the pool analog of _classify_llm_change.
#
# Before this gate, EVERY save of Jarvis Settings while proxy_active - pattern-
# learning windows, any unrelated field - re-POSTed the full
# pool spec + secrets to admin. on_update now skips _enqueue_pool_sync only
# when (a) a doc_before_save exists, (b) the pool-relevant snapshot
# (_pool_state_snapshot) is identical, and (c) last_sync_status starts with
# "ok" - so a failed sync stays retryable by re-saving.
# ---------------------------------------------------------------------------


class TestPoolSyncChangeDetection(_RT3SettingsTestCase):
	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("proxy_recommended", 0, update_modified=False)
		frappe.db.commit()

	def _save_two_model_pool(self):
		"""Establish a synced 2-model pool: proxy_active=1, status 'ok ...'."""
		from unittest.mock import patch

		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			tier="strong",
			order=0,
			api_key="sk-diff-key-1",
			base_url="https://api.openai.com",
		)
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-3.5-turbo",
			tier="cheap",
			order=1,
			api_key="sk-diff-key-2",
			base_url="https://api.openai.com",
		)
		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()
		settings = frappe.get_single("Jarvis Settings")
		# Pre-condition shared by every test here: the pool applied cleanly.
		assert (settings.last_sync_status or "").startswith("ok"), (
			f"fixture: expected ok status, got {settings.last_sync_status!r}"
		)
		return settings

	# ------------------------------------------------------------------ #
	# (a) no-change save -> NO enqueue, status left alone
	# ------------------------------------------------------------------ #

	def test_unchanged_save_skips_pool_sync_enqueue(self):
		"""A save that changes nothing pool-relevant while the last sync is
		'ok ...' must NOT enqueue a pool sync, and must leave
		last_sync_status untouched (no 'pending:' write)."""
		from unittest.mock import patch

		self._save_two_model_pool()
		settings = frappe.get_single("Jarvis Settings")
		status_before = settings.last_sync_status

		with patch.object(type(settings), "_enqueue_pool_sync") as mock_enqueue:
			settings.save()

		mock_enqueue.assert_not_called()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			settings.last_sync_status, status_before, "skipping must leave last_sync_status alone"
		)

	# ------------------------------------------------------------------ #
	# (b) pool-relevant change -> enqueue fires
	# ------------------------------------------------------------------ #

	def test_model_row_change_enqueues_pool_sync(self):
		from unittest.mock import patch

		self._save_two_model_pool()
		settings = frappe.get_single("Jarvis Settings")
		settings.models[1].model = "gpt-4o-mini"

		with patch.object(type(settings), "_enqueue_pool_sync") as mock_enqueue:
			settings.save()

		mock_enqueue.assert_called_once()

	def test_same_length_api_key_rotation_enqueues_pool_sync(self):
		"""Regression guard for the snapshot's capture point: a freshly-typed
		api_key of the SAME LENGTH as the old one masks to an identical
		'*'*len string by on_update time, so a snapshot taken there would
		read the rotation as 'unchanged' and silently drop the push. The
		snapshot is captured in validate() (pre-masking), where the new
		plaintext never equals the stored mask."""
		from unittest.mock import patch

		self._save_two_model_pool()
		settings = frappe.get_single("Jarvis Settings")
		# Same length as the stored "sk-diff-key-1".
		settings.models[0].api_key = "sk-diff-key-9"

		with patch.object(type(settings), "_enqueue_pool_sync") as mock_enqueue:
			settings.save()

		mock_enqueue.assert_called_once()

	# ------------------------------------------------------------------ #
	# (c) unchanged save while last sync FAILED -> still enqueues (retry)
	# ------------------------------------------------------------------ #

	def test_unchanged_save_with_failed_status_still_enqueues(self):
		"""A failed sync must always be retryable by re-saving, even with an
		identical pool config."""
		from unittest.mock import patch

		self._save_two_model_pool()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("last_sync_status", "failed: admin unreachable: boom", update_modified=False)
		frappe.db.commit()

		settings = frappe.get_single("Jarvis Settings")
		with patch.object(type(settings), "_enqueue_pool_sync") as mock_enqueue:
			settings.save()

		mock_enqueue.assert_called_once()
