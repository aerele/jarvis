"""Submittable documents linked to a source, walking the cancel-tree.

Wraps ``frappe.desk.form.linked_with.get_submitted_linked_docs``.
Returns every submittable record in the cancel-impact tree rooted at
the source - the same set Frappe's "Cancel" dialog confirms before
processing.

The agent uses this for "if I cancel this Sales Order what else gets
cancelled?" / "what's the full impact tree?" questions. Without this
the LLM would either undercount (only direct children) or miss the
submittable filter entirely.

Underlying helper enforces read permission on the source.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import require_doctype_and_name


def get_submitted_linked_docs(doctype: str, name: str) -> dict:
	"""Return ``{linked, doctype, name}`` where ``linked`` is a list of
	``{doctype, name, docstatus}`` for every submittable record in the
	cancel-impact tree rooted at ``doctype/name``.
	"""
	require_doctype_and_name(doctype, name)
	if not frappe.db.exists(doctype, name):
		raise InvalidArgumentError(f"unknown {doctype}: {name}")

	from frappe.desk.form.linked_with import (
		get_submitted_linked_docs as _gsl,
	)

	result = _gsl(doctype=doctype, name=name)
	# The underlying helper returns either a dict {"docs": [...]} or a
	# raw list depending on the Frappe release; normalise to list.
	if isinstance(result, dict):
		linked = result.get("docs") or []
	else:
		linked = result or []
	return {
		"linked": linked,
		"doctype": doctype,
		"name": name,
	}
