"""Native subscription pool and agent runtime 2026.11 on the customer site.

Admin tells the site two facts (``native_subscription_pool``, ``agent_runtime_version``). With
the first on, the site derives no proxy; with the second at 2026.11 or later, the few
provider ids the gateway renames are translated at one boundary (``chat.gateway_ids``).
Fixtures are bare ``frappe._dict`` settings: no site data is read or changed."""

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import account, native_runtime
from jarvis.chat import gateway_ids, session_pin_sweep, turn_handler, usage
from jarvis.chat.agent_client import AgentSession
from jarvis.jarvis import pool_serialize

V11 = {"agent_runtime_version": "2026.11.5"}


def _acct(upstream="openai", ref="A1"):
	return frappe._dict(upstream=upstream, account_ref=ref, label="x@example.com", oauth_blob="{}")


def _sub(model="gpt-5.5", upstream="openai", accounts=None, order=0):
	return frappe._dict(
		model=model,
		tier="strong",
		order=order,
		enabled=1,
		credential_type="subscription",
		provider="",
		accounts=accounts if accounts is not None else [_acct(upstream)],
	)


def _key(model="gpt-4o", order=1):
	return frappe._dict(
		model=model,
		tier="strong",
		order=order,
		enabled=1,
		credential_type="api_key",
		provider="openai_compat",
		accounts=[],
	)


def _settings(models, **extra):
	return frappe._dict(models=models, preset=None, routing_mode="failover", **extra)


class TestProxyActive(unittest.TestCase):
	def test_a_sidecar_pool_is_proxied_and_the_flag_removes_the_proxy(self):
		models = [_sub(), _sub("kimi-k2", "kimi", order=1)]
		self.assertTrue(pool_serialize.compute_proxy_active(_settings(models)))
		self.assertTrue(pool_serialize.compute_proxy_active(_settings(models, native_subscription_pool=0)))
		self.assertFalse(pool_serialize.compute_proxy_active(_settings(models, native_subscription_pool=1)))

	def test_xai_is_a_renderer_upstream_only_on_a_native_tenant(self):
		lone = [_sub("grok-4", "xai")]
		self.assertFalse(pool_serialize._lone_direct_capable(_settings(lone)))
		self.assertTrue(pool_serialize._lone_direct_capable(_settings(lone, native_subscription_pool=1)))
		self.assertTrue(pool_serialize._upstream_has_renderer("openai"))
		self.assertFalse(pool_serialize._upstream_has_renderer("xai"))

	def test_two_accounts_on_one_model_still_take_the_pool_leg_on_a_native_tenant(self):
		# The direct leg holds one sign-in, so rotation needs the pool leg on either kind of tenant.
		two = [_sub(accounts=[_acct("openai", "A1"), _acct("openai", "A2")])]
		native = _settings(two, native_subscription_pool=1)
		self.assertFalse(pool_serialize._lone_direct_capable(native))
		self.assertTrue(pool_serialize.compute_pool_mode(native))
		self.assertFalse(pool_serialize.compute_proxy_active(native))

	def test_no_handover_is_due_on_a_native_tenant(self):
		settings = _settings([_sub()], llm_pool_synced_at="2026-08-01 00:00:00")
		self.assertTrue(pool_serialize.lone_direct_handover_due(settings))
		settings.native_subscription_pool = 1
		self.assertFalse(pool_serialize.lone_direct_handover_due(settings))

	def test_subscription_only_models_leave_out_an_id_an_api_key_row_also_serves(self):
		settings = _settings([_sub("gpt-5.5"), _sub("gpt-4o", order=2), _key("gpt-4o")])
		self.assertEqual(pool_serialize.subscription_only_models(settings), {"gpt-5.5"})


