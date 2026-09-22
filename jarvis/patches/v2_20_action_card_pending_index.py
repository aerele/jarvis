"""Composite index on Jarvis Chat Message for the hourly action-card health scan
(jarvis.chat.session_lifecycle.reconcile_action_cards).

That scan filters `role='tool' AND tool_status='pending' AND expires_at BETWEEN ..`
and orders by `expires_at DESC`. The doctype JSON only indexes `conversation` and
`tool_call_id`, so without this the scan is a full table scan + filesort of the
app's largest table, every hour. `(tool_status, expires_at)` turns it into a tight
range seek over just the handful of pending rows in the window and satisfies the
ORDER BY. (`role` is left out: `tool_status='pending'` only ever occurs on a
role='tool' action-row, so it is already the selective predicate.)

add_index is idempotent (no-ops when the index already exists), so this is safe to
re-run on an upgraded site.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Jarvis Chat Message"):
		return
	frappe.db.add_index(
		"Jarvis Chat Message", ["tool_status", "expires_at"], index_name="tool_status_expires"
	)
