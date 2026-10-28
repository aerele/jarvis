"""Reset ambiguous legacy chat review shares; requesters must re-tag reviewers.

The owner approved removing correlated read-only Desk shares even when their
original purpose cannot be distinguished. Actual Jarvis chat-sharing grants and
File Box/collaborative conversations are never modified. No decision grants are
backfilled. Rollback does not recreate the removed access.
"""

import frappe
from frappe.query_builder.functions import Coalesce


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
			.limit(500)
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
	frappe.logger("jarvis").info("Chat review legacy access reset: %s", counts)
	return counts
