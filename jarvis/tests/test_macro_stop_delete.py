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

from jarvis.chat import admission, error_taxonomy, macros, macros_api
from jarvis.chat import api as chat_api
from jarvis.tests import test_macro_run_outcome as outcome
from jarvis.tests._pending_action_helpers import ensure_user
from jarvis.tests.race_harness import (
	assert_fix_closes_race,
	open_read_view,
	other_connection,
	snapshot_isolation_on,
)

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


# --------------------------------------------------------------------------- #
# Deleting a macro stops its runs first
# --------------------------------------------------------------------------- #
class TestDeletingAMacroStopsItsRuns(StopBase):
	def _start_run_elsewhere(self, macro, conv):
		"""A run of ``macro`` committed from another connection, the way a second
		request (or the scheduler) starts one while this request is busy deleting."""
		name = frappe.generate_hash(length=10)
		now = frappe.utils.now()
		with other_connection() as other:
			other.sql(
				f"""INSERT INTO `tab{RUN}`
					(name, creation, modified, owner, modified_by, docstatus, macro, conversation,
					 status, current_step, total_steps, run_mode, `trigger`)
				VALUES (%s, %s, %s, %s, %s, 0, %s, %s, 'running', 1, 2, 'stepped', 'manual')""",
				(name, now, now, OWNER, OWNER, macro, conv),
			)
			other.commit()
		return name

	def _armed_chat(self):
		frappe.set_user(OWNER)
		conv = frappe.get_doc({"doctype": CONV, "title": f"{outcome.PFX} second run"})
		conv.flags.ignore_permissions = True
		conv.insert()
		frappe.db.set_value(CONV, conv.name, "skip_confirmation", 1, update_modified=False)
		frappe.db.commit()
		return conv.name

	def test_a_running_armed_macro_is_stopped_then_deleted(self):
		# The run row used to be deleted from under a run that kept going: its queued
		# step ran, and nothing was left to disarm the chat.
		run, conv, macro = self._mk_run(steps=3, at_step=1, armed=True)
		self._turn(conv)  # step 1, done
		frappe.db.set_value(RUN, run, "current_step", 2)
		queued, _, _ = self._step(conv, "queued")  # step 2, waiting to start
		frappe.set_user(OWNER)
		self.assertEqual(macros_api.delete_macro(macro), {"ok": True, "stopped_runs": 1})
		self.assertFalse(frappe.db.exists(MACRO, macro))
		self.assertFalse(frappe.db.exists(RUN, run))
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)
		(closing,) = self._closing(conv, run)
		self.assertEqual(closing.content, "■ The macro was deleted.")
		self.assertEqual(self._turn_state(queued), "cancelled")
		self.assertEqual(self._live_turns(conv), [])
		# Nothing later either: a turn ending in that chat finds no run to move.
		with patch("jarvis.chat.api._enqueue_turn") as enqueue:
			macros.advance_after_turn(conv, errored=False)
		enqueue.assert_not_called()

	def test_a_parked_run_is_stopped_too(self):
		run, _, macro = self._mk_run(steps=2, at_step=0)
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		frappe.db.commit()
		frappe.set_user(OWNER)
		self.assertEqual(macros_api.delete_macro(macro)["stopped_runs"], 1)
		with patch("jarvis.chat.api._enqueue_turn") as enqueue:
			macros.resume_waiting_capacity_runs()
		enqueue.assert_not_called()

	def test_a_macro_with_no_live_run_is_deleted_and_stops_nothing(self):
		run, conv, macro = self._mk_run(tag="finished")
		frappe.db.set_value(RUN, run, "status", "completed")
		_, _, never_ran = self._mk_run(tag="bare")
		frappe.db.delete(RUN, {"macro": never_ran})
		frappe.db.commit()
		frappe.set_user(OWNER)
		with patch.object(macros, "_stop_run") as stop:
			self.assertEqual(macros_api.delete_macro(macro), {"ok": True, "stopped_runs": 0})
			self.assertEqual(macros_api.delete_macro(never_ran), {"ok": True, "stopped_runs": 0})
		stop.assert_not_called()
		self.assertEqual(self._closing(conv, run), [])
		self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 0)

	def test_a_run_that_starts_between_the_stop_and_the_delete_is_stopped_first(self):
		# Every stop commits, so no lock spans "stop, then delete". A run that starts
		# in that gap is found by the look under the macro's row lock, and the delete
		# goes round again. Two real connections.
		first, _, macro = self._mk_run(steps=2, at_step=1, armed=True)
		second_chat = self._armed_chat()
		started = []
		real = macros._lock_macro_row

		def a_run_starts_then_lock(name):
			if not started:
				started.append(self._start_run_elsewhere(macro, second_chat))
			return real(name)

		frappe.set_user(OWNER)
		with (
			snapshot_isolation_on(),
			patch.object(macros, "_lock_macro_row", side_effect=a_run_starts_then_lock),
		):
			out = macros_api.delete_macro(macro)
		self.assertEqual(out, {"ok": True, "stopped_runs": 2})
		self.assertFalse(frappe.db.exists(MACRO, macro))
		self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 0)
		# The proof it was stopped, not just deleted: its chat is disarmed and closed.
		self.assertEqual(frappe.db.get_value(CONV, second_chat, "skip_confirmation"), 0)
		(closing,) = self._closing(second_chat, started[0])
		self.assertEqual(closing.content, "■ The macro was deleted.")

	def test_a_macro_that_keeps_starting_runs_is_not_deleted(self):
		first, _, macro = self._mk_run(steps=2, at_step=1)
		started = []
		real = macros._lock_macro_row

		def a_run_starts_every_time(name):
			started.append(self._start_run_elsewhere(macro, self._armed_chat()))
			frappe.set_user(OWNER)
			return real(name)

		frappe.set_user(OWNER)
		with (
			patch.object(macros, "_lock_macro_row", side_effect=a_run_starts_every_time),
			self.assertRaises(macros_api.MacroBusyError) as refused,
		):
			macros_api.delete_macro(macro)
		self.assertIn("This macro is running; try again", str(refused.exception))
		self.assertEqual(len(started), macros_api._DELETE_STOP_ROUNDS)
		self.assertTrue(frappe.db.exists(MACRO, macro))
		# No run row was deleted, and the one still live is untouched.
		statuses = dict(
			frappe.get_all(RUN, filters={"macro": macro}, fields=["name", "status"], as_list=True)
		)
		self.assertEqual(len(statuses), 1 + len(started))
		self.assertEqual(statuses[started[-1]], "running")
		self.assertEqual({statuses[r] for r in [first, *started[:-1]]}, {"stopped"})

	def test_the_macros_row_is_released_before_another_round_of_stops(self):
		# Lock order: the macro row first, a run lock second, never the row held
		# while waiting for a run lock.
		_, _, macro = self._mk_run(steps=2, at_step=1)
		real = macros._lock_macro_row
		started = []

		def a_run_starts_then_lock(name):
			if not started:
				started.append(self._start_run_elsewhere(macro, self._armed_chat()))
				frappe.set_user(OWNER)
			return real(name)

		def the_row_is_free(run_name, **kw):
			# Another connection can take the macro row at once: this one let go.
			with other_connection() as other:
				other.sql("SET SESSION innodb_lock_wait_timeout=1")
				other.sql(f"SELECT name FROM `tab{MACRO}` WHERE name=%s FOR UPDATE", macro)
			return real_stop(run_name, **kw)

		real_stop = macros._stop_run
		frappe.set_user(OWNER)
		with (
			patch.object(macros, "_lock_macro_row", side_effect=a_run_starts_then_lock),
			patch.object(macros, "_stop_run", side_effect=the_row_is_free) as stop,
		):
			self.assertEqual(macros_api.delete_macro(macro)["stopped_runs"], 2)
		self.assertEqual(stop.call_count, 2)

	def test_someone_else_cannot_stop_a_run_by_trying_to_delete_its_macro(self):
		run, _, macro = self._mk_run()
		self._as(BYSTANDER)
		with self.assertRaises(frappe.PermissionError):
			macros_api.delete_macro(macro)
		frappe.set_user("Administrator")
		self.assertEqual(self._run(run).status, "running")

	def test_a_bulk_delete_goes_the_same_way_and_adds_up_the_stops(self):
		live, live_chat, running = self._mk_run(tag="bulk-live", armed=True)
		done, _, idle = self._mk_run(tag="bulk-idle")
		frappe.db.set_value(RUN, done, "status", "completed")
		frappe.db.commit()
		frappe.set_user(OWNER)
		out = macros_api.delete_macros_bulk([running, idle])
		self.assertEqual(out, {"deleted": 2, "skipped": [], "stopped_runs": 1})
		self.assertEqual(frappe.db.get_value(CONV, live_chat, "skip_confirmation"), 0)
		self.assertEqual(len(self._closing(live_chat, live)), 1)

	def test_a_bulk_delete_says_which_macro_was_still_running(self):
		_, _, busy = self._mk_run(tag="bulk-busy")
		frappe.set_user(OWNER)
		with (
			patch.object(macros_api, "delete_macro", side_effect=macros_api.MacroBusyError("busy")),
			patch("frappe.log_error") as log,
		):
			out = macros_api.delete_macros_bulk([busy])
		self.assertEqual(out["skipped"], [{"name": busy, "reason": "running"}])
		log.assert_not_called()


