"""Unit tests for ``jarvis.connectors.mcp_oauth`` - the pure, frappe-free MCP
OAuth client core (Phase A of MCP_OAUTH_CLIENT_DESIGN.md).

Plain ``unittest.TestCase`` (NOT ``FrappeTestCase``) on purpose, mirroring
``test_connector_ssrf.py`` / ``test_connector_mcp_client.py``: nothing in this
package imports frappe, so the whole file runs under a bare
``python -m unittest jarvis.tests.test_connector_mcp_oauth`` with no bench, DB
or site. A scripted fake transport stands in for
``jarvis.connectors.mcp_oauth.transport.open_pinned`` in every test - no socket
is ever opened. The transport's OWN tests go one layer lower and patch
``ssrf.open_pinned_request`` with a fake streaming response, so the deadline and
the close-on-failure contract are exercised without a network either.
"""

from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, parse_qsl, urlparse

from jarvis.connectors.mcp_oauth import canonical, discovery, flow, pkce, registration
from jarvis.connectors.mcp_oauth import transport as transport_module
from jarvis.connectors.mcp_oauth.errors import (
	OAuthDiscoveryError,
	OAuthIssuerError,
	OAuthRegistrationError,
	OAuthTokenError,
	OAuthTransportError,
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
		total_timeout=None,
		egress_allowed=None,
		**kwargs,
	):
		self.calls.append(
			{
				"url": url,
				"method": method,
				"headers": headers,
				"body": body,
				"total_timeout": total_timeout,
				**kwargs,
			}
		)
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
# transport.py - the one layer that talks to ssrf, tested with a fake response
# --------------------------------------------------------------------------- #
class _FakeResponse:
	"""The streaming shape ``ssrf.open_pinned_request`` hands back."""

	def __init__(self, status: int, headers: dict, chunks: list, on_chunk=None):
		self.status = status
		self.headers = dict(headers)
		self._chunks = list(chunks)
		self._on_chunk = on_chunk
		self.closed = False
		self.streamed = False

	def stream(self, _amt, decode_content=True):
		self.streamed = True
		for chunk in self._chunks:
			if self._on_chunk:
				self._on_chunk()
			yield chunk

	def close(self):
		self.closed = True


class _UndrainableResponse(_FakeResponse):
	def stream(self, _amt, decode_content=True):
		raise AssertionError("this body must never be read")
		yield b""  # pragma: no cover - generator shape only


class _FakePool:
	def __init__(self):
		self.closed = False

	def close(self):
		self.closed = True


