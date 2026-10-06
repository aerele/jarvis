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

Three limits the check has and the hook does not. It sends no further step for a run
whose step ended ``CONTINUE_WITHIN_S`` ago or more, for an owner who may not run
unattended work, or while the send gate a typed message passes is closed
(maintenance, suspension, a usage limit): that run ends ``failed`` and says why. A
step its owner stopped ends the run ``stopped`` whatever "Stop on error" says. And
it never sends a step AGAIN: a run that sent nothing at all ends ``failed``.

Only where Turn rows are the truth: the turn machine on and the run's shard in
``pump`` mode (``pump.transport_mode``). Elsewhere a step has no Turn, or its Turn is
never closed, and the check does nothing. Off switch: ``jarvis_macro_reconcile_disabled``
in site config. The stale-run sweep stays for what this cannot see: a turn that never
ends (``ready``, a prepare that keeps being reclaimed, ``finalizing`` with no job)
and a run whose newest step message has no Turn row.

The same tick settles a macro's summary that sits "summarizing" because its turn's
end never landed it (``_SummaryCheck``): the summarize turn is a turn like a step's,
its hook can be lost the same ways, and Run stays refused while the mark is there.
Failing a summary is harmless (the steps run as written), so a summarize turn that
never ends is failed too, after ``SUMMARY_GIVE_UP_AFTER_S``.

