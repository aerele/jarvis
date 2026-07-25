"""Tests for jarvis.admin_client - HTTPS wrapper for jarvis_admin calls.

All HTTP is mocked. Tests verify header construction, error mapping,
envelope unwrapping, and missing-config handling.
"""

from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client
from jarvis.admin_client import (
	DEFAULT_ADMIN_URL,
	DEFAULT_TIMEOUT_S,
	AdminAuthError,
	AdminUnreachableError,
	AdminValidationError,
	post_update_llm_creds,
)


def _settings_for_admin(
	admin_url="https://admin.example.com", api_key="customer-key-123", api_secret="customer-secret-456"
):
	"""Configure Jarvis Settings for the admin path. Uses db_set to bypass
	read_only on the fields."""
	settings = frappe.get_single("Jarvis Settings")
	settings.db_set("jarvis_admin_url", admin_url)
	settings.db_set("jarvis_admin_api_key", api_key)
	settings.db_set("jarvis_admin_api_secret", api_secret)
	frappe.db.commit()


def _settings_for_oauth(
	admin_url="https://admin.example.com",
	email="cust@example.com",
	password="pw-secret",
	api_key="",
	api_secret="",
):
	"""Configure Jarvis Settings for the OAuth bearer path. By default no
	api_key/secret so the bearer path is exercised in isolation; pass them to
	test the 401 -> legacy fallback."""
	settings = frappe.get_single("Jarvis Settings")
	settings.db_set("jarvis_admin_url", admin_url)
	settings.db_set("jarvis_admin_customer_email", email)
	settings.db_set("jarvis_admin_customer_password", password)
	settings.db_set("jarvis_admin_api_key", api_key)
	settings.db_set("jarvis_admin_api_secret", api_secret)
	frappe.db.commit()
	frappe.cache().delete_value(admin_client._OAUTH_CACHE_KEY)


def _settings_clear_admin():
	from frappe.utils.password import remove_encrypted_password

	settings = frappe.get_single("Jarvis Settings")
	settings.db_set("jarvis_admin_url", "")
	settings.db_set("jarvis_admin_customer_email", "")
	# Password fields need __Auth cleared too, not just the column.
	for f in ("jarvis_admin_api_key", "jarvis_admin_api_secret", "jarvis_admin_customer_password"):
		remove_encrypted_password("Jarvis Settings", "Jarvis Settings", f)
		settings.db_set(f, "")
	frappe.db.commit()
	frappe.cache().delete_value(admin_client._OAUTH_CACHE_KEY)


def _mock_response(status_code: int, json_body=None, text: str = ""):
	resp = MagicMock(spec=requests.Response)
	resp.status_code = status_code
	if json_body is None:
		resp.json.side_effect = ValueError("no json")
		resp.text = text
	else:
		resp.json.return_value = json_body
		resp.text = text or ""
	return resp


class TestHappyPath(FrappeTestCase):
	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_reload_returns_unwrapped_data(self):
		mock_post = MagicMock(
			return_value=_mock_response(
				200,
				json_body={"message": {"ok": True, "data": {"action": "reload", "result": "ok"}}},
			)
		)
		with patch("requests.post", mock_post):
			result = post_update_llm_creds(
				provider="Anthropic",
				model="claude-sonnet-4-6",
				base_url="https://api.anthropic.com",
				api_key="sk-new",
			)
		self.assertEqual(result, {"action": "reload", "result": "ok"})

	def test_restart_returns_unwrapped_data(self):
		mock_post = MagicMock(
			return_value=_mock_response(
				200,
				json_body={"message": {"ok": True, "data": {"action": "restart", "result": "ok"}}},
			)
		)
		with patch("requests.post", mock_post):
			result = post_update_llm_creds("OpenAI", "gpt-4o", "https://api.openai.com", "sk-new")
		self.assertEqual(result["action"], "restart")


