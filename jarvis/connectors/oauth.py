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

1. **Refresh proactively, before the concurrency slot and the 20s tool budget.**
   ``Connected App.get_active_token`` may make a token-endpoint round-trip; that
   round-trip must not eat into the outbound MCP call's deadline, and a refresh
   failure is an AUTH problem (surface ``connector_not_ready`` / re-consent),
   never an endpoint-health signal that feeds the circuit breaker.

2. **The token-endpoint call is egress too.** When we own the refresh path, it
   goes through the same ``broker._egress_allowed`` + SSRF IP-pin as any other
   outbound connection — no silent side channel to the provider.
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
	refreshing proactively via ``Connected App.get_active_token`` per the module
	docstring.

	Returns ``None`` (never raises) when the row has no ``connected_app``, the
	Connected App is missing, the user has never connected, or anything else
	goes wrong resolving/refreshing the token - the caller (``broker._credential``)
	maps a ``None`` to a friendly ``connector_not_ready`` error rather than a
	broken/blank bearer reaching the outbound call."""
	import frappe

	try:
		name = row.get("connected_app")
		if not name:
			return None
		app = frappe.get_doc("Connected App", name)
		token_cache = app.get_active_token(frappe.session.user)
		if not token_cache:
			return None
		return token_cache.get_password("access_token")
	except Exception:
		frappe.logger("jarvis.connectors").warning("oauth token resolution failed", exc_info=True)
		return None