# --------------------------------------------------------------------------- #
# Starting a run and deleting the macro are one at a time
# --------------------------------------------------------------------------- #
class TestRunStartAndDeleteTakeTurns(StopBase):
	def _macro(self, tag, *, armed=False):
		frappe.set_user(OWNER)
		macro = frappe.get_doc(
			{"doctype": MACRO, "macro_name": f"{outcome.PFX}-{tag}", "steps": [{"prompt": "a"}]}
		).insert(ignore_permissions=True)
		if armed:  # arming is an administrator's act; the fixture writes it directly
			frappe.db.set_value(MACRO, macro.name, "skip_confirmation", 1, update_modified=False)
		frappe.db.commit()
		return macro.name

	def _delete_elsewhere(self, macro):
		with other_connection() as other:
			other.sql("DELETE FROM `tabJarvis Macro Step` WHERE parent=%s", macro)
			other.sql(f"DELETE FROM `tab{MACRO}` WHERE name=%s", macro)
			other.commit()

	def _chats(self):
		return frappe.db.count(CONV, {"owner": OWNER})

	def test_both_wait_for_the_macros_row(self):
		# Another connection holds the row. A run start waits for it BEFORE it creates
		# anything, and a delete does not go ahead either.
		macro = self._macro("held")
		frappe.set_user(OWNER)
		previous = frappe.db.sql("SELECT @@session.innodb_lock_wait_timeout")[0][0]
		with other_connection() as holder:
			holder.sql(f"SELECT name FROM `tab{MACRO}` WHERE name=%s FOR UPDATE", macro)
			try:
				for blocked in (lambda: macros.run_macro(macro), lambda: macros_api.delete_macro(macro)):
					frappe.db.commit()
					frappe.db.sql("SET SESSION innodb_lock_wait_timeout=1")
					with (
						patch("jarvis.chat.api._enqueue_turn") as enqueue,
						self.assertRaises(frappe.QueryTimeoutError),
					):
						blocked()
					# Read inside the transaction that timed out: a lock wait timeout
					# undoes the one statement, so a chat or a run row inserted ahead
					# of the lock would still be here.
					self.assertEqual(self._chats(), 0, "a chat was created before the macro row was locked")
					self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 0)
					frappe.db.rollback()
					enqueue.assert_not_called()
			finally:
				frappe.db.rollback()
				frappe.db.sql(f"SET SESSION innodb_lock_wait_timeout={int(previous)}")
		self.assertTrue(frappe.db.exists(MACRO, macro))

	def test_a_run_that_lost_to_a_delete_finds_no_macro(self):
		# The request read the macro's world before the delete committed. On that
		# snapshot the macro is still there: a run would start for a macro that is gone.
		macro = self._macro("gone")
		frappe.set_user(OWNER)
		with snapshot_isolation_on():
			open_read_view()
			self.assertTrue(frappe.db.exists(MACRO, macro))  # this transaction still sees it
			self._delete_elsewhere(macro)
			with (
				patch("jarvis.chat.api._enqueue_turn") as enqueue,
				self.assertRaises(frappe.DoesNotExistError),
			):
				macros.run_macro(macro)
		frappe.db.rollback()
		enqueue.assert_not_called()
		self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 0)
		self.assertEqual(self._chats(), 0)

	def test_a_run_starts_from_the_macro_as_it_is_now(self):
		# An administrator disarmed the macro after this request's snapshot opened.
		# The run must not go out armed on what the macro used to say.
		def scenario():
			self._purge()
			macro = self._macro("disarmed", armed=True)
			frappe.set_user(OWNER)
			try:
				with snapshot_isolation_on():
					open_read_view()
					frappe.db.get_value(MACRO, macro, "skip_confirmation")
					with other_connection() as other:
						other.sql(f"UPDATE `tab{MACRO}` SET skip_confirmation=0 WHERE name=%s", macro)
						other.commit()
					with patch(
						"jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}
					):
						out = macros.run_macro(macro)
				return frappe.db.get_value(CONV, out["data"]["conversation"], "skip_confirmation")
			finally:
				frappe.db.rollback()

		assert_fix_closes_race(self, scenario, lambda armed: self.assertEqual(armed, 0))

	def test_a_refusal_lets_go_of_the_macros_row(self):
		# A scheduled run that is refused returns, it does not raise: the scheduler
		# then writes the macro row from its own bookkeeping.
		macro = self._macro("refused")
		frappe.db.set_value(MACRO, macro, "enabled", 0)
		frappe.db.commit()
		frappe.set_user(OWNER)
		self.assertEqual(macros.run_macro(macro, trigger="scheduled")["reason"], "macro disabled")
		with patch.object(macros, "entitlement_block", return_value="usage_limit"):
			frappe.db.set_value(MACRO, macro, "enabled", 1)
			frappe.db.commit()
			self.assertEqual(macros.run_macro(macro, trigger="scheduled")["reason"], "usage_limit")
			with other_connection() as other:
				other.sql("SET SESSION innodb_lock_wait_timeout=1")
				other.sql(f"SELECT name FROM `tab{MACRO}` WHERE name=%s FOR UPDATE", macro)
			frappe.db.set_value(MACRO, macro, "enabled", 0)
			frappe.db.commit()
			self.assertEqual(macros.run_macro(macro, trigger="scheduled")["reason"], "macro disabled")
			with other_connection() as other:
				other.sql("SET SESSION innodb_lock_wait_timeout=1")
				other.sql(f"SELECT name FROM `tab{MACRO}` WHERE name=%s FOR UPDATE", macro)


