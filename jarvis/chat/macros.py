"""Macro execution engine.

A macro (``Jarvis Macro``) is an ordered list of prompts. Running one creates a
fresh conversation and executes each prompt as its own agent turn, chained
server-side: after a turn's ``run:end`` (or an error) the chaining hook in
``jarvis.chat.turn_handler`` calls :func:`advance_after_turn`, which enqueues the
next step. Because the steps run as turns in one conversation they share the
agent session, so context accumulates across the macro — and the run survives
a closed browser tab (unlike a frontend-driven loop).

State lives in ``Jarvis Macro Run`` (``current_step`` = 1-based index of the step
last started; ``status`` in queued/running/completed/failed/stopped). Progress is
pushed to the owner's realtime channel as ``macro:progress`` / ``macro:done``.

A run also records the SHAPE it was dispatched in (``run_mode``, #470). Everything
that re-enters a live run reads that snapshot rather than re-deriving the shape from
the macro, because the macro is editable while the run is in flight.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from jarvis.chat.events import publish_to_user

MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"

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

# Human sentences for the MANUAL path (thrown, so the SPA's existing toast renders
# them). The scheduled path reports the machine code instead and the scheduler
# writes its own owner-facing sentence onto the failed run row.
_BLOCK_MESSAGE = {
	"usage_limit": "You have reached your monthly usage limit, so this macro cannot run.",
	"subscription_suspended": "Your subscription does not currently include chat, so this macro cannot run.",
	"release_update_required": "A Jarvis update is rolling out. Try this macro again in a few minutes.",
	"workspace_resetting": "Your workspace is being rebuilt. Try this macro again in a few minutes.",
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
	when unset. Caller commits (consistent with ``_cas_run_status``)."""
	if conversation and frappe.db.get_value(CONV, conversation, "skip_confirmation"):
		frappe.db.set_value(CONV, conversation, "skip_confirmation", 0, update_modified=False)


def _disarm_run_conversation(run_name: str) -> None:
	"""Disarm by run name (for the _cas terminal paths, which only have the run)."""
	_disarm_conversation(frappe.db.get_value(RUN, run_name, "conversation"))


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def run_macro(macro_name: str, *, trigger: str = "manual") -> dict:
	"""Start a macro: create a fresh conversation, a Macro Run, and enqueue the
	first step. Returns ``{ok, data:{macro_run, conversation}}``. ``trigger`` is
	``manual`` (user clicked Run) or ``scheduled`` (the cron)."""
	doc = frappe.get_doc(MACRO, macro_name)
	doc.check_permission("read")  # owner-gate: get_doc alone doesn't enforce if_owner
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
	# transcript reads as a self-contained run.
	conv = frappe.get_doc({"doctype": CONV, "title": doc.macro_name[:140], "status": "Active"})
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
		if merged:
			_run_merged(run, doc, merged)
		else:
			_run_step(run, doc, 0)
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
		_finish(run, "failed", error=_DISPATCH_FAILED_ERROR)
		try:
			_publish_done(run, doc, "failed")
		except Exception:
			pass
		if trigger == "scheduled":
			return {"ok": False, "reason": BLOCK_DISPATCH_FAILED}
		frappe.throw(_(_BLOCK_MESSAGE[BLOCK_DISPATCH_FAILED]))
	return {"ok": True, "data": {"macro_run": run.name, "conversation": conv.name}}


