"""The five minute check moves or ends a macro run whose step is over and whose step
end never reached the chaining hook (``jarvis.chat.macro_reconcile``).

From the 2026-10-01 macros review: such a run sat ``running``, its chat armed, until
the stale-run sweep three hours later. The check rests on one question, "this turn
has ended and no hook is on its way" (``step_is_over``), so these tests do two things:

* each way a step's turn ends WITHOUT the hook moving the run is produced by the real
  code that ends it (prepare, the ack failure handler, the pump watchdog, the cancel
  endpoint, the finalize ledger, the hook's own lock miss), on a real macro run whose
  step went out through the real dispatch; the check then has to end or move the run;
* ``macros.advance_after_turn`` is wrapped by a spy that records, at every real call,
  what ``step_is_over`` said the instant before. For a hook the finalize ledger
  delivers it must say "no": the check must never act on a step whose hook is coming.

The state just before an edge is set on the Turn row; the edge itself is real. The
queue age-out and the prepare failure run through the pump's own promote and watchdog.
"""

from __future__ import annotations

import json
from collections import defaultdict
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from jarvis import _redis_lock
from jarvis.chat import admission, finalize, macro_reconcile, macros, prepare, pump, settlement
from jarvis.chat import turn_state as ts
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests.test_pump import TEST_USER, _Recorder, _RejectGateway
from jarvis.tests.test_pump_pipeline import _PipelineCase

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
EFFECT = "Jarvis Turn Effect"
MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"
PUMP = "Jarvis Relay Pump"
LOG = "Error Log"
SIGNAL = "jarvis.chat.macros.reconciled: "

# ``jarvis_pump_enabled`` in site config, the Phase-0 flag, and the shard row's own
# ``transport_mode``. ``None`` = the key is absent.
MODES = {
	"pump": (None, None, "pump"),
	"legacy": (0, None, "legacy"),
	"draining": ("draining", None, "draining"),
	"phase0": (0, 1, "legacy"),
	# The row was flipped to `legacy`; the config mirror still says pump.
	"kill switch": (None, None, "legacy"),
	# The reverse instant: the config already says off, the row is still `pump`.
	"config off, row pump": (0, None, "pump"),
}


def setUpModule():
	install_synthetic_runtime_profile()


class _Killed(BaseException):
	"""A worker process dying: not an ``Exception``, so nothing on the way out handles
	it (no rollback, no release, no log), like SIGKILL or an OOM."""


