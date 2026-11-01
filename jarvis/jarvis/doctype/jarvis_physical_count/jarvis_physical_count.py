"""Human physical-count facts used by the Cycle-Count Planner.

This DocType intentionally has no stock or accounting hooks. It records what a
person observed. ERPNext Stock Reconciliation remains the separate, human-owned
workflow for a later inventory adjustment.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from jarvis.permissions import NotRenamable, refuse_in_tool_dispatch

_MUTATION_PTYPES = frozenset({"create", "write", "submit", "cancel", "amend", "delete"})


class JarvisPhysicalCount(NotRenamable, Document):
	def check_permission(self, permtype="read", permlevel=None):
		"""Reject native writes before Frappe locks a draft or source row."""
		if permtype in _MUTATION_PTYPES and not self.flags.ignore_permissions:
			from jarvis.chat.cycle_count_entry import is_controlled_count_mutation

			if not is_controlled_count_mutation(self):
				frappe.throw(
					_("Record or correct physical counts from the approved count plan."),
					frappe.PermissionError,
				)
		return super().check_permission(permtype, permlevel)

	def before_insert(self):
		refuse_in_tool_dispatch()
		if self.docstatus not in (0, None):
			frappe.throw(_("A physical count must be inserted as a draft before it is submitted."))
		if self.get("amended_from"):
			# Desk copies no_copy fields when a user selects Amend. These values are
			# stamped only when the replacement submits. The controlled API owns its
			# fresh idempotency key and marks that one in-memory document explicitly.
			# Frappe's default amended-name rule assigns the same suffix to every
			# draft from one source. A retained, unsubmitted draft must not prevent a
			# later correction, so use this hash-named DocType's normal opaque name.
			self.name = frappe.generate_hash(length=20)
			self.flags.name_set = True
			self.recorded_by = ""
			self.recorded_at = None
			self.entry_digest = ""
			if not self.flags.get("cycle_count_controlled_entry"):
				self.entry_key = ""

	def validate(self):
		refuse_in_tool_dispatch()
		from jarvis.chat.cycle_count_entry import validate_count_document

		self._guard_draft_amendment_lineage()
		validate_count_document(self, action="cancel" if self.docstatus == 2 else "record")
		self._guard_immutable_submitted_fields()

	def before_submit(self):
		refuse_in_tool_dispatch()
		from jarvis.chat.cycle_count_entry import stamp_submitted_count

		stamp_submitted_count(self)

	def before_cancel(self):
		refuse_in_tool_dispatch()
		from jarvis.chat.cycle_count_entry import validate_count_document

		# Desk savedocs supplies client JSON and calls before_cancel without this
		# controller's validate hook. Reject changed server-owned facts before
		# authorization, then authorize against the locked stored predecessor.
		self._guard_immutable_submitted_fields()
		validate_count_document(self, action="cancel")

	def before_discard(self):
		refuse_in_tool_dispatch()
		# A Desk client can edit the in-memory draft before it asks to discard it.
		# Use the saved draft so a plan viewer cannot clear amended_from and turn a
		# correction attempt into permanent cancelled history.
		before = self.get_doc_before_save()
		if (before or self).get("amended_from"):
			frappe.throw(
				_(
					"Amendment drafts cannot be discarded. Leave the draft unsubmitted and create a fresh correction."
				),
				frappe.PermissionError,
			)

	def discard(self):
		"""Refuse an obsolete native draft action before Frappe locks the row."""
		if self.flags.ignore_permissions:
			return super().discard()
		refuse_in_tool_dispatch()
		frappe.throw(
			_(
				"Physical-count drafts cannot be discarded. Record or correct counts from the approved count plan."
			),
			frappe.PermissionError,
		)

	def on_trash(self):
		refuse_in_tool_dispatch()
		# A cancellation is retained as evidence. A count fact must never become
		# erasable by cancelling it first.
		if self.docstatus != 0:
			frappe.throw(
				_("Submitted or cancelled physical-count facts cannot be deleted."), frappe.PermissionError
			)

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
			"amended_from",
			"source_run",
			"company",
			"warehouse",
			"counted_at",
			"recorded_by",
			"recorded_at",
			"entry_key",
			"entry_digest",
		):
			if not self._same_immutable_value(field, self.get(field), before.get(field)):
				frappe.throw(_("Submitted physical-count fields cannot be changed."), frappe.PermissionError)
		if not self._same_submitted_items(before):
			frappe.throw(_("Submitted physical-count items cannot be changed."), frappe.PermissionError)

	def _guard_draft_amendment_lineage(self):
		"""Keep a saved amendment draft in its controlled correction path."""
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if before and before.docstatus == 0 and before.get("amended_from"):
			if self.get("amended_from") != before.get("amended_from"):
				frappe.throw(
					_("An amendment draft must keep its original physical-count fact."),
					frappe.PermissionError,
				)

	@staticmethod
	def _same_immutable_value(field, value, before_value):
		"""Compare stored facts without rejecting the Desk JSON datetime form."""
		if field not in {"counted_at", "recorded_at"}:
			return value == before_value
		if not value or not before_value:
			return value == before_value
		try:
			return frappe.utils.get_datetime(value) == frappe.utils.get_datetime(before_value)
		except (TypeError, ValueError):
			return False

	def _same_submitted_items(self, before):
		"""Allow only Frappe's submit-to-cancel status transition on child rows."""
		fields = ("name", "item_code", "counted_qty", "stock_uom", "batch_detail_recorded")
		current = [tuple(row.get(field) for field in fields) for row in self.items or []]
		stored = [tuple(row.get(field) for field in fields) for row in before.items or []]
		return len(current) == len(stored) and set(current) == set(stored)


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
	frappe.db.add_index(
		"Jarvis Physical Count",
		["amended_from", "docstatus"],
		index_name="physical_count_amendment_index",
	)
