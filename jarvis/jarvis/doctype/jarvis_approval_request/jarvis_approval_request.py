# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class JarvisApprovalRequest(Document):
	"""A decision the agent needs a human for.

	Created by the agent itself (via the generic ``jarvis__create_doc``
	tool, per the ocr-data-entry skill contract). The chat turn ENDS
	there - the customer decides later from the Approvals pane/page, and
	the decision is posted back into the linked conversation so the
	agent resumes with full context.
	"""

	def validate(self):
		self._guard_decided_fields()
		if self.status != "Pending" and not self.decision:
			frappe.throw("A decided approval must carry the decision text.")

	def _guard_decided_fields(self):
		"""TASK 25(b): the decided-outcome fields (status / decision / decided_by /
		decided_at / trace_comment_added) are permlevel-1 (SM-only), so a normal
		Jarvis User's REST create/write cannot set them — but assert the invariant
		here too so no doc.save path can forge a decided or misattributed approval:

		  * a NEW row by a normal caller must be Pending & undecided;
		  * a CHANGE to ``decided_by`` must set it to the acting user.

		Server transitions bypass this entirely: the real decide/dismiss/restore
		flows use raw ``db.sql`` / ``db.set_value`` (never reach validate), and the
		chat-ask materializer + agent tool insert with ``ignore_permissions``.
		Administrator is trusted. An update that leaves ``decided_by`` untouched
		(e.g. the agent filling ref_doctype/ref_name after the decision) is allowed."""
		if self.flags.ignore_permissions:
			return
		user = frappe.session.user
		if user == "Administrator":
			return
		if self.is_new():
			if (self.status or "Pending") != "Pending" or self.decision or self.decided_by:
				frappe.throw(
					"A new approval request must be Pending and undecided.",
					frappe.PermissionError,
				)
			return
		if self.has_value_changed("decided_by") and self.decided_by and self.decided_by != user:
			frappe.throw(
				"decided_by must be the deciding user.",
				frappe.PermissionError,
			)

	def on_update(self):
		self._leave_trace_comment()

	def _leave_trace_comment(self):
		"""Audit trail ON the business document: once this approval is
		decided AND points at a real document, add a Comment to that
		document in the name of the deciding user. Runs on every save so
		it also fires when the agent fills ref_doctype/ref_name AFTER the
		decision (the resumed flow creates the document later)."""
		if (
			self.status == "Pending"
			or self.trace_comment_added
			or not (self.ref_doctype and self.ref_name)
			or not frappe.db.exists(self.ref_doctype, self.ref_name)
		):
			return
		decider = self.decided_by or frappe.session.user
		# TASK 4: the trace comment is an ignore_permissions write attributed to
		# the decider on a user-controllable ref_doctype/ref_name. Do not plant
		# an attributed comment on a document the decider cannot access — that
		# would be a permission-bypass write driven by attacker-chosen fields.
		# Skip the trace (the decision itself is still recorded) when the decider
		# lacks read+write on the target.
		if not (
			frappe.has_permission(self.ref_doctype, "read", self.ref_name, user=decider)
			and frappe.has_permission(self.ref_doctype, "write", self.ref_name, user=decider)
		):
			return
		comment = frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Comment",
				"reference_doctype": self.ref_doctype,
				"reference_name": self.ref_name,
				"content": (
					f"Approval <b>{self.name}</b> ({frappe.utils.escape_html(self.title or '')}): "
					f"<b>{self.status}</b> - {frappe.utils.escape_html(self.decision or '')}"
				),
				"comment_email": decider,
				"comment_by": frappe.utils.get_fullname(decider),
			}
		)
		comment.flags.ignore_permissions = True
		comment.insert(ignore_permissions=True)
		# owner = the approver, so the trace reads as their action.
		frappe.db.set_value("Comment", comment.name, "owner", decider, update_modified=False)
		self.db_set("trace_comment_added", 1, update_modified=False)