class CheckBase(_PipelineCase):
	"""Real macro runs on a shard of this test's own, in ``pump`` mode. The accept gate,
	the pump's edges and the finalize ledger are the real ones; nothing wakes a pump,
	so a step that is sent stays ``queued`` until the test moves it."""

	def setUp(self):
		frappe.set_user("Administrator")
		super().setUp()
		self.calls: list[dict] = []
		self._runs: list[str] = []
		self._made: list[tuple[str, str]] = []
		self._real_advance = macros.advance_after_turn
		self._conf = {k: frappe.local.conf.get(k) for k in ("jarvis_pump_enabled", admission.FLAG)}
		self._conf[macro_reconcile.OFF_SWITCH] = frappe.local.conf.get(macro_reconcile.OFF_SWITCH)
		frappe.local.conf.pop(macro_reconcile.OFF_SWITCH, None)
		self._set_mode("pump")
		self._drop_logs()
		real_candidates = macro_reconcile._candidates

		def candidates():
			# The site is shared: only this test's runs, through the real query.
			return [row for row in real_candidates() if row.name in self._runs]

		for p in (
			patch.object(pump, "ensure_pump", lambda *a, **k: {"enqueued": False}),
			patch.object(pump, "lpush_wake", lambda *a, **k: None),
			patch.object(admission, "relay_target_id", lambda conversation=None: self._target),
			patch.object(macros, "advance_after_turn", self._spy),
			patch.object(macro_reconcile, "_candidates", candidates),
			# Every finalize effect other than the macro hook, at its boundary.
			patch("jarvis.chat.turn_handler.persist_rich_outputs", _Recorder()),
			patch("jarvis.learning.app_analysis.on_turn_end", _Recorder()),
			patch("jarvis.chat.title.enqueue_autotitle", _Recorder()),
			patch.dict("jarvis.chat.finalize._RUNNERS", {"usage": self._usage_noop}),
			patch("jarvis.chat.wiki.wiki_enabled", return_value=False),
		):
			p.start()
			self.addCleanup(p.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for key, value in self._conf.items():
			if value is None:
				frappe.local.conf.pop(key, None)
			else:
				frappe.local.conf[key] = value
		for run in self._runs:
			for kind in ("skip", "resent", "signalled"):
				frappe.cache().delete_value(macro_reconcile._key(kind, run))
		frappe.db.delete(
			EFFECT,
			{"turn": ["in", frappe.get_all(TURN, {"relay_target_id": self._target}, pluck="name") or [""]]},
		)
		frappe.db.delete(TURN, {"relay_target_id": self._target})
		for dt in (RUN, MACRO):
			for name in frappe.get_all(dt, filters={"owner": TEST_USER}, pluck="name"):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		for dt, name in self._made:
			if dt == CONV:
				frappe.db.delete(MSG, {"conversation": name})
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.delete("Notification Log", {"for_user": TEST_USER})
		self._drop_logs()
		frappe.db.commit()
		pump._LIFECYCLE_MODE_CACHE.pop(self._target, None)
		super().tearDown()

	def _drop_logs(self):
		frappe.db.delete(LOG, {"method": ["like", "jarvis.chat.macros.reconcil%"]})
		frappe.db.commit()

	@staticmethod
	def _usage_noop(ctx):
		ts._run_cas(
			f"UPDATE `tab{TURN}` SET usage_recorded=1 WHERE name=%(r)s AND usage_recorded=0",
			{"r": ctx.run_id},
		)

	def _set_mode(self, name):
		pump_flag, phase0, row = MODES[name]
		for key, value in (("jarvis_pump_enabled", pump_flag), (admission.FLAG, phase0)):
			if value is None:
				frappe.local.conf.pop(key, None)
			else:
				frappe.local.conf[key] = value
		frappe.db.set_value(PUMP, self._target, "transport_mode", row, update_modified=False)
		frappe.db.commit()
		pump._LIFECYCLE_MODE_CACHE.pop(self._target, None)

	# --- the spy ---------------------------------------------------------------- #

	def _spy(self, conversation_id, **kw):
		rid = kw.get("run_id")
		self.calls.append(
			{
				"kw": kw,
				"turn_state": frappe.db.get_value(TURN, rid, "state") if rid else None,
				"effect": frappe.db.get_value(EFFECT, f"{rid}::macro_advance", "status") if rid else None,
				# With no grace: the ledger alone must say the hook is coming.
				"over": macro_reconcile.step_is_over(rid, grace_s=0) if rid else None,
			}
		)
		return self._real_advance(conversation_id, **kw)

	# --- fixtures --------------------------------------------------------------- #

	def _mk_run(
		self,
		*,
		steps=2,
		stop_on_error=1,
		armed=False,
		trigger="manual",
		owner=TEST_USER,
		send=True,
		prompt="step",
	):
		"""A real macro and run of ``owner``'s. ``send``: step 1 goes out through the
		real ``_dispatch_step`` and sits ``queued``. Returns ``(run, conv, turn)``."""
		frappe.set_user(owner)
		macro = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"reconcile-{frappe.generate_hash(length=6)}",
				"stop_on_error": stop_on_error,
				"steps": [{"prompt": f"{prompt} {i}"} for i in range(1, steps + 1)],
			}
		).insert(ignore_permissions=True)
		conv = self._mk_conv()
		frappe.db.set_value(CONV, conv, "session_key", f"sk-{conv}", update_modified=False)
		if armed:
			frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"macro": macro.name,
				"conversation": conv,
				"status": "running",
				"current_step": 0,
				"total_steps": steps,
				"run_mode": "stepped",
				"trigger": trigger,
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		frappe.db.commit()
		self._runs.append(run.name)
		self._made += [(RUN, run.name), (MACRO, macro.name), (CONV, conv)]
		rid = None
		if send:
			self.assertEqual(macros._dispatch_step(run, macro, 0), macros._STEP_SENT)
			rid = macros._step_turn_id(run.name, 0)
			self.assertEqual(self._state(rid), "queued")
		frappe.set_user(TEST_USER)
		return run.name, conv, rid

	def _force(self, rid, **fields):
		"""Put the Turn where a real pipeline would have it just before the edge under
		test (the edge itself then runs for real)."""
		frappe.db.set_value(TURN, rid, fields, update_modified=False)
		frappe.db.commit()

	def _placeholder(self, conv, **extra):
		return self._mk_msg(conv, role="assistant", content=extra.pop("content", ""), **extra)

	def _run(self, name):
		frappe.db.commit()
		return frappe.db.get_value(RUN, name, ["status", "error", "current_step"], as_dict=True)

	def _row(self, name):
		"""The whole run row, as stored."""
		frappe.db.commit()
		return frappe.db.sql(f"SELECT * FROM `tab{RUN}` WHERE name=%s", name, as_dict=True)[0]

	def _seeds(self, conv):
		return [m.name for m in macros._macro_seeds(conv)]

	def _ago(self, seconds):
		return frappe.utils.add_to_date(None, seconds=-seconds)

	def _wd(self):
		"""One tick of the pump's own watchdog on this shard."""
		deps = pump.PumpDeps()
		deps.enqueue_finalize = _Recorder()
		deps.enqueue_pump_job = _Recorder()
		summary = defaultdict(int)
		pump._watchdog_shard(self._target, deps, summary)
		return summary

	def _idle(self, run, seconds=300):
		"""The run row was last written ``seconds`` ago."""
		frappe.db.sql(f"UPDATE `tab{RUN}` SET modified=%s WHERE name=%s", (self._ago(seconds), run))
		frappe.db.commit()

	def _ended(self, rid, seconds=300):
		"""The turn ended ``seconds`` ago."""
		self.assertIsNotNone(frappe.db.get_value(TURN, rid, "done_at"), "premise: the turn has ended")
		frappe.db.sql(f"UPDATE `tab{TURN}` SET done_at=%s WHERE name=%s", (self._ago(seconds), rid))
		frappe.db.commit()

	def _settled_long_enough(self, run, rid, seconds=300):
		self._ended(rid, seconds)
		self._idle(run, seconds)

	def _tick(self):
		"""The cron: as Administrator, like the scheduler. The session is the same after."""
		frappe.db.commit()
		frappe.set_user("Administrator")
		try:
			out = macro_reconcile.reconcile_running_runs()
			self.assertEqual(frappe.session.user, "Administrator", "the check left the session switched")
		finally:
			frappe.set_user(TEST_USER)
		frappe.db.commit()
		return out

	def _signals(self):
		frappe.db.commit()
		return frappe.get_all(LOG, filters={"method": ["like", f"{SIGNAL}%"]}, fields=["method", "error"])

	def _closing_lines(self, conv):
		return frappe.get_all(
			MSG, filters={"conversation": conv, "role": "assistant", "ref_doctype": RUN}, pluck="content"
		)

	def _notifications(self):
		return frappe.get_all("Notification Log", filters={"for_user": TEST_USER}, pluck="email_content")

	# --- ways a step ends without moving its run --------------------------------- #

	def _age_out(self, rid):
		"""The pump's watchdog cancels a step that waited 15 minutes in the queue."""
		self._force(rid, enqueued_at=self._ago(ts.QUEUED_MAX_AGE_S + 60))
		self.assertEqual(self._wd()["aged_out"], 1)
		self.assertEqual(self._state(rid), "cancelled")

	def _settle_success(self, conv, rid, reply="answer"):
		amsg = self._placeholder(conv, content=reply, streaming=0)
		self._force(
			rid, state="finalizing", version=5, assistant_message=amsg, finalizing_at=frappe.utils.now()
		)
		ts.insert_required_effects(rid, settlement.FINAL_EFFECTS)
		frappe.db.commit()

	def _drop_the_hook(self, run, conv, rid, reply="answer"):
		"""A step that WORKED, and whose hook gave up on the run lock: the Turn is
		``done``, its ``macro_advance`` effect ``done``, and the run where it was."""
		self._settle_success(conv, rid, reply)
		real_lock = _redis_lock.redis_lock

		@contextmanager
		def impatient(name, *, timeout_s=60, blocking_timeout_s=0.0):
			# The same lock and code path; only the 10 s wait is shortened.
			with real_lock(name, timeout_s=timeout_s, blocking_timeout_s=min(blocking_timeout_s, 0.3)) as got:
				yield got

		before = len(self.calls)
		with real_lock(f"jarvis_macro_run:{run}", timeout_s=60) as held:
			self.assertTrue(held)
			with patch.object(_redis_lock, "redis_lock", impatient):
				finalize.run_finalize(rid, self._target)
		self.assertEqual(len(self.calls), before + 1, "premise: the hook WAS called")
		self.assertEqual((self._state(rid), self._effects(rid)["macro_advance"]), ("done", "done"))
		frappe.db.delete(LOG, {"method": ["like", "jarvis.chat.macros.turn_end_dropped%"]})
		frappe.db.commit()

	def _die_in_the_hook(self, rid):
		def dies(*a, **k):
			raise _Killed()

		with patch.object(macros, "advance_after_turn", dies):
			with self.assertRaises(_Killed):
				finalize.run_finalize(rid, self._target)
		frappe.db.rollback()

	def _force_done(self, rid):
		"""The finalize job dies inside the hook until the ledger gives up on it."""
		for _ in range(ts.FINALIZE_MAX_ATTEMPTS):
			self._die_in_the_hook(rid)
			self.assertFalse(macro_reconcile.step_is_over(rid, grace_s=0), "a retry is still owed")
			frappe.db.set_value(
				EFFECT,
				f"{rid}::macro_advance",
				"claimed_at",
				self._ago(ts.EFFECT_CLAIM_STALE_S + 5),
				update_modified=False,
			)
			frappe.db.commit()
		before = len(self.calls)
		finalize.run_finalize(rid, self._target)
		self.assertEqual(len(self.calls), before, "premise: force-done, the hook never ran")
		self.assertEqual(self._effects(rid)["macro_advance"], "done")

	def _assert_a_late_hook_changes_nothing(self, run, conv, rid):
		before = (dict(self._row(run)), len(self._seeds(conv)), frappe.db.count(TURN, {"conversation": conv}))
		self._real_advance(conv, errored=True, run_id=rid)
		self._real_advance(conv, errored=False, run_id=rid)
		after = (dict(self._row(run)), len(self._seeds(conv)), frappe.db.count(TURN, {"conversation": conv}))
		self.assertEqual(before, after, "a hook after the check moved the run again")


