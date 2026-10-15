"""Hand a skill to another owner (``skills_admin_api.admin_handover``, jarvis-admin-v2#720
piece 5b) and the people it can go to (``admin_handover_targets``).

A skill has no runs to stop, so unlike the macro hand-over this is ONE transaction,
under ``skills_admin.locked`` (the Pending request rows, the catalog lock for a shared
skill, the skill row): the owner the dialog showed, the target's checks and a LOCKING
read for a same-name clash, all read under the lock; the owner write and what must not
follow the skill to its new owner; the old owner's Pending requests rejected; the audit
row and the Comment; one commit. Then both owners' personal-skill counts are dropped and
both are told (never the acting admin about their own act).

The skill arrives switched off and disarmed, with no shares, no File Box opt-in (a
private skill's), no open assignments, nobody following it and no hold. Only its new
owner (a reviewer, for a Role or company skill) turns any of that back on.

Lineage: a private source of a published company copy keeps its ``name``, and the
copy's ``source_skill`` holds that name, untouched here. So the copy's lineage author
(``skills_admin.lineage_author``) and ``request_skill_promotion``'s owner check now
resolve to the new owner: the right to publish an update moves with the skill.

An admin may hand a skill to themselves (the way to edit someone's private skill) and
may hand their own skill away. Both are audited.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.query_builder.functions import Count

from jarvis.chat import list_filters, skills_admin
from jarvis.chat.custom_skills import set_skill_raw
from jarvis.chat.skills_admin import HOLD_FIELD, HOLD_REASON_FIELD, SKILL
from jarvis.jarvis.doctype.jarvis_custom_skill import jarvis_custom_skill as skill_doctype
from jarvis.jarvis.doctype.jarvis_skill_admin_log import jarvis_skill_admin_log as admin_log

SHARE = "Jarvis Custom Skill Share"
FILE_BOX_FIELD = "use_in_file_box"
# How many people the hand-over dialog's search offers at a time.
TARGETS_MAX = 20


class SkillHandoverError(frappe.ValidationError):
	"""The skill cannot go to that user."""


class SkillOwnerChangedError(SkillHandoverError):
	"""The skill changed hands since the dialog showed it. Its own class, so the dialog
	offers Reload by the response's ``exc_type``, never by reading the message."""


def available() -> bool:
	"""Whether the audit log has the hand-over's column (the 5b migrate ran)."""
	return admin_log.handover_available()


# --------------------------------------------------------------------------- #
# The act
# --------------------------------------------------------------------------- #
def handover(row, new_owner: str, expected_owner: str) -> dict:
	"""Hand ``row`` to ``new_owner`` (module docstring). ``expected_owner`` is the owner
	the dialog showed: a skill that changed hands since is refused with nothing written.
	``{ok, new_owner, cleared, hold_lifted, requests_cancelled, needs_apply}``."""
	# A plain answer before any lock, then again under it on the target's LOCKED rows.
	refuse(row, new_owner, expected_owner, _target_rows(new_owner))
	with skills_admin.locked(row, requests=True, extra_fields=_extra_fields()) as cur:
		refuse(cur, new_owner, expected_owner, _target_rows(new_owner, lock=True))
		needs_apply = skills_admin.in_company_catalog(cur.name)
		cleared = _clear(cur.name)
		values, flags, held_for = _owner_values(cur, new_owner)
		cleared.update(flags)
		set_skill_raw(cur.name, values, update_modified=True)  # modified_by: the admin
		skills_admin.reject_pending(cur.pending, _("Rejected: the skill was handed to another owner."))
		_record(cur, new_owner, held_for, needs_apply)
		skills_admin.commit_act()
	skills_admin.after_commit(cur, "handover", new_owner=new_owner)
	_tell_both_owners(cur, new_owner, hold_lifted=held_for is not None)
	return {
		"ok": True,
		"new_owner": new_owner,
		"cleared": cleared,
		"hold_lifted": held_for is not None,
		"requests_cancelled": len(cur.pending),
		"needs_apply": needs_apply,
	}


def refuse(row, new_owner: str, expected_owner: str, owned: list) -> None:
	"""Refuse a skill that changed hands since the dialog showed it, then anything
	``refusal`` names. ``owned``: the target's skill rows (``_target_rows``)."""
	if row.owner != expected_owner:
		frappe.throw(_("This skill changed while you acted on it. Reload."), SkillOwnerChangedError)
	reason = refusal(row, new_owner, owned)
	if reason:
		frappe.throw(frappe.utils.escape_html(reason), SkillHandoverError)


