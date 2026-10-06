"""Site-side health of the chat-subscription sign-ins (jarvis.subscription_health).

``unittest.TestCase`` only: the DB seam (``_read_stored`` / ``_write_stored``) and the pool
introspection (``_live_accounts``) are patched, so nothing here saves Jarvis Settings or commits.
The Redis signal queue is real but lives under one key that setUp/tearDown delete.
"""

import json
import unittest
from unittest.mock import patch

import frappe

from jarvis import subscription_health as sh

POOL = {
	"ACC_OA1": {"upstream": "openai", "label": "OpenAI", "email": "a@x.com"},
	"ACC_OA2": {"upstream": "openai", "label": "OpenAI", "email": "b@x.com"},
	"ACC_CL1": {"upstream": "anthropic", "label": "Anthropic", "email": "c@x.com"},
}
DIRECT = {sh.DIRECT_REF: {"upstream": "openai", "label": "OpenAI", "email": "d@x.com"}}


def _entry(ref, source="poll", since=100, accounts=POOL):
	acct = accounts[ref]
	return {**acct, "state": "expired", "source": source, "since": since}


class _Base(unittest.TestCase):
	live = POOL

	def setUp(self):
		self.stored = ""
		self.writes = []
		self.events = []

		def write(text):
			self.stored = text
			self.writes.append(text)

		self.patches = [
			patch.object(sh, "_read_stored", side_effect=lambda: self.stored),
			patch.object(sh, "_write_stored", side_effect=write),
			patch.object(sh, "_live_accounts", side_effect=lambda: dict(self.live)),
			patch.object(sh, "_publish_changed", side_effect=lambda: self.events.append(1)),
		]
		for p in self.patches:
			p.start()
		frappe.cache().delete_value(sh.SIGNAL_KEY)

	def tearDown(self):
		for p in self.patches:
			p.stop()
		frappe.cache().delete_value(sh.SIGNAL_KEY)

	def seed(self, health):
		self.stored = json.dumps(health, sort_keys=True, separators=(",", ":"))


