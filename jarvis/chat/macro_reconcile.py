"""The five minute check on running macro runs: a run whose step is over, and whose
step end never moved it, is moved or ended here.

A run moves when its current step's turn ends and that end reaches
``macros.advance_after_turn``. Several ends never do. A turn that fails before it is
handed to the agent (the gateway unreachable, the send rejected), runs out of its
recovery budget, ages out of the queue, or is cancelled by its owner while queued has
no finalize job, so nothing calls the hook. A finalize job that died three times
inside the hook is given up on. A hook that waited ten seconds for the run lock
returns without acting. Each left the run ``running``, and its chat armed, until the
hourly stale-run sweep closed it three hours later.

What the check reads is the step's Turn row and the finalize ledger, never its own
memory: ``step_is_over`` is "the turn has ended, and no hook is on its way". It then
does what the hook would have done, through the hook's own body
(``macros._apply_step_end``), under the same run lock. A hook that arrives later
finds the run moved or ended and does nothing, as a repeated event always has.

Two things the hook does that the check does not: it never sends ANOTHER step for a
run that stood still for ``CONTINUE_WITHIN_S``, nor for an owner who may not run
unattended work. That run ends, saying which steps ran.

Only where Turn rows are the truth: the turn machine on and the run's shard in
``pump`` mode (``pump.transport_mode``). Elsewhere a step has no Turn, or its Turn is
never closed, and the check does nothing. Off switch: ``jarvis_macro_reconcile_disabled``
in site config. The stale-run sweep stays for what this cannot see (a turn that never
ends: ``ready``, a prepare that keeps being reclaimed, ``finalizing`` with no job).
"""

import time

import frappe
from frappe.utils import get_datetime, now_datetime

from jarvis.chat import macros

RUN = macros.RUN
MACRO = macros.MACRO
CONV = macros.CONV
TURN = macros.TURN
MSG = macros.MSG

OFF_SWITCH = "jarvis_macro_reconcile_disabled"
BY = "jarvis.chat.macro_reconcile"

# A run is looked at once nothing has written its row for this long: every step sent,
# park and resume writes it, so a younger row is a run that is moving.
IDLE_BEFORE_A_LOOK_S = 120
# How long after a turn's ``done_at`` its step counts as over. Not for the hook (the
# ledger answers that with no wait): every edge that ends a turn without a hook
# commits the end first and writes the rest after (the cancel marker in the chat, the
# reply's stamp). A next step sent in that gap would land above the marker of the
# step before it.
STEP_END_GRACE_S = 120
# Owner decision (2026-10-03): the check sends another step only for a step that
# ended less than this long ago. Found later, the run ends and says which steps ran:
# nobody is watching, and on an armed macro the step writes.
CONTINUE_WITHIN_S = 20 * 60
# A run that has sent nothing and written nothing for this long has no dispatch in
# flight: the longest one (a gateway handshake, the shard lock wait) is seconds.
NOT_STARTED_AFTER_S = 10 * 60
MAX_RUNS_PER_TICK = 200
TICK_BUDGET_S = 120
SKIP_AFTER_A_RAISE_S = 3600
# Longer than a run can stay `running` once the check has touched it.
_MARKER_TTL_S = 24 * 3600

_RUN_NOT_STARTED = "The run could not start. Run it again."

# Why the hook did not move the run, when it should have (the Error Log's shape).
SHAPE_FORCE_DONE = "force_done"  # the finalize job died in the hook until it was given up on
SHAPE_HOOK_DROPPED = "hook_dropped"  # the hook was called and left the run where it was
SHAPE_NEVER_COMMITTED = "dispatch_never_committed"  # the first step was sent again
SHAPE_NO_STEP_SENT = "no_step_sent"  # nothing was sent, and it was too late to send it


def reconcile_running_runs() -> dict:
	"""Cron entry (every five minutes). Never raises. Returns what the tick did, for
	the tests; the scheduler ignores it.

	A tick that changes nothing writes nothing: not a run row, not its ``modified``
	(the stale-run sweep measures a run's idleness on it)."""
	summary = {"candidates": 0, "acted": 0, "busy": 0, "raised": 0, "conflicts": 0, "out_of_time": 0}
	try:
		if _switched_off() or not _turn_rows_are_written():
			return summary
		started = _clock()
		for row in _candidates():
			summary["candidates"] += 1
			if _clock() - started >= TICK_BUDGET_S:
				summary["out_of_time"] = 1
				break
			if not _shard_is_on_the_pump(row.conversation):
				continue
			_RunCheck(row.name, summary).run()
	except Exception:
		_rollback()
		frappe.log_error(title="jarvis.chat.macros.reconcile_tick_failed", message=frappe.get_traceback())
	return summary


