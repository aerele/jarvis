"""What the skill admin verbs do (``jarvis.chat.skills_admin_api``): hold, release and
delete another user's skill, each as ONE transaction, and what follows the commit
(the owner's notice, the operator's log line). The hand-over (``skills_handover``)
takes the same locks through ``locked``.

Locks, in the order ``decide_skill_promotion`` takes them, so the two cannot wait on
each other (jarvis-admin-v2#720, plan check item 3):

1. the skill's Pending promotion request rows: their names read plainly (the first
   read of a fresh transaction), then exactly those rows locked by name, FOR UPDATE.
   By name, so record locks only: a range read on (skill, status) would also lock the
   gap ``request_skill_promotion`` inserts into while it holds the skill row, and the
   two would deadlock (1213);
2. the catalog lock (``custom_skills_api._catalog_lock``), for a Role or company skill;
3. the skill row, FOR UPDATE, read again: everything decided after it is the present;
4. the Pending names again, as a LOCKING read (a plain one reads step 1's snapshot).
   ``request_skill_promotion`` takes the skill row before it files a request, so none
   can be in flight now and the range lock waits on nobody. A request filed between
   steps 1 and 3 fails this read (1020) or changes the set: "try again".

The act commits once, inside the catalog lock (held through the commit, as the
approval holds it). Nothing here pushes to the assistant: the answer says
``needs_apply`` and the admin presses Apply (D1).
"""

from __future__ import annotations

import contextlib

import frappe
from frappe import _

from jarvis.chat.custom_skills import MANAGED_OWNER, set_skill_raw
from jarvis.jarvis.doctype.jarvis_custom_skill import jarvis_custom_skill as skill_doctype
from jarvis.jarvis.doctype.jarvis_skill_admin_log import jarvis_skill_admin_log as admin_log

SKILL = "Jarvis Custom Skill"
PROMO = "Jarvis Skill Promotion Request"
HOLD_FIELD = skill_doctype.HOLD_FIELD
HOLD_REASON_FIELD = skill_doctype.HOLD_REASON_FIELD

# The owner's notices (Notification Log subjects start with one of these). The Skills
# list shows them until dismissed, as the Macros list does (``macros_api.admin_notices``).
ADMIN_HOLD_NOTICE = "Skill put on hold by an admin"
ADMIN_RELEASE_NOTICE = "Skill released by an admin"
ADMIN_DELETE_NOTICE = "Skill deleted by an admin"
ADMIN_HANDOVER_NOTICE = "Skill handed over by an admin"
REVIEWER_EDIT_NOTICE = "Skill changed by a skill reviewer"
NOTICE_KINDS = (
	ADMIN_HOLD_NOTICE,
	ADMIN_RELEASE_NOTICE,
	ADMIN_DELETE_NOTICE,
	ADMIN_HANDOVER_NOTICE,
	REVIEWER_EDIT_NOTICE,
)

ROW_FIELDS = (
	"name",
	"owner",
	"skill_name",
	"scope",
	"target_role",
	"enabled",
	"allow_approve_run",
	"managed_by_learning",
	"source_skill",
)


def hold_fields_exist() -> bool:
	"""Through the controller module's attribute, so one patch covers every reader."""
	return skill_doctype.hold_fields_exist()


def admin_log_exists() -> bool:
	"""Whether the audit doctype's table exists (it arrives with the same migrate)."""
	return frappe.db.table_exists(admin_log.DT)


