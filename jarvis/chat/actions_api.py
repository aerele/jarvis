"""Direct apply for chat action cards (the record draft panel).

The agent emits a ``jarvis-action`` block; the SPA renders it in a side-panel
editor and posts the FINAL values here - the apply itself never runs an LLM
turn. All mutations route through the existing permission-checked tools
(``jarvis.tools.create_doc`` etc.), so this module adds routing + a receipt,
not a second write path.

Multi-step plans: when the applied card carries ``continue`` (or after any
confirmed gated write), the bench dispatches a follow-up agent turn carrying
the receipt, so the agent stages the plan's next step without the user typing
"continue". See ``jarvis.chat.api.enqueue_continuation``.
"""

import frappe
from frappe import _

from jarvis import audit
from jarvis._session import impersonate
from jarvis.chat.api import _NON_EDIT_FIELDTYPES, _next_seq, enqueue_continuation
from jarvis.exceptions import InvalidArgumentError
from jarvis.permissions import require_jarvis_user

MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"

# Child-grid columns can be any data-bearing fieldtype except nested tables
# (no grid-in-grid in v1).
_SKIP_CHILD_FIELDTYPES = _NON_EDIT_FIELDTYPES | {"Table", "Table MultiSelect"}


def _field_dict(df) -> dict:
	return {
		"fieldname": df.fieldname,
		"label": df.label or df.fieldname,
		"fieldtype": df.fieldtype,
		"options": df.options or "",
		"reqd": int(df.reqd or 0),
		"read_only": int(df.read_only or 0),
	}


def _child_columns(child_doctype: str) -> list[dict]:
	"""Grid columns for one child table: the child's in_list_view fields (what
	the Desk grid shows), falling back to the first 4 editable fields when the
	child marks none."""
	meta = frappe.get_meta(child_doctype)
	editable = [df for df in meta.fields if df.fieldname and df.fieldtype not in _SKIP_CHILD_FIELDTYPES]
	listed = [df for df in editable if df.in_list_view]
	return [_field_dict(df) for df in (listed or editable[:4])]


@frappe.whitelist()
@require_jarvis_user
def get_doctype_form_meta(doctype: str) -> dict:
	"""Form metadata for the draft panel: main fields INCLUDING Table fields,
	plus per-table child columns - one call, so the panel never fans out.
	Gated on read permission of the parent (child meta rides on that gate)."""
	doctype = (doctype or "").strip()
	if not doctype or not frappe.db.exists("DocType", doctype):
		return {"ok": False, "reason": _("unknown doctype")}
	if not frappe.has_permission(doctype, "read"):
		frappe.throw(_("You don't have access to {0}.").format(doctype), frappe.PermissionError)
	meta = frappe.get_meta(doctype)
	fields, tables = [], {}
	for df in meta.fields:
		if not df.fieldname:
			continue
		if df.fieldtype == "Table" and df.options:
			fields.append(_field_dict(df))
			tables[df.fieldname] = {
				"child_doctype": df.options,
				"label": df.label or df.fieldname,
				"columns": _child_columns(df.options),
			}
			continue
		if df.fieldtype in _NON_EDIT_FIELDTYPES:
			continue
		fields.append(_field_dict(df))
	return {
		"ok": True,
		"doctype": doctype,
		"is_submittable": int(meta.is_submittable or 0),
		"title_field": meta.get("title_field") or "",
		"fields": fields,
		"tables": tables,
	}


@frappe.whitelist()
@require_jarvis_user
def load_doc(doctype: str, name: str) -> dict:
	"""Current values of one document (main fields + child rows restricted to
	the form-meta columns) so the panel can pre-fill an update draft. Gated on
	WRITE permission - this endpoint exists to edit."""
	doctype = (doctype or "").strip()
	name = (name or "").strip()
	if not doctype or not name:
		raise InvalidArgumentError("doctype and name are required")
	if not frappe.db.exists(doctype, name):
		raise frappe.DoesNotExistError(f"{doctype} {name} not found")
	if not frappe.has_permission(doctype, "write", doc=name):
		frappe.throw(_("You can't edit {0} {1}.").format(doctype, name), frappe.PermissionError)
	fm = get_doctype_form_meta(doctype)
	doc = frappe.get_doc(doctype, name)
	values = {}
	for f in fm["fields"]:
		if f["fieldtype"] == "Table":
			continue
		v = doc.get(f["fieldname"])
		values[f["fieldname"]] = "" if v is None else v
	tables = {}
	for tf, spec in fm["tables"].items():
		cols = [c["fieldname"] for c in spec["columns"]]
		tables[tf] = [{c: row.get(c) for c in cols} for row in (doc.get(tf) or [])]
	return {
		"ok": True,
		"doctype": doctype,
		"name": name,
		"docstatus": int(doc.docstatus or 0),
		"values": values,
		"tables": tables,
	}


# apply_action is the human-authored EDIT path only: the human deliberately
# changes values in the draft panel and applies them under their own session.
# The confirm-as-proposed verbs run the payload the MODEL proposed, so they must
# route through the token gate (confirm_tool), never here.
_EDIT_VERBS = {"create", "update"}
_CONFIRM_VERBS = {"submit", "cancel", "delete", "amend"}
_RECEIPT = {"create": "Created", "update": "Updated"}


def _require_own_conversation(conversation: str) -> None:
	owner = frappe.db.get_value(CONV, conversation, "owner")
	if not owner:
		raise InvalidArgumentError("unknown conversation")
	if owner != frappe.session.user:
		frappe.throw(_("Not your conversation."), frappe.PermissionError)


