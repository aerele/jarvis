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

``run_method`` also judges, BEFORE it dispatches, every call that cancels, discards,
renames or carries a document payload (``check_run_method``): a doctype's own
``before_cancel`` hooks (an e-invoice or e-way bill cancel) run before the handler
above, and a cancel through a save skips Frappe's checks on the other fields, so a
cancel may not carry any other change.

Not covered, like the write-risk guard (R2-13, aerele/jarvis#1635):
``frappe.db.set_value``, ``doc.db_update()`` and raw SQL in a whitelisted method, which
run no hook. A ``db_set`` of a field other than the state field (a status button,
Close) is not an edit of the form, as in Desk. Deleting is a permission question (and,
without a card, the write brake's), not a workflow one, as in Desk.

Stricter than Desk, on purpose: a document's own method that submits ANOTHER workflow
document as a root write (HRMS Payroll Entry submitting Salary Slips that have their
own workflow) is refused from Jarvis; the user runs that button in Desk.
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
			return _cancel_move(stored, new)
		return (
			PermissionDeniedError(
				f"{dt} {stored.name} follows a workflow with its own cancel action, so it is "
				f"cancelled through that action, not directly. {_USE}"
			),
			"cancel",
		)
	if action in ("save", "update_after_submit", "discard", "rename"):
		found = locked_state(stored)
		if found:
			return _locked_error(stored, *found), "locked"
	if action in ("discard", "rename"):
		return None
	before, after = _state_of(dt, stored), _state_of(dt, new)
	if before != after:
		return _move_error(stored, before, after), "state_move"
	return None


def _cancel_move(stored, new) -> tuple[PermissionDeniedError, str] | None:
	"""A cancel keeps the state field, or moves it into a cancelled state (doc_status
	2), as a cancel step would; Frappe runs no validate_workflow on a cancel, so any
	other value would stick (``frappe.desk.form.save.cancel`` takes the state from its
	caller)."""
	workflow = get_workflow(stored.doctype)
	field = workflow.workflow_state_field
	before, after = str(stored.get(field) or ""), str(new.get(field) or "")
	cancelled = {s.state for s in workflow.get("states") or [] if cint(s.doc_status) == 2}
	if after == before or after in cancelled:
		return None
	return _move_error(stored, before or _state_of(stored.doctype, stored), after), "state_move"


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


def _in_own_save(doc) -> bool:
	"""Whether ``doc``'s own save or insert is on the call stack (a db_set from its
	on_update / on_submit). Read from the frames: ``frappe.flags.currently_saving``
	keeps an entry when a save fails, so it can be stale."""
	from jarvis.tools._write_risk import _frame_codes as write_frames

	save_codes = write_frames()[0]
	frame = sys._getframe(1)
	while frame is not None:
		if frame.f_code in save_codes and _same(frame.f_locals.get("self"), doc):
			return True
		frame = frame.f_back
	return False


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
		if doc.is_new() or doc.get("_action") == "discard" or _in_own_save(doc):
			return  # this document's own save (judged), or a discard (before_discard)
		action = "db_set"
	elif event in _CANCEL_EVENTS:
		action = "cancel"
	elif event == "before_discard":
		action = "discard"
	elif event == "before_rename":
		action = "rename"
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
	elif action in ("discard", "rename"):
		found = judge(frappe.get_doc(doc.doctype, doc.name), action, doc)
	else:
		found = judge(_stored(doc), action, doc)
	if found:
		_refuse(found[0], doc, event, found[1])


# --------------------------------------------------------------------------- #
# run_method: judged BEFORE the call
# --------------------------------------------------------------------------- #
# A cancel runs the doctype's own before_cancel hooks (an e-invoice or e-way bill
# cancel at the government portal) before the "*" handler above, so run_method judges
# every route that cancels, discards, renames or carries a document payload before it
# dispatches. The handler stays as the net for everything else.
_SYSTEM_KEYS = frozenset(
	{"doctype", "name", "docstatus", "modified", "modified_by", "creation", "owner", "idx"}
)


def _loads(value):
	if isinstance(value, str):
		try:
			return frappe.parse_json(value)
		except Exception:
			return None
	return value


def _same_value(df, a, b) -> bool:
	from frappe.model import numeric_fieldtypes
	from frappe.utils import cstr, flt, get_datetime

	if df.fieldtype in numeric_fieldtypes:
		return flt(a) == flt(b)
	if df.fieldtype in ("Date", "Datetime", "Time") and (a or b):
		try:
			return get_datetime(a) == get_datetime(b)
		except Exception:
			return cstr(a) == cstr(b)
	return cstr(a) == cstr(b)


def payload_changes(stored, payload: dict) -> list[str]:
	"""The fields (and table rows) ``payload`` would change on ``stored``."""
	from frappe.model import table_fields

	out = []
	for key, value in payload.items():
		if key in _SYSTEM_KEYS or str(key).startswith("_"):
			continue
		df = stored.meta.get_field(key)
		if df is None:
			continue
		if df.fieldtype in table_fields:
			rows = {r.name: r for r in stored.get(key) or []}
			given = value if isinstance(value, list) else []
			if len(given) != len(rows):
				out.append(key)
				continue
			for row in given:
				row = row if isinstance(row, dict) else {}
				old = rows.get(row.get("name"))
				if old is None or payload_changes(old, row):
					out.append(key)
					break
		elif not _same_value(df, stored.get(key), value):
			out.append(key)
	return out


def _deny(e: PermissionDeniedError, doctype, name, why: str) -> None:
	from jarvis.tools._write_risk import log_line

	log_line("workflow", doctype, name, "refused", event="run_method", why=why)
	raise e


def _stored_doc(doctype, name):
	if not isinstance(doctype, str) or not doctype or name in (None, ""):
		return None
	if not get_workflow_name(doctype) or not frappe.db.exists(doctype, name):
		return None
	return frappe.get_doc(doctype, name)


def _judge_stored(doctype, name, action: str, new: dict | None = None) -> None:
	stored = _stored_doc(doctype, name)
	if stored is None:
		return
	found = judge(stored, action, frappe._dict({**stored.as_dict(), **(new or {})}))
	if found:
		_deny(found[0], doctype, name, found[1])


def _judge_payload(doc, method: str | None = None) -> None:
	"""A document payload (``frappe.client.save``, ``run_doc_method`` docs, a
	bulk_update row): refused when it cancels with any other change, or changes a
	document whose state the user may not edit, or moves its state."""
	doc = _loads(doc)
	if not isinstance(doc, dict):
		return
	stored = _stored_doc(doc.get("doctype"), doc.get("name"))
	if stored is None:
		return
	changes = payload_changes(stored, doc)
	dt, name = stored.doctype, stored.name
	cancels = method == "cancel" or (method is None and cint(doc.get("docstatus")) == 2)
	if cancels or method in ("submit", "discard"):
		_judge_stored(dt, name, method or "cancel", doc)
		if changes:
			_deny(
				PermissionDeniedError(
					f"{dt} {name} follows a workflow; a {method or 'cancel'} cannot also change "
					f"{', '.join(changes)}. Make the change first (if its state allows it), then "
					f"{method or 'cancel'} it on its own."
				),
				dt,
				name,
				"payload",
			)
		return
	if method is None and cint(doc.get("docstatus")) == 1 and stored.docstatus == 0:
		_judge_stored(dt, name, "submit", doc)
	if changes:
		_judge_stored(dt, name, "save" if stored.docstatus == 0 else "update_after_submit", doc)


def check_run_method(paths, args: dict | None, doctype=None, name=None, method=None) -> None:
	"""Refuse a ``run_method`` call the workflow of its document would refuse, before
	anything runs. ``paths``: the dotted paths the call goes by; or ``doctype`` /
	``name`` / ``method`` for a controller method."""
	args = args if isinstance(args, dict) else {}
	if doctype is not None:
		if method in ("cancel", "submit", "discard", "rename"):
			_judge_stored(doctype, name, method)
		return
	paths = set(paths or ())
	lower = str(args.get("action") or "").strip().lower()
	if paths & {"frappe.client.cancel"}:
		_judge_stored(args.get("doctype"), args.get("name"), "cancel")
	if paths & {"frappe.desk.form.save.cancel"}:
		field = args.get("workflow_state_fieldname")
		new = {field: args.get("workflow_state")} if field and args.get("workflow_state") else None
		_judge_stored(args.get("doctype"), args.get("name"), "cancel", new)
	if paths & {"frappe.desk.form.save.discard"}:
		_judge_stored(args.get("doctype"), args.get("name"), "discard")
	if paths & {"frappe.client.rename_doc", "frappe.rename_doc", "frappe.model.rename_doc.rename_doc"}:
		_judge_stored(args.get("doctype"), args.get("old") or args.get("old_name"), "rename")
	if paths & {"frappe.model.rename_doc.update_document_title"}:
		_judge_stored(args.get("doctype"), args.get("docname"), "rename")
	if paths & {"frappe.desk.form.save.savedocs"}:
		doc = _loads(args.get("doc")) or {}
		if lower == "cancel":
			_judge_stored(doc.get("doctype"), doc.get("name"), "cancel")
		elif lower == "submit":
			_judge_stored(doc.get("doctype"), doc.get("name"), "submit")
		else:
			_judge_payload(doc)
	if paths & {"frappe.client.save", "frappe.client.submit"}:
		_judge_payload(args.get("doc"), "submit" if "frappe.client.submit" in paths else None)
	if paths & {"frappe.client.bulk_update"}:
		for doc in _loads(args.get("docs")) or []:
			_judge_payload(doc)
	if paths & {"frappe.client.set_value"}:
		values = _loads(args.get("fieldname"))
		values = values if isinstance(values, dict) else {args.get("fieldname"): args.get("value")}
		_judge_payload({"doctype": args.get("doctype"), "name": args.get("name"), **values})
	if paths & {"frappe.handler.run_doc_method", "frappe.desk.form.run_method.runserverobj"}:
		docs = _loads(args.get("docs"))
		if isinstance(docs, dict):
			_judge_payload(docs, str(args.get("method") or "") or None)
		elif args.get("dt") and args.get("dn"):
			check_run_method((), args.get("args"), args.get("dt"), args.get("dn"), args.get("method"))
	if paths & {"frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs"}:
		names = _loads(args.get("docnames")) or []
		action = {"update": None}.get(lower, lower)
		for docname in names if isinstance(names, list) else []:
			if action in ("cancel", "submit"):
				_judge_stored(args.get("doctype"), docname, action)
			else:
				data = _loads(args.get("data")) or {}
				_judge_payload({"doctype": args.get("doctype"), "name": docname, **data})