class TestRuntimeFacts(unittest.TestCase):
	def test_stand_ins_without_the_fields_read_as_unset(self):
		self.assertFalse(native_runtime.is_native_pool(frappe._dict()))
		self.assertFalse(native_runtime.is_native_pool(object()))
		self.assertIsNone(native_runtime.agent_runtime_version(frappe._dict()))

	def test_version_parsing_and_the_2026_11_boundary(self):
		for raw, expected in (("2026.11.5", True), ("2026.11.0", True), ("2027.1.0", True)):
			self.assertEqual(
				native_runtime.gateway_is_v11(frappe._dict(agent_runtime_version=raw)), expected, raw
			)
		for raw in ("2026.9.2", "2026.10.9", "", None, "latest", "2026.11", "2026.11.5-2"):
			self.assertFalse(native_runtime.gateway_is_v11(frappe._dict(agent_runtime_version=raw)), raw)


class TestGatewayIds(unittest.TestCase):
	_RENAMED = ("moonshot", "deepseek", "groq", "mistral", "zai", "kimi", "qwen")
	_KEPT = ("openai", "anthropic", "xai", "gemini", "google", "openrouter", "openai_compat", "zai_coding")

	def test_write_renames_the_seven_ids_only_on_a_known_2026_11_gateway(self):
		v11, v9, unknown = frappe._dict(V11), frappe._dict(agent_runtime_version="2026.9.2"), frappe._dict()
		for pid in self._RENAMED:
			self.assertEqual(gateway_ids.provider_to_gateway(pid, v11), f"{pid}_api")
			self.assertEqual(gateway_ids.provider_to_gateway(pid, v9), pid)
			self.assertEqual(gateway_ids.provider_to_gateway(pid, unknown), pid)
		for pid in (*self._KEPT, "moonshot-0"):
			self.assertEqual(gateway_ids.provider_to_gateway(pid, v11), pid)

	def test_read_maps_back_whatever_the_version(self):
		for pid in self._RENAMED:
			self.assertEqual(gateway_ids.provider_from_gateway(f"{pid}_api"), pid)
		for pid in (*self._KEPT, "moonshot-0", "", None):
			self.assertEqual(gateway_ids.provider_from_gateway(pid), pid)

	def test_a_bare_model_id_with_a_slash_is_never_renamed(self):
		v11 = frappe._dict(V11)
		self.assertEqual(
			gateway_ids.model_ref_for_gateway(None, "deepseek/deepseek-r1", v11), "deepseek/deepseek-r1"
		)
		self.assertEqual(
			gateway_ids.model_ref_for_gateway("moonshot", "kimi-k2.6", v11), "moonshot_api/kimi-k2.6"
		)
		self.assertEqual(gateway_ids.model_ref_for_gateway("openai", "gpt-5.5", v11), "openai/gpt-5.5")

	def _fold(self, provider, model, settings):
		with (
			patch.object(turn_handler, "_session_model_for", return_value=(model, provider)),
			patch.object(
				native_runtime.frappe, "get_cached_value", side_effect=lambda *a: settings.get(a[2])
			),
		):
			return turn_handler._session_model_patch(object())

	def test_the_session_pin_uses_the_gateway_id_on_2026_11(self):
		self.assertEqual(
			self._fold("moonshot", "kimi-k2.6", frappe._dict(V11)), (True, "moonshot_api/kimi-k2.6")
		)
		self.assertEqual(self._fold("moonshot", "kimi-k2.6", frappe._dict()), (True, "moonshot/kimi-k2.6"))
		self.assertEqual(self._fold(None, "gpt-5.5", frappe._dict(V11)), (True, "gpt-5.5"))

	def test_the_one_shot_pin_uses_the_gateway_id_on_2026_11(self):
		session = object.__new__(AgentSession)
		with (
			patch.object(AgentSession, "set_session_model") as pin,
			patch("jarvis.chat.agent_client._log_pinned_oneshot"),
			patch.object(native_runtime.frappe, "get_cached_value", side_effect=lambda *a: V11.get(a[2])),
		):
			session._apply_oneshot_model("k", "kimi-k2.6", "kimi", "idem")
		self.assertEqual(pin.call_args.args[:2], ("k", "kimi_api/kimi-k2.6"))

	def test_gateway_rows_are_read_back_as_the_app_ids(self):
		row = {"model": "kimi-k2.6", "modelProvider": "moonshot_api"}
		self.assertEqual(usage.resolved_model_identity(row), ("kimi-k2.6", "moonshot"))
		self.assertEqual(
			usage.resolved_model_identity({"model": "m", "modelProvider": "openai"}), ("m", "openai")
		)
		self.assertEqual(session_pin_sweep._model_ref(row), "moonshot/kimi-k2.6")
		self.assertEqual(session_pin_sweep._model_ref({"model": "gpt-5.5"}), "gpt-5.5")


