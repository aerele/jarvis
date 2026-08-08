"""Role-gate tests for Settings-mutating whitelisted endpoints.

Sprint-1 Important from the 2026-06-16 code review (#7, #6, #9). Every
endpoint that writes Jarvis Settings, initiates billing, or starts an
OAuth flow MUST refuse a non-System-Manager caller. Without these gates,
a low-role staff user on the customer's Frappe site could flip
``llm_base_url`` to an attacker URL, complete a peer's pending OAuth, or
initiate a paid signup under the site admin's contract.

We don't create a portal-tier User row (which trips Frappe's global
search assertion under FrappeTestCase). Instead we switch the session
user to ``Guest`` (built-in, no roles other than All/Guest). That's
what ``frappe.only_for`` consults; Guest is sufficient to prove the
gate fires.
"""

import time

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import oauth as _oauth_pkg
from jarvis.oauth.api import _CACHE_KEY

# (module path, callable name, kwargs).
GATED_ENDPOINTS = [
	("jarvis.onboarding", "sync_connection", {}),
	("jarvis.onboarding", "start_signup", {"email": "x@y.test", "company": "C", "plan": "p"}),
	("jarvis.onboarding", "check_signup_payment_state", {}),
	("jarvis.onboarding", "finish_payment", {"payload": {}}),
	("jarvis.onboarding", "renew", {}),
	("jarvis.onboarding", "save_llm_creds", {"provider": "OpenAI", "model": "gpt-4o", "api_key": "k"}),
	("jarvis.onboarding", "save_llm_pool", {"models": "[]"}),
	("jarvis.onboarding", "get_llm_config", {}),
	("jarvis.onboarding", "get_account_defaults", {}),
	("jarvis.account", "start_upgrade", {"target_plan": "team_monthly"}),
	# The read half of the same billing surface. Both were whitelisted but
	# ungated: the settings rail and the desk page's roles=["System Manager"]
	# are presentation, and neither constrains a direct /api/method call.
	("jarvis.account", "get_account", {}),
	("jarvis.account", "preview_upgrade", {"target_plan": "team_monthly"}),
	# Billing payment-recovery surface: the passive poll re-echoes a live pay-page
	# token and the check converges provider truth, so an ungated caller could read
	# another workspace's checkout handle or spend its gateway budget.
	("jarvis.account", "get_billing_payment_state", {}),
	("jarvis.account", "check_billing_payment_status", {}),
	("jarvis.account", "get_llm_usage", {}),
	("jarvis.account", "get_llm_connection_status", {}),
	("jarvis.oauth.api", "begin_paste_signin", {"provider": "OpenAI", "model": "gpt-5.5"}),
	(
		"jarvis.oauth.api",
		"complete_paste_signin",
		{"nonce": "x" * 48, "redirected_url": "http://localhost:1455/auth/callback?code=A&state=B"},
	),
	# Pool OAuth-flow endpoints — start/complete a subscription-account capture.
	# Must refuse a non-System-Manager caller (they return account credentials).
	# #200 review #10: previously uncovered, so a dropped only_for would slip through.
	("jarvis.oauth.api", "begin_pool_account_signin", {"provider": "OpenAI", "model": "gpt-5.5"}),
	(
		"jarvis.oauth.api",
		"complete_pool_account_signin",
		{"nonce": "x" * 48, "redirected_url": "http://localhost:1455/auth/callback?code=A&state=B"},
	),
	("jarvis.oauth.api", "disconnect", {}),
	# Tenant-admin usage endpoints (design section 4) — gated by
	# require_jarvis_admin. A non-admin caller must be refused; admin_set_user_limit
	# needs an existing user so Administrator gets past the gate into the body
	# (Administrator always exists) rather than tripping the unknown_user guard.
	("jarvis.chat.user_settings_api", "admin_list_user_usage", {}),
	(
		"jarvis.chat.user_settings_api",
		"admin_set_user_limit",
		{"user": "Administrator", "monthly_token_limit": 0},
	),
	(
		"jarvis.chat.user_settings_api",
		"admin_set_user_model_limit",
		{"user": "Administrator", "model": "gpt-4o", "monthly_token_limit": 0},
	),
	("jarvis.chat.user_settings_api", "admin_sync_usage", {}),
	# Owner-level theme setter — gated by require_jarvis_access (writes the
	# caller's own User.desk_theme). A Guest (no Jarvis access) must be refused.
	("jarvis.chat.user_settings_api", "set_user_theme", {"theme": "dark"}),
]


