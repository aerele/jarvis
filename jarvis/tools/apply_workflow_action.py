"""Apply a Workflow action (Approve / Reject / ...) to a document - one, or a
whole batch under one action.

The WRITE half of workflow support (read half: ``get_workflow_transitions``).
Frappe-workflow-governed DocTypes advance by applying a named *action*, which
the engine validates (is it a legal transition from the current state for this
user's roles?), checks self-approval on, then commits by internally
save()/submit()/cancel()-ing the doc per the target state's docstatus - firing
the usual on_submit / on_cancel side effects.

Consequential: a transition can post to the GL, move stock, or cancel a doc.
It is confirmation-gated in api.py.

Two shapes:

- **Single:** ``apply_workflow_action(doctype, name, action)`` -> the updated
  doc as a dict (or ``{"queued": True}`` when enqueued).
- **Batch:** ``apply_workflow_action(doctype, action=..., names=[...])`` -> a
  lean ``{"doctype","action","results":[{"name","queued"}],"count":N}``, one
  action applied to every name in ONE atomic savepoint (a single confirmation
  card; e.g. "approve these 20 leave applications"). Each transition is still
  validated per doc (role + self-approval) inside the loop.
"""

import frappe
from frappe.model.workflow import apply_workflow, get_workflow_name

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import require_doctype_and_name
from jarvis.tools._bulk import run_atomic_batch


def apply_workflow_action(
	doctype: str,
	name: str | None = None,
	action: str | None = None,
	names: list | None = None,
) -> dict:
	"""Apply ``action`` to one doc's workflow - or to a whole batch when
	``names`` is given.

	Raises:
	  - InvalidArgumentError on empty args, a DocType with no active workflow,
	    or an action the workflow does not offer from the current state
	  - frappe.DoesNotExistError when a record doesn't exist
	  - frappe.ValidationError for a self-approval violation, a failed
	    transition condition, or a DocType's own validate()/on_submit rules

	When a DocType submits in the background (``queue_in_background``), that
	doc's transition is enqueued rather than committed inline; it reports
	``queued: True`` (no doc dict, because nothing is committed yet).
	"""
	if names is not None:
		return _apply_batch(doctype, action, names)

	require_doctype_and_name(doctype, name)
	if not action or not str(action).strip():
		raise InvalidArgumentError("action is required")
	action = str(action).strip()

	if not get_workflow_name(doctype):
		raise InvalidArgumentError(
			f"{doctype} is not governed by an active Workflow; use submit_doc / "
			f"update_doc / cancel_doc instead of a workflow action."
		)

	doc = frappe.get_doc(doctype, name)  # raises DoesNotExistError if missing
	result = apply_workflow(doc, action)  # validates transition + role + self-approval
	if result is None:
		return {"doctype": doctype, "name": name, "action": action, "queued": True}

	result.apply_fieldlevel_read_permissions()
	return result.as_dict()


def _apply_batch(doctype: str, action: str, names: list) -> dict:
	"""Apply one ``action`` to every name atomically. If any transition fails
	(illegal state, role/self-approval, condition), the whole batch rolls back."""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not action or not str(action).strip():
		raise InvalidArgumentError("action is required")
	action = str(action).strip()
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")
	if not get_workflow_name(doctype):
		raise InvalidArgumentError(
			f"{doctype} is not governed by an active Workflow; use submit_doc / "
			f"update_doc / cancel_doc instead of a workflow action."
		)
	# A queue_in_background transition takes a FILESYSTEM document lock and
	# defers the submit to a post-commit RQ job. In an atomic batch that job's
	# enqueue is dropped on rollback but the lock file is not - a later item's
	# failure would leave earlier docs frozen (DocumentLockedError) for hours.
	# Refuse bulk for such DocTypes; the single tool handles them fine.
	if frappe.get_meta(doctype).queue_in_background:
		raise InvalidArgumentError(
			f"{doctype} submits in the background (queue_in_background); apply "
			f"the workflow action to these documents one at a time, not as a batch."
		)

	def _do(name: str) -> dict:
		doc = frappe.get_doc(doctype, name)  # raises DoesNotExistError if missing
		result = apply_workflow(doc, action)  # validates transition + role + self-approval
		return {"name": name, "queued": result is None}

	results = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "action": action, "results": results, "count": len(results)}
