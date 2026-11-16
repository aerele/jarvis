"""A Workflow holds for Jarvis users as it does in Desk (aerele/jarvis#2506).

Frappe enforces a Workflow mostly in the Desk form. On the server a ``doc.save()``
never checks a state's "Only Allow Edit For" role (``frappe.workflow.is_read_only``
lives in the browser, frappe/frappe#46476), ``frappe.client.submit`` of a workflow
document skips the approval (``Document.validate_workflow`` just sets the first
submitted state), and a save that moves the state field checks the transition's role
but not self-approval, the transition's tasks or the next state's ``update_field``.
Desk lets a user do none of that: the form is read-only, Submit and Cancel give way
to the workflow's actions, and the state field is read-only.

So, while a Jarvis tool call runs, every ROOT write of a document of a doctype with
an active Workflow is judged on the STORED document (a payload cannot unlock itself
by moving the state field):

- **edit** (a save, an edit after submit): refused when the stored state does not
  let the user edit (``locked_state``, which mirrors ``is_read_only`` exactly: a new
  document is never locked; the state is the state field, else the first state of
  the document's docstatus; editable with ANY ``allow_edit`` role of that state; a
  state no row grants edit to is read-only for everyone);
- **state move**: a write that changes the state the document is in other than
  through a transition (a save, an edit after submit, a ``db_set``) is refused;
- **submit**: refused, also for a document inserted already submitted; a workflow
  document is submitted by a transition;
- **cancel**: refused when the workflow models cancelling (a transition into a
  cancelled state), as Desk and the ``cancel_doc`` tool do; allowed otherwise, as
  Desk allows it (``frappe.model.workflow.can_cancel_document``);
- **a child row** saved, added or deleted on its own edits its parent: refused when
  the parent (read from the stored row) is in a state the user may not edit.

What passes:

- every write under a workflow transition (``run_workflow_transition``, and
  ``apply_workflow`` on a Frappe without ``run_workflow_transition``): the
  ``apply_workflow_action`` tool, ``run_method`` on ``apply_workflow`` or
  ``bulk_workflow_approval``, the emailed Workflow Action's ``confirm_action``.
  Frappe reloads the document from the database there and checks the transition,
  its role, its condition and self-approval; a role allowed to approve need not be
  one allowed to edit. Frames are matched by code object, so any caller (including
  one a later Frappe adds) that goes through the transition is covered;
- the Submission Queue job finishing the queued submit / cancel / edit of that very
  document: making the queue entry inside a tool call is itself judged (as above)
  unless a transition makes it, so the job only finishes a judged action;
- a write another document's own save or delete makes (``_write_risk.nested_under``),
  as from Desk;
- everything outside a Jarvis tool call (Desk, REST, scheduler, a job no tool call
  queued).

Where: ``update_doc`` refuses an edit or a state move before any change is set (the
early refusal, before a card is built); ``guard_doc_event`` is a ``doc_events["*"]``
handler on ``before_validate`` (save, submit, a new Submission Queue entry, a row),
``before_update_after_submit``, ``before_cancel``, ``before_change`` (``db_set``) and
``on_trash`` (a row only),
plus ``on_update_after_submit`` and ``on_cancel``: Frappe skips the ``before_*``
event of an edit after submit and of a cancel under ``flags.ignore_validate``, and a
refusal there still rolls the tool call back. A refusal is logged
(``jarvis.risky_write risk=workflow``) and recorded for ``_write_risk.take_refusal``,
so a method that swallows it cannot report success.

Not covered, like the write-risk guard (R2-13, aerele/jarvis#1635):
``frappe.db.set_value`` and raw SQL, which run no hook. A ``db_set`` of a field other
than the state field (a status button, Close) is not an edit of the form, as in
Desk. Deleting is a permission question (and, without a card, the write brake's),
not a workflow one, as in Desk.
"""

import inspect
import sys

import frappe
from frappe.model.workflow import get_workflow, get_workflow_name
from frappe.utils import cint

from jarvis.exceptions import PermissionDeniedError

_QUEUE = "Submission Queue"
_USE = "Use apply_workflow_action (get_workflow_transitions lists the actions open to the user)."


def _state_of(doctype: str, values) -> str:
	"""The workflow state of ``values`` (a document or a dict), as Desk reads it: the
	state field, else the first state whose doc_status matches the docstatus."""
	workflow = get_workflow(doctype)
	return str(
		values.get(workflow.workflow_state_field)
		or next(
			(
				s.state
				for s in workflow.get("states") or []
				if cint(s.doc_status) == cint(values.get("docstatus"))
			),
			"",
		)
		or ""
	)


