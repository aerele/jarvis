"""#482: snapshot the reviewed text of already-approved learned patterns.

The compiler now ships ``Jarvis Learned Pattern.approved_draft`` instead of the
live ``skill_draft`` (nightly re-detection rewrites the live field in place, so
approved wording could change and be pushed org-wide unreviewed). Rows approved
before that field existed have an empty snapshot, and would keep compiling from
the live draft - exactly the behaviour being fixed. Stamp the current draft as
the snapshot so those rows are protected from the NEXT re-detection onward.

This is the best available baseline, not a perfect one: if a nightly run already
rewrote such a row, the current draft IS the rewritten text and the original
wording is unrecoverable from the row (Jarvis Learned Pattern has
``track_changes``, so the version history still holds it). Freezing what is
there today stops the drift going forward.

Fresh installs never run patches (``install_app(set_as_patched=True)`` marks
them done without executing), which is correct here - a fresh site has no
pre-existing approved rows to backfill.

Idempotent: only rows whose ``approved_draft`` is still NULL/empty are touched,
raw ``db.set_value`` with ``update_modified=False`` (never ``doc.save()``, which
would run the status-transition guard mid-migrate), so a re-run is a no-op.
"""

import frappe

JLP = "Jarvis Learned Pattern"
BACKFILL_STATUSES = ("Approved", "Active")


def execute():
	if not frappe.db.has_column(JLP, "approved_draft"):
		return

	rows = frappe.get_all(
		JLP,
		filters={"status": ["in", list(BACKFILL_STATUSES)], "approved_draft": ["in", [None, ""]]},
		fields=["name", "skill_draft", "approved_draft"],
	)
	stamped = 0
	for row in rows:
		if (row.approved_draft or "").strip():
			continue  # belt-and-braces against a partial deploy
		draft = (row.skill_draft or "").strip()
		if not draft:
			# No text to freeze; _bullet falls back to pattern_statement.
			continue
		frappe.db.set_value(JLP, row.name, "approved_draft", draft, update_modified=False)
		stamped += 1

	if stamped:
		frappe.db.commit()
		frappe.logger("jarvis").info(
			f"#482 approved_draft backfill: froze the reviewed text of {stamped} "
			f"learned pattern(s) (of {len(rows)} unsnapshotted Approved/Active rows)"
		)
