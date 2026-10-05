"""Jarvis Trigger Activity DocType controller.

One append-only row per trigger firing (Success / Failed / Blocked / Skipped),
written server-side by ``jarvis.triggers.engine._insert_activity``. The
``trigger`` reference is a Data SNAPSHOT — deliberately NOT a Link — so
deleting a trigger never blocks on LinkExistsError and the log outlives the
trigger it narrates. Doctype perms are System Manager read+delete only; every
other user sees visibility-filtered rows via
``jarvis.chat.triggers_api.list_activity_page`` (read access on the target
doc is the axis). Reaped after 90 days via hooks'
``default_log_clearing_doctypes`` (mirrors core's WebhookRequestLog).
"""

import frappe
from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisTriggerActivity(NotRenamable, Document):
	@staticmethod
	def clear_old_logs(days=90):
		from frappe.query_builder import Interval
		from frappe.query_builder.functions import Now

		table = frappe.qb.DocType("Jarvis Trigger Activity")
		frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))
