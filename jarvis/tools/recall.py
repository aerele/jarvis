"""Recall what the agent knows about the CURRENT user.

Returns the current user's durable memory blob (empty string if none yet) and its
``version``, which ``remember`` takes back as ``base_version`` so a save built on a
stale read is refused instead of overwriting newer facts (see ``_user_memory``).
Scope is implicit - ``frappe.session.user``, resolved from the chat session by
``jarvis.api.call_tool`` and set via ``impersonate(user)`` - so there is no user
argument and no way to read another user's memory. Auto-injected at session start
(the bench-owned, per-user replacement for the shared-workspace MEMORY.md); also callable
mid-session for a fresh read.

Read via ``frappe.db.get_value`` filtered on the current user, so it never returns
another user's row regardless of doc permissions.
"""

import frappe

from jarvis.tools import _user_memory


def recall() -> dict:
	content = frappe.db.get_value(_user_memory.DOCTYPE, {"user": frappe.session.user}, "content") or ""
	version = _user_memory.version_of(content)
	_user_memory.note_seen(version)
	return {"content": content, "version": version}
