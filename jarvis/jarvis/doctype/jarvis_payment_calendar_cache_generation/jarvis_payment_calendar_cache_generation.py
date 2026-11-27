"""Payment Calendar cache generation: one refresh of a (company, side).

The name comes from a sequence (``autoincrement``). Metadata stays 30 days; the rows of the generation
(Jarvis Payment Calendar Cache Row) are deleted after the grace. Read access is denied to every user but
Administrator by ``jarvis.permissions.deny_cache_table_*`` (registered in hooks.py).
"""

import frappe
from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisPaymentCalendarCacheGeneration(NotRenamable, Document):
	pass


def on_doctype_update():
	"""The indexes of the janitor, the tick and the status. Each is created only when it is missing.

	- (company, side, state): the janitor of one side and the per-side generation bound of the preflight;
	- (state, deadline): the tick looks for `building` and `failed` generations past their deadline;
	- (state, modified): retired generations past the grace, and old metadata;
	- (as_of): the status reads the newest days of night records and durations.
	"""
	table = "Jarvis Payment Calendar Cache Generation"
	frappe.db.add_index(table, ["company", "side", "state"], index_name="company_side_state")
	frappe.db.add_index(table, ["state", "deadline"], index_name="state_deadline")
	frappe.db.add_index(table, ["state", "modified"], index_name="state_modified")
	frappe.db.add_index(table, ["as_of"], index_name="as_of")
