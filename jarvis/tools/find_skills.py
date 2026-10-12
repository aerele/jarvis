"""Search the customer's saved skills (Jarvis Custom Skill rows).

The pushed container catalog only carries Org-scope skills; Personal-scope
rows never leave the bench. This tool is how the agent discovers BOTH
mid-turn: visibility mirrors the SPA (owner / shared_with / allowed_roles via
``user_can_use_skill``), with one extra rule — a Personal row is strictly
owner-only, regardless of shares or roles.

Candidates are filtered in SQL by the registered ``skill_query_conditions``
hook and paged until the limit is met: a run of matches the caller cannot see
must never crowd a usable skill out of the result (audit finding F06).
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


def _maybe_prefetch_children(rows: list, user: str, user_roles: list[str]) -> None:
	"""Batch-load child tables (shared_with/allowed_roles) for the visibility
	check, so ``_visible`` doesn't fire a per-row fallback query per candidate.
	Skipped when it wouldn't help: a single candidate (no batching win), or an
	Administrator / System Manager caller (``user_can_use_skill`` short-circuits
	before any child read)."""
	if len(rows) <= 1:
		return
	if user == "Administrator" or "System Manager" in user_roles:
		return
	# Every row the caller OWNS short-circuits user_can_use_skill before any child
	# read, so if the caller owns them all there is nothing to batch (those rows
	# cost 0 child queries on the per-row path — batching would only add 2). Fire
	# only when at least one row belongs to someone else.
	if not any(r.get("owner") != user for r in rows):
		return
	from jarvis.jarvis.doctype.jarvis_custom_skill.jarvis_custom_skill import (
		prefetch_child_values,
	)

	prefetch_child_values(rows)


def _candidate_pages(q: str, user: str):
	"""Yield pages of enabled skills matching ``q`` that ``user`` may see, in
	name order, until the matches run out.

	The visibility filter is the registered ``skill_query_conditions`` hook, the
	SQL form of ``user_can_use_skill``. Not ``frappe.get_list``: that also demands
	DocType read, which a Jarvis Admin or a plugin-delegated caller can lack, and
	on Frappe 15 it re-escapes the LIKE pattern."""
	from pypika.terms import LiteralValue

	from jarvis.chat.skill_permissions import skill_query_conditions

	skill = frappe.qb.DocType(SKILL)
	pattern = f"%{_escape_like(q)}%"
	query = (
		frappe.qb.from_(skill)
		# target_role is REQUIRED for user_can_use_skill to admit a Role-scope
		# skill's role-holder (security review PART 2 TASK 13); omitting it made
		# the whole User->Role promotion tier dead through this tool.
		.select(
			skill.name,
			skill.owner,
			skill.skill_name,
			skill.description,
			skill.scope,
			skill.target_role,
			skill.managed_by_learning,
		)
		.where(skill.enabled == 1)
		.where(skill.skill_name.like(pattern) | skill.description.like(pattern))
		.orderby(skill.skill_name)
		.orderby(skill.name)
		.limit(_CANDIDATE_ROWS)
	)
	visibility = skill_query_conditions(user)
	if visibility:
		# As built: the hook quotes every value with frappe.db.escape, which already
		# doubles "%" for the percent-formatting frappe.db.sql applies.
		query = query.where(LiteralValue(visibility))
	start = 0
	while True:
		rows = query.offset(start).run(as_dict=True)
		if rows:
			yield rows
		if len(rows) < _CANDIDATE_ROWS:
			return
		start += _CANDIDATE_ROWS


def find_skills(query: str, limit: int = 10) -> dict:
	"""Search enabled skills by name/description; returns only skills the
	calling user may use. ``{"skills": [{skill_name, scope, description,
	managed}], "count"}``, capped at ``limit``.

	In an armed macro's chat (``ARMED_SKILL_OWNER_FLAG``, set by ``api._run_tool``)
	only skills the macro's owner controls are listed: another user's description is
	their text, and fetching that skill would disarm the run anyway."""
	from jarvis.chat.skill_permissions import controlled_by
	from jarvis.permissions import ARMED_SKILL_OWNER_FLAG

	user = _require_system_user()
	armed_owner = frappe.flags.get(ARMED_SKILL_OWNER_FLAG)
	q = (query or "").strip()
	if len(q) < _MIN_QUERY_LEN:
		raise InvalidArgumentError(f"query must be at least {_MIN_QUERY_LEN} characters")
	try:
		limit = int(limit or 10)
	except (TypeError, ValueError):
		raise InvalidArgumentError("limit must be an integer")
	limit = max(1, min(limit, _MAX_LIMIT))

	user_roles = frappe.get_roles(user)
	skills = []
	for rows in _candidate_pages(q, user):
		_maybe_prefetch_children(rows, user, user_roles)
		for row in rows:
			# The SQL hook already scoped the page; the shared Python rule stays as
			# the authority, and the armed-macro filter has no SQL form.
			if not _visible(row, user, user_roles):
				continue
			if armed_owner and not controlled_by(row, armed_owner):
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
				return {"skills": skills, "count": len(skills)}
	return {"skills": skills, "count": len(skills)}
