"""REV-3 OAuth API tests. Bench owns the full OAuth flow (PKCE gen +
token exchange + blob push). Customer's laptop just hosts a browser
session that pastes the redirected URL back."""

import base64
import json
import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.oauth import api as oauth_api


def _jwt(payload: dict) -> str:
	"""Build a fake JWT for tests. Header + signature are bogus; the
	bench never verifies the signature - TLS to the provider is the
	trust root for token-derived claims."""
	payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
	return f"header.{payload_b64}.signature"


_CACHE_KEY = "jarvis.oauth.codex_signin"


class _OAuthApiBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		cls._snap = {
			"llm_auth_mode": settings.llm_auth_mode,
			"llm_provider": settings.llm_provider,
			"llm_model": settings.llm_model,
			"llm_oauth_account_email": settings.llm_oauth_account_email,
			"llm_oauth_connected_at": settings.llm_oauth_connected_at,
		}

	@classmethod
	def tearDownClass(cls):
		settings = frappe.get_single("Jarvis Settings")
		for f, v in cls._snap.items():
			settings.db_set(f, v, update_modified=False)
		frappe.cache.delete_key(_CACHE_KEY)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.cache.delete_key(_CACHE_KEY)


class TestGcExpiredNonces(_OAuthApiBase):
	# Punch-list "stale PKCE verifiers + unconsumed nonces never GC'd"
	# from the 2026-06-16 review. The cache stores per-nonce metadata
	# in a Redis hash whose fields can't carry individual TTLs, so
	# abandoned begin_paste_signin flows leave verifier+state hanging
	# until something explicitly wipes them. Now that disconnect() no
	# longer wipes the hash (its own punch-list fix), the GC sweep on
	# begin is the only cleanup; pin its behaviour.

	def test_begin_evicts_expired_peer_nonces(self):
		# Seed a peer's pending nonce that's already past its TTL.
		frappe.cache.hset(
			_CACHE_KEY,
			"expired_peer",
			{
				"status": "pending",
				"originator_user": "peer@example.com",
				"expires_at_ts": int(time.time()) - 60,
				"state": "old-state",
				"verifier": "old-verifier",
				"provider": "OpenAI",
				"model": "gpt-5.5",
			},
		)
		oauth_api.begin_paste_signin("OpenAI", "gpt-5.5")
		# Sweep dropped the expired entry.
		self.assertIsNone(frappe.cache.hget(_CACHE_KEY, "expired_peer"))

	def test_begin_keeps_unexpired_peer_nonces(self):
		# Peer's nonce hasn't expired yet - sweep must not touch it.
		frappe.cache.hset(
			_CACHE_KEY,
			"live_peer",
			{
				"status": "pending",
				"originator_user": "peer@example.com",
				"expires_at_ts": int(time.time()) + 600,
				"state": "live-state",
				"verifier": "live-verifier",
				"provider": "OpenAI",
				"model": "gpt-5.5",
			},
		)
		oauth_api.begin_paste_signin("OpenAI", "gpt-5.5")
		survived = frappe.cache.hget(_CACHE_KEY, "live_peer")
		self.assertIsNotNone(survived)
		self.assertEqual(survived["originator_user"], "peer@example.com")


class TestBeginPasteSignin(_OAuthApiBase):
	def test_returns_nonce_authorize_url_expiry(self):
		out = oauth_api.begin_paste_signin("OpenAI", "gpt-4o")
		self.assertTrue(out["ok"])
		self.assertIn("nonce", out["data"])
		self.assertIn("authorize_url", out["data"])
		self.assertEqual(out["data"]["expires_in"], 600)
		nonce = out["data"]["nonce"]
		self.assertEqual(len(nonce), 48)  # 24 hex bytes

	def test_authorize_url_contains_codex_params(self):
		out = oauth_api.begin_paste_signin("OpenAI", "gpt-4o")
		url = out["data"]["authorize_url"]
		self.assertIn("client_id=app_EMoamEEZ73f0CkXaXp7hrann", url)
		self.assertIn("originator=codex_cli_rs", url)
		self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback", url)

	def test_caches_verifier_state_provider_model(self):
		out = oauth_api.begin_paste_signin("OpenAI", "gpt-5.5")
		entry = frappe.cache.hget(_CACHE_KEY, out["data"]["nonce"])
		self.assertEqual(entry["provider"], "OpenAI")
		self.assertEqual(entry["model"], "gpt-5.5")
		self.assertEqual(entry["status"], "pending")
		self.assertIn("verifier", entry)
		self.assertIn("state", entry)
		self.assertGreater(len(entry["verifier"]), 40)  # base64url(32 bytes)

	def test_gemini_provider_returns_gemini_url(self):
		# Pin the secret: whether one resolves for real depends on the machine
		# (env var / Jarvis Settings / an installed npm package). providers.py
		# binds the resolver at import, so patch ITS reference, not jarvis.hooks.
		with patch(
			"jarvis.oauth.providers.get_oauth_client_secret",
			return_value="GOCSPX-test",
		):
			out = oauth_api.begin_paste_signin("Google Gemini", "gemini-2.0-pro")
		self.assertIn("accounts.google.com", out["data"]["authorize_url"])

	def test_gemini_without_client_secret_fails_before_redirect(self):
		# Confidential client: without a secret the token exchange would fail
		# AFTER the Google consent dance. The begin call must refuse instead,
		# pointing at the Jarvis Settings field.
		with patch(
			"jarvis.oauth.providers.get_oauth_client_secret",
			return_value="",
		):
			out = oauth_api.begin_paste_signin("Google Gemini", "gemini-2.0-pro")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "provider_not_configured")
		self.assertIn("Jarvis Settings", out["error"]["message"])

	def test_unknown_provider_rejected(self):
		out = oauth_api.begin_paste_signin("Anthropic", "claude-3-5")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "unknown_provider")

	def test_standard_api_model_coerced_to_codex_default(self):
		"""Customer-supplied models that aren't in the codex subscription list
		(e.g. "gpt-4o" carried over from api_key mode) get rewritten to the
		default codex model. Otherwise the agent codex extension fails
		every chat turn with ProviderAuthError "No API key found for provider
		openai" - treats model-mismatch as auth failure. Confirmed live on
		jarvis-pool-05b704 (2026-06-11)."""
		out = oauth_api.begin_paste_signin("OpenAI", "gpt-4o")
		entry = frappe.cache.hget(_CACHE_KEY, out["data"]["nonce"])
		self.assertEqual(entry["model"], "gpt-5.5")

	def test_empty_model_coerced_to_default(self):
		out = oauth_api.begin_paste_signin("OpenAI", "")
		entry = frappe.cache.hget(_CACHE_KEY, out["data"]["nonce"])
		self.assertEqual(entry["model"], "gpt-5.5")

	def test_valid_codex_model_passed_through(self):
		for model in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini"):
			out = oauth_api.begin_paste_signin("OpenAI", model)
			entry = frappe.cache.hget(_CACHE_KEY, out["data"]["nonce"])
			self.assertEqual(
				entry["model"], model, f"valid codex model {model!r} should pass through unchanged"
			)

	def test_gemini_standard_api_model_coerced_to_cli_default(self):
		"""Same hazard on the Gemini side: a gemini-pro / gemini-1.0-pro from
		the api_key catalog has to be rewritten to a gemini-cli model before
		entering the OAuth nonce cache. Assert against the catalogue's
		DEFAULT_MODEL rather than a literal so a catalogue refresh (e.g. the
		2.0→2.5 bump) doesn't strand this test."""
		from jarvis._subscription_models import DEFAULT_MODEL

		# Pin the secret so the not-configured guard can't trip on CI (no env
		# var, no Jarvis Settings value, no installed npm package there).
		with patch(
			"jarvis.oauth.providers.get_oauth_client_secret",
			return_value="GOCSPX-test",
		):
			out = oauth_api.begin_paste_signin("Google Gemini", "gemini-pro")
		entry = frappe.cache.hget(_CACHE_KEY, out["data"]["nonce"])
		self.assertEqual(entry["model"], DEFAULT_MODEL["Google Gemini"])
		self.assertNotEqual(entry["model"], "gemini-pro")


