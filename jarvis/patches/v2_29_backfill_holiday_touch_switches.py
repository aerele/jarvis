import frappe

_SWITCHES = ("holiday_touches_enabled", "birthday_greetings_enabled")


def execute():
	"""Seed holiday_touches_enabled=1 and birthday_greetings_enabled=1 for benches
	whose Jarvis Settings Single predates the fields, so existing sites read both
	switches ON after migrate, like fresh installs.

	Without a real row, ``get_single_value`` coerces an unset Check to 0
	(``frappe/model/document.py:496-505``, ``frappe/database/database.py:905-922``),
	so every existing site would read the feature as OFF. A full
	``Jarvis Settings.save()`` would also write an explicit "0" through
	``get_valid_dict``. Seeding a real "1" row before either happens prevents both.

	Same shape as ``v2_09_backfill_persona_enabled`` (the same v16
	get_single_value coercion): a row-existence probe, not a value test, so a
	re-run never clobbers an admin's explicit 0, and ``update_modified=False`` so
	the backfill does not bump ``Jarvis Settings.modified``.

	Fresh installs never run patches, and the field default does NOT reach them:
	``after_install`` writes other Jarvis Settings rows, so ``new_doc`` defaults are never
	stored. They converge on ON by two other means instead:
	``learning.roles._seed_personalise_settings_defaults`` (run by ``after_install`` and
	every ``after_migrate``) seeds the same two rows, and ``holiday_touch.settings.site``
	reads an absent row as the field default (on).

	Jarvis Settings is a Single (data in tabSingles, no tab<Doctype> table), so
	gate on the meta field, not a column check. Jarvis User Settings needs no
	backfill: its ``show_holiday_touches`` column is added as NOT NULL DEFAULT 1.
	"""
	meta = frappe.get_meta("Jarvis Settings")
	for field in _SWITCHES:
		if not meta.has_field(field):
			continue
		row = frappe.db.sql(
			"select value from tabSingles where doctype=%s and field=%s",
			("Jarvis Settings", field),
		)
		if row:
			# Already has a row - possibly an admin's explicit 0. Never clobber it.
			continue
		frappe.db.set_single_value("Jarvis Settings", field, 1, update_modified=False)
