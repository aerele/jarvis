"""Stopping a macro run stops it, however the stop arrives.

From the 2026-10-01 macros review:

* Stop could lose a race with a step being sent: the step went out after the run
  read ``stopped`` (issue #421), and a step waiting in the queue ran anyway;
* deleting a macro mid-run removed the run row and left the run going, in a chat
  that stayed armed;
* archiving the run's chat, or clearing chat history, left the run ``running``
  until the stale-run sweep.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import frappe

from jarvis.chat import admission, error_taxonomy, macros
from jarvis.tests import test_macro_run_outcome as outcome
from jarvis.tests._pending_action_helpers import ensure_user

CONV = outcome.CONV
MACRO = outcome.MACRO
RUN = outcome.RUN
MSG = outcome.MSG
TURN = outcome.TURN
OWNER = outcome.OWNER
BYSTANDER = "macro-stop-bystander@example.com"
DELETED = "The macro was deleted."


class StopBase(outcome.MacroRunOutcomeBase):
	"""Runs with real Turn rows in the states the turn machine leaves them in.

	What follows a won cancel (waking the pump, promoting the next queued turn) is
	site-wide and is pinned off: these tests share one site with every other suite."""

	def setUp(self):
		super().setUp()
		for target in (
			patch("jarvis.chat.pump.pump_lifecycle_configured", return_value=False),
			patch("jarvis.chat.admission.promote_next"),
		):
			target.start()
			self.addCleanup(target.stop)

	def _step(self, conv, state, *, placeholder=False):
		"""The current step as ``_enqueue_turn`` and the pump leave it: the macro's
		user row and its Turn, plus the reply placeholder once prepare attached one.
		Returns ``(run_id, seed_message, assistant_message)``."""
		seed = self._msg(conv, "user", origin="macro", content="prompt")
		assistant = self._msg(conv, "assistant") if placeholder else None
		if assistant:
			frappe.db.set_value(MSG, assistant, "streaming", 1, update_modified=False)
		run_id = frappe.generate_hash(length=12)
		turn = frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": run_id,
				"conversation": conv,
				"relay_target_id": admission.DEFAULT_RELAY_TARGET,
				"turn_class": "background",
				"state": state,
				"version": 0 if state == "queued" else 2,
				"seed_message": seed,
				"assistant_message": assistant,
				"enqueued_at": frappe.utils.now(),
			}
		)
		turn.flags.ignore_permissions = True
		turn.insert()
		frappe.db.commit()
		return run_id, seed, assistant

	def _turn_state(self, run_id):
		return frappe.db.get_value(TURN, run_id, "state")

	def _markers(self, conv):
		return frappe.get_all(
			MSG,
			filters={"conversation": conv, "role": "assistant", "error": macros._STOPPED_BEFORE_START},
			pluck="name",
		)

	def _closing(self, conv, run):
		return frappe.get_all(
			MSG,
			filters={"conversation": conv, "role": "assistant", "ref_doctype": RUN, "ref_name": run},
			fields=["name", "content", "seq"],
		)

	def _live_turns(self, conv):
		return frappe.get_all(
			TURN, filters={"conversation": conv, "state": ["not in", macros._TURN_ENDED]}, pluck="name"
		)

	def _queueing(self, conv, *, before=None):
		"""Stand-in for ``api._enqueue_turn`` on the pump path: the step's message and
		a ``queued`` Turn, which is all an accepted turn is until the pump promotes it.
		``before`` runs first, where another request can land in a real dispatch."""

		def enqueue(conversation, prompt, **kw):
			if before:
				before()
			user = frappe.session.user  # the fixture helpers switch to the owner
			run_id, seed, _ = self._step(conv, "queued")
			frappe.set_user(user)
			return {"run_id": run_id, "message_id": seed, "queued": True}

		return patch("jarvis.chat.api._enqueue_turn", side_effect=enqueue)

	def _as(self, who):
		frappe.set_user("Administrator")
		frappe.set_user(who if who == "Administrator" else ensure_user(who))


# --------------------------------------------------------------------------- #
# One Stop
# --------------------------------------------------------------------------- #
class TestStoppingARun(StopBase):
	def test_the_owners_stop_ends_the_run_and_says_nothing_in_the_chat(self):
		# Their own act: no reason is stored and no closing line is posted. A `stopped`
		# run that carries a reason is one the ENGINE stopped.
		run, conv, macro = self._mk_run(steps=2, at_step=1, armed=True)
		frappe.set_user(OWNER)
		with patch("jarvis.chat.macros.publish_to_user") as publish:
			self.assertEqual(macros.stop_macro_run(run), {"ok": True})
		row = self._run(run)
		self.assertEqual((row.status, row.error or ""), ("stopped", ""))
		self.assertEqual(self._closing(conv, run), [])
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)
		self.assertTrue(frappe.db.get_value(RUN, run, "finished_at"))
		(done,) = [c.args[1] for c in publish.call_args_list if c.args[1].get("kind") == "macro:done"]
		self.assertEqual((done["macro_run"], done["status"], done["error"]), (run, "stopped", ""))

	def test_only_the_owner_can_press_stop(self):
		run, _, _ = self._mk_run()
		self._as(BYSTANDER)
		with self.assertRaises(frappe.PermissionError):
			macros.stop_macro_run(run)
		self.assertEqual(self._run(run).status, "running")

	def test_a_stop_with_a_reason_records_it_and_closes_the_chat(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1, armed=True)
		with patch("jarvis.chat.macros.publish_to_user") as publish:
			self.assertTrue(macros._stop_run(run, reason=DELETED, by=OWNER))
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("stopped", DELETED))
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)
		(closing,) = self._closing(conv, run)
		self.assertEqual(closing.content, f"■ {DELETED}")
		(done,) = [c.args[1] for c in publish.call_args_list if c.args[1].get("kind") == "macro:done"]
		self.assertEqual((done["status"], done["error"]), ("stopped", DELETED))

	def test_a_run_waiting_for_capacity_is_stopped_too(self):
		# Parked is live: the capacity resume would keep re-attempting it.
		run, conv, _ = self._mk_run(steps=2, at_step=0)
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		frappe.db.commit()
		self.assertTrue(macros._stop_run(run, reason=DELETED, by="Administrator"))
		self.assertEqual(self._run(run).status, "stopped")
		with patch("jarvis.chat.api._enqueue_turn") as enqueue:
			macros.resume_waiting_capacity_runs()
		enqueue.assert_not_called()

	def test_a_run_that_already_ended_keeps_its_outcome(self):
		# The compare-and-set: a Stop that arrives after the run finished must not
		# overwrite how it finished, or say anything more in its chat.
		for status in ("completed", "failed", "stopped"):
			run, conv, _ = self._mk_run(tag=f"ended-{status}")
			frappe.db.set_value(RUN, run, {"status": status, "error": "as it ended"})
			frappe.db.commit()
			with patch("jarvis.chat.macros.publish_to_user") as publish:
				self.assertFalse(macros._stop_run(run, reason=DELETED, by=OWNER))
			row = self._run(run)
			self.assertEqual((row.status, row.error), (status, "as it ended"))
			self.assertEqual(self._closing(conv, run), [])
			publish.assert_not_called()

	def test_stop_does_not_overwrite_an_outcome_written_while_it_waited(self):
		# Stop read `running`, then the hook ended the run. The write is fenced on the
		# status Stop read, so the hook's outcome stands.
		run, _, _ = self._mk_run()
		real = macros._cas_run_status

		def hook_wins(run_name, expect, new, **extra):
			frappe.db.set_value(RUN, run_name, {"status": "failed", "error": "Step 1 failed."})
			return real(run_name, expect, new, **extra)

		with patch.object(macros, "_cas_run_status", side_effect=hook_wins):
			self.assertFalse(macros._stop_run(run, reason=DELETED, by=OWNER))
		self.assertEqual((self._run(run).status, self._run(run).error), ("failed", "Step 1 failed."))

	def test_a_stop_that_cannot_reach_the_chat_still_stops_the_run(self):
		run, _, _ = self._mk_run()
		with (
			patch.object(macros, "_post_closing_message", side_effect=RuntimeError("db")),
			patch.object(macros, "_cancel_current_step", side_effect=RuntimeError("db")),
			patch("frappe.log_error"),
		):
			self.assertTrue(macros._stop_run(run, reason=DELETED, by=OWNER))
		self.assertEqual(self._run(run).status, "stopped")


# --------------------------------------------------------------------------- #
# Stop cancels the step that has not started
# --------------------------------------------------------------------------- #
class TestStopCancelsAStepThatHasNotStarted(StopBase):
	def _stopped_by(self, who, run):
		"""Stop as the owner would (the button), or as the engine does for anyone else."""
		self._as(who)
		if who == OWNER:
			macros.stop_macro_run(run)
		else:
			self.assertTrue(macros._stop_run(run, reason=DELETED, by=who))
		frappe.set_user("Administrator")

	def test_a_queued_step_is_cancelled_whoever_stops_the_run(self):
		# The owner's own cancel endpoint is owner-gated. The engine's is not: a run is
		# also stopped by a cron and by someone deleting the macro.
		for who in (OWNER, "Administrator", BYSTANDER):
			run, conv, _ = self._mk_run(steps=2, at_step=1, tag=f"queued-{who[:5]}")
			turn, _, _ = self._step(conv, "queued")
			self._stopped_by(who, run)
			self.assertEqual(self._turn_state(turn), "cancelled", who)
			self.assertEqual(len(self._markers(conv)), 1, who)
			self.assertEqual(self._live_turns(conv), [], who)

	def test_a_step_being_prepared_is_cancelled_and_its_spinner_cleared(self):
		for who in (OWNER, "Administrator", BYSTANDER):
			for state in ("preparing", "ready"):
				run, conv, _ = self._mk_run(steps=2, at_step=1, tag=f"{state}-{who[:5]}")
				turn, _, placeholder = self._step(conv, state, placeholder=True)
				self._stopped_by(who, run)
				self.assertEqual(self._turn_state(turn), "cancelled", (who, state))
				self.assertEqual(frappe.db.get_value(MSG, placeholder, "streaming"), 0)
				self.assertEqual(frappe.db.get_value(TURN, turn, "reserved"), 0)

	def test_a_step_already_with_the_agent_finishes(self):
		# Not in scope: aborting an in-flight turn. It ends on its own and moves nothing.
		for state in ("dispatching", "streaming", "finalizing"):
			run, conv, _ = self._mk_run(steps=2, at_step=1, tag=f"inflight-{state}")
			turn, _, _ = self._step(conv, state, placeholder=True)
			self.assertTrue(macros._stop_run(run, reason=DELETED, by="Administrator"))
			self.assertEqual(self._turn_state(turn), state)
			self.assertEqual(self._markers(conv), [])

	def test_only_the_runs_own_step_is_cancelled(self):
		# An unarmed run's chat takes typed messages. One waiting in the queue is the
		# user's, not the run's.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step, _, _ = self._step(conv, "streaming", placeholder=True)
		typed_seed = self._msg(conv, "user", origin="human", content="and another thing")
		typed = frappe.generate_hash(length=12)
		frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": typed,
				"conversation": conv,
				"relay_target_id": admission.DEFAULT_RELAY_TARGET,
				"turn_class": "interactive",
				"state": "queued",
				"seed_message": typed_seed,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		macros._stop_run(run, reason=DELETED, by="Administrator")
		self.assertEqual((self._turn_state(step), self._turn_state(typed)), ("streaming", "queued"))

	def test_the_pump_promoting_the_step_mid_cancel_does_not_save_it(self):
		# Read `queued`, the pump promotes to `preparing`, the cancel matches no row.
		# The step has still not started: the helper reads again and takes the other
		# route, once.
		from jarvis.chat import turn_state

		run, conv, _ = self._mk_run(steps=2, at_step=1)
		turn, _, _ = self._step(conv, "queued")
		real = turn_state.cancel_queued

		def promoted_first(run_id, version):
			frappe.db.sql(
				f"UPDATE `tab{TURN}` SET state='preparing', version=version+1 WHERE name=%s", run_id
			)
			return real(run_id, version)

		with patch.object(turn_state, "cancel_queued", side_effect=promoted_first) as lost:
			self.assertTrue(macros._cancel_undispatched_turn(turn, macros._STOPPED_BEFORE_START))
		self.assertEqual(lost.call_count, 1)
		self.assertEqual(self._turn_state(turn), "cancelled")

	def test_the_cancel_never_raises(self):
		self.assertFalse(macros._cancel_undispatched_turn("no-such-turn", "x"))
		_, conv, _ = self._mk_run()
		turn, _, _ = self._step(conv, "queued")
		for who in ("Administrator", BYSTANDER):
			self._as(who)
			with (
				patch.object(admission, "_cancel_pre_dispatch", side_effect=RuntimeError("db")),
				patch("frappe.log_error") as log,
			):
				self.assertFalse(macros._cancel_undispatched_turn(turn, "x"))
			self.assertEqual(log.call_count, 1)
		frappe.set_user("Administrator")
		self.assertEqual(self._turn_state(turn), "queued")

	def test_the_owners_own_cancel_still_refuses_anyone_else(self):
		# The shared internals take the gate as an argument; the endpoint still passes it.
		_, conv, _ = self._mk_run()
		turn, _, _ = self._step(conv, "queued")
		self._as(BYSTANDER)
		with self.assertRaises(frappe.PermissionError):
			admission.cancel_queued_turn(turn)
		frappe.set_user("Administrator")
		self.assertEqual(self._turn_state(turn), "queued")
		frappe.set_user(OWNER)
		self.assertEqual(admission.cancel_queued_turn(turn)["path"], "queued")
		markers = frappe.get_all(MSG, filters={"conversation": conv, "error": admission.USER_CANCEL_REASON})
		self.assertEqual(len(markers), 1)

	def test_the_marker_reads_as_cancelled_not_as_an_error(self):
		# The clients show an error with a Retry button unless the text classifies as
		# `cancelled`. Retry would run the stopped step as a plain turn.
		self.assertEqual(error_taxonomy.classify_error_text(macros._STOPPED_BEFORE_START), "cancelled")


# --------------------------------------------------------------------------- #
# The fence: a step sent while the run was being stopped (#421)
# --------------------------------------------------------------------------- #
class TestNoStepStartsAfterStop(StopBase):
	@contextmanager
	def _stop_cannot_wait(self):
		"""The dispatcher holds the run lock while it sends. A Stop that lands then
		waits its limit and goes ahead without the lock; shortened here."""
		with patch.object(macros, "_STOP_LOCK_BLOCK_S", 0.05):
			yield

	def _progress(self, publish):
		return [c.args[1] for c in publish.call_args_list if c.args[1].get("kind") == "macro:progress"]

	def test_stop_landing_after_the_step_was_queued_cancels_it(self):
		for who in ("Administrator", BYSTANDER):
			run, conv, _ = self._mk_run(steps=3, at_step=1, tag=f"after-{who[:5]}")
			step_turn, _, _ = self._turn(conv)
			with self._queueing(conv):
				macros.advance_after_turn(conv, errored=False, run_id=step_turn)
			self.assertEqual(self._run(run).current_step, 2)
			(queued,) = self._live_turns(conv)
			self._as(who)
			self.assertTrue(macros._stop_run(run, reason=DELETED, by=who))
			frappe.set_user("Administrator")
			self.assertEqual(self._turn_state(queued), "cancelled", who)
			self.assertEqual(self._live_turns(conv), [], who)

	def test_stop_landing_while_the_step_is_being_sent_cancels_it(self):
		# The dispatcher read `running`; Stop wrote `stopped`; the step went out anyway
		# and ran. Now the dispatcher looks again once the turn exists.
		for who in ("Administrator", BYSTANDER):
			run, conv, _ = self._mk_run(steps=3, at_step=1, tag=f"during-{who[:5]}")
			step_turn, _, _ = self._turn(conv)

			def stop(run=run, who=who):
				macros._stop_run(run, reason=DELETED, by=who)

			self._as(who)  # the dispatcher too: its cancel must not need the owner
			with (
				self._stop_cannot_wait(),
				self._queueing(conv, before=stop),
				patch("jarvis.chat.macros.publish_to_user") as publish,
			):
				macros.advance_after_turn(conv, errored=False, run_id=step_turn)
			frappe.set_user("Administrator")
			row = self._run(run)
			self.assertEqual((row.status, row.error), ("stopped", DELETED), who)
			self.assertEqual(row.current_step, 1, "the cursor moved to a step that was cancelled")
			self.assertEqual(self._live_turns(conv), [], who)
			self.assertEqual(len(self._markers(conv)), 1, who)
			self.assertEqual(self._progress(publish), [])

	def test_the_first_step_of_a_run_stopped_at_once_is_cancelled(self):
		# run_macro sends step 1 outside the run lock, after the run row is committed.
		frappe.set_user(OWNER)
		macro = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{outcome.PFX}-first",
				"steps": [{"prompt": "a"}, {"prompt": "b"}],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		started = {}

		def enqueue(conversation, prompt, **kw):
			started["conv"] = conversation
			run = frappe.db.get_value(RUN, {"conversation": conversation}, "name")
			macros.stop_macro_run(run)
			run_id, seed, _ = self._step(conversation, "queued")
			return {"run_id": run_id, "message_id": seed, "queued": True}

		with patch("jarvis.chat.api._enqueue_turn", side_effect=enqueue):
			out = macros.run_macro(macro.name)
		row = self._run(out["data"]["macro_run"])
		self.assertEqual((row.status, row.current_step), ("stopped", 0))
		self.assertEqual(self._live_turns(started["conv"]), [])

	def test_a_summarized_run_is_fenced_the_same_way(self):
		run, conv, macro = self._mk_run(steps=2, at_step=0)
		frappe.db.set_value(RUN, run, {"run_mode": "merged", "total_steps": 1})
		frappe.db.commit()
		run_doc, macro_doc = frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)
		with self._queueing(conv, before=lambda: macros._stop_run(run, reason=DELETED, by=OWNER)):
			self.assertFalse(macros._run_merged(run_doc, macro_doc, "all of it"))
		self.assertEqual(self._run(run).current_step, 0)
		self.assertEqual(self._live_turns(conv), [])

	def test_a_run_that_failed_or_completed_meanwhile_is_fenced_too(self):
		for status in ("failed", "completed"):
			run, conv, macro = self._mk_run(steps=2, at_step=0, tag=f"ended-{status}")
			run_doc, macro_doc = frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)

			def ended(run=run, status=status):
				frappe.db.set_value(RUN, run, "status", status)
				frappe.db.commit()

			with self._queueing(conv, before=ended):
				self.assertFalse(macros._run_step(run_doc, macro_doc, 0))
			self.assertEqual(self._run(run).current_step, 0, status)
			self.assertEqual(self._live_turns(conv), [], status)

	def test_a_run_parked_for_capacity_is_not_ended(self):
		# `waiting_capacity` is live. Cancelling its turn would leave the step marked
		# as sent with nothing to run it.
		run, conv, macro = self._mk_run(steps=2, at_step=0)
		run_doc, macro_doc = frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)

		def parked():
			frappe.db.set_value(RUN, run, "status", "waiting_capacity")
			frappe.db.commit()

		with self._queueing(conv, before=parked):
			self.assertTrue(macros._run_step(run_doc, macro_doc, 0))
		self.assertEqual(self._run(run).current_step, 1)
		self.assertEqual(len(self._live_turns(conv)), 1)

	def test_a_step_sent_to_a_live_run_is_left_alone(self):
		run, conv, macro = self._mk_run(steps=2, at_step=0)
		run_doc, macro_doc = frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)
		with self._queueing(conv):
			self.assertTrue(macros._run_step(run_doc, macro_doc, 0))
		self.assertEqual(self._run(run).current_step, 1)
		self.assertEqual(len(self._live_turns(conv)), 1)
		self.assertEqual(self._markers(conv), [])
