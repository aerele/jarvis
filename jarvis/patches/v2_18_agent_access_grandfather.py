"""Grandfather every EXISTING agent installation into the deny-by-default access model.

jarvis#1062 inverts the ``Jarvis Agent Listing`` access default. An empty
``allowed_roles`` table used to mean "unrestricted — everyone may install and
run this"; it now means CLOSED (only a Jarvis Admin / System Manager gets
through), and access is granted explicitly to roles and/or to named users.

Every listing in the shipped registry declares ``default_allowed_roles: []``, so
on a live tenant the inversion closes ALL of them at once. Without this patch the
upgrade would silently break every install that works today: the owner would lose
the catalog row, ``run_agent_now`` would refuse, ``_sweep_one`` would skip the
scheduled dispatch, and ``build_agent_push_payload`` would drop the slug from the
container roster on the next Apply.

What it does: for every ``Jarvis Agent Installation``, add its ``owner`` and its
``run_as_user`` (the EXECUTING identity — the one the run gates actually litigate,
and frequently NOT the owner after an admin re-target) to that listing's
``allowed_users``. Both identities are grandfathered because both are gated:
the owner drives catalog visibility and install-surface reads, the run-as user
drives dispatch and the push.

Deliberately NARROW in three ways:

  * Only identities that ALREADY have a working install are added. Nobody gains
    access they did not effectively have a minute before the migrate.
  * An identity already allowed BY ROLE is skipped — the role grant is the
    stronger, more durable statement, and pinning the person by name as well
    would silently outlive the role being revoked.
  * ``Administrator`` and ``Guest`` are never added. They are refused as run-as
    identities everywhere else in this app (``permissions.is_valid_run_as`` and
    the installation controller), so naming them here would record an allowance
    no dispatch path would ever honour.

Idempotent: re-running adds nothing. Written with direct child-row inserts rather
than ``doc.save()`` on the parent listing, for the same reason as
``v2_12_backfill_role_skill_allowed_roles`` — re-saving a legacy listing would
re-run its whole controller validate chain against rows that predate those rules,
and a throw there would block the entire migrate for a backfill unrelated to it.
"""

import frappe

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
ALLOWED_ROLE = "Jarvis Agent Allowed Role"
ALLOWED_USER = "Jarvis Agent Allowed User"

# Never grantable as an agent identity — mirrors permissions.is_valid_run_as and
# agents_api._NON_SELECTABLE_ROLES.
_NEVER_GRANT = ("Administrator", "Guest")


def execute():
	if not (
		frappe.db.table_exists(INSTALLATION)
		and frappe.db.table_exists(LISTING)
		and frappe.db.table_exists(ALLOWED_USER)
	):
		return
	grandfather_existing_installs()


def grandfather_existing_installs() -> dict:
	"""The patch body, callable directly so a test can drive it. -> {"added": n}."""
	installs = frappe.get_all(INSTALLATION, fields=["agent", "owner", "run_as_user"])
	if not installs:
		return {"added": 0}

	# Which identities each listing already admits, in two queries rather than two
	# per install row.
	listings = sorted({i.agent for i in installs if i.agent})
	roles_by_listing: dict[str, set[str]] = {name: set() for name in listings}
	users_by_listing: dict[str, set[str]] = {name: set() for name in listings}
	if frappe.db.table_exists(ALLOWED_ROLE):
		for row in frappe.get_all(
			ALLOWED_ROLE,
			filters={"parenttype": LISTING, "parentfield": "allowed_roles", "parent": ("in", listings)},
			fields=["parent", "role"],
		):
			roles_by_listing.setdefault(row.parent, set()).add(row.role)
	for row in frappe.get_all(
		ALLOWED_USER,
		filters={"parenttype": LISTING, "parentfield": "allowed_users", "parent": ("in", listings)},
		fields=["parent", "user"],
	):
		users_by_listing.setdefault(row.parent, set()).add(row.user)

	roles_by_user: dict[str, set[str]] = {}
	added = 0
	for inst in installs:
		listing = inst.agent
		if not listing or listing not in roles_by_listing:
			# A listing deleted out from under its install: there is nothing to grant
			# access ON, and the install is already inert at every gate.
			continue
		for identity in (inst.owner, inst.run_as_user):
			identity = (identity or "").strip()
			if not identity or identity in _NEVER_GRANT:
				continue
			if identity in users_by_listing.get(listing, set()):
				continue
			if not frappe.db.exists("User", identity):
				# A deleted user would fail the child row's Link validation and break
				# the whole migrate for a row nothing can dispatch anyway.
				continue
			if identity not in roles_by_user:
				roles_by_user[identity] = set(frappe.get_roles(identity))
			if roles_by_user[identity] & roles_by_listing.get(listing, set()):
				continue  # already allowed by role — the stronger grant, leave it alone
			frappe.get_doc(
				{
					"doctype": ALLOWED_USER,
					"parenttype": LISTING,
					"parentfield": "allowed_users",
					"parent": listing,
					"user": identity,
				}
			).insert(ignore_permissions=True)
			users_by_listing.setdefault(listing, set()).add(identity)
			added += 1
	if added:
		frappe.db.commit()
	return {"added": added}
