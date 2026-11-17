"""Install the Document Intake indexes and the ``read_on`` column on existing sites.

The indexes are declared in ``Jarvis Trigger Activity.on_doctype_update``; a plain model
sync does not fire it on a site whose DocType JSON did not change that part, so the forced
reload does. ``frappe.db.add_index`` no-ops when an index already exists.
"""

import frappe


def execute():
	frappe.reload_doc("jarvis", "doctype", "jarvis_trigger_activity", force=True)
