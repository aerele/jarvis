"""Persist tool ownership from a run event or the plugin callback, in either order."""

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


def bind_to_live_reply(conversation: str, tool_call_id: str | None) -> None:
	"""Bind a tool that reached the bench through the plugin callback to the reply of
	the turn that is running now. Joins the caller's transaction, like ``record_tool_owner``.

	A Claude CLI turn sends no tool events, so ``record_tool_owner`` is never fed from
	the run for its tools, and the callback id (``X-Jarvis-Tool-Call-Id``) is the only
	one there is. Admission dispatches at most one turn per conversation; a follow-up
	sent meanwhile waits as queued and cannot run tools, so only a dispatched turn is
	looked at. The bench saves the receipt before the agent gets the tool result, so
	the callback lands while that turn still runs. A chat with no turn row (the legacy
	path) stays unbound.
	"""
	if not tool_call_id:
		return
	from jarvis.chat import turn_state

	running = turn_state.unfinished_turn(
		conversation, states=(*turn_state.EPOCH_INFLIGHT_STATES, "finalizing", "recovering")
	)
	if running:
		reply = frappe.db.get_value(turn_state.TURN, running[0], "assistant_message")
		record_tool_owner(conversation, reply, tool_call_id)
