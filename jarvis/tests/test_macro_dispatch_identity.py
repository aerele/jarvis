"""A macro step is sent once, whoever sends it and however often they try.

From the 2026-10-01 macros review: the run's cursor was written after the step's turn
existed, so anything between the two (a raise, a lapsed run lock, a second healer for
a message with no turn) sent the same step again (issue #422). The step's turn now has
a fixed id, ``sha1("<run>:<index>")[:12]``, every dispatcher goes through one
function that lands on it, and where Turn rows are written the step's message and its
turn are written in ONE transaction by the accept gate: a second dispatcher, a full
site or a dispatcher that dies leaves no step message without its turn.

These tests drive the REAL dispatch (``api._enqueue_turn`` and the admission gate, on
a shard of their own with the pump's wake and the RQ enqueue stubbed), not a stand-in
for it. A race runs its other side on a second real database connection.
"""

from __future__ import annotations

import hashlib
import inspect
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from jarvis.chat import admission, macros, pump, stale_scan
from jarvis.chat import api as chat_api
from jarvis.chat import turn_state as ts
from jarvis.tests import test_macro_run_outcome as outcome
from jarvis.tests._pending_action_helpers import ensure_user
from jarvis.tests.race_harness import other_connection, snapshot_isolation_on

CONV = outcome.CONV
MACRO = outcome.MACRO
RUN = outcome.RUN
MSG = outcome.MSG
TURN = outcome.TURN
OWNER = outcome.OWNER
PUMP = "Jarvis Relay Pump"
BYSTANDER = "macro-identity-bystander@example.com"
TARGET_PREFIX = "mcid_"

# How a site dispatches a turn: ``jarvis_pump_enabled`` in site config, the Phase-0
# admission flag, and the shard row's own ``transport_mode`` (which the accept gate
# reads under its lock). ``None`` = the key is absent.
MODES = {
	"pump": (None, None, "pump"),
	"legacy": (0, None, "legacy"),
	"draining": ("draining", None, "draining"),
	"phase0": (0, 1, "legacy"),
	# The row was flipped to `legacy` and the config mirror still says pump.
	"kill switch": (None, None, "legacy"),
}


def step_turn_id(run: str, index: int) -> str:
	"""The id the design gives the turn of step ``index`` (0-based) of ``run``. Spelled
	out here, not imported: the tests pin the formula."""
	return hashlib.sha1(f"{run}:{index}".encode()).hexdigest()[:12]


class _Killed(BaseException):
	"""A worker process dying: not an ``Exception``, so nothing on the way out handles
	it (no compensation, no log), like SIGKILL or an OOM."""


def _age(message: str, seconds: int = 300) -> None:
	"""Backdate a message so it sits inside the orphan sweep's window."""
	old = now_datetime() - timedelta(seconds=seconds)
	frappe.db.sql(f"UPDATE `tab{MSG}` SET creation=%s WHERE name=%s", (old, message))
	frappe.db.commit()


class IdentityBase(outcome.MacroRunOutcomeBase):
	"""Macro runs on a shard of this test's own. The accept gate is the real one (it
	reads the shard row), a turn the pump accepts stays ``queued`` because nothing
	wakes a pump, and a legacy job is recorded, not enqueued. Both connections run
	with the snapshot-isolation check on."""

	def setUp(self):
		super().setUp()
		self._target = f"{TARGET_PREFIX}{frappe.generate_hash(length=10)}"
		ts._ensure_control_row(self._target)
		self._conf = {k: frappe.local.conf.get(k) for k in ("jarvis_pump_enabled", admission.FLAG)}
		self._set_mode("pump")
		# As production runs (MariaDB 11.6.2+): a write on a row another connection
		# moved since this transaction's snapshot fails with 1020 (``jarvis.chat.txn``).
		strict = snapshot_isolation_on()
		strict.__enter__()
		self.addCleanup(strict.__exit__, None, None, None)
		self._jobs: list[dict] = []
		real_enqueue = frappe.enqueue

		def enqueue(*args, **kw):
			# A turn's worker job is recorded, never queued: nothing must run it.
			if kw.get("method") == "jarvis.chat.worker.run_agent_turn":
				self._jobs.append(kw)
				return None
			return real_enqueue(*args, **kw)

		for target in (
			patch.object(pump, "ensure_pump", lambda *a, **k: {"enqueued": False}),
			patch.object(pump, "lpush_wake", lambda *a, **k: None),
			patch.object(admission, "relay_target_id", lambda conversation=None: self._target),
			patch.object(frappe, "enqueue", enqueue),
			# What follows a settled or cancelled turn is site-wide; one shared site.
			patch.object(admission, "promote_next"),
			patch.object(admission, "_try_llm_switch_apply"),
			patch.object(admission, "_legacy_streaming_count", return_value=0),
		):
			target.start()
			self.addCleanup(target.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for key, value in self._conf.items():
			if value is None:
				frappe.local.conf.pop(key, None)
			else:
				frappe.local.conf[key] = value
		frappe.db.delete(TURN, {"relay_target_id": self._target})
		frappe.db.delete(PUMP, {"relay_target_id": ["like", f"{TARGET_PREFIX}%"]})
		frappe.db.delete("Error Log", {"method": ["like", "jarvis.chat.macros.step_%"]})
		frappe.db.commit()
		pump._LIFECYCLE_MODE_CACHE.pop(self._target, None)
		super().tearDown()

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

	# --- fixtures -------------------------------------------------------------- #

	def _mk_run(self, **kw):
		"""As the base's, on a chat that already has its gateway session (the dispatch
		would otherwise open a socket to mint one)."""
		run, conv, macro = super()._mk_run(**kw)
		frappe.db.set_value(CONV, conv, "session_key", f"sk-{conv}", update_modified=False)
		frappe.db.commit()
		return run, conv, macro

	def _msg(self, conv, role, **kw):
		# The base counts seq itself; here the real dispatch writes messages too.
		self._seq[conv] = (
			frappe.db.sql(f"SELECT MAX(seq) FROM `tab{MSG}` WHERE conversation=%s", conv)[0][0] or 0
		)
		return super()._msg(conv, role, **kw)

	def _as(self, who):
		frappe.set_user("Administrator")
		frappe.set_user(who if who == "Administrator" else ensure_user(who))

	def _docs(self, run, macro):
		return frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)

	def _seeds(self, conv):
		"""The chat's step messages, oldest first."""
		return [m.name for m in macros._macro_seeds(conv)]

	def _prompts(self, conv):
		return [frappe.db.get_value(MSG, seed, "content") for seed in self._seeds(conv)]

	def _turns(self, conv, *, besides=()):
		"""Every Turn in the chat, as ``(name, seed_message)``, without ``besides``."""
		rows = frappe.get_all(
			TURN, filters={"conversation": conv}, fields=["name", "seed_message"], order_by="creation asc"
		)
		return [(r.name, r.seed_message) for r in rows if r.name not in besides]

	def _names(self, conv, besides=()):
		return [name for name, _ in self._turns(conv, besides=besides)]

	def _finish(self, conv, turn, *, state="done", reply="done", error="", deliver=True):
		"""End a step's turn the way the pump leaves it (a reply row, the Turn in its
		final state) and deliver its end to the engine, as finalize does."""
		message = self._msg(conv, "assistant", content=reply)
		frappe.db.set_value(
			TURN, turn, {"state": state, "assistant_message": message, "error": error}, update_modified=False
		)
		frappe.db.commit()
		if deliver:
			macros.advance_after_turn(conv, errored=state != "done", run_id=turn)
		return message

	@contextmanager
	def _killed_at_the_turn_insert(self):
		"""The process dies as the accept gate inserts the step's turn, after it has
		written the step's message: SIGKILL, so nothing on the way out runs, and the
		server rolls the dead connection's open transaction back."""
		real = Document.insert

		def insert(doc, *args, **kw):
			if doc.doctype == TURN:
				raise _Killed()
			return real(doc, *args, **kw)

		with patch.object(Document, "insert", insert), self.assertRaises(_Killed):
			yield
		frappe.db.rollback()

	@contextmanager
	def _locks_always_granted(self):
		"""A run lock that lapsed under a slow holder: both dispatchers get in."""

		@contextmanager
		def granted(*a, **kw):
			yield True

		with patch("jarvis._redis_lock.redis_lock", granted):
			yield

	def _elsewhere(self, fn):
		"""Run ``fn`` to completion on a second real connection: another worker."""
		mine, user = frappe.local.db, frappe.session.user
		with other_connection() as other:
			frappe.local.db = other
			other.sql("SET SESSION innodb_snapshot_isolation=ON")  # as the first connection
			try:
				fn()
				other.commit()
			finally:
				frappe.local.db = mine
				frappe.set_user(user)

	@contextmanager
	def _while_the_first_is_at_the_accept_gate(self, second):
		"""Run ``second`` to completion, on a second real connection, at the moment the
		first dispatcher has decided the step is not out and is about to ask the accept
		gate for its turn. Then let the first carry on."""
		real = admission.accept_or_queue
		ran = []

		def gate(**kw):
			if not ran:
				ran.append(1)
				self._elsewhere(second)
			return real(**kw)

		with patch.object(admission, "accept_or_queue", side_effect=gate):
			yield
		self.assertEqual(ran, [1], "premise: the first dispatcher reached the accept gate")

	@contextmanager
	def _paused_after_its_look(self, second):
		"""The first dispatcher looked, found the step not out, and then stalled (its
		run lock lapsed under it). ``second`` runs to completion on a second real
		connection before it carries on."""
		real, ran = macros._step_prompt, []

		def prompt(*args, **kw):
			if not ran:
				ran.append(1)
				self._elsewhere(second)
			return real(*args, **kw)

		with patch.object(macros, "_step_prompt", side_effect=prompt):
			yield
		self.assertEqual(ran, [1], "premise: the first dispatcher stalled after its look")

	def _assert_step_sent_once(self, run, conv, index, *, besides, seeds):
		"""One turn for step ``index``, under its fixed id; ``seeds`` step messages in
		the chat; the cursor on the step after."""
		turns = self._turns(conv, besides=besides)
		self.assertEqual(len(turns), 1, f"step {index + 1} was sent {len(turns)} times: {turns}")
		self.assertEqual(turns[0][0], step_turn_id(run, index), "the turn does not carry the step's fixed id")
		self.assertEqual(len(self._seeds(conv)), seeds, "a step message with no turn of its own")
		self.assertEqual(turns[0][1], self._seeds(conv)[-1], "the turn is not on the step's message")
		row = self._run(run)
		self.assertEqual((row.status, row.current_step), ("running", index + 1))

	def _sweep(self):
		stale_scan._sweep_orphan_turns(now_datetime())

	def _every_message_has_its_turn(self, conv):
		"""Nothing a step left for the orphan sweep: every step message has a turn."""
		lone = [m for m in self._seeds(conv) if not frappe.db.exists(TURN, {"seed_message": m})]
		self.assertEqual(lone, [], "a step message with no turn")


