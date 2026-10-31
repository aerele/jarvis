"""Live, explicit review access for Chat-sourced questions only.

A grant admits the review APIs, never the requester's private conversation or a
Frappe DocShare. Generic approval sharing keeps its policy for other sources.
"""

from __future__ import annotations

import frappe
from frappe.query_builder.functions import Coalesce
from pypika.terms import ExistsCriterion, LiteralValue

from jarvis.permissions import JARVIS_ADMIN_ROLES

AR = "Jarvis Approval Request"
GRANT = "Jarvis Approval Reviewer"
CONV = "Jarvis Conversation"
DECIDED = ("Approved", "Rejected")
# A decision whose chat continuation is still 'due' this long after it was decided
# lost its process (the HTTP worker timeout is shorter): it reads as failed, and the
# requester may send it. Display only; the claim alone prevents a second message.
RESUME_STALE_S = 180


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
	recipients = {user, requester(doc)}
	frappe.db.after_commit.add(lambda: publish_each(recipients, payload))
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- publish only the committed explicit grant
	return reviewers(name)


def enrich_rows(rows, user: str, *, source_key="source", owner_key="owner"):
	"""Batch bylines and remove private chat navigation from reviewer payloads.
	Scoped rows also carry their chat continuation (from ``resume_state``)."""
	tracked = any("resume_state" in r for r in rows)
	states = {r.get("name"): r.pop("resume_state", None) for r in rows}
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
		if tracked:
			state = continuation(states.get(row.get("name")), row.get("decided_at"))
			row["continuation"] = state
			row["can_continue"] = can_continue(
				state, row.get("status"), who == user, row.get("conversation") in info
			)
		if who != user:
			row["conversation"], row["origin_page"], row["shared"] = "", "", 1


def decide_pending(name: str, decision: str, approve: bool):
	"""Serialize authorization with role/untag changes, then win Pending once.

	The same statement marks the chat continuation 'due' when the linked
	conversation exists, so the decision and its pending delivery commit together."""
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
	update = (
		frappe.qb.update(a)
		.set(a.status, "Approved" if approve else "Rejected")
		.set(a.decision, decision)
		.set(a.decided_by, user)
		.set(a.decided_at, frappe.utils.now_datetime())
	)
	if resume_tracked():
		c = frappe.qb.DocType(CONV)
		linked = frappe.qb.from_(c).select(1).where(c.name == a.conversation)
		update = update.set(
			a.resume_state, LiteralValue(f"CASE WHEN EXISTS ({linked.get_sql()}) THEN 'due' END")
		)
	(
		update.where(
			(a.name == name)
			& (a.status == "Pending")
			& (a.source == "Chat")
			& (requester_condition(a, user) | grant_condition(a, user))
		).run()
	)
	if rowcount() != 1:
		frappe.throw("This question was decided or access was revoked.", frappe.PermissionError)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- settle once before resuming as requester
	return doc


def resume_tracked() -> bool:
	"""``resume_state`` is migrated. Code served before the migrate behaves as
	before: no state is written, none is offered."""
	return bool(frappe.db.has_column(AR, "resume_state"))


def _continuable(a, name: str, conversation: str):
	return (
		(a.name == name)
		& a.resume_state.isin(("due", "failed"))
		& a.status.isin(DECIDED)
		& (a.source == "Chat")
		& (a.conversation == conversation)
	)


def claim_continuation(name: str, conversation: str) -> bool:
	"""Inside send_message's user-row transaction: 'done' commits with the
	decision message or not at all. Exactly one attempt wins."""
	from jarvis.chat.pending_actions._store import rowcount

	a = frappe.qb.DocType(AR)
	frappe.qb.update(a).set(a.resume_state, "done").where(_continuable(a, name, conversation)).run()
	return rowcount() == 1


def mark_continuation_failed(name: str, conversation: str) -> bool:
	"""A known refusal: the decision message was not accepted. Never undoes 'done'.
	True only when this call recorded it (another attempt may have sent it)."""
	from jarvis.chat.pending_actions._store import rowcount

	a = frappe.qb.DocType(AR)
	frappe.qb.update(a).set(a.resume_state, "failed").where(_continuable(a, name, conversation)).run()
	return rowcount() == 1


