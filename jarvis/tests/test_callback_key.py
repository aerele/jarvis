"""Task J2: the per-tenant callback key verifies plugin callbacks, and the stored
owner token stops verifying once a callback key exists."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import _http, _plugin_auth, onboarding, settings_reset
from jarvis._password_utils import clear_settings_password, set_settings_password
from jarvis._plugin_auth import PluginAuthError
from jarvis.tests.test_api import _FakeRequest, sign_plugin_headers

SETTINGS = "Jarvis Settings"
OWNER = "owner-token-j2"
CALLBACK = "ab" * 32
SESSION = "agent:test:j2"


class TestCallbackKey(FrappeTestCase):
	def setUp(self):
		super().setUp()
		s = frappe.get_single(SETTINGS)
		self._orig_agent = s.get_password("agent_token", raise_exception=False) or ""
		self._orig_cb = s.get_password("callback_key", raise_exception=False) or ""
		self._orig_issued = s.get("agent_token_issued_at")
		self.settings = s
		set_settings_password(s, "agent_token", OWNER, update_modified=False)
		clear_settings_password(s, "callback_key")

	def tearDown(self):
		s = self.settings
		clear_settings_password(s, "callback_key")
		clear_settings_password(s, "agent_token")
		if self._orig_agent:
			set_settings_password(s, "agent_token", self._orig_agent, update_modified=False)
		if self._orig_cb:
			set_settings_password(s, "callback_key", self._orig_cb, update_modified=False)
		s.db_set("agent_token_issued_at", self._orig_issued, update_modified=False)
		super().tearDown()

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
		set_settings_password(self.settings, "callback_key", CALLBACK)
		settings_reset.apply(self.settings, settings_reset.CONNECTION)
		self.assertEqual(_plugin_auth._callback_key(), "")
