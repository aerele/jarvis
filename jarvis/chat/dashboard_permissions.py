"""Scope visibility + write matrix for ``Jarvis Dashboard``.

The data-layer twin of ``jarvis/chat/wiki_permissions.py``, registered in
hooks.py via ``permission_query_conditions`` / ``has_permission``.

Visibility (read) — enforced everywhere (Desk lists via the query-conditions
hook, per-doc access via the has_permission hook, SPA raw-SQL lists via
``visible_scope_condition``):

  * Org dashboards: every jarvis user.
  * Role dashboards: users holding ``target_role`` (+ the admin tier).
  * User dashboards: ``target_user`` only (+ the admin tier).
  * Blank/NULL scope is PRIVATE (treated like User via ``target_user``, NOT
    Org — the opposite of the wiki's blank-scope default: a dashboard that
    somehow lost its scope must fail closed, not org-wide).
  * The OWNER always reads their own dashboard regardless of scope.

Write matrix: the owner, or the admin tier (System Manager / Jarvis Admin).
Delete follows write. The doctype permission rows grant Jarvis User r/w/c/d
broadly and this hook DENIES non-owner writes — the "grant + deny-hook" shape
used by Jarvis Trigger. The scope-widening gate (plain users may not create
Org/Role dashboards) lives in the DocType controller, NOT here: "create"
reaches the hook without a doc, so it could never inspect the scope.

NOTE on the has_permission hook: on this Frappe version controller hooks can
only DENY — any falsy return (None included) denies, so this module returns
an explicit True to defer to the normal role-perm check.
"""

from __future__ import annotations

import frappe

from jarvis.chat.wiki_permissions import _NON_TARGETABLE_ROLES
from jarvis.permissions import JARVIS_ADMIN_ROLE, has_jarvis_admin_access

DASHBOARD = "Jarvis Dashboard"

# ptypes that reveal dashboard content; everything read-shaped maps to
# visibility (mirrors wiki_permissions).
_READ_PTYPES = ("read", "select", "print", "email", "export", "share", "report")


def _is_sm(user: str) -> bool:
	# Administrator's get_roles returns every Role, so the explicit check is
	# just a shortcut; both spellings of "full access" land here.
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def _get(doc, field: str):
	# dict rows (frappe.get_all) and Document both expose .get.
	return doc.get(field)


def can_read_dashboard(doc, user: str | None = None) -> bool:
	"""Visibility matrix for one dashboard (dict with owner/scope/target_*
	fields, or a Document)."""
	user = user or frappe.session.user
	if has_jarvis_admin_access(user):
		return True
	if (_get(doc, "owner") or "") == user:
		return True
	scope = (_get(doc, "scope") or "").strip()
	if scope == "Org":
		return True
	if scope == "Role":
		target_role = _get(doc, "target_role")
		return bool(target_role) and target_role in frappe.get_roles(user)
	# "User" — and blank/None scope, which is PRIVATE (fail closed, not Org).
	return (_get(doc, "target_user") or "") == user


def can_edit_dashboard(doc, user: str | None = None) -> bool:
	"""Write matrix: the owner, or the admin tier."""
	user = user or frappe.session.user
	if has_jarvis_admin_access(user):
		return True
	return (_get(doc, "owner") or "") == user


def creatable_scopes(user: str | None = None) -> list[str]:
	"""Scopes this user may create dashboards in. Personal dashboards for
	everyone; sharing (Org/Role) is the admin tier's call."""
	user = user or frappe.session.user
	if has_jarvis_admin_access(user):
		return ["Org", "Role", "User"]
	return ["User"]


def manageable_roles(user: str | None = None) -> list[str]:
	"""Roles the user may target for Role-scope dashboards: SM targets any
	enabled desk role; a Jarvis Admin targets roles they hold. The wiki's
	non-targetable set (framework/blanket roles) is excluded in both cases —
	targeting e.g. "Desk User" would be an org-wide side door."""
	user = user or frappe.session.user
	if _is_sm(user):
		return [
			r
			for r in frappe.get_all(
				"Role",
				filters={"disabled": 0, "desk_access": 1},
				order_by="name asc",
				pluck="name",
			)
			if r not in _NON_TARGETABLE_ROLES
		]
	roles = set(frappe.get_roles(user))
	if JARVIS_ADMIN_ROLE not in roles:
		return []
	return sorted(roles - set(_NON_TARGETABLE_ROLES))


def visible_scope_condition(user: str | None = None) -> str:
	"""SQL boolean fragment over ``tabJarvis Dashboard`` implementing the
	visibility matrix; always a parenthesized valid expression so callers can
	splice it with ``and``. Values go through frappe.db.escape — role names
	and user emails are org-author-controlled strings."""
	user = user or frappe.session.user
	if has_jarvis_admin_access(user):
		return "(1=1)"
	table = "`tabJarvis Dashboard`"
	escaped_user = frappe.db.escape(user)
	clauses = [
		f"{table}.`owner` = {escaped_user}",
		f"{table}.`scope` = 'Org'",
	]
	roles = [r for r in frappe.get_roles(user) if r]
	if roles:
		role_list = ", ".join(frappe.db.escape(r) for r in roles)
		clauses.append(f"({table}.`scope` = 'Role' and {table}.`target_role` in ({role_list}))")
	# Blank/NULL scope is PRIVATE — it rides the target_user clause (and the
	# owner clause above), never the Org one.
	clauses.append(
		f"(coalesce({table}.`scope`, '') in ('', 'User') and {table}.`target_user` = {escaped_user})"
	)
	return "(" + " or ".join(clauses) + ")"


def dashboard_query_conditions(user: str | None = None) -> str:
	"""hooks.permission_query_conditions entry — scopes every Desk/ORM list
	query. Empty string (no restriction) for the admin tier."""
	user = user or frappe.session.user
	if has_jarvis_admin_access(user):
		return ""
	return visible_scope_condition(user)


def has_dashboard_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	"""hooks.has_permission entry — per-doc gate. Read-shaped ptypes follow
	the visibility matrix; write and delete follow the edit matrix; everything
	else (including "create") defers — hooks can only deny, and the create-time
	scope gate lives in the DocType controller. True defers to the role-perm
	check."""
	user = user or frappe.session.user
	if ptype in _READ_PTYPES:
		return can_read_dashboard(doc, user)
	if ptype in ("write", "delete"):
		return can_edit_dashboard(doc, user)
	return True