class TestCompletePasteSigninParsing(_OAuthApiBase):
	def _seed(self, **overrides):
		nonce = "n_" + ("a" * 46)
		entry = {
			"provider": "OpenAI",
			# Seed with a codex-CLI model - in production begin_paste_signin
			# has already coerced the customer-supplied model via
			# _coerce_subscription_model before caching, so any nonce
			# complete_paste_signin sees holds a valid codex model.
			"model": "gpt-5.5",
			"status": "pending",
			"expires_at_ts": int(time.time()) + 600,
			"verifier": "test-verifier",
			"state": "test-state",
			"authorize_url": "https://auth.openai.com/oauth/authorize?...",
			# Per-user binding (Sprint-1 #9 fix): cache entry remembers who
			# began the sign-in; complete_paste_signin enforces match.
			"originator_user": frappe.session.user,
		}
		entry.update(overrides)
		frappe.cache.hset(_CACHE_KEY, nonce, entry)
		return nonce

	def test_rejects_unknown_nonce(self):
		out = oauth_api.complete_paste_signin(
			nonce="bogus",
			redirected_url="http://localhost:1455/auth/callback?code=A&state=B",
		)
		self.assertEqual(out["error"]["code"], "unknown_nonce")

	def test_rejects_expired_nonce(self):
		nonce = self._seed(expires_at_ts=0)
		out = oauth_api.complete_paste_signin(
			nonce=nonce,
			redirected_url="http://localhost:1455/auth/callback?code=A&state=test-state",
		)
		self.assertEqual(out["error"]["code"], "expired")
		# Expired nonces get evicted on read so the hash doesn't accumulate
		# dead PKCE verifiers waiting for the periodic GC sweep.
		self.assertIsNone(frappe.cache.hget(_CACHE_KEY, nonce))

	def test_rejects_missing_code(self):
		nonce = self._seed()
		out = oauth_api.complete_paste_signin(
			nonce=nonce,
			redirected_url="http://localhost:1455/auth/callback?state=test-state",
		)
		self.assertEqual(out["error"]["code"], "missing_code")

	def test_rejects_state_mismatch(self):
		nonce = self._seed()
		out = oauth_api.complete_paste_signin(
			nonce=nonce,
			redirected_url="http://localhost:1455/auth/callback?code=A&state=wrong",
		)
		self.assertEqual(out["error"]["code"], "state_mismatch")

	def test_rejects_state_one_char_off_constant_time(self):
		# Pins the constant-time compare on state. The previous shape
		# used ``!=`` which short-circuits on the first differing byte and
		# would leak a prefix-recovery oracle to an attacker who can
		# measure complete_paste_signin's response time. Punch-list
		# "state comparison non-constant-time" - this test pins the
		# behaviour by asserting a single-char drift still rejects with
		# the same opaque code.
		nonce = self._seed()
		out = oauth_api.complete_paste_signin(
			nonce=nonce,
			# state seeded as "test-state"; flip last char.
			redirected_url="http://localhost:1455/auth/callback?code=A&state=test-statf",
		)
		self.assertEqual(out["error"]["code"], "state_mismatch")

	def test_rejects_missing_state_doesnt_crash(self):
		# Defensive: secrets.compare_digest raises TypeError on None.
		# Make sure the wrapper coerces both sides to "" before comparing,
		# so a customer who pastes a code-only URL gets state_mismatch
		# not a 500.
		nonce = self._seed()
		out = oauth_api.complete_paste_signin(
			nonce=nonce,
			redirected_url="http://localhost:1455/auth/callback?code=A",
		)
		self.assertEqual(out["error"]["code"], "state_mismatch")

	def test_accepts_query_string_only(self):
		nonce = self._seed()
		with (
			patch(
				"jarvis.oauth.api._exchange_code",
				return_value={
					"access_token": "AT",
					"refresh_token": "RT",
					"expires_in": 3600,
					"id_token": "",
					"email": "x@y.com",
				},
			),
			patch("jarvis.oauth.api.admin_client.post_push_oauth_blob"),
			patch("jarvis.oauth.api.onboarding.save_llm_creds", return_value={"last_sync_status": "ok"}),
		):
			out = oauth_api.complete_paste_signin(
				nonce=nonce,
				redirected_url="?code=ABC&state=test-state",
			)
		self.assertTrue(out["ok"], msg=str(out))

	def test_accepts_bare_querystring_no_prefix(self):
		nonce = self._seed()
		with (
			patch(
				"jarvis.oauth.api._exchange_code",
				return_value={
					"access_token": "AT",
					"refresh_token": "RT",
					"expires_in": 3600,
					"id_token": "",
					"email": "x@y.com",
				},
			),
			patch("jarvis.oauth.api.admin_client.post_push_oauth_blob"),
			patch("jarvis.oauth.api.onboarding.save_llm_creds", return_value={"last_sync_status": "ok"}),
		):
			out = oauth_api.complete_paste_signin(
				nonce=nonce,
				redirected_url="code=ABC&state=test-state",
			)
		self.assertTrue(out["ok"], msg=str(out))

	# ---- bare-code paste (xAI) -------------------------------------------
	# xAI's approval screen shows a bare authorization code, not a callback URL,
	# so parse_qs finds no `code=` and every xAI sign-in used to dead-end on
	# missing_code. See providers.py "code_only_paste".

	def test_xai_accepts_bare_code(self):
		nonce = self._seed(provider="xAI Grok")
		with (
			patch(
				"jarvis.oauth.api._exchange_code",
				return_value={
					"access_token": "AT",
					"refresh_token": "RT",
					"expires_in": 3600,
					"id_token": "",
					"email": "x@y.com",
				},
			) as ex,
			patch("jarvis.oauth.api.admin_client.post_push_oauth_blob"),
			patch("jarvis.oauth.api.onboarding.save_llm_creds", return_value={"last_sync_status": "ok"}),
		):
			out = oauth_api.complete_paste_signin(
				nonce=nonce,
				redirected_url="xai-code-ABC123def456",
			)
		self.assertTrue(out["ok"], msg=str(out))
		# The whole paste IS the code, and PKCE still binds it to this nonce -
		# that binding is what lets the state compare be skipped.
		self.assertEqual(ex.call_args.kwargs["code"], "xai-code-ABC123def456")
		self.assertEqual(ex.call_args.kwargs["code_verifier"], "test-verifier")

	def test_bare_code_rejected_for_url_provider(self):
		# OpenAI returns a real callback URL, so a bare code there is a paste
		# mistake, not a supported shape. Only code_only_paste providers opt in.
		nonce = self._seed()
		out = oauth_api.complete_paste_signin(
			nonce=nonce,
			redirected_url="some-bare-code-ABC123",
		)
		self.assertEqual(out["error"]["code"], "missing_code")

	def test_xai_pasted_url_is_still_state_checked(self):
		# Relaxing state is scoped to a BARE code. If a `code=` param arrives, a
		# `state=` one should have too, so the compare still runs for xAI.
		nonce = self._seed(provider="xAI Grok")
		out = oauth_api.complete_paste_signin(
			nonce=nonce,
			redirected_url="http://127.0.0.1:56121/callback?code=A&state=wrong",
		)
		self.assertEqual(out["error"]["code"], "state_mismatch")

	def test_xai_bare_state_fragment_is_not_a_code(self):
		# "state=..." parses to a recognised OAuth param with no `code`, so it is
		# a code-less callback (missing_code), never a code to redeem.
		nonce = self._seed(provider="xAI Grok")
		out = oauth_api.complete_paste_signin(
			nonce=nonce,
			redirected_url="state=test-state",
		)
		self.assertEqual(out["error"]["code"], "missing_code")

	def test_xai_accepts_base64_padded_code(self):
		# A code carrying "=" as base64/JWT padding is still a bare code. Keying
		# "is this a query?" off the "=" character alone would reject exactly the
		# shape this path exists to accept, so the check keys off whether the
		# paste parses to real OAuth params instead.
		nonce = self._seed(provider="xAI Grok")
		with (
			patch(
				"jarvis.oauth.api._exchange_code",
				return_value={
					"access_token": "AT",
					"refresh_token": "RT",
					"expires_in": 3600,
					"id_token": "",
					"email": "x@y.com",
				},
			) as ex,
			patch("jarvis.oauth.api.admin_client.post_push_oauth_blob"),
			patch("jarvis.oauth.api.onboarding.save_llm_creds", return_value={"last_sync_status": "ok"}),
		):
			out = oauth_api.complete_paste_signin(
				nonce=nonce,
				redirected_url="QUJDMTIzZGVmNDU2Zw==",
			)
		self.assertTrue(out["ok"], msg=str(out))
		self.assertEqual(ex.call_args.kwargs["code"], "QUJDMTIzZGVmNDU2Zw==")

	def test_xai_error_bounce_is_not_redeemed_as_a_code(self):
		# An ?error= callback must not be mistaken for a bare code and shipped to
		# the token endpoint.
		nonce = self._seed(provider="xAI Grok")
		out = oauth_api.complete_paste_signin(
			nonce=nonce,
			redirected_url="http://127.0.0.1:56121/callback?error=access_denied",
		)
		self.assertEqual(out["error"]["code"], "missing_code")


