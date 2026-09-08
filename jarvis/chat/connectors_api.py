"""MCP Connectors - SPA API (MCP_CONNECTORS_PLAN.md P3; OAuth tier v1 per
OAUTH_CONNECTORS_DESIGN.md).

The SPA's Connectors settings pane talks to the endpoints below. Every
call runs as the logged-in user (``@require_jarvis_user``, no impersonation
here — that already happened, if at all, one layer up in the chat dispatcher)
so ``Jarvis Connector`` row permissions
(``jarvis.chat.connector_permissions``) and the controller's Shared-scope
create/widen gate (``jarvis.jarvis.doctype.jarvis_connector.jarvis_connector
._guard_shared_scope``) apply exactly as they do from the Desk. This module
never bypasses them for a user-authored write; it only reaches for
``ignore_permissions``/``frappe.db.set_value`` for SERVER-DERIVED state (a
fresh ``tools/list`` result and the annotations-driven defaults computed from
it) after the caller-level gate has already run — see ``test_connector`` and
``_replace_allowed_actions`` below.

Real MCP work (the outbound call, SSRF guard, circuit breaker) lives entirely
in ``jarvis.connectors.broker`` (a parallel P1 change, not this file) — this
module resolves a row, checks a permission, and reads/writes plain Frappe
fields around that one call.

Endpoints:
  * ``list_connectors``       - the pane's two-section list (Shared + Mine),
    plus ``catalog``: the in-app provider list the SPA builds its preset picker
    from (``jarvis.connectors.catalog.to_public``, public fields only).
  * ``add_connector``         — create; presets are pinned to their vendor
    endpoint server-side (see ``_PRESET_BASE_URLS``) so "Custom URL" is the
    ONLY way a caller's own ``base_url`` ever reaches a saved row — otherwise
    ``allow_custom_urls=0`` would be decorative.
  * ``test_connector``        — the "Test connection" button: runs a real
    ``initialize`` + ``tools/list`` through the broker, and on success writes
    ``tools_cache`` and MERGES the ``allowed_actions`` table (existing
    choices survive; a newly-seen action defaults to allowed only if the
    server marked it read-only and non-destructive; a vanished action drops).
  * ``set_allowed_actions``   — the picker's Save: takes ONLY the ``allowed``
    bit from the client per action; ``read_only``/``destructive`` are always
    recomputed from ``tools_cache``, never trusted from the request.
  * ``update_connector``      — edit label/base_url(Custom URL only)/
    credential/enabled; changing the endpoint or the credential invalidates
    the last test (and, for a new endpoint, the whole tools/allowed-actions
    table — a stale cache from a different server would just make the broker
    deny everything as ``action_unknown``, which is confusing, not safer).
  * ``delete_connector``.
  * ``connect_oauth``         - OAuth tier: returns the provider's authorize
    URL for an OAuth-auth-method row. Every OAuth row returns through
    ``mcp_oauth_callback`` below.
  * ``disconnect_oauth``      - deletes the CURRENT user's stored sign-in for an
    OAuth row (per-user, idempotent).
  * ``probe_connector_auth``  - Custom URL sign-in discovery, WITHOUT creating
    anything: "does this address need a sign-in, and where?".
  * ``set_oauth_client_credentials`` - admin-only, static-mode only: the
    client_id/secret an administrator registered at the provider by hand.
  * ``mcp_oauth_callback``    - the ONE redirect URI the discovery engine
    registers and returns to. Login-required (never allow_guest).

ONE SIGN-IN ENGINE (MCP_OAUTH_CLIENT_DESIGN.md), and the CATALOG decides whether
a preset may use it. ``jarvis.connectors.catalog`` gives every preset an ``auth``
class, and that class is the whole routing rule for ``auth_method="OAuth"``:

  * ``dcr`` / ``static`` - the spec-compliant discovery engine in
    ``jarvis.connectors.mcp_oauth`` (+ ``mcp_oauth_store``), the SAME code path a
    ``Custom URL`` row takes, except that the address discovery runs against is
    the catalog's pinned one rather than the caller's. A ``static`` preset that
    pins its own endpoints in the catalog (GitHub) is a bring-your-own-app
    provider: its client is SEEDED from those endpoints with no discovery, and an
    administrator (or a Personal row's owner) pastes the client id/secret they
    registered at the vendor. A ``static`` preset that publishes discovery
    metadata (Slack, Box, ...) still runs discovery.
  * ``token`` / ``open`` - no sign-in exists, so asking for OAuth is refused
    (here AND in the connector controller, so a raw DocType write cannot take
    the shortcut the API refuses).

Every OAuth row carries a ``mcp_oauth_client`` link, never anything else, and the
connector controller guarantees a key-only row carries no link.

``set_custom_url_policy`` is deliberately NOT here — MCP_CONNECTORS_PLAN.md's
UI/UX decision #2 puts that admin control on the Jarvis Settings Desk form.

Boot wiring: ``connector_flags()`` (not whitelisted) is also imported by
``jarvis/www/jarvis.py`` to ship ``connectors_allow_custom_urls`` in the SPA
boot payload, so the nav can read the Custom URL policy without a round trip.
``list_connectors`` returns the same flag for callers that only have the API
(e.g. a stale boot cache).
"""

from __future__ import annotations

import html
import json
import re
import secrets
import time
from datetime import timezone
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, get_system_timezone, get_url, now_datetime

from jarvis.connectors import action_rows, broker, catalog, mcp_oauth, mcp_oauth_store, oauth
from jarvis.permissions import has_jarvis_admin_access, require_jarvis_user

CONNECTOR = "Jarvis Connector"
ACTION_DT = "Jarvis Connector Action"
SETTINGS = "Jarvis Settings"

#: The transport the discovery engine's outbound hops go through here (the
#: SSRF-guarded, IP-pinned client). Tests swap in a scripted fake; the broker's
#: own refresh path has the twin hook on ``oauth.MCP_OAUTH_TRANSPORT``.
MCP_OAUTH_TRANSPORT = mcp_oauth.open_pinned

#: The ONE redirect URI this app registers and returns to. It must be
#: byte-identical at registration time, at authorize time and at callback time -
#: providers exact-match it - so every producer goes through
#: :func:`oauth_redirect_uri` rather than building it again.
_CALLBACK_METHOD = "jarvis.chat.connectors_api.mcp_oauth_callback"

#: Where the browser lands after a sign-in attempt, success or failure. The SPA
#: has one place to handle the return.
_SPA_CONNECTORS_PATH = "/jarvis?settings=connectors"

# Per-user rate limit on the outbound Test-connection probe (calendar-minute
# bucket). The probe fires a real network call to a user-chosen host, so it must
# not be spammable; the broker's circuit breaker + concurrency cap protect the
# WORKER, this protects against a single user hammering the button.
_TEST_RATE_PER_MIN = 20

# Vendor MCP endpoints come from ``jarvis.connectors.catalog``, the in-app
# provider list that is the single source of truth for which apps may be
# connected and where each one lives. Pinned SERVER-SIDE: add_connector /
# update_connector never let a caller point a catalog preset anywhere else,
# which is what makes the Custom URL policy (Jarvis Settings.allow_custom_urls)
# mean anything at all.
#
# ``base_urls``/``keys`` deliberately INCLUDE entries the catalog has disabled,
# so an already-saved row still resolves its endpoint; ``_PRESETS`` (the create
# allowlist) does not, so a disabled entry can never back a NEW connector.
_PRESET_BASE_URLS = catalog.base_urls()
_PRESET_KEYS = catalog.keys()
_PRESETS = (*catalog.preset_names(), catalog.CUSTOM_URL)
_SCOPES = ("Shared", "Personal")
_AUTH_METHODS = ("API Key", "OAuth")

#: The catalog auth classes the sign-in engine serves. A preset outside this set
#: has no sign-in at all.
_DISCOVERY_AUTH = (catalog.AUTH_DCR, catalog.AUTH_STATIC)

#: ``auth_class`` for a row whose preset the catalog does not carry: a Custom URL
#: row, or a legacy row on a preset that has since been dropped.
_AUTH_CLASS_CUSTOM = "custom"

_DESC_MAX = 500


# --------------------------------------------------------------------------- #
# settings flags (shared with jarvis/www/jarvis.py boot payload)
# --------------------------------------------------------------------------- #
def connector_flags() -> dict:
	"""``{allow_custom_urls}`` off ``Jarvis Settings``. NOT whitelisted — called
	directly by ``list_connectors`` below and by ``www/jarvis.py`` for the boot
	payload (the custom-URL policy).

	``allow_custom_urls`` defaults to 1 but — per its own field description —
	Single defaults are NOT backfilled onto an existing site's ``tabSingles``
	row on migrate, and ``get_single_value`` coerces a genuinely missing row to
	0/off via ``cint`` — indistinguishable from an admin explicitly turning it
	off. So it needs the same tabSingles row-existence probe
	``personalise_api._single_bool`` uses, treating "no row at all" as ON."""
	return {
		"allow_custom_urls": _single_bool("allow_custom_urls", True),
	}


def _single_bool(field: str, default: bool) -> bool:
	row = frappe.db.sql(
		"select value from tabSingles where doctype=%s and field=%s",
		(SETTINGS, field),
	)
	if not row:
		return default
	return bool(cint(row[0][0]))


def _over_test_rate_limit(user: str) -> bool:
	"""Per-user calendar-minute bucket for the Test-connection probe. Atomic
	``incrby`` (race-free under concurrent requests, and not fooled by frappe's
	request-local cache), self-expiring so old buckets never accumulate. Mirrors
	``api_errors._over_report_rate_limit``. No-op under tests."""
	if frappe.flags.in_test:
		return False
	bucket = int(time.time()) // 60
	# incrby is a raw redis-py call that bypasses RedisWrapper's site prefix, so
	# prefix the site ourselves - otherwise a multi-site bench sharing one Redis
	# would let two sites' same-named users share a bucket.
	key = f"{frappe.local.site}:jarvis.connector_test.{user}.{bucket}"
	count = frappe.cache.incrby(key, 1)
	if count == 1:
		frappe.cache.expire(key, 120)
	return count > _TEST_RATE_PER_MIN


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _clip(text: str, length: int) -> str:
	text = text or ""
	return text if len(text) <= length else text[:length]