class TransportDeadlineTests(unittest.TestCase):
	URL = "https://as.example.com/token"

	def _open(self, resp, pool, clock=None, **kw):
		with patch.object(
			transport_module.ssrf, "open_pinned_request", return_value=(resp, pool, self.URL)
		) as opened:
			result = transport_module.open_pinned(self.URL, clock=clock or (lambda: 0.0), **kw)
		return result, opened

	def test_a_slow_drip_body_trips_the_wall_clock_deadline(self):
		# Each chunk arrives inside the read timeout, so only a TOTAL deadline can
		# stop a server that trickles a body forever.
		now = [0.0]
		resp = _FakeResponse(
			200,
			{"content-type": "application/json"},
			[b'{"a"', b":1", b"}"],
			on_chunk=lambda: now.__setitem__(0, now[0] + 4.0),
		)
		pool = _FakePool()

		with (
			patch.object(transport_module.ssrf, "open_pinned_request", return_value=(resp, pool, self.URL)),
			self.assertRaises(OAuthTransportError) as ctx,
		):
			transport_module.open_pinned(self.URL, total_timeout=10.0, clock=lambda: now[0])

		self.assertEqual(ctx.exception.code, "timeout")
		self.assertTrue(resp.closed, "the response is closed on the way out")
		self.assertTrue(pool.closed, "the pool is closed on the way out")

	def test_a_body_read_inside_the_budget_is_returned(self):
		now = [0.0]
		resp = _FakeResponse(
			200,
			{"Content-Type": "application/json"},
			[b'{"ok"', b":true}"],
			on_chunk=lambda: now.__setitem__(0, now[0] + 1.0),
		)
		result, _ = self._open(resp, _FakePool(), clock=lambda: now[0], total_timeout=10.0)

		self.assertEqual(result.json, {"ok": True})
		self.assertEqual(result.status, 200)

	def test_the_deadline_and_the_redirect_cap_reach_ssrf(self):
		resp = _FakeResponse(200, {}, [b""])
		_result, opened = self._open(resp, _FakePool(), total_timeout=12.0)

		kwargs = opened.call_args.kwargs
		self.assertEqual(kwargs["max_redirects"], transport_module.MAX_REDIRECTS)
		self.assertEqual(kwargs["deadline"], 12.0)

	def test_a_guard_rejection_keeps_its_own_code(self):
		error = transport_module.ssrf.SsrfError("nope", kind="blocked_address")
		with (
			patch.object(transport_module.ssrf, "open_pinned_request", side_effect=error),
			self.assertRaises(OAuthTransportError) as ctx,
		):
			transport_module.open_pinned(self.URL, total_timeout=10.0, clock=lambda: 0.0)
		self.assertEqual(ctx.exception.code, "blocked_address")

	def test_a_failure_after_the_budget_is_reported_as_a_timeout(self):
		now = [0.0]

		def _blow_up(*_a, **_kw):
			now[0] = 99.0
			raise transport_module.ssrf.SsrfError("connect failed", kind="connect_failed")

		with (
			patch.object(transport_module.ssrf, "open_pinned_request", side_effect=_blow_up),
			self.assertRaises(OAuthTransportError) as ctx,
		):
			transport_module.open_pinned(self.URL, total_timeout=10.0, clock=lambda: now[0])
		self.assertEqual(ctx.exception.code, "timeout")

	def test_an_event_stream_body_is_not_drained_when_the_caller_opts_out(self):
		resp = _UndrainableResponse(200, {"content-type": "text/event-stream"}, [])
		pool = _FakePool()

		result, _ = self._open(resp, pool, drain_event_stream=False)

		self.assertEqual(result.status, 200)
		self.assertEqual(result.text, "")
		self.assertIsNone(result.json)
		self.assertTrue(resp.closed)
		self.assertTrue(pool.closed)

	def test_a_json_array_body_is_not_a_document(self):
		resp = _FakeResponse(200, {"content-type": "application/json"}, [b"[1, 2, 3]"])
		result, _ = self._open(resp, _FakePool())
		self.assertIsNone(result.json)
		self.assertEqual(result.text, "[1, 2, 3]")


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

	def test_no_401_rejected(self):
		script, _ = _happy_path_script()
		script[BASE_URL] = HttpResult(status=200, headers={}, json={}, text="{}")
		exc = self._discover_and_expect(script)
		self.assertEqual(exc.code, "no_401_challenge")

	def test_insecure_resource_metadata_url_rejected(self):
		# The DOCUMENT's own address, not merely the endpoints it names: an http
		# metadata URL hands the whole flow to whoever is on the wire.
		script, _ = _happy_path_script()
		script[BASE_URL] = HttpResult(
			status=401,
			headers={
				"www-authenticate": 'Bearer resource_metadata="http://mcp.example.com/.well-known/oauth-protected-resource/mcp"'
			},
			json=None,
			text="",
		)
		exc = self._discover_and_expect(script)
		self.assertEqual(exc.code, "insecure_metadata")

	def test_insecure_authorization_server_url_rejected(self):
		script, _ = _happy_path_script(authorization_servers=["http://as.example.com"])
		exc = self._discover_and_expect(script)
		self.assertEqual(exc.code, "insecure_metadata")