def _resolve(module_path: str, name: str):
	mod = frappe.get_module(module_path)
	return getattr(mod, name)


class TestRoleGates(FrappeTestCase):
	def test_administrator_passes_role_check(self):
		"""Smoke: Administrator must get past the role check on every gated
		endpoint. We can't fully run them (args are bogus on purpose),
		but anything that gets past the gate into the function body
		counts. We just assert the failure isn't frappe.PermissionError.
		"""
		frappe.set_user("Administrator")
		try:
			for module_path, name, kwargs in GATED_ENDPOINTS:
				fn = _resolve(module_path, name)
				try:
					fn(**kwargs)
				except frappe.PermissionError as e:
					self.fail(f"{module_path}.{name} raised PermissionError for Administrator: {e}")
				except Exception:
					pass
		finally:
			frappe.set_user("Administrator")

	def test_guest_blocked_on_all_gated_endpoints(self):
		"""Every gated endpoint must raise frappe.PermissionError when the
		caller doesn't have System Manager (here: Guest)."""
		original = frappe.session.user
		try:
			frappe.set_user("Guest")
			for module_path, name, kwargs in GATED_ENDPOINTS:
				fn = _resolve(module_path, name)
				with self.assertRaises(
					frappe.PermissionError,
					msg=f"{module_path}.{name} did not refuse Guest",
				):
					fn(**kwargs)
		finally:
			frappe.set_user(original)


class TestOAuthNoncePerUserBinding(FrappeTestCase):
	"""Sprint-1 #9: the cache entry remembers which user began the
	sign-in. A second System Manager on the same site must NOT be able
	to complete an in-flight peer sign-in.
	"""

	def setUp(self):
		# Seed a fresh nonce tied to a specific originator. We don't go
		# through begin_paste_signin (which requires real OAuth setup);
		# we write the cache entry directly with the contract shape.
		self.nonce = "nuser_" + ("x" * 42)
		self.originator = "originator@example.com"
		frappe.cache.hset(
			_CACHE_KEY,
			self.nonce,
			{
				"provider": "OpenAI",
				"model": "gpt-5.5",
				"status": "pending",
				"expires_at_ts": int(time.time()) + 600,
				"verifier": "v",
				"state": "s",
				"originator_user": self.originator,
			},
		)
		self.addCleanup(lambda: frappe.cache.hdel(_CACHE_KEY, self.nonce))

	def test_different_user_gets_unknown_nonce(self):
		"""When a System Manager OTHER than the originator calls
		complete_paste_signin, they get the same opaque ``unknown_nonce``
		response as if the nonce didn't exist - we don't leak that the
		nonce IS live, just under different ownership.
		"""
		from jarvis.oauth.api import complete_paste_signin

		# Run as Administrator (not the originator). Administrator
		# bypasses frappe.only_for so the role check doesn't fire first.
		frappe.set_user("Administrator")
		try:
			out = complete_paste_signin(
				nonce=self.nonce,
				redirected_url="http://localhost:1455/auth/callback?code=A&state=s",
			)
		finally:
			frappe.set_user("Administrator")

		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "unknown_nonce")


