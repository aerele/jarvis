"""Tests for jarvis.oauth.providers - provider OAuth metadata."""

import base64
import json
import unittest
from urllib.parse import parse_qs, urlparse

from jarvis.oauth import providers


def _jwt(payload: dict) -> str:
	"""Build a fake JWT with the given payload. Header + signature are bogus."""
	payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
	return f"header.{payload_b64}.signature"


class TestGetProvider(unittest.TestCase):
	def test_openai_returns_codex_metadata(self):
		p = providers.get_provider("OpenAI")
		self.assertEqual(p["authorize"], "https://auth.openai.com/oauth/authorize")
		self.assertEqual(p["token"], "https://auth.openai.com/oauth/token")
		self.assertEqual(p["userinfo"], "https://api.openai.com/v1/userinfo")
		self.assertEqual(p["client_id"], "app_EMoamEEZ73f0CkXaXp7hrann")
		# Storage/lookup identity is the mapped model-provider key, not the
		# OAuth flow id. See providers.py for rationale.
		self.assertEqual(p["agent_provider"], "openai")
		self.assertIn("openid", p["scope"])
		self.assertIn("api.connectors.read", p["scope"])
		self.assertIn("api.connectors.invoke", p["scope"])

	def test_google_gemini_no_longer_an_oauth_provider(self):
		# Google's chat subscription was removed 2026-08-19 (Google discontinued
		# consumer login-with-Google for Gemini 2026-06-18); the gemini-cli OAuth
		# flow is gone from the provider map. Gemini stays available via API key
		# (a separate, unaffected code path).
		with self.assertRaises(providers.UnknownProviderError):
			providers.get_provider("Google Gemini")
		self.assertFalse(providers.is_oauth_provider("Google Gemini"))
		self.assertEqual(providers.agent_provider_for("Google Gemini"), "")

	def test_unknown_provider_raises(self):
		with self.assertRaises(providers.UnknownProviderError):
			providers.get_provider("Anthropic")


class TestExtractAccountId(unittest.TestCase):
	"""agent's codex auth resolver requires `accountId` on the OAuth
	profile; without it the credential is treated as unusable and the
	chat surfaces 'No API key found for provider openai'. See
	openclaw/docs/concepts/oauth.md step 6 of the codex OAuth exchange."""

	def test_openai_pulls_chatgpt_account_id_from_jwt(self):
		jwt = _jwt(
			{
				"https://api.openai.com/auth": {
					"chatgpt_account_id": "9151840e-6317-4e8c-a575-8ea33beda869",
				},
			}
		)
		self.assertEqual(
			providers.extract_account_id("OpenAI", jwt),
			"9151840e-6317-4e8c-a575-8ea33beda869",
		)

	def test_openai_returns_empty_when_claim_missing(self):
		jwt = _jwt({"https://api.openai.com/auth": {}})
		self.assertEqual(providers.extract_account_id("OpenAI", jwt), "")

	def test_openai_returns_empty_when_namespace_missing(self):
		jwt = _jwt({"sub": "user-1"})
		self.assertEqual(providers.extract_account_id("OpenAI", jwt), "")

	def test_returns_empty_on_malformed_jwt(self):
		for bad in ["", "not-a-jwt", "only.one.dot", "x.!!notb64!!.y"]:
			self.assertEqual(providers.extract_account_id("OpenAI", bad), "")

	def test_returns_empty_when_payload_is_not_a_dict(self):
		# OpenAI's JWT spec requires the payload be a JSON object, but if the
		# spec ever drifts (or a custom provider sends a list/string), the
		# helper must not raise - it's called inline from complete_paste_signin
		# where any exception would 500 the wizard.
		for bad_payload in [[1, 2, 3], "just-a-string", 42, None]:
			jwt = _jwt(bad_payload)
			self.assertEqual(providers.extract_account_id("OpenAI", jwt), "")

	def test_returns_empty_when_namespace_value_is_not_a_dict(self):
		jwt = _jwt({"https://api.openai.com/auth": "unexpected-string"})
		self.assertEqual(providers.extract_account_id("OpenAI", jwt), "")

	def test_returns_empty_when_account_id_is_not_a_string(self):
		jwt = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": 12345}})
		self.assertEqual(providers.extract_account_id("OpenAI", jwt), "")

	def test_unknown_provider_returns_empty(self):
		jwt = _jwt({"sub": "x"})
		self.assertEqual(providers.extract_account_id("Anthropic", jwt), "")