def refusal(row, new_owner: str, owned: list) -> str:
	"""Why ``row`` cannot go to ``new_owner``, or "". Read as the database is now: called
	again under the lock. The target rules are the macro hand-over's
	(``macros_admin_api._handover_refusal``): the Jarvis User ROLE, because only it has a
	permission row on the skill doctype; a skill held by anyone else could never be
	switched on, edited or deleted by them. The per-owner cap is checked here because the
	controller checks it on insert only; it and the same-name clash are read off
	``owned``, the target's rows (locked, under the lock)."""
	if skills_admin.is_learned(row):
		return _("Learned skills are managed on the learning board, not here.")
	if skills_admin.is_published(row):
		return _("This is a published company copy. Hand over the private skill it was published from.")
	if new_owner == row.owner:
		return _("{0} already owns this skill.").format(new_owner)
	reason = _target_refusal(new_owner)
	if reason:
		return reason
	if skills_admin.is_shared(row) and not _is_reviewer(new_owner):
		return _("{0} is not a skill reviewer, and only a reviewer may own a Role or company skill.").format(
			new_owner
		)
	cap = skill_doctype.MAX_SKILLS_PER_OWNER
	if len(owned) >= cap:
		return _("{0} already has {1} skills, the most one user can have.").format(new_owner, cap)
	if any(r.skill_name == row.skill_name and r.name != row.name for r in owned):
		return _("{0} already has a skill named {1}.").format(new_owner, row.skill_name)
	return ""


def _target_refusal(user: str) -> str:
	from jarvis.permissions import JARVIS_USER_ROLE, has_jarvis_access, is_valid_unattended_owner

	if user in ("Administrator", "Guest"):
		return _("A skill cannot be handed to {0}. Choose a named user.").format(user)
	if not is_valid_unattended_owner(user):
		return _("{0} is disabled.").format(user)
	if not has_jarvis_access(user):
		return _("{0} does not have access to Jarvis.").format(user)
	if JARVIS_USER_ROLE not in frappe.get_roles(user):
		return _("{0} does not have the Jarvis User role, so they could not edit or use the skill.").format(
			user
		)
	return ""


def _is_reviewer(user: str) -> bool:
	from jarvis.permissions import is_skill_reviewer

	return is_skill_reviewer(user)


def _target_rows(user: str, *, lock: bool = False) -> list:
	"""``user``'s skill rows (name, skill_name), for the cap and the same-name clash in
	one read. Neither is an index (the cap is counted on insert, uniqueness is a SELECT
	in ``_validate_unique_per_owner``), so under the lock this is a LOCKING read: it
	sees a row the target committed after this transaction's snapshot (or fails with
	1020 where snapshot isolation is on, refused as "busy"). Nothing indexes ``owner``,
	so the read next-key-locks the rows it scans until the commit: skill inserts wait
	behind a hand-over for that moment, which is acceptable for a rare admin act on a
	narrow table."""
	s = frappe.qb.DocType(SKILL)
	query = frappe.qb.from_(s).select(s.name, s.skill_name).where(s.owner == user)
	if not lock:
		return query.run(as_dict=True)
	with skills_admin.busy_as_retry():
		return query.for_update().run(as_dict=True)


def _extra_fields() -> tuple:
	"""Read with the row under the lock: the File Box opt-in the write may clear. Code
	served ahead of the File Box migrate never names the column."""
	from jarvis.chat.pending_actions._store import filebox_migrated

	return (FILE_BOX_FIELD,) if filebox_migrated() else ()


# --------------------------------------------------------------------------- #
# The writes
# --------------------------------------------------------------------------- #
def _owner_values(cur, new_owner: str) -> tuple[dict, dict, str | None]:
	"""The raw write on the skill row; ``{armed, file_box}``: whether it was armed and
	whether its File Box opt-in was cleared; the reason of a hold it lifts (None when it
	was not held).

	The File Box opt-in is cleared on a private skill only: a private skill serves its
	owner's File Box drops by itself, so the new owner opts in on purpose. A Role or
	company skill's File Box setting is the reviewed audience's, and stays."""
	values = {"owner": new_owner, "enabled": 0, "allow_approve_run": 0, "_assign": None}
	armed = int(bool(frappe.utils.cint(cur.allow_approve_run)))
	file_box = int(not skills_admin.is_shared(cur) and bool(frappe.utils.cint(cur.get(FILE_BOX_FIELD))))
	if file_box:
		values[FILE_BOX_FIELD] = 0
	held_for = None
	if skills_admin.hold_fields_exist() and frappe.utils.cint(cur.get(HOLD_FIELD)):
		held_for = cur.get(HOLD_REASON_FIELD) or ""
		values.update({HOLD_FIELD: 0, HOLD_REASON_FIELD: None})
	return values, {"armed": armed, "file_box": file_box}, held_for


def _clear(skill: str) -> dict:
	"""Remove what must not follow the skill to its new owner. ``shares``: the share
	list's rows and Frappe's document shares; ``assignments``: open assignments
	cancelled (a closed one is history and stays) and follows removed."""
	shares = _delete(SHARE, {"parent": skill, "parenttype": SKILL})
	shares += _delete("DocShare", {"share_doctype": SKILL, "share_name": skill})
	assignments = _delete("Document Follow", {"ref_doctype": SKILL, "ref_docname": skill})
	open_todos = {"reference_type": SKILL, "reference_name": skill, "status": "Open"}
	todos = frappe.db.count("ToDo", open_todos)
	if todos:
		frappe.db.set_value("ToDo", open_todos, "status", "Cancelled")
	return {"shares": shares, "assignments": assignments + todos}


