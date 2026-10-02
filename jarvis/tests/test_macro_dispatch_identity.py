"""A macro step is sent once, whoever sends it and however often they try.

From the 2026-10-01 macros review: the run's cursor was written after the step's turn
existed, so anything between the two (a raise, a lapsed run lock, a second healer for
a message with no turn) sent the same step again (issue #422). The step's turn now has
a fixed id, ``sha1("<run>:<index>")[:12]``, and every dispatcher goes through one
function that lands on it.

These tests drive the REAL dispatch (``api._enqueue_turn`` and the admission gate, on
a shard of their own with the pump's wake and the RQ enqueue stubbed), not a stand-in
for it.
"""

from __future__ import annotations

import hashlib
import inspect
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from jarvis.chat import admission, macros, pump, stale_scan
from jarvis.chat import api as chat_api
from jarvis.chat import turn_state as ts
from jarvis.tests import test_macro_run_outcome as outcome
from jarvis.tests._pending_action_helpers import ensure_user
from jarvis.tests.race_harness import other_connection

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
	wakes a pump, and a legacy job is recorded, not enqueued."""

	def setUp(self):
		super().setUp()
		self._target = f"{TARGET_PREFIX}{frappe.generate_hash(length=10)}"
		ts._ensure_control_row(self._target)
		self._conf = {k: frappe.local.conf.get(k) for k in ("jarvis_pump_enabled", admission.FLAG)}
		self._set_mode("pump")
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

	def _strikes(self, seed):
		return int(frappe.db.get_value(MSG, seed, "was_recovered") or 0)

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

	def _dangling_step(self, *, steps=3, tag="m", **kw):
		"""A run whose step 1 is done and whose step 2 message was committed by a
		dispatcher that died before the turn was created: the message is there, the
		turn is not, the cursor still says 1. Returns ``(run, conv, macro, seed)``."""
		run, conv, macro = self._mk_run(steps=steps, at_step=1, tag=tag, **kw)
		step_one, _, _ = self._turn(conv)
		self._step_one = step_one
		with patch.object(admission, "accept_or_queue", side_effect=_Killed()), self.assertRaises(_Killed):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		frappe.db.rollback()
		seed = self._seeds(conv)[-1]
		self.assertEqual(self._turns(conv, besides=(step_one,)), [], "premise: no turn for the step")
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 1))
		return run, conv, macro, seed

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
			try:
				fn()
				other.commit()
			finally:
				frappe.local.db = mine
				frappe.set_user(user)

	@contextmanager
	def _while_the_first_is_between_its_message_and_its_turn(self, second):
		"""Run ``second`` to completion, on a second real connection, at the moment the
		first dispatcher has committed its step message and has not yet asked the
		accept gate for a turn. Then let the first carry on."""
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

	def _assert_step_sent_once(self, run, conv, index, *, besides, seeds):
		"""One turn for step ``index``, under its fixed id; ``seeds`` step messages in
		the chat; the cursor on the step after."""
		turns = self._turns(conv, besides=besides)
		self.assertEqual(len(turns), 1, f"step {index + 1} was sent {len(turns)} times: {turns}")
		self.assertEqual(turns[0][0], step_turn_id(run, index), "the turn does not carry the step's fixed id")
		self.assertEqual(len(self._seeds(conv)), seeds, "a second message for one step")
		self.assertEqual(turns[0][1], self._seeds(conv)[-1], "the turn is not on the step's message")
		row = self._run(run)
		self.assertEqual((row.status, row.current_step), ("running", index + 1))


# --------------------------------------------------------------------------- #
# Two dispatchers at once
# --------------------------------------------------------------------------- #
class TestTwoDispatchersAtOnce(IdentityBase):
	"""The run lock is the only thing that kept two dispatchers apart, and it lapses
	(60 s) under a slow holder. Each pairing: the first has committed the step's
	message, the second runs start to finish, the first carries on."""

	def test_the_same_step_end_delivered_twice(self):
		# The finalize effect that calls the hook is retried.
		for who in ("Administrator", BYSTANDER):
			run, conv, _ = self._mk_run(steps=3, at_step=1, tag=f"twice-{who[:5]}")
			step_one, _, _ = self._turn(conv)

			def again(conv=conv, step_one=step_one):
				macros.advance_after_turn(conv, errored=False, run_id=step_one)

			self._as(who)
			with (
				self._locks_always_granted(),
				self._while_the_first_is_between_its_message_and_its_turn(again),
			):
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
			with (
				self._locks_always_granted(),
				self._while_the_first_is_between_its_message_and_its_turn(step_end),
			):
				macros.resume_waiting_capacity_runs()
			frappe.set_user("Administrator")
			self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_the_step_end_and_the_orphan_sweep(self):
		# The sweep finds a message with no turn and no reply and re-dispatches it. A
		# step's message is exactly that between its commit and its turn.
		for who in ("Administrator", BYSTANDER):
			run, conv, _ = self._mk_run(steps=3, at_step=1, tag=f"sweep-{who[:5]}")
			step_one, _, _ = self._turn(conv)

			def sweep(conv=conv):
				seed = frappe.get_all(
					MSG,
					filters={"conversation": conv, "role": "user", "origin": "macro"},
					fields=["name", "conversation", "seq", "was_recovered", "origin"],
					order_by="seq desc",
					limit=1,
				)[0]
				stale_scan._heal_or_error_orphan({**seed, "owner": OWNER}, None, None)

			self._as(who)
			with (
				self._locks_always_granted(),
				self._while_the_first_is_between_its_message_and_its_turn(sweep),
			):
				macros.advance_after_turn(conv, errored=False, run_id=step_one)
			frappe.set_user("Administrator")
			self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_second_dispatcher_at_a_full_queue_is_told_duplicate_not_overloaded(self):
		# The second dispatcher's turn fills the queue; the first then asks for the same
		# turn. Told "the site is full" it removed the step's message (the one the turn
		# is on) and parked a run whose step was out.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)

		def again():
			macros.advance_after_turn(conv, errored=False, run_id=step_one)

		with (
			patch.object(admission, "MAX_QUEUE_DEPTH", 1),
			self._locks_always_granted(),
			self._while_the_first_is_between_its_message_and_its_turn(again),
		):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_dispatcher_that_lost_removes_the_message_it_wrote(self):
		# The second runs before the first has written its message, so each writes one.
		# The first is then told `duplicate`: its message has no turn and never will.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		real, ran = chat_api._enqueue_turn, []

		def enqueue(*args, **kw):
			if not ran:
				ran.append(1)
				self._elsewhere(lambda: macros.advance_after_turn(conv, errored=False, run_id=step_one))
			return real(*args, **kw)

		with self._locks_always_granted(), patch.object(chat_api, "_enqueue_turn", side_effect=enqueue):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		self.assertEqual(ran, [1])
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)

	def test_a_stop_while_the_step_is_really_being_sent_withdraws_it(self):
		# The fence lives in the one dispatch function now; this is it on the real path.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		with (
			patch.object(macros, "_STOP_LOCK_BLOCK_S", 0.05),
			self._while_the_first_is_between_its_message_and_its_turn(
				lambda: macros._stop_run(run, reason="", by=OWNER)
			),
		):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		row = self._run(run)
		self.assertEqual((row.status, row.current_step), ("stopped", 1))
		self.assertEqual(frappe.db.get_value(TURN, step_turn_id(run, 1), "state"), "cancelled")


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

	def test_enqueue_turn_takes_the_engines_id_and_says_when_it_was_a_duplicate(self):
		_, conv, _ = self._mk_run()
		first = chat_api._enqueue_turn(conv, "step", origin="macro", run_id="ident_enq_01")
		self.assertEqual(first["run_id"], "ident_enq_01")
		self.assertNotIn("duplicate", first)
		self.assertEqual(frappe.db.get_value(TURN, "ident_enq_01", "seed_message"), first["message_id"])
		again = chat_api._enqueue_turn(conv, "step", origin="macro", run_id="ident_enq_01")
		self.assertTrue(again["duplicate"])
		self.assertNotEqual(again["message_id"], first["message_id"])
		self.assertTrue(frappe.db.exists(MSG, again["message_id"]), "the caller decides what becomes of it")
		self.assertEqual(frappe.db.count(TURN, {"conversation": conv}), 1)

	def test_everyone_else_still_gets_a_random_id(self):
		_, conv, _ = self._mk_run()
		one = chat_api._enqueue_turn(conv, "a", origin="continuation", hidden=True, exempt_overload=True)
		two = chat_api._enqueue_turn(conv, "a", origin="continuation", hidden=True, exempt_overload=True)
		self.assertNotEqual(one["run_id"], two["run_id"])
		self.assertRegex(one["run_id"], r"^[0-9a-f]{12}$")

	def test_the_id_never_comes_from_a_request(self):
		# The functions that take an id are internal, and the endpoints that send a
		# turn have no parameter a client could put one in.
		for fn in (
			chat_api._enqueue_turn,
			chat_api._redispatch_orphan,
			macros._dispatch_step,
			macros.heal_dangling_seed,
			macros.fail_run_for_seed,
		):
			self.assertNotIn(fn, frappe.whitelisted, fn.__name__)
		for endpoint in (chat_api.send_message, chat_api.retry_message, chat_api.enqueue_continuation):
			self.assertNotIn("run_id", inspect.signature(endpoint).parameters, endpoint.__name__)


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

	def _names(self, conv, besides):
		return [name for name, _ in self._turns(conv, besides=besides)]

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

	def test_a_hook_that_raises_after_the_turn_keeps_the_run_running(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		with self._after_the_turn(RuntimeError("db")), patch("frappe.log_error"):
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
	"""The step's message is committed before its turn is asked for. A failure there
	leaves the message alone in the chat."""

	def _before_the_turn(self, error):
		return patch.object(admission, "accept_or_queue", side_effect=error)

	def test_a_resume_that_raises_before_the_turn_sends_the_step_once_on_the_next_cycle(self):
		# The next cycle wrote a second message for the step, and the orphan sweep
		# later ran the first one.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		frappe.db.commit()
		with self._before_the_turn(RuntimeError("redis")), patch("frappe.log_error"):
			macros.resume_waiting_capacity_runs()
		row = self._run(run)
		self.assertEqual((row.status, row.current_step), ("waiting_capacity", 1))
		self.assertEqual(self._turns(conv, besides=(step_one,)), [])
		left = self._seeds(conv)[-1]
		macros.resume_waiting_capacity_runs()
		self._assert_step_sent_once(run, conv, 1, besides=(step_one,), seeds=2)
		self.assertEqual(self._seeds(conv)[-1], left, "the step's own message was not the one sent")

	def test_a_hook_killed_before_the_turn_is_healed_when_the_step_end_comes_again(self):
		run, conv, _, seed = self._dangling_step()
		macros.advance_after_turn(conv, errored=False, run_id=self._step_one)
		self._assert_step_sent_once(run, conv, 1, besides=(self._step_one,), seeds=2)
		self.assertEqual(self._seeds(conv)[-1], seed)
		self.assertEqual(frappe.db.get_value(TURN, step_turn_id(run, 1), "turn_class"), "interactive")

	def test_a_hook_that_raises_before_the_turn_ends_the_run_as_before(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_one, _, _ = self._turn(conv)
		with self._before_the_turn(RuntimeError("redis")), patch("frappe.log_error"):
			macros.advance_after_turn(conv, errored=False, run_id=step_one)
		row = self._run(run)
		self.assertEqual(row.status, "failed")
		self.assertIn("Step 2 could not be started", row.error)

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
			row = frappe.db.get_value(RUN, {"macro": macro}, ["status", "error"], as_dict=True)
			self.assertEqual((row.status, row.error), ("failed", macros._DISPATCH_FAILED_ERROR), trigger)


# --------------------------------------------------------------------------- #
# The orphan sweep hands a live run's step to the engine
# --------------------------------------------------------------------------- #
class TestTheSweepHandsAStepToTheEngine(IdentityBase):
	"""The sweep is site-wide and this is a shared site: every assertion is on this
	test's own run and chat."""

	def _sweep(self):
		stale_scan._sweep_orphan_turns(now_datetime())

	def _ordinary_turns(self, message):
		"""The turns the sweep's own re-dispatch made for ``message``."""
		return frappe.get_all(TURN, filters={"seed_message": message}, fields=["name", "turn_class"])

	def test_a_step_message_with_no_turn_is_given_its_turn_in_place(self):
		run, conv, _, seed = self._dangling_step(armed=True)
		_age(seed)
		with patch.object(chat_api, "_enqueue_turn", wraps=chat_api._enqueue_turn) as fresh:
			self._sweep()
		fresh.assert_not_called()  # no second message for the step
		self._assert_step_sent_once(run, conv, 1, besides=(self._step_one,), seeds=2)
		self.assertEqual(self._seeds(conv)[-1], seed)
		self.assertEqual(self._strikes(seed), 1)
		# The next sweep finds a message that has its turn.
		self._sweep()
		self._assert_step_sent_once(run, conv, 1, besides=(self._step_one,), seeds=2)
		# And the run carries on from there.
		self._finish(conv, step_turn_id(run, 1))
		self.assertEqual(self._run(run).current_step, 3)
		self.assertEqual(self._prompts(conv), ["prompt", "step 2", "step 3"])

	def test_a_second_strike_ends_the_run(self):
		# The one retry failed too. The run used to stay `running`, and armed, until
		# the stale-run sweep hours later.
		run, conv, _, seed = self._dangling_step(armed=True, trigger="scheduled")
		_age(seed)
		with (
			patch.object(macros, "_dispatch_step", side_effect=RuntimeError("gateway down")),
			patch("frappe.log_error"),
		):
			self._sweep()
		self.assertEqual((self._strikes(seed), self._run(run).status), (1, "running"))
		self.assertEqual(self._ordinary_turns(seed), [])
		with patch("jarvis.chat.macros.publish_to_user") as publish:
			self._sweep()
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("failed", "A step could not be started."))
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)
		self.assertEqual(self._ordinary_turns(seed), [], "a step of an ended run was sent")
		after = frappe.get_all(
			MSG,
			filters={
				"conversation": conv,
				"role": "assistant",
				"seq": [">", frappe.db.get_value(MSG, seed, "seq")],
			},
			fields=["error", "content"],
			order_by="seq asc",
		)
		self.assertEqual(
			[(m.error or "", m.content or "") for m in after],
			[(stale_scan._ORPHAN_ERR, ""), ("", "✗ Macro failed. A step could not be started.")],
		)
		done = [c.args[1] for c in publish.call_args_list if c.args[1].get("kind") == "macro:done"]
		self.assertEqual([(d["macro_run"], d["status"]) for d in done], [(run, "failed")])
		self.assertEqual(len(frappe.get_all("Notification Log", filters={"for_user": OWNER})), 1)

	def test_a_full_site_does_not_cost_the_strike_and_the_step_is_sent_once_later(self):
		run, conv, _, seed = self._dangling_step()
		_age(seed)
		with patch.object(admission, "MAX_QUEUE_DEPTH", 0):
			self._sweep()
		self.assertEqual(self._run(run).status, "waiting_capacity")
		self.assertEqual(self._turns(conv, besides=(self._step_one,)), [])
		# A parked run leaves no message behind (the resume writes it afresh), so there
		# is nothing left for a second strike to land on.
		self.assertFalse(frappe.db.exists(MSG, seed))
		macros.resume_waiting_capacity_runs()
		self._assert_step_sent_once(run, conv, 1, besides=(self._step_one,), seeds=2)

	def test_a_run_that_is_busy_is_left_for_the_next_sweep(self):
		run, conv, _, seed = self._dangling_step()
		_age(seed)

		@contextmanager
		def busy(*a, **kw):
			yield False

		with patch("jarvis._redis_lock.redis_lock", busy):
			self._sweep()
		self.assertEqual(self._strikes(seed), 0, "a skipped heal used the strike")
		self.assertEqual(self._turns(conv, besides=(self._step_one,)), [])
		self._sweep()
		self._assert_step_sent_once(run, conv, 1, besides=(self._step_one,), seeds=2)

	def test_the_heal_and_the_ending_never_raise_into_the_sweep(self):
		_, conv, _, seed = self._dangling_step()
		with (
			patch.object(macros, "_next_step_index", side_effect=RuntimeError("db")),
			patch("frappe.log_error"),
		):
			self.assertEqual(macros.heal_dangling_seed(conv, seed), {"ok": True})
		with (
			patch.object(macros, "_cas_run_status", side_effect=RuntimeError("db")),
			patch("frappe.log_error"),
		):
			self.assertFalse(macros.fail_run_for_seed(conv, seed))

	def _assert_healed_the_ordinary_way(self, message, engine, why):
		engine.assert_not_called()
		turns = self._ordinary_turns(message)
		self.assertEqual(len(turns), 1, why)
		self.assertEqual(turns[0].turn_class, "background", why)
		self.assertEqual(self._strikes(message), 1, why)

	def test_a_message_someone_typed_in_a_run_chat_is_healed_the_ordinary_way(self):
		# An unarmed run's chat takes typed messages. Handed to the engine it would
		# send the run's NEXT step while the current one may still be running.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		self._turn(conv, state="streaming", reply="")  # step 1, in flight; no reply row yet
		frappe.db.delete(MSG, {"conversation": conv, "role": "assistant"})
		typed = self._msg(conv, "user", origin="human", content="and another thing")
		frappe.db.commit()
		_age(typed)
		with patch.object(macros, "_dispatch_step") as engine:
			self._sweep()
		self._assert_healed_the_ordinary_way(typed, engine, "typed")
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 1))

	def test_a_step_message_of_a_parked_run_is_healed_the_ordinary_way(self):
		# The resume owns a parked run. A turn made for a run that is not `running`
		# has nobody listening for its end.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		self._turn(conv)
		left = self._msg(conv, "user", origin="macro", content="step 2")
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		frappe.db.commit()
		_age(left)
		with patch.object(macros, "_dispatch_step") as engine:
			self._sweep()
		self._assert_healed_the_ordinary_way(left, engine, "parked")
		self.assertNotEqual(self._ordinary_turns(left)[0].name, step_turn_id(run, 1))
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("waiting_capacity", 1))

	def test_the_summarize_message_is_healed_the_ordinary_way(self):
		# `summarize_macro` sends its prompt as a macro message too, in a chat no run owns.
		frappe.set_user(OWNER)
		conv = frappe.get_doc({"doctype": CONV, "title": f"{outcome.PFX} summarize"})
		conv.flags.ignore_permissions = True
		conv.insert()
		message = self._msg(conv.name, "user", origin="macro", content="summarize these steps")
		frappe.db.commit()
		_age(message)
		with patch.object(macros, "_dispatch_step") as engine:
			self._sweep()
		self._assert_healed_the_ordinary_way(message, engine, "summarize")

	def test_an_older_step_message_is_healed_the_ordinary_way(self):
		run, conv, macro = self._mk_run(steps=3, at_step=1)
		older = self._msg(conv, "user", origin="macro", content="left behind")
		frappe.db.commit()
		self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_SENT)
		self.assertNotEqual(self._seeds(conv)[-1], older, "premise: the run wrote a newer step message")
		_age(older)
		with patch.object(macros, "_dispatch_step") as engine:
			self._sweep()
		self._assert_healed_the_ordinary_way(older, engine, "older")

	def test_an_older_step_message_out_of_strikes_does_not_end_the_run(self):
		# The second strike ends a run only for the step the run is waiting on.
		run, conv, macro = self._mk_run(steps=3, at_step=1)
		older = self._msg(conv, "user", origin="macro", content="left behind")
		frappe.db.set_value(MSG, older, "was_recovered", 1, update_modified=False)
		frappe.db.commit()
		self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 1), macros._STEP_SENT)
		_age(older)
		self._sweep()
		self.assertEqual(self._ordinary_turns(older), [])
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 2))

	def test_a_message_that_is_not_the_next_steps_prompt_is_healed_the_ordinary_way(self):
		# A step sent while the site wrote no Turn rows, found after the switch to the
		# turn machine: it is the step BEFORE the one the run would send next.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		earlier = self._msg(conv, "user", origin="macro", content="step 1")
		frappe.db.commit()
		_age(earlier)
		with patch.object(macros, "_dispatch_step") as engine:
			self._sweep()
		self._assert_healed_the_ordinary_way(earlier, engine, "not the next prompt")
		self.assertEqual(self._run(run).current_step, 1)

	def test_without_turn_rows_the_sweep_is_the_only_healer_as_before(self):
		self._set_mode("legacy")
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step = self._msg(conv, "user", origin="macro", content="step 2")
		frappe.db.commit()
		_age(step)
		with (
			patch.object(macros, "_dispatch_step") as engine,
			patch.object(macros, "heal_dangling_seed") as heal,
		):
			self._sweep()
		engine.assert_not_called()
		heal.assert_not_called()
		self.assertEqual([job["message_id"] for job in self._jobs], [step])
		self.assertEqual(self._strikes(step), 1)


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

	def test_without_turn_rows_a_summarized_run_is_sent_again_as_the_summary(self):
		# No Turn rows, so the cursor is all there is, and a cursor left behind sends
		# the turn again as it always did. What it sends is the summary.
		self._set_mode("legacy")
		run, conv, macro = self._summarized()
		self.assertEqual(macros._dispatch_step(*self._docs(run, macro), 0), macros._STEP_SENT)
		frappe.db.set_value(RUN, run, "current_step", 0, update_modified=False)
		frappe.db.commit()
		seed = self._seeds(conv)[-1]
		self._msg(conv, "assistant", content="did it")
		frappe.db.commit()
		macros.advance_after_turn(conv, errored=False, seed_message=seed)
		self.assertEqual(self._prompts(conv), ["do all three", "do all three"])

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