def step_is_over(turn_id: str, *, grace_s: int = STEP_END_GRACE_S) -> bool:
	"""Whether the turn has ended and NO hook is on its way for it: the one question
	the check rests on (proved shape by shape in ``test_macro_reconcile``).

	* The turn is ``done``, ``errored`` or ``cancelled``. ``finalizing`` is not over,
	  whatever its ledger says: the hook is called in that state.
	* Its ``macro_advance`` effect row is absent or ``done``. A settled error or Stop
	  is terminal BEFORE its hook runs, and only that row, written in the same commit
	  as the end, says the hook is coming. ``running`` covers a finalize job that died
	  inside the hook: it is re-run until the ledger gives up and marks it ``done``.
	* Its reply is not turn recovery's (``streaming`` and ``recovering``): recovery
	  finishes that reply up to an hour later and calls the hook itself, maybe with a
	  recovered answer.
	* ``done_at`` is at least ``grace_s`` old (``STEP_END_GRACE_S``). A turn with no
	  ``done_at`` has not ended."""
	turn = frappe.db.get_value(TURN, turn_id, ["state", "assistant_message", "done_at"], as_dict=True)
	if not turn or turn.state not in macros._TURN_ENDED:
		return False
	if macros._hook_effect_status(turn_id) not in (None, "done"):
		return False
	if turn.assistant_message:
		reply = frappe.db.get_value(MSG, turn.assistant_message, ["streaming", "recovering"], as_dict=True)
		if reply and int(reply.streaming or 0) and int(reply.recovering or 0):
			return False
	return bool(turn.done_at) and _seconds_since(turn.done_at) >= grace_s


