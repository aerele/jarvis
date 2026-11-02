"""Explicit shared-write authorization; legacy owner-only APIs stay unchanged."""

import json
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cint

from jarvis.chat import collaboration as c
from jarvis.chat import sharing, txn
from jarvis.permissions import JARVIS_ACCESS_ROLES, refuse_in_tool_dispatch, require_jarvis_access


@dataclass(frozen=True)
class Participant:
	conversation: str
	actor: str
	owner: str
	share: str | None
	grant_revision: int
	conversation_epoch: int


def _refuse(message="You cannot chat in this shared conversation."):
	frappe.throw(_(message), frappe.PermissionError)


def _lock_users(users: set[str], *, lock=True):
	return frappe.db.get_values(
		"User",
		{"name": ["in", sorted(users)]},
		["name", "enabled", "user_type"],
		as_dict=True,
		order_by="name",
		for_update=lock,
	)


def _live_users(users: set[str], rows, *, lock=True) -> set[str]:
	"""Read live roles after grants, matching membership's Conv/User/Share/Role order."""
	roles = set(
		frappe.db.get_values(
			"Has Role",
			{"parent": ["in", sorted(users)], "parenttype": "User", "role": ["in", JARVIS_ACCESS_ROLES]},
			"parent",
			pluck=True,
			order_by="parent, name",
			for_update=lock,
		)
	)
	return {
		row.name
		for row in rows
		if row.enabled
		and row.user_type == "System User"
		and row.name in roles
		and row.name not in {"Guest", "Administrator"}
	}


def require_eligible(doc):
	if (
		not sharing.sharing_enabled()
		or not sharing._shareable(doc)
		or any(cint(doc.get(flag)) for flag in ("file_box", "skip_confirmation", "agent_initiated"))
		or frappe.db.exists("Jarvis Macro Run", {"conversation": doc.name})
	):
		_refuse("This conversation cannot accept shared chat messages.")


def authorize(conversation: str, *, actor: str | None = None) -> Participant:
	"""Server-only entry; a worker supplies its immutable binding's actor.

	No endpoint exposes the actor argument. Callers retain the transaction until
	claiming dispatch, so a concurrent downgrade has a defined before/after fence.
	"""
	c.require_writes_ready()
	actor = actor or frappe.session.user
	if not isinstance(conversation, str) or not frappe.db.exists(c.CONVERSATION, conversation):
		_refuse()
	doc = frappe.get_doc(c.CONVERSATION, conversation, for_update=True)
	require_eligible(doc)
	users = {actor, doc.owner}
	locked_users = _lock_users(users)
	grant = None
	if actor != doc.owner:
		grant = frappe.db.get_value(
			c.SHARE,
			{"conversation": conversation, "user": actor},
			["name", "access_mode", "access_revision"],
			as_dict=True,
			for_update=True,
		)
		if not grant or c.effective_mode(grant.access_mode) != "Chat":
			_refuse()
	if not users.issubset(_live_users(users, locked_users)):
		_refuse()
	return Participant(
		conversation=conversation,
		actor=actor,
		owner=doc.owner,
		share=grant.name if grant else None,
		grant_revision=cint(grant.access_revision) if grant else 0,
		conversation_epoch=cint(doc.get("collaboration_epoch")),
	)


@frappe.whitelist(methods=["POST"])
def set_access(conversation: str, user: str, access_mode: str) -> dict:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	if not isinstance(access_mode, str) or access_mode not in {"View", "Chat"}:
		frappe.throw(_("Choose Can view or Can chat."), frappe.ValidationError)
	if not isinstance(user, str) or not user:
		frappe.throw(_("Choose a shared recipient."), frappe.ValidationError)
	sharing._get_owned_conversation(conversation)
	return txn.replay_on_conflict(
		lambda: _set_access(conversation, user, access_mode), label="collaboration.access"
	)


