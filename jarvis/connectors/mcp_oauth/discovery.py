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
  (b) GET that resource-metadata URL and parse the RFC 9728 document. A 401 that
      names no URL (or carries no challenge header at all) is NOT fatal: RFC 9728
      section 3.1 also defines where the document lives by default, so the two
      well-known locations are tried before giving up.
  (c) fetch the first ``authorization_servers`` entry's metadata, trying the RFC
      8414 and OIDC Discovery locations for that issuer (see
      :func:`_as_metadata_urls` - a path-bearing issuer has three).

Every step (b)/(c) GET goes through ``http_form`` with ``form=None`` (no
body), which still routes through the same single ``transport`` seam.

TIME. Each hop above is bounded by :data:`HOP_TOTAL_TIMEOUT_S`, and the whole
run by :data:`RUN_TOTAL_TIMEOUT_S` - the fallbacks mean a run can make up to six
hops (1 probe + 2 resource-metadata + 3 metadata), and six independent 15s hops
would be 90 seconds of a worker held on a stalling host.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from jarvis.connectors.mcp_oauth import transport as transport_module
from jarvis.connectors.mcp_oauth.canonical import canonical_resource
from jarvis.connectors.mcp_oauth.errors import OAuthDiscoveryError

# Matches quoted key="value" params in a WWW-Authenticate challenge, e.g.
# ``Bearer error="invalid_request", resource_metadata="https://...", scope="a b"``.
_CHALLENGE_PARAM_RE = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)\s*=\s*"([^"]*)"')

_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
_AS_8414_PATH = "/.well-known/oauth-authorization-server"  # RFC 8414
_AS_OIDC_PATH = "/.well-known/openid-configuration"  # OpenID Connect Discovery

#: Wall-clock ceiling for ONE discovery hop, and for a whole discovery run.
#: 6 hops x 15s would be 90s; the run budget is what actually bounds a caller
#: (``connectors_api.add_connector`` documents the arithmetic it depends on).
HOP_TOTAL_TIMEOUT_S = transport_module.DISCOVERY_TOTAL_TIMEOUT_S
RUN_TOTAL_TIMEOUT_S = 45.0

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
	#: The ``scope`` the 401 challenge itself asked for, when it named one. The
	#: spec prefers it over ``scopes_supported`` (least privilege: the server is
	#: naming what THIS resource needs, not everything its AS can issue).
	#: Keyword-defaulted so every existing positional/keyword construction of
	#: this dataclass keeps working.
	challenge_scope: str | None = None
	#: The ``resource`` string the protected-resource document itself declared,
	#: VERBATIM. ``resource`` above is its canonical form and is what the
	#: anti-phishing gate and the per-token pin compare; this is what goes on the
	#: wire as the resource indicator, because a server that declares
	#: ``https://host/mcp/`` may well reject the slashless form of its own name.
	resource_declared: str = ""


def _parse_www_authenticate(value: str) -> dict:
	return dict(_CHALLENGE_PARAM_RE.findall(value))


class _Budget:
	"""One discovery run's wall clock. Every hop asks for its own total and gets
	the smaller of :data:`HOP_TOTAL_TIMEOUT_S` and whatever the run has left, so
	the fallback chains cannot add up past :data:`RUN_TOTAL_TIMEOUT_S`."""

	def __init__(self, run_timeout: float, clock: Callable[[], float]):
		self._clock = clock
		self._deadline = clock() + run_timeout

	def hop(self) -> float:
		left = self._deadline - self._clock()
		if left <= 0:
			raise OAuthDiscoveryError("timeout", "Reading this app's sign-in details took too long.")
		return min(HOP_TOTAL_TIMEOUT_S, left)


def _probe_unauthenticated(
	base_url: str,
	transport: Callable,
	egress_allowed: Callable[[str], bool] | None,
	connect_timeout: float,
	read_timeout: float,
	budget: _Budget,
) -> dict:
	"""Send the unauthenticated ``initialize`` probe and return the parsed
	``WWW-Authenticate`` challenge params (``{}`` when the 401 carried none).
	Raises :class:`OAuthDiscoveryError` only if the server does not answer 401 -
	an address that serves an unauthenticated call needs no sign-in at all.

	``drain_event_stream=False``: a server that ANSWERS this probe may answer it
	with an event stream it holds open indefinitely, and its body tells us
	nothing the status line has not - we already know there is no challenge."""
	body = json.dumps(_PROBE_METHOD).encode("utf-8")
	headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
	result = transport(
		base_url,
		method="POST",
		headers=headers,
		body=body,
		connect_timeout=connect_timeout,
		read_timeout=read_timeout,
		total_timeout=budget.hop(),
		egress_allowed=egress_allowed,
		drain_event_stream=False,
	)
	if result.status != 401:
		raise OAuthDiscoveryError(
			"no_401_challenge",
			f"Expected 401 from an unauthenticated MCP call, got HTTP {result.status}.",
		)
	challenge_header = result.headers.get("www-authenticate")
	if not challenge_header:
		# RFC 9728 section 3.1 still says where the document lives by default, so a
		# bare 401 is a fallback case, not a dead end.
		return {}
	return _parse_www_authenticate(challenge_header)