def _owns_conversation(conversation: str) -> bool:
	"""Soft ownership check: True iff the current session user owns
	``conversation``. Used to gate the conversation-less-token receipt +
	continuation attach (F1): the fallback ``passed_conv`` is client-supplied, so
	a caller could otherwise point a confirm/dismiss of their OWN conversation-
	less token at another user's conversation and inject a receipt chip +
	continuation turn there. Unlike ``_require_own_conversation`` this returns
	False instead of raising - the write has already executed, so a non-owned
	target must be skipped gracefully, not turned into a post-write 500."""
	return bool(conversation) and frappe.db.get_value(CONV, conversation, "owner") == frappe.session.user


def _receipt_text(verb: str, doctype: str, name: str, submitted: int = 0) -> str:
	if verb == "create" and submitted:
		return f"Created and submitted {doctype} {name}."
	return f"{_RECEIPT[verb]} {doctype} {name}."


def _append_receipt(conversation: str, verb: str, doctype: str, name: str, args: dict, text: str) -> None:
	"""Tool message first (feeds the SPA's docRefs → the receipt's doc id
	linkifies to Desk), then a short assistant receipt the agent also sees in
	the transcript on its next turn - so it never re-applies the change."""
	frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": conversation,
			"seq": _next_seq(conversation),
			"role": "tool",
			"streaming": 0,
			"tool_name": f"{verb}_doc",
			"tool_args": frappe.as_json(args),
			"tool_result": frappe.as_json({"ok": True, "data": {"doctype": doctype, "name": name}}),
			"tool_status": "completed",
			# This row DID come from a confirmation card - the human pressed Confirm on
			# the draft - so mark it and let the SPA render the same receipt chip (with
			# its open-in-Desk shortcut) that the gated path gets. Without this it fell
			# into the Activity accordion, so a confirmed DELETE offered a shortcut to a
			# record that no longer exists while a confirmed CREATE offered none to a
			# record that does.
			"action_outcome": "confirmed",
		}
	).insert(ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": conversation,
			"seq": _next_seq(conversation),
			"role": "assistant",
			"content": text,
			"streaming": 0,
		}
	).insert(ignore_permissions=True)
	frappe.db.set_value(CONV, conversation, "last_active_at", frappe.utils.now(), update_modified=False)


@frappe.whitelist()
@require_jarvis_user
def apply_action(action: dict | str | None = None) -> dict:
	"""Apply a human-authored draft-panel edit: create or update ONLY, with the
	values the human deliberately entered before applying. Runs as the session
	user; the mutation goes through the existing tool (its permission and
	protected-field checks fire unchanged), is audited as a human-authored write,
	and leaves a receipt in the conversation.

	The confirm-as-proposed verbs (submit/cancel/delete/amend) run the payload
	the MODEL proposed, so they are NOT accepted here; they route through the
	token gate (``confirm_tool``). ``conversation`` is mandatory and always
	owner-checked: an apply can only ever act inside the caller's own
	conversation."""
	a = frappe.parse_json(action) if isinstance(action, str) else (action or {})
	verb = (a.get("verb") or "").strip()
	doctype = (a.get("doctype") or "").strip()
	name = (a.get("name") or "").strip()
	conversation = (a.get("conversation") or "").strip()
	values = a.get("values") or {}
	do_submit = int(a.get("submit") or 0)
	# The model marks a card "continue": 1 when it is a non-final step of a
	# multi-step plan; the SPA forwards it. Only effect: one follow-up agent
	# turn in the caller's own conversation - no extra write authority.
	do_continue = int(a.get("continue") or 0)
	if verb in _CONFIRM_VERBS:
		raise InvalidArgumentError(
			f"{verb!r} is a confirm-as-proposed action; approve it through the "
			"confirmation card, not the draft-edit path."
		)
	if verb not in _EDIT_VERBS:
		raise InvalidArgumentError(f"unsupported verb {verb!r}")
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not conversation:
		raise InvalidArgumentError("conversation is required")
	_require_own_conversation(conversation)

	# A Prompt-autonamed DocType (e.g. Server Script) has NO `name` FIELD - the
	# human-entered document name arrives only as `name`, never inside `values`
	# (the panel builds values from DocFields). Fold it in for a create so the doc
	# gets a name; without this the insert fails "Please set the document name".
	# That error is raised ONLY by _prompt_autoname (frappe/model/naming.py), for
	# autoname.startswith("prompt") - i.e. "Set by user". Every other naming rule
	# either derives the name from a field already in `values` (By fieldname / By
	# Naming Series) or auto-generates it (Autoincrement / Expression / Random /
	# UUID / By script), so it needs no fold and folding could override it. Match
	# Frappe's own check exactly so the scope stays correct.
	if verb == "create" and name and "name" not in values:
		if (frappe.get_meta(doctype).autoname or "").lower().startswith("prompt"):
			values = {**values, "name": name}

	from jarvis import api

	# The audit/receipt args, built up front (independent of the write outcome);
	# a create fills in its real `name` after insert. For create the args are
	# {doctype, values}; for update {doctype, name, changes}.
	args = (
		{"doctype": doctype, "values": values}
		if verb == "create"
		else {"doctype": doctype, "name": name, "changes": values}
	)

	# Surface a failed apply through the SAME {ok:false, error} envelope the
	# model/confirm paths use (rich detail + hint), instead of leaking Frappe's
	# raw 403/417 to the SPA. ``mark`` lets _translate_write_error harvest only
	# the reason THIS write logged.
	mark = api._msglog_mark()
	try:
		if verb == "create":
			from jarvis.tools.create_doc import create_doc

			res = create_doc(doctype, values)
			name = res.get("name")
			if do_submit:
				# Submit of the JUST-created draft the human authored (the same
				# payload they saw) - low risk, kept as part of the draft-editor UX.
				from jarvis.tools.submit_doc import submit_doc

				submit_doc(doctype, name)
		else:  # update
			from jarvis.tools.update_doc import update_doc

			update_doc(doctype, name, values)
	except Exception as e:
		envelope = api._translate_write_error(e, mark)
		if envelope is None:
			# Unexpected - audit + re-raise so a real bug still surfaces as a 500
			# (never enveloped, never leaks a traceback to the client).
			audit.record(
				tool=f"apply_action.{verb}_doc",
				args=args,
				ok=False,
				error_code=type(e).__name__,
				error_message=str(e),
			)
			raise
		# A RETURNED envelope makes Frappe commit at end-of-request; roll back so
		# a partial create+submit (create ok, submit failed) leaves NO changes -
		# the SPA's "No changes were saved" line stays truthful.
		frappe.db.rollback()
		err_obj = envelope["error"]
		audit.record(
			tool=f"apply_action.{verb}_doc",
			args=args,
			ok=False,
			error_code=err_obj["code"],
			error_message=err_obj["message"],
		)
		return envelope

	# Audit as a human-authored write, distinct from a model tool call. The
	# actor (frappe.session.user) is captured by audit.record; the tool label
	# marks the human-edit origin.
	audit.record(
		tool=f"apply_action.{verb}_doc", args=args, ok=True, result={"doctype": doctype, "name": name}
	)

	frappe.db.commit()
	receipt = _receipt_text(verb, doctype, name, do_submit)
	try:
		_append_receipt(conversation, verb, doctype, name, args, receipt)
		frappe.db.commit()
	except Exception:
		# The mutation is already committed - a receipt hiccup must not
		# report failure (the SPA would retry and duplicate the create).
		frappe.log_error(title="apply_action receipt failed", message=frappe.get_traceback())
	# SUX-3: acknowledge the synchronous write IMMEDIATELY ("Change saved"),
	# decoupled from the reply queue - the continuation turn below then renders
	# the standard queued chip with its position. Flag-gated + best-effort.
	from jarvis.chat import admission

	admission.publish_action_confirmed(conversation)
	_cont = None
	if do_continue:
		try:
			_cont = enqueue_continuation(conversation, receipt)
		except Exception:
			# Best-effort like the receipt: the write is committed, and the
			# user can always nudge the agent manually if dispatch hiccups.
			frappe.log_error(title="apply_action continuation failed", message=frappe.get_traceback())
	slug = doctype.lower().replace(" ", "-")
	resp = {"ok": True, "verb": verb, "name": name, "doc_url": f"/app/{slug}/{name}"}
	# SUX-3/SUXI-2: when the continuation turn queued (all slots taken), thread
	# its run_id + position so the SPA renders the standard queued chip instead
	# of the card vanishing into silence after the "Change saved" ack clears.
	if _cont and _cont.get("queued"):
		resp["queued"] = True
		resp["queued_position"] = _cont.get("queued_position")
		resp["run_id"] = _cont.get("run_id")
		resp["message_id"] = _cont.get("message_id")
	return resp


