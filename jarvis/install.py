"""after_install hook - seed what a fresh site cannot get any other way.

``install_app`` runs ``after_install`` but NEVER ``after_migrate``, and it marks
every patch complete without executing it (``frappe/installer.py``). So a site
that is installed and not yet migrated only has what DocType sync produced.

DocType sync auto-creates any role named in a permission row
(``core/doctype/doctype/doctype.py``), which covers "Jarvis User" (16 DocTypes),
"Jarvis Admin" (10) and "Jarvis Skill Reviewer" (1). Named in NO DocType, and so
absent on such a site before this hook existed:

  * "Knowledge Wiki Manager"  (wiki curator rights, wiki_permissions.py)
  * "Jarvis Support User" / "Jarvis Support Admin"  (support panel scope)

Observed live on a freshly reinstalled tenant: all three synced roles present,
all three of the above missing.

Reuses the migrate-time seeder rather than duplicating it, so the two entry
points cannot drift. Idempotent: every seeder inside is exists-guarded.

Mirrors ``jarvis_admin_v2/install.py``, which exists for the same reason.
"""

import frappe

from jarvis.chat.wiki_permissions import WIKI_MANAGER_ROLE
from jarvis.learning.roles import after_migrate as seed_roles_and_settings
from jarvis.permissions import JARVIS_SUPPORT_ADMIN_ROLE, JARVIS_SUPPORT_USER_ROLE

# The roles no DocType names, i.e. the ones that exist ONLY because this hook
# (or a later migrate) seeded them. Verified below because the seeder cannot
# report its own failure -- see after_install.
_INSTALL_ONLY_ROLES = (
	WIKI_MANAGER_ROLE,
	JARVIS_SUPPORT_USER_ROLE,
	JARVIS_SUPPORT_ADMIN_ROLE,
)


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
	# fails and gets retried beats a tenant that is silently missing its wiki
	# and support roles.
	missing = [r for r in _INSTALL_ONLY_ROLES if not frappe.db.exists("Role", r)]
	if missing:
		frappe.throw(
			"jarvis after_install could not seed required roles: "
			+ ", ".join(missing)
			+ ". The seeder swallows its own errors; see the 'jarvis wiki roles "
			"seed failed' Error Log entry for the cause."
		)
