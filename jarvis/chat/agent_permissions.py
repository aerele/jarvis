"""Row-level ownership scoping for the Jarvis agent doctypes (security review
PART 3, TASK 29).

The data-layer twin of ``jarvis/chat/chat_permissions.py`` for the four
owner/installer-scoped agent doctypes:

  * Jarvis Agent Installation -> the row's own ``owner`` (the installer).
  * Jarvis Agent Run          -> the row's own ``owner``.
  * Jarvis Agent Finding      -> the row's own ``owner``.
  * Jarvis Agent Activity     -> the row's own ``owner``.

Runs / findings / activity are inserted server-side (``ignore_permissions``) by
the audit engine and then handed to the installation owner via a raw
``db.set_value(..., "owner", owner)`` reassignment (``agent_runs.py`` /
``agent_scheduler.py``), so the row's own ``owner`` is always the installer — a
reliable single axis. Findings/runs can carry sensitive audit output, so this
keeps them owner-or-SM scoped even over generic REST.

The catalog doctype ``Jarvis Agent Listing`` is deliberately NOT scoped here: it
is a shared vendor catalog readable by every Jarvis User (its perm row stays
read-for-all). Its proprietary ``skill_bundle`` field is protected by a
permlevel-1 grant (TASK 33) instead of an owner hook.

System Manager (and Administrator) get org-wide READ (oversight; the existing SM
perm rows are the base grant). ``Administrator`` bypasses Frappe perms entirely.

Every interpolated value goes through ``frappe.db.escape``.

NOTE (hooks can only DENY): a falsy ``has_permission`` return denies, so every
allow path returns an explicit ``True`` to defer to the normal role-perm check.
"""

from __future__ import annotations

import frappe

INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"
ACTIVITY = "Jarvis Agent Activity"
LISTING = "Jarvis Agent Listing"


def _is_sm(user: str) -> bool:
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def _owner_query(table: str, user: str | None) -> str:
	user = user or frappe.session.user
	if _is_sm(user):
		return ""
	return f"`tab{table}`.`owner` = {frappe.db.escape(user)}"


def _owner_has_permission(doc, ptype: str, user: str | None) -> bool:
	user = user or frappe.session.user
	if _is_sm(user):
		return True
	if ptype == "create":
		# ``owner`` is assigned by Frappe / reassigned to the installer by the
		# engine; the role + ``if_owner`` rule governs create.
		return True
	return doc.get("owner") == user


# --------------------------------------------------------------------------- #
# Jarvis Agent Installation
# --------------------------------------------------------------------------- #
def installation_query_conditions(user: str | None = None) -> str:
	return _owner_query(INSTALLATION, user)


def has_installation_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	return _owner_has_permission(doc, ptype, user)


# --------------------------------------------------------------------------- #
# Jarvis Agent Run
# --------------------------------------------------------------------------- #
def run_query_conditions(user: str | None = None) -> str:
	return _owner_query(RUN, user)


def has_run_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	return _owner_has_permission(doc, ptype, user)


# --------------------------------------------------------------------------- #
# Jarvis Agent Finding
# --------------------------------------------------------------------------- #
def finding_query_conditions(user: str | None = None) -> str:
	return _owner_query(FINDING, user)


def has_finding_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	return _owner_has_permission(doc, ptype, user)


# --------------------------------------------------------------------------- #
# Jarvis Agent Activity
# --------------------------------------------------------------------------- #
def activity_query_conditions(user: str | None = None) -> str:
	return _owner_query(ACTIVITY, user)


def has_activity_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	return _owner_has_permission(doc, ptype, user)


# --------------------------------------------------------------------------- #
# Jarvis Agent Listing — the operator catalogue-visibility BOUNDARY.
#
# The Listing catalog is otherwise All-readable (Jarvis User: read). The app SPA
# serves it through agents_api (_enriched_catalog / get_agent), which MASK a
# ``teaser`` and HIDE a ``hidden`` agent from a non-admin — but those use
# frappe.get_all (ignore_permissions), so they see every row and redact in Python.
# The generic REST / Desk path (frappe.client.get_list / get) does NOT go through
# that masking, so without these hooks a plain Jarvis User could read the real
# title of a masked agent straight off the raw row. These hooks are that boundary:
# a non-admin never LISTS or READS a teaser/hidden row it has not installed. An
# admin (System Manager / Jarvis Admin) is unrestricted, matching the app layer;
# an installed row is exempt (owner carve-out, parity with _enriched_catalog).
# --------------------------------------------------------------------------- #
def _listing_is_admin(user: str) -> bool:
	from jarvis.permissions import has_jarvis_admin_access

	return user == "Administrator" or has_jarvis_admin_access(user)


def listing_query_conditions(user: str | None = None) -> str:
	user = user or frappe.session.user
	if _listing_is_admin(user):
		return ""
	esc = frappe.db.escape(user)
	# Show a row via generic list only if it is 'available', OR the caller installed it.
	return (
		f"(`tab{LISTING}`.`operator_visibility` = 'available' "
		f"OR `tab{LISTING}`.`name` IN "
		f"(SELECT `agent` FROM `tab{INSTALLATION}` WHERE `owner` = {esc}))"
	)


def has_listing_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	# Hooks can only DENY (a falsy return denies; True defers to normal role perms).
	user = user or frappe.session.user
	if ptype != "read" or _listing_is_admin(user):
		return True
	if (doc.get("operator_visibility") or "available") == "available":
		return True
	# teaser/hidden: readable only by an owner who already installed it.
	return bool(frappe.db.exists(INSTALLATION, {"owner": user, "agent": doc.get("name")}))
