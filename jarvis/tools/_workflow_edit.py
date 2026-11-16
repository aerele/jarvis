"""The workflow edit lock Desk shows, enforced when a Jarvis tool saves (aerele/jarvis#2506).

A Workflow state's "Only Allow Edit For" role is enforced only by the Desk form
(``frappe.workflow.is_read_only``): a server-side ``doc.save()`` never checks it
(frappe/frappe#46476). Without this, a user who sees a document read-only in Desk
("Pending Approval: only Sales Manager may edit") could change it through Jarvis.

Mirrors ``is_read_only`` exactly, so Jarvis and Desk agree:

- a new (unsaved) document is never locked;
- the state is the document's workflow-state field, else the first workflow state
  whose doc_status matches the document's docstatus (Desk's ``get_default_state``);
- the document is editable when the user holds ANY role a row for that state lists
  in ``allow_edit``; a state no row grants edit to is read-only for everyone.

State moves stay with ``apply_workflow_action`` (Frappe checks transitions and roles
there); this check is about editing a document in its current state.

Two places check it:

- ``update_doc`` (single and batch), before any change is set: the early refusal,
  before a confirmation card is built.
- ``guard_doc_event``, a ``doc_events["*"]`` handler on ``before_validate`` and
  ``before_update_after_submit``, for every other way a Jarvis tool call saves a
  document (``run_method`` -> ``frappe.client.save`` / ``set_value``, a whitelisted
  method, a macro or skill run, a job a tool call queued). Like the write-risk guard
  it judges only a ROOT save inside a Jarvis tool call: Desk, REST and scheduler saves
  are untouched; a save a document's own code makes while another document is saved
  passes, as from Desk; and so does every save under Frappe's ``apply_workflow``, which
  checks the transition and its role itself. The state is read from the STORED
  document. ``db_set`` (a status update, a Close button) is not an edit of the form and
  is not checked; raw ``frappe.db.set_value`` runs no hook (R2-13, aerele/jarvis#1635).
"""

import sys

import frappe
from frappe.model.workflow import get_workflow, get_workflow_name
from frappe.utils import cint

from jarvis.exceptions import PermissionDeniedError


def locked_state(doc, user: str | None = None) -> tuple[str, list[str]] | None:
	"""``(state, edit_roles)`` when ``doc`` sits in a workflow state ``user`` may not
	edit, else ``None``. ``doc`` must hold its STORED values (check before setting
	the changes), so a payload cannot unlock itself by moving the state field."""
	if doc.is_new():
		return None
	if not get_workflow_name(doc.doctype):
		return None
	workflow = get_workflow(doc.doctype)
	states = workflow.get("states") or []
	state = doc.get(workflow.workflow_state_field) or next(
		(s.state for s in states if cint(s.doc_status) == cint(doc.docstatus)), None
	)
	if not state:
		return None
	roles = sorted({s.allow_edit for s in states if s.state == state and s.allow_edit})
	if set(roles) & set(frappe.get_roles(user)):
		return None
	return state, roles


def require_workflow_edit(doc, user: str | None = None) -> None:
	"""Refuse, as Desk does, when ``doc``'s current workflow state does not let the
	user edit it. A no-op for doctypes without an active Workflow."""
	found = locked_state(doc, user)
	if not found:
		return
	state, roles = found
	who = " or ".join(roles) if roles else "no role"
	raise PermissionDeniedError(
		f"{doc.doctype} {doc.name} is in the workflow state '{state}', which only {who} may edit. "
		"Ask someone with that role to make the change, or move it on with apply_workflow_action "
		"if a transition is open to you."
	)


def _in_workflow_action() -> bool:
	"""Whether Frappe's ``apply_workflow`` is on the call stack (the
	``apply_workflow_action`` tool, ``run_method`` on it, an emailed Workflow Action):
	it has checked the transition and its role, and a role allowed to approve need
	not be one allowed to edit."""
	import inspect

	from frappe.model import workflow

	code = getattr(inspect.unwrap(workflow.apply_workflow), "__code__", None)
	frame = sys._getframe(1)
	while frame is not None:
		if frame.f_code is code:
			return True
		frame = frame.f_back
	return False


def guard_doc_event(doc, method=None, *args, **kwargs) -> None:
	"""``doc_events["*"]`` handler on before_validate / before_update_after_submit:
	refuse a Jarvis tool call's root save of a document in a workflow state the user
	may not edit, whatever route reached the save. A no-op outside a tool call."""
	from jarvis.tools._write_risk import _active_state, nested_under

	if _active_state() is None or doc.meta.istable or doc.is_new():
		return
	if not get_workflow_name(doc.doctype):
		return
	if _in_workflow_action() or nested_under(doc) is not None:
		return
	# Judge the stored document, never the incoming values: a save that also moves
	# the state field must not unlock itself.
	stored = doc.get_doc_before_save() or frappe.get_doc(doc.doctype, doc.name)
	require_workflow_edit(stored)
