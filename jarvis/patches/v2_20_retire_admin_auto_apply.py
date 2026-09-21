"""Retire admin Auto-Apply: notify affected conversations, clear stale flags.

Admin "Auto-Apply" (Jarvis Conversation.auto_apply) was removed in the
action-card overhaul - every real change now asks for confirmation, and typed
"confirm all" is the user-facing replacement. The auto_apply column is retained
but deprecated (nothing reads it for the gate any more). This patch, once:

  1. Posts a one-time teaching notice into each conversation that still had
     auto_apply=1, so its user learns the behaviour changed and how to bulk-
     approve going forward. Delivered as a durable assistant chat-message row,
     so it renders on every surface (SPA, PWA, Desk widget) with no client work.
  2. Flips those stale auto_apply=1 values to 0.

Idempotent: step 2 clears the selector, so a re-run finds no auto_apply=1
conversation and does nothing (no duplicate notice). The whole pass commits
once, so a mid-run failure rolls back cleanly and re-runs from scratch.
"""

import frappe

_NOTICE = (
	"Heads up: Auto-Apply has been turned off for this conversation. Every change now "
	'asks for confirmation before it runs. Tip: reply "confirm all" (or "do everything") '
	"to approve a whole request at once."
)


def execute():
	affected = frappe.get_all("Jarvis Conversation", filters={"auto_apply": 1}, pluck="name")
	for conv in affected:
		next_seq = (
			frappe.db.sql(
				"SELECT MAX(seq) FROM `tabJarvis Chat Message` WHERE conversation = %s",
				(conv,),
			)[0][0]
			or 0
		) + 1
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": conv,
				"seq": next_seq,
				"role": "assistant",
				"content": _NOTICE,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("Jarvis Conversation", conv, "auto_apply", 0, update_modified=False)
	frappe.db.commit()
