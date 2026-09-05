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
  * ``list_connectors``       — the pane's two-section list (Shared + Mine).
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
    URL for an OAuth-auth-method row; Frappe's native Connected App callback
    handles the return trip, no custom callback lives here.
  * ``disconnect_oauth``      - deletes the CURRENT user's Token Cache for an
    OAuth row (per-user, idempotent).

``set_custom_url_policy`` is deliberately NOT here — MCP_CONNECTORS_PLAN.md's
UI/UX decision #2 puts that admin control on the Jarvis Settings Desk form.

Boot wiring: ``connector_flags()`` (not whitelisted) is also imported by
``jarvis/www/jarvis.py`` to ship ``connectors_enabled``/
``connectors_allow_custom_urls`` in the SPA boot payload, so the nav can gate
the Connectors tab without a round trip. ``list_connectors`` returns the same
two flags for callers that only have the API (e.g. a stale boot cache).
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from jarvis.connectors import broker, oauth
from jarvis.permissions import require_jarvis_user

CONNECTOR = "Jarvis Connector"
ACTION_DT = "Jarvis Connector Action"
SETTINGS = "Jarvis Settings"

# Per-user rate limit on the outbound Test-connection probe (calendar-minute
# bucket). The probe fires a real network call to a user-chosen host, so it must
# not be spammable; the broker's circuit breaker + concurrency cap protect the
# WORKER, this protects against a single user hammering the button.
_TEST_RATE_PER_MIN = 20

# Vendor MCP endpoints for the four built-in presets (validated against each
# vendor's current docs 2026-09-04 — see the P3 report). Pinned SERVER-SIDE:
# add_connector/update_connector never let a caller point a "preset"
# connector anywhere else, which is what makes the Custom URL policy
# (Jarvis Settings.allow_custom_urls) mean anything at all.
_PRESET_BASE_URLS = {
	"GitHub": "https://api.githubcopilot.com/mcp/",
	"Atlassian": "https://mcp.atlassian.com/v2/mcp",
	"Linear": "https://mcp.linear.app/mcp",
	"Stripe": "https://mcp.stripe.com/",
}
_PRESET_KEYS = {"GitHub": "github", "Atlassian": "atlassian", "Linear": "linear", "Stripe": "stripe"}
_PRESETS = (*_PRESET_BASE_URLS, "Custom URL")
_SCOPES = ("Shared", "Personal")
_AUTH_METHODS = ("API Key", "OAuth")

# Preset -> the Connected App's own ``provider_name`` (an operator-set System
# Manager-only field, never client input). Extend this map, not the resolver,
# when a second preset gets an OAuth path.
_PRESET_PROVIDER = {"GitHub": "GitHub"}

_DESC_MAX = 500


