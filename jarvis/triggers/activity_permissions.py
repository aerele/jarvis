"""Who may read ``Jarvis Trigger Activity`` rows at the ORM.

System Managers and Jarvis Admins (the people who manage triggers, same rule as
the SPA feed) see everything. Everyone else sees only their OWN rows with
``action_type = 'Intake'`` (the uploader owns the row because it is inserted in
their request). Script/LLM activity rows are excluded on purpose: their
``detail`` is managers-only and their owner is the event user, so an owner-only
rule alone would leak it.

The DocType JSON grants role ``Desk User`` read with ``if_owner`` so Frappe consults
these hooks for non-managers at all; the hooks then narrow that grant. Hooks can
only DENY, so every allow path returns ``True`` to defer to the role check.
"""

import frappe

from jarvis.permissions import has_jarvis_admin_access

ACTIVITY = "Jarvis Trigger Activity"
INTAKE = "Intake"
_READ_PTYPES = ("read", "select", "export", "report")


def _can_see_all(user: str) -> bool:
	# One definition with triggers_api._can_manage, so the SPA feed and the ORM agree.
	return has_jarvis_admin_access(user)


def activity_query_conditions(user: str | None = None) -> str:
	"""hooks.permission_query_conditions: empty for managers, otherwise
	own Intake rows only."""
	user = user or frappe.session.user
	if _can_see_all(user):
		return ""
	table = f"`tab{ACTIVITY}`"
	return (
		f"({table}.`action_type` = {frappe.db.escape(INTAKE)} and {table}.`owner` = {frappe.db.escape(user)})"
	)


def has_activity_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool:
	"""hooks.has_permission: non-managers may only read their own Intake rows."""
	user = user or frappe.session.user
	if _can_see_all(user):
		return True
	if ptype not in _READ_PTYPES:
		return False
	return doc.get("action_type") == INTAKE and doc.get("owner") == user
