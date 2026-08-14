"""jarvis#840: the pre-chat preflight answers what readiness cannot.

Pins the verdict policy: a provider quota/429 classifies rate_limit (NON-
blocking by contract), only a genuine credential rejection classifies auth,
everything undeterminable is unchecked/unknown and never an error. Pins the
leg discrimination (pool reuses its persisted verdict, api-key reuses the
bench key probe, only direct-subscription fires the live turn), the deep
escalation of a not-ok integration answer, the billed-probe cache, and the
throwaway session's cleanup."""

from unittest.mock import patch

import frappe
from frappe.utils import now

from jarvis import preflight
from jarvis.tests.test_settings_on_update import _reset_settings
from jarvis.tests.test_unified_llm_config import _RT3SettingsTestCase

_QUOTA_TEXT = "OpenAI API error (429): usage_limit_reached - your plan is out of messages"
_AUTH_TEXT = "OpenAI API error (401): Incorrect API key provided"


class _FakeSession:
	"""Signature-compatible AgentSession stand-in (the admin#330 CI trap:
	kwargs-only fakes die on real positional args BEFORE assertions run)."""

	def __init__(self, events):
		self._events = events
		self.deleted = []
		self.closed = False
		self.created = []

	def create_session(self, *a, **kw):
		key = f"throwaway-{len(self.created)}"
		self.created.append(kw.get("label") or (a[0] if a else ""))
		return key

	def stream_agent_turn(self, *a, **kw):
		yield from self._events

	def delete_session(self, *a, **kw):
		self.deleted.append(a[0] if a else kw.get("session_key"))

	def close(self, *a, **kw):
		self.closed = True


