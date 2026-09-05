"""Unit tests for ``jarvis.connectors.mcp_oauth`` - the pure, frappe-free MCP
OAuth client core (Phase A of MCP_OAUTH_CLIENT_DESIGN.md).

Plain ``unittest.TestCase`` (NOT ``FrappeTestCase``) on purpose, mirroring
``test_connector_ssrf.py`` / ``test_connector_mcp_client.py``: nothing in this
package imports frappe, so the whole file runs under a bare
``python -m unittest jarvis.tests.test_connector_mcp_oauth`` with no bench, DB
or site. A scripted fake transport stands in for
``jarvis.connectors.mcp_oauth.transport.open_pinned`` in every test - no
socket is ever opened, and ``ssrf`` is never imported here.
"""

from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, parse_qsl, urlparse

from jarvis.connectors.mcp_oauth import canonical, discovery, flow, pkce, registration
from jarvis.connectors.mcp_oauth.errors import (
	OAuthDiscoveryError,
	OAuthIssuerError,
	OAuthRegistrationError,
	OAuthTokenError,
)
from jarvis.connectors.mcp_oauth.transport import HttpResult

BASE_URL = "https://mcp.example.com/mcp/"
CANONICAL_BASE = "https://mcp.example.com/mcp"
RESOURCE_METADATA_URL = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
AS_URL = "https://as.example.com"
AS_8414_URL = "https://as.example.com/.well-known/oauth-authorization-server"


# --------------------------------------------------------------------------- #
# fake transport - the single injection seam every module under test uses
# --------------------------------------------------------------------------- #
class _ScriptedTransport:
	"""Stand-in for ``transport.open_pinned``'s call shape: pops the next canned
	``HttpResult`` for a URL (in call order, for URLs hit more than once) and
	records every call for assertion. Raises if a test forgot to script a URL -
	an unscripted call is a bug in the test, not a network access to allow."""

	def __init__(self, script: dict):
		self._script = {
			url: (list(results) if isinstance(results, list) else [results])
			for url, results in script.items()
		}
		self.calls = []

	def __call__(
		self,
		url,
		*,
		method="GET",
		headers=None,
		body=None,
		connect_timeout=5.0,
		read_timeout=20.0,
		egress_allowed=None,
	):
		self.calls.append({"url": url, "method": method, "headers": headers, "body": body})
		queue = self._script.get(url)
		if not queue:
			raise AssertionError(f"unscripted transport call to {url}")
		return queue.pop(0)


def _json_result(payload, status=200, headers=None) -> HttpResult:
	return HttpResult(status=status, headers=dict(headers or {}), json=payload, text=json.dumps(payload))


def _happy_path_script(**resource_metadata_overrides):
	rm_doc = {
		"resource": CANONICAL_BASE,
		"authorization_servers": [AS_URL],
		"scopes_supported": ["repo", "read:org"],
	}
	rm_doc.update(resource_metadata_overrides)
	as_doc = {
		"issuer": AS_URL,
		"authorization_endpoint": f"{AS_URL}/authorize",
		"token_endpoint": f"{AS_URL}/token",
		"registration_endpoint": f"{AS_URL}/register",
		"authorization_response_iss_parameter_supported": True,
	}
	return {
		BASE_URL: HttpResult(
			status=401,
			headers={
				"www-authenticate": f'Bearer error="invalid_request", resource_metadata="{RESOURCE_METADATA_URL}"'
			},
			json=None,
			text="",
		),
		RESOURCE_METADATA_URL: _json_result(rm_doc),
		AS_8414_URL: _json_result(as_doc),
	}, as_doc