# --------------------------------------------------------------------------- #
# Archiving or clearing a live run's chat stops the run
# --------------------------------------------------------------------------- #
class TestTheChatGoingAwayStopsTheRun(StopBase):
	def test_archiving_the_runs_chat_stops_the_run(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1, armed=True)
		queued, _, _ = self._step(conv, "queued")
		frappe.set_user(OWNER)
		self.assertEqual(chat_api.archive_conversation(conv), {"ok": True})
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("stopped", macros._CONVERSATION_GONE_ERROR))
		self.assertEqual(frappe.db.get_value(CONV, conv, "status"), "Archived")
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)
		self.assertEqual(self._turn_state(queued), "cancelled")
		# Nobody reads an archived chat: no closing line is posted into it.
		self.assertEqual(self._closing(conv, run), [])

	def test_archiving_an_ordinary_chat_stops_nothing(self):
		_, conv, _ = self._mk_run()
		frappe.db.set_value(RUN, {"conversation": conv}, "status", "completed")
		frappe.db.commit()
		frappe.set_user(OWNER)
		with patch.object(macros, "_stop_run") as stop:
			chat_api.archive_conversation(conv)
		stop.assert_not_called()

	def test_a_stop_that_fails_does_not_fail_the_archive(self):
		run, conv, _ = self._mk_run()
		frappe.set_user(OWNER)
		with patch.object(macros, "_stop_run", side_effect=RuntimeError("redis")), patch("frappe.log_error"):
			self.assertEqual(chat_api.archive_conversation(conv), {"ok": True})
		self.assertEqual(frappe.db.get_value(CONV, conv, "status"), "Archived")

	def test_clearing_chat_history_stops_the_run_while_its_chat_still_exists(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1, armed=True)
		parked, _, _ = self._mk_run(steps=2, at_step=0, tag="parked")
		frappe.db.set_value(RUN, parked, "status", "waiting_capacity")
		self._step(conv, "queued")
		seen = {}
		real = macros._cancel_current_step

		def cancel(conversation):
			# The step to cancel is found through the chat's messages.
			seen[conversation] = frappe.db.count(MSG, {"conversation": conversation})
			result = real(conversation)
			seen["cancelled"] = seen.get("cancelled") or result
			return result

		frappe.set_user(OWNER)
		with patch.object(macros, "_cancel_current_step", side_effect=cancel):
			out = chat_api.clear_chat_history()
		self.assertEqual(out["ok"], True)
		self.assertTrue(seen[conv] > 0, "the run was stopped after its messages were deleted")
		self.assertTrue(seen["cancelled"], "the queued step was not cancelled")
		for name in (run, parked):
			row = frappe.db.get_value(RUN, name, ["status", "error", "conversation"], as_dict=True)
			self.assertEqual((row.status, row.error), ("stopped", macros._CONVERSATION_GONE_ERROR))
			self.assertFalse(row.conversation)
		self.assertFalse(frappe.db.exists(CONV, conv))