def _get_json(
	url: str,
	transport: Callable,
	egress_allowed: Callable[[str], bool] | None,
	connect_timeout: float,
	read_timeout: float,
	budget: _Budget,
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
		total_timeout=budget.hop(),
	)
	if not (200 <= result.status < 300):
		raise OAuthDiscoveryError(error_code, f"GET {url} returned HTTP {result.status}.")
	# A JSON ARRAY or scalar is not a metadata document. Guarded here as well as
	# in the real transport because a caller may inject any transport it likes,
	# and every read below this line assumes a mapping.
	if not isinstance(result.json, dict):
		raise OAuthDiscoveryError(error_code, f"GET {url} did not return a JSON document.")
	return result.json


def _require_https(value, field_name: str) -> str:
	"""An endpoint the flow will actually use: present, a string, and https."""
	if value is None or value == "":
		raise OAuthDiscoveryError("missing_endpoint", f"AS metadata is missing {field_name}.")
	url = _require_str(value, field_name)
	if urlparse(url).scheme != "https":
		raise OAuthDiscoveryError("insecure_endpoint", f"AS metadata {field_name} must be https.")
	return url


def _require_secure_metadata_url(url: str, field_name: str) -> str:
	"""The metadata DOCUMENTS themselves must be fetched over https, not only the
	endpoints they name. A challenge that points at an http document, or an
	``authorization_servers`` entry on http, would hand the whole flow (endpoints
	included) to whoever is on the wire."""
	value = _require_str(url, field_name)
	if urlparse(value).scheme != "https":
		raise OAuthDiscoveryError("insecure_metadata", f"{field_name} must be an https address.")
	return value


def _require_str(value, field_name: str) -> str:
	"""Every field read out of a server's metadata is untrusted input: ``null``,
	a number or a nested object where a URL belongs must fail as a clean
	discovery error, never as a TypeError from urlparse or an AttributeError
	from a later ``.rstrip``."""
	if not isinstance(value, str) or not value.strip():
		raise OAuthDiscoveryError("malformed_metadata", f"{field_name} was not a usable value.")
	return value.strip()


def _resource_metadata_urls(base_url: str) -> list[str]:
	"""RFC 9728 section 3.1's default locations for ``base_url``'s document: the
	well-known path with the resource's own path INSERTED after it, then the bare
	well-known path at the origin (which is all a root-path resource has)."""
	parsed = urlparse(base_url)
	origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
	path = (parsed.path or "").rstrip("/")
	urls = [origin + _RESOURCE_METADATA_PATH + path] if path else []
	urls.append(origin + _RESOURCE_METADATA_PATH)
	return urls


def _as_metadata_urls(as_url: str) -> list[str]:
	"""Where an issuer's metadata may live, in the order RFC 8414 section 3.1 and
	OpenID Connect Discovery 1.0 define.

	A ROOT issuer has the two obvious locations. A PATH-BEARING issuer
	(``https://host/tenant``) has three, and the order matters: RFC 8414 INSERTS
	its well-known segment before the issuer's path, OIDC Discovery historically
	APPENDS it, and real deployments serve one or the other. Trying only the
	appended form (or only the inserted one) fails against half the world."""
	parsed = urlparse(as_url)
	origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
	path = (parsed.path or "").rstrip("/")
	if not path:
		return [origin + _AS_8414_PATH, origin + _AS_OIDC_PATH]
	return [
		origin + _AS_8414_PATH + path,
		origin + _AS_OIDC_PATH + path,
		origin + path + _AS_OIDC_PATH,
	]


def _fetch_first_json(
	urls: list[str],
	transport: Callable,
	egress_allowed: Callable[[str], bool] | None,
	connect_timeout: float,
	read_timeout: float,
	budget: _Budget,
	*,
	error_code: str,
	message: str,
) -> dict:
	"""GET each candidate in turn and return the first usable JSON document.
	Raises :class:`OAuthDiscoveryError` (chained from the last attempt's failure)
	when every candidate misses."""
	last_error: OAuthDiscoveryError | None = None
	for url in urls:
		try:
			return _get_json(
				url,
				transport,
				egress_allowed,
				connect_timeout,
				read_timeout,
				budget,
				error_code=error_code,
			)
		except OAuthDiscoveryError as exc:
			if exc.code == "timeout":
				raise
			last_error = exc
	raise OAuthDiscoveryError(error_code, message) from last_error