class TestHeaders(FrappeTestCase):
	def setUp(self):
		_settings_for_admin(admin_url="https://admin.example.com", api_key="hdr-key", api_secret="hdr-secret")

	def tearDown(self):
		_settings_clear_admin()

	def test_sends_native_token_header(self):
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["url"] = url
			captured["headers"] = headers
			captured["json"] = json
			captured["timeout"] = timeout
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"action": "reload"}}})

		# Blank the bench's site-config jarvis_admin_url so the doctype-field
		# URL is the one exercised here (site config otherwise outranks it).
		with (
			patch.dict(frappe.local.conf, {"jarvis_admin_url": ""}),
			patch("requests.post", side_effect=_fake_post),
		):
			post_update_llm_creds("p", "m", "b", "k")

		self.assertEqual(captured["headers"]["Authorization"], "token hdr-key:hdr-secret")
		self.assertNotIn("X-Jarvis-Site", captured["headers"])
		self.assertEqual(captured["headers"]["Content-Type"], "application/json")
		self.assertEqual(captured["timeout"], DEFAULT_TIMEOUT_S)
		self.assertEqual(
			captured["json"],
			{
				"provider": "p",
				"model": "m",
				"base_url": "b",
				"api_key": "k",
				"auth_mode": "api_key",
				# Gating: the client sends the tenant's installed apps so the admin
				# can scope per-tenant skills/tools. Assert against the same source
				# the client reads, so this stays correct as apps change.
				"installed_apps": frappe.get_installed_apps(),
			},
		)
		self.assertTrue(captured["url"].startswith("https://admin.example.com/api/method/"))
		# Build the expected path from the namespace resolver so this passes
		# under the v2 default (jarvis_admin_v2.*) and any config override.
		self.assertIn(admin_client._m("api.tenant.update_llm_creds"), captured["url"])

	def test_raises_when_credentials_missing(self):
		from jarvis.exceptions import AdminAuthError

		_settings_clear_admin()
		with self.assertRaises(AdminAuthError):
			post_update_llm_creds("p", "m", "b", "k")

	def test_api_key_decrypted_via_get_password_not_attribute(self):
		"""Regression: jarvis_admin_api_key is a Password field. Attribute
		access on the settings doc returns Frappe's masked "*****" placeholder;
		only get_password() decrypts the real value out of __Auth. The previous
		bug read the attribute and shipped "*****" as the api_key, which admin
		rejected with 401 (see signup flow last_sync_status leakage)."""
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["headers"] = headers
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"action": "reload"}}})

		fake_settings = MagicMock()
		# Mimic the production load: attribute is the masked placeholder.
		fake_settings.jarvis_admin_api_key = "*****"
		fake_settings.jarvis_admin_url = "https://admin.example.com"

		# get_password returns the decrypted real value.
		def _get_password(field, raise_exception=False):
			return {"jarvis_admin_api_key": "real-key-xyz", "jarvis_admin_api_secret": "real-secret-abc"}.get(
				field, ""
			)

		fake_settings.get_password.side_effect = _get_password

		with (
			patch("frappe.get_single", return_value=fake_settings),
			patch("requests.post", side_effect=_fake_post),
		):
			post_update_llm_creds("p", "m", "b", "k")

		self.assertEqual(captured["headers"]["Authorization"], "token real-key-xyz:real-secret-abc")
		self.assertNotIn("*", captured["headers"]["Authorization"])


class TestNetworkErrors(FrappeTestCase):
	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_connection_error_raises_unreachable(self):
		with patch("requests.post", side_effect=requests.ConnectionError("refused")):
			with self.assertRaises(AdminUnreachableError):
				post_update_llm_creds("p", "m", "b", "k")

	def test_timeout_raises_unreachable(self):
		with patch("requests.post", side_effect=requests.Timeout("read timeout")):
			with self.assertRaises(AdminUnreachableError):
				post_update_llm_creds("p", "m", "b", "k")


