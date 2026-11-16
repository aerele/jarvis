"""Save a draft card with the values the person edited: their Save confirms THOSE values.

Every check runs under the card's row lock and writes nothing, so a refusal leaves the
row Pending with its card unchanged. Then the row is claimed (``Executing``,
``edited``), the edited call runs as the person, and the row ends Executed or Failed
through the Approval Board's Edit & create steps (``approvals_api._edit_created`` /
``_edit_failed``). The claim is NOT committed before the write: it commits with the
record, so a worker that dies first leaves the card Pending and nothing saved. ``settle``
then turns the card's own row into its receipt (``on_chat_settled`` decides the turn).
What ``values`` means is ``draft_values``' contract.

The reply is decided by the row's own state, so a retry is safe: a save that already
went through answers as it did, a card that is being handled answers "try again" with
the form kept open, and a card that ended answers how it ended.

Lock order: the card's row (``FOR UPDATE NOWAIT``, as Confirm takes it), then the close
fences (``confirmation_is_current``). No conversation lock is held, as on Confirm."""

from __future__ import annotations

import copy

import frappe
from frappe import _

TOOLS = ("create_doc", "update_doc")
CLOSED, BUSY, STALE, NEEDS_CARD = (
	"DraftClosedError",
	"DraftBusyError",
	"DraftStaleError",
	"DraftNeedsCardError",
)
_CLOSED = "This draft is no longer open. Ask me to draft it again."
_ALREADY_SAVED = "This draft was already saved from the card."
_NOT_SAVED = "This draft was not saved. Ask me to draft it again."
_UNFINISHED = "This draft did not finish saving."
_UNREADABLE = "This draft could not be read. Close the form and open it again."
_BUSY = "This draft is being handled. Try again in a moment."
_STALE = "This record changed after the draft was made. Ask me to draft the update again."
_NEEDS_CARD = "This change needs a confirmation card and can't be edited here."
_NOT_HERE = "This can't be fixed in the form. Ask me to change the draft."
_UNCHECKED = (
	"These values could not be checked safely, so nothing was saved. Check the record before trying again."
)
_CRASHED = "Something went wrong and nothing was saved. Try again."
_CRASHED_LATE = "Something went wrong while saving. Check the record before trying again."
_FIX_HINT = "Correct the marked fields, then save again."
# The row's fixed reason text for a draft saved with edited values (never free text).
EDITED_REASON = "Saved from the draft card with edited values."


def save(token: str, conversation: str, values, approver: str) -> dict:
	"""``actions_api.save_draft_card`` after its owner check."""
	edits = values_of(values)
	if edits is None:
		return {
			"ok": False,
			"error": {"code": "InvalidArgumentError", "message": _(_UNREADABLE), "fields": []},
		}
	return DraftSave(token, conversation, edits, approver).run()


def values_of(values) -> dict | None:
	"""The form's values as a dict (a JSON string from the client); None in, no edits out.
	Anything that is not a map once parsed (malformed JSON, a list, a number, "null") is
	None: the caller answers with a reply the form shows, never a server error."""
	if values in (None, ""):
		return {}
	if isinstance(values, str):
		try:
			values = frappe.parse_json(values)
		except ValueError:
			return None
	return values if isinstance(values, dict) else None


