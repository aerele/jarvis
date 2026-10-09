"""Execute: claim-first and at most once, for every tool (§4.4 Execute, steps 1-13).

The row is claimed (committed ``Executing``) before the call runs, so a crash
anywhere after the claim ends ``Failed``/``interrupted`` via the reconciler and is
never re-run. A failed dispatch is fully rolled back, so nothing it queued for
after-commit (jobs, webhooks, realtime) escapes; its failure audit row is
re-recorded after the rollback.

A guarded structure write (``jarvis.tools._guarded_structure``; R2-10) runs under
the structure lock of every form it alters, taken BEFORE the claim and held to the
end: a held lock leaves the row Pending ("try again"), what its clean-up needs is
sealed on the row in the claim's transaction, and a failure is cleaned up before
the locks are let go."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass

import frappe

from jarvis._commit_watch import CommitWatch
from jarvis._session import authenticated_user, impersonate
from jarvis.chat.pending_actions import _seal
from jarvis.chat.pending_actions._park import target_state
from jarvis.chat.pending_actions._settle import outcome_for, settle, with_reference
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
	clear_undo,
	get_row,
	stash_undo,
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


@dataclass
class _Structure:
	"""A guarded structure write being confirmed: its handler and what its clean-up
	needs (sealed on the row at the claim; ``stamp_written`` adds the exact row the
	save wrote, in the save's own transaction)."""

	handler: object
	undo: dict


def _structure_of(row, args: dict) -> _Structure | None:
	"""The guarded structure write a chat card stands for, else None."""
	from jarvis.tools import _guarded_structure, _write_risk

	if row.kind != "chat" or row.tool not in ("create_doc", "update_doc"):
		return None
	risk = _write_risk.risk_of(row.tool, args)
	if risk not in _write_risk.GUARDED_STRUCTURE or not _guarded_structure.available():
		return None  # dispatch_confirmed refuses it as any structure write
	if not _parked_as_structure(row):
		# The call is a guarded structure write NOW but its card was not parked as
		# one (CRM Settings was carded as plain settings, then the data sync was
		# switched on in Desk): the person never saw what it would do. No lock is
		# taken, so dispatch_confirmed refuses it as any structure write.
		return None
	return _Structure(_guarded_structure.handler_for(risk, args), {})


def _card_holds(row, structure: _Structure) -> bool:
	"""Under the form's lock, before the claim: whether the card still says what the
	write will do (the handler's ``card_holds``, when it has one). A handler whose
	own checks now refuse the call says so itself a moment later: the write tool
	runs those checks again under this same lock before it writes anything, and its
	refusal says why. Any other failure counts as a card that no longer holds: a
	check that could not be made never lets the write run."""
	from jarvis.exceptions import JarvisError

	holds = getattr(structure.handler, "card_holds", None)
	if holds is None:
		return True
	card = row.get("card")
	try:
		card = frappe.parse_json(card) if isinstance(card, str) else card
		return bool(holds(str((card or {}).get("risk_line") or "")))
	except JarvisError:
		frappe.clear_messages()
		return True
	except Exception:
		frappe.log_error(title="jarvis.pending_action.card_check_failed", message=frappe.get_traceback())
		frappe.clear_messages()
		return False


def _parked_as_structure(row) -> bool:
	"""Whether the card the person confirmed was a guarded structure card
	(``card.structural``, set at park and bound into the seal with the card)."""
	card = row.get("card")
	try:
		card = frappe.parse_json(card) if isinstance(card, str) else card
	except Exception:
		return False
	return isinstance(card, dict) and card.get("structural") is True


def _busy_structure(e: Exception) -> dict:
	"""The lock of that form is held: nothing ran and the card stays Pending."""
	from jarvis import api

	out = api._error("RetryLaterError", str(e), hint=api._ERROR_HINTS["RetryLaterError"])
	out["error"]["kind"] = "retry_later"
	return {**out, "reason_code": "busy", "pa_status": PENDING, "outcome": None}


def _clean_up_structure(name: str, structure: _Structure):
	"""After a failed guarded structure write (already rolled back), still under its
	lock: remove what it left half-made, from the clean-up state as it COMMITTED
	(what the save stamped went with the rollback unless the save's row committed).
	Tried twice. The ``CleanUp``, or None when it failed both times: the outcome
	then stays partial, the state stays on the row, and the reconciler finishes it
	(``_reconcile._retry_structure_cleanups``)."""
	from jarvis.tools import _guarded_structure

	for attempt in (1, 2):
		try:
			undo = _seal.unseal_undo(get_row(name)) or structure.undo
			if structure.undo.get("unstamped"):
				# The stamp never reached the row: clean up from what the save held
				# in memory (the same exact name and timestamp), and never call the
				# result proven.
				note = _guarded_structure.clean_up(structure.undo)
				note.clean = False
				return note
			return _guarded_structure.clean_up(undo)
		except Exception:
			frappe.db.rollback()
			if attempt == 2:
				frappe.log_error(
					title="jarvis.pending_action.structure_cleanup_failed",
					message=f"{name}: needs a person if the next reconcile cannot finish it\n"
					+ frappe.get_traceback(),
				)
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist the log
	return None


def _held_to_the_card(name: str, args: dict, structure: _Structure, result: dict) -> dict:
	"""After a guarded structure write was saved, before anything says so: is what
	was stored what the card showed (``_verify.unlike_the_card``)? ``result`` when
	it is. Otherwise a failure in its place: the caller then rolls back and cleans
	up exactly as after any failed guarded write, so the difference is not kept."""
	from jarvis import api
	from jarvis.chat.pending_actions import _verify

	tracebacks: list = []
	unlike = _verify.unlike_the_card(args, structure.undo, tracebacks)
	if not unlike:
		return result
	frappe.db.rollback()
	frappe.log_error(
		title="jarvis.pending_action.post_write_mismatch",
		message=f"{name}: what was saved differs from the card in: {unlike}\n" + "\n".join(tracebacks),
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist the log
	# What was and was not kept is the clean-up's to say (it is appended to this):
	# a column that had been added stays, and its field with it.
	out = api._error(
		"InternalError",
		f"What was saved did not match the confirmation card (it differed in: {unlike}), so the "
		"change was undone as far as it can be.",
	)
	out["error"]["kind"] = "not_fixable"
	return out


def _still_locked(name: str, structure: _Structure) -> bool:
	"""After the dispatch: is the form's structure lock still this confirm's, as the
	database sees it? Logged when not."""
	from jarvis.tools._guarded_structure import lock_names, locks_held

	if locks_held(lock_names(structure.handler)):
		return True
	frappe.log_error(
		title="jarvis.pending_action.structure_lock_lost",
		message=f"{name}: the structure lock of {', '.join(lock_names(structure.handler))} was lost during "
		"the confirm (its database connection was dropped); the outcome could not be verified",
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist the log
	return False


def _report_structure_failure(row, args: dict, structure: _Structure, result, note, code: str) -> None:
	"""Say on the failure what the clean-up did (the receipt and the card show it),
	file the attempt (``partial`` when part of it had committed, E4) and log it."""
	from jarvis.tools import _write_risk

	err = result.get("error") if isinstance(result, dict) and isinstance(result.get("error"), dict) else None
	said = note.text if note else "The clean-up could not be completed; check the form in Desk."
	if err is not None:
		err["message"] = f"{str(err.get('message') or '').rstrip()} {said}".strip()
	# A refusal under the lock (the table changed since the park) is already filed
	# as refused by ``_discard_failed_dispatch``.
	if (err or {}).get("code") not in _write_risk.REFUSAL_CODES:
		_record_failed_write(row, args, "partial" if code == "partial" else "failed")
	doctype, name = _write_risk.first_risky_target(row.tool, args)
	_write_risk.log_line(structure.handler.risk, doctype, name, code, tool=row.tool, cleanup=said)
	if note is not None and note.needs_person:
		# The same line the reconciler files for a row it had to leave: found from
		# the Error Log, also when the clean-up ran inside the confirm itself.
		frappe.log_error(
			title="jarvis.pending_action.structure_needs_a_person", message=f"{row.name}: {note.text}"
		)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist the log


def _dispatch(row, args: dict, *, user: str | None = None) -> tuple[dict | None, bool, str]:
	"""Run the sealed call as ``exec_user`` (or ``user``: an Edit & create runs as the
	approver). Returns ``(result, interfered, crash_tb)``.

	``interfered``: the transaction was not ours start to finish (a mid-dispatch commit
	such as ``run_import`` or DDL, or a mid-dispatch full rollback), so a failure may
	have left part of the write behind (``jarvis._commit_watch``)."""
	from jarvis import api

	watch = CommitWatch().arm()
	crash_tb = ""
	result = None
	try:
		with impersonate(user or row.exec_user):
			# allow_risky: a human approved this row, so the write-risk guard admits
			# the sensitive records its arguments name (and nothing else).
			result = api.dispatch_confirmed(
				row.tool,
				args,
				provenance=_PROVENANCE[row.kind],
				provenance_name=row.name if row.kind != "chat" else "",
				allow_risky=True,
			)
			api._apply_run_method_read_filter(row.tool, result)
	except Exception:
		crash_tb = frappe.get_traceback() or "dispatch raised"
	return result, watch.disarm(), crash_tb


def _record_failed_write(
	row, args: dict, outcome: str, *, actor: str | None = None, ref_doctype: str | None = None
) -> None:
	"""The ``Jarvis Agent Write`` row of a failed confirm, written after its rollback."""
	from jarvis import agent_audit

	agent_audit.record_write(
		actor=actor or row.exec_user,
		tool=row.tool,
		args=args,
		result=None,
		outcome=outcome,
		provenance=_PROVENANCE[row.kind],
		provenance_name=row.name if row.kind != "chat" else "",
		ref_doctype=ref_doctype,
	)


def _discard_failed_dispatch(
	row,
	args: dict,
	crash_tb: str,
	*,
	actor: str | None = None,
	result: dict | None = None,
	record: bool = True,
) -> None:
	"""Full rollback of a failed dispatch, then re-record what must survive it. A
	write the risk guard refused stays ``refused`` (the rollback dropped the row
	``_dispatch_and_wrap`` wrote), against the doctype it refused. ``record=False``:
	the caller files the attempt itself, once its clean-up has said how it ended
	(a guarded structure write)."""
	from jarvis.tools._write_risk import REFUSAL_CODES

	err = (result or {}).get("error") if isinstance(result, dict) else None
	err = err if isinstance(err, dict) else {}
	refused = not crash_tb and err.get("code") in REFUSAL_CODES

	frappe.db.rollback()  # resets both callback deques; after_rollback clears realtime
	# An empty list is falsy, so the next webhook re-registers its flush; a stale
	# ``_link_count`` would stop the next link count from re-registering at all.
	frappe.local._webhook_queue = []
	if hasattr(frappe.local, "_link_count"):
		del frappe.local._link_count
	if record or refused:
		_record_failed_write(
			row,
			args,
			"refused" if refused else "failed",
			actor=actor,
			ref_doctype=err.get("doctype") if refused else None,
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
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist before settle
	if row.kind == "chat":
		# A card that fails before it runs ends an approved skill run, as one that
		# fails when it runs does (``_claim_and_run``).
		from jarvis.chat import turn_message_binding

		turn_message_binding.end_skill_autorun_if_open(row.conversation, "card_failed")
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
	"""Steps 3-5: lock, preflight, unseal, staleness; then ``_claim_and_run``."""
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- end snapshot before lock
	try:
		row = get_row(name, lock="nowait")
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
		frappe.db.rollback()
		return _refusal(*_BUSY)
	if not row:
		return _refusal(*_NOT_FOUND)
	if row.status != PENDING:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist before settle
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

	structure = _structure_of(row, call.get("args") or {})
	with ExitStack() as held:
		if structure is not None:
			from jarvis.tools._guarded_structure import StructureBusyError, lock_names, structure_locks

			# Before the claim: a held lock leaves the row Pending, so the same card
			# can be confirmed once the other change to that form is done.
			try:
				held.enter_context(structure_locks(lock_names(structure.handler)))
			except StructureBusyError as e:
				frappe.db.rollback()
				return _busy_structure(e)
			if not _card_holds(row, structure):
				# What the save would do to the form is no longer what the card says
				# (another workflow was activated since): never run it.
				return _fail_unrun(row, approver, "stale", **unrun)
		return _claim_and_run(
			row,
			call,
			approver,
			adopt,
			structure,
			defer_continuation=defer_continuation,
			batch_id=batch_id,
			arm=arm,
		)


def _claim_and_run(
	row, call: dict, approver: str, adopt: str | None, structure, *, defer_continuation: bool, batch_id, arm
) -> dict:
	"""Steps 6-13: claim, dispatch, final write, settle. For a guarded structure
	write (``structure``) the caller holds its form's lock throughout."""
	from jarvis import api

	name = row.name
	if structure is not None:
		# With the claim, in its transaction: a worker that dies mid-way leaves the
		# reconciler what to clean up (and, for an edit, the record to restore).
		from jarvis.tools import _guarded_structure
		from jarvis.tools._guarded_structure import lock_names

		structure.undo = {**structure.handler.snapshot(), "lock": lock_names(structure.handler)}
		if structure.undo.get(_guarded_structure.INCOMPLETE) and not structure.undo.get(
			_guarded_structure.REFUSED
		):
			# Nothing to undo by: not claimed and not run, so the same card can be
			# confirmed once whatever kept the snapshot from being read has passed.
			frappe.db.rollback()
			return _busy_structure(RuntimeError(_guarded_structure.NO_SNAPSHOT))
		stash_undo(name, _seal.seal_undo(name, structure.undo))
	if not claim(name, approver, batch_id=batch_id, adopted_conversation=adopt):
		frappe.db.rollback()
		return _refusal(*_BUSY)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist claim before dispatch
	if arm and arm.claimed:
		try:
			arm.claimed(row)
		except Exception:
			frappe.log_error(title="jarvis.pending_action.arm_claimed_failed", message=frappe.get_traceback())
			frappe.db.commit()

	if row.kind == "chat":
		# A card of an approved skill run: the wait for this click does not count
		# against the run (outside the row lock; the claim is committed).
		from jarvis.chat import turn_message_binding

		turn_message_binding.keep_skill_autorun_open(row.conversation)
	args = call.get("args") or {}
	with ExitStack() as stamped:
		if structure is not None:
			from jarvis.tools._guarded_structure import stamping

			stamped.enter_context(stamping(name, structure.undo))
		result, interfered, crash_tb = _dispatch(row, args)
	if structure is not None and not crash_tb and isinstance(result, dict) and result.get("ok"):
		result = _held_to_the_card(name, args, structure, result)
	if crash_tb or not (isinstance(result, dict) and result.get("ok")):
		_discard_failed_dispatch(row, args, crash_tb, result=result, record=structure is None)
	ok = not crash_tb and bool(isinstance(result, dict) and result.get("ok"))
	if not ok and row.kind == "chat":
		# ...and a card that fails ends the run, as a failed write does.
		from jarvis.chat import turn_message_binding

		turn_message_binding.end_skill_autorun_if_open(row.conversation, "card_failed")
	status = EXECUTED if ok else FAILED
	lock_lost = structure is not None and not _still_locked(name, structure)
	note = _clean_up_structure(name, structure) if structure is not None and not ok else None
	# Owner decision D1: a guarded structure write whose clean-up PROVED nothing is
	# left (the half-made field removed, the edit put back) failed, plainly: nothing
	# was changed. Partial only when something may remain (the field was complete
	# and kept, a later edit stands, the clean-up itself failed).
	# Never when the form's lock was lost during the confirm (its connection was
	# dropped by the server): another change may have run beside this one, so what
	# is on the form could not be verified.
	nothing_left = note is not None and note.clean and not lock_lost
	code = None if ok else ("partial" if lock_lost or (interfered and not nothing_left) else "failed")
	outcome = outcome_for(status, code, row.tool)
	if crash_tb:
		result = api._error(
			"InternalError",
			"the confirmed action failed unexpectedly and was not saved"
			if outcome == "failed"
			else "the confirmed action may have partly run; check before retrying",
		)
	if structure is not None:
		if not ok:
			_report_structure_failure(row, args, structure, result, note, code)
		from jarvis.tools._guarded_structure import keeps_undo

		# Done with: only a failed clean-up keeps its state for the reconciler, and a
		# confirmed write whose snapshot is its undo (the roles it removed, the
		# states it filled) keeps it on the row.
		if (ok and not keeps_undo(structure.handler.risk)) or (not ok and note is not None):
			clear_undo(name)
	if not ok:
		result = with_reference(result, name)

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
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist before settle

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
