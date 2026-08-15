"""Rename openclaw_* fields on Jarvis Settings to agent_* and jarvis_admin_*.

Background: Phase 1 reserved two placeholder fields (openclaw_endpoint,
openclaw_api_key) for the future SaaS control plane. Phase 3 finally
fills that role with `jarvis_admin`, so the placeholders get their real
names. The operator-tab fields (gateway URL/token/paths) get brand-
neutral `agent_*` names so customers never see "openclaw" in the UI.

Module file names (openclaw_bootstrap.py, etc.) are intentionally NOT
renamed - they're implementation detail, not customer-visible.

Implementation note: Jarvis Settings is a Single DocType. Single DocTypes
store data as key/value rows in `tabSingles` rather than columns on a
per-doctype table. `frappe.model.rename_field` is designed for regular
DocTypes and does not rewrite `tabSingles.field` values. So this patch
manually updates the `field` column for each rename, plus the matching
rows in `__Auth` for Password-type fields.
"""

import frappe

RENAMES = [
	("openclaw_endpoint", "jarvis_admin_url"),
	("openclaw_api_key", "jarvis_admin_api_key"),
	("openclaw_gateway_url", "agent_url"),
	("openclaw_gateway_token", "agent_token"),
	("openclaw_compose_dir", "agent_compose_dir"),
	("openclaw_config_path", "agent_config_path"),
	("openclaw_llm_key_path", "agent_llm_key_path"),
]

# Password fields whose values also live in __Auth and must be renamed there.
PASSWORD_RENAMES = {
	"openclaw_api_key": "jarvis_admin_api_key",
	"openclaw_gateway_token": "agent_token",
}


def execute():
	"""Rename each field's row in tabSingles + matching row in __Auth."""
	doctype = "Jarvis Settings"

	for old_name, new_name in RENAMES:
		# Skip if no row exists under the old name (fresh install, or
		# patch already applied).
		existing = frappe.db.sql(
			"SELECT 1 FROM tabSingles WHERE doctype = %s AND field = %s LIMIT 1",
			(doctype, old_name),
		)
		if not existing:
			continue

		# If a row already exists under the new name, drop the old one
		# to avoid a UNIQUE constraint conflict.
		already_renamed = frappe.db.sql(
			"SELECT 1 FROM tabSingles WHERE doctype = %s AND field = %s LIMIT 1",
			(doctype, new_name),
		)
		if already_renamed:
			frappe.db.sql(
				"DELETE FROM tabSingles WHERE doctype = %s AND field = %s",
				(doctype, old_name),
			)
		else:
			frappe.db.sql(
				"UPDATE tabSingles SET field = %s WHERE doctype = %s AND field = %s",
				(new_name, doctype, old_name),
			)

	# __Auth stores encrypted password values keyed by (doctype, name, fieldname).
	# For Single DocTypes the `name` is the DocType name itself.
	for old_name, new_name in PASSWORD_RENAMES.items():
		existing = frappe.db.sql(
			"SELECT 1 FROM `__Auth` WHERE doctype = %s AND name = %s AND fieldname = %s LIMIT 1",
			(doctype, doctype, old_name),
		)
		if not existing:
			continue
		already_renamed = frappe.db.sql(
			"SELECT 1 FROM `__Auth` WHERE doctype = %s AND name = %s AND fieldname = %s LIMIT 1",
			(doctype, doctype, new_name),
		)
		if already_renamed:
			frappe.db.sql(
				"DELETE FROM `__Auth` WHERE doctype = %s AND name = %s AND fieldname = %s",
				(doctype, doctype, old_name),
			)
		else:
			frappe.db.sql(
				"UPDATE `__Auth` SET fieldname = %s WHERE doctype = %s AND name = %s AND fieldname = %s",
				(new_name, doctype, doctype, old_name),
			)

	frappe.db.commit()
	frappe.clear_cache(doctype=doctype)
