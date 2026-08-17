"""Tests for jarvis.admin_client - HTTPS wrapper for jarvis_admin calls.

All HTTP is mocked. Tests verify header construction, error mapping,
envelope unwrapping, and missing-config handling.
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client
from jarvis.admin_client import (
	DEFAULT_ADMIN_URL,
	DEFAULT_TIMEOUT_S,
	AdminAmbiguousError,
	AdminAuthError,
	AdminUnreachableError,
	AdminValidationError,
	post_update_llm_creds,
)

# jarvis #566. These fixtures used to configure the REAL Jarvis Settings Single
# and then, on teardown, call remove_encrypted_password() on three of its
# Password fields. Both halves committed, so the whole thing escaped
# FrappeTestCase's rollback, and running this module against a real site
# permanently destroyed that site's control-plane credentials. That is not
# theoretical: it took site.jarvis's jarvis_admin_api_key, api_secret and
# customer_password on 2026-07-30 and they had to be restored by hand.
#
# The teardown's deletion was load-bearing GIVEN the setup: db_set on a Password
# field writes the plaintext straight to tabSingles and never touches __Auth
# (jarvis/_password_utils.py documents this), and BaseDocument.get_password
# prefers that column but FALLS BACK to __Auth once it is blanked. So on a real
# doc, "cleared" could not be made to read as cleared without dropping the
# site's own rows. The fix is therefore not to soften the teardown but to stop
# using a real doc.
#
# Nothing here needs one. Everything admin_client reads off Jarvis Settings is
# three things - jarvis_admin_url, jarvis_admin_customer_email and
# get_password() - so a plain object serves the whole surface while writing no
# tabSingles row, no __Auth row and no commit. Same seam
# TestPermanentRejectionClassification already uses; #542 proved it there and
# this generalises it to the module.


class _FakeSettings:
	"""Stand-in for the Jarvis Settings Single, for admin_client's readers only.

	``get_password`` mirrors ``BaseDocument.get_password``'s contract minus the
	``__Auth`` fallback, which is the entire point: there is no encrypted store
	behind this, so a field that was cleared reads as cleared and cannot serve a
	real credential left over from the site.
	"""

	_FIELDS = (
		"jarvis_admin_url",
		"jarvis_admin_customer_email",
		"jarvis_admin_api_key",
		"jarvis_admin_api_secret",
		"jarvis_admin_customer_password",
	)

	def __init__(self):
		self.reset()

	def reset(self):
		for field in self._FIELDS:
			setattr(self, field, "")

	def get_password(self, fieldname="password", raise_exception=True):
		value = getattr(self, fieldname, "") or ""
		if not value and raise_exception:
			frappe.throw(f"Password not found for Jarvis Settings {fieldname}")
		return value

	def db_set(self, fieldname, value=None, **kwargs):
		"""Accepts both call shapes the real Document.db_set does. Writes to this
		object alone - the tests that reach for it are configuring the fixture,
		not asserting on persistence."""
		for field, val in (fieldname if isinstance(fieldname, dict) else {fieldname: value}).items():
			setattr(self, field, val)


_ADMIN_SETTINGS = _FakeSettings()
_REAL_GET_SINGLE = frappe.get_single
_get_single_patcher = None


def _fake_get_single(doctype, *args, **kwargs):
	"""Serve the fake for Jarvis Settings ONLY; every other doctype still
	resolves normally, so this cannot quietly change unrelated behaviour."""
	if doctype == "Jarvis Settings":
		return _ADMIN_SETTINGS
	return _REAL_GET_SINGLE(doctype, *args, **kwargs)


def setUpModule():
	global _get_single_patcher
	_get_single_patcher = patch("frappe.get_single", side_effect=_fake_get_single)
	_get_single_patcher.start()


def tearDownModule():
	if _get_single_patcher is not None:
		_get_single_patcher.stop()


def _require_fake_settings():
	"""Refuse to run against a real Single. Without this, a runner that skipped
	the module fixture would send every helper below straight back at the site's
	own Jarvis Settings - silently, which is exactly how #566 went unnoticed."""
	if frappe.get_single("Jarvis Settings") is not _ADMIN_SETTINGS:
		raise RuntimeError(
			"test_admin_client fixtures require the module-level frappe.get_single patch; "
			"refusing to touch the real Jarvis Settings (see jarvis #566)"
		)


def _admin_settings():
	"""The ONLY sanctioned way for a test in this module to hold the settings doc.

	A bare frappe.get_single here would be correct while the module patch is up and
	catastrophic if it ever is not, and it reads identically in both cases - so the
	guard has to sit in front of every reach for the doc, not just the three fixture
	helpers. Checking first also keeps the failure ordered: a test that grabbed the
	real Single and then wrote to it would blow up in its own `finally` cleanup,
	AFTER the write it was supposed to prevent."""
	_require_fake_settings()
	return frappe.get_single("Jarvis Settings")


def _settings_for_admin(
	admin_url="https://admin.example.com", api_key="customer-key-123", api_secret="customer-secret-456"
):
	"""Configure the fake Jarvis Settings for the api_key:api_secret path."""
	_require_fake_settings()
	_ADMIN_SETTINGS.reset()
	_ADMIN_SETTINGS.jarvis_admin_url = admin_url
	_ADMIN_SETTINGS.jarvis_admin_api_key = api_key
	_ADMIN_SETTINGS.jarvis_admin_api_secret = api_secret


