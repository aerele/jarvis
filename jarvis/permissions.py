"""Single source of truth for the Jarvis app-access gate.

Reaching Jarvis at all requires the dedicated ``Jarvis User`` role, with
``System Manager`` always allowed and ``Administrator`` implicitly allowed
(it bypasses all perms).

Enforcement is at the HUMAN ENTRY POINTS — the SPA route (``www/jarvis.py``),
the desk page (``jarvis_chat``) and the user-initiated chat APIs. It is
deliberately NOT applied to machine-authenticated or delegated paths
(``send_message`` invoked under ``set_user(owner)`` by the scheduler /
approvals resume), which act as an identity that legitimately may not hold
the role.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager

import frappe

# The role name + the access rule, everywhere: "Jarvis User" OR "System Manager"
# (Administrator is implicitly allowed by has_jarvis_access / frappe perms).
# One constant so the seed (learning/roles.py, tests) and the gate can never
# drift.
JARVIS_USER_ROLE = "Jarvis User"

# The tenant-side admin role (design section 2): a customer power-user who can
# manage Jarvis (per-user usage limits, the admin pane) WITHOUT full System
# Manager rights.
#
# NAMING NOTE: the control-plane app (``jarvis_admin``, a DIFFERENT site in a
# different tree) defines an UNRELATED role also named "Jarvis Admin" for Aerele
# ops staff. Roles are site-local so there is no technical clash; here "Jarvis
# Admin" means *the tenant's own admin*. This comment exists so a cross-repo grep
# finds the disambiguation.
JARVIS_ADMIN_ROLE = "Jarvis Admin"

# App-access: a Jarvis Admin can also use chat (appended so an admin isn't
# locked out of the surface they administer).
JARVIS_ACCESS_ROLES = ("System Manager", JARVIS_USER_ROLE, JARVIS_ADMIN_ROLE)

# Tenant-admin gate: System Manager OR the tenant admin role (Administrator is
# implicitly allowed by has_jarvis_admin_access).
JARVIS_ADMIN_ROLES = ("System Manager", JARVIS_ADMIN_ROLE)

# Reviewer set authorized for org-wide skill / pattern / wiki effects (security
# review PART 2, TASK 15 — RATIFIED). Housed in this lightweight module (no heavy
# imports) so the Jarvis Custom Skill controller / skill_permissions can import it
# without pulling in the learned_api import graph. Keep in sync with
# jarvis.chat.learned_api._REVIEWER_ROLES.
JARVIS_REVIEWER_ROLES = ("Jarvis Skill Reviewer", "Jarvis Admin", "System Manager")


def is_skill_reviewer(user: str | None = None) -> bool:
	"""True iff ``user`` may authorize org-wide skill effects: promote a Custom
	Skill up the User→Role→Org scope ladder, or apply skills bench-wide.
	``Administrator`` always passes. Defaults to the current session user."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return bool(set(JARVIS_REVIEWER_ROLES) & set(frappe.get_roles(user)))


def require_skill_reviewer(user: str | None = None) -> None:
	"""Raise ``frappe.PermissionError`` unless ``user`` is a skill reviewer
	(Jarvis Skill Reviewer / Jarvis Admin / System Manager; Administrator
	implicit). Whitelisted endpoints call this to gate org-wide skill effects."""
	if not is_skill_reviewer(user):
		frappe.throw(
			"This action needs a Jarvis Skill Reviewer, Jarvis Admin or System Manager role.",
			frappe.PermissionError,
		)


def ensure_jarvis_user_role() -> None:
	"""Idempotently create the ``Jarvis User`` role (desk-access).

	Mostly belt-and-braces: 16 DocTypes name this role in a permission row and
	DocType sync auto-creates any role it finds there, so the role already exists
	by the time the after_migrate seed calls this. Kept so the definition lives in
	exactly one place.

	``is_custom`` is 0 deliberately: the role ships WITH the app (DocType sync
	materializes it from the permission rows), so it is not a user-defined custom
	role. It also has to match what sync actually creates -- sync wins the race,
	so a mismatched value here would never be applied and would misdescribe every
	live site.

	NOTE: :func:`grant_onboarding_admin` is the ONLY code path that grants this
	role, and it grants it alongside ``Jarvis Admin`` (the admin role is additive
	and owns no chat permission rows of its own). A tenant's additional users are
	granted ``Jarvis User`` by hand in Desk."""
	if not frappe.db.exists("Role", JARVIS_USER_ROLE):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": JARVIS_USER_ROLE,
				"desk_access": 1,
				"is_custom": 0,
			}
		).insert(ignore_permissions=True)


