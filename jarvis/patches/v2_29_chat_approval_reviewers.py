"""Reset ambiguous legacy chat review shares; requesters must re-tag reviewers.

The Jarvis chat-sharing grants (Jarvis Conversation Share records, a standalone
DocType), File Box conversations and collaborative conversations are never
modified. The owner-approved reset deletes the Desk DocShares on Chat questions.
It also deletes a read-only Desk DocShare of the same conversation for the same
former reviewer, or for everyone, because its original purpose cannot be told
apart; an unrelated read-only share of that kind is removed too. The log records
counts only. No decision grants are backfilled. Rollback does not recreate the
removed access.
"""

import frappe
from frappe.query_builder.functions import Coalesce

BATCH = 500  # legacy approval shares read per page


def execute():
	from jarvis.chat.approval_reviewers import AR, CONV, scoped_condition

	a, c, s = (frappe.qb.DocType(dt) for dt in (AR, CONV, "DocShare"))
	counts = {"approval_shares_reset": 0, "conversation_shares_reset": 0}
	last = ""
	while True:
		rows = (
			frappe.qb.from_(s)
			.join(a)
			.on((s.share_doctype == AR) & (s.share_name == a.name))
			.left_join(c)
			.on(c.name == a.conversation)
			.select(s.name, s.user, s.everyone, a.conversation)
			.where(scoped_condition(a) & (Coalesce(c.collaboration_active, 0) == 0) & (s.name > last))
			.orderby(s.name)
			.limit(BATCH)
			.run(as_dict=True)
		)
		if not rows:
			break
		last = rows[-1].name
		for row in rows:
			if row.conversation:
				filters = {
					"share_doctype": CONV,
					"share_name": row.conversation,
					"everyone": int(bool(row.everyone)),
					"read": 1,
					"write": 0,
					"share": 0,
					"submit": 0,
				}
				if not row.everyone:
					filters["user"] = row.user
				mirrors = frappe.get_all("DocShare", filters=filters, pluck="name")
				if mirrors:
					frappe.db.delete("DocShare", {"name": ["in", mirrors]})
					counts["conversation_shares_reset"] += len(mirrors)
		frappe.db.delete("DocShare", {"name": ["in", [r.name for r in rows]]})
		counts["approval_shares_reset"] += len(rows)
	# Counts only (never names or users), visible in the migrate output and the log.
	print(f"Chat review legacy access reset: {counts}")
	frappe.logger("jarvis").info("Chat review legacy access reset: %s", counts)
	return counts