def _slug(text: str) -> str:
	"""Lowercase [a-z0-9_] slug used as a Custom URL connector's uniqueness key
	when the SPA does not send one (it derives from the host, e.g.
	``mcp.example.com`` -> ``mcp_example_com``). The controller's own
	``_normalize_key`` re-validates this, so an empty result fails cleanly there
	with "Connector Key is required" rather than saving a keyless row."""
	return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def _parse_json(raw, default):
	"""Tolerant str-or-native payload parse (``personalise_api``'s idiom): the
	SPA's ``call()`` wrapper posts list/dict params as form fields, so a
	``list``-typed param can arrive as either a real list or its JSON string."""
	if raw in (None, ""):
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(raw)
	except Exception:
		try:
			return frappe.parse_json(raw)
		except Exception:
			return default


def _action_summary(allowed: int, total: int) -> dict:
	return {"allowed": allowed, "total": total}


def _action_summaries(parent_names: list[str]) -> dict[str, dict]:
	"""One bounded query mapping ``connector name -> {allowed, total}`` over
	its ``allowed_actions`` children (``jarvis.connectors.action_rows``, shared
	with the agent's discovery tool) — avoids an N+1 when listing."""
	out: dict[str, dict] = {}
	for parent, rows in action_rows.by_parent(parent_names, ["allowed"]).items():
		out[parent] = _action_summary(sum(1 for row in rows if row["allowed"]), len(rows))
	return out


def _auth_class(preset: str) -> str:
	"""The catalog's connection class for ``preset`` (``dcr``/``static``/``token``/
	``open``), or ``"custom"`` for a Custom URL row and for any preset the catalog
	no longer carries. Shipped on every list row so the SPA reads a row's flow off
	the row instead of re-deriving it from the catalog."""
	return catalog.auth_of(preset) or _AUTH_CLASS_CUSTOM


def _uses_discovery_engine(preset: str) -> bool:
	"""True when an OAuth row for ``preset`` is backed by the sign-in engine: a
	Custom URL, or a catalog preset whose class is ``dcr``/``static``. The connector
	controller re-derives the same rule from the same catalog, so the API and the
	last-line guard cannot drift."""
	return preset == catalog.CUSTOM_URL or catalog.auth_of(preset) in _DISCOVERY_AUTH


def oauth_redirect_uri() -> str:
	"""The single redirect URI the discovery engine uses everywhere: dynamic
	registration, the authorize URL, and the callback's own token exchange. A
	provider exact-matches this string, so all three MUST agree.

	TRAP: ``get_url()`` reads site_config, which can legitimately differ from the
	host the browser actually used (an aliased or e2e host). That is tolerable
	here in a way it was not for the shipped path's ``success_uri`` - this value
	is registered WITH the provider and compared by it, so it has to be the
	site's own canonical URL rather than whatever host this particular request
	arrived on."""
	return f"{get_url()}/api/method/{_CALLBACK_METHOD}"


def _error(code: str, message: str) -> dict:
	return {"ok": False, "error": {"code": code, "message": message}}


def _host(url: str | None) -> str:
	return (urlparse(url or "").hostname or "").strip()


def _utc_iso(dt) -> str | None:
	"""A frappe-stored, system-timezone-naive datetime (``now_datetime()``'s shape,
	and a document's ``modified``) rendered as a UTC ISO-8601 string ending in
	``+00:00``. The SPA opens the sign-in tab with ``connect_oauth``'s ``started_at``
	and then polls ``oauth_signin_status`` for ``connected_at``; both go through here
	so ``connected_at > started_at`` compares in ONE frame no matter what timezone
	the site itself runs in. ``None`` in, ``None`` out."""
	if not dt:
		return None
	if isinstance(dt, str):
		dt = get_datetime(dt)
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=ZoneInfo(get_system_timezone()))
	return dt.astimezone(timezone.utc).isoformat()


# Stable engine error code -> friendly copy. NO protocol words: the person
# reading this pasted a URL and pressed a button, and "the sign-in service"
# is a thing they can act on in a way "the authorization server metadata" is
# not. Codes come from the core's typed errors and from the SSRF guard's own
# ``kind``, which OAuthTransportError mirrors.
_OAUTH_ERROR_MESSAGES = {
	# Kept although the engine no longer raises it: a 401 that names nothing now
	# falls back to the two default document locations, and only a miss on BOTH
	# is reported (as no_resource_metadata).
	"no_www_authenticate": "This app did not say how to sign in to it.",
	"no_resource_metadata": "This app did not say where to sign in.",
	"resource_metadata_unavailable": "We could not read this app's sign-in details.",
	"resource_mismatch": "This app's sign-in details do not match the address you entered.",
	"no_authorization_servers": "This app did not name a sign-in service.",
	"as_metadata_unavailable": "We could not read the sign-in service's details.",
	"issuer_mismatch": "The sign-in service's details do not match its address.",
	"missing_endpoint": "The sign-in service's details are incomplete.",
	"insecure_endpoint": "The sign-in service is not using a secure address.",
	"insecure_metadata": "This app's sign-in details are not on a secure address.",
	"malformed_metadata": "We could not make sense of this app's sign-in details.",
	"timeout": "This app took too long to answer.",
	"registration_failed": "The sign-in service would not set this workspace up.",
	"no_client_id": "The sign-in service did not return the details we need.",
	"token_request_failed": "The sign-in service rejected the sign-in.",
	"no_access_token": "The sign-in service did not complete the sign-in.",
	"response_too_large": "The sign-in service sent more data than we accept.",
	"invalid_url": "That address is not valid.",
	"unresolved": "That address could not be found.",
	"blocked_address": "This connector address is not permitted.",
	"egress_denied": "This connector address is not permitted by your administrator's policy.",
	"connect_failed": "The connector could not be reached.",
	"too_many_redirects": "That address redirects too many times.",
	"cross_host_redirect": "That address redirects somewhere we cannot follow.",
	"insecure_redirect": "That address redirects to an insecure address.",
}


#: Codes that DO carry a provider-supplied detail worth showing (see
#: mcp_oauth.errors.OAuthError.detail): a hop the provider answered but
#: refused, not a guard rejection or a validation gate of our own.
_OAUTH_ERROR_DETAIL_CODES = {"registration_failed", "token_request_failed"}


def _oauth_error_message(code: str, detail: str = "") -> str:
	"""The friendly sentence for ``code``, with the provider's OWN reason
	folded in parenthetically when one was captured (``detail`` - see
	``OAuthError.detail``) and the code is one that can carry it. The friendly
	sentence always comes first; nothing here adds a protocol word of our
	own - whatever text follows is the provider's, passed through as-is."""
	message = _OAUTH_ERROR_MESSAGES.get(code, "We could not set up sign-in for this address.")
	if detail and code in _OAUTH_ERROR_DETAIL_CODES:
		return f"{message.rstrip('.')} ({detail.rstrip('.')})."
	return message


def _mcp_oauth_status(doc) -> dict:
	"""The sign-in state for a row backed by an ``MCP OAuth Client``.
	``oauth_configured`` means we hold a client_id to sign in WITH;
	``needs_static_client`` means the sign-in service does not self-register, so
	an administrator (or a Personal row's owner) has to register their own app
	there and paste the result in.

	Presence-only: a stale or expired token still reads as connected for display,
	and nothing here ever triggers a refresh. The broker is what enforces liveness
	on an actual call."""
	connector = doc.get("name")
	client = mcp_oauth_store.client_for(connector)
	if client is None:
		return {
			"oauth_configured": False,
			"oauth_connected": False,
			"signin_host": "",
			"needs_static_client": False,
		}
	client_id = (client.get("client_id") or "").strip()
	is_static = (client.get("registration_mode") or "static") == "static"
	token = mcp_oauth_store.load_token(connector, frappe.session.user)
	status = {
		"oauth_configured": bool(client_id),
		"oauth_connected": bool(token and token.get_password("access_token", raise_exception=False)),
		"signin_host": _host(client.get("issuer")),
		"needs_static_client": is_static and not client_id,
	}
	if is_static:
		# An admin registering by hand needs the exact callback to paste at the
		# provider, so ship it with the row rather than making them find it.
		status["oauth_redirect_uri"] = oauth_redirect_uri()
	return status


def _oauth_status(doc) -> dict:
	"""``{oauth_configured, oauth_connected, signin_host}`` for an
	OAuth-auth-method row.

	``signin_host`` is the confused-deputy defense (design section 6): it shows
	the user WHERE they are about to sign in, and reads the same whether the row is
	a preset or a Custom URL.

	Every OAuth row is backed by the sign-in engine's ``MCP OAuth Client``; a row
	whose client has not been seeded yet has nothing to sign in with and reads as
	not configured.

	A row on a ``static`` preset that has not been seeded yet is NOT a dead end: it
	reads as ``needs_static_client`` so the SPA shows the credentials block (with the
	callback URL to register at the provider) rather than a dead "Setup needed", and
	:func:`connect_oauth` / :func:`set_oauth_client_credentials` seed the client the
	moment the user acts."""
	if doc.get("mcp_oauth_client"):
		return _mcp_oauth_status(doc)
	preset = (doc.get("preset") or "").strip()
	if catalog.auth_of(preset) == catalog.AUTH_STATIC:
		provider = catalog.by_name(preset)
		return {
			"oauth_configured": False,
			"oauth_connected": False,
			"signin_host": _host(provider.issuer) if provider and provider.issuer else "",
			"needs_static_client": True,
			"oauth_redirect_uri": oauth_redirect_uri(),
		}
	return {"oauth_configured": False, "oauth_connected": False, "signin_host": ""}


