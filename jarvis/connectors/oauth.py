"""OAuth credential resolution for connectors — the *only* seam OAuth adds.

Wired into ``broker._credential`` (OAUTH_CONNECTORS_DESIGN.md §4, §6): a row
whose ``auth_method`` is OAuth resolves its bearer through
:func:`resolve_connector_token` here rather than the shipped ``credential``
Password field::

    def _credential(row):
        if oauth.is_oauth(row):
            token = oauth.resolve_connector_token(row)
            if not token:
                raise _BrokerError("connector_not_ready", ...)
            return token
        return row.get_password("credential")  # shipped path, unchanged

TWO ENGINES SIT BEHIND THAT ONE CALL (MCP_OAUTH_CLIENT_DESIGN.md §7). A row
carrying ``connected_app`` is the shipped v1 preset path (Frappe's Connected
App); a row carrying ``mcp_oauth_client`` is the discovery-driven engine, which
backs a catalog ``dcr``/``static`` preset as well as a Custom URL server.
:func:`resolve_connector_token` is the ONLY place that
branch is made, so the broker and the SPA's Test button can never disagree
about which engine a row uses.

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

from jarvis.connectors import mcp_oauth

# NOTE: no ``import frappe`` at module load — keep this file importable in the
# same contexts as the rest of ``jarvis.connectors`` (only ``broker`` imports
# frappe today). The real implementation imports frappe lazily inside the
# functions, exactly as ``broker`` accesses ``frappe.session.user``.

OAUTH_AUTH_METHOD = "OAuth"  # matches Jarvis Connector.auth_method Select option

#: The transport every outbound hop of the discovery engine goes through.
#: Defaults to the SSRF-guarded, IP-pinned client; tests swap in a scripted fake
#: so no socket is opened. Never replace this with plain ``requests`` - the pin
#: is what keeps a user-pasted server URL from reaching a private address.
MCP_OAUTH_TRANSPORT = mcp_oauth.open_pinned

#: Refresh this many seconds BEFORE a token actually expires, so a token that
#: would die mid-call is replaced first.
REFRESH_SKEW_S = 120

#: Read timeout for a refresh. Deliberately far tighter than the core's 20s
#: default: a refresh runs inside the broker's sub-30s plugin budget, before the
#: concurrency slot, and a slow sign-in service must not eat the call's budget.
REFRESH_READ_TIMEOUT_S = 8.0
REFRESH_CONNECT_TIMEOUT_S = 5.0

#: WALL-CLOCK ceiling for the whole refresh hop (connect + redirects + body), not
#: just one recv. This is the number the broker subtracts from its own call
#: budget, so a refresh can never quietly spend more of a chat turn than this.
REFRESH_TOTAL_TIMEOUT_S = 10.0


def is_oauth(row) -> bool:
	"""True when this connector authenticates via the OAuth Connect flow rather
	than a pasted API key. Unset / legacy rows are API-key (the shipped default),
	so this stays False for every row that exists today."""
	try:
		return (row.get("auth_method") or "").strip() == OAUTH_AUTH_METHOD
	except Exception:
		return False


def is_mcp_oauth(row) -> bool:
	"""True when this row signs in through the discovery engine rather than a
	Connected App. The two are told apart by WHICH link is set, not by
	``auth_method`` (both are "OAuth") - the connector controller guarantees a
	row never carries both."""
	if not is_oauth(row):
		return False
	try:
		return bool(row.get("mcp_oauth_client"))
	except Exception:
		return False


def resolve_connector_token(row, *, total_timeout: float = REFRESH_TOTAL_TIMEOUT_S) -> str | None:
	"""The one dispatcher between the two OAuth engines. Returns a live access
	token for the CURRENT impersonated user, or ``None`` when they have not
	finished connecting - callers map ``None`` to ``connector_not_ready``.

	``total_timeout`` is the wall clock a refresh (if one happens) may spend. The
	broker passes what its own call budget can afford to lose; the default suits
	a caller with no budget of its own, like the SPA's readiness check."""
	if is_mcp_oauth(row):
		return resolve_mcp_oauth_token(row, total_timeout=total_timeout)
	return resolve_access_token(row)


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


