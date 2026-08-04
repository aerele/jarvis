"""Re-push learned skills once, to evict role-restricted bodies already sitting in
live tenant containers (issue #479).

Before this deploy, ``build_learned_push_payload`` wrote EVERY managed
``learned-<domain>`` row into the container's ``learned_skills`` dir, including the
role-restricted ones. The code fix stops NEW pushes from doing that; it cannot
remove what is already on disk, and a body on disk is readable by every user of
that tenant's single, role-blind container (openclaw's entrypoint copies it into
``workspace/skills/`` and ``bash cat`` needs no permission). Shipping the filter
without this patch closes the tap and leaves the puddle.

The removal mechanism already exists and needs no fleet change: ``PUT
/v1/containers/{name}/learned-skills`` is a FULL reconcile. It rmtree's every
learned dir absent from the pushed set, drops the slug from the agent skill
allowlist, and restarts the container, which is the load-bearing step - the
restart is what rebuilds ``workspace/skills/`` from the (now smaller) source dir.
An empty payload is a valid "remove all learned skills" reconcile. So exactly one
push per affected tenant finishes the job.

Deliberately narrow, because the push costs that tenant a container restart:

* ``learned_skills_sync_status`` empty means this bench has never pushed through
  the learned namespace, so nothing of ours is in its container. Skip.
* no managed row carrying ``allowed_roles`` means nothing restricted was ever
  pushed. Skip.

What is left is precisely the exposed set, so the restart wave is bounded by the
tenants that actually have a body to evict rather than by the fleet size. The
alternative (wait for each tenant's next reviewer Apply) is unbounded: a tenant
that never applies keeps the exposure forever, which is not a close-out for a
security issue.

``JarvisSettings._resync_learned_skills_after_restart`` is NOT this path. It fires
only when an LLM-config change restarted the container; a deploy never triggers
it. It is a backstop, not a migration.

The "patches never run on fresh installs" trap (``install_app`` marks patches done
without running them) does not bite: a fresh install has no container to clean.
"""

import frappe

SKILL = "Jarvis Custom Skill"
ALLOWED_ROLE = "Jarvis Custom Skill Allowed Role"
SETTINGS = "Jarvis Settings"
MANAGED_OWNER = "Administrator"


def execute():
	if not frappe.db.table_exists(SKILL) or not frappe.db.table_exists(ALLOWED_ROLE):
		return
	# Never pushed through the learned namespace -> no learned dir in the container.
	if not (frappe.db.get_single_value(SETTINGS, "learned_skills_sync_status") or "").strip():
		return

	managed = frappe.get_all(
		SKILL,
		filters={"managed_by_learning": 1, "enabled": 1, "owner": MANAGED_OWNER},
		pluck="name",
	)
	if not managed:
		return
	restricted = frappe.get_all(
		ALLOWED_ROLE,
		filters={"parenttype": SKILL, "parent": ("in", managed)},
		fields=["parent"],
		limit=1,
	)
	if not restricted:
		return

	# Best-effort: a tenant whose push cannot be enqueued must not fail the whole
	# migrate. The row stays as it was and the next Apply re-pushes it correctly,
	# with the fix already in place.
	try:
		from jarvis.chat.learned_skills_api import enqueue_learned_skills_push

		enqueue_learned_skills_push()
	except Exception:
		frappe.log_error(
			title="Jarvis: learned-skills role-gating re-push failed to enqueue",
			message=frappe.get_traceback(),
		)