class _RunCheck:
	"""One run, in one tick: skipped if another holder has its lock, isolated from the
	runs after it."""

	def __init__(self, run_name: str, summary: dict):
		self.name = run_name
		self.summary = summary
		self.run_doc = None
		self.macro_doc = None
		self.conv = None

	def run(self) -> None:
		from jarvis._redis_lock import redis_lock
		from jarvis.chat import txn

		if frappe.cache().get_value(_key("skip", self.name)):
			return
		session_user = frappe.session.user
		try:
			# The lock of the hook, the capacity resume, a stop and the stale-run sweep.
			# Never waited for: its holder is moving the run right now.
			with redis_lock(f"jarvis_macro_run:{self.name}", timeout_s=60, blocking_timeout_s=0.0) as held:
				if not held:
					self.summary["busy"] += 1
					return
				# The candidate list is from before the lock: read the present.
				txn.fresh_snapshot(owned=True)
				if self._look():
					self.summary["acted"] += 1
		except Exception as e:
			self._raised(e)
		finally:
			if frappe.session.user != session_user:
				frappe.set_user(session_user)

	def _raised(self, e: Exception) -> None:
		"""Logged once, then the run is left alone for an hour: a run that raises will
		raise again, every five minutes, until the stale-run sweep closes it.

		A write conflict or a lock wait is not that: both are routine on a row other
		workers write (``jarvis.chat.txn``), and an hour's skip would turn one lost race
		into "the run stood still too long". Tried again on the next tick, unlogged."""
		from jarvis.chat import txn

		traceback = frappe.get_traceback()
		_rollback()
		if txn.is_write_conflict(e) or isinstance(e, frappe.QueryTimeoutError):
			self.summary["conflicts"] += 1
			return
		self.summary["raised"] += 1
		frappe.cache().set_value(_key("skip", self.name), 1, expires_in_sec=SKIP_AFTER_A_RAISE_S)
		frappe.log_error(title=f"jarvis.chat.macros.reconcile_failed: {self.name}", message=traceback)
		frappe.db.commit()

	def _look(self) -> bool:
		"""Under the lock. Returns whether the run was changed."""
		if not frappe.db.exists(RUN, self.name):
			return False
		run = self.run_doc = frappe.get_doc(RUN, self.name)
		if run.status != "running" or _seconds_since(run.modified) < IDLE_BEFORE_A_LOOK_S:
			return False
		self.conv = (
			frappe.db.get_value(CONV, run.conversation, ["owner", "status"], as_dict=True)
			if run.conversation
			else None
		)
		if not self.conv or self.conv.status == "Archived":
			return macros._stop_run_locked(run.name, reason=macros._CONVERSATION_GONE_ERROR, by=BY)
		if not frappe.db.exists(MACRO, run.macro):
			return macros._stop_run_locked(run.name, reason=macros._MACRO_DELETED_ERROR, by=BY)
		self.macro_doc = frappe.get_doc(MACRO, run.macro)
		steps = self.macro_doc.steps or []
		sent = macros._next_step_index(run, min(run.total_steps or len(steps), len(steps)))
		if sent == 0:
			return self._nothing_sent()
		turn = _step_turn(run, sent - 1)
		if not turn or not step_is_over(turn.name):
			# No Turn row: a step sent where none was written (before a cutover). Not
			# known to be over, so not touched.
			return False
		return self._apply_the_end(turn, sent)

	def _apply_the_end(self, turn, sent: int) -> bool:
		"""The step is over and no hook is coming: do what the hook would have done.
		Ending the run goes through unchanged. Another step goes out only for a step
		that ended lately and an owner who may run unattended; as that owner, like the
		scheduler (``macro_scheduler._sweep_one``)."""
		run = self.run_doc
		owner_ok = _may_run_unattended(self.conv.owner)
		too_late = _seconds_since(turn.done_at) >= CONTINUE_WITHIN_S

		def refuse_next_step(steps_sent: int) -> str | None:
			return _interrupted(steps_sent) if too_late or not owner_ok else None

		shape = _lost_hook_shape(turn.name)
		if owner_ok:
			frappe.set_user(self.conv.owner)
		macros._apply_step_end(
			run.name,
			run.conversation,
			errored=turn.state in macros._TURN_FAILED,
			run_id=turn.name,
			refuse_next_step=refuse_next_step,
		)
		return self._after(shape, turn.state, was=("running", sent))

	def _nothing_sent(self) -> bool:
		"""The run has sent no step: no turn under step 1's id, no step message. With
		Turn rows written, a step's message and its turn are one transaction, so a
		dispatcher that died before its commit (the request that started the run, a
		capacity resume) leaves exactly this, and nothing delivers its trigger again.

		Sent ONCE more, by ``_dispatch_step``, between ``NOT_STARTED_AFTER_S`` and
		``CONTINUE_WITHIN_S`` of the run's last write. "Once" is a cache marker. If the
		marker is lost the run gets one more attempt at most: an attempt that dies
		writes nothing, so the run's last write keeps ageing towards the limit, and one
		that raises ends the run here. After the attempt, or too late for one, or for
		an owner who may not run unattended, the run ends ``failed``."""
		run, macro_doc = self.run_doc, self.macro_doc
		if macros._macro_seeds(run.conversation, limit=1):
			return False  # a step went out with no Turn row (before a cutover): not known to be dead
		idle = _seconds_since(run.modified)
		if idle < NOT_STARTED_AFTER_S:
			return False
		if macros._fail_run_unless_one_owner(run, macro_doc):
			return True
		merged = macros._run_mode(run, macro_doc) == macros.MODE_MERGED
		marker = _key("resent", run.name)
		if frappe.cache().get_value(marker):
			return self._fail(macros._step_not_started(0, merged), SHAPE_NEVER_COMMITTED)
		if idle >= CONTINUE_WITHIN_S or not _may_run_unattended(self.conv.owner):
			return self._fail(_RUN_NOT_STARTED, SHAPE_NO_STEP_SENT)
		frappe.cache().set_value(marker, 1, expires_in_sec=_MARKER_TTL_S)
		frappe.set_user(self.conv.owner)
		try:
			macros._dispatch_step(run, macro_doc, 0)
		except Exception:
			frappe.log_error(
				title=f"jarvis macro step dispatch failed: {run.name}", message=frappe.get_traceback()
			)
			if not macros._step_went_out_anyway(run, 0):
				return self._fail(macros._step_not_started(0, merged), SHAPE_NEVER_COMMITTED)
		return self._after(SHAPE_NEVER_COMMITTED, None, was=("running", 0))

	def _fail(self, error: str, shape: str) -> bool:
		"""End the run ``failed``, from ``running`` only: a stop that went ahead without
		the lock (``macros._stop_run``) keeps its outcome."""
		from jarvis.chat import txn

		run = self.run_doc
		txn.fresh_snapshot(owned=True)
		failed = macros._cas_run_status(
			run.name, "running", "failed", finished_at=frappe.utils.now(), error=error[:500]
		)
		frappe.db.commit()
		if not failed:
			return False
		macros._announce(run, self.macro_doc, "failed", error)
		return self._after(shape, None, was=("running", int(run.current_step or 0)))

	def _after(self, shape: str | None, turn_state: str | None, *, was: tuple) -> bool:
		"""What the look did to the run: nothing (the engine declined the end, as it
		declines a repeated one) or something. Something is logged when a hook should
		have done it, and a run that ended ``failed`` is told to its owner."""
		run = self.run_doc
		now = frappe.db.get_value(RUN, run.name, ["status", "current_step", "error"], as_dict=True)
		if now and (now.status, int(now.current_step or 0)) == was:
			return False
		if shape:
			_signal(run.name, shape, turn_state)
		if now and now.status == "failed" and (run.get("trigger") or "manual") != "scheduled":
			# ``macros._announce`` left the closing line and, for a scheduled run, the
			# notification. A manual run gets none there: whoever started it is told by
			# the realtime event. Nobody is watching a run found minutes later.
			macros.notify_owner(
				self.macro_doc.owner,
				subject=f"Macro run failed: {self.macro_doc.macro_name}",
				body=now.error or "",
			)
		return True