_INVALID_CONFIRM = {
	"ok": False,
	"error": {
		"type": "InvalidConfirmation",
		"message": "This confirmation is no longer valid.",
	},
}

_ARMED_RUN_CONFIRM_WITHDRAWN = {
	"ok": False,
	"error": {
		"type": "InvalidConfirmation",
		"message": "This macro run is stopping; that confirmation was withdrawn and nothing ran.",
	},
}

_CONFIRMATION_UNAVAILABLE = {
	"ok": False,
	"error": {
		"type": "ConfirmationUnavailableError",
		"message": (
			"Confirmation storage is temporarily unavailable. Nothing was changed "
			"by this request. Keep this card and try again shortly."
		),
	},
}

_CONFIRMATION_OUTCOME_UNKNOWN = {
	"ok": False,
	"error": {
		"type": "ConfirmationOutcomeUnknownError",
		"message": (
			"The confirmation token's storage outcome could not be verified. The "
			"business action was not run by this request. Refresh the confirmations "
			"before trying again."
		),
	},
}


# ── "Approve & run" refusals (skill "Approve & run the plan", design §3.3/§3.4) ──
# Stable "nothing changed" envelopes for approve_and_run. Each is returned WITHOUT
# consuming the token (the card stays confirmable the ordinary way), mirroring the
# non-consuming armed-refusal in _confirm_core.
_APPROVE_RUN_NOT_RUNNABLE = {
	"ok": False,
	"error": {
		"type": "InvalidConfirmation",
		"message": (
			"This card can't be approved as a run - confirm the step on its own instead. Nothing was changed."
		),
	},
}

_APPROVE_RUN_NOT_ARMED = {
	"ok": False,
	"error": {
		"type": "InvalidConfirmation",
		"message": (
			"This skill is no longer set up for Approve & run. Nothing was changed - "
			"confirm the step on its own instead."
		),
	},
}

_APPROVE_RUN_MACRO_CONVERSATION = {
	"ok": False,
	"error": {
		"type": "InvalidConfirmation",
		"message": "Approve & run isn't available in an armed macro run. Nothing was changed.",
	},
}

_APPROVE_RUN_NEVER_TOOL = {
	"ok": False,
	"error": {
		"type": "InvalidConfirmation",
		"message": (
			"This step needs its own confirmation and can't be run as part of an approved run. "
			"Nothing was changed - confirm the step on its own instead."
		),
	},
}


