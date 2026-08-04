"""Jarvis Agent Finding DocType controller.

One persistent, severity-tagged finding produced by a delegate's deterministic
evaluator and written by ``jarvis.chat.agent_runs.record_delegate_run`` (never
model-emitted — the delegate narrates, this row is the reproducibility
guarantee). Deduped across runs on ``fingerprint``. The ``All`` role gets ``if_owner`` read + write
so the customer can move a finding to acknowledged/resolved from the SPA, but
cannot forge or reassign one (rows are inserted server-side and owned by the
installation owner).
"""

import frappe
from frappe import _
from frappe.model.document import Document

RUN = "Jarvis Agent Run"

# Engine-owned audit fields — everything except the user-actionable ``state``.
# Frozen on any customer save so the audited party cannot rewrite/erase a finding.
# PP-5 review-provenance fields are included: once the engine/ledger stamps them
# (via ``db.set_value``, which bypasses this controller) a customer ORM save can
# never rewrite who/when/what-kind resolved the finding, nor forge a result link.
_FROZEN_FIELDS = (
	"run",
	"agent",
	"rule_id",
	"severity",
	"title",
	"detail_md",
	"section",
	"effective_date",
	"disclaimer",
	"ref_doctype",
	"ref_name",
	"amount",
	"fingerprint",
	"first_seen_run",
	"last_seen_run",
	# PP-5 provenance / review fields (immutable-once-written at the ORM level).
	"confirmation_status",
	"outcome_provenance",
	"resolved_by",
	"resolved_at",
	"resolution_kind",
	"result_link_doctype",
	"result_link_name",
)


class JarvisAgentFinding(Document):
	def validate(self):
		self._guard_result_class_set_once()
		self._freeze_audit_fields()

	def on_trash(self):
		self._guard_uninstall_cascade()

	def _guard_uninstall_cascade(self):
		"""#455 defence in depth — an uninstall cascade may only destroy findings its
		OWN installation produced.

		``agents_api.uninstall_agent`` names the installation it is cascading for on
		the delete flags (``frappe.delete_doc(..., flags={...})`` applies them to the
		fetched doc BEFORE ``on_trash`` runs), and this re-derives membership
		independently from the row's frozen creation stamps. A mismatch means the
		caller widened its selection past this installation, so the delete is refused
		rather than silently destroying another customer's audit history.

		The guard is inert outside a cascade: with no flag present (Desk delete, test
		teardown, admin cleanup) it does nothing. ``force=True`` and
		``ignore_permissions=True`` do NOT bypass ``on_trash``, which is precisely why
		it is the right place for this check — the original defect ran with both.
		"""
		installation = self.flags.get("jarvis_uninstall_installation")
		if not installation:
			return
		for field in ("run", "first_seen_run"):
			run = self.get(field)
			if run and frappe.db.get_value(RUN, run, "installation") == installation:
				return
		frappe.throw(
			_(
				"Refusing to delete finding {0}: it was not produced by installation {1}. "
				"An uninstall may only remove its own installation's findings."
			).format(self.name, installation),
			frappe.PermissionError,
		)

	def _guard_result_class_set_once(self):
		"""PP-1: ``result_class`` is the immutable epistemic class of the finding —
		set once at writeback and never editable afterwards. Unlike the audit fields
		(silently restored), a changed class is a contract violation, so this RAISES.
		The insert (``is_new``) is exempt (the one legitimate write); the engine /
		ledger never re-classes a finding via the ORM, so the guard fires regardless
		of ``ignore_permissions`` — the class is a hard, one-way commit."""
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return
		stored = before.get("result_class")
		if stored and self.result_class != stored:
			frappe.throw(
				_("result_class is set once at writeback (PP-1) and cannot be changed afterwards."),
				frappe.PermissionError,
			)

	def _freeze_audit_fields(self):
		"""TASK 31 (AGENTS-3): a finding is the reproducibility guarantee of an
		audit run. The customer gets ``if_owner`` WRITE so they can move a finding's
		``state`` (acknowledge / resolve), but the audited party must NOT be able to
		silently rewrite or erase the audit content (severity / detail_md / amount /
		…) via a generic-REST PUT and defeat the audit trail. On any non-new,
		non-server save, restore every engine-owned field to its stored value so
		only ``state`` can change. The engine writes with ``ignore_permissions``
		(insert) or raw ``db.set_value`` (recurrence / auto-resolve bumps), both of
		which bypass this."""
		if self.is_new() or self.flags.ignore_permissions:
			return
		before = self.get_doc_before_save()
		if not before:
			return
		for field in _FROZEN_FIELDS:
			stored = before.get(field)
			if self.get(field) != stored:
				self.set(field, stored)