# --------------------------------------------------------------------------- #
# Two dispatchers at once
# --------------------------------------------------------------------------- #
class TestTwoDispatchersAtOnce(IdentityBase):
	"""The run lock is the only thing that kept two dispatchers apart, and it lapses
	(60 s) under a slow holder. Each pairing: the first is about to ask for the step's
	turn, the second runs start to finish, the first carries on."""

	def test_the_same_step_end_delivered_twice(self):
		# The finalize effect that calls the hook is retried.
		for who in ("Administrator", BYSTANDER):
			run, conv, _ = self._mk_run(steps=3, at_step=1, tag=f"twice-{who[:5]}")
			step_one, _, _ = self._turn(conv)

			def again(conv=conv, step_one=step_one):
				macros.advance_after_turn(conv, errored=False, run_id=step_one)

			self._as(who)
			with self._locks_always_granted(), self._while_the_first_is_at_the_accept_gate(again):
				macros.advance_after_turn(conv, errored=False, run_id=step_one)
			frappe.set_user("Administrator")
			self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_the_capacity_resume_and_the_step_end(self):
		# The resume cron has flipped the parked run back to `running` and is sending
		# its step when the previous step's end is delivered again.
		for who in ("Administrator", BYSTANDER):
			run, conv, _ = self._mk_run(steps=3, at_step=1, tag=f"resume-{who[:5]}")
			step_one, _, _ = self._turn(conv)
			frappe.db.set_value(RUN, run, "status", "waiting_capacity")
			frappe.db.commit()

			def step_end(conv=conv, step_one=step_one):
				macros.advance_after_turn(conv, errored=False, run_id=step_one)

			self._as(who)
			with self._locks_always_granted(), self._while_the_first_is_at_the_accept_gate(step_end):
				macros.resume_waiting_capacity_runs()
			frappe.set_user("Administrator")
			self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_second_dispatcher_at_a_full_queue_is_told_duplicate_not_overloaded(self):
		# The second dispatcher's turn fills the queue; the first then asks for the same
		# turn. Told "the site is full" it parked a run whose step was out.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)

		def again():
			macros.advance_after_turn(conv, errored=False, run_id=step_one)

		with (
			patch.object(admission, "MAX_QUEUE_DEPTH", 1),
			self._locks_always_granted(),
			self._while_the_first_is_at_the_accept_gate(again),
		):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_dispatcher_that_lost_and_then_raised_leaves_no_copy_of_the_step(self):
		# The loser failed at the accept gate (a lock wait that timed out). With the
		# message committed before the gate, its message stayed: the newest step
		# message, with no turn, and the engine refused the real step's end.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		real, calls = admission.accept_or_queue, []

		def gate(**kw):
			calls.append(1)
			if len(calls) == 1:
				self._elsewhere(lambda: macros.advance_after_turn(conv, errored=False, run_id=step_one))
				raise RuntimeError("lock wait timeout")
			return real(**kw)

		with (
			self._locks_always_granted(),
			patch.object(admission, "accept_or_queue", side_effect=gate),
			patch("frappe.log_error"),
		):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self.assertEqual(len(calls), 2, "premise: both dispatchers reached the gate")
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_dispatcher_told_the_site_is_full_does_not_park_a_run_whose_step_is_out(self):
		# One dispatcher is told "full"; a slot frees and the other gets the step's
		# turn; the first then parks the run. A parked run ignores its step's end.
		run, conv, macro = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		real = chat_api._enqueue_turn

		def full(*args, **kw):
			with patch.object(chat_api, "_enqueue_turn", real):
				self._elsewhere(lambda: macros._dispatch_step(*self._docs(run, macro), 1))
			return {"ok": False, "overloaded": True, "reason": "busy"}

		with self._locks_always_granted(), patch.object(chat_api, "_enqueue_turn", side_effect=full):
			self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_ALREADY_OUT)
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_dispatcher_that_lost_writes_nothing(self):
		# Each dispatcher asks for the turn; the second to get the gate is told
		# `duplicate`. It used to have written its own copy of the step's message first.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		real, ran = chat_api._enqueue_turn, []

		def enqueue(*args, **kw):
			if not ran:
				ran.append(1)
				self._elsewhere(lambda: macros.advance_after_turn(conv, errored=False, run_id=step_one))
			return real(*args, **kw)

		with (
			self._locks_always_granted(),
			patch.object(chat_api, "_enqueue_turn", side_effect=enqueue),
			patch.object(chat_api, "_delete_enqueue_seed") as cleanup,
		):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self.assertEqual(ran, [1])
		cleanup.assert_not_called()  # nothing to clean up: nothing was written
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_stop_while_the_step_is_really_being_sent_withdraws_it(self):
		# The fence lives in the one dispatch function now; this is it on the real path.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		with (
			patch.object(macros, "_STOP_LOCK_BLOCK_S", 0.05),
			self._while_the_first_is_at_the_accept_gate(lambda: macros._stop_run(run, reason="", by=OWNER)),
		):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		row = self._run(run)
		self.assertEqual((row.status, row.current_step), ("stopped", 1))
		self.assertEqual(frappe.db.get_value(TURN, step_turn_id(run, 1), "state"), "cancelled")


