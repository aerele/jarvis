"""Recall what the agent knows about the CURRENT user.

Returns the current user's durable memory blob (empty string if none yet). Scope
is implicit - ``frappe.session.user``, resolved from the chat session by
``jarvis.api.call_tool`` and set via ``impersonate(user)`` - so there is no user
argument and no way to read another user's memory. Auto-injected at session start
(the bench-owned replacement for openclaw's shared MEMORY.md); also callable
mid-session for a fresh read.

Read via ``frappe.db.get_value`` filtered on the current user, so it never returns
another user's row regardless of doc permissions.
"""

import frappe


def recall() -> dict:
	content = frappe.db.get_value("Jarvis User Memory", {"user": frappe.session.user}, "content")
	return {"content": content or ""}