class TestCompletePasteSigninFlow(_OAuthApiBase):
	def _seed(self, provider="OpenAI", model="gpt-5.5"):
		nonce = "k_" + ("d" * 46)
		frappe.cache.hset(
			_CACHE_KEY,
			nonce,
			{
				"provider": provider,
				"model": model,
				"status": "pending",
				"expires_at_ts": int(time.time()) + 600,
				"verifier": "test-verifier",
				"state": "test-state",
				"authorize_url": "https://auth.openai.com/oauth/authorize?...",
				# Per-user binding (Sprint-1 #9 fix).
				"originator_user": frappe.session.user,
			},
		)
		return nonce

	@patch("jarvis.oauth.api.onboarding.save_llm_creds")
	@patch("jarvis.oauth.api.admin_client.post_push_oauth_blob")
	@patch("jarvis.oauth.api._exchange_code")
	def test_happy_path_pushes_blob_and_saves_creds(
		self,
		mock_exchange,
		mock_push,
		mock_save,
	):
		# Realistic JWT shape: agent's codex auth resolver pulls accountId
		# from the access token's `https://api.openai.com/auth.chatgpt_account_id`
		# claim; without that field the OAuth profile is treated as unusable
		# and chat surfaces "No API key found for provider openai".
		access_jwt = _jwt(
			{
				"https://api.openai.com/auth": {
					"chatgpt_account_id": "9151840e-6317-4e8c-a575-8ea33beda869",
				},
			}
		)
		mock_exchange.return_value = {
			"access_token": access_jwt,
			"refresh_token": "RT-456",
			"expires_in": 3600,
			"id_token": "",
			"email": "manager@acme.com",
		}
		mock_save.return_value = {"last_sync_status": "ok"}
		nonce = self._seed()

		out = oauth_api.complete_paste_signin(
			nonce=nonce,
			redirected_url="http://localhost:1455/auth/callback?code=ABC&state=test-state",
		)

		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["account_email"], "manager@acme.com")
		self.assertEqual(out["data"]["last_sync_status"], "ok")

		mock_exchange.assert_called_once()
		kwargs = mock_exchange.call_args.kwargs
		self.assertEqual(kwargs["provider"], "OpenAI")
		self.assertEqual(kwargs["code"], "ABC")
		self.assertEqual(kwargs["code_verifier"], "test-verifier")

		mock_push.assert_called_once()
		args = mock_push.call_args.args
		# Blob is keyed by the mapped model-provider name so agent's
		# request-time auth lookup hits. The OAuth flow itself (authorize
		# URL, client_id, codex-cli params) still uses the OpenAI metadata.
		self.assertEqual(args[0], "openai")
		blob = args[1]
		self.assertEqual(blob["type"], "oauth")
		self.assertEqual(blob["provider"], "openai")
		self.assertEqual(blob["access"], access_jwt)
		self.assertEqual(blob["refresh"], "RT-456")
		self.assertEqual(blob["email"], "manager@acme.com")
		self.assertEqual(blob["accountId"], "9151840e-6317-4e8c-a575-8ea33beda869")
		self.assertEqual(blob["clientId"], "app_EMoamEEZ73f0CkXaXp7hrann")

		mock_save.assert_called_once()
		sk = mock_save.call_args.kwargs
		self.assertEqual(sk["provider"], "OpenAI")
		self.assertEqual(sk["model"], "gpt-5.5")
		self.assertEqual(sk["api_key"], "")
		self.assertEqual(sk["auth_mode"], "oauth")
		# force=True is mandatory in the re-authorize path: without it
		# Jarvis Settings.on_update's diff classifier sees no change
		# and skips the re-render + restart, so agent keeps serving
		# the previous (broken) auth state. Verified live 2026-06-11.
		self.assertTrue(
			sk.get("force"),
			"complete_paste_signin must pass force=True so a no-diff "
			"save still re-renders openclaw.json + restarts the container",
		)

		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.llm_oauth_account_email, "manager@acme.com")
		self.assertIsNotNone(settings.llm_oauth_connected_at)

		self.assertIsNone(frappe.cache.hget(_CACHE_KEY, nonce))

	@patch("jarvis.oauth.api.onboarding.save_llm_creds")
	@patch("jarvis.oauth.api.admin_client.post_push_oauth_blob")
	@patch("jarvis.oauth.api._exchange_code")
	def test_stale_nonce_model_recoerced_at_complete_time(
		self,
		mock_exchange,
		mock_push,
		mock_save,
	):
		"""Belt-and-suspenders: if a nonce somehow holds a non-codex model
		(e.g. _SUBSCRIPTION_MODELS tightened mid-flight, or a manually
		seeded cache row), complete_paste_signin must re-coerce before
		writing to the blob and save_llm_creds. Otherwise the customer's
		container ends up rendering openclaw.json with the bad model and
		every chat turn fails."""
		jwt = _jwt(
			{
				"https://api.openai.com/auth": {
					"chatgpt_account_id": "acct-test",
				},
			}
		)
		mock_exchange.return_value = {
			"access_token": jwt,
			"refresh_token": "RT",
			"expires_in": 3600,
			"id_token": "",
			"email": "manager@acme.com",
		}
		mock_save.return_value = {"last_sync_status": "ok"}
		nonce = self._seed(model="gpt-4o")  # bypasses begin_paste_signin's coercion

		oauth_api.complete_paste_signin(
			nonce=nonce,
			redirected_url="?code=ABC&state=test-state",
		)

		self.assertEqual(
			mock_save.call_args.kwargs["model"],
			"gpt-5.5",
			"complete_paste_signin must re-coerce a stale-cached non-codex model",
		)
		# Blob doesn't carry the model field today, but the push provider id
		# remains tied to OAuth flow, not to the model.
		self.assertEqual(mock_push.call_args.args[0], "openai")

	@patch("jarvis.oauth.api._exchange_code", side_effect=Exception("provider 400"))
	def test_token_exchange_failure_returns_error(self, _):
		"""Generic exception path - actual TokenExchangeError covered by
		the inner _exchange_code function's own tests. Here we just check
		that the endpoint surfaces the failure cleanly."""
		from jarvis.oauth import api as oa

		nonce = self._seed()
		with patch("jarvis.oauth.api._exchange_code", side_effect=oa.TokenExchangeError("provider 400")):
			out = oauth_api.complete_paste_signin(
				nonce=nonce,
				redirected_url="?code=ABC&state=test-state",
			)
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "token_exchange_failed")
		self.assertIsNotNone(frappe.cache.hget(_CACHE_KEY, nonce))