# --------------------------------------------------------------------------- #
# The review's race probes, as tests
# --------------------------------------------------------------------------- #
class TestTheReviewsRaceProbes(IdentityBase):
	"""Each one drove a step out twice, or a run into a stall until the 3 h reaper,
	through the real code. The other side always runs on a second real connection."""

	def test_p1_a_loser_killed_inside_the_gate_leaves_nothing_for_the_sweep(self):
		# The loser had written its copy of the step's message and died before the
		# gate: a turnless copy that the orphan sweep later ran, the step twice.
		run, conv, _ = self._mk_run(steps=3, at_step=1, armed=True)
		step_one, _, _ = self._turn(conv)
		with self._locks_always_granted(), self._killed_at_the_turn_insert():
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self.assertEqual(len(self._seeds(conv)), 1, "the dead loser's message survived it")
		self._elsewhere(lambda: macros.advance_after_turn(conv, errored=False, run_id=step_one))
		frappe.db.commit()  # this connection's next read sees the other's work
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)
		for seed in self._seeds(conv):
			_age(seed)
		self._sweep()
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)
		self._every_message_has_its_turn(conv)

	def test_p2_a_stale_dispatcher_does_not_unpark_a_run_parked_for_the_next_step(self):
		# A stalled while B sent step 2, step 2 ended, and C was told the site was full
		# and parked the run at step 3. A then found step 2 out and un-parked the run:
		# `running`, with nothing coming, until the reaper.
		run, conv, macro = self._mk_run(steps=4, at_step=1)
		step_one, _, _ = self._turn(conv)
		two = step_turn_id(run, 1)

		def meanwhile():
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
			with patch.object(admission, "MAX_QUEUE_DEPTH", 0):
				self._finish(conv, two)

		with self._locks_always_granted(), self._paused_after_its_look(meanwhile):
			self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_ALREADY_OUT)
		row = self._run(run)
		self.assertEqual((row.status, row.current_step), ("waiting_capacity", 2), "un-parked by a stale step")
		macros.resume_waiting_capacity_runs()
		self.assertEqual(self._names(conv, (step_one,)), [two, step_turn_id(run, 2)])
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 3))

	def test_p3_a_dispatcher_that_wakes_after_the_run_moved_on_writes_nothing(self):
		# A stalled after its look while step 2 was sent, ended, and step 3 was sent.
		for killed in (False, True):
			run, conv, macro = self._mk_run(steps=4, at_step=1, tag=f"p3-{killed}")
			step_one, _, _ = self._turn(conv)
			two, three = step_turn_id(run, 1), step_turn_id(run, 2)

			def meanwhile(conv=conv, step_one=step_one, two=two):
				macros.advance_after_turn(conv, errored=False, run_id=step_one)
				self._finish(conv, two)

			with self._locks_always_granted(), self._paused_after_its_look(meanwhile):
				if killed:
					# Dies on its way to the gate: whatever it could have written by then.
					mine, real = frappe.local.db, admission.accept_or_queue

					def gate(mine=mine, real=real, **kw):
						if frappe.local.db is mine:
							raise _Killed()
						return real(**kw)

					with (
						patch.object(admission, "accept_or_queue", side_effect=gate),
						self.assertRaises(_Killed),
					):
						macros._dispatch_step(*self._docs(run, macro), 1)
					frappe.db.rollback()
				else:
					self.assertEqual(
						macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_ALREADY_OUT
					)
			self.assertEqual(self._names(conv, (step_one,)), [two, three], killed)
			self.assertEqual(self._prompts(conv), ["prompt", "step 2", "step 3"], killed)
			self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 3))
			for seed in self._seeds(conv):
				_age(seed)
			self._sweep()
			self.assertEqual(self._names(conv, (step_one,)), [two, three], f"the sweep ran a copy ({killed})")

	def test_p4_a_deadlock_or_a_lock_wait_timeout_at_the_gate_is_retried_once(self):
		for error in (frappe.QueryDeadlockError("1213"), frappe.QueryTimeoutError("1205")):
			run, conv, _ = self._mk_run(steps=3, at_step=1, tag=f"p4-{type(error).__name__}")
			step_one, _, _ = self._turn(conv)
			real, calls = admission.accept_or_queue, []

			def gate(error=error, **kw):
				calls.append(1)
				if len(calls) == 1:
					raise error
				return real(**kw)

			with (
				patch.object(admission, "accept_or_queue", side_effect=gate),
				patch("frappe.log_error") as log,
			):
				macros.advance_after_turn(conv, errored=False, run_id=step_one)
			self.assertEqual(len(calls), 2)
			self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)
			self.assertIn(f"step_send_retried: {run}", log.call_args_list[0].kwargs["title"])
			calls.clear()

	def test_p4_a_second_failure_ends_the_run_and_leaves_no_message(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		with (
			patch.object(admission, "accept_or_queue", side_effect=frappe.QueryDeadlockError("1213")) as gate,
			patch("frappe.log_error"),
		):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self.assertEqual(gate.call_count, 2, "retried more than once, or not at all")
		row = self._run(run)
		self.assertEqual(row.status, "failed")
		self.assertIn("Step 2 could not be started", row.error)
		self.assertEqual(len(self._seeds(conv)), 1)

	def test_p4_on_the_legacy_path_a_deadlock_is_not_retried(self):
		# There the message is committed before the job is asked for: a retry would
		# write a second one.
		self._set_mode("legacy")
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, seed, _ = self._turn(conv)
		with (
			patch.object(chat_api, "_dispatch_turn", side_effect=frappe.QueryDeadlockError("1213")) as send,
			patch("frappe.log_error"),
		):
			macros.advance_after_turn(conv, errored=False, run_id=step_one, seed_message=seed)
		self.assertEqual(send.call_count, 1)
		self.assertEqual(self._run(run).status, "failed")

	def test_p5_a_typed_message_after_a_killed_dispatch_is_healed_alone(self):
		# Step 2's message was left with no turn, then the owner typed: the sweep healed
		# the step message the ordinary way, and step 2's end sent step 2 again.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		with self._killed_at_the_turn_insert():
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		typed = self._msg(conv, "user", origin="human", content="and another thing")
		frappe.db.commit()
		_age(typed)
		self._sweep()
		self.assertEqual(len(frappe.get_all(TURN, filters={"seed_message": typed})), 1)
		self.assertEqual(self._names(conv, (step_one,)), [frappe.db.get_value(TURN, {"seed_message": typed})])
		self.assertEqual(len(self._seeds(conv)), 1)
		macros.advance_after_turn(conv, errored=False, run_id=step_one)  # the end, delivered again
		self.assertEqual(self._prompts(conv), ["prompt", "step 2"])
		self.assertTrue(frappe.db.exists(TURN, step_turn_id(run, 1)))

	def test_p6_a_step_that_ended_while_its_run_was_parked_is_carried_on_by_the_resume(self):
		# A second dispatcher parked the run while the first sent step 2, and the first
		# died before it could un-park. Step 2 then ended, ignored (the run was parked).
		# The resume un-parked, found step 2 out, and stopped there: `running`, nothing
		# coming, until the reaper.
		run, conv, macro = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		two = step_turn_id(run, 1)
		with (
			patch.object(macros, "_fenced_by_an_ended_run", side_effect=_Killed()),
			self.assertRaises(_Killed),
		):
			macros._dispatch_step(*self._docs(run, macro), 1)
		frappe.db.rollback()
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")  # the loser's park
		frappe.db.commit()
		self._finish(conv, two)
		self.assertEqual(self._names(conv, (step_one,)), [two], "premise: a parked run takes no step end")
		macros.resume_waiting_capacity_runs()
		self.assertEqual(self._names(conv, (step_one,)), [two, step_turn_id(run, 2)])
		self.assertEqual(self._prompts(conv), ["prompt", "step 2", "step 3"])
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 3))
		self._finish(conv, step_turn_id(run, 2))
		self.assertEqual(self._run(run).status, "completed")

	def test_p6_the_last_step_ended_while_parked_ends_the_run(self):
		run, conv, macro = self._mk_run(steps=2, at_step=1)
		step_one, _, _ = self._turn(conv)
		with (
			patch.object(macros, "_fenced_by_an_ended_run", side_effect=_Killed()),
			self.assertRaises(_Killed),
		):
			macros._dispatch_step(*self._docs(run, macro), 1)
		frappe.db.rollback()
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		frappe.db.commit()
		self._finish(conv, step_turn_id(run, 1), state="errored", error="The model is overloaded.")
		macros.resume_waiting_capacity_runs()
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("failed", "Step 2 failed: The model is overloaded."))

	def test_p7_a_dispatcher_killed_before_its_turn_twice_leaves_nothing_behind(self):
		# The second strike that ended such a run is gone with the sweep's hand-off: a
		# dispatcher killed before the turn now leaves no message, so there is nothing
		# for a strike to count. The step goes out when its end is delivered again.
		run, conv, _ = self._mk_run(steps=3, at_step=1, armed=True)
		step_one, _, _ = self._turn(conv)
		for _ in range(2):
			with self._killed_at_the_turn_insert():
				macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self.assertEqual(len(self._seeds(conv)), 1)
		self._sweep()
		self.assertEqual(self._turns(conv, besides=(step_one,)), [])
		row = self._run(run)
		self.assertEqual((row.status, row.current_step), ("running", 1))
		macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)


