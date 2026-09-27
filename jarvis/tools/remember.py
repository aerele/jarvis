"""Remember durable facts about the CURRENT user.

Persists the agent's consolidated memory for whoever it is helping now, as one
terse markdown blob (one line per fact) - the bench-owned, per-user replacement
for the shared-workspace ``MEMORY.md``. Scope is implicit: the current user
(``frappe.session.user``, resolved from the chat session by ``jarvis.api.call_tool``
and set via ``impersonate(user)``). There is NO user argument, so the model cannot
address another user's memory.

Full replace: pass the whole updated blob (the agent already has the current memory
in context from session-start injection). Capped to keep it terse and to bound the
context it later consumes. One row per user (unique ``user`` field), so concurrent
writes from the same user's two chats are last-write-wins; a stale-timestamp save is
transparently reloaded and retried rather than surfaced to the agent.
"""

import frappe
from frappe.exceptions import TimestampMismatchError

from jarvis.exceptions import InvalidArgumentError

MAX_BYTES = 8192


def remember(content: str) -> dict:
	if content is None:
		content = ""
	if not isinstance(content, str):
		raise InvalidArgumentError("content must be a string")
	size = len(content.encode("utf-8"))
	if size > MAX_BYTES:
		raise InvalidArgumentError(f"memory too large ({size} bytes; max {MAX_BYTES})")

	user = frappe.session.user
	name = frappe.db.get_value("Jarvis User Memory", {"user": user}, "name")
	if not name:
		frappe.get_doc({"doctype": "Jarvis User Memory", "user": user, "content": content}).insert(
			ignore_permissions=True
		)
		return {"saved": True, "bytes": size}

	# Existing row: full-replace the blob. Last-write-wins across a user's own
	# concurrent sessions; on a stale-timestamp collision, reload the fresh row and
	# retry rather than surface a TimestampMismatchError to the agent.
	doc = frappe.get_doc("Jarvis User Memory", name)
	for _attempt in range(3):
		doc.content = content
		try:
			doc.save(ignore_permissions=True)
			return {"saved": True, "bytes": size}
		except TimestampMismatchError:
			doc.reload()
	raise InvalidArgumentError("memory write conflicted repeatedly; please retry")
