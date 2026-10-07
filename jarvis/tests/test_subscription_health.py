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
		self._clear_epochs()

	def _clear_epochs(self):
		frappe.cache().delete_value(sh.SIGNAL_KEY)
		for upstream in ("openai", "anthropic"):
			frappe.cache().delete_value(sh.CLEARED_KEY_PREFIX + upstream)

	def tearDown(self):
		for p in self.patches:
			p.stop()
		self._clear_epochs()

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


class TestClearEpoch(_Base):
	"""Admin builds its reply from its stored map before it sees our ok signal, so the next reply
	can still list an entry we just cleared. That stale chat entry must not come back."""

	def _stale_reply(self, since=100):
		return {"ACC_OA1": {**_entry("ACC_OA1", source="chat", since=since)}}

	def test_stale_admin_chat_entry_after_a_local_clear_stays_cleared(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat", since=100)})
		sh.record_chat_success("openai")
		self.assertEqual(sh.current_health(), {})
		sh.apply_admin_health(self._stale_reply())
		self.assertEqual(sh.current_health(), {})
		sh.apply_admin_health({})
		self.assertEqual(sh.current_health(), {})

	def test_stale_admin_chat_entry_after_a_signin_complete_stays_cleared(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat", since=100)})
		sh.record_signin_complete("openai", "ACC_OA1", all_sources=True)
		sh.apply_admin_health(self._stale_reply())
		self.assertEqual(sh.current_health(), {})

	def test_a_new_failure_after_the_clear_is_accepted(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat", since=100)})
		sh.record_chat_success("openai")
		later = int(frappe.cache().get_value(sh.CLEARED_KEY_PREFIX + "openai")) + 50
		sh.apply_admin_health(self._stale_reply(since=later))
		self.assertEqual(sh.current_health()["ACC_OA1"]["since"], later)

	def test_a_poll_entry_is_not_filtered_by_the_clear_epoch(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat", since=100)})
		sh.record_chat_success("openai")
		reply = {"ACC_OA1": _entry("ACC_OA1", source="poll", since=100)}
		sh.apply_admin_health(reply)
		self.assertEqual(sh.current_health()["ACC_OA1"]["source"], "poll")

	def test_a_leftover_chat_entry_older_than_the_clear_is_dropped_when_admin_omits_it(self):
		frappe.cache().set_value(sh.CLEARED_KEY_PREFIX + "openai", 1000)
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat", since=100)})
		sh.apply_admin_health({})
		self.assertEqual(sh.current_health(), {})

	def test_a_chat_entry_newer_than_the_clear_survives_admin_omitting_it(self):
		frappe.cache().set_value(sh.CLEARED_KEY_PREFIX + "openai", 1000)
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat", since=2000)})
		sh.apply_admin_health({})
		self.assertIn("ACC_OA1", sh.current_health())


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


class TestPublishChanged(unittest.TestCase):
	"""The change ping goes to the site room, never to an unscoped audience (the Marketplace audit's
	semgrep rule fails a publish_realtime with no doctype, docname, room or user)."""

	def test_publishes_to_the_site_room_after_commit(self):
		from frappe.realtime import get_site_room

		with patch("frappe.publish_realtime") as publish:
			sh._publish_changed()
		publish.assert_called_once_with(sh.EVENT, {"changed": True}, room=get_site_room(), after_commit=True)


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

	def test_all_sources_clears_every_entry_of_that_upstream_only(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll"), "ACC_CL1": _entry("ACC_CL1", source="chat")})
		sh.record_signin_complete("openai", all_sources=True)
		self.assertEqual(list(sh.current_health()), ["ACC_CL1"])
		self.assertEqual([(s["upstream"], s["code"]) for s in sh.drain_signals()], [("openai", "ok")])
		self.assertEqual(self.events, [1])

	def test_two_accounts_one_upstream_reconnecting_b_keeps_a_expired_and_queues_nothing(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll")})
		sh.record_signin_complete("openai", account_ref=None)
		self.assertEqual(list(sh.current_health()), ["ACC_OA1"])
		self.assertEqual(self.writes, [])
		self.assertEqual(sh.drain_signals(), [])

	def test_reconnecting_the_expired_account_clears_it_and_queues_ok(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll")})
		sh.record_signin_complete("openai", account_ref="ACC_OA1")
		self.assertEqual(sh.current_health(), {})
		self.assertEqual([s["code"] for s in sh.drain_signals()], ["ok"])

	def test_replacing_b_clears_b_but_not_a_and_queues_no_ok(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll"), "ACC_OA2": _entry("ACC_OA2", source="poll")})
		sh.record_signin_complete("openai", account_ref="ACC_OA2")
		self.assertEqual(list(sh.current_health()), ["ACC_OA1"])
		self.assertEqual(sh.drain_signals(), [])

	def test_without_an_entry_it_still_queues_ok_and_writes_nothing(self):
		sh.record_signin_complete("openai", all_sources=True)
		self.assertEqual(self.writes, [])
		self.assertEqual([(s["upstream"], s["code"]) for s in sh.drain_signals()], [("openai", "ok")])

	def test_an_unknown_upstream_is_ignored(self):
		sh.record_signin_complete("nope")
		self.assertEqual(sh.drain_signals(), [])

	def test_one_live_accounts_read_per_signin_completion(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll"), "ACC_CL1": _entry("ACC_CL1", source="chat")})
		with patch.object(sh, "_live_accounts", side_effect=lambda: dict(self.live)) as live:
			sh.record_signin_complete("openai", account_ref=None)
		self.assertEqual(live.call_count, 1)

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

	def test_complete_pool_account_signin_keeps_another_accounts_poll_entry(self):
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
		self.assertEqual(sorted(sh.current_health()), ["ACC_CL1", "ACC_OA1"])
		self.assertEqual(sh.drain_signals(), [])

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


PROD = (
	"Still verifying your openai subscription: internal_server_error: auth_unavailable: no auth available "
	'(providers=codex, model=gpt-5.6-terra; last upstream error: unauthorized: token: [REDACTED] "error": '
	'{ "message": "Your refresh token: [REDACTED] "type": "invalid_request_error", "param": null, '
	'"code": "refresh_token_reused" } })'
)


class TestFailureHook(unittest.TestCase):
	def test_the_classifier_itself_records_nothing(self):
		"""The pump's pre-ack paths classify too: a classifier must never write health."""
		from jarvis.chat import turn_handler

		with patch.object(sh, "record_chat_failure") as rec, patch.object(sh, "note_turn_error") as note:
			self.assertEqual(turn_handler._classify_error(PROD), "subscription-expired")
		rec.assert_not_called()
		note.assert_not_called()

	def test_the_turn_handler_publish_helper_records_a_dead_sign_in(self):
		from jarvis.chat import turn_handler

		with patch.object(sh, "record_chat_failure") as rec:
			turn_handler._note_subscription_error(PROD, "subscription-expired")
		rec.assert_called_once_with("openai")

	def test_the_all_models_failed_form_names_the_failing_upstream(self):
		text = "All models failed (2): xai/grok: 500 auth_unavailable: unauthorized | openai/gpt-5.6: timeout"
		with patch.object(sh, "record_chat_failure") as rec:
			sh.note_turn_error(text, "subscription-expired")
		rec.assert_called_once_with("xai")

	def test_other_codes_record_nothing(self):
		from jarvis.chat import turn_handler

		with patch.object(sh, "record_chat_failure") as rec:
			turn_handler._note_subscription_error(PROD, "gateway")
			sh.note_turn_error("OpenAI API error: 401 Unauthorized. Incorrect API key", "auth")
		rec.assert_not_called()

	def test_an_upstream_less_strong_signal_records_nothing(self):
		with patch.object(sh, "record_chat_failure") as rec:
			sh.note_turn_error("auth_unavailable: unauthorized", "subscription-expired")
		rec.assert_not_called()

	def test_a_fault_in_the_hook_is_logged_once_and_never_raised(self):
		with (
			patch.object(sh, "record_chat_failure", side_effect=RuntimeError("boom")),
			patch.object(sh, "_log_throttled") as log,
		):
			sh.note_turn_error(PROD, "subscription-expired")
		log.assert_called_once_with()

	def test_settlement_of_a_dead_sign_in_records_it_before_the_settling_commit(self):
		from jarvis.chat import settlement

		order = []
		row = {"state": "terminal_observed", "version": 1, "assistant_message": "m", "last_event_seq": 3}
		with (
			patch.object(settlement.ts, "read_turn", return_value=row),
			patch.object(settlement.ts, "_run_cas"),
			patch.object(settlement.ts, "settle_errored", return_value=True),
			patch.object(settlement.ts, "insert_required_effects"),
			patch.object(settlement, "_stamp_reply_duration"),
			patch.object(settlement, "_stamp_steps"),
			patch.object(settlement.frappe.db, "commit", side_effect=lambda: order.append("commit")),
			patch.object(settlement.ts, "publish_fenced"),
			patch.object(settlement, "_bump_turn_count"),
			patch.object(settlement, "_extra_with_pending", side_effect=lambda extra, *a: extra),
			patch.object(sh, "record_chat_failure", side_effect=lambda up: order.append(f"fail:{up}")),
			patch("jarvis.chat.llm_switch.apply_if_active"),
		):
			deps = type("D", (), {"enqueue_finalize": staticmethod(lambda *a: None)})()
			settlement.invoke_settlement(
				"r1",
				relay_target_id="t",
				epoch=1,
				version=1,
				terminal_kind="relay:error",
				terminal_payload={"error": PROD},
				assistant_message="m",
				owner="u@x.com",
				conversation="c",
				deps=deps,
			)
		self.assertEqual(order, ["fail:openai", "commit"])


def _row(model, sub=True, order=1, upstream="openai", provider=""):
	account = {"account_ref": f"ACC_{model}", "upstream": upstream}
	return frappe._dict(
		enabled=1,
		order=order,
		model=model,
		provider=provider,
		credential_type="subscription" if sub else "api_key",
		subscription_accounts=json.dumps([account]) if sub else "",
	)


class TestServingUpstream(_Base):
	def _settings_with(self, *rows, **flat):
		return patch.object(sh, "_settings", return_value=frappe._dict(models=list(rows), **flat))

	def test_pooled_subscription_is_matched_by_model_id_not_by_the_proxy_provider(self):
		with self._settings_with(_row("gpt-5.6"), _row("claude-sonnet-4-5", upstream="anthropic", order=2)):
			self.assertEqual(sh.upstream_for_serving("gpt-5.6", "openai_compat"), "openai")
			self.assertEqual(sh.upstream_for_serving("claude-sonnet-4-5", "anthropic_cli"), "anthropic")

	def test_a_prefixed_model_id_is_stripped(self):
		with self._settings_with(_row("gpt-5.6")):
			self.assertEqual(sh.upstream_for_serving("jarvis-pool/gpt-5.6", "openai_compat"), "openai")

	def test_an_api_key_model_never_maps(self):
		with self._settings_with(_row("gpt-4.1", sub=False)):
			self.assertEqual(sh.upstream_for_serving("gpt-4.1", "openai"), "")

	def test_an_unknown_model_never_maps(self):
		with self._settings_with(_row("gpt-5.6")):
			self.assertEqual(sh.upstream_for_serving("mystery", "openai_compat"), "")
			self.assertEqual(sh.upstream_for_serving("", "openai"), "")

	def test_a_model_id_shared_by_two_upstreams_needs_the_provider_to_disambiguate(self):
		rows = (_row("shared-1", upstream="openai"), _row("shared-1", upstream="xai", order=2))
		with self._settings_with(*rows):
			self.assertEqual(sh.upstream_for_serving("shared-1", "openai_compat"), "")
			self.assertEqual(sh.upstream_for_serving("shared-1", "xai"), "xai")

	def test_direct_mode_maps_the_codex_provider(self):
		self.live = DIRECT
		flat = dict(llm_auth_mode="oauth", llm_provider="OpenAI", proxy_active=0)
		with self._settings_with(**flat):
			self.assertEqual(sh.upstream_for_serving("gpt-5.6", "openai-codex"), "openai")
			self.assertEqual(sh.upstream_for_serving("gpt-5.6", "openai"), "openai")
			self.assertEqual(sh.upstream_for_serving("gpt-5.6", "openai_compat"), "")


class TestSuccessHook(_Base):
	def test_a_turn_reads_each_enabled_rows_accounts_at_most_once(self):
		from jarvis.jarvis import pool_serialize

		real = pool_serialize._model_accounts
		rows = [
			_row("gpt-5.6", order=1),
			_row("deepseek-flash", sub=False, order=2),
			_row("claude-x", upstream="anthropic", order=3),
		]
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat")})
		with (
			patch.object(sh, "_settings", return_value=frappe._dict(models=rows)),
			patch("jarvis.jarvis.pool_serialize._model_accounts", side_effect=real) as read,
			patch.object(sh, "_enabled_rows", side_effect=sh._enabled_rows) as walk,
		):
			sh.note_turn_success("gpt-5.6", "openai_compat")
		walk.assert_called_once_with()  # the serving lookup shares the one row pass
		self.assertLessEqual(read.call_count, 2)  # the 2 subscription rows, once each
		self.assertEqual(sh.current_health(), {})

	def test_success_clears_the_chat_entry_of_the_serving_upstream(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat")})
		with patch.object(sh, "upstream_for_serving", return_value="openai"):
			sh.note_turn_success("gpt-5.6", "openai_compat")
		self.assertEqual(sh.current_health(), {})
		self.assertEqual([s["code"] for s in sh.drain_signals()], ["ok"])

	def test_success_on_another_upstream_changes_nothing(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat")})
		with patch.object(sh, "upstream_for_serving", return_value="anthropic"):
			sh.note_turn_success("claude", "anthropic_cli")
		self.assertIn("ACC_OA1", sh.current_health())
		self.assertEqual(sh.drain_signals(), [])

	def test_no_chat_entry_means_no_mapping_work_at_all(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="poll")})
		with patch.object(sh, "upstream_for_serving") as up:
			sh.note_turn_success("gpt-5.6", "openai_compat")
		up.assert_not_called()

	def test_an_unmappable_turn_changes_nothing(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat")})
		with patch.object(sh, "upstream_for_serving", return_value=""):
			sh.note_turn_success("mystery", "openai_compat")
		self.assertIn("ACC_OA1", sh.current_health())

	def test_direct_mode_success_clears_direct_openai(self):
		self.live = DIRECT
		self.seed({sh.DIRECT_REF: _entry(sh.DIRECT_REF, source="chat", accounts=DIRECT)})
		with patch.object(sh, "upstream_for_serving", return_value="openai"):
			sh.note_turn_success("gpt-5.6", "openai-codex")
		self.assertEqual(sh.current_health(), {})

	def test_session_row_helper_reads_the_gateway_identity(self):
		with patch.object(sh, "note_turn_success") as note:
			sh.note_session_row({"model": "gpt-5.6", "modelProvider": "openai_compat"})
			sh.note_session_row(None)
		note.assert_called_once_with("gpt-5.6", "openai_compat")

	def test_the_hooks_never_raise_and_log_once(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", source="chat")})
		with (
			patch.object(sh, "current_health", side_effect=RuntimeError("boom")),
			patch.object(sh, "_log_throttled") as log,
		):
			sh.note_turn_success("gpt-5.6", "openai_compat")
		log.assert_called_once_with()
		self.assertIn("ACC_OA1", json.loads(self.stored))


class TestFallbackLabel(unittest.TestCase):
	def _with(self, *rows):
		return patch.object(sh, "_settings", return_value=frappe._dict(models=list(rows)))

	def test_first_non_expired_row_in_pool_order_answers(self):
		rows = (_row("gpt-5.6", order=1), _row("claude-sonnet-4-5", upstream="anthropic", order=2))
		with self._with(*rows):
			self.assertEqual(sh.fallback_label({"ACC_gpt-5.6"}), "Anthropic")

	def test_an_api_key_row_is_labelled_by_its_model_id(self):
		rows = (_row("gpt-5.6", order=1), _row("gemini-2.5-pro", sub=False, order=2))
		with self._with(*rows):
			self.assertEqual(sh.fallback_label({"ACC_gpt-5.6"}), "gemini-2.5-pro")

	def test_a_row_with_one_live_account_still_answers(self):
		row = _row("gpt-5.6")
		row.subscription_accounts = json.dumps(
			[{"account_ref": "A1", "upstream": "openai"}, {"account_ref": "A2", "upstream": "openai"}]
		)
		with self._with(row):
			self.assertEqual(sh.fallback_label({"A1"}), "OpenAI")
			self.assertEqual(sh.fallback_label({"A1", "A2"}), "")

	def test_nothing_left_means_chats_fail(self):
		with self._with(_row("gpt-5.6")):
			self.assertEqual(sh.fallback_label({"ACC_gpt-5.6"}), "")

	def test_disabled_rows_and_accountless_subscription_rows_never_answer(self):
		off = _row("claude-sonnet-4-5", upstream="anthropic", order=2)
		off.enabled = 0
		empty = _row("kimi-k2", upstream="kimi", order=3)
		empty.subscription_accounts = ""
		with self._with(_row("gpt-5.6"), off, empty):
			self.assertEqual(sh.fallback_label({"ACC_gpt-5.6"}), "")


class TestSkippedEntries(_Base):
	"""``skipped``: the entry's own row is AFTER the answering row, so Auto never used it."""

	ACCOUNTS = {
		"ACC_gpt": {"upstream": "openai", "label": "OpenAI", "email": ""},
		"ACC_claude": {"upstream": "anthropic", "label": "Anthropic", "email": ""},
	}
	live = ACCOUNTS

	def _skipped(self, rows, *expired):
		self.seed({ref: _entry(ref, accounts=self.ACCOUNTS) for ref in expired})
		with patch.object(sh, "_settings", return_value=frappe._dict(models=list(rows))):
			return {e["account_ref"]: (e["skipped"], e["fallback"]) for e in sh.ui_entries()}

	def _owner_pool(self):
		return (
			_row("gpt", order=1),
			_row("deepseek-flash", sub=False, order=2),
			_row("claude", upstream="anthropic", order=3),
		)

	def test_owners_pool_skips_the_expired_row_after_the_answering_one(self):
		out = self._skipped(self._owner_pool(), "ACC_claude")
		self.assertEqual(out, {"ACC_claude": (True, "OpenAI")})

	def test_the_first_row_expired_is_not_skipped(self):
		out = self._skipped(self._owner_pool(), "ACC_gpt")
		self.assertEqual(out, {"ACC_gpt": (False, "deepseek-flash")})

	def test_two_expired_entries_compare_against_the_one_shared_fallback(self):
		out = self._skipped(self._owner_pool(), "ACC_gpt", "ACC_claude")
		self.assertEqual(out, {"ACC_gpt": (False, "deepseek-flash"), "ACC_claude": (True, "deepseek-flash")})

	def test_no_fallback_is_never_skipped(self):
		rows = (_row("gpt", order=1), _row("claude", upstream="anthropic", order=2))
		out = self._skipped(rows, "ACC_gpt", "ACC_claude")
		self.assertEqual(out, {"ACC_gpt": (False, ""), "ACC_claude": (False, "")})

	def test_direct_mode_is_never_skipped(self):
		# A fallback exists (the api-key row answers) and a row after it lists the direct ref, so only the
		# DIRECT_REF guard keeps the entry from reading as skipped.
		self.live = DIRECT
		self.seed({sh.DIRECT_REF: _entry(sh.DIRECT_REF, accounts=DIRECT)})
		after = _row("gpt-direct", order=2)
		after.subscription_accounts = json.dumps([{"account_ref": sh.DIRECT_REF, "upstream": "openai"}])
		rows = [_row("deepseek-flash", sub=False, order=1), after]
		with patch.object(sh, "_settings", return_value=frappe._dict(models=rows)):
			entry = sh.ui_entries()[0]
		self.assertEqual(entry["fallback"], "deepseek-flash")
		self.assertFalse(entry["skipped"])

	def test_a_member_entry_does_not_carry_skipped(self):
		self.assertNotIn(
			"skipped",
			sh._member_entry({"upstream": "openai", "label": "OpenAI", "models": [], "skipped": True}),
		)


class TestAttentionAndNotice(_Base):
	def test_ui_entries_carry_the_fallback(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", since=100)})
		with patch.object(sh, "fallback_label", return_value="Anthropic") as fb:
			entries = sh.ui_entries()
		self.assertEqual(fb.call_count, 1)
		self.assertEqual(fb.call_args.args[0], {"ACC_OA1"})
		self.assertEqual(entries[0]["fallback"], "Anthropic")
		self.assertEqual(entries[0]["account_ref"], "ACC_OA1")

	def test_same_upstream_fallback_reads_another_account(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", since=100)})
		with patch.object(sh, "fallback_label", return_value="OpenAI"):
			entries = sh.ui_entries()
		self.assertEqual(entries[0]["fallback"], "Another OpenAI account")

	def test_other_upstream_fallback_is_untouched(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1", since=100), "ACC_CL1": _entry("ACC_CL1", since=200)})
		with patch.object(sh, "fallback_label", return_value="Anthropic"):
			by_ref = {e["account_ref"]: e["fallback"] for e in sh.ui_entries()}
		self.assertEqual(by_ref["ACC_OA1"], "Anthropic")
		self.assertEqual(by_ref["ACC_CL1"], "Another Anthropic account")

	def test_ui_entries_empty_when_nothing_expired(self):
		self.assertEqual(sh.ui_entries(), [])

	def _notice(self, admin):
		frappe.set_user("Administrator")
		with (
			patch("jarvis.permissions.require_jarvis_access"),
			patch("jarvis.permissions.has_jarvis_admin_access", return_value=admin),
		):
			return sh.get_subscription_notice()

	def test_admin_gets_every_entry_and_the_workspace_upstreams(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1"), "ACC_CL1": _entry("ACC_CL1", since=200)})
		with patch.object(sh, "fallback_label", return_value=""):
			out = self._notice(admin=True)
		self.assertEqual([e["account_ref"] for e in out["expired"]], ["ACC_OA1", "ACC_CL1"])
		self.assertEqual(out["upstreams"], ["anthropic", "openai"])

	def test_nothing_expired_reads_no_row_accounts(self):
		rows = [_row("gpt-5.6", order=1), _row("claude-x", upstream="anthropic", order=2)]
		with (
			patch.object(sh, "_settings", return_value=frappe._dict(models=rows)),
			patch("jarvis.jarvis.pool_serialize._model_accounts") as read,
		):
			admin = self._notice(admin=True)
			member = self._notice(admin=False)
		read.assert_not_called()
		self.assertEqual(admin, {"expired": [], "upstreams": ["anthropic", "openai"]})
		self.assertEqual(member, {"expired": [], "expired_models": [], "upstreams": None})

	def test_an_expired_entry_reads_each_enabled_subscription_row_once(self):
		from jarvis.jarvis import pool_serialize

		real = pool_serialize._model_accounts
		rows = [
			_row("gpt-5.6", order=1),
			_row("deepseek-flash", sub=False, order=2),
			_row("claude-x", upstream="anthropic", order=3),
			_row("kimi-k2", upstream="kimi", order=4),
		]
		off = _row("gpt-off", order=5)
		off.enabled = 0
		self.live = {**POOL, "ACC_gpt-5.6": POOL["ACC_OA1"]}
		self.seed({"ACC_gpt-5.6": _entry("ACC_gpt-5.6", accounts=self.live)})
		with (
			patch.object(sh, "_settings", return_value=frappe._dict(models=[*rows, off])),
			patch("jarvis.jarvis.pool_serialize._model_accounts", side_effect=real) as read,
		):
			out = self._notice(admin=True)
		self.assertEqual(out["expired"][0]["fallback"], "deepseek-flash")
		self.assertEqual(
			read.call_count, 3
		)  # the 3 enabled subscription rows; not the api-key or disabled one

	def test_member_sees_only_a_failing_entry_with_no_fallback_and_nothing_else(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1")})
		with patch.object(sh, "fallback_label", return_value=""):
			out = self._notice(admin=False)
		self.assertEqual(
			out,
			{
				"expired": [{"upstream": "openai", "label": "OpenAI", "models": []}],
				"expired_models": [{"upstream": "openai", "label": "OpenAI", "models": []}],
				"upstreams": None,
			},
		)

	def test_entries_carry_the_models_their_sign_in_serves(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1")})
		served = {"ACC_OA1": ["gpt-5.6-terra", "gpt-5.5"], "ACC_CL1": ["claude-x"]}
		with (
			patch.object(sh, "fallback_label", return_value=""),
			patch.object(sh, "_account_models", return_value=served),
		):
			admin = self._notice(admin=True)
			member = self._notice(admin=False)
		self.assertEqual(admin["expired"][0]["models"], ["gpt-5.6-terra", "gpt-5.5"])
		self.assertEqual(
			member["expired"],
			[{"upstream": "openai", "label": "OpenAI", "models": ["gpt-5.6-terra", "gpt-5.5"]}],
		)

	def test_direct_entry_carries_the_direct_model(self):
		self.live = DIRECT
		self.seed({sh.DIRECT_REF: _entry(sh.DIRECT_REF, source="chat", accounts=DIRECT)})
		with (
			patch.object(sh, "fallback_label", return_value=""),
			patch.object(sh, "_account_models", return_value={sh.DIRECT_REF: ["gpt-5.6"]}),
		):
			out = self._notice(admin=True)
		self.assertEqual(out["expired"][0]["models"], ["gpt-5.6"])

	def _pool(self, rows, llm_model="gpt-direct"):
		def row(model, refs, credential_type="subscription", enabled=1):
			return frappe._dict(
				model=model,
				enabled=enabled,
				credential_type=credential_type,
				accounts=[frappe._dict(account_ref=r) for r in refs],
			)

		return frappe._dict(models=[row(*r[:2], **r[2]) for r in rows], llm_model=llm_model)

	def _served(self, pool, expired):
		with (
			patch.object(sh, "_settings", return_value=pool),
			patch("jarvis.jarvis.pool_serialize._get_password", return_value=""),
		):
			return sh._account_models(expired)

	def test_account_models_lists_a_model_only_the_expired_account_serves(self):
		pool = self._pool([("gpt-a", ["ACC_OA1"], {}), ("gpt-b", ["ACC_OA1"], {})])
		self.assertEqual(self._served(pool, {"ACC_OA1"}), {"ACC_OA1": ["gpt-a", "gpt-b"]})

	def test_account_models_excludes_a_model_shared_with_a_healthy_account(self):
		pool = self._pool([("gpt-a", ["ACC_OA1", "ACC_OA2"], {}), ("gpt-b", ["ACC_OA1"], {})])
		self.assertEqual(self._served(pool, {"ACC_OA1"}), {"ACC_OA1": ["gpt-b"]})

	def test_account_models_excludes_a_model_shared_with_an_api_key_row(self):
		pool = self._pool([("gpt-a", ["ACC_OA1"], {}), ("openai/gpt-a", [], {"credential_type": "api_key"})])
		self.assertEqual(self._served(pool, {"ACC_OA1"}), {})

	def test_account_models_ignores_disabled_rows(self):
		pool = self._pool([("gpt-a", ["ACC_OA1"], {}), ("gpt-a", ["ACC_OA2"], {"enabled": 0})])
		self.assertEqual(self._served(pool, {"ACC_OA1"}), {"ACC_OA1": ["gpt-a"]})

	def test_account_models_direct_mode_reads_the_flat_model(self):
		self.live = DIRECT
		self.assertEqual(self._served(self._pool([]), {sh.DIRECT_REF}), {sh.DIRECT_REF: ["gpt-direct"]})

	def test_member_gets_every_expired_entrys_models_for_the_card_and_nothing_else(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1"), "ACC_CL1": _entry("ACC_CL1", since=200)})
		served = {"ACC_OA1": ["gpt-a"], "ACC_CL1": ["claude-x"]}
		with (
			patch.object(sh, "fallback_label", return_value="Anthropic"),
			patch.object(sh, "_account_models", return_value=served),
		):
			member = self._notice(admin=False)
			admin = self._notice(admin=True)
		# The banner rule is unchanged: only the first entry with no fallback.
		self.assertEqual(member["expired"], [])
		self.assertEqual(
			member["expired_models"],
			[
				{"upstream": "openai", "label": "OpenAI", "models": ["gpt-a"]},
				{"upstream": "anthropic", "label": "Anthropic", "models": ["claude-x"]},
			],
		)
		self.assertNotIn("expired_models", admin)

	def test_member_sees_nothing_while_another_model_still_answers(self):
		self.seed({"ACC_OA1": _entry("ACC_OA1")})
		with patch.object(sh, "fallback_label", return_value="Anthropic"):
			out = self._notice(admin=False)
		self.assertEqual(out["expired"], [])
		self.assertEqual(out["upstreams"], None)


class TestLlmHealthPrecedence(unittest.TestCase):
	def _health(self, **settings):
		from jarvis import account

		base = {"last_sync_status": "ok (restart via admin)", "last_subscription_status": ""}
		with (
			patch.object(account, "_llm_apply_confirmed", return_value=True),
			patch.object(account, "_last_turn_errored", return_value=settings.pop("turn_error", False)),
			patch.object(account, "_last_turn_succeeded", return_value=False),
		):
			return account._llm_health(frappe._dict({**base, **settings}), True)

	def _expired(self, value=True):
		return patch.object(sh, "expired_entries", return_value=[{"account_ref": "A"}] if value else [])

	def test_expired_outranks_turn_error_and_subscription_unverified(self):
		with self._expired():
			self.assertEqual(
				self._health(turn_error=True, last_subscription_status="unverified"),
				("attention", "subscription_expired"),
			)

	def test_sync_failed_and_applying_still_win(self):
		with self._expired():
			self.assertEqual(self._health(last_sync_status="failed: boom"), ("attention", "sync_failed"))
			self.assertEqual(self._health(last_sync_status="pending: applying"), ("applying", ""))

	def test_without_an_expired_entry_the_old_causes_are_unchanged(self):
		with self._expired(False):
			self.assertEqual(self._health(turn_error=True), ("attention", "turn_error"))
			self.assertEqual(
				self._health(last_subscription_status="unverified"), ("attention", "subscription_unverified")
			)
			self.assertEqual(self._health(), ("ok", ""))

	def test_a_member_gets_the_same_payload_as_for_every_other_cause(self):
		from jarvis import account

		frappe.set_user("Administrator")
		with (
			patch.object(account, "_has_llm_config", return_value=True),
			patch.object(account, "compute_pool_mode", return_value=True),
			patch.object(account, "_llm_health", return_value=("attention", "subscription_expired")),
		):
			self.assertEqual(account.get_llm_connection_health(), {"state": "attention"})

	def test_connection_status_carries_detail_and_the_entries(self):
		from jarvis import account, admin_client

		entry = {
			"account_ref": "ACC_OA1",
			"upstream": "openai",
			"label": "OpenAI",
			"email": "a@x.com",
			"state": "expired",
			"source": "poll",
			"since": 123,
			"fallback": "Anthropic",
		}
		frappe.set_user("Administrator")
		with (
			patch.object(account, "_has_llm_config", return_value=True),
			patch.object(account, "compute_pool_mode", return_value=True),
			patch.object(account, "_llm_health", return_value=("attention", "subscription_expired")),
			patch.object(sh, "ui_entries", return_value=[entry]),
			patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}),
		):
			out = account.get_llm_connection_status()
		self.assertEqual(out["attention_reason"], "subscription_expired")
		self.assertEqual(
			out["attention_detail"],
			{"upstream": "openai", "label": "OpenAI", "since": 123, "account_ref": "ACC_OA1"},
		)
		self.assertEqual(out["subscription_health"], [entry])

	def test_other_causes_have_an_empty_detail(self):
		from jarvis import account, admin_client

		frappe.set_user("Administrator")
		with (
			patch.object(account, "_has_llm_config", return_value=True),
			patch.object(account, "compute_pool_mode", return_value=True),
			patch.object(account, "_llm_health", return_value=("attention", "turn_error")),
			patch.object(sh, "ui_entries", return_value=[]),
			patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}),
		):
			out = account.get_llm_connection_status()
		self.assertEqual((out["attention_detail"], out["subscription_health"]), ({}, []))


class TestNoteRecordedTurn(_Base):
	"""Carried from S3 review: the legacy relay:final hook acts only on a recorded / valid-zero outcome."""

	ROW = {"model": "gpt-5.6", "modelProvider": "openai_compat"}

	def test_a_retry_outcome_never_clears_anything(self):
		from jarvis.chat import usage

		with patch.object(sh, "note_session_row") as note:
			sh.note_recorded_turn(usage.USAGE_RETRY, self.ROW)
		note.assert_not_called()

	def test_recorded_and_valid_zero_outcomes_clear_by_the_row(self):
		from jarvis.chat import usage

		for outcome in (usage.USAGE_RECORDED, usage.USAGE_VALID_ZERO):
			with patch.object(sh, "note_session_row") as note:
				sh.note_recorded_turn(outcome, self.ROW)
			note.assert_called_once_with(self.ROW)

	def test_the_legacy_final_hook_goes_through_the_outcome_gate(self):
		import inspect

		from jarvis.chat import turn_handler

		src = inspect.getsource(turn_handler)
		self.assertIn("note_recorded_turn(", src)
		self.assertNotIn("note_session_row(", src)


class TestUnmigratedSite(unittest.TestCase):
	"""Code deployed before ``bench migrate``: the field is missing and nothing may raise."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.patch = patch.object(sh, "_has_field", return_value=False)
		self.patch.start()

	def tearDown(self):
		self.patch.stop()

	def test_read_is_empty_and_write_is_a_no_op(self):
		with patch("frappe.db.set_single_value") as setter, patch("frappe.db.get_single_value") as getter:
			self.assertEqual(sh._read_stored(), "")
			sh._write_stored("{}")
		setter.assert_not_called()
		getter.assert_not_called()
		self.assertEqual(sh.current_health(), {})

	def test_status_health_and_notice_return_the_no_health_shape(self):
		from jarvis import account, admin_client

		# An in-memory copy (never saved) with the two stored statuses blanked: whatever an earlier test
		# or the site left in last_sync_status / last_subscription_status would otherwise turn the
		# health to "attention" for reasons this test is not about (CI's shared DB did exactly that).
		settings = frappe.get_single("Jarvis Settings")
		settings.last_sync_status = ""
		settings.last_subscription_status = ""
		with (
			patch("frappe.get_single", return_value=settings),
			patch.object(account, "_has_llm_config", return_value=True),
			patch.object(account, "compute_pool_mode", return_value=True),
			patch.object(account, "_llm_apply_confirmed", return_value=True),
			patch.object(account, "_last_turn_errored", return_value=False),
			patch.object(account, "_last_turn_succeeded", return_value=False),
			patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}),
			patch.object(sh, "_live_accounts", return_value={}),
			patch("jarvis.permissions.require_jarvis_access"),
			patch("jarvis.permissions.has_jarvis_admin_access", return_value=True),
		):
			status = account.get_llm_connection_status()
			health = account.get_llm_connection_health()
			admin_notice = sh.get_subscription_notice()
		with (
			patch("jarvis.permissions.require_jarvis_access"),
			patch("jarvis.permissions.has_jarvis_admin_access", return_value=False),
		):
			notice = sh.get_subscription_notice()
		self.assertNotEqual(status["attention_reason"], "subscription_expired")
		self.assertEqual((status["attention_detail"], status["subscription_health"]), ({}, []))
		self.assertEqual(health, {"state": "ok"})
		self.assertEqual(admin_notice, {"expired": [], "upstreams": []})
		self.assertEqual(notice, {"expired": [], "expired_models": [], "upstreams": None})