class TestNoPlaceholderOnANativeTenant(unittest.TestCase):
	def _pin(self, **extra):
		settings = _settings([_sub(), _sub("grok-4", "xai", order=1)], proxy_active=1, **extra)
		with (
			patch.object(turn_handler.frappe, "get_single", return_value=settings),
			patch.object(turn_handler, "_resolve_model_and_provider", return_value=("", None)),
		):
			return turn_handler._session_model_for(frappe._dict())

	def test_a_sidecar_pool_names_the_placeholder(self):
		self.assertEqual(self._pin(), (turn_handler.POOL_VIRTUAL_MODEL, None))

	def test_a_native_tenant_resets_the_session_instead(self):
		# Even with a stale stored proxy_active=1 (the heartbeat has not refreshed it yet).
		self.assertEqual(self._pin(native_subscription_pool=1), (None, None))


class TestPersistFacts(FrappeTestCase):
	def _persist(self, reply, stored):
		fields = {"native_subscription_pool": 0, "agent_runtime_version": "", **stored}
		with (
			patch.object(native_runtime.frappe, "get_cached_value", return_value=frappe._dict(fields)),
			patch.object(native_runtime.frappe.db, "set_single_value") as write,
			patch.object(native_runtime, "_refresh_proxy_active") as refresh,
		):
			native_runtime.persist(reply)
		return write, refresh

	def test_a_new_value_is_written_and_a_flag_change_recomputes_proxy_active(self):
		write, refresh = self._persist({"native_subscription_pool": True, **V11}, {})
		self.assertEqual(
			write.call_args.args[1], {"native_subscription_pool": 1, "agent_runtime_version": "2026.11.5"}
		)
		refresh.assert_called_once()

	def test_a_version_change_alone_does_not_touch_proxy_active(self):
		write, refresh = self._persist(
			{"agent_runtime_version": "2026.11.5"}, {"native_subscription_pool": 1}
		)
		self.assertEqual(write.call_args.args[1], {"agent_runtime_version": "2026.11.5"})
		refresh.assert_not_called()

	def test_unchanged_absent_or_malformed_values_write_nothing(self):
		stored = {"native_subscription_pool": 1, "agent_runtime_version": "2026.11.5"}
		for reply in (
			{"native_subscription_pool": True, **V11},
			{},
			None,
			{"native_subscription_pool": "yes", "agent_runtime_version": "latest"},
		):
			write, refresh = self._persist(reply, stored)
			write.assert_not_called()
			refresh.assert_not_called()

	def test_a_null_version_clears_the_stored_one(self):
		write, _ = self._persist({"agent_runtime_version": None}, {"agent_runtime_version": "2026.11.5"})
		self.assertEqual(write.call_args.args[1], {"agent_runtime_version": ""})


class TestHeartbeatCarriesTheFacts(unittest.TestCase):
	def test_reply_facts_are_stored_and_a_fault_is_contained(self):
		from jarvis.chat import heartbeat

		with patch.object(native_runtime, "persist") as persist:
			heartbeat._apply_native_facts_safely({"native_subscription_pool": True})
		persist.assert_called_once_with({"native_subscription_pool": True})
		with (
			patch.object(native_runtime, "persist", side_effect=RuntimeError("db")),
			patch.object(heartbeat, "_log_failure_throttled") as logged,
		):
			heartbeat._apply_native_facts_safely({})
		logged.assert_called_once()