# --------------------------------------------------------------------------- #
# The accept gate and the id
# --------------------------------------------------------------------------- #
class TestTheAcceptGateKnowsATurnItAlreadyAccepted(IdentityBase):
	def _accept(self, conv, run_id, seed, dispatch=None):
		return admission.accept_or_queue(
			conversation=conv, run_id=run_id, seed_message=seed, dispatch=dispatch or (lambda: None)
		)

	def _seed(self):
		_, conv, _ = self._mk_run()
		seed = self._msg(conv, "user", origin="macro", content="step")
		frappe.db.commit()
		return conv, seed

	def test_a_turn_that_exists_is_a_duplicate_even_at_a_full_queue(self):
		conv, seed = self._seed()
		self.assertFalse(self._accept(conv, "ident_full_1", seed).get("duplicate"))
		with patch.object(admission, "MAX_QUEUE_DEPTH", 1):
			again = self._accept(conv, "ident_full_1", seed)
			another = self._accept(conv, "ident_full_2", seed)
		self.assertTrue(another.get("overloaded"), "premise: the queue is full")
		self.assertEqual((again.get("duplicate"), again.get("overloaded")), (True, None))
		self.assertEqual(frappe.db.count(TURN, {"conversation": conv}), 1)

	def test_a_duplicate_just_after_the_kill_switch_enqueues_no_second_job(self):
		# The shard row says `legacy`: a turn the gate has never seen falls back to a
		# legacy job. One it accepted before the flip must not get a second runner.
		conv, seed = self._seed()
		self._accept(conv, "ident_kill_1", seed)
		self._set_mode("kill switch")
		ran = []
		again = self._accept(conv, "ident_kill_1", seed, dispatch=lambda: ran.append(1))
		self.assertTrue(again.get("duplicate"))
		self.assertEqual(ran, [])
		fresh = self._accept(conv, "ident_kill_2", seed, dispatch=lambda: ran.append(1))
		self.assertEqual((fresh.get("legacy_fallback"), ran), (True, [1]), "premise: the fallback is live")

	def test_enqueue_turn_takes_the_engines_id_and_a_duplicate_writes_nothing(self):
		_, conv, _ = self._mk_run()
		first = chat_api._enqueue_turn(conv, "step", origin="macro", run_id="ident_enq_01")
		self.assertEqual(first["run_id"], "ident_enq_01")
		self.assertNotIn("duplicate", first)
		self.assertEqual(frappe.db.get_value(TURN, "ident_enq_01", "seed_message"), first["message_id"])
		again = chat_api._enqueue_turn(conv, "step", origin="macro", run_id="ident_enq_01")
		self.assertEqual(again, {"run_id": "ident_enq_01", "message_id": None, "duplicate": True})
		self.assertEqual(frappe.db.count(MSG, {"conversation": conv, "role": "user"}), 1)
		self.assertEqual(frappe.db.count(TURN, {"conversation": conv}), 1)

	def test_a_full_site_writes_no_message(self):
		_, conv, _ = self._mk_run()
		with (
			patch.object(admission, "MAX_QUEUE_DEPTH", 0),
			patch.object(chat_api, "_delete_enqueue_seed") as rm,
		):
			out = chat_api._enqueue_turn(conv, "step", origin="macro", run_id="ident_full_0")
		self.assertTrue(out.get("overloaded"))
		rm.assert_not_called()  # nothing to clean up
		self.assertEqual(frappe.db.count(MSG, {"conversation": conv, "role": "user"}), 0)

	def test_a_process_killed_between_the_message_and_the_turn_leaves_neither(self):
		_, conv, _ = self._mk_run()
		with self._killed_at_the_turn_insert():
			chat_api._enqueue_turn(conv, "step", origin="macro", run_id="ident_kill_00")
		self.assertEqual(frappe.db.count(MSG, {"conversation": conv, "role": "user"}), 0)
		self.assertFalse(frappe.db.exists(TURN, "ident_kill_00"))

	def test_the_message_is_the_chats_owners_with_the_callers_stamp(self):
		# A scheduled run dispatches as Administrator; the worker runs the turn as the
		# message's owner.
		_, conv, _ = self._mk_run()
		frappe.set_user("Administrator")
		out = chat_api._enqueue_turn(conv, "step one", origin="macro", run_id="ident_owner_1")
		row = frappe.db.get_value(
			MSG, out["message_id"], ["owner", "origin", "hidden", "seq", "role"], as_dict=True
		)
		self.assertEqual((row.owner, row.origin, row.hidden, row.role), (OWNER, "macro", 0, "user"))
		self.assertEqual(
			row.seq, frappe.db.sql(f"SELECT MAX(seq) FROM `tab{MSG}` WHERE conversation=%s", conv)[0][0]
		)

	def test_under_the_kill_switch_the_message_is_committed_before_the_job(self):
		# The gate falls back to a legacy job (no Turn row). A worker must never start on
		# a message it cannot read yet.
		_, conv, _ = self._mk_run()
		self._set_mode("kill switch")
		seen = []
		real = frappe.enqueue

		def enqueue(*args, **kw):
			if kw.get("method") == "jarvis.chat.worker.run_agent_turn":
				self._elsewhere(lambda: seen.append(frappe.db.exists(MSG, kw["message_id"])))
			return real(*args, **kw)

		with patch.object(frappe, "enqueue", enqueue):
			out = chat_api._enqueue_turn(conv, "step", origin="macro", run_id="ident_kill_03")
		self.assertTrue(out["message_id"])
		self.assertEqual([j["message_id"] for j in self._jobs], [out["message_id"]])
		self.assertEqual(seen, [out["message_id"]], "the job was queued before its message was committed")
		self.assertFalse(frappe.db.exists(TURN, "ident_kill_03"))

	def test_everyone_else_still_gets_a_random_id_and_writes_its_own_message(self):
		_, conv, _ = self._mk_run()
		with patch.object(admission, "accept_or_queue", wraps=admission.accept_or_queue) as gate:
			one = chat_api._enqueue_turn(conv, "a", origin="continuation", hidden=True, exempt_overload=True)
			two = chat_api._enqueue_turn(conv, "a", origin="continuation", hidden=True, exempt_overload=True)
		self.assertNotEqual(one["run_id"], two["run_id"])
		self.assertRegex(one["run_id"], r"^[0-9a-f]{12}$")
		self.assertEqual(
			[c.kwargs["seed_message"] for c in gate.call_args_list], [one["message_id"], two["message_id"]]
		)

	def test_the_id_never_comes_from_a_request(self):
		# The functions that take an id are internal, and the endpoints that send a
		# turn have no parameter a client could put one in.
		for fn in (chat_api._enqueue_turn, chat_api._enqueue_turn_with_its_message, macros._dispatch_step):
			self.assertNotIn(fn, frappe.whitelisted, fn.__name__)
		for endpoint in (chat_api.send_message, chat_api.retry_message, chat_api.enqueue_continuation):
			self.assertNotIn("run_id", inspect.signature(endpoint).parameters, endpoint.__name__)

	def test_a_fixed_id_taken_by_another_chat_is_an_error_not_a_skipped_step(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		_, elsewhere, _ = self._mk_run(tag="elsewhere")
		other_seed = self._msg(elsewhere, "user", origin="human", content="hi")
		frappe.db.commit()
		self._accept(elsewhere, step_turn_id(run, 1), other_seed)
		with patch("frappe.log_error"):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		row = self._run(run)
		self.assertEqual(row.status, "failed")
		self.assertIn("Step 2 could not be started", row.error)
		self.assertEqual(self._turns(conv, besides=(step_one,)), [])

	def test_a_step_asked_for_again_leaves_a_trace_without_the_prompt(self):
		run, conv, macro = self._mk_run(steps=3, at_step=1)
		self._turn(conv)
		step = frappe.get_doc(MACRO, macro).steps[1]
		frappe.db.set_value(step.doctype, step.name, "prompt", "the confidential prompt")
		frappe.db.commit()
		macros._dispatch_step(*self._docs(run, macro), 1)
		frappe.db.set_value(RUN, run, "current_step", 1)
		frappe.db.commit()
		with patch("frappe.log_error") as log:
			self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_ALREADY_OUT)
		(call,) = log.call_args_list
		self.assertEqual(call.kwargs["title"], f"jarvis.chat.macros.step_already_sent: {run}")
		self.assertIn(step_turn_id(run, 1), call.kwargs["message"])
		self.assertNotIn("confidential", call.kwargs["message"])


