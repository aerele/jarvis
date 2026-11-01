"""Human physical-count facts used by the Cycle-Count Planner.

This DocType intentionally has no stock or accounting hooks. It records what a
person observed. ERPNext Stock Reconciliation remains the separate, human-owned
workflow for a later inventory adjustment.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from jarvis.permissions import NotRenamable, refuse_in_tool_dispatch


class JarvisPhysicalCount(NotRenamable, Document):
	def before_insert(self):
		refuse_in_tool_dispatch()
		if self.docstatus not in (0, None):
			frappe.throw(_("A physical count must be inserted as a draft before it is submitted."))

	def validate(self):
		refuse_in_tool_dispatch()
		from jarvis.chat.cycle_count_entry import validate_count_document

		validate_count_document(self, action="cancel" if self.docstatus == 2 else "record")
		self._guard_immutable_submitted_fields()

	def before_submit(self):
		refuse_in_tool_dispatch()
		from jarvis.chat.cycle_count_entry import stamp_submitted_count

		stamp_submitted_count(self)

	def before_cancel(self):
		refuse_in_tool_dispatch()
		from jarvis.chat.cycle_count_entry import validate_count_document

		validate_count_document(self, action="cancel")

	def on_trash(self):
		refuse_in_tool_dispatch()
		# A cancellation is retained as evidence. A count fact must never become
		# erasable by cancelling it first.
		if self.docstatus != 0:
			frappe.throw(_("Submitted or cancelled physical-count facts cannot be deleted."), frappe.PermissionError)

	def _guard_immutable_submitted_fields(self):
		if self.is_new() or self.docstatus == 0:
			return
		before = self.get_doc_before_save()
		# The first submit changes docstatus from draft and stamps the server-owned
		# recorder fields. It is governed by validate/before_submit above, not by
		# the after-submit immutability guard.
		if not before or before.docstatus == 0:
			return
		for field in (
			"approval_request",
			"source_run",
			"company",
			"warehouse",
			"counted_at",
			"recorded_by",
			"recorded_at",
			"entry_key",
			"entry_digest",
		):
			if self.get(field) != before.get(field):
				frappe.throw(_("Submitted physical-count fields cannot be changed."), frappe.PermissionError)
		if self.has_value_changed("items"):
			frappe.throw(_("Submitted physical-count items cannot be changed."), frappe.PermissionError)


def on_doctype_update():
	"""Indexes for history collection and idempotent count entry."""
	frappe.db.add_index(
		"Jarvis Physical Count",
		["company", "warehouse", "docstatus", "counted_at"],
		index_name="physical_count_history_index",
	)
	frappe.db.add_index(
		"Jarvis Physical Count",
		["approval_request", "docstatus"],
		index_name="physical_count_approval_index",
	)
