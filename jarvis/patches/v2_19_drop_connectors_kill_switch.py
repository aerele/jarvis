"""Drop the retired connectors_enabled field's leftover row from Jarvis Settings.

MCP connectors are now a default feature (no site-wide kill switch); the
allow_custom_urls policy is unaffected. connectors_enabled is removed from the
Jarvis Settings schema. Jarvis Settings is a Single, so any value lived as a
row in tabSingles - delete it so nothing lingers. Keyed on doctype AND field
so sibling Settings values are untouched. Idempotent: a DELETE of an absent
row is a no-op.
"""

import frappe


def execute():
	frappe.db.delete("Singles", {"doctype": "Jarvis Settings", "field": "connectors_enabled"})
	frappe.clear_cache(doctype="Jarvis Settings")
