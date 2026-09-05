"""The Frappe side of the spec-compliant sign-in engine: the two DocTypes, the
in-flight auth state, and the translation between stored rows and the pure core
in ``jarvis.connectors.mcp_oauth`` (Phase B of MCP_OAUTH_CLIENT_DESIGN.md).

Everything here is an INTERNAL owned by the ``Jarvis Connector`` row the caller
already holds permissions on. Neither DocType grants the Jarvis User role
anything, so every read/write below runs ``ignore_permissions`` (or goes through
``frappe.db.*``, which never consults permissions) AFTER the caller-level gate
on the connector has run. ``connectors_api`` and ``broker`` are the only callers.

Three responsibilities, in the order they appear below:

  1. **In-flight auth state** - the ``state`` a browser carries to the provider
     and back. Deliberately NOT a DocType: it lives ~10 minutes, must be
     single-use, and must vanish on its own if the user abandons the flow. A
     cache entry does all three; a DocType would need a sweeper and would leave
     a row behind for every abandoned sign-in.
  2. **MCP OAuth Client** - the discovered, validated configuration for one
     connector, plus the client credentials we present to its sign-in service.
  3. **MCP OAuth Token** - one user's tokens for one connector.

``import frappe`` is lazy in every function, matching ``oauth.py``: this module
sits under ``jarvis.connectors``, where only ``broker`` imports frappe at load.
"""

from __future__ import annotations

import json

from jarvis.connectors.mcp_oauth import ClientCreds, Discovery

CLIENT_DT = "MCP OAuth Client"
TOKEN_DT = "MCP OAuth Token"

#: How long a started sign-in stays resumable. Long enough for a user to read a
#: consent screen and pick an account, short enough that an abandoned flow's
#: state is not sitting around.
STATE_TTL_S = 600

_STATE_PREFIX = "jarvis:mcp_oauth_state:"


# --------------------------------------------------------------------------- #
# 1. in-flight auth state (single-use, self-expiring)
# --------------------------------------------------------------------------- #
def put_state(state: str, record: dict) -> None:
	"""Bind ``state`` to the server-side record the callback will need. The
	record carries the code verifier, the issuer to validate the response
	against, and the user who started the flow - none of which may be taken from
	the callback's own query string, which is attacker-influenced."""
	import frappe

	frappe.cache().set_value(_state_key(state), dict(record), expires_in_sec=STATE_TTL_S)


def consume_state(state: str) -> dict | None:
	"""Read and destroy ``state``'s record, returning it only to the caller that
	actually removed the key. Returns ``None`` for an unknown, expired or
	already-consumed state.

	The removal is what makes a callback single-use, so it is an ATOMIC claim
	(redis ``UNLINK`` reports how many keys it actually removed) rather than a
	read-then-delete pair: two replays arriving together would both pass a
	read-then-delete, and only one can win an unlink. If the cache backend has no
	unlink, fall back to the plain delete - a degraded backend must not fail the
	sign-in open."""
	import frappe

	if not state:
		return None
	cache = frappe.cache()
	key = _state_key(state)
	record = cache.get_value(key, expires=True)
	if not _claim(cache, key):
		return None
	return record if isinstance(record, dict) else None


def _state_key(state: str) -> str:
	return _STATE_PREFIX + state


def _claim(cache, key: str) -> bool:
	import frappe

	try:
		made_key = cache.make_key(key)
		removed = cache.unlink(made_key)
		frappe.local.cache.pop(made_key, None)
		return bool(removed)
	except Exception:
		cache.delete_value(key)
		return True


# --------------------------------------------------------------------------- #
# 2. MCP OAuth Client
# --------------------------------------------------------------------------- #
def client_for(connector: str):
	"""This connector's client document, or ``None``. The client is NAMED after
	its connector, so this is a primary-key read, not a search."""
	import frappe

	if not connector or not frappe.db.exists(CLIENT_DT, connector):
		return None
	return frappe.get_doc(CLIENT_DT, connector)


def save_client(connector: str, discovery: Discovery, creds: ClientCreds, scope: str):
	"""Create or refresh ``connector``'s client from a validated discovery result.
	Only ever called with a :class:`Discovery` the core has already run its
	anti-phishing gates over - nothing here re-checks a URL, so nothing here may
	be handed an unvalidated one."""
	import frappe
	from frappe.utils import now_datetime

	fields = {
		"registration_mode": creds.mode,
		"client_id": creds.client_id or "",
		"issuer": discovery.issuer,
		"authorization_endpoint": discovery.authorization_endpoint,
		"token_endpoint": discovery.token_endpoint,
		"registration_endpoint": discovery.registration_endpoint or "",
		"resource": discovery.resource,
		"scope": scope,
		"iss_param_supported": 1 if _iss_supported(discovery) else 0,
		"as_metadata": frappe.as_json(discovery.raw_as_metadata or {}),
		"discovered_at": now_datetime(),
	}
	doc = client_for(connector)
	is_new = doc is None
	if is_new:
		doc = frappe.get_doc({"doctype": CLIENT_DT, "connector": connector, **fields})
	else:
		doc.update(fields)
	# Password fields: only overwrite when this registration actually produced one,
	# so re-running discovery on a static client never wipes an admin's secret.
	if creds.client_secret:
		doc.client_secret = creds.client_secret
	if creds.registration_access_token:
		doc.registration_access_token = creds.registration_access_token
	if is_new:
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return doc


