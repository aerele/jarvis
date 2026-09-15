"""What the CURRENT chat user can do in this instance: their roles plus any
User Permission restrictions that narrow which records they see.

The persona answers "who am I" / "what roles do I have" straight from the
``[Context:]`` bracket (turn_handler folds the roles in on an identity/permission
turn) — no tool needed for that. This tool is the bounded, one-call answer for
DEEPER "what am I actually restricted to" questions (record-level User
Permissions), so the agent never has to improvise an open-ended walk over the
permission meta (the get_list/get_doc grind that ran into the turn wall-clock).

Read-only and self-scoped: it reports the calling user's OWN access only
(``frappe.session.user``), never another user's — so it can't be used to probe
someone else's privileges.
"""

from __future__ import annotations

import frappe

# Base roles every user carries — noise, never part of "my permissions".
_BASE_ROLES = ("All", "Guest")
# Cap the restriction list like get_list's ROW_GUARD: a summary, not a dump. A
# user with more than this many User Permission rows gets the first page + a flag.
_MAX_RESTRICTIONS = 200


def get_my_access() -> dict:
	"""Return the calling user's roles + record-level restrictions.

	Shape::

	    {
	      "user": "<id>", "full_name": "<name>",
	      "roles": [...meaningful roles, sorted...], "role_count": N,
	      "is_system_manager": bool,      # System Manager / Administrator
	      "restrictions": [{"doctype", "value", "applies_to"}...],
	      "restriction_count": N, "restrictions_truncated": bool,
	    }

	``restrictions`` are the user's User Permission rows — each limits the records
	of ``doctype`` they can see to ``value`` (``applies_to`` narrows it to one
	linked doctype when set). An empty list means no record-level restriction:
	access is governed by roles alone.
	"""
	user = frappe.session.user
	full_name = frappe.db.get_value("User", user, "full_name") or ""
	all_roles = frappe.get_roles(user)
	roles = sorted(r for r in all_roles if r not in _BASE_ROLES)
	is_system_manager = "System Manager" in all_roles or user == "Administrator"

	# The user's OWN User Permission rows only — hard-filtered to session user, so
	# there is no cross-user leakage even though get_all skips the doctype's own
	# read gate (which a plain user lacks on User Permission). +1 to detect overflow.
	rows = frappe.get_all(
		"User Permission",
		filters={"user": user},
		fields=["allow", "for_value", "applicable_for"],
		order_by="allow asc, for_value asc",
		limit_page_length=_MAX_RESTRICTIONS + 1,
	)
	truncated = len(rows) > _MAX_RESTRICTIONS
	restrictions = [
		{"doctype": r.allow, "value": r.for_value, "applies_to": r.applicable_for or None}
		for r in rows[:_MAX_RESTRICTIONS]
	]

	return {
		"user": user,
		"full_name": full_name,
		"roles": roles,
		"role_count": len(roles),
		"is_system_manager": is_system_manager,
		"restrictions": restrictions,
		"restriction_count": len(restrictions),
		"restrictions_truncated": truncated,
	}
