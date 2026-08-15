"""Search the customer's saved skills (Jarvis Custom Skill rows).

The pushed container catalog only carries Org-scope skills; Personal-scope
rows never leave the bench. This tool is how the agent discovers BOTH
mid-turn: visibility mirrors the SPA (owner / shared_with / allowed_roles via
``user_can_use_skill``), with one extra rule — a Personal row is strictly
owner-only, regardless of shares or roles.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError

SKILL = "Jarvis Custom Skill"
_CANDIDATE_ROWS = 50
_MAX_LIMIT = 50
_MIN_QUERY_LEN = 2


def _require_system_user() -> str:
	"""Skill tools are a Desk-only surface: reject Guest and portal (Website)
	users. Shared by get_skill / create_custom_skill."""
	user = frappe.session.user
	if not user or user == "Guest":
		raise PermissionDeniedError("authentication required")
	if frappe.db.get_value("User", user, "user_type") != "System User":
		raise PermissionDeniedError("skill tools require a System User")
	return user


def _escape_like(s: str) -> str:
	"""Escape LIKE wildcards in user search input (``\\`` is the default escape)."""
	return (s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _visible(row, user: str, user_roles: list[str]) -> bool:
	from jarvis.jarvis.doctype.jarvis_custom_skill.jarvis_custom_skill import (
		user_can_use_skill,
	)

	# Single visibility rule shared with the ORM hook + SPA (security review
	# PART 2 TASK 13): owner OR shared OR scope=Org (unless role-narrowed) OR
	# scope=Role role-match. User-scope rows are owner-only inside the helper, so
	# the old "Personal is owner-only" special case is subsumed.
	return user_can_use_skill(row, user, user_roles)


def find_skills(query: str, limit: int = 10) -> dict:
	"""Search enabled skills by name/description; returns only skills the
	calling user may use. ``{"skills": [{skill_name, scope, description,
	managed}], "count"}``, capped at ``limit``."""
	user = _require_system_user()
	q = (query or "").strip()
	if len(q) < _MIN_QUERY_LEN:
		raise InvalidArgumentError(f"query must be at least {_MIN_QUERY_LEN} characters")
	try:
		limit = int(limit or 10)
	except (TypeError, ValueError):
		raise InvalidArgumentError("limit must be an integer")
	limit = max(1, min(limit, _MAX_LIMIT))

	rows = frappe.db.sql(
		# target_role is REQUIRED for user_can_use_skill to admit a Role-scope
		# skill's role-holder (security review PART 2 TASK 13); omitting it made
		# the whole User->Role promotion tier dead through this tool.
		"""SELECT name, owner, skill_name, description, scope, target_role,
               managed_by_learning
        FROM `tabJarvis Custom Skill`
        WHERE enabled = 1 AND (skill_name LIKE %(q)s OR description LIKE %(q)s)
        ORDER BY skill_name ASC, name ASC
        LIMIT %(rows)s""",
		{"q": f"%{_escape_like(q)}%", "rows": _CANDIDATE_ROWS},
		as_dict=True,
	)
	user_roles = frappe.get_roles(user)
	skills = []
	for row in rows:
		if not _visible(row, user, user_roles):
			continue
		skills.append(
			{
				"skill_name": row.skill_name,
				"scope": row.scope or "Org",
				"description": row.description or "",
				"managed": bool(row.managed_by_learning),
			}
		)
		if len(skills) >= limit:
			break
	return {"skills": skills, "count": len(skills)}
