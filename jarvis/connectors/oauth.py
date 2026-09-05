"""OAuth credential resolution for connectors — the *only* seam OAuth adds.

DORMANT SCAFFOLDING. Nothing imports this yet. It exists so the OAuth build
(task #6, gated on the owner handing over a GitHub OAuth App's client_id/secret —
see OAUTH_CONNECTORS_DESIGN.md §8) has a home that keeps the OAuth token dance
out of ``broker.py``'s hot path. The shipped API-key path in
``broker._credential`` is untouched until this is wired in and tested live.

Design contract (OAUTH_CONNECTORS_DESIGN.md §4, §6), so the eventual wiring is
a one-line branch in ``broker._credential``::

    def _credential(row):
        if _is_oauth(row):
            return oauth.resolve_access_token(row)  # <- this module
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


def resolve_access_token(row) -> str:  # pragma: no cover - not yet wired
	"""Return a live access token for ``row``'s linked Connected App, for the
	CURRENT impersonated user (``frappe.session.user`` — the identity the broker
	already runs under and the key the per-user Token Cache is stored on),
	refreshing proactively per the module docstring.

	NOT IMPLEMENTED until the OAuth build (needs the GitHub OAuth App handover,
	OAUTH_CONNECTORS_DESIGN.md §8). Wiring this into ``broker._credential`` before
	it is implemented and tested live would regress the shipped API-key path, so
	it deliberately raises rather than returning a broken value.
	"""
	raise NotImplementedError(
		"OAuth connector credential resolution is not implemented yet — "
		"see OAUTH_CONNECTORS_DESIGN.md (task #6, blocked on GitHub OAuth App handover)."
	)