def _settings_for_oauth(
	admin_url="https://admin.example.com",
	email="cust@example.com",
	password="pw-secret",
	api_key="",
	api_secret="",
):
	"""Configure the fake Jarvis Settings for the OAuth bearer path. By default no
	api_key/secret so the bearer path is exercised in isolation; pass them to
	test the 401 -> legacy fallback."""
	_require_fake_settings()
	_ADMIN_SETTINGS.reset()
	_ADMIN_SETTINGS.jarvis_admin_url = admin_url
	_ADMIN_SETTINGS.jarvis_admin_customer_email = email
	_ADMIN_SETTINGS.jarvis_admin_customer_password = password
	_ADMIN_SETTINGS.jarvis_admin_api_key = api_key
	_ADMIN_SETTINGS.jarvis_admin_api_secret = api_secret
	frappe.cache().delete_value(admin_client._OAUTH_CACHE_KEY)


def _settings_clear_admin():
	"""Blank every admin field. No __Auth row exists behind the fake, so this is
	the whole of "not onboarded" - nothing is deleted and nothing to restore."""
	_require_fake_settings()
	_ADMIN_SETTINGS.reset()
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

	# ------------------------------------------------------------------ #
	# #743: distinguish an AMBIGUOUS transport failure (admin MAY have received
	# the request) from a CONFIRMED not-delivered one (nothing was sent). Every
	# ambiguous case is still an AdminUnreachableError subclass, so the two tests
	# above keep passing unchanged - that inheritance is the zero-regret proof.
	# ------------------------------------------------------------------ #
	@staticmethod
	def _refused_connection_error():
		"""The shape requests raises when the socket is REFUSED / never opened:
		ConnectionError(MaxRetryError(reason=NewConnectionError(...))). Built by hand
		so the classifier is exercised against the real production nesting, not a bare
		ConnectionError."""
		from urllib3.exceptions import MaxRetryError, NewConnectionError

		reason = NewConnectionError(None, "[Errno 111] Connection refused")
		return requests.ConnectionError(MaxRetryError(pool=None, url="/x", reason=reason))

	@staticmethod
	def _reset_connection_error():
		"""A connection RESET after the request went out: ConnectionError(ProtocolError(
		'Connection aborted.', ConnectionResetError(...))). The request may already have
		crossed, so this is ambiguous."""
		from urllib3.exceptions import ProtocolError

		aborted = ProtocolError("Connection aborted.", ConnectionResetError(104, "reset by peer"))
		return requests.ConnectionError(aborted)

	def test_read_timeout_is_ambiguous(self):
		with patch("requests.post", side_effect=requests.ReadTimeout("read timed out")):
			with self.assertRaises(AdminAmbiguousError):
				post_update_llm_creds("p", "m", "b", "k")

	def test_bare_timeout_is_ambiguous(self):
		with patch("requests.post", side_effect=requests.Timeout("timed out")):
			with self.assertRaises(AdminAmbiguousError):
				post_update_llm_creds("p", "m", "b", "k")

	def test_connect_timeout_is_not_ambiguous(self):
		"""A connect timeout never completed the handshake, so nothing was sent."""
		with patch("requests.post", side_effect=requests.ConnectTimeout("connect timed out")):
			with self.assertRaises(AdminUnreachableError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertNotIsInstance(cm.exception, AdminAmbiguousError)

	def test_refused_connection_is_not_ambiguous(self):
		"""A refused / never-opened socket sent nothing, so a retry is safe."""
		with patch("requests.post", side_effect=self._refused_connection_error()):
			with self.assertRaises(AdminUnreachableError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertNotIsInstance(cm.exception, AdminAmbiguousError)

	def test_reset_connection_is_ambiguous(self):
		"""A reset AFTER the request went out may already have been received."""
		with patch("requests.post", side_effect=self._reset_connection_error()):
			with self.assertRaises(AdminAmbiguousError):
				post_update_llm_creds("p", "m", "b", "k")

	def test_ambiguous_is_an_unreachable_subclass(self):
		"""Every existing catch site that catches AdminUnreachableError keeps working."""
		self.assertTrue(issubclass(AdminAmbiguousError, AdminUnreachableError))


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

	def test_unlisted_4xx_exc_type_routes_to_validation_error(self):
		"""A ValidationError SUBCLASS whose exc_type isn't on the allowlist
		(e.g. Helpdesk's InvalidEmailAddressError on 417) still means the admin
		was REACHED and rejected the request. Surface its clean message as
		AdminValidationError, not the misleading "admin is unreachable"."""
		mock_post = MagicMock(
			return_value=_mock_response(
				417,
				json_body={
					"exception": "frappe.exceptions.InvalidEmailAddressError: Administrator is not a valid Email Address",
					"exc_type": "InvalidEmailAddressError",
					"_server_messages": '["{\\"message\\": \\"Administrator is not a valid Email Address\\", \\"indicator\\": \\"red\\"}"]',
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				post_update_llm_creds("p", "m", "b", "k")
		self.assertEqual(str(cm.exception), "Administrator is not a valid Email Address")

	def test_unlisted_5xx_exc_type_stays_unreachable(self):
		"""An unrecognised exc_type on a 5xx is a genuine admin-side fault - the
		unreachable class is still correct there."""
		mock_post = MagicMock(
			return_value=_mock_response(
				500,
				json_body={
					"exception": "some.module.WeirdInternalError: kaboom",
					"exc_type": "WeirdInternalError",
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminUnreachableError):
				post_update_llm_creds("p", "m", "b", "k")

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


class TestIsMethodNotFound(FrappeTestCase):
	"""admin_client.is_method_not_found: the subscription_connect deploy-window
	discriminator that tells "admin has not shipped this dotted path yet"
	apart from the endpoint's own business rejections. The landed admin build's
	rejections (InvalidBlob, UnknownProvider, ProviderMismatch, ...) all carry
	a structured error.code via @fleet_endpoint's EndpointError family, which
	excludes them outright; the message-text check is the fallback signal for
	anything that reaches here uncoded (see is_method_not_found's docstring)."""

	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_frappes_own_missing_method_rejection_is_detected_end_to_end(self):
		"""Frappe's handler.py execute_cmd wraps get_attr()'s failure in
		frappe.throw(_("Failed to get method for command {0} with {1}")...) -
		default exc class ValidationError, http_status_code 417. Shape the mock
		response exactly like that wire contract and confirm the resulting
		AdminValidationError classifies as method-not-found."""
		mock_post = MagicMock(
			return_value=_mock_response(
				417,
				json_body={
					"exception": (
						"frappe.exceptions.ValidationError: Failed to get method for command "
						"jarvis_admin_v2.api.tenant.subscription_connect with module "
						"'jarvis_admin_v2.api.tenant' has no attribute 'subscription_connect'"
					),
					"exc_type": "ValidationError",
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				admin_client.post_subscription_connect("openai", {}, "OpenAI", model="m", base_url="")
		self.assertTrue(admin_client.is_method_not_found(cm.exception))

	def test_a_coded_business_rejection_is_not_method_not_found(self):
		"""The REAL wire shape of the landed subscription_connect's business
		rejections: @fleet_endpoint RETURNS (never throws) {"ok": false,
		"error": {"code": ..., "message": ...}} under a 400 - e.g.
		ProviderMismatch when llm_provider's expected auth-profile id disagrees
		with provider. That arrives here as an AdminContractError whose
		exc_type is None, the SAME as the missing-method case - so the code
		check (not exc_type) is what has to exclude it."""
		mock_post = MagicMock(
			return_value=_mock_response(
				400,
				json_body={
					"message": {
						"ok": False,
						"error": {
							"code": "ProviderMismatch",
							"message": (
								"provider 'openai' does not match llm_provider 'gemini' "
								"(expected 'google-gemini-cli')"
							),
						},
					}
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				admin_client.post_subscription_connect("openai", {}, "gemini", model="m", base_url="")
		self.assertEqual(getattr(cm.exception, "code", ""), "ProviderMismatch")
		self.assertFalse(admin_client.is_method_not_found(cm.exception))

	def test_an_ordinary_uncoded_business_rejection_is_not_method_not_found(self):
		"""Belt-and-suspenders: even a hypothetical bare frappe.throw business
		rejection (no code, exc_type "ValidationError" - the same shape the
		missing-method case has) must not be misread as "admin doesn't have
		this method yet" on message content alone."""
		mock_post = MagicMock(
			return_value=_mock_response(
				417,
				json_body={
					"exception": "frappe.exceptions.ValidationError: unusable oauth grant",
					"exc_type": "ValidationError",
					"_server_messages": '["{\\"message\\": \\"unusable oauth grant\\", \\"indicator\\": \\"red\\"}"]',
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				admin_client.post_subscription_connect("openai", {}, "OpenAI", model="m", base_url="")
		self.assertFalse(admin_client.is_method_not_found(cm.exception))

	def test_a_coded_exception_is_not_method_not_found_regardless_of_text(self):
		"""A DuplicateEntryError (or any other coded/typed rejection) can never
		be Frappe's missing-method rejection, whatever its text happens to
		say - is_method_not_found excludes on code/exc_type before ever
		reading the message."""
		exc = AdminValidationError(
			"Failed to get method for command x with y", exc_type="DuplicateEntryError"
		)
		self.assertFalse(admin_client.is_method_not_found(exc))


class TestPermanentRejectionClassification(FrappeTestCase):
	"""jarvis #542: 502 is admin's answer BOTH to a gateway fault AND to its
	fleet layer permanently refusing the config (@fleet_endpoint answers every
	FleetError with 502 + error.code = the raising class's name). Only the
	second is terminal, and that code is the only thing telling them apart.

	Deliberately drives a FAKE settings doc (the same seam
	test_api_key_decrypted_via_get_password_not_attribute uses) instead of the
	_settings_for_admin / _settings_clear_admin fixture: these tests need no
	real state, and that fixture COMMITS - its teardown removes the site's
	stored admin passwords from __Auth, outside FrappeTestCase's rollback. A
	classification test must not be able to take a bench's admin credentials
	with it."""

	def _fake_settings(self):
		fake = MagicMock()
		fake.jarvis_admin_url = "https://admin.example.com"
		# No OAuth password stored -> _admin_access_token short-circuits and the
		# call goes out on the legacy api_key:api_secret header.
		fake.jarvis_admin_customer_email = ""

		def _get_password(field, raise_exception=False):
			return {
				"jarvis_admin_api_key": "fake-key",
				"jarvis_admin_api_secret": "fake-secret",
			}.get(field, "")

		fake.get_password.side_effect = _get_password
		return fake

	def _post_creds(self, response):
		"""Run one creds push against a canned admin response, touching no DB."""
		with (
			patch("frappe.get_single", return_value=self._fake_settings()),
			patch("requests.post", MagicMock(return_value=response)),
		):
			post_update_llm_creds("p", "m", "b", "k")

	def test_502_with_permanent_rejection_code_raises_rejected(self):
		with self.assertRaises(admin_client.AdminRejectedError) as cm:
			self._post_creds(
				_mock_response(
					502,
					json_body={
						"message": {
							"ok": False,
							"error": {
								"code": "FleetConfigError",
								"message": "unknown llm_provider: 'gemini'",
							},
						}
					},
				)
			)
		self.assertEqual(cm.exception.code, "FleetConfigError")
		# detail is admin's message ALONE - without the "admin returned a 502
		# error: " wrapper the message carries - so a caller can put it in front
		# of a customer without parsing it back out.
		self.assertEqual(cm.exception.detail, "unknown llm_provider: 'gemini'")
		self.assertIn("unknown llm_provider", str(cm.exception))

	def test_rejected_error_is_an_unreachable_subclass(self):
		"""Every pre-existing catch site keys off AdminUnreachableError and
		already records a terminal failure, so inheriting leaves all of them
		correct without touching one. Only the two sites that WAIT on a
		reconcile branch on the subclass."""
		self.assertTrue(issubclass(admin_client.AdminRejectedError, AdminUnreachableError))

	def test_502_with_unrecognised_code_stays_unreachable(self):
		"""Allowlist, not denylist: a fleet error class we have not classified
		(a health-check timeout here) keeps the optimistic converge/pending
		handling, so an unclassified code can only ever be too patient."""
		with self.assertRaises(AdminUnreachableError) as cm:
			self._post_creds(
				_mock_response(
					502,
					json_body={
						"message": {
							"ok": False,
							"error": {"code": "HealthCheckError", "message": "healthz timed out"},
						}
					},
				)
			)
		self.assertNotIsInstance(cm.exception, admin_client.AdminRejectedError)

	def test_non_json_502_stays_unreachable(self):
		"""A real gateway fault (an nginx HTML 502) carries no envelope at all,
		so it can never be read as a refusal."""
		with self.assertRaises(AdminUnreachableError) as cm:
			self._post_creds(_mock_response(502, json_body=None, text="<html>502 Bad Gateway</html>"))
		self.assertNotIsInstance(cm.exception, admin_client.AdminRejectedError)

	def test_200_ok_false_with_permanent_code_raises_rejected(self):
		"""The rare inlined-failure envelope gets the same classification, so an
		endpoint that reports its refusal at 200 is not the one shape that can
		still strand a tenant in "pending"."""
		with self.assertRaises(admin_client.AdminRejectedError) as cm:
			self._post_creds(
				_mock_response(
					200,
					json_body={
						"message": {
							"ok": False,
							"error": {
								"code": "PoolSpecRejected",
								"message": "Your subscription cannot serve this.",
							},
						}
					},
				)
			)
		self.assertEqual(cm.exception.code, "PoolSpecRejected")
		self.assertEqual(cm.exception.detail, "Your subscription cannot serve this.")


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

		settings = _admin_settings()
		settings.db_set("jarvis_admin_url", "https://admin.example.com")
		settings.db_set("jarvis_admin_api_key", "")
		settings.db_set("jarvis_admin_api_secret", "")
		try:
			with self.assertRaises(AdminAuthError):
				post_update_llm_creds("p", "m", "b", "k")
		finally:
			_settings_clear_admin()

	def test_no_secret_raises_auth_error(self):
		from jarvis.exceptions import AdminAuthError

		settings = _admin_settings()
		settings.db_set("jarvis_admin_url", "https://admin.example.com")
		settings.db_set("jarvis_admin_api_key", "some-key")
		settings.db_set("jarvis_admin_api_secret", "")
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

	def test_renew_sends_target_plan_only_when_given(self):
		_settings_for_admin(api_key="renew-key", api_secret="renew-secret")
		captured_with = {}
		captured_without = {}

		def _fake_post_with(url, json=None, headers=None, timeout=None):
			captured_with["json"] = json
			return _mock_response(
				200, json_body={"message": {"ok": True, "data": {"razorpay_order_id": "order_R"}}}
			)

		def _fake_post_without(url, json=None, headers=None, timeout=None):
			captured_without["json"] = json
			return _mock_response(
				200, json_body={"message": {"ok": True, "data": {"razorpay_order_id": "order_R"}}}
			)

		with patch("requests.post", side_effect=_fake_post_with):
			admin_client.renew(target_plan="Growth")
		self.assertEqual(captured_with["json"]["target_plan"], "Growth")

		with patch("requests.post", side_effect=_fake_post_without):
			admin_client.renew()
		self.assertNotIn("target_plan", captured_without["json"])

	def test_get_billing_payment_state_posts_empty_body(self):
		_settings_for_admin()
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["url"] = url
			captured["json"] = json
			return _mock_response(
				200, json_body={"message": {"ok": True, "data": {"code": "PAYMENT_ACTIVE"}}}
			)

		with patch("requests.post", side_effect=_fake_post):
			admin_client.get_billing_payment_state()
		self.assertIn(admin_client._m("api.account.get_billing_payment_state"), captured["url"])
		self.assertEqual(captured["json"], {})

	def test_check_billing_payment_status_posts_empty_body(self):
		_settings_for_admin()
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["url"] = url
			captured["json"] = json
			return _mock_response(
				200, json_body={"message": {"ok": True, "data": {"code": "PAYMENT_ACTIVE"}}}
			)

		with patch("requests.post", side_effect=_fake_post):
			admin_client.check_billing_payment_status()
		self.assertIn(admin_client._m("api.account.check_billing_payment_status"), captured["url"])
		self.assertEqual(captured["json"], {})

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


_EXPECTED_ADVERT = {
	"pay_page": "admin_pay_page_v1",
	"provider_shapes": [
		"razorpay_order",
		"razorpay_mandate",
		"cashfree_order",
		"cashfree_mandate",
	],
}


class TestClientCapabilityAdvert(FrappeTestCase):
	"""Plan-09 WS2: behavior-neutral pay-page capability advert.

	The bench advertises ``admin_pay_page_v1`` + the provider checkout shapes it
	understands on every signup-payment initiation/resume call. Today's admin
	ignores the field (Frappe drops unknown form_dict keys), so this is purely
	outbound predeployment — these tests pin (a) the advert rides both seams,
	(b) the frozen contract names are exact, (c) a reply carrying no new keys
	decodes byte-for-byte as it did before the advert existed.
	"""

	def tearDown(self):
		_settings_clear_admin()

	def test_advert_constant_matches_frozen_contract(self):
		self.assertEqual(admin_client.PAY_PAGE_CAPABILITY, "admin_pay_page_v1")
		self.assertEqual(
			list(admin_client.PROVIDER_SHAPES),
			["razorpay_order", "razorpay_mandate", "cashfree_order", "cashfree_mandate"],
		)
		self.assertEqual(admin_client._client_capabilities(), _EXPECTED_ADVERT)

	def test_helper_returns_a_fresh_copy(self):
		# A caller mutating the returned advert (it becomes a request body) must
		# not corrupt the next call's advert.
		first = admin_client._client_capabilities()
		first["pay_page"] = "tampered"
		first["provider_shapes"].append("tampered")
		self.assertEqual(admin_client._client_capabilities(), _EXPECTED_ADVERT)

	def test_signup_carries_capability_advert(self):
		_settings_clear_admin()  # guest signup path
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["json"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {}}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.signup("e@x.com", "Co", "Annual Plan")
		self.assertEqual(captured["json"]["client_capabilities"], _EXPECTED_ADVERT)

	def test_resume_pending_signup_carries_capability_advert(self):
		_settings_for_admin()  # authenticated resume path
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["json"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {}}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.resume_pending_signup("Annual Plan")
		self.assertEqual(captured["json"]["client_capabilities"], _EXPECTED_ADVERT)

	def test_response_without_new_keys_decodes_unchanged(self):
		# Behavior neutrality: an admin that ignores the advert replies exactly as
		# before, and the decoded result is the same dict the bench saw pre-WS2.
		# The advert is outbound-only; it never touches the reply path.
		_settings_clear_admin()
		legacy_data = {
			"api_key": "k",
			"api_secret": "s",
			"payment_provider": "razorpay",
			"razorpay_order_id": "order_R",
			"razorpay_key_id": "rzp",
			"amount_inr": 1500,
		}

		def _fake_post(url, json=None, headers=None, timeout=None):
			return _mock_response(200, json_body={"message": {"ok": True, "data": dict(legacy_data)}})

		with patch("requests.post", side_effect=_fake_post):
			out = admin_client.signup("e@x.com", "Co", "Annual Plan")
		self.assertEqual(out, legacy_data)

	def test_signup_forwards_partner_code_when_given(self):
		_settings_clear_admin()  # guest signup path
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["json"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {}}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.signup("e@x.com", "Co", "Annual Plan", partner_code="PARTNER-1")
		self.assertEqual(captured["json"]["partner_code"], "PARTNER-1")

	def test_signup_omits_partner_code_when_not_given(self):
		# An older admin ignoring the kwarg is fine; the bench must never SEND an
		# empty one - that would be a real (if blank) value, not "not provided".
		_settings_clear_admin()
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["json"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {}}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.signup("e@x.com", "Co", "Annual Plan")
		self.assertNotIn("partner_code", captured["json"])

	def test_resume_pending_signup_forwards_partner_code_when_given(self):
		_settings_for_admin()  # authenticated resume path
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["json"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {}}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.resume_pending_signup("Annual Plan", partner_code="PARTNER-1")
		self.assertEqual(captured["json"]["partner_code"], "PARTNER-1")

	def test_resume_pending_signup_omits_partner_code_when_not_given(self):
		_settings_for_admin()
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["json"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {}}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.resume_pending_signup("Annual Plan")
		self.assertNotIn("partner_code", captured["json"])

	def test_partner_code_rejection_is_surfaced_as_a_details_error(self):
		"""`PartnerCodeRejected` is a ValidationError SUBCLASS admin raises for an
		unknown or inactive partner code, before it built any provider object. It
		used to fall through admin_client's exc_type allowlist into the unknown
		branch and (via onboarding_contract) render as BENCH_ADMIN_REJECTED, "the
		payment service refused this request" - false in every particular, since no
		payment service was ever contacted. It must classify like a bad GSTIN: a
		details-rejection the customer can actually fix."""
		_settings_clear_admin()  # guest signup path
		mock_post = MagicMock(
			return_value=_mock_response(
				417,
				json_body={
					"exception": "jarvis_admin.exceptions.PartnerCodeRejected: Unknown partner code: ACME-2026",
					"exc_type": "PartnerCodeRejected",
					"_server_messages": '["{\\"message\\": \\"Unknown partner code: ACME-2026\\", \\"indicator\\": \\"red\\"}"]',
				},
			)
		)
		with patch("requests.post", mock_post):
			with self.assertRaises(AdminValidationError) as cm:
				admin_client.signup("e@x.com", "Co", "Annual Plan", partner_code="ACME-2026")
		self.assertEqual(str(cm.exception), "Unknown partner code: ACME-2026")

		from jarvis import onboarding_contract

		error, status = onboarding_contract.error_object(cm.exception)
		self.assertEqual(error["code"], onboarding_contract.BENCH_SIGNUP_DETAILS_REJECTED)
		self.assertNotEqual(error["code"], onboarding_contract.BENCH_ADMIN_REJECTED)
		self.assertEqual(status, 409)
		self.assertEqual(error["message"], "Unknown partner code: ACME-2026")

	def test_billing_facades_carry_capability_advert(self):
		"""plan-09 P0-2: the capability advert now rides EVERY billing initiation call,
		not just signup/resume, because admin's billing facades gate on it too. Without
		this, a cutover admin refuses every renew/upgrade/downgrade/reauthorize with
		CLIENT_UPGRADE_REQUIRED. (Bench-first deploy: this advert must ship before admin
		hard-gates.)"""
		_settings_for_admin()  # authenticated billing calls
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["json"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {}}})

		calls = [
			("renew", lambda: admin_client.renew()),
			("start_upgrade", lambda: admin_client.start_upgrade("Pro Plan")),
			("reauthorize_autopay", lambda: admin_client.reauthorize_autopay()),
			("start_downgrade", lambda: admin_client.start_downgrade("Basic Plan")),
			("cancel_scheduled_downgrade", lambda: admin_client.cancel_scheduled_downgrade()),
		]
		for name, call in calls:
			with self.subTest(call=name):
				captured.clear()
				with patch("requests.post", side_effect=_fake_post):
					call()
				self.assertEqual(captured["json"].get("client_capabilities"), _EXPECTED_ADVERT, name)


@contextlib.contextmanager
def _fake_request(*, host: str, user: str = "Administrator"):
	"""Minimal request + session so get_url's allow_header_override path reads a Host.
	Restores frappe.local.request + the session user (determinism)."""
	sentinel = object()
	prev_request = getattr(frappe.local, "request", sentinel)
	prev_user = frappe.session.user
	frappe.local.request = SimpleNamespace(host=host, headers={})
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(prev_user)
		if prev_request is sentinel:
			try:
				del frappe.local.request
			except AttributeError:
				pass
		else:
			frappe.local.request = prev_request


class TestPublicOriginSweep(FrappeTestCase):
	"""plan-09 P1-5 + plan 2026-08-17: the ``frappe_site_url`` the bench hands admin.
	Non-signup callers must NOT derive it from the request ``Host`` (a spoofed Host
	could choose the recorded site URL). ONLY the signup flow (``trust_host=True``)
	trusts the authenticated Host, forcing https for a real public domain; a configured
	``host_name`` still wins for everyone."""

	def tearDown(self):
		_settings_clear_admin()
		for k in ("host_name", "hostname", "restart_supervisor_on_update", "restart_systemd_on_update"):
			frappe.conf.pop(k, None)

	def test_public_origin_prefers_configured_host_name(self):
		frappe.conf["host_name"] = "https://tenant.example.com"
		self.assertEqual(admin_client._public_origin(), "https://tenant.example.com")

	def test_public_origin_normalizes_a_bare_host_name_to_https(self):
		frappe.conf["host_name"] = "tenant.example.com"
		self.assertEqual(admin_client._public_origin(), "https://tenant.example.com")

	def test_public_origin_falls_back_with_header_override_OFF(self):
		# No host_name → the get_url fallback MUST be called with
		# allow_header_override=False (the load-bearing anti-injection fix).
		frappe.conf.pop("host_name", None)
		frappe.conf.pop("hostname", None)
		with patch("frappe.utils.get_url", return_value="https://fallback.example.com") as gu:
			out = admin_client._public_origin()
		gu.assert_called_once_with(allow_header_override=False)
		self.assertEqual(out, "https://fallback.example.com")

	def test_http_fallback_on_a_production_bench_warns_but_does_not_throw(self):
		frappe.conf.pop("host_name", None)
		frappe.conf.pop("hostname", None)
		frappe.conf["restart_supervisor_on_update"] = 1  # marks a production bench
		with patch("frappe.utils.get_url", return_value="http://dev.local"):
			# Must NOT raise (would break signup on an unconfigured bench).
			out = admin_client._public_origin()
		self.assertEqual(out, "http://dev.local")

	def test_signup_sends_the_swept_origin_never_the_raw_header(self):
		_settings_clear_admin()  # guest signup path
		frappe.conf["host_name"] = "https://tenant.example.com"
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["json"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {}}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.signup("e@x.com", "Co", "Annual Plan")
		self.assertEqual(captured["json"]["frappe_site_url"], "https://tenant.example.com")

	def test_reconnect_and_replacement_seams_use_the_swept_origin(self):
		_settings_clear_admin()
		frappe.conf["host_name"] = "https://tenant.example.com"
		seen = []

		def _fake_post(url, json=None, headers=None, timeout=None):
			seen.append(json.get("frappe_site_url"))
			return _mock_response(200, json_body={"message": {"ok": True, "data": {}}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.site_replacement()
			admin_client.request_account_reconnect("e@x.com", "Co")
			# redeem is guest-callable and hands over a workspace: the site URL MUST
			# be server-derived, never taken from the customer, or the code could be
			# redeemed onto a site the attacker doesn't control.
			admin_client.redeem_reconnect_code("ABCD2345", "e@x.com")
		self.assertEqual(seen, ["https://tenant.example.com"] * 3)

	def test_redeem_forwards_code_and_email(self):
		_settings_clear_admin()
		frappe.conf["host_name"] = "https://tenant.example.com"
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["url"] = url
			captured["json"] = json
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"status": "invalid"}}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.redeem_reconnect_code("ABCD2345", "e@x.com")
		self.assertTrue(captured["url"].endswith("billing.reconnect.redeem_reconnect_code"))
		self.assertEqual(captured["json"]["code"], "ABCD2345")
		self.assertEqual(captured["json"]["email"], "e@x.com")

	# --- signup-flow Host capture (plan 2026-08-17) ---

	def _pop_host(self):
		frappe.conf.pop("host_name", None)
		frappe.conf.pop("hostname", None)

	def test_signup_flow_call_uses_header_override_ON(self):
		# trust_host=True (signup only) flips get_url to allow_header_override=True.
		self._pop_host()
		with patch("frappe.utils.get_url", return_value="https://x.example.com") as gu:
			admin_client._public_origin(trust_host=True)
		gu.assert_called_once_with(allow_header_override=True)

	def test_signup_forces_https_on_an_http_real_domain(self):
		# A real domain reached over http (proxy without XF-Proto) -> https. Mutation: drop the
		# normalization -> stays http -> reds.
		self._pop_host()
		with patch("frappe.utils.get_url", return_value="http://staging-fleet.klerk.in"):
			out = admin_client._public_origin(trust_host=True)
		self.assertEqual(out, "https://staging-fleet.klerk.in")

	def test_signup_keeps_dev_local_host_unchanged(self):
		# A dev .local bench (reserved TLD) must NOT be https-forced / port-dropped. Mutation:
		# drop the reserved-TLD / isalpha guard -> normalizes -> reds.
		self._pop_host()
		with patch("frappe.utils.get_url", return_value="http://jarvis.local:8002"):
			out = admin_client._public_origin(trust_host=True)
		self.assertEqual(out, "http://jarvis.local:8002")

	def test_signup_does_not_normalize_a_dotted_ip(self):
		# A dotted IP literal is not a public domain. Double-guarded by design: the ipaddress check
		# AND the numeric-last-label isalpha check both reject it (defense-in-depth).
		self._pop_host()
		with patch("frappe.utils.get_url", return_value="http://192.168.1.5:8000"):
			out = admin_client._public_origin(trust_host=True)
		self.assertEqual(out, "http://192.168.1.5:8000")

	def test_signup_does_not_normalize_an_internal_site_name(self):
		# staging.v15 - non-alphabetic TLD (v15) - is an internal site name, not a public domain.
		self._pop_host()
		with patch("frappe.utils.get_url", return_value="http://staging.v15"):
			out = admin_client._public_origin(trust_host=True)
		self.assertEqual(out, "http://staging.v15")

	def test_signup_malformed_host_does_not_raise(self):
		# A get_url value whose netloc breaks urlsplit must never 500 signup.
		self._pop_host()
		with patch("frappe.utils.get_url", return_value="http://[oops"):
			out = admin_client._public_origin(trust_host=True)  # must not raise
		self.assertEqual(out, "http://[oops")

	def test_default_ignores_a_spoofed_host_even_when_authenticated(self):
		# SECURITY (real get_url): the DEFAULT (non-signup) path must NOT trust a spoofed Host,
		# even under an authenticated session. Mutation: default trust_host to True -> reds.
		self._pop_host()
		with _fake_request(host="evil.attacker.com", user="Administrator"):
			out = admin_client._public_origin()  # default trust_host=False
		self.assertNotIn("evil.attacker.com", out)

	def test_signup_captures_the_authenticated_host_end_to_end(self):
		# End-to-end (real get_url): trust_host=True derives the customer's real host from the
		# request and forces https for a real domain.
		self._pop_host()
		with _fake_request(host="staging-fleet.klerk.in", user="Administrator"):
			out = admin_client._public_origin(trust_host=True)
		self.assertEqual(out, "https://staging-fleet.klerk.in")

	def test_configured_host_name_wins_over_a_spoofed_host_on_signup(self):
		# host_name must win even on the trust_host=True path (early return before the Host is
		# consulted). Guards against a future reorder that lets a spoofed Host beat host_name.
		frappe.conf["host_name"] = "https://tenant.example.com"
		with _fake_request(host="evil.attacker.com", user="Administrator"):
			out = admin_client._public_origin(trust_host=True)
		self.assertEqual(out, "https://tenant.example.com")


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


class TestPostSubscriptionConnect(FrappeTestCase):
	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_happy_path_posts_combined_payload(self):
		captured = {}
		blob = {
			"type": "oauth",
			"provider": "openai",
			"access": "AT-fresh",
			"refresh": "RT-fresh",
		}

		def _fake_post(url, json=None, timeout=None, **_kw):
			captured["url"] = url
			captured["body"] = json
			captured["timeout"] = timeout
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"action": "restart"}}})

		with patch("requests.post", side_effect=_fake_post):
			result = admin_client.post_subscription_connect(
				"openai",
				blob,
				"OpenAI",
				model="gpt-5.5",
				base_url="",
			)
		self.assertEqual(result, {"action": "restart"})
		self.assertIn("subscription_connect", captured["url"])
		# Admin's subscription_connect does not declare api_key or auth_mode
		# (an oauth-only endpoint has no other mode to name) - neither is sent.
		self.assertEqual(
			captured["body"],
			{
				"provider": "openai",
				"blob": blob,
				"llm_provider": "OpenAI",
				"model": "gpt-5.5",
				"base_url": "",
				"installed_apps": frappe.get_installed_apps(),
			},
		)
		# Must exceed the admin->fleet leg's own 210s budget (each layer
		# exceeds the one below it, same rule PR#826 established) so the
		# bench never gives up on an apply admin is still driving.
		self.assertGreater(
			captured["timeout"],
			210,
			"post_subscription_connect timeout must outlast admin's own admin->fleet budget",
		)


class TestPostSubscriptionDisconnect(FrappeTestCase):
	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_happy_path(self):
		captured = {}

		def _fake_post(url, json=None, timeout=None, **_kw):
			captured["url"] = url
			captured["body"] = json
			captured["timeout"] = timeout
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"ok": True}}})

		with patch("requests.post", side_effect=_fake_post):
			result = admin_client.post_subscription_disconnect()
		self.assertEqual(result, {"ok": True})
		self.assertIn("subscription_disconnect", captured["url"])
		self.assertEqual(captured["body"], {})
		# This lands on admin's DELETE /auth-profile, whose own agent bound is
		# 150s and which runs doctor + restart inside it. The shared 150s left no
		# headroom for the HTTPS round trip on top, so the bench could give up on
		# a call admin was still serving.
		self.assertGreater(
			captured["timeout"],
			150,
			"must leave headroom over admin's own 150s delete_auth_profile bound",
		)


class TestPostDisconnectLlm(FrappeTestCase):
	def setUp(self):
		_settings_for_admin()

	def tearDown(self):
		_settings_clear_admin()

	def test_timeout_outlasts_admins_own_budget(self):
		"""Same rule as post_push_oauth_blob above, which the disconnect missed.

		The rationale (and the invariant this bound encodes) lives once, on
		``admin_client._DISCONNECT_TIMEOUT_S``. This asserts two separate things:
		that the constant is actually PLUMBED to requests, and that it still
		clears admin's worst-case budget -- so bumping the constant alone cannot
		silently drop back under it.
		"""
		captured = {}

		def _fake_post(url, json=None, headers=None, timeout=None):
			captured["url"] = url
			captured["timeout"] = timeout
			return _mock_response(200, json_body={"message": {"ok": True, "data": {"ok": True}}})

		with patch("requests.post", side_effect=_fake_post):
			admin_client.post_disconnect_llm()

		self.assertIn("disconnect_llm", captured["url"])
		self.assertEqual(
			captured["timeout"],
			admin_client._DISCONNECT_TIMEOUT_S,
			"the dedicated budget must actually reach requests, not just be declared",
		)
		self.assertGreater(
			captured["timeout"],
			210,
			"must outlast admin's provision_healthz_timeout_s (180) + its own 30s headroom, "
			"or a slow-but-successful disconnect is reported to the customer as unreachable",
		)


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


class TestFixturesCannotDestroySiteCredentials(FrappeTestCase):
	"""jarvis #566: pins the safety property of this module's own fixtures.

	They previously committed writes to the real Jarvis Settings Single and
	dropped three of its Password fields from __Auth on teardown, which took
	site.jarvis's control-plane credentials on 2026-07-30. The damage was silent
	- the teardown restored nothing and the failures surfaced much later, in
	admin calls with no visible link back to a test run - so the guard against
	it needs to be an assertion rather than a comment.
	"""

	def tearDown(self):
		_ADMIN_SETTINGS.reset()

	def test_no_fixture_removes_an_encrypted_password(self):
		"""The exact operation from the incident. Patched at its definition site
		so a lazy `from frappe.utils.password import ...` inside a helper is
		caught too."""
		with patch("frappe.utils.password.remove_encrypted_password") as removed:
			_settings_for_admin()
			_settings_for_oauth()
			_settings_clear_admin()
		removed.assert_not_called()

	def test_the_module_resolves_jarvis_settings_to_the_fake(self):
		self.assertIs(frappe.get_single("Jarvis Settings"), _ADMIN_SETTINGS)

	def test_other_doctypes_still_resolve_normally(self):
		"""The patch is scoped by doctype, so it cannot quietly change behaviour
		for anything this module did not intend to fake."""
		self.assertIsNot(frappe.get_single("System Settings"), _ADMIN_SETTINGS)

	def test_a_cleared_credential_reads_as_cleared(self):
		"""What the destructive teardown was actually FOR. On the real doc,
		blanking the column made get_password fall through to __Auth and hand
		back the site's own secret, so "not onboarded" tests could only be made
		honest by deleting it. With no encrypted store behind the fake, blank is
		simply blank."""
		_settings_for_admin(api_key="fixture-key", api_secret="fixture-secret")
		_settings_clear_admin()
		settings = _admin_settings()
		for field in ("jarvis_admin_api_key", "jarvis_admin_api_secret", "jarvis_admin_customer_password"):
			self.assertEqual(settings.get_password(field, raise_exception=False), "")

	def test_a_fixture_refuses_to_run_against_the_real_single(self):
		"""A runner that skipped setUpModule would otherwise send every helper
		straight back at the site's own settings, silently."""
		with patch("frappe.get_single", _REAL_GET_SINGLE):
			with self.assertRaises(RuntimeError):
				_settings_clear_admin()