# --------------------------------------------------------------------------- #
# The acts
# --------------------------------------------------------------------------- #
def hold(row, reason: str) -> dict:
	"""Put ``row`` on hold: off, disarmed, its Pending promotion request rejected, the
	audit row and the Comment, one commit. A repeat updates the reason."""
	admin = frappe.session.user
	refuse(row, _("Switch it off on the skill's own page instead."))
	used = used_by_macros(row.name)  # informational: kept out of the locked transaction
	with locked(row, requests=True) as cur:
		refuse(cur, _("Switch it off on the skill's own page instead."))
		needs_apply = in_company_catalog(cur.name)
		set_skill_raw(
			cur.name,
			{HOLD_FIELD: 1, HOLD_REASON_FIELD: reason, "enabled": 0, "allow_approve_run": 0},
			update_modified=True,
		)
		reject_pending(cur.pending, _("Rejected: an admin put this skill on hold."))
		admin_log.write(
			verb="hold",
			skill=cur.name,
			skill_name=cur.skill_name,
			skill_owner=cur.owner,
			reason=reason,
			needs_apply=needs_apply,
		)
		comment(cur.name, f"{admin} put this skill on hold: {reason}")
		commit_act()
	after_commit(cur, "hold")
	# Not the reason: Desk renders a notice's body as HTML and the reason is free text.
	# The owner reads it on the skill, through the skill APIs.
	notify(
		cur,
		ADMIN_HOLD_NOTICE,
		_("An admin put your skill {0} on hold. The reason is shown on the skill.").format(cur.skill_name),
	)
	return {"ok": True, "needs_apply": needs_apply, "used_by_macros": used}


def release(row, restore: bool) -> dict:
	"""Lift the hold. ``restore`` (a Role or company skill only) switches it back on;
	arming is never restored. A skill that is not held is left as it is."""
	refuse(row, _("Another admin has to release it."))
	if restore and not is_shared(row):
		frappe.throw(
			_(
				"Only a Role or company skill is switched back on here. Its owner switches a private skill on."
			),
			frappe.ValidationError,
		)
	admin = frappe.session.user
	with locked(row, requests=False) as cur:
		refuse(cur, _("Another admin has to release it."))
		if not frappe.utils.cint(cur.get(HOLD_FIELD)):
			return {"ok": True, "restored": False, "needs_apply": False}
		values = {HOLD_FIELD: 0, HOLD_REASON_FIELD: None}
		if restore:
			values["enabled"] = 1
		set_skill_raw(cur.name, values, update_modified=True)
		needs_apply = bool(restore) and in_company_catalog(cur.name)
		admin_log.write(
			verb="release",
			skill=cur.name,
			skill_name=cur.skill_name,
			skill_owner=cur.owner,
			needs_apply=needs_apply,
		)
		comment(
			cur.name,
			f"{admin} released the hold on this skill" + (" and switched it back on." if restore else "."),
		)
		commit_act()
	after_commit(cur, "release")
	notify(
		cur, ADMIN_RELEASE_NOTICE, _("An admin released the hold on your skill {0}.").format(cur.skill_name)
	)
	return {"ok": True, "restored": bool(restore), "needs_apply": needs_apply}


def delete(row) -> dict:
	"""Delete ``row``: its Pending request rejected, the audit row FIRST (it must outlive
	the row), then the delete with permissions ignored, one commit, then the notice
	(no document link: there is no document)."""
	refuse(row, _("Delete it from your Skills list instead."))
	used = used_by_macros(row.name)  # informational: kept out of the locked transaction
	with locked(row, requests=True) as cur:
		refuse(cur, _("Delete it from your Skills list instead."))
		needs_apply = in_company_catalog(cur.name)
		reject_pending(cur.pending, _("Rejected: an admin deleted this skill."))
		admin_log.write(
			verb="delete",
			skill=cur.name,
			skill_name=cur.skill_name,
			skill_owner=cur.owner,
			needs_apply=needs_apply,
		)
		frappe.delete_doc(SKILL, cur.name, ignore_permissions=True)
		commit_act()
	after_commit(cur, "delete")
	notify(cur, ADMIN_DELETE_NOTICE, _("An admin deleted your skill {0}.").format(cur.skill_name))
	return {"ok": True, "needs_apply": needs_apply, "used_by_macros": used}


def delete_own(row) -> None:
	"""The owner's delete (``custom_skills_api``, single and bulk). The request's Link
	is exempt from link checks (``hooks.ignore_links_on_delete``), so a Pending request
	would outlive the skill as one no reviewer can approve: it is rejected first, under
	the admin delete's locks, one commit. A failure undoes the row's work, so the bulk
	delete never commits a rejection whose skill survived."""
	with locked(row, requests=True) as cur:
		try:
			reject_pending(cur.pending, _("Rejected: the owner deleted this skill."))
			frappe.delete_doc(SKILL, cur.name)  # the owner's own permission, as before
		except Exception:
			frappe.db.rollback()
			raise
		commit_act()