from jarvis import admin_client as _admin_module


class TestDisconnect(_OAuthApiBase):
	@patch("jarvis.oauth.api.admin_client.post_subscription_disconnect")
	def test_disconnect_clears_state(self, mock_disc):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_auth_mode", "oauth", update_modified=False)
		settings.db_set("llm_oauth_account_email", "x@y.com", update_modified=False)
		settings.db_set("llm_oauth_connected_at", frappe.utils.now_datetime(), update_modified=False)
		frappe.db.commit()

		out = oauth_api.disconnect()
		self.assertTrue(out["ok"])
		mock_disc.assert_called_once()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.llm_auth_mode, "api_key")
		self.assertEqual(settings.last_sync_status, "disconnected")
		self.assertFalse(settings.llm_oauth_account_email)
		self.assertIsNone(settings.llm_oauth_connected_at)

	@patch("jarvis.oauth.api.admin_client.post_subscription_disconnect")
	def test_disconnect_preserves_peers_pending_signins(self, _):
		# Punch-list "disconnect() wipes entire OAuth signin cache hash"
		# from the 2026-06-16 review. The previous shape called
		# frappe.cache.delete_key(_CACHE_KEY) on disconnect, which wipes
		# every System Manager's pending sign-in - not just the caller's.
		# Two co-admins on the same site: user A starts a paste-signin,
		# user B clicks Disconnect, user A's nonce vanishes mid-flow.
		# Disconnect must leave the (per-user-bound, short-TTL) nonce
		# cache untouched.
		frappe.cache.hset(
			_CACHE_KEY,
			"peer_pending_nonce",
			{
				"status": "pending",
				"originator_user": "peer@example.com",
				"expires_at_ts": int(time.time()) + 600,
				"state": "peer-state",
				"verifier": "peer-verifier",
				"provider": "OpenAI",
				"model": "gpt-5.5",
			},
		)
		out = oauth_api.disconnect()
		self.assertTrue(out["ok"])
		survived = frappe.cache.hget(_CACHE_KEY, "peer_pending_nonce")
		self.assertIsNotNone(survived)
		self.assertEqual(survived["originator_user"], "peer@example.com")

	@patch(
		"jarvis.oauth.api.admin_client.post_subscription_disconnect",
		side_effect=_admin_module.AdminUnreachableError("net"),
	)
	def test_disconnect_admin_failure(self, _):
		out = oauth_api.disconnect()
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "disconnect_failed")


class _FakeResp:
	def __init__(self, *, ok, status=400, json_body=None, text=""):
		self.ok = ok
		self.status_code = status
		self.text = text
		self._body = json_body

	def json(self):
		if self._body is None:
			raise ValueError("no json")
		return self._body


class TestExchangeCodeErrorParsing(_OAuthApiBase):
	"""Regression for the prod crash: a provider that returns `error` as an
	OBJECT made _exchange_code use a dict as a dict key
	(_TOKEN_EXCHANGE_OPAQUE_CODES.get(<dict>)) -> TypeError: unhashable type.
	The error path must surface a clean TokenExchangeError instead."""

	_PROV = {"client_id": "cid", "token": "https://p.example/token", "client_secret": None}

	def _exchange(self, json_body):
		resp = _FakeResp(ok=False, status=400, json_body=json_body)
		with (
			patch("jarvis.oauth.api.get_provider", return_value=self._PROV),
			patch("jarvis.oauth.api.requests.post", return_value=resp),
			patch("jarvis.oauth.api.frappe.log_error"),
		):
			with self.assertRaises(oauth_api.TokenExchangeError) as ctx:
				oauth_api._exchange_code(provider="openai", code="ac_x", code_verifier="v")
		return ctx.exception

	def test_object_error_does_not_crash_and_maps_nested_code(self):
		# The exact prod shape: `error` is an object with a nested code.
		exc = self._exchange({"error": {"type": "invalid_grant", "message": "bad code"}})
		self.assertEqual(exc.code, "code_invalid")

	def test_string_error_still_maps(self):
		exc = self._exchange({"error": "invalid_client"})
		self.assertEqual(exc.code, "auth_failed")

	def test_unmappable_object_error_falls_back(self):
		exc = self._exchange({"error": {"unexpected": "shape"}})
		self.assertEqual(exc.code, "token_exchange_failed")

	def test_non_json_error_body_falls_back(self):
		resp = _FakeResp(ok=False, status=500, json_body=None, text="<html>502</html>")
		with (
			patch("jarvis.oauth.api.get_provider", return_value=self._PROV),
			patch("jarvis.oauth.api.requests.post", return_value=resp),
			patch("jarvis.oauth.api.frappe.log_error"),
		):
			with self.assertRaises(oauth_api.TokenExchangeError) as ctx:
				oauth_api._exchange_code(provider="openai", code="ac_x", code_verifier="v")
		self.assertEqual(ctx.exception.code, "token_exchange_failed")


