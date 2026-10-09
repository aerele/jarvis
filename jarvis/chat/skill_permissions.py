"""Scope-ladder visibility for ``Jarvis Custom Skill`` at the ORM (security
review PART 2, TASK 13).

The visibility rule ({owner OR shared OR scope=Org (unless role-narrowed) OR
scope=Role role-match}) used to live in FOUR hand-rolled read surfaces that
disagreed (the plugin find/get honored ``allowed_roles``; the SPA list/get did
not; generic REST was only accidentally safe because ``if_owner`` hid
everything, so an owner couldn't even REST-list a skill shared to them). This
module lifts the ONE rule (``jarvis_custom_skill.user_can_use_skill``) to the
ORM so list/report/generic-REST queries (``permission_query_conditions``) and
per-doc access (``has_permission``) inherit it automatically — exactly the
``jarvis/chat/wiki_permissions.py`` pattern.

Writes stay owner-only (create/save/delete of your own row); scope WIDENING is
guarded in the controller (``_guard_scope_change`` / ``_guard_new_scope``), and so
is what a shared (Role/Org) skill says and whom it reaches
(``_guard_content_change``), so both hold under ``ignore_permissions`` too. Reviewer/compiler writes (the promotion
decide + insight-apply + compiler upsert) go through ``ignore_permissions`` and
so never consult ``has_permission``.

Every interpolated value in the SQL fragment goes through ``frappe.db.escape``;
role names / user emails are org-author-controlled strings.

NOTE (hooks can only DENY): a falsy ``has_permission`` return denies, so every
allow path returns an explicit ``True`` to defer to the normal role-perm check.
"""

from __future__ import annotations

import re

import frappe

SKILL = "Jarvis Custom Skill"
SHARE = "Jarvis Custom Skill Share"
ALLOWED_ROLE = "Jarvis Custom Skill Allowed Role"

_READ_PTYPES = ("read", "select", "print", "email", "export", "share", "report")


def _is_sm(user: str) -> bool:
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def skill_query_conditions(user: str | None = None) -> str:
	"""hooks.permission_query_conditions — scopes every list/report/REST query to
	the caller's visible skill set. Empty (no restriction) for System Managers.
	Always a parenthesized valid boolean expression."""
	user = user or frappe.session.user
	if _is_sm(user):
		return ""
	table = f"`tab{SKILL}`"
	esc_user = frappe.db.escape(user)
	clauses = [
		f"{table}.`owner` = {esc_user}",
		# shared with me
		(
			f"exists (select 1 from `tab{SHARE}` sh "
			f"where sh.parent = {table}.`name` and sh.parenttype = {frappe.db.escape(SKILL)} "
			f"and sh.`user` = {esc_user})"
		),
		# Org (or legacy empty) with NO allowed_roles narrowing = everyone.
		(
			f"(coalesce({table}.`scope`, '') in ('', 'Org') and not exists "
			f"(select 1 from `tab{ALLOWED_ROLE}` ar where ar.parent = {table}.`name` "
			f"and ar.parenttype = {frappe.db.escape(SKILL)}))"
		),
	]
	roles = [r for r in frappe.get_roles(user) if r]
	if roles:
		role_list = ", ".join(frappe.db.escape(r) for r in roles)
		# Role-match via allowed_roles (managed learned rows + legacy Org narrowed
		# by roles). Never applies to User-scope rows (private).
		clauses.append(
			f"(coalesce({table}.`scope`, '') != 'User' and exists "
			f"(select 1 from `tab{ALLOWED_ROLE}` ar where ar.parent = {table}.`name` "
			f"and ar.parenttype = {frappe.db.escape(SKILL)} and ar.`role` in ({role_list})))"
		)
		# Role-scope via target_role.
		clauses.append(f"({table}.`scope` = 'Role' and {table}.`target_role` in ({role_list}))")
	return "(" + " or ".join(clauses) + ")"


def has_skill_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	"""hooks.has_permission — per-doc gate. Reads follow the scope-ladder
	visibility rule (``user_can_use_skill``); write/delete are owner-only (scope
	widening is separately controller-guarded); create defers to the role perm
	(owner is the creator; the controller caps a non-reviewer's new scope to
	User). True defers to the normal role-perm check (hooks can only deny)."""
	user = user or frappe.session.user
	if _is_sm(user):
		return True
	if ptype == "create":
		return True
	if ptype in _READ_PTYPES or ptype is None:
		# ptype is None when Frappe builds the FULL perm dict (get_doc_permissions);
		# resolve it as read-visibility so a visible skill isn't wrongly denied to a
		# non-owner (the actual write/delete op re-checks with an explicit ptype).
		from jarvis.jarvis.doctype.jarvis_custom_skill.jarvis_custom_skill import (
			user_can_use_skill,
		)

		return user_can_use_skill(doc, user)
	# write / delete / submit / cancel / amend: owner only. This branch MUST return
	# a bool (never frappe.throw) — get_doc_permissions calls this hook for every
	# ptype while building a doc's perm dict, so a throw here would break plain
	# reads. The clear user-facing "you can only edit your own skills" message is
	# raised by the SPA write endpoints (custom_skills_api._require_skill_owner);
	# generic REST writes fall back to Frappe's "does not have access" message.
	return (doc.get("owner") or "") == user