Is it working? ``stuck_runs`` lists the runs it should have dealt with and has not
(``bench --site <site> execute jarvis.chat.macro_reconcile.stuck_runs``: an empty
list is the healthy answer), and the stale-run sweep writes an Error Log,
``jarvis.chat.macros.reaped_despite_check``, when it closes such a run.
"""

import contextlib
import time

import frappe
from frappe.utils import get_datetime, now_datetime
from rq.timeouts import JobTimeoutException

from jarvis.chat import macros

RUN = macros.RUN
MACRO = macros.MACRO
CONV = macros.CONV
TURN = macros.TURN
MSG = macros.MSG
LOG = "Error Log"

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
# ``stuck_runs``: a run the check should have dealt with this long ago. The check's
# own latency is one tick after the grace (or after ``NOT_STARTED_AFTER_S``).
STUCK_AFTER_S = 20 * 60
MAX_RUNS_PER_TICK = 200
# A summary is looked at once its chat is this old (the chat's ``creation`` is the
# only clock: the macro row's ``modified`` is not written by a summary). A chat that
# is gone is looked at at once: nothing is coming from it.
SUMMARY_SETTLE_AFTER_S = 20 * 60
# A summarize turn that has not ended by then is given up on and the summary failed.
SUMMARY_GIVE_UP_AFTER_S = 60 * 60
MAX_SUMMARIES_PER_TICK = 200
TICK_BUDGET_S = 120
SKIP_AFTER_A_RAISE_S = 3600
LOG_AT_MOST_EVERY_S = 3600
# Longer than a run can stay `running` once the check has touched it.
_MARKER_TTL_S = 24 * 3600
# Every cache key of this module starts with this, and ``persistent_cache_keys`` in
# ``hooks.py`` lists it. The pump's watchdog runs on the same five minute cron and
# ends with ``frappe.db.set_default``, which clears every site cache key not listed
# there: without the entry "logged once" and "skipped for an hour" lasted one tick.
CACHE_PREFIX = "jarvis:macro_reconcile"

_RUN_NOT_STARTED = "The run could not start. Run it again."
_GATE_CLOSED = "This macro cannot run right now."

# Why the hook did not move the run, when it should have (the Error Log's shape).
SHAPE_FORCE_DONE = "force_done"  # the finalize job died in the hook until it was given up on
SHAPE_HOOK_DROPPED = "hook_dropped"  # the hook gave up waiting for the run lock
SHAPE_NO_STEP_SENT = "no_step_sent"  # the run's first step was never written
# The same for a summary: its turn's finalize job died in the hook until it was given
# up on, or its chat has no turn (the request that started it died before the dispatch).
SHAPE_SUMMARY_FORCE_DONE = "summary_force_done"
SHAPE_SUMMARY_NO_TURN = "summary_no_turn"

TICK_FAILED = "jarvis.chat.macros.reconcile_tick_failed"
TICK_CUT_SHORT = "jarvis.chat.macros.reconcile_cut_short"
LOCK_WAIT = "jarvis.chat.macros.reconcile_lock_wait"
REAPED_DESPITE_CHECK = "jarvis.chat.macros.reaped_despite_check"

_RUN_FIELDS = ["name", "macro", "conversation", "status", "current_step", "total_steps", "modified"]


class _LockFault(Exception):
	"""The run lock could not be asked for (redis is unreachable). Not one run's
	failure: every run after it would fail the same way, one Error Log each. It ends
	the tick."""


def reconcile_running_runs() -> dict:
	"""Cron entry (every five minutes). Returns what the tick did, for the tests; the
	scheduler ignores it. Raises nothing but the job's own timeout: that one is the
	scheduler's to record, not a failure of the run in hand.

	A tick that changes nothing writes nothing: not a run row, not its ``modified``
	(the stale-run sweep measures a run's idleness on it)."""
	summary = {
		"candidates": 0,
		"acted": 0,
		"busy": 0,
		"raised": 0,
		"conflicts": 0,
		"out_of_time": 0,
		"left": 0,
		"summaries": 0,
		"summaries_settled": 0,
		"summaries_left": 0,
	}
	try:
		if _switched_off() or not _turn_rows_are_written():
			return summary
		started = _clock()
		rows = _candidates()
		summary["left"] += _beyond_the_cap(rows, MAX_RUNS_PER_TICK, _candidate_count)
		for row in _within_the_budget(rows, started, summary, "left"):
			summary["candidates"] += 1
			if not _shard_is_on_the_pump(row.conversation):
				continue
			_RunCheck(row.name, summary).run()
		if summary["out_of_time"]:
			summary["summaries_left"] += _stuck_summary_count()
		else:
			_reconcile_stuck_summaries(summary, started)
		if summary["left"] or summary["summaries_left"]:
			_cut_short(summary)
	except JobTimeoutException:
		raise
	except Exception:
		# One row an hour, not one a tick: a fault that lasts (redis down, a schema
		# the code does not match) would otherwise write 288 rows a day.
		traceback = frappe.get_traceback()
		_rollback()
		_log_at_most_hourly(TICK_FAILED, traceback)
	return summary


def stuck_runs() -> list[dict]:
	"""The runs the check should have dealt with and has not: ``running``, and for
	``STUCK_AFTER_S`` or more

	* their current step has been over by the check's own rule (``step_is_over``:
	  the turn ended, no hook owed on any turn of the step, the reply not turn
	  recovery's), or
	* they have sent no step at all, or
	* their chat or their macro is gone.

	Empty on a healthy site in ``pump`` mode with the check on. Reads only, and
	looks whatever the gate and the off switch say, so it also shows what a switched
	off check is leaving to the stale-run sweep.
	``bench --site <site> execute jarvis.chat.macro_reconcile.stuck_runs``.

	The cutoff is computed here, on the site's clock, like every age in this module:
	``done_at`` is written in the site's time zone, and SQL's ``NOW()`` is the
	database server's. A row that is gone five minutes later was the check's own
	latency: a turn whose hook was given up on is over only from that moment, while
	its ``done_at`` is older."""
	cutoff = frappe.utils.add_to_date(now_datetime(), seconds=-STUCK_AFTER_S)
	rows = frappe.get_all(
		RUN,
		filters={"status": "running", "modified": ["<", cutoff]},
		fields=_RUN_FIELDS,
		order_by="modified asc",
	)
	return [found for found in map(_stuck, rows) if found]


def left_by_the_check(run_name: str) -> dict | None:
	"""For the stale-run sweep, about a run it is about to close: the ``stuck_runs``
	row of a run the check should have ended or moved hours ago, or None. None too
	where the check does not act at all (the off switch, the turn machine off, the
	shard not in ``pump`` mode) and for a run it leaves to the sweep on purpose (its
	newest step message has no Turn, or its turn never ended).

	The one signal of a check that is not running: the cron was never registered (no
	migrate), every tick fails or is cut short, the run raises on every look."""
	if _switched_off() or not _turn_rows_are_written():
		return None
	run = frappe.db.get_value(RUN, run_name, _RUN_FIELDS, as_dict=True)
	if not run or run.status != "running" or not _shard_is_on_the_pump(run.conversation):
		return None
	return _stuck(run)


def step_is_over(turn_id: str, *, grace_s: int = STEP_END_GRACE_S) -> bool:
	"""Whether the turn has ended and NO hook is on its way for its step: the one
	question the check rests on (proved shape by shape in ``test_macro_reconcile``).

	* The turn is ``done``, ``errored`` or ``cancelled``. ``finalizing`` is not over,
	  whatever its ledger says: the hook is called in that state.
	* No turn of the step's message has a ``macro_advance`` effect row other than
	  ``done``. A settled error or Stop is terminal BEFORE its hook runs, and only
	  that row, written in the same commit as the end, says the hook is coming.
	  ``running`` covers a finalize job that died inside the hook: it is re-run until
	  the ledger gives up and marks it ``done``. Every turn of the message, not this
	  one alone: the owner's Retry is a second turn on it, and when that retry is
	  cancelled before it starts the FIRST turn's hook is the one that counts
	  (``macros._superseded_by_a_retry``) and may still be owed.
	* Its reply is not turn recovery's (``streaming`` and ``recovering``): recovery
	  finishes that reply up to an hour later and calls the hook itself, maybe with a
	  recovered answer.
	* ``done_at`` is at least ``grace_s`` old (``STEP_END_GRACE_S``). A turn with no
	  ``done_at`` has not ended."""
	turn = frappe.db.get_value(
		TURN, turn_id, ["state", "seed_message", "assistant_message", "done_at"], as_dict=True
	)
	if not turn or turn.state not in macros._TURN_ENDED:
		return False
	if _a_hook_is_owed(turn_id, turn.seed_message):
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
		self.signal = None  # (shape, turn state): the Error Log this look owes

	def run(self) -> None:
		if frappe.cache().get_value(_key("skip", self.name), expires=True):
			return
		session_user = frappe.session.user
		failure = None
		try:
			self._under_the_lock()
		except (JobTimeoutException, _LockFault):
			raise  # the job's, and the tick's: neither is this run's failure
		except Exception as e:
			failure = (e, frappe.get_traceback())
			_rollback()
		finally:
			if frappe.session.user != session_user:
				frappe.set_user(session_user)
		# The Error Logs are written here, after the session is the scheduler's again.
		# Written while the check acted as the run's owner they were that user's rows,
		# and were forwarded off the bench with a reference to them.
		if failure:
			self._raised(*failure)
		elif self.signal:
			_signal(self.name, *self.signal)

	def _under_the_lock(self) -> None:
		from jarvis._redis_lock import redis_lock
		from jarvis.chat import txn

		with contextlib.ExitStack() as stack:
			# The lock of the hook, the capacity resume, a stop and the stale-run sweep.
			# Never waited for: its holder is moving the run right now.
			lock = redis_lock(f"jarvis_macro_run:{self.name}", timeout_s=60, blocking_timeout_s=0.0)
			try:
				held = stack.enter_context(lock)
			except Exception as e:
				raise _LockFault(f"the run lock of {self.name} could not be asked for: {e}") from e
			if not held:
				self.summary["busy"] += 1
				return
			# The candidate list is from before the lock: read the present. Without
			# this a hook that moved the run in between is not seen, and the step it
			# sent is asked for again (found out, and logged as a hook lost).
			txn.fresh_snapshot(owned=True)
			if self._look():
				self.summary["acted"] += 1

	def _raised(self, e: Exception, traceback: str) -> None:
		"""Logged once, then the run is left alone for an hour: a run that raises will
		raise again, every five minutes, until the stale-run sweep closes it.

		A write conflict or a lock wait is not that: both are routine on a row other
		workers write (``jarvis.chat.txn``), and an hour's skip would turn one lost race
		into "the run stood still too long". Tried again on the next tick. A conflict
		cannot last (each look starts on a fresh snapshot). A lock wait can, fifty
		seconds of the tick each time, so that one leaves a trace: one row an hour."""
		if _a_lost_race(e, self.summary, f"run {self.name}: tried again on the next tick\n\n{traceback}"):
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
		self.conv = _chat(run)
		if not self.conv:
			return macros._stop_run_locked(run.name, reason=macros._CONVERSATION_GONE_ERROR, by=BY)
		if not frappe.db.exists(MACRO, run.macro):
			return macros._stop_run_locked(run.name, reason=macros._MACRO_DELETED_ERROR, by=BY)
		self.macro_doc = frappe.get_doc(MACRO, run.macro)
		if macros.is_held(self.macro_doc):
			# An admin put the macro on hold and the run is still going (the hold's
			# own stop of it lost): end it here rather than move it on.
			return macros._stop_run_locked(run.name, reason=macros._MACRO_HELD_ERROR, by=BY)
		steps = macros._run_content(run, self.macro_doc).steps
		sent = macros._next_step_index(run, min(run.total_steps or len(steps), len(steps)))
		step = _current_step(run, sent)
		if step is _NOTHING_SENT:
			return self._nothing_sent()
		if step is None or not step_is_over(step.name):
			return False
		return self._apply_the_end(step, sent)

	def _apply_the_end(self, turn, sent: int) -> bool:
		"""The step is over and no hook is coming: do what the hook would have done.
		Ending the run goes through unchanged, but for a step its owner stopped: that
		ends the run ``stopped`` whatever "Stop on error" says (the hook, seconds
		after the Stop, sends the next step when it is off; minutes later, with nobody
		there, a step sent after a Stop is not what the owner asked for). Another step
		goes out only for a step that ended lately, an owner who may run unattended
		and an open send gate; as that owner, like the scheduler
		(``macro_scheduler._sweep_one``)."""
		run = self.run_doc
		owner = self.conv.owner
		owner_ok = _may_run_unattended(owner)
		too_late = _seconds_since(turn.done_at) >= CONTINUE_WITHIN_S

		def refuse_next_step(_steps_sent: int) -> str | None:
			if too_late or not owner_ok:
				return _interrupted(_steps_that_ran(run.conversation))
			return _send_gate_refusal(owner)

		shape = _lost_hook_shape(run.name, turn.name)
		if owner_ok:
			frappe.set_user(owner)
		macros._apply_step_end(
			run.name,
			run.conversation,
			errored=turn.state in macros._TURN_FAILED,
			run_id=turn.name,
			refuse_next_step=refuse_next_step,
			a_stop_ends_the_run=True,
		)
		return self._after(shape, turn.state, was=("running", sent))

	def _nothing_sent(self) -> bool:
		"""The run has sent no step: no turn under step 1's id, no step message. With
		Turn rows written, a step's message and its turn are one transaction, so a
		dispatcher that died before its commit (the request that started the run, the
		scheduler's job, a capacity resume) leaves exactly this, and nothing delivers
		its trigger again.

		Ended ``failed`` once ``NOT_STARTED_AFTER_S`` has passed, whatever started the
		run, and never sent: the owner of a manual run saw the Run click fail and
		pressed it again, and a first step sent ten minutes later would chain to the
		end of a second run of the same macro, on an armed one with every write done
		twice and nobody asked."""
		run = self.run_doc
		if _seconds_since(run.modified) < NOT_STARTED_AFTER_S:
			return False
		if macros._fail_run_unless_one_owner(run, self.macro_doc):
			return True
		return self._fail(_RUN_NOT_STARTED, SHAPE_NO_STEP_SENT)

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
		have done it (by ``run``, once the session is restored), and the owner of a
		manual run that ended WITH A REASON is told. A run the owner stopped, or one
		that simply completed, is told to nobody, as on the hook's own path
		(``macros._announce`` notifies only when there is an error to give)."""
		run = self.run_doc
		now = frappe.db.get_value(RUN, run.name, ["status", "current_step", "error"], as_dict=True)
		if now and (now.status, int(now.current_step or 0)) == was:
			return False
		if shape:
			self.signal = (shape, turn_state)
		manual = (run.get("trigger") or "manual") != "scheduled"
		if now and manual and now.status in macros._TERMINAL_RUN_STATUSES and now.error:
			# ``macros._announce`` left the closing line and, for a scheduled run, the
			# notification. A manual run gets none there: whoever started it is told by
			# the realtime event. Nobody is watching a run found minutes later, and a
			# run stopped at a confirmation card has no closing line either (it is
			# withheld while the reply holds the card), so the run row was the only
			# trace.
			macros.notify_owner(
				self.macro_doc.owner,
				subject=f"{macros._OUTCOME_SUBJECT[now.status]}: {self.macro_doc.macro_name}",
				body=now.error,
			)
		return True


def _reconcile_stuck_summaries(summary: dict, started: float) -> None:
	"""The tick's second half: every macro whose summary looks stuck, in what is left of
	the tick's time budget."""
	rows = _stuck_summary_candidates()
	summary["summaries_left"] += _beyond_the_cap(rows, MAX_SUMMARIES_PER_TICK, _stuck_summary_count)
	for row in _within_the_budget(rows, started, summary, "summaries_left"):
		summary["summaries"] += 1
		if not _shard_is_on_the_pump(row.conversation):
			continue
		_SummaryCheck(row.name, row.conversation or "", summary).run()


def _stuck_summary_candidates(*, limit: int | None = None) -> list:
	"""Macros ``pending`` on a chat created ``SUMMARY_SETTLE_AFTER_S`` ago or more, or on
	one that is gone; those first, then the oldest chat first, ``MAX_SUMMARIES_PER_TICK``
	at most. The cutoff is the site's clock, like the chat's ``creation``."""
	return frappe.db.sql(
		"""SELECT m.name, m.merge_conversation AS conversation
		FROM `tabJarvis Macro` m LEFT JOIN `tabJarvis Conversation` c ON c.name = m.merge_conversation
		WHERE m.merge_status = 'pending' AND (c.name IS NULL OR c.creation < %(cutoff)s)
		ORDER BY c.creation IS NOT NULL, c.creation
		LIMIT %(limit)s""",
		{"cutoff": _stuck_summary_cutoff(), "limit": limit or MAX_SUMMARIES_PER_TICK},
		as_dict=True,
	)


def _stuck_summary_count() -> int:
	"""How many macros ``_stuck_summary_candidates`` would list with no cap. Asked only
	for a tick that was cut short."""
	rows = frappe.db.sql(
		"""SELECT COUNT(*)
		FROM `tabJarvis Macro` m LEFT JOIN `tabJarvis Conversation` c ON c.name = m.merge_conversation
		WHERE m.merge_status = 'pending' AND (c.name IS NULL OR c.creation < %(cutoff)s)""",
		{"cutoff": _stuck_summary_cutoff()},
	)
	return int(rows[0][0] or 0)


def _stuck_summary_cutoff():
	return frappe.utils.add_to_date(now_datetime(), seconds=-SUMMARY_SETTLE_AFTER_S)


class _SummaryCheck:
	"""One macro's "summarizing" mark, in one tick, isolated from the macros after it.

	Settled, the way the summarize turn's own hook settles it
	(``macros._land_summary``), when nothing more will come for it:

	* its chat is gone: failed at once, as the hook fails it;
	* its turn is over by the run check's own rule (``step_is_over``: ended, no hook
	  owed, its reply not turn recovery's, two minutes since): the reply decides,
	  ready or failed, as it decides for the hook. A summary that worked and whose
	  hook was lost lands;
	* its chat has no turn and no reply in progress: the request that started it died
	  before the dispatch. With Turn rows written that is all it can be.

	A turn that has not ended ``SUMMARY_GIVE_UP_AFTER_S`` after its chat was created
	(``ready`` for good, a prepare that keeps being reclaimed) fails the summary, and
	its chat is kept while the turn is live. So does a look that raises, once the chat
	is that old (``_give_up_after_a_fault``). A late end of that turn lands nothing:
	the owner was told "failed", and Run may already have run the steps as written.

	No lock. Every write is a compare-and-set on the mark still naming the chat this
	look read (``macros._settle_summary_mark``): of the check, the hook and a
	Re-summarize started meanwhile, one wins, and only the winner deletes the chat."""

	def __init__(self, macro_name: str, conversation: str, summary: dict):
		self.name = macro_name
		self.conversation = conversation
		self.summary = summary
		self.signal = None  # (shape, turn state): the Error Log this look owes

	def run(self) -> None:
		from jarvis.chat import txn

		if frappe.cache().get_value(_summary_key("skip", self.name), expires=True):
			return
		try:
			# The candidate list is from before: read the present.
			txn.fresh_snapshot(owned=True)
			settled = self._look()
		except JobTimeoutException:
			raise
		except Exception as e:
			traceback = frappe.get_traceback()
			_rollback()
			if not self._raised(e, traceback):
				return
			self.signal = None
			settled = self._give_up_after_a_fault()
		if not settled:
			return
		self.summary["summaries_settled"] += 1
		if self.signal:
			_signal(
				self.name, *self.signal, subject="macro", key=_summary_key("signalled", self.conversation)
			)

	def _raised(self, e: Exception, traceback: str) -> bool:
		"""As a run's (``_RunCheck._raised``): a lost race is tried again on the next
		tick; anything else is logged once and the macro left alone for an hour.
		Returns whether it was such a fault."""
		lock_wait = f"summary of macro {self.name}: a lock wait, tried again on the next tick"
		if _a_lost_race(e, self.summary, lock_wait):
			return False
		self.summary["raised"] += 1
		frappe.cache().set_value(_summary_key("skip", self.name), 1, expires_in_sec=SKIP_AFTER_A_RAISE_S)
		frappe.log_error(title=f"jarvis.chat.macros.summary_reconcile_failed: {self.name}", message=traceback)
		frappe.db.commit()
		return True

	def _give_up_after_a_fault(self) -> bool:
		"""A look that raises raises again on every look, and a summary must not stay
		"summarizing" for good: once its chat is ``SUMMARY_GIVE_UP_AFTER_S`` old, or gone,
		it is failed as a turn that never ends is, with none of the reads that raised.
		Never raises (but the job's timeout)."""
		from jarvis.chat import txn

		try:
			txn.fresh_snapshot(owned=True)
			created = frappe.db.get_value(CONV, self.conversation, "creation") if self.conversation else None
			if created is not None and _seconds_since(created) < SUMMARY_GIVE_UP_AFTER_S:
				return False
			return macros._finish_merge(self.name, self.conversation, "failed", "", chat_may_be_live=True)
		except JobTimeoutException:
			raise
		except Exception:
			_rollback()
			return False

	def _look(self) -> bool:
		"""Returns whether this look took the macro off "summarizing"."""
		mark = frappe.db.get_value(MACRO, self.name, ["merge_status", "merge_conversation"], as_dict=True)
		if not mark or mark.merge_status != "pending" or (mark.merge_conversation or "") != self.conversation:
			return False
		created = frappe.db.get_value(CONV, self.conversation, "creation") if self.conversation else None
		if created is None:
			# "Delete all chat history" mid-summary: by design, no Error Log.
			return macros._land_summary(self.name, self.conversation, errored=True)
		# At least ``SUMMARY_SETTLE_AFTER_S`` old: the candidate query's own cutoff
		# (a chat's ``creation`` does not change).
		age = _seconds_since(created)
		turn = _summary_turn(self.conversation)
		if turn and step_is_over(turn.name):
			if _ledger_gave_up_on_the_hook(turn.name):
				self.signal = (SHAPE_SUMMARY_FORCE_DONE, turn.state)
			return self._land(turn)
		if not turn and not _reply_in_progress(self.conversation):
			self.signal = (SHAPE_SUMMARY_NO_TURN, None)
			# ``errored=False``: the reply decides, and with no turn there is none
			# that finished, so it is failed (a chat sent where no Turn row is written
			# can hold a finished one, and that one lands as its hook would land it).
			return macros._land_summary(self.name, self.conversation, errored=False)
		if age < SUMMARY_GIVE_UP_AFTER_S:
			return False
		return macros._finish_merge(self.name, self.conversation, "failed", "", chat_may_be_live=True)

	def _land(self, turn) -> bool:
		"""The turn is over: what the hook would have landed. How the turn ended is read
		as the run check reads a step's end (``macros._step_verdict``), so a reply that
		turn recovery finished counts as finished."""
		reply = None
		if turn.assistant_message:
			reply = frappe.db.get_value(
				MSG, turn.assistant_message, ["was_recovered", "streaming", "content", "error"], as_dict=True
			)
		errored = bool(macros._step_verdict(turn, reply, errored=False))
		return macros._land_summary(self.name, self.conversation, errored=errored)


def _summary_turn(conversation: str):
	"""The summary chat's newest turn (it has one), or None."""
	rows = frappe.get_all(
		TURN,
		filters={"conversation": conversation},
		fields=_STEP_TURN_FIELDS,
		order_by="creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def _reply_in_progress(conversation: str) -> bool:
	from jarvis.chat import admission

	return bool(admission.reply_in_progress(conversation))


def _summary_key(kind: str, name: str) -> str:
	return f"{CACHE_PREFIX}:summary-{kind}:{name}"


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


def _candidate_filters() -> dict:
	cutoff = frappe.utils.add_to_date(now_datetime(), seconds=-IDLE_BEFORE_A_LOOK_S)
	return {"status": "running", "modified": ["<", cutoff]}


def _candidates() -> list:
	"""Runs ``running`` and idle, the longest idle first, ``MAX_RUNS_PER_TICK`` at
	most. Any beyond that are not read: they wait for a later tick."""
	return frappe.get_all(
		RUN,
		filters=_candidate_filters(),
		fields=["name", "conversation"],
		order_by="modified asc",
		limit=MAX_RUNS_PER_TICK,
	)


def _candidate_count() -> int:
	return int(frappe.db.count(RUN, _candidate_filters()) or 0)


def _beyond_the_cap(rows: list, cap: int, count) -> int:
	"""How many candidates a full list left unread (``count``: all of them, uncapped).
	Asked only when the list is full."""
	return max(count() - cap, 0) if len(rows) >= cap else 0


def _within_the_budget(rows: list, started: float, summary: dict, left: str):
	"""``rows`` in order, while the tick's time budget lasts. Out of time, the tick is
	marked so and every row not reached is counted in ``summary[left]``."""
	for position, row in enumerate(rows):
		if _clock() - started >= TICK_BUDGET_S:
			summary["out_of_time"] = 1
			summary[left] += len(rows) - position
			return
		yield row


def _cut_short(summary: dict) -> None:
	"""A tick that left candidates unlooked at (the cap, or its time budget) says so,
	once an hour: a run the check cannot act on keeps its place at the head of the
	list until the stale-run sweep closes it, and enough of them starve the rest. A
	summary the same, and when the runs used up the budget the summaries were not
	looked at at all."""
	why = "its time budget ran out" if summary["out_of_time"] else f"more than {MAX_RUNS_PER_TICK} candidates"
	left = []
	if summary["left"]:
		left.append(f"{summary['left']} candidate runs were not looked at")
	if summary["summaries_left"]:
		left.append(f"{summary['summaries_left']} stuck summaries were not looked at")
	_log_at_most_hourly(TICK_CUT_SHORT, f"The tick was cut short ({why}); {'; '.join(left)}.")


_NOTHING_SENT = object()
_STEP_TURN_FIELDS = [*macros._TURN_FIELDS, "done_at"]


def _chat(run):
	"""The run's chat (owner, status), or None when it is gone or archived."""
	if not run.conversation:
		return None
	conv = frappe.db.get_value(CONV, run.conversation, ["owner", "status"], as_dict=True)
	return conv if conv and conv.status != "Archived" else None


def _current_step(run, sent: int):
	"""The turn that decides how the run's current step ended: the newest turn of the
	step's message (the owner's Retry of a failed step is a second turn on it).
	``sent``: how many steps the run has sent. ``_NOTHING_SENT`` for a run with no
	step message and no turn. None for a run the check must not judge:

	* The chat's NEWEST step message has no Turn row. It was sent where none is
	  written (the shard was draining or on the legacy transport for a moment) and
	  its worker may be running it. ``macros._current_step_message`` takes such a
	  message for one that was never dispatched and answers with the step BEFORE it;
	  judged on that turn, long ``done``, the check sent step 3 before step 2 had
	  started, and ended a two-step run ``completed`` with its last step not run
	  (2026-10-03 review). The hook can afford that reading (it needs a second
	  delivery of the old end); a timer cannot. Left to the stale-run sweep.
	* The cursor is at 0 and a step message exists: sent under a random turn id.

	The message is found through the step's fixed turn id; a step sent before that id
	existed is the chat's newest step message."""
	newest = macros._newest_seed(run.conversation)
	if newest and not frappe.db.exists(TURN, {"seed_message": newest}):
		return None
	if sent == 0:
		return None if newest else _NOTHING_SENT
	seed = frappe.db.get_value(
		TURN,
		{"name": macros._step_turn_id(run.name, sent - 1), "conversation": run.conversation},
		"seed_message",
	)
	seed = seed or newest
	if not seed:
		return None
	rows = frappe.get_all(
		TURN, filters={"seed_message": seed}, fields=_STEP_TURN_FIELDS, order_by="creation desc", limit=1
	)
	return rows[0] if rows else None


def _steps_sent(run) -> int:
	"""``macros._next_step_index`` for a reader: the same walk past every step whose
	turn exists, with no cursor repair (``stuck_runs`` writes nothing)."""
	index, bound = int(run.current_step or 0), int(run.total_steps or 0)
	while index < bound and macros._step_is_out(run, index):
		index += 1
	return index


def _stuck(run) -> dict | None:
	"""``stuck_runs``' row for one ``running`` run, or None: what ``_RunCheck._look``
	acts on, read without acting, and only once it is ``STUCK_AFTER_S`` old."""
	if _seconds_since(run.modified) < STUCK_AFTER_S:
		return None

	def row(why: str, since, turn=None) -> dict:
		return {
			"run": run.name,
			"macro": run.macro,
			"conversation": run.conversation,
			"steps_sent": int(run.current_step or 0),
			"why": why,
			"turn": turn.name if turn else None,
			"turn_state": turn.state if turn else None,
			"since": str(since),
		}

	if not _chat(run):
		return row("chat_gone", run.modified)
	if not frappe.db.exists(MACRO, run.macro):
		return row("macro_gone", run.modified)
	step = _current_step(run, _steps_sent(run))
	if step is _NOTHING_SENT:
		return row("no_step_sent", run.modified)
	if step is None or not step_is_over(step.name, grace_s=STUCK_AFTER_S):
		return None
	return row("step_over", step.done_at, step)


def _a_hook_is_owed(turn_id: str, seed: str | None) -> bool:
	"""Whether the finalize ledger still owes the hook for any turn of the step's
	message (a ``macro_advance`` row that is not ``done``)."""
	turns = frappe.get_all(TURN, filters={"seed_message": seed}, pluck="name") if seed else []
	return any(macros._hook_effect_status(turn) not in (None, "done") for turn in {turn_id, *turns})


def _lost_hook_shape(run_name: str, turn_id: str) -> str | None:
	"""Which lost hook this is, or None when no hook was lost.

	* No ledger row: an end that never has a hook (the turn failed before it was
	  sent, ran out of recovery, aged out of the queue or was cancelled while
	  queued). By design: the run's status and reason are the record.
	* The row used its attempts: the ledger gave up on the hook (``force_done``).
	* The row is ``done`` and the hook itself logged that it gave up on the run lock
	  for this run (``turn_end_dropped``): ``hook_dropped``.
	* The row is ``done`` and no such log: the hook WAS delivered. What left the run
	  standing is something else (a hook that raised and logged it, a capacity
	  resume that died after its flip), and calling it a lost hook would send
	  whoever reads the log after the wrong thing. No Error Log."""
	attempts = _hook_attempts(turn_id)
	if attempts is None:
		return None
	if _ledger_gave_up_on_the_hook(turn_id, attempts):
		return SHAPE_FORCE_DONE
	if frappe.db.exists(LOG, {"method": f"jarvis.chat.macros.turn_end_dropped: {run_name}"}):
		return SHAPE_HOOK_DROPPED
	return None


def _hook_attempts(turn_id: str) -> int | None:
	"""How many times the finalize ledger tried the turn's hook; None: it has no row."""
	from jarvis.chat import turn_state

	attempts = frappe.db.get_value(turn_state.EFFECT, f"{turn_id}::macro_advance", "attempts")
	return None if attempts is None else int(attempts or 0)


def _ledger_gave_up_on_the_hook(turn_id: str, attempts: int | None = None) -> bool:
	"""Whether the turn's hook used every attempt the ledger gives it (``force_done``)."""
	from jarvis.chat import turn_state

	if attempts is None:
		attempts = _hook_attempts(turn_id)
	return attempts is not None and attempts >= turn_state.FINALIZE_MAX_ATTEMPTS


def _a_lost_race(e: Exception, summary: dict, lock_wait_message: str) -> bool:
	"""Whether ``e`` is a write conflict or a lock wait (``frappe.QueryTimeoutError``):
	both routine on a row other workers write (``jarvis.chat.txn``), counted and tried
	again on the next tick. A lock wait can last, fifty seconds of the tick each time,
	so it leaves ``lock_wait_message`` in an Error Log at most once an hour."""
	from jarvis.chat import txn

	if not (txn.is_write_conflict(e) or isinstance(e, frappe.QueryTimeoutError)):
		return False
	summary["conflicts"] += 1
	if isinstance(e, frappe.QueryTimeoutError):
		_log_at_most_hourly(LOCK_WAIT, lock_wait_message)
	return True


def _signal(
	name: str, shape: str, turn_state: str | None, *, subject: str = "run", key: str | None = None
) -> None:
	"""One Error Log row per run (or per stuck summary: ``subject`` "macro", ``key``
	its chat) the check moved in a hook's place. The title is forwarded off the bench
	(``api_errors``), and its body with it: the shape in the title; the run or macro,
	the turn's state and the shape in the body. Never a prompt or a reply."""
	key = key or _key("signalled", name)
	if frappe.cache().get_value(key, expires=True):
		return
	frappe.cache().set_value(key, 1, expires_in_sec=_MARKER_TTL_S)
	frappe.log_error(
		title=f"jarvis.chat.macros.reconciled: {shape}",
		message=f"{subject} {name}; turn state {turn_state or 'no turn'}; shape {shape}",
	)
	frappe.db.commit()


def _log_at_most_hourly(title: str, message: str) -> None:
	"""An Error Log row unless one of this title was written in the last hour. Asked
	of the Error Log itself, not of a cache marker: redis being down is one of the
	faults this reports. Never raises."""
	try:
		since = frappe.utils.add_to_date(now_datetime(), seconds=-LOG_AT_MOST_EVERY_S)
		if frappe.db.exists(LOG, {"method": title, "creation": [">", since]}):
			return
		frappe.log_error(title=title, message=message)
		frappe.db.commit()
	except Exception:
		_rollback()


def _steps_that_ran(conversation: str) -> list[int]:
	"""The numbers of the run's steps that were handed to the agent and ended. Not
	the cursor, which counts steps SENT: with "Stop on error" off a step that never
	reached the agent (the gateway unreachable, aged out of the queue, cancelled
	while queued) is passed over, and "Steps 1 to 3 ran" then named one that did
	not. A step with no Turn row (sent where none is written) is counted: nothing
	says it did not run."""
	seeds = [m.name for m in macros._macro_seeds(conversation)]
	if not seeds:
		return []
	turns = frappe.get_all(
		TURN, filters={"seed_message": ["in", seeds]}, fields=["seed_message", "state", "dispatching_at"]
	)
	ran: dict[str, bool] = {}
	for turn in turns:  # a retried step: any of its turns
		reached_the_agent = turn.state == "done" or bool(turn.dispatching_at)
		ran[turn.seed_message] = ran.get(turn.seed_message, False) or (
			turn.state in macros._TURN_ENDED and reached_the_agent
		)
	return [number for number, seed in enumerate(seeds, start=1) if ran.get(seed, True)]


def _interrupted(ran: list[int]) -> str:
	head = "The run was interrupted and did not finish."
	if not ran:
		return f"{head} No step ran."
	if ran == list(range(1, len(ran) + 1)):
		which = "Step 1" if len(ran) == 1 else f"Steps 1 to {len(ran)}"
	else:
		which = ("Step " if len(ran) == 1 else "Steps ") + macros._listed(ran)
	return f"{head} {which} ran; the rest did not."


def _send_gate_refusal(owner: str) -> str | None:
	"""The sentence of the send gate a typed message and ``run_macro`` pass
	(``policy.validate_can_send``: a usage limit, a suspended subscription, a release
	or maintenance hold, a workspace being rebuilt), or None when it is open. The
	hook does not ask, seconds after a step. The check sends a step up to twenty
	minutes after the last, and lost hooks cluster around exactly the rolls and
	rebuilds that close the gate: with "Stop on error" off each tick sent one more
	step into it."""
	from jarvis.chat.policy import validate_can_send

	ok, reason = validate_can_send(owner)
	return None if ok else macros._BLOCK_MESSAGE.get(reason, _GATE_CLOSED)


def _may_run_unattended(owner: str | None) -> bool:
	"""The scheduler's two identity checks (``macro_scheduler._sweep_one``): never
	Administrator, Guest or a disabled user, and only one who can still reach Jarvis."""
	from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

	return bool(owner) and is_valid_unattended_owner(owner) and has_jarvis_access(owner)


def _seconds_since(moment) -> float:
	"""On the site's clock, like ``done_at`` and ``modified`` themselves: naive local
	times. In the hour a daylight-saving change repeats or skips, an age read here
	is an hour off."""
	return (now_datetime() - get_datetime(moment)).total_seconds()


def _key(kind: str, run_name: str) -> str:
	return f"{CACHE_PREFIX}:{kind}:{run_name}"


def _clock() -> float:
	return time.monotonic()


def _rollback() -> None:
	try:
		frappe.db.rollback()
	except Exception:
		pass
