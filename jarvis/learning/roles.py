"""Computed role attachment for pattern learning (plan sections 4.1, 6.6).

Role tags on a learned pattern are DERIVED, never hardcoded: a role is attached
to a doctype iff it holds an org-wide (permlevel 0, not if_owner) read grant on
that doctype. ``get_all_perms`` already implements the Custom-DocPerm-replaces-
standard rule, so we read straight off it.

Two consumers:
  * the executor stamps ``roles_for_doctype(spec.doctype)`` onto every candidate
    (falls back to the spec's declared ``role_priors`` if this ever fails);
  * the compiler unions the compiled patterns' role sets into each learned
    skill's ``allowed_roles``; ``turn_handler`` intersects
    ``roles_for_user(chat_user)`` with those to decide activation.

Both are short-cached in redis (roles/perms change rarely and a stale minute is
harmless); the cache is bypassed under ``frappe.flags.in_test`` so a test that
mutates roles/perms inside its rolled-back transaction sees fresh results.
``roles_for_user`` rides ``frappe.get_roles`` (itself redis-cached per user), so
it is cheap enough for the chat hot path.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint

from jarvis.permissions import (
	JARVIS_ADMIN_ROLE,
	JARVIS_USER_ROLE,
	ensure_jarvis_admin_role,
	ensure_jarvis_user_role,
)

# _v2: the cached value is now {"roles": [...], "derived": bool}, not a bare
# list, so the key changes rather than teaching the reader both shapes.
_DT_CACHE_KEY = "jarvis_learning_roles_for_doctype_v2"
_CACHE_TTL_S = 300


def roles_for_doctype(doctype: str) -> list[str]:
	"""Enabled, desk-access roles with an org-wide permlevel-0 read on ``doctype``.

	Excludes: disabled roles, portal roles (``desk_access=0`` never reach Desk
	data), and if_owner-only grants (a per-owner read is not org-wide, so it is
	not a delivery signal for a bench-global learned skill). Deterministic,
	sorted. Empty on any failure (the caller falls back to declared priors)."""
	return derive_roles_for_doctype(doctype)[0]


def derive_roles_for_doctype(doctype: str) -> tuple[list[str], bool]:
	"""``(roles, derived)`` - :func:`roles_for_doctype` plus its provenance.

	``derived`` says whether the permission scan actually RAN to a conclusion.
	True means the answer is a real finding even when it is empty ("no desk role
	holds an org-wide read here"); False means we determined nothing and the
	empty list is a fallback. The two are indistinguishable from the return value
	alone, which is what makes an empty ``allowed_roles`` ambiguous downstream
	(issue #479, design Q3). Callers recording provenance want this one; everyone
	else keeps calling :func:`roles_for_doctype`.
	"""
	if not doctype:
		return [], False

	use_cache = not frappe.flags.in_test
	if use_cache:
		try:
			cached = frappe.cache().hget(_DT_CACHE_KEY, doctype)
			if isinstance(cached, dict):
				return list(cached.get("roles") or []), bool(cached.get("derived"))
		except Exception:
			pass

	roles, derived = _compute_roles_for_doctype(doctype)

	if use_cache:
		try:
			frappe.cache().hset(_DT_CACHE_KEY, doctype, {"roles": roles, "derived": derived})
			# Bound staleness even if the hash is never explicitly cleared.
			frappe.cache().expire(_make_key(_DT_CACHE_KEY), _CACHE_TTL_S)
		except Exception:
			pass
	return roles, derived


def _compute_roles_for_doctype(doctype: str) -> tuple[list[str], bool]:
	from frappe.permissions import get_all_perms

	try:
		roles = frappe.get_all(
			"Role",
			filters={"disabled": 0},
			fields=["name", "desk_access"],
		)
	except Exception:
		return [], False

	out: set[str] = set()
	scanned = 0
	for role in roles:
		name = role.get("name")
		if not name:
			continue
		# Portal roles (Customer/Supplier/Guest/...) never reach Desk data, so an
		# org-wide learned skill scoped to them is meaningless (plan section 4.1).
		if not cint(role.get("desk_access")):
			continue
		try:
			perms = get_all_perms(name) or []
		except Exception:
			continue
		scanned += 1
		if _grants_orgwide_read(perms, doctype):
			out.add(name)
	# A scan that read NO role's permissions concluded nothing, even though it
	# returns the same empty list as a scan that read every role and found none.
	return sorted(out), scanned > 0


def _grants_orgwide_read(perms, doctype: str) -> bool:
	"""True iff any perm row grants read at permlevel 0 WITHOUT if_owner. A role
	whose only permlevel-0 read is if_owner=1 is per-owner, not org-wide, so it
	does not qualify (plan section 4.1)."""
	for p in perms:
		# get_all_perms rows are frappe._dict of DocPerm / Custom DocPerm fields.
		if (
			p.get("parent") == doctype
			and cint(p.get("permlevel")) == 0
			and cint(p.get("read"))
			and not cint(p.get("if_owner"))
		):
			return True
	return False


def roles_for_user(user: str | None = None) -> set[str]:
	"""The user's roles as a set (chat hot path). Rides ``frappe.get_roles``,
	which is redis-cached per user, so this is a cache read in the common case."""
	user = user or frappe.session.user
	try:
		return set(frappe.get_roles(user))
	except Exception:
		return set()


def clear_cache() -> None:
	"""Drop the doctype-role cache (call after a perms/role change; tests bypass
	the cache entirely via frappe.flags.in_test)."""
	try:
		frappe.cache().delete_value(_DT_CACHE_KEY)
	except Exception:
		pass


def _make_key(key: str) -> str:
	"""The site-namespaced redis key frappe.cache() actually writes for ``key``
	(hset stores under make_key(key)); used only to attach a TTL."""
	try:
		return frappe.cache().make_key(key)
	except Exception:
		return key


# --------------------------------------------------------------------------- #
# Wiki v2 role seeding (hooks.after_migrate)
# --------------------------------------------------------------------------- #
# The wiki curator role (write matrix in jarvis/chat/wiki_permissions.py).
# "Knowledge Wiki User" was retired — no longer seeded here; personal
# User-scope editing now rides the "Jarvis User" platform role (seeded below).
# Seeded on after_migrate because this app has no after_install channel and
# migrate follows a fresh install anyway (same reasoning as the voice_facts
# seeding).
_WIKI_ROLES = ("Knowledge Wiki Manager",)

# --------------------------------------------------------------------------- #
# Skills-area rework role seeding (DESIGN.md section 1 / 6 OQ-1)
# --------------------------------------------------------------------------- #
# Two bench-side roles for the Personalise/Analysis/Review split:
#   - "Jarvis Admin": org-level Jarvis administration (Personalisation
#     Settings, the Analysis tab, question-rule config, caps). The owner
#     named this role explicitly (3x) despite a same-named Role already
#     existing on the SEPARATE jarvis_admin SaaS control-plane site — that
#     role lives on a different Frappe site entirely and is not reachable
#     from a customer bench, so there is no technical collision, only a
#     naming one. Document the distinction wherever this role is gated.
#   - "Jarvis Skill Reviewer": Review-tab actions (decide patterns,
#     promotions, skill updates from review, trigger follow-up questions).
# Both seeded with the exact idempotent idiom already used for the
# Knowledge Wiki roles above (same after_migrate hook, same insert shape).
_PERSONALISE_ROLES = ("Jarvis Admin", "Jarvis Skill Reviewer")

# Personalisation Settings (Jarvis Settings Single) Check/Int defaults, seeded
# on migrate for the same reason as voice_facts._SETTINGS_DEFAULTS: v16's
# frappe.db.get_single_value casts an unset field via cint(), so BOTH an
# unset Check (personalise_enabled) and an unset Int
# (personalise_daily_question_cap) silently read back as 0 — not the JSON
# defaults of 1 / 5 — until a tabSingles row exists for that field. Kept in
# sync with jarvis_settings.json's Personalisation section defaults.
_PERSONALISE_SETTINGS = "Jarvis Settings"
_PERSONALISE_SETTINGS_DEFAULTS = {
	"personalise_daily_question_cap": 5,
	"personalise_enabled": 1,
	"chat_question_mining_enabled": 1,
}


def after_migrate() -> None:
	"""Idempotently create the Knowledge Wiki roles, the Skills-area
	Personalise/Review roles, and the Jarvis User role, and backfill the
	Personalisation Settings Single defaults (best-effort, never blocks a
	migrate)."""
	try:
		created = False
		# Backfill the Personalisation Settings Single defaults first, before the
		# role loop. Order is irrelevant (this is an idempotent Settings backfill,
		# not a role op) — placing it here keeps this function's diff away from the
		# JARVIS_USER_ROLE block, which a sibling branch also extends to seed the
		# same "Jarvis Admin" role; non-adjacent edits 3-way-merge cleanly and the
		# duplicate seed is a harmless no-op either way (both are exists-guarded).
		if _seed_personalise_settings_defaults():
			created = True
		for role_name in _WIKI_ROLES + _PERSONALISE_ROLES:
			if frappe.db.exists("Role", role_name):
				continue
			frappe.get_doc(
				{
					"doctype": "Role",
					"role_name": role_name,
					"desk_access": 1,
					"is_custom": 0,
				}
			).insert(ignore_permissions=True)
			created = True
		# The app-access role — definition lives in jarvis/permissions.py (single
		# source of truth). Normally already created by DocType sync (16 DocTypes
		# name it in a permission row); this is the fallback.
		if not frappe.db.exists("Role", JARVIS_USER_ROLE):
			ensure_jarvis_user_role()
			created = True
		# The tenant-admin role (design section 2), same single-source-of-truth
		# definition in jarvis/permissions.py. Idempotent.
		if not frappe.db.exists("Role", JARVIS_ADMIN_ROLE):
			ensure_jarvis_admin_role()
			created = True
		if created:
			frappe.db.commit()
	except Exception:
		frappe.log_error(title="jarvis wiki roles seed failed", message=frappe.get_traceback())


def _seed_personalise_settings_defaults() -> bool:
	"""Row-existence probe (not a value test — see the module comment above):
	a loaded Check/Int field on a Single coerces an unset row to 0, which is
	indistinguishable from "admin turned it off / down to zero". Returns True
	iff at least one field was backfilled."""
	existing = {
		r[0]
		for r in frappe.db.sql(
			"select field from tabSingles where doctype=%s and field in %s",
			(_PERSONALISE_SETTINGS, tuple(_PERSONALISE_SETTINGS_DEFAULTS)),
		)
	}
	updates = {f: v for f, v in _PERSONALISE_SETTINGS_DEFAULTS.items() if f not in existing}
	if not updates:
		return False
	frappe.db.set_single_value(_PERSONALISE_SETTINGS, updates, update_modified=False)
	return True