def _armed_run_parked_card(conversation: str, owner: str) -> dict | None:
	"""The first live confirmation card STRICTLY bound to ``conversation`` (or None).

	An armed macro runs the covered set uncarded, so the ONLY thing that parks a card
	on its run is an EXCLUDED tool (delete/cancel/amend) - i.e. the D5 stop signal.
	Mirrors the gate's own strict-conversation filter (a conv-less token surfaces
	under any filter). A storage error is swallowed to None: a pending-confirm outage
	must not strand the run - it then advances as before, and the human-inert send
	block + clear-on-terminal still hold the security line."""
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
	covered steps before it already ran, so re-running the whole macro repeats them."""
	what = parked.get("summary") or parked.get("tool") or "a confirmation"
	return (
		f"Stopped at step {current_step + 1}: this step needs a confirmation that can't be "
		f"shown in an unattended run ({what}). The steps before it already ran, so "
		f"re-running the whole macro would repeat them - fix this step's data or run it "
		f"interactively, don't re-run the macro. Nothing after this step ran."
	)[:500]


def advance_after_turn(conversation_id: str, *, errored: bool) -> None:
	"""Chaining hook, called from ``turn_handler`` after every terminal turn
	outcome. If the conversation belongs to a ``running`` Macro Run, advance it:
	enqueue the next step, or finish. If it's a macro's background SUMMARIZE
	turn, apply the summary to the macro. No-op for normal (non-macro) chats.

	Best-effort: never raises (a macro bug must not strand a normal turn).
	Serialized + idempotent via a per-run redis lock + the ``current_step`` guard
	so a re-delivered event / RQ retry can't double-advance."""
	try:
		_apply_merge_after_turn(conversation_id, errored=errored)
	except Exception:
		frappe.log_error(title="jarvis macro merge-apply failed", message=frappe.get_traceback())
	try:
		run_name = frappe.db.get_value(RUN, {"conversation": conversation_id, "status": "running"}, "name")
		if not run_name:
			return
		from jarvis._redis_lock import redis_lock

		with redis_lock(f"jarvis_macro_run:{run_name}", timeout_s=60, blocking_timeout_s=10.0) as acquired:
			if not acquired:
				return
			run = frappe.get_doc(RUN, run_name)
			if run.status != "running":  # stopped mid-flight, or already advanced
				return
			macro_doc = frappe.get_doc(MACRO, run.macro)
			steps = macro_doc.steps or []
			total = min(run.total_steps or len(steps), len(steps))

			# D5 (armed runs only): the step that just ran parked a confirmation card.
			# In an armed run the covered set runs uncarded, so a parked card means an
			# EXCLUDED tool (delete/cancel/amend) - and this is an unattended run with
			# nobody to click it. Advancing would report a false "completed" with the
			# destructive step silently never run. Detect it (a park returns ok:True, so
			# `errored` is False - the only per-step signal is the parked token itself),
			# STOP the run with a legible re-run-hazard message, and SWEEP the token so
			# the card can't be confirmed later and fire an armed continuation turn.
			# Checked BEFORE the stop_on_error / next_index decisions, so a merged-mode
			# (single-turn) run is covered too.
			if frappe.db.get_value(CONV, run.conversation, "skip_confirmation"):
				owner_user = frappe.db.get_value(CONV, run.conversation, "owner")
				parked = _armed_run_parked_card(run.conversation, owner_user)
				if parked:
					from jarvis.chat import pending_confirm

					_finish(run, "failed", error=_armed_stop_message(run.current_step or 0, parked))
					pending_confirm.clear_for_conversation(owner_user, run.conversation)
					_publish_done(run, macro_doc, "failed")
					return

			if errored and macro_doc.stop_on_error:
				_finish(run, "failed", error=f"Step {run.current_step} failed.")
				_publish_done(run, macro_doc, "failed")
				return

			next_index = run.current_step or 0  # 0-based index of the next step
			if next_index >= total:
				_finish(run, "completed")
				_publish_done(run, macro_doc, "completed")
				return

			_run_step(run, macro_doc, next_index)
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
	run.check_permission("write")  # owner-gate the stop action
	from jarvis._redis_lock import redis_lock

	# The block body runs whether or not the lock is acquired — a stop must never be silently
	# dropped. In the (rare) not-acquired case the resume path's status-fenced CAS still prevents a
	# resume from clobbering this stop.
	with redis_lock(f"jarvis_macro_run:{run_name}", timeout_s=60, blocking_timeout_s=_STOP_LOCK_BLOCK_S):
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


