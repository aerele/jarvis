"""Which listed connectors the caller can use right now, and why not.

Shared by the per-turn "connected apps" line and the no-argument listing of
``list_connector_actions``, so the two can never disagree about a connector.

A connector is ready when all of these hold:

  * its last connection test passed;
  * at least one stored action is allowed (``policy.action_decision``), so
    ``call_connector`` has something it would accept;
  * for a sign-in (OAuth) connector, the caller has connected it.

The sign-in check is ONE bounded read of the token DocType. It never refreshes
a token: a listing must stay cheap and must not call the provider.
"""

from __future__ import annotations

import frappe

from jarvis.connectors import oauth, policy
from jarvis.connectors.action_rows import ACTIONS_FIELD
from jarvis.connectors.mcp_oauth_store import TOKEN_DT

NOT_TESTED = "not_tested"
NO_ALLOWED_ACTIONS = "no_allowed_actions"
NOT_SIGNED_IN = "not_signed_in"

# Fields the rows and child rows must carry for ``reasons``.
ROW_FIELDS = ("last_test_status", "auth_method")
ACTION_FIELDS = ("action", "allowed")


def reasons(rows: list[dict], actions_by_parent: dict[str, list[dict]]) -> dict[str, str | None]:
	"""``{connector name: reason}`` for ``rows``; ``None`` means ready.
	``rows`` must come from the permission-checked resolver and carry
	``ROW_FIELDS``; ``actions_by_parent`` from ``action_rows.by_parent``."""
	out = {row["name"]: _static_reason(row, actions_by_parent.get(row["name"], [])) for row in rows}
	candidates = [row["name"] for row in rows if out[row["name"]] is None and oauth.is_oauth(row)]
	signed_in = _signed_in(candidates)
	for name in candidates:
		if name not in signed_in:
			out[name] = NOT_SIGNED_IN
	return out


def _static_reason(row: dict, children: list[dict]) -> str | None:
	if row.get("last_test_status") != "Passed":
		return NOT_TESTED
	if not _has_allowed_action(children):
		return NO_ALLOWED_ACTIONS
	return None


def _has_allowed_action(children: list[dict]) -> bool:
	row = {ACTIONS_FIELD: children}
	return any(policy.action_decision(row, child.get("action")) is None for child in children)


def _signed_in(names: list[str]) -> set[str]:
	"""The connectors in ``names`` the session user has a stored token for.
	``get_all`` because the token DocType grants the Jarvis User role nothing;
	it is safe because ``names`` come from the permission-checked resolver and
	the read is pinned to the session user's own tokens."""
	if not names:
		return set()
	connectors = frappe.get_all(
		TOKEN_DT,
		filters={"connector": ["in", names], "user": frappe.session.user},
		pluck="connector",
	)
	return set(connectors)
