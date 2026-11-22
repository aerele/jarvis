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

	def test_the_proxy_era_combination_rule_no_longer_judges_kimi_next_to_claude_on_native(self):
		# Whether the runtime can serve a Kimi sign-in is admin's native check (no Kimi sign-in
		# exists in the runtime); the app no longer applies the proxy-era combination rule.
		models = [self._claude(0), _sub("kimi-k2", "kimi", order=1)]
		self.assertFalse(
			[e for e in self._errors(models, native_subscription_pool=1) if "Claude can be combined" in e]
		)

	def test_one_claude_account_per_row_still_applies_on_native(self):
		# Several Claude models on the one login are fine on native (see TestNativeClaudeAndSignInIdRules).
		two_rows = [self._claude(0), self._claude(1)]
		self.assertNotIn(
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


class TestPinSelfHealWhenFactsAreStale(unittest.TestCase):
	_MODELS = [_sub("claude-opus-5", "anthropic", order=0), _sub("gpt-5.5", order=1)]

	def _patch(self, settings, conv):
		with (
			patch.object(turn_handler.frappe, "get_single", return_value=settings),
			patch.object(turn_handler, "_is_native_claude_pick", return_value=False),
		):
			return turn_handler._session_model_patch(conv)

	def test_a_conversation_pinned_before_the_flip_is_repatched_with_the_real_provider(self):
		conv = frappe._dict(model_override="gpt-5.5")
		before = self._patch(_settings(self._MODELS, proxy_active=1), conv)
		self.assertEqual(before, (True, "openai_compat/gpt-5.5"))
		after = self._patch(_settings(self._MODELS, proxy_active=0, native_subscription_pool=1), conv)
		self.assertEqual(after, (True, "openai/gpt-5.5"))

	def _refused(self, code="INVALID_REQUEST"):
		from jarvis.exceptions import AgentUnreachableError

		err = AgentUnreachableError("refused")
		err.rejected_method, err.code = "sessions.patch", code
		return err

	def _run(self, errors, refreshed, fresh_ref):
		sess = MagicMock()
		sess.set_session_model.side_effect = errors
		with (
			patch("jarvis.native_runtime.refresh_from_admin", return_value=refreshed) as refresh,
			patch.object(turn_handler, "_session_model_patch", return_value=(True, fresh_ref)),
		):
			try:
				turn_handler.set_session_model_refreshing(sess, "k", frappe._dict(), "openai_compat/gpt-5.5")
				raised = None
			except Exception as e:
				raised = e
		return sess, refresh, raised

	def test_a_refused_pin_refreshes_the_facts_once_and_retries_with_the_new_pin(self):
		sess, refresh, raised = self._run([self._refused(), None], True, "openai/gpt-5.5")
		self.assertIsNone(raised)
		refresh.assert_called_once()
		self.assertEqual(
			[c.args[1] for c in sess.set_session_model.call_args_list],
			["openai_compat/gpt-5.5", "openai/gpt-5.5"],
		)

	def test_the_original_error_stands_when_the_refresh_fails_or_changes_nothing(self):
		first = self._refused()
		for refreshed, fresh in ((False, "openai/gpt-5.5"), (True, "openai_compat/gpt-5.5")):
			sess, _, raised = self._run([first], refreshed, fresh)
			self.assertIs(raised, first)
			self.assertEqual(sess.set_session_model.call_count, 1)

	def test_a_second_refusal_is_not_refreshed_again(self):
		sess, refresh, raised = self._run([self._refused(), self._refused()], True, "openai/gpt-5.5")
		self.assertIsNotNone(raised)
		refresh.assert_called_once()
		self.assertEqual(sess.set_session_model.call_count, 2)

	def test_temporary_errors_and_session_changed_do_not_refresh(self):
		for code in ("UNAVAILABLE",):
			sess, refresh, raised = self._run([self._refused(code)], True, "openai/gpt-5.5")
			self.assertIsNotNone(raised)
			refresh.assert_not_called()
		changed = self._refused()
		changed.details = {"reason": "session-changed"}
		sess, refresh, raised = self._run([changed], True, "openai/gpt-5.5")
		self.assertIs(raised, changed)
		refresh.assert_not_called()

	def test_leave_alone_after_a_refresh_is_not_a_retry_and_never_a_reset(self):
		sess = MagicMock()
		sess.set_session_model.side_effect = [self._refused()]
		with (
			patch("jarvis.native_runtime.refresh_from_admin", return_value=True),
			patch.object(turn_handler, "_session_model_patch", return_value=(False, None)),
		):
			with self.assertRaises(Exception):
				turn_handler.set_session_model_refreshing(sess, "k", frappe._dict(), "openai_compat/gpt-5.5")
		self.assertEqual(sess.set_session_model.call_count, 1)  # no null (reset) patch sent

	def test_the_refresh_is_throttled_to_once_a_minute(self):
		cache = MagicMock()
		cache.get_value.return_value = None
		with (
			patch.object(native_runtime.frappe, "cache", return_value=cache),
			patch("jarvis.admin_client.get_connection", return_value={}) as read,
			patch.object(native_runtime, "persist"),
		):
			self.assertTrue(native_runtime.refresh_from_admin())
			cache.set_value.assert_called_once()
			self.assertEqual(cache.set_value.call_args.kwargs["expires_in_sec"], 60)
			cache.get_value.return_value = 1
			self.assertFalse(native_runtime.refresh_from_admin())
		read.assert_called_once()

	def test_refresh_from_admin_stores_the_connection_facts_and_never_raises(self):
		reply = {"native_subscription_pool": True, "agent_runtime_version": "2026.11.5"}
		with (
			patch("jarvis.admin_client.get_connection", return_value=reply) as read,
			patch.object(native_runtime, "persist") as persist,
		):
			self.assertTrue(native_runtime.refresh_from_admin())
		read.assert_called_once()
		persist.assert_called_once_with(reply)
		with patch("jarvis.admin_client.get_connection", side_effect=RuntimeError("down")):
			self.assertFalse(native_runtime.refresh_from_admin())


class TestNativePoolShapesPassTheAppChecks(unittest.TestCase):
	def _errors(self, models, native=True):
		return pool_serialize.validate_models(_settings(models, native_subscription_pool=1 if native else 0))

	def test_two_sign_in_kinds_with_no_api_key_have_no_app_side_backstop_rule(self):
		models = [
			_sub("gpt-5.6-terra", "openai", order=0, accounts=[_acct("openai", "O1")]),
			_sub("grok-4.3", "xai", order=1, accounts=[_acct("xai", "X1")]),
		]
		self.assertEqual(self._errors(models), [])
		self.assertEqual(self._errors(models, native=False), [])  # the rule is admin's, not the app's

	def test_xai_alone_xai_with_claude_and_claude_removed(self):
		self.assertEqual(self._errors([_sub("grok-4.3", "xai", accounts=[_acct("xai", "X1")])]), [])
		# xAI next to a Claude member: none of the position or combination rules apply on native
		# (the Claude credential itself is a separate check, not exercised here).
		claude = _sub("claude-opus-5", "anthropic", order=0, accounts=[_acct("anthropic", "C1")])
		xai = _sub("grok-4.3", "xai", order=1, accounts=[_acct("xai", "X1")])
		errors = self._errors([claude, xai])
		self.assertFalse(
			[e for e in errors if "combined" in e or "first or last" in e or "backstop" in e.lower()]
		)
		# the Claude member removed again leaves a plain pool that validates
		self.assertEqual(
			self._errors([_sub("gpt-5.6-terra", "openai", accounts=[_acct("openai", "O1")])]), []
		)

	def test_get_llm_config_carries_the_native_flag(self):
		from jarvis import onboarding

		with (
			patch.object(onboarding, "require_jarvis_admin"),
			patch.object(
				onboarding.frappe,
				"get_single",
				return_value=_settings([], native_subscription_pool=1, proxy_active=0),
			),
		):
			out = onboarding.get_llm_config()
		self.assertIs(out["native_subscription_pool"], True)


class TestProxyEraRulesAreGoneOnNative(unittest.TestCase):
	def _errors(self, models, native):
		return pool_serialize.validate_models(_settings(models, native_subscription_pool=1 if native else 0))

	def test_claude_in_the_middle_and_next_to_xai_validate_only_on_native(self):
		def claude():
			return _sub("claude-opus-5", "anthropic", order=1, accounts=[_acct("anthropic", "C1")])

		models = [
			_sub("gpt-5.6-terra", "openai", order=0, accounts=[_acct("openai", "O1")]),
			claude(),
			_sub("grok-4.3", "xai", order=2, accounts=[_acct("xai", "X1")]),
		]
		sidecar = self._errors(models, native=False)
		native = self._errors(models, native=True)
		self.assertTrue([e for e in sidecar if "first or last" in e or "combined" in e])
		self.assertFalse([e for e in native if "first or last" in e or "combined" in e])

	def test_one_claude_row_and_one_account_still_hold_on_native(self):
		two = [
			_sub("claude-opus-5", "anthropic", order=0, accounts=[_acct("anthropic", "C1")]),
			_sub("claude-sonnet-5", "anthropic", order=1, accounts=[_acct("anthropic", "C2")]),
		]
		self.assertTrue([e for e in self._errors(two, True) if "one Claude account" in e])

	def test_the_chain_keeps_every_member_in_the_customers_order_with_no_cap(self):
		models = [_sub(f"m{i}", "openai", order=i, accounts=[_acct("openai", f"O{i}")]) for i in range(12)]
		spec, _keys, _blobs = pool_serialize.build_pool_payload(_settings(models, native_subscription_pool=1))
		self.assertEqual([m["model"] for m in spec["models"]], [f"m{i}" for i in range(12)])
		self.assertEqual([m["order"] for m in spec["models"]], list(range(12)))


def _keyrow(model, provider, order, enabled=1):
	return frappe._dict(
		model=model,
		tier="strong",
		order=order,
		enabled=enabled,
		credential_type="api_key",
		provider=provider,
		api_key="sk",
		accounts=[],
	)


class TestEveryNativePickNamesItsOwnProvider(unittest.TestCase):
	def _pick(self, models, model, native=True):
		settings = _settings(models, native_subscription_pool=1 if native else 0)
		conv = frappe._dict(model_override=model)
		with (
			patch.object(turn_handler.frappe, "get_single", return_value=settings),
			patch.object(turn_handler, "_is_native_claude_pick", return_value=False),
		):
			return turn_handler._resolve_model_and_provider(conv)

	def test_a_pool_without_a_claude_member_pins_each_sign_in_by_its_upstream(self):
		models = [
			_sub("gpt-5.6-terra", "openai", order=0, accounts=[_acct("openai", "O1")]),
			_sub("grok-4.3", "xai", order=1, accounts=[_acct("xai", "X1")]),
		]
		self.assertEqual(self._pick(models, "grok-4.3"), ("grok-4.3", "xai"))
		self.assertEqual(self._pick(models, "gpt-5.6-terra"), ("gpt-5.6-terra", "openai"))
		# The live bug: the picked ref was "openai/grok-4.3".
		model, provider = self._pick(models, "grok-4.3")
		self.assertEqual(
			gateway_ids.model_ref_for_gateway(provider, model, frappe._dict(V11)), "xai/grok-4.3"
		)

	def test_the_first_row_in_pool_order_wins_when_two_rows_serve_one_model(self):
		sub = _sub("gpt-5.5", "openai", order=1, accounts=[_acct("openai", "O1")])
		self.assertEqual(
			self._pick([sub, _keyrow("gpt-5.5", "openai_compat", 0)], "gpt-5.5")[1], "openai_compat-0"
		)
		self.assertEqual(self._pick([sub, _keyrow("gpt-5.5", "openai_compat", 2)], "gpt-5.5")[1], "openai")

	def test_an_api_key_row_uses_the_id_the_fleet_renders_for_it(self):
		# Fleet numbers the enabled API-key chat rows in pool order: sign-ins, disabled rows and
		# image models do not take a number, so a reorder or a disable keeps the numbering.
		models = [
			_keyrow("deepseek-chat", "deepseek", 2),
			_keyrow("skipped", "groq", 0, enabled=0),
			_sub("gpt-5.5", "openai", order=1, accounts=[_acct("openai", "O1")]),
			_keyrow("gpt-image-2", "openai_compat", 2),
			_keyrow("llama-3.3", "groq", 3),
		]
		self.assertEqual(self._pick(models, "deepseek-chat")[1], "deepseek-0")
		self.assertEqual(self._pick(models, "llama-3.3")[1], "groq-1")
		self.assertEqual(self._pick(models, "gpt-5.5")[1], "openai")

	def test_a_model_id_with_a_slash_is_pinned_as_provider_then_the_whole_id(self):
		models = [
			_keyrow("anthropic/claude-sonnet-5", "openrouter", 0),
			_keyrow("deepseek/deepseek-r1", "openrouter", 1),
		]
		model, provider = self._pick(models, "deepseek/deepseek-r1")
		self.assertEqual(provider, "openrouter-1")
		ref = gateway_ids.model_ref_for_gateway(provider, model, frappe._dict(V11))
		self.assertEqual(ref, "openrouter-1/deepseek/deepseek-r1")

	def test_the_2026_11_rename_is_applied_last_and_never_to_a_numbered_id(self):
		v11 = frappe._dict(V11)
		self.assertEqual(
			gateway_ids.model_ref_for_gateway("moonshot-0", "kimi-k2.6", v11), "moonshot-0/kimi-k2.6"
		)
		self.assertEqual(gateway_ids.model_ref_for_gateway("xai", "grok-4.3", v11), "xai/grok-4.3")

	def test_a_model_no_row_serves_has_no_provider_and_a_sidecar_tenant_is_unchanged(self):
		models = [_sub("grok-4.3", "xai", order=0, accounts=[_acct("xai", "X1")])]
		self.assertEqual(turn_handler._native_pick_provider(_settings(models), "nothing"), None)
		self.assertEqual(self._pick(models, "grok-4.3", native=False), ("grok-4.3", None))

	def test_one_shot_and_title_pins_use_the_same_resolved_provider(self):
		models = [
			_sub("gpt-5.6-terra", "openai", order=0, accounts=[_acct("openai", "O1")]),
			_sub("grok-4.3", "xai", order=1, accounts=[_acct("xai", "X1")]),
		]
		model, provider = self._pick(models, "grok-4.3")  # what title / polish / suggestions / prewarm get
		session = object.__new__(AgentSession)
		with (
			patch.object(AgentSession, "set_session_model") as pin,
			patch("jarvis.chat.agent_client._log_pinned_oneshot"),
			patch.object(native_runtime.frappe, "get_cached_value", side_effect=lambda *a: V11.get(a[2])),
		):
			session._apply_oneshot_model("k", model, provider, "idem")
		self.assertEqual(pin.call_args.args[:2], ("k", "xai/grok-4.3"))
		# no pick, no pin: an Auto conversation's one-shots never name a ref the gateway lacks
		with (
			patch.object(
				turn_handler.frappe, "get_single", return_value=_settings(models, native_subscription_pool=1)
			),
		):
			self.assertEqual(
				turn_handler._resolve_model_and_provider(frappe._dict(model_override="")), ("", None)
			)


class TestKeyedRowNumberingMatchesFleet(unittest.TestCase):
	"""The cases fleet pins in test_keyed_row_numbering_rule_on_native."""

	def _rows(self):
		return [
			_keyrow("gpt-5.5", "openai", 1),
			_keyrow("deepseek-chat", "deepseek", 1),
			_keyrow("Qwen/Qwen3", "openai_compat", 1),
			_sub("claude-opus-5", "anthropic", order=0, accounts=[_acct("anthropic", "C1")]),
			_keyrow("claude-opus-5", "anthropic", 2, enabled=0),
			_keyrow("claude-sonnet-5", "anthropic", 0),
			_keyrow("gpt-image-1.5", "openai", 0),
			_sub("gpt-5.6-terra", "openai", order=0, accounts=[_acct("openai", "O1")]),
			_keyrow("deepseek-chat", "deepseek", 1),
		]

	def _provider(self, model):
		return turn_handler._native_pick_provider(_settings(self._rows(), native_subscription_pool=1), model)

	def test_ids_follow_the_fleets_order_and_one_counter(self):
		self.assertEqual(
			self._provider("claude-sonnet-5"), "anthropic-0"
		)  # order 0, "anthropic" < "anthropic_cli"
		self.assertEqual(self._provider("Qwen/Qwen3"), "openai_compat-1")  # sorts as compat-qwen-qwen3
		self.assertEqual(self._provider("deepseek-chat"), "deepseek-2")  # first of a full tie
		self.assertEqual(self._provider("gpt-5.5"), "openai-4")  # the tied twin took 3
		self.assertEqual(self._provider("gpt-5.6-terra"), "openai")  # sign-in: no number
		self.assertEqual(self._provider("claude-opus-5"), "anthropic")  # the Claude member: no number
		self.assertIsNone(self._provider("gpt-image-1.5"))  # image rows are never picked as chat

	def test_zai_sorts_by_its_own_name_and_a_stored_order_tie_is_stable(self):
		rows = [
			_keyrow("glm-4.6", "zai", 0),
			_keyrow("aaa", "openai_compat", 0),
			_keyrow("zzz", "anthropic", 0),
		]
		settings = _settings(rows, native_subscription_pool=1)
		# The app sends a z.ai row on the openai_compat transport (pool_serialize._wire_provider), so
		# the fleet sorts it as compat-<model> and names it openai_compat-<n>.
		# sort names: anthropic < compat-aaa < compat-glm-4.6
		self.assertEqual(turn_handler._native_pick_provider(settings, "zzz"), "anthropic-0")
		self.assertEqual(turn_handler._native_pick_provider(settings, "aaa"), "openai_compat-1")
		self.assertEqual(turn_handler._native_pick_provider(settings, "glm-4.6"), "openai_compat-2")


class TestNativeClaudeAndSignInIdRules(unittest.TestCase):
	def _errors(self, models):
		return pool_serialize.validate_models(_settings(models, native_subscription_pool=1))

	def _claude(self, model, ref, order=0):
		return _sub(model, "anthropic", order=order, accounts=[_acct("anthropic", ref)])

	def test_any_number_of_claude_models_on_one_login(self):
		models = [self._claude("claude-opus-5", "C1", 0), self._claude("claude-sonnet-5", "C1", 1)]
		self.assertEqual(
			[e for e in self._errors(models) if "one Claude account" in e or "Duplicate" in e], []
		)

	def test_a_second_claude_login_is_refused(self):
		models = [self._claude("claude-opus-5", "C1", 0), self._claude("claude-sonnet-5", "C2", 1)]
		self.assertTrue([e for e in self._errors(models) if "one Claude account" in e])

	def test_the_claude_id_has_a_shape_and_no_list(self):
		self.assertEqual(
			[e for e in self._errors([self._claude("claude-opus-9.9-x", "C1")]) if "Claude model id" in e], []
		)
		self.assertTrue([e for e in self._errors([self._claude("gpt-5.5", "C1")]) if "Claude model id" in e])

	def test_chatgpt_and_xai_ids_are_shape_checked_only(self):
		ok = [_sub("grok-9-brand-new", "xai", accounts=[_acct("xai", "X1")])]
		self.assertEqual([e for e in self._errors(ok) if "sign-in model id" in e], [])
		bad = [_sub("openai/gpt-5.5", "openai", accounts=[_acct("openai", "O1")])]
		self.assertTrue([e for e in self._errors(bad) if "sign-in model id" in e])

	def test_a_sidecar_pool_keeps_one_claude_row(self):
		two = [self._claude("claude-opus-5", "C1", 0), self._claude("claude-sonnet-5", "C2", 1)]
		errors = pool_serialize.validate_models(_settings(two, native_subscription_pool=0))
		self.assertTrue([e for e in errors if "Only one Claude" in e])


class TestNativeSignInHealthReachesTheSite(unittest.TestCase):
	"""Admin marks a sign-in the native apply could not make usable as expired; the site stores it
	through the existing health mechanism, so the pool editor shows it as needing a reconnect."""

	def test_an_expired_entry_from_a_native_tenant_is_stored_whatever_its_source(self):
		from jarvis import subscription_health as sh

		live = {"X1": {"upstream": "xai", "label": "xAI Grok", "email": ""}}
		reply = {"X1": {"state": "expired", "source": "runtime", "since": 1000, "label": "", "email": ""}}
		with (
			patch.object(sh, "_live_accounts", return_value=live),
			patch.object(sh, "current_health", return_value={}),
			patch.object(sh, "_cleared_at", return_value=0),
			patch.object(sh, "_store", side_effect=lambda merged: bool(merged)) as store,
		):
			self.assertTrue(sh.apply_admin_health(reply))
		stored = store.call_args.args[0]
		self.assertEqual((stored["X1"]["state"], stored["X1"]["source"]), ("expired", "poll"))

	def test_an_ok_entry_clears_a_stored_poll_expiry(self):
		from jarvis import subscription_health as sh

		live = {"X1": {"upstream": "xai", "label": "xAI Grok", "email": ""}}
		prior = {
			"X1": {
				"upstream": "xai",
				"label": "xAI Grok",
				"email": "",
				"state": "expired",
				"source": "poll",
				"since": 5,
			}
		}
		with (
			patch.object(sh, "_live_accounts", return_value=live),
			patch.object(sh, "current_health", return_value=prior),
			patch.object(sh, "_cleared_at", return_value=0),
			patch.object(sh, "_store", side_effect=lambda merged: True) as store,
		):
			sh.apply_admin_health({"X1": {"state": "ok", "source": "runtime"}})
		self.assertEqual(store.call_args.args[0], {})
