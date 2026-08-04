import frappe

# The indexes live in each doctype's on_doctype_update(), which is the right
# place: Frappe runs it whenever the DocType document is saved, so a FRESH
# install gets the indexes for free when the doctype is first created, and
# patches never run on a fresh install anyway.
#
# The gap is the EXISTING site. `bench migrate` only re-imports a DocType when
# its .json changes, and this work added indexes by editing the .py controller
# only. So on_doctype_update() never fired, and the indexes were silently never
# created. Confirmed on a real bench: after migrate the tables carried only
# PRIMARY/creation/modified, and the indexes appeared the instant the doctypes
# were reloaded.
#
# reload_doc re-saves the DocType, which is what actually triggers
# on_doctype_update(). frappe.db.add_index() itself no-ops when the index is
# already present, so this is safe to re-run.
DOCTYPES = ("jarvis_agent_finding", "jarvis_agent_run")


def execute():
	for dt in DOCTYPES:
		frappe.reload_doc("jarvis", "doctype", dt, force=True)
