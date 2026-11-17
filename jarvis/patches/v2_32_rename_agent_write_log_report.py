"""Rename the standard Report "Agent Write Log" to "Jarvis User Activity".

The report now covers every Jarvis action (agent reads, exports, writes and direct
app actions), not only agent writes, so it gets a new name and a new reference
doctype. The old report folder is deleted in the same change, so migrate's sync
cannot re-insert "Agent Write Log"; this patch moves the existing DB row instead.

Why rename rather than delete and re-create: ``frappe.rename_doc`` rewrites every
Link to the report (Custom Role.report, so any role an admin granted on top of the
JSON's own roles survives) and every Dynamic Link whose type is "Report" (Workspace
shortcuts and links). Delete and insert would drop all of them. The Dynamic Link
rewrite reads a cached map that can be up to 12 hours stale, so the Workspace
tables are also updated directly below; that is a no-op when the rename already
did it.

``rename_doc`` only changes the row's ``name`` (plus ``report_name``, the autoname
field, and the child rows' ``parent``). ``ref_doctype`` and the roles still describe
the old report, and the row's ``modified`` is left alone, until
``v2_32_reload_jarvis_user_activity_report`` (post_model_sync) reloads the JSON.

Runs in pre_model_sync so the row already has its new name when sync compares it
with the new JSON. Idempotent: skipped when the old row is gone.

When the new row already exists as well (a site where sync created it before this
patch ran), the old row is not renamed over it. Its Custom Role and Workspace links
are moved to the new name first, then the old row is deleted so no orphan with no
controller is left in Desk."""

import frappe

OLD = "Agent Write Log"
NEW = "Jarvis User Activity"

# (child table, type column) of the Dynamic Link fields that can point at a Report.
_DYNAMIC_LINK_TABLES = (
	("Workspace Shortcut", "type"),
	("Workspace Link", "link_type"),
	("Workspace Sidebar Item", "link_type"),  # Frappe 16 only
)


def _move_links():
	"""Point every Workspace link at the new name. Run after ``rename_doc`` because
	its own Dynamic Link rewrite reads a cache that can be hours stale."""
	for table, type_col in _DYNAMIC_LINK_TABLES:
		if frappe.db.table_exists(table):
			frappe.db.sql(
				f"update `tab{table}` set link_to = %s where `{type_col}` = 'Report' and link_to = %s",
				(NEW, OLD),
			)


def execute():
	if not frappe.db.exists("Report", OLD):
		return
	if not frappe.db.exists("Report", NEW):
		frappe.rename_doc("Report", OLD, NEW, force=True, rebuild_search=False)
		_move_links()
		return

	# Both rows exist: keep the new one, carry the old one's links over, drop the old.
	# Report.on_trash would delete the old row's Custom Role, so move it first, unless
	# the new report already has its own.
	if not frappe.db.exists("Custom Role", {"report": NEW}):
		frappe.db.set_value("Custom Role", {"report": OLD}, "report", NEW, update_modified=False)
	_move_links()
	frappe.delete_doc("Report", OLD, force=True, ignore_permissions=True)