def _connector_summary(doc) -> dict:
	"""Same item shape as a ``list_connectors`` row, built from a loaded
	Document (add_connector/update_connector's return value). NEVER includes
	``credential`` — nothing below reads it."""
	children = doc.get("allowed_actions") or []
	total = len(children)
	allowed = sum(1 for c in children if c.get("allowed"))
	last_test_at = doc.get("last_test_at")
	auth_method = doc.get("auth_method") or "API Key"
	summary = {
		"name": doc.name,
		"key": doc.key,
		"label": doc.label,
		"preset": doc.preset,
		"auth_class": _auth_class(doc.preset),
		"base_url": doc.base_url,
		"scope": doc.scope,
		"enabled": bool(doc.enabled),
		"auth_method": auth_method,
		"last_test_status": doc.get("last_test_status") or "",
		"last_test_at": str(last_test_at) if last_test_at else None,
		"allowed_actions": _action_summary(allowed, total),
	}
	if auth_method == oauth.OAUTH_AUTH_METHOD:
		summary.update(_oauth_status(doc))
	return summary


def _cached_tools(doc) -> list[dict]:
	"""Parse ``tools_cache`` back into a list of tool dicts, same defensive
	shape ``jarvis.connectors.policy._input_schema`` reads (``{"tools": [...]}``
	or a bare array)."""
	cache = doc.get("tools_cache")
	if not cache:
		return []
	try:
		data = json.loads(cache) if isinstance(cache, str) else cache
	except (TypeError, ValueError):
		return []
	tools = data.get("tools") if isinstance(data, dict) else data
	return [t for t in (tools or []) if isinstance(t, dict)]


def _tool_flags(tool: dict) -> tuple[bool, bool]:
	"""Derive (read_only, destructive) from a tool's ``annotations`` (MCP spec:
	``readOnlyHint``/``destructiveHint``, both OPTIONAL and untrusted hints from
	the server). Absent ``destructiveHint`` reads as False here — safe only
	because a non-read-only action still needs an explicit admin
	``allowed=1`` (via ``set_allowed_actions``) before the broker's own
	``policy.action_decision`` will ever let it run; nothing about this
	default alone grants access."""
	annotations = tool.get("annotations") or {}
	if not isinstance(annotations, dict):
		return False, False
	read_only = bool(annotations.get("readOnlyHint"))
	destructive = False if read_only else bool(annotations.get("destructiveHint"))
	return read_only, destructive


def _merge_allowed_actions(existing_children, tools: list[dict]) -> list[dict]:
	"""Build the new ``allowed_actions`` row set from a fresh ``tools/list``.

	For an action that ALREADY has a stored child row, the stored
	``allowed``/``read_only``/``destructive`` flags are PRESERVED as-is; they are
	NOT recomputed from the server's ``annotations``. This closes a relabel attack:
	a compromised server could otherwise mark a known destructive action
	``readOnlyHint: true`` on the next re-test and have ``policy.action_decision``
	auto-allow it (it auto-allows read-only, non-destructive actions). Re-testing
	must never silently revoke an admin's earlier grant either, and preserving the
	stored row does both.

	A NEWLY-seen action derives its flags from the server annotations and defaults
	to ``allowed = read_only`` (read-only pre-checked, writes off -
	MCP_CONNECTORS_PLAN.md UI/UX decision #3); this initial trust is unavoidable
	(the action has never been seen) and a non-read-only default still needs an
	explicit admin grant to run. An action that vanished from the server's tool
	list is simply not re-added."""
	existing_by_action = {c.get("action"): c for c in existing_children if c.get("action")}
	merged: list[dict] = []
	seen: set[str] = set()
	for tool in tools:
		action = (tool.get("name") or "").strip()
		if not action or action in seen:
			continue
		seen.add(action)
		prior = existing_by_action.get(action)
		if prior is not None:
			# Trust the STORED flags, never the server's fresh annotations.
			read_only = bool(prior.get("read_only"))
			destructive = bool(prior.get("destructive"))
			allowed = bool(prior.get("allowed"))
		else:
			read_only, destructive = _tool_flags(tool)
			allowed = read_only
		merged.append(
			{
				"action": action,
				"allowed": 1 if allowed else 0,
				"read_only": 1 if read_only else 0,
				"destructive": 1 if destructive else 0,
				"description": _clip((tool.get("description") or "").strip(), _DESC_MAX),
			}
		)
	return merged


def _replace_allowed_actions(parent_name: str, actions: list[dict]) -> None:
	"""Rewrite ``parent_name``'s ``allowed_actions`` table from ``actions``
	(each already ``{action, allowed, read_only, destructive, description}``).
	Delete-then-rebuild by hand rather than ``doc.save()`` — the caller
	(``test_connector``/``set_allowed_actions``) has already written the
	parent's own changed fields via a targeted ``frappe.db.set_value``, and a
	full parent ``save()`` here would silently overwrite those with whatever
	was in memory before that write. ``ignore_permissions=True`` because this
	IS the server-derived write the caller-level gate (read for a test,
	write for an explicit save) already authorized — mirrors
	``onboarding._clear_llm_secrets``'s delete-then-rebuild idiom for a
	Single's child table."""
	frappe.db.delete(
		ACTION_DT, {"parenttype": CONNECTOR, "parent": parent_name, "parentfield": "allowed_actions"}
	)
	for idx, action in enumerate(actions, start=1):
		frappe.get_doc(
			{
				"doctype": ACTION_DT,
				"parenttype": CONNECTOR,
				"parent": parent_name,
				"parentfield": "allowed_actions",
				"idx": idx,
				**action,
			}
		).insert(ignore_permissions=True)
	frappe.clear_document_cache(CONNECTOR, parent_name)


# --------------------------------------------------------------------------- #
# 1. list
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def list_connectors() -> dict:
	"""``{allow_custom_urls, oauth_redirect_uri, catalog, shared, mine}``.
	``frappe.get_list`` (not ``get_all``) so ``connector_query_conditions`` scopes
	the query the same way the Desk list view is scoped: every Shared row plus the
	caller's own Personal rows, nothing more. Never selects ``credential``.

	``oauth_redirect_uri`` is the site-wide, non-secret callback address (see
	:func:`oauth_redirect_uri`), shipped unconditionally so the SPA can show a
	bring-your-own-app preset's "register your app" step BEFORE any row exists,
	not only after a row's own ``_oauth_status``/``_mcp_oauth_status`` carries it.

	``catalog`` is the in-app provider list (public fields only, no ``base_url``,
	no secrets, disabled entries absent) the SPA builds its preset picker, logos,
	hints, help links and per-preset flow from, so the flow a preset takes is
	decided once, here, rather than duplicated in the client."""
	flags = connector_flags()
	rows = frappe.get_list(
		CONNECTOR,
		fields=[
			"name",
			"key",
			"label",
			"preset",
			"base_url",
			"scope",
			"enabled",
			"auth_method",
			"mcp_oauth_client",
			"last_test_status",
			"last_test_at",
		],
		order_by="scope asc, label asc",
	)
	summaries = _action_summaries([r["name"] for r in rows])

	shared: list[dict] = []
	mine: list[dict] = []
	for row in rows:
		row["allowed_actions"] = summaries.get(row["name"], _action_summary(0, 0))
		row["last_test_at"] = str(row["last_test_at"]) if row.get("last_test_at") else None
		# Normalize to the same shape _connector_summary returns (bool/""), not
		# frappe.get_list's raw int/None - one shape for both response paths.
		row["enabled"] = bool(row["enabled"])
		row["last_test_status"] = row.get("last_test_status") or ""
		row["auth_method"] = row.get("auth_method") or "API Key"
		row["auth_class"] = _auth_class(row.get("preset") or "")
		if row["auth_method"] == oauth.OAUTH_AUTH_METHOD:
			row.update(_oauth_status(row))
		# The engine link is an internal - never shipped to the SPA.
		row.pop("mcp_oauth_client", None)
		(shared if row["scope"] == "Shared" else mine).append(row)

	return {
		"allow_custom_urls": flags["allow_custom_urls"],
		"oauth_redirect_uri": oauth_redirect_uri(),
		"catalog": catalog.to_public(),
		"shared": shared,
		"mine": mine,
	}