def _switched_off() -> bool:
	"""The off switch, read on every tick (site config is loaded per job). Any value
	but an absent, empty or zero one switches the check off: ``bench set-config``
	stores ``1`` as a string."""
	value = frappe.conf.get(OFF_SWITCH)
	return str(value).strip().lower() not in ("none", "", "0", "false")


def _turn_rows_are_written() -> bool:
	from jarvis.chat import admission

	return admission.turn_machine_enabled()


def _shard_is_on_the_pump(conversation: str | None) -> bool:
	"""The run's shard is in ``pump`` mode by its own row. Draining: new steps have no
	Turn row. The ``legacy`` kill switch (and Phase-0, whose row is ``legacy``): the
	pump's watchdog is off, so Turn rows are never closed, and a worker calls the hook
	before it settles its turn."""
	from jarvis.chat import admission, pump

	return pump.transport_mode(admission.relay_target_id(conversation)) == "pump"


def _candidates() -> list:
	"""Runs ``running`` and idle, oldest first. One more than a tick takes is left for
	the next one."""
	cutoff = frappe.utils.add_to_date(now_datetime(), seconds=-IDLE_BEFORE_A_LOOK_S)
	return frappe.get_all(
		RUN,
		filters={"status": "running", "modified": ["<", cutoff]},
		fields=["name", "conversation"],
		order_by="modified asc",
		limit=MAX_RUNS_PER_TICK,
	)


def _step_turn(run, index: int):
	"""The turn that decides how step ``index`` ended: the newest turn of the step's
	message (the owner's Retry of a failed step is a second turn on it). The message
	is found through the step's fixed turn id; a step sent before that id existed is
	the chat's newest step message."""
	seed = frappe.db.get_value(
		TURN,
		{"name": macros._step_turn_id(run.name, index), "conversation": run.conversation},
		"seed_message",
	)
	seed = seed or macros._current_step_message(run.conversation)
	turn = macros._ended_turn(seed, None) if seed else None
	if turn:
		turn.done_at = frappe.db.get_value(TURN, turn.name, "done_at")
	return turn


def _lost_hook_shape(turn_id: str) -> str | None:
	"""Which lost hook this is, or None for an end that never has one (no ledger row:
	the turn failed before it was sent, ran out of recovery, aged out of the queue or
	was cancelled while queued). Those are by design and write no Error Log: the run's
	status and reason are the record."""
	from jarvis.chat import turn_state

	attempts = frappe.db.get_value(turn_state.EFFECT, f"{turn_id}::macro_advance", "attempts")
	if attempts is None:
		return None
	return SHAPE_FORCE_DONE if int(attempts or 0) >= turn_state.FINALIZE_MAX_ATTEMPTS else SHAPE_HOOK_DROPPED


def _signal(run_name: str, shape: str, turn_state: str | None) -> None:
	"""One Error Log row per run the check moved in a hook's place. The title is
	forwarded off the bench (``api_errors``): the shape only. The body: the run, the
	turn's state, the shape. Never a prompt or a reply."""
	if frappe.cache().get_value(_key("signalled", run_name)):
		return
	frappe.cache().set_value(_key("signalled", run_name), 1, expires_in_sec=_MARKER_TTL_S)
	frappe.log_error(
		title=f"jarvis.chat.macros.reconciled: {shape}",
		message=f"run {run_name}; turn state {turn_state or 'no turn'}; shape {shape}",
	)
	frappe.db.commit()


def _interrupted(steps_sent: int) -> str:
	ran = "Step 1 ran" if steps_sent <= 1 else f"Steps 1 to {steps_sent} ran"
	return f"The run was interrupted and did not finish. {ran}; the rest did not."


def _may_run_unattended(owner: str | None) -> bool:
	"""The scheduler's two identity checks (``macro_scheduler._sweep_one``): never
	Administrator, Guest or a disabled user, and only one who can still reach Jarvis."""
	from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

	return bool(owner) and is_valid_unattended_owner(owner) and has_jarvis_access(owner)


def _seconds_since(moment) -> float:
	return (now_datetime() - get_datetime(moment)).total_seconds()


def _key(kind: str, run_name: str) -> str:
	return f"jarvis:macro_reconcile:{kind}:{run_name}"


def _clock() -> float:
	return time.monotonic()


def _rollback() -> None:
	try:
		frappe.db.rollback()
	except Exception:
		pass