class TestNativeUsage(unittest.TestCase):
	_ROWS = [
		{"model": "gpt-5.5", "in_": 1_000_000, "out_": 1_000_000},
		{"model": "gpt-4o", "in_": 1_000_000, "out_": 0},
	]

	def _usage(self, settings):
		with (
			patch.object(usage, "current_month_key", return_value="2026-11"),
			patch.object(usage, "tenant_wide_per_model_tokens", return_value=self._ROWS),
			patch("jarvis.chat.pricing.price_for_model", return_value=(2.0, 10.0)),
		):
			return account._direct_llm_usage(settings)

	def test_sign_in_legs_show_tokens_only_and_key_legs_keep_their_cost(self):
		out = self._usage(_settings([_sub("gpt-5.5"), _key("gpt-4o")], native_subscription_pool=1))
		by_model = {r["model"]: r for r in out["per_model"]}
		self.assertEqual(by_model["gpt-5.5"]["cost_usd"], 0.0)
		self.assertTrue(by_model["gpt-5.5"]["subscription"])
		self.assertEqual(by_model["gpt-5.5"]["tokens_out"], 1_000_000)
		self.assertEqual(by_model["gpt-4o"]["cost_usd"], 2.0)
		self.assertNotIn("subscription", by_model["gpt-4o"])
		self.assertEqual(out["cost_usd"], 2.0)

	def test_the_direct_path_without_settings_is_unchanged(self):
		out = self._usage(None)
		self.assertEqual(out["cost_usd"], 12.0 + 2.0)

	def _get_usage(self, settings):
		with (
			patch.object(account, "require_jarvis_admin"),
			patch.object(account.frappe, "get_single", return_value=settings),
			patch.object(account, "_direct_llm_usage", return_value={"applicable": True}) as direct,
			patch.object(account.admin_client, "get_llm_usage", MagicMock(return_value={"x": 1})),
		):
			return account.get_llm_usage(), direct

	def test_a_native_subscription_tenant_gets_usage_instead_of_the_managed_proxy_text(self):
		sub = _settings([_sub()], proxy_active=0, llm_auth_mode="subscription")
		out, direct = self._get_usage(sub)
		self.assertFalse(out["applicable"])  # the managed-proxy wording
		direct.assert_not_called()
		sub.native_subscription_pool = 1
		out, direct = self._get_usage(sub)
		direct.assert_called_once_with(sub)
		self.assertTrue(out["applicable"])


class TestPinRetryWithTheOtherForm(unittest.TestCase):
	def test_alternate_ref_flips_only_the_seven_ids(self):
		self.assertEqual(gateway_ids.alternate_ref("moonshot/kimi-k2.6"), "moonshot_api/kimi-k2.6")
		self.assertEqual(gateway_ids.alternate_ref("moonshot_api/kimi-k2.6"), "moonshot/kimi-k2.6")
		for ref in ("openai/gpt-5.5", "gpt-5.5", "moonshot-0/x", None):
			self.assertIsNone(gateway_ids.alternate_ref(ref))

	def _session(self, errors):
		from jarvis.exceptions import AgentUnreachableError

		session = object.__new__(AgentSession)
		calls = []

		def request(method, params, timeout_s=None):
			calls.append(params["model"])
			if errors:
				err = AgentUnreachableError("refused")
				err.rejected_method, err.code = "sessions.patch", errors.pop(0)
				raise err
			return {}

		session._request = request
		return session, calls

	def test_a_refused_pin_is_retried_once_with_the_other_form(self):
		session, calls = self._session(["INVALID_REQUEST"])
		session.set_session_model("k", "moonshot/kimi-k2.6")
		self.assertEqual(calls, ["moonshot/kimi-k2.6", "moonshot_api/kimi-k2.6"])

	def test_the_first_error_is_raised_when_the_retry_fails_too(self):
		from jarvis.exceptions import AgentUnreachableError

		session, calls = self._session(["INVALID_REQUEST", "FORBIDDEN"])
		with self.assertRaises(AgentUnreachableError) as cm:
			session.set_session_model("k", "moonshot/kimi-k2.6")
		self.assertEqual(cm.exception.code, "INVALID_REQUEST")
		self.assertEqual(len(calls), 2)

	def test_temporary_errors_and_other_providers_are_not_retried(self):
		from jarvis.exceptions import AgentUnreachableError

		for ref, code in (("moonshot/kimi-k2.6", "UNAVAILABLE"), ("openai/gpt-5.5", "INVALID_REQUEST")):
			session, calls = self._session([code])
			with self.assertRaises(AgentUnreachableError):
				session.set_session_model("k", ref)
			self.assertEqual(len(calls), 1)


