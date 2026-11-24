"""Drop the retired allow_custom_urls policy's leftover row from Jarvis Settings.

Custom URL connectors are now always available, like every catalog preset: the
same guards apply to both (a Custom URL row is MCP-only, SSRF-checked, unusable
until its connection test passes, write actions park behind a confirmation
card, every call is audited). Jarvis Settings is a Single, so the old flag
lived as a row in tabSingles - delete it so nothing lingers. Keyed on doctype
AND field so sibling Settings values are untouched. Idempotent.
"""

import frappe


def execute():
	frappe.db.delete("Singles", {"doctype": "Jarvis Settings", "field": "allow_custom_urls"})
	frappe.clear_cache(doctype="Jarvis Settings")
