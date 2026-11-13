"""Who may read ``Jarvis Trigger Activity`` rows at the ORM.

System Managers see everything. Everyone else sees only their OWN rows with
``action_type = 'Intake'`` (the uploader owns the row because it is inserted in
their request). Script/LLM activity rows are excluded on purpose: their
``detail`` is managers-only and their owner is the event user, so an owner-only
rule alone would leak it.

The DocType JSON grants role ``All`` read with ``if_owner`` so Frappe consults
these hooks for non-managers at all; the hooks then narrow that grant. Hooks can
only DENY, so every allow path returns ``True`` to defer to the role check.
"""

import frappe

ACTIVITY = "Jarvis Trigger Activity"
INTAKE = "Intake"
_READ_PTYPES = ("read", "select", "export", "report")


def _is_sm(user: str) -> bool:
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def activity_query_conditions(user: str | None = None) -> str:
	"""hooks.permission_query_conditions: empty for System Managers, otherwise
	own Intake rows only."""
	user = user or frappe.session.user
	if _is_sm(user):
		return ""
	table = f"`tab{ACTIVITY}`"
	return (
		f"({table}.`action_type` = {frappe.db.escape(INTAKE)} and {table}.`owner` = {frappe.db.escape(user)})"
	)


def has_activity_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool:
	"""hooks.has_permission: non-managers may only read their own Intake rows."""
	user = user or frappe.session.user
	if _is_sm(user):
		return True
	if ptype not in _READ_PTYPES:
		return False
	return doc.get("action_type") == INTAKE and doc.get("owner") == user