# --------------------------------------------------------------------------- #
# Ends that never call the hook, by design: the run ends with the turn's own reason
# --------------------------------------------------------------------------- #
class TestAStepThatEndedWithNoHook(CheckBase):
	def _assert_ended(self, run, conv, rid, *, status, reason=None):
		self.assertEqual(self._effects(rid), {}, "premise: no finalize ledger for this end")
		self.assertEqual(self.calls, [], "premise: the hook was never called")
		self.assertEqual(self._run(run).status, "running", "premise: the run is stuck")
		self._settled_long_enough(run, rid)
		self.assertEqual(self._tick()["acted"], 1)
		row = self._run(run)
		self.assertEqual(row.status, status)
		if reason:
			self.assertIn(reason, row.error or "")
		self.assertEqual(len(self._seeds(conv)), 1, "no step was sent after a failed one (Stop on error)")
		# By design: the run's status and reason are the record.
		self.assertEqual(self._signals(), [])
		self._assert_a_late_hook_changes_nothing(run, conv, rid)

	def test_the_gateway_was_unreachable_when_the_step_was_prepared(self):
		# Through the pump's own promote and the real prepare job.
		run, conv, rid = self._mk_run()

		@contextmanager
		def down(url):
			raise AgentUnreachableError("The assistant could not be reached.")
			yield

		def real_prepare(run_id, target):
			with patch("jarvis.chat.agent_session_pool.checkout", down):
				prepare.run_prepare(run_id, target)

		ctx = self._make_ctx(self._deps(double=self._double(), prepare=real_prepare))
		pump._promote_queued(ctx)
		self.assertEqual(self._state(rid), "errored")
		self._assert_ended(
			run, conv, rid, status="failed", reason="Step 1 failed: The assistant could not be reached."
		)
		# The owner is told: the reason on the run, the closing line in the chat, a
		# notification (nobody is watching a run found minutes later).
		self.assertIn("could not be reached", self._closing_lines(conv)[0])
		self.assertIn("could not be reached", self._notifications()[0])

	def test_the_send_was_rejected(self):
		run, conv, rid = self._mk_run()
		amsg = self._placeholder(conv, streaming=1)
		self._force(
			rid,
			state="ready",
			version=2,
			reserved=1,
			assistant_message=amsg,
			ready_at=frappe.utils.now(),
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		double = _RejectGateway()
		self._doubles.append(double)
		ctx = self._make_ctx(self._deps(double=double))
		pump._dispatch_ready(ctx)
		self._pump_until(ctx, lambda: self._state(rid) in ("errored", "recovering"))
		self.assertEqual(self._state(rid), "errored")
		self._assert_ended(run, conv, rid, status="failed", reason="Step 1 failed")

	def test_the_recovery_budget_ran_out(self):
		run, conv, rid = self._mk_run()
		amsg = self._placeholder(conv, content="partial", streaming=1, recovering=1)
		epoch = self._acquire_fresh("budget")
		self._force(
			rid,
			state="streaming",
			version=4,
			pump_epoch=epoch,
			reserved=1,
			assistant_message=amsg,
			dispatching_at=self._ago(1500),
			deadline_at=self._ago(900),
			recovery_started_at=self._ago(pump.RECOVERY_BUDGET_S + 60),
		)
		self.assertEqual(self._wd()["errored"], 1)
		self.assertEqual(
			int(frappe.db.get_value(MSG, amsg, "streaming")), 0, "premise: the reply was stamped"
		)
		self._assert_ended(run, conv, rid, status="failed", reason="Step 1 failed")

	def test_the_step_waited_too_long_in_the_queue(self):
		# Through the pump's own watchdog.
		run, conv, rid = self._mk_run()
		self._age_out(rid)
		self._assert_ended(run, conv, rid, status="failed", reason="Waited too long")

	def test_the_owner_cancelled_the_queued_step(self):
		run, conv, rid = self._mk_run()
		out = admission.cancel_queued_turn(rid)
		self.assertEqual((out["ok"], out["path"]), (True, "queued"))
		# Their own stop of the step: the run stopped, it did not fail. No reason.
		self._assert_ended(run, conv, rid, status="stopped")
		self.assertEqual(self._run(run).error or "", "")

	def test_the_owner_cancelled_the_step_while_it_was_being_prepared(self):
		run, conv, rid = self._mk_run()
		amsg = self._placeholder(conv, streaming=1)
		self._force(
			rid,
			state="preparing",
			version=1,
			reserved=1,
			assistant_message=amsg,
			preparing_at=frappe.utils.now(),
		)
		out = admission.cancel_queued_turn(rid)
		self.assertEqual((out["ok"], out["path"]), (True, "preparing_ready"))
		self._assert_ended(run, conv, rid, status="stopped")

	def test_with_stop_on_error_off_the_next_step_goes_out(self):
		run, conv, rid = self._mk_run(steps=3, stop_on_error=0)
		self._age_out(rid)
		self._settled_long_enough(run, rid)
		self.assertEqual(self._tick()["acted"], 1)
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 2))
		self.assertEqual(self._state(macros._step_turn_id(run, 1)), "queued")
		self.assertEqual(self._signals(), [])

	def test_an_armed_run_the_check_ends_has_its_chat_disarmed(self):
		run, conv, rid = self._mk_run(armed=True)
		self._age_out(rid)
		self._settled_long_enough(run, rid)
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation")), 1)
		self._tick()
		self.assertEqual(self._run(run).status, "failed")
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation")), 0)

	def test_a_scheduled_run_is_told_once(self):
		run, conv, rid = self._mk_run(trigger="scheduled")
		self._age_out(rid)
		self._settled_long_enough(run, rid)
		self._tick()
		self.assertEqual(len(self._notifications()), 1, "the engine's own notification, not a second one")


