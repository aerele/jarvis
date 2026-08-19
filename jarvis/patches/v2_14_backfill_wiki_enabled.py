import frappe


def execute():
	"""Seed wiki_enabled=1 for benches whose Jarvis Settings Single predates the
	field, so BOTH the read and the write paths default to ON.

	The NULL=ON reader (``chat.wiki.wiki_enabled``) already treats a missing/NULL
	row as ON, but that alone is not enough for the #731 kill switch. Two problems
	the explicit row fixes:

	* A full ``Jarvis Settings.save()`` (onboarding, re-sync, any Desk save) runs
	  the unset Check through ``get_valid_dict``, which coerces it to 0 and writes
	  an explicit "0" row - silently disabling the wiki fleet-wide with no admin
	  action. This mirrors ``v2_09_backfill_persona_enabled`` for the sibling
	  kill switch.
	* The #731 scrub trigger fires on a genuine 1 -> 0 change of the toggle. A
	  bare NULL coerces to 0 on load, so a real disable of a pre-v2 tenant would
	  read as "0 -> 0" (unchanged) and the scrub would never run - the kill switch
	  would be inert for exactly the tenants that predate the field. A real "1"
	  row makes a disable a 1 -> 0 change the trigger can see.

	Probe mirrors ``wiki_enabled`` itself (row absent OR value NULL reads as ON):
	seed ONLY those, so a re-run never clobbers an admin's explicit 0 (a deliberate
	disable) and an already-explicit 1 is left alone. ``update_modified=False`` so
	the backfill does not bump ``Jarvis Settings.modified``. Fresh installs get "1"
	from the field default on first insert and never run patches, so both paths
	converge on ON.

	Jarvis Settings is a Single (data in tabSingles, no tab<Doctype> table), so
	gate on the meta field, not a column check.
	"""
	if not frappe.get_meta("Jarvis Settings").has_field("wiki_enabled"):
		return
	row = frappe.db.sql(
		"select value from tabSingles where doctype=%s and field=%s",
		("Jarvis Settings", "wiki_enabled"),
	)
	# Absent row, or a row whose value is NULL: both currently read as ON by
	# default. An explicit "0" (deliberate disable) or "1" must never be touched.
	if row and row[0][0] is not None:
		return
	frappe.db.set_single_value("Jarvis Settings", "wiki_enabled", 1, update_modified=False)
