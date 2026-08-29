"""Drop the retired auto_apply_changes field's leftover row from Jarvis Settings.

auto_apply_changes was a site-wide "skip confirmation" toggle deprecated in
issue #186 (auto-apply moved to per-conversation Jarvis Conversation.auto_apply)
and was never read or written since. It is now removed from the Jarvis Settings
schema. Jarvis Settings is a Single, so any value lived as a row in tabSingles -
delete it so nothing lingers. Keyed on doctype AND field so sibling Settings
values are untouched. Idempotent: a DELETE of an absent row is a no-op.

(This is not a rejection of a site-wide skip switch - the field simply never
worked; the live control is the admin-armed per-macro skip_confirmation.)
"""

import frappe


def execute():
	frappe.db.delete("Singles", {"doctype": "Jarvis Settings", "field": "auto_apply_changes"})
	frappe.clear_cache(doctype="Jarvis Settings")
