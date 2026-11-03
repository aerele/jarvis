"""Projected common history; no participant is entitled to another actor's raw rows."""

import json
from dataclasses import dataclass

import frappe
from frappe.utils import cint

from jarvis.chat import collaboration as c
from jarvis.chat import sharing
from jarvis.chat.collaboration_access import _live_users, _lock_users
from jarvis.chat.collaboration_database import fresh_read
from jarvis.chat.error_taxonomy import headline_for
from jarvis.chat.share_projection import project_row, safe_value

DISCLOSURE = "Messages, replies, ERP data and files added here are visible to everyone with access to this chat. Jarvis uses your own permissions for new requests and actions."
_OLD_FILE = "/api/method/jarvis.chat.sharing.get_shared_file?"
_COMMON_FILE = "/api/method/jarvis.chat.collaboration_api.get_file?"


@dataclass
class Reader:
	doc: object
	user: str
	grant: object
	mode: str
	can_chat: bool


def authorize_read(conversation: str, *, user: str | None = None, extra_users=(), lock=True) -> Reader:
	user = user or frappe.session.user
	if not isinstance(conversation, str) or not frappe.db.exists(c.CONVERSATION, conversation):
		c._deny()
	doc = frappe.get_doc(c.CONVERSATION, conversation, for_update=lock)
	users = {doc.owner, user}
	locked = _lock_users(users | set(extra_users), lock=lock)
	grant = None
	if user != doc.owner:
		grant = frappe.db.get_value(
			c.SHARE,
			{"conversation": conversation, "user": user},
			["name", "access_mode", "access_revision"],
			as_dict=True,
			for_update=lock,
		)
		if not grant or not sharing.sharing_enabled() or not sharing._shareable(doc):
			c._deny()
	if not users.issubset(_live_users(users | set(extra_users), locked, lock=lock)):
		c._deny()
	mode = c.effective_mode(grant.access_mode) if grant else "Chat"
	eligible = (
		sharing.sharing_enabled()
		and sharing._shareable(doc)
		and not any(cint(doc.get(k)) for k in ("file_box", "skip_confirmation", "agent_initiated"))
		and not frappe.db.exists("Jarvis Macro Run", {"conversation": conversation})
	)
	return Reader(doc, user, grant, mode, bool(c.writes_ready() and mode == "Chat" and eligible))


def rewrite_common_files(value):
	"""Projection already scrubbed the values; only route group files for owners too."""
	result = [None]
	work = [(value, result, 0)]
	while work:
		item, target, key = work.pop()
		if isinstance(item, dict):
			target[key] = {}
			work.extend((v, target[key], k) for k, v in item.items())
		elif isinstance(item, list):
			target[key] = [None] * len(item)
			work.extend((v, target[key], k) for k, v in enumerate(item))
		else:
			target[key] = item.replace(_OLD_FILE, _COMMON_FILE) if isinstance(item, str) else item
	return result[0]


def _json(value):
	try:
		return json.loads(value) if isinstance(value, str) else value
	except (ValueError, RecursionError):
		return None


def _current_binding(name, reader):
	if not reader.can_chat or not name:
		return None
	binding = frappe.db.get_value(
		c.BINDING,
		name,
		["actor", "conversation", "share", "grant_revision", "conversation_epoch", "state"],
		as_dict=True,
	)
	if (
		not binding
		or binding.state != "Active"
		or (binding.actor, binding.conversation, cint(binding.conversation_epoch))
		!= (reader.user, reader.doc.name, cint(reader.doc.collaboration_epoch))
	):
		return None
	if (binding.share or "", cint(binding.grant_revision)) != (
		reader.grant.name if reader.grant else "",
		cint(reader.grant.access_revision) if reader.grant else 0,
	):
		return None
	return binding


def _control(row, reader):
	if row.get("actor") != reader.user or row.get("tool_status") != "pending":
		return None
	binding = _current_binding(row.get("collaboration_binding"), reader)
	if not binding:
		return None
	action = frappe.db.get_value(
		"Jarvis Pending Action",
		row.tool_call_id,
		[
			"name",
			"status",
			"actor",
			"exec_user",
			"owner_user",
			"conversation",
			"collaboration_binding",
			"collaboration_grant_revision",
			"collaboration_epoch",
			"run_id",
			"card",
			"summary",
			"tool",
		],
		as_dict=True,
	)
	if (
		not action
		or action.status != "Pending"
		or (
			action.actor,
			action.exec_user,
			action.owner_user,
			action.conversation,
			action.collaboration_binding,
			action.run_id,
			cint(action.collaboration_grant_revision),
			cint(action.collaboration_epoch),
		)
		!= (
			reader.user,
			reader.user,
			reader.user,
			reader.doc.name,
			row.collaboration_binding,
			row.collaboration_run_id,
			cint(binding.grant_revision),
			cint(binding.conversation_epoch),
		)
	):
		return None
	return {
		"action": action.name,
		"tool": action.tool,
		"card": rewrite_common_files(safe_value(_json(action.card), reader.doc.name)),
		"summary": safe_value(action.summary or "", reader.doc.name),
	}


def project_common_row(row, reader, labels):
	row = frappe._dict(row)
	for field in ("canvas", "steps", "live_steps", "live_step_times"):
		row[field] = _json(row.get(field))
	out = rewrite_common_files(project_row(row, reader.doc.name))
	actor = row.get("actor") or reader.doc.owner
	out["actor"] = {"id": actor, "name": labels.get(actor) or actor}
	if row.get("tool_status") == "pending":
		out.pop("awaiting_owner", None)
		out["awaiting_actor"] = out["actor"]
		control = _control(row, reader)
		if control:
			out["control"] = control
	return out