def _confirmation_storage_error(exc) -> dict:
	"""Stable user envelope for a Redis failure; never mislabel it as expiry."""
	from jarvis.chat import pending_confirm

	frappe.logger("jarvis.pending_confirm").error(
		"confirmation endpoint stopped before the business action: %s", type(exc).__name__
	)
	if isinstance(exc, pending_confirm.PendingConfirmOutcomeUnknown):
		return _CONFIRMATION_OUTCOME_UNKNOWN
	return _CONFIRMATION_UNAVAILABLE


@frappe.whitelist()
@require_jarvis_user
def confirm_tool(token: str, conversation: str | None = None) -> dict:
	"""Execute a parked mutating tool call after the human clicked Confirm.

	Owner-bound + conversation-bound + single-use via ``pending_confirm``. The
	confirmation gate in ``jarvis.api._run_tool`` parks every gated write and
	stores the authoritative call; this endpoint is the ONLY path that runs it.

	Human cookie-session only (whitelisted, not allow_guest, not the plugin
	path).

	Identity model (issue #186, #1/#5/#6): the gate binds the token to the
	CONVERSATION OWNER - the human whose browser is subscribed and who clicks
	Confirm. That is ``frappe.session.user`` here, so we consume under the
	session user directly. ``consume`` re-validates owner + conversation
	atomically and single-uses the token; a wrong-owner caller learns nothing
	and does NOT burn the token.

	Conversation guard (#11): ``conversation`` is the conversation the click came
	from (the SPA passes its current id). When given it is passed into
	``consume`` as a REAL check (record.conversation must match). When omitted
	(back-compat) the record's own conversation is used - a tautology - so the
	guard reduces to owner + single-use and the conversation check does not
	actually run.

	Execution scope (#6): the confirmed write executes AS the stored
	``exec_user`` (the scoped model-execution identity), so a confirm can never
	exceed the model path's permission scope.
	The switch goes through ``impersonate`` (session-safe), so the confirming
	browser session's sid + data are always restored - a bare ``frappe.set_user``
	would gut the cookie session and log the user out.
	"""
	return _confirm_core(token, conversation)