class TestBuildAuthorizeUrl(unittest.TestCase):
	def test_openai_authorize_url_has_codex_specific_params(self):
		url = providers.build_authorize_url(
			provider="OpenAI",
			redirect_uri="http://localhost:1455/auth/callback",
			code_challenge="CHALLENGE",
			state="STATE",
		)
		parsed = urlparse(url)
		self.assertEqual(parsed.netloc, "auth.openai.com")
		self.assertEqual(parsed.path, "/oauth/authorize")
		q = parse_qs(parsed.query)
		self.assertEqual(q["response_type"], ["code"])
		self.assertEqual(q["client_id"], ["app_EMoamEEZ73f0CkXaXp7hrann"])
		self.assertEqual(q["redirect_uri"], ["http://localhost:1455/auth/callback"])
		self.assertEqual(q["code_challenge"], ["CHALLENGE"])
		self.assertEqual(q["code_challenge_method"], ["S256"])
		self.assertEqual(q["state"], ["STATE"])
		self.assertEqual(q["originator"], ["codex_cli_rs"])
		self.assertEqual(q["id_token_add_organizations"], ["true"])
		self.assertEqual(q["codex_cli_simplified_flow"], ["true"])
		self.assertIn("api.connectors.read", q["scope"][0])

	def test_gemini_authorize_url_raises_unknown_provider(self):
		# Google Gemini's gemini-cli OAuth flow was removed 2026-08-19; building
		# an authorize URL for it now fails the same way any unknown provider
		# does, rather than returning a gemini-cli offline-access URL.
		with self.assertRaises(providers.UnknownProviderError):
			providers.build_authorize_url(
				provider="Google Gemini",
				redirect_uri="http://localhost:1455/auth/callback",
				code_challenge="C",
				state="S",
			)


class TestPhase2Providers(unittest.TestCase):
	"""Phase 2: xAI (paste-back) + Kimi (device-code) provider metadata."""

	def test_xai_authorize_url_has_nonce_and_pinned_redirect(self):
		url = providers.build_authorize_url(
			provider="xAI Grok",
			redirect_uri="http://ignored",
			code_challenge="CH",
			state="ST",
			pool=True,
			oidc_nonce="NON",
		)
		q = parse_qs(urlparse(url).query)
		self.assertEqual(urlparse(url).netloc, "auth.x.ai")
		self.assertEqual(q["nonce"], ["NON"])
		self.assertEqual(q["code_challenge_method"], ["S256"])
		self.assertEqual(q["client_id"], ["b1a00492-073a-47ea-816f-4c329264a828"])
		self.assertIn("grok-cli:access", q["scope"][0])

	def test_openai_authorize_url_has_no_nonce(self):
		# Only requires_nonce providers get a nonce; OpenAI must be unaffected.
		url = providers.build_authorize_url(
			provider="OpenAI", redirect_uri="R", code_challenge="C", state="S", pool=True, oidc_nonce="N"
		)
		self.assertNotIn("nonce", parse_qs(urlparse(url).query))

	def test_provider_redirect_uri_xai_pinned_openai_default(self):
		self.assertEqual(
			providers.provider_redirect_uri("xAI Grok", "DEF"), "http://127.0.0.1:56121/callback"
		)
		self.assertEqual(providers.provider_redirect_uri("OpenAI", "DEF"), "DEF")

	def test_is_oauth_provider_excludes_device_code(self):
		# Paste-back providers -> True; device-code (Kimi) -> False (no authorize URL).
		self.assertTrue(providers.is_oauth_provider("OpenAI"))
		self.assertTrue(providers.is_oauth_provider("xAI Grok"))
		self.assertFalse(providers.is_oauth_provider("Kimi (Moonshot)"))
		self.assertFalse(providers.is_oauth_provider("Anthropic"))
		self.assertFalse(providers.is_oauth_provider("Google Gemini"))

	def test_kimi_is_device_code(self):
		p = providers.get_provider("Kimi (Moonshot)")
		self.assertEqual(p["grant_type"], "device_code")
		self.assertEqual(p["agent_provider"], "kimi")
		self.assertEqual(p["client_id"], "17e5f671-d194-4dfb-9706-5516cb48c098")
		self.assertIn("device_authorization", p)

	def test_xai_client_secret_empty_public_client(self):
		self.assertEqual(providers.get_provider("xAI Grok")["client_secret"], "")
