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

from jarvis.connectors.mcp_oauth.errors import OAuthRegistrationError


@dataclass
class ClientCreds:
	client_id: str
	client_secret: str | None
	registration_access_token: str | None
	mode: str  # "static" | "dcr"


def static_client(client_id: str, client_secret: str | None = None) -> ClientCreds:
	"""An admin-entered client_id/secret for an AS that does not support
	dynamic registration (e.g. GitHub's OAuth App)."""
	return ClientCreds(
		client_id=client_id, client_secret=client_secret, registration_access_token=None, mode="static"
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
) -> ClientCreds:
	"""RFC 7591 self-registration. Requests a PUBLIC client (no client secret to
	hold before the AS has even answered) - the flow this engine builds is
	authorization-code + PKCE, which needs none for a public client;
	``token_endpoint_auth_method: "none"`` is the corresponding RFC 7591 value.
	An AS that hands back a ``client_secret`` anyway still has it captured on
	:class:`ClientCreds` and used on subsequent requests.

	Raises :class:`OAuthRegistrationError` on a non-2xx response or a response
	with no ``client_id``."""
	request_body = {
		"redirect_uris": [redirect_uri],
		"token_endpoint_auth_method": "none",
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
		egress_allowed=egress_allowed,
	)
	if not (200 <= result.status < 300):
		raise OAuthRegistrationError(
			"registration_failed", f"Dynamic client registration returned HTTP {result.status}."
		)
	doc = result.json or {}
	client_id = doc.get("client_id")
	if not client_id:
		raise OAuthRegistrationError("no_client_id", "Dynamic registration response had no client_id.")
	return ClientCreds(
		client_id=client_id,
		client_secret=doc.get("client_secret"),
		registration_access_token=doc.get("registration_access_token"),
		mode="dcr",
	)
