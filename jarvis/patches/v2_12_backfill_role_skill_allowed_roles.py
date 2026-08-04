"""Backfill ``allowed_roles`` on Role-scope skills promoted before issue #478 was fixed.

``_materialize_promotion`` used to write ``target_role`` and never ``allowed_roles``.
``target_role`` alone is honoured by the per-doc read paths (``user_can_use_skill``, and
through it ``jarvis__find_skills`` / ``jarvis__get_skill``), but every path that answers
"which skills may this user invoke" matches on the ``Jarvis Custom Skill Allowed Role``
CHILD rows: ``custom_skills._role_scoped_invocable_names``, and through it the ``/slug``
context clause. So a skill promoted to Role scope before the fix stays undiscoverable by
``/slug`` forever unless its child row is planted here. The code fix alone only helps
promotions made after deploy.

Deliberately narrow: it touches ONLY rows whose ``allowed_roles`` is completely EMPTY,
which is the exact shape the old promotion left behind. A row that already carries child
rows is left alone, because a migration should not delete audience data it cannot prove is
stale; the controller mirror in ``JarvisCustomSkill._validate_scope`` normalizes such a row
to its ``target_role`` on the next save.

Nothing is widened. The row added always names the skill's OWN ``target_role``, whose
holders ``user_can_use_skill`` already admitted, so nobody gains access they did not have.

Written with direct child-table inserts rather than ``doc.save()`` on purpose. Re-saving a
legacy parent would re-run the full controller validate chain (owner cap, shared-slug
uniqueness, scope guards) against rows that predate those rules, and a throw there would
block the whole migrate for a backfill that has nothing to do with them.
"""

import frappe

SKILL = "Jarvis Custom Skill"
ALLOWED_ROLE = "Jarvis Custom Skill Allowed Role"


def execute():
	if not frappe.db.table_exists(SKILL) or not frappe.db.table_exists(ALLOWED_ROLE):
		return
	rows = frappe.get_all(
		SKILL,
		filters={"scope": "Role", "target_role": ("is", "set")},
		fields=["name", "target_role"],
	)
	if not rows:
		return
	# One query for every existing child row across the whole candidate set, so the loop
	# below is a dict lookup rather than a per-skill SELECT.
	existing: dict[str, set[str]] = {r.name: set() for r in rows}
	for child in frappe.get_all(
		ALLOWED_ROLE,
		filters={"parenttype": SKILL, "parent": ("in", [r.name for r in rows])},
		fields=["parent", "role"],
	):
		if child.role:
			existing.setdefault(child.parent, set()).add(child.role)

	added = 0
	for r in rows:
		target_role = (r.target_role or "").strip()
		# Empty-only: a row that already has ANY allowed_roles was not left broken by the
		# old promotion path, so leave its audience exactly as an operator set it.
		if not target_role or existing.get(r.name):
			continue
		# A target_role pointing at a since-deleted Role would fail the child row's
		# Link validation and break the migrate; skip it and leave the skill as it was.
		if not frappe.db.exists("Role", target_role):
			continue
		frappe.get_doc(
			{
				"doctype": ALLOWED_ROLE,
				"parenttype": SKILL,
				"parentfield": "allowed_roles",
				"parent": r.name,
				"role": target_role,
			}
		).insert(ignore_permissions=True)
		added += 1
	if added:
		frappe.db.commit()