# --------------------------------------------------------------------------- #
# Kinds and refusals
# --------------------------------------------------------------------------- #
def load(name: str) -> frappe._dict:
	"""The skill's row by a name already checked to be text, or "deleted"."""
	row = frappe.db.get_value(SKILL, {"name": name}, list(ROW_FIELDS), as_dict=True)
	if not row:
		frappe.throw(_("This skill was deleted."), frappe.DoesNotExistError)
	return row


def scope_of(row) -> str:
	return skill_doctype.skill_scope(row)


def is_shared(row) -> bool:
	return scope_of(row) in ("Role", "Org")


def is_learned(row) -> bool:
	return bool(frappe.utils.cint(row.get("managed_by_learning")))


def is_published(row) -> bool:
	"""A company copy a promotion published: owned by the system identity, not learned,
	with a lineage source. Desk-made Administrator skills have no source."""
	return row.get("owner") == MANAGED_OWNER and not is_learned(row) and bool(row.get("source_skill"))


def lineage_author(row) -> str | None:
	"""Who the skill is "theirs": the owner, or for a published copy the owner of the
	private source it was promoted from (None once that source is deleted)."""
	if not is_published(row):
		return row.get("owner")
	return frappe.db.get_value(SKILL, row.source_skill, "owner")


def refuse(row, own_hint: str) -> None:
	"""Learned rows belong to the learning board. An admin's own skill (the lineage
	author's, for a published copy) is not theirs to hold: a hold they could not lift
	would lock them out, and one they could lift would hold nothing."""
	if is_learned(row):
		frappe.throw(_("Learned skills are managed on the learning board, not here."), frappe.ValidationError)
	if lineage_author(row) == frappe.session.user:
		frappe.throw(_("This is your own skill. {0}").format(own_hint), frappe.ValidationError)


# --------------------------------------------------------------------------- #
# Locks and writes
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def locked(row, *, requests: bool, extra_fields: tuple = ()):
	"""Hold the act's locks (module docstring) and yield the row as read under them,
	with ``pending``: the Pending request rows locked in step 1. ``extra_fields`` are
	read with ``ROW_FIELDS`` in the locking read."""
	from jarvis.chat import txn
	from jarvis.chat.custom_skills_api import _catalog_lock

	hold_fields_exist()  # warm the meta: a miss below the lock would open a snapshot
	txn.fresh_snapshot(owned=True)
	pending = lock_requests(pending_requests(row.name)) if requests else []
	shared = is_shared(row)
	with _catalog_lock() if shared else contextlib.nullcontext():
		cur = _lock_row(row.name, *extra_fields)
		moved = requests and set(pending_requests(cur.name, lock=True)) != set(pending)
		if moved or is_shared(cur) != shared:
			frappe.throw(_("This skill changed while you acted on it. Try again."), frappe.ValidationError)
		cur.pending = pending
		yield cur


@contextlib.contextmanager
def busy_as_retry():
	"""A locking read that waited too long, or lost a race (1213, or 1020 on a row
	changed since the snapshot, ``jarvis.chat.txn``), refused as a sentence to act on."""
	try:
		yield
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		frappe.throw(_("This skill is busy right now. Try again in a moment."), frappe.ValidationError)


def pending_requests(skill: str, *, lock: bool = False) -> list[str]:
	"""The skill's Pending request names. ``lock`` only once the skill row is held
	(module docstring, step 4): before it, the range lock can deadlock."""
	P = frappe.qb.DocType(PROMO)
	query = frappe.qb.from_(P).select(P.name).where((P.skill == skill) & (P.status == "Pending"))
	if not lock:
		return query.run(pluck=True)
	with busy_as_retry():
		return query.for_update().run(pluck=True)


def lock_requests(names: list[str]) -> list[str]:
	"""Lock exactly these request rows by name (record locks, no gap); none, no query."""
	if not names:
		return []
	P = frappe.qb.DocType(PROMO)
	with busy_as_retry():
		frappe.qb.from_(P).select(P.name).where(P.name.isin(names)).for_update().run()
	return list(names)