class DiscoveryResourceMetadataFallbackTests(unittest.TestCase):
	"""RFC 9728 section 3.1: a 401 that names no document (or carries no challenge
	at all) still has two default locations to try before giving up."""

	ORIGIN_RM_URL = "https://mcp.example.com/.well-known/oauth-protected-resource"

	def _bare_401(self, with_header: bool = True) -> HttpResult:
		headers = {"www-authenticate": 'Bearer error="invalid_request"'} if with_header else {}
		return HttpResult(status=401, headers=headers, json=None, text="")

	def test_falls_back_to_the_path_inserted_default_location(self):
		script, _ = _happy_path_script()
		script[BASE_URL] = self._bare_401()

		result = discovery.discover(BASE_URL, transport=_ScriptedTransport(script))

		self.assertEqual(result.resource, CANONICAL_BASE)

	def test_falls_back_to_the_origin_when_the_path_form_misses(self):
		script, _ = _happy_path_script()
		script[BASE_URL] = self._bare_401(with_header=False)
		rm_doc = script.pop(RESOURCE_METADATA_URL)
		script[RESOURCE_METADATA_URL] = HttpResult(status=404, headers={}, json=None, text="")
		script[self.ORIGIN_RM_URL] = rm_doc

		result = discovery.discover(BASE_URL, transport=_ScriptedTransport(script))

		self.assertEqual(result.resource, CANONICAL_BASE)

	def test_a_default_location_on_http_is_refused_before_it_is_fetched(self):
		# The derived locations inherit the connector's scheme, and a Custom URL
		# may be http - so they get the same https check a named one gets.
		insecure_base = BASE_URL.replace("https://", "http://")
		script, _ = _happy_path_script()
		script[insecure_base] = self._bare_401()
		transport = _ScriptedTransport(script)

		with self.assertRaises(OAuthDiscoveryError) as ctx:
			discovery.discover(insecure_base, transport=transport)

		self.assertEqual(ctx.exception.code, "insecure_metadata")
		self.assertEqual(len(transport.calls), 1, "nothing was fetched over http")

	def test_both_defaults_missing_raises_no_resource_metadata(self):
		script, _ = _happy_path_script()
		script[BASE_URL] = self._bare_401()
		script[RESOURCE_METADATA_URL] = HttpResult(status=404, headers={}, json=None, text="")
		script[self.ORIGIN_RM_URL] = HttpResult(status=404, headers={}, json=None, text="")

		with self.assertRaises(OAuthDiscoveryError) as ctx:
			discovery.discover(BASE_URL, transport=_ScriptedTransport(script))
		self.assertEqual(ctx.exception.code, "no_resource_metadata")


class DiscoveryAsMetadataUrlTests(unittest.TestCase):
	"""A path-bearing issuer has THREE candidate metadata locations, and the
	RFC 8414 path-INSERTED one comes first."""

	TENANT_AS = "https://as.example.com/tenant-7"

	def _script_for(self, served_url: str) -> dict:
		as_doc = {
			"issuer": self.TENANT_AS,
			"authorization_endpoint": f"{self.TENANT_AS}/authorize",
			"token_endpoint": f"{self.TENANT_AS}/token",
		}
		script, _ = _happy_path_script(authorization_servers=[self.TENANT_AS])
		del script[AS_8414_URL]
		for url in discovery._as_metadata_urls(self.TENANT_AS):
			script[url] = (
				_json_result(as_doc)
				if url == served_url
				else HttpResult(status=404, headers={}, json=None, text="")
			)
		return script

	def test_candidate_order_for_a_path_bearing_issuer(self):
		self.assertEqual(
			discovery._as_metadata_urls(self.TENANT_AS),
			[
				"https://as.example.com/.well-known/oauth-authorization-server/tenant-7",
				"https://as.example.com/.well-known/openid-configuration/tenant-7",
				"https://as.example.com/tenant-7/.well-known/openid-configuration",
			],
		)

	def test_candidate_order_for_a_root_issuer_is_unchanged(self):
		self.assertEqual(
			discovery._as_metadata_urls(AS_URL),
			[AS_8414_URL, f"{AS_URL}/.well-known/openid-configuration"],
		)

	def test_path_inserted_rfc8414_location_is_used(self):
		served = "https://as.example.com/.well-known/oauth-authorization-server/tenant-7"
		transport = _ScriptedTransport(self._script_for(served))

		result = discovery.discover(BASE_URL, transport=transport)

		self.assertEqual(result.issuer, self.TENANT_AS)
		self.assertEqual(transport.calls[-1]["url"], served)

	def test_path_appended_oidc_location_is_the_last_resort(self):
		served = "https://as.example.com/tenant-7/.well-known/openid-configuration"
		transport = _ScriptedTransport(self._script_for(served))

		result = discovery.discover(BASE_URL, transport=transport)

		self.assertEqual(result.issuer, self.TENANT_AS)
		self.assertEqual(transport.calls[-1]["url"], served)


