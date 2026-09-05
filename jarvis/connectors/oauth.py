"""OAuth credential resolution for connectors — the *only* seam OAuth adds.

Wired into ``broker._credential`` (OAUTH_CONNECTORS_DESIGN.md §4, §6): a row
whose ``auth_method`` is OAuth resolves its bearer through
:func:`resolve_access_token` here rather than the shipped ``credential``
Password field::

    def _credential(row):
        if oauth.is_oauth(row):
            token = oauth.resolve_access_token(row)
            if not token:
                raise _BrokerError("connector_not_ready", ...)
            return token
        return row.get_password("credential")  # shipped path, unchanged

Two rules this module MUST honour (they are why OAuth lives here, not inline):

1. **Refresh only a genuinely refreshable token, before the concurrency slot and
   the 20s tool budget.** Many providers (a classic GitHub OAuth App among them)
   issue a long-lived access token with NO ``expires_in`` and NO ``refresh_token``;
   Frappe then reads ``expires_in`` as 0, so ``is_expired()`` is True one second
   later, and calling ``get_active_token`` on it fires a doomed refresh (null
   refresh token) that returns None and logs the secret. So we refresh ONLY when a
   real refresh token is present AND the cache is expired; otherwise we return the
   stored access token as-is. A refresh failure is an AUTH problem (surface
   ``connector_not_ready`` / re-consent), never an endpoint-health signal that
   feeds the circuit breaker.

2. **Refresh egress is Frappe-owned, and that's fine.** When a refresh does happen
   it goes through ``requests_oauthlib`` inside Frappe to the Connected App's
   operator-set ``token_uri`` (System-Manager-only config, never user input), so
   the SSRF/IP-pin guard - which exists to stop a USER aiming a connector at a
   private address - does not apply to it and is not reimplemented here. That guard
   stays on the MCP ``base_url``, the user-influenced address, exactly as today.
"""

from __future__ import annotations

# NOTE: no ``import frappe`` at module load — keep this file importable in the
# same contexts as the rest of ``jarvis.connectors`` (only ``broker`` imports
# frappe today). The real implementation imports frappe lazily inside the
# functions, exactly as ``broker`` accesses ``frappe.session.user``.

OAUTH_AUTH_METHOD = "OAuth"  # matches Jarvis Connector.auth_method Select option


def is_oauth(row) -> bool:
	"""True when this connector authenticates via the OAuth Connect flow rather
	than a pasted API key. Unset / legacy rows are API-key (the shipped default),
	so this stays False for every row that exists today."""
	try:
		return (row.get("auth_method") or "").strip() == OAUTH_AUTH_METHOD
	except Exception:
		return False


def resolve_access_token(row) -> str | None:
	"""Return a live access token for ``row``'s linked Connected App, for the
	CURRENT impersonated user (``frappe.session.user`` — the identity the broker
	already runs under and the key the per-user Token Cache is stored on),
	refreshing only a genuinely refreshable token per the module docstring rule 1.

	Returns ``None`` (never raises) when the row has no ``connected_app``, the
	Connected App is missing, the user has never finished connecting (no access
	token stored), or anything else goes wrong resolving/refreshing the token -
	the caller (``broker._credential``) maps a ``None`` to a friendly
	``connector_not_ready`` error rather than a broken/blank bearer reaching the
	outbound call."""
	import frappe

	try:
		name = row.get("connected_app")
		if not name:
			return None
		app = frappe.get_doc("Connected App", name)
		token_cache = app.get_token_cache(frappe.session.user)
		if not token_cache:
			return None
		# A state-only cache (user clicked Connect but never authorized) has no
		# access token yet - treat as not connected.
		access_token = token_cache.get_password("access_token", raise_exception=False)
		if not access_token:
			return None
		# Refresh ONLY a token that can actually be refreshed and is expired.
		# GitHub classic OAuth-App tokens carry no refresh token and no expiry, so
		# this stored access token is used as-is - never routed through
		# ``get_active_token``, whose refresh attempt would fail and leak the
		# client secret into the Error Log (see module docstring rule 1).
		refresh_token = token_cache.get_password("refresh_token", raise_exception=False)
		if refresh_token and token_cache.is_expired():
			fresh = app.get_active_token(frappe.session.user)
			if not fresh:
				return None
			return fresh.get_password("access_token", raise_exception=False) or None
		return access_token
	except Exception:
		frappe.logger("jarvis.connectors").warning("oauth token resolution failed", exc_info=True)
		return None