# --------------------------------------------------------------------------- #
# A failure after the step's turn exists
# --------------------------------------------------------------------------- #
class _Starting:
	"""Mixin: start a run through ``run_macro`` without a gateway or a usage check."""

	def _mk_macro(self, *, steps=3, tag="start", stop_on_error=1):
		frappe.set_user(OWNER)
		macro = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{outcome.PFX}-{tag}",
				"stop_on_error": stop_on_error,
				"steps": [{"prompt": f"step {i}"} for i in range(1, steps + 1)],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return macro.name

	@contextmanager
	def _starting(self):
		with (
			patch.object(macros, "entitlement_block", return_value=None),
			# Unique per chat: `session_key` is a unique column.
			patch(
				"jarvis.chat.api._ensure_session_key",
				side_effect=lambda *a, **kw: f"sk-identity-{frappe.generate_hash(length=8)}",
			),
		):
			yield


class TestAFailureAfterTheTurnExists(_Starting, IdentityBase):
	"""The cursor is written after the turn. Whatever failed in between left a step
	that was out and a run that did not know it."""

	def _after_the_turn(self, error):
		"""The dispatch fails right after the accept gate created the step's turn."""
		return patch.object(macros, "_fenced_by_an_ended_run", side_effect=error)

	def test_a_hook_killed_after_the_turn_does_not_send_the_step_again(self):
		# Issue #422. The cursor stayed on step 1, so step 2's own end sent step 2 again.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		with self._after_the_turn(_Killed()), self.assertRaises(_Killed):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		frappe.db.rollback()
		two, three = step_turn_id(run, 1), step_turn_id(run, 2)
		self.assertEqual(self._names(conv, (step_one,)), [two])
		self.assertEqual(self._run(run).current_step, 1, "premise: the cursor was left behind")
		# The resume cron has nothing to do for a `running` run, and step 1's end
		# delivered again names a step the run has left.
		macros.resume_waiting_capacity_runs()
		macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self.assertEqual(self._names(conv, (step_one,)), [two])
		# Step 2 ends: step 3 goes out, once, and the cursor says three steps were sent.
		self._finish(conv, two)
		self.assertEqual(self._names(conv, (step_one,)), [two, three])
		self.assertEqual(self._prompts(conv), ["prompt", "step 2", "step 3"])
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 3))

	def test_a_step_that_failed_after_a_lost_cursor_is_named_by_its_own_number(self):
		# The cursor was left on 1 while step 2 ran. Step 2 failed ("Stop on error"): the
		# run was ended "Step 1 failed", and the month's budget never counted step 2.
		run, conv, _ = self._mk_run(steps=3, at_step=1, trigger="scheduled")
		step_one, _, _ = self._turn(conv)
		with self._after_the_turn(_Killed()), self.assertRaises(_Killed):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		frappe.db.rollback()
		self._finish(conv, step_turn_id(run, 1), state="errored", error="The model is overloaded.")
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("failed", "Step 2 failed: The model is overloaded."))
		self.assertEqual(row.current_step, 2)

	def test_a_hook_that_raises_after_the_turn_keeps_the_run_running(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		with self._after_the_turn(RuntimeError("db")), patch("frappe.log_error"):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_raise_after_the_turn_undoes_a_park_that_came_meanwhile(self):
		# A second dispatcher parked the run while this one created the step's turn,
		# and this one then raised. The run stayed parked: the step's end was ignored.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)

		def parked_then_raise(*a, **kw):
			frappe.db.set_value(RUN, run, "status", "waiting_capacity")
			frappe.db.commit()
			raise RuntimeError("db")

		with self._after_the_turn(parked_then_raise), patch("frappe.log_error"):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_resume_that_raises_after_the_turn_does_not_park_the_run_again(self):
		# The compensation put the run back to `waiting_capacity` whatever had gone
		# out: the running step's end was then ignored, and the next cycle sent the
		# step a second time.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		frappe.db.commit()
		with self._after_the_turn(RuntimeError("db")), patch("frappe.log_error"):
			macros.resume_waiting_capacity_runs()
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)
		macros.resume_waiting_capacity_runs()  # the next cycle
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_resume_killed_after_the_turn_then_the_next_cycles(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		frappe.db.commit()
		with self._after_the_turn(_Killed()), self.assertRaises(_Killed):
			macros.resume_waiting_capacity_runs()
		frappe.db.rollback()
		two, three = step_turn_id(run, 1), step_turn_id(run, 2)
		macros.resume_waiting_capacity_runs()
		self.assertEqual(self._names(conv, (step_one,)), [two])
		self._finish(conv, two)
		self.assertEqual(self._names(conv, (step_one,)), [two, three])
		self.assertEqual(self._run(run).current_step, 3)

	def test_a_parked_run_whose_step_is_out_is_put_back_to_running_and_nothing_is_sent(self):
		# How a run got here: a second dispatcher met a full queue before the first got
		# its turn, or a compensation from before this fix. The resume flips it to
		# `running` and asks for the step it parked at.
		run, conv, macro = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_SENT)
		frappe.db.set_value(RUN, run, {"status": "waiting_capacity", "current_step": 1})
		frappe.db.commit()
		with patch.object(chat_api, "_enqueue_turn", wraps=chat_api._enqueue_turn) as fresh:
			macros.resume_waiting_capacity_runs()
		fresh.assert_not_called()
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_asked_twice_the_dispatch_sends_once(self):
		run, conv, macro = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_SENT)
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")  # parked by a racing loser
		frappe.db.commit()
		self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_ALREADY_OUT)
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_first_step_that_went_out_does_not_fail_the_run(self):
		# run_macro failed the run on any raise: "never started", then the step's reply.
		macro = self._mk_macro(steps=2)
		with self._starting(), self._after_the_turn(RuntimeError("db")), patch("frappe.log_error"):
			out = macros.run_macro(macro)
		self.assertTrue(out["ok"])
		run, conv = out["data"]["macro_run"], out["data"]["conversation"]
		self.assertEqual(self._names(conv, ()), [step_turn_id(run, 0)])
		row = self._run(run)
		self.assertEqual((row.status, row.error or "", row.current_step), ("running", "", 1))


