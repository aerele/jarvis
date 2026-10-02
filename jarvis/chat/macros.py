"""Macro execution engine.

A macro (``Jarvis Macro``) is an ordered list of prompts. Running one creates a
fresh conversation and executes each prompt as its own agent turn, chained
server-side: every turn that ends calls :func:`advance_after_turn` (from
``finalize``, ``turn_handler`` and ``turn_recovery``), and when the turn that ended
is the run's CURRENT STEP the hook enqueues the next one, or ends the run. Other
turns in the conversation (a typed message, a confirmation card's follow-up) do not
move it. Because the steps run as turns in one conversation they share the
agent session, so context accumulates across the macro — and the run survives
a closed browser tab (unlike a frontend-driven loop).

State lives in ``Jarvis Macro Run`` (``current_step`` = 1-based index of the step
last started; ``status`` in queued/running/completed/failed/stopped; ``error`` = why
a run failed or stopped, or a plain note on a completed one). Progress is pushed to
the owner's realtime channel as ``macro:progress`` / ``macro:done``, and a run that
ends with something to say leaves it as the last message of its conversation.

A run also records the SHAPE it was dispatched in (``run_mode``, #470). Everything
that re-enters a live run reads that snapshot rather than re-deriving the shape from
the macro, because the macro is editable while the run is in flight.
"""

import hashlib
import re
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from jarvis.chat.events import publish_to_user

MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"

# CDX-19: how many capacity-resume cron cycles a step may be deferred before the run
# fails honestly. The resume cron runs every 5 min, so ~20 attempts ≈ 100 min of sustained
# site overload before giving up — far longer than any real transient backpressure.
_MAX_CAPACITY_ATTEMPTS = 20

# CDX-22: how long stop_macro_run waits for the SAME per-run lock the capacity-resume critical
# section holds. The resume section is a few DB writes + an enqueue (milliseconds), so this is
# ample headroom; on the pathological miss the state-fenced CAS in the resume path is the hard
# backstop. Module-level so tests can shorten it.
_STOP_LOCK_BLOCK_S = 10.0

# #471: how long a run may sit ``running`` WITHOUT FORWARD PROGRESS before the sweep
# terminalizes it. Sized against the longest a SINGLE step can legitimately take without
# writing the run row, which is NOT just the RQ turn cap. Worst legitimate case, traced
# end to end:
#     api._AGENT_TURN_WORKER_TIMEOUT   720 s   RQ worker envelope for one turn
#   + stale_scan promotion             720 s   MANAGED_RECOVER_AFTER_SECONDS, on a */5 cron
#   + turn_recovery._RECOVERY_CEILING_MINUTES  60 min a turn may sit `recovering`
#   + the next */2 recovery tick
#   ~= 70-80 min with ZERO writes to Jarvis Macro Run, because recovery works on the chat
#      MESSAGE and only touches the run when it finalizes into advance_after_turn.
# 3 h is therefore roughly a 2.5x margin, not the 12x you get by counting the turn cap
# alone. Do NOT tighten this below ~2 h without re-tracing those four numbers; the
# recovery ceiling, not the worker timeout, is the binding constraint. Module-level so
# tests can shorten it.
STALE_RUN_AFTER_SECONDS = 3 * 3600
_STALE_RUN_ERROR = "The run stopped making progress and was closed by the stale-run sweep. Run it again."

# #468: the monthly budget for UNATTENDED macro work, per owner. Counted in STEPS,
# not runs: one run is up to MAX_STEPS (25) agent turns, so a run count would
# under-bound the real spend by 25x. Read from Jarvis Settings at run time (not a
# constant) so a bench admin can raise it without a code change, and floored at
# MIN_ so a misconfigured 0/blank can never wedge every schedule for a month.
#
# The uncapped ceiling this replaces: MAX_MACROS_PER_OWNER (25) x MAX_STEPS (25) at
# the shortest cadence = 625 turns/day/owner, ~19k/month, times the number of users
# on the bench. The default leaves room for a couple of daily multi-step macros
# plus headroom; the floor is a full daily schedule of a single-step macro.
DEFAULT_SCHEDULED_STEP_BUDGET_MONTHLY = 500
MIN_SCHEDULED_STEP_BUDGET_MONTHLY = 31

# The machine reason ``run_macro`` reports when the budget is spent. The rest come
# straight from ``policy.validate_can_send`` so a macro and a typed message speak
# the same vocabulary.
BLOCK_STEP_BUDGET = "scheduled_step_budget"

# #471: reported when the first turn's dispatch RAISED after the conversation and run
# row were already committed. Distinct from the codes above because the run row exists
# and has already been terminalized, so the scheduler must not record a second one.
BLOCK_DISPATCH_FAILED = "dispatch_failed"
_DISPATCH_FAILED_ERROR = "The agent turn could not be dispatched, so this run never started."

# #470: the two shapes a run can be dispatched in, snapshotted on the run row at insert.
MODE_STEPPED = "stepped"
MODE_MERGED = "merged"

# #470: what the owner is told when a mid-park macro edit made the run's dispatch-time
# shape unrunnable. Ending the run is the honest outcome: the alternatives are executing
# the OLD summary the user just replaced, or silently switching shape mid-flight.
_MACRO_CHANGED_ERROR = (
	"The macro was edited while this run was waiting for capacity, so the run could no "
	"longer continue the way it started. Run it again."
)
# The capacity resume is a cron: its turn never binds a disabled user, Administrator or Guest.
_OWNER_INELIGIBLE_ERROR = "The run's owner can no longer run unattended work, so the run was closed."
_NO_CAPACITY_ERROR = "The site stayed busy — the macro could not get capacity to run this step."

# Human sentences for the MANUAL path (thrown, so the SPA's existing toast renders
# them). The scheduled path reports the machine code instead and the scheduler
# writes its own owner-facing sentence onto the failed run row.
_BLOCK_MESSAGE = {
	"usage_limit": "You have reached your monthly usage limit, so this macro cannot run.",
	"subscription_suspended": "Your subscription does not currently include chat, so this macro cannot run.",
	"release_update_required": "A Jarvis update is rolling out. Try this macro again in a few minutes.",
	"workspace_resetting": "Your workspace is being rebuilt. Try this macro again in a few minutes.",
	"maintenance": "Jarvis is being upgraded. Try this macro again in a few minutes.",
	"llm_not_configured": "Connect an AI model before running a macro.",
	BLOCK_STEP_BUDGET: "This month's budget for scheduled macro runs is used up.",
	BLOCK_DISPATCH_FAILED: "This macro could not be started. Please try again in a few minutes.",
}


def _run_cas(sql: str, params: dict) -> int:
	"""Run a CAS UPDATE and return affected rows (read BEFORE commit — a commit can reset the
	cursor). Mirrors ``admission._run_cas``."""
	frappe.db.sql(sql, params)
	cursor = getattr(frappe.db, "_cursor", None)
	return int(cursor.rowcount) if cursor else 0


def _cas_run_status(run_name: str, expect: str, new: str, **extra) -> bool:
	"""State-fenced status transition on ``Jarvis Macro Run``: flip ``expect`` -> ``new`` in ONE
	UPDATE, returning True iff it matched exactly one row. The fence closes the CDX-22/CDX-23 races
	where a stop (or a compensating restore) must not clobber a concurrent transition — a run that
	already moved off ``expect`` (e.g. a stop set ``stopped``) matches 0 rows and the caller aborts.
	Extra columns (``capacity_attempts``/``finished_at``/``error``) are set atomically in the same
	statement. Caller commits."""
	sets = ["status=%(new)s", "modified=%(now)s"]
	params = {"n": run_name, "expect": expect, "new": new, "now": frappe.utils.now()}
	for i, (col, val) in enumerate(extra.items()):
		sets.append(f"`{col}`=%(x{i})s")
		params[f"x{i}"] = val
	ok = (
		_run_cas(
			f"UPDATE `tab{RUN}` SET {', '.join(sets)} WHERE name=%(n)s AND status=%(expect)s",
			params,
		)
		== 1
	)
	# Clear the armed flag on every terminal transition driven through the CAS
	# (reap, resume-incompatible compensation, ...) - not only via _finish (T5).
	if ok and new in _TERMINAL_RUN_STATUSES:
		_disarm_run_conversation(run_name)
	return ok


def _run_status_now(run_name: str) -> str | None:
	"""The run's CURRENT durable status (its own function so the CDX-22 pre-enqueue eligibility
	re-check is a clean, patchable seam)."""
	return frappe.db.get_value(RUN, run_name, "status")


_TERMINAL_RUN_STATUSES = ("completed", "failed", "stopped")


def _disarm_conversation(conversation: str | None) -> None:
	"""Clear an armed run conversation's ``skip_confirmation`` when its run reaches a
	terminal state, so the flag never outlives the run. Belt-and-suspenders on top of
	the human-inert send-block (T4): the send-block already keeps a lingering flag
	un-exploitable (send/retry are refused while set), but clearing keeps state clean
	and stops any re-dispatched turn from running armed after the run is over. No-op
	when unset. Caller commits (consistent with ``_cas_run_status``).

	The run's cards are swept FIRST, while the flag still refuses a Confirm: cards
	never expire, so one a stop, the stale reaper or a store blip left behind would
	otherwise stay confirmable forever (the sweep commits the caller's work first).
	A card the sweep left live keeps the flag (fail closed)."""
	if not conversation or not frappe.db.get_value(CONV, conversation, "skip_confirmation"):
		return
	from jarvis import api as _jarvis_api
	from jarvis.chat import pending_confirm

	owner = frappe.db.get_value(CONV, conversation, "owner")
	pending_confirm.clear_for_conversation(owner, conversation)
	try:
		_jarvis_api.cancel_pending_action_rows(conversation)
	except Exception:
		frappe.log_error(title="jarvis.macro.disarm_sweep_failed", message=frappe.get_traceback())
	if pending_confirm.has_live_card(owner, conversation):
		frappe.log_error(
			title="jarvis.macro.disarm_withheld", message=f"{conversation}: a card outlived the sweep"
		)
		return
	frappe.db.set_value(CONV, conversation, "skip_confirmation", 0, update_modified=False)


def _disarm_run_conversation(run_name: str) -> None:
	"""Disarm by run name (for the _cas terminal paths, which only have the run)."""
	_disarm_conversation(frappe.db.get_value(RUN, run_name, "conversation"))


def refuse_acting_for_barred_owner(owner: str) -> None:
	"""Someone other than the owner is starting work that runs AS the owner (a run, a
	summary). The owner is not at the keyboard, so this is an unattended turn and the
	scheduler's two identity checks apply (#469, ``macro_scheduler._sweep_one``):
	never for Administrator, Guest, a disabled account, or one without Jarvis access."""
	from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

	if owner == frappe.session.user:
		return
	if is_valid_unattended_owner(owner) and has_jarvis_access(owner):
		return
	frappe.throw(
		_(
			"This macro's owner ({0}) can no longer use Jarvis, so it cannot be run or summarized for them."
		).format(owner),
		frappe.PermissionError,
	)


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def run_macro(macro_name: str, *, trigger: str = "manual") -> dict:
	"""Start a macro: create a fresh conversation, a Macro Run, and enqueue the
	first step. Returns ``{ok, data:{macro_run, conversation}}``. ``trigger`` is
	``manual`` (user clicked Run) or ``scheduled`` (the cron)."""
	doc = frappe.get_doc(MACRO, macro_name)
	doc.check_permission("read")  # get_doc alone doesn't enforce if_owner
	# Owner-gate. A run executes as the macro's owner (the rows below are handed to
	# them), uncarded when the macro is armed. On read alone, anyone who could only
	# SEE a macro (oversight, a read share) chose when its owner's steps ran. Checked
	# on the owner directly, like stop_macro_run, not through "write": an owner's
	# write comes from the Jarvis User role row, and an owner who holds another Jarvis
	# role instead must still be able to run their own macro. The scheduler passes: it
	# runs as the owner.
	if frappe.session.user not in (doc.owner, "Administrator"):
		frappe.throw(_("Only a macro's owner can run it."), frappe.PermissionError)
	refuse_acting_for_barred_owner(doc.owner)
	steps = doc.steps or []
	if not steps:
		frappe.throw(_("This macro has no steps."))
	if trigger == "scheduled" and not doc.enabled:
		return {"ok": False, "reason": "macro disabled"}
	if (doc.merge_status or "") == "pending" and trigger != "scheduled":
		frappe.throw(_("Still summarizing this macro — try again in a few seconds."))
	owner = doc.owner
	# A stored merged prompt (the background LLM summary) runs as ONE turn
	# instead of chaining the steps — the steps stay as the editable source
	# the summary was made from. merge_status "failed"/unmergeable falls back
	# to the sequence (an unmergeable sequence NEEDS its checkpoints).
	merged = (doc.merged_prompt or "").strip()
	total = 1 if merged else len(steps)

	# #468: the entitlement + unattended-spend gate, BEFORE anything is created —
	# a refused run must leave no conversation, no intro message and no run row.
	blocked = entitlement_block(owner, steps=total, trigger=trigger)
	if blocked:
		if trigger == "scheduled":
			# The scheduler decides what a refusal costs: it records the failure
			# against the owner and works out whether the slot is consumed.
			return {"ok": False, "reason": blocked}
		frappe.throw(_(_BLOCK_MESSAGE.get(blocked, "This macro cannot run right now.")))

	# Fresh conversation titled after the macro, seeded with an intro so the
	# transcript reads as a self-contained run. agent_initiated: that run log is
	# not a chat session the user chose to start (and unlike skip_confirmation,
	# this marker is NOT cleared when the run ends).
	conv = frappe.get_doc(
		{"doctype": CONV, "title": doc.macro_name[:140], "status": "Active", "agent_initiated": 1}
	)
	conv.flags.ignore_permissions = True
	conv.insert()
	intro = frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": conv.name,
			"seq": 1,
			"role": "assistant",
			"content": (
				f"▶ Running macro **{doc.macro_name}** — summarized prompt."
				if merged
				else f"▶ Running macro **{doc.macro_name}** — {len(steps)} step(s)."
			),
		}
	)
	intro.flags.ignore_permissions = True
	intro.insert()

	run = frappe.get_doc(
		{
			"doctype": RUN,
			"macro": doc.name,
			"conversation": conv.name,
			"status": "running",
			"current_step": 0,
			"total_steps": total,
			# #470: the shape is decided HERE, once, and every later re-entry into this
			# run reads it back off the row. It used to be re-derived from the live macro
			# on each dispatch, so a macro edited while the run sat parked for capacity
			# flipped the run's shape mid-flight.
			"run_mode": MODE_MERGED if merged else MODE_STEPPED,
			"trigger": trigger,
			"started_at": frappe.utils.now(),
		}
	)
	run.flags.ignore_permissions = True
	run.insert()
	if trigger != "scheduled":
		# ``last_run_at`` is what the list sorts "Last run" by. The scheduler stamps it
		# for a slot it consumed (and deliberately not for one that never ran, #471); a
		# manual run never stamped it, so a macro run by hand sorted as never run.
		frappe.db.set_value(MACRO, doc.name, "last_run_at", frappe.utils.now(), update_modified=False)

	# When run on behalf of another user (the scheduler runs as the owner, but be
	# defensive), hand ownership of the owner-scoped rows over so they're visible
	# to the intended user only.
	if owner != frappe.session.user:
		for dt, name in ((CONV, conv.name), (MSG, intro.name), (RUN, run.name)):
			frappe.db.set_value(dt, name, "owner", owner, update_modified=False)
	# Arm the run conversation when the macro is armed (doc.skip_confirmation, set
	# only by a Jarvis Admin - guarded on the macro controller). Stamped via a raw
	# db.set_value, the File-Box pattern that bypasses the conversation controller's
	# admin guard: the value derives from the already-admin-gated macro flag, so a
	# re-check would just refuse a legitimate non-admin owner running their own armed
	# macro. The gate reads THIS conversation flag; the conversation is human-inert
	# while set (send_message / retry_message are blocked, T4), so only macro-step
	# turns ever run on it. Cleared on every run-terminal transition (T5).
	if doc.skip_confirmation:
		frappe.db.set_value(CONV, conv.name, "skip_confirmation", 1, update_modified=False)
	frappe.db.commit()

	# Scheduled runs surface via the proactive "conversation:new" toast; manual
	# runs are navigated to directly by the SPA, so no toast there.
	if trigger == "scheduled":
		publish_to_user(
			owner,
			{
				"kind": "conversation:new",
				"conversation_id": conv.name,
				"title": doc.macro_name[:140],
				"preview": f"Scheduled macro: {doc.macro_name}",
			},
		)

	try:
		_dispatch_step(run, doc, 0)
	except Exception:
		# #471: the conversation and the run row are already COMMITTED above, but the
		# first turn never dispatched (``_ensure_session_key`` throws when the gateway
		# is down). No turn means the chaining hook that terminalizes a run never fires
		# for it, so without this the row sits `running` until the stale sweep picks it
		# up three hours later — and the scheduler, seeing the raise, would record a
		# SECOND failed row, showing the customer two failures for one missed slot.
		# Terminalize the row that actually carries the conversation, and report the
		# refusal in the normal return shape instead of raising.
		frappe.log_error(title=f"jarvis macro dispatch failed: {doc.name}", message=frappe.get_traceback())