# --------------------------------------------------------------------------- #
# A hook that should have come and did not
# --------------------------------------------------------------------------- #
class TestAHookThatWasLost(CheckBase):
	def test_a_hook_dropped_on_the_run_lock_is_made_up_for(self):
		run, conv, rid = self._mk_run()
		self._drop_the_hook(run, conv, rid)
		self.assertEqual(self._run(run).current_step, 1, "premise: the run did not move")
		self._settled_long_enough(run, rid)
		self.assertEqual(self._tick()["acted"], 1)
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 2))
		self.assertEqual(self._state(macros._step_turn_id(run, 1)), "queued")
		self.assertEqual([s.method for s in self._signals()], [SIGNAL + "hook_dropped"])
		self._assert_a_late_hook_changes_nothing(run, conv, rid)

	def test_a_finalize_job_that_died_three_times_in_the_hook(self):
		run, conv, rid = self._mk_run()
		self._settle_success(conv, rid)
		self._force_done(rid)
		self.assertEqual((self._state(rid), self._run(run).current_step), ("done", 1))
		self._settled_long_enough(run, rid)
		self.assertEqual(self._tick()["acted"], 1)
		self.assertEqual(self._run(run).current_step, 2)
		self.assertEqual([s.method for s in self._signals()], [SIGNAL + "force_done"])

	def test_the_same_on_a_step_that_failed(self):
		run, conv, rid = self._mk_run()
		self._force(rid, state="errored", version=5, error="The model refused.", done_at=frappe.utils.now())
		ts.insert_required_effects(rid, settlement.TERMINAL_EFFECTS)
		frappe.db.commit()
		self._force_done(rid)
		self._settled_long_enough(run, rid)
		self._tick()
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("failed", "Step 1 failed: The model refused."))
		self.assertEqual([s.method for s in self._signals()], [SIGNAL + "force_done"])

	def test_the_last_step_ends_the_run_as_the_hook_would_have(self):
		run, conv, rid = self._mk_run(steps=1)
		self._drop_the_hook(run, conv, rid)
		self._settled_long_enough(run, rid, seconds=3 * 3600 - 300)  # however late: an end is an end
		self._tick()
		self.assertEqual((self._run(run).status, self._run(run).error or ""), ("completed", ""))
		self.assertEqual(self._notifications(), [])

	def test_the_signal_carries_no_prompt_and_no_reply_and_is_written_once_per_run(self):
		run, conv, rid = self._mk_run(steps=3, prompt="PROMPT-TEXT-zq7")
		self._drop_the_hook(run, conv, rid, reply="REPLY-TEXT-zq7")
		self._settled_long_enough(run, rid)
		self._tick()
		second = macros._step_turn_id(run, 1)
		self._drop_the_hook(run, conv, second, reply="REPLY-TEXT-zq7")
		self._settled_long_enough(run, second)
		self._tick()
		self.assertEqual(self._run(run).current_step, 3, "premise: the check moved the run twice")
		rows = self._signals()
		self.assertEqual(len(rows), 1, "one Error Log row per run")
		self.assertEqual(rows[0].method, SIGNAL + "hook_dropped")
		self.assertEqual(rows[0].error, f"run {run}; turn state done; shape hook_dropped")
		for row in frappe.get_all(
			LOG, filters={"method": ["like", "jarvis.chat.macros.reconcil%"]}, fields=["*"]
		):
			self.assertNotIn("zq7", json.dumps(row, default=str))