def _lock_row(name: str, *extra_fields: str) -> frappe._dict:
	fields = [*ROW_FIELDS, *extra_fields]
	if hold_fields_exist():
		fields += [HOLD_FIELD, HOLD_REASON_FIELD]
	with busy_as_retry():
		row = frappe.db.get_value(SKILL, name, fields, as_dict=True, for_update=True)
	if not row:
		frappe.throw(_("This skill was deleted."), frappe.DoesNotExistError)
	return row


def reject_pending(requests: list[str], note: str) -> None:
	"""Reject the locked Pending requests with a raw write: ``_stamp_decision`` commits,
	and the act must stay one transaction (plan check item 2)."""
	if not requests:
		return
	frappe.db.set_value(
		PROMO,
		{"name": ("in", requests)},
		{
			"status": "Rejected",
			"reviewer": frappe.session.user,
			"decided_at": frappe.utils.now_datetime(),
			"decision_note": note[:140],
		},
	)


def comment(skill: str, text: str) -> None:
	"""An Info Comment on the skill's timeline, for people looking at the skill."""
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"comment_email": frappe.session.user,
			"reference_doctype": SKILL,
			"reference_name": skill,
			"content": frappe.utils.escape_html(text),
		}
	).insert(ignore_permissions=True)


def commit_act() -> None:
	"""The act's one commit. Here and not at the request's end because the catalog lock
	(redis) must be held THROUGH the commit, as ``decide_skill_promotion`` holds it, and
	the owner's notice must follow a committed act."""
	try:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- held through the catalog lock
	except Exception:
		frappe.db.rollback()
		raise


def after_commit(row, verb: str, *, new_owner: str | None = None) -> None:
	"""Drop the owner's personal-skill count (``jarvis:pskills:{user}``) once the change
	is committed, so a recount cannot cache the old state, and log the act (ids only).
	A hand-over passes ``new_owner``: their count changed too."""
	from jarvis.chat.custom_skills import personal_skills_cache_key

	for user in (row.get("owner"), new_owner):
		if user:
			frappe.cache().delete_value(personal_skills_cache_key(user))
	line = f"skills_admin.{verb} skill={row.name} owner={row.get('owner')} admin={frappe.session.user}"
	if new_owner:
		line += f" new_owner={new_owner}"
	frappe.logger("jarvis").info(line)


# --------------------------------------------------------------------------- #
# Reads the acts answer with
# --------------------------------------------------------------------------- #
def in_company_catalog(name: str) -> bool:
	"""Whether the skill is in the company catalog the Apply pushes (the exact
	``build_push_payload`` eligibility, uncapped): an act that changes that membership
	is waiting for the next Apply."""
	from jarvis.chat.custom_skills import is_pushable_org_row

	return is_pushable_org_row(name)


def used_by_macros(name: str) -> dict:
	"""``{total, armed}``: how many macros have a step that applies the skill, and how
	many of those run without asking (D9). Macro Step ``skills`` is a JSON list of
	skill names, so this is a LIKE scan on the quoted name (plan check item 14)."""
	from jarvis.chat.list_filters import escape_like

	step = frappe.qb.DocType("Jarvis Macro Step")
	macro = frappe.qb.DocType("Jarvis Macro")
	rows = (
		frappe.qb.from_(step)
		.join(macro)
		.on(macro.name == step.parent)
		.select(macro.name, macro.skip_confirmation)
		.distinct()
		.where((step.parenttype == "Jarvis Macro") & step.skills.like(f'%"{escape_like(name)}"%'))
		.run(as_dict=True)
	)
	return {"total": len(rows), "armed": sum(1 for r in rows if frappe.utils.cint(r.skip_confirmation))}


def catalog_update_pending() -> bool:
	"""Whether an admin act changed the company catalog after the last Apply (D1):
	derived, not stored. The audit log's ``needs_apply`` rows newer than
	``custom_skills_synced_at``. A failed Apply stamps that time too (there is no separate
	success stamp), so the stamp counts only while the sync status reads ``ok``."""
	if not admin_log_exists():
		return False
	synced_at, status = frappe.db.get_value(
		"Jarvis Settings", "Jarvis Settings", ["custom_skills_synced_at", "custom_skills_sync_status"]
	)
	if not (status or "").startswith("ok"):
		synced_at = None
	log = frappe.qb.DocType(admin_log.DT)
	query = frappe.qb.from_(log).select(log.name).where(log.needs_apply == 1).limit(1)
	if synced_at:
		query = query.where(log.acted_at > synced_at)
	return bool(query.run())


