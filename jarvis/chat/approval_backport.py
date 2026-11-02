"""Approval guards for hotfix branches without the shared-chat runtime.

The optional schema marker is authoritative when present. These branches cannot
create collaborative approvals; reject them instead of importing that runtime.
"""

import frappe
from frappe.query_builder.functions import IfNull
from pypika.terms import ExistsCriterion, Not, ValueWrapper


def is_collaborative(conversation):
	if not conversation or not frappe.db.has_column("Jarvis Conversation", "collaboration_active"):
		return False
	return bool(
		frappe.utils.cint(frappe.db.get_value("Jarvis Conversation", conversation, "collaboration_active"))
	)


def require_legacy_conversation(conversation):
	if is_collaborative(conversation):
		frappe.throw("Not permitted", frappe.PermissionError)


def guard_record(doc):
	if any(
		doc.get(field)
		for field in (
			"collaboration_binding",
			"collaboration_run_id",
			"collaboration_grant_revision",
			"collaboration_epoch",
			"collaboration_question_key",
		)
	):
		frappe.throw("Not permitted", frappe.PermissionError)
	require_legacy_conversation(doc.get("conversation"))


def legacy_approval_condition(approval):
	if not frappe.db.has_column("Jarvis Conversation", "collaboration_active"):
		return ValueWrapper(1) == 1
	c = frappe.qb.DocType("Jarvis Conversation").as_("collaborative_thread")
	return Not(
		ExistsCriterion(
			frappe.qb.from_(c)
			.select(1)
			.where((c.name == approval.conversation) & (IfNull(c.collaboration_active, 0) != 0))
		)
	)