class TestAdminErrorResponses(FrappeTestCase):
	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_401_raises_auth_error(self):
		mock_post = MagicMock(
			return_value=_mock_response(
				401,
				json_body={
					"message": {"ok": False, "error": {"code": "AuthenticationError", "message": "bad token"}}
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminAuthError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertIn("bad token", str(cm.exception))

	def test_403_raises_auth_error(self):
		mock_post = MagicMock(
			return_value=_mock_response(
				403,
				json_body={
					"message": {"ok": False, "error": {"code": "AuthenticationError", "message": "suspended"}}
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminAuthError):
				post_update_llm_creds("p", "m", "b", "k")

	def test_500_raises_unreachable(self):
		mock_post = MagicMock(
			return_value=_mock_response(
				500,
				json_body={"message": {"ok": False, "error": {"code": "ServerError", "message": "boom"}}},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminUnreachableError):
				post_update_llm_creds("p", "m", "b", "k")

	def test_200_with_ok_false_raises_unreachable(self):
		mock_post = MagicMock(
			return_value=_mock_response(
				200,
				json_body={
					"message": {"ok": False, "error": {"code": "NoRunningTenant", "message": "no tenant"}}
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminUnreachableError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertIn("NoRunningTenant", str(cm.exception))

	# Sprint-3 PR-8: 4xx envelope responses route to AdminValidationError,
	# not AdminUnreachableError. Used to be one bucket; UI showed
	# "admin is unreachable" for things like NoRunningTenant or
	# downgrade-not-supported.

	def test_400_with_envelope_raises_validation_error(self):
		"""InvalidArgument / InvalidToken / InvalidBlob etc. on tenant.py
		now show up on the bench as AdminValidationError - operator-actionable
		clean text, not "admin is unreachable; try again."""
		mock_post = MagicMock(
			return_value=_mock_response(
				400,
				json_body={
					"message": {
						"ok": False,
						"error": {"code": "InvalidToken", "message": "token too short"},
					}
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertEqual(str(cm.exception), "token too short")

	def test_409_with_envelope_raises_validation_error(self):
		"""NoRunningTenant (409) is a business-rule error, not a network
		failure - now routes to AdminValidationError so _surface() shows
		the actual reason."""
		mock_post = MagicMock(
			return_value=_mock_response(
				409,
				json_body={
					"message": {
						"ok": False,
						"error": {"code": "NoRunningTenant", "message": "no running tenant"},
					}
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertEqual(str(cm.exception), "no running tenant")

	def test_5xx_envelope_still_raises_unreachable(self):
		"""5xx is genuinely server-side; the unreachable class is correct."""
		mock_post = MagicMock(
			return_value=_mock_response(
				503,
				json_body={
					"message": {
						"ok": False,
						"error": {"code": "ServiceUnavailable", "message": "down"},
					}
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminUnreachableError):
				post_update_llm_creds("p", "m", "b", "k")

	def test_frappe_validation_error_surfaces_clean_message(self):
		"""When admin raises frappe.ValidationError inside a whitelisted endpoint
		(e.g. signup → _reject_duplicate_email), the response body has
		Frappe's exc_type/_server_messages envelope, not the {ok, error} shape.
		We extract the user-facing message and raise AdminValidationError."""
		mock_post = MagicMock(
			return_value=_mock_response(
				417,
				json_body={
					"exception": "frappe.exceptions.ValidationError: An active account already exists for venkat@aerele.in",
					"exc_type": "ValidationError",
					"_server_messages": '["{\\"message\\": \\"An active account already exists for venkat@aerele.in\\", \\"indicator\\": \\"red\\"}"]',
					"exc": '["Traceback (most recent call last):\\n..."]',
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertEqual(str(cm.exception), "An active account already exists for venkat@aerele.in")
		# No traceback dump should leak into the message.
		self.assertNotIn("Traceback", str(cm.exception))
		self.assertNotIn("frappe.exceptions.", str(cm.exception))

	def test_frappe_validation_error_falls_back_to_exception_string(self):
		"""If _server_messages is missing/empty, parse the message from the
		`exception` field by stripping the class prefix."""
		mock_post = MagicMock(
			return_value=_mock_response(
				417,
				json_body={
					"exception": "frappe.exceptions.DuplicateEntryError: Customer C-001 already exists",
					"exc_type": "DuplicateEntryError",
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertEqual(str(cm.exception), "Customer C-001 already exists")

	def test_frappe_permission_error_routes_to_auth_error(self):
		mock_post = MagicMock(
			return_value=_mock_response(
				403,
				json_body={
					"exception": "frappe.exceptions.PermissionError: customer status: Suspended",
					"exc_type": "PermissionError",
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminAuthError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertEqual(str(cm.exception), "customer status: Suspended")


class TestSecretScrubbingAtBoundary(FrappeTestCase):
	"""Cross-repo punch-list "secret values can leak to last_sync_status /
	Error Log via upstream passthrough" from the 2026-06-16 review.

	Defense-in-depth: even though admin's whitelisted endpoints shouldn't
	echo secrets in error messages, a future admin handler raising
	``frappe.throw("body was %s" % body)`` would reflect any token in the
	request straight back to the bench. admin_client must scrub
	token-shaped substrings BEFORE the Admin*Error reaches the caller,
	since the caller f-strings ``{e}`` straight into ``last_sync_status``
	(jarvis_settings.py:157) and frappe.log_error (Error Log doctype).
	"""

	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_api_key_value_redacted_from_validation_error(self):
		mock_post = MagicMock(
			return_value=_mock_response(
				500,
				json_body={
					"exc_type": "ValidationError",
					"_server_messages": (
						'["{\\"message\\": \\"upstream rejected: api_key=sk-AbCdEf1234567890abcdef is invalid\\"}"]'
					),
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		msg = str(cm.exception)
		self.assertNotIn("sk-AbCdEf1234567890abcdef", msg)
		self.assertIn("[REDACTED]", msg)
		# Keyword survives so operators can still see what kind of error
		# this was - the secret is the only thing redacted.
		self.assertIn("upstream rejected", msg)

	def test_authorization_header_redacted_from_4xx_envelope(self):
		# 4xx + structured envelope path (the most common cross-boundary
		# shape for admin handler validation failures).
		mock_post = MagicMock(
			return_value=_mock_response(
				417,
				json_body={
					"message": {
						"ok": False,
						"error": {
							"code": "BadAuth",
							"message": "echoed Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload-stuff.sig-stuff",
						},
					},
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		msg = str(cm.exception)
		self.assertNotIn("eyJhbGciOiJIUzI1NiJ9.payload-stuff.sig-stuff", msg)
		self.assertIn("[REDACTED]", msg)

	def test_jwt_in_exc_redacted_from_500_envelope(self):
		# Frappe always pairs ``exception`` with ``exc_type`` for raised
		# Python exceptions; the bench routes by exc_type allowlist into
		# AdminUnreachableError (unknown class). The scrub applies before
		# the exception class name is stripped.
		mock_post = MagicMock(
			return_value=_mock_response(
				500,
				json_body={
					"exc_type": "ValueError",
					"exception": "ValueError: id_token rejected: eyJ0eXAiOiJKV1QifQ.abcde123.signature456",
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(admin_client.AdminUnreachableError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		msg = str(cm.exception)
		self.assertNotIn("eyJ0eXAiOiJKV1QifQ.abcde123.signature456", msg)
		self.assertIn("[REDACTED]", msg)

	def test_oversized_message_truncated(self):
		# A 10KB traceback embedded in _server_messages must not blow up
		# the Data field that last_sync_status writes to.
		giant = "x" * 5000
		mock_post = MagicMock(
			return_value=_mock_response(
				500,
				json_body={
					"exc_type": "ValidationError",
					"_server_messages": f'["{{\\"message\\": \\"{giant}\\"}}"]',
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertLess(len(str(cm.exception)), 600)
		self.assertIn("...[truncated]", str(cm.exception))

	def test_clean_message_passes_through_unchanged(self):
		# Negative case: a message with no token-shaped substring stays
		# byte-identical (modulo the truncate cap).
		mock_post = MagicMock(
			return_value=_mock_response(
				417,
				json_body={
					"message": {
						"ok": False,
						"error": {
							"code": "no_subscription",
							"message": "no active subscription on this account",
						},
					},
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertEqual(str(cm.exception), "no active subscription on this account")


class TestNonJsonResponse(FrappeTestCase):
	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_html_error_page_raises_unreachable(self):
		mock_post = MagicMock(
			return_value=_mock_response(
				500,
				json_body=None,
				text="<html>Internal Server Error</html>",
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminUnreachableError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertIn("non-JSON", str(cm.exception))


class TestMissingConfig(FrappeTestCase):
	def setUp(self):
		_settings_clear_admin()

	def test_no_credentials_raises_auth_error(self):
		"""api_key + api_secret are required; missing either raises early."""
		from jarvis.exceptions import AdminAuthError

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("jarvis_admin_url", "https://admin.example.com")
		settings.db_set("jarvis_admin_api_key", "")
		settings.db_set("jarvis_admin_api_secret", "")
		frappe.db.commit()
		try:
			with self.assertRaises(AdminAuthError):
				post_update_llm_creds("p", "m", "b", "k")
		finally:
			_settings_clear_admin()

	def test_no_secret_raises_auth_error(self):
		from jarvis.exceptions import AdminAuthError

		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("jarvis_admin_url", "https://admin.example.com")
		settings.db_set("jarvis_admin_api_key", "some-key")
		settings.db_set("jarvis_admin_api_secret", "")
		frappe.db.commit()
		try:
			with self.assertRaises(AdminAuthError):
				post_update_llm_creds("p", "m", "b", "k")
		finally:
			_settings_clear_admin()


class TestOnboardingClient(FrappeTestCase):
	def tearDown(self):
		_settings_clear_admin()

	def test_signup_unwraps_data_and_uses_default_url_when_unset(self):
		_settings_clear_admin()  # no jarvis_admin_url → DEFAULT_ADMIN_URL
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["url"] = url
			captured["json"] = json
			return _mock_response(
				200,
				json_body={
					"message": {
						"ok": True,
						"data": {"api_key": "k", "api_secret": "s", "razorpay_key_id": "rzp"},
					}
				},
			)

		with patch("requests.post", side_effect=_fake_post):
			out = admin_client.signup("e@x.com", "Co", "Annual Plan")
		self.assertEqual(out["api_key"], "k")
		self.assertEqual(out["api_secret"], "s")
		self.assertTrue(captured["url"].startswith(DEFAULT_ADMIN_URL))
		self.assertIn("billing.signup.signup", captured["url"])
		self.assertEqual(captured["json"]["email"], "e@x.com")

	def test_get_plans_returns_list(self):
		_settings_for_admin()
		with patch(
			"requests.post",
			return_value=_mock_response(
				200, json_body={"message": {"ok": True, "data": [{"name": "p1", "plan_name": "P1"}]}}
			),
		):
			out = admin_client.get_plans()
		self.assertEqual(out[0]["name"], "p1")

	def test_get_connection_unwraps_data(self):
		_settings_for_admin()
		with patch(
			"requests.post",
			return_value=_mock_response(
				200,
				json_body={
					"message": {
						"ok": True,
						"data": {"agent_url": "ws://localhost:19000", "tenant_status": "running"},
					}
				},
			),
		):
			out = admin_client.get_connection()
		self.assertEqual(out["agent_url"], "ws://localhost:19000")

	def test_get_connection_reports_jarvis_version(self):
		"""The control plane closes out a release rollout from this — a tenant that
		has updated stops being served the release notice."""
		from jarvis import __version__

		_settings_for_admin()
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["body"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {}}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.get_connection()
		self.assertEqual(captured["body"]["jarvis_version"], __version__)

	def test_renew_posts_to_renew_and_unwraps_order(self):
		_settings_for_admin(api_key="renew-key", api_secret="renew-secret")
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["url"] = url
			captured["headers"] = headers
			return _mock_response(
				200,
				json_body={
					"message": {
						"ok": True,
						"data": {
							"razorpay_order_id": "order_R",
							"razorpay_key_id": "rzp",
							"amount_inr": 1500,
						},
					}
				},
			)

		with patch("requests.post", side_effect=_fake_post):
			out = admin_client.renew()
		self.assertEqual(out["razorpay_order_id"], "order_R")
		self.assertIn(admin_client._m("api.tenant.renew"), captured["url"])
		self.assertEqual(captured["headers"]["Authorization"], "token renew-key:renew-secret")

	def test_guest_call_omits_authorization_header(self):
		# get_plans is a guest endpoint - no auth header is sent.
		_settings_clear_admin()
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["headers"] = headers
			return _mock_response(200, json_body={"message": {"ok": True, "data": []}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.get_plans()
		self.assertNotIn("Authorization", captured["headers"])

	def test_get_preset_catalog_falls_back_on_non_admin_error(self):
		# A scheme-less jarvis_admin_url raises requests.MissingSchema — NOT an
		# Admin* error — which must still degrade to the bundled catalog rather
		# than 500 the onboarding preset step. #200 review #9.
		import requests

		from jarvis._preset_catalog import BUNDLED_PRESET_CATALOG

		frappe.cache().delete_value(admin_client._PRESET_CATALOG_CACHE_KEY)
		with patch(
			"jarvis.admin_client._post_guest",
			side_effect=requests.exceptions.MissingSchema("no scheme in URL"),
		):
			out = admin_client.get_preset_catalog()
		self.assertEqual(out, BUNDLED_PRESET_CATALOG)


class TestPostUpdateLlmCredsAuthMode(FrappeTestCase):
	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_default_auth_mode_is_api_key(self):
		captured = {}

		def _fake_post(url, json=None, **_kw):
			captured["body"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"action": "restart"}}})

		with patch("requests.post", side_effect=_fake_post):
			post_update_llm_creds(provider="OpenAI", model="gpt-4o", base_url="", api_key="sk-1")
		self.assertEqual(captured["body"]["auth_mode"], "api_key")

	def test_explicit_subscription_auth_mode(self):
		captured = {}

		def _fake_post(url, json=None, **_kw):
			captured["body"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"action": "restart"}}})

		with patch("requests.post", side_effect=_fake_post):
			post_update_llm_creds(
				provider="OpenAI",
				model="",
				base_url="",
				api_key="AT-1",
				auth_mode="subscription",
			)
		self.assertEqual(captured["body"]["auth_mode"], "subscription")
		self.assertEqual(captured["body"]["api_key"], "AT-1")


class TestPostRotateLlmSecret(FrappeTestCase):
	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_happy_path(self):
		captured = {}

		def _fake_post(url, json=None, **_kw):
			captured["url"] = url
			captured["body"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"action": "reload"}}})

		with patch("requests.post", side_effect=_fake_post):
			result = admin_client.post_rotate_llm_secret(secret="AT-rotated")
		self.assertEqual(result.get("action"), "reload")
		self.assertIn("rotate_llm_secret", captured["url"])
		self.assertEqual(captured["body"], {"secret": "AT-rotated"})

	def test_429_raises_AdminRateLimitedError(self):
		from jarvis.exceptions import AdminRateLimitedError

		mock_post = MagicMock(
			return_value=_mock_response(
				429,
				json_body={
					"message": {
						"ok": False,
						"error": {
							"code": "RateLimitExceeded",
							"message": "rotation rate limit hit",
							"retry_after_seconds": 1200,
						},
					}
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminRateLimitedError) as ctx:
				admin_client.post_rotate_llm_secret(secret="AT-x")
		self.assertEqual(ctx.exception.retry_after_seconds, 1200)


class TestPostPushOauthBlob(FrappeTestCase):
	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_happy_path_posts_blob(self):
		captured = {}
		blob = {
			"type": "oauth",
			"provider": "openai-codex",
			"access": "AT-fresh",
			"refresh": "RT-fresh",
			"expires": 1735689600,
		}

		def _fake_post(url, json=None, timeout=None, **_kw):
			captured["url"] = url
			captured["body"] = json
			captured["timeout"] = timeout
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"ok": True}}})

		with patch("requests.post", side_effect=_fake_post):
			result = admin_client.post_push_oauth_blob("openai-codex", blob)
		self.assertEqual(result, {"ok": True})
		self.assertIn("push_oauth_blob", captured["url"])
		self.assertEqual(captured["body"], {"provider": "openai-codex", "blob": blob})
		# Timeout must exceed admin's own put_auth_profile bound (150s) so
		# bench doesn't time out before admin can return. Lower bound
		# locked at 180s; raise this and admin's bound together if either
		# bumps. Doctor + restart + healthz easily fits inside 150s on a
		# healthy host.
		self.assertGreaterEqual(
			captured["timeout"],
			180,
			"post_push_oauth_blob timeout must accommodate fleet-agent's "
			"doctor + restart + healthz chain (~120s typical, 150s cap)",
		)


class TestPostSubscriptionDisconnect(FrappeTestCase):
	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_happy_path(self):
		captured = {}

		def _fake_post(url, json=None, **_kw):
			captured["url"] = url
			captured["body"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"ok": True}}})

		with patch("requests.post", side_effect=_fake_post):
			result = admin_client.post_subscription_disconnect()
		self.assertEqual(result, {"ok": True})
		self.assertIn("subscription_disconnect", captured["url"])
		self.assertEqual(captured["body"], {})


class TestPairChatDevice(FrappeTestCase):
	"""Sprint-2 plumb-through (2026-06-16 review): bench's pair_chat_device
	now accepts request_timeout_s and forwards it as a body field so admin
	can configure its admin -> fleet-agent leg accordingly."""

	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_default_timeout_sent_as_30(self):
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["body"] = json
			return _mock_response(
				200,
				json_body={
					"message": {
						"ok": True,
						"data": {"device_token": "tok"},
					}
				},
			)

		with patch("requests.post", side_effect=_fake_post):
			admin_client.pair_chat_device(public_key="pk", device_id="did")
		self.assertEqual(captured["body"]["request_timeout_s"], 30)
		self.assertEqual(captured["body"]["public_key"], "pk")
		self.assertEqual(captured["body"]["device_id"], "did")

	def test_caller_supplied_timeout_forwarded(self):
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["body"] = json
			return _mock_response(
				200,
				json_body={
					"message": {
						"ok": True,
						"data": {"device_token": "tok"},
					}
				},
			)

		with patch("requests.post", side_effect=_fake_post):
			admin_client.pair_chat_device(
				public_key="pk",
				device_id="did",
				request_timeout_s=75,
			)
		self.assertEqual(captured["body"]["request_timeout_s"], 75)


class TestOAuthBearer(FrappeTestCase):
	"""Bench-side OAuth password-grant: prefer a cached bearer token, fall back
	to legacy api_key:api_secret when no password is stored."""

	def tearDown(self):
		_settings_clear_admin()

	def _route(self, *, token_response, api_capture, api_response=None):
		"""Build a requests.post stand-in that routes the OAuth token endpoint
		vs the real API call by URL. ``api_capture`` records the API call's
		headers; ``token_response``/``api_response`` are _mock_response objects
		(or callables returning one, for stateful tests)."""

		# A _mock_response is itself a MagicMock (callable); only treat a real
		# function as a stateful factory, never a mock response object.
		def _resolve(r, arg):
			if callable(r) and not isinstance(r, MagicMock):
				return r(arg)
			return r

		def _fake_post(url, data=None, json=None, headers=None, timeout=None):
			if url.endswith(admin_client._OAUTH_TOKEN_PATH):
				api_capture.setdefault("token_calls", []).append(data)
				return _resolve(token_response, data)
			api_capture.setdefault("api_headers", []).append(headers)
			api_capture["json"] = json
			r = _resolve(api_response, headers)
			return r or _mock_response(200, json_body={"message": {"ok": True, "data": {"x": 1}}})

		return _fake_post

	def test_prefers_bearer_and_omits_client_secret(self):
		_settings_for_oauth()
		cap = {}
		token_resp = _mock_response(
			200,
			json_body={
				"access_token": "ACCESS-1",
				"refresh_token": "REFRESH-1",
				"token_type": "Bearer",
				"expires_in": 900,
			},
		)
		with patch("requests.post", side_effect=self._route(token_response=token_resp, api_capture=cap)):
			admin_client.get_connection()
		# Token requested via password grant, public-client (no client_secret).
		grant = cap["token_calls"][0]
		self.assertEqual(grant["grant_type"], "password")
		self.assertEqual(grant["username"], "cust@example.com")
		self.assertEqual(grant["password"], "pw-secret")
		self.assertEqual(grant["client_id"], "jarvis-bench")
		self.assertNotIn("client_secret", grant)
		# API call carried the bearer, not a token key:secret header.
		self.assertEqual(cap["api_headers"][0]["Authorization"], "Bearer ACCESS-1")

	def test_caches_access_token_across_calls(self):
		_settings_for_oauth()
		cap = {}
		token_resp = _mock_response(
			200,
			json_body={
				"access_token": "ACCESS-1",
				"refresh_token": "REFRESH-1",
				"token_type": "Bearer",
				"expires_in": 900,
			},
		)
		with patch("requests.post", side_effect=self._route(token_response=token_resp, api_capture=cap)):
			admin_client.get_connection()
			admin_client.get_connection()
		# Two API calls, but the token endpoint was hit only once (cached).
		self.assertEqual(len(cap["token_calls"]), 1)
		self.assertEqual(len(cap["api_headers"]), 2)
		self.assertEqual(cap["api_headers"][1]["Authorization"], "Bearer ACCESS-1")

	def test_uses_refresh_token_when_access_expired_but_refresh_present(self):
		# Cache holds a live refresh token but the access token has expired.
		# The next call must renew via grant_type=refresh_token (the cheap
		# path) instead of replaying the password grant, and must carry the
		# freshly minted access token on the API call. Exercises the
		# refresh-first branch in _admin_access_token (the password grant is
		# only the durable bootstrap fallback). 2026-06-21 review follow-up.
		_settings_for_oauth()
		# Seed AFTER _settings_for_oauth (it clears the cache): a stale access
		# token (expires_at in the past) plus a still-valid refresh token.
		frappe.cache().set_value(
			admin_client._OAUTH_CACHE_KEY,
			{
				"access_token": "STALE-ACCESS",
				"refresh_token": "REFRESH-1",
				"access_expires_at": 0,
			},
		)
		cap = {}
		token_resp = _mock_response(
			200,
			json_body={
				"access_token": "ACCESS-2",
				"refresh_token": "REFRESH-2",
				"token_type": "Bearer",
				"expires_in": 900,
			},
		)
		with patch("requests.post", side_effect=self._route(token_response=token_resp, api_capture=cap)):
			admin_client.get_connection()
		# Exactly one token call, and it was the refresh grant - never password.
		self.assertEqual(len(cap["token_calls"]), 1)
		grant = cap["token_calls"][0]
		self.assertEqual(grant["grant_type"], "refresh_token")
		self.assertEqual(grant["refresh_token"], "REFRESH-1")
		self.assertNotIn("username", grant)
		self.assertNotIn("password", grant)
		# Public client: client_id present, no client_secret.
		self.assertEqual(grant["client_id"], "jarvis-bench")
		self.assertNotIn("client_secret", grant)
		# The API call carried the access token minted off the refresh grant.
		self.assertEqual(cap["api_headers"][0]["Authorization"], "Bearer ACCESS-2")

	def test_falls_back_to_legacy_without_password(self):
		# No customer_password -> legacy api_key:api_secret path.
		_settings_for_admin(api_key="legacy-key", api_secret="legacy-secret")
		cap = {}

		def _fake_post(url, data=None, json=None, headers=None, timeout=None):
			cap["headers"] = headers
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"x": 1}}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.get_connection()
		self.assertEqual(cap["headers"]["Authorization"], "token legacy-key:legacy-secret")

	def test_bearer_401_remints_then_falls_back_to_legacy(self):
		# Password present (bearer preferred) AND legacy creds present. The API
		# rejects the bearer twice (revoked); after a re-mint it still 401s, so
		# the call falls back to the legacy header and succeeds.
		_settings_for_oauth(api_key="legacy-key", api_secret="legacy-secret")
		cap = {}
		token_resp = _mock_response(
			200,
			json_body={
				"access_token": "ACCESS-1",
				"token_type": "Bearer",
				"expires_in": 900,
			},
		)

		def _api_response(headers):
			auth = headers["Authorization"]
			if auth.startswith("Bearer "):
				return _mock_response(
					401,
					json_body={
						"message": {"ok": False, "error": {"code": "AuthError", "message": "bad token"}}
					},
				)
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"ok": 1}}})

		with patch(
			"requests.post",
			side_effect=self._route(token_response=token_resp, api_capture=cap, api_response=_api_response),
		):
			result = admin_client.get_connection()
		self.assertEqual(result, {"ok": 1})
		# Two bearer attempts (initial + re-mint) then the legacy header.
		auths = [h["Authorization"] for h in cap["api_headers"]]
		self.assertEqual(auths[0], "Bearer ACCESS-1")
		self.assertEqual(auths[-1], "token legacy-key:legacy-secret")
		self.assertGreaterEqual(len(cap["token_calls"]), 2)
		# The poisoned token must be evicted so the next call re-mints clean.
		self.assertIsNone(frappe.cache().get_value(admin_client._OAUTH_CACHE_KEY))

	def test_bearer_403_is_terminal_no_remint_no_fallback(self):
		# A 403 is an authorization denial, not a stale token. The bearer call
		# must NOT re-mint, NOT fall back to legacy, and NOT evict the cached
		# token - doing so would storm the token endpoint on every call and
		# mask the real "forbidden" behind a generic auth retry. Both the
		# bearer and the legacy api_key:api_secret back the same customer
		# principal, so the fallback would 403 again anyway.
		_settings_for_oauth(api_key="legacy-key", api_secret="legacy-secret")
		cap = {}
		token_resp = _mock_response(
			200,
			json_body={
				"access_token": "ACCESS-1",
				"token_type": "Bearer",
				"expires_in": 900,
			},
		)

		def _api_response(headers):
			return _mock_response(
				403,
				json_body={
					"message": {"ok": False, "error": {"code": "Forbidden", "message": "not allowed"}}
				},
			)

		with patch(
			"requests.post",
			side_effect=self._route(token_response=token_resp, api_capture=cap, api_response=_api_response),
		):
			with self.assertRaises(AdminAuthError) as ctx:
				admin_client.get_connection()
		# The real 403 is surfaced (status tagged), not retried away.
		self.assertEqual(ctx.exception.status_code, 403)
		# Exactly one bearer attempt: no force-refresh, no legacy header.
		auths = [h["Authorization"] for h in cap["api_headers"]]
		self.assertEqual(auths, ["Bearer ACCESS-1"])
		# Token endpoint hit once (initial mint only) - no re-mint storm.
		self.assertEqual(len(cap["token_calls"]), 1)
		# Cache retained (not evicted) so the next call reuses the valid token.
		self.assertIsNotNone(frappe.cache().get_value(admin_client._OAUTH_CACHE_KEY))

	def test_zero_expiry_token_is_not_cached(self):
		# A token with no usable lifetime must not be cached (else every call
		# would miss and storm the token endpoint).
		admin_client._cache_oauth_token({"access_token": "A", "expires_in": 0})
		self.assertIsNone(frappe.cache().get_value(admin_client._OAUTH_CACHE_KEY))
		admin_client._cache_oauth_token({"access_token": "B", "expires_in": 900})
		self.assertEqual(frappe.cache().get_value(admin_client._OAUTH_CACHE_KEY)["access_token"], "B")
		frappe.cache().delete_value(admin_client._OAUTH_CACHE_KEY)


class TestAdminUrlResolution(FrappeTestCase):
	"""_admin_url resolution order: site/common config (frappe.conf
	jarvis_admin_url) -> Jarvis Settings override -> hardcoded fallback, all
	resolved FRESH per call so a config value added after worker start is
	honored. Site config outranks the doctype field so a stale value left in
	Jarvis Settings by a reinstall cannot mask a correctly-configured
	control plane."""

	def test_config_wins_over_settings_field(self):
		s = MagicMock()
		s.jarvis_admin_url = "https://override.example.com/"
		with patch.dict(frappe.local.conf, {"jarvis_admin_url": "https://conf.example.com"}):
			self.assertEqual(admin_client._admin_url(s), "https://conf.example.com")

	def test_stale_dev_field_does_not_mask_site_config(self):
		"""Reinstall regression: Jarvis Settings.jarvis_admin_url ends up
		holding the stale dev default "http://127.0.0.1:8000", but the site
		config correctly points at the real control plane. The resolver must
		use the site config, not the stale doctype value, or the admin is
		unreachable after every reinstall."""
		s = MagicMock()
		s.jarvis_admin_url = "http://127.0.0.1:8000"
		with patch.dict(frappe.local.conf, {"jarvis_admin_url": "http://jarvis.admin:8002"}):
			self.assertEqual(admin_client._admin_url(s), "http://jarvis.admin:8002")

	def test_falls_back_to_field_when_config_blank(self):
		s = MagicMock()
		s.jarvis_admin_url = "https://override.example.com/"
		with patch.dict(frappe.local.conf, {"jarvis_admin_url": ""}):
			self.assertEqual(admin_client._admin_url(s), "https://override.example.com")

	def test_falls_back_to_hardcoded_when_field_and_config_blank(self):
		from jarvis.hooks import _DEFAULT_ADMIN_URL_FALLBACK

		s = MagicMock()
		s.jarvis_admin_url = ""
		with patch.dict(frappe.local.conf, {"jarvis_admin_url": ""}):
			self.assertEqual(admin_client._admin_url(s), _DEFAULT_ADMIN_URL_FALLBACK)


class TestAdminAppNamespace(FrappeTestCase):
	"""The admin control-plane app namespace is resolved FRESH per call from
	site config ``jarvis_admin_app`` and defaults to ``jarvis_admin_v2`` (the
	v2 switch). ``_m`` builds every admin /api/method path under it; the
	Frappe-native OAuth token endpoint stays un-namespaced."""

	def test_default_namespace_is_v2(self):
		# No override configured -> the SWITCH-TO-V2 default.
		with patch.dict(frappe.local.conf, {"jarvis_admin_app": ""}):
			self.assertEqual(admin_client._admin_app(), "jarvis_admin_v2")
			self.assertEqual(
				admin_client._m("api.tenant.renew"),
				"/api/method/jarvis_admin_v2.api.tenant.renew",
			)

	def test_config_override_repoints_to_v1(self):
		# A bench pinned back to v1 via site config.
		with patch.dict(frappe.local.conf, {"jarvis_admin_app": "jarvis_admin"}):
			self.assertEqual(admin_client._admin_app(), "jarvis_admin")
			self.assertEqual(
				admin_client._m("billing.signup.get_plans"),
				"/api/method/jarvis_admin.billing.signup.get_plans",
			)

	def test_blank_override_falls_back_to_default(self):
		# A whitespace-only override is treated as unset.
		with patch.dict(frappe.local.conf, {"jarvis_admin_app": "   "}):
			self.assertEqual(admin_client._admin_app(), "jarvis_admin_v2")

	def test_override_read_fresh_per_call(self):
		# A config value flipped after import is honored without a restart.
		with patch.dict(frappe.local.conf, {"jarvis_admin_app": "jarvis_admin"}):
			first = admin_client._m("api.tenant.get_connection")
		with patch.dict(frappe.local.conf, {"jarvis_admin_app": "jarvis_admin_v2"}):
			second = admin_client._m("api.tenant.get_connection")
		self.assertIn("/jarvis_admin.api.tenant.get_connection", first)
		self.assertIn("/jarvis_admin_v2.api.tenant.get_connection", second)

	def test_oauth_token_path_is_not_namespaced(self):
		# The OAuth token mint hits Frappe's native endpoint, never the admin app
		# namespace — it must not gain a jarvis_admin* prefix under either config.
		self.assertEqual(
			admin_client._OAUTH_TOKEN_PATH,
			"/api/method/frappe.integrations.oauth2.get_token",
		)
		with patch.dict(frappe.local.conf, {"jarvis_admin_app": "jarvis_admin_v2"}):
			self.assertNotIn("jarvis_admin", admin_client._OAUTH_TOKEN_PATH)
