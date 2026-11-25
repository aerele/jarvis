"""The ``allowed_actions`` child rows of a set of connectors, in ONE bounded
``frappe.get_all``. Shared by the agent-facing discovery tool
(``jarvis.tools.list_connector_actions``) and the Settings list
(``jarvis.chat.connectors_api``) so the N+1 avoidance and its permission
reasoning live in exactly one place.

``get_all`` skips Frappe's permission check and a child DocType has no
permission hook of its own, so ``parent_names`` MUST come from a
permission-checked parent query (``frappe.get_list`` under the calling user).
That bounding is the permission boundary; nothing in here adds one.
"""

from __future__ import annotations

import frappe

CONNECTOR_DOCTYPE = "Jarvis Connector"
ACTION_DOCTYPE = "Jarvis Connector Action"
ACTIONS_FIELD = "allowed_actions"


def by_parent(parent_names: list[str], fields: list[str]) -> dict[str, list[dict]]:
	"""``{connector name: [child row, ...]}`` for ``parent_names``, each row
	carrying ``fields`` plus ``parent``, in stored (``idx``) order. A parent
	with no rows is absent, so read with ``.get(name, [])``."""
	if not parent_names:
		return {}
	rows = frappe.get_all(
		ACTION_DOCTYPE,
		filters={
			"parent": ["in", parent_names],
			"parenttype": CONNECTOR_DOCTYPE,
			"parentfield": ACTIONS_FIELD,
		},
		fields=["parent", *fields],
		order_by="parent asc, idx asc",
	)
	grouped: dict[str, list[dict]] = {}
	for row in rows:
		grouped.setdefault(row["parent"], []).append(row)
	return grouped