class TestApplyAdminHealth(_Base):
	def test_absent_field_changes_nothing(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat")})
		self.assertFalse(sh.apply_admin_health(None))
		self.assertEqual(self.writes, [])
		self.assertEqual(list(sh.current_health()), ["ACC_OA1"])

	def test_admin_expired_entry_is_stored_as_poll_and_published(self):
		changed = sh.apply_admin_health(
			{
				"ACC_OA1": {
					"upstream": "openai",
					"label": "OpenAI",
					"email": "a@x.com",
					"state": "expired",
					"source": "poll",
					"since": 500,
				}
			}
		)
		self.assertTrue(changed)
		stored = sh.current_health()["ACC_OA1"]
		self.assertEqual((stored["state"], stored["source"], stored["since"]), ("expired", "poll", 500))
		self.assertEqual(self.events, [1])

	def test_identical_merge_skips_the_write(self):
		payload = {"ACC_OA1": {"label": "OpenAI", "email": "a@x.com", "state": "expired", "since": 500}}
		sh.apply_admin_health(payload)
		self.writes.clear()
		self.events.clear()
		self.assertFalse(sh.apply_admin_health(payload))
		self.assertEqual((self.writes, self.events), ([], []))

	def test_admin_empty_map_clears_poll_entries_but_never_a_chat_entry(self):
		"""Review Focus 4: an admin response must never clear a chat-source entry."""
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll"), "ACC_CL1": _entry("ACC_CL1", source="chat")})
		sh.apply_admin_health({})
		self.assertEqual(list(sh.current_health()), ["ACC_CL1"])

	def test_admin_ok_for_a_chat_entry_keeps_it(self):
		self.seed({"ACC_CL1": _entry("ACC_CL1", source="chat")})
		sh.apply_admin_health({"ACC_OA1": {"state": "expired", "since": 5}})
		self.assertIn("ACC_CL1", sh.current_health())
		self.assertEqual(sh.current_health()["ACC_CL1"]["source"], "chat")

	def test_admin_entry_keeps_the_source_admin_gave_it(self):
		"""R1: an admin entry with source chat stays chat (it is not promoted to poll)."""
		sh.apply_admin_health({"ACC_OA1": {"state": "expired", "source": "chat", "since": 900}})
		self.assertEqual(sh.current_health()["ACC_OA1"]["source"], "chat")
		sh.apply_admin_health({"ACC_OA1": {"state": "expired", "source": "chat", "since": 900}})
		self.assertEqual(sh.current_health()["ACC_OA1"]["source"], "chat")

	def test_admin_expired_on_a_local_chat_entry_keeps_the_earlier_since(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat", since=100)})
		sh.apply_admin_health({"ACC_OA1": {"state": "expired", "source": "poll", "since": 900}})
		entry = sh.current_health()["ACC_OA1"]
		self.assertEqual((entry["source"], entry["since"]), ("poll", 100))

	def test_an_unknown_source_from_admin_is_stored_as_poll(self):
		sh.apply_admin_health({"ACC_OA1": {"state": "expired", "source": "weird", "since": 5}})
		self.assertEqual(sh.current_health()["ACC_OA1"]["source"], "poll")

	def test_reconnect_drops_the_old_account_ref_and_the_new_one_starts_clean(self):
		"""Review Focus 3: a reconnect mints a new account_ref."""
		self.seed({"ACC_OLD": {**_entry("ACC_OA1"), "since": 1}})
		self.live = {"ACC_NEW": POOL["ACC_OA1"]}
		sh.apply_admin_health({})
		self.assertEqual(sh.current_health(), {})
		self.assertEqual(sh.expired_entries(), [])

	def test_admin_entries_for_accounts_not_in_the_pool_are_ignored(self):
		sh.apply_admin_health({"ACC_GONE": {"state": "expired", "since": 1}})
		self.assertEqual(sh.current_health(), {})

	def test_junk_entries_and_unknown_states_are_ignored(self):
		sh.apply_admin_health({"ACC_OA1": "x", "ACC_OA2": {"state": "unknown"}, "ACC_CL1": {"state": "ok"}})
		self.assertEqual(sh.current_health(), {})


class TestDirectMode(_Base):
	"""A lone OpenAI subscription served directly has no account_ref: the key is direct:openai."""

	live = DIRECT

	def test_direct_account_is_not_dropped_as_not_in_the_pool(self):
		self.seed({sh.DIRECT_REF: _entry(sh.DIRECT_REF, source="chat", accounts=DIRECT)})
		sh.apply_admin_health({})
		self.assertIn(sh.DIRECT_REF, sh.current_health())

	def test_chat_failure_creates_the_direct_entry(self):
		sh.record_chat_failure("openai")
		entry = sh.current_health()[sh.DIRECT_REF]
		self.assertEqual((entry["label"], entry["source"], entry["state"]), ("OpenAI", "chat", "expired"))
		self.assertEqual(sh.expired_entries()[0]["account_ref"], "direct:openai")

	def test_chat_success_clears_the_direct_entry(self):
		sh.record_chat_failure("openai")
		sh.record_chat_success("openai")
		self.assertEqual(sh.current_health(), {})

	def test_a_pool_style_account_ref_is_dropped_in_direct_mode(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1")})
		sh.apply_admin_health({})
		self.assertEqual(sh.current_health(), {})

	def test_live_accounts_uses_the_same_predicate_as_get_direct_subscription_status(self):
		settings = frappe._dict(
			llm_auth_mode="oauth",
			llm_provider="OpenAI",
			proxy_active=0,
			models=[],
			llm_oauth_account_email="d@x.com",
		)
		for p in self.patches:
			p.stop()
		try:
			with patch.object(sh, "_settings", return_value=settings):
				live = sh._live_accounts()
		finally:
			for p in self.patches:
				p.start()
		self.assertEqual(
			live, {"direct:openai": {"upstream": "openai", "label": "OpenAI", "email": "d@x.com"}}
		)


class TestDirectModePredicate(_Base):
	"""R5: the flat provider must be OpenAI, and the pool wins over a flat config."""

	def _live(self, **overrides):
		fields = dict(
			llm_auth_mode="oauth",
			llm_provider="OpenAI",
			proxy_active=0,
			models=[],
			llm_oauth_account_email="",
		)
		fields.update(overrides)
		for p in self.patches:
			p.stop()
		try:
			with patch.object(sh, "_settings", return_value=frappe._dict(**fields)):
				return sh._live_accounts()
		finally:
			for p in self.patches:
				p.start()

	def test_a_non_openai_flat_provider_is_not_direct(self):
		self.assertEqual(self._live(llm_provider="xAI Grok"), {})

	def test_an_api_key_tenant_is_not_direct(self):
		self.assertEqual(self._live(llm_auth_mode="api_key"), {})

	def test_a_proxy_tenant_is_not_direct(self):
		self.assertEqual(self._live(proxy_active=1), {})

	def test_an_openai_oauth_tenant_with_no_models_is_direct(self):
		self.assertEqual(list(self._live()), [sh.DIRECT_REF])


class TestChatDetectedState(_Base):
	def test_failure_marks_the_only_account_of_the_upstream_expired_from_chat(self):
		sh.record_chat_failure("anthropic")
		health = sh.current_health()
		self.assertEqual(set(health), {"ACC_CL1"})
		self.assertEqual((health["ACC_CL1"]["source"], health["ACC_CL1"]["state"]), ("chat", "expired"))
		self.assertEqual(self.events, [1])

	def test_failure_with_two_accounts_of_the_upstream_creates_no_entry_and_no_signal(self):
		"""R4: the failure cannot say which of two OpenAI accounts died; the poll decides."""
		sh.record_chat_failure("openai")
		self.assertEqual(sh.current_health(), {})
		self.assertEqual((self.writes, self.events), ([], []))
		self.assertEqual(sh.drain_signals(), [])

	def test_failure_keeps_an_existing_poll_entry_of_a_single_account_upstream(self):
		self.seed({"ACC_CL1": _entry("ACC_CL1", source="poll", since=7)})
		sh.record_chat_failure("anthropic")
		entry = sh.current_health()["ACC_CL1"]
		self.assertEqual((entry["source"], entry["since"]), ("poll", 7))

	def test_failure_for_an_upstream_with_no_account_is_a_noop_and_queues_nothing(self):
		sh.record_chat_failure("kimi")
		self.assertEqual(sh.current_health(), {})
		self.assertEqual(sh.drain_signals(), [])

	def test_success_clears_only_chat_entries_of_that_upstream_and_queues_ok(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat"), "ACC_CL1": _entry("ACC_CL1", source="chat")})
		sh.record_chat_success("openai")
		self.assertEqual(list(sh.current_health()), ["ACC_CL1"])
		self.assertEqual([s["code"] for s in sh.drain_signals()], ["ok"])

	def test_success_clears_a_chat_entry_that_arrived_from_admin(self):
		"""R1(a): whether the entry was set locally or arrived from admin."""
		sh.apply_admin_health({"ACC_CL1": {"state": "expired", "source": "chat", "since": 3}})
		sh.record_chat_success("anthropic")
		self.assertEqual(sh.current_health(), {})
		self.assertEqual([s["code"] for s in sh.drain_signals()], ["ok"])

	def test_success_leaves_a_poll_entry_and_records_nothing_without_a_chat_entry(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll")})
		sh.record_chat_success("openai")
		self.assertIn("ACC_OA1", sh.current_health())
		self.assertEqual(sh.drain_signals(), [])

	def test_record_functions_never_raise_and_change_nothing(self):
		self.seed({"ACC_CL1": _entry("ACC_CL1", source="chat")})
		before = self.stored
		for fn in (sh.record_chat_failure, sh.record_chat_success, sh.record_signin_complete):
			with (
				patch.object(sh, "_live_accounts", side_effect=RuntimeError("boom")),
				patch.object(sh, "_log_throttled") as log,
			):
				fn("anthropic")
			log.assert_called_once()
		self.assertEqual((self.stored, self.writes, self.events), (before, [], []))
		self.assertEqual(sh.drain_signals(), [])


class TestSignalQueue(_Base):
	def test_signals_carry_the_i2_shape(self):
		sh.record_chat_failure("anthropic")
		(signal,) = sh.drain_signals()
		self.assertEqual(set(signal), {"upstream", "code", "at"})
		self.assertEqual((signal["upstream"], signal["code"]), ("anthropic", "subscription-expired"))
		self.assertIsInstance(signal["at"], int)

	def test_repeat_failures_do_not_grow_the_queue(self):
		for _ in range(5):
			sh.record_chat_failure("anthropic")
		self.assertEqual(len(sh.drain_signals()), 1)

	def test_a_failure_after_a_success_is_queued_again_in_order(self):
		sh.record_chat_failure("anthropic")
		sh.record_chat_success("anthropic")
		sh.record_chat_failure("anthropic")
		self.assertEqual(
			[s["code"] for s in sh.drain_signals()], ["subscription-expired", "ok", "subscription-expired"]
		)

	def test_drain_pops_at_most_eight(self):
		cache = frappe.cache()
		for i in range(12):
			cache.rpush(sh.SIGNAL_KEY, json.dumps({"upstream": "openai", "code": "ok", "at": i}))
		self.assertEqual(len(sh.drain_signals()), 8)
		self.assertEqual(len(sh.drain_signals()), 4)

	def test_requeue_puts_signals_back_at_the_front_in_order(self):
		sh.record_chat_failure("anthropic")
		sh.record_chat_success("anthropic")
		drained = sh.drain_signals()
		sh.requeue_signals(drained)
		self.assertEqual(sh.drain_signals(), drained)

	def test_junk_queue_items_are_skipped(self):
		cache = frappe.cache()
		cache.rpush(sh.SIGNAL_KEY, "not json")
		cache.rpush(sh.SIGNAL_KEY, json.dumps({"upstream": "nope", "code": "ok", "at": 1}))
		cache.rpush(sh.SIGNAL_KEY, json.dumps({"upstream": "kimi", "code": "ok", "at": 1}))
		self.assertEqual([s["upstream"] for s in sh.drain_signals()], ["kimi"])


class TestPersistentCacheKey(unittest.TestCase):
	"""frappe.clear_cache() wipes every site key except persistent_cache_keys, and pump's watchdog
	triggers one on every tick: the pending signals must survive it. Reads the hook straight from the
	module (the bench may serve another checkout's hooks) and never clears the shared site cache."""

	def test_signal_and_log_keys_sit_under_a_persistent_prefix(self):
		from jarvis import hooks

		prefixes = [k for k in hooks.persistent_cache_keys if "subscription" in k]
		self.assertEqual(prefixes, ["jarvis:subscription_"])
		for key in (sh.SIGNAL_KEY, sh._LOG_KEY):
			self.assertTrue(key.startswith(prefixes[0]), key)


class TestHeartbeatWiring(_Base):
	def _run(self, push):
		from jarvis.chat import heartbeat

		with (
			patch.object(heartbeat, "_admin_configured", return_value=True),
			patch("jarvis.admin_client.push_bench_heartbeat", push),
		):
			heartbeat.push_bench_heartbeat()

	def test_payload_carries_subscription_signals_and_reply_is_applied(self):
		sh.record_chat_failure("anthropic")
		self.seed({**sh.current_health(), "ACC_OA1": _entry("ACC_OA1", source="poll")})
		seen = {}

		def push(vector):
			seen.update(vector)
			return {"subscription_health": {}}

		self._run(push)
		self.assertEqual(seen["subscription_signals"][0]["upstream"], "anthropic")
		# admin replied {}: the reply was applied (poll entry dropped), the chat entry survives
		self.assertEqual(list(sh.current_health()), ["ACC_CL1"])

	def test_no_signals_means_no_key(self):
		seen = {}
		self._run(lambda vector: seen.update(vector) or {})
		self.assertNotIn("subscription_signals", seen)

	def test_signals_are_requeued_when_the_push_fails(self):
		from jarvis.exceptions import AdminUnreachableError

		sh.record_chat_failure("anthropic")

		def push(_vector):
			raise AdminUnreachableError("down")

		self._run(push)
		self.assertEqual([s["code"] for s in sh.drain_signals()], ["subscription-expired"])

	def test_reply_without_the_field_changes_nothing(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll")})
		self._run(lambda _v: {"ok": True})
		self.assertIn("ACC_OA1", sh.current_health())

	def test_a_fault_applying_the_reply_never_escapes_the_heartbeat(self):
		from jarvis.chat import heartbeat

		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll")})
		with (
			patch.object(sh, "apply_admin_health", side_effect=RuntimeError("boom")) as apply,
			patch.object(heartbeat, "_log_failure_throttled") as log,
		):
			self._run(lambda _v: {"subscription_health": {}})
		apply.assert_called_once_with({})
		log.assert_called_once()
		self.assertIn("ACC_OA1", sh.current_health())

	def test_a_fault_draining_signals_still_sends_the_liveness_push(self):
		from jarvis.chat import heartbeat

		seen = {}
		with (
			patch.object(sh, "drain_signals", side_effect=RuntimeError("redis down")),
			patch.object(heartbeat, "_log_failure_throttled") as log,
		):
			self._run(lambda vector: seen.update(vector) or {})
		self.assertIn("watchdog_last_completed_age_s", seen)
		self.assertNotIn("subscription_signals", seen)
		log.assert_called_once()


class TestSigninCompletionClears(_Base):
	"""R1(c): a completed sign-in clears that upstream's entries (any source) and queues ok."""

	def test_record_signin_complete_clears_every_source_of_that_upstream_only(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll"), "ACC_CL1": _entry("ACC_CL1", source="chat")})
		sh.record_signin_complete("openai")
		self.assertEqual(list(sh.current_health()), ["ACC_CL1"])
		self.assertEqual([(s["upstream"], s["code"]) for s in sh.drain_signals()], [("openai", "ok")])
		self.assertEqual(self.events, [1])

	def test_without_an_entry_it_still_queues_ok_and_writes_nothing(self):
		sh.record_signin_complete("openai")
		self.assertEqual(self.writes, [])
		self.assertEqual([(s["upstream"], s["code"]) for s in sh.drain_signals()], [("openai", "ok")])

	def test_an_unknown_upstream_is_ignored(self):
		sh.record_signin_complete("nope")
		self.assertEqual(sh.drain_signals(), [])

	def _api(self):
		from jarvis.oauth import api

		return api

	def _admin(self):
		return patch("jarvis.oauth.api.require_jarvis_admin")

	def test_complete_paste_signin_clears_the_direct_entry(self):
		api = self._api()
		self.live = DIRECT
		self.seed({sh.DIRECT_REF: _entry(sh.DIRECT_REF, source="poll", accounts=DIRECT)})
		result = {"provider": "OpenAI", "model": "gpt", "email": "d@x.com", "blob": {"id_token": "t"}}
		with (
			self._admin(),
			patch.object(api, "_validate_signin_nonce", return_value=({}, None)),
			patch.object(api, "_exchange_and_build_blob", return_value=(result, None)),
			patch.object(api.admin_client, "post_push_oauth_blob"),
			patch.object(api.onboarding, "save_llm_creds", return_value={}),
			patch.object(api.frappe, "get_single", return_value=frappe._dict(db_set=lambda *a, **k: None)),
			patch.object(api.frappe.cache, "hdel", create=True),
		):
			out = api.complete_paste_signin("n", "http://x")
		self.assertTrue(out.get("ok"), out)
		self.assertEqual(sh.current_health(), {})
		self.assertEqual([(s["upstream"], s["code"]) for s in sh.drain_signals()], [("openai", "ok")])

	def test_complete_pool_account_signin_clears_that_upstream(self):
		api = self._api()
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll"), "ACC_CL1": _entry("ACC_CL1", source="chat")})
		result = {"provider": "OpenAI", "model": "gpt", "email": "n@x.com", "blob": {"accountId": "a"}}
		with (
			self._admin(),
			patch.object(api, "_validate_signin_nonce", return_value=({"pool": True}, None)),
			patch.object(api, "_exchange_and_build_blob", return_value=(result, None)),
			patch.object(api.pending_capture, "create_capture", return_value={"capture_id": "c"}),
			patch.object(api.frappe.cache, "hdel", create=True),
		):
			out = api.complete_pool_account_signin("n", "http://x")
		self.assertTrue(out.get("ok"), out)
		self.assertEqual(list(sh.current_health()), ["ACC_CL1"])
		self.assertEqual([(s["upstream"], s["code"]) for s in sh.drain_signals()], [("openai", "ok")])

	def test_complete_claude_cli_login_clears_the_anthropic_entry(self):
		api = self._api()
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll"), "ACC_CL1": _entry("ACC_CL1", source="poll")})
		with (
			self._admin(),
			patch.object(
				api.admin_client, "post_claude_login_submit", return_value={"account_email": "c@x.com"}
			),
			patch.object(api.pending_capture, "create_capture", return_value={"capture_id": "c"}),
		):
			out = api.complete_claude_cli_login("login1", "abc-123_code")
		self.assertTrue(out.get("ok"), out)
		self.assertEqual(list(sh.current_health()), ["ACC_OA1"])
		self.assertEqual([(s["upstream"], s["code"]) for s in sh.drain_signals()], [("anthropic", "ok")])

	def test_a_failed_exchange_clears_nothing(self):
		api = self._api()
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll")})
		with (
			self._admin(),
			patch.object(api, "_validate_signin_nonce", return_value=({"pool": True}, None)),
			patch.object(api, "_exchange_and_build_blob", return_value=(None, {"ok": False})),
		):
			api.complete_pool_account_signin("n", "http://x")
		self.assertIn("ACC_OA1", sh.current_health())
		self.assertEqual(sh.drain_signals(), [])