def stored_resume_state(name: str) -> str | None:
	return frappe.db.get_value(AR, name, "resume_state")


def continuation(state: str | None, decided_at=None) -> str:
	"""The client-facing continuation: "" (untracked), "pending", "failed", "done"."""
	if state == "due":
		age = frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), decided_at) if decided_at else 0
		return "failed" if age > RESUME_STALE_S else "pending"
	return state if state in ("failed", "done") else ""


def can_continue(state: str, status: str, is_requester: bool, conversation_exists: bool) -> int:
	return int(bool(is_requester and state == "failed" and status in DECIDED and conversation_exists))


def log_bounded(title: str, message: str) -> None:
	"""An Error Log carrying constant metadata only; it can never raise."""
	try:
		frappe.log_error(title=title, message=message or "-")
	except Exception:
		pass


def publish_each(users, payload: dict) -> None:
	"""Best-effort, per-recipient delivery: one failure skips nobody else and never
	raises (after-commit callbacks run inside a later commit). The log names the
	event kind and the approval only."""
	from jarvis.chat.events import publish_to_user

	for user in sorted(u for u in set(users) if u):
		try:
			publish_to_user(user, payload)
		except Exception:
			log_bounded("jarvis.approval.notify_failed", f"{payload.get('kind')} {payload.get('name') or ''}")


def notify_settled(doc, *, include_requester=True):
	"""Publish only approval metadata; a reviewer never receives private chat ids."""
	payload = {"kind": "action:settled", "name": doc.name, "status": doc.status}
	users = set()
	try:
		if not is_chat(doc):
			return
		users |= {r.user for r in reviewers(doc.name)}
	except Exception:
		log_bounded("jarvis.approval.notify_failed", f"action:settled {doc.name}")
	if include_requester:
		try:
			users.add(requester(doc))
		except Exception:
			log_bounded("jarvis.approval.notify_failed", f"action:settled {doc.name}")
	publish_each(users, payload)


def queue_settled(doc):
	if is_chat(doc):
		frappe.db.after_commit.add(lambda: notify_settled(doc))


def on_user_update(doc, _method=None):
	"""Remove invalid grants when a User save (Desk/REST, role profile, delete)
	makes the reviewer ineligible; restored roles then require a fresh tag.

	Raw writes (``db.set_value`` on User, a Role disabled from Desk, which deletes
	Has Role rows directly) skip this hook. Their grant row survives but grants
	nothing while the user is ineligible: every authority read is live SQL. If the
	user becomes eligible again, the requester's original tag applies again and
	stays visible to the requester, who can untag it."""
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
		for row in rows:
			payload = {"kind": "approval:untagged", "name": row.approval}
			recipients = {row.user}
			try:
				if frappe.db.exists(AR, row.approval):
					recipients.add(requester(frappe.get_doc(AR, row.approval)))
			except Exception:
				log_bounded("jarvis.approval.notify_failed", f"approval:untagged {row.approval}")
			publish_each(recipients, payload)

	frappe.db.after_commit.add(publish)


def on_approval_trash(doc, _method=None):
	if frappe.db.table_exists(GRANT):
		recipients = {*(r.user for r in reviewers(doc.name)), requester(doc)}
		frappe.db.delete(GRANT, {"approval": doc.name})
		payload = {"kind": "approval:untagged", "name": doc.name}
		frappe.db.after_commit.add(lambda: publish_each(recipients, payload))


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


# --------------------------------------------------------------------------- #
# Audit copies (Version, Deleted Document) of a review-scoped question follow the
# question's own generic policy: the requester (and Administrator) only. Each
# doctype has ONE SQL classifier, used by its list condition and, row by row, by
# its has_permission hook, so list and per-document reads always agree. Archived
# JSON is parsed (JSON_VALID / JSON_TYPE / JSON_VALUE), never pattern-matched;
# anything an approval-linked artifact cannot be classified as is root-only.
# Unrelated doctypes, File Box and other sources keep the stock DocPerm policy;
# hooks only deny, so nobody gains a read they did not have.
# --------------------------------------------------------------------------- #
_ARCHIVE_LINKS = {
	"Comment": ("reference_doctype", "reference_name"),
	"File": ("attached_to_doctype", "attached_to_name"),
}