<<<<<<< HEAD
		_finish(run, "failed", error=_DISPATCH_FAILED_ERROR)
		try:
			# The scheduler records and notifies this refusal itself.
			_publish_done(run, doc, "failed", _DISPATCH_FAILED_ERROR)
		except Exception:
			pass
		if trigger == "scheduled":
			return {"ok": False, "reason": BLOCK_DISPATCH_FAILED}
		frappe.throw(_(_BLOCK_MESSAGE[BLOCK_DISPATCH_FAILED]))
=======
		# Only when the step did not go out. A raise AFTER its turn existed (the cursor
		# write, the look at the run) used to fail the run here all the same: "never
		# started", followed by the step's reply in a chat whose run had ended.
		if not _step_went_out_anyway(run, 0):
			# From the status this call left the run in, not by name: a Stop (or a delete,
			# or an archive) that landed while the dispatch was failing keeps its outcome.
			# The blind write turned that `stopped` into `failed`.
			failed = _cas_run_status(
				run.name, run.status, "failed", finished_at=frappe.utils.now(), error=_DISPATCH_FAILED_ERROR
			)
			frappe.db.commit()
			if failed:
				try:
					# The scheduler records and notifies this refusal itself.
					_publish_done(run, doc, "failed", _DISPATCH_FAILED_ERROR)
				except Exception:
					pass
			if trigger == "scheduled":
				return {"ok": False, "reason": BLOCK_DISPATCH_FAILED}
			frappe.throw(_(_BLOCK_MESSAGE[BLOCK_DISPATCH_FAILED]))
>>>>>>> ab24563 (fix(macros): a step is sent once: a fixed turn id per step and one dispatch function)
	return {"ok": True, "data": {"macro_run": run.name, "conversation": conv.name}}


def _parked_card(conversation: str, owner: str) -> dict | None:
	"""The first live confirmation card STRICTLY bound to ``conversation`` (or None).

	An armed macro runs the covered set uncarded, so the ONLY thing that parks a card
	on its run is an EXCLUDED tool (delete/cancel/amend) - i.e. the D5 stop signal. An
	unarmed run parks one for every gated write. Mirrors the gate's own
	strict-conversation filter (a conv-less token surfaces under any filter). A storage
	error is swallowed to None: a pending-confirm outage must not strand the run - it
	then advances as before, and for an armed run the human-inert send block +
	clear-on-terminal still hold the security line."""
	from jarvis.chat import pending_confirm

	try:
		for rec in pending_confirm.list_for_owner(owner, conversation=conversation, strict=True):
			if rec.get("conversation") == conversation:
				return rec
	except Exception:
		return None
	return None


def _armed_stop_message(current_step: int, parked: dict) -> str:
	"""D5 stop reason - names the un-clickable action AND the re-run hazard (§7): the
	covered steps before it already ran, so re-running the whole macro repeats them.

	``current_step`` is already the 1-based number of the step that just ran and parked
	(``_dispatch_step`` sets ``current_step = index + 1`` once the turn exists), so it is used
	as-is (floored at 1 for the merged/edge case), NOT +1."""
	step = max(int(current_step or 0), 1)
	what = _card_label(parked) or "a confirmation"
	return (
		f"Stopped at step {step}: this step needs a confirmation that can't be "
		f"shown in an unattended run ({what}). The steps before it already ran, so "
		f"re-running the whole macro would repeat them - fix this step's data or run it "
		f"interactively, don't re-run the macro. Nothing after this step ran."
	)[:500]


# Characters that would turn a card's summary into a link or formatting once the
# reason is posted into the chat, where assistant content renders as markdown. Not
# the underscore: a summary is "delete_doc doctype=Sales Order name=ITEM_A_1", and
# without it the tool and the record's name read as something else.
_MARKDOWN = str.maketrans("", "", "[]`*<>")


def _card_label(parked: dict) -> str:
	"""What a parked card is for, as plain words. The summary is built from the tool
	call's own arguments (a recipient, a subject, a record's title): model-authored
	text. It gets one bounded line like every other reason, and loses the characters
	that would make it formatting or a link in the closing chat message. With no
	summary, the tool's name in words."""
	label = _one_line(parked.get("summary")) or str(parked.get("tool") or "").replace("_", " ")
	return label.translate(_MARKDOWN).strip()


def _card_stop_message(current_step: int, total: int, parked: dict, *, scheduled: bool) -> str:
	"""Why an UNARMED run ended at a step when a confirmation card was waiting.

	The card is the user's to answer, so it is left alone (an armed run sweeps its
	card, see ``_armed_stop_message``). The run does not wait for the answer: only one
	card can be live in a conversation and a macro step cannot replace it, so any gated
	write in a later step would be refused. Ending here, saying so, is the honest
	outcome; it used to read ``completed`` with the write never applied.

	Says what the owner cannot guess: confirming the card applies that write but does
	not bring the run back, and for a scheduled macro, what stops this happening every
	time. The card's summary is built from the tool call's own arguments, so it gets
	the same one-line bound as every other reason on the row."""
	step = max(int(current_step or 0), 1)
	what = _card_label(parked)
	text = f"Stopped at step {step} of {total}: a confirmation is waiting in the conversation"
	text += f" ({what})." if what else "."
	text += " You can still confirm or discard it there."
	if step < total:
		text += " The steps after it did not run, and confirming it will not resume them."
	if scheduled:
		text += (
			" To let this macro run without stopping here, ask an administrator to"
			" switch on Skip confirmation for it."
		)
	return text[:500]


# Jarvis Chat Turn states after which the turn will produce nothing more.
_TURN_ENDED = ("done", "errored", "cancelled")
_TURN_FAILED = ("errored", "cancelled")
_TURN_FIELDS = ["name", "seed_message", "assistant_message", "state", "error", "cancel_reason"]
# One step's reason on a run row: several must fit the 500 characters of ``error``.
_REASON_MAX = 120
_SCRUB_INPUT_MAX = 2000
_STEP_STOPPED = "the step was stopped."
_STEP_ERRORED = "the step ended with an error."
_OUTCOME_UNREAD = "Finished, but how its steps went could not be read. Open the chat to check."


def _step_turn_id(run_name: str, index: int) -> str:
	"""The id of the turn of step ``index`` (0-based) of a run. A summarized run has
	one turn: index 0.

	THE fact "step N of run R was sent" is "a ``Jarvis Chat Turn`` named this exists".
	It used to be read off the run's cursor, which is written after the turn exists:
	a failure in between, a run lock that lapsed under a slow holder, a second healer
	for a message with no turn, each sent the step again (issue #422), and on an armed
	run its writes happened twice. A turn's name is its ``run_id`` (the primary key), so
	two dispatchers of one step now meet on one row and the second is told
	``duplicate`` (``admission.accept_or_queue``).

	Same shape as a random id (12 hex characters), and as unique where it matters: a
	run's name is a hash, and a new run of the same macro has a new name."""
	return hashlib.sha1(f"{run_name}:{index}".encode()).hexdigest()[:12]


def _step_is_out(run_name: str, index: int) -> bool:
	"""Whether step ``index`` of the run has its turn. Always False where no Turn rows
	are written (the pump off and draining, without Phase-0): a step sent there gets a
	random id and has no row to find."""
	return bool(frappe.db.exists(TURN, _step_turn_id(run_name, index)))


def _raise_cursor(run, to: int, *, sent: bool = False) -> None:
	"""Move the run's cursor UP to ``to``; never down. Commits.

	``current_step`` is how many steps the run has sent, and it is what the month's
	scheduled budget adds up (``_scheduled_steps_this_month``): a cursor moved back
	would hand an owner steps they already spent.

	``sent``: this call just sent the step, which is progress, so ``modified`` moves
	(the stale-run sweep measures a run's idleness on it). A repair (the step turned
	out to be sent already) is not progress and leaves ``modified`` alone: a repeated
	event must not keep a stuck run looking alive.

	On a fresh snapshot: the run row is also written by a second dispatcher and by a
	stop, and a write judged against an older view fails with 1020 (``jarvis.chat.txn``).
	``owned``: every caller has committed its own work by here."""
	from jarvis.chat import txn

	txn.fresh_snapshot(owned=True)
	if sent:
		frappe.db.sql(
			f"""UPDATE `tab{RUN}` SET current_step = GREATEST(current_step, %(to)s), modified = %(now)s
			    WHERE name = %(n)s""",
			{"n": run.name, "to": to, "now": frappe.utils.now()},
		)
	else:
		frappe.db.sql(
			f"UPDATE `tab{RUN}` SET current_step = %(to)s WHERE name = %(n)s AND current_step < %(to)s",
			{"n": run.name, "to": to},
		)
	frappe.db.commit()
	run.current_step = max(int(run.current_step or 0), to)


def _next_step_index(run) -> int:
	"""The 0-based index of the first step the run has NOT sent, and the cursor
	repaired to match.

	The cursor says where the run is unless a dispatcher died between creating a
	step's turn and writing it; then it is one behind (or more), and the step it
	points at already has its turn. Walked forward from the cursor only: turns below
	it are never read, so a step sent while no Turn rows were written is not a gap.
	With no Turn rows at all this is the cursor, as before.

	Only for a caller that knows the current step is over (an accepted step end) or
	that nothing is running (a message with no turn). The capacity resume sends
	exactly the index it parked at and lets ``_dispatch_step`` answer "already out":
	walking forward there would send the step after one that is still running."""
	cursor = int(run.current_step or 0)
	index = cursor
	while _step_is_out(run.name, index):
		index += 1
	if index != cursor:
		_raise_cursor(run, index)
	return index


def _macro_seeds(conversation: str, *, newest_first: bool = False, limit: int | None = None) -> list:
	"""The run's step messages: every ``origin="macro"`` user row in its conversation.
	Every step is dispatched through ``_enqueue_turn(origin="macro")``, and a step
	deferred for capacity has its seed deleted, so these are the steps that went out."""
	return frappe.get_all(
		MSG,
		filters={"conversation": conversation, "role": "user", "origin": "macro"},
		fields=["name", "seq"],
		order_by="seq desc" if newest_first else "seq asc",
		limit=limit,
	)


def _never_dispatched(conversation: str, latest, previous) -> bool:
	"""Whether ``latest`` is a step message whose turn was never created.

	``_enqueue_turn`` commits the message, THEN creates the turn. A worker that dies
	between the two leaves a message no turn will ever end for. Treated as the current
	step it would be the current step for good: every later event, including the
	re-delivery of the step before it (which is what recovers this), would name
	something else. Told apart by what a dispatched step always has on the turn-machine
	path, a Turn row, and only when the step before it has one (so Turn rows are in
	use here) and nothing was written to the conversation after it."""
	from jarvis.chat import admission

	if not admission.turn_machine_enabled():
		# New turns go out on the legacy path right now (pump off, or draining): a
		# step just dispatched there has no Turn row either, and is not dangling.
		return False
	if frappe.db.exists(TURN, {"seed_message": latest.name}):
		return False
	if not frappe.db.exists(TURN, {"seed_message": previous.name}):
		return False
	return not frappe.db.exists(MSG, {"conversation": conversation, "seq": [">", latest.seq]})


def _current_step_message(conversation: str) -> str | None:
	"""The user message of the step this run last dispatched: its latest macro
	message, unless that one never became a turn (``_never_dispatched``)."""
	seeds = _macro_seeds(conversation, newest_first=True, limit=2)
	if not seeds:
		return None
	if len(seeds) > 1 and _never_dispatched(conversation, seeds[0], seeds[1]):
		return seeds[1].name
	return seeds[0].name


