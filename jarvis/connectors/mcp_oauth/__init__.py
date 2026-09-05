"""Pure, frappe-free MCP OAuth client core (Phase A).

Spec-compliant client for the 2026-07-28 MCP Authorization spec:
RFC 9728 discovery -> RFC 8414/OIDC AS metadata -> OAuth 2.1 authorization
code + PKCE -> RFC 8707 resource indicators -> RFC 9207 issuer validation ->
refresh. Static and RFC 7591 dynamic client registration; CIMD is a later
phase (MCP_OAUTH_CLIENT_DESIGN.md section 9).

Every outbound hop goes through the single injectable transport in
``transport.py`` (default: the SSRF-guarded, IP-pinned
``ssrf.open_pinned_request``), so every function here is unit-testable with a
fake transport and no network. No DocTypes, no whitelisted APIs, and no
``import frappe`` anywhere in this package - that is Phase B's job.
"""

from __future__ import annotations

from jarvis.connectors.mcp_oauth.canonical import canonical_resource
from jarvis.connectors.mcp_oauth.discovery import Discovery, discover
from jarvis.connectors.mcp_oauth.errors import (
	OAuthDiscoveryError,
	OAuthError,
	OAuthIssuerError,
	OAuthRegistrationError,
	OAuthTokenError,
	OAuthTransportError,
)
from jarvis.connectors.mcp_oauth.flow import (
	TokenSet,
	build_authorize_url,
	exchange_code,
	refresh,
	validate_iss,
)
from jarvis.connectors.mcp_oauth.pkce import METHOD as PKCE_METHOD
from jarvis.connectors.mcp_oauth.pkce import challenge as pkce_challenge
from jarvis.connectors.mcp_oauth.pkce import new_verifier as pkce_new_verifier
from jarvis.connectors.mcp_oauth.registration import ClientCreds, register_dynamic, static_client
from jarvis.connectors.mcp_oauth.transport import HttpResult, http_form, open_pinned

__all__ = [
	"PKCE_METHOD",
	"ClientCreds",
	"Discovery",
	"HttpResult",
	"OAuthDiscoveryError",
	"OAuthError",
	"OAuthIssuerError",
	"OAuthRegistrationError",
	"OAuthTokenError",
	"OAuthTransportError",
	"TokenSet",
	"build_authorize_url",
	"canonical_resource",
	"discover",
	"exchange_code",
	"http_form",
	"open_pinned",
	"pkce_challenge",
	"pkce_new_verifier",
	"refresh",
	"register_dynamic",
	"static_client",
	"validate_iss",
]
