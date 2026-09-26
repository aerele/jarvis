"""Execute: claim-first and at most once, for every tool (§4.4 Execute, steps 1-13).

The row is claimed (committed ``Executing``) before the call runs, so a crash
anywhere after the claim ends ``Failed``/``interrupted`` via the reconciler and is
never re-run. A failed dispatch is fully rolled back, so nothing it queued for
after-commit (jobs, webhooks, realtime) escapes; its failure audit row is
re-recorded after the rollback."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import frappe

from jarvis._session import authenticated_user, impersonate
from jarvis.chat.pending_actions import _seal
from jarvis.chat.pending_actions._park import target_state
from jarvis.chat.pending_actions._settle import outcome_for, settle
from jarvis.chat.pending_actions._store import (
	CONV,
	EXECUTED,
	EXECUTING,
	FAILED,
	PENDING,
	REASON_TEXT,
	SHEET,
	TERMINAL,
	_terminal_update,
	claim,
	get_row,
)
from jarvis.permissions import is_valid_unattended_owner, refuse_in_tool_dispatch

_PROVENANCE = {"chat": "chat", "file_box_held": "approval", SHEET: "approval"}
# Kinds a System Manager may act on for another user (the File Box dropper's).
_SM_KINDS = frozenset({"file_box_held", SHEET})
_SYSTEM_MANAGER = "System Manager"


@dataclass(frozen=True)
class ArmHook:
	"""Approve & run (PR-3a). ``refuse(row)`` runs under the row lock and returns a
	refusal envelope (nothing consumed) or None; ``apply(row)`` arms the run after a
	successful execute has committed, in its own short transaction. ``claimed(row)``
	runs once the claim has committed, before the call (best-effort)."""

	refuse: Callable[[dict], dict | None]
	apply: Callable[[dict], None]
	claimed: Callable[[dict], None] | None = None


class ExecuteCrashed(frappe.ValidationError):
	"""An unexpected error inside ``execute``. Carries no values: the traceback
	(no locals) is in the Error Log ``jarvis.pending_action.execute_crashed``."""


_CRASHED = "This action hit an unexpected error. Check the record before retrying."


def _refusal(reason_code: str, message: str, **extra) -> dict:
	return {
		"ok": False,
		"error": {"type": "InvalidConfirmation", "message": message},
		"reason_code": reason_code,
		**extra,
	}


_NOT_FOUND = ("not_found", "This confirmation is no longer valid.")
_BUSY = ("busy", "This action is being handled right now. Try again in a moment.")
_ARMED = ("armed_run", "This macro run is stopping; that confirmation was withdrawn and nothing ran.")
_IDENTITY = (
	"identity_refused",
	"This action can no longer run as the user it was proposed for. Nothing ran.",
)


def authorize(
	row, approver: str, kind: str, conversation: str | None = None, *, acting: bool = True
) -> tuple[bool, str | None]:
	"""D1 owner rule + the replay guard (no lock). Returns ``(allowed, adopt)``:
	chat rows are owner-only (an SM too); a System Manager may act on another user's
	held row or sheet. A sheet still collecting may be read (``acting=False``), never
	acted on. A caller-passed conversation must be the row's, or an owned
	conversation a conversation-less card adopts (D9)."""
	if not row or row.kind != kind or (acting and kind == SHEET and row.get("collecting")):
		return False, None
	if row.owner_user != approver and not (
		kind in _SM_KINDS and _SYSTEM_MANAGER in frappe.get_roles(approver)
	):
		return False, None
	passed = str(conversation or "").strip()
	if not passed or passed == row.conversation:
		return True, None
	if row.conversation:
		return False, None
	owned = frappe.db.get_value(CONV, passed, "owner") == approver
	return owned, passed if owned else None


def _preflight(row, approver: str, arm: ArmHook | None) -> dict | None:
	"""Refusals under the row lock; the row stays Pending."""
	if row.conversation and frappe.db.get_value(CONV, row.conversation, "skip_confirmation"):
		return _refusal(*_ARMED)
	if arm:
		refused = arm.refuse(row)
		if refused:
			return refused
	# Data columns, not Links: a renamed or deleted user no longer resolves.
	if not frappe.db.get_value("User", row.exec_user, "enabled"):
		return _refusal(*_IDENTITY)
	if approver != row.exec_user and not is_valid_unattended_owner(row.exec_user):
		return _refusal(*_IDENTITY)
	return None


def _stale_code(targets) -> str | None:
	"""D2: ``target_missing`` / ``stale`` against the park-time snapshot."""
	for doctype, name, modified_us, docstatus in targets or []:
		try:
			current = target_state(doctype, name)
		except Exception:
			return "target_missing"
		if current[0] is None:
			return "target_missing"
		if current != [modified_us, docstatus]:
			return "stale"
	return None


def outcome_of(status: str | None, reason_code: str | None, tool: str | None) -> str | None:
	"""The chip of a terminal row, None while it is Pending/Executing."""
	return outcome_for(status, reason_code, tool) if status in TERMINAL else None


def handled(row, message: str = _NOT_FOUND[1]) -> dict:
	"""The refusal for a row that is no longer Pending (every reply carries
	``reason_code`` / ``pa_status`` / ``outcome``)."""
	code = "executing" if row.status == EXECUTING else "already_handled"
	return _refusal(
		code, message, pa_status=row.status, outcome=outcome_of(row.status, row.reason_code, row.tool)
	)


def value_free(fn, title: str, *args, **kwargs):
	"""AC-U4: run ``fn`` so the unsealed call lives only in its frames; an escaping
	error is logged without locals (``jarvis.pending_action.<title>``) and re-raised
	value-free, unchained."""
	try:
		return fn(*args, **kwargs)
	except Exception:
		tb = frappe.get_traceback()
		frappe.db.rollback()
		frappe.log_error(title=f"jarvis.pending_action.{title}", message=tb)
		frappe.db.commit()
		raise ExecuteCrashed(_CRASHED) from None


def _dispatch(row, args: dict, *, user: str | None = None) -> tuple[dict | None, bool, str]:
	"""Run the sealed call as ``exec_user`` (or ``user``: an Edit & create runs as the
	approver). Returns ``(result, interfered, crash_tb)``.

	``interfered``: the transaction was not ours start to finish. A mid-dispatch
	commit (``run_import``, DDL) pops and runs the ``before_commit`` sentinel; a
	mid-dispatch full rollback clears it (a later commit would then go unseen).
	Either way it is gone from the deque."""
	from jarvis import api

	def _pa_commit_sentinel():
		pass

	callbacks = frappe.db.before_commit
	callbacks.add(_pa_commit_sentinel)
	crash_tb = ""
	result = None
	try:
		with impersonate(user or row.exec_user):
			result = api.dispatch_confirmed(
				row.tool,
				args,
				provenance=_PROVENANCE[row.kind],
				provenance_name=row.name if row.kind != "chat" else "",
			)
			api._apply_run_method_read_filter(row.tool, result)
	except Exception:
		crash_tb = frappe.get_traceback() or "dispatch raised"
	interfered = _pa_commit_sentinel not in callbacks._functions
	if not interfered:
		callbacks._functions.remove(_pa_commit_sentinel)  # disarm before any later commit
	return result, interfered, crash_tb


def _discard_failed_dispatch(row, args: dict, crash_tb: str, *, actor: str | None = None) -> None:
	"""Full rollback of a failed dispatch, then re-record what must survive it."""
	from jarvis import agent_audit

	frappe.db.rollback()  # resets both callback deques; after_rollback clears realtime
	# An empty list is falsy, so the next webhook re-registers its flush; a stale
	# ``_link_count`` would stop the next link count from re-registering at all.
	frappe.local._webhook_queue = []
	if hasattr(frappe.local, "_link_count"):
		del frappe.local._link_count
	agent_audit.record_write(
		actor=actor or row.exec_user,
		tool=row.tool,
		args=args,
		result=None,
		outcome="failed",
		provenance=_PROVENANCE[row.kind],
		provenance_name=row.name if row.kind != "chat" else "",
	)
	if crash_tb:
		frappe.log_error(title="jarvis.pending_action.dispatch_crashed", message=crash_tb)


def late_outcome(name: str, status: str, code: str | None, outcome: str) -> None:
	"""The row moved on while the call ran (the reconciler's interrupted): its verdict
	stands; the real outcome is appended and logged. Does not commit."""
	frappe.db.sql(
		"UPDATE `tabJarvis Pending Action` SET reason=CONCAT(IFNULL(reason, ''), %(late)s) WHERE name=%(n)s",
		{"n": name, "late": f" Late outcome: {outcome}."},
	)
	frappe.log_error(
		title="jarvis.pending_action.late_outcome",
		message=f"{name}: finished {status} ({code or 'ok'}) after the reconciler's verdict",
	)


def _result_ref(args: dict, result) -> tuple[str, str]:
	from jarvis.chat.entities import refs_from_tool

	data = result.get("data") if isinstance(result, dict) else None
	doctype, name = refs_from_tool(args, data)
	return doctype or "", name or ""


def _fail_unrun(row, approver: str, code: str, binding: str = "", *, batch_id=None, defer=False) -> dict:
	"""Step 5: a seal or staleness failure ends the row without dispatching. In a typed
	batch it joins the batch and waits for ``settle_batch`` (ONE continuation)."""
	cols = {"batch_id": batch_id} if batch_id else {}
	_terminal_update(row.name, [PENDING], FAILED, reason_code=code, decided_by=approver, **cols)
	if code in ("tampered", "unverifiable"):
		frappe.log_error(
			title=f"jarvis.pending_action.{code}", message=f"{row.name}: failed binding {binding}"
		)
	frappe.db.commit()
	if not defer:
		settle(row.name)
	return _refusal(code, REASON_TEXT[code], pa_status=FAILED, outcome="failed")


def execute(
	name: str,
	*,
	kind: str = "chat",
	conversation: str | None = None,
	defer_continuation: bool = False,
	batch_id: str | None = None,
	arm: ArmHook | None = None,
) -> dict:
	"""Run pending action ``name`` for the authenticated user. Returns the tool's
	envelope plus ``reason_code`` / ``pa_status`` / ``outcome``, or a refusal
	envelope. ``defer_continuation`` leaves settling to ``settle_batch(batch_id)``.
	An unexpected error raises :class:`ExecuteCrashed` (value-free)."""
	refuse_in_tool_dispatch()
	approver = authenticated_user()
	if not approver or approver == "Guest":
		raise frappe.PermissionError("authentication required")
	name = str(name or "").strip()
	allowed, adopt = authorize(get_row(name) if name else None, approver, kind, conversation)
	if not allowed:
		return _refusal(*_NOT_FOUND)
	# AC-U4: the row and the unsealed call live only in the inner frames.
	return value_free(
		_execute_locked,
		"execute_crashed",
		name,
		approver,
		adopt,
		defer_continuation=defer_continuation,
		batch_id=batch_id,
		arm=arm,
	)


def _execute_locked(
	name: str, approver: str, adopt: str | None, *, defer_continuation: bool, batch_id, arm
) -> dict:
	"""Steps 3-13: lock, preflight, unseal, claim, dispatch, final write, settle."""
	from jarvis import api

	frappe.db.commit()
	try:
		row = get_row(name, lock="nowait")
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
		frappe.db.rollback()
		return _refusal(*_BUSY)
	if not row:
		return _refusal(*_NOT_FOUND)
	if row.status != PENDING:
		frappe.db.commit()
		if row.status in TERMINAL and not row.settled:
			settle(name)
		return handled(row)

	refused = _preflight(row, approver, arm)
	if refused:
		frappe.db.rollback()
		return refused
	unrun = {"batch_id": batch_id, "defer": defer_continuation}
	try:
		call = _seal.unseal_call(row)
	except _seal.SealError as e:
		return _fail_unrun(row, approver, e.reason_code, e.binding, **unrun)
	stale = _stale_code(call.get("targets"))
	if stale:
		return _fail_unrun(row, approver, stale, **unrun)

	if not claim(name, approver, batch_id=batch_id, adopted_conversation=adopt):
		frappe.db.rollback()
		return _refusal(*_BUSY)
	frappe.db.commit()
	if arm and arm.claimed:
		try:
			arm.claimed(row)
		except Exception:
			frappe.log_error(title="jarvis.pending_action.arm_claimed_failed", message=frappe.get_traceback())
			frappe.db.commit()

	args = call.get("args") or {}
	result, interfered, crash_tb = _dispatch(row, args)
	if crash_tb or not (isinstance(result, dict) and result.get("ok")):
		_discard_failed_dispatch(row, args, crash_tb)
	ok = not crash_tb and api.envelope_ok(row.tool, result)
	status = EXECUTED if ok else FAILED
	code = None if ok else ("partial" if interfered else "failed")
	outcome = outcome_for(status, code, row.tool)
	if crash_tb:
		result = api._error(
			"InternalError",
			"the confirmed action failed unexpectedly and was not saved"
			if outcome == "failed"
			else "the confirmed action may have partly run; check before retrying",
		)

	result_doctype = result_name = ""
	if ok:
		from jarvis.chat import import_announce

		# Bound to the sealed owner/origin conversation, never a client value.
		import_announce.bind_after_run_import(
			{
				"tool": row.tool,
				"conversation": call.get("origin_conversation"),
				"owner": call.get("owner_user"),
			},
			result,
		)
		result_doctype, result_name = _result_ref(args, result)
	done = _terminal_update(
		name,
		[EXECUTING],
		status,
		reason_code=code,
		result_doctype=result_doctype,
		result_name=result_name,
		sealed_settlement=_seal.seal_settlement(name, {"result": result, "outcome": outcome}),
	)
	if not done:
		late_outcome(name, status, code, outcome)
	frappe.db.commit()

	if ok and done and arm:
		try:
			arm.apply(row)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="jarvis.pending_action.arm_failed", message=frappe.get_traceback())
			frappe.db.commit()
	if not defer_continuation:
		settle(name)

	final = get_row(name) if not done else None
	response = dict(result) if isinstance(result, dict) else {"ok": ok}
	response.update(
		reason_code=(final.reason_code if final else code) or None,
		pa_status=final.status if final else status,
		outcome=outcome_for(final.status, final.reason_code, row.tool) if final else outcome,
	)
	return response
