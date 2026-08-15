"""Jarvis Learned Pattern DocType controller.

One row per detected behavioural pattern (proposal). Rows are created and
evidence-refreshed exclusively by the learning engine (which sets
``frappe.flags.jarvis_pattern_engine`` to bypass the transition guard);
System Managers move rows through the review lifecycle via the whitelisted
``learned_api`` endpoints (SM has read+write, no create/delete, no if_owner).
The status state machine below is plan section 6.5 - any other jump (e.g.
Rejected -> Active, Archived -> anything) is rejected so a buggy endpoint or
a stray Desk write cannot activate a pattern without review.
"""

import frappe
from frappe import _
from frappe.model.document import Document

LEGAL_TRANSITIONS = {
	"Proposed": {"Approved", "Rejected", "Snoozed"},
	"Approved": {"Active", "Proposed", "Stale"},
	"Active": {"Stale", "Superseded"},
	"Snoozed": {"Proposed"},
	"Stale": {"Approved", "Rejected"},
	"Rejected": {"Proposed", "Archived"},
	"Superseded": {"Archived"},
}


class JarvisLearnedPattern(Document):
	def validate(self):
		self.validate_transition()

	def validate_transition(self):
		if frappe.flags.jarvis_pattern_engine:
			return
		before = self.get_doc_before_save()
		if before is None:
			# Only the engine inserts rows; anything else enters at Proposed.
			if (self.status or "Proposed") != "Proposed":
				frappe.throw(
					_("New learned patterns must start as Proposed."),
					frappe.ValidationError,
				)
			return
		old_status = before.status or "Proposed"
		new_status = self.status or "Proposed"
		if old_status == new_status:
			return
		if new_status not in LEGAL_TRANSITIONS.get(old_status, set()):
			frappe.throw(
				_("Illegal learned-pattern status transition: {0} to {1}.").format(old_status, new_status),
				frappe.ValidationError,
			)