# --------------------------------------------------------------------------- #
# The predicate: never true while a hook is on its way
# --------------------------------------------------------------------------- #
class TestThePredicateWhileAHookIsComing(CheckBase):
	def _assert_never_over_at_a_hook_call(self, n):
		self.assertEqual(len(self.calls), n)
		for call in self.calls:
			self.assertFalse(call["over"], f"the check would have acted under a hook being delivered: {call}")

	def test_a_normal_step_through_the_whole_pipeline(self):
		run, conv, rid = self._mk_run()
		double = self._double()
		ctx = self._make_ctx(self._deps(double=double, prepare=self._prepare_stub(conv, double, "success")))
		self._pump_until(ctx, lambda: self._state(rid) in ("finalizing", "done"))
		self.assertEqual(self._state(rid), "finalizing")
		self.assertFalse(macro_reconcile.step_is_over(rid, grace_s=0), "settled, the hook still to come")
		self._idle(run)
		self.assertEqual(self._tick()["acted"], 0)
		finalize.run_finalize(rid, self._target)
		self._assert_never_over_at_a_hook_call(1)
		self.assertEqual((self.calls[0]["turn_state"], self.calls[0]["effect"]), ("finalizing", "running"))
		self.assertEqual((self._state(rid), self._run(run).current_step), ("done", 2))

	def test_a_settled_error_is_terminal_before_its_hook(self):
		run, conv, rid = self._mk_run()
		amsg = self._placeholder(conv, content="partial", streaming=1)
		epoch = self._acquire_fresh("err")
		payload = {"state": "error", "error": "provider quota exceeded"}
		self._force(
			rid,
			state="terminal_observed",
			version=3,
			pump_epoch=epoch,
			reserved=1,
			assistant_message=amsg,
			terminal_kind="relay:error",
			terminal_payload=json.dumps(payload),
			terminal_observed_at=frappe.utils.now(),
		)
		deps = pump.PumpDeps()
		deps.enqueue_finalize = _Recorder()
		settlement.invoke_settlement(
			rid,
			relay_target_id=self._target,
			epoch=epoch,
			version=3,
			terminal_kind="relay:error",
			terminal_payload=payload,
			assistant_message=None,
			owner=TEST_USER,
			conversation=conv,
			deps=deps,
		)
		# Terminal already, and only the ledger row says a hook is coming: however
		# long ago it ended, the check waits.
		self.assertEqual((self._state(rid), self._effects(rid)["macro_advance"]), ("errored", "pending"))
		self._settled_long_enough(run, rid, seconds=900)
		self.assertEqual(self._tick()["acted"], 0)
		self.assertEqual(self._run(run).status, "running")
		finalize.run_finalize(rid, self._target)
		self._assert_never_over_at_a_hook_call(1)
		self.assertEqual(self._run(run).status, "failed")

	def test_a_stop_mid_stream(self):
		run, conv, rid = self._mk_run()
		amsg = self._placeholder(conv, content="partial", streaming=1)
		ctx = self._make_ctx(self._deps(double=self._double()))
		self._force(
			rid,
			state="streaming",
			version=4,
			pump_epoch=ctx.epoch,
			reserved=1,
			assistant_message=amsg,
			gateway_run_id=rid,
			dispatching_at=frappe.utils.now(),
			dispatch_payload=json.dumps({"session_key": f"sess-{rid}", "message": "hi"}),
		)
		self.assertTrue(ts.request_cancel(rid, 4))
		frappe.db.commit()
		self.assertEqual(pump._cancel_sweep(ctx), 1)
		self.assertEqual(self._state(rid), "cancelled")
		self.assertFalse(macro_reconcile.step_is_over(rid, grace_s=0))
		finalize.run_finalize(rid, self._target)
		self._assert_never_over_at_a_hook_call(1)
		self.assertEqual(self._run(run).status, "stopped")

	def test_a_finalize_job_that_died_once_in_the_hook_is_waited_for(self):
		run, conv, rid = self._mk_run()
		self._settle_success(conv, rid)
		self._die_in_the_hook(rid)
		self.assertEqual(self._effects(rid)["macro_advance"], "running")
		self._idle(run)
		self.assertEqual(self._tick()["acted"], 0)
		frappe.db.set_value(
			EFFECT,
			f"{rid}::macro_advance",
			"claimed_at",
			self._ago(ts.EFFECT_CLAIM_STALE_S + 5),
			update_modified=False,
		)
		frappe.db.commit()
		finalize.run_finalize(rid, self._target)
		self._assert_never_over_at_a_hook_call(1)
		self.assertEqual(self._run(run).current_step, 2)

	def test_a_hook_delivered_twice_by_the_ledger(self):
		run, conv, rid = self._mk_run(steps=3)
		self._settle_success(conv, rid)
		with patch("jarvis.learning.app_analysis.on_turn_end", side_effect=RuntimeError("boom")):
			finalize.run_finalize(rid, self._target)
			finalize.run_finalize(rid, self._target)
		self._assert_never_over_at_a_hook_call(2)
		self.assertEqual((self._run(run).current_step, len(self._seeds(conv))), (2, 2))

	def test_a_hook_that_gives_up_on_the_lock_was_not_pre_empted(self):
		run, conv, rid = self._mk_run()
		self._drop_the_hook(run, conv, rid)
		self._assert_never_over_at_a_hook_call(1)

	def test_a_finalizing_turn_is_never_over_whatever_its_ledger_says(self):
		run, conv, rid = self._mk_run()
		self._settle_success(conv, rid)
		frappe.db.set_value(EFFECT, f"{rid}::macro_advance", "status", "done", update_modified=False)
		self._force(rid, done_at=self._ago(900))
		self._idle(run)
		self.assertFalse(macro_reconcile.step_is_over(rid))
		self.assertEqual(self._tick()["acted"], 0)

	def test_a_reply_turn_recovery_still_owns_is_waited_for(self):
		# The budget errored the Turn and the reply's stamp did not land: turn recovery
		# finishes that reply (up to an hour later) and calls the hook itself.
		run, conv, rid = self._mk_run()
		amsg = self._placeholder(conv, content="partial", streaming=1, recovering=1)
		self._force(
			rid, state="errored", version=5, assistant_message=amsg, error="stalled", done_at=self._ago(900)
		)
		self._idle(run)
		self.assertEqual(self._tick()["acted"], 0)
		frappe.db.set_value(MSG, amsg, {"streaming": 0, "recovering": 0}, update_modified=False)
		self.assertEqual(self._tick()["acted"], 1)

	def test_a_step_that_ended_under_two_minutes_ago_is_left_for_the_next_tick(self):
		# The edge commits the end first and writes the rest after (the cancel marker).
		run, conv, rid = self._mk_run()
		self._age_out(rid)
		self._idle(run)
		self._ended(rid, seconds=macro_reconcile.STEP_END_GRACE_S - 30)
		self.assertEqual(self._tick()["acted"], 0)
		self._ended(rid, seconds=macro_reconcile.STEP_END_GRACE_S + 5)
		self.assertEqual(self._tick()["acted"], 1)

	def test_a_retried_step_is_judged_by_its_newest_turn(self):
		run, conv, rid = self._mk_run(stop_on_error=0)
		self._force(rid, state="errored", version=5, error="The model refused.", done_at=self._ago(900))
		seed = self._seeds(conv)[0]
		self._mk_turn(conv, "reconcile_retry", seed, "streaming")
		self._idle(run)
		self.assertEqual(self._tick()["acted"], 0, "the owner's Retry is still running")
		self.assertEqual(self._run(run).current_step, 1)

	def test_a_retry_that_worked_and_lost_its_hook_moves_the_run(self):
		# Judged by the step's first turn the end would be refused (it was retried),
		# and the run would wait for the stale-run sweep.
		run, conv, rid = self._mk_run()
		self._force(rid, state="errored", version=5, error="The model refused.", done_at=self._ago(900))
		reply = self._placeholder(conv, content="answer")
		self._mk_turn(
			conv,
			"reconcile_retry",
			self._seeds(conv)[0],
			"done",
			assistant_message=reply,
			done_at=self._ago(300),
		)
		self._idle(run)
		self.assertEqual(self._tick()["acted"], 1)
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 2))


# --------------------------------------------------------------------------- #
# One advance, whoever comes first
# --------------------------------------------------------------------------- #
class TestOneAdvance(CheckBase):
	def _one_step_two(self, run, conv):
		self.assertEqual(self._run(run).current_step, 2)
		self.assertEqual(len(self._seeds(conv)), 2)
		self.assertEqual(frappe.db.count(TURN, {"conversation": conv}), 2)

	def test_the_hook_then_the_check(self):
		run, conv, rid = self._mk_run(steps=3)
		self._settle_success(conv, rid)
		finalize.run_finalize(rid, self._target)
		self._one_step_two(run, conv)
		self._settled_long_enough(run, rid)
		before = self._row(run)
		self.assertEqual(self._tick()["acted"], 0)
		self.assertEqual(self._row(run), before)
		self._one_step_two(run, conv)

	def test_the_check_then_the_hook(self):
		run, conv, rid = self._mk_run(steps=3)
		self._drop_the_hook(run, conv, rid)
		self._settled_long_enough(run, rid)
		self._tick()
		self._one_step_two(run, conv)
		self._real_advance(conv, errored=False, run_id=rid)
		self._one_step_two(run, conv)

	def test_the_check_twice(self):
		run, conv, rid = self._mk_run(steps=3)
		self._drop_the_hook(run, conv, rid)
		self._settled_long_enough(run, rid)
		self.assertEqual(self._tick()["acted"], 1)
		self._idle(run)
		self.assertEqual(self._tick()["acted"], 0)
		self._one_step_two(run, conv)

	def test_a_run_whose_lock_is_held_is_skipped(self):
		run, conv, rid = self._mk_run()
		self._drop_the_hook(run, conv, rid)
		self._settled_long_enough(run, rid)
		with _redis_lock.redis_lock(f"jarvis_macro_run:{run}", timeout_s=60) as held:
			self.assertTrue(held)
			out = self._tick()
		self.assertEqual((out["acted"], out["busy"]), (0, 1))
		self.assertEqual(self._run(run).current_step, 1)


