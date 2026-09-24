"""Persist tool ownership from a run event, independently of callback arrival order."""

import json

import frappe


def record_tool_owner(conversation: str, assistant_message: str | None, tool_call_id: str | None) -> None:
	"""Join the caller's transaction/fence; never commit or guess the active turn.

	The callback receipt can arrive before or after the event. Keeping IDs on
	the known assistant row allows either order and survives reload/replay.
	"""
	if not assistant_message or not isinstance(tool_call_id, str) or not tool_call_id:
		return
	rows = frappe.db.sql(
		"""SELECT tool_call_ids FROM `tabJarvis Chat Message`
		WHERE name=%s AND conversation=%s AND role='assistant' FOR UPDATE""",
		(assistant_message, conversation),
	)
	if not rows:
		return
	ids = json.loads(rows[0][0] or "[]")
	if tool_call_id not in ids:
		ids.append(tool_call_id)
		frappe.db.set_value(
			"Jarvis Chat Message", assistant_message, "tool_call_ids", json.dumps(ids), update_modified=False
		)
