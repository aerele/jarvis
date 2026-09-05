"""RFC 9728 -> RFC 8414 / OIDC discovery, with the anti-phishing validation
gates from MCP_OAUTH_CLIENT_DESIGN.md section 6. Every network hop goes
through the injected ``transport`` callable (see ``transport.py``) - a
discovery run against an untrusted, user-pasted server URL is egress to a host
that URL chose, so nothing here trusts a response before it is validated.

Steps:
  (a) an unauthenticated MCP ``initialize`` POST to ``base_url``; the server is
      expected to answer 401 with a ``WWW-Authenticate`` header naming a
      ``resource_metadata`` URL (RFC 9728). This hop needs a JSON-RPC body, not
      a form, so it calls ``transport`` directly rather than through
      ``http_form`` - see ``transport.py``'s module docstring.
  (b) GET that resource-metadata URL and parse the RFC 9728 document.
  (c) fetch the first ``authorization_servers`` entry's metadata, trying RFC
      8414 (``<issuer>/.well-known/oauth-authorization-server``) then OIDC
      Discovery (``<issuer>/.well-known/openid-configuration``).

Every step (b)/(c) GET goes through ``http_form`` with ``form=None`` (no
body), which still routes through the same single ``transport`` seam.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from jarvis.connectors.mcp_oauth import transport as transport_module
from jarvis.connectors.mcp_oauth.canonical import canonical_resource
from jarvis.connectors.mcp_oauth.errors import OAuthDiscoveryError

# Matches quoted key="value" params in a WWW-Authenticate challenge, e.g.
# ``Bearer error="invalid_request", resource_metadata="https://...", scope="a b"``.
_CHALLENGE_PARAM_RE = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)\s*=\s*"([^"]*)"')

_AS_METADATA_PATHS = (
	"/.well-known/oauth-authorization-server",  # RFC 8414
	"/.well-known/openid-configuration",  # OpenID Connect Discovery
)

_PROBE_METHOD = {
	"jsonrpc": "2.0",
	"id": 1,
	"method": "initialize",
	"params": {
		"protocolVersion": "2025-06-18",
		"capabilities": {},
		"clientInfo": {"name": "jarvis-mcp-oauth-probe", "version": "1.0"},
	},
}


@dataclass
class Discovery:
	resource: str
	authorization_servers: list
	scopes_supported: list
	issuer: str
	authorization_endpoint: str
	token_endpoint: str
	registration_endpoint: str | None
	raw_as_metadata: dict


def _parse_www_authenticate(value: str) -> dict:
	return dict(_CHALLENGE_PARAM_RE.findall(value))


def _probe_unauthenticated(
	base_url: str,
	transport: Callable,
	egress_allowed: Callable[[str], bool] | None,
	connect_timeout: float,
	read_timeout: float,
) -> dict:
	"""Send the unauthenticated ``initialize`` probe and return the parsed
	``WWW-Authenticate`` challenge params. Raises :class:`OAuthDiscoveryError`
	if the server does not answer 401, carries no challenge header, or the
	challenge names no ``resource_metadata`` URL - the RFC 9728 entry point."""
	body = json.dumps(_PROBE_METHOD).encode("utf-8")
	headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
	result = transport(
		base_url,
		method="POST",
		headers=headers,
		body=body,
		connect_timeout=connect_timeout,
		read_timeout=read_timeout,
		egress_allowed=egress_allowed,
	)
	if result.status != 401:
		raise OAuthDiscoveryError(
			"no_401_challenge",
			f"Expected 401 from an unauthenticated MCP call, got HTTP {result.status}.",
		)
	challenge_header = result.headers.get("www-authenticate")
	if not challenge_header:
		raise OAuthDiscoveryError("no_www_authenticate", "401 response carried no WWW-Authenticate header.")
	params = _parse_www_authenticate(challenge_header)
	if "resource_metadata" not in params:
		raise OAuthDiscoveryError(
			"no_resource_metadata",
			"WWW-Authenticate challenge named no resource_metadata URL.",
		)
	return params


def _get_json(
	url: str,
	transport: Callable,
	egress_allowed: Callable[[str], bool] | None,
	connect_timeout: float,
	read_timeout: float,
	*,
	error_code: str,
) -> dict:
	result = transport_module.http_form(
		url,
		method="GET",
		form=None,
		transport=transport,
		egress_allowed=egress_allowed,
		connect_timeout=connect_timeout,
		read_timeout=read_timeout,
	)
	if not (200 <= result.status < 300):
		raise OAuthDiscoveryError(error_code, f"GET {url} returned HTTP {result.status}.")
	if result.json is None:
		raise OAuthDiscoveryError(error_code, f"GET {url} did not return a JSON document.")
	return result.json


def _require_https(url: str | None, field_name: str) -> None:
	if not url:
		raise OAuthDiscoveryError("missing_endpoint", f"AS metadata is missing {field_name}.")
	if urlparse(url).scheme != "https":
		raise OAuthDiscoveryError("insecure_endpoint", f"AS metadata {field_name} must be https.")


def _fetch_as_metadata(
	as_url: str,
	transport: Callable,
	egress_allowed: Callable[[str], bool] | None,
	connect_timeout: float,
	read_timeout: float,
) -> dict:
	"""Try RFC 8414, then OIDC Discovery, at ``as_url``. Raises
	:class:`OAuthDiscoveryError` (chained from the last attempt's failure) if
	neither answers with a usable JSON document."""
	last_error: OAuthDiscoveryError | None = None
	for suffix in _AS_METADATA_PATHS:
		url = as_url.rstrip("/") + suffix
		try:
			return _get_json(
				url,
				transport,
				egress_allowed,
				connect_timeout,
				read_timeout,
				error_code="as_metadata_unavailable",
			)
		except OAuthDiscoveryError as exc:
			last_error = exc
	raise OAuthDiscoveryError(
		"as_metadata_unavailable",
		"Could not fetch authorization server metadata via RFC 8414 or OIDC discovery.",
	) from last_error


def discover(
	base_url: str,
	*,
	transport: Callable,
	egress_allowed: Callable[[str], bool] | None = None,
	connect_timeout: float = 5.0,
	read_timeout: float = 20.0,
) -> Discovery:
	"""Run the full RFC 9728 -> 8414/OIDC discovery flow for ``base_url`` and
	return the validated :class:`Discovery`. Raises :class:`OAuthDiscoveryError`
	(a stable ``code``, never protocol jargon in the message) on any discovery
	or validation-gate failure."""
	resource = canonical_resource(base_url)

	challenge_params = _probe_unauthenticated(
		base_url, transport, egress_allowed, connect_timeout, read_timeout
	)
	resource_metadata_url = challenge_params["resource_metadata"]

	rm_doc = _get_json(
		resource_metadata_url,
		transport,
		egress_allowed,
		connect_timeout,
		read_timeout,
		error_code="resource_metadata_unavailable",
	)

	# Compare CANONICAL forms on both sides, not raw strings: the design doc's
	# own GitHub grounding (section 4) shows a real server returning
	# "https://api.githubcopilot.com/mcp/" (trailing slash) as `resource` for a
	# connector whose base_url is the same URL - the gate's job is anti-phishing
	# (this metadata describes THIS host), not trailing-slash pedantry, and RFC
	# 3986 section 6 syntax-based normalization permits exactly this comparison.
	try:
		rm_resource = canonical_resource(rm_doc.get("resource") or "")
	except ValueError:
		rm_resource = None
	if rm_resource != resource:
		raise OAuthDiscoveryError(
			"resource_mismatch",
			"Protected-resource metadata's resource did not match the connector's canonical URL.",
		)

	authorization_servers = list(rm_doc.get("authorization_servers") or [])
	if not authorization_servers:
		raise OAuthDiscoveryError(
			"no_authorization_servers", "Protected-resource metadata named no authorization_servers."
		)
	scopes_supported = list(rm_doc.get("scopes_supported") or [])

	as_url = authorization_servers[0]
	as_metadata = _fetch_as_metadata(as_url, transport, egress_allowed, connect_timeout, read_timeout)

	issuer = as_metadata.get("issuer")
	if issuer != as_url:
		# RFC 8414 section 3.3: the issuer MUST be identical to the URL used to
		# retrieve the metadata. Exact string compare, no normalization - a
		# server that returns a subtly different issuer is either misconfigured
		# or attempting a mix-up attack, and either way we do not proceed.
		raise OAuthDiscoveryError(
			"issuer_mismatch", "AS metadata issuer did not match the authorization server URL."
		)

	authorization_endpoint = as_metadata.get("authorization_endpoint")
	token_endpoint = as_metadata.get("token_endpoint")
	registration_endpoint = as_metadata.get("registration_endpoint")
	_require_https(authorization_endpoint, "authorization_endpoint")
	_require_https(token_endpoint, "token_endpoint")
	if registration_endpoint is not None:
		_require_https(registration_endpoint, "registration_endpoint")

	return Discovery(
		resource=resource,
		authorization_servers=authorization_servers,
		scopes_supported=scopes_supported,
		issuer=issuer,
		authorization_endpoint=authorization_endpoint,
		token_endpoint=token_endpoint,
		registration_endpoint=registration_endpoint,
		raw_as_metadata=as_metadata,
	)