def _set_access(conversation: str, user: str, mode: str) -> dict:
	doc = frappe.get_doc(c.CONVERSATION, conversation, for_update=True)
	if doc.owner != frappe.session.user:
		_refuse("Only the owner can change chat access.")
	users = {doc.owner, user}
	locked_users = _lock_users(users)
	row = frappe.db.get_value(
		c.SHARE,
		{"conversation": conversation, "user": user},
		["name", "access_mode", "access_revision"],
		as_dict=True,
		for_update=True,
	)
	if not row:
		_refuse("Share this conversation with the recipient first.")
	eligible = _live_users(users, locked_users)
	old_mode = c.effective_mode(row.access_mode)
	revision = cint(row.access_revision)
	if old_mode == mode:
		return {"user": user, "access_mode": mode, "access_revision": revision}
	if mode == "Chat":
		c.require_writes_ready()
		require_eligible(doc)
		if not {doc.owner, user}.issubset(eligible) or user == doc.owner:
			_refuse()
	revision = max(1, revision) + 1
	# Intentional trusted update: the ORM continues to reject mode/revision
	# writes, including generic REST and Administrator saves.
	frappe.db.set_value(c.SHARE, row.name, {"access_mode": mode, "access_revision": revision})
	sharing._invalidate_change(conversation)
	owner = doc.owner

	def committed():
		from jarvis.chat.events import publish_to_user

		sharing._audit("access_changed", conversation, [user], actor=owner)
		for recipient in sorted({owner, user}):
			publish_to_user(recipient, {"kind": "share:changed", "conversation_id": conversation})

	frappe.db.after_commit.add(committed)
	return {"user": user, "access_mode": mode, "access_revision": revision}


def _access_metadata(doc):
	from jarvis.chat.collaboration_read import DISCLOSURE

	result = sharing._get_shares(doc, for_update=True)
	modes = {}
	if all(frappe.db.has_column(c.SHARE, field) for field in ("access_mode", "access_revision")):
		modes = {
			row.user: row
			for row in frappe.db.get_values(
				c.SHARE,
				{"conversation": doc.name},
				["user", "access_mode", "access_revision"],
				as_dict=True,
				for_update=True,
				order_by="name",
			)
		}
	for user in result["users"]:
		row = modes.get(user["name"], {})
		user.update(
			access_mode=c.effective_mode(row.get("access_mode")),
			access_revision=cint(row.get("access_revision")),
		)
	result.update(notice=DISCLOSURE, can_configure_chat=False)
	if c.writes_ready():
		try:
			require_eligible(doc)
			result["can_configure_chat"] = True
		except frappe.PermissionError:
			frappe.clear_messages()
	return result


def _owner_locked(conversation):
	doc = frappe.get_doc(c.CONVERSATION, conversation, for_update=True)
	if doc.owner != frappe.session.user:
		_refuse("Only the owner can change chat access.")
	return doc


@frappe.whitelist(methods=["POST"])
def get_access(conversation: str) -> dict:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	sharing._get_owned_conversation(conversation)
	return txn.replay_on_conflict(
		lambda: _access_metadata(_owner_locked(conversation)), label="collaboration.access_metadata"
	)


@frappe.whitelist(methods=["POST"])
def set_recipients(conversation: str, recipients: str | list) -> dict:
	"""Explicit Save: membership and every requested mode succeed or roll back together."""
	refuse_in_tool_dispatch()
	require_jarvis_access()
	sharing._get_owned_conversation(conversation)
	if isinstance(recipients, str):
		try:
			recipients = json.loads(recipients)
		except (ValueError, RecursionError):
			frappe.throw("Recipients must be a list of users with Can view or Can chat.")
	if not isinstance(recipients, list):
		frappe.throw("Recipients must be a list of users with Can view or Can chat.")
	wanted = {}
	for item in recipients:
		if (
			not isinstance(item, dict)
			or set(item) != {"user", "access_mode"}
			or not isinstance(item.get("user"), str)
			or not item["user"]
			or item.get("access_mode") not in ("View", "Chat")
			or item["user"] in wanted
		):
			frappe.throw("Choose each recipient once, with Can view or Can chat.")
		wanted[item["user"]] = item["access_mode"]

	def unit():
		doc = _owner_locked(conversation)
		# Existing membership code owns eligibility, the 25-visible limit and
		# revocation cleanup. It does not commit; mode changes share its txn.
		sharing._set_shares(conversation, set(wanted))
		for user in sorted(wanted):
			_set_access(conversation, user, wanted[user])
		return _access_metadata(doc)

	try:
		return txn.replay_on_conflict(unit, label="collaboration.recipients")
	except Exception:
		frappe.db.rollback()
		raise
