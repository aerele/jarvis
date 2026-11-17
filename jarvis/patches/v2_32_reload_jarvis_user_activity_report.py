"""Refresh the "Jarvis User Activity" report row from its JSON.

``v2_32_rename_agent_write_log_report`` only renames the old row, so after it the
row still says ``ref_doctype = "Jarvis Agent Write"`` and carries the old roles. Normal sync would fix that, but it skips a JSON whose
``modified`` is not newer than the DB row's, and the renamed row keeps the old
(possibly later) stamp. ``force=True`` re-imports regardless.

The re-import deletes and re-inserts the row with ``for_reload=True``, which skips
``Report.on_trash`` and so keeps the Custom Role that points at the report. It also
resets the roles to the JSON's. On a fresh install, or a site that never had the old
report, this is a plain (re)import. Idempotent."""

import frappe


def execute():
	frappe.reload_doc("jarvis", "report", "jarvis_user_activity", force=True)
