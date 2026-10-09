"""A learned pattern mined from someone's chats stays theirs until they answer.

Chat mining reads every user's conversations and writes Jarvis Learned Pattern
rows from them, each with a private Personalise question to the chat's owner.
Reviewers (System Manager, Jarvis Admin, Skill Reviewer) must not see such a row
until its owner has answered that question: no one reads another user's chats
(``jarvis.chat.chat_permissions``), and a pattern inferred from them is their
content too.

"Awaiting owner" = ``detector_id`` is chat mining's and no linked question is
Answered. It is computed live, so rows mined before this rule are covered too.
Administrator is not restricted (as for the chat doctypes' row scoping).
"""

from __future__ import annotations

import frappe
from frappe.query_builder.functions import Coalesce
from pypika.terms import ExistsCriterion

JLP = "Jarvis Learned Pattern"
QUESTION = "Jarvis Personalise Question"
CHAT_DETECTOR = "chat-context"  # chat_mining.DETECTOR_ID


def awaiting_owner(name: str) -> bool:
	"""True while ``name`` is a chat-mined pattern its owner has not answered."""
	if frappe.db.get_value(JLP, name, "detector_id") != CHAT_DETECTOR:
		return False
	return not _answered(name)


def _answered(name: str) -> bool:
	return bool(frappe.db.exists(QUESTION, {"source_pattern": name, "status": "Answered"}))


def reviewable(p):
	"""frappe.qb criterion on Learned Pattern table ``p``: the rows reviewers may see."""
	q = frappe.qb.DocType(QUESTION)
	answered = (
		frappe.qb.from_(q).select(q.name).where((q.source_pattern == p.name) & (q.status == "Answered"))
	)
	return (Coalesce(p.detector_id, "") != CHAT_DETECTOR) | ExistsCriterion(answered)


def require_reviewable(name: str) -> None:
	"""Reviewer endpoints: an awaiting-owner pattern does not exist for them."""
	if awaiting_owner(name):
		raise frappe.DoesNotExistError(frappe._("{0} {1} not found").format(frappe._(JLP), name))


def release_for_question(question: str) -> None:
	"""The owner answered ``question``: surface its chat-mined pattern to reviewers."""
	pattern = frappe.db.get_value(QUESTION, question, "source_pattern")
	if not pattern:
		return
	row = frappe.db.get_value(JLP, pattern, ["detector_id", "status", "surfaced"], as_dict=True)
	if row and row.detector_id == CHAT_DETECTOR and row.status == "Proposed" and not row.surfaced:
		frappe.db.set_value(
			JLP, pattern, {"surfaced": 1, "surfaced_at": frappe.utils.now_datetime()}, update_modified=False
		)


def pattern_query_conditions(user: str | None = None) -> str:
	"""``permission_query_conditions`` for Jarvis Learned Pattern (Desk lists, REST)."""
	if (user or frappe.session.user) == "Administrator":
		return ""
	return (
		f"(ifnull(`tab{JLP}`.`detector_id`, '') != {frappe.db.escape(CHAT_DETECTOR)}"
		f" or exists (select 1 from `tab{QUESTION}` q where q.`source_pattern` = `tab{JLP}`.`name`"
		" and q.`status` = 'Answered'))"
	)


def has_pattern_permission(doc, ptype=None, user=None, **kwargs) -> bool:
	"""``has_permission`` for Jarvis Learned Pattern: refuse an awaiting-owner row."""
	if (user or frappe.session.user) == "Administrator":
		return True
	if doc.get("detector_id") != CHAT_DETECTOR or not doc.get("name"):
		return True
	return _answered(doc.name)
