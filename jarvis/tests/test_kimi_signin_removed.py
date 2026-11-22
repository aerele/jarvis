"""Kimi chat sign-in is no longer offered for NEW connections; a saved Kimi sign-in keeps
working, and Kimi stays available by API key (OpenAI-Compatible, membership key)."""

import unittest
from unittest.mock import patch

import frappe

from jarvis.jarvis import pool_serialize as ps

MESSAGE = ps.KIMI_SIGNIN_REMOVED_MESSAGE


def _acct(ref, upstream="kimi"):
	return frappe._dict(upstream=upstream, account_ref=ref, label="x", oauth_blob="{}")


def _sub(model="kimi-k2.7-code", upstream="kimi", refs=("K1",), order=0):
	return frappe._dict(
		model=model,
		tier="strong",
		order=order,
		enabled=1,
		credential_type="subscription",
		provider="",
		accounts=[_acct(r, upstream) for r in refs],
	)


def _settings(models):
	return frappe._dict(models=models, preset=None, routing_mode="failover")


class TestNewKimiSignInRefused(unittest.TestCase):
	def test_the_message_names_the_replacement(self):
		for part in ("OpenAI-Compatible", "https://api.kimi.com/coding/v1", "kimi-for-coding"):
			self.assertIn(part, MESSAGE)

	def test_an_unchanged_saved_pool_with_a_kimi_sign_in_saves(self):
		before = _settings([_sub(), _sub("gpt-5.5", "openai", ("O1",), 1)])
		now = _settings([_sub(), _sub("gpt-5.5", "openai", ("O1",), 1)])
		self.assertEqual(ps.new_kimi_subscription_errors(now, before), [])

	def test_removing_or_keeping_other_changes_is_fine(self):
		before = _settings([_sub(refs=("K1", "K2"))])
		self.assertEqual(ps.new_kimi_subscription_errors(_settings([_sub(refs=("K1",))]), before), [])
		self.assertEqual(ps.new_kimi_subscription_errors(_settings([]), before), [])

	def test_a_new_kimi_account_row_or_pool_is_refused(self):
		before = _settings([_sub(refs=("K1",))])
		self.assertEqual(
			ps.new_kimi_subscription_errors(_settings([_sub(refs=("K1", "K2"))]), before), [MESSAGE]
		)
		self.assertEqual(
			ps.new_kimi_subscription_errors(
				_settings([_sub(), _sub("kimi-k2.6", refs=("K3",), order=1)]), before
			),
			[MESSAGE],
		)
		self.assertEqual(ps.new_kimi_subscription_errors(_settings([_sub()]), None), [MESSAGE])

	def test_other_subscriptions_are_not_affected(self):
		self.assertEqual(
			ps.new_kimi_subscription_errors(_settings([_sub("gpt-5.5", "openai", ("O1",))]), None), []
		)


class TestStartingAKimiSignInIsRefused(unittest.TestCase):
	def test_the_device_sign_in_does_not_start(self):
		from jarvis.oauth import api

		with (
			patch.object(api, "require_jarvis_admin"),
			patch.object(
				api, "get_provider", return_value={"agent_provider": "kimi", "grant_type": "device_code"}
			),
			patch.object(api, "_begin_device_signin") as begin,
		):
			out = api.begin_pool_account_signin("Kimi (Moonshot)", "kimi-k2.7-code")
		begin.assert_not_called()
		self.assertEqual(out["error"]["code"], "kimi_signin_removed")
		self.assertIn("membership key", str(out))


class TestKimiByApiKeyStillValidates(unittest.TestCase):
	def test_openai_compatible_with_the_membership_endpoint_and_model(self):
		row = frappe._dict(
			model="kimi-for-coding",
			tier="strong",
			order=0,
			enabled=1,
			credential_type="api_key",
			api_key="sk-kimi-test",
			provider="openai_compat",
			base_url="https://api.kimi.com/coding/v1",
			accounts=[],
		)
		self.assertEqual(ps.validate_models(_settings([row])), [])
		spec, keys, _blobs = ps.build_pool_payload(_settings([row]))
		self.assertEqual(spec["models"][0]["base_url"], "https://api.kimi.com/coding/v1")
		self.assertEqual(spec["models"][0]["model"], "kimi-for-coding")
		self.assertIn("sk-kimi-test", keys.values())
