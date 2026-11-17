import frappe
from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisActivityDaily(NotRenamable, Document):
	@staticmethod
	def clear_old_logs(days=90):
		"""Satisfies frappe's ``LogType`` protocol so Log Settings picks this
		doctype up via ``default_log_clearing_doctypes`` in hooks.py (same contract
		as ``Jarvis Activity Event``: WITHOUT this method ON THE CLASS the hook
		registration is silently dropped and retention never runs). Prunes on the
		indexed ``day`` column: a day is gone once it is more than ``days`` old."""
		from frappe.utils import add_days, getdate

		table = frappe.qb.DocType("Jarvis Activity Daily")
		frappe.db.delete(table, filters=(table.day < add_days(getdate(), -days)))


def on_doctype_update():
	"""One row per (day, actor). The rollup deletes a day and re-inserts it in one
	transaction, so the constraint is a backstop against a double insert, not a
	routine path. ``add_unique`` no-ops when the constraint exists (repeat migrates)."""
	frappe.db.add_unique("Jarvis Activity Daily", ["day", "actor"], constraint_name="day_actor")
