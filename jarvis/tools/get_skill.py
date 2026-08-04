"""Fetch one saved skill's full instructions (Jarvis Custom Skill row).

Accepts the bare authored slug (``invoicing``), the container wire slug
(``custom-invoicing``) or a learned-namespace slug (``learned-selling`` —
stored verbatim as the row's skill_name). ``skill_name`` is unique per OWNER,
not globally, so several rows may match; the caller's own row wins.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.find_skills import _require_system_user, _visible

SKILL = "Jarvis Custom Skill"
_CUSTOM_PREFIX = "custom-"
_LEARNED_PREFIX = "learned-"
# One Error Log row per slug per 10 minutes (the throttle idiom of
# _plugin_auth._should_audit_unsigned_reject): a tenant whose clause and whose
# fetch check disagree would otherwise fill the log from every turn.
_MISS_AUDIT_TTL_S = 600
_MISS_AUDIT_PREFIX = "jarvis:learned_get_skill_miss:"


def get_skill(skill_name: str) -> dict:
	"""Return one skill the calling user may use: ``{skill_name, description,
	instructions, scope, enabled, user_invocable}``.

	This is the ONLY delivery path for a role-restricted body, custom or learned
	(issues #477 / #479): those are never written into the shared, role-blind
	container, so the role check here is enforcement on the bytes rather than
	advice on a context line. It re-derives the caller's roles at fetch time and
	runs them through ``user_can_use_skill``, the same predicate that decided
	whether to name the slug in the first place."""
	user = _require_system_user()
	raw = (skill_name or "").strip().lower()
	if not raw:
		raise InvalidArgumentError("skill_name is required")

	candidates = {raw}
	if raw.startswith(_CUSTOM_PREFIX):
		# The wire slug is custom-<bare>; rows store the bare slug (a stored
		# skill_name can never itself start with the reserved prefix).
		candidates.add(raw[len(_CUSTOM_PREFIX) :])

	rows = frappe.get_all(
		SKILL,
		# enabled=1 mirrors find_skills: a disabled row is not discoverable, so it
		# must not be fetchable either. Load-bearing since #479 made this tool the
		# only way a role-restricted body reaches the agent.
		filters={"skill_name": ["in", sorted(candidates)], "enabled": 1},
		fields=[
			"name",
			"owner",
			"skill_name",
			"description",
			"instructions",
			# target_role is REQUIRED for user_can_use_skill to admit a Role-scope
			# skill's role-holder (security review PART 2 TASK 13).
			"scope",
			"target_role",
			"enabled",
			"user_invocable",
		],
	)
	if not rows:
		_audit_learned_miss(raw, user, "unknown")
		raise InvalidArgumentError(f"unknown skill: {skill_name}")

	user_roles = frappe.get_roles(user)
	usable = [r for r in rows if _visible(r, user, user_roles)]
	if not usable:
		_audit_learned_miss(raw, user, "denied")
		raise PermissionDeniedError(f"no access to skill: {skill_name}")

	row = next((r for r in usable if r.owner == user), usable[0])
	return {
		"skill_name": row.skill_name,
		"description": row.description or "",
		"instructions": row.instructions or "",
		"scope": row.scope or "Org",
		"enabled": int(row.enabled or 0),
		"user_invocable": int(row.user_invocable or 0),
	}


def _audit_learned_miss(slug: str, user: str, reason: str) -> None:
	"""Log one throttled Error Log row when a ``learned-`` fetch misses.

	``learned_skill_clause`` names a slug only after ``user_can_use_skill``
	admitted this user, so a deny here means discovery and retrieval disagreed:
	roles changed mid-turn, or one of the two read a stale ``frappe.get_roles``
	cache. An ``unknown`` means the managed row was deleted between the two (a
	concurrent Apply drops emptied domains). Both are invisible otherwise, and a
	steady stream of either is the ONLY signal that the two role checks #479
	deliberately made agree have drifted apart again.

	Narrow on purpose: only ``learned-`` slugs, where the clause is the sole
	discovery channel. A missed ``custom-`` slug is usually just the model
	guessing a name. Never raises - an audit row must not fail a tool call."""
	try:
		if not (slug or "").startswith(_LEARNED_PREFIX):
			return
		cache = frappe.cache()
		# Site-scoped by hand: the raw redis ``set`` is NOT namespaced the way
		# ``set_value`` is, and every tenant bench shares the same six learned
		# slugs, so an unqualified key would let one site's throttle silence
		# another's audit row.
		key = f"{_MISS_AUDIT_PREFIX}{frappe.local.site}:{slug}"
		if not cache.set(key, b"1", nx=True, ex=_MISS_AUDIT_TTL_S):
			return
		frappe.log_error(
			title="Jarvis: learned skill fetch missed",
			message=(
				f"jarvis__get_skill({slug!r}) {reason} for user {user!r}. The learned "
				"context clause and the fetch-time role check disagreed, or the managed "
				"row was deleted between them; the agent answered without this domain's "
				"learned defaults."
			),
		)
	except Exception:
		pass
