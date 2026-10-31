"""Task J2: the per-tenant callback key verifies plugin callbacks, and the stored
owner token stops verifying once a callback key exists."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import onboarding_contract, _http, _plugin_auth, onboarding, settings_reset
from jarvis.tests._settings_snapshot import JarvisSettingsSnapshot
from jarvis._password_utils import clear_settings_password, set_settings_password
from jarvis._plugin_auth import PluginAuthError
from jarvis.tests.test_api import _FakeRequest, sign_plugin_headers

SETTINGS = "Jarvis Settings"
OWNER = "owner-token-j2"
CALLBACK = "ab" * 32
SESSION = "agent:test:j2"


class TestCallbackKey(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		# The retired-without-key path fetches the key through admin: never for real.
		fetch = patch("jarvis.onboarding.fetch_callback_key_if_missing")
		fetch.start()
		self.addCleanup(fetch.stop)
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

	def test_agent_token_rejected_once_callback_key_set(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		with self.assertRaises(PluginAuthError) as ctx:
			self._validate(OWNER)
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

	def test_retired_payload_clears_agent_token(self):
		set_settings_password(self.settings, "callback_key", CALLBACK)
		onboarding.write_connection({"owner_key_retired": True})
		self.assertEqual(_plugin_auth._agent_token(), "")
		self.assertEqual(_plugin_auth._callback_key(), CALLBACK)

	def test_not_retired_payload_keeps_agent_token(self):
		onboarding.write_connection({"owner_key_retired": False})
		self.assertEqual(_plugin_auth._agent_token(), OWNER)

	def test_settings_reset_clears_callback_key(self):
		# Never run the real reset on a live site: assert the scope and drive apply()
		# against a mock settings object with every side effect patched out.
		from unittest.mock import MagicMock

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
