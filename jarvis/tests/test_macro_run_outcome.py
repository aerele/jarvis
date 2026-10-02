"""A macro run's recorded outcome is what happened.

Three defects from the 2026-10-01 macros review, all in the turn-end chaining hook
(``macros.advance_after_turn``):

* it advanced the run on ANY finished turn in the run's conversation (a message the
  user typed, a confirmation card's hidden follow-up, a re-delivered event), so steps
  overlapped and runs finished early;
* a run ended ``completed`` when a step was still waiting on a confirmation card, or
  when steps had failed with "Stop on error" off;
* a failed step stored only "Step N failed.", and the owner was told nothing.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import macros, macros_api
from jarvis.tests._conv_helpers import NON_ADMIN_USER, _ensure_non_admin_user

CONV = "Jarvis Conversation"
MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
OWNER = NON_ADMIN_USER
PFX = "outcome"

_DRAFT = '```jarvis-action\n{"kind": "doc", "verb": "create", "doctype": "ToDo", "fields": {}}\n```'


class MacroRunOutcomeBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_non_admin_user()

	def setUp(self):
		self._orig = frappe.session.user
		self._seq = {}
		self._purge()

	def tearDown(self):
		frappe.set_user(self._orig)
		frappe.db.rollback()
		self._purge()

	def _purge(self):
		from jarvis.chat import pending_confirm

		frappe.set_user("Administrator")
		for conv in frappe.get_all(CONV, filters={"owner": OWNER}, pluck="name"):
			try:
				pending_confirm.clear_for_conversation(OWNER, conv)
			except Exception:
				pass
			frappe.db.delete(TURN, {"conversation": conv})
			frappe.db.delete(MSG, {"conversation": conv})
		for dt in (RUN, MACRO, CONV, "ToDo"):
			for name in frappe.get_all(dt, filters={"owner": OWNER}, pluck="name"):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.delete("Notification Log", {"for_user": OWNER})
		frappe.db.commit()

	# --- fixtures -------------------------------------------------------------- #

	def _mk_run(self, *, steps=2, at_step=1, armed=False, stop_on_error=1, trigger="manual", tag="m"):
		"""A macro with ``steps`` steps and a run of it that has dispatched step
		``at_step`` (1-based), the way ``_run_step`` leaves the row."""
		frappe.set_user(OWNER)
		macro = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-{tag}",
				"stop_on_error": stop_on_error,
				"steps": [{"prompt": f"step {i}"} for i in range(1, steps + 1)],
			}
		).insert(ignore_permissions=True)
		conv = frappe.get_doc({"doctype": CONV, "title": f"{PFX} run"})
		conv.flags.ignore_permissions = True
		conv.insert()
		if armed:
			frappe.db.set_value(CONV, conv.name, "skip_confirmation", 1, update_modified=False)
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"macro": macro.name,
				"conversation": conv.name,
				"status": "running",
				"current_step": at_step,
				"total_steps": steps,
				"run_mode": "stepped",
				"trigger": trigger,
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		frappe.db.commit()
		return run.name, conv.name, macro.name

	def _msg(self, conv, role, *, origin="", content="", error=""):
		frappe.set_user(OWNER)
		seq = self._seq[conv] = self._seq.get(conv, 0) + 1
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": seq,
				"role": role,
				"origin": origin,
				"content": content,
				"error": error,
			}
		)
		doc.flags.ignore_permissions = True
		doc.flags.jarvis_server_write = True
		doc.insert()
		return doc.name

	def _turn(self, conv, *, origin="macro", state="done", error="", reply="done", reply_error=""):
		"""One turn the way the turn machine records it: the user row that started it,
		the assistant row, and the Turn row linking the two. Returns
		``(run_id, seed_message, assistant_message)``."""
		seed = self._msg(conv, "user", origin=origin, content="prompt")
		assistant = self._msg(conv, "assistant", content=reply, error=reply_error)
		run_id = frappe.generate_hash(length=16)
		turn = frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": run_id,
				"conversation": conv,
				"relay_target_id": "t",
				"turn_class": "interactive",
				"state": state,
				"seed_message": seed,
				"assistant_message": assistant,
				"error": error,
			}
		)
		turn.flags.ignore_permissions = True
		turn.insert()
		frappe.db.commit()
		return run_id, seed, assistant

	def _dispatching(self, conv):
		"""Stand-in for ``api._enqueue_turn``: records the next step's turn in the
		conversation, still in flight, exactly what a real dispatch leaves behind."""

		def enqueue(conversation, prompt, **kw):
			run_id, seed, _ = self._turn(conv, origin=kw.get("origin", "macro"), state="streaming", reply="")
			return {"run_id": run_id, "message_id": seed}

		return patch("jarvis.chat.api._enqueue_turn", side_effect=enqueue)

	def _park_card(self, conv):
		"""A real confirmation card on the conversation (delete_doc always parks)."""
		frappe.set_user(OWNER)
		todo = frappe.get_doc({"doctype": "ToDo", "description": f"{PFX}-doomed"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		r = api._run_tool("delete_doc", {"doctype": "ToDo", "name": todo.name}, conversation=conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation")

	def _live_cards(self, conv):
		from jarvis.chat import pending_confirm

		rows = pending_confirm.list_for_owner(OWNER, conversation=conv)
		return [r for r in rows if r.get("conversation") == conv]

	def _run(self, name):
		return frappe.db.get_value(RUN, name, ["status", "error", "current_step"], as_dict=True)


# --------------------------------------------------------------------------- #
# Only the step's own turn moves the run
# --------------------------------------------------------------------------- #
class TestOnlyTheStepsOwnTurnAdvancesTheRun(MacroRunOutcomeBase):
	def test_the_steps_own_turn_dispatches_the_next_step(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv)
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(self._run(run).current_step, 2)

	def test_a_message_the_user_typed_does_not_advance_the_run(self):
		# The run conversation of an unarmed macro accepts typed messages. That turn
		# ending used to dispatch step 2 while step 1 was still running.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		self._turn(conv, state="streaming")  # step 1, still in flight
		typed, _, _ = self._turn(conv, origin="human")
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False, run_id=typed)
		enqueue.assert_not_called()
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 1))

	def test_a_confirmation_cards_follow_up_does_not_advance_the_run(self):
		run, conv, _ = self._mk_run(steps=3, at_step=2)
		self._turn(conv)  # step 1
		self._turn(conv, state="streaming")  # step 2, in flight
		follow_up, _, _ = self._turn(conv, origin="continuation")
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False, run_id=follow_up)
		enqueue.assert_not_called()
		self.assertEqual(self._run(run).current_step, 2)

	def test_a_typed_message_does_not_finish_the_run_early(self):
		run, conv, _ = self._mk_run(steps=1, at_step=1)
		self._turn(conv, state="streaming")
		typed, _, _ = self._turn(conv, origin="human")
		macros.advance_after_turn(conv, errored=False, run_id=typed)
		self.assertEqual(self._run(run).status, "running", "a typed message completed the run")

	def test_a_step_event_delivered_twice_advances_once(self):
		# The finalize effect that calls the hook is retried when a later sibling raises.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_turn, _, _ = self._turn(conv)
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False, run_id=step_turn)
			macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		self.assertEqual(enqueue.call_count, 1, "a re-delivered event skipped a step")
		self.assertEqual(self._run(run).current_step, 2)

	def test_a_failed_typed_message_is_not_reported_as_a_failed_step(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		self._turn(conv, state="streaming")
		typed, _, _ = self._turn(conv, origin="human", state="errored", error="model overloaded")
		macros.advance_after_turn(conv, errored=True, run_id=typed)
		self.assertEqual(self._run(run).status, "running")

	def test_the_caller_can_name_the_message_that_started_the_turn(self):
		# The legacy (no Turn row) worker has the user message id, not a Turn.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_msg = self._msg(conv, "user", origin="macro")
		typed_msg = self._msg(conv, "user", origin="human")
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False, seed_message=typed_msg)
			enqueue.assert_not_called()
			macros.advance_after_turn(conv, errored=False, seed_message=step_msg)
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(self._run(run).current_step, 2)

	def test_recovery_names_the_turn_by_its_reply(self):
		# Turn recovery works from the assistant row; the Turn links it to its start.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		_, _, step_reply = self._turn(conv, state="streaming")
		_, _, typed_reply = self._turn(conv, origin="human")
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False, assistant_message=typed_reply)
			enqueue.assert_not_called()
			macros.advance_after_turn(conv, errored=False, assistant_message=step_reply)
		self.assertEqual(enqueue.call_count, 1)

	def test_a_turn_nobody_can_identify_still_advances_the_run(self):
		# No Turn row and no message id: the engine cannot tell, and a stalled run is
		# worse than the old looseness. This is what every caller did before.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False)
		self.assertEqual(enqueue.call_count, 1)

	def test_an_unidentified_turn_does_not_advance_while_the_step_is_still_running(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		self._turn(conv, state="streaming")  # the step's turn has not ended
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False)
		enqueue.assert_not_called()
		self.assertEqual(self._run(run).current_step, 1)


# --------------------------------------------------------------------------- #
# A step waiting on a confirmation card is not "completed"
# --------------------------------------------------------------------------- #
class TestARunStopsAtAWaitingConfirmation(MacroRunOutcomeBase):
	def test_an_unarmed_run_stops_at_a_step_that_is_waiting_for_confirmation(self):
		# Only one card can be live in a conversation and a macro step cannot replace
		# it, so the steps after it could not have written anything either.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_turn, _, _ = self._turn(conv)
		self._park_card(conv)
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		enqueue.assert_not_called()
		row = self._run(run)
		self.assertEqual(row.status, "stopped")
		self.assertIn("Stopped at step 1 of 3", row.error)
		self.assertIn("confirmation", row.error)
		self.assertIn("did not run", row.error)
		self.assertEqual(len(self._live_cards(conv)), 1, "the card is the user's to answer; it was swept")

	def test_the_last_step_waiting_for_confirmation_is_not_completed(self):
		run, conv, _ = self._mk_run(steps=1, at_step=1)
		step_turn, _, _ = self._turn(conv)
		self._park_card(conv)
		macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		row = self._run(run)
		self.assertEqual(row.status, "stopped")
		self.assertIn("waiting for your confirmation", row.error)
		self.assertNotIn("did not run", row.error, "there are no later steps to mention")

	def test_a_run_with_no_card_waiting_still_completes(self):
		run, conv, _ = self._mk_run(steps=1, at_step=1)
		step_turn, _, _ = self._turn(conv)
		macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		row = self._run(run)
		self.assertEqual((row.status, row.error or ""), ("completed", ""))

	def test_an_armed_run_still_sweeps_the_card_and_fails(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1, armed=True)
		step_turn, _, _ = self._turn(conv)
		self._park_card(conv)
		macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		row = self._run(run)
		self.assertEqual(row.status, "failed")
		self.assertIn("unattended run", row.error)
		self.assertEqual(self._live_cards(conv), [])


# --------------------------------------------------------------------------- #
# A failed step is never "completed", and says why
# --------------------------------------------------------------------------- #
class TestAFailedStepIsRecordedWithItsReason(MacroRunOutcomeBase):
	def test_a_failed_step_stores_the_reason_the_turn_gave(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv, state="errored", error="The model is overloaded.\nTry again.")
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		row = self._run(run)
		self.assertEqual(row.status, "failed")
		self.assertEqual(row.error, "Step 1 failed: The model is overloaded. Try again.")

	def test_the_reason_falls_back_to_the_replys_own_error(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv, state="errored", reply_error="gateway timeout")
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		self.assertEqual(self._run(run).error, "Step 1 failed: gateway timeout")

	def test_a_stopped_step_says_it_was_stopped(self):
		# A turn the user stopped carries no error text at all.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv, state="cancelled")
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		self.assertEqual(self._run(run).error, "Step 1 failed: the step was stopped.")

	def test_a_long_reason_is_cut_to_one_short_line(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv, state="errored", error="x" * 2000)
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		self.assertLessEqual(len(self._run(run).error), 160)

	def test_with_stop_on_error_off_a_run_whose_step_failed_is_not_completed(self):
		run, conv, _ = self._mk_run(steps=3, at_step=3, stop_on_error=0)
		self._turn(conv)  # step 1 fine
		self._turn(conv, state="errored", error="no such customer")  # step 2 failed
		last, _, _ = self._turn(conv)  # step 3 fine
		macros.advance_after_turn(conv, errored=False, run_id=last)
		row = self._run(run)
		self.assertEqual(row.status, "failed", "a run with a failed step was recorded as completed")
		self.assertEqual(row.error, "Finished, but 1 of 3 steps failed. Step 2: no such customer")

	def test_with_stop_on_error_off_the_run_keeps_going_past_a_failed_step(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1, stop_on_error=0)
		step_turn, _, _ = self._turn(conv, state="errored", error="boom")
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		self.assertEqual(enqueue.call_count, 1)
		row = self._run(run)
		self.assertEqual((row.status, row.error or ""), ("running", ""), "a running row shows no failure")

	def test_a_last_step_that_failed_counts_even_without_a_turn_row(self):
		# The legacy worker has no Turn rows; it still knows the turn it just ran failed.
		run, conv, _ = self._mk_run(steps=2, at_step=2, stop_on_error=0)
		macros.advance_after_turn(conv, errored=True)
		row = self._run(run)
		self.assertEqual(row.status, "failed")
		self.assertIn("Step 2", row.error)

	def test_a_run_whose_steps_all_worked_is_completed_with_no_note(self):
		run, conv, _ = self._mk_run(steps=2, at_step=2)
		self._turn(conv)
		last, _, _ = self._turn(conv)
		macros.advance_after_turn(conv, errored=False, run_id=last)
		row = self._run(run)
		self.assertEqual((row.status, row.error or ""), ("completed", ""))

	def test_a_step_that_only_drafted_a_record_is_noted_on_the_completed_run(self):
		# An unarmed step that creates or updates a record ends with a draft for the
		# user to apply. Nothing is written until they do, and the run cannot see it.
		run, conv, _ = self._mk_run(steps=2, at_step=2)
		self._turn(conv, reply=f"Here is the draft.\n{_DRAFT}")
		last, _, _ = self._turn(conv)
		macros.advance_after_turn(conv, errored=False, run_id=last)
		row = self._run(run)
		self.assertEqual(row.status, "completed")
		self.assertIn("Step 1 proposed a draft", row.error)
		self.assertIn("did not wait", row.error)


# --------------------------------------------------------------------------- #
# The owner is told
# --------------------------------------------------------------------------- #
class TestTheOwnerIsTold(MacroRunOutcomeBase):
	def _done_events(self, publish):
		return [c.args[1] for c in publish.call_args_list if c.args[1].get("kind") == "macro:done"]

	def _notes(self):
		return frappe.get_all(
			"Notification Log", filters={"for_user": OWNER}, fields=["subject", "email_content"]
		)

	def test_the_done_event_carries_the_macros_name_and_the_reason(self):
		run, conv, macro = self._mk_run(steps=2, at_step=1, tag="named")
		step_turn, _, _ = self._turn(conv, state="errored", error="boom")
		with patch("jarvis.chat.macros.publish_to_user") as publish:
			macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		(event,) = self._done_events(publish)
		self.assertEqual(event["status"], "failed")
		self.assertEqual(event["macro_name"], f"{PFX}-named")
		self.assertEqual(event["error"], "Step 1 failed: boom")
		self.assertEqual(event["trigger"], "manual")

	def test_a_scheduled_run_that_fails_mid_way_notifies_its_owner(self):
		# Nobody is watching a scheduled run, and a realtime event reaches only an
		# open tab. Before, a mid-run failure of a scheduled macro told nobody.
		run, conv, _ = self._mk_run(steps=2, at_step=1, trigger="scheduled", tag="sched")
		step_turn, _, _ = self._turn(conv, state="errored", error="boom")
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		notes = self._notes()
		self.assertEqual(len(notes), 1, notes)
		self.assertIn(f"{PFX}-sched", notes[0].subject)
		self.assertIn("Step 1 failed: boom", notes[0].email_content)

	def test_a_scheduled_run_waiting_for_confirmation_notifies_its_owner(self):
		run, conv, _ = self._mk_run(steps=1, at_step=1, trigger="scheduled", tag="sched-card")
		step_turn, _, _ = self._turn(conv)
		self._park_card(conv)
		macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		notes = self._notes()
		self.assertEqual(len(notes), 1, notes)
		self.assertIn("confirmation", notes[0].email_content)

	def test_a_manual_run_does_not_write_a_notification(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv, state="errored", error="boom")
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		self.assertEqual(self._notes(), [])

	def test_a_scheduled_run_that_completes_cleanly_does_not_notify(self):
		run, conv, _ = self._mk_run(steps=1, at_step=1, trigger="scheduled")
		step_turn, _, _ = self._turn(conv)
		macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		self.assertEqual(self._notes(), [])


# --------------------------------------------------------------------------- #
# The list and the form say how the last run went
# --------------------------------------------------------------------------- #
class TestTheLastRunIsReported(MacroRunOutcomeBase):
	def _finish(self, run, status, error=""):
		frappe.db.set_value(RUN, run, {"status": status, "error": error, "finished_at": frappe.utils.now()})
		frappe.db.commit()

	def test_the_form_reports_the_last_runs_outcome(self):
		run, conv, macro = self._mk_run(tag="form")
		self._finish(run, "failed", "Step 1 failed: boom")
		frappe.set_user(OWNER)
		last = macros_api.get_macro(macro)["last_run"]
		self.assertEqual((last["status"], last["error"]), ("failed", "Step 1 failed: boom"))
		self.assertEqual(last["conversation"], conv)
		self.assertEqual(last["trigger"], "manual")

	def test_a_macro_that_never_ran_has_no_last_run(self):
		frappe.set_user(OWNER)
		macro = frappe.get_doc(
			{"doctype": MACRO, "macro_name": f"{PFX}-never", "steps": [{"prompt": "a"}]}
		).insert(ignore_permissions=True)
		self.assertIsNone(macros_api.get_macro(macro.name)["last_run"])

	def test_the_list_reports_each_macros_latest_run(self):
		old, _, macro = self._mk_run(tag="list")
		self._finish(old, "completed")
		frappe.db.set_value(RUN, old, "creation", frappe.utils.add_days(frappe.utils.now(), -1))
		frappe.set_user(OWNER)
		conv = frappe.get_doc({"doctype": CONV, "title": f"{PFX} newer"})
		conv.flags.ignore_permissions = True
		conv.insert()
		newer = frappe.get_doc(
			{"doctype": RUN, "macro": macro, "conversation": conv.name, "status": "failed", "error": "boom"}
		)
		newer.flags.ignore_permissions = True
		newer.insert()
		frappe.db.commit()
		rows = {r["name"]: r for r in macros_api.list_macros_page(page_length=100)["rows"]}
		self.assertEqual(rows[macro]["last_run"]["status"], "failed")
		self.assertEqual(rows[macro]["last_run"]["error"], "boom")

	def test_someone_overseeing_a_macro_is_not_handed_its_owners_conversation(self):
		run, conv, macro = self._mk_run(tag="oversight")
		self._finish(run, "failed", "Step 1 failed: boom")
		frappe.set_user("Administrator")
		last = macros_api.get_macro(macro)["last_run"]
		self.assertEqual(last["status"], "failed")
		self.assertFalse(last["conversation"], "another user's conversation was linked")
