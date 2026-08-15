"""PP-1 / PP-5: backfill ``result_class`` + ``agent``/``run``/``preparation_mode``
on agent-originated ``Jarvis Approval Request`` rows.

The PP-1/PP-5 contract (``01-PLATFORM-PREREQS.md``) adds these fields so an
agent-originated approval is DISTINGUISHABLE from a human one: an agent-originated
request carries ``agent``/``run``/``preparation_mode`` + a PP-1 ``result_class``; a
HUMAN File-Box/Chat request leaves them blank and MUST stay untouched. The sibling
patches (``v2_03_backfill_finding_result_class`` / ``..._installation_activation``)
grandfathered findings + installations but NOT approvals — this closes that gap.

Legacy agent-originated rows predate these fields. The only signal that a legacy
approval was raised INSIDE an agent run is that its ``conversation`` matches a
``Jarvis Agent Run.conversation`` — an agent run owns a dedicated conversation,
whereas a human ask links the user's own chat conversation. Rows with no such
linkage are human-originated and are left blank (untouched).

For each agent-originated row still missing its provenance:

  * ``agent``            <- the run's ``agent``;
  * ``run``              <- the run's name;
  * ``preparation_mode`` <- the run's ``preparation_mode`` (falling back to the
    installation's ``activation_state``, else ``shadow``);
  * ``result_class``     <- CONSERVATIVE (never silently a fact): ``observed_fact``
    UNLESS the row's title/question/context carries a match/estimate/exposure
    marker or a strong verb, in which case ``derived_candidate``. No legacy row is
    backfilled to ``confirmed_outcome`` (reserved for the PP-5 ledger) — mirrors
    the finding backfill's conservative classification, which it reuses verbatim.

Raw ``db.set_value`` (never ``doc.save()``): ``agent``/``run``/``preparation_mode``/
``result_class`` are ``read_only`` and this is trusted migration infrastructure.
Idempotent: a row is processed only while its ``agent`` link is still empty (the
origination marker), so a re-run is a no-op and any row a newer code path already
stamped is skipped.
"""

import frappe

from jarvis.patches.v2_03_backfill_finding_result_class import _looks_like_candidate

APPROVAL = "Jarvis Approval Request"
RUN = "Jarvis Agent Run"
INSTALLATION = "Jarvis Agent Installation"


def execute():
	# Only run once the schema carries the new columns (schema-sync precedes this
	# post_model_sync patch, but guard so an out-of-order run is a safe no-op).
	if not (
		frappe.db.has_column(APPROVAL, "agent")
		and frappe.db.has_column(APPROVAL, "run")
		and frappe.db.has_column(APPROVAL, "preparation_mode")
		and frappe.db.has_column(APPROVAL, "result_class")
	):
		return

	# Agent-originated legacy rows = an approval whose conversation is an agent
	# run's conversation, still missing its origination stamp (agent empty). A human
	# File-Box/Chat approval never matches a run conversation, so it is excluded.
	rows = frappe.db.sql(
		"""
		SELECT a.name AS approval, a.title, a.question, a.context_md,
		       r.name AS run, r.agent, r.installation, r.preparation_mode
		FROM `tabJarvis Approval Request` a
		JOIN `tabJarvis Agent Run` r ON r.conversation = a.conversation
		WHERE a.conversation IS NOT NULL AND a.conversation != ''
		  AND (a.agent IS NULL OR a.agent = '')
		ORDER BY r.creation ASC
		""",
		as_dict=True,
	)

	seen: set[str] = set()
	facts = candidates = 0
	for row in rows:
		if row.approval in seen:
			continue  # a conversation maps to one run; first (earliest) wins
		seen.add(row.approval)

		prep = (row.preparation_mode or "").strip()
		if not prep and row.installation:
			prep = (frappe.db.get_value(INSTALLATION, row.installation, "activation_state") or "").strip()
		prep = prep or "shadow"

		blob = f"{row.title or ''}\n{row.question or ''}\n{row.context_md or ''}"
		if _looks_like_candidate(blob):
			result_class = "derived_candidate"
			candidates += 1
		else:
			result_class = "observed_fact"
			facts += 1

		frappe.db.set_value(
			APPROVAL,
			row.approval,
			{
				"agent": row.agent,
				"run": row.run,
				"preparation_mode": prep,
				"result_class": result_class,
			},
			update_modified=False,
		)

	frappe.db.commit()
	if seen:
		frappe.logger("jarvis").info(
			f"PP-1/PP-5 approval provenance backfill: {facts} observed_fact, "
			f"{candidates} derived_candidate (of {len(seen)} agent-originated approvals)"
		)
