"""Clear the legacy custom-Bearer value from jarvis_admin_api_key.

Before this migration, jarvis_admin_api_key held a 32-char hex token sent as
'Authorization: Bearer <token>'. The new auth uses native Frappe
api_key:api_secret, so the same field is repurposed for the native api_key
(15-char). Mixing the two would mean sending a Bearer-formatted value as an
api_key, which 401s. Clear it so the operator re-onboards cleanly."""

import frappe


def execute():
	frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "jarvis_admin_api_key", "")
	frappe.db.commit()
