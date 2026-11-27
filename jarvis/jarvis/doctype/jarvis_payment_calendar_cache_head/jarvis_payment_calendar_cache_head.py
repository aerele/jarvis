"""Payment Calendar cache head: the durable state of one (company, side).

Written only by the cache code with SQL, never by a desk save. Read access is denied to every user but
Administrator by ``jarvis.permissions.deny_cache_table_*`` (registered in hooks.py).
"""

import frappe
from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisPaymentCalendarCacheHead(NotRenamable, Document):
	pass


def on_doctype_update():
	"""One head per (company, side): `INSERT IGNORE` at claim time relies on this unique index.

	add_unique no-ops when the constraint exists, so repeated migrates are harmless.
	"""
	frappe.db.add_unique(
		"Jarvis Payment Calendar Cache Head", ["company", "side"], constraint_name="company_side"
	)