# --------------------------------------------------------------------------- #
# pkce.py
# --------------------------------------------------------------------------- #
class PkceTests(unittest.TestCase):
	def test_rfc7636_appendix_b_vector(self):
		verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
		self.assertEqual(pkce.challenge(verifier), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

	def test_new_verifier_is_in_bounds_and_unreserved(self):
		verifier = pkce.new_verifier()
		self.assertGreaterEqual(len(verifier), 43)
		self.assertLessEqual(len(verifier), 128)
		allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
		self.assertTrue(set(verifier) <= allowed)

	def test_new_verifier_is_not_constant(self):
		self.assertNotEqual(pkce.new_verifier(), pkce.new_verifier())

	def test_method_is_s256(self):
		self.assertEqual(pkce.METHOD, "S256")


# --------------------------------------------------------------------------- #
# canonical.py
# --------------------------------------------------------------------------- #
class CanonicalResourceTests(unittest.TestCase):
	def test_strips_trailing_slash(self):
		self.assertEqual(
			canonical.canonical_resource("https://api.githubcopilot.com/mcp/"),
			"https://api.githubcopilot.com/mcp",
		)

	def test_no_trailing_slash_is_unchanged(self):
		self.assertEqual(
			canonical.canonical_resource("https://api.githubcopilot.com/mcp"),
			"https://api.githubcopilot.com/mcp",
		)

	def test_bare_root_drops_slash(self):
		self.assertEqual(canonical.canonical_resource("https://example.com/"), "https://example.com")

	def test_lowercases_scheme_and_host_only(self):
		self.assertEqual(
			canonical.canonical_resource("HTTPS://EXAMPLE.COM/MyPath"), "https://example.com/MyPath"
		)

	def test_rejects_fragment(self):
		with self.assertRaises(ValueError):
			canonical.canonical_resource("https://example.com/mcp#frag")

	def test_rejects_missing_scheme(self):
		with self.assertRaises(ValueError):
			canonical.canonical_resource("example.com/mcp")


# --------------------------------------------------------------------------- #
# discovery.py
# --------------------------------------------------------------------------- #
class DiscoveryHappyPathTests(unittest.TestCase):
	def test_discover_returns_validated_metadata(self):
		script, as_doc = _happy_path_script()
		transport = _ScriptedTransport(script)

		result = discovery.discover(BASE_URL, transport=transport)

		self.assertEqual(result.resource, CANONICAL_BASE)
		self.assertEqual(result.authorization_servers, [AS_URL])
		self.assertEqual(result.scopes_supported, ["repo", "read:org"])
		self.assertEqual(result.issuer, AS_URL)
		self.assertEqual(result.authorization_endpoint, f"{AS_URL}/authorize")
		self.assertEqual(result.token_endpoint, f"{AS_URL}/token")
		self.assertEqual(result.registration_endpoint, f"{AS_URL}/register")
		self.assertEqual(result.raw_as_metadata, as_doc)

	def test_accepts_resource_metadata_with_trailing_slash(self):
		# MCP_OAUTH_CLIENT_DESIGN.md section 4's own GitHub grounding shows a
		# real server returning "resource" WITH a trailing slash for a
		# connector whose base_url is that same URL - the gate must canonicalize
		# both sides, not string-compare raw values.
		script, _ = _happy_path_script(resource=BASE_URL)
		transport = _ScriptedTransport(script)

		result = discovery.discover(BASE_URL, transport=transport)

		self.assertEqual(result.resource, CANONICAL_BASE)

	def test_accepts_resource_metadata_with_uppercase_host(self):
		script, _ = _happy_path_script(resource="HTTPS://MCP.EXAMPLE.COM/mcp")
		transport = _ScriptedTransport(script)

		result = discovery.discover(BASE_URL, transport=transport)

		self.assertEqual(result.resource, CANONICAL_BASE)

	def test_falls_back_to_oidc_discovery_when_8414_unavailable(self):
		script, as_doc = _happy_path_script()
		del script[AS_8414_URL]
		oidc_url = f"{AS_URL}/.well-known/openid-configuration"
		script[AS_8414_URL] = HttpResult(status=404, headers={}, json=None, text="not found")
		script[oidc_url] = _json_result(as_doc)
		transport = _ScriptedTransport(script)

		result = discovery.discover(BASE_URL, transport=transport)

		self.assertEqual(result.issuer, AS_URL)


class DiscoveryGateTests(unittest.TestCase):
	def _discover_and_expect(self, script):
		transport = _ScriptedTransport(script)
		with self.assertRaises(OAuthDiscoveryError) as ctx:
			discovery.discover(BASE_URL, transport=transport)
		return ctx.exception

	def test_resource_mismatch_rejected(self):
		script, _ = _happy_path_script(resource="https://attacker.example.com/mcp")
		exc = self._discover_and_expect(script)
		self.assertEqual(exc.code, "resource_mismatch")

	def test_issuer_mismatch_rejected(self):
		script, as_doc = _happy_path_script()
		bad_as_doc = dict(as_doc, issuer="https://not-the-as.example.com")
		script[AS_8414_URL] = _json_result(bad_as_doc)
		exc = self._discover_and_expect(script)
		self.assertEqual(exc.code, "issuer_mismatch")

	def test_http_endpoint_rejected(self):
		script, as_doc = _happy_path_script()
		insecure_as_doc = dict(as_doc, authorization_endpoint=f"{AS_URL.replace('https', 'http')}/authorize")
		script[AS_8414_URL] = _json_result(insecure_as_doc)
		exc = self._discover_and_expect(script)
		self.assertEqual(exc.code, "insecure_endpoint")

	def test_missing_resource_metadata_in_challenge_rejected(self):
		script, _ = _happy_path_script()
		script[BASE_URL] = HttpResult(
			status=401, headers={"www-authenticate": 'Bearer error="invalid_request"'}, json=None, text=""
		)
		exc = self._discover_and_expect(script)
		self.assertEqual(exc.code, "no_resource_metadata")

	def test_no_401_rejected(self):
		script, _ = _happy_path_script()
		script[BASE_URL] = HttpResult(status=200, headers={}, json={}, text="{}")
		exc = self._discover_and_expect(script)
		self.assertEqual(exc.code, "no_401_challenge")


# --------------------------------------------------------------------------- #
# registration.py
# --------------------------------------------------------------------------- #
class RegistrationTests(unittest.TestCase):
	REGISTRATION_ENDPOINT = f"{AS_URL}/register"

	def test_static_client_carries_mode(self):
		creds = registration.static_client("gh-client-id", "gh-secret")
		self.assertEqual(creds.client_id, "gh-client-id")
		self.assertEqual(creds.client_secret, "gh-secret")
		self.assertEqual(creds.mode, "static")
		self.assertIsNone(creds.registration_access_token)

	def test_register_dynamic_parses_response(self):
		transport = _ScriptedTransport(
			{
				self.REGISTRATION_ENDPOINT: _json_result(
					{
						"client_id": "dcr-id",
						"client_secret": "dcr-secret",
						"registration_access_token": "rat-123",
					},
					status=201,
				)
			}
		)

		creds = registration.register_dynamic(
			self.REGISTRATION_ENDPOINT,
			redirect_uri="https://jarvis.example/oauth/callback",
			scope="repo read:org",
			transport=transport,
		)

		self.assertEqual(creds.client_id, "dcr-id")
		self.assertEqual(creds.client_secret, "dcr-secret")
		self.assertEqual(creds.registration_access_token, "rat-123")
		self.assertEqual(creds.mode, "dcr")

		sent = json.loads(transport.calls[0]["body"])
		self.assertEqual(sent["redirect_uris"], ["https://jarvis.example/oauth/callback"])
		self.assertEqual(sent["scope"], "repo read:org")
		self.assertEqual(sorted(sent["grant_types"]), ["authorization_code", "refresh_token"])
		self.assertEqual(sent["response_types"], ["code"])

	def test_register_dynamic_missing_client_id_raises(self):
		transport = _ScriptedTransport({self.REGISTRATION_ENDPOINT: _json_result({}, status=200)})

		with self.assertRaises(OAuthRegistrationError) as ctx:
			registration.register_dynamic(
				self.REGISTRATION_ENDPOINT,
				redirect_uri="https://jarvis.example/oauth/callback",
				scope="repo",
				transport=transport,
			)
		self.assertEqual(ctx.exception.code, "no_client_id")

	def test_register_dynamic_non_2xx_raises(self):
		transport = _ScriptedTransport(
			{
				self.REGISTRATION_ENDPOINT: HttpResult(
					status=400, headers={}, json={"error": "invalid_request"}, text=""
				)
			}
		)

		with self.assertRaises(OAuthRegistrationError) as ctx:
			registration.register_dynamic(
				self.REGISTRATION_ENDPOINT,
				redirect_uri="https://jarvis.example/oauth/callback",
				scope="repo",
				transport=transport,
			)
		self.assertEqual(ctx.exception.code, "registration_failed")


# --------------------------------------------------------------------------- #
# flow.py
# --------------------------------------------------------------------------- #
def _discovery_fixture():
	return discovery.Discovery(
		resource=CANONICAL_BASE,
		authorization_servers=[AS_URL],
		scopes_supported=["repo"],
		issuer=AS_URL,
		authorization_endpoint=f"{AS_URL}/authorize",
		token_endpoint=f"{AS_URL}/token",
		registration_endpoint=f"{AS_URL}/register",
		raw_as_metadata={},
	)


class BuildAuthorizeUrlTests(unittest.TestCase):
	def test_query_carries_pkce_and_resource(self):
		disc = _discovery_fixture()
		url = flow.build_authorize_url(
			disc,
			"cid-1",
			"https://jarvis.example/oauth/callback",
			scope="repo read:org",
			resource=CANONICAL_BASE,
			state="state-xyz",
			code_challenge="chal-abc",
		)
		parsed = urlparse(url)
		self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", disc.authorization_endpoint)
		params = parse_qs(parsed.query)
		self.assertEqual(params["response_type"], ["code"])
		self.assertEqual(params["client_id"], ["cid-1"])
		self.assertEqual(params["resource"], [CANONICAL_BASE])
		self.assertEqual(params["code_challenge"], ["chal-abc"])
		self.assertEqual(params["code_challenge_method"], ["S256"])
		self.assertEqual(params["state"], ["state-xyz"])


class ExchangeCodeTests(unittest.TestCase):
	def test_exchange_code_posts_form_and_parses_tokens(self):
		disc = _discovery_fixture()
		creds = registration.static_client("cid-1")
		transport = _ScriptedTransport(
			{
				disc.token_endpoint: _json_result(
					{
						"access_token": "acc-tok",
						"refresh_token": "ref-tok",
						"expires_in": 3600,
						"scope": "repo",
						"token_type": "Bearer",
					}
				)
			}
		)

		token_set = flow.exchange_code(
			disc,
			creds,
			code="auth-code-1",
			code_verifier="verifier-1",
			redirect_uri="https://jarvis.example/oauth/callback",
			resource=CANONICAL_BASE,
			transport=transport,
		)

		self.assertEqual(token_set.access_token, "acc-tok")
		self.assertEqual(token_set.refresh_token, "ref-tok")
		self.assertEqual(token_set.expires_in, 3600)

		sent = dict(parse_qsl(transport.calls[0]["body"].decode("utf-8")))
		self.assertEqual(sent["grant_type"], "authorization_code")
		self.assertEqual(sent["code"], "auth-code-1")
		self.assertEqual(sent["code_verifier"], "verifier-1")
		self.assertEqual(sent["redirect_uri"], "https://jarvis.example/oauth/callback")
		self.assertEqual(sent["resource"], CANONICAL_BASE)
		self.assertEqual(sent["client_id"], "cid-1")
		self.assertNotIn("client_secret", sent)

	def test_no_access_token_raises(self):
		disc = _discovery_fixture()
		creds = registration.static_client("cid-1")
		transport = _ScriptedTransport({disc.token_endpoint: _json_result({})})

		with self.assertRaises(OAuthTokenError):
			flow.exchange_code(
				disc,
				creds,
				code="c",
				code_verifier="v",
				redirect_uri="https://jarvis.example/oauth/callback",
				resource=CANONICAL_BASE,
				transport=transport,
			)


class RefreshTests(unittest.TestCase):
	def test_refresh_keeps_old_token_when_response_omits_it(self):
		disc = _discovery_fixture()
		creds = registration.static_client("cid-1", "secret-1")
		transport = _ScriptedTransport(
			{disc.token_endpoint: _json_result({"access_token": "new-acc-tok", "expires_in": 3600})}
		)

		token_set = flow.refresh(
			disc, creds, refresh_token="old-ref-tok", resource=CANONICAL_BASE, transport=transport
		)

		self.assertEqual(token_set.access_token, "new-acc-tok")
		self.assertEqual(token_set.refresh_token, "old-ref-tok")

		sent = dict(parse_qsl(transport.calls[0]["body"].decode("utf-8")))
		self.assertEqual(sent["grant_type"], "refresh_token")
		self.assertEqual(sent["refresh_token"], "old-ref-tok")
		self.assertEqual(sent["client_secret"], "secret-1")

	def test_refresh_uses_rotated_token_when_response_provides_one(self):
		disc = _discovery_fixture()
		creds = registration.static_client("cid-1")
		transport = _ScriptedTransport(
			{
				disc.token_endpoint: _json_result(
					{"access_token": "new-acc-tok", "refresh_token": "rotated-ref-tok"}
				)
			}
		)

		token_set = flow.refresh(
			disc, creds, refresh_token="old-ref-tok", resource=CANONICAL_BASE, transport=transport
		)

		self.assertEqual(token_set.refresh_token, "rotated-ref-tok")


class ValidateIssTests(unittest.TestCase):
	ISSUER = "https://as.example.com"

	def test_supported_and_present_matching_passes(self):
		flow.validate_iss(self.ISSUER, self.ISSUER, True)

	def test_supported_and_present_mismatch_raises(self):
		with self.assertRaises(OAuthIssuerError) as ctx:
			flow.validate_iss("https://attacker.example.com", self.ISSUER, True)
		self.assertEqual(ctx.exception.code, "iss_mismatch")

	def test_supported_and_absent_raises(self):
		with self.assertRaises(OAuthIssuerError) as ctx:
			flow.validate_iss(None, self.ISSUER, True)
		self.assertEqual(ctx.exception.code, "iss_missing")

	def test_not_supported_and_present_matching_passes(self):
		flow.validate_iss(self.ISSUER, self.ISSUER, False)

	def test_not_supported_and_present_mismatch_raises(self):
		with self.assertRaises(OAuthIssuerError) as ctx:
			flow.validate_iss("https://attacker.example.com", self.ISSUER, False)
		self.assertEqual(ctx.exception.code, "iss_mismatch")

	def test_not_supported_and_absent_proceeds(self):
		flow.validate_iss(None, self.ISSUER, False)


if __name__ == "__main__":
	unittest.main()
