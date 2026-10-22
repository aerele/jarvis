"""Correlate a plugin call with its observed gateway run, never the current run.

The plugin's logical session key is reused by confirmation children. Therefore a
session alone cannot prove which child originated a delayed callback. Only a
run-fenced gateway tool-start event supplies a capability; no args are retained.
"""

import json
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import txn
from jarvis.chat.collaboration_bindings import require_live
from jarvis.chat.collaboration_tools import tool_kind
from jarvis.chat.collaboration_turn import TurnContext

TURN = "Jarvis Chat Turn"
CALLBACK_STATES = ("dispatching", "streaming", "recovering")
MAX_CALLS = 256


@dataclass(frozen=True)
class CallbackPermit:
	turn: TurnContext
	tool: str
	call_id: str


_permit: ContextVar[CallbackPermit | None] = ContextVar("jarvis_collaboration_callback", default=None)


def permitted_callback(conversation: str, tool: str) -> CallbackPermit | None:
	permit = _permit.get()
	if (
		permit
		and permit.turn.binding.conversation == conversation
		and permit.tool == tool
		and frappe.session.user == permit.turn.binding.actor
	):
		return permit
	return None


def _calls(value):
	try:
		result = json.loads(value or "{}")
	except (ValueError, RecursionError, TypeError):
		c._deny()
	if not isinstance(result, dict) or len(result) > MAX_CALLS:
		c._deny()
	return result


def _name(value):
	if not isinstance(value, str):
		return ""
	return value.rsplit(".", 1)[-1].removeprefix("jarvis__")


def record_gateway_tool(run_id: str, call_id: str, tool: str) -> bool:
	"""Server-only; joins the event applier's canonical fence/transaction."""
	tool = _name(tool)
	if not isinstance(call_id, str) or not call_id or len(call_id) > 200 or tool_kind(tool) == "refused":
		return False
	conversation = frappe.db.get_value(TURN, run_id, "conversation")
	if not conversation:
		return False
	frappe.db.get_value(c.CONVERSATION, conversation, "name", for_update=True)
	row = frappe.db.get_value(
		TURN,
		run_id,
		["actor", "collaboration_binding", "conversation", "state", "collaboration_tools"],
		as_dict=True,
		for_update=True,
	)
	if not row or not row.collaboration_binding or row.state not in CALLBACK_STATES:
		return False
	key = frappe.db.get_value(c.BINDING, row.collaboration_binding, "session_key")
	binding = c.resolve_identity(key)
	if binding.actor != row.actor or binding.conversation != conversation:
		return False
	# A provider that reuses IDs cannot ambiguously assign an old callback to
	# a child. Read all this binding's maps CURRENT under the conversation lock.
	for other in frappe.db.get_values(
		TURN,
		{"collaboration_binding": binding.name},
		["name", "collaboration_tools"],
		as_dict=True,
		order_by="name",
		for_update=True,
	):
		if other.name != run_id and call_id in _calls(other.collaboration_tools):
			return False
	calls = _calls(row.collaboration_tools)
	if call_id in calls:
		return calls[call_id] == tool
	if len(calls) >= MAX_CALLS:
		return False
	calls[call_id] = tool
	frappe.db.set_value(
		TURN, run_id, "collaboration_tools", json.dumps(calls, separators=(",", ":")), update_modified=False
	)
	return True


def _proof(binding, tool, call_id, *, lock=False):
	rows = frappe.db.get_values(
		TURN,
		{"collaboration_binding": binding.name, "state": ["in", CALLBACK_STATES]},
		[
			"name",
			"actor",
			"conversation",
			"collaboration_tools",
			"collaboration_send_started_at",
			"cancel_requested",
		],
		as_dict=True,
		order_by="name",
		for_update=lock,
	)
	for row in rows:
		if (
			row.actor == binding.actor
			and row.conversation == binding.conversation
			and row.collaboration_send_started_at
			and not row.cancel_requested
			and _calls(row.collaboration_tools).get(call_id) == tool
		):
			return row.name
	return None


def authorize_callback(
	session_key: str, tool: str, call_id: str, *, wait_s=1.5, _permissions=None
) -> TurnContext:
	c.require_writes_ready()
	if tool_kind(tool) == "refused" or not isinstance(call_id, str) or not call_id or len(call_id) > 200:
		c._deny()
	binding = c.resolve_binding(session_key)
	metadata = frappe.db.get_value(
		c.BINDING, binding.name, ["runtime_session_id", "runtime_policy_hash"], as_dict=True
	)
	if not metadata.runtime_session_id or not metadata.runtime_policy_hash:
		c._deny()
	deadline = time.monotonic() + max(0, min(2, wait_s))
	while not _proof(binding, tool, call_id):
		if time.monotonic() >= deadline or not txn.replay_is_safe():
			c._deny()
		# No writes/callbacks to discard. Release the old HTTP snapshot so the
		# gateway event's committed capability can become visible. No locks held
		# while waiting and no tool runs during this bounded rendezvous.
		frappe.db.rollback()
		time.sleep(0.025)

	def claim():
		require_live(binding)
		if _permissions:
			_permissions(binding.actor)
		run_id = _proof(binding, tool, call_id, lock=True)
		if not run_id:
			c._deny()
		return TurnContext(binding, run_id)

	return txn.replay_on_conflict(claim, label="collaboration.callback")


@contextmanager
def callback_scope(session_key: str, tool: str, call_id: str, *, wait_s=1.5):
	"""Installed only by authenticated plugin dispatch, never by message writers.

	Fresh actor permissions are read after participant/grant locks and before the
	Turn claim. A retry closes the previous attempt's local cache/identity scope.
	"""
	from jarvis.chat.collaboration_permissions import actor_permissions
	from jarvis.chat.collaboration_turn import turn_context

	with ExitStack() as permissions:

		def load_permissions(actor):
			permissions.close()
			permissions.enter_context(actor_permissions(actor))

		turn = authorize_callback(session_key, tool, call_id, wait_s=wait_s, _permissions=load_permissions)
		token = _permit.set(CallbackPermit(turn, tool, call_id))
		try:
			with turn_context(turn.binding, turn.run_id):
				yield _permit.get()
		finally:
			_permit.reset(token)
