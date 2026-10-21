"""Named, same-site, read-only chat shares. Existing owner gates stay intact."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, get_datetime

from jarvis.chat import txn
from jarvis.chat.api import _get_owned_conversation
from jarvis.chat.personalise_api import _single_bool
from jarvis.permissions import (
	JARVIS_ACCESS_ROLES,
	require_jarvis_access,
	require_jarvis_admin,
)

SHARE = "Jarvis Conversation Share"
CONVERSATION = "Jarvis Conversation"
MESSAGE = "Jarvis Chat Message"
MAX_RECIPIENTS = 25


@contextmanager
def share_write():
	previous = frappe.flags.get("jarvis_share_write")
	frappe.flags.jarvis_share_write = True
	try:
		yield
	finally:
		frappe.flags.jarvis_share_write = previous


def sharing_enabled() -> bool:
	return _single_bool("chat_sharing_enabled", True)


def _enabled_user(user: str) -> bool:
	if user in ("Guest", "Administrator"):
		return False
	# Read eligibility from this request's database snapshot, never the
	# shared roles cache: an older request must not repopulate revoked roles
	# and let a later recipient request through. Existing global gates remain.
	return bool(
		frappe.db.sql(
			"""SELECT u.name FROM `tabUser` u
			WHERE u.name=%s AND u.enabled=1 AND u.user_type='System User'
			AND EXISTS (SELECT 1 FROM `tabHas Role` r WHERE r.parent=u.name
			AND r.parenttype='User' AND r.role IN %s)""",
			(user, JARVIS_ACCESS_ROLES),
		)
	)


def _shareable(doc) -> bool:
	return bool(
		doc
		and doc.owner != "Administrator"
		and doc.status == "Active"
		and doc.get("origin_page") != "dashboards"
		and not cint(doc.get("file_box"))
		and frappe.db.exists(MESSAGE, {"conversation": doc.name})
	)


def _validate_addition(doc, user: str) -> None:
	if not sharing_enabled():
		frappe.throw(_("Sharing chats is turned off."), frappe.PermissionError)
	if not _shareable(doc) or not _enabled_user(doc.owner):
		frappe.throw(_("This chat cannot be shared."), frappe.PermissionError)
	if user == doc.owner or not _enabled_user(user):
		frappe.throw(_("Choose another enabled user with Jarvis access."), frappe.ValidationError)


def _hidden_reason(doc, recipient: str) -> str | None:
	if not sharing_enabled():
		return "sharing_off"
	if not _shareable(doc):
		return "chat_unavailable"
	if not _enabled_user(doc.owner):
		return "owner_unavailable"
	if not _enabled_user(recipient):
		return "recipient_unavailable"
	return None


def _visible_share(conversation: str, *, for_update: bool = False):
	doc = frappe.get_doc(CONVERSATION, conversation) if frappe.db.exists(CONVERSATION, conversation) else None
	if doc and doc.owner == frappe.session.user:
		return doc, None, "owner"
	if not sharing_enabled():
		return doc, None, "sharing_off"
	row = frappe.db.get_value(
		SHARE,
		{"conversation": conversation, "user": frappe.session.user},
		["name", "seen_marker"],
		as_dict=True,
		for_update=for_update,
	)
	if not row or not doc or _hidden_reason(doc, frappe.session.user):
		return doc, None, "not_shared"
	return doc, row, None


def _require_share(conversation: str):
	doc, row, reason = _visible_share(conversation)
	if reason:
		frappe.throw(_("This shared chat is unavailable."), frappe.PermissionError)
	return doc, row


def invalidate_conversation(conversation: str) -> None:
	"""Delete the raw, site-prefixed recipient-list key specified for PR 3."""
	cache = frappe.cache()
	cache.delete(cache.make_key(f"jarvis:share:rcpt:{conversation}"))


def invalidate_all_recipients() -> None:
	# Include negative entries for conversations with no stored shares.
	cache = frappe.cache()
	for key in cache.scan_iter(match=cache.make_key("jarvis:share:rcpt:*"), count=200):
		cache.delete(key)


def _invalidate_change(conversation: str) -> None:
	invalidate_conversation(conversation)
	frappe.db.after_commit.add(lambda: invalidate_conversation(conversation))
	frappe.db.after_rollback.add(lambda: invalidate_conversation(conversation))


def _audit(
	action: str, conversation: str | None = None, users: list[str] | None = None, *, actor: str | None = None
) -> None:
	frappe.logger("jarvis.share_audit").info(
		json.dumps(
			{
				"actor": actor or frappe.session.user,
				"action": action,
				"conversation": conversation,
				"users": users or [],
				"ok": True,
			}
		)
	)


def on_sharing_setting_changed(old, new) -> None:
	if bool(cint(old)) == bool(cint(new)):
		return
	invalidate_all_recipients()
	frappe.db.after_commit.add(invalidate_all_recipients)
	frappe.db.after_rollback.add(invalidate_all_recipients)
	_audit("sharing_enabled" if cint(new) else "sharing_disabled")


def _after_change(
	conversation: str, added: set[str], removed: set[str], *, action: str | None = None
) -> None:
	actor = frappe.session.user
	_invalidate_change(conversation)

	def committed():
		from jarvis.chat.events import publish_to_user

		for user in sorted(added | removed):
			publish_to_user(user, {"kind": "share:changed", "conversation_id": conversation})
		for user in sorted(added):
			_audit("share_added", conversation, [user], actor=actor)
		for user in sorted(removed):
			_audit(action or "share_removed", conversation, [user], actor=actor)

	frappe.db.after_commit.add(committed)


@frappe.whitelist()
def share_candidates(query: str = "") -> list[dict]:
	require_jarvis_access()
	query = (query or "").strip()[:140]
	return frappe.db.sql(
		"""SELECT u.name, u.full_name FROM `tabUser` u
		WHERE u.enabled=1 AND u.user_type='System User'
		AND u.name NOT IN %(exclude)s
		AND (u.name LIKE %(query)s OR u.full_name LIKE %(query)s)
		AND EXISTS (SELECT 1 FROM `tabHas Role` r WHERE r.parent=u.name
		AND r.parenttype='User' AND r.role IN %(roles)s)
		ORDER BY u.full_name, u.name LIMIT 20""",
		{
			"exclude": (frappe.session.user, "Guest", "Administrator"),
			"query": "%" + query + "%",
			"roles": JARVIS_ACCESS_ROLES,
		},
		as_dict=True,
	)


@frappe.whitelist()
def get_shares(conversation: str) -> dict:
	require_jarvis_access()
	doc = _get_owned_conversation(conversation)
	return _get_shares(doc)


def _get_shares(doc, *, for_update=False):
	rows = (
		frappe.db.sql(
			"SELECT user, creation FROM `tabJarvis Conversation Share` WHERE conversation=%s ORDER BY creation, name FOR UPDATE",
			(doc.name,),
			as_dict=True,
		)
		if for_update
		else frappe.get_all(
			SHARE, filters={"conversation": doc.name}, fields=["user", "creation"], order_by="creation, name"
		)
	)
	users = []
	for row in rows:
		reason = _hidden_reason(doc, row.user)
		users.append(
			{
				"name": row.user,
				"full_name": frappe.utils.get_fullname(row.user),
				"shared_on": row.creation,
				"hidden": bool(reason),
				"hidden_reason": reason,
			}
		)
	return {"enabled": sharing_enabled(), "users": users}


@frappe.whitelist(methods=["POST"])
def set_shares(conversation: str, users: str | list) -> dict:
	require_jarvis_access()
	_get_owned_conversation(conversation)
	if isinstance(users, str):
		try:
			users = json.loads(users)
		except ValueError:
			frappe.throw(_("Recipients must be a list of users."))
	if not isinstance(users, list) or any(not isinstance(user, str) or not user for user in users):
		frappe.throw(_("Recipients must be a list of users."))
	return txn.replay_on_conflict(lambda: _set_shares(conversation, set(users)), label="share.set")


def _set_shares(conversation: str, wanted: set[str]) -> dict:
	# Serialize the entire read/diff/write so duplicate requests cannot race
	# the unique constraint or exceed the cap.
	doc = frappe.get_doc(CONVERSATION, conversation, for_update=True)
	# The initial gate can precede a lock wait. Snapshot checking may reject
	# this lock with 1020; the endpoint replays the whole unit, reloading all
	# authorization and recipients, only if no earlier caller work is lost.
	if doc.owner != frappe.session.user:
		frappe.throw(_("Only the owner can share this chat."), frappe.PermissionError)
	# Synchronize additions with User saves/deletes before taking share-row
	# locks, including users removed while the initial owner gate was waiting.
	locked_users = {
		row.name: row
		for row in frappe.db.get_values(
			"User",
			filters={"name": ["in", sorted(wanted | {doc.owner})]},
			fieldname=["name", "enabled", "user_type"],
			as_dict=True,
			order_by="name",
			for_update=True,
		)
	}
	existing = {
		row.user: row.name
		for row in frappe.db.sql(
			"SELECT name, user FROM `tabJarvis Conversation Share` WHERE conversation=%s FOR UPDATE",
			(conversation,),
			as_dict=True,
		)
	}
	added, removed = wanted - existing.keys(), existing.keys() - wanted
	if added:
		# A roles-cache miss can read the earlier request snapshot even after
		# a User lock sees the latest row. Validate live role rows as well.
		current_roles = set(
			frappe.db.get_values(
				"Has Role",
				filters={
					"parent": ["in", sorted(wanted | {doc.owner})],
					"parenttype": "User",
					"role": ["in", JARVIS_ACCESS_ROLES],
				},
				fieldname="parent",
				pluck=True,
				for_update=True,
			)
		)
		for user in added | {doc.owner}:
			current = locked_users.get(user)
			if (
				not current
				or not current.enabled
				or current.user_type != "System User"
				or user not in current_roles
			):
				frappe.throw(_("Choose enabled users with Jarvis access."), frappe.PermissionError)
		for user in added:
			_validate_addition(doc, user)
		# Existing hidden grants are retained without consuming visible capacity.
		# User and role rows above are locked and read fresh for the whole request.
		visible = sum(
			1
			for user in wanted
			if user in current_roles
			and user in locked_users
			and locked_users[user].enabled
			and locked_users[user].user_type == "System User"
			and user not in {"Guest", "Administrator", doc.owner}
		)
		if visible > MAX_RECIPIENTS:
			frappe.throw(_("A chat can have at most 25 visible recipients."))
	# Validate the full request before deleting anything. Revoking requires no
	# enabled/shareable check and remains possible while sharing is switched off.
	for user in removed:
		_delete_share(existing[user])
	with share_write():
		for user in sorted(added):
			frappe.get_doc(
				{
					"doctype": SHARE,
					"conversation": conversation,
					"user": user,
					"shared_by": frappe.session.user,
				}
			).insert(ignore_permissions=True)
	if added or removed:
		_after_change(conversation, set(added), set(removed))
	return _get_shares(doc, for_update=True)


def _latest_activity(conversation: str):
	return frappe.db.sql(
		"SELECT MAX(modified) FROM `tabJarvis Chat Message` WHERE conversation=%s AND hidden=0",
		(conversation,),
	)[0][0]


def _mark_seen(name: str, latest) -> None:
	if latest is None:
		return
	current = frappe.db.get_value(SHARE, name, "seen_marker", for_update=True)
	if current is None or get_datetime(latest) > get_datetime(current):
		frappe.db.set_value(SHARE, name, "seen_marker", latest, update_modified=False)


@frappe.whitelist()
def list_shared_with_me() -> list[dict]:
	require_jarvis_access()
	if not sharing_enabled() or not _enabled_user(frappe.session.user):
		return []
	# Apply visibility before LIMIT so hidden shares cannot consume the fifty
	# slots. The owner role and message existence checks are indexed SQL
	# predicates, avoiding one conversation/user query per stored share.
	rows = frappe.db.sql(
		"""SELECT c.name, c.title, c.last_active_at,
		 COALESCE(NULLIF(o.full_name, ''), o.name) AS owner_name, s.seen_marker,
		 (SELECT MAX(m.modified) FROM `tabJarvis Chat Message` m
		  WHERE m.conversation=c.name AND m.hidden=0) AS latest
		 FROM `tabJarvis Conversation Share` s
		 JOIN `tabJarvis Conversation` c ON c.name=s.conversation
		 JOIN `tabUser` o ON o.name=c.owner
		 WHERE s.user=%(user)s AND c.status='Active'
		 AND COALESCE(c.origin_page, '') != 'dashboards' AND COALESCE(c.file_box, 0)=0
		 AND c.owner NOT IN ('Administrator', 'Guest') AND o.enabled=1 AND o.user_type='System User'
		 AND EXISTS (SELECT 1 FROM `tabHas Role` r WHERE r.parent=o.name
		  AND r.parenttype='User' AND r.role IN %(roles)s)
		 AND EXISTS (SELECT 1 FROM `tabJarvis Chat Message` m2 WHERE m2.conversation=c.name)
		 ORDER BY c.last_active_at DESC, s.name LIMIT 50""",
		{"user": frappe.session.user, "roles": JARVIS_ACCESS_ROLES},
		as_dict=True,
	)
	from jarvis.chat.share_projection import project_index_row

	return [project_index_row(row) for row in rows]


@frappe.whitelist()
def is_shared_with_me(conversation: str) -> bool:
	require_jarvis_access()
	return _visible_share(conversation)[2] is None


@frappe.whitelist(methods=["POST"])
def get_shared_conversation(
	conversation: str,
	limit: int = 80,
	before: int | None = None,
	after: int | None = None,
	refresh: str | None = None,
) -> dict:
	require_jarvis_access()
	return txn.replay_on_conflict(
		lambda: _read_shared_conversation(conversation, limit, before, after, refresh), label="share.read"
	)


def _read_shared_conversation(conversation, limit, before, after, refresh) -> dict:
	# Replay the projection and authorization too: a retry must not return an
	# earlier payload after a concurrent revoke or eligibility change.
	latest = _latest_activity(conversation)
	doc, share, reason = _visible_share(conversation, for_update=True)
	if reason:
		frappe.local.response.http_status_code = 403
		return {"refused": reason}
	from jarvis.chat.api import _conversation_history
	from jarvis.chat.share_projection import project_conversation

	body = _conversation_history(
		doc, limit=max(1, min(200, cint(limit))), before=cint(before), after=cint(after), refresh=refresh
	)
	result = project_conversation(body, doc)
	from jarvis.chat import collaboration

	# Old clients keep their inert projection. New clients discover the
	# separately authorized common reader, including after the rollout pauses.
	if collaboration.writes_ready() or collaboration.is_collaborative(conversation):
		result["conversation"].update(
			collaboration_available=collaboration.writes_ready(),
			collaboration_active=collaboration.is_collaborative(conversation),
		)
	_mark_seen(share.name, latest)
	return result


def _shared_message(message: str):
	row = frappe.db.get_value(MESSAGE, message, ["conversation", "hidden"], as_dict=True)
	if not row or row.hidden:
		frappe.throw(_("This shared message is unavailable."), frappe.PermissionError)
	doc, _share = _require_share(row.conversation)
	return doc


@frappe.whitelist()
def get_shared_tool_detail(message: str) -> dict:
	require_jarvis_access()
	doc = _shared_message(message)
	from jarvis.chat.share_projection import project_row

	row = frappe.get_doc(MESSAGE, message).as_dict()
	if row.get("role") != "tool":
		frappe.throw(_("This message is not a tool result."))
	row["canvas"] = []
	return project_row(row, doc.name)


@frappe.whitelist()
def get_shared_canvas(message: str, name: str, dark: int = 0) -> dict:
	require_jarvis_access()
	doc = _shared_message(message)
	from jarvis.chat.share_projection import safe_value
	from jarvis.chat.shared_files import canvas_item, read_file, static_html

	item = canvas_item(message, name)
	file_doc = read_file(doc, item.get("file_url"))
	if not (file_doc.file_name or "").lower().endswith((".html", ".htm")):
		frappe.throw(_("This attachment is available as a download."))
	return {
		"type": "html",
		"title": safe_value(item.get("title", name), doc.name),
		"content": static_html(file_doc.get_content(), doc.name, dark=bool(cint(dark))),
	}


@frappe.whitelist()
def get_shared_file(conversation: str, file_url: str) -> None:
	require_jarvis_access()
	doc, _share = _require_share(conversation)
	from jarvis.chat.shared_files import download, read_file

	download(read_file(doc, file_url))


@frappe.whitelist()
def get_sharing_setting() -> dict:
	require_jarvis_access()
	require_jarvis_admin()
	return {"enabled": sharing_enabled()}


@frappe.whitelist(methods=["POST"])
def set_sharing_setting(enabled: bool) -> dict:
	require_jarvis_access()
	require_jarvis_admin()
	old = sharing_enabled()
	new = bool(cint(enabled))
	frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", int(new), update_modified=False)
	on_sharing_setting_changed(old, new)
	return {"enabled": new}


@frappe.whitelist()
def list_all_shares(start: int = 0, page_length: int = 50, search: str = "", include_modes: int = 0) -> dict:
	require_jarvis_access()
	require_jarvis_admin()
	start, page_length = max(0, cint(start)), max(1, min(200, cint(page_length)))
	params = {"search": "%" + (search or "").strip()[:140] + "%", "start": start, "length": page_length}
	where = """FROM `tabJarvis Conversation Share` s
		JOIN `tabJarvis Conversation` c ON c.name=s.conversation
		JOIN `tabUser` o ON o.name=s.shared_by JOIN `tabUser` u ON u.name=s.user
		WHERE (o.name LIKE %(search)s OR o.full_name LIKE %(search)s
		 OR u.name LIKE %(search)s OR u.full_name LIKE %(search)s)"""
	mode_column = "s.access_mode" if frappe.db.has_column(SHARE, "access_mode") else "'View'"
	mode_select = f", {mode_column} AS access_mode" if cint(include_modes) else ""
	rows = frappe.db.sql(
		f"SELECT s.name, s.conversation, s.user, s.creation AS shared_on, c.title AS conversation_title, o.full_name AS owner_name, u.full_name AS recipient_name{mode_select} "
		+ where
		+ " ORDER BY s.creation DESC, s.name LIMIT %(length)s OFFSET %(start)s",
		params,
		as_dict=True,
	)
	for row in rows:
		if cint(include_modes):
			row["access_mode"] = "Chat" if row.get("access_mode") == "Chat" else "View"
		doc = frappe.get_doc(CONVERSATION, row.pop("conversation"))
		row["hidden"] = bool(_hidden_reason(doc, row.pop("user")))
	return {"rows": rows, "total": frappe.db.sql("SELECT COUNT(*) " + where, params)[0][0]}


@frappe.whitelist(methods=["POST"])
def revoke_share(name: str) -> dict:
	require_jarvis_access()
	require_jarvis_admin()
	return txn.replay_on_conflict(lambda: _revoke_share(name), label="share.revoke")


def _revoke_share(name: str) -> dict:
	row = frappe.db.get_value(SHARE, name, ["conversation", "user"], as_dict=True, for_update=True)
	if row:
		_delete_share(name)
		_after_change(row.conversation, set(), {row.user}, action="admin_revoke")
	return {"ok": True}


def _delete_share(name: str) -> None:
	modified = frappe.db.get_value(SHARE, name, "modified", for_update=True)
	if modified is None:
		return
	# Frappe delete_doc uses ordinary snapshot reads for exists/get_doc. A
	# grant committed after this request's snapshot would otherwise be skipped.
	# A real write to the locked row makes it visible to those reads. Keep
	# deletion/hooks in the same transaction; rollback also undoes this touch.
	# With snapshot checking enabled, a conflicting lock/write raises 1020.
	# Let the transaction owner replay or abort the WHOLE operation. Replaying
	# a lifecycle hook alone could discard the caller's earlier writes/locks.
	frappe.db.set_value(
		SHARE, name, "modified", get_datetime(modified) + timedelta(microseconds=1), update_modified=False
	)
	frappe.delete_doc(SHARE, name, ignore_permissions=True)


def _delete_shares(filters) -> None:
	# Lifecycle requests may have established their snapshot before waiting
	# for the Conversation/User lock. Include grants committed during that wait.
	for row in frappe.db.get_values(
		SHARE, filters=filters, fieldname=["name", "conversation", "user"], as_dict=True, for_update=True
	):
		_delete_share(row.name)
		_after_change(row.conversation, set(), {row.user})


def on_conversation_update(doc, method=None) -> None:
	if doc.has_value_changed("status") and doc.status != "Active":
		_delete_shares({"conversation": doc.name})


def on_conversation_trash(doc, method=None) -> None:
	_delete_shares({"conversation": doc.name})


def on_user_trash(doc, method=None) -> None:
	_delete_shares({"user": doc.name})
	_delete_shares({"shared_by": doc.name})


def on_user_update(doc, method=None) -> None:
	conversations = frappe.get_all(
		SHARE, or_filters={"user": doc.name, "shared_by": doc.name}, pluck="conversation", distinct=True
	)
	for conversation in conversations:
		_invalidate_change(conversation)
