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
	affected = frappe.get_all("Jarvis Conversation", filters={"auto_apply": 1}, fields=["name", "status"])
	done = 0
	for row in affected:
		conv = row["name"]
		try:
			# Post the teaching notice only into a still-Active thread - an Archived thread
			# its owner may never reopen would just accrue noise. The stale flag is still
			# flipped for every affected conversation (hygiene + idempotency).
			if row.get("status") == "Active":
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
			# Commit per conversation so one bad row cannot abort the whole patch (and
			# block every later patch + the deploy). Idempotent: a re-run's selector
			# (auto_apply=1) no longer matches a committed conversation, so no duplicate
			# notice; a conversation that raised is rolled back (flag still 1) and retried.
			frappe.db.commit()
			done += 1
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="retire_admin_auto_apply: conversation failed",
				message=f"conversation={conv}\n{frappe.get_traceback()}",
			)
	if done:
		frappe.logger("jarvis.migrate").info(f"retired admin auto_apply on {done} conversation(s)")
