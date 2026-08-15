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
guarded in the controller (``_guard_scope_change`` / ``_guard_new_scope``) so it
holds under ``ignore_permissions`` too. Reviewer/compiler writes (the promotion
decide + insight-apply + compiler upsert) go through ``ignore_permissions`` and
so never consult ``has_permission``.

Every interpolated value in the SQL fragment goes through ``frappe.db.escape``;
role names / user emails are org-author-controlled strings.

NOTE (hooks can only DENY): a falsy ``has_permission`` return denies, so every
allow path returns an explicit ``True`` to defer to the normal role-perm check.
"""

from __future__ import annotations

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
