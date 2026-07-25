"""Additive backfill for the Codex round-3/4 skill-promotion hardening.

R3-SP-1 (shared-slug uniqueness): at most one SHARED (Role/Org) skill may carry a
given ``skill_name`` - the container writes ONE ``custom-<slug>`` dir, so a second
shared row with the same slug collides on it and corrupts the push budget. Expected
count is ~0 (this is a new feature), so rather than delete data
``_resolve_shared_slug_collisions`` keeps the OLDEST shared row of each colliding slug
and DISABLES the newer duplicates (reversible, non-destructive), logging the action
loudly for an operator to reconcile.

SR4-1 (snapshot presence marker): the ``snapshotted`` Check records that a promotion
request captured a FULL content snapshot (all three fields) at request time; approval
refuses a request whose marker is absent. The marker is DELIBERATELY NOT backfilled
from the stored snapshot columns: the pre-marker controller normalized an OMITTED
user-invocable value to 0, so a non-NULL ``user_invocable_snapshot`` does NOT prove
the field was ever captured - stamping the marker from it would bless the exact
ambiguity the marker exists to reject (publishing "invocable off" as if a reviewer
had chosen it). No independent provenance signal proves all three fields were
explicitly captured, so pre-marker Pending requests are left UNMARKED and must be
resubmitted (a resubmission mints a fresh full snapshot + marker). This is safe: the
feature is new, so ~0 in-flight requests exist, and an unmarked request is refused at
approval rather than published wrong.
"""

import frappe

SKILL = "Jarvis Custom Skill"


def _resolve_shared_slug_collisions() -> None:
	if not frappe.db.table_exists(SKILL):
		return
	# Shared = scope in (Role, Org) OR legacy NULL/empty scope (== Org). ifnull folds
	# the legacy rows in so a NULL-scope duplicate is not missed.
	groups = frappe.db.sql(
		"""
		SELECT skill_name, COUNT(*) c
		FROM `tabJarvis Custom Skill`
		WHERE ifnull(scope, '') IN ('Role', 'Org', '')
		GROUP BY skill_name
		HAVING c > 1
		""",
		as_dict=True,
	)
	for g in groups:
		rows = frappe.get_all(
			SKILL,
			filters={"skill_name": g.skill_name, "scope": ("in", ("Role", "Org", ""))},
			fields=["name", "enabled", "owner", "scope"],
			order_by="creation asc",
		)
		if len(rows) < 2:
			continue
		keep, extras = rows[0], rows[1:]
		disabled = []
		for extra in extras:
			if extra.enabled:
				frappe.db.set_value(SKILL, extra.name, "enabled", 0, update_modified=False)
			disabled.append(extra.name)
		frappe.log_error(
			title="Jarvis: duplicate shared skill slug resolved",
			message=(
				f"Shared-slug collision on '{g.skill_name}' (R3-SP-1): kept {keep.name} "
				f"({keep.scope}, owner {keep.owner}); disabled duplicate(s): {', '.join(disabled)}. "
				"Reconcile manually - rename or delete the extras, then re-enable if intended."
			),
		)
	if groups:
		frappe.db.commit()


def execute():
	_resolve_shared_slug_collisions()
