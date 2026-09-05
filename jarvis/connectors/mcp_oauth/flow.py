"""Authorization-code + PKCE + resource-indicator flow, and RFC 9207 issuer
validation. Every token/refresh POST is a form body, so both go through
``transport.http_form`` (form-urlencoded, per OAuth 2.1 section 3.2.1) with
the caller's injected ``transport``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

from jarvis.connectors.mcp_oauth import transport as transport_module
from jarvis.connectors.mcp_oauth.discovery import Discovery
from jarvis.connectors.mcp_oauth.errors import OAuthIssuerError, OAuthTokenError
from jarvis.connectors.mcp_oauth.pkce import METHOD
from jarvis.connectors.mcp_oauth.registration import ClientCreds


@dataclass
class TokenSet:
	access_token: str
	refresh_token: str | None
	expires_in: int | None
	scope: str | None
	token_type: str | None


def build_authorize_url(
	discovery: Discovery,
	client_id: str,
	redirect_uri: str,
	*,
	scope: str,
	resource: str,
	state: str,
	code_challenge: str,
) -> str:
	"""The browser-bound authorize URL: PKCE challenge and the RFC 8707
	``resource`` indicator are always present, never optional."""
	params = {
		"response_type": "code",
		"client_id": client_id,
		"redirect_uri": redirect_uri,
		"scope": scope,
		"state": state,
		"resource": resource,
		"code_challenge": code_challenge,
		"code_challenge_method": METHOD,
	}
	separator = "&" if "?" in discovery.authorization_endpoint else "?"
	return f"{discovery.authorization_endpoint}{separator}{urlencode(params)}"


def _parse_token_response(result) -> TokenSet:
	if not (200 <= result.status < 300):
		raise OAuthTokenError("token_request_failed", f"Token request returned HTTP {result.status}.")
	doc = result.json or {}
	access_token = doc.get("access_token")
	if not access_token:
		raise OAuthTokenError("no_access_token", "Token response had no access_token.")
	return TokenSet(
		access_token=access_token,
		refresh_token=doc.get("refresh_token"),
		expires_in=doc.get("expires_in"),
		scope=doc.get("scope"),
		token_type=doc.get("token_type"),
	)


def _client_form(creds: ClientCreds) -> dict:
	form = {"client_id": creds.client_id}
	if creds.client_secret:
		form["client_secret"] = creds.client_secret
	return form


def exchange_code(
	discovery: Discovery,
	creds: ClientCreds,
	*,
	code: str,
	code_verifier: str,
	redirect_uri: str,
	resource: str,
	transport: Callable,
	egress_allowed: Callable[[str], bool] | None = None,
	connect_timeout: float = 5.0,
	read_timeout: float = 20.0,
) -> TokenSet:
	"""Trade an authorization code for a :class:`TokenSet`. Raises
	:class:`OAuthTokenError` on a non-2xx response or a response with no
	access_token."""
	form = {
		"grant_type": "authorization_code",
		"code": code,
		"code_verifier": code_verifier,
		"redirect_uri": redirect_uri,
		"resource": resource,
		**_client_form(creds),
	}
	result = transport_module.http_form(
		discovery.token_endpoint,
		method="POST",
		form=form,
		transport=transport,
		egress_allowed=egress_allowed,
		connect_timeout=connect_timeout,
		read_timeout=read_timeout,
	)
	return _parse_token_response(result)


def refresh(
	discovery: Discovery,
	creds: ClientCreds,
	*,
	refresh_token: str,
	resource: str,
	transport: Callable,
	egress_allowed: Callable[[str], bool] | None = None,
	connect_timeout: float = 5.0,
	read_timeout: float = 20.0,
) -> TokenSet:
	"""Use ``refresh_token`` to get a new :class:`TokenSet`. Per OAuth 2.1, a
	server issuing ROTATING refresh tokens MAY omit ``refresh_token`` from the
	response when it means "unchanged"; when that happens the returned
	TokenSet carries the CALLER'S OLD ``refresh_token`` forward rather than
	``None``, so a caller never has to special-case a missing field itself."""
	form = {
		"grant_type": "refresh_token",
		"refresh_token": refresh_token,
		"resource": resource,
		**_client_form(creds),
	}
	result = transport_module.http_form(
		discovery.token_endpoint,
		method="POST",
		form=form,
		transport=transport,
		egress_allowed=egress_allowed,
		connect_timeout=connect_timeout,
		read_timeout=read_timeout,
	)
	token_set = _parse_token_response(result)
	if token_set.refresh_token is None:
		token_set = dataclasses.replace(token_set, refresh_token=refresh_token)
	return token_set


def validate_iss(returned_iss: str | None, recorded_issuer: str, iss_param_supported: bool) -> None:
	"""RFC 9207 mix-up-attack defense: validate the authorization response's
	``iss`` parameter against the ``issuer`` recorded from validated AS
	metadata, BEFORE the token exchange. Simple string comparison, no
	normalization - the same exact-match rule ``discover()`` uses for the
	issuer's own metadata.

	The table this implements exactly (spec-mandated, all four rows):

	  * supported + present  -> compare; raise on mismatch.
	  * supported + absent   -> reject (an AS that advertises support but
	    omits ``iss`` is not behaving as declared).
	  * not supported + present -> compare; raise on mismatch (an ``iss`` we
	    were not told to expect is still validated if the AS sent one).
	  * not supported + absent  -> proceed (nothing to check).

	Raises :class:`OAuthIssuerError` on any reject/mismatch."""
	if iss_param_supported and returned_iss is None:
		raise OAuthIssuerError(
			"iss_missing",
			"Authorization server declares iss support but the response carried no iss.",
		)
	if returned_iss is not None and returned_iss != recorded_issuer:
		raise OAuthIssuerError(
			"iss_mismatch", "Authorization response iss did not match the recorded issuer."
		)
