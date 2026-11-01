"""Configuration boundary for the SLA-breach and ticket-ageing auditor.

The delegate receives its team boundary from the bench-owned launch scope.  It
must never infer a team, and a malformed or unreadable saved boundary must stop
the launch before a run row or a fleet request exists.
"""

import frappe
from frappe import _

AGENT = "sla-breach-ageing-auditor"
RECORD_SCOPE_DOCTYPE = "HD Team"
_RECORD_SCOPE_KEYS = frozenset(("doctype", "name"))


def configuration_issues(raw) -> dict[str, str]:
	"""Return field-level setup guidance for the selected Helpdesk team.

	The detail page checks the configuring user's visibility. The launch path
	checks the run-as user's visibility. Checking the actual document at both
	points keeps the hint and launch guard honest without exposing inaccessible
	team names.
	"""
	try:
		config = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (TypeError, ValueError):
		return {"sla_team": _("Select one readable HD Team record.")}
	if not isinstance(config, dict):
		return {"sla_team": _("Select one readable HD Team record.")}
	record = config.get("record_scope")
	if (
		not isinstance(record, dict)
		or set(record) != _RECORD_SCOPE_KEYS
		or record.get("doctype") != RECORD_SCOPE_DOCTYPE
		or not isinstance(record.get("name"), str)
		or not record["name"].strip()
		or record["name"] != record["name"].strip()
	):
		return {"sla_team": _("Select one readable HD Team record.")}
	try:
		frappe.get_doc(RECORD_SCOPE_DOCTYPE, record["name"]).check_permission("read")
	except (frappe.DoesNotExistError, frappe.PermissionError):
		return {"sla_team": _("Select one readable HD Team record.")}
	return {}


def assert_configuration(installation) -> None:
	"""Fail before launch when the SLA team's saved record boundary is invalid."""
	issues = configuration_issues(installation.get("config") or "{}")
	if issues:
		frappe.throw(list(issues.values()), title=_("Complete SLA configuration"), as_list=True)
