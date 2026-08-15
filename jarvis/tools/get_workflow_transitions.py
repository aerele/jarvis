"""List the Workflow transitions available to the current user for a doc (or a batch).

Frappe DocTypes can be governed by a **Workflow** (a state machine: e.g.
Leave Application *Applied -> Approved / Rejected*). You advance such a doc by
applying an *action* (Approve / Reject), not by calling submit(). Which actions
are available depends on the current state, the acting user's roles, each
transition's condition, and the self-approval rule.

This is the READ half of workflow support: it tells the agent (and the user)
each doc's current state and exactly which actions THIS user may take next, so
the agent never guesses. The WRITE half is ``apply_workflow_action``.

Batch shape (``names=[...]``) powers approval-queue triage: "which of these 20
leave applications can I approve right now?" in one call.

Pure read - enforces read permission per record and reuses Frappe's own
user-aware ``get_transitions`` (roles + condition) plus ``has_approval_access``
(the self-approval rule the engine omits from ``get_transitions``).
"""

import frappe
from frappe.model.workflow import (
	WorkflowStateError,
	get_transitions,
	get_workflow_name,
	get_workflow_state_field,
	has_approval_access,
)

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import require_doctype_and_name
from jarvis.tools._bulk import _MAX_BATCH


def get_workflow_transitions(doctype: str, name: str | None = None, names: list | None = None) -> dict:
	"""Return the current workflow state + the actions available to the caller.

	Single shape when the DocType has no active workflow:
	    {"doctype", "name", "has_workflow": False}
	Single shape when it does:
	    {"doctype", "name", "has_workflow": True, "workflow", "state_field",
	     "current_state", "available_actions": [{"action", "next_state"}, ...]}
	Batch (``names``): {"doctype", "results": [<single shape>, ...], "count"}.

	``available_actions`` is filtered to what THIS user may do now (role +
	condition + self-approval).
	"""
	if names is not None:
		return _gwt_batch(doctype, names)

	require_doctype_and_name(doctype, name)
	return _gwt_one(doctype, name)


def _gwt_one(doctype: str, name: str) -> dict:
	"""Existence + per-record read-permission check, then the transitions."""
	if not frappe.db.exists(doctype, name):
		raise InvalidArgumentError(f"unknown {doctype}: {name}")

	if not frappe.has_permission(doctype, ptype="read", doc=name):
		raise PermissionDeniedError(f"no read permission on {doctype} {name}")

	wf = get_workflow_name(doctype)
	if not wf:
		return {"doctype": doctype, "name": name, "has_workflow": False}

	doc = frappe.get_doc(doctype, name)
	state_field = get_workflow_state_field(wf)
	current_state = doc.get(state_field)

	base = {
		"doctype": doctype,
		"name": name,
		"has_workflow": True,
		"workflow": wf,
		"state_field": state_field,
	}

	# A cancelled doc has no forward transitions: apply_workflow's docstatus
	# ladder rejects docstatus 2, so surfacing any action here would list a
	# move the write tool would always refuse.
	if doc.docstatus == 2:
		return {**base, "current_state": current_state or None, "available_actions": []}

	try:
		# raise_exception=True keeps the empty-state case a bare raise; the
		# default frappe.throw()s, which first msgprints into message_log and
		# would ride out of our ok response as a stray _server_messages.
		transitions = get_transitions(doc, raise_exception=True)
	except WorkflowStateError:
		# No workflow state on the doc yet (e.g. created before the workflow was
		# activated). Report it plainly instead of a confusing error envelope.
		return {
			**base,
			"current_state": None,
			"available_actions": [],
			"note": "This document has no workflow state set yet.",
		}

	user = frappe.session.user
	available = [
		{"action": t.get("action"), "next_state": t.get("next_state")}
		for t in transitions
		if has_approval_access(user, doc, t)
	]
	return {**base, "current_state": current_state, "available_actions": available}


def _gwt_batch(doctype: str, names: list) -> dict:
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")
	if len(names) > _MAX_BATCH:
		raise InvalidArgumentError(f"too many names in one batch (max {_MAX_BATCH})")

	# Pure read - no savepoint. Each doc gets its own read-permission check.
	results = [_gwt_one(doctype, n) for n in names]
	return {"doctype": doctype, "results": results, "count": len(results)}