# --------------------------------------------------------------------------- #
# settings flags (shared with jarvis/www/jarvis.py boot payload)
# --------------------------------------------------------------------------- #
def connector_flags() -> dict:
	"""``{enabled, allow_custom_urls}`` off ``Jarvis Settings``. NOT whitelisted
	— called directly by ``list_connectors`` below and by ``www/jarvis.py`` for
	the boot payload (design section 2's kill switch + custom-URL policy).

	``enabled`` reuses ``_connector_gate.connectors_enabled()`` — the SAME source
	of truth the chat tools gate on — so the ``jarvis_connectors_enabled``
	site_config override (an operator's fleet-wide kill switch) is honored here
	too; otherwise the SPA boot payload could say "on" while chat returns
	``connectors_disabled``.
	``allow_custom_urls`` defaults to 1 but — per its own field description —
	Single defaults are NOT backfilled onto an existing site's ``tabSingles``
	row on migrate, and ``get_single_value`` coerces a genuinely missing row to
	0/off via ``cint`` — indistinguishable from an admin explicitly turning it
	off. So it needs the same tabSingles row-existence probe
	``personalise_api._single_bool`` uses, treating "no row at all" as ON."""
	from jarvis.tools._connector_gate import connectors_enabled

	return {
		"enabled": connectors_enabled(),
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
	"""One aggregate query mapping ``connector name -> {allowed, total}`` over
	its ``allowed_actions`` children — avoids an N+1 when listing."""
	if not parent_names:
		return {}
	rows = frappe.get_all(
		ACTION_DT,
		filters={"parent": ["in", parent_names], "parenttype": CONNECTOR},
		fields=["parent", "allowed"],
	)
	out: dict[str, dict] = {}
	for row in rows:
		summary = out.setdefault(row["parent"], _action_summary(0, 0))
		summary["total"] += 1
		if row["allowed"]:
			summary["allowed"] += 1
	return out


def _resolve_connected_app_for_preset(preset: str) -> str | None:
	"""The site's Connected App for ``preset``'s OAuth provider, or ``None`` when
	none is configured yet. Matched on ``provider_name`` - an operator-set,
	System-Manager-only field on Connected App - NEVER on anything the caller
	supplies, so a client can ask for OAuth but can never name or steer which
	Connected App backs it (OAUTH_CONNECTORS_DESIGN.md §6b)."""
	provider = _PRESET_PROVIDER.get(preset)
	if not provider:
		return None
	found = frappe.get_all("Connected App", filters={"provider_name": provider}, pluck="name", limit=1)
	return found[0] if found else None


def _oauth_status(doc) -> dict:
	"""``{oauth_configured, oauth_connected}`` for an OAuth-auth-method row.
	``oauth_configured`` is true when a Connected App still resolves for the
	preset (an admin could remove it after the row was created).

	``oauth_connected`` is PRESENCE of a real access token on the current
	user's Token Cache - NOT mere presence of the Token Cache doc. Frappe's own
	``initiate_web_application_flow`` creates that doc up front to hold ``state``
	before the user ever reaches the provider's consent screen, so a bare
	``get_token_cache`` truthy check would read "connected" for a user who
	clicked Connect and never finished (or bounced off consent). Mirrors
	Frappe's own ``connected_app.has_token``; this must never trigger a refresh
	(a stale/expired token still counts as "connected" for display - the broker
	is what enforces liveness on an actual call)."""
	connected_app = doc.get("connected_app")
	if not connected_app or not frappe.db.exists("Connected App", connected_app):
		return {"oauth_configured": False, "oauth_connected": False}
	app = frappe.get_doc("Connected App", connected_app)
	token_cache = app.get_token_cache(frappe.session.user)
	connected = bool(token_cache and token_cache.get_password("access_token", False))
	return {"oauth_configured": True, "oauth_connected": connected}


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
	"""``{enabled, allow_custom_urls, shared, mine}``. ``frappe.get_list``
	(not ``get_all``) so ``connector_query_conditions`` scopes the query the
	same way the Desk list view is scoped: every Shared row plus the caller's
	own Personal rows — nothing more. Never selects ``credential``."""
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
			"connected_app",
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
		if row["auth_method"] == oauth.OAUTH_AUTH_METHOD:
			row.update(_oauth_status(row))
		row.pop("connected_app", None)  # internal - never shipped to the SPA
		(shared if row["scope"] == "Shared" else mine).append(row)

	return {
		"enabled": flags["enabled"],
		"allow_custom_urls": flags["allow_custom_urls"],
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
	base_url: str,
	scope: str,
	credential: str,
	label: str | None = None,
	key: str | None = None,
	auth_method: str = "API Key",
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

	``auth_method`` picks the connection method (OAUTH_CONNECTORS_DESIGN.md §4,
	§6b): "OAuth" IGNORES the ``credential`` argument entirely (nothing is ever
	stored in the Password field for an OAuth row) and resolves
	``connected_app`` SERVER-SIDE from the preset - a caller can ask for OAuth
	but can never name or steer which Connected App backs it. "API Key" is the
	shipped, unchanged behaviour."""
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
		frappe.throw(_("Auth Method must be API Key or OAuth."))

	if preset == "Custom URL":
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
		"enabled": 1,
	}
	if auth_method == oauth.OAUTH_AUTH_METHOD:
		# Never store a pasted secret for an OAuth row, and never trust a
		# client-supplied Connected App - resolve it from the preset ourselves.
		connected_app = _resolve_connected_app_for_preset(preset)
		if not connected_app:
			frappe.throw(_("This app isn't set up for sign-in yet. Ask your admin."))
		doc_fields["credential"] = ""
		doc_fields["connected_app"] = connected_app
	else:
		doc_fields["credential"] = credential

	doc = frappe.get_doc(doc_fields)
	doc.insert()
	frappe.db.commit()
	return _connector_summary(doc)


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

	Two guards run BEFORE the outbound probe:
	  * the site-wide kill switch (``connectors_enabled``) - an operator's incident
	    override must stop the outbound probe too, not only ``call_connector``;
	  * a per-user rate limit - the probe is a real network call to a user-chosen
	    host, so it must not be spammable.

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
	from jarvis.tools._connector_gate import connectors_enabled

	if not connectors_enabled():
		return {
			"ok": False,
			"error": {
				"code": "connectors_disabled",
				"message": "Connectors are not enabled for this workspace.",
			},
		}

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

	if oauth.is_oauth(doc) and not oauth.resolve_access_token(doc):
		# Fail BEFORE the outbound probe (and before touching the rate limit's
		# sibling breaker/cap): an unconnected OAuth row has no bearer to test
		# with, and that is a sign-in problem, not a transport/health one.
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
	authorize URL for the SPA to redirect the browser to. Frappe's native
	Connected App callback (``connected_app.callback/{app}``) handles the
	provider's redirect back and writes the Token Cache itself - there is no
	custom callback here.

	``success_uri`` sends the browser back to the SPA's connectors settings
	pane, carrying ``name`` so it can refresh that one row's status once the
	user returns. Connect is PER USER (OAUTH_CONNECTORS_DESIGN.md §6a): each
	user who wants to use a Shared OAuth connector runs their own sign-in."""
	doc = frappe.get_doc(CONNECTOR, name)
	if not doc.has_permission("read"):
		frappe.throw(_("Not permitted."), frappe.PermissionError)

	if not oauth.is_oauth(doc):
		return {"ok": False, "error": {"code": "not_oauth", "message": "This connector doesn't use sign-in."}}

	connected_app = doc.get("connected_app")
	if not connected_app or not frappe.db.exists("Connected App", connected_app):
		return {
			"ok": False,
			"error": {
				"code": "oauth_not_configured",
				"message": "This app isn't set up for sign-in yet. Ask your admin.",
			},
		}

	app = frappe.get_doc("Connected App", connected_app)
	# A bare path, not an absolute URL built off frappe.utils.get_url(): Frappe's
	# own sanitize_redirect (frappe.www.login) only trusts a redirect whose netloc
	# matches the CURRENT request's host, and rewrites anything else to "/desk".
	# get_url() reads site_config, which can legitimately differ from the host the
	# browser actually used (e.g. an aliased/e2e host) - a bare path has no netloc,
	# so sanitize_redirect always resolves it against the real request host instead.
	success_uri = "/jarvis?settings=connectors&oauth=" + name
	url = app.initiate_web_application_flow(user=frappe.session.user, success_uri=success_uri)
	return {"ok": True, "url": url}


@frappe.whitelist()
@require_jarvis_user
def disconnect_oauth(name: str) -> dict:
	"""End the CURRENT user's sign-in for an OAuth connector by deleting their
	Token Cache. Idempotent - never errors when there was nothing to remove
	(no Connected App configured, or the user never connected)."""
	doc = frappe.get_doc(CONNECTOR, name)
	if not doc.has_permission("read"):
		frappe.throw(_("Not permitted."), frappe.PermissionError)

	connected_app = doc.get("connected_app")
	if not connected_app or not frappe.db.exists("Connected App", connected_app):
		return {"ok": True}

	app = frappe.get_doc("Connected App", connected_app)
	token_cache = app.get_token_cache(frappe.session.user)
	if token_cache:
		frappe.delete_doc("Token Cache", token_cache.name, ignore_permissions=True, force=True)
		frappe.db.commit()
	return {"ok": True}
