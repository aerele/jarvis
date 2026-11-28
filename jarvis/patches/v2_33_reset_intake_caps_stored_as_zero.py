"""Give existing sites the default Document Intake limits back (issue #2889).

The two limits (``intake_daily_cap`` 300, ``intake_daily_cap_per_user`` 50) were added to
an existing Single days ago. Frappe stores no row for a new field, and the first save of
Jarvis Settings after migrate turned the unset Int into a stored "0"
(``get_valid_dict``: ``cint(None)``). ``intake.worker._setting`` reads a stored 0 as
"paused", so intake stopped on every site that saved Settings once. At that scale the cast
is the only way these values became 0; an admin who really paused can pause again.

Delete the row when it holds 0 (or empty) so the default applies. A non-zero value is a
deliberate choice and stays. Safe to re-run.
"""

import frappe

_FIELDS = ("intake_daily_cap", "intake_daily_cap_per_user")


def execute():
	singles = frappe.qb.DocType("Singles")
	for field in _FIELDS:
		rows = (
			frappe.qb.from_(singles)
			.select(singles.value)
			.where((singles.doctype == "Jarvis Settings") & (singles.field == field))
			.run()
		)
		if rows and frappe.utils.cint(rows[0][0]) == 0:
			frappe.qb.from_(singles).delete().where(
				(singles.doctype == "Jarvis Settings") & (singles.field == field)
			).run()
	frappe.db.value_cache.pop("Jarvis Settings", None)
	frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")