def _ended_turn_seed(
	conversation: str, run_id: str | None, seed_message: str | None, assistant_message: str | None
):
	"""The user message that started the turn that just ended, from whichever handle
	the caller has: the message itself (the legacy worker), the Turn (finalize), or
	the reply (turn recovery works from the assistant row). None when it cannot be
	known: no Turn row exists on the legacy path."""
	if seed_message:
		return seed_message
	if run_id:
		seed = frappe.db.get_value(TURN, run_id, "seed_message")
		if seed:
			return seed
	if assistant_message:
		# ``conversation`` is the indexed column; ``assistant_message`` alone is not.
		return frappe.db.get_value(
			TURN, {"conversation": conversation, "assistant_message": assistant_message}, "seed_message"
		)
	return None


def _is_the_steps_own_turn(conversation: str, seed: str | None) -> bool:
	"""Whether the turn that just ended is the run's CURRENT step, so the run may move.

	The hook fires for every finished turn in the conversation. An unarmed run's
	conversation accepts typed messages, a confirmed card runs a hidden follow-up turn,
	and the finalize effect that calls the hook is retried. Each of those used to
	advance the run: steps overlapped, the run finished early, and "Step N failed"
	named the wrong step. Only the turn started by the current step's message counts;
	a re-delivered event for an earlier step no longer matches once the next is out."""
	current = _current_step_message(conversation)
	if seed:
		if current:
			return seed == current
		# No macro-stamped message here at all (a run from before the stamp existed):
		# go by the ended turn's own stamp, and let an unstamped one through.
		return (frappe.db.get_value(MSG, seed, "origin") or "") in ("", "macro")
	# The caller could not say which turn ended. A stalled run is worse than the old
	# looseness, so it advances, unless the step's own turn is visibly still running.
	if current and frappe.db.exists(TURN, {"seed_message": current, "state": ["not in", _TURN_ENDED]}):
		return False
	return True


def _one_line(text: str | None) -> str:
	"""Text fit for a run row: one line, bounded, scrubbed. It is stored and shown
	away from the chat (the run row, a notification, the closing message), so beside
	the brand scrub an error gets on its way to the chat it loses anything shaped like
	a credential: a sentence can carry a URL with a key in it."""
	from jarvis.admin_client import _scrub_secrets
	from jarvis.chat import egress_rules

	# Bounded before the scrub: only the first line's worth is kept anyway, and the
	# credential patterns are slow on a very long payload. Cut at a word boundary, so
	# a credential is never left as a prefix the patterns no longer recognise.
	line = " ".join(str(text or "").split())
	if len(line) > _SCRUB_INPUT_MAX:
		line = line[:_SCRUB_INPUT_MAX].rsplit(" ", 1)[0]
	line = egress_rules.redact(_scrub_secrets(line) or "") or ""
	return line if len(line) <= _REASON_MAX else line[: _REASON_MAX - 1].rstrip() + "…"


def _reads_as_a_sentence(line: str) -> bool:
	"""Whether a turn's recorded error is already something to show an owner: it
	starts like a sentence, ends like one, and carries no payload or exception name.
	Most are not. A turn records whatever failed it: a provider's JSON, a transport
	code, "unexpected worker error: KeyError"."""
	if not line or not line[0].isupper() or line[-1] not in ".!?" or len(line.split()) < 3:
		return False
	return not any(mark in line for mark in ("{", "}", "Traceback", "Error:", "error:", "Exception"))


def _plain_reason(raw) -> str:
	"""``raw`` as a reason an owner can read, or "" when there is none to give.

	A sentence is kept as it is. Anything else is classified by the same rules the
	chat uses for a failed turn (``error_taxonomy``) and given that rule's headline, so
	the run row says what the chat said, not the payload behind it."""
	from jarvis.chat import error_taxonomy

	line = _one_line(raw)
	if not line:
		return ""
	# Judged on the whole text: a long sentence is still one after it is cut.
	if _reads_as_a_sentence(" ".join(str(raw).split())):
		return line
	headline = error_taxonomy.headline_for(raw)
	return _one_line(headline.rstrip(".") + ".") if headline else ""


_REPLY_FIELDS = ["name", "content", "error", "streaming", "was_recovered"]
_NO_REPLY = object()


def _step_reply(turn):
	"""The reply row of a step's turn, with what the verdict and the reason read."""
	if not turn or not turn.get("assistant_message"):
		return None
	return frappe.db.get_value(MSG, turn.get("assistant_message"), _REPLY_FIELDS, as_dict=True)


def _step_verdict(turn, reply, errored: bool) -> str:
	"""How a step ended: "" (it worked), ``errored`` or ``cancelled``. The one answer
	the hook, the run's outcome and a step's reason all use.

	* No Turn row, or one that has not ended yet: the caller's ``errored`` decides, as
	  it always did. The legacy worker calls the hook BEFORE it settles its turn (the
	  row is still ``dispatching``), a pump turn is ``finalizing`` when its hook runs,
	  and a site without the turn machine has no row. Reading the row there would call
	  every failed step a success and ignore "Stop on error".
	* A turn that has ended: the row decides.
	* One exception: a turn recorded ``errored`` whose reply turn recovery later
	  finished (``turn_recovery._finalize`` stamps ``was_recovered``, clears the error
	  and writes the text). The answer arrived; the row was written before it did.
	  Only that shape. A step the user stopped is ``cancelled`` with a partial reply
	  and no error, and it stays stopped."""
	state = turn.get("state") if turn else None
	if state not in _TURN_ENDED:
		return "errored" if errored else ""
	if state == "errored" and _recovered(reply):
		return ""
	return state if state in _TURN_FAILED else ""


def _recovered(reply) -> bool:
	"""A reply turn recovery finished: stamped, done streaming, with text, no error."""
	return bool(
		reply
		and int(reply.get("was_recovered") or 0)
		and not int(reply.get("streaming") or 0)
		and (reply.get("content") or "").strip()
		and not (reply.get("error") or "").strip()
	)


def _turn_failure_reason(turn, reply=_NO_REPLY) -> str:
	"""Why a step's turn failed. A turn the user stopped records nothing at all; one
	the system cancelled (it waited too long in the queue) says so in
	``cancel_reason`` only. ``reply``: the turn's reply row when the caller already
	has it."""
	if reply is _NO_REPLY:
		reply = _step_reply(turn)
	if turn:
		for raw in (turn.get("error"), (reply or {}).get("error"), turn.get("cancel_reason")):
			reason = _plain_reason(raw)
			if reason:
				return reason
	return _STEP_STOPPED if _step_verdict(turn, reply, True) == "cancelled" else _STEP_ERRORED


def _stopped_by_the_user(turn) -> bool:
	"""A turn the user pressed Stop on: cancelled, with nothing recorded as to why
	(a system cancel records its reason)."""
	return bool(
		turn and turn.get("state") == "cancelled" and not turn.get("error") and not turn.get("cancel_reason")
	)


def _newest_seed(conversation: str) -> str | None:
	newest = _macro_seeds(conversation, newest_first=True, limit=1)
	return newest[0].name if newest else None


def _step_went_out_anyway(run, index: int) -> bool:
	"""A dispatch of step ``index`` raised. Whether the step's turn exists all the same
	(the raise came after it: the cursor write, the look at the run, a lost
	connection). If so the cursor is put where the dispatch would have left it and the
	answer is True: the step is running, its own end moves the run, and the caller
	must not do what it does for a step that never went out (end the run, or park it
	again for a resume that would then have nothing to send).

	Asked, and the cursor written, on a fresh snapshot: whatever raised may have left
	this transaction's view older than the run row's last write, and the write would
	then fail with 1020 inside the very branch that is cleaning up. A commit, never a
	rollback: the dispatch may have queued its send for after the commit."""
	from jarvis.chat import txn

	txn.fresh_snapshot(owned=True)
	if not _step_is_out(run.name, index):
		return False
	_raise_cursor(run, index + 1, sent=True)
	try:
		_drop_copies_of_step(run, index)
	except Exception:
		frappe.log_error(
			title=f"jarvis macro step copy cleanup failed: {run.name}", message=frappe.get_traceback()
		)
	return True


def _drop_copies_of_step(run, index: int) -> int:
	"""Remove the step messages written AFTER the message step ``index``'s turn is on
	that have no turn of their own; returns how many. Each is a second dispatcher's
	copy of the step: it wrote its message, and then failed (or was told
	``duplicate``) because the other dispatcher's turn carries the step.

	Left in the chat, a copy becomes the run's "current step" (it is the newest step
	message), so the real step's end is refused and the run sits still; or the orphan
	sweep later runs it, and the step runs twice.

	Safe only while step ``index`` is the last one sent, which is when it is called:
	the step after it has no message yet."""
	from jarvis.chat import api

	seed = frappe.db.get_value(TURN, _step_turn_id(run.name, index), "seed_message")
	seq = frappe.db.get_value(MSG, seed, "seq") if seed else None
	if seq is None:
		return 0
	later = frappe.get_all(
		MSG,
		filters={"conversation": run.conversation, "role": "user", "origin": "macro", "seq": [">", seq]},
		pluck="name",
	)
	copies = [m for m in later if not frappe.db.exists(TURN, {"seed_message": m})]
	for message in copies:
		api._delete_enqueue_seed(message)
	return len(copies)


def _superseded_by_a_retry(run_id: str, seed: str | None) -> bool:
	"""Whether the turn the caller names is an OLD turn of its message: a newer Turn
	exists for the same one. That is the owner's Retry of a failed step (a deliberate
	second turn on the step's message, with a random id). The retry's own end moves
	the run. The old turn's end, delivered late (the finalize effect is retried for
	minutes), named the right message and so used to pass as the step's end: the run
	moved on, or failed on "Stop on error", while the retry was still running.

	Only for a caller that names a turn. A caller with nothing but the message (no
	Turn rows) or the reply is judged as before."""
	named = frappe.db.get_value(TURN, run_id, ["seed_message", "creation"], as_dict=True)
	if not named or not seed or named.seed_message != seed:
		return False
	return bool(
		frappe.db.exists(
			TURN, {"seed_message": seed, "creation": [">", named.creation], "name": ["!=", run_id]}
		)
	)


def _went_out_since(conversation: str, newest_before: str | None) -> bool:
	"""Whether a dispatch that raised nevertheless created a turn: there is a step
	message newer than the newest one from BEFORE the attempt, and it has a Turn row.
	Compared with what was there before, not with the turn that ended: the caller
	may not know which turn that was. Only knowable where Turn rows are written;
	without them the answer is no, and the run ends.

	Second to ``_step_went_out_anyway``, which asks for the step's own turn by its
	fixed id. Kept for a turn created under a random id: a step sent on the legacy
	path that the cutover gate re-routed into the turn machine."""
	newest = _newest_seed(conversation)
	if not newest or newest == newest_before:
		return False
	return bool(frappe.db.exists(TURN, {"seed_message": newest}))


def _ended_turn(seed: str | None, run_id: str | None):
	"""The Turn row of the turn that just ended (None on the legacy path, which
	writes none). By its own id when the caller has it: a retried step has several
	turns for one message."""
	if run_id:
		turn = frappe.db.get_value(TURN, run_id, _TURN_FIELDS, as_dict=True)
		if turn:
			return turn
	if not seed:
		return None
	rows = frappe.get_all(
		TURN, filters={"seed_message": seed}, fields=_TURN_FIELDS, order_by="creation desc", limit=1
	)
	return rows[0] if rows else None


def _turn_to_judge(conversation: str, turn, run_id: str | None, assistant_message: str | None):
	"""The Turn row ``_step_verdict`` may read for the turn that just ended: that turn's
	own row, or None (then the caller's flag decides).

	``_ended_turn`` falls back to the NEWEST turn of the step's message. That is right
	for wording a reason, and wrong for a verdict when the step was retried: a caller
	that names a turn with no row (a job the kill switch sent down the legacy path), or
	names only the reply (turn recovery), would be judged by a sibling turn's row, and
	an old `errored` attempt would fail the retry that just worked."""
	if not turn:
		return None
	if run_id:
		return turn if turn.get("name") == run_id else None
	if assistant_message:
		return turn if turn.get("assistant_message") == assistant_message else None
	return turn


def _step_turns(conversation: str) -> list:
	"""The run's dispatched steps in order, each with its Turn row (None on the legacy
	path, which writes none). Step N is the Nth step message. Where Turn rows are in
	use, a message that never became a turn (``_never_dispatched``) is not a step."""
	seeds = [m.name for m in _macro_seeds(conversation)]
	if not seeds:
		return []
	turns = frappe.get_all(
		TURN,
		filters={"seed_message": ["in", seeds]},
		fields=_TURN_FIELDS,
		order_by="creation asc",
	)
	latest = {t.seed_message: t for t in turns}  # a retried step: its last turn counts
	if latest:
		seeds = [seed for seed in seeds if seed in latest]
	return [latest.get(seed) for seed in seeds]


_DRAFT_GONE_HINT = (
	"To create it, run that step again in a chat, or ask an administrator to switch on "
	"Skip confirmation so the macro writes directly."
)
_DRAFTS_GONE_HINT = (
	"To create them, run those steps again in a chat, or ask an administrator to switch on "
	"Skip confirmation so the macro writes directly."
)


def _draft_note(drafted: list[int], last_step: int) -> str:
	"""What became of the steps that only drafted a record.

	The chat keeps a draft open on the LAST reply only. A draft from the final step is
	still there to apply. One from an earlier step was closed when the next step
	replied: that record was not created, and the note must not send the owner looking
	for a draft that is gone."""
	closed = [n for n in drafted if n < last_step]
	parts = []
	if closed:
		numbers = _listed(closed)
		parts.append(
			f"Step {numbers} drafted a record that was not created: the macro did not wait, "
			f"and the next step closed the draft."
			if len(closed) == 1
			else f"Steps {numbers} drafted records that were not created: the macro did not wait, "
			f"and the steps after them closed the drafts."
		)
		parts.append(_DRAFT_GONE_HINT if len(closed) == 1 else _DRAFTS_GONE_HINT)
	if last_step in drafted:
		parts.append(f"Step {last_step} left a draft for you to apply in the chat.")
	return " ".join(parts)


def _listed(numbers: list[int]) -> str:
	if len(numbers) == 1:
		return str(numbers[0])
	return ", ".join(str(n) for n in numbers[:-1]) + f" and {numbers[-1]}"


