"""Shared site-wide kill switch for the two connector-facing tools
(``call_connector`` / ``list_connector_actions``).

Neither tool lives under ``jarvis.connectors`` (that package is reserved for
the broker plane - see its ``__init__`` docstring), so this check is theirs to
own; it deliberately does NOT import ``jarvis.connectors.broker`` and must run
BEFORE either tool touches it, so a disabled workspace never reaches the
broker, a row lookup, or an outbound call at all.

Mirrors ``jarvis.connectors.broker._egress_allowed``'s read style (a bare
``frappe.db.get_single_value``), but the two checks are independent: this is
the all-or-nothing feature flag, that is the per-host egress policy consulted
only once the flag is already on.
"""

from __future__ import annotations

import frappe

SETTINGS_DOCTYPE = "Jarvis Settings"

# Optional site_config.json override, checked first so an operator can force
# connectors off (or on, ahead of the Jarvis Settings row) without a Desk
# edit - e.g. to kill a misbehaving connector fleet-wide during an incident.
_SITE_CONFIG_KEY = "jarvis_connectors_enabled"


def connectors_enabled() -> bool:
	"""Whether MCP connectors are turned on for this workspace.

	Fail-CLOSED: an unset/unreadable ``Jarvis Settings.connectors_enabled``
	(default ``0``) or any error reading it means OFF, never a silent
	fall-open. A site_config override, when present, wins outright in either
	direction."""
	override = frappe.conf.get(_SITE_CONFIG_KEY)
	if override is not None:
		return bool(override)
	try:
		return bool(frappe.db.get_single_value(SETTINGS_DOCTYPE, "connectors_enabled"))
	except Exception:
		return False