# --------------------------------------------------------------------------- #
# 2. add
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def add_connector(
	preset: str,
	base_url: str = "",
	scope: str = "",
	credential: str = "",
	label: str | None = None,
	key: str | None = None,
	auth_method: str = "API Key",
	enabled: int = 1,
) -> dict:
	"""Create a connector. Never marks it Passed — a fresh row's
	``last_test_status`` is blank by field default, and only ``test_connector``
	may ever set it to Passed. ``doc.insert()`` runs WITHOUT
	``ignore_permissions``, so both the plain create-role check and the
	controller's ``_guard_shared_scope`` (only the admin tier may create a
	Shared row) apply exactly as they do from the Desk — a plain user asking
	for ``scope="Shared"`` fails cleanly with the controller's own
	``frappe.PermissionError``, not a custom message here.

	``label`` is OPTIONAL: the SPA no longer asks for a name, so when it is
	omitted the display label is derived here — the preset's own name for a
	built-in preset, or the base URL's hostname for a Custom URL. ``key`` (the
	uniqueness slug the agent passes) is likewise derived: the preset's fixed
	key, or a slug of the Custom URL host. Uniqueness is then enforced on that
	key by the controller (one per app for Shared, one per app per user for
	Personal), so a user cannot connect the same app twice.

	``auth_method`` picks the connection method: "OAuth" IGNORES the
	``credential`` argument entirely (nothing is ever stored in the Password field
	for an OAuth row) and sets the backing link up SERVER-SIDE - a caller can ask
	for OAuth but can never name or steer what backs it. "API Key" is the shipped,
	unchanged behaviour, and an ``open`` preset needs no credential at all (the
	broker sends no Authorization header when the credential is empty).

	``enabled`` defaults to 1 (the shipped behaviour). The dialog passes ``enabled=0``
	when it creates the row on the "Sign in" press, so a half-made Shared row is not
	visible to other users until Save turns it on; the value is coerced with ``cint``
	and clamped to 0/1. ``update_connector`` is what later flips it back on.

	Every OAuth-capable preset uses the sign-in engine (see the module docstring):
	a ``dcr``/``static`` preset and a Custom URL row get an ``MCP OAuth Client``
	via :func:`_setup_mcp_oauth_client`. A bring-your-own-app static preset that
	pins its endpoints (GitHub) is seeded straight from the catalog with no
	discovery; every other row runs discovery, the only difference being WHICH
	address it runs against: the catalog's pinned one for a preset, the caller's
	for a Custom URL. A ``token``/``open`` preset has no sign-in and is refused
	before anything is created."""
	label = (label or "").strip()
	preset = (preset or "").strip()
	scope = (scope or "").strip()
	auth_method = (auth_method or "API Key").strip()
	credential = credential or ""

	if preset not in _PRESETS:
		frappe.throw(_("Unknown connector preset."))
	if scope not in _SCOPES:
		frappe.throw(_("Scope must be Shared or Personal."))
	if auth_method not in _AUTH_METHODS:
		frappe.throw(_("Choose a key or a sign-in for this connector."))
	if auth_method == oauth.OAUTH_AUTH_METHOD and not _uses_discovery_engine(preset):
		# A key-only or no-credential app has no sign-in to start, so there is
		# nothing to route this to. Refused HERE, before anything is created or any
		# request leaves, and refused again in the connector controller so a raw
		# DocType write cannot take the shortcut this rejects.
		frappe.throw(_("This app connects with a key, not a sign-in."))

	if preset == catalog.CUSTOM_URL:
		if not connector_flags()["allow_custom_urls"]:
			frappe.throw(
				_(
					"Custom URL connectors are turned off. Ask an administrator to "
					"enable them, or choose one of the built-in presets."
				)
			)
		resolved_base_url = (base_url or "").strip()
		host = (urlparse(resolved_base_url).hostname or "").strip()
		resolved_key = (key or _slug(host)).strip()
		resolved_label = label or host or _("Custom connector")
	else:
		# Presets are pinned to the vendor's own endpoint — a caller's base_url
		# is ignored entirely for anything but Custom URL.
		resolved_base_url = _PRESET_BASE_URLS[preset]
		resolved_key = (key or _PRESET_KEYS.get(preset) or "").strip()
		resolved_label = label or preset

	doc_fields = {
		"doctype": CONNECTOR,
		"key": resolved_key,
		"label": resolved_label,
		"preset": preset,
		"base_url": resolved_base_url,
		"scope": scope,
		"auth_method": auth_method,
		"enabled": 1 if cint(enabled) else 0,
	}
	engine_oauth = auth_method == oauth.OAUTH_AUTH_METHOD and _uses_discovery_engine(preset)
	# A bring-your-own-app static preset (GitHub) seeds its client straight from the
	# catalog and makes NO outbound request, so it must not spend the outbound slot.
	seeds_from_catalog = engine_oauth and _catalog_seed_provider(preset) is not None
	if engine_oauth:
		# Discovery below is real egress, to a host the CALLER chose on the Custom
		# URL path and to a vendor on the preset path, so it is rate limited exactly
		# like the Test button either way. Without the gate the discovery endpoint is
		# an unmetered outbound-request amplifier for any Jarvis User. The seeding
		# branch makes no request, so it is deliberately NOT metered - metering it
		# would let a slow trickle of catalog-seeded creates lock a user out of the
		# Test button and real sign-ins.
		if not seeds_from_catalog and _over_test_rate_limit(frappe.session.user):
			frappe.throw(_("Too many attempts. Please wait a moment and try again."))
		doc_fields["credential"] = ""
	else:
		doc_fields["credential"] = credential

	doc = frappe.get_doc(doc_fields)
	doc.insert()
	if engine_oauth:
		_setup_mcp_oauth_client(doc)
	frappe.db.commit()
	return _connector_summary(doc)


def _setup_mcp_oauth_client(doc) -> None:
	"""Discover ``doc``'s sign-in service, self-register with it when it supports
	that, and store the result as the row's ``MCP OAuth Client``.

	Discovery runs against ``doc.base_url``, which by this point is ALREADY the
	resolved address: the catalog's pinned endpoint for a preset, the caller's own
	only for a Custom URL. Nothing here re-reads a caller-supplied URL, so a
	preset row can never be discovered - or later signed in - against an address
	its caller named.

	TIME. This is the slowest thing the connectors API does, and every second of it
	is a held worker. The ceiling is deliberate and arithmetic, and WALL-CLOCK
	(``ssrf.open_pinned_request`` clamps DNS, connect and header wait to one
	deadline per hop; ``mcp_oauth.transport._read_capped`` reads the body one
	syscall at a time under the same deadline): ``mcp_oauth.discovery.RUN_TOTAL_TIMEOUT_S`` (45s) for the
	whole discovery run, up to six hops inside it, plus
	``transport.TOKEN_TOTAL_TIMEOUT_S`` (10s) for the registration POST - 55s worst
	case, with at most ``transport.MAX_REDIRECTS`` (2) redirects per hop, well under
	gunicorn's 120s. The rate limit above is what stops a caller from queueing
	several of those at once.

	ORDER MATTERS. This runs AFTER the insert so every cheap gate has already run
	- the Shared-scope permission check, key uniqueness, the custom-URL policy.
	Those are the likely failures and none of them should cost an outbound
	request first; in particular, registering before the permission check would
	let any Jarvis User drive registration POSTs at an arbitrary host by asking
	for a Shared row they cannot create. If discovery then fails, the row just
	created is removed rather than left behind as an unusable connector.

	The link is written with ``frappe.db.set_value`` because the client cannot
	exist before the connector it points at: this is the second half of ONE
	create, not a user-initiated edit, and must not re-run the row's validation.

	BRING-YOUR-OWN-APP STATIC: a static preset whose sign-in service publishes no
	discovery document declares its endpoints in the catalog (GitHub). For it,
	discover() cannot run and MUST be skipped - the client is seeded straight from
	the pinned, reviewed catalog values, making NO outbound request at all. Every
	other row (a self-registering ``dcr`` preset, a static preset that publishes
	metadata, or a Custom URL) keeps the discover() path unchanged.

	ALL-OR-NOTHING: either half - the catalog seed or discovery/registration - is
	wrapped, so a failure AFTER the row was inserted removes the row rather than
	leaving a half-created, unusable connector behind. The seed half catches any
	exception (a broken client insert is as much a dead end as a failed discovery);
	the discovery half catches the engine's own ``OAuthError``."""
	provider = _catalog_seed_provider((doc.get("preset") or "").strip())
	if provider is not None:
		try:
			_seed_static_client_from_catalog(doc, provider)
		except Exception as exc:
			# ANY seed failure (not just an OAuthError) must not orphan the row: a
			# broken client insert is as much a dead end as a failed discovery. The
			# cause is logged (never the row's credentials) so a schema/DB fault is
			# not hidden behind the friendly message.
			frappe.logger("jarvis.connectors").warning("catalog seeding failed after insert", exc_info=True)
			_discard_connector(doc)
			frappe.throw(_oauth_error_message(getattr(exc, "code", ""), getattr(exc, "detail", "")))
		return
	try:
		_discover_and_save_client(doc)
	except mcp_oauth.OAuthError as exc:
		_discard_connector(doc)
		frappe.throw(_oauth_error_message(exc.code, exc.detail))


def _discover_and_save_client(doc) -> None:
	"""Discover ``doc``'s sign-in service, self-register with it when it supports
	that, and store the result as the row's ``MCP OAuth Client``. Raises
	``mcp_oauth.OAuthError`` on any discovery/registration failure and leaves the
	row untouched - the CALLER decides whether a failure deletes the row (a fresh
	create) or keeps it (a self-heal on an existing row).

	The link is written with ``frappe.db.set_value`` because the client cannot exist
	before the connector it points at: this is a second write on ONE create/heal,
	not a user-initiated edit, and must not re-run the row's validation."""
	found = mcp_oauth.discover(
		doc.base_url, transport=MCP_OAUTH_TRANSPORT, egress_allowed=broker._egress_allowed
	)
	scope = _requested_scope(found)
	if found.registration_endpoint:
		redirect_uri = oauth_redirect_uri()
		creds = mcp_oauth.register_dynamic(
			found.registration_endpoint,
			redirect_uri=redirect_uri,
			scope=scope,
			client_name=_dcr_client_name(redirect_uri),
			transport=MCP_OAUTH_TRANSPORT,
			egress_allowed=broker._egress_allowed,
		)
	else:
		# No self-registration on offer: park a static client with no client_id until
		# an administrator registers this workspace at the provider and fills it in
		# via set_oauth_client_credentials.
		creds = mcp_oauth.static_client("")
	client = mcp_oauth_store.save_client(doc.name, found, creds, scope)
	frappe.db.set_value(CONNECTOR, doc.name, "mcp_oauth_client", client.name, update_modified=False)
	doc.mcp_oauth_client = client.name


def _discard_connector(doc) -> None:
	"""Remove a just-created connector whose sign-in setup failed, so a failed setup
	never leaves a half-created, unusable row behind. Only ever called from the
	CREATE path (:func:`add_connector`), never from a self-heal on an existing row."""
	frappe.delete_doc(CONNECTOR, doc.name, ignore_permissions=True, force=True)
	frappe.db.commit()


