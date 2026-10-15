"""The chat picker offers only models a turn can actually reach (jarvis#1999).

In a pool only the native Claude plan serves an id the pool spec never named; every other
leg routes by its saved ids, and Bifrost answers an unknown id with the pool's own model.
The picker therefore offers Claude-plan extras only, while pin validation keeps the full
catalog so a chat already pinned to such a model still sends.

Pure in-memory: settings, catalog and pool-mode predicates are patched; nothing is saved.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

CATALOG = [
	{
		"catalog_id": "anthropic",
		"subscription_label": "Anthropic",
		"models": [
			{"model_id": "claude-opus-5", "label": "Opus 5", "tier": "subscription", "sort_order": 0},
			{"model_id": "claude-sonnet-5", "label": "Sonnet 5", "tier": "subscription", "sort_order": 1},
			{"model_id": "claude-sonnet-4-6", "label": "Sonnet 4.6", "tier": "api_key", "sort_order": 1},
		],
	},
	{
		"catalog_id": "openai",
		"subscription_label": "OpenAI",
		"models": [
			{"model_id": "gpt-5.6-terra", "label": "Terra", "tier": "subscription", "sort_order": 0},
			{"model_id": "gpt-5.6-luna", "label": "Luna", "tier": "subscription", "sort_order": 1},
			{"model_id": "gpt-5-api", "label": "GPT-5 API", "tier": "api_key", "sort_order": 0},
		],
	},
]


def _sub_row(model, upstream):
	return frappe._dict(
		enabled=1,
		provider="",
		model=model,
		credential_type="subscription",
		subscription_accounts=json.dumps([{"upstream": upstream}]),
	)


def _api_row(model, provider):
	return frappe._dict(enabled=1, provider=provider, model=model, credential_type="api_key")


CLAUDE_PLAN = _sub_row("claude-opus-5", "anthropic")
CHATGPT_PLAN = _sub_row("gpt-5.6-terra", "openai")


class TestPickerRoutableModels(FrappeTestCase):
	def _catalog(self, rows, *, pool, claude_plan, routable_only):
		from jarvis.chat import api

		settings = frappe._dict(models=list(rows))
		with (
			patch("jarvis.admin_client.get_model_catalog", return_value=CATALOG),
			patch("jarvis.jarvis.pool_serialize.compute_pool_mode", return_value=pool),
			patch("jarvis.jarvis.pool_serialize.has_native_claude_subscription", return_value=claude_plan),
		):
			return api._catalog_models_for_pool(settings, routable_only=routable_only)

	def _ids(self, out):
		return {key: [r["model"] for r in rows] for key, rows in out.items()}

	def test_pool_with_claude_plan_offers_only_claude_plan_extras(self):
		out = self._catalog([CLAUDE_PLAN, CHATGPT_PLAN], pool=True, claude_plan=True, routable_only=True)
		self.assertEqual(self._ids(out), {"anthropic": ["claude-opus-5", "claude-sonnet-5"]})

	def test_pool_without_a_claude_plan_offers_no_extras(self):
		out = self._catalog(
			[CHATGPT_PLAN, _api_row("gpt-5-api", "openai")], pool=True, claude_plan=False, routable_only=True
		)
		self.assertEqual(out, {})

	def test_an_anthropic_api_key_extra_is_not_offered_in_a_pool(self):
		"""Its id would resolve to the plan, not the key, so offering it would mislead."""
		out = self._catalog(
			[CLAUDE_PLAN, _api_row("claude-sonnet-4-6", "anthropic")],
			pool=True,
			claude_plan=True,
			routable_only=True,
		)
		self.assertNotIn("claude-sonnet-4-6", self._ids(out).get("anthropic", []))

	def test_outside_a_pool_extras_stay_offered(self):
		"""A lone direct leg sends the pinned id as is, so switching without a re-save works."""
		out = self._catalog([CHATGPT_PLAN], pool=False, claude_plan=False, routable_only=True)
		self.assertEqual(self._ids(out), {"openai": ["gpt-5.6-terra", "gpt-5.6-luna"]})

	def test_pin_validation_still_sees_the_full_catalog(self):
		"""A chat already pinned to an out-of-pool model keeps sending (the SPA resends the pin)."""
		out = self._catalog([CLAUDE_PLAN, CHATGPT_PLAN], pool=True, claude_plan=True, routable_only=False)
		self.assertIn("gpt-5.6-luna", self._ids(out)["openai"])

	def test_the_picker_payload_asks_for_routable_models(self):
		from jarvis.chat import api

		with open(api.__file__) as fh:
			src = fh.read()
		self.assertIn('"catalog_models": _catalog_models_for_pool(settings, routable_only=True)', src)