@frappe.whitelist()
@require_jarvis_user
def approve_and_run(token: str, conversation: str | None = None) -> dict:
	"""Open an APPROVED skill run: confirm step 1 AND arm the conversation's
	``skill_autorun`` flag so the run's covered writes then execute uncarded.

	A SIBLING of ``confirm_tool`` (design §3.3; precedent ``apply_action``), NOT a
	``confirm_tool`` overload. It shares confirm_tool's spine - owner-bound
	single-use ``consume``, execution under the stored ``exec_user``, the transcript
	receipt + one continuation - but adds two things a plain confirm never does:

	  * it is valid ONLY for a card the park-time offer gate stamped with a
	    ``skill_docname`` (an armed ``/slug`` run); a plain card must go through
	    ``confirm_tool``; and
	  * on step-1 SUCCESS it raw-sets ``Jarvis Conversation.skill_autorun=1`` (+ the
	    sliding ``skill_autorun_at``) BEFORE enqueueing the continuation, so the
	    resuming worker's gate runs the plan's covered writes directly. A FAILED
	    first step opens NO run (the flag is never set) - the user re-invokes.

	Fail-closed refusals (correctness-C2 + security) - each returns a stable
	"nothing changed" envelope and consumes NOTHING: a missing token; a token that
	carries no ``skill_docname``; a skill whose ``allow_approve_run`` is no longer 1
	(a live TOCTOU re-check - un-armed between the offer and this click); or a token
	whose conversation is a ``skip_confirmation`` (armed-macro) run (defense-in-
	depth: makes a both-flags conversation unreachable by construction, mirroring the
	non-consuming armed-refusal in ``_confirm_core``).

	Human cookie-session only (whitelisted, gated, not the plugin path).

	NOTE (P0): the step-1 receipt is the STANDARD confirmed/failed receipt shared
	with ``confirm_tool``; the distinct skill-run provenance LABEL (``armed_by``
	skill) is a later task.
	"""
	if frappe.session.user == "Guest":
		raise frappe.PermissionError("authentication required")

	from jarvis import api
	from jarvis.chat import pending_confirm

	# Defensive str() coercion (not just `or ""`): both params are unvalidated
	# client JSON, and a non-string value (a client bug, or a crafted int/dict)
	# would otherwise blow past `or ""` unchanged - it's truthy - and 500 on
	# `.strip()`, matching the codebase's usual unvalidated-client-JSON handling.
	token = str(token or "").strip()
	try:
		record = pending_confirm.peek(token, strict=True)
		if not record:
			return _INVALID_CONFIRM

		# Runnable-offer only: a token with no skill_docname was never offered
		# Approve & run (a plain card goes through confirm_tool). Refuse WITHOUT
		# consuming so the card stays confirmable the ordinary way.
		skill_docname = record.get("skill_docname")
		if not skill_docname:
			return _APPROVE_RUN_NOT_RUNNABLE

		# Covered-tool only (I4): approve_and_run executes the parked write as "step 1"
		# and opens the run behind it. A _SKILL_AUTORUN_NEVER tool (create_custom_skill /
		# delete / cancel / amend) must never be run that way - the offer gate already
		# refuses to stamp one, so a stamped NEVER tool means a bug; defend anyway and
		# refuse WITHOUT consuming (the card stays confirmable through confirm_tool).
		if record.get("tool") not in api._SKILL_AUTORUN_COVERED:
			return _APPROVE_RUN_NEVER_TOOL

		# TOCTOU re-check: the skill must be live-armed RIGHT NOW off the EXACT row
		# the offer stamped (an admin may have un-armed it between the park-time offer
		# and this click). Read live; refuse without consuming when it is not 1.
		if not frappe.db.get_value("Jarvis Custom Skill", skill_docname, "allow_approve_run"):
			return _APPROVE_RUN_NOT_ARMED

		# Defense-in-depth (design §3.4.1): the token's conversation must NOT be an
		# armed macro run (skip_confirmation=1). This closes the endpoint door so a
		# both-flags-in-one-conversation state is unreachable by construction, not
		# merely by UI convention. Refuse without consuming (mirror _confirm_core
		# actions_api armed-refusal), keyed on the flag (the single source of truth).
		token_conv = record.get("conversation")
		if token_conv and frappe.db.get_value("Jarvis Conversation", token_conv, "skip_confirmation"):
			return _APPROVE_RUN_MACRO_CONVERSATION

		# Owner-bound, single-use consume. The OWNER is the real authorization
		# boundary; the conversation is only a SECONDARY replay guard, so NEVER trust
		# a client-supplied conversation id for authz - guard_conv falls back to the
		# token's OWN conversation when the caller passes none (mirror _confirm_core).
		# str() coercion for the same reason as `token` above - unvalidated client JSON.
		passed_conv = str(conversation or "").strip()
		guard_conv = passed_conv if passed_conv else record.get("conversation")
		record = pending_confirm.consume(token, owner=frappe.session.user, conversation=guard_conv)
	except pending_confirm.PendingConfirmStorageError as exc:
		return _confirmation_storage_error(exc)
	if not record:
		return _INVALID_CONFIRM

	# STEP 1: execute the parked write AS the scoped exec_user the gate stored,
	# restoring the clicking session afterwards no matter what (mirror _confirm_core).
	exec_user = record.get("exec_user") or record.get("owner") or frappe.session.user
	try:
		with impersonate(exec_user):
			result = api.dispatch_confirmed(record["tool"], record["args"])
			# run_method returns its target verbatim; strip permlevel>0 fields the agent
			# can't read before they reach the receipt / continuation. Shared with the gate
			# branches (_run_covered_write) + _confirm_core via one helper (I6) so this
			# filter is not triple-maintained. Best-effort - a hiccup never fails the call.
			api._apply_run_method_read_filter(record["tool"], result)
	except Exception:
		# An UNEXPECTED (untranslated) exception from the confirmed write would 500
		# with the token ALREADY consumed. Roll back the partial write, log it, and
		# fall through to the FAILED receipt + continuation (mirror _confirm_core F5).
		# ok stays False below, so the run is NOT opened.
		frappe.db.rollback()
		frappe.log_error(
			title="approve_and_run dispatch crashed",
			message=f"token={token} conversation={guard_conv}\n{frappe.get_traceback()}",
		)
		result = api._error("InternalError", "the confirmed action failed unexpectedly and was not saved")

	ok = isinstance(result, dict) and bool(result.get("ok"))

	# Announce a step-1 run_import's completion back into the chat (mirror
	# _confirm_core). Self-gating + best-effort (no-ops unless tool == run_import +
	# ok); binds to the token's OWN conversation, never the client-supplied conv.
	from jarvis.chat import import_announce

	import_announce.bind_after_run_import(record, result)

	# Open the run ONLY on step-1 success, and BEFORE the continuation so the
	# resuming worker's gate/assemble_prompt sees skill_autorun (design §3.4). Raw
	# db.set_value on the token's OWN conversation is the SANCTIONED enable path that
	# bypasses the owner-save guard (the guard blocks a generic owner save; this is
	# the one legitimate server enabler). A failed first step sets NOTHING.
	run_conv = record.get("conversation")
	if ok and run_conv:
		# Stamp skill_autorun_skill (C2) so the gate can re-read allow_approve_run LIVE
		# off THIS exact armed row before each uncarded covered write - un-arming the
		# skill mid-run then hard-stops the auto-run within one write.
		frappe.db.set_value(
			"Jarvis Conversation",
			run_conv,
			{
				"skill_autorun": 1,
				"skill_autorun_at": frappe.utils.now_datetime(),
				"skill_autorun_skill": skill_docname,
			},
			update_modified=False,
		)
		frappe.db.commit()
		# I1: a leftover run-cancel key from a PRIOR stop_run on this conversation must
		# not halt the run we just opened. Clear it so the fresh run's first covered
		# write dispatches instead of hard-stopping on a stale Halt.
		from jarvis.chat import turn_message_binding

		turn_message_binding.clear_run_cancel(run_conv)

	# Receipt + continuation, exactly as _confirm_core: attach to the token's own
	# conversation, or the client-supplied passed_conv ONLY when the token was minted
	# conversation-less AND the caller owns that conversation (a skill_docname token
	# is always conversation-bound, so the fallback is a belt-and-suspenders mirror).
	# DEFERRED FOLLOW-UP (I6): this settlement TAIL (receipt + continuation) is still
	# duplicated between approve_and_run and _confirm_core. Only the small run_method
	# read-filter + the run_import announce are de-duplicated in this pass; extracting the
	# shared settlement tail is a larger, riskier refactor left for a later change.
	conv = record.get("conversation")
	if not conv and _owns_conversation(passed_conv):
		conv = passed_conv
	if conv:
		try:
			api.persist_tool_receipt(
				conv,
				record["tool"],
				record["args"],
				result,
				action_outcome="confirmed" if ok else "failed",
			)
		except Exception:
			frappe.log_error(
				title="approve_and_run receipt failed",
				message=f"token={token} conversation={conv}\n{frappe.get_traceback()}",
			)
		if ok:
			from jarvis.chat import admission

			admission.publish_action_confirmed(conv)
		# ONE continuation: the plan's next step (on ok) or the rolled-back-write
		# scaffold (on failure - explain + stop, do not auto-retry).
		_cont = None
		try:
			_cont = enqueue_continuation(conv, _confirm_receipt_text(record, result), failed=not ok)
		except Exception:
			frappe.log_error(
				title="approve_and_run continuation failed",
				message=f"token={token} conversation={conv}\n{frappe.get_traceback()}",
			)
		if ok and isinstance(result, dict) and _cont and _cont.get("queued"):
			result["queued"] = True
			result["queued_position"] = _cont.get("queued_position")
			result["run_id"] = _cont.get("run_id")
			result["message_id"] = _cont.get("message_id")

	return result


