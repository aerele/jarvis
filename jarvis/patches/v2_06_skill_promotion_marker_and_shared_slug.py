"""Two additive backfills for the Codex round-3 skill-promotion hardening.

R3-SP-4 (snapshot presence marker): the ``snapshotted`` Check now records that a
promotion request captured a FULL content snapshot (all three fields) at request
time; approval refuses a request whose marker is absent, exactly like a
missing-instructions one. A schema sync adds the column defaulting to 0, so an
in-flight Pending request filed before this change — which already carries all
three snapshots but has no marker — would be spuriously refused at approval. Stamp
the marker on those complete Pending rows so no legitimate in-flight request is
stranded. Rows that are genuinely partial (any snapshot NULL) are left unmarked, so
they still refuse at approval and must be resubmitted.

R3-SP-1 (global shared-slug uniqueness): at most one SHARED (Role/Org) skill may
carry a given ``skill_name`` — the container writes ONE ``custom-<slug>`` dir, so a
second shared row with the same slug collides on it and corrupts the push budget.
The controller now enforces this on every shared create/rename, but any collision
that predates the guard must be resolved or ALTER-equivalent writes keep tripping.
Expected count is ~0 (this is a new feature), so rather than delete data we keep the
oldest shared row of each colliding slug and DISABLE the newer duplicates (reversible,
non-destructive), logging the action loudly for an operator to reconcile.
"""

import frappe

SKILL = "Jarvis Custom Skill"
PROMO = "Jarvis Skill Promotion Request"


def _backfill_snapshot_marker() -> None:
	if not frappe.db.table_exists(PROMO):
		return
	complete = frappe.get_all(
		PROMO,
		filters={
			"status": "Pending",
			"snapshotted": 0,
			"instructions_snapshot": ["is", "set"],
			"description_snapshot": ["is", "set"],
			"user_invocable_snapshot": ["is", "set"],
		},
		pluck="name",
	)
	for name in complete:
		frappe.db.set_value(PROMO, name, "snapshotted", 1, update_modified=False)
	if complete:
		frappe.db.commit()


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
				"Reconcile manually — rename or delete the extras, then re-enable if intended."
			),
		)
	if groups:
		frappe.db.commit()


def execute():
	_backfill_snapshot_marker()
	_resolve_shared_slug_collisions()
