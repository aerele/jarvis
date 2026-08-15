"""``jarvis__save_dashboard`` — chat/builder build → saved Jarvis Dashboard (#884).

The dashboards flow no longer rides the openclaw hosted-canvas mechanism (the
gateway fetch that #880 had to defend against). The agent authors the complete
self-contained dashboard HTML and delivers it HERE, in the same "Option A" wire
``save_agent_dashboard`` already proved for delegate runs: persist the agent's
OWN document directly, so the bench never fetches a canvas back out of the
container gateway.

Identity mirrors the other session-bound tools: the conversation is resolved
from the CALLER's ``session_key`` (``Jarvis Conversation.session_key`` — never a
model-supplied id), so an agent can only ever save into its own conversation.
All payload validation (field allow-list, sources parsing from the HTML's
``jarvis-sources`` block, the 1MB html cap, the save-time theme validator,
scope/permission gates) is delegated to ``dashboards_api.save_dashboard`` — one
save path, whether the click or the agent does it. Validation failures come
back as ``InvalidArgumentError`` so the model reads the validator's message and
re-saves a fixed document instead of dying on a 500.

On success a ``{"kind": "dashboard"}`` realtime frame tells the builder page to
render the saved row (its ``loadEdit`` path — no canvas frame involved).
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError

CONVERSATION = "Jarvis Conversation"
DASHBOARD = "Jarvis Dashboard"


def save_dashboard(
	html: str,
	dashboard_title: str | None = None,
	name: str | None = None,
	theme: str | None = None,
	description: str | None = None,
) -> dict:
	"""Persist the authored dashboard HTML as a real ``Jarvis Dashboard``.

	Args:
	  html: the FULL self-contained HTML document (inline CSS/JS only; live
	    sources declared in the ``jarvis-sources`` script block are parsed out
	    server-side).
	  dashboard_title: required on the first save of a conversation's dashboard.
	  name: the saved dashboard's name, required when revising (returned by the
	    first call — pass it back on every iteration).
	  theme: Jarvis | Insight | Claude | Graphite | Custom (defaults server-side).
	  description: optional one-line summary.

	Returns ``{dashboard, dashboard_title, dashboard_type, theme}``.
	"""
	from jarvis.chat import dashboards_api
	from jarvis.chat.events import publish_to_user
	from jarvis.tools._agent_run_ctx import get_session_key

	if not (html or "").strip():
		raise InvalidArgumentError("save_dashboard requires a non-empty html document")

	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError(
			"save_dashboard must be called from a chat agent session "
			"(no session_key in context)"
		)

	conv = frappe.db.get_value(
		CONVERSATION,
		{"session_key": session_key},
		["name", "owner"],
		as_dict=True,
	)
	if not conv:
		raise InvalidArgumentError("no chat conversation is bound to this session")
	# The dispatcher already impersonated the mapped user; a mismatch here means
	# the session/conversation binding drifted — refuse rather than cross-save.
	if frappe.session.user != conv.owner:
		raise InvalidArgumentError("session user does not own this conversation")

	fields: dict = {"html": html, "source_conversation": conv.name}
	if name:
		row = frappe.db.get_value(
			DASHBOARD, name, ["name", "source_conversation"], as_dict=True
		)
		if not row:
			raise InvalidArgumentError(f"no saved dashboard named {name!r}")
		if (row.source_conversation or "") != conv.name:
			raise InvalidArgumentError(
				"that dashboard belongs to a different conversation; "
				"omit name to save a new one"
			)
		fields["name"] = name
	else:
		if not (dashboard_title or "").strip():
			raise InvalidArgumentError(
				"dashboard_title is required on the first save"
			)
	if dashboard_title:
		fields["dashboard_title"] = dashboard_title
	if theme:
		fields["theme"] = theme
	if description:
		fields["description"] = description

	try:
		result = dashboards_api.save_dashboard(payload=frappe.as_json(fields))
	except frappe.ValidationError as e:
		# Caps, scope, sources shape, and the theme validator all land here —
		# hand the model the message so it can fix the document and re-save.
		raise InvalidArgumentError(str(e))

	detail = (result or {}).get("data") or {}
	saved_name = detail.get("name")
	publish_to_user(
		conv.owner,
		{
			"kind": "dashboard",
			"conversation_id": conv.name,
			"name": saved_name,
			"dashboard_title": detail.get("dashboard_title"),
		},
	)
	# Same test-vs-live commit discipline as save_agent_dashboard: keep the
	# enclosing test transaction rollback-able; make the row durable live.
	if not frappe.flags.in_test:
		frappe.db.commit()

	return {
		"dashboard": saved_name,
		"dashboard_title": detail.get("dashboard_title"),
		"dashboard_type": detail.get("dashboard_type"),
		"theme": detail.get("theme"),
	}