def _confirm_core(token: str, conversation: str | None = None, *, batch: bool = False) -> dict:
	"""The confirmation itself, with no HTTP surface of its own.

	Two human paths reach it: the Confirm button (``confirm_tool`` above) and a
	typed approval in the composer (``jarvis.chat.api.send_message``). Both are
	the same authenticated session user acting on the same card, so they share
	one implementation rather than one calling the other's whitelisted endpoint.

	Every guarantee stays below this line, not in the callers: Guest refusal,
	owner + conversation binding, the atomic single-use ``consume``, execution
	under the stored ``exec_user``, the transcript receipt and the continuation
	turn. A second entry point therefore cannot weaken the gate, and two racing
	approvals (a click and a typed one) still resolve to exactly one winner.

	``batch``: this card is one of several being approved together. The write, the
	receipt chip and every guard still run per card, so nothing about the gate is
	relaxed; only the follow-up turn is deferred. The receipt line is returned as
	``receipt_text`` for the caller to fold into ONE continuation covering the
	whole batch, instead of N turns queueing against each other.
	"""
	if frappe.session.user == "Guest":
		raise frappe.PermissionError("authentication required")

	from jarvis import api
	from jarvis.chat import pending_confirm

	token = (token or "").strip()
	try:
		record = pending_confirm.peek(token, strict=True)
		if not record:
			return _INVALID_CONFIRM

		# Defense-in-depth (security review): an armed macro-run conversation must have
		# NO human-confirmable card. The only card that parks there is a D5 excluded
		# (delete/cancel/amend) write, which stop-and-report sweeps in advance_after_turn.
		# Refuse a racing Confirm published-at-park before that sweep - WITHOUT consuming
		# the token, so the sweep still clears it - else the excluded write would run and
		# fire an armed continuation turn. Keyed on the flag (the single source of truth);
		# the typed-approval path is already blocked upstream in send_message.
		token_conv = record.get("conversation")
		if token_conv and frappe.db.get_value("Jarvis Conversation", token_conv, "skip_confirmation"):
			return _ARMED_RUN_CONFIRM_WITHDRAWN

		# Real conversation guard (#11): if the caller passed the conversation the
		# click came from, enforce it; otherwise fall back to the record's own
		# conversation (owner + single-use remain the guarantees).
		passed_conv = (conversation or "").strip()
		guard_conv = passed_conv if passed_conv else record.get("conversation")
		record = pending_confirm.consume(token, owner=frappe.session.user, conversation=guard_conv)
	except pending_confirm.PendingConfirmStorageError as exc:
		return _confirmation_storage_error(exc)
	if not record:
		return _INVALID_CONFIRM

	# Execute AS the scoped model-execution identity the gate stored, restoring
	# the confirming session user afterwards no matter what. exec_user defaults
	# to the owner for tokens minted before this field existed.
	exec_user = record.get("exec_user") or record.get("owner") or frappe.session.user
	# impersonate is session-safe: a bare frappe.set_user here would gut the
	# browser's cookie session (sid + data) and log the confirming user out.
	try:
		with impersonate(exec_user):
			# Same envelope + audit as an inline write - dispatch_confirmed bypasses
			# the gate so the stored call actually executes instead of parking again.
			result = api.dispatch_confirmed(record["tool"], record["args"])
			# run_method returns its target verbatim, unlike get_doc/create_doc which
			# permlevel-filter before as_dict. Strip permlevel>0 fields the agent can't read
			# from a Document return HERE (still as exec_user), before they reach the receipt
			# chip or the model's continuation dump. Shared with the gate branches
			# (_run_covered_write) + approve_and_run via one helper (I6) so this filter is
			# not triple-maintained. Best-effort - a hiccup never fails the committed call.
			api._apply_run_method_read_filter(record["tool"], result)
	except Exception:
		# F5: an UNEXPECTED (untranslated) exception from the confirmed write would
		# otherwise 500 with the token ALREADY consumed (GETDEL above) - no receipt,
		# no continuation, the agent stuck at "awaiting confirmation" and the user
		# unable to retry. _dispatch_and_wrap re-raises such exceptions with its
		# savepoint still open, so roll back the partial write, log it, and fall
		# through to a graceful failure envelope: the "failed" receipt + continuation
		# below still fire, so the user sees it and the agent learns.
		frappe.db.rollback()
		frappe.log_error(
			title="confirm dispatch crashed",
			message=f"token={token} conversation={guard_conv}\n{frappe.get_traceback()}",
		)
		result = api._error("InternalError", "the confirmed action failed unexpectedly and was not saved")

	# Slice B: bind a Jarvis Import Announcement so the import's completion is
	# announced back into this chat unprompted. Best-effort + self-gating (tool ==
	# run_import + ok); binds to the token's OWN guarded conversation (PC-4), never
	# the client-supplied passed_conv.
	from jarvis.chat import import_announce

	import_announce.bind_after_run_import(record, result)

	# Leave a transcript receipt (#7) so a confirmed delete/submit/email shows on
	# reload, matching the inline model-write path's tool card. Best-effort: the
	# write already committed, so a receipt hiccup must not report failure.
	# Attach to the conversation the click came from (``passed_conv``) when the
	# token itself was minted conversation-less (F1: a session_key lookup miss),
	# but only when the caller OWNS that
	# conversation (passed_conv is client-supplied - never inject into another
	# user's chat). A true headless caller with no owned conversation just skips.
	conv = record.get("conversation")
	if not conv and _owns_conversation(passed_conv):
		conv = passed_conv
	if conv:
		ok = isinstance(result, dict) and bool(result.get("ok"))
		# Leave a durable receipt CHIP (#7 / receipt-chips): action_outcome makes
		# the SPA render it inline as "✓ confirmed" / "✗ failed" instead of a
		# buried Activity-accordion row, so the confirmation card is replaced by a
		# persistent summary rather than vanishing.
		try:
			api.persist_tool_receipt(
				conv,
				record["tool"],
				record["args"],
				result,
				action_outcome="confirmed" if ok else "failed",
			)
		except Exception:
			frappe.log_error(
				title="confirm receipt failed",
				message=f"token={token} conversation={conv}\n{frappe.get_traceback()}",
			)

		# Continue the agent's plan: the model was told only "awaiting the
		# user's confirmation" and stopped, so without this turn it never
		# sees the real outcome (or continues a multi-step request). Always
		# dispatched on the confirm path - there is no card to carry a
		# continue flag here, and the post-write acknowledgment is part of
		# the persona's write recipes. On failure the rolled-back-write scaffold
		# makes the agent explain + stop instead of auto-retrying. Best-effort.
		# SUX-3: on a SUCCESSFUL confirm, acknowledge the write immediately so
		# the card doesn't vanish into silence while the continuation queues.
		# A failed confirm skips this - the failed scaffold explains instead.
		if ok:
			from jarvis.chat import admission

			admission.publish_action_confirmed(conv)
		# In a batch the caller owns the follow-up: N cards approved in one breath
		# must produce ONE continuation carrying all N receipts, not N turns racing
		# each other through admission. The receipt line rides out on the envelope
		# so the caller can compose them.
		if batch:
			if isinstance(result, dict):
				result["receipt_text"] = _confirm_receipt_text(record, result)
			return result
		_cont = None
		try:
			_cont = enqueue_continuation(conv, _confirm_receipt_text(record, result), failed=not ok)
		except Exception:
			frappe.log_error(
				title="confirm continuation failed",
				message=f"token={token} conversation={conv}\n{frappe.get_traceback()}",
			)
		# SUX-3/SUXI-2: thread the queued continuation's position onto a SUCCESSFUL
		# confirm result so the SPA shows the queued chip (the card doesn't vanish
		# into silence while the continuation sits queued). A failed confirm keeps
		# its error envelope untouched.
		if ok and isinstance(result, dict) and _cont and _cont.get("queued"):
			result["queued"] = True
			result["queued_position"] = _cont.get("queued_position")
			result["run_id"] = _cont.get("run_id")
			result["message_id"] = _cont.get("message_id")

	return result