def discover(
	base_url: str,
	*,
	transport: Callable,
	egress_allowed: Callable[[str], bool] | None = None,
	connect_timeout: float = 5.0,
	read_timeout: float = 20.0,
	run_timeout: float = RUN_TOTAL_TIMEOUT_S,
	clock: Callable[[], float] = time.monotonic,
) -> Discovery:
	"""Run the full RFC 9728 -> 8414/OIDC discovery flow for ``base_url`` and
	return the validated :class:`Discovery`. Raises :class:`OAuthDiscoveryError`
	(a stable ``code``, never protocol jargon in the message) on any discovery
	or validation-gate failure, including running past ``run_timeout``."""
	resource = canonical_resource(base_url)
	budget = _Budget(run_timeout, clock)

	challenge_params = _probe_unauthenticated(
		base_url, transport, egress_allowed, connect_timeout, read_timeout, budget
	)
	named_url = (challenge_params.get("resource_metadata") or "").strip()
	candidates = [named_url] if named_url else _resource_metadata_urls(base_url)
	# The DERIVED locations inherit the connector's own scheme, and a Custom URL
	# may legitimately be http - so they are checked exactly like a named one.
	resource_metadata_urls = [_require_secure_metadata_url(url, "resource_metadata") for url in candidates]

	rm_doc = _fetch_first_json(
		resource_metadata_urls,
		transport,
		egress_allowed,
		connect_timeout,
		read_timeout,
		budget,
		error_code="resource_metadata_unavailable" if named_url else "no_resource_metadata",
		message="Could not read this app's protected-resource details.",
	)

	# Compare CANONICAL forms on both sides, not raw strings: the design doc's
	# own GitHub grounding (section 4) shows a real server returning
	# "https://api.githubcopilot.com/mcp/" (trailing slash) as `resource` for a
	# connector whose base_url is the same URL - the gate's job is anti-phishing
	# (this metadata describes THIS host), not trailing-slash pedantry, and RFC
	# 3986 section 6 syntax-based normalization permits exactly this comparison.
	resource_declared = _require_str(rm_doc.get("resource"), "resource")
	try:
		rm_resource = canonical_resource(resource_declared)
	except ValueError:
		rm_resource = None
	if rm_resource != resource:
		raise OAuthDiscoveryError(
			"resource_mismatch",
			"Protected-resource metadata's resource did not match the connector's canonical URL.",
		)

	authorization_servers = [
		_require_secure_metadata_url(entry, "authorization_servers")
		for entry in (rm_doc.get("authorization_servers") or [])
	]
	if not authorization_servers:
		raise OAuthDiscoveryError(
			"no_authorization_servers", "Protected-resource metadata named no authorization_servers."
		)
	scopes_supported = [s for s in (rm_doc.get("scopes_supported") or []) if isinstance(s, str)]

	as_url = authorization_servers[0]
	as_metadata = _fetch_first_json(
		_as_metadata_urls(as_url),
		transport,
		egress_allowed,
		connect_timeout,
		read_timeout,
		budget,
		error_code="as_metadata_unavailable",
		message="Could not fetch authorization server metadata via RFC 8414 or OIDC discovery.",
	)

	issuer = as_metadata.get("issuer")
	if not isinstance(issuer, str) or issuer != as_url:
		# RFC 8414 section 3.3: the issuer MUST be identical to the URL used to
		# retrieve the metadata. Exact string compare, no normalization - a
		# server that returns a subtly different issuer is either misconfigured
		# or attempting a mix-up attack, and either way we do not proceed.
		raise OAuthDiscoveryError(
			"issuer_mismatch", "AS metadata issuer did not match the authorization server URL."
		)

	authorization_endpoint = _require_https(
		as_metadata.get("authorization_endpoint"), "authorization_endpoint"
	)
	token_endpoint = _require_https(as_metadata.get("token_endpoint"), "token_endpoint")
	registration_endpoint = as_metadata.get("registration_endpoint")
	if registration_endpoint is not None:
		registration_endpoint = _require_https(registration_endpoint, "registration_endpoint")

	return Discovery(
		resource=resource,
		resource_declared=resource_declared,
		authorization_servers=authorization_servers,
		scopes_supported=scopes_supported,
		issuer=issuer,
		authorization_endpoint=authorization_endpoint,
		token_endpoint=token_endpoint,
		registration_endpoint=registration_endpoint,
		raw_as_metadata=as_metadata,
		challenge_scope=(challenge_params.get("scope") or "").strip() or None,
	)