# --------------------------------------------------------------------------- #
# A failure before the step's turn exists
# --------------------------------------------------------------------------- #
class TestAFailureBeforeTheTurnExists(_Starting, IdentityBase):
	"""A failure before the step's turn exists leaves nothing: the step's message and
	its turn are one transaction."""

	def _before_the_turn(self, error):
		return patch.object(admission, "accept_or_queue", side_effect=error)

	def test_a_resume_that_raises_before_the_turn_sends_the_step_once_on_the_next_cycle(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		frappe.db.commit()
		with self._before_the_turn(RuntimeError("redis")), patch("frappe.log_error"):
			macros.resume_waiting_capacity_runs()
		row = self._run(run)
		self.assertEqual((row.status, row.current_step), ("waiting_capacity", 1))
		self.assertEqual(self._turns(conv, besides=(step_one,)), [])
		self.assertEqual(len(self._seeds(conv)), 1, "the parked run kept a message with no turn")
		macros.resume_waiting_capacity_runs()
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_resume_killed_before_the_turn_leaves_no_message(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		frappe.db.commit()
		with self._killed_at_the_turn_insert():
			macros.resume_waiting_capacity_runs()
		self.assertEqual(len(self._seeds(conv)), 1)
		self.assertEqual(self._turns(conv, besides=(step_one,)), [])

	def test_a_hook_killed_before_the_turn_sends_the_step_once_when_the_step_end_comes_again(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		with self._killed_at_the_turn_insert():
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self.assertEqual(len(self._seeds(conv)), 1, "the dead dispatcher's message survived it")
		macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)
		self.assertEqual(frappe.db.get_value(TURN, step_turn_id(run, 1), "turn_class"), "interactive")

	def test_a_hook_that_raises_before_the_turn_ends_the_run_as_before(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		with self._before_the_turn(RuntimeError("redis")), patch("frappe.log_error"):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		row = self._run(run)
		self.assertEqual(row.status, "failed")
		self.assertIn("Step 2 could not be started", row.error)
		self.assertEqual(len(self._seeds(conv)), 1)

	def test_a_first_step_that_never_went_out_still_fails_the_run_at_once(self):
		for trigger in ("manual", "scheduled"):
			macro = self._mk_macro(steps=2, tag=f"never-{trigger}")
			with self._starting(), self._before_the_turn(RuntimeError("redis")), patch("frappe.log_error"):
				if trigger == "manual":
					with self.assertRaises(frappe.ValidationError):
						macros.run_macro(macro)
				else:
					self.assertEqual(
						macros.run_macro(macro, trigger="scheduled"),
						{"ok": False, "reason": macros.BLOCK_DISPATCH_FAILED},
					)
			frappe.db.rollback()
			row = frappe.db.get_value(
				RUN, {"macro": macro}, ["status", "error", "conversation"], as_dict=True
			)
			self.assertEqual((row.status, row.error), ("failed", macros._DISPATCH_FAILED_ERROR), trigger)
			self.assertEqual(self._seeds(row.conversation), [], trigger)

	def test_on_the_legacy_path_a_first_step_left_without_a_job_is_closed_not_run_by_the_sweep(self):
		# There the message is committed before the job is asked for. The run was failed
		# "never started", and three minutes later the sweep ran the step anyway.
		self._set_mode("legacy")
		macro = self._mk_macro(steps=2, tag="legacy-lone")
		with (
			self._starting(),
			patch.object(chat_api, "_dispatch_turn", side_effect=RuntimeError("redis")),
			patch("frappe.log_error"),
			self.assertRaises(frappe.ValidationError),
		):
			macros.run_macro(macro)
		frappe.db.rollback()
		row = frappe.db.get_value(RUN, {"macro": macro}, ["name", "status", "conversation"], as_dict=True)
		self.assertEqual(row.status, "failed")
		(seed,) = self._seeds(row.conversation)
		closing = frappe.get_all(
			MSG,
			filters={"conversation": row.conversation, "role": "assistant", "ref_name": row.name},
			pluck="content",
		)
		self.assertEqual(closing, [f"✗ Macro failed. {macros._DISPATCH_FAILED_ERROR}"])
		_age(seed)
		self._sweep()
		self.assertEqual(self._jobs, [], "the sweep ran the step of a run that never started")


# --------------------------------------------------------------------------- #
# Where a run is
# --------------------------------------------------------------------------- #
class TestWhereARunIs(IdentityBase):
	def _summarized(self, **kw):
		run, conv, macro = self._mk_run(steps=3, at_step=0, **kw)
		frappe.db.set_value(RUN, run, {"run_mode": "merged", "total_steps": 1})
		frappe.db.set_value(MACRO, macro, "merged_prompt", "do all three", update_modified=False)
		frappe.db.commit()
		return run, conv, macro

	def test_a_stray_message_with_no_turn_does_not_skip_a_step(self):
		# Counting messages to find the position let one stray message move it.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		self._msg(conv, "user", origin="macro", content="left behind by something else")
		frappe.db.commit()
		macros.advance_after_turn(conv, errored=False, run_id=step_one)
		two = step_turn_id(run, 1)
		self.assertEqual(self._run(run).current_step, 2)
		self.assertEqual(
			frappe.db.get_value(MSG, frappe.db.get_value(TURN, two, "seed_message"), "content"), "step 2"
		)
		self._finish(conv, two)
		three = frappe.db.get_value(TURN, step_turn_id(run, 2), "seed_message")
		self.assertEqual(frappe.db.get_value(MSG, three, "content"), "step 3")
		self.assertEqual(self._run(run).current_step, 3)

	def test_the_cursor_is_never_lowered(self):
		run, conv, macro = self._mk_run(steps=5, at_step=1, trigger="scheduled")
		self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_SENT)
		frappe.db.set_value(RUN, run, "current_step", 4)  # the run has moved on since
		frappe.db.commit()
		spent = macros._scheduled_steps_this_month(OWNER)
		self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_ALREADY_OUT)
		self.assertEqual(self._run(run).current_step, 4)
		self.assertEqual(macros._scheduled_steps_this_month(OWNER), spent)

	def test_no_cursor_write_lowers_it(self):
		run, _, macro = self._mk_run(steps=5, at_step=4)
		run_doc, _ = self._docs(run, macro)
		for sent in (True, False):
			macros._raise_cursor(run_doc, 2, sent=sent)
			self.assertEqual((self._run(run).current_step, run_doc.current_step), (4, 4), sent)
		macros._raise_cursor(run_doc, 5, sent=True)
		self.assertEqual((self._run(run).current_step, run_doc.current_step), (5, 5))

	def test_a_repair_counts_the_step_that_was_sent_and_nothing_more(self):
		# The month's scheduled budget adds up the cursors. A cursor left behind a sent
		# step under-counted it; the repair counts it once, and repeating it adds nothing.
		run, conv, macro = self._mk_run(steps=5, at_step=1, trigger="scheduled")
		self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_SENT)
		frappe.db.set_value(RUN, run, "current_step", 1, update_modified=False)  # left behind
		frappe.db.commit()
		spent, modified = macros._scheduled_steps_this_month(OWNER), frappe.db.get_value(RUN, run, "modified")
		for _ in range(2):
			self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_ALREADY_OUT)
			self.assertEqual(self._run(run).current_step, 2)
			self.assertEqual(macros._scheduled_steps_this_month(OWNER), spent + 1)
		# A repair is not progress: the stale-run sweep measures idleness on `modified`.
		self.assertEqual(frappe.db.get_value(RUN, run, "modified"), modified)
		self.assertEqual(len(self._turns(conv)), 1)

	def test_the_position_walks_past_every_step_that_is_out(self):
		run, conv, macro = self._mk_run(steps=5, at_step=0)
		for index in (0, 1):
			self.assertEqual(macros._dispatch_step(*self._docs(run, macro), index), macros._STEP_SENT)
		frappe.db.set_value(RUN, run, "current_step", 0, update_modified=False)  # two failures in a row
		frappe.db.commit()
		run_doc, _ = self._docs(run, macro)
		self.assertEqual(macros._next_step_index(run_doc), 2)
		self.assertEqual((self._run(run).current_step, run_doc.current_step), (2, 2))

	def test_a_summarized_run_is_never_sent_step_ones_prompt(self):
		# Its one turn ended with the cursor left on 0: the hook took that for "step 1
		# is next" and sent step 1's prompt to a run that had done all the steps as one.
		run, conv, macro = self._summarized()
		self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 0), macros._STEP_SENT)
		frappe.db.set_value(RUN, run, "current_step", 0, update_modified=False)
		frappe.db.commit()
		self._finish(conv, step_turn_id(run, 0))
		row = self._run(run)
		self.assertEqual((row.status, row.current_step), ("completed", 1))
		self.assertEqual(self._prompts(conv), ["do all three"])

	def test_a_summarized_runs_one_turn_ends_it_whatever_its_id(self):
		# A turn with a random id (re-routed in a cutover, or healed by the orphan sweep)
		# has no fixed id for the walk to find: with the cursor on 0 the summary went
		# out a second time, and EMPTY when an edit had cleared it meanwhile.
		run, conv, macro = self._summarized()
		other, seed, _ = self._turn(conv, state="done")
		frappe.db.set_value(MSG, seed, "content", "do all three")
		frappe.db.set_value(MACRO, macro, "merged_prompt", "", update_modified=False)
		frappe.db.commit()
		macros.advance_after_turn(conv, errored=False, run_id=other)
		row = self._run(run)
		self.assertEqual((row.status, row.current_step), ("completed", 1))
		self.assertEqual(self._prompts(conv), ["do all three"])

	def test_without_turn_rows_a_summarized_run_ends_on_its_one_turn(self):
		# No Turn rows, so the cursor is all there is: a cursor left on 0 sent the
		# summary again. It is one turn whatever the cursor says.
		self._set_mode("legacy")
		run, conv, macro = self._summarized()
		self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 0), macros._STEP_SENT)
		frappe.db.set_value(RUN, run, "current_step", 0, update_modified=False)
		frappe.db.commit()
		seed = self._seeds(conv)[-1]
		self._msg(conv, "assistant", content="did it")
		frappe.db.commit()
		macros.advance_after_turn(conv, errored=False, seed_message=seed)
		self.assertEqual(self._prompts(conv), ["do all three"])
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("completed", 1))

	def test_the_run_lock_still_expires_after_sixty_seconds(self):
		import re

		self.assertEqual(set(re.findall(r"[ (]timeout_s=(\d+)", inspect.getsource(macros))), {"60"})