def _status_rows(doctype, filters, fields, *, state_field, active, order_by):
	# One active request and the bounded queue must survive terminal churn.
	return frappe.get_all(
		doctype, {**filters, state_field: ["in", active]}, fields, order_by=order_by, limit=26
	) + frappe.get_all(
		doctype, {**filters, state_field: ["not in", active]}, fields, order_by=order_by, limit=25
	)


@fresh_read
def read_conversation(conversation: str, *, limit=80, before=None, watch_run=None):
	reader = authorize_read(conversation, lock=False)
	length = max(1, min(100, cint(limit)))
	filters = {"conversation": conversation, "hidden": 0}
	if cint(before) > 0:
		filters["seq"] = ["<", cint(before)]
	rows = frappe.get_all("Jarvis Chat Message", filters, ["*"], order_by="seq desc", limit=length + 1)
	has_more = len(rows) > length
	rows = list(reversed(rows[:length]))
	turns, questions = [], []
	if c.schema_ready():
		turns = _status_rows(
			"Jarvis Chat Turn",
			{"conversation": conversation, "collaboration_binding": ["is", "set"]},
			[
				"name",
				"actor",
				"state",
				"seed_message",
				"assistant_message",
				"error",
				"cancel_reason",
				"collaboration_binding",
				"collaboration_send_started_at",
				"collaboration_retry_allowed",
			],
			state_field="state",
			active=[
				"queued",
				"preparing",
				"ready",
				"dispatching",
				"streaming",
				"recovering",
				"terminal_observed",
				"finalizing",
			],
			order_by="enqueued_at desc, name desc",
		)
		questions = _status_rows(
			"Jarvis Approval Request",
			{"conversation": conversation, "source": "Chat", "collaboration_binding": ["is", "set"]},
			[
				"name",
				"actor",
				"owner",
				"question",
				"options",
				"status",
				"collaboration_binding",
				"collaboration_run_id",
				"collaboration_grant_revision",
				"collaboration_epoch",
			],
			state_field="status",
			active=["Pending"],
			order_by="creation desc, name desc",
		)
		if watch_run and watch_run not in {row.name for row in turns}:
			turns.extend(
				frappe.get_all(
					"Jarvis Chat Turn",
					{
						"name": watch_run,
						"conversation": conversation,
						"collaboration_binding": ["is", "set"],
					},
					[
						"name",
						"actor",
						"state",
						"seed_message",
						"assistant_message",
						"error",
						"cancel_reason",
						"collaboration_binding",
						"collaboration_send_started_at",
						"collaboration_retry_allowed",
					],
					limit=1,
				)
			)
	actors = sorted(
		{reader.doc.owner} | {row.get("actor") or reader.doc.owner for row in [*rows, *turns, *questions]}
	)
	labels = (
		dict(frappe.db.get_values("User", {"name": ["in", actors]}, ["name", "full_name"])) if actors else {}
	)
	return {
		"conversation": {
			"name": conversation,
			"title": safe_value(reader.doc.title, conversation),
			"owner": reader.doc.owner,
			"owner_name": safe_value(labels.get(reader.doc.owner) or reader.doc.owner, conversation),
			"status": reader.doc.status,
			"is_owner": reader.user == reader.doc.owner,
			"access_mode": reader.mode,
			"can_chat": reader.can_chat,
			"activated": bool(cint(reader.doc.collaboration_active)),
			"notice": DISCLOSURE,
		},
		"messages": [project_common_row(row, reader, labels) for row in rows],
		"turns": [_turn_status(row, reader, labels) for row in turns],
		"questions": [_question(row, reader, labels) for row in questions],
		"has_more": has_more,
		"oldest_seq": rows[0].seq if rows else None,
	}


def _actor(actor, labels):
	return {"id": actor, "name": labels.get(actor) or actor}


def _turn_status(row, reader, labels):
	from jarvis.chat.collaboration_retry import allowed

	live = row.state in {"queued", "preparing", "ready", "dispatching", "streaming", "recovering"}
	own = row.actor == reader.user and bool(_current_binding(row.collaboration_binding, reader))
	return {
		"run_id": row.name,
		"state": row.state,
		"actor": _actor(row.actor, labels),
		"seed_message": row.seed_message,
		"assistant_message": row.assistant_message,
		"can_stop": bool(live and (reader.user == reader.doc.owner or own)),
		"can_retry": bool(own and allowed(row)),
		"error_headline": headline_for(row.error or row.cancel_reason)
		if row.error or row.cancel_reason
		else None,
	}


def _question(row, reader, labels):
	options = _json(row.options)
	result = {
		"run_id": row.collaboration_run_id,
		"actor": _actor(row.actor, labels),
		"question": safe_value(row.question or "", reader.doc.name),
		"options": safe_value(options if isinstance(options, list) else [], reader.doc.name),
		"status": row.status,
	}
	if row.actor == row.owner == reader.user and row.status == "Pending":
		binding = _current_binding(row.collaboration_binding, reader)
		if binding and (cint(row.collaboration_grant_revision), cint(row.collaboration_epoch)) == (
			cint(binding.grant_revision),
			cint(binding.conversation_epoch),
		):
			result["control"] = {"question": row.name}
	return result


@fresh_read
def read_tool_detail(message: str):
	row = frappe.db.get_value(
		"Jarvis Chat Message", message, ["conversation", "hidden", "role"], as_dict=True
	)
	if not row or row.hidden or row.role != "tool":
		c._deny()
	reader = authorize_read(row.conversation, lock=False)
	row = frappe.get_doc("Jarvis Chat Message", message).as_dict()
	if row.hidden or row.conversation != reader.doc.name or row.role != "tool":
		c._deny()
	labels = {row.actor or reader.doc.owner: frappe.utils.get_fullname(row.actor or reader.doc.owner)}
	return project_common_row(row, reader, labels)