# --------------------------------------------------------------------------- #
# Another step only lately, and only for an owner who may run unattended
# --------------------------------------------------------------------------- #
class TestSendingAnotherStepUnattended(CheckBase):
	def _lost(self, **kw):
		run, conv, rid = self._mk_run(steps=3, **kw)
		self._drop_the_hook(run, conv, rid)
		return run, conv, rid

	def _assert_interrupted(self, run, conv):
		row = self._run(run)
		self.assertEqual(
			(row.status, row.error),
			("failed", "The run was interrupted and did not finish. Step 1 ran; the rest did not."),
		)
		self.assertEqual(len(self._seeds(conv)), 1, "a step was sent")

	def test_the_dispatch_runs_as_the_owner_and_the_session_is_put_back(self):
		run, conv, rid = self._lost()
		self._settled_long_enough(run, rid, seconds=macro_reconcile.CONTINUE_WITHIN_S - 60)
		seen = []
		real = macros._dispatch_step

		def dispatch(*a, **kw):
			seen.append(frappe.session.user)
			return real(*a, **kw)

		with patch.object(macros, "_dispatch_step", dispatch):
			self._tick()  # asserts the session is Administrator again
		self.assertEqual(seen, [TEST_USER])
		self.assertEqual(self._run(run).current_step, 2)

	def test_a_step_that_ended_over_twenty_minutes_ago_ends_the_run(self):
		self.assertEqual(macro_reconcile.CONTINUE_WITHIN_S, 20 * 60)
		run, conv, rid = self._lost()
		self._settled_long_enough(run, rid, seconds=macro_reconcile.CONTINUE_WITHIN_S + 60)
		self._tick()
		self._assert_interrupted(run, conv)
		self.assertIn("interrupted", self._closing_lines(conv)[0])
		self.assertIn("interrupted", self._notifications()[0])
		self.assertEqual([s.method for s in self._signals()], [SIGNAL + "hook_dropped"])

	def test_the_message_counts_the_steps_that_ran(self):
		run, conv, rid = self._lost()
		self._settled_long_enough(run, rid)
		self._tick()
		second = macros._step_turn_id(run, 1)
		self._drop_the_hook(run, conv, second)
		self._settled_long_enough(run, second, seconds=macro_reconcile.CONTINUE_WITHIN_S + 60)
		self._tick()
		self.assertEqual(
			self._run(run).error,
			"The run was interrupted and did not finish. Steps 1 to 2 ran; the rest did not.",
		)

	def test_an_owner_who_was_disabled(self):
		run, conv, rid = self._lost()
		self._settled_long_enough(run, rid)
		frappe.db.set_value("User", TEST_USER, "enabled", 0)
		self.addCleanup(frappe.db.set_value, "User", TEST_USER, "enabled", 1)
		frappe.db.commit()
		try:
			self._tick()
		finally:
			frappe.db.set_value("User", TEST_USER, "enabled", 1)
			frappe.db.commit()
		self._assert_interrupted(run, conv)

	def test_an_owner_who_lost_jarvis_access(self):
		run, conv, rid = self._lost()
		self._settled_long_enough(run, rid)
		with patch("jarvis.permissions.has_jarvis_access", lambda user=None: user != TEST_USER):
			self._tick()
		self._assert_interrupted(run, conv)

	def test_a_run_owned_by_administrator(self):
		run, conv, rid = self._lost(owner="Administrator")
		self._settled_long_enough(run, rid)
		self._tick()
		self._assert_interrupted(run, conv)

	def test_a_barred_owners_run_still_ends_as_the_hook_would_end_it(self):
		# Ending a run sends nothing: no owner check, no time limit.
		run, conv, rid = self._mk_run(steps=1, owner="Administrator")
		self._drop_the_hook(run, conv, rid)
		self._settled_long_enough(run, rid, seconds=3600)
		self._tick()
		self.assertEqual(self._run(run).status, "completed")


# --------------------------------------------------------------------------- #
# A tick that changes nothing writes nothing; the gate; the limits
# --------------------------------------------------------------------------- #
class TestANoOpTick(CheckBase):
	def test_a_run_whose_step_is_still_going_is_left_byte_identical(self):
		run, conv, rid = self._mk_run()
		self._idle(run, seconds=1800)
		before = self._row(run)
		out = self._tick()
		self.assertEqual((out["candidates"], out["acted"]), (1, 0))
		self.assertEqual(self._row(run), before, "a no-op tick wrote the run row (`modified` included)")
		self.assertEqual(frappe.db.count(LOG, {"method": ["like", "jarvis.chat.macros.reconcil%"]}), 0)

	def test_the_stale_run_sweep_still_closes_a_run_the_check_cannot_see(self):
		# A turn that is `ready` has no deadline: it never ends, so the check waits.
		run, conv, rid = self._mk_run()
		amsg = self._placeholder(conv, streaming=1)
		self._force(
			rid, state="ready", version=2, reserved=1, assistant_message=amsg, ready_at=self._ago(4 * 3600)
		)
		self._idle(run, seconds=macros.STALE_RUN_AFTER_SECONDS + 600)
		before = self._row(run)
		for _ in range(3):
			self.assertEqual(self._tick()["acted"], 0)
		self.assertEqual(self._row(run), before, "the check kept the run looking alive")
		with patch.object(macros, "_stale_run_candidates", lambda cutoff: [run]):
			self.assertEqual(macros.reap_stale_macro_runs(), 1)
		self.assertEqual((self._run(run).status, self._run(run).error), ("failed", macros._STALE_RUN_ERROR))

	def test_a_parked_run_is_not_the_checks(self):
		run, conv, rid = self._mk_run()
		self._age_out(rid)
		self._settled_long_enough(run, rid)
		frappe.db.set_value(RUN, run, "status", "waiting_capacity", update_modified=False)
		self.assertEqual(self._tick()["candidates"], 0)
		self.assertEqual(self._run(run).status, "waiting_capacity")

	def test_a_run_written_under_two_minutes_ago_is_not_looked_at(self):
		run, conv, rid = self._mk_run()
		self._age_out(rid)
		self._ended(rid)
		self._idle(run, seconds=macro_reconcile.IDLE_BEFORE_A_LOOK_S - 30)
		self.assertEqual(self._tick()["candidates"], 0)

	def test_a_run_that_moved_after_the_list_was_read_is_left_alone(self):
		# The list is read before the run's lock is taken: the hook may have sent the
		# next step in between, and the row then says so.
		run, conv, rid = self._mk_run()
		self._age_out(rid)
		self._ended(rid)
		self._idle(run, seconds=20)
		stale = [frappe._dict(name=run, conversation=conv)]
		with patch.object(macro_reconcile, "_candidates", lambda: stale):
			self.assertEqual(self._tick()["acted"], 0)
		self.assertEqual(self._run(run).status, "running")