def _run_step(run, macro_doc, index: int) -> bool:
	"""Enqueue the turn for step ``index`` (0-based) and stamp current_step. Returns
	True when the step dispatched, False when it was DEFERRED for capacity.

	CDX-19: at the entry to this call ``run.current_step == index`` (run_macro seeds 0
	for step 0; advance_after_turn passes ``next_index == current_step``). If the accept
	gate is overloaded, ``_enqueue_turn`` returns {overloaded:True} WITHOUT dispatching a
	turn and after deleting its seed — the step must NOT advance (no turn will ever chain
	the run forward). Park the run in ``waiting_capacity`` with current_step left pointing
	at this same step so ``resume_waiting_capacity_runs`` re-attempts it next cycle."""
	step = macro_doc.steps[index]
	from jarvis.chat import api

	out = api._enqueue_turn(
		run.conversation,
		(step.prompt or "").strip() + _skill_invocations(step),
		model_override=(step.model_override or None),
		thinking_override=(step.thinking_override or None),
	)
	if isinstance(out, dict) and out.get("overloaded"):
		_defer_capacity(run, macro_doc)
		return False
	frappe.db.set_value(RUN, run.name, "current_step", index + 1)
	frappe.db.commit()
	run.current_step = index + 1
	_publish_progress(run, macro_doc, index)
	return True


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

	for run_name in rows:
		try:
			with redis_lock(f"jarvis_macro_run:{run_name}", timeout_s=60, blocking_timeout_s=0.0) as acquired:
				if not acquired:
					continue
				run = frappe.get_doc(RUN, run_name)
				if run.status != "waiting_capacity":
					continue
				macro_doc = frappe.get_doc(MACRO, run.macro)
				attempts = int(run.capacity_attempts or 0) + 1
				if attempts > _MAX_CAPACITY_ATTEMPTS:
					_finish(
						run,
						"failed",
						error="The site stayed busy — the macro could not get capacity to run this step.",
					)
					_publish_done(run, macro_doc, "failed")
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
						_publish_done(run, macro_doc, "failed")
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
				try:
					# #470: the run's OWN snapshot decides the shape. This branch used to read
					# ``macro_doc.merged_prompt`` live, so a summary that landed during the park
					# turned a part-executed stepped run merged (re-running steps 2..N after the
					# merged turn), and a step edit that cleared the summary turned a merged run
					# stepped (one step ran, then ``advance_after_turn`` computed
					# total=min(1,N)=1 and reported the run COMPLETED).
					if _run_mode(run, macro_doc) == MODE_MERGED:
						_run_merged(run, macro_doc, (macro_doc.merged_prompt or "").strip())
					else:
						_run_step(run, macro_doc, int(run.current_step or 0))
				except Exception:
					# CDX-23: the waiting_capacity -> running flip already committed; an enqueue that
					# RAISED (session setup / Message persistence / dispatch) leaves NO Turn and NO owner
					# (no terminal hook, and the run is no longer waiting_capacity for this cron) — it
					# would sit ``running`` forever, bypassing the bounded-attempt promise. Compensate
					# atomically under the lock.
					frappe.log_error(
						title="jarvis macro capacity-resume enqueue raised", message=frappe.get_traceback()
					)
					_compensate_resume_enqueue_failure(run, macro_doc, attempts)
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
	  only turn (``_run_merged`` defers ahead of the ``current_step`` write, and
	  ``advance_after_turn`` never re-enters merged), so nothing has executed and ending
	  the run costs no work.
	* **stepped, step list shrank past the cursor.** ``macro_doc.steps[index]`` would
	  IndexError, and the CDX-23 compensation would then restore ``waiting_capacity`` and
	  retry that certain IndexError once per cron cycle until the attempt cap ~100 min
	  later."""
	if _run_mode(run, macro_doc) == MODE_MERGED:
		return not (macro_doc.merged_prompt or "").strip()
	return int(run.current_step or 0) >= len(macro_doc.steps or [])


def _compensate_resume_enqueue_failure(run, macro_doc, attempts: int) -> None:
	"""CDX-23: a resumed step's enqueue raised AFTER the ``waiting_capacity -> running`` flip
	committed. Restore the run to a re-attemptable state atomically (state-fenced on ``running`` so a
	stop that landed meanwhile is never clobbered): at the attempt CAP take the documented honest
	failure terminal; otherwise restore ``waiting_capacity`` (the attempt was already counted at the
	flip, so the bound still holds) so the next cron retries the SAME step. Best-effort — never
	re-raises into the resume loop."""
	try:
		if attempts >= _MAX_CAPACITY_ATTEMPTS:
			if _cas_run_status(
				run.name,
				"running",
				"failed",
				finished_at=frappe.utils.now(),
				error="The site stayed busy — the macro could not get capacity to run this step.",
			):
				frappe.db.commit()
				_publish_done(run, macro_doc, "failed")
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
		_publish_done(run, macro_doc, "failed")
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


def _finish(run, status: str, error: str | None = None) -> None:
	frappe.db.set_value(
		RUN,
		run.name,
		{"status": status, "finished_at": frappe.utils.now(), "error": (error or "")[:500]},
	)
	_disarm_conversation(run.conversation)  # the flag never outlives the run (T5)
	frappe.db.commit()


def _publish_progress(run, macro_doc, index: int) -> None:
	step = macro_doc.steps[index]
	publish_to_user(
		macro_doc.owner,
		{
			"kind": "macro:progress",
			"macro_run": run.name,
			"macro": macro_doc.name,
			"conversation": run.conversation,
			"step": index + 1,
			"total": run.total_steps,
			"label": (step.label or "").strip() or f"Step {index + 1}",
			"status": "running",
		},
	)


def _publish_done(run, macro_doc, status: str) -> None:
	publish_to_user(
		macro_doc.owner,
		{
			"kind": "macro:done",
			"macro_run": run.name,
			"macro": macro_doc.name,
			"conversation": run.conversation,
			"status": status,
		},
	)