def _ensure_mcp_oauth_client(doc) -> None:
	"""Make an existing OAuth row that carries no ``mcp_oauth_client`` usable again,
	IN PLACE - never deleting the row. Raises ``mcp_oauth.OAuthError`` when discovery
	cannot set one up; the caller keeps the row and surfaces a friendly error.

	Three cases, in order:

	  * the client doc already exists (only its link was lost) - rewrite the link and
	    stop. This is what makes the heal IDEMPOTENT: ``mcp_oauth_store.save_client``
	    overwrites ``client_id`` (it guards only the password fields), so re-seeding
	    over a client whose admin already pasted an id/secret would wipe the id and
	    strand the row. Never re-seed over an existing client.
	  * a bring-your-own-app static preset with no client - SEED from the catalog,
	    no network (the customer pastes their id/secret next).
	  * every other discovery-engine preset - run the same discovery add_connector
	    runs, against the row's already-resolved address."""
	existing = mcp_oauth_store.client_for(doc.name)
	if existing is not None:
		# Relink ONLY while the client still describes this row's address. The
		# controller pins every ORM link write to the row's canonical resource; a
		# db.set_value relink must not be the one path around that pin (a raw write
		# can null the link and re-point base_url in a single save).
		from jarvis.connectors.mcp_oauth import canonical_resource

		try:
			same = canonical_resource(existing.get("resource") or "") == canonical_resource(
				doc.base_url or ""
			)
		except ValueError:
			same = False
		if not same:
			raise mcp_oauth.OAuthDiscoveryError(
				"client_address_mismatch", "This connector's address changed. Remove it and add it again."
			)
		frappe.db.set_value(CONNECTOR, doc.name, "mcp_oauth_client", existing.name, update_modified=False)
		doc.mcp_oauth_client = existing.name
		return
	provider = _catalog_seed_provider((doc.get("preset") or "").strip())
	if provider is not None:
		_seed_static_client_from_catalog(doc, provider)
		return
	_discover_and_save_client(doc)


def _requested_scope(found) -> str:
	"""The permissions to ask for: what the connector's own challenge named when
	it named any (least privilege - the server is stating what THIS resource
	needs), else everything its sign-in service advertises."""
	if found.challenge_scope:
		return found.challenge_scope
	return " ".join(found.scopes_supported or [])


def _brand_name() -> str:
	"""The tenant's white-label assistant name, same field and fallback every
	other server-side reader uses (``www/jarvis.py``, ``pwa.py``): the
	non-secret ``Jarvis Settings.agent_name``, or "Jarvis" when unset.
	``cache=False`` - a name set moments ago (Settings -> Branding) must show up
	in the very next registration, not a request-local value read before it."""
	return (frappe.db.get_single_value(SETTINGS, "agent_name", cache=False) or "").strip() or "Jarvis"


def _dcr_client_name(redirect_uri: str) -> str:
	"""The RFC 7591 ``client_name`` to register with: some servers (Atlassian,
	at least) answer a body with none with a bare HTTP 400, so this is always
	sent. "<brand> (<host>)" so the provider's own app-management screen shows
	which tenant a registration belongs to, not an identical "Jarvis" across
	every tenant that self-registers there."""
	netloc = urlparse(redirect_uri).netloc
	return f"{_brand_name()} ({netloc})" if netloc else _brand_name()


def _provider_declares_endpoints(provider) -> bool:
	"""True when a catalog provider pins its own sign-in endpoints, so its client
	can be seeded without discovery. All three are required together - a
	half-declared provider is a catalog bug, not a seedable one."""
	return bool(provider and provider.issuer and provider.authorization_endpoint and provider.token_endpoint)


def _catalog_seed_provider(preset: str):
	"""The catalog provider for ``preset`` when it is a bring-your-own-app static
	provider that pins its own sign-in endpoints (so its client is SEEDED from the
	catalog with no discovery and no outbound request), else ``None``. The ONE
	predicate the rate-limit gate, the create-time setup and the self-heal all
	share, so they cannot drift on which presets skip discovery."""
	provider = catalog.by_name(preset) if preset else None
	if catalog.auth_of(preset) == catalog.AUTH_STATIC and _provider_declares_endpoints(provider):
		return provider
	return None


def _seed_static_client_from_catalog(doc, provider) -> None:
	"""Seed ``doc``'s ``MCP OAuth Client`` from the catalog's pinned endpoints, for
	a bring-your-own-app static provider. NO discover() runs and NO request leaves:
	the endpoints are reviewed catalog constants, so the synthetic Discovery is
	built from them directly (registration_endpoint None, ``iss`` param support
	False via an empty metadata snapshot, resource = canonical base_url,
	resource_declared = the base_url verbatim, scope = the catalog default).
	``mcp_oauth_store.discovery_from_client`` rebuilds exactly this for connect,
	exchange and refresh.

	The client is seeded with an EMPTY client_id: the customer registers their own
	app at the vendor and pastes the id/secret via ``set_oauth_client_credentials``,
	which is why the row reads ``needs_static_client`` until they do."""
	resource = mcp_oauth.canonical_resource(doc.base_url)
	scope = (provider.scopes or "").strip()
	discovery = mcp_oauth.Discovery(
		resource=resource,
		resource_declared=doc.base_url,
		authorization_servers=[provider.issuer],
		scopes_supported=[],
		issuer=provider.issuer,
		authorization_endpoint=provider.authorization_endpoint,
		token_endpoint=provider.token_endpoint,
		registration_endpoint=None,
		raw_as_metadata={},
		challenge_scope=scope or None,
	)
	client = mcp_oauth_store.save_client(doc.name, discovery, mcp_oauth.static_client(""), scope)
	frappe.db.set_value(CONNECTOR, doc.name, "mcp_oauth_client", client.name, update_modified=False)
	doc.mcp_oauth_client = client.name


# --------------------------------------------------------------------------- #
# 3. test
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def test_connector(name: str) -> dict:
	"""Run initialize + tools/list through ``broker.test_connector`` and, when the
	caller has WRITE permission, persist the outcome: on success write
	``tools_cache`` + ``last_test_status="Passed"`` and MERGE the ``allowed_actions``
	table (see ``_merge_allowed_actions``); on failure move only
	``last_test_status="Failed"`` (an existing good cache from a PRIOR passing test
	is left alone, so a transient failure never wipes a working connector's config).

	A per-user rate limit guards the outbound probe - it is a real network call
	to a user-chosen host, so it must not be spammable.

	Read runs the probe, WRITE persists. Any user who can SEE a connector (a Shared
	one is visible to every tenant user) may run the live health/discovery probe and
	gets the tool list back for display, but a read-only caller causes NO DB write:
	without this a plain reader could flip a Shared connector to
	``last_test_status="Failed"`` and disable it tenant-wide (``call_connector``'s
	readiness guard refuses a non-Passed connector). The parent-field write goes
	through ``frappe.db.set_value`` (the write-only DocType permission a reader lacks
	would block ``doc.save()``), and the ``allowed_actions`` MERGE is a server-derived
	rewrite that can only grant a read-only, non-destructive default or preserve an
	admin's existing choice - never turn ON a write/destructive action.
	"""
	doc = frappe.get_doc(CONNECTOR, name)
	if not doc.has_permission("read"):
		frappe.throw(_("Not permitted."), frappe.PermissionError)

	if _over_test_rate_limit(frappe.session.user):
		return {
			"ok": False,
			"error": {
				"code": "rate_limited",
				"message": "Too many connection tests. Please wait a moment and try again.",
			},
		}

	if oauth.is_oauth(doc) and not oauth.resolve_connector_token(doc):
		# Fail BEFORE the outbound probe (and before touching the rate limit's
		# sibling breaker/cap): an unconnected OAuth row has no bearer to test
		# with, and that is a sign-in problem, not a transport/health one. The
		# resolver picks the engine, so this covers both.
		return {
			"ok": False,
			"error": {
				"code": "connector_not_ready",
				"message": "Connect this app first, then test it.",
			},
		}

	can_write = doc.has_permission("write")
	result = broker.test_connector(doc)
	now = now_datetime()

	if not result.get("ok"):
		error = result.get("error") or {"code": "unknown_error", "message": "The connection test failed."}
		# circuit_open / at_capacity are guard / transient-load signals, not a health
		# measurement (the probe never ran), so they must NOT flip last_test_status to
		# Failed and disable the connector tenant-wide via call_connector's guard.
		transient = error.get("code") in {"circuit_open", "at_capacity"}
		# Only a writer may flip the stored status; a reader's probe is display-only
		# so it can never disable a Shared connector tenant-wide.
		if can_write and not transient:
			frappe.db.set_value(
				CONNECTOR,
				doc.name,
				{"last_test_status": "Failed", "last_test_at": now},
				update_modified=False,
			)
			frappe.db.commit()
		return {"ok": False, "error": error}

	tools = [t for t in (result.get("tools") or []) if isinstance(t, dict)]
	merged = _merge_allowed_actions(doc.get("allowed_actions") or [], tools)
	if can_write:
		frappe.db.set_value(
			CONNECTOR,
			doc.name,
			{
				"tools_cache": frappe.as_json({"tools": tools}),
				"tools_cached_at": now,
				"last_test_status": "Passed",
				"last_test_at": now,
			},
			update_modified=False,
		)
		_replace_allowed_actions(doc.name, merged)
		frappe.db.commit()
	return {
		"ok": True,
		"tools": [
			{
				"action": m["action"],
				"allowed": bool(m["allowed"]),
				"read_only": bool(m["read_only"]),
				"destructive": bool(m["destructive"]),
				"description": m["description"],
			}
			for m in merged
		],
	}


