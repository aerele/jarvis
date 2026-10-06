"""Jarvis-initiated (proactive) conversations.

Normally every conversation starts from the user. This lets Jarvis START one:
create a Conversation owned by the target user, post an assistant message, and
push a realtime ``conversation:new`` event so an open ``/jarvis`` tab surfaces
it immediately (sidebar refresh + a toast).

Triggers (a scheduled daily summary, a doc-event alert, an admin "message this
customer" action) just call :func:`start_conversation`. A trivial demo trigger
(:func:`demo_proactive`) is whitelisted so the flow can be exercised directly.
"""

import frappe

from jarvis.chat.events import publish_to_user
from jarvis.permissions import require_jarvis_access

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"


def start_conversation(message: str, *, title: str | None = None, user: str | None = None) -> dict:
	"""Create a new conversation for ``user`` seeded with an assistant message.

	Returns ``{ok, conversation, message}``. Publishes ``conversation:new`` to
	the user's realtime channel so an open SPA shows it without a reload.
	"""
	user = user or frappe.session.user
	message = (message or "").strip()
	if not message:
		frappe.throw("message is required")
	title = (title or "Message from Jarvis")[:140]

	# agent_initiated: Jarvis opened this thread; the user did not ask for it.
	conv = frappe.get_doc({"doctype": CONV, "title": title, "status": "Active", "agent_initiated": 1})
	conv.flags.ignore_permissions = True
	conv.insert()

	msg = frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": conv.name,
			"seq": 1,  # first message in a brand-new conversation
			"role": "assistant",
			"content": message,
		}
	)
	msg.flags.ignore_permissions = True
	msg.insert()

	# If posting on behalf of another user, hand ownership over (insert set the
	# owner to the caller). The Conversation is owner-gated, so this makes it
	# visible to the intended recipient only.
	if user != frappe.session.user:
		frappe.db.set_value(CONV, conv.name, "owner", user, update_modified=False)
		frappe.db.set_value(MSG, msg.name, "owner", user, update_modified=False)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist before realtime publish

	publish_to_user(
		user,
		{
			"kind": "conversation:new",
			"conversation_id": conv.name,
			"title": title,
			"preview": message[:90],
		},
	)
	return {"ok": True, "conversation": conv.name, "message": msg.name}


@frappe.whitelist()
def demo_proactive(message: str | None = None, title: str | None = None) -> dict:
	"""Test trigger: Jarvis proactively messages the current user.

	Gated (PART 1 TASK 1): this whitelisted endpoint hands the caller an OWNED
	conversation (via start_conversation's ignore_permissions insert) and could
	otherwise seed a chat for a user without Jarvis access. ``start_conversation``
	itself stays ungated — it is the internal seam the scheduler/doc-event/admin
	triggers call directly."""
	require_jarvis_access()
	return start_conversation(
		message
		or "Hi! Your monthly close is due in 3 days. Want me to prepare a draft "
		"summary of open invoices and pending entries?",
		title=title or "Monthly close reminder",
	)