class DraftSave:
	def __init__(self, token: str, conversation: str, values: dict, approver: str):
		self.token, self.conversation, self.values, self.approver = token, conversation, values, approver
		self.doctype = ""

	def run(self) -> dict:
		from jarvis.chat.pending_actions._execute import ExecuteCrashed, value_free
		from jarvis.chat.pending_actions._store import PENDING, get_row

		# Checked before the lock too, so a stranger never takes another person's row lock.
		row = get_row(self.token) if self.token else None
		if not self._authorized(row):
			return _closed(CLOSED, _(_CLOSED))
		if row.status != PENDING:
			return self._decided(row)
		try:
			# AC-U4: the unsealed call lives only under value_free (logged value-free).
			return value_free(self._locked, "draft_save_crashed")
		except ExecuteCrashed:
			return self._after_crash()

	def _authorized(self, row) -> bool:
		"""A draft card of this conversation, which the person owns, which runs as them,
		and which is not a File Box chat's (File Box only drafts, never writes)."""
		from jarvis.chat import draft_card
		from jarvis.chat.pending_actions._execute import authorize
		from jarvis.chat.pending_actions._settle import _draft_of
		from jarvis.chat.pending_actions._store import CONV

		if not row or not draft_card.enabled():
			return False
		allowed, adopt = authorize(row, self.approver, "chat", self.conversation)
		return bool(
			allowed
			and adopt is None
			and row.conversation == self.conversation
			and row.tool in TOOLS
			and row.owner_user == self.approver
			and row.exec_user == self.approver
			and isinstance(_draft_of(row), dict)
			and not frappe.db.get_value(CONV, self.conversation, "file_box")
		)

	def _locked(self) -> dict:
		from jarvis.chat import approvals_api
		from jarvis.chat.agent_closure import confirmation_is_current
		from jarvis.chat.pending_actions import _seal
		from jarvis.chat.pending_actions._execute import _preflight, _stale_code
		from jarvis.chat.pending_actions._store import PENDING

		locked, busy = approvals_api._lock_pending(self.token)  # commits first: a fresh snapshot
		if busy:
			return _busy()
		if not self._authorized(locked):
			return self._refuse(_closed(CLOSED, _(_CLOSED)))
		if locked.status != PENDING:
			return self._no_longer_open(locked)
		if not confirmation_is_current(locked, lock=True):
			return self._refuse(_closed(CLOSED, _(_CLOSED)))
		refused = _preflight(locked, self.approver, None)
		if refused:
			return self._refuse(_closed(CLOSED, refused["error"]["message"]))
		try:
			call = _seal.unseal_call(locked)
		except _seal.SealError as e:
			out = self._refuse(_closed(CLOSED, _(_CLOSED)))
			frappe.log_error(
				title=f"jarvis.pending_action.{e.reason_code}",
				message=f"{locked.name}: failed binding {e.binding}",
			)
			return out
		args = call.get("args") or {}
		if not self._is_draft_call(locked.tool, args):
			return self._refuse(_closed(CLOSED, _(_CLOSED)))
		if locked.tool == "update_doc" and _stale_code(call.get("targets")):
			return self._refuse(_closed(STALE, _(_STALE)))
		edited, refused = self._edited(locked.tool, args)
		if refused:
			return self._refuse(refused)
		return self._write(locked, edited)

	def _is_draft_call(self, tool: str, args: dict) -> bool:
		from jarvis.chat import draft_card, live_draft

		self.doctype = live_draft.real_doctype(args.get("doctype")) or ""
		return bool(self.doctype) and draft_card.is_draft_call(tool, args, must_card=False)

	def _edited(self, tool: str, args: dict) -> tuple[dict | None, dict | None]:
		"""``(edited call, None)``, or ``(None, refusal)``: values the form may not set, a
		change that needs its own card, or a trial run that fails. Nothing is kept."""
		from jarvis.chat import draft_values

		update = tool == "update_doc"
		found = draft_values.validate(
			self.doctype,
			self.values,
			is_update=update,
			user=self.approver,
			name=args.get("name") or "",
			base=args.get("changes") if update else args.get("values"),
		)
		if found:
			fields = {"fields": found, "hint": _(_FIX_HINT)}
			return None, _fixable("InvalidArgumentError", draft_values.summary(found), **fields)
		tables = {df.fieldname for df in frappe.get_meta(self.doctype).get_table_fields()}
		edited = draft_values.edited_call(tool, args, self.values, tables=tables)
		return edited, self._risk(tool, edited) or self._trial(tool, edited)

	def _risk(self, tool: str, edited: dict) -> dict | None:
		"""Sensitive or structural: the person's one click is not that card (apply_action's
		rule; the write below gets no ``allow_risky``)."""
		from jarvis import api
		from jarvis.tools import _write_risk

		try:
			risk = _write_risk.check(tool, edited, guarded=True)
		except api.WriteRefusedError:
			risk = "refused"
		return _closed(NEEDS_CARD, _(_NEEDS_CARD)) if risk else None

	def _trial(self, tool: str, edited: dict) -> dict | None:
		"""The edited call run in a sandbox, as the person: its failure keeps the card open."""
		from jarvis import api

		try:
			if tool == "create_doc" and not edited.get("values"):
				# Every field left empty: marked as missing, like apply_action.
				raise frappe.MandatoryError(_("Value missing"))
			# A copy: Frappe's row init adds keys (``doctype``) to the dicts it is given, and
			# the write and the settlement must get ``edited`` exactly as the person sent it.
			api._run_preview(tool, copy.deepcopy(edited))
		except api.PreviewSandboxLost:
			return _fixable("InternalError", _(_UNCHECKED))
		except (
			api.JarvisError,
			frappe.PermissionError,
			frappe.ValidationError,
			frappe.DuplicateEntryError,
		) as e:
			return self._trial_refusal(tool, edited, e)
		return None

	def _trial_refusal(self, tool: str, edited: dict, e: Exception) -> dict:
		"""Fixable with the fields marked, except what no value in the form fixes (a missing
		value it cannot fill, a permission denial): closed, the card still Pending."""
		from jarvis import api
		from jarvis.chat.actions_api import _mark_fields, _value_failure
		from jarvis.tools._write_risk import REFUSAL_CODES

		err = dict(api._preview_error(e)["error"])
		frappe.clear_messages()
		if err.get("code") in REFUSAL_CODES:
			return _closed(NEEDS_CARD, _(_NEEDS_CARD))
		update = tool == "update_doc"
		marked = None
		if _value_failure(err):
			written = edited["changes"] if update else edited["values"]
			verb = "update" if update else "create"
			marked = _mark_fields(err, verb, self.doctype, edited.get("name") or "", written, e)
		message = frappe.utils.strip_html(str(err.get("message") or "")) or _("These values can't be saved.")
		code = err.get("code") or "InvalidArgumentError"
		if marked is False or code == "PermissionDeniedError":
			return _closed(code, f"{_sentence(message)} {_(_NOT_HERE)}")
		extra = {"fields": err["fields"], "hint": err.get("hint")} if err.get("fields") else {}
		return _fixable(code, message, **extra)

	def _write(self, locked, edited: dict) -> dict:
		"""Claim, run as the person, end the row (one commit with the record), settle."""
		from jarvis import api
		from jarvis.chat import approvals_api, turn_message_binding
		from jarvis.chat.pending_actions import _execute
		from jarvis.chat.pending_actions._store import claim, get_row

		if not claim(locked.name, self.approver, edited=True):
			return self._refuse(_busy())
		stamp = get_row(locked.name).executing_at  # tells our claim apart after an interference
		result, interfered, crash_tb = _execute._dispatch(
			locked, edited, user=self.approver, allow_risky=False, savepoint=True
		)
		if not crash_tb and api.envelope_ok(locked.tool, result):
			ref = _execute._result_ref(edited, result)
			res = approvals_api._edit_created(
				locked, self.approver, stamp, result, ref, reason=EDITED_REASON, edited_args=edited
			)
			return self._finish(res)
		_execute._discard_failed_dispatch(locked, edited, crash_tb, actor=self.approver, result=result)
		# A card that fails ends an approved skill run, as a failed Confirm does.
		turn_message_binding.end_skill_autorun_if_open(locked.conversation, "card_failed")
		res = approvals_api._edit_failed(locked, self.approver, stamp, result, interfered, edited_args=edited)
		return self._finish(res)

	def _finish(self, res: dict) -> dict:
		"""The reply, from the row's final state (settled by now). Only an edited Failed row
		is this save's own failure. Anything else was decided elsewhere, for example a
		Confirm that ran the card while this failed write was being rolled back: it
		answers as that row ended."""
		from jarvis.chat.pending_actions._store import FAILED, get_row

		row = get_row(self.token)
		if not row:
			return _closed(CLOSED, _(_CLOSED))
		if row.status == FAILED and row.edited:
			return self._failed(row, res)
		return self._decided(row)

	def _failed(self, row, res: dict) -> dict:
		from jarvis.chat.pending_actions._settle import outcome_for
		from jarvis.chat.pending_actions._store import REASON_TEXT

		error = res.get("error") if isinstance(res.get("error"), dict) else {}
		message = frappe.utils.strip_html(str(error.get("message") or ""))
		message = message or REASON_TEXT.get(row.reason_code or "") or REASON_TEXT["failed"]
		partial = outcome_for(row.status, row.reason_code, row.tool) in ("partial", "unknown")
		# Told when the failure's continuation turn is seeded: it marks the row settled.
		return _closed(row.reason_code or "failed", message, told=bool(row.settled), partial=partial)

	def _decided(self, row) -> dict:
		"""The reply for a card that is no longer Pending (a retry, a second device)."""
		from jarvis.chat.pending_actions._settle import outcome_for
		from jarvis.chat.pending_actions._store import EXECUTED, EXECUTING, FAILED

		if row.status == EXECUTING:
			return _busy()
		# The agent is told when the row settles, so a settled row's closed reply says so
		# (the desktop adds "Jarvis could not be told" only when this is False).
		told = bool(row.settled)
		if row.status == EXECUTED:
			if row.edited and row.decided_by == self.approver:
				return self._saved(row)  # this person's save already went through
			return _closed(CLOSED, _(_ALREADY_SAVED), told=told)
		if row.status == FAILED:
			if outcome_for(row.status, row.reason_code, row.tool) in ("partial", "unknown"):
				return _closed(CLOSED, _(_UNFINISHED), told=told, partial=True)
			return _closed(CLOSED, _(_NOT_SAVED), told=told)
		return _closed(CLOSED, _(_CLOSED), told=told)

	def _saved(self, row) -> dict:
		"""``apply_action``'s success shape (queued fields when a continuation queued)."""
		from jarvis.chat.actions_api import _suggest_next_safe, _take_continuation

		verb = "create" if row.tool == "create_doc" else "update"
		name = row.result_name or ""
		slug = (row.result_doctype or "").lower().replace(" ", "-")
		out = {"ok": True, "verb": verb, "name": name, "doc_url": f"/app/{slug}/{name}"}
		if verb == "create":
			step = _suggest_next_safe(row.result_doctype, name)
			if step:
				out["suggested_next"] = step
		cont = _take_continuation(row.name)
		if cont and cont.get("queued"):
			out.update(
				queued=True,
				queued_position=cont.get("queued_position"),
				run_id=cont.get("run_id"),
				message_id=cont.get("message_id"),
			)
		return out

	def _after_crash(self) -> dict:
		"""An unexpected error (rolled back and logged by ``value_free``). Still Pending:
		nothing was saved and the card is open. Executing: something committed part-way."""
		from jarvis.chat.pending_actions._store import EXECUTING, PENDING, get_row

		row = get_row(self.token)
		if not row:
			return _closed("InternalError", _(_CRASHED_LATE))
		if row.status == PENDING:
			return _fixable("InternalError", _(_CRASHED))
		if row.status == EXECUTING:
			return _closed("InternalError", _(_CRASHED_LATE), partial=True)
		return self._decided(row)

	def _no_longer_open(self, locked) -> dict:
		from jarvis.chat.pending_actions import settle
		from jarvis.chat.pending_actions._store import TERMINAL, get_row

		frappe.db.rollback()
		if locked.status in TERMINAL and not locked.settled:
			settle(locked.name)
		# Read again: the settle above is what decides whether the agent was told.
		return self._decided(get_row(self.token) or locked)

	@staticmethod
	def _refuse(envelope: dict) -> dict:
		"""Before the claim: drop the row lock and anything the checks queued."""
		frappe.db.rollback()
		frappe.clear_messages()
		return envelope


def _closed(code: str, message: str, *, told: bool = False, partial: bool = False) -> dict:
	"""A save no retry fixes: both apps drop the card (``draftFailureOf``)."""
	out = {"ok": False, "closed": True, "assistant_told": told, "error": {"code": code, "message": message}}
	if partial:
		out["outcome"] = "partial"
	return out


def _fixable(code: str, message: str, *, fields=None, hint=None) -> dict:
	"""The panel stays open with ``error.fields`` marked; the card is untouched."""
	error = {"code": code, "message": message}
	if fields:
		error["fields"] = fields
	if hint:
		error["hint"] = hint
	return {"ok": False, "error": error}


def _busy() -> dict:
	"""Another decider holds the card (a Confirm, a second device): the form stays open
	with the person's edits, and nothing was claimed."""
	return {"ok": False, "error": {"code": BUSY, "message": _(_BUSY), "fields": []}}


def _sentence(text: str) -> str:
	text = text.strip()
	return text if text.endswith((".", "!", "?")) else f"{text}."
