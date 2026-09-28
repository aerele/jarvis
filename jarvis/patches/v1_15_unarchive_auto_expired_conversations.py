"""Un-archive conversations the OLD idle sweep auto-archived.

Before this change, session_lifecycle's daily sweep ARCHIVED a conversation
idle past the retention window (status=Archived, auto_expired=1) and hid it from
the sidebar, on the promise of a "separate follow-on purge" that was never
built. The sweep now only frees the agent session and leaves the chat Active
and visible (nothing is hidden or deleted for idleness). Conversations the old
sweep already auto-archived would otherwise stay hidden forever with no way to
resume them, contradicting the new "your chats stay in your list" contract.

Restore them: flip the auto_expired=1 rows back to Active. Their session_key was
already freed by the old sweep, so the next message lazily mints a fresh session
(same as any idle chat now). Chats the USER archived by hand (auto_expired=0,
status=Archived) are left untouched - that was their choice.

Idempotent: after one run no auto_expired=1 row remains Archived.
"""

import frappe


def execute():
	# The auto_expired flag cleanly distinguishes system-archived (this sweep)
	# from user-archived rows, so we only restore the ones the system hid. Also
	# clear the now-dead auto_expired/expired_at markers on the restored rows: if
	# the user later hand-archives one, it must not read as system-archived to any
	# future purge keyed on that flag.
	frappe.db.sql(
		"""
		UPDATE `tabJarvis Conversation`
		SET status = 'Active', auto_expired = 0, expired_at = NULL
		WHERE status = 'Archived' AND auto_expired = 1
		"""
	)
