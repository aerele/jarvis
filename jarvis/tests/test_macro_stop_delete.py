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

from jarvis.chat import admission, error_taxonomy, macro_scheduler, macros, macros_api
from jarvis.chat import api as chat_api
from jarvis.tests import test_macro_run_outcome as outcome
from jarvis.tests._pending_action_helpers import ensure_user
from jarvis.tests.race_harness import (
	assert_fix_closes_race,
	open_read_view,
	other_connection,
	snapshot_isolation_off,
	snapshot_isolation_on,
)

CONV = outcome.CONV
MACRO = outcome.MACRO
RUN = outcome.RUN
MSG = outcome.MSG
TURN = outcome.TURN
OWNER = outcome.OWNER
BYSTANDER = "macro-stop-bystander@example.com"
DELETED = macros._MACRO_DELETED_ERROR
CLOSING = "■ Stopped: its macro was being deleted."


def _cleared(*kept, deleted) -> dict:
	"""What ``clear_chat_history`` answers when nothing failed: ``kept`` are the chats
	left because a reply is in progress."""
	return {
		"ok": True,
		"deleted": deleted,
		"skipped": len(kept),
		"kept": list(kept),
		"failed": 0,
		"failed_names": [],
	}


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

	def _logged(self, log, title):
		"""The ``frappe.log_error`` calls whose title starts with ``title``, as kwargs."""
		return [c.kwargs for c in log.call_args_list if (c.kwargs.get("title") or "").startswith(title)]

	def _write_elsewhere(self, sql, *params):
		"""Commit one statement from a second connection: another request's write."""
		with other_connection() as other:
			other.sql(sql, params)
			other.commit()


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
			self._write_elsewhere(
				f"UPDATE `tab{RUN}` SET status='failed', error='Step 1 failed.' WHERE name=%s", run_name
			)
			return real(run_name, expect, new, **extra)

		with patch.object(macros, "_cas_run_status", side_effect=hook_wins):
			self.assertFalse(macros._stop_run(run, reason=DELETED, by=OWNER))
		self.assertEqual((self._run(run).status, self._run(run).error), ("failed", "Step 1 failed."))

	def test_stop_is_not_dropped_when_the_run_moves_between_two_live_states(self):
		# Stop read `running`; the step was deferred for capacity before Stop wrote. The
		# write is fenced on `running` and matches nothing, but the run is still live:
		# Stop reads again and stops it. It used to answer ok and leave the run going.
		for first, then in (("running", "waiting_capacity"), ("waiting_capacity", "running")):
			run, conv, _ = self._mk_run(steps=2, at_step=1, armed=True, tag=f"flip-{first[:4]}")
			frappe.db.set_value(RUN, run, "status", first)
			frappe.db.commit()
			real = macros._cas_run_status
			flipped = []

			def flips_once(run_name, expect, new, _then=then, _flipped=flipped, **extra):
				if not _flipped:
					_flipped.append(1)
					self._write_elsewhere(f"UPDATE `tab{RUN}` SET status=%s WHERE name=%s", _then, run_name)
				return real(run_name, expect, new, **extra)

			frappe.set_user(OWNER)
			with (
				snapshot_isolation_off(),
				patch.object(macros, "_cas_run_status", side_effect=flips_once) as cas,
				patch("jarvis.chat.macros.publish_to_user") as publish,
			):
				self.assertEqual(macros.stop_macro_run(run), {"ok": True})
			self.assertEqual(cas.call_count, 2, first)
			self.assertEqual(self._run(run).status, "stopped", first)
			self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0, first)
			self.assertEqual(
				len([c for c in publish.call_args_list if c.args[1].get("kind") == "macro:done"]), 1
			)

	def test_a_run_that_will_not_hold_still_is_reported_not_dropped(self):
		run, _, _ = self._mk_run()
		with (
			patch.object(macros, "_cas_run_status", return_value=False) as cas,
			patch("frappe.log_error") as log,
			self.assertRaises(frappe.ValidationError),
		):
			macros._stop_run(run, reason="", by=OWNER)
		self.assertEqual(cas.call_count, macros._STOP_CAS_TRIES)
		self.assertEqual(len(self._logged(log, f"jarvis.chat.macros.stop_lost: {run}")), 1)

	def test_stop_is_replayed_when_the_run_row_is_written_under_it(self):
		# The first step's cursor is written outside the run lock. With snapshot
		# isolation, a write to the run row between Stop's read and its own write
		# fails Stop's with 1020: a 500 for Stop and for a delete, and an archive that
		# swallowed it and left the run `running` and armed.
		def scenario():
			self._purge()
			run, conv, _ = self._mk_run(steps=2, at_step=0, armed=True)
			real = macros._cas_run_status
			moved = []

			def cursor_moves_first(run_name, expect, new, **extra):
				if not moved:
					moved.append(1)
					self._write_elsewhere(f"UPDATE `tab{RUN}` SET current_step=1 WHERE name=%s", run_name)
				return real(run_name, expect, new, **extra)

			try:
				with (
					snapshot_isolation_on(),
					patch.object(macros, "_cas_run_status", side_effect=cursor_moves_first),
				):
					macros._stop_run(run, reason=DELETED, by=OWNER)
			finally:
				frappe.db.rollback()
			return self._run(run).status, frappe.db.get_value(CONV, conv, "skip_confirmation")

		assert_fix_closes_race(self, scenario, lambda got: self.assertEqual(got, ("stopped", 0)))

	def test_the_chat_is_disarmed_in_the_same_commit_as_the_stop(self):
		# On an armed chat the card sweep commits whatever is pending. The status used
		# to be written first, so it was durable before the flag was cleared: a failure
		# in between left a stopped run with an armed chat that nothing disarmed again.
		from jarvis.chat import pending_confirm

		run, conv, _ = self._mk_run(steps=2, at_step=1, armed=True)
		with (
			patch.object(pending_confirm, "has_live_card", side_effect=RuntimeError("store down")),
			self.assertRaises(RuntimeError),
		):
			macros._stop_run(run, reason=DELETED, by=OWNER)
		frappe.db.rollback()  # what a failed request does
		armed = frappe.db.get_value(CONV, conv, "skip_confirmation")
		self.assertEqual((self._run(run).status, armed), ("running", 1), "stopped, and still armed")
		# Nothing was half done, so the next Stop does both.
		self.assertTrue(macros._stop_run(run, reason=DELETED, by=OWNER))
		self.assertEqual(self._run(run).status, "stopped")
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)

	def test_a_card_that_outlives_the_sweep_still_keeps_the_chat_armed(self):
		# The existing rule (fail closed), unchanged by where the disarm now sits: the
		# run is stopped, the flag stays, and it is said once.
		from jarvis.chat import pending_confirm

		run, conv, _ = self._mk_run(steps=2, at_step=1, armed=True)
		with (
			patch.object(pending_confirm, "has_live_card", return_value=True),
			patch("frappe.log_error") as log,
		):
			self.assertTrue(macros._stop_run(run, reason=DELETED, by=OWNER))
		self.assertEqual(self._run(run).status, "stopped")
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 1)
		self.assertEqual(len(self._logged(log, "jarvis.macro.disarm_withheld")), 1)

	def test_a_run_that_is_gone_is_not_an_error(self):
		# Deleted while the stop waited for its lock.
		self.assertFalse(macros._stop_run("no-such-run", reason=DELETED, by=OWNER))

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

	def test_a_cancel_that_loses_both_attempts_is_logged(self):
		# The pump moved the step on under both attempts: it runs although its run has
		# ended, and nothing else would say so. The log names the run, the turn and
		# its state, never the prompt.
		from jarvis.chat import turn_state

		run, conv, _ = self._mk_run(steps=2, at_step=1)
		turn, _, _ = self._step(conv, "queued")
		with (
			patch.object(turn_state, "cancel_queued", return_value=False) as lost,
			patch("frappe.log_error") as log,
		):
			self.assertTrue(macros._stop_run(run, reason="", by=OWNER))
		self.assertEqual(lost.call_count, 2)
		(entry,) = self._logged(log, "jarvis.chat.macros.cancel_lost")
		self.assertEqual(entry["title"], f"jarvis.chat.macros.cancel_lost: {run}")
		for said in (run, turn, "queued"):
			self.assertIn(said, entry["message"])
		self.assertNotIn("prompt", entry["message"])

	def test_a_step_that_had_already_started_is_not_logged(self):
		# The ordinary case: Stop while a step is with the agent. It finishes; quiet.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		self._step(conv, "streaming", placeholder=True)
		with patch("frappe.log_error") as log:
			self.assertTrue(macros._stop_run(run, reason="", by=OWNER))
			self.assertFalse(macros._cancel_undispatched_turn("no-such-turn", "x", macro_run=run))
		log.assert_not_called()

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
			self.assertEqual(macros._dispatch_step(run_doc, macro_doc, 0), macros._STEP_WITHDRAWN)
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
				self.assertEqual(macros._dispatch_step(run_doc, macro_doc, 0), macros._STEP_WITHDRAWN)
			self.assertEqual(self._run(run).current_step, 0, status)
			self.assertEqual(self._live_turns(conv), [], status)

	def test_a_step_sent_while_its_macro_is_deleted_is_cancelled(self):
		# A delete stops the run and then removes its ROW, and can do both while a
		# step is being sent: the stop finds no turn to cancel yet. A run with no row
		# has ended. The fence asked "is the status an ended one", and no row has no
		# status, so the step stayed queued and ran after its macro was gone.
		run, conv, macro = self._mk_run(steps=3, at_step=1, armed=True)
		step_turn, _, _ = self._turn(conv)

		def deleted():
			self.assertEqual(macros_api.delete_macro(macro), {"ok": True, "stopped_runs": 1})

		frappe.set_user(OWNER)
		with self._stop_cannot_wait(), self._queueing(conv, before=deleted):
			macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists(RUN, run))
		self.assertFalse(frappe.db.exists(MACRO, macro))
		self.assertEqual(self._live_turns(conv), [])
		self.assertEqual(len(self._markers(conv)), 1)

	def test_the_first_step_of_a_run_whose_macro_is_deleted_at_once_is_cancelled(self):
		# The realistic one: run_macro sends step 1 outside the run lock, and the send
		# includes the session setup of a brand-new chat. A delete that was waiting
		# for the macro row runs to completion inside it.
		frappe.set_user(OWNER)
		macro = frappe.get_doc(
			{"doctype": MACRO, "macro_name": f"{outcome.PFX}-gone-first", "steps": [{"prompt": "a"}]}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		started = {}

		def enqueue(conversation, prompt, **kw):
			started["conv"] = conversation
			started["deleted"] = macros_api.delete_macro(macro.name)
			run_id, seed, _ = self._step(conversation, "queued")
			frappe.set_user(OWNER)
			return {"run_id": run_id, "message_id": seed, "queued": True}

		with patch("jarvis.chat.api._enqueue_turn", side_effect=enqueue):
			macros.run_macro(macro.name)
		frappe.set_user("Administrator")
		self.assertEqual(started["deleted"], {"ok": True, "stopped_runs": 1})
		self.assertFalse(frappe.db.exists(MACRO, macro.name))
		self.assertEqual(self._live_turns(started["conv"]), [])
		self.assertEqual(len(self._markers(started["conv"])), 1)
		self.assertEqual(frappe.db.get_value(CONV, started["conv"], "skip_confirmation"), 0)

	def test_a_step_that_had_started_before_the_fence_still_counts(self):
		# The fence cannot cancel a step the agent already has (always so in Phase-0,
		# where a step is dispatched as it is accepted). That step runs: the cursor
		# must say so, or the month's scheduled budget counts one step too few.
		for stepped in (True, False):
			run, conv, macro = self._mk_run(steps=2, at_step=0, tag=f"started-{stepped}")
			run_doc, macro_doc = frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)

			def enqueue(conversation, prompt, run=run, **kw):
				macros._stop_run(run, reason="", by=OWNER)
				run_id, seed, _ = self._step(conversation, "dispatching", placeholder=True)
				return {"run_id": run_id, "message_id": seed}

			with (
				patch("jarvis.chat.api._enqueue_turn", side_effect=enqueue),
				patch("jarvis.chat.macros.publish_to_user") as publish,
				patch("frappe.log_error") as log,
			):
				if not stepped:
					run_doc.run_mode = "merged"  # a summarized run: one turn, the summary
				self.assertEqual(macros._dispatch_step(run_doc, macro_doc, 0), macros._STEP_RUNS_ANYWAY)
			row = self._run(run)
			self.assertEqual((row.status, row.current_step), ("stopped", 1), stepped)
			self.assertEqual(len(self._live_turns(conv)), 1, "the started step was cancelled")
			self.assertEqual(self._progress(publish), [], "progress was announced for an ended run")
			log.assert_not_called()  # the fence firing is an ordinary race

	def test_a_step_stop_cancelled_first_does_not_count(self):
		# Stop found the turn and cancelled it before the fence looked. The fence's
		# own cancel then has nothing to do; the step did not run.
		run, conv, macro = self._mk_run(steps=2, at_step=0)
		run_doc, macro_doc = frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)

		def enqueue(conversation, prompt, **kw):
			run_id, seed, _ = self._step(conversation, "queued")
			macros._stop_run(run, reason="", by=OWNER)
			return {"run_id": run_id, "message_id": seed, "queued": True}

		with patch("jarvis.chat.api._enqueue_turn", side_effect=enqueue):
			self.assertEqual(macros._dispatch_step(run_doc, macro_doc, 0), macros._STEP_WITHDRAWN)
		self.assertEqual(self._run(run).current_step, 0)
		self.assertEqual(self._live_turns(conv), [])
		self.assertEqual(len(self._markers(conv)), 1)

	def test_a_run_parked_for_capacity_is_not_ended(self):
		# `waiting_capacity` is live. Cancelling its turn would leave the step marked
		# as sent with nothing to run it.
		run, conv, macro = self._mk_run(steps=2, at_step=0)
		run_doc, macro_doc = frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)

		def parked():
			frappe.db.set_value(RUN, run, "status", "waiting_capacity")
			frappe.db.commit()

		with self._queueing(conv, before=parked):
			self.assertEqual(macros._dispatch_step(run_doc, macro_doc, 0), macros._STEP_SENT)
		self.assertEqual(self._run(run).current_step, 1)
		self.assertEqual(len(self._live_turns(conv)), 1)

	def test_a_step_sent_to_a_live_run_is_left_alone(self):
		run, conv, macro = self._mk_run(steps=2, at_step=0)
		run_doc, macro_doc = frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)
		with self._queueing(conv):
			self.assertEqual(macros._dispatch_step(run_doc, macro_doc, 0), macros._STEP_SENT)
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
		self.assertEqual(closing.content, CLOSING)
		self.assertEqual(self._turn_state(queued), "cancelled")
		self.assertEqual(self._live_turns(conv), [])
		# Nothing later either: a turn ending in that chat finds no run to move.
		with patch("jarvis.chat.api._enqueue_turn") as enqueue:
			macros.advance_after_turn(conv, errored=False)
		enqueue.assert_not_called()

	def test_the_closing_line_does_not_close_a_draft_the_owner_can_still_apply(self):
		# The chat keeps a card open on its LAST reply only. A line after it would
		# take the draft away; the run is stopped all the same.
		run, conv, macro = self._mk_run(steps=2, at_step=1)
		self._turn(conv, reply=outcome._DRAFT)  # step 1 ended on a draft; the hook has not run yet
		frappe.set_user(OWNER)
		self.assertEqual(macros_api.delete_macro(macro)["stopped_runs"], 1)
		self.assertEqual(self._closing(conv, run), [])

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
		self.assertEqual(closing.content, CLOSING)

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
			patch("frappe.log_error") as log,
			self.assertRaises(macros_api.MacroBusyError) as refused,
		):
			macros_api.delete_macro(macro)
		# What happened and what to do, not "this macro is running": the delete itself
		# stopped those runs, and they stay stopped.
		self.assertEqual(
			str(refused.exception),
			"3 runs were stopped, but another run started before the macro could be deleted. Delete it again.",
		)
		self.assertEqual(refused.exception.stopped_runs, 3)
		# The one trace an operator gets of a macro being started in a loop.
		(entry,) = self._logged(log, "jarvis.chat.macros_api.delete_refused_busy")
		self.assertIn(macro, entry["title"])
		self.assertIn("3 run(s) were stopped", entry["message"])
		self.assertNotIn("step 1", entry["message"])  # no prompt text
		self.assertEqual(len(started), macros_api._DELETE_STOP_ROUNDS)
		self.assertTrue(frappe.db.exists(MACRO, macro))
		# The macro survives, so what its stopped runs say must still be true.
		for stopped_run in (first, *started[:-1]):
			self.assertEqual(self._run(stopped_run).error, "Stopped: its macro was being deleted.")
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

	def test_a_bulk_delete_names_the_macro_it_skipped_and_counts_its_stopped_runs(self):
		_, _, busy = self._mk_run(tag="bulk-busy")
		done, _, idle = self._mk_run(tag="bulk-idle2")
		frappe.db.set_value(RUN, done, "status", "completed")
		frappe.db.commit()
		real = macros._lock_macro_row

		def a_run_starts_every_time(name):
			if name == busy:
				self._start_run_elsewhere(busy, self._armed_chat())
				frappe.set_user(OWNER)
			return real(name)

		frappe.set_user(OWNER)
		with (
			patch.object(macros, "_lock_macro_row", side_effect=a_run_starts_every_time),
			patch("frappe.log_error") as log,
		):
			out = macros_api.delete_macros_bulk([busy, idle])
		self.assertEqual(
			out["skipped"],
			[
				{
					"name": busy,
					"title": f"{outcome.PFX}-bulk-busy",
					"reason": "running",
					"message": (
						"Its run was stopped, but another run started before it could be deleted. "
						"Delete it again."
					),
				}
			],
		)
		# The runs of the macro that was skipped were stopped all the same.
		self.assertEqual((out["deleted"], out["stopped_runs"]), (1, 3))
		self.assertFalse(frappe.db.exists(MACRO, idle))
		self.assertEqual(len(self._logged(log, "jarvis.chat.macros_api.delete_refused_busy")), 1)
		self.assertEqual(self._logged(log, "Jarvis: bulk macro delete failed"), [])
		self.assertEqual(frappe.local.message_log, [])

	def test_a_bulk_delete_that_fails_on_one_macro_leaves_it_whole_and_lets_go_of_it(self):
		# The failed macro's row lock and its half-done delete (run history already
		# gone) were carried into the next macro: that one waited for a run lock while
		# holding the first one's row, and its first commit made the half-done delete
		# permanent.
		kept_run, _, broken = self._mk_run(tag="bulk-broken")
		frappe.db.set_value(RUN, kept_run, "status", "completed")
		_, _, fine = self._mk_run(tag="bulk-fine")
		frappe.db.commit()
		real_delete, real_stop = frappe.delete_doc, macros._stop_run

		def delete_doc(doctype, name, *a, **kw):
			if name == broken:
				raise RuntimeError("delete_doc failed")
			return real_delete(doctype, name, *a, **kw)

		def the_first_row_is_free(run_name, **kw):
			with other_connection() as other:
				other.sql("SET SESSION innodb_lock_wait_timeout=1")
				other.sql(f"SELECT name FROM `tab{MACRO}` WHERE name=%s FOR UPDATE", broken)
			return real_stop(run_name, **kw)

		frappe.set_user(OWNER)
		with (
			patch.object(macros_api.frappe, "delete_doc", side_effect=delete_doc),
			patch.object(macros, "_stop_run", side_effect=the_first_row_is_free) as stop,
			patch("frappe.log_error") as log,
		):
			out = macros_api.delete_macros_bulk([broken, fine])
		self.assertEqual(stop.call_count, 1)
		self.assertEqual((out["deleted"], out["stopped_runs"]), (1, 1))
		(skipped,) = out["skipped"]
		self.assertEqual((skipped["name"], skipped["reason"]), (broken, "error"))
		self.assertEqual(skipped["message"], "It could not be deleted. Try again.")
		self.assertTrue(frappe.db.exists(MACRO, broken))
		self.assertTrue(frappe.db.exists(RUN, kept_run), "its run history went although it was not deleted")
		self.assertEqual(len(self._logged(log, "Jarvis: bulk macro delete failed")), 1)

	def test_a_bulk_delete_reports_the_other_reasons_in_words(self):
		_, _, theirs = self._mk_run(tag="bulk-theirs")
		# _mk_run leaves the session as the owner, who may not create a user: on a site where
		# the bystander does not exist yet (CI), ensure_user must run as Administrator.
		frappe.set_user("Administrator")
		frappe.db.set_value(MACRO, theirs, "owner", ensure_user(BYSTANDER), update_modified=False)
		frappe.db.commit()
		frappe.set_user(OWNER)
		out = macros_api.delete_macros_bulk([theirs, "no-such-macro"])
		self.assertEqual(
			[(s["reason"], s["message"]) for s in out["skipped"]],
			[("not owner", "It belongs to someone else."), ("not found", "It was already deleted.")],
		)
		self.assertEqual((out["deleted"], out["stopped_runs"]), (0, 0))
		frappe.set_user("Administrator")
		frappe.delete_doc(RUN, frappe.db.get_value(RUN, {"macro": theirs}), force=True)
		frappe.delete_doc(MACRO, theirs, force=True)
		frappe.db.commit()

	def test_a_macro_row_that_stays_locked_is_busy_not_a_crash(self):
		# A lock wait timeout on the macro row was the database's own error: a 500
		# for a delete or a manual Run, and the bare word "error" in a bulk delete.
		_, _, macro = self._mk_run(tag="row-held")
		frappe.db.set_value(RUN, {"macro": macro}, "status", "completed")
		frappe.db.commit()
		frappe.set_user(OWNER)
		previous = frappe.db.sql("SELECT @@session.innodb_lock_wait_timeout")[0][0]
		with other_connection() as holder:
			holder.sql(f"SELECT name FROM `tab{MACRO}` WHERE name=%s FOR UPDATE", macro)
			try:
				frappe.db.commit()
				frappe.db.sql("SET SESSION innodb_lock_wait_timeout=1")
				with self.assertRaises(macros.MacroRowBusyError) as busy:
					macros_api.delete_macro(macro)
				self.assertEqual(str(busy.exception), "This macro is busy right now. Try again in a moment.")
				self.assertIsInstance(busy.exception, frappe.ValidationError)
				frappe.db.rollback()
				frappe.clear_messages()
				with patch("frappe.log_error") as log:
					out = macros_api.delete_macros_bulk([macro])
				self.assertEqual(
					[(s["reason"], s["message"]) for s in out["skipped"]],
					[("busy", "It is busy right now. Try again in a moment.")],
				)
				log.assert_not_called()
			finally:
				frappe.db.rollback()
				frappe.db.sql(f"SET SESSION innodb_lock_wait_timeout={int(previous)}")
		self.assertTrue(frappe.db.exists(MACRO, macro))


