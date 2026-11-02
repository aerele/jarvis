"""Hard-close the legacy Reorder & Replenishment operator.

The operator remains unavailable. Keep history, but remove every enabled or
scheduled legacy route, terminalize an in-flight legacy run, and request a
fleet reconcile before a later release can introduce a typed backend.

Deployment runbook: put the site in maintenance/drain state; stop every web
and background worker that can hold older Jarvis imports; run ``bench migrate``
from the new checkout; restart all workers; then Apply and verify the fleet
roster omits this operator. A restart only after migrate is too late: a Python
worker that loaded the older confirmation code cannot observe this new database
fence or revoke a credential it already read.
"""

import frappe

from jarvis.chat.reorder_replenishment_gate import (
	CLOSED_STATUS,
	CLOSURE_GENERATION,
	REORDER_AGENT,
	RUN_UNAVAILABLE,
	activate_legacy_confirmation_fence,
)

INSTALLATION = "Jarvis Agent Installation"
LISTING = "Jarvis Agent Listing"
RUN = "Jarvis Agent Run"
SESSION = "Jarvis Chat Session"

_SESSION_KEY_PATTERNS = (
	f"agent:agent-{REORDER_AGENT}:%",  # current delegate agent ID form
	f"agent:{REORDER_AGENT}:%",  # pre-2B bare slug form
)


def execute():
	# This must be the first durable state change. A historical RRO confirmation
	# has no reliable agent/run provenance, so every new-code confirmation path
	# serializes on this fence and rejects its old unmarked credential. Commit it
	# before this patch scans/cancels cards: an interrupted scan stays fail-closed.
	activate_legacy_confirmation_fence()
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist safety fence before cleanup

	# A raw historical edit may have disabled the install without triggering Apply,
	# and an allowed Published listing can have shipped the delegate with no install.
	# This patch changes the desired fleet roster in either case. Its strict Apply
	# marker is written with the raw roster updates, before any cleanup can commit.
	frappe.db.sql(
		f"""UPDATE `tab{INSTALLATION}`
		SET enabled = 0, schedule_enabled = 0, next_run_at = NULL
		WHERE agent = %s""",
		(REORDER_AGENT,),
	)
	# Restore the catalogue display state as well. The fixed server gate remains
	# authoritative if a raw writer changes it again after this migration.
	frappe.db.sql(
		f"""UPDATE `tab{LISTING}`
		SET status = %s
		WHERE name = %s""",
		(CLOSED_STATUS, REORDER_AGENT),
	)
	# Old uninstall paths could delete a run before the A8 session teardown was
	# introduced. Remove those orphan bearer rows by both historical key formats
	# before looking up the Run table, since an orphan has no row to reach it.
	frappe.db.sql(
		f"""DELETE FROM `tab{SESSION}`
		WHERE session_key LIKE %s OR session_key LIKE %s""",
		_SESSION_KEY_PATTERNS,
	)
	# These three raw changes alter the fleet payload. Mark them in this same
	# transaction before any helper can commit it. Strict mode means Patch Log can
	# never record a completed close that did not request an Apply.
	from jarvis.chat.agents_api import _mark_catalog_dirty

	_mark_catalog_dirty(strict=True)

	# Revoke every local bearer ever stamped on this closed operator. This includes
	# a legacy terminal run: old versions could leave its session row behind, and
	# capability resolution does not use the run status as authorization. Use a
	# strict raw delete for each recorded key, not the normal best-effort teardown:
	# this never-skippable migration must fail rather than leave an odd historic
	# bearer key usable. Running rows then use the scheduler's compare-and-set path
	# for an honest failure.
	from jarvis.chat import agent_scheduler

	runs = frappe.get_all(
		RUN,
		filters={"agent": REORDER_AGENT},
		fields=["name", "status", "session_key"],
	)
	for run in runs:
		if run.session_key:
			frappe.db.sql(
				f"DELETE FROM `tab{SESSION}` WHERE session_key = %s",
				(run.session_key,),
			)
	for run in runs:
		if run.status == "running":
			# ``fail_run`` is deliberately best-effort for normal request paths. This
			# never-skippable safety migration cannot accept that contract: verify the
			# terminal state under the Run row lock before it can complete. Otherwise a
			# swallowed scheduler failure could leave an old delegate marked running
			# after Patch Log records this close as complete.
			agent_scheduler.fail_run(run.name, RUN_UNAVAILABLE)
			terminal = frappe.db.get_value(RUN, run.name, "status", for_update=True)
			if terminal == "running":
				frappe.throw(
					f"Could not terminalize legacy Reorder run {run.name}; retry migration after repair.",
					frappe.ValidationError,
				)

	# A legacy Redis token and an old durable chat card can both contain a generic
	# create/update call with no RRO identity. Re-propose ordinary cards instead of
	# guessing: the fence above makes an interrupted cleanup fail closed as well.
	from jarvis.chat import pending_actions, pending_confirm_legacy

	pending_actions.cancel_chat_pending_before_generation(CLOSURE_GENERATION)
	pending_confirm_legacy.revoke_all()
