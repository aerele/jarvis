"""Doc-metadata bundle + mutations for the v3 document pages (DESIGN-V3 §8.1 + §14).

One server-reshaped ``get_docmeta`` bundle (comments / assignees / likes /
attachments / shares / byline) replaces desk's ``get_docinfo`` (which fills
``frappe.response["docinfo"]`` and returns nothing — frappe-ui ``call()`` would
yield undefined — and drags versions/communications/views the SPA never
renders). Mutations gate FIRST (owner / approval-conversation-owner / System
Manager / DocShare-read for readers), then act with ``ignore_permissions`` —
the established house pattern (see filebox cascade, macros run cleanup).

Why not the desk wrappers (§14 DA-03): ``frappe.desk.form.utils.add_comment``
and ``frappe.desk.like.toggle_like`` re-check read via the permission framework,
which fails for the approval-conversation-owner (the approval row itself may be
owned by Administrator). Comments are inserted directly and likes update
``_liked_by`` directly, mirroring desk semantics without its permission
re-check. Assignments use the internal ``assign_to._add/_remove`` (identical
signatures on v15 and v16) plus an explicit DocShare read-grant (D23) — created
BEFORE the ToDo so ``_add``'s assignee-permission probe passes via the DocShare
fallback and never enters its auto-share branch (which would demand the caller
hold the ``share`` ptype no jarvis role has).

DocShare marker convention (share/assign disambiguation): one DocShare row can
back BOTH an explicit share and an assignment's read grant, so the rows are
tagged via ``notify_by_email`` — ``toggle_share`` creates/updates rows with
``notify_by_email=1`` (explicit share) while ``toggle_assignment`` creates its
plumbing rows with ``notify_by_email=0``. Unassign deletes ONLY
``notify_by_email=0`` rows (an explicit share independently granted via
``toggle_share`` survives the unassignment), and ``toggle_share("remove")``
refuses only when an OPEN ToDo exists AND the row is ``notify_by_email=0``
(assignment-owned — remove the assignment instead). Explicitly sharing an
already-assigned user upgrades the row to ``notify_by_email=1``.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.desk.form import assign_to as _assign_to
from frappe.share import add_docshare

from jarvis.permissions import has_jarvis_access, require_jarvis_user

ALLOWED_DOCTYPES = {
	"Jarvis Custom Skill",
	"Jarvis Macro",
	"Jarvis Approval Request",
	"Jarvis Agent Installation",
}
# §14 DA-09 / F1: skills keep their child-table share model (it feeds the sync
# pipeline); ToDo assignment + DocShare sharing are NOT offered on them.
ASSIGNABLE_DOCTYPES = {"Jarvis Macro", "Jarvis Approval Request", "Jarvis Agent Installation"}
SHAREABLE_DOCTYPES = {"Jarvis Macro", "Jarvis Approval Request", "Jarvis Agent Installation"}


# --------------------------------------------------------------------------- #
# gate + shaping helpers
# --------------------------------------------------------------------------- #
def _get_gated(doctype: str, name: str, write: bool = False):
	"""Load the doc after the docmeta gate (§8.1).

	Allowed: System Manager · the doc owner · the linked-conversation owner for
	``Jarvis Approval Request`` (``decide()`` parity) · for READS only, a user
	holding a DocShare read row (assignees / explicit shares)."""
	if doctype not in ALLOWED_DOCTYPES:
		frappe.throw(_("Doctype not allowed."), frappe.PermissionError)
	doc = frappe.get_doc(doctype, name)
	me = frappe.session.user
	if "System Manager" in frappe.get_roles():
		return doc
	if doc.owner == me:
		return doc
	if doctype == "Jarvis Approval Request" and doc.get("conversation"):
		if frappe.db.get_value("Jarvis Conversation", doc.conversation, "owner") == me:
			return doc
	if not write and frappe.db.exists(
		"DocShare", {"share_doctype": doctype, "share_name": name, "user": me, "read": 1}
	):
		return doc
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _user_info(users: list[str]) -> dict[str, dict]:
	"""One query for full_name/user_image over a set of user ids."""
	users = [u for u in set(users) if u]
	if not users:
		return {}
	return {
		r.name: r
		for r in frappe.get_all(
			"User",
			filters={"name": ["in", users]},
			fields=["name", "full_name", "user_image"],
		)
	}


def _full_name(user: str) -> str:
	return frappe.db.get_value("User", user, "full_name") or user


def _comments(doctype: str, name: str) -> list[dict]:
	rows = frappe.get_all(
		"Comment",
		filters={
			"comment_type": "Comment",
			"reference_doctype": doctype,
			"reference_name": name,
		},
		fields=["name", "content", "owner", "creation", "modified"],
		order_by="creation asc",
	)
	info = _user_info([r.owner for r in rows])
	for r in rows:
		u = info.get(r.owner) or {}
		r["owner_name"] = u.get("full_name") or r.owner
		r["owner_image"] = u.get("user_image")
	return rows


def _comment_row(doc) -> dict:
	"""One Comment document in the get_docmeta comments shape."""
	u = _user_info([doc.owner]).get(doc.owner) or {}
	return {
		"name": doc.name,
		"content": doc.content,
		"owner": doc.owner,
		"owner_name": u.get("full_name") or doc.owner,
		"owner_image": u.get("user_image"),
		"creation": str(doc.creation),
		"modified": str(doc.modified),
	}


def _assignees(doctype: str, name: str) -> list[dict]:
	users: list[str] = []
	for u in frappe.get_all(
		"ToDo",
		filters={
			"reference_type": doctype,
			"reference_name": name,
			"status": ["!=", "Cancelled"],
		},
		pluck="allocated_to",
		order_by="creation asc",
	):
		if u and u not in users:
			users.append(u)
	info = _user_info(users)
	return [
		{
			"user": u,
			"full_name": (info.get(u) or {}).get("full_name") or u,
			"image": (info.get(u) or {}).get("user_image"),
		}
		for u in users
	]


def _shares(doctype: str, name: str, owner: str) -> list[dict]:
	"""DocShare rows for the doc, excluding the owner (§14 F1). Assignment-created
	read grants show here too — that is intended (visible access)."""
	rows = [
		r
		for r in frappe.get_all(
			"DocShare",
			filters={"share_doctype": doctype, "share_name": name},
			fields=["user", "read", "write"],
			order_by="creation asc",
		)
		if r.user and r.user != owner
	]
	info = _user_info([r.user for r in rows])
	return [
		{
			"user": r.user,
			"full_name": (info.get(r.user) or {}).get("full_name") or r.user,
			"image": (info.get(r.user) or {}).get("user_image"),
			"read": int(r.read or 0),
			"write": int(r.write or 0),
		}
		for r in rows
	]


def _attachments(doctype: str, name: str) -> list[dict]:
	return frappe.get_all(
		"File",
		filters={"attached_to_doctype": doctype, "attached_to_name": name},
		fields=["name", "file_name", "file_url", "file_size", "is_private", "creation", "owner"],
		order_by="creation asc",
	)


def _liked_list(doc) -> list[str]:
	raw = doc.get("_liked_by")
	try:
		out = json.loads(raw) if raw else []
	except Exception:
		out = []
	return out if isinstance(out, list) else []


def _open_todo_exists(doctype: str, name: str, user: str) -> bool:
	return bool(
		frappe.get_all(
			"ToDo",
			filters={
				"reference_type": doctype,
				"reference_name": name,
				"allocated_to": user,
				"status": ["!=", "Cancelled"],
			},
			limit=1,
		)
	)


def _validate_target_user(user: str) -> str:
	"""Assign/share target must be a real, enabled, non-Guest user WHO CAN REACH
	JARVIS. TASK 27 (TAG-01): a share/assign grants a DocShare-read that Part-1's
	approval scoping honors as visibility, so the target must be confined to the
	Jarvis population — otherwise a Website/portal (or any non-Jarvis) user could
	be handed visibility into a Jarvis Approval Request / Macro / Agent
	Installation they must never reach."""
	user = (user or "").strip()
	if not user or user == "Guest":
		frappe.throw(_("Invalid user."))
	if not frappe.db.get_value("User", user, "enabled"):
		frappe.throw(_("Invalid user."))
	if not has_jarvis_access(user):
		frappe.throw(_("You can only share or assign to Jarvis users."))
	return user


# --------------------------------------------------------------------------- #
# read bundle
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def get_docmeta(doctype: str, name: str) -> dict:
	"""The doc-page metadata bundle (§8.1 as amended by §14 F1) in one round trip."""
	doc = _get_gated(doctype, name)
	liked_by = _liked_list(doc)
	return {
		"comments": _comments(doctype, name),
		"assignees": _assignees(doctype, name),
		"liked_by": liked_by,
		"liked": frappe.session.user in liked_by,
		"attachments": _attachments(doctype, name),
		"shares": _shares(doctype, name, doc.owner),
		"created": {
			"owner": doc.owner,
			"full_name": _full_name(doc.owner),
			"creation": str(doc.creation or ""),
		},
		"modified": {
			"modified_by": doc.modified_by,
			"full_name": _full_name(doc.modified_by) if doc.modified_by else "",
			"modified": str(doc.modified or ""),
		},
	}


# --------------------------------------------------------------------------- #
# comments (direct inserts/updates AFTER the gate — §14 DA-03)
# --------------------------------------------------------------------------- #
def _strip_mention_spans(content: str) -> str:
	"""TASK 28 (TAG-02): neutralize @mention markup before insert.
	``Comment.after_insert`` -> ``notify_mentions`` emails/notifies every
	``class="mention"`` target of the comment (and thus the doc's title). On these
	Jarvis boards a DocShare-tagged NON-owner could otherwise forge a mention and
	leak a sensitive approval title ("SECRET payroll approve $840k") to an
	arbitrary third party. Access to these docs is granted via ``toggle_share``,
	never via @mention, so mention-NOTIFY is not a needed feature here — unwrap
	each mention element to its plain text so the comment still reads naturally
	(e.g. "@Carol") but triggers no notification."""
	if not content or "mention" not in content:
		return content
	from bs4 import BeautifulSoup

	soup = BeautifulSoup(content, "html.parser")
	changed = False
	for el in soup.find_all(class_="mention"):
		el.unwrap()
		changed = True
	return str(soup) if changed else content


@frappe.whitelist()
@require_jarvis_user
def add_comment(doctype: str, name: str, content: str) -> dict:
	"""Add a comment (read-gated, matching desk's 'read is enough to comment').

	Inserts the Comment doc directly (never the read-gated desk wrapper).
	Mention notifications are native: ``Comment.after_insert`` calls
	``notify_mentions`` (verified §14 F6)."""
	_get_gated(doctype, name)
	content = (content or "").strip()
	if not content:
		frappe.throw(_("Comment is empty."))
	content = _strip_mention_spans(content)
	comment = frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": doctype,
			"reference_name": name,
			"content": content,
			"comment_email": frappe.session.user,
			"comment_by": _full_name(frappe.session.user),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return _comment_row(comment)


def _comment_gated(comment: str):
	"""Comment mutation gate (§8.1): the comment must live on an allowlisted
	doctype and the caller must be its author or a System Manager."""
	doc = frappe.get_doc("Comment", comment)
	if doc.reference_doctype not in ALLOWED_DOCTYPES:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if doc.owner != frappe.session.user and "System Manager" not in frappe.get_roles():
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return doc


@frappe.whitelist()
@require_jarvis_user
def update_comment(comment: str, content: str) -> dict:
	"""Edit a comment's HTML (author or System Manager only)."""
	doc = _comment_gated(comment)
	content = (content or "").strip()
	if not content:
		frappe.throw(_("Comment is empty."))
	doc.content = content
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _comment_row(doc)


@frappe.whitelist()
@require_jarvis_user
def delete_comment(comment: str) -> None:
	"""Delete a comment (author or System Manager only)."""
	_comment_gated(comment)
	frappe.delete_doc("Comment", comment, ignore_permissions=True)
	frappe.db.commit()


# --------------------------------------------------------------------------- #
# assignment (ToDo + DocShare read-grant — D23, §14 DA-09)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def toggle_assignment(doctype: str, name: str, user: str, action: str = "add") -> list[dict]:
	"""Assign/unassign ``user`` on the doc (write-gated: owner / approval-
	conversation-owner / SM). Returns the fresh assignees list.

	add: DocShare read-grant first, then the internal ``assign_to._add`` —
	grant-first means ``_add``'s assignee-permission probe passes via the
	DocShare fallback instead of entering its auto-share branch (which requires
	the caller to hold the ``share`` ptype). A DocShare row this path CREATES is
	marked ``notify_by_email=0`` (assignment plumbing — see the module-docstring
	marker convention); a pre-existing explicit-share row is left untouched.
	remove: cancel the ToDo, then drop the ``notify_by_email=0`` DocShare when no
	non-cancelled ToDo remains for that user (D23 cleanup) — an explicit share
	(``notify_by_email=1``) survives the unassignment."""
	if doctype not in ASSIGNABLE_DOCTYPES:
		frappe.throw(_("Assignees are not available on {0}.").format(doctype))
	if action not in ("add", "remove"):
		frappe.throw(_("Invalid action."))
	_get_gated(doctype, name, write=True)
	user = _validate_target_user(user)

	if action == "add":
		if not _open_todo_exists(doctype, name, user):
			had_share = frappe.db.exists(
				"DocShare", {"share_doctype": doctype, "share_name": name, "user": user}
			)
			share = add_docshare(doctype, name, user, read=1, flags={"ignore_share_permission": True})
			if not had_share:
				# Marker: assignment plumbing, not an explicit share.
				frappe.db.set_value("DocShare", share.name, "notify_by_email", 0, update_modified=False)
			_assign_to._add(
				{"doctype": doctype, "name": name, "assign_to": [user]},
				ignore_permissions=True,
			)
	else:
		_assign_to._remove(doctype, name, user, ignore_permissions=True)
		if not _open_todo_exists(doctype, name, user):
			frappe.db.delete(
				"DocShare",
				{
					"share_doctype": doctype,
					"share_name": name,
					"user": user,
					"notify_by_email": 0,
				},
			)
	frappe.db.commit()
	return _assignees(doctype, name)


# --------------------------------------------------------------------------- #
# sharing (DocShare read — §14 F1)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def toggle_share(doctype: str, name: str, user: str, action: str = "add") -> list[dict]:
	"""Share/unshare the doc with ``user`` (write-gated). Returns the fresh
	shares list. Explicit shares carry the ``notify_by_email=1`` marker (see the
	module docstring) — sharing an already-assigned user upgrades the
	assignment-created row, so a later unassign no longer revokes it. Removing an
	ASSIGNMENT-OWNED row (``notify_by_email=0``) that still backs a live
	assignment (open ToDo) is refused — remove the assignment instead (§14 F1).
	Skills are excluded: they keep the child-table share model that feeds the
	sync pipeline.

	Approvals additionally MIRROR the read-share onto the linked conversation
	(best-effort): Frappe gates a private File's byte-fetch on READ of the doc
	the File is attached to — the CONVERSATION, not the approval — so without
	the mirror a tagged user sees the approval but cannot preview its file."""
	if doctype not in SHAREABLE_DOCTYPES:
		frappe.throw(_("Sharing is not available on {0}.").format(doctype))
	if action not in ("add", "remove"):
		frappe.throw(_("Invalid action."))
	doc = _get_gated(doctype, name, write=True)
	user = _validate_target_user(user)

	if action == "add":
		if user != doc.owner:  # owner already has full access; skip the no-op row
			share = add_docshare(doctype, name, user, read=1, flags={"ignore_share_permission": True})
			if not int(share.notify_by_email or 0):
				# Marker: explicit share (upgrades an assignment-created row).
				frappe.db.set_value("DocShare", share.name, "notify_by_email", 1, update_modified=False)
	else:
		row = frappe.db.get_value(
			"DocShare",
			{"share_doctype": doctype, "share_name": name, "user": user},
			["name", "notify_by_email"],
			as_dict=True,
		)
		if row and not int(row.notify_by_email or 0) and _open_todo_exists(doctype, name, user):
			frappe.throw(_("This share backs an active assignment — remove the assignment instead."))
		frappe.db.delete("DocShare", {"share_doctype": doctype, "share_name": name, "user": user})

	# Mirror the read-share onto the linked conversation so a tagged user can
	# actually fetch the approval's file bytes (File Box list + preview).
	# Best-effort — the mirror must never break the primary approval share.
	if doctype == "Jarvis Approval Request":
		try:
			conv = doc.get("conversation")
			if conv and frappe.db.exists("Jarvis Conversation", conv):
				if action == "add":
					add_docshare(
						"Jarvis Conversation",
						conv,
						user,
						read=1,
						flags={"ignore_share_permission": True},
					)
				else:
					frappe.db.delete(
						"DocShare",
						{
							"share_doctype": "Jarvis Conversation",
							"share_name": conv,
							"user": user,
						},
					)
		except Exception:
			frappe.log_error(
				title="Jarvis: conversation share mirror failed",
				message=frappe.get_traceback(),
			)
	frappe.db.commit()
	return _shares(doctype, name, doc.owner)


# --------------------------------------------------------------------------- #
# like (direct _liked_by update — §14 DA-03)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def toggle_like(doctype: str, name: str, like: int = 1) -> list[str]:
	"""Like (``like=1``) / unlike (``like=0``) the doc for the current user.
	Read-gated. Mirrors desk semantics (``_liked_by`` JSON + a "Like" Comment
	trace row) without desk's permission re-check; the ``add="Yes"`` string
	quirk of the desk endpoint is hidden here. Returns the updated liked_by."""
	doc = _get_gated(doctype, name)
	me = frappe.session.user
	liked_by = _liked_list(doc)
	if int(like):
		if me not in liked_by:
			liked_by.append(me)
			doc.add_comment("Like", _("Liked"))
	else:
		if me in liked_by:
			liked_by.remove(me)
			for c in frappe.get_all(
				"Comment",
				filters={
					"comment_type": "Like",
					"reference_doctype": doctype,
					"reference_name": name,
					"owner": me,
				},
				pluck="name",
			):
				frappe.delete_doc("Comment", c, ignore_permissions=True, force=True)
	_set_liked_by(doctype, name, liked_by)
	frappe.db.commit()
	return liked_by


def _set_liked_by(doctype: str, name: str, liked_by: list[str]) -> None:
	"""Persist ``_liked_by``, auto-adding the column on first use (desk parity)."""
	try:
		frappe.db.set_value(doctype, name, "_liked_by", json.dumps(liked_by), update_modified=False)
	except frappe.db.ProgrammingError as e:  # pragma: no cover - first-like only
		if frappe.db.is_missing_column(e):
			from frappe.database.schema import add_column

			add_column(doctype, "_liked_by", "Text")
			frappe.db.set_value(doctype, name, "_liked_by", json.dumps(liked_by), update_modified=False)
		else:
			raise


# --------------------------------------------------------------------------- #
# attachments (upload rides stock /api/method/upload_file; delete is gated here)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def delete_attachment(doctype: str, name: str, file: str) -> None:
	"""Delete an attachment of the doc (write-gated). The File must actually be
	attached to this doc — a foreign file name is refused."""
	_get_gated(doctype, name, write=True)
	f = frappe.db.get_value("File", file, ["attached_to_doctype", "attached_to_name"], as_dict=True)
	if not f or f.attached_to_doctype != doctype or f.attached_to_name != name:
		frappe.throw(_("File is not attached to this document."))
	frappe.delete_doc("File", file, ignore_permissions=True)
	frappe.db.commit()