def locked_state(doc, user: str | None = None) -> tuple[str, list[str]] | None:
	"""``(state, edit_roles)`` when ``doc`` sits in a workflow state ``user`` may not
	edit, else ``None``. ``doc`` must hold its STORED values (check before setting
	the changes), so a payload cannot unlock itself by moving the state field."""
	if doc.is_new() or not get_workflow_name(doc.doctype):
		return None
	state = _state_of(doc.doctype, doc)
	if not state:
		return None
	states = get_workflow(doc.doctype).get("states") or []
	roles = sorted({s.allow_edit for s in states if s.state == state and s.allow_edit})
	if set(roles) & set(frappe.get_roles(user)):
		return None
	return state, roles


def _locked_error(doc, state: str, roles: list[str]) -> PermissionDeniedError:
	who = f"only {' or '.join(roles)} may edit" if roles else "nobody may edit"
	return PermissionDeniedError(
		f"{doc.doctype} {doc.name} is in the workflow state '{state}', which {who}. Ask someone "
		"with that role to make the change, or move it on with one of the workflow's actions if "
		f"one is open to you. {_USE}"
	)


def _move_error(doc, before: str, after: str) -> PermissionDeniedError:
	return PermissionDeniedError(
		f"{doc.doctype} {doc.name} is in the workflow state '{before}'. A workflow state changes "
		f"only through one of the workflow's actions, so it was not set to '{after}'. {_USE}"
	)


def require_workflow_edit(doc, user: str | None = None) -> None:
	"""Refuse, as Desk does, when ``doc``'s current workflow state does not let the
	user edit it. A no-op for doctypes without an active Workflow."""
	found = locked_state(doc, user)
	if found:
		raise _locked_error(doc, *found)


def require_no_state_move(stored, changes: dict) -> None:
	"""Refuse ``changes`` that would move ``stored`` (the document as saved) into
	another workflow state: a state moves only through a transition."""
	if stored.is_new() or not get_workflow_name(stored.doctype):
		return
	field = get_workflow(stored.doctype).workflow_state_field
	if field not in (changes or {}):
		return
	before = _state_of(stored.doctype, stored)
	after = _state_of(stored.doctype, {**stored.as_dict(), field: changes.get(field)})
	if before != after:
		raise _move_error(stored, before, after)


# --------------------------------------------------------------------------- #
# Frames that mean "a workflow transition is running"
# --------------------------------------------------------------------------- #
_CODES = None


def _code(fn):
	return getattr(inspect.unwrap(fn), "__code__", None) if fn is not None else None


def _frame_codes():
	"""(transition code objects, the Submission Queue job's code object). Frappe 15
	and 16 since frappe/frappe#45939 run every transition through
	``run_workflow_transition`` (``apply_workflow`` wraps it; bulk approval and the
	emailed action call it directly); older releases do the work in
	``apply_workflow``."""
	global _CODES
	if _CODES is None:
		from frappe.model import workflow

		transition = frozenset(
			{_code(getattr(workflow, "run_workflow_transition", None)), _code(workflow.apply_workflow)}
			- {None}
		)
		try:
			from frappe.core.doctype.submission_queue.submission_queue import SubmissionQueue

			queued = _code(SubmissionQueue.background_submission)
		except Exception:
			queued = None
		_CODES = (transition, queued)
	return _CODES


def _same(a, b) -> bool:
	try:
		return a is b or (a.doctype == b.doctype and a.name is not None and str(a.name) == str(b.name))
	except Exception:
		return False


def _exempt(doc) -> bool:
	"""A workflow transition is running, or a Submission Queue job is finishing the
	queued action of this very document (its entry was judged when it was made)."""
	transition, queued = _frame_codes()
	frame = sys._getframe(1)
	while frame is not None:
		code = frame.f_code
		if code in transition:
			return True
		if queued is not None and code is queued:
			entry = frame.f_locals.get("self")
			target = frame.f_locals.get("to_be_queued_doc")
			if (
				entry is not None
				and target is not None
				and _same(target, doc)
				and (entry.get("ref_doctype"), str(entry.get("ref_docname"))) == (doc.doctype, str(doc.name))
			):
				return True
		frame = frame.f_back
	return False


# --------------------------------------------------------------------------- #
# The judgement and the doc_events handler
# --------------------------------------------------------------------------- #
_EDIT_EVENTS = frozenset({"before_validate", "before_update_after_submit", "on_update_after_submit"})
_CANCEL_EVENTS = frozenset({"before_cancel", "on_cancel"})


def judge(stored, action: str, new) -> tuple[PermissionDeniedError, str] | None:
	"""``(refusal, why)`` for ``action`` (save / submit / update_after_submit /
	cancel / db_set) of the document ``stored`` (its values in the database) with the
	incoming values ``new``, else ``None``."""
	dt = stored.doctype
	if action == "submit":
		return (
			PermissionDeniedError(
				f"{dt} {stored.name} follows a workflow, so it is submitted by one of the workflow's "
				f"actions (for example Approve), not directly. {_USE}"
			),
			"submit",
		)
	if action == "cancel":
		from frappe.model.workflow import can_cancel_document

		if can_cancel_document(dt):
			return None
		return (
			PermissionDeniedError(
				f"{dt} {stored.name} follows a workflow with its own cancel action, so it is "
				f"cancelled through that action, not directly. {_USE}"
			),
			"cancel",
		)
	if action in ("save", "update_after_submit"):
		found = locked_state(stored)
		if found:
			return _locked_error(stored, *found), "locked"
	before, after = _state_of(dt, stored), _state_of(dt, new)
	if before != after:
		return _move_error(stored, before, after), "state_move"
	return None


