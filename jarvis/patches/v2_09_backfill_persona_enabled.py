import frappe


def execute():
	"""Seed persona_enabled=1 for benches whose Jarvis Settings Single predates
	the field, so BOTH the read and the write paths default to ON.

	The NULL=ON readers (``turn_handler.persona_feature_enabled``) already treat a
	missing row as ON, but that alone is not enough: a full
	``Jarvis Settings.save()`` (e.g. the onboarding LLM connect / re-sync) runs the
	unset Check through ``get_valid_dict``, which coerces it to 0 and writes an
	explicit "0" row - silently flipping the feature OFF with no admin action.
	Seeding a real "1" row before any such save prevents that.

	Row-existence probe, not a value test - mirrors
	``learning.roles._seed_personalise_settings_defaults`` (the same v16
	get_single_value coercion): seed ONLY when the row is absent, so a re-run
	never clobbers an admin's explicit 0, and pass ``update_modified=False`` so the
	backfill does not bump ``Jarvis Settings.modified``. Fresh installs get "1"
	from the field default on first insert (``_set_defaults``) and never run
	patches, so both paths converge on ON.

	Jarvis Settings is a Single (data in tabSingles, no tab<Doctype> table), so
	gate on the meta field, not a column check.
	"""
	if not frappe.get_meta("Jarvis Settings").has_field("persona_enabled"):
		return
	row = frappe.db.sql(
		"select value from tabSingles where doctype=%s and field=%s",
		("Jarvis Settings", "persona_enabled"),
	)
	if row:
		# Already has a row - possibly an admin's explicit 0. Never clobber it.
		return
	frappe.db.set_single_value("Jarvis Settings", "persona_enabled", 1, update_modified=False)