class TestCompletePasteSigninStripsIdToken(_OAuthApiBase):
	"""The fleet-agent's PUT /auth-profile schema is strict (pydantic
	extra_forbidden) and REJECTS an ``id_token`` field, so the DIRECT push
	from complete_paste_signin must strip it — otherwise every direct
	(re)authorize 502s with ``extra_forbidden ... blob.id_token`` (the
	2026-07 direct-reauthorize outage). The earlier assumption that agent
	"ignores unknown blob keys" was wrong against the live agent. The blob
	builder still retains id_token for the POOL path's CLIProxyAPI-codex
	reformat; only this direct auth-profiles.json push drops it."""

	def _seed(self, provider="OpenAI", model="gpt-5.5"):
		nonce = "i_" + ("d" * 46)
		frappe.cache.hset(
			_CACHE_KEY,
			nonce,
			{
				"provider": provider,
				"model": model,
				"status": "pending",
				"expires_at_ts": int(time.time()) + 600,
				"verifier": "test-verifier",
				"state": "test-state",
				"originator_user": frappe.session.user,
			},
		)
		return nonce

	@patch("jarvis.oauth.api.onboarding.save_llm_creds")
	@patch("jarvis.oauth.api.admin_client.post_push_oauth_blob")
	@patch("jarvis.oauth.api._exchange_code")
	def test_pushed_blob_strips_id_token(self, mock_exchange, mock_push, mock_save):
		access_jwt = _jwt(
			{
				"https://api.openai.com/auth": {"chatgpt_account_id": "acct-idt"},
			}
		)
		mock_exchange.return_value = {
			"access_token": access_jwt,
			"refresh_token": "RT",
			"expires_in": 3600,
			"id_token": "ID.TOKEN.XYZ",
			"email": "manager@acme.com",
		}
		mock_save.return_value = {"last_sync_status": "ok"}
		nonce = self._seed()

		out = oauth_api.complete_paste_signin(
			nonce=nonce,
			redirected_url="?code=ABC&state=test-state",
		)
		self.assertTrue(out["ok"], msg=str(out))
		mock_push.assert_called_once()
		blob = mock_push.call_args.args[1]
		# id_token stripped so the fleet /auth-profile schema accepts the push…
		self.assertNotIn("id_token", blob)
		# …but the rest of the OAuth blob still rides along untouched.
		self.assertEqual(blob["refresh"], "RT")
		self.assertEqual(blob["email"], "manager@acme.com")
		self.assertEqual(blob["accountId"], "acct-idt")


class TestBeginPoolAccountSignin(_OAuthApiBase):
	"""Task 2: begin_pool_account_signin reuses the DIRECT nonce/PKCE/state
	machinery but tags the cached entry with pool=True so the pool complete
	endpoint can tell it apart from a DIRECT paste-signin nonce."""

	def test_returns_nonce_and_authorize_url(self):
		out = oauth_api.begin_pool_account_signin("OpenAI", "gpt-5.5")
		self.assertTrue(out["ok"], msg=str(out))
		self.assertIn("nonce", out["data"])
		self.assertIn("authorize_url", out["data"])
		self.assertIn("auth.openai.com", out["data"]["authorize_url"])
		self.assertEqual(out["data"]["expires_in"], 600)
		self.assertEqual(len(out["data"]["nonce"]), 48)  # 24 hex bytes

	def test_caches_pool_flag_and_user_binding(self):
		out = oauth_api.begin_pool_account_signin("OpenAI", "gpt-5.5")
		entry = frappe.cache.hget(_CACHE_KEY, out["data"]["nonce"])
		self.assertTrue(entry.get("pool"))
		self.assertEqual(entry["provider"], "OpenAI")
		self.assertEqual(entry["model"], "gpt-5.5")
		self.assertEqual(entry["status"], "pending")
		self.assertEqual(entry["originator_user"], frappe.session.user)
		self.assertIn("verifier", entry)
		self.assertIn("state", entry)

	def test_unknown_provider_rejected(self):
		out = oauth_api.begin_pool_account_signin("Anthropic", "claude-3-5")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "unknown_provider")

	def test_direct_begin_does_not_set_pool_flag(self):
		# The DIRECT path must stay untouched: its nonce carries no pool flag.
		out = oauth_api.begin_paste_signin("OpenAI", "gpt-5.5")
		entry = frappe.cache.hget(_CACHE_KEY, out["data"]["nonce"])
		self.assertIsNone(entry.get("pool"))


