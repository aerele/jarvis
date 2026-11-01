"""Hard-close the unreleased Reorder & Replenishment operator.

The marketplace listing is useful display data, but it is mutable tenant data.
This module is the server-side authority while the operator has no native,
typed proposal path. It intentionally does not inspect a model result, an
installation configuration, or a Negative Stock receipt: none of those facts
can make the unreleased operator safe to run.
"""

from __future__ import annotations

import frappe
from frappe import _

REORDER_AGENT = "reorder-replenishment-operator"
CLOSED_STATUS = "Coming Soon"

# This is a one-way server fence. The Patch Log is the immutable long-term
# proof; this internal normal DocType gives the migration and an old credential
# one fixed, primary-key row to lock before Frappe records that log row. It is
# deliberately not a Single: a Single has no unique row key and an editable
# Settings save rewrites that doctype's whole row set.
FENCE_DOCTYPE = "Jarvis Reorder Safety Fence"
FENCE_NAME = "reorder-close-fence"
_FENCE_FIELD = "legacy_confirmation_generation"
PENDING_ACTION_FENCE_FIELD = "reorder_close_generation"
CLOSURE_PATCH = "jarvis.patches.v2_31_disable_reorder_replenishment_installations"
CLOSURE_GENERATION = "rro-close-v1"

_UNAVAILABLE = "This agent is not available yet. It cannot be installed, enabled, scheduled, or run."
_LISTING_UNAVAILABLE = "This agent is not available yet. Its status must stay Coming Soon."
_LEGACY_UNAVAILABLE = "This agent is not available yet. Disable its old installation before you edit it."
RUN_UNAVAILABLE = "This agent is not available yet. The old run was stopped. No ERP record was changed."
CONFIRMATION_REVOKED = (
	"This confirmation was cancelled during an agent safety update. "
	"Submit the action again if it is still needed."
)

# Agent runs have always used one of these two opaque bearer forms. The current
# scheduler emits the first form; the second is the pre-2B form that can still
# exist in a historic session row. Keep this identity check independent of the
# Run table: the close migration deliberately deletes that row's bearer before
# it terminalizes every old Run.
_REORDER_SESSION_PREFIXES = (
	f"agent:agent-{REORDER_AGENT}:",
	f"agent:{REORDER_AGENT}:",
)


def is_reorder(agent: str | None) -> bool:
	"""Return whether an agent name is the closed Reorder operator."""
	return (agent or "").strip() == REORDER_AGENT


def is_reorder_session_key(session_key: str | None) -> bool:
	"""Return whether an opaque agent bearer belongs to the closed operator."""
	key = (session_key or "").strip()
	return any(key.startswith(prefix) for prefix in _REORDER_SESSION_PREFIXES)


def assert_session_dispatch_allowed(session_key: str | None) -> None:
	"""Refuse a historic Reorder bearer at the last generic dispatch boundary.

	Plugin authentication resolves the session user before ``_dispatch_from_session``
	runs. The close migration can then delete the bearer row while that request is
	in flight. This check uses the immutable agent id embedded in every issued
	Reorder bearer, rather than the Run or session row that cleanup can remove.
	It therefore closes an orphan bearer even when authentication already captured
	the run-as user.
	"""
	if is_reorder_session_key(session_key):
		frappe.throw(_("This agent is not available yet. No action was taken."), frappe.PermissionError)


def _fence_value(*, lock: bool) -> str:
	"""Read the close-fence value, optionally holding its row lock.

	The normal DocType table gives this row a primary-key ``name``. Seed it before
	``FOR UPDATE`` so a pre-migration confirm and the migration have a real common
	lock. The caller owns the transaction and therefore the lock.
	"""
	# New application code can run briefly before model sync creates this table.
	# That is an inactive state: mint does not stamp a card, and the fail-closed
	# confirmation rule below refuses that unmarked card. Never make a pre-sync
	# request crash just because this migration's internal table is not there yet.
	if not frappe.db.table_exists(FENCE_DOCTYPE, cached=False):
		if lock:
			frappe.throw("The Reorder safety fence table is not ready.", frappe.ValidationError)
		return ""
	params = {"name": FENCE_NAME, "user": "Administrator"}
	if lock:
		frappe.db.sql(
			f"""INSERT IGNORE INTO `tab{FENCE_DOCTYPE}`
			(`name`, `creation`, `modified`, `modified_by`, `owner`, `docstatus`, `idx`, `{_FENCE_FIELD}`)
			VALUES (%(name)s, NOW(6), NOW(6), %(user)s, %(user)s, 0, 0, '')""",
			params,
		)
	rows = frappe.db.sql(
		f"SELECT `{_FENCE_FIELD}` FROM `tab{FENCE_DOCTYPE}` WHERE `name`=%(name)s"
		+ (" FOR UPDATE" if lock else ""),
		params,
	)
	if lock and not rows:
		frappe.throw("The Reorder safety fence could not be locked.", frappe.ValidationError)
	return str(rows[0][0] or "") if rows else ""


def _closure_patch_logged() -> bool:
	"""Return whether Frappe recorded this irreversible close patch."""
	return bool(frappe.db.exists("Patch Log", {"patch": CLOSURE_PATCH, "skipped": 0}))