# --------------------------------------------------------------------------- #
# 4. set allowed actions
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def set_allowed_actions(name: str, actions: str | list) -> dict:
	"""Replace the ``allowed_actions`` table from the user's picker choices.
	``actions`` is typed ``str | list`` (not a bare ``list``) because every
	other list/dict-shaped param in this codebase's whitelisted API is posted
	as a ``JSON.stringify``'d string by the SPA's ``call()`` wrapper (see
	``personalise_api.set_personalisation_settings``'s ``payload`` param) -
	``_parse_json`` below accepts either.

	Only the ``allowed`` bit is trusted from ``actions``
	(``[{"action": str, "allowed": bool}, ...]``); ``read_only``/``destructive``
	are always recomputed from ``tools_cache`` — a client-sent flag for either
	is IGNORED, never stored. An action named by the client that is not in
	``tools_cache`` is rejected outright (test the connector first); an action
	present in the cache but not mentioned by the client keeps its prior
	``allowed`` value (a partial save never silently drops existing grants)."""
	doc = frappe.get_doc(CONNECTOR, name)
	if not doc.has_permission("write"):
		frappe.throw(_("Not permitted."), frappe.PermissionError)

	parsed = _parse_json(actions, actions if isinstance(actions, list) else [])
	if not isinstance(parsed, list):
		frappe.throw(_("Provide a list of actions."))

	client_allowed: dict[str, bool] = {}
	for item in parsed:
		if not isinstance(item, dict) or not str(item.get("action") or "").strip():
			frappe.throw(_("Each action must include an action name."))
		client_allowed[str(item["action"]).strip()] = bool(item.get("allowed"))

	cached_tools = _cached_tools(doc)
	cached_names = {t.get("name") for t in cached_tools if t.get("name")}
	unknown = sorted(set(client_allowed) - cached_names)
	if unknown:
		frappe.throw(_("Unknown action(s), test the connector first: {0}.").format(", ".join(unknown)))

	existing_allowed = {c.get("action"): bool(c.get("allowed")) for c in (doc.get("allowed_actions") or [])}
	merged: list[dict] = []
	for tool in cached_tools:
		action = (tool.get("name") or "").strip()
		if not action:
			continue
		read_only, destructive = _tool_flags(tool)
		allowed = (
			client_allowed[action] if action in client_allowed else existing_allowed.get(action, read_only)
		)
		merged.append(
			{
				"action": action,
				"allowed": 1 if allowed else 0,
				"read_only": 1 if read_only else 0,
				"destructive": 1 if destructive else 0,
				"description": _clip((tool.get("description") or "").strip(), _DESC_MAX),
			}
		)

	_replace_allowed_actions(doc.name, merged)
	frappe.db.commit()
	return {
		"ok": True,
		"actions": [
			{
				"action": m["action"],
				"allowed": bool(m["allowed"]),
				"read_only": bool(m["read_only"]),
				"destructive": bool(m["destructive"]),
				"description": m["description"],
			}
			for m in merged
		],
	}


# --------------------------------------------------------------------------- #
# 5. update
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def update_connector(
	name: str,
	label: str | None = None,
	base_url: str | None = None,
	credential: str | None = None,
	enabled: int | None = None,
) -> dict:
	"""Edit label/base_url/credential/enabled. ``base_url`` may only change on
	a Custom URL connector (a preset's endpoint is pinned — see
	``add_connector``). An empty/blank ``credential`` means "leave the stored
	one unchanged" (the SPA never round-trips the real secret back to show
	it, so it cannot re-submit it as-is). Changing either the endpoint or the
	credential clears ``last_test_status``/``last_test_at`` — the last test no
	longer proves anything; changing the endpoint ALSO clears
	``tools_cache``/``allowed_actions``, since a cached tool list from a
	different server would just make the broker deny every action as
	``action_unknown`` until the next test, which is more confusing than an
	honest "not tested yet" state."""
	doc = frappe.get_doc(CONNECTOR, name)
	if not doc.has_permission("write"):
		frappe.throw(_("Not permitted."), frappe.PermissionError)

	base_url_changed = False
	credential_changed = False

	if label is not None:
		label = label.strip()
		if not label:
			frappe.throw(_("Label cannot be empty."))
		doc.label = label

	if base_url is not None and base_url.strip():
		base_url = base_url.strip()
		# An edit form re-submits every field, including an unchanged base_url -
		# only a REAL change is gated to Custom URL, so re-saving a preset
		# connector's own (already-pinned) URL back at itself is a no-op, not
		# an error.
		if base_url != doc.base_url:
			if doc.preset != "Custom URL":
				frappe.throw(_("Only Custom URL connectors may change their Base URL."))
			if doc.get("mcp_oauth_client"):
				# The stored sign-in was granted FOR this address, by a service this
				# address named. Re-pointing would mean either forwarding that
				# sign-in to a different app (which the resource pin in
				# ``oauth.resolve_mcp_oauth_token`` refuses anyway, leaving the row
				# permanently unusable) or silently re-running discovery against a
				# new host under an existing grant. Neither is safe, so a different
				# address is a different connector.
				frappe.throw(_("Add a new connector to use a different address."))
			# Re-point is a fresh custom URL, so re-apply the same allow_custom_urls
			# gate add_connector uses. Without this, an admin turning the toggle OFF
			# would still leave existing Custom URL rows freely re-pointable on edit.
			if not connector_flags()["allow_custom_urls"]:
				frappe.throw(
					_(
						"Custom URL connectors are turned off. Ask an administrator to "
						"enable them before changing this Base URL."
					)
				)
			doc.base_url = base_url
			base_url_changed = True

	if credential is not None and credential.strip():
		doc.credential = credential.strip()
		credential_changed = True

	if enabled is not None:
		doc.enabled = 1 if cint(enabled) else 0

	if base_url_changed or credential_changed:
		doc.last_test_status = ""
		doc.last_test_at = None
		if base_url_changed:
			# None, not "" - the JSON fieldtype maps to a MariaDB `json` column,
			# which since 10.4.3 carries an implicit CHECK(json_valid(col)); an
			# empty string fails that check on save, NULL does not.
			doc.tools_cache = None
			doc.tools_cached_at = None
			doc.set("allowed_actions", [])

	doc.save()
	frappe.db.commit()
	return _connector_summary(doc)


# --------------------------------------------------------------------------- #
# 6. delete
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def delete_connector(name: str) -> dict:
	"""Hard delete. ``frappe.delete_doc`` checks the delete permission itself
	(Shared -> admin tier only, Personal -> owner only, per
	``connector_permissions.can_edit_connector``), so a caller without it gets
	a clean ``frappe.PermissionError``."""
	if not frappe.db.exists(CONNECTOR, name):
		frappe.throw(_("Connector not found."), frappe.DoesNotExistError)
	frappe.delete_doc(CONNECTOR, name)
	frappe.db.commit()
	return {"ok": True}


# --------------------------------------------------------------------------- #
# 7. OAuth connect / disconnect (OAUTH_CONNECTORS_DESIGN.md §4, §6a, §7)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def connect_oauth(name: str) -> dict:
	"""Start the sign-in flow for an OAuth connector: return the provider's own
	authorize URL for the SPA to redirect the browser to. The provider's redirect
	back is handled by :func:`mcp_oauth_callback` below, which trades the code for
	tokens and stores them for the current user.

	Connect is PER USER (OAUTH_CONNECTORS_DESIGN.md §6a): each user who wants to use
	a Shared OAuth connector runs their own sign-in.

	Rate limited exactly like the probe and the Test button. Starting a sign-in
	mints server-side state, so an unmetered version is a state-minting loop for any
	user who can see a Shared connector."""
	doc = frappe.get_doc(CONNECTOR, name)
	if not doc.has_permission("read"):
		frappe.throw(_("Not permitted."), frappe.PermissionError)

	if not oauth.is_oauth(doc):
		return {"ok": False, "error": {"code": "not_oauth", "message": "This connector doesn't use sign-in."}}

	if _over_test_rate_limit(frappe.session.user):
		return _error("rate_limited", "Too many attempts. Please wait a moment and try again.")

	if not doc.get("mcp_oauth_client") and _uses_discovery_engine(doc.get("preset") or ""):
		# A row that never got a client (a raw insert, or a create whose seed did not
		# run) is not a dead end: seed it from the catalog or discover it now, then
		# proceed. Non-fatal - a discovery failure keeps the row the user already owns
		# and returns a friendly error rather than deleting it. The heal WRITES (a
		# client, a link, maybe a remote registration), so a reader of a Shared row
		# does not get to trigger it, and a Custom URL row honours the workspace
		# policy exactly as add_connector does.
		if not doc.has_permission("write"):
			return _error("oauth_not_configured", "Ask your admin to finish setup.")
		if (doc.get("preset") or "") == catalog.CUSTOM_URL and not connector_flags()["allow_custom_urls"]:
			return _error("oauth_not_configured", "Custom addresses are turned off. Ask an administrator.")
		try:
			_ensure_mcp_oauth_client(doc)
		except mcp_oauth.OAuthError as exc:
			return _error("oauth_not_configured", _oauth_error_message(exc.code, exc.detail))

	if doc.get("mcp_oauth_client"):
		return _connect_mcp_oauth(doc)

	return _error("oauth_not_configured", "This app isn't set up for sign-in yet. Ask your admin.")


