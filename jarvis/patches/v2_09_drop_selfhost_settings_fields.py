"""Drop the retired selfhost_* fields' leftover rows from Jarvis Settings.

Self-hosted deployments were removed (jarvis/selfhost.py deleted); the four
selfhost_* fields are gone from the Jarvis Settings schema. Jarvis Settings is a
Single, so any values lived as rows in tabSingles - delete them so nothing
lingers. Idempotent: a DELETE of absent rows is a no-op.
"""

import frappe

_FIELDS = (
	"selfhost_last_validated_at",
	"selfhost_last_validation",
	"selfhost_tool_user",
	"selfhost_stream",
)


def execute():
	frappe.db.sql(
		"DELETE FROM tabSingles WHERE doctype = %s AND field IN %s",
		("Jarvis Settings", _FIELDS),
	)
	frappe.clear_cache(doctype="Jarvis Settings")
