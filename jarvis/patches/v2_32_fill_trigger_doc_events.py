"""Fill Jarvis Trigger.doc_events from doc_event (#825).

A trigger can now run on several doc events, listed in ``doc_events``. Rows saved
before the field existed have it blank; the engine and the controller already fall
back to ``doc_event`` for those, so this only makes the stored value say what the
trigger does (the list filter and the Desk form read it). Safe to run again.
"""

import frappe


def execute():
	if not frappe.get_meta("Jarvis Trigger").has_field("doc_events"):
		return
	frappe.db.sql(
		"""UPDATE `tabJarvis Trigger`
		SET doc_events = doc_event
		WHERE (doc_events IS NULL OR doc_events = '') AND doc_event IS NOT NULL AND doc_event != ''"""
	)