def legacy_confirmation_generation(*, lock: bool = False) -> str | None:
	"""Return the active close generation, and optionally hold its DB lock.

	A confirmation that locks this row before the migration is ordered before the
	close. A confirmation that locks it after the migration sees this generation
	and cannot dispatch an old unmarked call. Once Patch Log records the patch,
	the fence remains active even if someone changes the mutable Single value.
	"""
	if _fence_value(lock=lock) == CLOSURE_GENERATION:
		return CLOSURE_GENERATION
	return CLOSURE_GENERATION if _closure_patch_logged() else None


def activate_legacy_confirmation_fence() -> str:
	"""Durably activate the close fence. The migration commits this first."""
	_fence_value(lock=True)
	frappe.db.sql(
		f"""UPDATE `tab{FENCE_DOCTYPE}`
		SET `{_FENCE_FIELD}`=%(generation)s, `modified`=NOW(6), `modified_by`=%(user)s
		WHERE `name`=%(name)s""",
		{"name": FENCE_NAME, "user": "Administrator", "generation": CLOSURE_GENERATION},
	)
	return CLOSURE_GENERATION


def stamp_pending_confirmation() -> str:
	"""Return the generation a newly parked durable card must carry."""
	return legacy_confirmation_generation() or ""


def legacy_confirmation_allowed(record: dict) -> bool:
	"""Accept only a token minted after this close generation.

	Historic generic confirmation tokens have no trustworthy agent identity. Do
	not try to guess whether one belonged to Reorder: reject every unmarked one
	before it reaches any generic tool. This is deliberately fail-closed during
	the short code-before-migration window as well; the release runbook drains
	requests, then activation lets later ordinary cards receive this stamp.
	"""
	return bool(record and record.get(PENDING_ACTION_FENCE_FIELD) == CLOSURE_GENERATION)


def pending_confirmation_allowed(row) -> bool:
	"""Accept a chat card only when it was stamped after the close fence."""
	if row.get("kind") != "chat":
		return True
	return row.get(PENDING_ACTION_FENCE_FIELD) == CLOSURE_GENERATION


def refuse() -> None:
	"""Raise the stable customer-facing refusal for an opening action."""
	frappe.throw(_(_UNAVAILABLE), frappe.PermissionError)


def assert_new_install_allowed(agent: str | None) -> None:
	"""Refuse a new install before it can create any installation state."""
	if is_reorder(agent):
		refuse()


def assert_listing_status(agent: str | None, status: str | None) -> None:
	"""Keep the Reorder listing closed on every ORM save surface."""
	if is_reorder(agent) and (status or "").strip() != CLOSED_STATUS:
		frappe.throw(_(_LISTING_UNAVAILABLE), frappe.PermissionError)


def assert_enabled(agent: str | None, enabled: int | bool) -> None:
	"""Refuse an attempt to enable the closed operator."""
	if is_reorder(agent) and frappe.utils.cint(enabled):
		refuse()


def assert_scheduled(agent: str | None, schedule_enabled: int | bool) -> None:
	"""Refuse an attempt to schedule the closed operator."""
	if is_reorder(agent) and frappe.utils.cint(schedule_enabled):
		refuse()


def assert_launch_allowed(agent: str | None) -> None:
	"""Refuse a direct or normal launch before it creates a run or conversation."""
	if is_reorder(agent):
		refuse()


def retire_document(doc) -> None:
	"""Make an old installation inert before a supported disable operation saves it."""
	if not is_reorder(doc.get("agent")):
		return
	doc.enabled = 0
	doc.schedule_enabled = 0
	doc.next_run_at = None


def validate_installation(doc) -> None:
	"""Reject new installs and active legacy rows on Desk, import, and REST saves."""
	if not is_reorder(doc.get("agent")):
		return
	if doc.is_new():
		refuse()
	if frappe.utils.cint(doc.get("enabled")) or frappe.utils.cint(doc.get("schedule_enabled")):
		frappe.throw(_(_LEGACY_UNAVAILABLE), frappe.PermissionError)
	# A raw legacy update can leave a stale due time after both switches are off.
	# Removing it here keeps a subsequent save from reviving a scheduler trigger.
	doc.next_run_at = None


def retire_legacy_installation(name: str | None) -> None:
	"""Disable one stale scheduler row without creating a run record.

	The caller marks the fleet catalog dirty and commits in the same transaction.
	"""
	if not name:
		return
	frappe.db.sql(
		"""UPDATE `tabJarvis Agent Installation`
		SET enabled = 0, schedule_enabled = 0, next_run_at = NULL
		WHERE name = %s AND agent = %s""",
		(name, REORDER_AGENT),
	)


def delegate_tool_denial(cap: dict, tool: str) -> dict | None:
	"""Return a fatal denial for every old Reorder delegate tool call."""
	if not is_reorder(cap.get("agent")):
		return None
	name = str(tool or "").strip()
	return {
		"run": cap.get("run"),
		"agent": REORDER_AGENT,
		"tool": name,
		"fatal": True,
		"message": (
			"the Reorder & Replenishment operator is unavailable while its server-side "
			"gate is closed; no tool call is allowed and no ERP record was changed"
		),
		"chat_message": "This agent is not available yet. No action was taken.",
		"run_error": RUN_UNAVAILABLE,
	}


def direct_write_refusal(operation: str, doctype: str) -> str:
	"""Return the refusal for a generic document tool reached outside the dispatcher."""
	return (
		f"agent '{REORDER_AGENT}' may not {operation} '{doctype}': the Reorder & "
		"Replenishment operator is unavailable and has no generic ERP write authority"
	)
