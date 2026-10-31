"""Trusted transactional creation and persistence of collaborative identities.

No RPCs or commits here: admission owns shard/conversation locks and atomically
commits the binding, authentication row, seed and durable Turn. Public endpoints
never accept an actor, binding, session key, or settlement flag from a client.
"""

import re

import frappe
from frappe import _
from frappe.utils import cint

from jarvis._session import impersonate
from jarvis.chat import collaboration as c
from jarvis.chat.collaboration_access import authorize

MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"


def _require_drained(conversation: str):
	from jarvis.chat.admission import _legacy_turn_jobs

	legacy, _ids, complete = _legacy_turn_jobs()
	if legacy or not complete:
		# No-Turn legacy jobs are invisible to the durable-turn inventory. First
		# activation waits for a complete, clear site queue scan; later shared
		# admissions use the existing canonical per-conversation machine.
		frappe.throw(
			_(
				"Shared chat cannot start until earlier requests have finished and the queue can be checked. Please retry in a moment."
			)
		)
	if (
		frappe.db.exists(
			TURN, {"conversation": conversation, "state": ["not in", ["done", "errored", "cancelled"]]}
		)
		or frappe.db.exists(MSG, {"conversation": conversation, "streaming": 1})
		or frappe.db.exists(MSG, {"conversation": conversation, "recovering": 1})
		or frappe.db.exists(MSG, {"conversation": conversation, "tool_status": "pending"})
		or frappe.db.exists(
			"Jarvis Pending Action",
			{"conversation": conversation, "status": ["in", ["Pending", "Executing"]]},
		)
		or frappe.db.exists("Jarvis Approval Request", {"conversation": conversation, "status": "Pending"})
		or frappe.db.get_value(c.CONVERSATION, conversation, "pending_agent_notes")
	):
		frappe.throw(
			_("Finish or cancel the current request and pending actions before starting shared chat.")
		)