def has_jarvis_access(user: str | None = None) -> bool:
	"""True iff ``user`` may reach Jarvis.

	``Administrator`` is always allowed; otherwise the user must be a **System
	User** (never a Website/portal user - PART 1 TASK 6: portal users are a
	distinct, lower-trust population and must not reach chat) AND hold at least
	one of :data:`JARVIS_ACCESS_ROLES`. Defaults to the current session user."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	if not is_system_user(user):
		return False
	return bool(set(JARVIS_ACCESS_ROLES) & set(frappe.get_roles(user)))


def is_system_user(user: str | None = None) -> bool:
	"""True iff ``user`` is a Desk (System User), not a Website/portal user.
	``Administrator`` counts. Defaults to the current session user."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	if not user or user == "Guest":
		return False
	return frappe.db.get_value("User", user, "user_type") == "System User"


def is_valid_unattended_owner(user: str | None) -> bool:
	"""True iff ``user`` may be bound as the identity of an UNATTENDED (cron) turn.

	Strictly narrower than :func:`has_jarvis_access`, which answers "may this
	identity reach Jarvis at all" and deliberately waves ``Administrator``
	through. A scheduled turn has no human watching it, so it must never bind to:

	* ``Administrator`` - every DocPerm and User Permission is bypassed, silently,
	  on a cron.
	* ``Guest`` - not a real identity.
	* a **disabled** user - disabling is how an offboarded employee's access is
	  revoked, but Frappe only clears their browser sessions: their roles survive,
	  ``frappe.get_roles`` does not filter on ``enabled``, and nothing else in this
	  module reads ``User.enabled``. Without this check their scheduled work keeps
	  reaching the ERP forever.

	This is the guard ``agent_scheduler._valid_owner`` has enforced since the agent
	scheduler shipped; it lives here so every unattended dispatcher shares ONE
	definition instead of each re-deriving it (jarvis #469: the macro scheduler had
	not, and admitted exactly the two identities the sibling refuses)."""
	if not user or user in ("Administrator", "Guest"):
		return False
	return bool(frappe.db.get_value("User", user, "enabled"))


def require_jarvis_access(user: str | None = None) -> None:
	"""Raise ``frappe.PermissionError`` (with a clear message) if the user lacks
	access. Defaults to the current session user.

	Note: implemented directly rather than via ``frappe.only_for`` — in this
	Frappe version ``only_for`` treats its ``message`` argument as a boolean flag
	and would discard a custom string, showing a generic desk-role error."""
	if not has_jarvis_access(user):
		frappe.throw(
			"You need the Jarvis User role to use Jarvis. Ask an administrator for access.",
			frappe.PermissionError,
		)


# --------------------------------------------------------------------------- #
# Tenant-admin gate (design section 2). Mirrors the app-access helpers above so
# the seed (learning/roles.py), the boot flag (www/jarvis.py), the admin APIs
# (chat/user_settings_api.py) and tests share one source of truth.
# --------------------------------------------------------------------------- #


def ensure_jarvis_admin_role() -> None:
	"""Idempotently create the ``Jarvis Admin`` role (desk-access).
	Definition lives here (single source of truth); the after_migrate seed
	calls this so the role exists on every migrated site.

	``is_custom`` is 0 for the same reason as :func:`ensure_jarvis_user_role`:
	10 DocTypes name this role, so DocType sync creates it first and this
	exists-guard never fires on a real site. The declared value has to match
	what sync produces or it merely misdescribes reality."""
	if not frappe.db.exists("Role", JARVIS_ADMIN_ROLE):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": JARVIS_ADMIN_ROLE,
				"desk_access": 1,
				"is_custom": 0,
			}
		).insert(ignore_permissions=True)


