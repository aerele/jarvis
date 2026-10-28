"""Live, explicit review access for Chat-sourced questions only.

A grant admits the review APIs, never the requester's private conversation or a
Frappe DocShare. Generic approval sharing keeps its policy for other sources.
"""

from __future__ import annotations

import frappe
from frappe.query_builder.functions import Coalesce
from pypika.terms import ExistsCriterion

from jarvis.permissions import JARVIS_ADMIN_ROLES

AR = "Jarvis Approval Request"
GRANT = "Jarvis Approval Reviewer"
CONV = "Jarvis Conversation"


def is_chat(doc) -> bool:
	if doc.get("source") != "Chat":
		return False
	# File Box can emit jarvis-ask fences too; preserve its existing policy.
	return (
		not frappe.utils.cint(frappe.db.get_value(CONV, doc.get("conversation"), "file_box"))
		if doc.get("conversation")
		else True
	)


def requester(doc) -> str:
	return (
		frappe.db.get_value(CONV, doc.get("conversation"), "owner") if doc.get("conversation") else None
	) or doc.owner


def eligible_condition(user):
	role = frappe.qb.DocType("Has Role")
	return (
		(user.enabled == 1)
		& (user.user_type == "System User")
		& user.name.notin(["Guest", "Administrator"])
		& ExistsCriterion(
			frappe.qb.from_(role)
			.select(1)
			.where(
				(role.parent == user.name) & (role.parenttype == "User") & role.role.isin(JARVIS_ADMIN_ROLES)
			)
		)
	)


def eligible(user: str) -> bool:
	u = frappe.qb.DocType("User")
	return bool(frappe.qb.from_(u).select(u.name).where((u.name == user) & eligible_condition(u)).run())


def requester_condition(a, user: str):
	c = frappe.qb.DocType(CONV)
	linked = frappe.qb.from_(c).select(1).where(c.name == a.conversation)
	owned = frappe.qb.from_(c).select(1).where((c.name == a.conversation) & (c.owner == user))
	return ExistsCriterion(owned) | (~ExistsCriterion(linked) & (a.owner == user))


def grant_condition(a, user: str):
	if not frappe.db.table_exists(GRANT):
		return a.name.isnull()  # Code may be served before schema migration.
	g, u = frappe.qb.DocType(GRANT), frappe.qb.DocType("User")
	return ExistsCriterion(
		frappe.qb.from_(g)
		.join(u)
		.on(u.name == g.user)
		.select(1)
		.where((g.approval == a.name) & (g.user == user) & eligible_condition(u))
	)


def scoped_condition(a):
	c = frappe.qb.DocType(CONV)
	filebox = frappe.qb.from_(c).select(1).where((c.name == a.conversation) & (Coalesce(c.file_box, 0) != 0))
	return (Coalesce(a.source, "") == "Chat") & ~ExistsCriterion(filebox)


def visible_condition(a, user: str, legacy):
	scoped = scoped_condition(a)
	return (~scoped & legacy) | (scoped & (requester_condition(a, user) | grant_condition(a, user)))


def may_review(doc, user: str | None = None) -> bool:
	user = user or frappe.session.user
	if requester(doc) == user:
		return True
	if not frappe.db.table_exists(GRANT):
		return False
	a = frappe.qb.DocType(AR)
	return bool(
		frappe.qb.from_(a).select(a.name).where((a.name == doc.name) & grant_condition(a, user)).run()
	)


def check(doc, *, manage=False):
	if requester(doc) != frappe.session.user and (manage or not may_review(doc)):
		frappe.throw("Not permitted", frappe.PermissionError)


def reviewers(name: str) -> list[dict]:
	if not frappe.db.table_exists(GRANT):
		return []
	g, u = frappe.qb.DocType(GRANT), frappe.qb.DocType("User")
	return (
		frappe.qb.from_(g)
		.join(u)
		.on(u.name == g.user)
		.select(g.user, u.full_name)
		.where((g.approval == name) & eligible_condition(u))
		.orderby(u.full_name, u.name)
		.run(as_dict=True)
	)


def _locked_user(user: str):
	# User.save locks its parent before changing roles. Use the same lock order
	# for tag/untag/decide so a completed revocation cannot race a new grant.
	frappe.db.sql("SELECT name FROM `tabUser` WHERE name=%s FOR UPDATE", user)
	frappe.db.sql("SELECT name FROM `tabHas Role` WHERE parent=%s AND parenttype='User' FOR UPDATE", user)