def _confirm_receipt_text(record: dict, result) -> str:
	"""Short receipt line for the post-confirm continuation prompt: the call,
	the created/affected record name when the result carries one, and the
	outcome (including a bounded error message so the agent can react).

	run_method is the exception. It is the generic escape hatch for
	data-returning whitelisted methods (getters, the make_* mappers), so its
	whole value IS the returned payload - and because it is a gated write it
	ALWAYS parks, making this receipt the only way its result reaches the model.
	A plain "<call> succeeded" leaves the agent blind to the data it was asked
	to use, so for a SUCCESSFUL run_method we append the FULL returned payload,
	deliberately UNTRUNCATED - the agent needs all of it to act on the result.
	It is serialized with frappe.as_json (the same encoder the inline tool path
	uses at the HTTP boundary), which renders a returned Document via as_dict and
	datetimes as ISO strings - where stdlib json.dumps(default=str) would emit a
	useless repr for the make_* mappers' Document returns. Safe despite the
	payload being attacker-influenceable: enqueue_continuation runs the whole
	receipt through _safe_label_name (all whitespace - including any pretty-print
	newlines - collapsed to single spaces, backticks disarmed) and quotes it as
	inline-code DATA, so it can neither forge the [System] voice nor break out of
	the code span - the same neutralization the record-name receipt relies on.
	Serialization never raises out of here (the write already committed): an
	exotic or circular return value falls back to a plain success line."""
	from jarvis.api import _describe_call

	is_run_method = record.get("tool") == "run_method"
	desc = _describe_call(record.get("tool") or "", record.get("args") or {})
	data = result.get("data") if isinstance(result, dict) else None
	# The name-append keeps write receipts terse; run_method dumps the whole
	# payload below (which already carries any name), so skip it there.
	if not is_run_method and isinstance(data, dict) and data.get("name"):
		desc += f" -> {data['name']}"
	if isinstance(result, dict) and not result.get("ok"):
		err = result.get("error") or {}
		msg = str(err.get("message") or "")[:200] if isinstance(err, dict) else ""
		return f"{desc} FAILED. {msg}".strip()
	if is_run_method:
		try:
			payload = frappe.as_json(data)
		except Exception:
			# Post-commit + best-effort, but not silent: log so a method whose return
			# consistently fails to serialize is diagnosable instead of vanishing.
			frappe.log_error(
				title="run_method receipt serialization failed",
				message=frappe.get_traceback(),
			)
			return f"{desc} succeeded (return value could not be serialized for the receipt)."
		return f"{desc} succeeded. Returned: {payload}"
	return f"{desc} succeeded."


