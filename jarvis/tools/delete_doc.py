"""Delete a Frappe document - one, or a whole batch.

The **most destructive** mutating tool. Unlike cancel - which keeps the
row and adds a reversal entry - delete removes the row outright. Some
DocTypes retain an audit trail via the ``Version`` DocType; many don't.
Treat every delete as irreversible from the user's perspective.

Two shapes:

- **Single:** ``delete_doc(doctype, name)`` -> ``{"deleted":True,"doctype","name"}``.
- **Batch:** ``delete_doc(doctype, names=[...])`` -> a lean
  ``{"doctype","deleted":[name,...],"count":N}``, every delete in ONE atomic
  savepoint (a single confirmation card; all-or-nothing - if one row is
  referenced/blocked, NONE are deleted).

Safety bounds (per doc):

- Calling user must have ``delete`` permission on the record.
- For submittable DocTypes: only Draft (0) or Cancelled (2) docs are
  deletable. Submitted (1) is refused - cancel first.
- Some DocTypes block delete entirely via ``allow_delete = 0``; Frappe's
  ``delete_doc`` raises and the error propagates unchanged.
- Linked records: if other docs reference this one, Frappe raises
  ``LinkExistsError``; it propagates so the agent can tell the user which
  record blocks the delete.
"""

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import require_doctype_and_name
from jarvis.tools._bulk import run_atomic_batch


def _delete_one(doctype: str, name: str) -> str:
	"""Guards + delete for ONE doc. Returns the deleted name. Shared by the
	single and batch paths so the guards never drift."""
	if not frappe.has_permission(doctype, ptype="delete", doc=name):
		raise PermissionDeniedError(f"no delete permission on {doctype} '{name}'")

	# Pre-load the doc to check docstatus - gives a clearer error than
	# Frappe's "cannot delete a submitted document".
	doc = frappe.get_doc(doctype, name)  # raises DoesNotExistError if missing
	if getattr(doc, "docstatus", 0) == 1:
		raise InvalidArgumentError(
			f"{doctype} '{name}' is Submitted (docstatus=1); cancel it first (cancel_doc) before deleting"
		)

	frappe.delete_doc(doctype, name)  # raises LinkExistsError if referenced
	return name


def delete_doc(doctype: str, name: str | None = None, names: list | None = None) -> dict:
	"""Delete a Draft/Cancelled/non-submittable document - or a whole batch
	when ``names`` is given.

	Single: returns ``{"deleted":True,"doctype","name"}``.
	Batch: returns ``{"doctype","deleted":[name,...],"count":N}``.

	Raises:
	  - InvalidArgumentError on empty args or attempting to delete a Submitted doc
	  - PermissionDeniedError when the calling user lacks delete
	  - frappe.DoesNotExistError when a record doesn't exist
	  - frappe.LinkExistsError when another record references one (whole batch
	    rolls back - nothing is deleted)
	"""
	if names is not None:
		return _delete_batch(doctype, names)

	require_doctype_and_name(doctype, name)
	_delete_one(doctype, name)
	return {"deleted": True, "doctype": doctype, "name": name}


def _delete_batch(doctype: str, names: list) -> dict:
	"""Delete every name atomically. A LinkExists/permission failure on ANY row
	rolls the whole batch back - nothing is deleted - so a partial destructive
	run is impossible (unlike Frappe's desk bulk-delete which commits per doc)."""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	deleted = run_atomic_batch(names, lambda n: _delete_one(doctype, n), label=lambda n: n)
	return {"doctype": doctype, "deleted": deleted, "count": len(deleted)}