class TestTheGate(CheckBase):
	def _stuck(self):
		run, conv, rid = self._mk_run()
		self._age_out(rid)
		self._settled_long_enough(run, rid)
		return run

	def test_the_check_does_nothing_outside_pump_mode(self):
		run = self._stuck()
		before = self._row(run)
		for mode in ("legacy", "draining", "phase0", "kill switch", "config off, row pump"):
			with self.subTest(mode=mode):
				self._set_mode(mode)
				self.assertEqual(self._tick()["acted"], 0)
				self.assertEqual(self._row(run), before)
		self._set_mode("pump")
		self.assertEqual(self._tick()["acted"], 1)
		self.assertEqual(self._run(run).status, "failed")

	def test_transport_mode_reads_the_shards_row(self):
		for mode, row in (
			("pump", "pump"),
			("draining", "draining"),
			("kill switch", "legacy"),
			("phase0", "legacy"),
		):
			self._set_mode(mode)
			self.assertEqual(pump.transport_mode(self._target), row)
		# The config mirror is not what it reads.
		self._set_mode("config off, row pump")
		self.assertEqual(pump.transport_mode(self._target), "pump")
		frappe.db.set_value(PUMP, self._target, "transport_mode", "something-else", update_modified=False)
		pump._LIFECYCLE_MODE_CACHE.pop(self._target, None)
		self.assertEqual(pump.transport_mode(self._target), "draining", "not active, and not the kill switch")

	def test_the_off_switch(self):
		run = self._stuck()
		before = self._row(run)
		for value in (1, "1", True, "true"):
			frappe.local.conf[macro_reconcile.OFF_SWITCH] = value
			out = self._tick()
			self.assertEqual((out["candidates"], out["acted"]), (0, 0))
		self.assertEqual(self._row(run), before)
		self.assertEqual(self._signals(), [])
		for value in (0, "0", "", None, False):
			frappe.local.conf[macro_reconcile.OFF_SWITCH] = value
			self.assertFalse(macro_reconcile._switched_off(), repr(value))
		self.assertEqual(self._tick()["acted"], 1)
		self.assertEqual(self._run(run).status, "failed")


class TestTheLimitsOfOneTick(CheckBase):
	def _three_stuck(self):
		"""Three stuck runs, the first the longest idle."""
		runs = []
		for idle in (900, 600, 300):
			run, conv, rid = self._mk_run()
			self._age_out(rid)
			self._ended(rid)
			self._idle(run, seconds=idle)
			runs.append(run)
		return runs

	def _statuses(self, runs):
		return [self._run(run).status for run in runs]

	def test_two_hundred_runs_a_tick_and_two_minutes(self):
		self.assertEqual((macro_reconcile.MAX_RUNS_PER_TICK, macro_reconcile.TICK_BUDGET_S), (200, 120))

	def test_the_cap_takes_the_oldest_and_leaves_the_rest_for_the_next_tick(self):
		runs = self._three_stuck()
		real = frappe.get_all
		asked = []

		def get_all(doctype, *a, **kw):
			if doctype == RUN and kw.get("limit"):
				asked.append((kw["limit"], kw.get("order_by")))
				kw["filters"] = {**kw["filters"], "name": ["in", runs]}
			return real(doctype, *a, **kw)

		with patch.object(macro_reconcile, "MAX_RUNS_PER_TICK", 2), patch.object(frappe, "get_all", get_all):
			self.assertEqual(self._tick()["acted"], 2)
			self.assertEqual(asked, [(2, "modified asc")])
			self.assertEqual(self._statuses(runs), ["failed", "failed", "running"])
			self.assertEqual(self._tick()["acted"], 1)
		self.assertEqual(self._statuses(runs), ["failed"] * 3)

	def test_a_tick_out_of_time_stops_and_the_next_one_goes_on(self):
		runs = self._three_stuck()
		clock = iter([0.0, 1.0, macro_reconcile.TICK_BUDGET_S + 1.0])
		with patch.object(macro_reconcile, "_clock", lambda: next(clock)):
			out = self._tick()
		self.assertEqual((out["acted"], out["out_of_time"]), (1, 1))
		self.assertEqual(self._statuses(runs), ["failed", "running", "running"])
		self.assertEqual(self._tick()["acted"], 2)

	def test_a_run_that_raises_does_not_stop_the_batch_and_is_skipped_for_an_hour(self):
		runs = self._three_stuck()
		real = macro_reconcile._RunCheck._look
		looked = []

		def look(check):
			looked.append(check.name)
			if check.name == runs[0]:
				raise RuntimeError("this run cannot be read")
			return real(check)

		with patch.object(macro_reconcile._RunCheck, "_look", look):
			out = self._tick()
			self.assertEqual((out["acted"], out["raised"]), (2, 1))
			self.assertEqual(self._statuses(runs), ["running", "failed", "failed"])
			self.assertEqual(self._tick()["raised"], 0)
			self.assertEqual(looked.count(runs[0]), 1, "looked at again inside the hour")
		logs = frappe.get_all(LOG, filters={"method": f"jarvis.chat.macros.reconcile_failed: {runs[0]}"})
		self.assertEqual(len(logs), 1, "logged once, not every tick")
		self.assertEqual(macro_reconcile.SKIP_AFTER_A_RAISE_S, 3600)
		# The hour over: looked at again.
		frappe.cache().delete_value(macro_reconcile._key("skip", runs[0]))
		self.assertEqual(self._tick()["acted"], 1)

	def test_a_lost_write_race_is_tried_again_on_the_next_tick(self):
		runs = self._three_stuck()[:1]
		real = macro_reconcile._RunCheck._look
		raised = []

		def look(check):
			if not raised:
				raised.append(1)
				raise frappe.QueryDeadlockError("Record has changed since last read")
			return real(check)

		with patch.object(macro_reconcile._RunCheck, "_look", look):
			out = self._tick()
			self.assertEqual((out["conflicts"], out["raised"]), (1, 0))
			self._tick()
		self.assertEqual(self._statuses(runs), ["failed"])
		self.assertEqual(
			frappe.db.count(LOG, {"method": ["like", "jarvis.chat.macros.reconcile_failed%"]}), 0
		)


