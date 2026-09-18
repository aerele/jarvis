"""list_connector_actions - read-only discovery of the connectors (and their
allowed actions) the calling user may reach through ``call_connector``.

Per-user, like the SPA pane: a Shared connector is visible to every tenant
user, a Personal connector only to its owner
(``jarvis.chat.connector_permissions``). The permission boundary is the parent
query ALONE: connectors come from ``frappe.get_list`` (permission-checked,
unlike ``get_all``) under the caller's impersonated identity, never
hand-filtered by owner, so it cannot leak a row the permission hook would have
denied. The child rows then come from ONE ``frappe.get_all`` on ``Jarvis
Connector Action`` (``jarvis.connectors.action_rows``, shared with the
Settings list) bounded to exactly those surviving parent names. ``get_all``
skips Frappe's permission check and a child DocType has no permission hook of
its own, so that bounding is load-bearing: it is safe only because ``names``
never holds a row the caller could not list.

Why not ``frappe.get_doc`` anywhere here: nothing mutates a document or needs a
Document method, and a full load pulls every parent column (the whole
``tools_cache`` blob and the encrypted credential included) plus a child SELECT
each, per connector, per chat turn, just to read four flags per action. The
default listing reads only the four child flags; the named-action view adds one
bounded ``frappe.db.get_values`` for the ``tools_cache`` column of the rows that
passed the gate - a read, never ``get_doc``, and bounded to the same listed
names.

The allow decision AND each action's description come only from the STORED
child-row flags on ``allowed_actions`` (the same flags
``jarvis.connectors.broker``/``policy`` gate a real call against), never from
the connector's raw, untrusted MCP ``tools_cache`` descriptors, and narrowed to
exactly the actions ``policy.action_decision`` would let through right now - so
what the model sees is exactly what ``call_connector`` will accept, never a
denied or destructive action it would then have to be told no about. Only when
a single ``action`` is named does the view additionally read that one action's
``inputSchema`` from ``tools_cache``, and only for rows that already passed the
gate; that schema is untrusted server data, so it is passed through unchanged
(size-capped), never consulted for any decision.
"""

from __future__ import annotations

import json

import frappe

from jarvis.connectors import action_rows, policy
from jarvis.connectors.action_rows import ACTIONS_FIELD, CONNECTOR_DOCTYPE

_ACTION_FIELDS = ["action", "allowed", "read_only", "destructive", "description"]

_MAX_CONNECTORS = 30
_MAX_ACTIONS_PER_CONNECTOR = 50
_DESCRIPTION_MAX = 200
# Cap on the serialised inputSchema returned to the model, protecting the chat
# turn's token budget from a pathological server schema. ``json.dumps`` defaults
# to ``ensure_ascii=True``, so the character length equals the byte length.
_SUMMARY_MAX_NAMES = 100
_SUMMARY_NAME_MAX = 128
_SCHEMA_MAX_BYTES = 16 * 1024


def list_connector_actions(connector: str | None = None, action: str | None = None) -> dict:
	"""List the connectors, and each one's currently-allowed actions, visible
	to the calling user - and, on request, the argument schema for one action.

	Pass ``connector`` (its key, e.g. ``"github"``) to narrow to one connector;
	omit it to list every connector the caller may use. Shared and the caller's
	own Personal connectors are both included, de-duplicated by key with the
	Personal row winning over a Shared row of the same key - the same resolution
	``call_connector`` uses.

	Without ``action`` (or an empty ``action``) this is the plain listing.
	Returns ``{"connectors": [{"connector", "label", "scope", "actions":
	[{"action", "description"}]}]}`` - one entry per visible connector, each
	action trimmed to name and description. When the caller can see no
	connectors, returns ``{"connectors": []}`` - never an error.

	With ``action`` this is the argument-schema view for that one action. Call it
	with ``connector`` and ``action=<name>`` before ``call_connector`` whenever
	the action takes arguments, to shape those arguments to the server's schema
	instead of guessing. Each action entry is
	``{"action", "description", "input_schema"}`` where ``input_schema`` is the
	connector's cached MCP ``inputSchema`` for that action, or ``null`` when the
	cache holds no schema for it. If that schema serialises past 16 KiB it is
	replaced by a compact summary of its top level,
	``{"truncated": true, "required": [names], "properties": [names]}``, so a
	huge schema never floods the turn.

	Resolution and gating with ``action``:

	  * ``connector`` given -> exactly one connector entry (or
	    ``{"connectors": []}`` when the key resolves to nothing). Its ``actions``
	    is ``[the one entry]`` when ``policy.action_decision`` allows the action,
	    and ``[]`` when the action is unknown to, or denied on, that connector.
	  * ``connector`` omitted -> a search across every visible connector,
	    returning only the ones that allow the action (the matches), one action
	    entry each. Connectors that do not offer the action are absent.

	The ``inputSchema`` is untrusted server data and never influences the allow
	decision, which reads only the stored child-row flags.
	"""
	by_key = _resolve_connectors(connector)
	if action:
		return _schema_view(by_key, action, single=bool(connector))
	return _listing_view(by_key)


def _resolve_connectors(connector: str | None) -> dict[str, dict]:
	"""The visible, enabled connectors as ``{key: row}``, Personal winning over
	Shared for a shared key. The single permission-checked query both views use;
	nothing downstream re-filters by owner."""
	filters: dict = {"enabled": 1}
	if connector:
		filters["key"] = connector
	rows = frappe.get_list(
		CONNECTOR_DOCTYPE,
		filters=filters,
		fields=["name", "key", "label", "scope"],
		# "Personal" sorts before "Shared" - the de-dupe below keeps the
		# FIRST row seen per key, so this ordering is what makes Personal win.
		order_by="scope asc, label asc",
		limit_page_length=_MAX_CONNECTORS,
	)
	by_key: dict[str, dict] = {}
	for row in rows:
		by_key.setdefault(row["key"], row)
	return by_key