def _jvalue(data: str, key: str) -> str:
	# Decoded JSON string, collation-normalized so it compares with any column.
	return f"CONVERT(JSON_VALUE({data}, '$.{key}') USING utf8mb4) COLLATE utf8mb4_unicode_ci"


def _jtype(data: str, key: str) -> str:
	return f"JSON_TYPE(JSON_EXTRACT({data}, '$.{key}'))"


def _json_object(data: str) -> str:
	return f"(JSON_VALID({data}) AND JSON_TYPE({data}) = 'OBJECT')"


def _approval_archive_ok(d: str, user: str) -> str:
	"""An approval's own archive row ``d`` under the live requester rules: the
	source/conversation/owner come from the archive, the File Box flag and the
	owner from the current conversation (archived owner once it is gone)."""
	data, u = f"{d}.data", frappe.db.escape(user)
	conv, src = _jvalue(data, "conversation"), _jtype(data, "source")
	exists = f"SELECT 1 FROM `tabJarvis Conversation` ac WHERE ac.name = {conv}"
	return (
		f"({_json_object(data)} AND COALESCE({src}, 'NULL') IN ('NULL', 'STRING') AND ("
		f"COALESCE({_jvalue(data, 'source')}, '') <> 'Chat'"
		f" OR EXISTS ({exists} AND COALESCE(ac.file_box, 0) <> 0)"
		f" OR EXISTS ({exists} AND ac.owner = {u})"
		f" OR (NOT EXISTS ({exists}) AND {_jvalue(data, 'owner')} = {u})))"
	)


def _live_approval(name_sql: str, user: str, alias: str = "pv_live") -> tuple[str, str]:
	"""``(live, live_ok)`` subqueries for approval ``name_sql``: it exists; it exists
	and the generic policy shows it to ``user``."""
	a = frappe.qb.DocType(AR).as_(alias)
	live = frappe.qb.from_(a).select(1).where(a.name == LiteralValue(name_sql))
	live_ok = live.where(~scoped_condition(a) | requester_condition(a, user))
	return live.get_sql(), live_ok.get_sql()


def _all_pass(rows: str, ok: str) -> str:
	"""Every row of ``rows`` passes ``ok``; an UNKNOWN (NULL) result fails."""
	return f"NOT EXISTS ({rows} AND NOT (({ok}) IS TRUE))"


def _approval_visible(name_sql: str, user: str) -> str:
	"""Does the generic policy show approval ``name_sql`` to ``user``? A live row
	always decides; otherwise every archive of it must; neither -> deny."""
	live, live_ok = _live_approval(name_sql, user)
	archived = (
		f"SELECT 1 FROM `tabDeleted Document` pv_d WHERE pv_d.deleted_doctype = {frappe.db.escape(AR)}"
		f" AND pv_d.deleted_name = {name_sql}"
	)
	return (
		f"(EXISTS ({live_ok}) OR (NOT EXISTS ({live}) AND EXISTS ({archived})"
		f" AND {_all_pass(archived, _approval_archive_ok('pv_d', user))}))"
	)


def _linked_archive_ok(d: str, user: str, doctype: str) -> str:
	"""A Comment/File archive: valid unrelated objects keep the stock policy;
	approval-linked ones follow their approval; unclassifiable ones are root-only."""
	data = f"{d}.data"
	link, name = _ARCHIVE_LINKS[doctype]
	link_type, name_type = _jtype(data, link), _jtype(data, name)
	target = _jvalue(data, name)
	return (
		f"({_json_object(data)} AND (COALESCE({link_type}, 'NULL') = 'NULL' OR ({link_type} = 'STRING' AND ("
		f"{_jvalue(data, link)} <> {frappe.db.escape(AR)}"
		f" OR ({name_type} = 'STRING' AND {target} <> '' AND {_approval_visible(target, user)})))))"
	)