class TestClaudeMemberOnANativePool(unittest.TestCase):
	def _claude(self, order):
		return _sub("claude-opus-5", "anthropic", order=order)

	def _errors(self, models, **extra):
		return pool_serialize.validate_models(_settings(models, **extra))

	def test_claude_in_the_middle_is_allowed_only_on_a_native_tenant(self):
		models = [
			_sub("gpt-5.5", order=0),
			self._claude(1),
			_sub("gpt-5.6", order=2, accounts=[_acct("openai", "A2")]),
		]
		self.assertIn("Claude runs outside the proxy. Keep it first or last.", self._errors(models))
		self.assertNotIn(
			"Claude runs outside the proxy. Keep it first or last.",
			self._errors(models, native_subscription_pool=1),
		)

	def test_xai_may_join_a_claude_pool_only_on_a_native_tenant(self):
		models = [self._claude(0), _sub("grok-4", "xai", order=1)]
		self.assertIn("Claude can be combined only with ChatGPT or API keys for now.", self._errors(models))
		errors = self._errors(models, native_subscription_pool=1)
		self.assertFalse([e for e in errors if "Claude can be combined" in e])

	def test_kimi_is_still_refused_with_claude_on_a_native_tenant(self):
		models = [self._claude(0), _sub("kimi-k2", "kimi", order=1)]
		self.assertTrue(
			[e for e in self._errors(models, native_subscription_pool=1) if "Claude can be combined" in e]
		)

	def test_one_claude_row_and_one_account_still_apply_on_native(self):
		two_rows = [self._claude(0), self._claude(1)]
		self.assertIn(
			"Only one Claude subscription model is supported per pool.",
			self._errors(two_rows, native_subscription_pool=1),
		)
		two_accounts = [
			_sub("claude-opus-5", "anthropic", accounts=[_acct("anthropic", "A"), _acct("anthropic", "B")])
		]
		self.assertTrue(
			[
				e
				for e in self._errors(two_accounts, native_subscription_pool=1)
				if "exactly one connected account" in e
			]
		)

	def test_a_claude_member_does_not_turn_the_proxy_back_on(self):
		settings = _settings([self._claude(0), _sub("gpt-5.5", order=1)], native_subscription_pool=1)
		self.assertTrue(pool_serialize.compute_pool_mode(settings))
		self.assertFalse(pool_serialize.compute_proxy_active(settings))
		# The reset path, not the placeholder, even with a stale stored proxy_active.
		settings.proxy_active = 1
		with (
			patch.object(turn_handler.frappe, "get_single", return_value=settings),
			patch.object(turn_handler, "_resolve_model_and_provider", return_value=("", None)),
		):
			self.assertEqual(turn_handler._session_model_for(frappe._dict()), (None, None))

	def test_a_chatgpt_pick_names_its_own_provider_on_native_and_the_door_otherwise(self):
		models = [self._claude(0), _sub("gpt-5.5", order=1), _sub("grok-4", "xai", order=2)]
		conv = frappe._dict(model_override="gpt-5.5")
		for extra, expected in (
			({}, turn_handler.PROXY_DOOR_PROVIDER),
			({"native_subscription_pool": 1}, "openai"),
		):
			settings = _settings(models, **extra)
			with (
				patch.object(turn_handler.frappe, "get_single", return_value=settings),
				patch.object(turn_handler, "_is_native_claude_pick", return_value=False),
			):
				self.assertEqual(turn_handler._resolve_model_and_provider(conv), ("gpt-5.5", expected))
		settings = _settings(models, native_subscription_pool=1)
		with (
			patch.object(turn_handler.frappe, "get_single", return_value=settings),
			patch.object(turn_handler, "_is_native_claude_pick", return_value=False),
		):
			conv.model_override = "grok-4"
			self.assertEqual(turn_handler._resolve_model_and_provider(conv), ("grok-4", "xai"))