def _failed_steps_note(failed: dict, total: int) -> str:
	""" "Finished, but 2 of 5 steps failed. Step 2: ...; Step 4: ..." within the 500
	characters the row holds, never cut in the middle of a reason."""
	if total <= 1:
		head = "Finished, but its only step failed."
	else:
		head = f"Finished, but {len(failed)} of {total} steps failed."
	parts: list[str] = []
	numbers = sorted(failed)
	for position, number in enumerate(numbers):
		piece = f" Step {number}: {failed[number]}"
		if len(head) + sum(len(x) for x in parts) + len(piece) > 460:
			parts.append(f" And {len(numbers) - position} more.")
			break
		parts.append(piece)
	return head + "".join(parts)


def _run_outcome(run, total: int, *, last_step_errored: bool) -> tuple[str, str]:
	"""``(status, note)`` for a run that has dispatched its last step.

	``completed`` used to be unconditional here. With "Stop on error" off the run keeps
	going past a failed step, by the owner's choice; that never made it a run that
	worked. And an unarmed step that creates or updates a record ends with a draft for
	the user to apply: nothing is written until they do, and there is no signal the
	run could wait on, so the run says which steps left one.

	Earlier steps are read back from their Turn rows. Without Turn rows (the legacy
	path) only the step that just ended is known, first-hand."""
	from jarvis.chat import filebox_cards

	steps = _step_turns(run.conversation)
	failed: dict[int, str] = {}
	drafted: list[int] = []
	names = [t.assistant_message for t in steps if t and t.assistant_message]
	replies = (
		{r.name: r for r in frappe.get_all(MSG, filters={"name": ["in", names]}, fields=_REPLY_FIELDS)}
		if names
		else {}
	)
	for number, turn in enumerate(steps, start=1):
		if not turn:
			continue
		reply = replies.get(turn.assistant_message)
		# An earlier step's turn has ended; one that has not carries no verdict yet.
		if _step_verdict(turn, reply, False):
			failed[number] = _turn_failure_reason(turn, reply)
		elif filebox_cards.parse_action((reply or {}).get("content")):
			drafted.append(number)
	# The step that just ended is known first-hand, Turn row or not.
	last = max(int(run.current_step or 0), 1)
	if last_step_errored and last not in failed:
		failed[last] = _turn_failure_reason(steps[last - 1] if last <= len(steps) else None)
	if failed:
		if (run.get("run_mode") or "") == MODE_MERGED:
			return "failed", f"The summarized run failed: {next(iter(failed.values()))}"[:500]
		return "failed", _failed_steps_note(failed, total)
	if drafted:
		return "completed", _draft_note(drafted, len(steps))
	return "completed", ""


def advance_after_turn(
	conversation_id: str,
	*,
	errored: bool,
	run_id: str | None = None,
	seed_message: str | None = None,
	assistant_message: str | None = None,
) -> None:
	"""Chaining hook, called after every terminal turn outcome. If the conversation
	belongs to a ``running`` Macro Run AND the turn that ended is that run's current
	step, advance it: enqueue the next step, or finish. If it's a macro's background
	SUMMARIZE turn, apply the summary to the macro. No-op for normal (non-macro) chats.

	``run_id`` / ``seed_message`` / ``assistant_message`` say WHICH turn ended; each
	caller passes the handle it has (see ``_ended_turn_seed``).

	Best-effort: never raises (a macro bug must not strand a normal turn).
	Serialized via a per-run redis lock; a re-delivered event / RQ retry cannot
	double-advance because it no longer names the current step."""
	try:
		_apply_merge_after_turn(conversation_id, errored=errored)
	except Exception:
		frappe.log_error(title="jarvis macro merge-apply failed", message=frappe.get_traceback())
	try:
		run_name = frappe.db.get_value(RUN, {"conversation": conversation_id, "status": "running"}, "name")
		if not run_name:
			return
		from jarvis._redis_lock import redis_lock
		from jarvis.chat import txn

		with redis_lock(f"jarvis_macro_run:{run_name}", timeout_s=60, blocking_timeout_s=10.0) as acquired:
			if not acquired:
				# The run will not move on this event again. An Error Log, not a logger
				# line: the site logger writes errors only, and this is the one trace of
				# why a run sat still until the stale-run sweep.
				frappe.log_error(
					title=f"jarvis.chat.macros.turn_end_dropped: {run_name}",
					message=f"The run lock was busy for 10s; a turn end in {conversation_id} was not applied.",
				)
				return
			# This transaction's snapshot was taken by the lookup above, BEFORE waiting
			# for the lock. Everything below must see what the previous holder wrote
			# (the next step dispatched, a Stop), or two deliveries of one step's end
			# both dispatch the step after it. ``owned``: every caller of this hook is a
			# worker that owns its transaction, including the in-process realtime
			# worker, which is not a "job" and would otherwise skip the commit.
			txn.fresh_snapshot(owned=True)
			run = frappe.get_doc(RUN, run_name)
			if run.status != "running":  # stopped mid-flight, or already advanced
				return
			seed = _ended_turn_seed(conversation_id, run_id, seed_message, assistant_message)
			if not _is_the_steps_own_turn(conversation_id, seed):
				# Not an error, and routine: a typed message, a card's follow-up or a
				# repeated event.
				return
			if run_id and _superseded_by_a_retry(run_id, seed):
				return  # the step was retried; that turn's end is the one that counts
			macro_doc = frappe.get_doc(MACRO, run.macro)
			if _fail_run_unless_one_owner(run, macro_doc):
				return
			steps = macro_doc.steps or []
			total = min(run.total_steps or len(steps), len(steps))
			merged = _run_mode(run, macro_doc) == MODE_MERGED

			# A step that parked a confirmation card. A park returns ok:True, so
			# `errored` is False: the only per-step signal is the parked token itself.
			# Checked BEFORE the stop_on_error / next_index decisions, so a merged-mode
			# (single-turn) run is covered too.
			owner_user = frappe.db.get_value(CONV, run.conversation, "owner")
			parked = _parked_card(run.conversation, owner_user)
			if parked and frappe.db.get_value(CONV, run.conversation, "skip_confirmation"):
				# D5 (armed runs): the covered set runs uncarded, so a parked card means
				# an EXCLUDED tool (delete/cancel/amend) - and this is an unattended run
				# with nobody to click it. Advancing would report a false "completed"
				# with the destructive step silently never run. STOP the run with a
				# legible re-run-hazard message, and SWEEP the token so the card can't be
				# confirmed later and fire an armed continuation turn.
				from jarvis.chat import pending_confirm

				# SWEEP the token FIRST, then terminalize. _finish disarms the
				# conversation (skip_confirmation -> 0), and the _confirm_core armed
				# guard keys off that flag - so if we disarmed before sweeping, a Confirm
				# racing in that window would read flag=0, bypass the guard, and run the
				# destructive write. Consuming the token first makes a racing Confirm hit
				# a dead token and be refused regardless of the flag (deterministic).
				pending_confirm.clear_for_conversation(owner_user, run.conversation)
				# PR-1: flip the swept row's pending card to a terminal 'cancelled'
				# receipt so it doesn't linger as a dangling 'pending' row on reload.
				try:
					from jarvis import api as _jarvis_api

					_jarvis_api.cancel_pending_action_rows(run.conversation)
				except Exception:
					frappe.log_error(title="macro-stop cancel pending rows", message=frappe.get_traceback())
				_end_run(run, macro_doc, "failed", _armed_stop_message(run.current_step or 0, parked))
				return
			if parked:
				# Unarmed: the card is the user's to answer and stays. The run ends here
				# rather than chaining on and reporting "completed" (_card_stop_message).
				_end_run(
					run,
					macro_doc,
					"stopped",
					_card_stop_message(
						run.current_step or 0,
						total,
						parked,
						scheduled=(run.get("trigger") or "") == "scheduled",
					),
				)
				return

			step_number = max(int(run.current_step or 0), 1)  # never "Step 0" (first-step race)
			turn = _ended_turn(seed, run_id)
			# From here on `errored` is the step's verdict, not the caller's word alone.
			judged = _turn_to_judge(conversation_id, turn, run_id, assistant_message)
			errored = bool(_step_verdict(judged, _step_reply(judged), errored))
			if errored and macro_doc.stop_on_error:
				if _stopped_by_the_user(turn):
					# Their own Stop on the step: the run stopped, it did not fail. No
					# reason is stored, exactly as for the Stop button on the run: a
					# `stopped` run that carries one is one the ENGINE stopped, and that
					# is what the success rate and the notification key on.
					_end_run(run, macro_doc, "stopped", "")
					return
				what = "The summarized run" if merged else f"Step {step_number}"
				_end_run(run, macro_doc, "failed", f"{what} failed: {_turn_failure_reason(turn)}")
				return

			# 0-based index of the next step: the cursor, or past it when the cursor was
			# left behind a step that is already out.
			next_index = _next_step_index(run)
			if next_index >= total:
				try:
					status, note = _run_outcome(run, total, last_step_errored=errored)
				except Exception:
					# Describing the run must never be what keeps it from ending.
					frappe.log_error(
						title=f"jarvis macro outcome failed: {run.name}", message=frappe.get_traceback()
					)
					status, note = (
						("failed", f"Step {step_number} failed: {_STEP_ERRORED}")
						if errored
						else ("completed", _OUTCOME_UNREAD)
					)
				_end_run(run, macro_doc, status, note)
				return

			newest_before = _newest_seed(conversation_id)
			try:
				_dispatch_step(run, macro_doc, next_index)
			except Exception:
				traceback = frappe.get_traceback()
				if _step_went_out_anyway(run, next_index) or _went_out_since(conversation_id, newest_before):
					# The turn exists: the failure came after the dispatch (the cursor
					# write). The step is running and its own end will move the run, so
					# ending the run here would be false, and would be followed by the
					# step's reply. Put the cursor where the dispatch left it. No
					# rollback: the dispatch may have queued its send for after the
					# commit, and a rollback would drop it.
					_raise_cursor(run, next_index + 1, sent=True)
					frappe.log_error(
						title=f"jarvis macro step bookkeeping failed: {run.name}", message=traceback
					)
					return
				# The step's message may already be committed with no turn behind it
				# (``_enqueue_turn`` commits, then dispatches). Nothing would ever end
				# for it, so the run would sit `running` until the stale sweep. End it
				# now, saying what happened, as ``run_macro`` does for the first step
				# (#471) and the capacity resume for a resumed one (CDX-23).
				frappe.log_error(title=f"jarvis macro step dispatch failed: {run.name}", message=traceback)
				_end_run(
					run,
					macro_doc,
					"failed",
					f"Step {next_index + 1} could not be started, so the run ended there. "
					f"The steps before it already ran.",
				)
	except Exception:
		frappe.log_error(title="jarvis macro advance failed", message=frappe.get_traceback())


def _merge_outcome(conversation_id: str, *, errored: bool) -> tuple[str, str]:
	"""``(merge_status, merged_prompt)`` for a finished summarize turn.

	Split out of :func:`_apply_merge_after_turn` (#632) so the landing write can be
	guaranteed to happen even when reading the outcome throws. ``failed`` is the
	default, so every path that is not a clean, mergeable answer lands as failed and
	the macro falls back to its step sequence."""
	if errored:
		return "failed", ""
	rows = frappe.get_all(
		MSG,
		filters={"conversation": conversation_id, "role": "assistant"},
		fields=["content", "streaming", "error"],
		order_by="seq desc",
		limit=1,
	)
	m = rows[0] if rows else None
	if not m or m.streaming or (m.error or "").strip():
		return "failed", ""
	# Lazily imported across the file boundary, and #632 is precisely the fallout of
	# that: a cleanup deleted _MERGE_RE from macros_api because it looked unused
	# THERE, and neither file's tests noticed. The guard in the caller now makes that
	# class of breakage loud instead of an eternal "pending".
	from jarvis.chat.macros_api import _MERGE_RE

	mt = _MERGE_RE.search(m.content or "")
	try:
		merge = frappe.parse_json(mt.group(1).strip()) if mt else None
	except Exception:
		merge = None
	if isinstance(merge, dict) and merge.get("mergeable") and str(merge.get("merged_prompt") or "").strip():
		return "ready", str(merge.get("merged_prompt")).strip()
	return "failed", ""