def deleted_document_query_conditions(user=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	d, ar = "`tabDeleted Document`", frappe.db.escape(AR)
	# A live row decides for its name; only without one does this archive row decide.
	live, live_ok = _live_approval(f"{d}.deleted_name", user, alias="dl")
	return (
		f"(COALESCE({d}.deleted_doctype, '') NOT IN ({ar}, 'Comment', 'File')"
		f" OR ({d}.deleted_doctype = {ar} AND (EXISTS ({live_ok})"
		f" OR (NOT EXISTS ({live}) AND {_approval_archive_ok(d, user)})))"
		f" OR ({d}.deleted_doctype = 'Comment' AND {_linked_archive_ok(d, user, 'Comment')})"
		f" OR ({d}.deleted_doctype = 'File' AND {_linked_archive_ok(d, user, 'File')}))"
	)


def version_query_conditions(user=None):
	"""A Comment or File Version follows its live row (approval-linked rows follow
	their approval), else that row's archives; a Version whose row left neither
	(bulk delete_dynamic_links, raw deletes) cannot be classified and is root-only,
	on any doctype."""
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	v, ar = "`tabVersion`", frappe.db.escape(AR)
	comment = f"SELECT 1 FROM `tabComment` vc WHERE vc.name = {v}.docname"
	comment_archive = f"SELECT 1 FROM `tabDeleted Document` vd WHERE vd.deleted_doctype = 'Comment' AND vd.deleted_name = {v}.docname"
	file = f"SELECT 1 FROM `tabFile` vf WHERE vf.name = {v}.docname"
	file_archive = f"SELECT 1 FROM `tabDeleted Document` vfd WHERE vfd.deleted_doctype = 'File' AND vfd.deleted_name = {v}.docname"
	return (
		f"(COALESCE({v}.ref_doctype, '') NOT IN ({ar}, 'Comment', 'File')"
		f" OR ({v}.ref_doctype = {ar} AND {_approval_visible(f'{v}.docname', user)})"
		f" OR ({v}.ref_doctype = 'Comment' AND (EXISTS ({comment} AND (COALESCE(vc.reference_doctype, '') <> {ar}"
		f" OR {_approval_visible('vc.reference_name', user)}))"
		f" OR (NOT EXISTS ({comment}) AND EXISTS ({comment_archive})"
		f" AND {_all_pass(comment_archive, _linked_archive_ok('vd', user, 'Comment'))})))"
		f" OR ({v}.ref_doctype = 'File' AND (EXISTS ({file} AND (COALESCE(vf.attached_to_doctype, '') <> {ar}"
		f" OR (COALESCE(vf.attached_to_name, '') <> '' AND {_approval_visible('vf.attached_to_name', user)})))"
		f" OR (NOT EXISTS ({file}) AND EXISTS ({file_archive})"
		f" AND {_all_pass(file_archive, _linked_archive_ok('vfd', user, 'File'))}))))"
	)


def _row_allowed(doctype: str, conditions, doc, user: str | None, ptype: str) -> bool:
	"""The list condition, evaluated for one stored row (parity by construction). A
	named row that is gone is not readable; creating one is not this hook's call
	(the framework writes these rows with ignore_permissions)."""
	user = user or frappe.session.user
	name = doc if isinstance(doc, str) else doc.get("name")
	if user == "Administrator" or not name:
		return True
	row = frappe.db.sql(f"SELECT ({conditions(user)}) FROM `tab{doctype}` WHERE name = %s", name)
	if not row:
		return ptype == "create"
	return bool(row[0][0])


def has_version_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	return _row_allowed("Version", version_query_conditions, doc, user, ptype)


def has_deleted_document_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	return _row_allowed("Deleted Document", deleted_document_query_conditions, doc, user, ptype)


AUDIT_LOOKUP_INDEX = "jarvis_deleted_doctype_name"


def ensure_audit_lookup_index() -> None:
	"""The classifier's archive lookups are equality seeks on (deleted_doctype,
	deleted_name); index them on the core table. Idempotent. DDL: commits, may wait
	on a metadata lock behind open transactions on the table. Kept on rollback; old
	code ignores it. Schema sync never drops a composite index."""
	if not frappe.db.table_exists("Deleted Document"):
		return
	frappe.db.add_index(
		"Deleted Document", ["deleted_doctype", "deleted_name"], index_name=AUDIT_LOOKUP_INDEX
	)