def create_binding(conversation: str, logical_run_id: str, *, actor: str) -> c.ActorBinding:
	"""Called inside canonical admission AFTER capacity checks, before seed insert."""
	if not isinstance(logical_run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", logical_run_id):
		frappe.throw(_("Invalid request identifier."), frappe.ValidationError)
	participant = authorize(conversation, actor=actor)
	if frappe.db.exists(c.BINDING, {"logical_run_id": logical_run_id}):
		raise frappe.DuplicateEntryError("A logical request already has an immutable actor binding.")
	if not c.is_collaborative(conversation):
		_require_drained(conversation)
		epoch = max(1, participant.conversation_epoch + 1)
		frappe.db.set_value(
			c.CONVERSATION, conversation, {"collaboration_active": 1, "collaboration_epoch": epoch}
		)
	else:
		epoch = participant.conversation_epoch
	# Resolve the actor's role profile without an RPC. The runtime floor attests
	# isolation for every addressed profile, not just the primary agent.
	settings = frappe.get_cached_doc("Jarvis Settings")
	choice = None
	if cint(settings.get("enable_role_profiles")):
		from jarvis.chat.api import _pushed_profile_slugs
		from jarvis.chat.collaboration_permissions import actor_permissions
		from jarvis.chat.role_profiles import resolve_profile

		with actor_permissions(actor):
			resolved = resolve_profile(actor)
			if resolved.agent_id in _pushed_profile_slugs(settings):
				choice = resolved
	agent_id = choice.agent_id if choice and choice.agent_id else "main"
	# A subagent session supports persisted native-tool restrictions and excludes
	# private workspace bootstrap. The dashboard namespace keeps lifecycle sweeps.
	key = f"agent:{agent_id}:subagent:dashboard:collab:{logical_run_id}"
	row = frappe.get_doc(
		{
			"doctype": c.BINDING,
			"conversation": conversation,
			"actor": actor,
			"logical_run_id": logical_run_id,
			"session_key": key,
			"share": participant.share,
			"grant_revision": participant.grant_revision,
			"conversation_epoch": epoch,
		}
	)
	row.flags.jarvis_collaboration_write = True
	row.insert(ignore_permissions=True)
	auth = frappe.get_doc(
		{
			"doctype": c.SESSION,
			"session_key": key,
			"user": actor,
			"collaboration_binding": row.name,
			"chat_device_id": settings.get("chat_device_id") or "",
			"profile_agent_id": choice.agent_id if choice else "",
			"profile_tier": choice.tier if choice else "full",
			"profile_sets": ",".join(choice.set_keys) if choice else "",
			"profile_n_skills": len(choice.skills or ()) if choice else 0,
			"profile_n_tools": choice.n_tools if choice else 0,
		}
	)
	auth.flags.jarvis_collaboration_write = True
	auth.insert(ignore_permissions=True)
	return c.resolve_binding(key)


def require_live(binding: c.ActorBinding):
	current = c.resolve_binding(binding.session_key)
	if current != binding:
		c._deny()
	participant = authorize(binding.conversation, actor=binding.actor)
	# A request may have read Active before a reset/drift fence committed. The
	# live grant alone cannot revive that old binding snapshot. These current
	# locks follow Conv/User/Share/Role and precede any Turn claim.
	row = frappe.db.get_value(
		c.BINDING, binding.name, ["name", *c.IDENTITY_FIELDS, "state"], as_dict=True, for_update=True
	)
	auth = frappe.db.get_value(
		c.SESSION,
		{"session_key": binding.session_key},
		["user", "collaboration_binding"],
		as_dict=True,
		for_update=True,
	)
	if (
		not row
		or row.state != "Active"
		or c.ActorBinding(**{field: row.get(field) for field in ("name", *c.IDENTITY_FIELDS)}) != binding
		or not auth
		or (auth.user, auth.collaboration_binding) != (binding.actor, binding.name)
	):
		c._deny()
	if (
		participant.share != binding.share
		or participant.grant_revision != binding.grant_revision
		or participant.conversation_epoch != binding.conversation_epoch
	):
		c._deny()
	return participant


def require_settlement(binding: c.ActorBinding, run_id: str, role: str):
	"""Allow only actual output of work already dispatched under THIS identity.

	This does not authorize any tool or confirmation. An untrusted insert cannot
	set the writer flag. Queued/preparing work has nothing dispatched to settle.
	"""
	if role not in {"assistant", "tool", "system"} or not run_id:
		c._deny()
	turn = frappe.db.get_value(
		TURN,
		run_id,
		["actor", "collaboration_binding", "conversation", "collaboration_send_started_at"],
		as_dict=True,
	)
	if (
		not turn
		or not turn.collaboration_send_started_at
		or turn.actor != binding.actor
		or turn.collaboration_binding != binding.name
		or turn.conversation != binding.conversation
	):
		c._deny()


def stamp(doc, binding: c.ActorBinding, *, run_id: str | None = None, settled_run_id: str | None = None):
	"""Server-only stamp BEFORE the controller validates the insert."""
	doc.actor = binding.actor
	doc.collaboration_binding = binding.name
	if doc.doctype == MSG:
		doc.collaboration_run_id = settled_run_id or run_id or binding.logical_run_id
	doc.flags.jarvis_collaboration_write = True
	doc.flags.jarvis_server_write = True
	if settled_run_id:
		require_settlement(binding, settled_run_id, doc.get("role"))
		doc.flags.jarvis_collaboration_settled_run = settled_run_id
	return doc


def insert_message(
	binding: c.ActorBinding, values: dict, *, run_id: str | None = None, settled_run_id: str | None = None
):
	if settled_run_id:
		require_settlement(binding, settled_run_id, values.get("role"))
	else:
		require_live(binding)
	# Same canonical conversation lock used by admission/prepare/receipt writers.
	frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
	seq = (
		frappe.db.sql(
			"SELECT MAX(seq) FROM `tabJarvis Chat Message` WHERE conversation=%s", (binding.conversation,)
		)[0][0]
		or 0
	) + 1
	# Metadata is never taken from values, even in trusted service callers.
	doc = frappe.get_doc({**values, "doctype": MSG, "conversation": binding.conversation, "seq": seq})
	stamp(doc, binding, run_id=run_id, settled_run_id=settled_run_id)
	with impersonate(binding.actor):
		doc.insert(ignore_permissions=True)
	return doc
