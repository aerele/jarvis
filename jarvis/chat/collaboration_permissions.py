"""Refresh permission INPUTS; Frappe still decides every ERP permission.

Locking reads prevent an old request snapshot/roles cache from reviving removed
access. Only request-local caches are populated; nothing publishes another
user's permissions into Redis. Call under the collaborative claim's lock order,
after Conversation/User/Share and before the Turn/action claim is committed.
"""

from contextlib import contextmanager, nullcontext

import frappe
from frappe.permissions import ALL_USER_ROLE, AUTOMATIC_ROLES, GUEST_ROLE, SYSTEM_USER_ROLE

from jarvis._session import impersonate
from jarvis.chat import collaboration as c
from jarvis.permissions import JARVIS_ACCESS_ROLES


def _inputs(actor: str, *, lock=True):
	user = frappe.db.get_value("User", actor, ["enabled", "user_type"], as_dict=True, for_update=lock)
	roles = frappe.db.get_values(
		"Has Role",
		{"parent": actor, "parenttype": "User"},
		"role",
		pluck=True,
		order_by="name",
		for_update=lock,
	)
	if (
		actor in {"Administrator", "Guest"}
		or not user
		or not user.enabled
		or user.user_type != "System User"
		or not set(roles).intersection(JARVIS_ACCESS_ROLES)
	):
		c._deny()
	roles = [role for role in roles if role not in AUTOMATIC_ROLES]
	roles += [ALL_USER_ROLE, GUEST_ROLE, SYSTEM_USER_ROLE]
	permissions = {}
	# Same data contract as Frappe's get_user_permissions, using current locking
	# rows instead of a cached dictionary/old snapshot. Permission decisions,
	# field access, document ownership and User Permission enforcement stay in
	# the unchanged ERP tools and framework permission engine.
	for row in frappe.db.get_values(
		"User Permission",
		{"user": actor},
		["allow", "for_value", "applicable_for", "is_default", "hide_descendants"],
		as_dict=True,
		order_by="name",
		for_update=lock,
	):
		values = [(row.for_value, row.is_default, row.hide_descendants)]
		if frappe.get_meta(row.allow).is_nested_set() and not row.hide_descendants:
			values.extend(
				(name, False, False) for name in frappe.db.get_descendants(row.allow, row.for_value)
			)
		for name, is_default, hide_descendants in values:
			permissions.setdefault(row.allow, []).append(
				frappe._dict(
					doc=name,
					applicable_for=row.applicable_for,
					is_default=is_default,
					hide_descendants=hide_descendants,
				)
			)
	return roles, frappe._dict(permissions)


@contextmanager
def actor_permissions(actor: str, *, impersonate_user: bool = True, lock=True):
	roles, permissions = _inputs(actor, lock=lock)
	# File checks pass an explicit user without changing the reader's session.
	with impersonate(actor) if impersonate_user else nullcontext():
		previous_cache = frappe.local.cache
		previous_role_permissions = frappe.local.role_permissions
		previous_user_permissions = frappe.local.user_perms
		cache = dict(previous_cache)
		for name, value in (("roles", roles), ("user_permissions", permissions)):
			key = frappe.cache.make_key(name)
			cache[key] = {**cache.get(key, {}), actor: value}
		frappe.local.cache = cache
		frappe.local.role_permissions = {}
		frappe.local.user_perms = None
		try:
			yield
		finally:
			frappe.local.cache = previous_cache
			frappe.local.role_permissions = previous_role_permissions
			frappe.local.user_perms = previous_user_permissions
