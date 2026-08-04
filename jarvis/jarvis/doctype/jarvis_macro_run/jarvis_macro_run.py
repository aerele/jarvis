"""Jarvis Macro Run — execution state for one run of a Jarvis Macro.

Created when a macro starts; the server-side chaining hook in
``jarvis.chat.turn_handler`` reads it (via ``jarvis.chat.macros.advance_after_turn``)
to decide whether to enqueue the next step, and the SPA renders progress from the
``status``/``current_step``/``total_steps`` fields.
"""

import frappe
from frappe.model.document import Document


class JarvisMacroRun(Document):
	pass


def on_doctype_update():
	"""Composite (owner, creation) index.

	Neither ``owner`` nor ``creation`` together is indexed on this table,
	which -- unlike ``Jarvis Macro`` -- grows without bound, so every
	owner-scoped read here falls back to a table scan:

	- ``list_macro_runs`` (macros_api.py:456) filters ``r.owner = %(owner)s``
	  (+ optional status/macro) and sorts ``ORDER BY r.creation DESC`` for the
	  dashboard's run history -- the exact filter+sort shape this index serves.
	- ``macro_run_stats`` (macros_api.py:488) runs two more owner-scoped
	  scans: a ``GROUP BY status WHERE owner = %(owner)s`` and a
	  ``MAX(creation) WHERE owner = %(owner)s`` -- the latter is answered
	  directly off this index without touching the table.

	``frappe.db.add_index`` no-ops when the index already exists, so repeated
	migrates are harmless.
	"""
	frappe.db.add_index(
		"Jarvis Macro Run",
		["owner", "creation"],
		index_name="owner_creation_index",
	)