# --------------------------------------------------------------------------- #
# Every transport mode
# --------------------------------------------------------------------------- #
_REAL_PROMOTE = admission.promote_next


class TestEveryTransportMode(_Starting, IdentityBase):
	"""A three-step run, start to finish, the way each transport delivers a turn's
	end. These five use nothing this change added, so they pass on the code before
	it too: that is the check that the modes behave as they did."""

	def _three_step_run(self, mode):
		self._set_mode(mode)
		macro = self._mk_macro(steps=3, tag=f"mode-{mode}")
		with self._starting():
			out = macros.run_macro(macro)
		run, conv = out["data"]["macro_run"], out["data"]["conversation"]
		ids = []
		for number in (1, 2, 3):
			seeds = self._seeds(conv)
			self.assertEqual(len(seeds), number, f"{mode}: step {number} was not sent exactly once")
			seed = seeds[-1]
			turn = frappe.db.get_value(TURN, {"seed_message": seed}, "name")
			reply = self._msg(conv, "assistant", content=f"did {number}")
			frappe.db.commit()
			if mode == "pump":
				# finalize delivers the end while the turn is `finalizing`, then closes it.
				frappe.db.set_value(TURN, turn, {"state": "finalizing", "assistant_message": reply})
				frappe.db.commit()
				macros.advance_after_turn(conv, errored=False, run_id=turn)
				frappe.db.set_value(TURN, turn, "state", "done")
				frappe.db.commit()
			else:
				# The worker delivers the end, naming its message, BEFORE it settles its turn.
				job = next(j for j in self._jobs if j["message_id"] == seed)
				macros.advance_after_turn(conv, errored=False, run_id=job["run_id"], seed_message=seed)
				if turn:
					admission.settle_turn(turn, "done")
			ids.append((turn, self._jobs[-1]["run_id"] if self._jobs else None))
		row = self._run(run)
		self.assertEqual((row.status, row.error or "", row.current_step), ("completed", "", 3), mode)
		self.assertEqual(self._prompts(conv), ["step 1", "step 2", "step 3"], mode)
		return run, conv

	def test_the_pump(self):
		self._three_step_run("pump")

	def test_pure_legacy(self):
		self._three_step_run("legacy")

	def test_draining(self):
		self._three_step_run("draining")

	def test_phase_0(self):
		with patch.object(admission, "promote_next", _REAL_PROMOTE):
			self._three_step_run("phase0")

	def test_the_legacy_kill_switch(self):
		self._three_step_run("kill switch")


class TestTheFixedIdIsUsedWhereTurnRowsAreWritten(_Starting, IdentityBase):
	def _run_ids(self, mode):
		self._set_mode(mode)
		macro = self._mk_macro(steps=1, tag=f"ids-{mode}")
		with self._starting():
			out = macros.run_macro(macro)
		run, conv = out["data"]["macro_run"], out["data"]["conversation"]
		return run, [name for name, _ in self._turns(conv)], [j["run_id"] for j in self._jobs]

	def test_the_turn_machine_names_the_turn_after_the_step(self):
		run, turns, jobs = self._run_ids("pump")
		self.assertEqual((turns, jobs), ([step_turn_id(run, 0)], []))
		run, turns, jobs = self._run_ids("phase0")
		self.assertEqual((turns, jobs[-1:]), ([step_turn_id(run, 0)], [step_turn_id(run, 0)]))

	def test_without_turn_rows_the_step_keeps_a_random_id(self):
		for mode in ("legacy", "draining"):
			self._jobs.clear()
			run, turns, jobs = self._run_ids(mode)
			self.assertEqual(turns, [], mode)
			self.assertEqual(len(jobs), 1, mode)
			self.assertNotEqual(jobs[0], step_turn_id(run, 0), mode)


