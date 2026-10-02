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

	def _turn(
		self, conv, *, origin="macro", state="done", error="", reply="done", reply_error="", cancel_reason=""
	):
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
				"cancel_reason": cancel_reason,
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
		self.assertIn("Stopped at step 1 of 3: a confirmation is waiting in the conversation", row.error)
		self.assertIn("did not run", row.error)
		# What the owner cannot guess: confirming applies that write, not the rest.
		self.assertIn("confirming it will not resume them", row.error)
		self.assertNotIn("administrator", row.error, "a run they started by hand needs no arming hint")
		self.assertEqual(len(self._live_cards(conv)), 1, "the card is the user's to answer; it was swept")

	def test_the_last_step_waiting_for_confirmation_is_not_completed(self):
		run, conv, _ = self._mk_run(steps=1, at_step=1)
		step_turn, _, _ = self._turn(conv)
		self._park_card(conv)
		macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		row = self._run(run)
		self.assertEqual(row.status, "stopped")
		self.assertIn("Stopped at step 1 of 1: a confirmation is waiting", row.error)
		self.assertNotIn("did not run", row.error, "there are no later steps to mention")

	def test_a_scheduled_run_is_told_how_to_stop_this_happening(self):
		# A scheduled macro that parks a card stops at it every single run. Only arming
		# changes that, and arming is an administrator's switch.
		run, conv, _ = self._mk_run(steps=2, at_step=1, trigger="scheduled")
		step_turn, _, _ = self._turn(conv)
		self._park_card(conv)
		macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		self.assertIn("ask an administrator to switch on Skip confirmation", self._run(run).error)

	def test_the_cards_summary_is_one_bounded_line(self):
		# The summary is built from the tool call's own arguments. Unbounded, a long
		# one pushed the rest of the sentence off the end of the row.
		long_card = {"summary": "Send email to " + "someone@example.com, " * 60, "tool": "send_email"}
		text = macros._card_stop_message(1, 3, long_card, scheduled=True)
		self.assertLessEqual(len(text), 500)
		self.assertIn("did not run", text)
		self.assertIn("Skip confirmation", text)
		self.assertNotIn("\n", macros._card_stop_message(1, 3, {"summary": "a\nb"}, scheduled=False))

	def test_a_card_with_no_summary_names_the_action_in_words(self):
		text = macros._card_stop_message(1, 1, {"summary": "", "tool": "send_email"}, scheduled=False)
		self.assertIn("(send email)", text)
		self.assertNotIn("()", macros._card_stop_message(1, 1, {}, scheduled=False))

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
		step_turn, _, _ = self._turn(conv, state="errored", reply_error="The gateway did not answer.")
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		self.assertEqual(self._run(run).error, "Step 1 failed: The gateway did not answer.")

	def test_a_payload_is_replaced_by_what_the_chat_says_for_that_failure(self):
		# A turn records whatever failed it. Most of it is not for an owner's eyes: a
		# provider's JSON, a transport code, an exception's class name.
		from jarvis.chat import error_taxonomy

		raw = '{"error": {"type": "overloaded_error", "message": "Overloaded"}}'
		headline = error_taxonomy.headline_for(raw)
		self.assertTrue(headline, "premise: the chat has a headline for this failure")
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv, state="errored", error=raw)
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		error = self._run(run).error
		self.assertEqual(error, f"Step 1 failed: {headline.rstrip('.')}.")
		self.assertNotIn("{", error)

	def test_text_that_says_nothing_to_an_owner_is_not_stored(self):
		for raw in ("agent error", "x" * 2000, "unexpected worker error: KeyError"):
			with self.subTest(raw=raw[:30]):
				reason = macros._plain_reason(raw)
				self.assertNotIn("KeyError", reason)
				self.assertNotIn("xxxx", reason)
				self.assertNotEqual(reason, "agent error")
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv, state="errored", error="x" * 2000)
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		self.assertEqual(self._run(run).error, "Step 1 failed: the step ended with an error.")

	def test_a_step_the_user_stopped_stops_the_run_and_is_not_a_failure(self):
		# A turn the user stopped carries no error text at all. It is their own act:
		# the run stopped, it did not fail, and nothing should announce a failure.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv, state="cancelled")
		with patch("jarvis.chat.macros.publish_to_user") as publish:
			macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		row = self._run(run)
		self.assertEqual(row.status, "stopped")
		self.assertEqual(
			row.error, "Stopped at step 1 of 2: the step was stopped, so the steps after it did not run."
		)
		done = [c.args[1] for c in publish.call_args_list if c.args[1].get("kind") == "macro:done"]
		self.assertEqual([e["status"] for e in done], ["stopped"])

	def test_a_step_the_system_cancelled_says_why_and_does_not_blame_the_user(self):
		# A turn that waited too long in the queue is cancelled with the reason in
		# cancel_reason only. Read as a user stop it said "the step was stopped".
		reason = "Waited too long in the queue and was cancelled. Please try again."
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv, state="cancelled", cancel_reason=reason)
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		row = self._run(run)
		self.assertEqual(row.status, "failed")
		self.assertEqual(row.error, f"Step 1 failed: {reason}")

	def test_a_long_reason_is_cut_to_one_short_line(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		long_sentence = "The request " + "really " * 400 + "failed."
		step_turn, _, _ = self._turn(conv, state="errored", error=long_sentence)
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		error = self._run(run).error
		self.assertTrue(error.startswith("Step 1 failed: The request really"))
		self.assertLessEqual(len(error), 140)

	def test_a_merged_run_does_not_talk_about_step_one(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		frappe.db.set_value(RUN, run, {"run_mode": "merged", "total_steps": 1})
		frappe.db.commit()
		step_turn, _, _ = self._turn(conv, state="errored", error="The model is overloaded.")
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		self.assertEqual(self._run(run).error, "The summarized run failed: The model is overloaded.")

	def test_with_stop_on_error_off_a_run_whose_step_failed_is_not_completed(self):
		run, conv, _ = self._mk_run(steps=3, at_step=3, stop_on_error=0)
		self._turn(conv)  # step 1 fine
		self._turn(conv, state="errored", error="The customer could not be found.")  # step 2 failed
		last, _, _ = self._turn(conv)  # step 3 fine
		macros.advance_after_turn(conv, errored=False, run_id=last)
		row = self._run(run)
		self.assertEqual(row.status, "failed", "a run with a failed step was recorded as completed")
		self.assertEqual(
			row.error, "Finished, but 1 of 3 steps failed. Step 2: The customer could not be found."
		)

	def test_many_failed_steps_are_listed_without_cutting_a_reason_in_half(self):
		failed = {n: "The model's request limit was reached for this account today." for n in range(1, 21)}
		note = macros._failed_steps_note(failed, 20)
		self.assertLessEqual(len(note), 500)
		self.assertTrue(note.startswith("Finished, but 20 of 20 steps failed. Step 1: The model"))
		self.assertRegex(note, r"account today\. And \d+ more\.$")

	def test_a_one_step_run_that_failed_is_not_one_of_one_steps(self):
		self.assertEqual(
			macros._failed_steps_note({1: "The model is overloaded."}, 1),
			"Finished, but its only step failed. Step 1: The model is overloaded.",
		)

	def test_with_stop_on_error_off_the_run_keeps_going_past_a_failed_step(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1, stop_on_error=0)
		step_turn, _, _ = self._turn(conv, state="errored", error="The model is overloaded.")
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
		step_turn, _, _ = self._turn(conv, state="errored", error="The model is overloaded.")
		with patch("jarvis.chat.macros.publish_to_user") as publish:
			macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		(event,) = self._done_events(publish)
		self.assertEqual(event["status"], "failed")
		self.assertEqual(event["macro_name"], f"{PFX}-named")
		self.assertEqual(event["error"], "Step 1 failed: The model is overloaded.")
		self.assertEqual(event["trigger"], "manual")

	def test_a_scheduled_run_that_fails_mid_way_notifies_its_owner(self):
		# Nobody is watching a scheduled run, and a realtime event reaches only an
		# open tab. Before, a mid-run failure of a scheduled macro told nobody.
		run, conv, _ = self._mk_run(steps=2, at_step=1, trigger="scheduled", tag="sched")
		step_turn, _, _ = self._turn(conv, state="errored", error="The model is overloaded.")
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		notes = self._notes()
		self.assertEqual(len(notes), 1, notes)
		self.assertIn(f"{PFX}-sched", notes[0].subject)
		self.assertIn("Step 1 failed: The model is overloaded.", notes[0].email_content)

	def test_a_scheduled_run_waiting_for_confirmation_notifies_its_owner(self):
		run, conv, _ = self._mk_run(steps=1, at_step=1, trigger="scheduled", tag="sched-card")
		step_turn, _, _ = self._turn(conv)
		self._park_card(conv)
		macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		notes = self._notes()
		self.assertEqual(len(notes), 1, notes)
		self.assertIn("confirmation is waiting", notes[0].email_content)

	def test_a_manual_run_does_not_write_a_notification(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv, state="errored", error="The model is overloaded.")
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


# --------------------------------------------------------------------------- #
# The identity rule, pinned on its own (no second guard standing behind it)
# --------------------------------------------------------------------------- #
class TestTheIdentityRuleStandsOnItsOwn(MacroRunOutcomeBase):
	"""The tests above leave the step's turn in flight, which the fallback for an
	unidentified turn also refuses. Here the step's turn has ENDED, so only reading
	the ended turn's own identity can tell a typed message from the step."""

	def test_a_typed_turn_is_ignored_even_after_the_steps_turn_ended(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		self._turn(conv, state="done")  # step 1 ended; its own event has not arrived yet
		typed, _, _ = self._turn(conv, origin="human")
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False, run_id=typed)
		enqueue.assert_not_called()
		self.assertEqual(self._run(run).current_step, 1)

	def test_a_follow_up_named_by_its_reply_is_ignored_after_the_step_ended(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		self._turn(conv, state="done")
		_, _, follow_up_reply = self._turn(conv, origin="continuation")
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False, assistant_message=follow_up_reply)
		enqueue.assert_not_called()

	def test_an_unidentified_turn_advances_once_the_steps_turn_has_ended(self):
		# The positive half of the fallback: with the step's Turn ended, an event
		# nobody can identify is taken to be the step's.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		self._turn(conv, state="done")
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False)
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(self._run(run).current_step, 2)

	def test_the_steps_turn_ends_the_run_while_it_is_still_finalizing(self):
		# The state a successful turn is really in when finalize calls the hook.
		run, conv, _ = self._mk_run(steps=1, at_step=1)
		step_turn, _, _ = self._turn(conv, state="finalizing")
		macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		self.assertEqual(self._run(run).status, "completed")


class TestEveryCallerSaysWhichTurnEnded(FrappeTestCase):
	"""The hook can only tell turns apart when its callers name them."""

	def test_finalize_names_the_turn(self):
		from jarvis.chat import finalize

		ctx = finalize._Ctx(
			run_id="run-x", turn={}, conversation="conv-x", owner="o@example.com", errored=True, payload={}
		)
		with (
			patch("jarvis.chat.macros.advance_after_turn") as advance,
			patch("jarvis.chat.turn_message_binding.on_terminal_turn"),
			patch("jarvis.learning.app_analysis.on_turn_end"),
		):
			finalize._effect_macro_advance(ctx)
		advance.assert_called_once_with("conv-x", errored=True, run_id="run-x")

	def test_the_legacy_worker_names_the_turn_and_its_message(self):
		from jarvis.chat import turn_handler

		with patch("jarvis.chat.macros.advance_after_turn") as advance:
			turn_handler._advance_macro("conv-x", errored=False, seed_message="msg-x")
		advance.assert_called_once_with("conv-x", errored=False, run_id=None, seed_message="msg-x")

	def test_every_exit_of_the_legacy_worker_passes_the_message(self):
		# Five exits call the hook. One left without the message would be a turn the
		# engine cannot place on a site with no Turn rows.
		import inspect
		import re

		from jarvis.chat import turn_handler

		source = inspect.getsource(turn_handler.handle_chat_send)
		calls = re.findall(r"_advance_macro\((.*?)\n?\s*\)", source, flags=re.S)
		self.assertGreaterEqual(len(calls), 5, "premise: the exits that call the hook")
		for call in calls:
			self.assertIn("seed_message=message_id", call, call)


# --------------------------------------------------------------------------- #
# A step that could not be started never strands the run
# --------------------------------------------------------------------------- #
class TestAStepThatCouldNotStart(MacroRunOutcomeBase):
	def test_a_dispatch_that_raises_ends_the_run_and_says_so(self):
		# _enqueue_turn commits the step's message and only then creates its turn. A
		# raise in between left a message nothing would ever end for: the run sat
		# `running` until the stale-run sweep, hours later.
		run, conv, _ = self._mk_run(steps=3, at_step=1, trigger="scheduled")
		step_turn, _, _ = self._turn(conv)
		with patch("jarvis.chat.api._enqueue_turn", side_effect=RuntimeError("gateway down")):
			macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		row = self._run(run)
		self.assertEqual(row.status, "failed")
		self.assertIn("Step 2 could not be started", row.error)
		notes = frappe.get_all("Notification Log", filters={"for_user": OWNER}, pluck="email_content")
		self.assertEqual(len(notes), 1, "a scheduled run that died starting a step told nobody")

	def test_a_message_that_never_became_a_turn_is_not_the_current_step(self):
		# The worker died between the commit and the dispatch: step 2's message exists,
		# its turn does not, and current_step was never advanced. The re-delivered
		# event for step 1 is what recovers the run, so it must still count.
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		step_turn, _, _ = self._turn(conv)  # step 1
		self._msg(conv, "user", origin="macro")  # step 2: message only
		frappe.db.commit()
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		self.assertEqual(enqueue.call_count, 1, "the run was stranded behind a message with no turn")
		self.assertEqual(self._run(run).current_step, 2)

	def test_a_step_whose_turn_is_running_is_not_mistaken_for_one_that_never_started(self):
		run, conv, _ = self._mk_run(steps=3, at_step=2)
		old_turn, _, _ = self._turn(conv)  # step 1
		self._turn(conv, state="streaming")  # step 2, has its Turn
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=False, run_id=old_turn)
		enqueue.assert_not_called()

	def test_a_message_that_never_became_a_turn_is_not_counted_as_a_step(self):
		run, conv, _ = self._mk_run(steps=2, at_step=2, stop_on_error=0)
		self._turn(conv)  # step 1
		self._msg(conv, "user", origin="macro")  # a dispatch that died
		last, _, _ = self._turn(conv, state="errored", error="The model is overloaded.")  # step 2
		macros.advance_after_turn(conv, errored=True, run_id=last)
		self.assertEqual(
			self._run(run).error, "Finished, but 1 of 2 steps failed. Step 2: The model is overloaded."
		)

	def test_describing_the_run_never_keeps_it_from_ending(self):
		run, conv, _ = self._mk_run(steps=1, at_step=1)
		step_turn, _, _ = self._turn(conv)
		with (
			patch("jarvis.chat.macros._run_outcome", side_effect=RuntimeError("boom")),
			patch("frappe.log_error") as log,
		):
			macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		self.assertEqual(self._run(run).status, "completed")
		self.assertTrue([c for c in log.call_args_list if "outcome" in str(c.kwargs.get("title"))])


