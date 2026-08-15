"""Amend a cancelled Frappe document - the lifecycle's only true "undo".

Amendment is Frappe's mechanism for editing a Submitted record AFTER it
has been Cancelled. It does NOT modify the Cancelled row; it creates a
fresh **Draft** copy with the same data + an ``amended_from`` link back
to the original. The user then edits the new Draft and re-submits it.

So the lifecycle closes:

    Draft (0) → Submitted (1) → Cancelled (2) → Amend → Draft (0) → ...

The Cancelled doc stays in the database as audit history; the new Draft
gets a derived name (Frappe's autoname appends ``-1``, ``-2``, etc.).

Safety bounds:

- DocType must be submittable.
- Calling user must have **amend** permission on the source record
  (``frappe.has_permission(doctype, ptype="amend", doc=name)``). Plus
  the implicit ``create`` perm on the DocType - Frappe's
  ``copy_doc().insert()`` checks that for us.
- Source doc must be Cancelled (``docstatus == 2``). Draft and Submitted
  refuse with state-aware messages.
- Frappe's autoname appends a suffix to derive the new name. The new
  draft passes ``validate()`` on insert, so any business-rule violations
  that lived in the source carry forward and need to be fixed in the
  copy before the user can re-submit.

Returns the **new Draft's dict** (not the original). The agent should
guide the user to edit/submit the new doc.
"""

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import require_doctype_and_name
from jarvis.tools._bulk import run_atomic_batch


def amend_doc(doctype: str, name: str | None = None, names: list | None = None) -> dict:
	"""Create a new Draft from a Cancelled document - or a whole batch.

	Single: returns the inserted Draft as a dict, including its new ``name``
	and the ``amended_from`` link back to the source.
	Batch (``names``): returns ``{"doctype","amended":[{source, name}],"count"}``,
	each amend in ONE atomic savepoint. Raises:
	  - InvalidArgumentError on empty args, non-submittable DocType, or wrong
	    source state (must be Cancelled)
	  - PermissionDeniedError when the calling user lacks amend on a record
	  - frappe.DoesNotExistError when a source record doesn't exist
	  - frappe.ValidationError if copied data fails a DocType's validate()
	"""
	if names is not None:
		return _amend_batch(doctype, names)

	require_doctype_and_name(doctype, name)
	new_doc = _amend_one(doctype, name)
	new_doc.apply_fieldlevel_read_permissions()
	return new_doc.as_dict()


def _amend_one(doctype: str, name: str) -> "frappe.model.document.Document":
	"""Guards + amend for ONE doc. Returns the new Draft Document. Shared by
	the single and batch paths so the guards never drift."""
	meta = frappe.get_meta(doctype)
	if not meta.is_submittable:
		raise InvalidArgumentError(
			f"{doctype} is not submittable - amendment only applies to docstatus-tracked DocTypes"
		)

	if not frappe.has_permission(doctype, ptype="amend", doc=name):
		raise PermissionDeniedError(f"no amend permission on {doctype} '{name}'")

	source = frappe.get_doc(doctype, name)
	if source.docstatus == 0:
		raise InvalidArgumentError(
			f"{doctype} '{name}' is in Draft (docstatus=0); amend is for "
			f"editing CANCELLED docs. Edit the Draft directly with update_doc."
		)
	if source.docstatus == 1:
		raise InvalidArgumentError(
			f"{doctype} '{name}' is Submitted (docstatus=1); amend is for "
			f"CANCELLED docs. To amend, cancel first via cancel_doc."
		)

	# frappe.copy_doc creates a deep copy with cleared name; we set
	# amended_from and reset docstatus to Draft, then insert.
	new_doc = frappe.copy_doc(source)
	new_doc.amended_from = source.name
	new_doc.docstatus = 0
	new_doc.insert()  # runs validate(); autoname appends suffix
	return new_doc


def _amend_batch(doctype: str, names: list) -> dict:
	"""Amend every Cancelled name atomically. If any fails, the whole batch
	rolls back and no new drafts are created."""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	def _do(name: str) -> dict:
		new_doc = _amend_one(doctype, name)
		return {"source": name, "name": new_doc.name}

	amended = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "amended": amended, "count": len(amended)}
