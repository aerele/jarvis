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

	def __init__(self, events, raise_exc=None, stall_s=0.0):
		self._events = events
		self._raise = raise_exc
		self._stall_s = stall_s
		self.closed = False
		self.created = []

	def create_session(self, *a, **kw):
		key = f"throwaway-{len(self.created)}"
		self.created.append(kw.get("label") or (a[0] if a else ""))
		return key

	def stream_agent_turn(self, *a, **kw):
		import time as _time

		if self._stall_s:
			_time.sleep(self._stall_s)
		if self._raise is not None:
			raise self._raise
		yield from self._events

	def delete_session(self, *a, **kw):
		pass  # reclaim_throwaway_session owns deletion; patched in tests

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
		s.db_set("llm_last_apply_fingerprint", "", update_modified=False)
		frappe.cache().delete_value(preflight._probe_cache_key(s))
		frappe.db.commit()

	def tearDown(self):
		frappe.cache().delete_value(preflight._probe_cache_key(frappe.get_single("Jarvis Settings")))

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
		from jarvis.admin_client import AdminValidationError

		method_not_found = AdminValidationError(
			"Failed to get method for command jarvis_admin_v2.api.tenant.integration_status"
		)
		for boom in (Exception("down"), method_not_found):
			with patch("jarvis.admin_client.get_integration_status", side_effect=boom) as m:
				item = preflight._integration_item()
			self.assertEqual(m.call_count, 1, "a failed admin round trip must not be retried deep")
			self.assertEqual(item["plugin"], "unchecked")
			self.assertEqual(item["persona"], "unchecked")

	def test_integration_cached_not_ok_is_not_escalated(self):
		"""Review M3: a cached/none answer means the fleet call already failed
		or there is no container - the deep retry would pay a second full
		round trip to meet the same wall."""
		with patch(
			"jarvis.admin_client.get_integration_status",
			return_value={"tri_state": {"plugin": "unchecked", "persona": "unchecked"}, "source": "cached"},
		) as m:
			preflight._integration_item()
		self.assertEqual(m.call_count, 1)

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
		with (
			patch("jarvis.chat.agent_client.AgentSession", conn),
			patch("jarvis.chat.session_lifecycle.reclaim_throwaway_session") as reclaim,
		):
			item = preflight._usable_item(s)
		self.assertEqual(item["state"], "ok")
		self.assertEqual(item["source"], "live_probe")
		# NEVER a bare delete_session (jarvis#525/#535: deleting under a live
		# run re-creates the session as an orphan or kills the run) - the
		# reclaim helper, with fired_at so a not-yet-started run is protected.
		reclaim.assert_called_once()
		self.assertEqual(reclaim.call_args.args[1], sess.created and "throwaway-0")
		self.assertIsNotNone(reclaim.call_args.kwargs.get("fired_at"))
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
		_, conn = self._fake_connect([{"kind": "lifecycle", "phase": "error", "error": _AUTH_TEXT}])
		with patch("jarvis.chat.agent_client.AgentSession", conn):
			item = preflight._usable_item(s)
		self.assertEqual(item["state"], "auth")

	def test_live_probe_raised_auth_fault_is_auth_not_unreachable(self):
		"""Review M1: an auth fault can arrive as a RAISED gateway rejection
		(agent RPC refused), not only as lifecycle error text. Mapping it to
		"unreachable" would open chat on a credential the probe just proved
		rejected."""
		from jarvis.chat.agent_client import AgentUnreachableError

		s = self._direct_sub()
		boom = AgentUnreachableError(f"agent rejected: INVALID_REQUEST: {_AUTH_TEXT}")
		sess = _FakeSession([], raise_exc=boom)

		class _Conn:
			@staticmethod
			def connect(*a, **kw):
				return sess

		with (
			patch("jarvis.chat.agent_client.AgentSession", _Conn),
			patch("jarvis.chat.session_lifecycle.reclaim_throwaway_session"),
		):
			item = preflight._usable_item(s)
		self.assertEqual(item["state"], "auth")

	def test_live_probe_hard_budget_never_hangs_the_request(self):
		"""Review B2: stream_agent_turn's own deadline is the chat turn's 600s
		and it can block long before its first yield. The probe must come back
		within its OWN budget with a non-blocking verdict, and cache it (the
		run was billed)."""
		s = self._direct_sub()
		sess = _FakeSession([{"kind": "lifecycle", "phase": "end"}], stall_s=2.0)

		class _Conn:
			@staticmethod
			def connect(*a, **kw):
				return sess

		import time as _time

		with (
			patch("jarvis.chat.agent_client.AgentSession", _Conn),
			patch("jarvis.chat.session_lifecycle.reclaim_throwaway_session"),
			patch.object(preflight, "_PROBE_BUDGET_S", 0.2),
		):
			t0 = _time.monotonic()
			item = preflight._usable_item(s)
			elapsed = _time.monotonic() - t0
		self.assertLess(elapsed, 1.5, "the probe must not wait out the stream's own deadline")
		self.assertEqual(item["state"], "unknown")
		self.assertIn("timed out", item["detail"])
		cached = frappe.cache().get_value(preflight._probe_cache_key(s))
		self.assertEqual((cached or {}).get("state"), "unknown", "a billed timeout must cache")

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
		verdict = preflight._classify_probe_error("insufficient credit balance for this credential")
		self.assertEqual(verdict["state"], "rate_limit")
		self.assertEqual(
			preflight._classify_probe_error("some entirely novel upstream failure")["state"],
			"unknown",
		)

	def test_scope_failures_are_auth_not_rate_limit(self):
		"""Review M2: the quota vocabulary is context-bound, so a genuine
		credential/permission failure that happens to say "insufficient" never
		lands on the non-blocking side."""
		verdict = preflight._classify_probe_error(
			"Google Generative AI API error (403): PERMISSION_DENIED: "
			"Request had insufficient authentication scopes."
		)
		self.assertEqual(verdict["state"], "auth")

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
