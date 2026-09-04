"""list_connector_actions - read-only discovery of the connectors (and their
allowed actions) the calling user may reach through ``call_connector``.

Per-user, like the SPA pane: a Shared connector is visible to every tenant
user, a Personal connector only to its owner
(``jarvis.chat.connector_permissions``). This tool relies ENTIRELY on Frappe
row permissions under the caller's impersonated identity - it queries with
``frappe.get_list`` (permission-checked, unlike ``get_all``) and never
hand-filters by owner itself, so it cannot leak a row the permission hook
would have denied.

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

from jarvis.connectors import policy
from jarvis.tools._connector_gate import connectors_enabled

CONNECTOR_DOCTYPE = "Jarvis Connector"

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
	[{"action", "description"}]}]}``. When connectors are turned off for this
	workspace, or the caller can see none, returns ``{"connectors": []}`` -
	never an error.
	"""
	if not connectors_enabled():
		return {"connectors": []}

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

	connectors = []
	for row in by_key.values():
		doc = frappe.get_doc(CONNECTOR_DOCTYPE, row["name"])
		connectors.append(
			{
				"connector": doc.key,
				"label": doc.label,
				"scope": doc.scope,
				"actions": _allowed_actions(doc),
			}
		)
	return {"connectors": connectors}


def _allowed_actions(doc) -> list[dict]:
	"""The subset of ``doc``'s configured actions ``policy.action_decision``
	currently permits, each trimmed to a compact ``{action, description}``."""
	actions = []
	for child in doc.allowed_actions or []:
		if policy.action_decision(doc, child.action) is not None:
			continue
		actions.append(
			{
				"action": child.action,
				"description": (child.description or "")[:_DESCRIPTION_MAX],
			}
		)
		if len(actions) >= _MAX_ACTIONS_PER_CONNECTOR:
			break
	return actions
