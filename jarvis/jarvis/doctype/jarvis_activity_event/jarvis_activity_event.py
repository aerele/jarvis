import frappe
from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisActivityEvent(NotRenamable, Document):
	@staticmethod
	def clear_old_logs(days=90):
		"""Satisfies frappe's ``LogType`` protocol so Log Settings picks this
		doctype up via ``default_log_clearing_doctypes`` in hooks.py (mirrors
		``Jarvis Agent Write``: WITHOUT this method ON THE CLASS the hook
		registration is silently dropped by ``remove_unsupported_doctypes`` and
		retention never runs). Prunes on the indexed ``at`` column, full-precision
		like the siblings."""
		from frappe.query_builder import Interval
		from frappe.query_builder.functions import Now

		table = frappe.qb.DocType("Jarvis Activity Event")
		frappe.db.delete(table, filters=(table.at < (Now() - Interval(days=days))))


def on_doctype_update():
	"""Composite (actor, at) index.

	The admin report filters one person over a date range and sorts by time
	(``activity_report.events`` keyset-pages on ``at``); the single-column
	``actor`` and ``at`` indexes cannot serve that filter+sort together.

	``frappe.db.add_index`` no-ops when the index already exists, so repeated
	migrates are harmless.
	"""
	frappe.db.add_index("Jarvis Activity Event", ["actor", "at"], index_name="actor_at_index")
	# (event, at): ``first_event_at`` is one ORDER BY at LIMIT 1 per event kind.
	frappe.db.add_index("Jarvis Activity Event", ["event", "at"], index_name="event_at_index")
	# No covering (at, actor, ...) index: the summary no longer scans this table over a
	# whole range. ``jarvis.activity_rollup`` reads ONE day at a time through the
	# single-column ``at`` index and keeps the totals in ``Jarvis Activity Daily``; a
	# 5-column index would only add write amplification to every insert (plan rev 4, RV4-8).