def _connect_mcp_oauth(doc) -> dict:
	"""Start a discovery-engine sign-in: mint the one-time state, bind everything
	the callback will need to it SERVER-SIDE, and hand back the authorize URL.

	Nothing the callback needs travels in the URL except the opaque ``state``.
	The code verifier, the issuer to validate the response against, the redirect
	URI and the user who started the flow all live in the state record, because
	the callback's own query string is attacker-influenced and may not be trusted
	for any of them."""
	# A parked error from an EARLIER flow (a popup-blocked, same-tab attempt whose
	# failure nothing ever polled) would otherwise be handed to the FIRST poll of this
	# brand-new sign-in and close the vendor tab we are about to open. Discard it here,
	# after the caller's permission checks and before any state is minted, so this flow
	# starts from a clean slate.
	mcp_oauth_store.take_signin_error(doc.name, frappe.session.user)
	client = mcp_oauth_store.client_for(doc.name)
	client_id = (client.get("client_id") or "").strip() if client else ""
	if not client_id:
		return _error("oauth_not_configured", "This app isn't set up for sign-in yet. Ask your admin.")

	state = secrets.token_urlsafe(32)
	code_verifier = mcp_oauth.pkce_new_verifier()
	redirect_uri = oauth_redirect_uri()
	# Two forms of one address, and they are not interchangeable. ``resource`` is
	# the canonical one the token gets PINNED to; ``resource_indicator`` is the
	# app's own wording, which is what goes on the wire (see
	# ``mcp_oauth_store.resource_indicator``).
	resource = client.get("resource") or ""
	indicator = mcp_oauth_store.resource_indicator(client)
	mcp_oauth_store.put_state(
		state,
		{
			"connector": doc.name,
			"user": frappe.session.user,
			"code_verifier": code_verifier,
			"issuer": client.get("issuer") or "",
			"iss_param_supported": bool(cint(client.get("iss_param_supported"))),
			"redirect_uri": redirect_uri,
			"resource": resource,
			"resource_indicator": indicator,
			"scope": client.get("scope") or "",
		},
	)
	url = mcp_oauth.build_authorize_url(
		mcp_oauth_store.discovery_from_client(client),
		client_id,
		redirect_uri,
		scope=client.get("scope") or "",
		resource=indicator,
		state=state,
		code_challenge=mcp_oauth.pkce_challenge(code_verifier),
		# A preset's fixed additions (Google: offline access + forced consent, or no
		# refresh token is issued). Looked up from the reviewed catalog by preset at
		# connect time, never stored; `{}` for a Custom URL row and for every preset
		# that declares none, which sends exactly the request they always did.
		extra_params=catalog.authorize_params_of((doc.get("preset") or "").strip()),
	)
	# ``started_at`` is the server clock at the moment this sign-in began, in the same
	# UTC frame ``oauth_signin_status`` reports ``connected_at`` in. The SPA opens the
	# sign-in tab, then treats the poll as landed only once the stored token's
	# ``connected_at`` is at or after this - so a pre-existing token from an earlier
	# sign-in is never mistaken for the one the user just completed.
	return {"ok": True, "url": url, "started_at": _utc_iso(now_datetime())}


@frappe.whitelist()
@require_jarvis_user
def disconnect_oauth(name: str) -> dict:
	"""End the CURRENT user's sign-in for an OAuth connector by deleting their
	stored tokens. Idempotent - never errors when there was nothing to remove
	(nothing configured, or the user never connected)."""
	doc = frappe.get_doc(CONNECTOR, name)
	if not doc.has_permission("read"):
		frappe.throw(_("Not permitted."), frappe.PermissionError)

	if doc.get("mcp_oauth_client") and mcp_oauth_store.delete_token(doc.name, frappe.session.user):
		frappe.db.commit()
	return {"ok": True}


@frappe.whitelist()
@require_jarvis_user
def oauth_signin_status(name: str) -> dict:
	"""Poll the CURRENT user's sign-in state for an OAuth connector. The SPA opens the
	provider sign-in in a NEW tab that renders :func:`mcp_oauth_callback`'s page and
	closes itself, so the original tab learns the outcome only by polling here.

	``{ok: True, connected, connected_at, error}``:
	  * ``connected`` - the current user holds an access token for this connector;
	  * ``connected_at`` - that token document's ``modified`` as a UTC ISO string (the
	    same frame as :func:`connect_oauth`'s ``started_at``), or ``null`` when there is
	    no token or no access token on it;
	  * ``error`` - the friendly reason the LAST callback failed for this (user,
	    connector), read ONCE and cleared (``mcp_oauth_store.take_signin_error``), so a
	    surfaced error never sticks once the pane has shown it.

	Access posture matches :func:`connect_oauth`: read permission on the row is enough,
	so a plain user may poll their own Personal row or a Shared one, and a permission
	failure returns through :func:`_error` rather than raising (a poll gets a clean
	answer, not a 403 to catch). Deliberately NOT metered: this is a cheap read that
	mints nothing and makes no outbound call, and the SPA polls it every couple of
	seconds - sharing ``connect_oauth``'s per-minute bucket would lock the user out of
	Test/Connect mid-flow. A non-sign-in row (key or open) answers a plain
	``connected: false`` with no error."""
	doc = frappe.get_doc(CONNECTOR, name)
	if not doc.has_permission("read"):
		return _error("forbidden", "Not permitted.")

	if not oauth.is_oauth(doc):
		return {"ok": True, "connected": False, "connected_at": None, "error": ""}

	user = frappe.session.user
	error = mcp_oauth_store.take_signin_error(doc.name, user)
	token = mcp_oauth_store.load_token(doc.name, user)
	has_token = bool(token and token.get_password("access_token", raise_exception=False))
	connected_at = _utc_iso(token.get("modified")) if has_token else None
	return {"ok": True, "connected": has_token, "connected_at": connected_at, "error": error}


# --------------------------------------------------------------------------- #
# 8. discovery engine: probe, static credentials, callback
#    (MCP_OAUTH_CLIENT_DESIGN.md §2, §5, §6)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def probe_connector_auth(base_url: str) -> dict:
	"""Ask an address whether it needs a sign-in, and where. Creates NOTHING -
	this is what the SPA calls while the user is still typing a URL, so it can
	say "this app signs you in at github.com" BEFORE anything is saved and before
	the user is sent anywhere. That preview is the confused-deputy defense
	(design section 6): the server the user pasted is what chose that host, so
	the user has to see it.

	``{ok: True, needs_signin: True, signin_host, registration, scopes}`` when the
	address asks for a sign-in; ``{ok: True, needs_signin: False}`` when it does
	not (an address that answers unauthenticated takes a key, or nothing);
	``{ok: False, error}`` when it asks for one but its details cannot be read or
	do not survive the validation gates.

	Gated like the Test button, and for the same reason: it is a real outbound
	call to a host the caller named."""
	if not connector_flags()["allow_custom_urls"]:
		return _error(
			"custom_urls_disabled",
			"Custom addresses are turned off. Ask an administrator to enable them.",
		)

	base_url = (base_url or "").strip()
	parsed = urlparse(base_url)
	if parsed.scheme not in ("http", "https") or not parsed.netloc:
		return _error("invalid_arguments", "Enter a valid http:// or https:// address.")

	if _over_test_rate_limit(frappe.session.user):
		return _error("rate_limited", "Too many checks. Please wait a moment and try again.")

	try:
		found = mcp_oauth.discover(
			base_url, transport=MCP_OAUTH_TRANSPORT, egress_allowed=broker._egress_allowed
		)
	except mcp_oauth.OAuthDiscoveryError as exc:
		if exc.code == "no_401_challenge":
			# The address served the call without asking for anything, so there is
			# no sign-in to set up. Not a failure - the other kind of connector.
			return {"ok": True, "needs_signin": False}
		return _error(exc.code, _oauth_error_message(exc.code, exc.detail))
	except mcp_oauth.OAuthError as exc:
		return _error(exc.code, _oauth_error_message(exc.code, exc.detail))

	return {
		"ok": True,
		"needs_signin": True,
		"signin_host": _host(found.issuer),
		"registration": "dcr" if found.registration_endpoint else "static",
		"scopes": list(found.scopes_supported or []),
	}


@frappe.whitelist()
@require_jarvis_user
def set_oauth_client_credentials(name: str, client_id: str, client_secret: str = "") -> dict:
	"""ADMIN ONLY. Supply the credentials an administrator got by registering this
	workspace at a provider by hand - the static path, taken when the provider
	does not set itself up automatically.

	For a Shared row this is admin work: the credentials identify the whole
	workspace to the provider. For a Personal row the owner does it themselves,
	for their own app. A blank ``client_secret`` means "leave the stored one alone",
	the same convention ``update_connector`` uses for a credential, since the SPA
	never round-trips a stored secret back.

	TWO gates, not one. WHO may configure a sign-in: the admin tier for a Shared
	row (these credentials identify the whole workspace), or the OWNER of a
	Personal row (their own app, their own connector - the same trust as pasting
	their own key). WHICH row: the row's own write permission, which
	``connector_permissions`` deliberately keeps owner-only for a Personal row,
	admin tier included (mirroring ``Jarvis Conversation``) - so an admin can
	never plant their own client credentials on another user's private connector
	and receive that user's sign-in, and a Personal row is never a dead end."""
	client_id = (client_id or "").strip()
	if not client_id:
		frappe.throw(_("Enter the ID the provider gave you."))

	doc = frappe.get_doc(CONNECTOR, name)
	owns_personal = doc.scope == "Personal" and doc.owner == frappe.session.user
	if not (owns_personal or has_jarvis_admin_access(frappe.session.user)):
		frappe.throw(
			_("Only a System Manager or Jarvis Admin may set up sign-in for a shared app."),
			frappe.PermissionError,
		)
	if not doc.has_permission("write"):
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	if not doc.get("mcp_oauth_client") and oauth.is_oauth(doc) and _uses_discovery_engine(doc.preset):
		# The same self-heal connect_oauth does: an OAuth row that never got a client
		# is still configurable here rather than a dead end waiting on an admin. A
		# key row is gated out by is_oauth, so this never discovers for one. A
		# discovery failure keeps the row and surfaces a friendly error. Metered and
		# policy-checked like every other path that can reach out to a host.
		if _over_test_rate_limit(frappe.session.user):
			frappe.throw(_("Too many attempts. Please wait a moment and try again."))
		if doc.preset == catalog.CUSTOM_URL and not connector_flags()["allow_custom_urls"]:
			frappe.throw(_("Custom addresses are turned off. Ask an administrator."))
		try:
			_ensure_mcp_oauth_client(doc)
		except mcp_oauth.OAuthError as exc:
			frappe.throw(_oauth_error_message(exc.code, exc.detail))
	client = mcp_oauth_store.client_for(doc.name) if doc.get("mcp_oauth_client") else None
	if client is None:
		frappe.throw(_("This connector does not use this kind of sign-in."))
	if (client.get("registration_mode") or "static") != "static":
		frappe.throw(_("This app set itself up, so it needs no details from you."))

	client.client_id = client_id
	if client_secret:
		client.client_secret = client_secret
	client.save(ignore_permissions=True)
	frappe.db.commit()
	return _connector_summary(doc)


