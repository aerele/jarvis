"""Composite index on Jarvis Chat Message for the File Box page's
latest-assistant-message subquery (chat-features migration, perf finding).
WHERE role='assistant' GROUP BY conversation ... MAX(seq) — (role, conversation,
seq) lets MariaDB satisfy it index-covered instead of a full scan + filesort."""

import frappe


def execute():
	frappe.db.add_index("Jarvis Chat Message", ["role", "conversation", "seq"], index_name="role_conv_seq")
