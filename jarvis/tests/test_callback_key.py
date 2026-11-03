"""Tasks J2 + J3a: the per-tenant callback key verifies plugin callbacks; the owner
token keeps verifying until retirement clears it. The site signs plugin-route
requests with the callback key, else the owner token."""

import hashlib
import hmac
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import _http, _plugin_auth, admin_client, onboarding, onboarding_contract, settings_reset
from jarvis._password_utils import clear_settings_password, set_settings_password
from jarvis._plugin_auth import PluginAuthError
from jarvis.chat import plugin_routes
from jarvis.exceptions import AdminSiteIsCopyError, AdminValidationError
from jarvis.tests._settings_snapshot import JarvisSettingsSnapshot
from jarvis.tests.test_api import _FakeRequest, sign_plugin_headers

SETTINGS = "Jarvis Settings"
OWNER = "owner-token-j2"
CALLBACK = "ab" * 32
SESSION = "agent:test:j2"


class TestCallbackKey(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		# The retired-without-key path and a wrong key both queue the self-heal sync
		# (through admin): never for real.
		for target in ("jarvis._plugin_auth._request_key_resync",):
			mocked = patch(target)
			mocked.start()
			self.addCleanup(mocked.stop)
		resync = patch("jarvis.onboarding.request_key_resync")
		self.resync = resync.start()
		self.addCleanup(resync.stop)
		self.settings = frappe.get_single(SETTINGS)
		set_settings_password(self.settings, "agent_token", OWNER, update_modified=False)
		clear_settings_password(self.settings, "callback_key")

	def _validate(self, token):
		headers = {"X-Jarvis-Token": token, "X-Jarvis-Session": SESSION}
		headers.update(sign_plugin_headers(token, SESSION))
		with patch.object(frappe, "request", _FakeRequest(headers), create=True):
			return _plugin_auth.validate_plugin_request(b"")

	def test_callback_key_verifies_plugin_request(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		self.assertEqual(self._validate(CALLBACK), SESSION)

	def test_keys_are_read_without_loading_the_settings_doc(self):
		"""Owner rule: no get_doc to read. Both keys come from __Auth directly."""
		set_settings_password(self.settings, "callback_key", CALLBACK)
		with patch("frappe.get_single", side_effect=AssertionError("loaded the Settings doc")):
			self.assertEqual(self._validate(CALLBACK), SESSION)
			self.assertTrue(onboarding.has_callback_key())

	def test_either_key_verifies_before_retirement(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		self.assertEqual(self._validate(CALLBACK), SESSION)
		self.assertEqual(self._validate(OWNER), SESSION)

	def test_agent_token_rejected_once_agent_token_cleared(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		clear_settings_password(self.settings, "agent_token")
		with self.assertRaises(PluginAuthError) as ctx:
			self._validate(OWNER)
		self.assertEqual(ctx.exception.http_status, 401)
		self.assertEqual(self._validate(CALLBACK), SESSION)

	def test_token_matching_one_key_with_hmac_of_the_other_is_rejected(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		for token, signer in ((OWNER, CALLBACK), (CALLBACK, OWNER)):
			headers = {"X-Jarvis-Token": token, "X-Jarvis-Session": SESSION}
			headers.update(sign_plugin_headers(signer, SESSION))
			with patch.object(frappe, "request", _FakeRequest(headers), create=True):
				with self.assertRaises(PluginAuthError) as ctx:
					_plugin_auth.validate_plugin_request(b"")
			self.assertEqual(ctx.exception.http_status, 401)

	def test_falls_back_to_agent_token_without_callback_key(self):
		self.assertEqual(self._validate(OWNER), SESSION)

	def test_validate_bearer_uses_callback_key(self):
		def check(token):
			with patch.object(frappe, "request", _FakeRequest({"X-Jarvis-Token": token}), create=True):
				return _http.validate_bearer()

		self.assertTrue(check(OWNER))
		set_settings_password(self.settings, "callback_key", CALLBACK)
		self.assertTrue(check(CALLBACK))
		self.assertTrue(check(OWNER))
		clear_settings_password(self.settings, "agent_token")
		self.assertTrue(check(CALLBACK))
		self.assertFalse(check(OWNER))

	def test_store_callback_key_validates_shape(self):
		self.assertFalse(onboarding._store_callback_key("short"))
		self.assertFalse(onboarding._store_callback_key("AB" * 32))
		self.assertFalse(onboarding._store_callback_key(""))
		self.assertEqual(_plugin_auth._callback_key(), "")
		self.assertTrue(onboarding._store_callback_key(CALLBACK))
		self.assertEqual(_plugin_auth._callback_key(), CALLBACK)

	def test_payload_stores_callback_key(self):
		onboarding.write_connection({"callback_key": CALLBACK})
		self.assertEqual(_plugin_auth._callback_key(), CALLBACK)
		self.assertEqual(_plugin_auth._agent_token(), OWNER)

	def test_retired_payload_with_a_key_clears_agent_token(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		onboarding.write_connection({"owner_key_retired": True})
		self.assertEqual(_plugin_auth._agent_token(), "")
		self.assertEqual(_plugin_auth._callback_key(), CALLBACK)
		self.resync.assert_not_called()

	def test_retired_payload_carrying_the_key_clears_agent_token(self):
		onboarding.write_connection({"owner_key_retired": True, "callback_key": CALLBACK})
		self.assertEqual(_plugin_auth._agent_token(), "")
		self.assertEqual(_plugin_auth._callback_key(), CALLBACK)
		self.resync.assert_not_called()

	def test_retired_payload_without_a_key_clears_agent_token_and_queues_resync(self):
		"""Fail closed: the old owner token also sits in every pre-retirement copy of
		the site, so it must stop verifying even with no callback key stored. The key
		is fetched by the throttled resync (and every later plugin call re-queues it)."""
		onboarding.write_connection({"owner_key_retired": True})
		self.assertEqual(_plugin_auth._agent_token(), "")
		self.assertEqual(_plugin_auth._callback_key(), "")
		self.resync.assert_called_once_with()

	def test_retired_payload_with_a_malformed_key_clears_agent_token_and_queues_resync(self):
		onboarding.write_connection({"owner_key_retired": True, "callback_key": "short"})
		self.assertEqual(_plugin_auth._agent_token(), "")
		self.resync.assert_called_once_with()

	def test_retired_payload_inside_a_key_sync_queues_no_nested_job(self):
		with (
			patch.object(frappe.flags, "jarvis_key_fetch", True, create=True),
			patch("jarvis.onboarding._shorten_key_resync_throttle") as shorten,
		):
			onboarding.write_connection({"owner_key_retired": True})
		self.assertEqual(_plugin_auth._agent_token(), "")
		self.resync.assert_not_called()
		shorten.assert_called_once_with()

	def test_not_retired_payload_keeps_agent_token(self):
		onboarding.write_connection({"owner_key_retired": False})
		self.assertEqual(_plugin_auth._agent_token(), OWNER)

	def test_settings_reset_clears_callback_key(self):
		# Never run the real reset on a live site: assert the scope and drive apply()
		# against a mock settings object with every side effect patched out.
		self.assertIn("callback_key", settings_reset.CONNECTION.passwords)
		mock_settings = MagicMock()
		with (
			patch.object(settings_reset, "clear_settings_password") as clear,
			patch("jarvis.admin_client.clear_cached_token"),
			patch.object(frappe.db, "delete"),
		):
			settings_reset.apply(mock_settings, settings_reset.CONNECTION)
		clear.assert_any_call(mock_settings, "callback_key")

	def _set_authority(self, gen, handle):
		self.settings.db_set("tenant_authority_generation", gen)
		self.settings.db_set("tenant_authority_handle", handle)

	def test_rejected_stale_payload_changes_neither_key_nor_token(self):
		self._set_authority(5, "h-current")
		payload = {
			"agent_url": "ws://stale.invalid",
			"tenant_authority_generation": 3,
			"tenant_authority_handle": "h-old",
			"callback_key": CALLBACK,
			"owner_key_retired": True,
		}
		onboarding.write_connection(payload)
		self.assertEqual(_plugin_auth._callback_key(), "")
		self.assertEqual(_plugin_auth._agent_token(), OWNER)

	def test_rejected_payload_without_agent_url_changes_neither(self):
		self._set_authority(5, "h-current")
		onboarding.write_connection(
			{"tenant_authority_generation": 3, "callback_key": CALLBACK, "owner_key_retired": True}
		)
		self.assertEqual(_plugin_auth._callback_key(), "")
		self.assertEqual(_plugin_auth._agent_token(), OWNER)

	def test_accepted_payload_stores_key_and_retires_token(self):
		self._set_authority(5, "h-current")
		onboarding.write_connection(
			{
				"agent_url": "ws://ok.invalid",
				"tenant_authority_generation": 6,
				"tenant_authority_handle": "h-new",
				"callback_key": CALLBACK,
				"owner_key_retired": True,
			}
		)
		self.assertEqual(_plugin_auth._callback_key(), CALLBACK)
		self.assertEqual(_plugin_auth._agent_token(), "")

	def test_new_admin_login_clears_old_callback_key(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		onboarding.write_connection({"customer": "new@example.com"})
		self.assertEqual(_plugin_auth._callback_key(), "")

	def test_workspace_reset_disconnect_keeps_callback_key(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		with patch("jarvis.chat.device.clear_credentials"):
			onboarding._disconnect_agent_transport(self.settings)
		self.assertEqual(_plugin_auth._callback_key(), CALLBACK)

	def test_strip_credentials_removes_callback_key(self):
		out = onboarding_contract.strip_credentials({"callback_key": CALLBACK, "agent_url": "x"})
		self.assertNotIn("callback_key", out)
		self.assertEqual(out["agent_url"], "x")


class TestSignedRouteHeaders(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.settings = frappe.get_single(SETTINGS)
		set_settings_password(self.settings, "agent_token", OWNER, update_modified=False)
		clear_settings_password(self.settings, "callback_key")

	def test_header_shape(self):
		h = plugin_routes.signed_route_headers(SESSION, b"{}")
		self.assertEqual(
			set(h),
			{
				"X-Jarvis-Token",
				"X-Jarvis-Session",
				"X-Jarvis-Signature",
				"X-Jarvis-Nonce",
				"X-Jarvis-Timestamp",
			},
		)
		self.assertEqual(h["X-Jarvis-Session"], SESSION)
		self.assertEqual(len(h["X-Jarvis-Nonce"]), 32)
		self.assertEqual(len(h["X-Jarvis-Signature"]), 64)
		self.assertNotEqual(
			h["X-Jarvis-Nonce"], plugin_routes.signed_route_headers(SESSION, b"{}")["X-Jarvis-Nonce"]
		)

	def test_signs_with_owner_token_without_callback_key(self):
		self.assertEqual(plugin_routes.signed_route_headers(SESSION, b"")["X-Jarvis-Token"], OWNER)

	def test_signs_with_callback_key_when_set(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		self.assertEqual(plugin_routes.signed_route_headers(SESSION, b"")["X-Jarvis-Token"], CALLBACK)

	def test_no_key_raises_fixed_message(self):
		clear_settings_password(self.settings, "agent_token")
		with self.assertRaises(plugin_routes.PluginRouteKeyMissingError) as ctx:
			plugin_routes.signed_route_headers(SESSION, b"")
		self.assertEqual(str(ctx.exception), "No plugin signing key is stored on this site.")

	def test_signature_verifies_with_the_sites_own_verifier(self):
		for callback in (None, CALLBACK):
			if callback:
				set_settings_password(self.settings, "callback_key", callback)
			body = b'{"session_key": "x"}'
			headers = plugin_routes.signed_route_headers(SESSION, body)
			with patch.object(frappe, "request", _FakeRequest(headers), create=True):
				self.assertEqual(_plugin_auth.validate_plugin_request(body), SESSION)

	def test_signature_covers_the_body(self):
		headers = plugin_routes.signed_route_headers(SESSION, b"a")
		with patch.object(frappe, "request", _FakeRequest(headers), create=True):
			with self.assertRaises(PluginAuthError):
				_plugin_auth.validate_plugin_request(b"b")


def _confirm_enqueues(enq):
	return [
		c for c in enq.call_args_list if c.args and c.args[0] == "jarvis.onboarding.confirm_callback_key_job"
	]


class TestCallbackKeyConfirm(JarvisSettingsSnapshot, FrappeTestCase):
	"""The site tells admin, by digest, that it stored the callback key (contract C1)."""

	def setUp(self):
		super().setUp()
		self.settings = frappe.get_single(SETTINGS)
		clear_settings_password(self.settings, "callback_key")
		resync = patch("jarvis.onboarding.request_key_resync")
		resync.start()
		self.addCleanup(resync.stop)

	def test_stored_key_queues_one_confirm_after_commit(self):
		with patch("frappe.enqueue") as enq:
			onboarding.write_connection({"callback_key": CALLBACK})
		(call,) = _confirm_enqueues(enq)
		self.assertEqual(call.kwargs["queue"], "short")
		self.assertTrue(call.kwargs["deduplicate"])
		self.assertTrue(call.kwargs["enqueue_after_commit"])
		self.assertEqual(call.kwargs["job_id"], f"jarvis_callback_key_confirm:{frappe.local.site}")

	def test_malformed_key_queues_no_confirm(self):
		with patch("frappe.enqueue") as enq:
			onboarding.write_connection({"callback_key": "short"})
		self.assertEqual(_confirm_enqueues(enq), [])

	def test_payload_without_a_key_queues_no_confirm(self):
		with patch("frappe.enqueue") as enq:
			onboarding.write_connection({"owner_key_retired": False})
		self.assertEqual(_confirm_enqueues(enq), [])

	def test_rejected_stale_payload_queues_no_confirm(self):
		self.settings.db_set("tenant_authority_generation", 5)
		self.settings.db_set("tenant_authority_handle", "h-current")
		with patch("frappe.enqueue") as enq:
			onboarding.write_connection(
				{
					"agent_url": "ws://stale.invalid",
					"tenant_authority_generation": 3,
					"tenant_authority_handle": "h-old",
					"callback_key": CALLBACK,
				}
			)
		self.assertEqual(_confirm_enqueues(enq), [])

	def test_an_enqueue_failure_never_fails_the_store(self):
		with patch("frappe.enqueue", side_effect=RuntimeError("queue down")):
			onboarding.write_connection({"callback_key": CALLBACK})
		self.assertEqual(_plugin_auth._callback_key(), CALLBACK)

	def test_digest_is_the_contract_hmac(self):
		expected = hmac.new(CALLBACK.encode("ascii"), b"jarvis-callback-key-confirm:v1", hashlib.sha256)
		self.assertEqual(onboarding.callback_key_digest(CALLBACK), expected.hexdigest())
		self.assertRegex(onboarding.callback_key_digest(CALLBACK), r"[0-9a-f]{64}")

	def test_confirm_job_sends_the_hmac_digest_never_the_key(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		with (
			patch.object(admin_client, "_post", return_value={"confirmed": True}) as post,
			patch.object(frappe, "log_error") as log_error,
		):
			onboarding.confirm_callback_key_job()
		post.assert_called_once()
		self.assertTrue(post.call_args.kwargs["path"].endswith(".api.tenant.confirm_callback_key"))
		self.assertEqual(
			post.call_args.kwargs["body"], {"key_digest": onboarding.callback_key_digest(CALLBACK)}
		)
		self.assertNotIn(CALLBACK, repr(post.call_args))
		self.assertFalse(post.call_args.kwargs.get("allow_copy", False))
		log_error.assert_not_called()

	def test_confirm_job_without_a_stored_key_calls_nothing(self):
		with patch.object(admin_client, "_post") as post:
			onboarding.confirm_callback_key_job()
		post.assert_not_called()

	def test_confirm_failure_is_logged_without_key_material(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)

		def _boom(**kwargs):
			secret_local = CALLBACK  # a frame local the log must not dump
			raise RuntimeError(f"admin down ({len(secret_local)})")

		with (
			patch.object(admin_client, "_post", side_effect=_boom),
			patch.object(frappe, "log_error") as log_error,
		):
			onboarding.confirm_callback_key_job()
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.kwargs["title"], "callback key confirm failed")
		self.assertIn("RuntimeError: admin down", log_error.call_args.kwargs["message"])
		self.assertNotIn(CALLBACK, repr(log_error.call_args))

	def test_old_admin_without_confirm_is_ignored(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		missing = AdminValidationError(
			"Failed to get method for command jarvis_admin_v2.api.tenant.confirm_callback_key",
			exc_type="ValidationError",
		)
		with (
			patch.object(admin_client, "_post", side_effect=missing),
			patch.object(frappe, "log_error") as log_error,
		):
			onboarding.confirm_callback_key_job()
		log_error.assert_not_called()

	def test_other_admin_validation_errors_are_logged(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		with (
			patch.object(
				admin_client,
				"_post",
				side_effect=AdminValidationError("bad shape", exc_type="ValidationError"),
			),
			patch.object(frappe, "log_error") as log_error,
		):
			onboarding.confirm_callback_key_job()
		log_error.assert_called_once()

	def test_a_flagged_copy_never_confirms_and_logs_nothing(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		with (
			patch.object(admin_client, "_post", side_effect=AdminSiteIsCopyError("other.site")),
			patch.object(frappe, "log_error") as log_error,
		):
			onboarding.confirm_callback_key_job()
		log_error.assert_not_called()