def _listing_view(by_key: dict[str, dict]) -> dict:
	"""The plain listing: every visible connector with its policy-allowed
	actions trimmed to ``{action, description}``."""
	actions_by_parent = action_rows.by_parent([row["name"] for row in by_key.values()], _ACTION_FIELDS)
	return {
		"connectors": [
			_connector_entry(row, _allowed_actions(actions_by_parent.get(row["name"], [])))
			for row in by_key.values()
		]
	}


def _schema_view(by_key: dict[str, dict], action: str, single: bool) -> dict:
	"""The named-action view: the one ``{action, description, input_schema}``
	entry for ``action`` per connector. ``single`` (a connector was named) keeps
	an entry with empty actions on deny/unknown; a bare action (search) returns
	only the connectors that allow it."""
	rows = list(by_key.values())
	actions_by_parent = action_rows.by_parent([row["name"] for row in rows], _ACTION_FIELDS)
	# Decide FIRST, then read tools_cache ONCE for only the rows that passed the
	# gate: the untrusted schema blob is never pulled for a denied action or a
	# non-match, and never row-by-row (no get_value in a loop).
	matched: dict[str, dict | None] = {}
	for row in rows:
		children = actions_by_parent.get(row["name"], [])
		if policy.action_decision({ACTIONS_FIELD: children}, action) is None:
			matched[row["name"]] = _child_for(children, action)
	caches = _tools_caches(list(matched))
	connectors = []
	for row in rows:
		name = row["name"]
		if name in matched:
			item = _schema_item(action, matched[name], caches.get(name))
			connectors.append(_connector_entry(row, [item]))
		elif single:
			connectors.append(_connector_entry(row, []))
	return {"connectors": connectors}


def _tools_caches(names: list[str]) -> dict[str, object]:
	"""``{connector name: raw tools_cache}`` for ``names`` in ONE bounded read.
	A read, so ``frappe.db.get_values``, never ``get_doc``; bounded to names the
	caller already listed - the same permission boundary as
	``action_rows.by_parent``."""
	if not names:
		return {}
	rows = frappe.db.get_values(
		CONNECTOR_DOCTYPE,
		{"name": ["in", names]},
		["name", "tools_cache"],
		as_dict=True,
	)
	return {row["name"]: row.get("tools_cache") for row in rows or []}


def _schema_item(action: str, child: dict | None, cache) -> dict:
	"""One ``{action, description, input_schema}`` entry for an allowed action.
	``input_schema`` is the cached MCP schema, size-capped, or ``None`` when the
	cache has none for this action."""
	description = (child.get("description") or "") if child else ""
	return {
		"action": action,
		"description": description[:_DESCRIPTION_MAX],
		"input_schema": _capped_schema(policy.input_schema({"tools_cache": cache}, action)),
	}


def _capped_schema(schema_obj):
	"""``schema_obj`` unchanged, unless it serialises past ``_SCHEMA_MAX_BYTES``,
	in which case a compact top-level summary. The schema is untrusted server
	data, so nothing here trusts its shape."""
	if schema_obj is None:
		return None
	try:
		serialised = json.dumps(schema_obj)
	except (TypeError, ValueError):
		return _schema_summary(schema_obj)
	if len(serialised) <= _SCHEMA_MAX_BYTES:
		return schema_obj
	return _schema_summary(schema_obj)


def _schema_summary(schema_obj) -> dict:
	"""Top-level shape of an over-large ``inputSchema``: its required names and
	its property names, nothing nested. Untrusted, so odd/non-dict types degrade
	to empty lists rather than raising."""
	required: list[str] = []
	properties: list[str] = []
	if isinstance(schema_obj, dict):
		raw_required = schema_obj.get("required")
		if isinstance(raw_required, list):
			required = _names(raw_required)
		raw_properties = schema_obj.get("properties")
		if isinstance(raw_properties, dict):
			properties = _names(list(raw_properties.keys()))
	return {"truncated": True, "required": required, "properties": properties}


def _names(raw: list) -> list[str]:
	"""At most ``_SUMMARY_MAX_NAMES`` string names, each clipped to
	``_SUMMARY_NAME_MAX`` chars: the summary must stay small even when the schema
	that overflowed the cap did so through its property names."""
	return [name[:_SUMMARY_NAME_MAX] for name in raw if isinstance(name, str)][:_SUMMARY_MAX_NAMES]


def _child_for(children: list[dict], action: str) -> dict | None:
	"""The child row for ``action``, or ``None``. Present for any action the gate
	allowed (the gate denies an action with no child row)."""
	for child in children:
		if child.get("action") == action:
			return child
	return None


def _connector_entry(row: dict, actions: list[dict]) -> dict:
	"""The connector envelope shared by both views."""
	return {
		"connector": row["key"],
		"label": row["label"],
		"scope": row["scope"],
		"actions": actions,
	}


def _allowed_actions(children: list[dict]) -> list[dict]:
	"""The subset of ``children`` ``policy.action_decision`` currently permits,
	each trimmed to a compact ``{action, description}``."""
	row = {ACTIONS_FIELD: children}
	actions = []
	for child in children:
		if policy.action_decision(row, child["action"]) is not None:
			continue
		actions.append(
			{
				"action": child["action"],
				"description": (child.get("description") or "")[:_DESCRIPTION_MAX],
			}
		)
		if len(actions) >= _MAX_ACTIONS_PER_CONNECTOR:
			break
	return actions
