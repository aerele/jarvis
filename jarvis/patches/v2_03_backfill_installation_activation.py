"""PP-4 / PP-6 / PP-3: backfill activation + reviewer + installable on existing
Jarvis Agent Installation rows.

Three new fields need grandfathering on the POPULATED installation table:

  * ``reviewer`` (NEW ``reqd`` Link) — same ``MandatoryError`` trap as the A13
    ``run_as_user`` backfill: a legacy row with a NULL reqd field raises the
    moment any code ``doc.save()``s it. Stamp ``reviewer = run_as_user`` (the
    identity that already sees the agent's output; fall back to ``owner``).

  * ``activation_state`` (NEW Select, default ``shadow``). DELIBERATE DECISION:
    existing installs are put into SHADOW, not grandfathered to ``live``. The
    contract gives no explicit migration note here, and the codebase convention
    is behaviour-preserving — but PP-4 is a BEFORE-LAUNCH gate whose whole point
    is that nothing surfaces a customer-facing attestation without a named
    reviewer's explicit sign-off, and PP-6 sets the initial live-module ceiling
    to 1. Grandfathering N pre-existing installs to ``live`` would (a) create a
    live population that never went through reviewer promotion, defeating the
    PP-4 gate on day one, and (b) put every customer instantly over the PP-6
    ceiling with no admin justification. Shadow-first is consistent with both.
    Consequence: existing installs stop surfacing to the general owner until a
    reviewer promotes them — an intended behaviour change under the launch gate.
    (Frappe's ``ADD COLUMN ... DEFAULT 'shadow'`` already stamps legacy rows
    ``shadow``; this patch only belt-and-braces any NULL/empty value.)

  * ``installable`` (NEW Check, default 1) — the default applies on insert only;
    stamp existing rows to 1 (min_apps was satisfied at their install time).

Raw ``db.set_value`` (never ``doc.save()``) so the reqd/controller guards are
not tripped mid-migrate. Idempotent: only NULL/empty fields are filled; a re-run
is a no-op. Rows whose ``run_as_user`` and ``owner`` are both empty are logged +
skipped (they stay un-backfilled and fail closed at run time).
"""

import frappe

INSTALLATION = "Jarvis Agent Installation"


def execute():
	have_reviewer = frappe.db.has_column(INSTALLATION, "reviewer")
	have_state = frappe.db.has_column(INSTALLATION, "activation_state")
	have_installable = frappe.db.has_column(INSTALLATION, "installable")
	if not (have_reviewer or have_state or have_installable):
		return

	rows = frappe.get_all(
		INSTALLATION,
		fields=["name", "owner", "run_as_user", "reviewer", "activation_state", "installable"],
	)
	skipped = []
	for r in rows:
		# activation_state: shadow-first for legacy rows (see module docstring).
		if have_state and not (r.activation_state or "").strip():
			frappe.db.set_value(INSTALLATION, r.name, "activation_state", "shadow", update_modified=False)

		# installable: legacy rows passed their min_apps gate at install time.
		if have_installable and r.installable in (None, ""):
			frappe.db.set_value(INSTALLATION, r.name, "installable", 1, update_modified=False)

		# reviewer (reqd): stamp run_as_user, then owner.
		if have_reviewer and not (r.reviewer or "").strip():
			reviewer = (r.run_as_user or "").strip() or (r.owner or "").strip()
			if not reviewer:
				skipped.append(r.name)
				continue
			frappe.db.set_value(INSTALLATION, r.name, "reviewer", reviewer, update_modified=False)

	frappe.db.commit()

	if skipped:
		frappe.log_error(
			title="jarvis PP-4 backfill: installation reviewer skipped (no run_as_user/owner)",
			message=(
				"These Jarvis Agent Installation rows had neither run_as_user nor owner, "
				"so reviewer was NOT backfilled; they fail closed until an admin sets a "
				"reviewer:\n  " + "\n  ".join(skipped)
			),
		)