def _refuse(e: PermissionDeniedError, doc, event: str, why: str) -> None:
	from jarvis.tools._write_risk import _LOCAL_REFUSAL, log_line

	log_line("workflow", doc.doctype, doc.name, "refused", event=event, why=why)
	setattr(frappe.local, _LOCAL_REFUSAL, e)
	raise e


def _stored(doc):
	"""``doc`` as it is in the database: Frappe's before-save copy when it has one,
	else a fresh read; never the incoming values."""
	before = doc.get_doc_before_save()
	return before if before is not None else frappe.get_doc(doc.doctype, doc.name)


def _guard_queue_entry(entry, event: str) -> None:
	"""A Submission Queue entry made in a tool call queues a submit, cancel or edit
	of a workflow document. Its job is let through, so it is judged now."""
	if event != "before_validate" or not entry.is_new():
		return
	dt, name = entry.get("ref_doctype"), entry.get("ref_docname")
	if not dt or not name or not get_workflow_name(dt) or _exempt(entry):
		return
	stored = frappe.get_doc(dt, name)
	target = getattr(entry, "to_be_queued_doc", None)
	action = str(entry.get("action_for_queuing") or "").lower()
	action = {"update": "update_after_submit"}.get(action, action)
	found = judge(stored, action, target if target is not None else stored)
	if found:
		_refuse(found[0], stored, event, found[1])


def _guard_child_row(row, event: str) -> None:
	"""A row saved, added or deleted on its own (``frappe.client.save`` /
	``frappe.client.delete`` of a row, ``row.insert()``) edits its parent: refused when
	the parent sits in a workflow state the user may not edit. The parent is read from
	the STORED row (a payload may name any parent); a new row has only its own."""
	if _exempt(row) or _nested(row):
		return
	where = None
	if not row.is_new():
		where = frappe.db.get_value(row.doctype, row.name, ["parenttype", "parent"], as_dict=True)
	where = where or {"parenttype": row.get("parenttype"), "parent": row.get("parent")}
	dt, name = where.get("parenttype"), where.get("parent")
	if not dt or not name or not get_workflow_name(dt) or not frappe.db.exists(dt, name):
		return
	parent = frappe.get_doc(dt, name)
	found = locked_state(parent)
	if found:
		_refuse(_locked_error(parent, *found), row, event, "locked_row")


def _nested(doc) -> bool:
	from jarvis.tools._write_risk import nested_under

	return nested_under(doc) is not None


_ROW_EVENTS = _EDIT_EVENTS | {"on_trash"}


def guard_doc_event(doc, method=None, *args, **kwargs) -> None:
	"""``doc_events["*"]`` handler (events in the module docstring): refuse a Jarvis
	tool call's root write that the document's Workflow does not allow. A no-op
	outside a tool call and for doctypes without an active Workflow."""
	from jarvis.tools._write_risk import _active_state

	if _active_state() is None:
		return
	event = method or ""
	if doc.doctype == _QUEUE:
		_guard_queue_entry(doc, event)
		return
	if doc.meta.istable:
		if event in _ROW_EVENTS:
			_guard_child_row(doc, event)
		return
	if doc.meta.issingle or not get_workflow_name(doc.doctype):
		return
	if event == "before_change":
		if doc.is_new() or (doc.doctype, doc.name) in (frappe.flags.currently_saving or []):
			return  # a db_set from this document's own save, which was judged
		action = "db_set"
	elif event in _CANCEL_EVENTS:
		action = "cancel"
	elif event in _EDIT_EVENTS:
		action = doc.get("_action") or "save"
		if action not in ("save", "submit", "update_after_submit"):
			return
	else:
		return
	if doc.is_new() and action != "submit":
		return  # a new draft: Frappe lets it start only in the first state
	if _exempt(doc) or _nested(doc):
		return
	if doc.is_new():
		# Inserted already submitted: Frappe would stamp the first submitted state
		# and skip the approval.
		found = judge(doc, "submit", doc)
	elif action == "db_set":
		# Only a move of the state is judged; the stored values are read from the
		# database, as db_set may run on a document whose before-save copy is stale.
		field = get_workflow(doc.doctype).workflow_state_field
		row = frappe.db.get_value(doc.doctype, doc.name, [field, "docstatus"], as_dict=True) or {}
		stored = frappe._dict(row, doctype=doc.doctype, name=doc.name)
		found = judge(stored, "db_set", doc)
	else:
		found = judge(_stored(doc), action, doc)
	if found:
		_refuse(found[0], doc, event, found[1])