def resolve_mcp_oauth_token(row, *, total_timeout: float = REFRESH_TOTAL_TIMEOUT_S) -> str | None:
	"""Return a live access token for ``row``'s discovery-engine sign-in, for the
	CURRENT impersonated user, refreshing it first when it is about to expire and
	a refresh token exists.

	Returns ``None`` (never raises) when the user has not connected, the stored
	grant no longer matches the connector's address, or a refresh failed - the
	caller maps that to a friendly ``connector_not_ready`` rather than handing the
	outbound call a dead bearer.

	THE RESOURCE PIN is the no-passthrough rule (design section 2) made
	enforceable. A token is issued FOR one connector address and carries it; if
	the connector now points somewhere else, this refuses to hand the token over.
	That closes the re-point attack at the seam every caller shares, instead of
	relying on each write path to notice."""
	import frappe

	from jarvis.connectors import mcp_oauth_store

	try:
		connector = row.get("name")
		if not connector:
			return None
		token = mcp_oauth_store.load_token(connector, frappe.session.user)
		if token is None:
			return None
		access_token = token.get_password("access_token", raise_exception=False)
		if not access_token:
			return None
		resource = mcp_oauth.canonical_resource(row.get("base_url") or "")
		if (token.get("resource") or "") != resource:
			return None
		if not _expiring_soon(token.get("expires_at")):
			return access_token
		refresh_token = token.get_password("refresh_token", raise_exception=False)
		if not refresh_token:
			# Nothing to refresh with. Hand the stored token over and let the server
			# be the judge: some providers report an expiry but keep honouring the
			# token, and a needless "connect again" prompt is worse than one 401.
			return access_token
		return _refresh_mcp_oauth_token(connector, refresh_token, resource, total_timeout)
	except Exception:
		frappe.logger("jarvis.connectors").warning("connector sign-in token resolution failed", exc_info=True)
		return None


def _expiring_soon(expires_at) -> bool:
	"""True when ``expires_at`` is set and within :data:`REFRESH_SKEW_S`. An EMPTY
	expiry means the provider issued none (a long-lived token) and must never be
	read as "expired" - that is the v1 trap (module docstring rule 1) wearing the
	new engine's clothes."""
	if not expires_at:
		return False
	from frappe.utils import get_datetime, now_datetime

	return (get_datetime(expires_at) - now_datetime()).total_seconds() <= REFRESH_SKEW_S


def _refresh_mcp_oauth_token(
	connector: str, refresh_token: str, resource: str, total_timeout: float = REFRESH_TOTAL_TIMEOUT_S
) -> str | None:
	"""Refresh through the pinned transport and persist the rotation. Returns the
	new access token, or ``None`` on any failure - a sign-in problem, surfaced as
	re-consent, and never a breaker signal (it says nothing about endpoint health).

	``resource`` is the CANONICAL address, and it is what the refreshed token is
	pinned to. What goes on the wire is the sign-in service's own declared form of
	it (``mcp_oauth_store.resource_indicator``) - the two differ only by the
	pedantry canonicalization removes, and a server may compare the indicator
	against its own string.

	Deliberately NOT committed here: this runs inside ``broker._credential``,
	before the concurrency slot and inside a caller's transaction, and committing
	would prematurely persist whatever else that caller has in flight. The
	state-changing requests this rides on commit at the end of the request."""
	import frappe

	from jarvis.connectors import broker, mcp_oauth_store

	client = mcp_oauth_store.client_for(connector)
	if client is None or not client.get("client_id"):
		return None
	try:
		token_set = mcp_oauth.refresh(
			mcp_oauth_store.discovery_from_client(client),
			mcp_oauth_store.creds_from_client(client),
			refresh_token=refresh_token,
			resource=mcp_oauth_store.resource_indicator(client),
			transport=MCP_OAUTH_TRANSPORT,
			egress_allowed=broker._egress_allowed,
			connect_timeout=REFRESH_CONNECT_TIMEOUT_S,
			read_timeout=REFRESH_READ_TIMEOUT_S,
			total_timeout=total_timeout,
		)
	except Exception:
		# The core's errors carry a stable code and a message that never embeds a
		# token, code or secret, and a Python traceback carries no local values -
		# so this is safe to log with exc_info.
		frappe.logger("jarvis.connectors").warning("connector sign-in refresh failed", exc_info=True)
		return None
	mcp_oauth_store.save_token(connector, frappe.session.user, token_set, resource)
	return token_set.access_token