def _park_generic_signin_failure(record: dict) -> None:
	"""Park the fixed GENERIC failure sentence for the flow ``record`` belongs to, so
	the original tab's poll resolves with a readable reason once the callback tab has
	closed. Only ever :data:`_SIGNIN_FAILURE_COPY`'s ``denied`` copy, NEVER anything from
	the provider's response, so a mix-up response is never surfaced (RFC 9207) and no
	attacker-influenced text is ever shown. The token-exchange failure is the ONE path
	that parks the provider's own reason instead; it does not go through here."""
	mcp_oauth_store.park_signin_error(
		record.get("connector") or "", record.get("user") or "", _SIGNIN_FAILURE_COPY["denied"]
	)


@frappe.whitelist(methods=["GET"])
@require_jarvis_user
def mcp_oauth_callback(
	code: str | None = None,
	state: str | None = None,
	iss: str | None = None,
	error: str | None = None,
	error_description: str | None = None,
) -> None:
	"""The one redirect URI the discovery engine registers. A browser lands here
	from the provider; this trades the authorization code for tokens and sends
	the browser back to the connectors pane.

	LOGIN REQUIRED - deliberately NOT ``allow_guest``. The whole flow is bound to
	the user who started it, so an anonymous hit has nothing to bind to and is
	refused by the framework before this body runs.

	The order of the checks is the security design, not a style choice:

	  1. Consume the state FIRST and destroy it - one callback per Connect,
	     always, including for every failure path below. A replay finds nothing.
	  2. The session user must be the user who started the flow, or a stolen
	     state cannot be redeemed into someone else's account.
	  3. RFC 9207 ``iss`` validation BEFORE any token request (mix-up defense).
	     On mismatch nothing from the response is shown; a GENERIC reason is parked for
	     the starting user's poll, so nothing about the mix-up is ever revealed.
	  4. Only then a provider-reported ``error`` (or a missing code) is turned into a
	     GENERIC result and parked. ``error_description`` is accepted so the URL parses
	     and is then dropped unread, being attacker-influenced text that is never echoed.

	This lands in the NEW tab the SPA opened for the provider, so it renders a small
	self-closing page (:func:`_callback_page`) rather than redirecting into the SPA;
	the SPA learns the real outcome by polling :func:`oauth_signin_status` in the
	original tab. Every page closes that tab. The success and token-exchange-failure
	pages name the connector and the provider's reason; the provider-error and
	iss-mismatch paths, once the state has been matched to the user who started it, park
	the GENERIC reason (never anything from the response, so a mix-up response is still
	never shown, RFC 9207) so the poll resolves instead of hanging on "window closed".
	The expired (no record) and stolen-state (wrong user) paths park nothing at all.

	Nothing secret is ever put in the page or a log line: it carries a connector label
	and a friendly reason, never a token, code or state. And the whole body is wrapped,
	because an exception escaping into Frappe's error page would render a stack trace
	for a URL that carries a live authorization code."""
	try:
		record = mcp_oauth_store.consume_state(state)
		if record is None:
			# No record: an expired or replayed state names no connector or user, so
			# there is nothing a poll could key a parked reason on. Left unparked.
			return _callback_page(reason="expired")
		if record.get("user") != frappe.session.user:
			# A stolen state redeemed by the WRONG session. Parking here would let that
			# session inject a message into the STARTING user's poll, so this path never
			# parks; the starting user's own tab keeps polling until it times out.
			return _callback_page(reason="denied")
		try:
			mcp_oauth.validate_iss(iss, record.get("issuer") or "", bool(record.get("iss_param_supported")))
		except mcp_oauth.OAuthError:
			# RFC 9207 mix-up defense: the response's issuer does not match the one this
			# flow started against. Nothing from the response is shown or parked; the
			# starting user (matched just above) gets the GENERIC reason so their tab
			# resolves instead of hanging on "window closed".
			_park_generic_signin_failure(record)
			return _callback_page(reason="denied")
		if error or not code:
			# The user declined or cancelled at the vendor, or it returned no code.
			# The record is matched to this user, so park the GENERIC reason (never the
			# provider's error text) for the poll to surface after this tab closes.
			_park_generic_signin_failure(record)
			return _callback_page(reason="denied")
		try:
			_exchange_and_store(record, code)
		except mcp_oauth.OAuthError as exc:
			# The one failure the user can act on: the provider answered and refused the
			# exchange. Show its friendly sentence with the provider's own reason folded
			# in (escaped), and park the SAME sentence so the original tab's poll can
			# surface it once this tab has closed.
			message = _oauth_error_message(exc.code, exc.detail)
			mcp_oauth_store.park_signin_error(
				record.get("connector") or "", record.get("user") or "", message
			)
			return _callback_page(reason="token_failed", message=message)
		return _callback_page(connector=record.get("connector"))
	except mcp_oauth.OAuthError:
		return _callback_page(reason="denied")
	except Exception:
		frappe.logger("jarvis.connectors").warning("connector sign-in callback failed", exc_info=True)
		return _callback_page(reason="denied")


def _exchange_and_store(record: dict, code: str) -> None:
	"""Trade the code for tokens and store them for the user who STARTED the flow
	(the record's user, already checked to be the session user), then commit.

	The commit is load-bearing: this is a GET, and Frappe rolls back anything a
	non-state-changing method wrote. Without it the sign-in would appear to work
	and store nothing."""
	connector = record.get("connector") or ""
	client = mcp_oauth_store.client_for(connector)
	if client is None or not (client.get("client_id") or "").strip():
		raise mcp_oauth.OAuthTokenError(
			"oauth_not_configured", "No sign-in is configured for this connector."
		)

	resource = record.get("resource") or client.get("resource") or ""
	token_set = mcp_oauth.exchange_code(
		mcp_oauth_store.discovery_from_client(client),
		mcp_oauth_store.creds_from_client(client),
		code=code,
		code_verifier=record.get("code_verifier") or "",
		# The redirect URI recorded when the flow STARTED, not one rebuilt now:
		# the provider compares it to what it was given, so they must match even
		# if the site's URL changed in between.
		redirect_uri=record.get("redirect_uri") or oauth_redirect_uri(),
		# On the wire: the app's own wording of its address, the same string the
		# authorize request carried. Pinned on the row below: the canonical form.
		resource=record.get("resource_indicator") or mcp_oauth_store.resource_indicator(client),
		transport=MCP_OAUTH_TRANSPORT,
		egress_allowed=broker._egress_allowed,
	)
	mcp_oauth_store.save_token(connector, record["user"], token_set, resource, record.get("scope") or "")
	frappe.db.commit()


#: Generic, protocol-free copy for the failure pages that name no provider reason
#: (an ``expired`` state names no connector; the ``denied`` paths - stolen state,
#: iss mismatch, provider ``error`` - must stay generic). A token-exchange failure
#: overrides this with the provider's own escaped sentence.
_SIGNIN_FAILURE_COPY = {
	"denied": "We could not finish signing you in. Please try connecting again.",
	"expired": "This sign-in took too long to finish. Please start it again.",
}

#: Appended to EVERY callback page, success and failure alike, so the vendor tab the
#: SPA opened closes itself 0.8s after render (long enough to read the outcome). A no-op
#: on a tab the browser will not let a script close (one the user opened by hand), which
#: is why every page still ships the back link. Fixed markup, no interpolation.
_TAB_CLOSE_SCRIPT = "<script>setTimeout(function(){window.close()},800)</script>"


def _signin_failure_body(reason: str | None, message: str | None) -> str:
	"""The escaped body sentence for a failed-sign-in page. A token-exchange failure
	carries the provider's own reason (already folded in by ``_oauth_error_message``);
	every other path uses a fixed generic sentence, so no attacker-influenced text is
	ever shown. The whole sentence is HTML-escaped - the message template renders it
	raw."""
	if reason == "token_failed" and message:
		text = message
	else:
		text = _SIGNIN_FAILURE_COPY.get(reason or "", _SIGNIN_FAILURE_COPY["denied"])
	return html.escape(text)


def _callback_page(
	connector: str | None = None, reason: str | None = None, message: str | None = None
) -> None:
	"""Render the sign-in outcome as a small self-closing web page in the tab the SPA
	opened for the provider, instead of redirecting into the SPA. The SPA polls
	:func:`oauth_signin_status` in the original tab for the real result; this page just
	tells the person what happened and offers a link back.

	Success (``connector`` set, no ``reason``) is green; a failure (any ``reason``) is
	red and its back link drops the ``oauth`` param. BOTH append the same
	``_TAB_CLOSE_SCRIPT`` that closes the tab the SPA opened. EVERYTHING interpolated
	(the connector label, the brand name, a provider detail) is HTML-escaped, because
	the message template renders the body raw; the back link is the fixed
	``_SPA_CONNECTORS_PATH`` with the connector name percent-encoded, never a value taken
	from the callback's own query string."""
	# LOAD-BEARING escape: frappe's ``www/message.html`` renders ``{{ message }}`` with
	# Jinja autoescape OFF, so this ``html.escape`` (and the ones below) is the only thing
	# between an interpolated value and raw HTML in the page. A future "double escaping"
	# cleanup must NOT remove it.
	back_label = f"Back to {html.escape(_brand_name())}"
	if reason is None and connector:
		label = html.escape(frappe.db.get_value(CONNECTOR, connector, "label") or connector)
		body = f"You're connected to {label}. You can close this tab." + _TAB_CLOSE_SCRIPT
		frappe.respond_as_web_page(
			"Connected",
			body,
			indicator_color="green",
			primary_action=_SPA_CONNECTORS_PATH + "&oauth=" + quote(connector, safe=""),
			primary_label=back_label,
		)
		return
	frappe.respond_as_web_page(
		"Sign-in didn't finish",
		_signin_failure_body(reason, message) + _TAB_CLOSE_SCRIPT,
		indicator_color="red",
		primary_action=_SPA_CONNECTORS_PATH,
		primary_label=back_label,
	)