def discovery_from_client(client) -> Discovery:
	"""Rebuild the core's :class:`Discovery` from the stored snapshot, so a token
	exchange or refresh never re-fetches metadata from a host the connector's own
	URL chose. Only ``authorization_endpoint`` and ``token_endpoint`` are read by
	the flow functions this feeds; ``scopes_supported`` is left empty because the
	scope actually being requested is stored on its own field."""
	return Discovery(
		resource=client.get("resource") or "",
		authorization_servers=[client.get("issuer")] if client.get("issuer") else [],
		scopes_supported=[],
		issuer=client.get("issuer") or "",
		authorization_endpoint=client.get("authorization_endpoint") or "",
		token_endpoint=client.get("token_endpoint") or "",
		registration_endpoint=client.get("registration_endpoint") or None,
		raw_as_metadata=_parse_json(client.get("as_metadata")),
		challenge_scope=client.get("scope") or None,
	)


def creds_from_client(client) -> ClientCreds:
	"""The credentials presented to the token endpoint. The registration access
	token is deliberately NOT carried here: it authenticates management of the
	registration itself and must never ride along on a token request."""
	return ClientCreds(
		client_id=client.get("client_id") or "",
		client_secret=client.get_password("client_secret", raise_exception=False) or None,
		registration_access_token=None,
		mode=client.get("registration_mode") or "static",
	)


def _iss_supported(discovery: Discovery) -> bool:
	metadata = discovery.raw_as_metadata or {}
	return bool(metadata.get("authorization_response_iss_parameter_supported"))


# --------------------------------------------------------------------------- #
# 3. MCP OAuth Token
# --------------------------------------------------------------------------- #
def token_name(connector: str, user: str) -> str:
	"""The docname ``autoname: format:{connector}-{user}`` produces. Computing it
	rather than querying is the per-user isolation: the broker runs as the
	impersonated user and can only name that user's row."""
	return f"{connector}-{user}"


def load_token(connector: str, user: str):
	"""``(connector, user)``'s token row, or ``None``."""
	import frappe

	name = token_name(connector, user)
	if not frappe.db.exists(TOKEN_DT, name):
		return None
	return frappe.get_doc(TOKEN_DT, name)


def save_token(connector: str, user: str, token_set, resource: str, requested_scope: str = ""):
	"""Upsert ``(connector, user)``'s tokens from a token response. ``resource``
	pins the row to the connector address the grant was issued for -
	``oauth.resolve_mcp_oauth_token`` refuses to use a token whose pin no longer
	matches the connector's address."""
	import frappe
	from frappe.utils import add_to_date, now_datetime

	expires_at = None
	if token_set.expires_in:
		expires_at = add_to_date(now_datetime(), seconds=int(token_set.expires_in))

	doc = load_token(connector, user)
	is_new = doc is None
	if is_new:
		doc = frappe.get_doc({"doctype": TOKEN_DT, "connector": connector, "user": user})
	doc.access_token = token_set.access_token
	if token_set.refresh_token:
		# A rotating provider MAY omit refresh_token to mean "unchanged"; the core
		# already carries the old one forward, so an empty value here really is
		# "there is none" and must not clobber a stored one.
		doc.refresh_token = token_set.refresh_token
	doc.expires_at = expires_at
	doc.granted_scopes = token_set.scope or requested_scope or ""
	doc.resource = resource
	doc.token_type = token_set.token_type or ""
	if is_new:
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return doc


def delete_token(connector: str, user: str) -> bool:
	"""Drop ``(connector, user)``'s tokens. True when there was one to drop."""
	import frappe

	name = token_name(connector, user)
	if not frappe.db.exists(TOKEN_DT, name):
		return False
	frappe.delete_doc(TOKEN_DT, name, ignore_permissions=True, force=True, delete_permanently=True)
	return True


def purge_connector(connector: str) -> None:
	"""Drop every sign-in internal for ``connector``. Called from the connector's
	own ``on_trash``: both DocTypes link back to it, so Frappe would refuse the
	delete otherwise, and a surviving token row would outlive the connector it
	authorizes."""
	import frappe

	for name in frappe.get_all(TOKEN_DT, filters={"connector": connector}, pluck="name"):
		frappe.delete_doc(TOKEN_DT, name, ignore_permissions=True, force=True, delete_permanently=True)
	if frappe.db.exists(CLIENT_DT, connector):
		frappe.delete_doc(CLIENT_DT, connector, ignore_permissions=True, force=True, delete_permanently=True)


def _parse_json(raw) -> dict:
	if isinstance(raw, dict):
		return raw
	if not raw:
		return {}
	try:
		parsed = json.loads(raw)
	except (TypeError, ValueError):
		return {}
	return parsed if isinstance(parsed, dict) else {}