# --------------------------------------------------------------------------- #
# Who controls what a skill says (an armed macro's steps, ``jarvis_macro``)
# --------------------------------------------------------------------------- #
def reviewer_locked(skill) -> bool:
	"""Whether only a reviewer (or the learning compiler) can change what ``skill``
	says: a Role or Org skill (``JarvisCustomSkill._guard_content_change``). A blank
	scope on a stored row reads as Org and "Personal" as User, as in
	``user_can_use_skill``."""
	scope = (skill.get("scope") or "Org").strip() or "Org"
	return scope in ("Role", "Org")


def controlled_by(skill, user: str) -> bool:
	"""Whether what ``skill`` says can change only by ``user`` or by a reviewer: it is
	``user``'s own, or it is reviewer-locked. Anything else (another user's private
	skill shared with ``user``) its author can rewrite at any time."""
	return (skill.get("owner") or "") == user or reviewer_locked(skill)


def shared_skills_no_reviewer_owns() -> list[dict]:
	"""The Role and company skills (an old row with no scope is a company skill) whose
	owner is not a skill reviewer, each with who else has a skill of the same name and
	how many people it is shared with. Reads only.

	``get_skill`` serves a reviewed (Role/Org) skill before a person's own of the same
	name. Skills made before the review workflow are Role/Org rows too, and nobody
	reviewed their text: this lists them so an operator can look before that rule goes
	live on a site. Empty where every such skill came through a promotion.
	``bench --site <site> execute jarvis.chat.skill_permissions.shared_skills_no_reviewer_owns``."""
	from jarvis.permissions import is_skill_reviewer

	rows = frappe.get_all(
		SKILL,
		filters={"scope": ("in", ("Role", "Org", "")), "managed_by_learning": 0},
		fields=["name", "skill_name", "scope", "owner", "enabled"],
		order_by="skill_name asc, name asc",
	)
	# A row with no owner has no reviewer for an owner (and ``is_skill_reviewer`` of
	# nobody would answer for whoever runs this).
	reviewer = {owner: bool(owner) and is_skill_reviewer(owner) for owner in {r.owner for r in rows}}
	found = []
	for row in rows:
		if reviewer[row.owner]:
			continue
		others = frappe.get_all(
			SKILL,
			filters={"skill_name": row.skill_name, "name": ("!=", row.name), "owner": ("!=", row.owner)},
			pluck="owner",
		)
		shares = frappe.db.count("Jarvis Custom Skill Share", {"parent": row.name, "parenttype": SKILL})
		found.append(
			{
				"name": row.name,
				"skill_name": row.skill_name,
				"scope": row.scope or "Org",
				"owner": row.owner,
				"enabled": int(row.enabled or 0),
				"same_name_owned_by": sorted(set(others)),
				"shared_with": shares,
			}
		)
	return found


def skills_outside_control(user: str, *, names=(), slugs=()) -> list[str]:
	"""The ``skill_name`` of each skill, among the rows ``names`` and the slugs
	``slugs``, that ``user`` does not control (``controlled_by``), sorted.

	``names`` are rows a macro step tags: each is judged, enabled or not (its author
	can enable it again). ``slugs`` are names the agent may fetch: each is judged by
	the row ``get_skill`` would serve ``user`` for it (``served_row``), so the
	``custom-`` wire name and any case count, a reviewed Role/Org row of that name
	comes before ``user``'s own as it does there, and a System Manager, who may read
	every skill, is judged on the row they would be served and not on whether it was
	shared with them."""
	found = set()
	names = [n for n in names or () if n]
	if names:
		for row in frappe.get_all(
			SKILL, filters={"name": ["in", names]}, fields=["skill_name", "owner", "scope"]
		):
			if not controlled_by(row, user):
				found.add(row.skill_name)
	for slug in sorted({s for s in slugs or () if s})[:_MAX_SLUGS]:
		row = served_row(slug, user)
		if row is not None and not controlled_by(row, user):
			found.add(row.skill_name)
	return sorted(found)


def served_row(skill_name: str, user: str):
	"""The row ``get_skill`` serves ``user`` for ``skill_name`` (``resolve_skill``),
	or None when it would serve none. A check, not a fetch: nothing is audited."""
	from jarvis.exceptions import JarvisError
	from jarvis.tools.get_skill import resolve_skill

	try:
		return resolve_skill(skill_name, user, audit=False)
	except JarvisError:
		return None


def prompt_slugs(text: str) -> set[str]:
	"""Every ``/slug`` in ``text`` that could name a skill, lower-cased: after a
	space, a bracket or a comma as well as at the start, and ``custom-`` included
	(``served_row`` strips it). Not after a word character or a slash, so a path or
	a date is not one."""
	return {slug.lower() for slug in _PROMPT_SLUG_RE.findall(text or "")}


# A ``/slug`` anywhere a reader would see one. Wider than the composer's own
# ``custom_skills._INVOKE_RE`` on purpose: the agent fetches what it reads.
_PROMPT_SLUG_RE = re.compile(r"(?<![\w/])/([a-z0-9]+(?:-[a-z0-9]+)*)", re.IGNORECASE)
# A summary may run to every step's length together; each slug costs a query.
_MAX_SLUGS = 50
