"""Cancel a submitted Frappe document - one, or a whole batch.

Inverse of submit. Moves docstatus 1 → 2 and fires the DocType's
``on_cancel`` hooks. In ERPNext that typically creates **reversal
entries** rather than deleting anything:

- Sales/Purchase Invoice cancel → posts negative GL entries
- Stock Entry cancel → returns the stock movement
- Payment Entry cancel → reverses the money movement
- Sales/Purchase Order cancel → releases reserved stock

A cancelled document keeps its row + history. To "undo a cancel" the
business workflow is **amend**.

Two shapes:

- **Single:** ``cancel_doc(doctype, name)`` -> the cancelled doc as a dict.
- **Batch:** ``cancel_doc(doctype, names=[...])`` -> a lean
  ``{"doctype","cancelled":[name,...],"count":N}``, every cancel in ONE atomic
  savepoint (a single confirmation card; all-or-nothing).

Safety bounds (per doc):

- DocType must be submittable.
- A workflow that models a cancel path is refused (advance via the workflow).
- Calling user must have ``cancel`` permission on the record.
- Doc must be in Submitted state (``docstatus == 1``).
- ``doc.cancel()`` runs ``on_cancel`` hooks, which may themselves fail; those
  propagate unchanged so the agent surfaces the real reason.
"""

import frappe
from frappe.model.workflow import can_cancel_document, get_workflow_name

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import require_doctype_and_name
from jarvis.tools._bulk import run_atomic_batch


def _cancel_one(doctype: str, name: str) -> "frappe.model.document.Document":
	"""Guards + cancel for ONE doc. Returns the cancelled Document. Shared by
	the single and batch paths so the guards never drift."""
	meta = frappe.get_meta(doctype)
	if not meta.is_submittable:
		raise InvalidArgumentError(
			f"{doctype} is not submittable - cancellation only applies to docstatus-tracked DocTypes"
		)

	# When a workflow models a cancel path (a transition into a docstatus=2
	# state), cancelling must go through that action, not a direct cancel.
	# get_workflow_name MUST short-circuit first: can_cancel_document assumes an
	# active workflow exists and errors otherwise. Workflows with no cancel path
	# still allow a plain cancel (Desk shows the Cancel button there too).
	if get_workflow_name(doctype) and not can_cancel_document(doctype):
		raise InvalidArgumentError(
			f"{doctype} is governed by a Workflow that models cancellation as a "
			f"state transition; use apply_workflow_action instead of cancelling "
			f"directly. Use get_workflow_transitions to see the actions "
			f"available to you."
		)

	if not frappe.has_permission(doctype, ptype="cancel", doc=name):
		raise PermissionDeniedError(f"no cancel permission on {doctype} '{name}'")

	doc = frappe.get_doc(doctype, name)
	if doc.docstatus == 0:
		raise InvalidArgumentError(
			f"{doctype} '{name}' is in Draft (docstatus=0); only Submitted "
			f"docs can be cancelled. To discard a Draft, use delete_doc."
		)
	if doc.docstatus == 2:
		raise InvalidArgumentError(f"{doctype} '{name}' is already cancelled (docstatus=2)")

	doc.cancel()  # runs on_cancel hooks; sets docstatus=2
	return doc


def cancel_doc(doctype: str, name: str | None = None, names: list | None = None) -> dict:
	"""Cancel a Submitted document - or a whole batch when ``names`` is given.

	Single: returns the cancelled document as a dict (with ``docstatus: 2``).
	Batch: returns ``{"doctype","cancelled":[name,...],"count":N}``.

	Raises:
	  - InvalidArgumentError on empty args, non-submittable DocType, a
	    workflow-governed DocType that models cancellation, or wrong docstatus
	  - PermissionDeniedError when the calling user lacks cancel
	  - frappe.DoesNotExistError when a record doesn't exist
	  - frappe.ValidationError from a DocType's on_cancel hook
	"""
	if names is not None:
		return _cancel_batch(doctype, names)

	require_doctype_and_name(doctype, name)
	doc = _cancel_one(doctype, name)
	doc.apply_fieldlevel_read_permissions()
	return doc.as_dict()


def _cancel_batch(doctype: str, names: list) -> dict:
	"""Cancel every name atomically. If any fails, the whole batch rolls back
	and nothing is cancelled (all-or-nothing)."""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	def _do(name: str) -> str:
		_cancel_one(doctype, name)
		return name

	cancelled = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "cancelled": cancelled, "count": len(cancelled)}
