"""Documents linked to a given record by foreign-key references.

Wraps ``frappe.desk.form.linked_with.get``. Returns a map of related
DocType -> list of records that link to the source via any FK
field. The agent uses this for "show me everything related to this
customer / invoice / order" questions where the relationships are
the Desk's "Connections" panel, not something get_list can express
without prior knowledge of every link field.

Underlying helper enforces read permission on the source via
``frappe.has_permission(doctype, doc=name, throw=True)``; we wrap it
for arg validation only.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import require_doctype_and_name


def get_linked_docs(doctype: str, name: str) -> dict:
	"""Return ``{linked, doctype, name}`` where ``linked`` is the map
	{DocType: [{name, ...}]} of every record linking to ``doctype/name``
	that the current user can read.
	"""
	require_doctype_and_name(doctype, name)
	if not frappe.db.exists(doctype, name):
		raise InvalidArgumentError(f"unknown {doctype}: {name}")

	from frappe.desk.form.linked_with import get as _get_linked

	linked = _get_linked(doctype=doctype, docname=name)
	return {
		"linked": linked or {},
		"doctype": doctype,
		"name": name,
	}
