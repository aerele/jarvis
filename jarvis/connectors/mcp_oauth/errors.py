"""Typed error hierarchy for the MCP OAuth client core.

Every error carries a stable ``code`` string - Phase B maps these to broker
``connector_not_ready`` / friendly UI copy (MCP_OAUTH_CLIENT_DESIGN.md section 6).
Codes are protocol-neutral identifiers (``resource_mismatch``,
``no_client_id``, ...); no message string embeds a client_secret, authorization
code, access token, or refresh token - see the SECRETS/LOGGING rule the phase
brief calls out. No ``import frappe`` here or anywhere in this package.
"""

from __future__ import annotations


class OAuthError(Exception):
	"""Base of the MCP OAuth client error hierarchy. ``code`` is stable across
	releases so a caller (the broker, in Phase B) can switch on it without
	string-matching the human-facing message."""

	def __init__(self, code: str, message: str):
		super().__init__(message)
		self.code = code


class OAuthTransportError(OAuthError):
	"""An outbound hop was rejected by the SSRF guard, or failed at the network
	layer. ``kind`` mirrors ``ssrf.SsrfError.kind`` so a caller can tell an
	unreachable endpoint from a guard rejection without inspecting the message."""

	def __init__(self, code: str, message: str, *, kind: str | None = None):
		super().__init__(code, message)
		self.kind = kind


class OAuthDiscoveryError(OAuthError):
	"""RFC 9728 / 8414 / OIDC discovery failed, or one of the anti-phishing
	validation gates rejected what a server returned."""


class OAuthRegistrationError(OAuthError):
	"""RFC 7591 dynamic client registration failed."""


class OAuthTokenError(OAuthError):
	"""A token or refresh request failed, or the response was unusable."""


class OAuthIssuerError(OAuthError):
	"""RFC 9207 ``iss`` validation rejected the authorization response."""
