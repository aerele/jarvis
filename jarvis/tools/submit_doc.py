"""Submit a Frappe document - one, or a whole batch.

``submit`` is qualitatively bigger than ``update`` / ``create`` - it moves
the doc from Draft (docstatus=0) to Submitted (docstatus=1), which triggers
the DocType's ``on_submit`` hooks. For ERPNext that's where the real-world
side effects live:

- Sales/Purchase Invoice → posts to General Ledger
- Stock Entry / Delivery Note → updates stock balances
- Payment Entry → moves money on the books
- Sales Order / Purchase Order → reserves stock, opens fulfilment

Submitted documents are generally **immutable** - changes require
cancellation (which creates reversal entries). The persona must emphasise
that ``submit_doc`` is one of the consequential actions and demand explicit
confirmation.

Two shapes:

- **Single:** ``submit_doc(doctype, name)`` -> the submitted doc as a dict.
- **Batch:** ``submit_doc(doctype, names=[...])`` -> a lean
  ``{"doctype","submitted":[name,...],"count":N}``, every submit in ONE atomic
  savepoint (a single confirmation card; all-or-nothing).

Safety bounds (per doc):

- DocType must be submittable (``meta.is_submittable``).
- A workflow-governed DocType is refused (advance via apply_workflow_action).
- Calling user must have ``submit`` permission on the record.
- Doc must be in Draft state (``docstatus == 0``).
- ``doc.submit()`` re-runs validate() - DocType business rules still apply.
"""

import frappe
from frappe.model.workflow import get_workflow_name

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import require_doctype_and_name
from jarvis.tools._bulk import run_atomic_batch


def _submit_one(doctype: str, name: str) -> "frappe.model.document.Document":
	"""Guards + submit for ONE doc. Returns the submitted Document. Shared by
	the single and batch paths so the guards (submittable, workflow-refusal,
	submit perm, Draft state) never drift."""
	meta = frappe.get_meta(doctype)
	if not meta.is_submittable:
		raise InvalidArgumentError(
			f"{doctype} is not submittable - submit only applies to docstatus-tracked DocTypes"
		)

	# A workflow-governed doctype must advance through its state machine, not a
	# direct submit (which would jump the doc straight to a submitted state,
	# skipping approval steps). Desk hides the Submit button here too.
	if get_workflow_name(doctype):
		raise InvalidArgumentError(
			f"{doctype} is governed by a Workflow; advance it with "
			f"apply_workflow_action (e.g. Approve/Reject) instead of "
			f"submitting directly. Use get_workflow_transitions to see the "
			f"actions available to you."
		)

	if not frappe.has_permission(doctype, ptype="submit", doc=name):
		raise PermissionDeniedError(f"no submit permission on {doctype} '{name}'")

	doc = frappe.get_doc(doctype, name)  # raises DoesNotExistError if missing
	if doc.docstatus == 1:
		raise InvalidArgumentError(f"{doctype} '{name}' is already submitted (docstatus=1)")
	if doc.docstatus == 2:
		raise InvalidArgumentError(
			f"{doctype} '{name}' is cancelled (docstatus=2); cancelled "
			f"docs cannot be re-submitted (amend creates a new draft)"
		)

	doc.submit()  # runs validate() + on_submit hooks; sets docstatus=1
	return doc


def submit_doc(doctype: str, name: str | None = None, names: list | None = None) -> dict:
	"""Submit a Draft document - or a whole batch when ``names`` is given.

	Single: returns the submitted document as a dict (with ``docstatus: 1``).
	Batch: returns ``{"doctype","submitted":[name,...],"count":N}``.

	Raises:
	  - InvalidArgumentError on empty args, non-submittable DocType, a
	    workflow-governed DocType, or a wrong starting docstatus
	  - PermissionDeniedError when the calling user lacks submit on a record
	  - frappe.DoesNotExistError when a record doesn't exist
	  - frappe.ValidationError from a DocType's own validate() hook
	"""
	if names is not None:
		return _submit_batch(doctype, names)

	require_doctype_and_name(doctype, name)
	doc = _submit_one(doctype, name)
	doc.apply_fieldlevel_read_permissions()
	return doc.as_dict()


def _submit_batch(doctype: str, names: list) -> dict:
	"""Submit every name in ``names`` atomically. If any fails, the whole batch
	rolls back and nothing is submitted (all-or-nothing)."""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	def _do(name: str) -> str:
		_submit_one(doctype, name)
		return name

	submitted = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "submitted": submitted, "count": len(submitted)}
