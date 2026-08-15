"""A13: backfill ``run_as_user`` on existing Jarvis Agent Installation rows.

Adding ``reqd:1 run_as_user`` to a POPULATED ``Jarvis Agent Installation`` table
is a trap: schema-sync adds the column nullable (so ``migrate`` itself passes),
but the moment any code path does ``doc.save()`` on a legacy row —
``set_enabled`` / ``set_schedule`` / ``set_config`` all do — Frappe raises
``MandatoryError`` (the user cannot even DISABLE the agent), and the scheduler
would mint a session with ``user=None``.

Behaviour-preserving fix: stamp ``run_as_user = owner`` on every existing row
BEFORE enforcement matters. Runs today already execute as the owner (the owner
passed install-time RBAC), so owner IS the correct run identity to grandfather.

This is a RAW ``db.set_value`` (never ``doc.save()``) so it does NOT trip the new
controller ``validate()`` (which would MandatoryError / re-run the escalation
guard against rows the migrate has not finished touching). Rows whose owner no
longer passes ``_valid_owner`` (disabled / Administrator / Guest) are LOGGED and
SKIPPED — never abort the migrate; they simply stay un-backfilled and fail closed
at run time (the scheduler / run_agent_now identity guard refuses an invalid
run-as user).
"""

import frappe

INSTALLATION = "Jarvis Agent Installation"


def execute():
	# The column exists after post_model_sync; guard anyway so a partial deploy
	# state can never 500 the patch.
	if not frappe.db.has_column(INSTALLATION, "run_as_user"):
		return

	from jarvis.chat.agent_scheduler import _valid_owner

	rows = frappe.get_all(INSTALLATION, fields=["name", "owner", "run_as_user"])
	skipped = []
	for r in rows:
		if (r.run_as_user or "").strip():
			continue  # already set (e.g. a fresh install between sync + patch)
		if not _valid_owner(r.owner):
			skipped.append(f"{r.name} (owner={r.owner})")
			continue
		frappe.db.set_value(INSTALLATION, r.name, "run_as_user", r.owner, update_modified=False)

	if skipped:
		frappe.log_error(
			title="jarvis A13 backfill: run_as_user skipped (invalid owner)",
			message=(
				"These Jarvis Agent Installation rows have an owner that fails the "
				"fail-closed run-as guard (disabled / Administrator / Guest), so "
				"run_as_user was NOT backfilled; they fail closed at run time until "
				"an admin re-maps them via set_run_as_user:\n  " + "\n  ".join(skipped)
			),
		)
	frappe.db.commit()