class TestPreflight(_RT3SettingsTestCase):
	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		s = frappe.get_single("Jarvis Settings")
		s.db_set("llm_pool_synced_at", None, update_modified=False)
		s.db_set("llm_direct_synced_at", None, update_modified=False)
		s.db_set("agent_url", "http://tenant.example:19060", update_modified=False)
		frappe.cache().delete_value(preflight._PROBE_CACHE_KEY)
		frappe.db.commit()

	def tearDown(self):
		frappe.cache().delete_value(preflight._PROBE_CACHE_KEY)

	def _fake_connect(self, events):
		sess = _FakeSession(events)

		class _Conn:
			@staticmethod
			def connect(*a, **kw):
				return sess

		return sess, _Conn

	def _direct_sub(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("llm_auth_mode", "subscription", update_modified=False)
		return s

	# -- integration rows ------------------------------------------------------

	def test_integration_unchecked_on_any_admin_failure(self):
		for boom in (Exception("down"), frappe.ValidationError("no such method")):
			with patch("jarvis.admin_client.get_integration_status", side_effect=boom):
				item = preflight._integration_item()
			self.assertEqual(item["plugin"], "unchecked")
			self.assertEqual(item["persona"], "unchecked")

	def test_integration_ok_needs_no_deep_probe(self):
		with patch(
			"jarvis.admin_client.get_integration_status",
			return_value={"tri_state": {"plugin": "ok", "persona": "ok"}, "source": "live"},
		) as m:
			item = preflight._integration_item()
		self.assertEqual(m.call_count, 1, "an all-ok cheap probe must not pay the deep one")
		self.assertEqual((item["plugin"], item["persona"]), ("ok", "ok"))

	def test_integration_escalates_once_to_the_deep_probe(self):
		answers = [
			{"tri_state": {"plugin": "degraded", "persona": "ok"}, "source": "live"},
			{"tri_state": {"plugin": "ok", "persona": "ok"}, "source": "live"},
		]
		with patch("jarvis.admin_client.get_integration_status", side_effect=answers) as m:
			item = preflight._integration_item()
		self.assertEqual(m.call_count, 2)
		self.assertTrue(m.call_args_list[1].kwargs.get("deep"), "the retry must be the deep probe")
		self.assertEqual((item["plugin"], item["persona"]), ("ok", "ok"))

	# -- usable: leg discrimination -------------------------------------------

	def test_pool_leg_reuses_the_persisted_verdict(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_subscription_status", "verified", update_modified=False)
		with (
			patch("jarvis.jarvis.pool_serialize.compute_pool_mode", return_value=True),
			patch("jarvis.chat.agent_client.AgentSession") as never,
		):
			item = preflight._usable_item(s)
		never.connect.assert_not_called()
		self.assertEqual(item["state"], "ok")
		self.assertEqual(item["source"], "pool_probe")

	def test_api_key_leg_reuses_the_bench_key_probe(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("llm_auth_mode", "api_key", update_modified=False)
		with patch(
			"jarvis.llm_key_probe.test_llm_api_key",
			return_value={"ok": False, "verdict": "fail", "message": _QUOTA_TEXT},
		) as probe:
			item = preflight._usable_item(s)
		self.assertTrue(probe.call_args.kwargs.get("use_stored_key"))
		self.assertEqual(item["state"], "rate_limit", "a key probe 429 is non-blocking too")

	# -- usable: the direct-subscription live turn -----------------------------

	def test_live_probe_ok_on_first_token_and_cleans_up(self):
		s = self._direct_sub()
		sess, conn = self._fake_connect([{"kind": "assistant", "text": "ok", "delta": "ok"}])
		with patch("jarvis.chat.agent_client.AgentSession", conn):
			item = preflight._usable_item(s)
		self.assertEqual(item["state"], "ok")
		self.assertEqual(item["source"], "live_probe")
		self.assertEqual(len(sess.deleted), 1, "the billed throwaway session must be deleted")
		self.assertTrue(sess.closed)

	def test_live_probe_quota_429_is_rate_limit(self):
		s = self._direct_sub()
		_, conn = self._fake_connect([{"kind": "lifecycle", "phase": "error", "error": _QUOTA_TEXT}])
		with patch("jarvis.chat.agent_client.AgentSession", conn):
			item = preflight._usable_item(s)
		self.assertEqual(item["state"], "rate_limit")
		self.assertIn("usage_limit_reached", item["detail"])

	def test_live_probe_credential_rejection_is_auth(self):
		s = self._direct_sub()
		_, conn = self._fake_connect([{"kind": "relay:error", "state": "error", "error": _AUTH_TEXT}])
		with patch("jarvis.chat.agent_client.AgentSession", conn):
			item = preflight._usable_item(s)
		self.assertEqual(item["state"], "auth")

	def test_live_probe_connect_failure_is_unreachable(self):
		from jarvis.chat.agent_client import AgentUnreachableError

		s = self._direct_sub()

		class _Conn:
			@staticmethod
			def connect(*a, **kw):
				raise AgentUnreachableError("gateway down")

		with patch("jarvis.chat.agent_client.AgentSession", _Conn):
			item = preflight._usable_item(s)
		self.assertEqual(item["state"], "unreachable")

	def test_live_probe_verdict_is_cached_not_rebilled(self):
		s = self._direct_sub()
		sess, conn = self._fake_connect([{"kind": "assistant", "text": "ok", "delta": "ok"}])
		with patch("jarvis.chat.agent_client.AgentSession", conn):
			first = preflight._usable_item(s)
			second = preflight._usable_item(s)
		self.assertEqual(first["state"], "ok")
		self.assertEqual(second["state"], "ok")
		self.assertEqual(len(sess.created), 1, "a repeat inside the TTL must not bill again")

	# -- classification order --------------------------------------------------

	def test_quota_wins_over_auth_vocabulary(self):
		"""Quota prose routinely contains words like credit; it must stay on
		the NON-blocking side even when auth words co-occur."""
		verdict = preflight._classify_probe_error("insufficient credit for this credential")
		self.assertEqual(verdict["state"], "rate_limit")
		self.assertEqual(
			preflight._classify_probe_error("some entirely novel upstream failure")["state"],
			"unknown",
		)

	# -- endpoint envelope -----------------------------------------------------

	def test_endpoint_shape(self):
		s = self._direct_sub()
		s.db_set("last_sync_at", now(), update_modified=False)
		_, conn = self._fake_connect([{"kind": "assistant", "text": "ok", "delta": "ok"}])
		with (
			patch(
				"jarvis.admin_client.get_integration_status",
				return_value={"tri_state": {"plugin": "ok", "persona": "ok"}, "source": "live"},
			),
			patch("jarvis.chat.agent_client.AgentSession", conn),
		):
			data = preflight.run_chat_preflight()
		self.assertEqual(data["plugin"], "ok")
		self.assertEqual(data["persona"], "ok")
		self.assertEqual(data["usable"]["state"], "ok")
