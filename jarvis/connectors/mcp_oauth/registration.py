"""Obtain a client_id (MCP_OAUTH_CLIENT_DESIGN.md section 2 modes). CIMD is
NOT in this phase (design section 9: static + DCR now, CIMD later once there
is a public host to serve the client-metadata document).

RFC 7591 dynamic registration's request body is JSON, not a form, so
``register_dynamic`` calls the injected ``transport`` directly with a
JSON-encoded body rather than going through ``http_form`` - see
``transport.py``'s module docstring for why that is still the same single
seam every hop is routed through.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from jarvis.connectors.mcp_oauth import transport as transport_module
from jarvis.connectors.mcp_oauth.errors import OAuthRegistrationError

#: How this client authenticates at the token endpoint (RFC 7591
#: ``token_endpoint_auth_method``). We are a CONFIDENTIAL, server-side client -
#: the bench holds the secret, no browser ever sees it - so we ask for
#: ``client_secret_post`` and honour whichever of the three the AS answers with.
AUTH_POST = "client_secret_post"
AUTH_BASIC = "client_secret_basic"
AUTH_NONE = "none"

_SUPPORTED_AUTH_METHODS = (AUTH_POST, AUTH_BASIC, AUTH_NONE)


@dataclass
class ClientCreds:
	client_id: str
	client_secret: str | None
	registration_access_token: str | None
	mode: str  # "static" | "dcr"
	#: One of :data:`AUTH_POST` / :data:`AUTH_BASIC` / :data:`AUTH_NONE`.
	#: Keyword-defaulted to the method we ask for, so every existing
	#: construction keeps the behaviour it had (secret in the form body).
	auth_method: str = AUTH_POST


def normalize_auth_method(value) -> str:
	"""Map an AS-returned ``token_endpoint_auth_method`` onto the three we can
	actually perform. Anything else (``private_key_jwt``, a typo, a non-string)
	falls back to what we REQUESTED: RFC 7591 section 3.2.1 has a compliant
	server echo the method it registered, so an unrecognised value is either a
	method we cannot do or noise, and the requested one is the honest default."""
	return value if value in _SUPPORTED_AUTH_METHODS else AUTH_POST


def static_client(
	client_id: str, client_secret: str | None = None, *, auth_method: str = AUTH_POST
) -> ClientCreds:
	"""An admin-entered client_id/secret for an AS that does not support
	dynamic registration (e.g. GitHub's OAuth App)."""
	return ClientCreds(
		client_id=client_id,
		client_secret=client_secret,
		registration_access_token=None,
		mode="static",
		auth_method=auth_method,
	)


def register_dynamic(
	registration_endpoint: str,
	*,
	redirect_uri: str,
	scope: str,
	transport: Callable,
	egress_allowed: Callable[[str], bool] | None = None,
	connect_timeout: float = 5.0,
	read_timeout: float = 20.0,
	total_timeout: float = transport_module.TOKEN_TOTAL_TIMEOUT_S,
) -> ClientCreds:
	"""RFC 7591 self-registration, as a CONFIDENTIAL web client: this engine runs
	server-side and can hold a secret, so it registers ``application_type: "web"``
	and asks for ``client_secret_post``. Whatever the AS answers with is what we
	then use - a server that registers us as ``client_secret_basic`` or as a
	public client (``none``) has its choice recorded on :class:`ClientCreds` and
	honoured by every later token request.

	Raises :class:`OAuthRegistrationError` on a non-2xx response or a response
	with no ``client_id``."""
	request_body = {
		"redirect_uris": [redirect_uri],
		"application_type": "web",
		"token_endpoint_auth_method": AUTH_POST,
		"grant_types": ["authorization_code", "refresh_token"],
		"response_types": ["code"],
		"scope": scope,
	}
	body = json.dumps(request_body).encode("utf-8")
	headers = {"Content-Type": "application/json", "Accept": "application/json"}
	result = transport(
		registration_endpoint,
		method="POST",
		headers=headers,
		body=body,
		connect_timeout=connect_timeout,
		read_timeout=read_timeout,
		total_timeout=total_timeout,
		egress_allowed=egress_allowed,
	)
	if not (200 <= result.status < 300):
		raise OAuthRegistrationError(
			"registration_failed", f"Dynamic client registration returned HTTP {result.status}."
		)
	doc = result.json if isinstance(result.json, dict) else {}
	client_id = doc.get("client_id")
	if not client_id or not isinstance(client_id, str):
		raise OAuthRegistrationError("no_client_id", "Dynamic registration response had no client_id.")
	return ClientCreds(
		client_id=client_id,
		client_secret=doc.get("client_secret"),
		registration_access_token=doc.get("registration_access_token"),
		mode="dcr",
		auth_method=normalize_auth_method(doc.get("token_endpoint_auth_method")),
	)