class DiscoveryMalformedMetadataTests(unittest.TestCase):
	"""A hostile or broken server must produce a clean discovery error, never a
	TypeError or AttributeError from deep inside a validation gate."""

	def _expect(self, script, code: str):
		with self.assertRaises(OAuthDiscoveryError) as ctx:
			discovery.discover(BASE_URL, transport=_ScriptedTransport(script))
		self.assertEqual(ctx.exception.code, code)

	def test_json_array_body_is_not_a_document(self):
		script, _ = _happy_path_script()
		script[RESOURCE_METADATA_URL] = _json_result([{"resource": CANONICAL_BASE}])
		self._expect(script, "resource_metadata_unavailable")

	def test_null_authorization_server_entry(self):
		script, _ = _happy_path_script(authorization_servers=[None])
		self._expect(script, "malformed_metadata")

	def test_non_string_resource(self):
		script, _ = _happy_path_script(resource=123)
		self._expect(script, "malformed_metadata")

	def test_non_string_token_endpoint(self):
		script, as_doc = _happy_path_script()
		script[AS_8414_URL] = _json_result(dict(as_doc, token_endpoint=42))
		self._expect(script, "malformed_metadata")

	def test_null_issuer(self):
		script, as_doc = _happy_path_script()
		script[AS_8414_URL] = _json_result(dict(as_doc, issuer=None))
		self._expect(script, "issuer_mismatch")


class DiscoveryProbeTests(unittest.TestCase):
	def test_the_probe_asks_the_transport_not_to_drain_an_event_stream(self):
		script, _ = _happy_path_script()
		transport = _ScriptedTransport(script)

		discovery.discover(BASE_URL, transport=transport)

		self.assertIs(transport.calls[0]["drain_event_stream"], False)

	def test_an_event_stream_answer_is_simply_no_challenge(self):
		# A server that ANSWERS the unauthenticated probe with a stream needs no
		# sign-in; the caller maps this code to "no sign-in here".
		script, _ = _happy_path_script()
		script[BASE_URL] = HttpResult(
			status=200, headers={"content-type": "text/event-stream"}, json=None, text=""
		)
		with self.assertRaises(OAuthDiscoveryError) as ctx:
			discovery.discover(BASE_URL, transport=_ScriptedTransport(script))
		self.assertEqual(ctx.exception.code, "no_401_challenge")

	def test_every_hop_is_handed_a_wall_clock_total(self):
		transport = _ScriptedTransport(_happy_path_script()[0])

		discovery.discover(BASE_URL, transport=transport)

		for call in transport.calls:
			self.assertLessEqual(call["total_timeout"], discovery.HOP_TOTAL_TIMEOUT_S)
			self.assertGreater(call["total_timeout"], 0)

	def test_an_exhausted_run_budget_stops_before_the_next_hop(self):
		ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0])
		transport = _ScriptedTransport(_happy_path_script()[0])

		with self.assertRaises(OAuthDiscoveryError) as ctx:
			discovery.discover(BASE_URL, transport=transport, clock=lambda: next(ticks))
		self.assertEqual(ctx.exception.code, "timeout")
		self.assertEqual(len(transport.calls), 1, "the run stopped instead of making a second hop")


