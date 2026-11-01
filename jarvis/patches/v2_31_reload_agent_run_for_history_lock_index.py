"""Install the narrow Run lock index on existing sites.

The index is declared in ``Jarvis Agent Run.on_doctype_update`` so new sites get
it during DocType creation. Existing sites need this reload because their
DocType JSON did not change when the controller gained the index.
"""

import frappe

DOCTYPES = ("jarvis_agent_run",)


def execute():
	for doctype in DOCTYPES:
		frappe.reload_doc("jarvis", "doctype", doctype, force=True)