# --------------------------------------------------------------------------- #
# Nothing writes a live or a failed status over a stop
# --------------------------------------------------------------------------- #
class TestAStoppedRunStaysStopped(StopBase):
	def test_a_deferral_does_not_put_a_stopped_run_back_in_the_queue(self):
		# The dispatcher read `running` and found the site busy; Stop landed; the
		# deferral then wrote `waiting_capacity` by name, and the resume cron sent the
		# step of a stopped run. Its own look at the status was no defence: it reads
		# the dispatcher's snapshot, which is from before the Stop.
		run, _, macro = self._mk_run(steps=2, at_step=0)
		run_doc, macro_doc = frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)
		with snapshot_isolation_off():
			frappe.db.commit()
			self.assertEqual(frappe.db.get_value(RUN, run, "status"), "running")  # the snapshot
			self._write_elsewhere(f"UPDATE `tab{RUN}` SET status='stopped' WHERE name=%s", run)
			with patch("jarvis.chat.macros.publish_to_user") as publish:
				macros._defer_capacity(run_doc, macro_doc)
		frappe.db.rollback()
		self.assertEqual(self._run(run).status, "stopped")
		self.assertEqual(run_doc.status, "running", "the caller was told the run is parked")
		publish.assert_not_called()

	def test_a_deferral_still_parks_a_running_run(self):
		run, _, macro = self._mk_run(steps=2, at_step=0)
		run_doc, macro_doc = frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)
		macros._defer_capacity(run_doc, macro_doc)
		self.assertEqual((self._run(run).status, run_doc.status), ("waiting_capacity", "waiting_capacity"))

	def test_a_first_step_that_fails_to_send_does_not_turn_a_stop_into_a_failure(self):
		# Stop (or an archive) landed while the first step's dispatch was failing. The
		# failure path wrote `failed` by name over `stopped`, and announced it.
		frappe.set_user(OWNER)
		macro = frappe.get_doc(
			{"doctype": MACRO, "macro_name": f"{outcome.PFX}-send-fails", "steps": [{"prompt": "a"}]}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		started = {}

		def enqueue(conversation, prompt, **kw):
			started["run"] = frappe.db.get_value(RUN, {"conversation": conversation}, "name")
			macros.stop_macro_run(started["run"])
			raise RuntimeError("gateway down")

		with (
			patch("jarvis.chat.api._enqueue_turn", side_effect=enqueue),
			patch("jarvis.chat.macros.publish_to_user") as publish,
			patch("frappe.log_error"),
			self.assertRaises(frappe.ValidationError),
		):
			macros.run_macro(macro.name)
		row = self._run(started["run"])
		self.assertEqual((row.status, row.error or ""), ("stopped", ""))
		done = [c.args[1]["status"] for c in publish.call_args_list if c.args[1].get("kind") == "macro:done"]
		self.assertEqual(done, ["stopped"])

	def test_a_first_step_that_fails_to_send_still_fails_a_live_run(self):
		frappe.set_user(OWNER)
		macro = frappe.get_doc(
			{"doctype": MACRO, "macro_name": f"{outcome.PFX}-send-fails2", "steps": [{"prompt": "a"}]}
		).insert(ignore_permissions=True)
		frappe.db.set_value(MACRO, macro.name, "skip_confirmation", 1, update_modified=False)
		frappe.db.commit()
		with (
			patch("jarvis.chat.api._enqueue_turn", side_effect=RuntimeError("gateway down")),
			patch("frappe.log_error"),
		):
			out = macros.run_macro(macro.name, trigger="scheduled")
		self.assertEqual(out, {"ok": False, "reason": macros.BLOCK_DISPATCH_FAILED})
		row = frappe.db.get_value(
			RUN, {"macro": macro.name}, ["status", "error", "conversation", "finished_at"], as_dict=True
		)
		self.assertEqual((row.status, row.error), ("failed", macros._DISPATCH_FAILED_ERROR))
		self.assertTrue(row.finished_at)
		self.assertEqual(frappe.db.get_value(CONV, row.conversation, "skip_confirmation"), 0)


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
						self.assertRaises(macros.MacroRowBusyError),
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

	def test_a_scheduled_run_that_lost_to_a_delete_is_a_quiet_no_op(self):
		# The scheduler claimed the slot, the owner deleted the macro, and the run
		# start raised "not found": two Error Logs and a notification telling the owner
		# the macro they had just deleted "did not run" and "will run again".
		macro = self._macro("gone-scheduled")
		row = frappe.db.get_value(MACRO, macro, ["name", "owner", "macro_name", "enabled"], as_dict=True)
		frappe.set_user(OWNER)
		self._delete_elsewhere(macro)
		with patch("jarvis.chat.api._enqueue_turn") as enqueue:
			out = macros.run_macro(macro, trigger="scheduled")
		self.assertEqual(out, {"ok": False, "reason": macros.BLOCK_MACRO_DELETED})
		enqueue.assert_not_called()
		self.assertEqual(self._chats(), 0)
		with (
			patch.object(macro_scheduler, "_record_failed") as record,
			patch.object(macro_scheduler, "_notify_owner") as notify,
			patch.object(macro_scheduler, "_retry_later") as retry,
			patch.object(macro_scheduler, "_stamp_last_run") as stamp,
			patch("frappe.log_error") as log,
		):
			macro_scheduler._settle(row, frappe.utils.now_datetime(), out, None)
		for untouched in (record, notify, retry, stamp, log):
			untouched.assert_not_called()
		# Someone pressing Run on a macro that is gone is still told so.
		with self.assertRaises(frappe.DoesNotExistError):
			macros.run_macro(macro)
		frappe.db.rollback()

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
		with (
			patch.object(macros, "_stop_run", side_effect=RuntimeError("redis")),
			patch("frappe.log_error") as log,
		):
			self.assertEqual(chat_api.archive_conversation(conv), {"ok": True})
		self.assertEqual(frappe.db.get_value(CONV, conv, "status"), "Archived")
		# Not blocking, and not silent either: the run that stayed live is named.
		(entry,) = self._logged(log, "jarvis.chat.macros.stop_for_conversation_failed")
		self.assertEqual(entry["title"], f"jarvis.chat.macros.stop_for_conversation_failed: {run}")

	def test_one_run_that_cannot_be_stopped_does_not_spare_the_others(self):
		# Clear history stops every live run of the user. One failure used to end the
		# loop, leaving the runs after it live.
		stuck, _, _ = self._mk_run(tag="stuck")
		other, _, _ = self._mk_run(tag="other")
		real = macros._stop_run

		def stop(run_name, **kw):
			if run_name == stuck:
				raise frappe.QueryDeadlockError("1020")
			return real(run_name, **kw)

		frappe.set_user(OWNER)
		with patch.object(macros, "_stop_run", side_effect=stop), patch("frappe.log_error") as log:
			self.assertEqual(chat_api.clear_chat_history()["ok"], True)
		self.assertEqual(self._run(other).status, "stopped")
		self.assertEqual(len(self._logged(log, "jarvis.chat.macros.stop_for_conversation_failed")), 1)

	def test_clearing_chat_history_stops_the_run_while_its_chat_still_exists(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1, armed=True)
		parked, _, _ = self._mk_run(steps=2, at_step=0, tag="parked")
		frappe.db.set_value(RUN, parked, "status", "waiting_capacity")
		self._step(conv, "done")  # between two steps: no reply is in progress
		seen = {}
		real = macros._cancel_current_step

		def cancel(conversation, **kw):
			# The step to cancel is found through the chat's messages.
			seen[conversation] = frappe.db.count(MSG, {"conversation": conversation})
			return real(conversation, **kw)

		frappe.set_user(OWNER)
		with patch.object(macros, "_cancel_current_step", side_effect=cancel):
			out = chat_api.clear_chat_history()
		self.assertEqual(out, _cleared(deleted=2))
		self.assertTrue(seen[conv] > 0, "the run was stopped after its messages were deleted")
		for name in (run, parked):
			row = frappe.db.get_value(RUN, name, ["status", "error", "conversation"], as_dict=True)
			self.assertEqual((row.status, row.error), ("stopped", macros._HISTORY_CLEARED_ERROR))
			self.assertFalse(row.conversation)
		self.assertFalse(frappe.db.exists(CONV, conv))

	def _clear_with_a_step_in(self, state):
		"""Clear history with one run whose current step is in ``state`` and one run
		between steps. Returns what the kept run's chat must still hold."""
		run, conv, _ = self._mk_run(steps=3, at_step=1, armed=True, tag="kept")
		step, _, _ = self._step(conv, state, placeholder=state == "streaming")
		before = frappe.db.count(MSG, {"conversation": conv})
		gone, other, _ = self._mk_run(steps=2, at_step=1, tag="gone")
		frappe.set_user(OWNER)
		self.assertEqual(chat_api.clear_chat_history(), _cleared(conv, deleted=1))
		# A kept chat keeps its run: not stopped, not disarmed, still linked.
		row = frappe.db.get_value(RUN, run, ["status", "conversation"], as_dict=True)
		self.assertEqual((row.status, row.conversation), ("running", conv))
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 1)
		self.assertEqual(self._turn_state(step), state)
		self.assertEqual(frappe.db.count(MSG, {"conversation": conv}), before)
		# The run in the chat that did go is stopped, and its link is blanked.
		row = frappe.db.get_value(RUN, gone, ["status", "error", "conversation"], as_dict=True)
		self.assertEqual((row.status, row.error), ("stopped", macros._HISTORY_CLEARED_ERROR))
		self.assertFalse(row.conversation)
		self.assertFalse(frappe.db.exists(CONV, other))

	def test_a_run_stopped_for_a_chat_that_is_then_kept_is_told_something_true(self):
		# The stop commits before the delete. A reply that starts in between keeps the
		# chat, and the stopped run's closing line is then read in a chat that exists.
		run, conv, _ = self._mk_run(steps=3, at_step=1, armed=True)
		self._step(conv, "done")
		real_stop = chat_api._stop_macro_runs_in

		def stop_then_a_send_lands(conversations, **kw):
			real_stop(conversations, **kw)
			self._step(conv, "queued")

		frappe.set_user(OWNER)
		with patch.object(chat_api, "_stop_macro_runs_in", side_effect=stop_then_a_send_lands):
			out = chat_api.clear_chat_history()
		self.assertEqual(out, _cleared(conv, deleted=0))
		row = frappe.db.get_value(RUN, run, ["status", "error", "conversation"], as_dict=True)
		self.assertEqual((row.status, row.conversation), ("stopped", conv))
		(closing,) = self._closing(conv, run)
		self.assertEqual(row.error, "This run was stopped because its owner deleted all chat history.")
		self.assertEqual(closing.content, f"■ {row.error}")

	def test_clearing_chat_history_leaves_a_run_whose_step_is_being_answered(self):
		# "Delete all" keeps a chat with a reply in progress: deleting it would take
		# the Turn row from under the pump.
		self._clear_with_a_step_in("streaming")

	def test_clearing_chat_history_leaves_a_run_whose_step_is_still_in_the_queue(self):
		# A step waiting in the queue is a reply in progress too: it is not cancelled.
		self._clear_with_a_step_in("queued")
