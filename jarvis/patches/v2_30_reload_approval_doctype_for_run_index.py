"""Ensure existing sites create the bounded Approval Request run index."""

import frappe


def execute():
	# The field JSON carries ``search_index`` for fresh installs. Reloading this
	# one DocType is safe and makes the required index explicit for upgrades.
	frappe.reload_doc("jarvis", "doctype", "jarvis_approval_request", force=True)