# --------------------------------------------------------------------------- #
# The hook decides as before
# --------------------------------------------------------------------------- #
class TestTheHookDecidesAsBefore(IdentityBase):
	def test_its_signature_is_unchanged(self):
		self.assertEqual(
			list(inspect.signature(macros.advance_after_turn).parameters),
			["conversation_id", "errored", "run_id", "seed_message", "assistant_message"],
		)

	def test_the_old_turn_of_a_retried_step_does_not_move_the_run(self):
		# The step failed, its end was dropped (a busy lock), and the owner pressed
		# Retry: a second turn on the step's message. The old turn's end is delivered
		# again minutes later. It named the right message, so it failed the run (Stop
		# on error) under a retry that was still running.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		old, seed, _ = self._turn(conv, state="errored", error="The model is overloaded.")
		retry = frappe.generate_hash(length=12)
		frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": retry,
				"conversation": conv,
				"relay_target_id": self._target,
				"turn_class": "interactive",
				"state": "streaming",
				"seed_message": seed,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		macros.advance_after_turn(conv, errored=True, run_id=old)
		row = self._run(run)
		self.assertEqual((row.status, row.current_step), ("running", 1))
		self.assertEqual(len(self._seeds(conv)), 1)
		# The retry's own end is the one that counts.
		self._finish(conv, retry)
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 2))
		self.assertTrue(frappe.db.exists(TURN, step_turn_id(run, 1)))

	def test_a_failed_step_whose_turn_has_not_settled_is_a_failure(self):
		# The legacy worker calls the hook before it settles its turn: with Phase-0 the
		# row still says `dispatching`. The row must not be what decides there.
		run, conv, _ = self._mk_run(steps=2, at_step=1, tag="unsettled-stop")
		turn, seed, _ = self._turn(
			conv, state="dispatching", reply="", reply_error="The model is overloaded."
		)
		macros.advance_after_turn(conv, errored=True, run_id=turn, seed_message=seed)
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("failed", "Step 1 failed: The model is overloaded."))
		self.assertEqual(len(self._seeds(conv)), 1, "Stop on error was not honoured")

		run, conv, _ = self._mk_run(steps=1, at_step=1, stop_on_error=0, tag="unsettled-go")
		turn, seed, _ = self._turn(
			conv, state="dispatching", reply="", reply_error="The model is overloaded."
		)
		macros.advance_after_turn(conv, errored=True, run_id=turn, seed_message=seed)
		row = self._run(run)
		self.assertEqual(row.status, "failed")
		self.assertEqual(row.error, "Finished, but its only step failed. Step 1: The model is overloaded.")

	def test_a_turn_with_no_row_is_not_judged_by_another_turns_row(self):
		# The step failed once (an `errored` turn), was retried, and the retry ran as a
		# legacy job with no Turn row (the kill switch). Its end names a turn that has
		# no row: the old attempt's row must not be what decides.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		_, seed, _ = self._turn(conv, state="errored", error="The model is overloaded.")
		self._msg(conv, "assistant", content="done on the second try")
		frappe.db.commit()
		macros.advance_after_turn(conv, errored=False, run_id="no-such-turn", seed_message=seed)
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 2))

	def test_a_failed_step_with_no_turn_row_is_a_failure(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		seed = self._msg(conv, "user", origin="macro", content="step 1")
		self._msg(conv, "assistant", error="The model is overloaded.")
		frappe.db.commit()
		macros.advance_after_turn(conv, errored=True, seed_message=seed)
		self.assertEqual(self._run(run).status, "failed")
		self.assertEqual(len(self._seeds(conv)), 1)

	def test_the_verdict(self):
		def turn(state):
			return frappe._dict(state=state)

		def reply(**kw):
			return frappe._dict({"was_recovered": 0, "streaming": 0, "content": "", "error": "", **kw})

		recovered = reply(was_recovered=1, content="the answer")
		for row, its_reply, flag, verdict, why in (
			(None, None, False, "", "no Turn row: the caller's flag"),
			(None, None, True, "errored", "no Turn row: the caller's flag"),
			(turn("dispatching"), None, True, "errored", "not settled yet: the caller's flag"),
			(turn("dispatching"), None, False, "", "not settled yet: the caller's flag"),
			(turn("finalizing"), None, True, "errored", "not ended yet: the caller's flag"),
			(turn("recovering"), recovered, False, "", "not ended yet: the caller's flag"),
			(turn("done"), None, True, "", "ended: the row decides"),
			(turn("errored"), None, False, "errored", "ended: the row decides"),
			(turn("errored"), reply(content="done"), False, "errored", "a reply with text is not a recovery"),
			(turn("cancelled"), reply(content="partial"), False, "cancelled", "a step the user stopped"),
			(turn("errored"), recovered, True, "", "recovered later: the answer arrived"),
			(
				turn("errored"),
				reply(was_recovered=1, content="x", streaming=1),
				False,
				"errored",
				"streaming",
			),
			(turn("errored"), reply(was_recovered=1), False, "errored", "recovered to nothing"),
			(
				turn("errored"),
				reply(was_recovered=1, content="x", error="boom"),
				False,
				"errored",
				"an error",
			),
			(turn("cancelled"), recovered, False, "cancelled", "a stop is never a success"),
		):
			with self.subTest(why=why, state=row and row.state, flag=flag):
				self.assertEqual(macros._step_verdict(row, its_reply, flag), verdict)

	def test_a_reply_recovered_after_its_turn_errored_is_a_success(self):
		# The turn was recorded `errored` (the stream broke), then turn recovery
		# finished the reply. Its end said "failed" by the row: Stop on error ended the
		# run on a step that had answered, and a run that went on was summed up as
		# "1 of 2 steps failed".
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		turn, _, reply = self._turn(conv, state="errored", error="The stream broke.", reply="the answer")
		frappe.db.set_value(MSG, reply, "was_recovered", 1, update_modified=False)
		frappe.db.commit()
		macros.advance_after_turn(conv, errored=True, run_id=turn)
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 2))
		self._finish(conv, step_turn_id(run, 1))
		row = self._run(run)
		self.assertEqual((row.status, row.error or ""), ("completed", ""))

	def test_a_step_the_user_stopped_is_still_stopped(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		turn, _, _ = self._turn(conv, state="cancelled", reply="partial")
		macros.advance_after_turn(conv, errored=False, run_id=turn)  # even if the caller says otherwise
		row = self._run(run)
		self.assertEqual((row.status, row.error or ""), ("stopped", ""))
		self.assertEqual(len(self._seeds(conv)), 1)

	def test_turn_recovery_naming_only_the_old_reply_does_not_move_a_retried_step(self):
		# Turn recovery names the reply, not the turn, and a recovered reply is exactly
		# the shape a late old end takes. The run moved on under a retry still running.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		old, seed, reply = self._turn(conv, state="errored", error="The stream broke.", reply="the answer")
		frappe.db.set_value(MSG, reply, "was_recovered", 1, update_modified=False)
		self._retry(conv, seed, "streaming")
		macros.advance_after_turn(conv, errored=False, assistant_message=reply)
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 1))
		self.assertEqual(len(self._seeds(conv)), 1)

	def test_a_retry_cancelled_before_it_started_lets_the_old_end_count(self):
		# The owner cancelled the retry's queued chip (or it aged out): no hook call for
		# it, ever. Ignoring the old end for it left the run waiting for the reaper.
		run, conv, _ = self._mk_run(steps=3, at_step=1, stop_on_error=0)
		old, seed, _ = self._turn(conv, state="errored", error="The model is overloaded.")
		self._retry(conv, seed, "cancelled")
		macros.advance_after_turn(conv, errored=True, run_id=old)
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 2))
		self.assertTrue(frappe.db.exists(TURN, step_turn_id(run, 1)))

	def _retry(self, conv, seed, state):
		retry = frappe.generate_hash(length=12)
		frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": retry,
				"conversation": conv,
				"relay_target_id": self._target,
				"turn_class": "interactive",
				"state": state,
				"seed_message": seed,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return retry