class TestCompletePoolAccountSignin(_OAuthApiBase):
	"""Task 2: complete_pool_account_signin captures ONE more subscription
	account and RETURNS the blob (with id_token) for the frontend to store in
	a pool subscription-account row. It must NOT touch the DIRECT path:
	no save_llm_creds, no post_push_oauth_blob, no Jarvis Settings write."""

	def _seed(self, provider="OpenAI", model="gpt-5.5", pool=True):
		nonce = "p_" + ("e" * 46)
		entry = {
			"provider": provider,
			"model": model,
			"status": "pending",
			"expires_at_ts": int(time.time()) + 600,
			"verifier": "test-verifier",
			"state": "test-state",
			"originator_user": frappe.session.user,
		}
		if pool:
			entry["pool"] = True
		frappe.cache.hset(_CACHE_KEY, nonce, entry)
		return nonce

	@patch("jarvis.oauth.api.onboarding.save_llm_creds")
	@patch("jarvis.oauth.api.admin_client.post_push_oauth_blob")
	@patch("jarvis.oauth.api._exchange_code")
	def test_captures_blob_and_leaves_direct_path_untouched(
		self,
		mock_exchange,
		mock_push,
		mock_save,
	):
		access_jwt = _jwt(
			{
				"https://api.openai.com/auth": {"chatgpt_account_id": "acct-pool"},
			}
		)
		mock_exchange.return_value = {
			"access_token": access_jwt,
			"refresh_token": "RT-pool",
			"expires_in": 3600,
			"id_token": "ID.POOL.TOKEN",
			"email": "pool-acct@acme.com",
		}
		# Pin a sentinel so we can prove Jarvis Settings is untouched.
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_oauth_account_email", "SENTINEL@unchanged", update_modified=False)
		frappe.db.commit()

		nonce = self._seed()
		out = oauth_api.complete_pool_account_signin(
			nonce=nonce,
			redirected_url="http://localhost:1455/auth/callback?code=ABC&state=test-state",
		)

		self.assertTrue(out["ok"], msg=str(out))
		data = out["data"]

		# account_ref satisfies the pool-key contract ^[A-Za-z0-9_-]{1,64}$.
		self.assertRegex(data["account_ref"], r"^[A-Za-z0-9_-]{1,64}$")
		self.assertTrue(data["account_ref"].startswith("SUB_"))

		# label + account_email are the connected account's email.
		self.assertEqual(data["label"], "pool-acct@acme.com")
		self.assertEqual(data["account_email"], "pool-acct@acme.com")

		# Plan-05 D2 (P0-04): the raw blob NO LONGER comes back - only an opaque
		# capture_id + safe display metadata. The token stayed server-side.
		self.assertTrue(data["capture_id"].startswith("oacap_"))
		self.assertNotIn("oauth_blob", data)
		flat = json.dumps(data)
		for secret in ("RT-pool", "ID.POOL.TOKEN", access_jwt):
			self.assertNotIn(secret, flat, "a token leaked into the capture response")

		# The blob was PERSISTED ENCRYPTED in the capture, decryptable server-side
		# for the save that adopts it, and carries the id_token (pool shape).
		from frappe.utils.password import get_decrypted_password

		name = frappe.db.get_value("Jarvis Pending OAuth Capture", {"capture_id": data["capture_id"]}, "name")
		parsed = json.loads(
			get_decrypted_password("Jarvis Pending OAuth Capture", name, "encrypted_oauth_blob")
		)
		self.assertEqual(parsed["type"], "oauth")
		self.assertEqual(parsed["provider"], "openai")
		self.assertEqual(parsed["access"], access_jwt)
		self.assertEqual(parsed["refresh"], "RT-pool")
		self.assertEqual(parsed["accountId"], "acct-pool")
		self.assertEqual(parsed["id_token"], "ID.POOL.TOKEN")
		frappe.delete_doc("Jarvis Pending OAuth Capture", name, force=True, ignore_permissions=True)

		# DIRECT path untouched: no container push, no creds save.
		mock_push.assert_not_called()
		mock_save.assert_not_called()

		# Jarvis Settings not modified.
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.llm_oauth_account_email, "SENTINEL@unchanged")

		# Nonce is single-use: consumed on success.
		self.assertIsNone(frappe.cache.hget(_CACHE_KEY, nonce))

	def test_rejects_unknown_nonce(self):
		out = oauth_api.complete_pool_account_signin(
			nonce="bogus",
			redirected_url="?code=A&state=B",
		)
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "unknown_nonce")

	def test_rejects_state_mismatch(self):
		nonce = self._seed()
		out = oauth_api.complete_pool_account_signin(
			nonce=nonce,
			redirected_url="?code=A&state=wrong",
		)
		self.assertEqual(out["error"]["code"], "state_mismatch")

	def test_rejects_direct_nonce(self):
		# A DIRECT paste-signin nonce (no pool flag) must not be completable
		# through the pool capture endpoint - same opaque code as unknown.
		nonce = self._seed(pool=False)
		out = oauth_api.complete_pool_account_signin(
			nonce=nonce,
			redirected_url="?code=A&state=test-state",
		)
		self.assertEqual(out["error"]["code"], "unknown_nonce")

	def test_distinct_accounts_get_distinct_refs(self):
		# Five DIFFERENT accounts (distinct emails/subjects) get distinct
		# account_refs so they don't collide (build_pool_payload keys oauth_blobs by
		# account_ref). Captures fold only on the SAME stable subject (P1-07), which
		# is exercised separately below.
		refs = set()
		captured = []
		for i in range(5):
			nonce = self._seed()
			with patch(
				"jarvis.oauth.api._exchange_code",
				return_value={
					"access_token": "AT",
					"refresh_token": "RT",
					"expires_in": 3600,
					"id_token": "ID.T",
					"email": f"a{i}@b.com",
				},
			):
				out = oauth_api.complete_pool_account_signin(
					nonce=nonce,
					redirected_url="?code=ABC&state=test-state",
				)
			refs.add(out["data"]["account_ref"])
			captured.append(out["data"]["capture_id"])
		self.assertEqual(len(refs), 5)
		for cid in captured:
			name = frappe.db.get_value("Jarvis Pending OAuth Capture", {"capture_id": cid}, "name")
			if name:
				frappe.delete_doc("Jarvis Pending OAuth Capture", name, force=True, ignore_permissions=True)

	def test_same_account_recapture_folds_on_stable_subject(self):
		# Recapturing the SAME account folds onto ONE capture only on a GENUINELY
		# STABLE subject (OpenAI's chatgpt_account_id), never the email (F4/P1-07).
		access_jwt = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-fold-1"}})
		ids = set()
		for _ in range(2):
			nonce = self._seed()
			with patch(
				"jarvis.oauth.api._exchange_code",
				return_value={
					"access_token": access_jwt,
					"refresh_token": "RT",
					"expires_in": 3600,
					"id_token": "ID.T",
					"email": "same@b.com",
				},
			):
				out = oauth_api.complete_pool_account_signin(
					nonce=nonce,
					redirected_url="?code=ABC&state=test-state",
				)
			ids.add(out["data"]["capture_id"])
		self.assertEqual(len(ids), 1, "same-account recapture must fold onto one capture")
		name = frappe.db.get_value("Jarvis Pending OAuth Capture", {"capture_id": ids.pop()}, "name")
		frappe.delete_doc("Jarvis Pending OAuth Capture", name, force=True, ignore_permissions=True)

	def test_no_stable_subject_never_folds(self):
		# No stable subject (non-OpenAI: extract_account_id returns "") → NEVER fold,
		# even for the same email. Prevents the cross-provider token clobber (F4).
		ids = set()
		for _ in range(2):
			nonce = self._seed()
			with patch(
				"jarvis.oauth.api._exchange_code",
				return_value={
					"access_token": "AT-not-a-jwt",
					"refresh_token": "RT",
					"expires_in": 3600,
					"email": "same@b.com",
				},
			):
				out = oauth_api.complete_pool_account_signin(
					nonce=nonce,
					redirected_url="?code=ABC&state=test-state",
				)
			ids.add(out["data"]["capture_id"])
		self.assertEqual(len(ids), 2, "no stable subject → two distinct captures, no email fold")
		for cid in ids:
			name = frappe.db.get_value("Jarvis Pending OAuth Capture", {"capture_id": cid}, "name")
			if name:
				frappe.delete_doc("Jarvis Pending OAuth Capture", name, force=True, ignore_permissions=True)


