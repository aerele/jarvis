"""Take back chat-mined learned patterns that reached reviewers before their owner
answered (#18).

Chat mining used to surface every pattern it mined straight away. A pattern mined
from someone's chats now stays hidden until its owner answers its question
(``jarvis.learning.chat_pattern_privacy``); the reviewer endpoints and the
permission hooks already apply that rule live. This clears ``surfaced`` on the
Proposed ones still awaiting an answer, so the board's badge and counts match.
Decided rows are left as they are. Idempotent.
"""

import frappe
from pypika.terms import ExistsCriterion

from jarvis.learning.chat_pattern_privacy import CHAT_DETECTOR, JLP, QUESTION


def execute():
	if not (frappe.db.table_exists(JLP) and frappe.db.table_exists(QUESTION)):
		return
	p = frappe.qb.DocType(JLP)
	q = frappe.qb.DocType(QUESTION)
	answered = (
		frappe.qb.from_(q).select(q.name).where((q.source_pattern == p.name) & (q.status == "Answered"))
	)
	(
		frappe.qb.update(p)
		.set(p.surfaced, 0)
		.where(
			(p.detector_id == CHAT_DETECTOR)
			& (p.status == "Proposed")
			& (p.surfaced == 1)
			& ExistsCriterion(answered).negate()
		)
	).run()
