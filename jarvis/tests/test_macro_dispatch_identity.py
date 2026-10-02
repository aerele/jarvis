"""A macro step is sent once, whoever sends it and however often they try.

From the 2026-10-01 macros review: the run's cursor was written after the step's turn
existed, so anything between the two (a raise, a lapsed run lock, a second healer for
a message with no turn) sent the same step again (issue #422). The step's turn now has
a fixed id, ``sha1("<run>:<index>")[:12]``, and every dispatcher goes through one
function that lands on it.

These tests drive the REAL dispatch (``api._enqueue_turn`` and the admission gate, on
a pump shard of their own with the wake stubbed), not a stand-in for it.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from jarvis.chat import admission, macros, pump, stale_scan
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


def step_turn_id(run: str, index: int) -> str:
	"""The id the design gives the turn of step ``index`` (0-based) of ``run``. Spelled
	out here, not imported: the tests pin the formula."""
	return hashlib.sha1(f"{run}:{index}".encode()).hexdigest()[:12]


class IdentityBase(outcome.MacroRunOutcomeBase):
	"""Macro runs on a pump shard of this test's own: the accept gate is the real one
	(it reads the shard row), a turn it accepts stays ``queued``, and nothing wakes a
	pump to run it."""

	def setUp(self):
		super().setUp()
		self._target = f"{TARGET_PREFIX}{frappe.generate_hash(length=10)}"
		ts._ensure_control_row(self._target)
		self._set_row_mode(pump._MODE_PUMP)
		for target in (
			patch.object(pump, "pump_mode_active", return_value=True),
			patch.object(pump, "pump_configured", return_value=True),
			patch.object(pump, "ensure_pump", lambda *a, **k: {"enqueued": False}),
			patch.object(pump, "lpush_wake", lambda *a, **k: None),
			patch.object(admission, "relay_target_id", lambda conversation=None: self._target),
			# What follows a cancelled turn is site-wide; these tests share one site.
			patch.object(admission, "promote_next"),
		):
			target.start()
			self.addCleanup(target.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.delete(TURN, {"relay_target_id": self._target})
		frappe.db.delete(PUMP, {"relay_target_id": ["like", f"{TARGET_PREFIX}%"]})
		frappe.db.commit()
		pump._LIFECYCLE_MODE_CACHE.pop(self._target, None)
		super().tearDown()

	def _set_row_mode(self, mode):
		frappe.db.set_value(PUMP, self._target, "transport_mode", mode, update_modified=False)
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

	def _as(self, who):
		frappe.set_user("Administrator")
		frappe.set_user(who if who == "Administrator" else ensure_user(who))

	def _seeds(self, conv):
		"""The chat's step messages, oldest first."""
		return [m.name for m in macros._macro_seeds(conv)]

	def _turns(self, conv, *, besides=()):
		"""Every Turn in the chat, as ``(name, seed_message)``, without ``besides``."""
		rows = frappe.get_all(
			TURN, filters={"conversation": conv}, fields=["name", "seed_message"], order_by="creation asc"
		)
		return [(r.name, r.seed_message) for r in rows if r.name not in besides]

	@contextmanager
	def _locks_always_granted(self):
		"""A run lock that lapsed under a slow holder: both dispatchers get in."""

		@contextmanager
		def granted(*a, **kw):
			yield True

		with patch("jarvis._redis_lock.redis_lock", granted):
			yield

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
				mine, user = frappe.local.db, frappe.session.user
				with other_connection() as other:
					frappe.local.db = other
					try:
						second()
						other.commit()
					finally:
						frappe.local.db = mine
						frappe.set_user(user)
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