def _delete(doctype: str, filters: dict) -> int:
	count = frappe.db.count(doctype, filters)
	if count:
		frappe.db.delete(doctype, filters)
	return count


def _record(cur, new_owner: str, held_for: str | None, needs_apply: bool) -> None:
	"""The audit row and the Comment. A lifted hold is named in both."""
	lifted = _("Lifted the hold set for: {0}").format(held_for) if held_for is not None else ""
	admin_log.write(
		verb="handover",
		skill=cur.name,
		skill_name=cur.skill_name,
		skill_owner=cur.owner,
		new_owner=new_owner,
		reason=lifted,
		needs_apply=needs_apply,
	)
	text = f"{frappe.session.user} handed this skill over from {cur.owner} to {new_owner}."
	if lifted:
		text += f" {lifted}"
	skills_admin.comment(cur.name, text)


def _tell_both_owners(cur, new_owner: str, *, hold_lifted: bool) -> None:
	"""A notice to each owner, no document named, never to the admin about their own
	act (handing a skill to oneself, or one's own skill away). A hand-over lifts a hold
	by design, and both notices say so."""
	from jarvis.permissions import is_valid_unattended_owner

	admin = frappe.session.user
	kind = skills_admin.ADMIN_HANDOVER_NOTICE
	lifted = (" " + _("The hold on it was lifted.")) if hold_lifted else ""
	if cur.owner != admin and is_valid_unattended_owner(cur.owner):
		name = frappe.utils.escape_html(_full_name(new_owner))
		body = _("An admin handed your skill {0} to {1}.").format(cur.skill_name, name)
		skills_admin.notify_user(cur.owner, cur, kind, body + lifted)
	if new_owner != admin:
		body = _("An admin handed you the skill {0}. It arrives switched off.").format(cur.skill_name)
		skills_admin.notify_user(new_owner, cur, kind, body + lifted)


def _full_name(user: str) -> str:
	return frappe.db.get_value("User", user, "full_name") or user


# --------------------------------------------------------------------------- #
# Who a skill can go to
# --------------------------------------------------------------------------- #
def stored_user(name: str) -> str | None:
	"""``name`` as ``User.name`` is stored. The database matches a name whatever its
	case and Python does not: written as typed, ``Asha@x`` would own a skill that
	``asha@x`` (the session user) does not."""
	return frappe.db.get_value("User", {"name": name}, "name")


def targets(search: str, exclude: str | None = None) -> dict:
	"""Enabled desk users with the Jarvis User role, not Administrator or Guest, nor
	``exclude`` (the skill's current owner), whose address or full name matches
	``search``, by full name, at most ``TARGETS_MAX``. ``at_cap``: they own the most
	skills one user can have (``cap``, the dialog says it). ``more``: others match too.
	The hand-over checks again.

	``frappe.qb`` skips permissions on purpose: the caller is behind
	``require_jarvis_admin``, and a Jarvis Admin who is not a System Manager may not read
	User rows through ``get_list``."""
	from jarvis.permissions import JARVIS_USER_ROLE

	u = frappe.qb.DocType("User")
	r = frappe.qb.DocType("Has Role")
	q = f"%{list_filters.escape_like(search)}%"
	where = (
		(u.enabled == 1)
		& (u.user_type == "System User")
		& u.name.notin(("Administrator", "Guest"))
		& (u.name.like(q) | u.full_name.like(q))
	)
	if exclude:
		where &= u.name != exclude
	query = (
		frappe.qb.from_(u)
		.join(r)
		.on((r.parent == u.name) & (r.parenttype == "User") & (r.role == JARVIS_USER_ROLE))
		.select(u.name, u.full_name)
		.distinct()
		.where(where)
		.orderby(u.full_name)
		.orderby(u.name)
		.limit(TARGETS_MAX + 1)  # one more than shown says whether there are more
	)
	users = list_filters.bounded_sql(*query.walk(), as_dict=True)
	more = len(users) > TARGETS_MAX
	users = users[:TARGETS_MAX]
	counts = _skill_counts([x.name for x in users])
	cap = skill_doctype.MAX_SKILLS_PER_OWNER
	return {
		"targets": [
			{"user": x.name, "full_name": x.full_name or x.name, "at_cap": counts.get(x.name, 0) >= cap}
			for x in users
		],
		"more": more,
		"cap": cap,
	}


def _skill_counts(users: list[str]) -> dict:
	"""``{user: how many skills they own}``, one grouped query."""
	if not users:
		return {}
	s = frappe.qb.DocType(SKILL)
	rows = (
		frappe.qb.from_(s)
		.select(s.owner, Count("*").as_("n"))
		.where(s.owner.isin(users))
		.groupby(s.owner)
		.run(as_dict=True)
	)
	return {row.owner: row.n for row in rows}
