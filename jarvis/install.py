"""after_install hook - seed what a fresh site cannot get any other way.

``install_app`` runs ``after_install`` but NEVER ``after_migrate``, and it marks
every patch complete without executing it (``frappe/installer.py``). So a site
that is installed and not yet migrated only has what DocType sync produced.

DocType sync auto-creates any role named in a permission row
(``core/doctype/doctype/doctype.py``), which covers "Jarvis User" (16 DocTypes),
"Jarvis Admin" (10) and "Jarvis Skill Reviewer" (1). Named in NO DocType, and so
absent on such a site before this hook existed:

  * "Knowledge Wiki Manager"  (wiki curator rights, wiki_permissions.py)

Observed live on a freshly reinstalled tenant: the synced roles present, the
one above missing.

The Agents Marketplace catalog (``Jarvis Agent Listing``) has the same shape:
it is synced from the bundled registry ONLY by the ``after_migrate`` hook, so a
freshly onboarded site shows an empty Agents section until its first later
migrate. Seeded here too, for the same reason.

Reuses the migrate-time seeders rather than duplicating them, so the two entry
points cannot drift. Idempotent: every seeder inside is exists-guarded (the
catalog sync upserts by ``agent_slug``).

Mirrors ``jarvis_admin_v2/install.py``, which exists for the same reason.
"""

import frappe

from jarvis.chat.wiki_permissions import WIKI_MANAGER_ROLE
from jarvis.learning.roles import after_migrate as seed_roles_and_settings

# The roles no DocType names, i.e. the ones that exist ONLY because this hook
# (or a later migrate) seeded them. Verified below because the seeder cannot
# report its own failure -- see after_install.
_INSTALL_ONLY_ROLES = (WIKI_MANAGER_ROLE,)


def after_install() -> None:
	seed_roles_and_settings()
	frappe.db.commit()

	# seed_roles_and_settings is best-effort BY DESIGN: it wraps its body in
	# try/except + log_error and never re-raises, because a failed seed must
	# never abort a `bench migrate`. At INSTALL time that same property is a
	# trap -- a transient failure would log quietly, this hook would commit and
	# return, install_app would report success, and the tenant would land in
	# exactly the half-seeded state this hook exists to prevent, discovered only
	# when a user hits a permission wall.
	#
	# So verify, and fail the install loudly instead. A provisioning run that
	# fails and gets retried beats a tenant that is silently missing its wiki role.
	missing = [r for r in _INSTALL_ONLY_ROLES if not frappe.db.exists("Role", r)]
	if missing:
		frappe.throw(
			"jarvis after_install could not seed required roles: "
			+ ", ".join(missing)
			+ ". The seeder swallows its own errors; see the 'jarvis wiki roles "
			"seed failed' Error Log entry for the cause."
		)

	# The Agents Marketplace catalog (Jarvis Agent Listing) is otherwise seeded
	# ONLY by the after_migrate hook, which install_app never runs -- so a freshly
	# onboarded site shows an EMPTY Agents section until its first later migrate
	# happens to fire the sync. Seed it here, the same as the roles above, so a
	# day-1 site has the catalog from the moment jarvis is installed.
	#
	# Called directly (not agent_catalog.after_migrate, whose wrapper swallows
	# exceptions -- against this hook's fail-loudly doctrine). Function-local
	# import to avoid a new module-level import edge (agent_catalog itself
	# lazy-imports to break a cycle).
	from jarvis.chat.agent_catalog import sync_agent_listings

	sync_agent_listings()
	# _load_registry() returns an empty agent list WITHOUT raising if the bundled
	# registry is missing, so a direct call can still seed nothing silently. The
	# bundle ships at least one agent, so a 0 count is a real provisioning
	# failure -- fail the install rather than leave the tenant with a permanently
	# empty catalog it can only recover from via a manual migrate.
	if frappe.db.count("Jarvis Agent Listing") == 0:
		frappe.throw(
			"jarvis after_install seeded an empty Agents Marketplace catalog. "
			"The bundled jarvis/agents/registry.json is missing or empty; see the "
			"'jarvis agent catalog: registry.json missing' Error Log entry."
		)