class DiscoveryResourceIndicatorTests(unittest.TestCase):
	def test_the_declared_resource_is_kept_verbatim(self):
		# The gate canonicalizes both sides, but the string the server declared is
		# what later requests must carry - a server that calls itself ".../mcp/"
		# may reject the slashless form of its own name.
		script, _ = _happy_path_script(resource=BASE_URL)

		result = discovery.discover(BASE_URL, transport=_ScriptedTransport(script))

		self.assertEqual(result.resource_declared, BASE_URL)
		self.assertEqual(result.resource, CANONICAL_BASE)


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
		# We are a confidential, server-side web client, and we say so.
		self.assertEqual(sent["application_type"], "web")
		self.assertEqual(sent["token_endpoint_auth_method"], "client_secret_post")

	def test_registration_falls_back_to_the_requested_auth_method(self):
		transport = _ScriptedTransport(
			{self.REGISTRATION_ENDPOINT: _json_result({"client_id": "dcr-id"}, status=201)}
		)
		creds = registration.register_dynamic(
			self.REGISTRATION_ENDPOINT,
			redirect_uri="https://jarvis.example/oauth/callback",
			scope="repo",
			transport=transport,
		)
		self.assertEqual(creds.auth_method, registration.AUTH_POST)

	def test_registration_honours_the_returned_auth_method(self):
		transport = _ScriptedTransport(
			{
				self.REGISTRATION_ENDPOINT: _json_result(
					{
						"client_id": "dcr-id",
						"client_secret": "dcr-secret",
						"token_endpoint_auth_method": "client_secret_basic",
					},
					status=201,
				)
			}
		)
		creds = registration.register_dynamic(
			self.REGISTRATION_ENDPOINT,
			redirect_uri="https://jarvis.example/oauth/callback",
			scope="repo",
			transport=transport,
		)
		self.assertEqual(creds.auth_method, registration.AUTH_BASIC)

	def test_an_auth_method_we_cannot_perform_falls_back(self):
		self.assertEqual(registration.normalize_auth_method("private_key_jwt"), registration.AUTH_POST)
		self.assertEqual(registration.normalize_auth_method(None), registration.AUTH_POST)
		self.assertEqual(registration.normalize_auth_method("none"), registration.AUTH_NONE)

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
		self.assertEqual(params["scope"], ["repo read:org"])

	def test_an_empty_scope_is_omitted_entirely(self):
		url = flow.build_authorize_url(
			_discovery_fixture(),
			"cid-1",
			"https://jarvis.example/oauth/callback",
			scope="",
			resource=CANONICAL_BASE,
			state="state-xyz",
			code_challenge="chal-abc",
		)
		self.assertNotIn("scope", parse_qs(urlparse(url).query))

	def test_a_declared_resource_is_sent_as_given(self):
		# RFC 8707 interop: the RS declared ".../mcp/", so that is what the
		# authorize request carries, trailing slash and all.
		url = flow.build_authorize_url(
			_discovery_fixture(),
			"cid-1",
			"https://jarvis.example/oauth/callback",
			scope="repo",
			resource=BASE_URL,
			state="state-xyz",
			code_challenge="chal-abc",
		)
		self.assertEqual(parse_qs(urlparse(url).query)["resource"], [BASE_URL])


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

	def test_client_secret_basic_moves_the_secret_into_the_header(self):
		disc = _discovery_fixture()
		creds = registration.static_client("cid 1", "s3cr!t", auth_method=registration.AUTH_BASIC)
		transport = _ScriptedTransport({disc.token_endpoint: _json_result({"access_token": "acc-tok"})})

		flow.exchange_code(
			disc,
			creds,
			code="auth-code-1",
			code_verifier="verifier-1",
			redirect_uri="https://jarvis.example/oauth/callback",
			resource=CANONICAL_BASE,
			transport=transport,
		)

		call = transport.calls[0]
		# form-encoding: a space is "+", per RFC 6749 Appendix B
		expected = base64.b64encode(b"cid+1:s3cr%21t").decode("ascii")
		self.assertEqual(call["headers"]["Authorization"], f"Basic {expected}")
		sent = dict(parse_qsl(call["body"].decode("utf-8")))
		# A secret sent twice is a secret in two logs: neither value is repeated
		# in the body when the header carries them.
		self.assertNotIn("client_secret", sent)
		self.assertNotIn("client_id", sent)

	def test_a_public_client_sends_no_secret_at_all(self):
		disc = _discovery_fixture()
		creds = registration.static_client("cid-1", "leftover", auth_method=registration.AUTH_NONE)
		transport = _ScriptedTransport({disc.token_endpoint: _json_result({"access_token": "acc-tok"})})

		flow.exchange_code(
			disc,
			creds,
			code="c",
			code_verifier="v",
			redirect_uri="https://jarvis.example/oauth/callback",
			resource=CANONICAL_BASE,
			transport=transport,
		)

		call = transport.calls[0]
		self.assertNotIn("Authorization", call["headers"] or {})
		sent = dict(parse_qsl(call["body"].decode("utf-8")))
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
