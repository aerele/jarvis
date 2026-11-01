"""Auth refusals keep correlation data without forwarding session bearers."""

import hashlib
import hmac
from unittest import TestCase
from unittest.mock import Mock, patch

from jarvis import _http
from jarvis import _plugin_auth as auth
from jarvis._http import constant_time_equal

SESSION = "agent:agent-log-fixture:PRIVATE-SESSION-BEARER"
NONCE = "PRIVATE-NONCE-FIXTURE-123456"
TOKEN = "synthetic-auth-fixture-token"


class TestPluginAuthLogPrivacy(TestCase):
	def setUp(self):
		logger = patch.object(auth.frappe, "log_error")
		self.log = logger.start()
		self.addCleanup(logger.stop)

	def assert_private_log(self):
		self.assertTrue(self.log.called)
		logged = str(self.log.call_args_list)
		self.assertNotIn(SESSION, logged)
		self.assertNotIn(NONCE[:8], logged)
		self.assertNotIn(TOKEN, logged)
		self.assertIn("session_hash=", logged)
		self.assertIn(hashlib.sha256(SESSION.encode()).hexdigest()[:12], logged)

	def test_rate_limit_log_does_not_contain_the_session(self):
		cache = Mock()
		cache.incr.return_value = 61
		with patch.object(auth.frappe, "cache", return_value=cache), self.assertRaises(auth.PluginAuthError):
			auth._enforce_rate_limit(SESSION)
		self.assert_private_log()

	def test_bad_signature_log_contains_no_session_or_nonce_text(self):
		with patch.object(auth.time, "time", return_value=1000), self.assertRaises(auth.PluginAuthError):
			auth._validate_signature(
				token=TOKEN,
				session_key=SESSION,
				body_bytes=b"{}",
				signature="invalid",
				nonce=NONCE,
				timestamp_str="1000",
			)
		self.assert_private_log()

	def test_replay_log_contains_no_session_or_nonce_text(self):
		canonical = auth._canonical_request(
			session_key=SESSION, body_bytes=b"{}", nonce=NONCE, timestamp_s=1000
		)
		signature = hmac.new(TOKEN.encode(), canonical, hashlib.sha256).hexdigest()
		cache = Mock()
		cache.set.return_value = False
		with (
			patch.object(auth.time, "time", return_value=1000),
			patch.object(auth.frappe, "cache", return_value=cache),
			self.assertRaises(auth.PluginAuthError),
		):
			auth._validate_signature(
				token=TOKEN,
				session_key=SESSION,
				body_bytes=b"{}",
				signature=signature,
				nonce=NONCE,
				timestamp_str="1000",
			)
		self.assert_private_log()

	def test_bad_bearer_log_does_not_forward_the_session_header(self):
		headers = {"X-Jarvis-Token": "invalid", "X-Jarvis-Session": SESSION}
		with (
			patch.object(auth, "_safe_headers", return_value=headers),
			patch.object(auth, "_agent_token", return_value=TOKEN),
			patch.object(auth, "_caller_ip", return_value="127.0.0.1"),
			self.assertRaises(auth.PluginAuthError),
		):
			auth.validate_plugin_request(b"{}")
		self.assert_private_log()

	def test_unsigned_log_does_not_forward_the_session_header(self):
		headers = {"X-Jarvis-Token": TOKEN, "X-Jarvis-Session": SESSION}
		with (
			patch.object(auth, "_safe_headers", return_value=headers),
			patch.object(auth, "_agent_token", return_value=TOKEN),
			patch.object(auth, "_caller_ip", return_value="127.0.0.1"),
			patch.object(auth, "_should_audit_unsigned_reject", return_value=True),
			self.assertRaises(auth.PluginAuthError),
		):
			auth.validate_plugin_request(b"{}")
		self.assert_private_log()

	def test_non_ascii_bearer_is_a_clean_authentication_rejection(self):
		presented = "t\u00f6k\u00e9n"
		headers = {"X-Jarvis-Token": presented, "X-Jarvis-Session": SESSION}
		with (
			patch.object(auth, "_safe_headers", return_value=headers),
			patch.object(auth, "_agent_token", return_value=TOKEN),
			patch.object(auth, "_caller_ip", return_value="127.0.0.1"),
			self.assertRaises(auth.PluginAuthError) as caught,
		):
			auth.validate_plugin_request(b"{}")
		self.assertEqual(caught.exception.http_status, 401)
		self.assertEqual(caught.exception.code, "AuthenticationError")
		self.assertNotIn(presented, str(self.log.call_args_list))
		self.assert_private_log()

	def test_legacy_bearer_helper_rejects_non_ascii_without_raising(self):
		request = Mock(headers={"X-Jarvis-Token": "t\u00f6k\u00e9n"})
		settings = Mock()
		settings.get_password.return_value = TOKEN
		with (
			patch.object(_http.frappe, "request", request, create=True),
			patch.object(_http.frappe, "get_single", return_value=settings),
		):
			self.assertFalse(_http.validate_bearer())

	def test_non_ascii_signature_is_a_clean_authentication_rejection(self):
		with (
			patch.object(auth.time, "time", return_value=1000),
			self.assertRaises(auth.PluginAuthError) as caught,
		):
			auth._validate_signature(
				token=TOKEN,
				session_key=SESSION,
				body_bytes=b"{}",
				signature="\u00e9" * 64,
				nonce=NONCE,
				timestamp_str="1000",
			)
		self.assertEqual(caught.exception.http_status, 401)
		self.assertEqual(caught.exception.code, "AuthenticationError")
		self.assert_private_log()

	def test_text_compare_remains_strict_and_never_raises_for_a_lone_surrogate(self):
		self.assertTrue(constant_time_equal("same", "same"))
		self.assertFalse(constant_time_equal("same", "SAME"))
		self.assertFalse(constant_time_equal("\ud800", "x"))
