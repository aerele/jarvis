"""MCP Connectors — SPA API (MCP_CONNECTORS_PLAN.md P3).

The SPA's Connectors settings pane talks to the six endpoints below. Every
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

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from jarvis.connectors import broker
from jarvis.permissions import require_jarvis_user

CONNECTOR = "Jarvis Connector"
ACTION_DT = "Jarvis Connector Action"
SETTINGS = "Jarvis Settings"

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

_DESC_MAX = 500


# --------------------------------------------------------------------------- #
# settings flags (shared with jarvis/www/jarvis.py boot payload)
# --------------------------------------------------------------------------- #
def connector_flags() -> dict:
	"""``{enabled, allow_custom_urls}`` off ``Jarvis Settings``. NOT whitelisted
	— called directly by ``list_connectors`` below and by ``www/jarvis.py`` for
	the boot payload (design section 2's kill switch + custom-URL policy).

	``connectors_enabled`` defaults to 0, so a plain ``get_single_value`` read
	is safe (an unset row already reads back as the intended-safe "off").
	``allow_custom_urls`` defaults to 1 but — per its own field description —
	Single defaults are NOT backfilled onto an existing site's ``tabSingles``
	row on migrate, and ``get_single_value`` coerces a genuinely missing row to
	0/off via ``cint`` — indistinguishable from an admin explicitly turning it
	off. So it needs the same tabSingles row-existence probe
	``personalise_api._single_bool`` uses, treating "no row at all" as ON."""
	return {
		"enabled": bool(frappe.db.get_single_value(SETTINGS, "connectors_enabled")),
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


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _clip(text: str, length: int) -> str:
	text = text or ""
	return text if len(text) <= length else text[:length]


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


def _connector_summary(doc) -> dict:
	"""Same item shape as a ``list_connectors`` row, built from a loaded
	Document (add_connector/update_connector's return value). NEVER includes
	``credential`` — nothing below reads it."""
	children = doc.get("allowed_actions") or []
	total = len(children)
	allowed = sum(1 for c in children if c.get("allowed"))
	last_test_at = doc.get("last_test_at")
	return {
		"name": doc.name,
		"key": doc.key,
		"label": doc.label,
		"preset": doc.preset,
		"base_url": doc.base_url,
		"scope": doc.scope,
		"enabled": bool(doc.enabled),
		"last_test_status": doc.get("last_test_status") or "",
		"last_test_at": str(last_test_at) if last_test_at else None,
		"allowed_actions": _action_summary(allowed, total),
	}


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
	"""Build the new ``allowed_actions`` row set from a fresh ``tools/list``,
	preserving every existing ``allowed`` choice for an action that is still
	present (re-testing must never silently revoke an admin's earlier grant),
	and defaulting a newly-seen action to ``allowed = read_only`` (read-only
	pre-checked, writes off — MCP_CONNECTORS_PLAN.md UI/UX decision #3). An
	action that vanished from the server's tool list is simply not re-added."""
	existing_allowed = {c.get("action"): bool(c.get("allowed")) for c in existing_children if c.get("action")}
	merged: list[dict] = []
	seen: set[str] = set()
	for tool in tools:
		action = (tool.get("name") or "").strip()
		if not action or action in seen:
			continue
		seen.add(action)
		read_only, destructive = _tool_flags(tool)
		allowed = existing_allowed.get(action, read_only)
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
	label: str,
	preset: str,
	base_url: str,
	scope: str,
	credential: str,
	key: str | None = None,
) -> dict:
	"""Create a connector. Never marks it Passed — a fresh row's
	``last_test_status`` is blank by field default, and only ``test_connector``
	may ever set it to Passed. ``doc.insert()`` runs WITHOUT
	``ignore_permissions``, so both the plain create-role check and the
	controller's ``_guard_shared_scope`` (only the admin tier may create a
	Shared row) apply exactly as they do from the Desk — a plain user asking
	for ``scope="Shared"`` fails cleanly with the controller's own
	``frappe.PermissionError``, not a custom message here."""
	label = (label or "").strip()
	preset = (preset or "").strip()
	scope = (scope or "").strip()
	credential = credential or ""

	if preset not in _PRESETS:
		frappe.throw(_("Unknown connector preset."))
	if scope not in _SCOPES:
		frappe.throw(_("Scope must be Shared or Personal."))

	if preset == "Custom URL":
		if not connector_flags()["allow_custom_urls"]:
			frappe.throw(
				_(
					"Custom URL connectors are turned off. Ask an administrator to "
					"enable them, or choose one of the built-in presets."
				)
			)
		resolved_base_url = (base_url or "").strip()
	else:
		# Presets are pinned to the vendor's own endpoint — a caller's base_url
		# is ignored entirely for anything but Custom URL.
		resolved_base_url = _PRESET_BASE_URLS[preset]

	resolved_key = (key or _PRESET_KEYS.get(preset) or "").strip()

	doc = frappe.get_doc(
		{
			"doctype": CONNECTOR,
			"key": resolved_key,
			"label": label,
			"preset": preset,
			"base_url": resolved_base_url,
			"scope": scope,
			"credential": credential,
			"enabled": 1,
		}
	)
	doc.insert()
	frappe.db.commit()
	return _connector_summary(doc)


# --------------------------------------------------------------------------- #
# 3. test
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def test_connector(name: str) -> dict:
	"""Run initialize + tools/list through ``broker.test_connector`` and, on
	success, write ``tools_cache`` + ``last_test_status="Passed"`` and MERGE the
	``allowed_actions`` table from the result (see ``_merge_allowed_actions``).
	On failure, only ``last_test_status="Failed"`` moves — an existing good
	``tools_cache``/``allowed_actions`` from a PRIOR passing test is left alone,
	so a transient failure never wipes an already-working connector's config.

	Gated on READ, not write: any user who can see a Shared connector (every
	tenant user) may run a live health/discovery probe against it, but must
	not be able to edit its base_url/credential — so the parent-field write
	goes through ``frappe.db.set_value`` (bypasses the write-only DocType
	permission a plain reader would fail), never ``doc.save()``. The
	``allowed_actions`` MERGE is a server-derived rewrite (see
	``_replace_allowed_actions``) that can only ever grant a read-only,
	non-destructive default or preserve an admin's existing choice — it can
	never turn ON a write/destructive action a plain user just discovered.
	"""
	doc = frappe.get_doc(CONNECTOR, name)
	if not doc.has_permission("read"):
		frappe.throw(_("Not permitted."), frappe.PermissionError)

	result = broker.test_connector(doc)
	now = now_datetime()

	if not result.get("ok"):
		frappe.db.set_value(
			CONNECTOR,
			doc.name,
			{"last_test_status": "Failed", "last_test_at": now},
			update_modified=False,
		)
		frappe.db.commit()
		return {
			"ok": False,
			"error": result.get("error")
			or {"code": "unknown_error", "message": "The connection test failed."},
		}

	tools = [t for t in (result.get("tools") or []) if isinstance(t, dict)]
	merged = _merge_allowed_actions(doc.get("allowed_actions") or [], tools)
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
