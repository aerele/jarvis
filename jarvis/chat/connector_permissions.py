"""Row-level scoping for ``Jarvis Connector`` (MCP connectors).

The data-layer twin of ``jarvis/chat/dashboard_permissions.py`` / ``jarvis/chat/
chat_permissions.py``: list/report/generic-REST queries are scoped via the
``permission_query_conditions`` hook, per-doc read/write/create/delete via
``has_permission`` - both registered in ``hooks.py``.

Visibility matrix:

  * Shared connectors   - readable by every Jarvis user.
  * Personal connectors - readable only by their owner. Deliberately NOT
    expanded to the admin tier (mirrors ``Jarvis Conversation`` in
    ``chat_permissions.py``, which keeps System Manager scoped to its own
    private chats too): a Personal connector's whole point is that nobody else,
    including a tenant admin, can read or ride the owner's credential.
    ``Administrator`` bypasses Frappe perms entirely regardless.

Write matrix:

  * Shared connectors   - the admin tier only (System Manager / Jarvis Admin).
    A plain user may never edit a Shared connector's credential or base URL,
    even though they can read it.
  * Personal connectors - the owner only, for the same reason as above.

The DocType permission rows grant "Jarvis User" / "Jarvis Admin" / "System
Manager" broad read/write/create/delete (no ``if_owner`` - Shared rows must be
readable by non-owners), and this module narrows via the "grant + deny-hook"
shape used by ``Jarvis Dashboard`` / ``Jarvis Trigger``. The Shared-scope
create/widen gate itself lives in the ``Jarvis Connector`` controller
(``_guard_shared_scope``) - "create" reaches ``has_permission`` doc-less, so it
can never see ``scope`` there.

NOTE (hooks can only DENY): on this Frappe version a falsy ``has_permission``
return (``None`` included) denies, so every allow path returns an explicit
``True`` to defer to the normal role-perm check.
"""

from __future__ import annotations

import frappe

from jarvis.permissions import has_jarvis_admin_access

CONNECTOR = "Jarvis Connector"

# ptypes that reveal a row's content; everything read-shaped maps to visibility.
_READ_PTYPES = ("read", "select", "print", "email", "export", "share", "report")


def can_read_connector(doc, user: str | None = None) -> bool:
	"""Shared: everyone. Personal: owner only. Administrator: always (Frappe
	perm bypass, checked here defensively too)."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	if (doc.get("scope") or "") == "Shared":
		return True
	return (doc.get("owner") or "") == user


def can_edit_connector(doc, user: str | None = None) -> bool:
	"""Write matrix: Shared -> admin tier only; Personal -> owner only (NOT
	widened to the admin tier - see module docstring)."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	if (doc.get("scope") or "") == "Shared":
		return has_jarvis_admin_access(user)
	return (doc.get("owner") or "") == user


def connector_query_conditions(user: str | None = None) -> str:
	"""Scope every list/report/REST query on Jarvis Connector: Administrator is
	unrestricted; everyone else (admin tier included) sees their own rows plus
	every Shared row - a System Manager does NOT get org-wide read of other
	users' Personal connectors, matching the per-doc gate above."""
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	table = "`tabJarvis Connector`"
	esc = frappe.db.escape(user)
	return f"({table}.`scope` = 'Shared' or {table}.`owner` = {esc})"


def has_connector_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	if ptype == "create":
		# The scope-widening gate (only the admin tier may create a Shared row)
		# lives in the controller - "create" reaches this hook doc-less.
		return True
	if ptype in _READ_PTYPES:
		return can_read_connector(doc, user)
	if ptype in ("write", "delete"):
		return can_edit_connector(doc, user)
	return True
