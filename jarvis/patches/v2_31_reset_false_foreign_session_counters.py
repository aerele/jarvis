"""Zero the foreign-session counter once (incident 2026-11-06).

Before this release the site reported its own background sessions (cache prewarm,
chat titles, polish, preflight) and long scheduled agent runs as foreign, so every
count to date is polluted. Reset it so a real copy of the site stands out from here
on. Safe to run on a site that never counted anything.
"""

import frappe


def execute():
	meta = frappe.get_meta("Jarvis Settings")
	values = {
		field: value
		for field, value in (("foreign_sessions_seen", 0), ("foreign_session_last_at", None))
		if meta.has_field(field)
	}
	if values:
		frappe.db.set_single_value("Jarvis Settings", values, update_modified=False)