def change_tag(name: str, user: str, *, remove=False) -> list[dict]:
	from jarvis.chat import txn
	from jarvis.chat.collaboration import require_legacy_conversation
	from jarvis.chat.events import publish_to_user

	txn.fresh_snapshot(owned=True)
	_locked_user(user)
	doc = frappe.get_doc(AR, name, for_update=True)
	require_legacy_conversation(doc.get("conversation"))
	if not is_chat(doc):
		frappe.throw("Use the existing share controls for this approval.", frappe.PermissionError)
	check(doc, manage=True)
	if not frappe.db.table_exists(GRANT):
		frappe.throw("Reviewer access needs a site migration.")
	if remove:
		if not frappe.db.exists(GRANT, {"approval": name, "user": user}):
			return reviewers(name)
		frappe.db.delete(GRANT, {"approval": name, "user": user})
	else:
		if user == requester(doc) or not eligible(user):
			frappe.throw("Choose an enabled administrator reviewer.", frappe.PermissionError)
		if not frappe.db.exists(GRANT, {"approval": name, "user": user}):
			grant = frappe.get_doc(
				{
					"doctype": GRANT,
					"approval": name,
					"user": user,
					"tagged_by": frappe.session.user,
					"tagged_at": frappe.utils.now_datetime(),
				}
			)
			grant.flags.jarvis_server_write = True
			grant.insert(ignore_permissions=True)
	payload = {"kind": "approval:untagged" if remove else "approval:tagged", "name": name}
	frappe.db.after_commit.add(
		lambda: [publish_to_user(recipient, payload) for recipient in {user, requester(doc)}]
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- publish only the committed explicit grant
	return reviewers(name)


def enrich_rows(rows, user: str, *, source_key="source", owner_key="owner"):
	"""Batch bylines and remove private chat navigation from reviewer payloads."""
	chat = [r for r in rows if r.get(source_key) == "Chat"]
	if not chat:
		return
	names = [r.get("conversation") for r in chat if r.get("conversation")]
	info = (
		{
			r.name: r
			for r in frappe.get_all(
				CONV, filters={"name": ["in", names]}, fields=["name", "owner", "file_box"]
			)
		}
		if names
		else {}
	)
	owners = {name: row.owner for name, row in info.items()}
	for row in chat:
		row["review_scoped"] = int(
			not frappe.utils.cint((info.get(row.get("conversation")) or {}).get("file_box"))
		)
	chat = [r for r in chat if r["review_scoped"]]
	requesters = {owners.get(r.get("conversation")) or r.get(owner_key) for r in chat}
	full_names = {
		r.name: r.full_name
		for r in frappe.get_all(
			"User", filters={"name": ["in", list(requesters)]}, fields=["name", "full_name"]
		)
	}
	for row in chat:
		who = owners.get(row.get("conversation")) or row.get(owner_key)
		row["requester"], row["requester_name"] = who, full_names.get(who) or who
		row["is_requester"] = int(who == user)
		row["shared"] = int(who != user)
		if who != user:
			row["conversation"], row["origin_page"], row["shared"] = "", "", 1


def decide_pending(name: str, decision: str, approve: bool):
	"""Serialize authorization with role/untag changes, then win Pending once."""
	from jarvis.chat import txn
	from jarvis.chat.pending_actions._store import rowcount

	txn.fresh_snapshot(owned=True)
	user = frappe.session.user
	_locked_user(user)
	doc = frappe.get_doc(AR, name, for_update=True)
	check(doc)
	if not is_chat(doc) or doc.get("sheet") or doc.get("routing") or doc.get("assessment_key"):
		frappe.throw("Use the approval's own decision controls.", frappe.PermissionError)
	if doc.status != "Pending":
		frappe.throw("This question has already been decided.")
	a = frappe.qb.DocType(AR)
	(
		frappe.qb.update(a)
		.set(a.status, "Approved" if approve else "Rejected")
		.set(a.decision, decision)
		.set(a.decided_by, user)
		.set(a.decided_at, frappe.utils.now_datetime())
		.where(
			(a.name == name)
			& (a.status == "Pending")
			& (a.source == "Chat")
			& (requester_condition(a, user) | grant_condition(a, user))
		)
		.run()
	)
	if rowcount() != 1:
		frappe.throw("This question was decided or access was revoked.", frappe.PermissionError)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- settle once before resuming as requester
	return doc


def notify_settled(doc, *, include_requester=True):
	"""Publish only approval metadata; a reviewer never receives private chat ids."""
	from jarvis.chat.events import publish_to_user

	if not is_chat(doc):
		return
	users = {r.user for r in reviewers(doc.name)}
	if include_requester:
		users.add(requester(doc))
	payload = {"kind": "action:settled", "name": doc.name, "status": doc.status}
	for user in users:
		publish_to_user(user, payload)


def queue_settled(doc):
	if is_chat(doc):
		frappe.db.after_commit.add(lambda: notify_settled(doc))


def on_user_update(doc, _method=None):
	"""Remove invalid grants permanently: restored roles require a fresh tag."""
	if not frappe.db.table_exists(GRANT) or (_method != "on_trash" and eligible(doc.name)):
		return
	g = frappe.qb.DocType(GRANT)
	where = g.user == doc.name
	if _method == "on_trash":
		where |= g.tagged_by == doc.name
	rows = frappe.qb.from_(g).select(g.approval, g.user).where(where).run(as_dict=True)
	if not rows:
		return
	frappe.qb.from_(g).delete().where(where).run()

	def publish():
		from jarvis.chat.events import publish_to_user

		for row in rows:
			payload = {"kind": "approval:untagged", "name": row.approval}
			publish_to_user(row.user, payload)
			if frappe.db.exists(AR, row.approval):
				publish_to_user(requester(frappe.get_doc(AR, row.approval)), payload)

	frappe.db.after_commit.add(publish)


def on_approval_trash(doc, _method=None):
	if frappe.db.table_exists(GRANT):
		users = [r.user for r in reviewers(doc.name)]
		frappe.db.delete(GRANT, {"approval": doc.name})

		def publish():
			from jarvis.chat.events import publish_to_user

			for user in {*users, requester(doc)}:
				publish_to_user(user, {"kind": "approval:untagged", "name": doc.name})

		frappe.db.after_commit.add(publish)


def comment_query_conditions(user=None):
	"""Generic comment lists must not expose a private Chat question indirectly.

	Reviewer comments are read through the review API; generic Comment reads keep
	the parent's requester-only policy. Frappe composes this with its normal
	doctype-level comment conditions. Other comment sources are unchanged.
	"""
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	comment, a = frappe.qb.DocType("Comment"), frappe.qb.DocType(AR)
	private = (
		frappe.qb.from_(a)
		.select(1)
		.where((a.name == comment.reference_name) & scoped_condition(a) & ~requester_condition(a, user))
	)
	return (
		"(COALESCE(`tabComment`.`reference_doctype`, '') <> 'Jarvis Approval Request' "
		f"OR NOT EXISTS ({private.get_sql()}))"
	)


def has_comment_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	"""Generic Comment access on a review-scoped question follows the question's
	own generic policy (requester only, collaborative denied).

	Core lets any Comment-role holder write, delete or share a comment whatever
	its parent. A write is checked against the stored parent and the proposed
	one, so a comment cannot be moved out of, or into, a private question.
	Reviewers comment through docmeta, which writes with ignore_permissions.
	Other comments keep core policy: True here only means "no objection".
	"""
	from jarvis.chat.chat_permissions import has_approval_permission

	user = user or frappe.session.user
	if user == "Administrator":
		return True
	parents = {(doc.get("reference_doctype"), doc.get("reference_name"))}
	if ptype not in ("read", "select", "create") and doc.get("name"):
		stored = frappe.db.get_value("Comment", doc.get("name"), ["reference_doctype", "reference_name"])
		if stored:
			parents.add(tuple(stored))
	for doctype, name in parents:
		if doctype != AR or not name:
			continue
		approval = frappe.db.get_value(AR, name, ["name", "source", "conversation", "owner"], as_dict=True)
		if approval and is_chat(approval) and not has_approval_permission(approval, "read", user):
			return False
	return True


def has_grant_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	"""Grants are read and written only by the review APIs, never generically.

	The doctype carries no DocPerm, but Frappe still gives System Manager
	default access; deny every ptype so no role gains untagged oversight.
	"""
	return (user or frappe.session.user) == "Administrator"


def grant_query_conditions(user=None):
	return "" if (user or frappe.session.user) == "Administrator" else "1=0"