def _dismiss_note(tool: str, args: dict) -> str:
	"""The deferred agent-correction note for a discarded action: bench truth
	(not user speech) that overrides the stale ``pending_confirmation`` result
	still sitting in the agent's in-container session memory. Folded into the
	NEXT turn's ``[Context: ...]`` bracket by turn_handler, so no extra agent
	turn fires now."""
	from jarvis.api import _describe_call

	return (
		f"the user declined the pending action ({_describe_call(tool, args)}); "
		"it was NOT performed - do not assume it ran, and do not retry unless asked"
	)


@frappe.whitelist()
@require_jarvis_user
def dismiss_tool(token: str, conversation: str | None = None) -> dict:
	"""Discard a parked gated write after the human clicked Discard.

	Owner-bound + single-use exactly like ``confirm_tool`` but it runs NOTHING:
	it consumes the token (closing the 15-min replay window and stopping the card
	from re-surfacing on reload), leaves a durable "discarded" receipt chip in the
	transcript, and queues a deferred note so the agent's next turn learns the
	action was vetoed - the bench never replays tool rows to the agent, so a
	persisted row alone would not reach its in-container memory. Fires NO agent
	turn: the user just said no; let them speak next.

	A benign no-op when the token is already consumed/expired (a Confirm in
	another tab won the race, or the 15-min TTL lapsed): returns ok with
	``already_handled`` so the SPA silently drops the card. Human cookie-session
	only.
	"""
	if frappe.session.user == "Guest":
		raise frappe.PermissionError("authentication required")

	from jarvis import api
	from jarvis.chat import pending_confirm

	token = (token or "").strip()
	try:
		record = pending_confirm.peek(token, strict=True)
		if not record:
			return {"ok": True, "data": {"status": "already_handled"}}

		# Same owner + conversation binding as confirm_tool: consume atomically so a
		# concurrent Confirm and Discard cannot both win.
		passed_conv = (conversation or "").strip()
		guard_conv = passed_conv if passed_conv else record.get("conversation")
		record = pending_confirm.consume(token, owner=frappe.session.user, conversation=guard_conv)
	except pending_confirm.PendingConfirmStorageError as exc:
		return _confirmation_storage_error(exc)
	if not record:
		return {"ok": True, "data": {"status": "already_handled"}}

	tool = record.get("tool") or ""
	args = record.get("args") or {}
	# Attach the discarded chip + veto note to the conversation the click came
	# from when the token was minted conversation-less (F1), but only when the
	# caller OWNS that conversation (passed_conv is client-supplied).
	conv = record.get("conversation")
	if not conv and _owns_conversation(passed_conv):
		conv = passed_conv
	if conv:
		# Durable "discarded" chip: what the user declined, in their transcript.
		try:
			api.persist_tool_receipt(conv, tool, args, None, action_outcome="discarded")
		except Exception:
			frappe.log_error(title="dismiss_tool receipt failed", message=frappe.get_traceback())
		# Correct the agent's stale pending_confirmation memory on its next turn.
		try:
			from jarvis.chat import agent_notes

			agent_notes.append(conv, _dismiss_note(tool, args))
		except Exception:
			frappe.log_error(title="dismiss_tool note failed", message=frappe.get_traceback())
		# Dismissing the paused card ENDS any approved skill run (skill "Approve & run",
		# design §3.4): the user declined the paused destructive / create_custom_skill
		# step, so the run is over. Guarded on the flag so an ordinary discard does no
		# needless write; clear_skill_autorun is itself best-effort.
		if frappe.db.get_value("Jarvis Conversation", conv, "skill_autorun"):
			from jarvis.chat import turn_message_binding

			turn_message_binding.clear_skill_autorun(conv)

	return {"ok": True, "data": {"status": "discarded", "tool": tool}}


@frappe.whitelist()
@require_jarvis_user
def list_pending_confirmations(conversation: str | None = None) -> dict:
	"""Re-surface the caller's OWN currently-parked confirmation cards after a
	reload/reconnect (issue #186, enables R3's fix for #3).

	Owner-scoped: returns only the calling user's live parked tokens (never
	another user's), optionally filtered to ``conversation``. Each item carries
	exactly what the ``action:pending`` realtime event already delivers to this
	same owner's UI - token + tool + preview + summary + conversation + run_id -
	so no new information is leaked. Human cookie-session only.
	"""
	if frappe.session.user == "Guest":
		raise frappe.PermissionError("authentication required")

	from jarvis.chat import pending_confirm

	conv = (conversation or "").strip() or None
	# Same client-facing item shape the action:pending event and the run:end
	# terminal use (pending_confirm.list_items_for_owner) so the three cannot drift
	# and no internal field leaks. The park-time preview is returned verbatim (F2 -
	# never a re-run dry-run) and the per-record F3 guard both live in the helper.
	try:
		items = pending_confirm.list_items_for_owner(frappe.session.user, conversation=conv, strict=True)
	except pending_confirm.PendingConfirmStorageError as exc:
		return _confirmation_storage_error(exc)
	return {"ok": True, "data": {"pending": items}}