class TestOAuthErrorSanitization(FrappeTestCase):
	"""Sprint-1 #6: provider error_description must NOT bubble verbatim
	through token_exchange_failed. The opaque code mapping collapses
	``invalid_client`` and ``invalid_grant`` into different user-safe
	buckets; raw provider text never reaches the wire.
	"""

	def test_invalid_grant_maps_to_opaque_code_invalid(self):
		from jarvis.oauth import api as oauth_api

		class _FakeResp:
			ok = False
			status_code = 400
			text = '{"error":"invalid_grant","error_description":"code is expired"}'

			def json(self):
				return {"error": "invalid_grant", "error_description": "code is expired"}

		with (
			self.assertRaises(oauth_api.TokenExchangeError) as ctx,
			frappe.utils.patches.patch("jarvis.oauth.api.requests.post", return_value=_FakeResp())
			if hasattr(frappe.utils, "patches")
			else _NoopCtx(),
		):
			# Fall through to the standard unittest.mock if the patches
			# helper isn't available.
			from unittest.mock import patch

			with (
				patch("jarvis.oauth.api.requests.post", return_value=_FakeResp()),
				patch(
					"jarvis.oauth.api.get_provider",
					return_value={
						"client_id": "cid",
						"token": "https://example/token",
						"client_secret": None,
					},
				),
				patch("jarvis.oauth.api.frappe.log_error"),
			):
				oauth_api._exchange_code(
					provider="OpenAI",
					code="ABC",
					code_verifier="V",
				)

		err = ctx.exception
		self.assertEqual(err.code, "code_invalid")
		# Critically: the raw "code is expired" string is NOT in the
		# user-facing message.
		self.assertNotIn("code is expired", str(err))
		self.assertNotIn("invalid_grant", str(err))

	def test_invalid_client_maps_to_opaque_auth_failed(self):
		"""``invalid_client`` is the oracle target - a provider returning
		it on missing/wrong client_secret would leak whether the secret
		is needed at all. Map to a generic 'auth_failed' bucket.
		"""
		from unittest.mock import patch

		from jarvis.oauth import api as oauth_api

		class _FakeResp:
			ok = False
			status_code = 401
			text = '{"error":"invalid_client","error_description":"client_secret required"}'

			def json(self):
				return {"error": "invalid_client", "error_description": "client_secret required"}

		with (
			patch("jarvis.oauth.api.requests.post", return_value=_FakeResp()),
			patch(
				"jarvis.oauth.api.get_provider",
				return_value={
					"client_id": "cid",
					"token": "https://example/token",
					"client_secret": None,
				},
			),
			patch("jarvis.oauth.api.frappe.log_error"),
		):
			with self.assertRaises(oauth_api.TokenExchangeError) as ctx:
				oauth_api._exchange_code(
					provider="OpenAI",
					code="ABC",
					code_verifier="V",
				)

		err = ctx.exception
		self.assertEqual(err.code, "auth_failed")
		self.assertNotIn("client_secret", str(err))
		self.assertNotIn("invalid_client", str(err))

	def test_network_error_maps_to_opaque_code(self):
		from unittest.mock import patch

		import requests

		from jarvis.oauth import api as oauth_api

		with (
			patch("jarvis.oauth.api.requests.post", side_effect=requests.ConnectionError("DNS failed")),
			patch(
				"jarvis.oauth.api.get_provider",
				return_value={
					"client_id": "cid",
					"token": "https://example/token",
					"client_secret": None,
				},
			),
			patch("jarvis.oauth.api.frappe.log_error"),
		):
			with self.assertRaises(oauth_api.TokenExchangeError) as ctx:
				oauth_api._exchange_code(
					provider="OpenAI",
					code="ABC",
					code_verifier="V",
				)

		err = ctx.exception
		self.assertEqual(err.code, "network_error")
		# Real underlying error stays out of the wire message.
		self.assertNotIn("DNS failed", str(err))


class _NoopCtx:
	def __enter__(self):
		return None

	def __exit__(self, *a):
		return False