# --------------------------------------------------------------------------- #
# The run's chat or macro is gone; nothing was ever sent
# --------------------------------------------------------------------------- #
class TestNothingToRunIn(CheckBase):
	def test_an_archived_chat_stops_the_run_and_disarms_it(self):
		run, conv, rid = self._mk_run(armed=True)
		frappe.db.set_value(CONV, conv, "status", "Archived", update_modified=False)
		self._idle(run)
		self.assertEqual(self._tick()["acted"], 1)
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("stopped", macros._CONVERSATION_GONE_ERROR))
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation")), 0)
		self.assertEqual(self._state(rid), "cancelled", "the step that had not started was withdrawn")
		self.assertEqual(self._signals(), [])

	def test_a_cleared_chat_stops_the_run(self):
		run, conv, rid = self._mk_run()
		frappe.db.set_value(RUN, run, "conversation", None, update_modified=False)
		self._idle(run)
		self.assertEqual(self._tick()["acted"], 1)
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("stopped", macros._CONVERSATION_GONE_ERROR))

	def test_a_deleted_macro_stops_the_run(self):
		run, conv, rid = self._mk_run()
		frappe.db.delete(MACRO, {"name": frappe.db.get_value(RUN, run, "macro")})
		self._idle(run)
		self.assertEqual(self._tick()["acted"], 1)
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("stopped", macros._MACRO_DELETED_ERROR))
		self.assertEqual(self._state(rid), "cancelled")


class TestAStepThatWasNeverSent(CheckBase):
	"""The dispatcher of the first step died before its transaction committed: no
	message, no turn, and nothing delivers its trigger again."""

	TEN = macro_reconcile.NOT_STARTED_AFTER_S

	def _unsent(self, **kw):
		run, conv, _ = self._mk_run(send=False, **kw)
		self.assertEqual((self._seeds(conv), frappe.db.count(TURN, {"conversation": conv})), ([], 0))
		return run, conv

	def test_it_is_left_alone_for_ten_minutes(self):
		self.assertEqual(self.TEN, 600)
		run, conv = self._unsent()
		self._idle(run, seconds=self.TEN - 60)
		before = self._row(run)
		self.assertEqual(self._tick()["acted"], 0)
		self.assertEqual(self._row(run), before)

	def test_it_is_sent_once_as_the_owner(self):
		run, conv = self._unsent()
		self._idle(run, seconds=self.TEN + 60)
		seen = []
		real = macros._dispatch_step

		def dispatch(*a, **kw):
			seen.append(frappe.session.user)
			return real(*a, **kw)

		with patch.object(macros, "_dispatch_step", dispatch):
			self.assertEqual(self._tick()["acted"], 1)
		self.assertEqual(seen, [TEST_USER])
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 1))
		self.assertEqual(self._state(macros._step_turn_id(run, 0)), "queued")
		self.assertEqual(len(self._seeds(conv)), 1)
		self.assertEqual([s.method for s in self._signals()], [SIGNAL + "dispatch_never_committed"])

	def _killed_tick(self, run):
		def dies(*a, **kw):
			raise _Killed()

		with patch.object(macros, "_dispatch_step", dies), self.assertRaises(_Killed):
			self._tick()
		frappe.db.rollback()
		frappe.set_user(TEST_USER)
		frappe.cache().delete_value(f"{_redis_lock.LOCK_PREFIX}jarvis_macro_run:{run}")  # the dead holder's

	def test_a_second_failure_ends_the_run(self):
		run, conv = self._unsent()
		self._idle(run, seconds=self.TEN + 60)
		self._killed_tick(run)
		self.assertEqual(self._run(run).status, "running")
		with patch.object(macros, "_dispatch_step") as dispatch:
			self.assertEqual(self._tick()["acted"], 1)
		dispatch.assert_not_called()
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("failed", "Step 1 could not be started, so nothing ran."))
		self.assertEqual(self._seeds(conv), [])
		self.assertIn("could not be started", self._notifications()[0])
		self.assertEqual([s.method for s in self._signals()], [SIGNAL + "dispatch_never_committed"])

	def test_a_lost_marker_costs_one_more_attempt_and_the_window_ends_it(self):
		run, conv = self._unsent()
		self._idle(run, seconds=self.TEN + 60)
		self._killed_tick(run)
		frappe.cache().delete_value(macro_reconcile._key("resent", run))
		self._killed_tick(run)  # sent again: the marker was all that said "once"
		frappe.cache().delete_value(macro_reconcile._key("resent", run))
		# A dead attempt wrote nothing, so the run's last write kept ageing.
		self._idle(run, seconds=macro_reconcile.CONTINUE_WITHIN_S + 60)
		with patch.object(macros, "_dispatch_step") as dispatch:
			self._tick()
		dispatch.assert_not_called()
		self.assertEqual(self._run(run).status, "failed")

	def test_a_dispatch_that_raises_ends_the_run_at_once(self):
		run, conv = self._unsent()
		self._idle(run, seconds=self.TEN + 60)
		with patch.object(macros, "_send_step", side_effect=RuntimeError("no gateway")):
			self._tick()
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("failed", "Step 1 could not be started, so nothing ran."))
		frappe.db.delete(LOG, {"method": ["like", "jarvis macro step dispatch failed%"]})

	def test_found_after_twenty_minutes_nothing_is_sent(self):
		run, conv = self._unsent()
		self._idle(run, seconds=macro_reconcile.CONTINUE_WITHIN_S + 60)
		with patch.object(macros, "_dispatch_step") as dispatch:
			self._tick()
		dispatch.assert_not_called()
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("failed", "The run could not start. Run it again."))
		self.assertIn("could not start", self._closing_lines(conv)[0])
		self.assertIn("could not start", self._notifications()[0])
		self.assertEqual([s.method for s in self._signals()], [SIGNAL + "no_step_sent"])

	def test_nothing_is_sent_for_an_administrator_owned_run(self):
		run, conv = self._unsent(owner="Administrator")
		self._idle(run, seconds=self.TEN + 60)
		with patch.object(macros, "_dispatch_step") as dispatch:
			self._tick()
		dispatch.assert_not_called()
		self.assertEqual(self._run(run).error, "The run could not start. Run it again.")

	def test_a_step_message_with_no_turn_is_not_taken_for_unsent(self):
		# A step sent where no Turn rows are written (before a cutover to the pump):
		# its worker may be running it. Not known to be dead, so not sent again.
		run, conv = self._unsent()
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": 2,
				"role": "user",
				"content": "step 1",
				"origin": "macro",
			}
		)
		doc.flags.ignore_permissions = doc.flags.jarvis_server_write = True
		doc.insert()
		frappe.db.commit()
		self._idle(run, seconds=self.TEN + 60)
		before = self._row(run)
		with patch.object(macros, "_dispatch_step") as dispatch:
			self.assertEqual(self._tick()["acted"], 0)
		dispatch.assert_not_called()
		self.assertEqual(self._row(run), before)