class TestPendingCaptureEndpoints(_OAuthApiBase):
	"""The rehydrate/cancel wire endpoints over the pending-capture store."""

	def _capture_one(self, email="rehydrate@acme.com"):
		nonce = "p_" + ("f" * 46)
		frappe.cache.hset(
			_CACHE_KEY,
			nonce,
			{
				"provider": "OpenAI",
				"model": "gpt-5.5",
				"status": "pending",
				"expires_at_ts": int(time.time()) + 600,
				"verifier": "v",
				"state": "s",
				"originator_user": frappe.session.user,
				"pool": True,
			},
		)
		with patch(
			"jarvis.oauth.api._exchange_code",
			return_value={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "email": email},
		):
			return oauth_api.complete_pool_account_signin(nonce, "?code=A&state=s")["data"]

	def _cleanup(self):
		for name in frappe.get_all("Jarvis Pending OAuth Capture", pluck="name"):
			frappe.delete_doc("Jarvis Pending OAuth Capture", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_list_pending_captures_rehydrates_without_secret(self):
		try:
			view = self._capture_one()
			out = oauth_api.list_pending_captures()
			self.assertTrue(out["ok"])
			ids = [c["capture_id"] for c in out["data"]["captures"]]
			self.assertIn(view["capture_id"], ids)
			self.assertNotIn("oauth_blob", json.dumps(out["data"]))
			self.assertNotIn("RT", json.dumps(out["data"]))
		finally:
			self._cleanup()

	def test_cancel_pending_capture_erases(self):
		try:
			view = self._capture_one()
			out = oauth_api.cancel_pending_capture(view["capture_id"])
			self.assertTrue(out["ok"])
			# Cancelled captures no longer rehydrate.
			listed = oauth_api.list_pending_captures()["data"]["captures"]
			self.assertNotIn(view["capture_id"], [c["capture_id"] for c in listed])
		finally:
			self._cleanup()

	def test_cancel_unknown_is_opaque(self):
		out = oauth_api.cancel_pending_capture("oacap_nope")
		self.assertFalse(out["ok"])


class TestPoolSigninScope(_OAuthApiBase):
	"""Pool sign-ins must request the codex-CLI scope set (no connectors).

	The connectors scope yields an access_token with
	aud=https://api.openai.com/v1 - agent's codex app-server accepts it
	(so the DIRECT flow keeps it), but cli-proxy-api's codex backend needs
	aud=chatgpt.com/backend-api and rejects the connectors-audience token:
	the account loads, /v1/models returns [], every call 502s "unknown
	provider". Live-verified 2026-07-03. The pool scope set matches
	cli-proxy-api's own --codex-login."""

	def test_pool_signin_uses_codex_scope_without_connectors(self):
		out = oauth_api.begin_pool_account_signin("OpenAI", "gpt-5.5")
		url = out["data"]["authorize_url"]
		self.assertIn("scope=openid+email+profile+offline_access", url)
		self.assertNotIn("api.connectors", url)

	def test_direct_signin_keeps_connectors_scope(self):
		out = oauth_api.begin_paste_signin("OpenAI", "gpt-5.5")
		url = out["data"]["authorize_url"]
		self.assertIn("api.connectors.read", url)
		self.assertIn("api.connectors.invoke", url)

	def test_pool_signin_provider_without_pool_scope_falls_back(self):
		# Google Gemini defines no pool_scope -> pool flow uses the normal
		# scope (the gemini-cli scope set is what its pool path needs too).
		# Secret pinned: see test_gemini_provider_returns_gemini_url.
		with patch(
			"jarvis.oauth.providers.get_oauth_client_secret",
			return_value="GOCSPX-test",
		):
			out = oauth_api.begin_pool_account_signin("Google Gemini", "gemini-2.5-pro")
		url = out["data"]["authorize_url"]
		self.assertIn("cloud-platform", url)  # the normal gemini-cli scope set


class TestIsDirectSubscriptionPredicate(FrappeTestCase):
	"""Pure branch logic for _is_direct_subscription (no DB access).

	A "direct chat-subscription" tenant onboarded a single OAuth subscription
	that is served by the container's auth-profiles.json (codex/gemini-cli
	runtime), NOT the pooled cliproxy sidecar. Their creds live in the flat
	llm_*/llm_oauth_* fields with an empty models[] (v1_seed_llm_models skips
	them), so the pool editor can't see them and AccountView must fall back to
	the DIRECT re-authorize card.
	"""

	# Signature: _is_direct_subscription(auth_mode, has_models, proxy_active,
	#                                    provider_is_oauth)

	def test_connected_oauth_no_models_is_direct(self):
		self.assertTrue(oauth_api._is_direct_subscription("oauth", False, False, True))

	def test_legacy_subscription_value_is_direct(self):
		# Migrated tenants may still carry the pre-REV-1 "subscription" value.
		self.assertTrue(oauth_api._is_direct_subscription("subscription", False, False, True))

	def test_api_key_mode_is_not_direct(self):
		self.assertFalse(oauth_api._is_direct_subscription("api_key", False, False, True))

	def test_empty_mode_is_not_direct(self):
		# Pre-config default; the normal pool editor / onboarding owns it.
		self.assertFalse(oauth_api._is_direct_subscription("", False, False, True))

	def test_models_present_is_not_direct(self):
		# ANY row in models[] (enabled or not) means the pool editor owns it —
		# an all-disabled pool mid-reconfiguration must not read as direct.
		self.assertFalse(oauth_api._is_direct_subscription("oauth", True, False, True))

	def test_proxy_active_is_not_direct(self):
		# proxy_active means they're already on the cliproxy/pool path.
		self.assertFalse(oauth_api._is_direct_subscription("oauth", False, True, True))

	def test_non_oauth_provider_is_not_direct(self):
		# oauth mode left on a non-OAuth provider (e.g. Anthropic after
		# reset_onboarding): a re-authorize card would only ever error
		# unknown_provider, so don't classify it as direct.
		self.assertFalse(oauth_api._is_direct_subscription("oauth", False, False, False))


class TestGetDirectSubscriptionStatus(_OAuthApiBase):
	"""Integration: the endpoint reflects the flat-field DIRECT connection."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		cls._extra = {"proxy_active": settings.proxy_active}

	@classmethod
	def tearDownClass(cls):
		settings = frappe.get_single("Jarvis Settings")
		for f, v in cls._extra.items():
			settings.db_set(f, v, update_modified=False)
		frappe.db.commit()
		super().tearDownClass()

	def test_reflects_connected_direct_oauth(self):
		settings = frappe.get_single("Jarvis Settings")
		# The real direct-tenant shape requires an empty models[] table.
		if settings.get("models"):
			self.skipTest("Jarvis Settings.models is non-empty in this environment")
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("llm_auth_mode", "oauth", update_modified=False)
		settings.db_set("llm_provider", "OpenAI", update_modified=False)
		settings.db_set("llm_model", "gpt-5.5", update_modified=False)
		settings.db_set("llm_oauth_account_email", "user@example.com", update_modified=False)
		settings.db_set("llm_oauth_connected_at", frappe.utils.now_datetime(), update_modified=False)
		out = oauth_api.get_direct_subscription_status()
		self.assertTrue(out["is_direct_subscription"])
		self.assertTrue(out["connected"])
		self.assertEqual(out["auth_mode"], "oauth")
		self.assertEqual(out["provider"], "OpenAI")
		self.assertEqual(out["model"], "gpt-5.5")
		self.assertEqual(out["account_email"], "user@example.com")
		self.assertNotEqual(out["connected_at"], "")
		# proxy_active is surfaced so AccountView can gate the Connection card.
		self.assertFalse(out["proxy_active"])
		# A direct oauth tenant (no models[]) is not a single-subscription pool.
		self.assertFalse(out["is_single_subscription_pool"])

	def test_api_key_tenant_is_not_direct(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("llm_auth_mode", "api_key", update_modified=False)
		out = oauth_api.get_direct_subscription_status()
		self.assertFalse(out["is_direct_subscription"])


class _MockResp:
	"""Minimal requests.Response stand-in for the device-flow HTTP mocks."""

	def __init__(self, status, body, text=""):
		self.status_code = status
		self.ok = 200 <= status < 300
		self._body = body
		self.text = text or (json.dumps(body) if body is not None else "")

	def json(self):
		if self._body is None:
			raise ValueError("no json")
		return self._body


class TestKimiDeviceFlow(_OAuthApiBase):
	"""Phase 2: Kimi (Moonshot) device-code pool capture (begin + poll)."""

	_DEV = {
		"device_code": "DC",
		"user_code": "ABCD-EFGH",
		"verification_uri": "https://www.kimi.com/device",
		"interval": 5,
		"expires_in": 900,
	}

	def _begin(self):
		with patch("jarvis.oauth.api.requests.post", return_value=_MockResp(200, dict(self._DEV))):
			return oauth_api.begin_pool_account_signin("Kimi (Moonshot)", "kimi-k2.7-code")

	def test_begin_returns_device_flow_and_caches_device_code(self):
		res = self._begin()
		self.assertTrue(res["ok"])
		d = res["data"]
		self.assertTrue(d["device_flow"])
		self.assertEqual(d["user_code"], "ABCD-EFGH")
		self.assertTrue(d["nonce"])
		# device_code is cached server-side, NOT returned to the browser
		self.assertNotIn("device_code", d)
		entry = frappe.cache.hget(_CACHE_KEY, d["nonce"])
		self.assertEqual(entry["device_code"], "DC")
		self.assertTrue(entry.get("pool") and entry.get("device"))

	def test_poll_pending_keeps_nonce_alive(self):
		nonce = self._begin()["data"]["nonce"]
		with patch(
			"jarvis.oauth.api.requests.post", return_value=_MockResp(400, {"error": "authorization_pending"})
		):
			res = oauth_api.poll_pool_account_signin(nonce)
		self.assertTrue(res["ok"])
		self.assertEqual(res["data"]["status"], "pending")
		self.assertIsNotNone(frappe.cache.hget(_CACHE_KEY, nonce))  # keep polling

	def test_poll_success_returns_kimi_device_blob(self):
		nonce = self._begin()["data"]["nonce"]
		token = {
			"access_token": "KAT",
			"refresh_token": "KRT",
			"token_type": "Bearer",
			"scope": "kimi:coding",
			"expires_in": 3600,
		}
		with patch("jarvis.oauth.api.requests.post", return_value=_MockResp(200, token)):
			res = oauth_api.poll_pool_account_signin(nonce)
		self.assertTrue(res["ok"])
		d = res["data"]
		self.assertEqual(d["status"], "ok")
		self.assertTrue(d["account_ref"].startswith("SUB_"))
		# Plan-05 D2 (P0-04): capture_id, not the raw blob.
		self.assertTrue(d["capture_id"].startswith("oacap_"))
		self.assertNotIn("oauth_blob", d)
		for secret in ("KAT", "KRT"):
			self.assertNotIn(secret, json.dumps(d))
		# The blob was persisted ENCRYPTED and decrypts to the Kimi device shape.
		from frappe.utils.password import get_decrypted_password

		name = frappe.db.get_value("Jarvis Pending OAuth Capture", {"capture_id": d["capture_id"]}, "name")
		blob = json.loads(
			get_decrypted_password("Jarvis Pending OAuth Capture", name, "encrypted_oauth_blob")
		)
		# agent blob shape the fleet oauth_blob_to_cliproxy_kimi transform consumes
		self.assertEqual(blob["provider"], "kimi")
		self.assertEqual(blob["access"], "KAT")
		self.assertEqual(blob["refresh"], "KRT")
		self.assertEqual(blob["scope"], "kimi:coding")
		self.assertTrue(blob["device_id"])  # minted at begin
		self.assertNotIn("id_token", blob)  # device flow has none
		frappe.delete_doc("Jarvis Pending OAuth Capture", name, force=True, ignore_permissions=True)
		self.assertIsNone(frappe.cache.hget(_CACHE_KEY, nonce))  # nonce consumed

	def test_poll_expired_burns_nonce(self):
		nonce = self._begin()["data"]["nonce"]
		with patch("jarvis.oauth.api.requests.post", return_value=_MockResp(400, {"error": "expired_token"})):
			res = oauth_api.poll_pool_account_signin(nonce)
		self.assertFalse(res["ok"])
		self.assertIsNone(frappe.cache.hget(_CACHE_KEY, nonce))  # terminal -> burned

	def test_begin_paste_signin_rejects_device_provider(self):
		# Review finding [2]: the DIRECT paste-back path must not KeyError on a
		# device-code provider (no authorize URL) — it returns a clean error.
		res = oauth_api.begin_paste_signin("Kimi (Moonshot)", "kimi-k2.7-code")
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["code"], "device_flow_required")

	def test_complete_pool_rejects_device_nonce(self):
		# Review finding [3]: a device nonce (no state/verifier) routed to
		# complete_pool_account_signin must return unknown_nonce, not KeyError-500.
		nonce = self._begin()["data"]["nonce"]
		res = oauth_api.complete_pool_account_signin(nonce, "http://x/cb?code=A&state=B")
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["code"], "unknown_nonce")


class TestXaiPoolCapture(_OAuthApiBase):
	"""Phase 2: xAI pool capture reuses the paste-back flow, producing an agent
	blob the fleet oauth_blob_to_cliproxy_xai transform consumes (provider='xai',
	id_token retained)."""

	def _seed_xai(self):
		nonce = "nx_" + ("b" * 45)
		frappe.cache.hset(
			_CACHE_KEY,
			nonce,
			{
				"provider": "xAI Grok",
				"model": "grok-4.3",
				"status": "pending",
				"expires_at_ts": int(time.time()) + 600,
				"verifier": "vv",
				"state": "test-state",
				"redirect_uri": "http://127.0.0.1:56121/callback",
				"pool": True,
				"originator_user": frappe.session.user,
			},
		)
		return nonce

	def test_pool_capture_builds_xai_blob_with_id_token(self):
		nonce = self._seed_xai()
		with patch(
			"jarvis.oauth.api._exchange_code",
			return_value={
				"access_token": "XAT",
				"refresh_token": "XRT",
				"id_token": _jwt({"email": "x@y.com"}),
				"expires_in": 3600,
				"email": "x@y.com",
			},
		):
			res = oauth_api.complete_pool_account_signin(
				nonce, "http://127.0.0.1:56121/callback?code=CODE&state=test-state"
			)
		self.assertTrue(res["ok"])
		d = res["data"]
		self.assertTrue(d["account_ref"].startswith("SUB_"))
		# Plan-05 D2 (P0-04): capture_id, not the raw blob; the blob is persisted
		# ENCRYPTED and decrypts to the xai shape (id_token retained for the transform).
		self.assertTrue(d["capture_id"].startswith("oacap_"))
		self.assertNotIn("oauth_blob", d)
		from frappe.utils.password import get_decrypted_password

		name = frappe.db.get_value("Jarvis Pending OAuth Capture", {"capture_id": d["capture_id"]}, "name")
		blob = json.loads(
			get_decrypted_password("Jarvis Pending OAuth Capture", name, "encrypted_oauth_blob")
		)
		self.assertEqual(blob["provider"], "xai")  # agent_provider
		self.assertEqual(blob["access"], "XAT")
		self.assertTrue(blob["id_token"])  # xai transform REQUIRES it
		frappe.delete_doc("Jarvis Pending OAuth Capture", name, force=True, ignore_permissions=True)
		self.assertIsNone(frappe.cache.hget(_CACHE_KEY, nonce))  # single-use


class TestFetchAccountEmail(_OAuthApiBase):
	"""Regression for the pool-signin "Account connected" bug: OpenAI's POOL
	access_token has aud=chatgpt.com/backend-api, so a userinfo call with it
	fails, but the id_token (scope includes ``email``) carries the email
	claim directly. _fetch_account_email must try the id_token first and
	only fall back to userinfo when there's no usable id_token."""

	def test_id_token_email_used_without_http_call(self):
		id_token = _jwt({"email": "alice@example.com"})
		with patch("jarvis.oauth.api.requests.get") as mock_get:
			email = oauth_api._fetch_account_email("OpenAI", "AT", id_token)
		mock_get.assert_not_called()
		self.assertEqual(email, "alice@example.com")

	def test_no_id_token_falls_back_to_userinfo(self):
		resp = _FakeResp(ok=True, status=200, json_body={"email": "bob@example.com"})
		with patch("jarvis.oauth.api.requests.get", return_value=resp) as mock_get:
			email = oauth_api._fetch_account_email("OpenAI", "AT", "")
		mock_get.assert_called_once()
		self.assertEqual(email, "bob@example.com")

	def test_malformed_id_token_falls_through_to_userinfo(self):
		resp = _FakeResp(ok=True, status=200, json_body={"email": "carol@example.com"})
		with patch("jarvis.oauth.api.requests.get", return_value=resp):
			email = oauth_api._fetch_account_email("OpenAI", "AT", "not-a-jwt")
		self.assertEqual(email, "carol@example.com")