# --------------------------------------------------------------------------- #
# The outcome is left in the run's conversation
# --------------------------------------------------------------------------- #
class TestTheRunsConversationSaysHowItEnded(MacroRunOutcomeBase):
	def _closing(self, conv, run):
		return frappe.get_all(
			MSG,
			filters={"conversation": conv, "role": "assistant", "ref_doctype": RUN, "ref_name": run},
			fields=["name", "content", "seq", "owner"],
		)

	def test_a_failed_run_leaves_its_reason_as_the_last_message(self):
		# Someone who opens the conversation later (from a notification, from the Runs
		# tab, on a phone) found a transcript that simply ended.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv, state="errored", error="The model is overloaded.")
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		(closing,) = self._closing(conv, run)
		self.assertEqual(closing.content, "✗ Macro failed. Step 1 failed: The model is overloaded.")
		self.assertEqual(closing.owner, OWNER)
		self.assertEqual(
			closing.seq,
			frappe.db.sql("SELECT MAX(seq) FROM `tabJarvis Chat Message` WHERE conversation=%s", conv)[0][0],
		)

	def test_a_run_stopped_at_a_card_says_so_in_the_conversation(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv)
		self._park_card(conv)
		macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		(closing,) = self._closing(conv, run)
		self.assertTrue(closing.content.startswith("■ Macro stopped. Stopped at step 1 of 2"))

	def test_a_run_that_simply_worked_adds_nothing(self):
		run, conv, _ = self._mk_run(steps=1, at_step=1)
		step_turn, _, _ = self._turn(conv)
		macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		self.assertEqual(self._closing(conv, run), [])

	def test_the_closing_message_is_posted_once(self):
		run, conv, macro = self._mk_run(steps=2, at_step=1)
		run_doc, macro_doc = frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)
		macros._announce(run_doc, macro_doc, "failed", "Step 1 failed: The model is overloaded.")
		macros._announce(run_doc, macro_doc, "failed", "Step 1 failed: The model is overloaded.")
		self.assertEqual(len(self._closing(conv, run)), 1)

	def test_an_open_chat_is_told_to_reload(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, _ = self._turn(conv, state="errored", error="The model is overloaded.")
		with patch("jarvis.chat.macros.publish_to_user") as publish:
			macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		closed = [c.args for c in publish.call_args_list if c.args[1].get("kind") == "macro:closed"]
		self.assertEqual(len(closed), 1)
		self.assertEqual((closed[0][0], closed[0][1]["conversation_id"]), (OWNER, conv))

	def test_a_failure_to_post_does_not_keep_the_owner_from_being_told(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1, trigger="scheduled")
		step_turn, _, _ = self._turn(conv, state="errored", error="The model is overloaded.")
		with patch("jarvis.chat.macros._post_closing_message", side_effect=RuntimeError("db")):
			macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		self.assertEqual(self._run(run).status, "failed")
		self.assertEqual(len(frappe.get_all("Notification Log", filters={"for_user": OWNER})), 1)


# --------------------------------------------------------------------------- #
# The older terminal paths carry the reason too
# --------------------------------------------------------------------------- #
class TestTheOtherWaysARunEnds(MacroRunOutcomeBase):
	def _done(self, publish):
		return [c.args[1] for c in publish.call_args_list if c.args[1].get("kind") == "macro:done"]

	def test_a_run_that_never_got_capacity_says_so_and_notifies(self):
		run, conv, _ = self._mk_run(steps=2, at_step=0, trigger="scheduled")
		frappe.db.set_value(
			RUN, run, {"status": "waiting_capacity", "capacity_attempts": macros._MAX_CAPACITY_ATTEMPTS}
		)
		frappe.db.commit()
		with patch("jarvis.chat.macros.publish_to_user") as publish:
			macros.resume_waiting_capacity_runs()
		self.assertEqual(self._run(run).status, "failed")
		(event,) = self._done(publish)
		self.assertEqual(event["error"], macros._NO_CAPACITY_ERROR)
		self.assertEqual(len(frappe.get_all("Notification Log", filters={"for_user": OWNER})), 1)

	def test_a_run_closed_by_the_stale_sweep_carries_the_reason(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		frappe.db.set_value(RUN, run, {"status": "failed", "error": macros._STALE_RUN_ERROR})
		frappe.db.commit()
		with patch("jarvis.chat.macros.publish_to_user") as publish:
			macros._announce_reaped(run)
		(event,) = self._done(publish)
		self.assertEqual(event["error"], macros._STALE_RUN_ERROR)

	def test_a_run_whose_conversation_was_deleted_is_announced(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		frappe.set_user("Administrator")
		frappe.db.delete(TURN, {"conversation": conv})
		frappe.db.delete(MSG, {"conversation": conv})
		frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.set_value(RUN, run, "conversation", conv, update_modified=False)  # the stale link
		frappe.db.commit()
		with patch("jarvis.chat.macros.publish_to_user") as publish:
			macros.advance_after_turn(conv, errored=False)
		self.assertEqual(self._run(run).status, "failed")
		(event,) = self._done(publish)
		self.assertEqual(event["error"], macros._CONVERSATION_GONE_ERROR)


# --------------------------------------------------------------------------- #
# Numbers the screens sort and summarise by
# --------------------------------------------------------------------------- #
class TestRunNumbers(MacroRunOutcomeBase):
	def test_a_run_started_by_hand_stamps_when_the_macro_last_ran(self):
		# The list sorts "Last run" by last_run_at, which only the scheduler stamped: a
		# macro run by hand sorted as if it had never run.
		frappe.set_user(OWNER)
		macro = frappe.get_doc(
			{"doctype": MACRO, "macro_name": f"{PFX}-stamp", "steps": [{"prompt": "a"}]}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self.assertFalse(frappe.db.get_value(MACRO, macro.name, "last_run_at"))
		with patch("jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}):
			macros.run_macro(macro.name)
		self.assertTrue(frappe.db.get_value(MACRO, macro.name, "last_run_at"))

	def test_a_run_the_engine_stopped_counts_against_the_success_rate(self):
		# `stopped` used to mean "the user cancelled", so it was left out of the rate.
		# A macro that stops at the same confirmation every day would read 100%.
		for status, error in (
			("completed", ""),
			("stopped", "Stopped at step 1 of 2: a confirmation is waiting in the conversation."),
			("stopped", ""),  # the user's own Stop: not an outcome
		):
			run, _, _ = self._mk_run(tag=f"rate-{status}-{bool(error)}")
			frappe.db.set_value(RUN, run, {"status": status, "error": error})
		frappe.db.commit()
		frappe.set_user(OWNER)
		stats = macros_api.macro_run_stats()
		self.assertEqual(stats["success_rate"], 50)
		self.assertEqual(stats["stopped"], 2)
