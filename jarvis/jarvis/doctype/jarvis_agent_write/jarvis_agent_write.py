import frappe
from frappe.model.document import Document


class JarvisAgentWrite(Document):
	pass


def clear_old_logs(days=90):
	"""Retention for the agent-write audit trail: reap rows older than ``days``,
	filtered on the indexed ``at`` column. Registered in
	``hooks.default_log_clearing_doctypes``; run by Frappe's daily clear_logs job
	(mirrors core's WebhookRequestLog / the sibling Jarvis log doctypes)."""
	cutoff = frappe.utils.add_days(frappe.utils.nowdate(), -abs(int(days or 90)))
	frappe.db.delete("Jarvis Agent Write", {"at": ["<", cutoff]})
