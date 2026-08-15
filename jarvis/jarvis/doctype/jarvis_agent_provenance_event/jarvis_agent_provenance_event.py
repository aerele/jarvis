"""PP-5 — append-only provenance ledger event.

Connects findings -> approvals -> drafts/transactions -> incidents with
immutable launch-time facts and first-class outcome events. Once a row is
persisted it may never be modified or deleted (System Manager included); the
only permitted operations are ``create`` + ``read``. This is the structural
guarantee that the measurement ledger — confirmed outcomes (PP-1), shadow
promotions (PP-4), approval decisions, the activation-budget raises (PP-6) —
cannot be rewritten after the fact.

The ledger-writer helpers (which actually append events and, for a
transaction_posted / finding_resolved(human) event, close a finding's
``outcome_provenance`` loop and move its ``confirmation_status`` to
``confirmed``) are layered on in a later phase; this controller only enforces
the append-only invariant and stamps ``occurred_at``.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class JarvisAgentProvenanceEvent(Document):
	def before_insert(self):
		if not self.occurred_at:
			self.occurred_at = frappe.utils.now()

	def on_update(self):
		# Frappe runs on_update on both insert and every subsequent save. The
		# insert is permitted (flags.in_insert is set only during insert); any
		# later modification is refused — append-only, no exceptions.
		if not self.flags.in_insert:
			frappe.throw(
				_("Jarvis Agent Provenance Event is append-only; a persisted event cannot be modified."),
				frappe.PermissionError,
			)

	def on_trash(self):
		frappe.throw(
			_("Jarvis Agent Provenance Event is append-only; a persisted event cannot be deleted."),
			frappe.PermissionError,
		)
