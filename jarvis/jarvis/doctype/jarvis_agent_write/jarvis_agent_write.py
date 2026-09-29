import frappe
from frappe.model.document import Document


class JarvisAgentWrite(Document):
	@staticmethod
	def clear_old_logs(days=90):
		"""Satisfies frappe's ``LogType`` protocol so Log Settings picks this
		doctype up via ``default_log_clearing_doctypes`` in hooks.py (mirrors
		``Jarvis Trigger Activity`` / ``Jarvis Connector Log`` — WITHOUT this method
		ON THE CLASS the hook registration is silently dropped by
		``remove_unsupported_doctypes`` and retention never runs). Prunes on the
		indexed, immutable ``at`` column, full-precision like the siblings."""
		from frappe.query_builder import Interval
		from frappe.query_builder.functions import Now

		table = frappe.qb.DocType("Jarvis Agent Write")
		frappe.db.delete(table, filters=(table.at < (Now() - Interval(days=days))))
