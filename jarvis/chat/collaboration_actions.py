"""Actor-only shared action boundary; legacy owner confirmation stays unchanged."""

import hashlib
from contextlib import ExitStack

import frappe
from frappe.utils import cint

from jarvis.chat import collaboration as c
from jarvis.chat.collaboration_bindings import require_live
from jarvis.chat.collaboration_callbacks import permitted_callback
from jarvis.chat.collaboration_permissions import actor_permissions
from jarvis.chat.collaboration_turn import turn_context
from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_access

FIELDS = (
	"actor",
	"collaboration_binding",
	"collaboration_grant_revision",
	"collaboration_epoch",
	"collaboration_call_key",
)


def call_key(permit):
	return hashlib.sha256((permit.turn.binding.name + "\0" + permit.call_id).encode()).hexdigest()


def park_identity(conversation, owner, executor, tool, run_id, kind):
	if not c.is_collaborative(conversation):
		return {}
	permit = permitted_callback(conversation, tool)
	if not permit:
		c._deny()
	binding = permit.turn.binding
	require_live(binding)
	if kind != "chat" or owner != binding.actor or executor != binding.actor or run_id != permit.turn.run_id:
		c._deny()
	return {
		"actor": binding.actor,
		"collaboration_binding": binding.name,
		"collaboration_grant_revision": binding.grant_revision,
		"collaboration_epoch": binding.conversation_epoch,
		"collaboration_call_key": call_key(permit),
	}


def validate_park_identity(doc):
	identity = park_identity(doc.conversation, doc.owner_user, doc.exec_user, doc.tool, doc.run_id, doc.kind)
	for field in FIELDS:
		if (doc.get(field) or "") != (identity.get(field) or ""):
			c._deny()


def _row_binding(row, conversation, actor):
	if (
		not row
		or row.kind != "chat"
		or row.conversation != conversation
		or row.owner_user != actor
		or row.exec_user != actor
		or row.get("actor") != actor
		or not row.get("collaboration_binding")
	):
		c._deny()
	key = frappe.db.get_value(c.BINDING, row.collaboration_binding, "session_key")
	binding = c.resolve_binding(key)
	if (
		binding.actor != actor
		or binding.conversation != conversation
		or cint(row.get("collaboration_grant_revision")) != binding.grant_revision
		or cint(row.get("collaboration_epoch")) != binding.conversation_epoch
	):
		c._deny()
	return binding


@frappe.whitelist(methods=["POST"])
def confirm_action(conversation: str, action: str) -> dict:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	actor = frappe.session.user
	from jarvis.chat.pending_actions import _execute, _store

	initial = _store.get_row(action)
	binding = _row_binding(initial, conversation, actor)
	with ExitStack() as contexts:

		def authorize_claim():
			require_live(binding)
			contexts.enter_context(actor_permissions(actor))
			turn = frappe.db.get_value(
				"Jarvis Chat Turn",
				initial.run_id,
				["conversation", "actor", "collaboration_binding", "collaboration_send_started_at"],
				as_dict=True,
				for_update=True,
			)
			if (
				not turn
				or not turn.collaboration_send_started_at
				or (turn.conversation, turn.actor, turn.collaboration_binding)
				!= (conversation, actor, binding.name)
			):
				c._deny()
			contexts.enter_context(turn_context(binding, initial.run_id))

		def authorize_row(row):
			if _row_binding(row, conversation, actor) != binding or row.run_id != initial.run_id:
				c._deny()

		return _execute._execute_locked(
			action,
			actor,
			None,
			defer_continuation=False,
			batch_id=None,
			arm=None,
			lock_authorizer=authorize_claim,
			row_authorizer=authorize_row,
		)


@frappe.whitelist(methods=["POST"])
def discard_action(conversation: str, action: str) -> dict:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	actor = frappe.session.user
	from jarvis.chat.pending_actions import _lifecycle, _store

	initial = _store.get_row(action)
	binding = _row_binding(initial, conversation, actor)

	def authorize_row(row):
		if _row_binding(row, conversation, actor) != binding or row.run_id != initial.run_id:
			c._deny()

	with turn_context(binding, initial.run_id):
		return _lifecycle.discard(
			action,
			kind="chat",
			conversation=conversation,
			lock_authorizer=lambda: require_live(binding),
			row_authorizer=authorize_row,
		)
