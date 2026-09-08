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

Why not ``frappe.get_doc`` per connector: nothing here mutates a document or
needs a Document method, and a full load pulls every parent column (the whole
``tools_cache`` blob and the encrypted credential included) plus a child SELECT
each, per connector, per chat turn, just to read four flags per action.

Each connector's actions are read from its ``allowed_actions`` child rows (the
same stored flags ``jarvis.connectors.broker``/``policy`` gate a real call
against - never the connector's raw, untrusted MCP ``tools_cache``
descriptors) and narrowed to exactly the ones ``policy.action_decision`` would
let through right now, so what the model sees here is exactly what
``call_connector`` will accept - never a denied or destructive action it
would then have to be told no about.
"""

from __future__ import annotations

import frappe

from jarvis.connectors import action_rows, policy
from jarvis.connectors.action_rows import ACTIONS_FIELD, CONNECTOR_DOCTYPE

_ACTION_FIELDS = ["action", "allowed", "read_only", "destructive", "description"]

_MAX_CONNECTORS = 30
_MAX_ACTIONS_PER_CONNECTOR = 50
_DESCRIPTION_MAX = 200


def list_connector_actions(connector: str | None = None) -> dict:
	"""List the connectors, and each one's currently-allowed actions, visible
	to the calling user.

	Pass ``connector`` (its key, e.g. ``"github"``) to narrow to one
	connector; omit it to list every connector the caller may use. Shared and
	the caller's own Personal connectors are both included, de-duplicated by
	key with the Personal row winning over a Shared row of the same key - the
	same resolution ``call_connector`` uses.

	Returns ``{"connectors": [{"connector", "label", "scope", "actions":
	[{"action", "description"}]}]}``. When the caller can see no connectors,
	returns ``{"connectors": []}`` - never an error.
	"""
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

	actions_by_parent = action_rows.by_parent([row["name"] for row in by_key.values()], _ACTION_FIELDS)
	return {
		"connectors": [
			{
				"connector": row["key"],
				"label": row["label"],
				"scope": row["scope"],
				"actions": _allowed_actions(actions_by_parent.get(row["name"], [])),
			}
			for row in by_key.values()
		]
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
