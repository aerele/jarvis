"""Authenticated plugin execution after run correlation and live actor claims."""

import json

import frappe

from jarvis.chat.collaboration_actions import call_key
from jarvis.chat.collaboration_callbacks import callback_scope
from jarvis.chat.collaboration_tool_claims import claim, complete, receipt_id
from jarvis.chat.collaboration_tools import validate_targets
from jarvis.chat.scrub import scrub_for_share
from jarvis.exceptions import JarvisError


def dispatch(session_key: str, tool: str, raw_args, call_id: str) -> dict:
	from jarvis import api
	from jarvis.tools import _agent_run_ctx

	try:
		with callback_scope(session_key, tool, call_id) as permit:
			binding = permit.turn.binding
			args = api._parse_args(raw_args, tool)
			validate_targets(tool, args)
			stored_id = receipt_id(binding, permit.turn.run_id, call_id)
			# The conversation lock held by the claim serializes duplicates. A
			# durable pending action also covers a crash after park's commit but
			# before the callback's ordinary receipt was written.
			cached = frappe.db.get_value(
				"Jarvis Chat Message",
				{
					"conversation": binding.conversation,
					"collaboration_binding": binding.name,
					"collaboration_run_id": permit.turn.run_id,
					"tool_call_id": stored_id,
				},
				["name", "tool_result"],
				as_dict=True,
			)
			if cached and cached.tool_result:
				complete(permit, cached.name)
				stored = json.loads(cached.tool_result)
				from jarvis.chat.source_receipts import SOURCE_TOOLS, is_source_receipt

				if tool in SOURCE_TOOLS and is_source_receipt(stored):
					return api._error(
						"SourceReadRequired",
						"Source values are not retained. Make a new read call; current permissions and source versions will be checked again.",
					)
				return scrub_for_share(stored)
			existing = frappe.db.get_value(
				"Jarvis Pending Action", {"collaboration_call_key": call_key(permit)}, "status"
			)
			if existing:
				return {
					"ok": True,
					"data": {
						"pending_confirmation": existing == "Pending",
						"status": existing,
						"message": "This call already proposed an action. Wait for its human decision; do not propose or execute it again.",
					},
				}
			if not claim(permit):
				return api._error(
					"ToolRunning",
					"This tool call was already claimed. Wait for its result; do not execute it again.",
				)
			_agent_run_ctx.set_session_key(session_key)
			try:
				with api._agent_read_time_limit(tool):
					result = scrub_for_share(api._run_tool(tool, args, conversation=binding.conversation))
				if result.get("ok"):
					result["data"], _event = api.enforce_result_budget(result.get("data"), tool=tool)
				api.persist_tool_receipt(
					binding.conversation, tool, scrub_for_share(args), result, tool_call_id=call_id
				)
				message = frappe.db.get_value(
					"Jarvis Chat Message",
					{
						"conversation": binding.conversation,
						"collaboration_binding": binding.name,
						"collaboration_run_id": permit.turn.run_id,
						"tool_call_id": stored_id,
					},
					"name",
				)
				complete(permit, message)
				return result
			finally:
				_agent_run_ctx.clear_session_key()
				_agent_run_ctx.take_armed_by_macro()
				_agent_run_ctx.take_armed_by_skill()
				_agent_run_ctx.take_request_autorun_applied()
	except frappe.PermissionError:
		frappe.local.response.http_status_code = 403
		return api._error(
			"PermissionError", "This shared tool call is no longer authorized for its initiating user."
		)
	except JarvisError as exc:
		return api._error(type(exc).__name__, scrub_for_share(str(exc)))
