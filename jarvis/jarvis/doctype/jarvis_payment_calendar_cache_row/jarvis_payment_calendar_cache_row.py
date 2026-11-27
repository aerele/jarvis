"""Payment Calendar cache row: one stored item of a generation.

Rows are inserted in chunks with ``bulk_insert`` (no controller runs) and deleted with ``frappe.db.delete``.
The name is ``<generation:012d><seq:08d>``.
The amount fields (original, paid, credit, discount, remaining, account_remaining) are Data text that holds
exact Decimal values. Never sum or sort them in SQL: sum them in Python with Decimal.
Read access is denied to every user but Administrator by
``jarvis.permissions.deny_cache_table_*`` (registered in hooks.py).
"""

import frappe
from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisPaymentCalendarCacheRow(NotRenamable, Document):
	pass


def on_doctype_update():
	"""Both indexes lead with the generation, so a purge or a per-generation scan never reads other rows."""
	table = "Jarvis Payment Calendar Cache Row"
	frappe.db.add_index(table, ["generation", "due_date", "name"], index_name="generation_due_date_name")
	frappe.db.add_index(table, ["generation", "party_name", "name"], index_name="generation_party_name_name")
