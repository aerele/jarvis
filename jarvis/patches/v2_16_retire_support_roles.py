import frappe

# The two roles retired in this change. Support access now rides the base Jarvis
# roles (Jarvis User -> own scope, Jarvis Admin -> all scope), so these — and the
# `Has Role` rows the old lazy boot-grant scattered across nearly every chat user —
# are dead weight.
_RETIRED_ROLES = ("Jarvis Support User", "Jarvis Support Admin")


def execute():
	"""Delete the standalone support panel roles and every assignment of them.

	The old model gave each chat user `Jarvis Support User` lazily at boot, so a
	live tenant carries one `Has Role` row per user plus the two `Role` docs.
	Nothing reads these roles any more. Drop the assignments first (a `Role`
	cannot be deleted while `Has Role` rows reference it), then the roles. Both
	steps are exists-guarded, so a re-run is a no-op. `force=True` clears any
	stray reference (e.g. a Role Profile) the assignment sweep did not.
	"""
	for role in _RETIRED_ROLES:
		if not frappe.db.exists("Role", role):
			continue
		frappe.db.delete("Has Role", {"role": role})
		frappe.delete_doc("Role", role, ignore_permissions=True, force=True)