# --------------------------------------------------------------------------- #
# Notices
# --------------------------------------------------------------------------- #
def notify(row, kind: str, body: str, users: set[str] | None = None) -> None:
	"""A Notification Log for whoever the skill is theirs: the owner, or for a published
	copy its lineage author and the skill reviewers (the reviewers only once the source
	is gone; D5, plan check item 9), or ``users`` when given. Never the acting admin. No
	document link. Each one in a savepoint: a failure is logged and never fails the
	committed act."""
	recipients = _recipients(row) if users is None else _valid(users)
	for user in sorted(recipients - {frappe.session.user}):
		notify_user(user, row, kind, body)


def notify_user(user: str, row, kind: str, body: str) -> None:
	"""One notice of ``notify``'s, to ``user``, in its own savepoint."""
	savepoint = "jarvis_skill_admin_notice"
	try:
		frappe.db.savepoint(savepoint)
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				# The desk bell renders a subject as HTML.
				"subject": f"{kind}: {frappe.utils.escape_html(row.skill_name)}"[:140],
				"email_content": body,
			}
		).insert(ignore_permissions=True)
		frappe.db.release_savepoint(savepoint)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			title=f"jarvis.chat.skills_admin.notify_failed: {row.name}", message=frappe.get_traceback()
		)


def _recipients(row) -> set[str]:
	if is_published(row):
		users = _skill_reviewers()
		author = lineage_author(row)
		if author:
			users.add(author)
	else:
		users = {row.owner} if row.get("owner") else set()
	return _valid(users)


def author_or_reviewers(row) -> set[str]:
	"""Who hears of a reviewer's change to ``row`` (5c): its author (``lineage_author``),
	or for a published copy whose author is gone or cannot be told, the skill
	reviewers (D5)."""
	author = lineage_author(row)
	users = _valid({author} if author else set())
	if not users and is_published(row):
		users = _valid(_skill_reviewers())
	return users


def _valid(users) -> set[str]:
	from jarvis.permissions import is_valid_unattended_owner

	return {u for u in users if u and is_valid_unattended_owner(u)}


def _skill_reviewers() -> set[str]:
	from frappe.utils.user import get_users_with_role

	from jarvis.permissions import JARVIS_REVIEWER_ROLES

	users: set[str] = set()
	for role in JARVIS_REVIEWER_ROLES:
		users |= set(get_users_with_role(role))
	return users


def owner_notices(user: str, limit: int) -> list[dict]:
	"""The user's unread notices of admin acts on their skills, newest first."""
	rows = frappe.get_list(
		"Notification Log",
		filters=_notice_filters(user),
		or_filters=notice_kinds(),
		fields=["name", "email_content", "creation"],
		order_by="creation desc",
		limit=limit,
	)
	return [{"name": r.name, "message": r.email_content or "", "creation": str(r.creation)} for r in rows]


def _notice_filters(user: str) -> dict:
	return {"for_user": user, "read": 0}


def notice_kinds() -> list:
	"""``or_filters``: a subject that starts with one of the kinds above."""
	from jarvis.chat.list_filters import escape_like

	return [["subject", "like", f"{escape_like(kind)}:%"] for kind in NOTICE_KINDS]


def dismiss_notices(user: str, names: list[str] | None) -> int:
	"""Mark ``user``'s own notices of these kinds read: those named, or all when
	``names`` is None. An empty list marks none."""
	if names is not None and not names:
		return 0
	filters = _notice_filters(user)
	if names is not None:
		filters["name"] = ["in", names]
	marked = frappe.get_list("Notification Log", filters=filters, or_filters=notice_kinds(), pluck="name")
	if marked:
		frappe.db.set_value("Notification Log", {"name": ("in", marked)}, "read", 1, update_modified=False)
	return len(marked)