def _finish_merge(macro_name: str, conversation_id: str, status: str, merged: str) -> None:
	"""Land the terminal merge outcome, bin the throwaway conversation, tell the SPA.

	The ONE place ``merge_status`` leaves ``pending``, and the one place the three
	terminal steps live, so the success path and the fault path cannot do different
	amounts of work (#632). Committed immediately: a caller may be about to re-raise,
	and these writes must survive that.

	Reads the owner with ``get_value`` rather than ``get_doc`` deliberately. The doc
	fetch used to sit ahead of the guard, so a row deleted in the window between
	finding it and loading it threw before anything could be written and left
	``merge_status`` on ``pending``, which is the exact bug this function exists to
	prevent."""
	frappe.db.set_value(
		MACRO,
		macro_name,
		{"merged_prompt": merged, "merge_status": status, "merge_conversation": ""},
		update_modified=False,
	)
	frappe.db.commit()

	# The throwaway conversation served its purpose - clean it up best-effort.
	try:
		frappe.db.delete(MSG, {"conversation": conversation_id})
		frappe.delete_doc("Jarvis Conversation", conversation_id, force=True, ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		pass

	# Best-effort too: an open tab learns the outcome ONLY from this event, but a
	# publish failure must not undo a landed merge or mask the fault being re-raised.
	try:
		row = frappe.db.get_value(MACRO, macro_name, ["owner", "macro_name"], as_dict=True)
		if row:
			publish_to_user(
				row.owner,
				{
					"kind": "macro:merged",
					"macro": macro_name,
					"macro_name": row.macro_name,
					"status": status,
				},
			)
	except Exception:
		pass


def _drop_summary_link_unless_one_owner(macro_name: str, conversation_id: str) -> bool:
	"""CLEARS the macro's summary link and returns True unless the macro and the
	conversation it names belong to one user. Returns False, touching nothing, when
	they do.

	Landing a summary reads the conversation's last reply into the macro and then
	deletes the conversation with permissions ignored. That is right for the throwaway
	chat ``summarize_macro`` created for the macro's owner, and for nothing else: a
	macro naming another user's conversation would take their reply and destroy their
	chat. The controller refuses a user changing ``merge_conversation``; this is the
	engine declining to act on a link it did not write, whatever did. The conversation
	is left exactly as it was."""
	row = frappe.db.get_value(MACRO, macro_name, ["owner", "macro_name"], as_dict=True)
	if not row:
		return True  # the macro was deleted while its summary ran: nothing to land on
	conv_owner = frappe.db.get_value(CONV, conversation_id, "owner")
	if row.owner == conv_owner:
		return False
	# State first, alarm second: if the log failed first the macro would stay
	# "pending" with nothing left to revisit it.
	frappe.db.set_value(
		MACRO,
		macro_name,
		{"merge_status": "failed", "merge_conversation": ""},
		update_modified=False,
	)
	frappe.db.commit()
	# A conversation that no longer exists is an ordinary state ("delete all
	# conversations" mid-summary), not a mismatch: no alarm for it.
	if conv_owner is not None:
		frappe.log_error(
			title=f"jarvis.chat.macros.summary_owner_mismatch: {macro_name}",
			message=(
				f"macro {macro_name!r} owner {row.owner!r}; "
				f"conversation {conversation_id!r} owner {conv_owner!r}"
			),
		)
	# An open form learns the outcome ONLY from this event; without it the Run button
	# sits on "summarizing" until a reload. Best-effort, like _finish_merge's own.
	try:
		publish_to_user(
			row.owner,
			{"kind": "macro:merged", "macro": macro_name, "macro_name": row.macro_name, "status": "failed"},
		)
	except Exception:
		pass
	return True


def _apply_merge_after_turn(conversation_id: str, *, errored: bool) -> None:
	"""When a macro's background summarize turn ends, land the result on the
	macro — server-side, so the summary arrives even if the user's tab is gone.
	ready → merged_prompt set (this is what run_macro executes); failed /
	unmergeable → the step sequence runs (correct fallback: an unmergeable
	sequence NEEDS its checkpoints). Publishes ``macro:merged`` so an open SPA
	refreshes its Run buttons."""
	macro_name = frappe.db.get_value(
		MACRO, {"merge_conversation": conversation_id, "merge_status": "pending"}, "name"
	)
	if not macro_name:
		return
	if _drop_summary_link_unless_one_owner(macro_name, conversation_id):
		return
	try:
		status, merged = _merge_outcome(conversation_id, errored=errored)
	except Exception:
		# #632: finish the macro as `failed` BEFORE re-raising. Everything inside
		# _merge_outcome can throw - the lazy cross-file _MERGE_RE import most of all,
		# which no test on either side notices if the symbol disappears - and a throw
		# used to skip the landing write entirely, leaving merge_status on "pending".
		#
		# "pending" means "still merging", so nothing ever revisits it: the user waits
		# forever on a Run button that will never enable, and a genuine code fault is
		# indistinguishable from work in progress. "failed" is both true and safe, since
		# it falls back to running the step sequence, which an unmergeable macro needs
		# anyway.
		#
		# The SAME _finish_merge runs on both paths. Writing the row but skipping the
		# socket publish would fix the database and not the user: an open tab only
		# learns the outcome from `macro:merged`, so the Run button would still sit
		# disabled on "summarizing" until a manual reload, which is the very symptom
		# being fixed. Skipping the cleanup would likewise orphan a conversation and
		# its messages on every fault.
		#
		# Re-raised on purpose rather than swallowed here. The caller
		# (``advance_after_turn``) logs the traceback, which is what makes an
		# ImportError or NameError in this path visible instead of silent, and its
		# own guard preserves the contract that a macro bug never strands a normal turn.
		_finish_merge(macro_name, conversation_id, "failed", "")
		raise
	_finish_merge(macro_name, conversation_id, status, merged)


def stop_macro_run(run_name: str) -> dict:
	"""Mark a run stopped so the chaining hook halts. The in-flight turn, if any,
	still finishes; no further steps are enqueued. Exposed via
	``macros_api.stop_macro_run`` (owner-gated there).

	CDX-22: acquire the SAME ``jarvis_macro_run:<run>`` lock the capacity-resume critical section
	holds, and re-read status UNDER it before writing ``stopped``, so a stop can neither erase a
	concurrent resume nor let one start new work after it. Serializes the two; the state-fenced CAS
	in the resume path is the hard backstop if the lock's TTL ever lapses."""
	run = frappe.get_doc(RUN, run_name)
	# Owner-gate the stop action. Checked on the row's owner, not via a "write"
	# permission: run rows are read-only through the document API (macro_permissions).
	if frappe.session.user not in (run.owner, "Administrator"):
		frappe.throw(_("You can only stop your own macro runs."), frappe.PermissionError)
	from jarvis._redis_lock import redis_lock

	# The block body runs whether or not the lock is acquired — a stop must never be silently
	# dropped. In the (rare) not-acquired case the resume path's status-fenced CAS still prevents a
	# resume from clobbering this stop.
	with redis_lock(f"jarvis_macro_run:{run_name}", timeout_s=60, blocking_timeout_s=_STOP_LOCK_BLOCK_S):
		from jarvis.chat import txn

		# The status re-read below must not be from before the wait: the hook may have
		# just ended this run, and a stale `running` here would overwrite its outcome.
		# ``owned``: this is a web request, where the helper is otherwise a no-op;
		# nothing has been written yet and the commit below was coming regardless.
		txn.fresh_snapshot(owned=True)
		# CDX-19: waiting_capacity is a live (non-terminal) run parked for capacity, so it must be
		# stoppable too — otherwise the resume cron would keep re-attempting a run the user stopped.
		if frappe.db.get_value(RUN, run_name, "status") in ("running", "waiting_capacity"):
			frappe.db.set_value(RUN, run.name, {"status": "stopped", "finished_at": frappe.utils.now()})
			_disarm_conversation(run.conversation)  # the flag never outlives the run (T5)
			frappe.db.commit()
			try:
				_publish_done(run, frappe.get_doc(MACRO, run.macro), "stopped")
			except Exception:
				pass
	return {"ok": True}


# --------------------------------------------------------------------------- #
# A step message with no turn: the orphan sweep hands it over
# --------------------------------------------------------------------------- #
_STEP_NOT_STARTED_ERROR = "A step could not be started."


def run_waiting_on_seed(conversation: str, seed: str) -> str | None:
	"""The ``running`` run whose step ``seed`` is, when it is that run's to heal: the
	conversation is the conversation of a ``running`` macro run and ``seed`` is its
	newest step message. None otherwise, and then the message is the orphan sweep's
	to heal the ordinary way (``stale_scan._heal_or_error_orphan``, which has already
	seen that the message is a macro's and has no turn and no reply).

	* Not a parked run (``waiting_capacity``): its leftover message is the resume's
	  business, and a step dispatched for a run that is not `running` has no hook
	  listening for its end.
	* Not an older step message: the run has moved past it.
	* Only where Turn rows are written. Without them "no turn" says nothing (no step
	  has one), the cursor is already past the message, and the sweep's own
	  re-dispatch is the only healer there ever was."""
	from jarvis.chat import admission

	if not admission.turn_machine_enabled():
		return None
	run_name = frappe.db.get_value(RUN, {"conversation": conversation, "status": "running"}, "name")
	if not run_name or _newest_seed(conversation) != seed:
		return None
	return run_name


def heal_dangling_seed(conversation: str, seed: str) -> dict | None:
	"""The orphan sweep found a step message of a live run with no turn and no reply
	(``_enqueue_turn`` committed it and the turn was never created). Give it its turn,
	through ``_dispatch_step``, which reuses the message.

	The sweep used to re-dispatch such a message itself, under a random id. That was a
	second healer for one step: the engine, coming back to the step by another road (a
	repeated event, the resume), did not know that turn and sent the step again.

	Returns what the sweep needs:

	* ``None``: not the engine's after all (the run moved on, or the message is not
	  the prompt of the step the run would send next). The sweep heals it as any other.
	  One such message is not left to the sweep: a second dispatcher's copy of the
	  step the run last sent, which is removed here.
	* ``{"skipped": True}``: the run is busy (its lock is held). Nothing was done and
	  no strike was used; the next sweep comes back.
	* ``{"overloaded": True}``: the site was full and the run is parked. The sweep
	  gives the strike back, as it does for its own re-dispatch.
	* ``{"ok": True}``: handled. The step has its turn, or the attempt failed and is
	  logged; either way the strike is used, and a message still without a turn at the
	  next sweep ends the run (``fail_run_for_seed``).

	Never raises: the sweep's loop would stop at this row."""
	from jarvis._redis_lock import redis_lock
	from jarvis.chat import txn

	try:
		run_name = run_waiting_on_seed(conversation, seed)
		if not run_name:
			return None
		with redis_lock(f"jarvis_macro_run:{run_name}", timeout_s=60, blocking_timeout_s=0.0) as acquired:
			if not acquired:
				return {"skipped": True}
			txn.fresh_snapshot(owned=True)  # the hook may have just moved this run
			if run_waiting_on_seed(conversation, seed) != run_name or frappe.db.exists(
				TURN, {"seed_message": seed}
			):
				return {"skipped": True}
			run = frappe.get_doc(RUN, run_name)
			macro_doc = frappe.get_doc(MACRO, run.macro)
			if _fail_run_unless_one_owner(run, macro_doc):
				return {"ok": True}
			index = _next_step_index(run)
			total = min(run.total_steps or len(macro_doc.steps or []), len(macro_doc.steps or []))
			if index >= total or _unsent_seed(conversation, _step_prompt(run, macro_doc, index)[0]) != seed:
				if _is_a_copy_of_the_last_step(run, macro_doc, index, seed):
					# Not a step that never started: a second dispatcher's copy of the step
					# the run last sent. Re-dispatched, that step would run twice.
					_drop_copies_of_step(run, index - 1)
					return {"ok": True}
				return None
			# The strike, under the lock and durable before the attempt: an attempt that
			# dies must not be free, or a step that cannot start is retried for ever.
			frappe.db.set_value(MSG, seed, "was_recovered", 1, update_modified=False)
			frappe.db.commit()
			result = _dispatch_step(run, macro_doc, index)
			return {"overloaded": True} if result == _STEP_DEFERRED else {"ok": True}
	except Exception:
		try:
			frappe.db.rollback()
		except Exception:
			pass
		frappe.log_error(title="jarvis.chat.macros.heal_dangling_seed_failed", message=frappe.get_traceback())
		try:
			# Whatever failed, it was this message's one retry: without the strike a
			# heal that cannot work is tried, and logged, at every sweep for hours.
			frappe.db.set_value(MSG, seed, "was_recovered", 1, update_modified=False)
		except Exception:
			pass
		frappe.db.commit()
		return {"ok": True}


def _is_a_copy_of_the_last_step(run, macro_doc, index: int, seed: str) -> bool:
	"""Whether ``seed`` (a step message with no turn, and not the message of the step
	the run would send next, ``index``) is a second copy of the step before it: that
	step has its turn, on another message, and ``seed`` carries its prompt."""
	if index < 1:
		return False
	on = frappe.db.get_value(TURN, _step_turn_id(run.name, index - 1), "seed_message")
	if not on or on == seed:
		return False
	steps = macro_doc.steps or []
	if _run_mode(run, macro_doc) != MODE_MERGED and index - 1 >= len(steps):
		return False
	return _same_prompt(frappe.db.get_value(MSG, seed, "content"), _step_prompt(run, macro_doc, index - 1)[0])


def fail_run_for_seed(conversation: str, seed: str) -> bool:
	"""Second strike: the step message was given its one retry and still has no turn.
	End the run, `failed`, the way any run ends (the chat disarmed, a closing line, the
	event an open app listens for, a notification for a scheduled run). It used to stay
	`running`, and armed, until the stale-run sweep hours later. Returns whether this
	call ended it. Never raises.

	Waits for the run lock as a stop does, and like a stop goes ahead without it: the
	write is fenced on `running`, so whatever ended the run in between keeps its
	outcome, and a dispatcher that sends a step into the ended run withdraws it
	(``_fenced_by_an_ended_run``)."""
	from jarvis._redis_lock import redis_lock
	from jarvis.chat import txn

	try:
		run_name = run_waiting_on_seed(conversation, seed)
		if not run_name:
			return False
		with redis_lock(f"jarvis_macro_run:{run_name}", timeout_s=60, blocking_timeout_s=_STOP_LOCK_BLOCK_S):
			txn.fresh_snapshot(owned=True)
			if run_waiting_on_seed(conversation, seed) != run_name or frappe.db.exists(
				TURN, {"seed_message": seed}
			):
				return False
			failed = _cas_run_status(
				run_name, "running", "failed", finished_at=frappe.utils.now(), error=_STEP_NOT_STARTED_ERROR
			)
			frappe.db.commit()
			if not failed:
				return False
			run = frappe.get_doc(RUN, run_name)
			_announce(run, frappe.get_doc(MACRO, run.macro), "failed", _STEP_NOT_STARTED_ERROR)
			return True
	except Exception:
		try:
			frappe.db.rollback()
		except Exception:
			pass
		frappe.log_error(title="jarvis.chat.macros.fail_run_for_seed_failed", message=frappe.get_traceback())
		frappe.db.commit()
		return False


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _skill_invocations(step) -> str:
	"""Render one STEP's tagged skills (a JSON list of Jarvis Custom Skill
	row-names on the step row) as a ``/slug`` invocation line appended to that
	step's prompt — the same mechanism as typing ``/slug`` in the composer, so
	``invoked_skill_clause`` names them at turn time (which also re-checks
	owner/shared visibility). Disabled or since-deleted skills drop out silently.

	How strong that activation is depends on the skill, and the step inherits the
	difference silently (issue #477). A skill the container push writes is named as
	an installed ``custom-<slug>`` and activates deterministically. A Role-scope,
	role-restricted, private or over-cap skill is not on disk, so the clause instead
	instructs the agent to fetch it with ``jarvis__get_skill``: reliable in practice
	but model-mediated, not a container guarantee."""
	try:
		names = frappe.parse_json(step.skills) if step.skills else []
	except Exception:
		names = []
	names = [n for n in names if isinstance(n, str) and n.strip()] if isinstance(names, list) else []
	if not names:
		return ""
	slugs = frappe.get_all(
		"Jarvis Custom Skill",
		filters={"name": ["in", names], "enabled": 1},
		fields=["skill_name"],
		order_by="skill_name asc",
	)
	if not slugs:
		return ""
	return "\n\nApply these skills: " + " ".join(f"/{s.skill_name}" for s in slugs)


<<<<<<< HEAD
def _run_step(run, macro_doc, index: int) -> bool:
	"""Enqueue the turn for step ``index`` (0-based) and stamp current_step. Returns
	True when the step dispatched, False when it was DEFERRED for capacity.
=======
# What ``_dispatch_step`` did (the fence's two answers, above, are the other two).
_STEP_SENT = "sent"  # this call created the step's turn
_STEP_ALREADY_OUT = "already out"  # the turn was there: nothing was sent
_STEP_DEFERRED = "deferred"  # the site was full: the run is parked for the resume
>>>>>>> ab24563 (fix(macros): a step is sent once: a fixed turn id per step and one dispatch function)


def _dispatch_step(run, macro_doc, index: int) -> str:
	"""Send step ``index`` (0-based) of the run, at most once however often and by
	whomever it is asked. THE way a step is sent: the chaining hook, the capacity
	resume, ``run_macro``'s first step and the orphan sweep's hand-off all call this
	and nothing dispatches around it. A summarized run has one step, index 0, and its
	prompt is the summary.

	Returns what happened: ``_STEP_SENT``, ``_STEP_ALREADY_OUT``, ``_STEP_DEFERRED``,
	or the fence's ``_STEP_WITHDRAWN`` / ``_STEP_RUNS_ANYWAY`` when the run had ended
	by the time the turn existed.

	There used to be one dispatch per caller, each writing the cursor after its turn
	existed and each deciding from the cursor alone. A failure between the two, a run
	lock that lapsed, or the sweep healing a message whose turn was a moment away,
	sent the step twice (issue #422). Now the step's turn has a fixed id
	(``_step_turn_id``) and the order is:

	1. The turn exists: the step is out. Send nothing; see that the cursor and the
	   run's status say so (``_step_already_out``).
	2. The newest step message in the chat has no turn and nothing after it, and is
	   this step's prompt (``_unsent_seed``): a dispatcher committed it and never got
	   as far as the turn. Give THAT message its turn. A second message would be a
	   second copy of the step in the chat, and one more thing for the sweep to heal.
	3. Otherwise write the message and ask for the turn (``api._enqueue_turn``).
	4. The site is full: park the run, as before (the message is removed, so the
	   resume starts clean). ``duplicate``: another dispatcher got the turn in
	   between; as 1.
	5. The turn exists and is ours: the look at the run (``_fenced_by_an_ended_run``),
	   then the cursor.

	Nothing the run needs is written before the dispatch: the accept gate rolls back
	on ``duplicate`` and on a full queue.

	The fixed id is used only where a dispatch creates Turn rows
	(``admission.turn_machine_enabled``). Elsewhere (the pump off or draining, without
	Phase-0) there is no row to meet on: the step gets a random id, steps 1 and 2 are
	skipped, and this is the old dispatch. A step sent in the instant a cutover
	re-routes it into the machine has a random id too; ``_went_out_since`` covers the
	hook's side of that."""
	from jarvis.chat import admission, api, txn

	turn_id = _step_turn_id(run.name, index) if admission.turn_machine_enabled() else None
	if turn_id and frappe.db.exists(TURN, turn_id):
		return _step_already_out(run, index)
	prompt, overrides = _step_prompt(run, macro_doc, index)
	seed = _unsent_seed(run.conversation, prompt) if turn_id else None
	if seed:
		# The message is already committed (and with it the overrides the dispatch that
		# wrote it staged on the conversation); only the turn is missing.
		accepted = api._redispatch_orphan(run.conversation, seed, run_id=turn_id, interactive=True)
		out = {"run_id": turn_id, "message_id": seed, **(accepted if isinstance(accepted, dict) else {})}
		if out.get("overloaded") and not _step_is_out_now(turn_id):
			api._delete_enqueue_seed(seed)  # as `_enqueue_turn` does: a parked run leaves no message
	else:
		out = api._enqueue_turn(
			run.conversation, prompt, origin="macro", **overrides, **({"run_id": turn_id} if turn_id else {})
		)
	if isinstance(out, dict) and out.get("overloaded"):
		# Parked, unless another dispatcher got the step's turn while this one was
		# being told the site is full. Looked for again AFTER the park: the other
		# dispatcher un-parks only what it finds parked, and it may have looked first.
		txn.fresh_snapshot(owned=True)  # the park is a write to a row that dispatcher writes too
		_defer_capacity(run, macro_doc)
<<<<<<< HEAD
<<<<<<< HEAD
		return False
	frappe.db.set_value(RUN, run.name, "current_step", index + 1)
	frappe.db.commit()
	run.current_step = index + 1
=======
=======
		if turn_id and _step_is_out_now(turn_id):
			return _step_already_out(run, index)
>>>>>>> f9c8200 (fix(macros): review round 1 for the step identity)
		return _STEP_DEFERRED
	if isinstance(out, dict) and out.get("duplicate"):
		if not seed:
			_drop_the_losers_message(turn_id, out.get("message_id"))
		return _step_already_out(run, index)
	fenced = _fenced_by_an_ended_run(run, out)
	if fenced == _STEP_WITHDRAWN:
		return fenced
	_raise_cursor(run, index + 1, sent=True)
	if fenced:
		return fenced  # the step runs and counts; its run has ended, so no progress event
	_unpark(run)
>>>>>>> ab24563 (fix(macros): a step is sent once: a fixed turn id per step and one dispatch function)
	try:
		_publish_progress(run, macro_doc, index)
	except Exception:
		pass  # a progress event is a courtesy; the step is already running
	return _STEP_SENT


def _step_is_out_now(turn_id: str) -> bool:
	"""Whether the turn exists, read on a fresh snapshot (another dispatcher may have
	created it since this transaction last looked)."""
	from jarvis.chat import txn

	txn.fresh_snapshot(owned=True)
	return bool(frappe.db.exists(TURN, turn_id))


def _step_prompt(run, macro_doc, index: int) -> tuple[str, dict]:
	"""What step ``index`` sends: its prompt, and the model and thinking overrides as
	``_enqueue_turn`` keywords. The shape comes from the run (``_run_mode``, #470),
	never from the live macro: a summarized run sends the summary, with the union of
	the steps' skills and the first override any step sets (the rule the summary was
	made under), whatever ``index`` a caller computed from its cursor. Asked for by
	index alone it would send step 1's prompt, to a run that had already run all of
	them as one."""
	steps = macro_doc.steps or []
	if _run_mode(run, macro_doc) == MODE_MERGED:
		return (
			(macro_doc.merged_prompt or "").strip() + _merged_skill_invocations(macro_doc),
			{
				"model_override": next(
					(s.model_override for s in steps if (s.model_override or "").strip()), None
				),
				"thinking_override": next(
					(s.thinking_override for s in steps if (s.thinking_override or "").strip()), None
				),
			},
		)
	step = steps[index]
	return (
		(step.prompt or "").strip() + _skill_invocations(step),
		{
			"model_override": (step.model_override or None),
			"thinking_override": (step.thinking_override or None),
		},
	)


def _unsent_seed(conversation: str, prompt: str) -> str | None:
	"""The step message that was committed and never got a turn, if the chat's newest
	step message is one: it has no Turn, nothing was written after it, and it carries
	``prompt``. ``_enqueue_turn`` commits the message and only then asks for the turn;
	a worker killed in between, or a raise there, leaves exactly this.

	The prompt is compared because "newest, no turn, nothing after" is also true, for
	a moment, of a step just sent on the legacy path (no Turn rows) when the site
	switches to the turn machine: that message belongs to the step BEFORE the one
	being sent, and giving it this step's id would skip a step. A message that does
	not match is left to the orphan sweep's ordinary heal, as before.

	Only asked where Turn rows are written."""
	newest = frappe.get_all(
		MSG,
		filters={"conversation": conversation, "role": "user", "origin": "macro"},
		fields=["name", "seq", "content"],
		order_by="seq desc",
		limit=1,
	)
	if not newest or not _same_prompt(newest[0].content, prompt):
		return None
	if frappe.db.exists(TURN, {"seed_message": newest[0].name}):
		return None
	if frappe.db.exists(MSG, {"conversation": conversation, "seq": [">", newest[0].seq]}):
		return None
	return newest[0].name


def _drop_unsent_message(run, macro_doc, index: int) -> None:
	"""Remove the message of step ``index`` that was committed without a turn, if there
	is one. Best-effort."""
	from jarvis.chat import admission, api

	try:
		if not admission.turn_machine_enabled():
			return  # no Turn rows: "no turn" says nothing about a message here
		seed = _unsent_seed(run.conversation, _step_prompt(run, macro_doc, index)[0])
		if seed:
			api._delete_enqueue_seed(seed)
	except Exception:
		frappe.log_error(
			title=f"jarvis macro unsent message cleanup failed: {run.name}", message=frappe.get_traceback()
		)


def _same_prompt(stored: str | None, prompt: str) -> bool:
	"""Whether a stored message is ``prompt``. A message's text goes through Frappe's
	HTML sanitizer on insert, which rewrites a prompt that contains markup, so the
	stored text is compared with the prompt both as written and as sanitized."""
	stored, prompt = (stored or "").strip(), (prompt or "").strip()
	if stored == prompt:
		return True
	if "<" not in prompt and ">" not in prompt:
		return False
	try:
		from frappe.utils.html_utils import sanitize_html

		return stored == (sanitize_html(prompt) or "").strip()
	except Exception:
		return False


def _step_already_out(run, index: int) -> str:
	"""Step ``index`` has its turn and this call did not create it: a repeated event,
	a resume after a failure that came later than the turn, a second dispatcher. Send
	nothing. Two things may still be wrong, and are put right:

	* the cursor, left behind by whoever created the turn and got no further. Raised,
	  never lowered;
	* the status. A second dispatcher that met a full queue BEFORE the first got its
	  turn parked the run; a parked run ignores its step's end, and the resume would
	  then send the step after it while this one is still running."""
	from jarvis.chat import txn

	# The writes below must not be judged against a snapshot from before the other
	# dispatcher's (``jarvis.chat.txn``). ``owned``: every dispatcher has committed its
	# own work by here, in a web request too.
	txn.fresh_snapshot(owned=True)
	_raise_cursor(run, index + 1)
	_unpark(run)
	return _STEP_ALREADY_OUT


def _unpark(run) -> None:
	"""The run's step is out, so the run is ``running``: undo a park another
	dispatcher of the same step left (see ``_step_already_out``). Nothing else is
	touched: an ended run stays ended."""
	if _run_status_now(run.name) != "waiting_capacity":
		return
	if _cas_run_status(run.name, "waiting_capacity", "running"):
		run.status = "running"
	frappe.db.commit()


def _drop_the_losers_message(turn_id: str, message: str | None) -> None:
	"""This dispatcher wrote its step message and was then told ``duplicate``: another
	dispatcher's turn carries the step. Remove the message this one wrote, as the
	full-queue branch does, or the chat shows the step twice and the orphan sweep
	later runs the copy. Unless the turn is ON that message: the other dispatcher
	found it without a turn and gave it one (``_dispatch_step`` step 2)."""
	from jarvis.chat import api

	if message and frappe.db.get_value(TURN, turn_id, "seed_message") != message:
		api._delete_enqueue_seed(message)


def _merged_skill_invocations(macro_doc) -> str:
	"""Union of ALL steps' tagged skills (order kept) for the merged
	single-turn run — the summary covers every step, so it inherits every
	step's skills. Same /slug mechanism + visibility rules as per-step."""
	names, seen = [], set()
	for s in macro_doc.steps or []:
		try:
			lst = frappe.parse_json(s.skills) if s.skills else []
		except Exception:
			lst = []
		for n in lst if isinstance(lst, list) else []:
			if isinstance(n, str) and n.strip() and n not in seen:
				seen.add(n)
				names.append(n)
	if not names:
		return ""
	slugs = frappe.get_all(
		"Jarvis Custom Skill",
		filters={"name": ["in", names], "enabled": 1},
		fields=["skill_name"],
		order_by="skill_name asc",
	)
	if not slugs:
		return ""
	return "\n\nApply these skills: " + " ".join(f"/{s.skill_name}" for s in slugs)


<<<<<<< HEAD
def _run_merged(run, macro_doc, merged_prompt: str) -> bool:
	"""Enqueue the macro's summarized prompt as its single turn. Overrides =
	first non-empty among the steps (same rule the merge apply used). Returns True
	when dispatched, False when DEFERRED for capacity (CDX-19: park in
	``waiting_capacity``; the resume cron re-attempts the merged turn next cycle)."""
	steps = macro_doc.steps or []
	model_o = next((s.model_override for s in steps if (s.model_override or "").strip()), None)
	think_o = next((s.thinking_override for s in steps if (s.thinking_override or "").strip()), None)
	from jarvis.chat import api

	out = api._enqueue_turn(
		run.conversation,
		merged_prompt + _merged_skill_invocations(macro_doc),
		origin="macro",
		model_override=model_o,
		thinking_override=think_o,
	)
	if isinstance(out, dict) and out.get("overloaded"):
		_defer_capacity(run, macro_doc)
		return False
	frappe.db.set_value(RUN, run.name, "current_step", 1)
	frappe.db.commit()
	run.current_step = 1
	publish_to_user(
		macro_doc.owner,
		{
			"kind": "macro:progress",
			"macro_run": run.name,
			"macro": macro_doc.name,
			"conversation": run.conversation,
			"step": 1,
			"total": 1,
			"label": "Summarized prompt",
			"status": "running",
		},
	)
	return True


=======
>>>>>>> ab24563 (fix(macros): a step is sent once: a fixed turn id per step and one dispatch function)
def _defer_capacity(run, macro_doc) -> None:
	"""CDX-19: the site's turn queue was momentarily full at the accept gate, so the current
	step could not be admitted (its seed was cleaned up, no turn dispatched). Park the run in
	``waiting_capacity`` WITHOUT advancing ``current_step`` — ``resume_waiting_capacity_runs``
	re-attempts the SAME step on its next cron cycle. Idempotent: only flips a ``running`` run
	(a stop/finish that already moved on wins)."""
	if frappe.db.get_value(RUN, run.name, "status") != "running":
		return
	frappe.db.set_value(RUN, run.name, {"status": "waiting_capacity"}, update_modified=True)
	frappe.db.commit()
	run.status = "waiting_capacity"
	try:
		publish_to_user(
			macro_doc.owner,
			{
				"kind": "macro:progress",
				"macro_run": run.name,
				"macro": macro_doc.name,
				"conversation": run.conversation,
				"step": (run.current_step or 0) + 1,
				"total": run.total_steps,
				"label": "Waiting for capacity",
				"status": "waiting_capacity",
			},
		)
	except Exception:
		pass


def resume_waiting_capacity_runs() -> None:
	"""Cron backstop (chat-concurrency CDX-19): re-attempt every macro run parked in
	``waiting_capacity``. A run lands there when a step could not be admitted because the
	site's turn queue was momentarily full (``_enqueue_turn`` returned overloaded). This is
	the ONLY re-attempt path for an in-flight step — a deferred step dispatches no turn, so
	the turn-end chaining hook (``advance_after_turn``) never fires for it.

	Bounded: ``capacity_attempts`` is incremented each cycle; once it exceeds
	``_MAX_CAPACITY_ATTEMPTS`` the run takes its NORMAL failure path with an honest reason
	rather than retrying forever. Serialized + idempotent via the same per-run redis lock
	the chaining hook uses, so a resume can never race a late step advance. Never raises.

	#470: the macro is re-read fresh here, but only for its CONTENT. Which shape to
	dispatch comes from the run's own ``run_mode`` snapshot, never from live macro state.
	The park window is long (``_MAX_CAPACITY_ATTEMPTS`` x the 5 min cron, ~100 min) and
	macro edits do not take this lock, so re-deriving the shape let an edit land mid-park
	and change what a live run executed."""
	rows = frappe.get_all(RUN, filters={"status": "waiting_capacity"}, pluck="name")
	if not rows:
		return
	from jarvis._redis_lock import redis_lock
	from jarvis.permissions import is_valid_unattended_owner

	for run_name in rows:
		try:
			with redis_lock(f"jarvis_macro_run:{run_name}", timeout_s=60, blocking_timeout_s=0.0) as acquired:
				if not acquired:
					continue
				run = frappe.get_doc(RUN, run_name)
				if run.status != "waiting_capacity":
					continue
				macro_doc = frappe.get_doc(MACRO, run.macro)
				if _fail_run_unless_one_owner(run, macro_doc):
					continue
				attempts = int(run.capacity_attempts or 0) + 1
				if attempts > _MAX_CAPACITY_ATTEMPTS:
					_end_run(run, macro_doc, "failed", _NO_CAPACITY_ERROR)
					continue
				# #470: end the run before spending an attempt on work its snapshot can no
				# longer describe. State-fenced off waiting_capacity so a stop that landed
				# first is never clobbered.
				if _resume_incompatible(run, macro_doc):
					if _cas_run_status(
						run.name,
						"waiting_capacity",
						"failed",
						finished_at=frappe.utils.now(),
						error=_MACRO_CHANGED_ERROR,
					):
						frappe.db.commit()
						_announce(run, macro_doc, "failed", _MACRO_CHANGED_ERROR)
					else:
						frappe.db.commit()
					continue
				if not is_valid_unattended_owner(frappe.db.get_value(CONV, run.conversation, "owner")):
					if _cas_run_status(
						run.name,
						"waiting_capacity",
						"failed",
						finished_at=frappe.utils.now(),
						error=_OWNER_INELIGIBLE_ERROR,
					):
						frappe.db.commit()
						_announce(run, macro_doc, "failed", _OWNER_INELIGIBLE_ERROR)
					else:
						frappe.db.commit()
					continue
				# CDX-22: flip waiting_capacity -> running with a STATE-FENCED CAS (not a blind set),
				# recording the attempt BEFORE re-enqueue so a re-overload keeps the bounded count. The
				# fence closes the stop-before-resume-write race: a stop that already set ``stopped``
				# matches 0 rows here and we abort WITHOUT starting work (never clobber the stop).
				if not _cas_run_status(run.name, "waiting_capacity", "running", capacity_attempts=attempts):
					frappe.db.commit()
					continue
				frappe.db.commit()
				run.status = "running"
				run.capacity_attempts = attempts
				# CDX-22 (belt): re-read status UNDER the lock IMMEDIATELY before enqueue. If a stop
				# landed after the flip (e.g. the redis lock's TTL lapsed), abort WITHOUT dispatching a
				# turn — never start work after the user's explicit stop. The stop already wrote
				# ``stopped``, so no restore is owed.
				if _run_status_now(run.name) != "running":
					continue
				# #470: the run's OWN snapshot decides the shape (``_step_prompt``). This
				# branch used to read ``macro_doc.merged_prompt`` live, so a summary that landed
				# during the park turned a part-executed stepped run merged (re-running steps
				# 2..N after the merged turn), and a step edit that cleared the summary turned a
				# merged run stepped (one step ran, then ``advance_after_turn`` computed
				# total=min(1,N)=1 and reported the run COMPLETED).
				#
				# The step the run parked at, exactly: not ``_next_step_index``. If that
				# step is already out (an earlier resume created its turn and then failed),
				# ``_dispatch_step`` says so and the run simply stays `running`.
				index = 0 if _run_mode(run, macro_doc) == MODE_MERGED else int(run.current_step or 0)
				try:
					_dispatch_step(run, macro_doc, index)
				except Exception:
					# CDX-23: the waiting_capacity -> running flip already committed; an enqueue that
					# RAISED (session setup / Message persistence / dispatch) leaves NO Turn and NO owner
					# (no terminal hook, and the run is no longer waiting_capacity for this cron) — it
					# would sit ``running`` forever, bypassing the bounded-attempt promise. Compensate
					# atomically under the lock.
					frappe.log_error(
						title="jarvis macro capacity-resume enqueue raised", message=frappe.get_traceback()
					)
					_compensate_resume_enqueue_failure(run, macro_doc, attempts, index=index)
		except Exception:
			frappe.log_error(title="jarvis macro capacity-resume failed", message=frappe.get_traceback())


def _run_mode(run, macro_doc) -> str:
	"""The shape this run was DISPATCHED in (#470).

	An explicit column rather than reusing ``total_steps == 1`` as the merged marker.
	``total_steps`` is already the loop bound ``advance_after_turn`` reads
	(``min(run.total_steps or len(steps), len(steps))``), so overloading it would make one
	column mean two things and tie any future change to either meaning to the other. It is
	also ambiguous on its own: a genuine ONE-STEP macro run as a sequence has
	``total_steps == 1`` too.

	A blank value means a row written before the column existed. It can only be a run that
	was live across the deploy, so the window is at most one park (~100 min), and
	``total_steps`` is the only dispatch-time record such a row carries. Resolve its
	one-step ambiguity toward ``stepped``: for a one-step macro the two shapes dispatch
	near-identical prompts, and ``stepped`` is the branch that can neither re-run a
	completed step nor complete early."""
	mode = (run.get("run_mode") or "").strip()
	if mode in (MODE_MERGED, MODE_STEPPED):
		return mode
	steps = macro_doc.steps or []
	return MODE_MERGED if int(run.total_steps or 0) == 1 and len(steps) > 1 else MODE_STEPPED


def _resume_incompatible(run, macro_doc) -> bool:
	"""#470: True when the macro was edited during the park in a way that leaves the run's
	dispatch-time shape unrunnable. Macro edits take no per-run lock, so the resume path is
	where that is noticed.

	* **merged, summary gone.** ``update_macro`` clears ``merged_prompt`` the moment the
	  steps change, precisely because the summary describes the OLD steps. Running it
	  anyway would execute the intent the user just replaced. A merged run parks BEFORE its
	  only turn (``_dispatch_step`` defers ahead of the ``current_step`` write, and
	  ``advance_after_turn`` never re-enters merged), so nothing has executed and ending
	  the run costs no work.
	* **stepped, step list shrank past the cursor.** ``macro_doc.steps[index]`` would
	  IndexError, and the CDX-23 compensation would then restore ``waiting_capacity`` and
	  retry that certain IndexError once per cron cycle until the attempt cap ~100 min
	  later."""
	if _run_mode(run, macro_doc) == MODE_MERGED:
		return not (macro_doc.merged_prompt or "").strip()
	return int(run.current_step or 0) >= len(macro_doc.steps or [])


def _compensate_resume_enqueue_failure(run, macro_doc, attempts: int, *, index: int | None = None) -> None:
	"""CDX-23: a resumed step's enqueue raised AFTER the ``waiting_capacity -> running`` flip
	committed. Restore the run to a re-attemptable state atomically (state-fenced on ``running`` so a
	stop that landed meanwhile is never clobbered): at the attempt CAP take the documented honest
	failure terminal; otherwise restore ``waiting_capacity`` (the attempt was already counted at the
	flip, so the bound still holds) so the next cron retries the SAME step. Best-effort — never
	re-raises into the resume loop.

	Only when the step did not go out. A raise that came after the step's turn existed left a
	running step in a run parked again: its end was ignored (the run was not `running`), and the
	next resume sent the same step a second time. With the turn there, the run stays `running`
	and the cursor is put on the step after it. ``index``: the step the resume was sending."""
	try:
		if index is None:
			index = 0 if _run_mode(run, macro_doc) == MODE_MERGED else int(run.current_step or 0)
		if _step_went_out_anyway(run, index):
			return
		# The step did not go out, but its message may be committed (``_enqueue_turn``
		# commits it before it asks for the turn). A parked run leaves no message: the
		# orphan sweep heals a parked run's message the ordinary way, under an id the
		# resume does not know, and the resume would then send the step again.
		_drop_unsent_message(run, macro_doc, index)
		if attempts >= _MAX_CAPACITY_ATTEMPTS:
			if _cas_run_status(
				run.name,
				"running",
				"failed",
				finished_at=frappe.utils.now(),
				error=_NO_CAPACITY_ERROR,
			):
				frappe.db.commit()
				_announce(run, macro_doc, "failed", _NO_CAPACITY_ERROR)
			else:
				frappe.db.commit()
			return
		if _cas_run_status(run.name, "running", "waiting_capacity"):
			frappe.db.commit()
			try:
				publish_to_user(
					macro_doc.owner,
					{
						"kind": "macro:progress",
						"macro_run": run.name,
						"macro": macro_doc.name,
						"conversation": run.conversation,
						"step": (run.current_step or 0) + 1,
						"total": run.total_steps,
						"label": "Waiting for capacity",
						"status": "waiting_capacity",
					},
				)
			except Exception:
				pass
		else:
			frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="jarvis macro capacity-resume compensate", message=frappe.get_traceback())


# --------------------------------------------------------------------------- #
# #468 entitlement + unattended-spend gate
# --------------------------------------------------------------------------- #
def entitlement_block(owner: str, *, steps: int, trigger: str) -> str | None:
	"""The machine reason a macro run for ``owner`` must NOT dispatch, or None.

	Two layers, both missing before #468:

	1. **The same entitlement gate a typed message passes.**
	   ``policy.validate_can_send`` had exactly three call sites — ``send_message``
	   (x2) and ``retry_message`` — all human paths. ``api._enqueue_turn``, the SOLE
	   dispatch path for macro steps, had none, and neither did ``dispatch``,
	   ``admission``, ``turn_handler`` or ``worker``. So a macro step bypassed the
	   per-user monthly token cap, the per-model cap and ``subscription_suspended``
	   while ``turn_handler.record_turn_usage`` metered it anyway. The inversion was
	   complete: the macro drained the owner's quota and the quota then rejected the
	   owner's own interactive chat while the macro kept running.

	   The gate is applied at the RUN, not per step, and no model is passed: a
	   macro's model is resolved per conversation at dispatch time, and the
	   aggregate monthly cap is the outer gate anyway (policy already documents the
	   Auto/per-model skip as an accepted gap).

	2. **A monthly step budget, for SCHEDULED runs only.** This is the path with no
	   human in it and no other bound. A manual run is an explicit, attended click
	   that the token caps above already price; refusing one on a monthly *run*
	   quota would break a legitimate workflow with no cost story to justify it.
	   Manual runs are not counted either, so a heavy interactive month cannot
	   silently starve the schedule.

	Mirrors ``agent_scheduler._over_run_budget`` in shape, and differs in key: an
	agent budget is keyed on the INSTALLATION because ``run_as_user`` decouples the
	executing identity from the owner. A macro has no such split, so the owner is
	both the executing identity and the metered one."""
	from jarvis.chat.policy import validate_can_send

	ok, reason = validate_can_send(owner)
	if not ok:
		return reason
	if trigger == "scheduled" and _over_step_budget(owner, steps):
		return BLOCK_STEP_BUDGET
	return None


def _scheduled_step_budget_monthly() -> int:
	"""The configured budget, or the default when it is unset.

	A configured value is CLAMPED UP to ``MIN_SCHEDULED_STEP_BUDGET_MONTHLY`` rather
	than replaced by the default, so an admin who deliberately sets a tight cap gets
	the tightest cap that still permits a daily single-step schedule, not a silent
	16x widening to the default. Unset/blank/unreadable means "no opinion" and takes
	the default."""
	try:
		v = frappe.utils.cint(frappe.db.get_single_value("Jarvis Settings", "macro_step_budget_monthly"))
	except Exception:
		v = 0
	if v <= 0:
		return DEFAULT_SCHEDULED_STEP_BUDGET_MONTHLY
	return max(v, MIN_SCHEDULED_STEP_BUDGET_MONTHLY)


def _scheduled_steps_this_month(owner: str) -> int:
	"""Agent turns this owner's SCHEDULED macro runs have dispatched this month.

	EVERY status counts, because ``current_step`` (how many steps a run actually
	dispatched) is already the honest measure of spend on its own:

	* a refusal recorded by ``macro_scheduler._record_failed`` carries
	  ``current_step = 0``, so it contributes nothing and the cap can never become
	  self-perpetuating. That is what the sibling's ``status != 'failed'`` filter
	  buys, obtained here structurally instead.
	* a run that failed PART WAY THROUGH really did dispatch and bill its completed
	  steps, and three paths produce exactly that (``stop_on_error``, the
	  capacity-attempt cap, and the stale-run sweep). Filtering on status would erase
	  those turns from the tally, so an owner whose macros keep failing halfway could
	  spend past the budget indefinitely — reopening the very "spends without ever
	  being refused by it" inversion #468 exists to close.
	* a ``stopped`` run likewise billed the steps it ran before the user stopped it.

	So an in-flight or part-failed run contributes honestly, not all-or-nothing."""
	row = frappe.db.sql(
		f"""SELECT COALESCE(SUM(current_step), 0) FROM `tab{RUN}`
		    WHERE owner = %(owner)s AND `trigger` = 'scheduled' AND creation >= %(since)s""",
		{"owner": owner, "since": frappe.utils.get_first_day(frappe.utils.today())},
	)
	return int(row[0][0] or 0) if row else 0


def _over_step_budget(owner: str, steps: int) -> bool:
	"""True iff dispatching ``steps`` more scheduled steps would breach the budget."""
	return (_scheduled_steps_this_month(owner) + max(int(steps or 1), 1)) > _scheduled_step_budget_monthly()


# --------------------------------------------------------------------------- #
# #471 stale-run reaper (backstop) — hooks cron
# --------------------------------------------------------------------------- #
def reap_stale_macro_runs() -> int:
	"""Terminalize macro runs stuck ``running`` with no forward progress, and return
	how many were reaped. Runs as Administrator (scheduler); never raises out.

	Without this a run orphaned before its first turn sat ``running`` FOREVER with an
	empty ``error`` (#471): ``run_macro`` dispatches through ``api._enqueue_turn``,
	whose ``_ensure_session_key`` throws when the gateway is down, and with no turn
	dispatched the chaining hook that terminalizes a run never fires for it.

	**Parked is not stranded.** Two independent discriminators keep a legitimately
	slow or deliberately parked run safe:

	* **Status is an ALLOWLIST of exactly ``running``.** ``waiting_capacity`` is a
	  live, deliberately parked state (CDX-19): a step that could not be admitted
	  waits there for ``resume_waiting_capacity_runs``, legitimately for up to
	  ``_MAX_CAPACITY_ATTEMPTS`` x 5 min (~100 min), and that path has its OWN bound
	  after which it fails honestly. It is never a candidate here — and because the
	  filter is an allowlist rather than "not terminal", any future non-terminal
	  status is excluded by construction too.
	* **Staleness is measured on ``modified`` (last forward progress), never on
	  ``started_at``.** A 25-step macro legitimately stays ``running`` for hours;
	  what it never does is stop making progress, because every step dispatch,
	  capacity park and capacity resume writes the row.

	Then, before acting, each candidate is re-read UNDER the same per-run redis lock
	the chaining hook and the capacity resume take, plus a row lock, and transitioned
	with a compare-and-set on ``status='running'`` — so a run that advanced between
	the scan and here is left alone rather than having a live step killed under it."""
	cutoff = now_datetime() - timedelta(seconds=STALE_RUN_AFTER_SECONDS)
	candidates = _stale_run_candidates(cutoff)
	if not candidates:
		return 0
	from jarvis._redis_lock import redis_lock

	reaped = 0
	for run_name in candidates:
		try:
			with redis_lock(f"jarvis_macro_run:{run_name}", timeout_s=60, blocking_timeout_s=0.0) as acquired:
				if not acquired:
					# A chaining advance / capacity resume is INSIDE its critical section for
					# this run right now, which is proof of life. Leave it for the next sweep.
					continue
				# REPEATABLE-READ discipline, matching agent_scheduler.fail_run: commit to
				# close the current snapshot so the FOR UPDATE read below sees the row as it
				# is NOW, not as this connection's transaction first saw it.
				frappe.db.commit()
				cur = frappe.db.get_value(
					RUN, run_name, ["status", "modified"], as_dict=True, for_update=True
				)
				if not cur or cur.status != "running" or get_datetime(cur.modified) >= cutoff:
					frappe.db.commit()  # release the row lock; the snapshot was stale
					continue
				if not _cas_run_status(
					run_name, "running", "failed", finished_at=frappe.utils.now(), error=_STALE_RUN_ERROR
				):
					frappe.db.commit()
					continue
				frappe.db.commit()
				reaped += 1
				_announce_reaped(run_name)
		except Exception:
			# Roll back explicitly: an exception between the FOR UPDATE read and its
			# balancing commit would otherwise leave the row lock and any half-applied
			# state open, to be resolved as a side effect of the NEXT candidate's commit.
			frappe.db.rollback()
			frappe.log_error(title="jarvis macro stale-run sweep failed", message=frappe.get_traceback())
	return reaped


def _stale_run_candidates(cutoff) -> list[str]:
	"""Runs stuck ``running`` with no forward progress since ``cutoff``. Its own
	function so the re-read-under-lock compare-and-set can be exercised against a
	deliberately STALE candidate snapshot in tests."""
	return frappe.get_all(RUN, filters={"status": "running", "modified": ["<", cutoff]}, pluck="name")


def _announce_reaped(run_name: str) -> None:
	"""Surface a reaped run the two ways a live failure is surfaced: the realtime
	``macro:done`` an open SPA listens for, and a Notification Log for the (likely
	absent) owner. Best-effort — the durable ``failed`` row is the real record."""
	try:
		run = frappe.get_doc(RUN, run_name)
		macro_doc = frappe.get_doc(MACRO, run.macro)
		_publish_done(run, macro_doc, "failed", _STALE_RUN_ERROR)
		notify_owner(
			macro_doc.owner,
			subject=f"Macro run did not finish: {macro_doc.macro_name}",
			body=_STALE_RUN_ERROR,
		)
	except Exception:
		pass


def notify_owner(owner: str, *, subject: str, body: str) -> None:
	"""Best-effort Notification Log for a macro owner (#471). A scheduled macro that
	fails at 03:00 reaches nobody through the realtime channel — ``publish_to_user``
	is delivered only to an open socket, and there is none — and ``frappe.log_error``
	writes an Error Log only a System Manager can read.

	Skipped for identities that cannot act on one (Administrator / Guest / disabled).
	Never raises: notification is a courtesy on top of the durable failed run row, and
	the scheduler's per-macro loop has no outer guard, so an escape here would abort
	the sweep for every REMAINING due macro. The eligibility check is therefore inside
	the try too: it reads ``User.enabled`` from the DB and can fail like any query."""
	try:
		from jarvis.permissions import is_valid_unattended_owner

		if not is_valid_unattended_owner(owner):
			return
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": owner,
				"type": "Alert",
				"subject": subject[:140],
				"email_content": body,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		pass


_OWNER_MISMATCH_ERROR = "This run was stopped: its macro and conversation do not belong to one user."
_CONVERSATION_GONE_ERROR = "This run was stopped because its conversation was deleted."


def _fail_run_unless_one_owner(run, macro_doc) -> bool:
	"""FAILS THE RUN and returns True unless the run, its macro and its conversation all
	belong to one user. Returns False, touching nothing, when they do.

	They do by construction: ``run_macro`` creates all three for the macro's owner. A
	step is dispatched as the CONVERSATION's owner, so a run row pointing at someone
	else's conversation would execute this macro's prompts under their identity. Run
	rows are read-only to users (``macro_permissions``); this is the engine declining
	to act on a row it did not write that way, whatever did.

	A conversation that no longer exists is a different, ordinary case ("delete all
	conversations" blanks the link on the owner's own runs): the run fails plainly,
	with no mismatch alarm."""
	conv_owner = frappe.db.get_value(CONV, run.conversation, "owner") if run.conversation else None
	if conv_owner is None:
		_fail_run_in_place(run, _CONVERSATION_GONE_ERROR)
		if run.owner == macro_doc.owner:
			_announce(run, macro_doc, "failed", _CONVERSATION_GONE_ERROR)
		return True
	if run.owner == macro_doc.owner == conv_owner:
		return False
	# Titled as a dotted ``jarvis.`` path on purpose: this log carries no traceback,
	# and that title is what lets the tenant error rollup (api_errors.is_jarvis_error)
	# forward it to the control plane. It should never fire, so someone must see it.
	frappe.log_error(
		title=f"jarvis.chat.macros.run_owner_mismatch: {run.name}",
		message=(
			f"run owner {run.owner!r}; macro {run.macro!r} owner {macro_doc.owner!r}; "
			f"conversation {run.conversation!r} owner {conv_owner!r}"
		),
	)
	_fail_run_in_place(run, _OWNER_MISMATCH_ERROR)
	return True


def _fail_run_in_place(run, error: str) -> None:
	"""Terminalize a run with a raw write, NOT ``_finish``. ``_finish`` disarms
	``run.conversation``, and on these paths that is either gone or a conversation this
	run has no claim on."""
	frappe.db.set_value(
		RUN,
		run.name,
		{"status": "failed", "finished_at": frappe.utils.now(), "error": error},
	)
	frappe.db.commit()


def _finish(run, status: str, error: str | None = None) -> None:
	frappe.db.set_value(
		RUN,
		run.name,
		{"status": status, "finished_at": frappe.utils.now(), "error": (error or "")[:500]},
	)
	_disarm_conversation(run.conversation)  # the flag never outlives the run (T5)
	frappe.db.commit()


def _publish_progress(run, macro_doc, index: int) -> None:
	if _run_mode(run, macro_doc) == MODE_MERGED:
		step, total, label = 1, 1, "Summarized prompt"
	else:
		step, total = index + 1, run.total_steps
		label = (macro_doc.steps[index].label or "").strip() or f"Step {index + 1}"
	publish_to_user(
		macro_doc.owner,
		{
			"kind": "macro:progress",
			"macro_run": run.name,
			"macro": macro_doc.name,
			"conversation": run.conversation,
			"step": step,
			"total": total,
			"label": label,
			"status": "running",
		},
	)


def _publish_done(run, macro_doc, status: str, error: str = "") -> None:
	publish_to_user(
		macro_doc.owner,
		{
			"kind": "macro:done",
			"macro_run": run.name,
			"macro": macro_doc.name,
			"macro_name": macro_doc.macro_name,
			"conversation": run.conversation,
			"status": status,
			# Why it ended that way, so an open app can say so without a second request.
			"error": (error or "")[:500],
			"trigger": run.get("trigger") or "manual",
		},
	)


_OUTCOME_SUBJECT = {
	"failed": "Macro run failed",
	"stopped": "Macro run stopped",
	"completed": "Macro run finished",
}
# "■" alone for a stop: its reason already begins "Stopped at step ...".
_CLOSING_MARK = {"failed": "✗ Macro failed.", "stopped": "■", "completed": "✓ Macro finished."}
# A reply that ends with one of these is a card the chat keeps open, on the LAST
# assistant message only (ChatView's `_lastAssistant`; the PWA has the same rule).
# The same shape the clients match (a CLOSED fence): a reply cut off mid-fence shows
# no card there, and must not hold the closing message back.
_LIVE_CARD_RE = re.compile(r"```(?:jarvis-action|jarvis-ask|confirm)[ \t]*\n[\s\S]*?```")


def _last_reply_holds_a_card(conversation: str) -> bool:
	"""Whether the conversation's last message is an assistant reply with a card on
	it (a draft to apply, a question, a confirm). Tool rows are skipped, as the
	clients skip them."""
	rows = frappe.get_all(
		MSG,
		filters={"conversation": conversation, "role": ["!=", "tool"]},
		fields=["role", "content"],
		order_by="seq desc",
		limit=1,
	)
	return bool(rows and rows[0].role == "assistant" and _LIVE_CARD_RE.search(rows[0].content or ""))


def _post_closing_message(run, status: str, note: str) -> None:
	"""Leave the run's outcome IN its conversation, as the last message.

	The run opens its conversation with "Running macro ..."; nothing closed it. The
	reason a run failed or stopped lived on the run row and in a realtime event, so
	someone opening the conversation later (from a notification, from the Runs tab, on
	a phone) found a transcript that just ends. A message is the one thing every
	surface already shows.

	Only when there is something to say (a clean ``completed`` needs no epilogue),
	and never after a reply that still holds a card (``_last_reply_holds_a_card``).
	Posted exactly once per run: the natural-key dedupe on (conversation, role,
	run) is taken under the conversation lock, like the import announcement."""
	if not note or not run.conversation:
		return
	conv = frappe.db.get_value(CONV, run.conversation, ["owner", "status"], as_dict=True)
	if not conv or not conv.owner or conv.status == "Archived":
		return
	if _last_reply_holds_a_card(run.conversation):
		# A message after it would close that card for good: the chat shows a card on
		# the last assistant message only. The draft the owner is being told to apply
		# would vanish. The run row, the list and the form still carry the note.
		return
	from jarvis._session import impersonate
	from jarvis.api import _locked_insert_chat_message

	message = None
	with impersonate(conv.owner):
		frappe.db.commit()  # commit-first: the FOR UPDATE is the first statement
		message = _locked_insert_chat_message(
			run.conversation,
			{
				"role": "assistant",
				"content": f"{_CLOSING_MARK.get(status, 'Macro run ended.')} {note}".strip(),
				"hidden": 0,
				"ref_doctype": RUN,
				"ref_name": run.name,
			},
			dedupe_filters={
				"conversation": run.conversation,
				"role": "assistant",
				"ref_doctype": RUN,
				"ref_name": run.name,
			},
		)
		frappe.db.commit()
	if message:
		publish_to_user(
			conv.owner,
			{"kind": "macro:closed", "conversation_id": run.conversation, "message_id": message},
		)


def _announce(run, macro_doc, status: str, error: str = "") -> None:
	"""Tell the owner how a run ended: a closing message in the run's conversation,
	the realtime event an open app listens for and, for a SCHEDULED run that did not
	simply work, a Notification Log.

	A realtime event reaches an open socket only, and nobody is watching a scheduled
	run. Before this, a scheduled run that failed part-way told nobody: only the
	scheduler's own refusals and the stale-run sweep wrote a notification. A manual
	run writes none; whoever started it gets the event. Never raises: each part is
	best-effort on top of the run row, which is the record."""
	try:
		_post_closing_message(run, status, error)
	except Exception:
		frappe.log_error(title="jarvis macro closing message failed", message=frappe.get_traceback())
	try:
		_publish_done(run, macro_doc, status, error)
	except Exception:
		frappe.log_error(title="jarvis macro done event failed", message=frappe.get_traceback())
	if (run.get("trigger") or "manual") != "scheduled" or not error:
		return
	notify_owner(
		macro_doc.owner,
		subject=f"{_OUTCOME_SUBJECT.get(status, 'Macro run ended')}: {macro_doc.macro_name}",
		body=error,
	)


def _end_run(run, macro_doc, status: str, error: str = "") -> None:
	"""Terminalize a run and say so: how a run ends from the chaining hook and the
	capacity resume. (``stop_macro_run`` and the owner-mismatch failure do not come
	through here: the first is the user's own act, the second must not write to a
	conversation the run has no claim on.)"""
	_finish(run, status, error=error)
	_announce(run, macro_doc, status, error)