def has_jarvis_admin_access(user: str | None = None) -> bool:
	"""True iff ``user`` may administer other users' Jarvis usage.

	``Administrator`` is always allowed (it bypasses all perms); otherwise the
	user must hold at least one of :data:`JARVIS_ADMIN_ROLES`. Defaults to the
	current session user."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return bool(set(JARVIS_ADMIN_ROLES) & set(frappe.get_roles(user)))


def require_jarvis_admin(user: str | None = None) -> None:
	"""Raise ``frappe.PermissionError`` (with a clear message) if the user is not
	a Jarvis Admin (or System Manager / Administrator). Defaults to the current
	session user. Implemented directly, not via ``frappe.only_for`` (same reason
	as :func:`require_jarvis_access`)."""
	if not has_jarvis_admin_access(user):
		frappe.throw(
			"You need the Jarvis Admin role to manage Jarvis users. Ask an administrator for access.",
			frappe.PermissionError,
		)


def grant_onboarding_admin(user: str | None = None) -> None:
	"""Grant ``Jarvis Admin`` AND ``Jarvis User`` to the onboarding/paying user
	(security review PART 4 REVISED, TASK 44/48).

	``Jarvis Admin`` is ADDITIVE — it means "admin rights on top of a normal
	user", never a standalone identity. An earlier revision granted it alone,
	reasoning that :data:`JARVIS_ACCESS_ROLES` contains it so the holder still
	passes :func:`has_jarvis_access` and every ``@require_jarvis_user`` endpoint.
	That holds at the GATE layer and fails at the DOCTYPE layer: ``Jarvis Admin``
	carries no permission row on ``Jarvis Conversation`` or ``Jarvis Chat
	Message`` (nor on Approval Request, Custom Skill, Macro, Macro Run, Voice
	Note or the three Agent doctypes), so such a user passed the gate, reached
	``send_message`` and then failed on the message insert. Granting both roles
	fixes all ten at once and keeps the two roles from having to be maintained in
	lockstep.

	The role names AND the target-user resolution are server-hardcoded (no
	caller-supplied role), so the ``ignore_permissions`` inserts carry NO
	privilege-escalation vector. Idempotent — already-held roles are skipped;
	never grants to Administrator / Guest."""
	ensure_jarvis_admin_role()
	ensure_jarvis_user_role()
	user = user or frappe.session.user
	if not user or user in ("Administrator", "Guest"):
		return
	# Deliberately NOT User.add_roles(): that calls save(), running the whole
	# User validation chain inside the signup / payment path, where an unrelated
	# validation error would abort onboarding. The row insert is surgical, and
	# set_system_user() (the reason add_roles is usually preferable) is moot here
	# because every caller is already a System User — require_jarvis_admin gates
	# all four call sites.
	granted = False
	for role in (JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE):
		if frappe.db.exists("Has Role", {"parenttype": "User", "parent": user, "role": role}):
			continue
		frappe.get_doc(
			{
				"doctype": "Has Role",
				"parenttype": "User",
				"parentfield": "roles",
				"parent": user,
				"role": role,
			}
		).insert(ignore_permissions=True)
		granted = True
	if granted:
		# frappe.get_roles is cached per user; without this the caller can read a
		# stale role list for the rest of the request (and beyond).
		frappe.clear_cache(user=user)


def support_scope(user: str | None = None) -> str | None:
	"""Support access rides the base Jarvis roles — no dedicated support role:

	* ``"all"`` for a Jarvis Admin (also System Manager / Administrator): the whole
	  organisation's ticket queue.
	* ``"own"`` for any Jarvis chat user: only the tickets they raised.
	* ``None`` for anyone without Jarvis access (Guest, portal users).

	The bench forwards this to the control plane as a filter. Admin is checked first
	so an admin gets ``all``, never ``own``."""
	user = user or frappe.session.user
	if user == "Guest":
		return None
	if has_jarvis_admin_access(user):
		return "all"
	if has_jarvis_access(user):
		return "own"
	return None


def require_jarvis_user(fn):
	"""Decorator form of :func:`require_jarvis_access` for whitelisted chat
	endpoints (PART 1 TASK 8).

	Replaces the per-function ``require_jarvis_access()`` call convention with a
	single declarative marker so the ``test_chat_endpoint_gating`` coverage test
	can enumerate every ``@frappe.whitelist`` function under ``jarvis/chat/`` and
	assert each is either decorated or explicitly allowlisted - the "new endpoint
	forgot the gate" regression class can no longer slip through.

	Stack it BELOW ``@frappe.whitelist()`` (whitelist outermost) so Frappe
	registers the wrapper, and the gate runs before the body. ``functools.wraps``
	preserves the signature + annotations so Frappe's
	``require_type_annotated_api_methods`` still sees the real parameters.
	"""

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		require_jarvis_access()
		return fn(*args, **kwargs)

	# Marker the coverage test reads (survives functools.wraps).
	wrapper.__jarvis_gated__ = True
	return wrapper


@contextmanager
def delegated_send():
	"""Mark the enclosed call stack as a TRUSTED server / delegated re-entry into
	``jarvis.chat.api.send_message`` (the scheduler, approval-resume, agent-run
	and File-Box drop paths), which run under ``impersonate(owner)`` where the
	impersonated owner legitimately may not hold the ``Jarvis User`` role.

	Uses ``frappe.flags`` (never an HTTP-settable parameter) so a browser POST
	cannot forge it. ``send_message`` reads this flag to (a) accept the call
	past its access gate and (b) insert the seed user message / touch the
	conversation with ``ignore_permissions`` so the now role-gated Message/
	Conversation create perms do not break the resume."""
	prev = frappe.flags.get("jarvis_delegated_send")
	frappe.flags.jarvis_delegated_send = True
	try:
		yield
	finally:
		frappe.flags.jarvis_delegated_send = prev
