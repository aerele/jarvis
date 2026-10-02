"""Saving a macro and its summary: who decides that the steps changed, and when a
summary is started.

The form posts ``steps`` on EVERY save, so "steps were sent" never meant "steps
changed". The server used to read it that way: a rename, a reschedule or the
enabled switch wiped the stored summary, or the "summarizing" mark of one in flight
(whose result was then dropped). The form made up for it by guessing, and its guess
started a model call on every save of a macro whose summary was empty or had failed.

Here the SERVER compares the steps with the stored ones and answers ``summarize``;
``summarize_macro`` is idempotent while a summary is pending, passes the entitlement
gate, and does not leave "summarizing" behind a dispatch that never went out.

Every save below posts what the form posts (all fields, ``steps`` included).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import macros
from jarvis.chat.macros_api import create_macro, get_macro, summarize_macro, update_macro
from jarvis.tests import race_harness
from jarvis.tests._pending_action_helpers import ensure_user

MACRO = "Jarvis Macro"
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
PFX = "summary-save"
# A named user: Administrator cannot put a macro on a schedule, and one of the saves
# below is a reschedule.
OWNER = "macro-summary-owner@example.com"

TWO_STEPS = [
	{"label": "a", "prompt": "Sales analytics for last quarter"},
	{"label": "b", "prompt": "Find the highest outstanding customer"},
]
ENQUEUED = {"run_id": "r1", "message_id": "m1"}


def _delete_conversation(name: str) -> None:
	frappe.db.delete(TURN, {"conversation": name})
	frappe.db.delete(MSG, {"conversation": name})
	frappe.delete_doc(CONV, name, force=True, ignore_permissions=True)


class _SummarySaveBase(FrappeTestCase):
	"""These flows commit (``update_macro``, ``summarize_macro``), so nothing here is
	undone by the per-test rollback: every macro and conversation is removed by name."""

	def setUp(self):
		super().setUp()
		ensure_user(OWNER)
		self._wipe()
		frappe.set_user(OWNER)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		self._wipe()
		super().tearDown()

	def _wipe(self):
		for name in frappe.get_all(MACRO, filters={"macro_name": ["like", f"{PFX}-%"]}, pluck="name"):
			frappe.delete_doc(MACRO, name, force=True, ignore_permissions=True)
		for name in frappe.get_all(CONV, filters={"title": ["like", f"%{PFX}-%"]}, pluck="name"):
			_delete_conversation(name)
		frappe.db.commit()

	def _macro(self, steps=None) -> str:
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-{frappe.generate_hash(length=6)}",
				"enabled": 1,
				"steps": [dict(s) for s in (steps or TWO_STEPS)],
			}
		)
		doc.insert()
		frappe.db.commit()
		return doc.name

	def _conversation(self) -> str:
		conv = frappe.get_doc({"doctype": CONV, "title": f"Merge: {PFX}-fixture", "status": "Archived"})
		conv.flags.ignore_permissions = True
		conv.insert()
		return conv.name

	def _set_summary(self, name: str, *, text: str | None, status: str | None, conversation: str | None):
		frappe.db.set_value(
			MACRO,
			name,
			{"merged_prompt": text, "merge_status": status, "merge_conversation": conversation},
			update_modified=False,
		)
		frappe.db.commit()

	def _pending(self, name: str, text: str | None = None) -> str:
		conv = self._conversation()
		self._set_summary(name, text=text, status="pending", conversation=conv)
		return conv

	def _summary(self, name: str) -> tuple:
		"""The three summary columns exactly as stored (NULL stays None)."""
		return frappe.db.sql(
			"SELECT merged_prompt, merge_status, merge_conversation FROM `tabJarvis Macro` WHERE name = %s",
			name,
		)[0]

	def _save(self, name: str, **changes) -> dict:
		"""One save the way the form posts it: every field, and the steps, every time."""
		shown = get_macro(name)
		payload = {
			"macro_name": shown["macro_name"],
			"description": shown["description"],
			"steps": frappe.as_json(shown["steps"]),
			"enabled": shown["enabled"],
			"stop_on_error": shown["stop_on_error"],
			"skip_confirmation": shown["skip_confirmation"],
			"schedule_enabled": shown["schedule_enabled"],
			"schedule_frequency": shown["schedule_frequency"],
			"schedule_time": "09:00",
			"schedule_weekday": "",
			"schedule_day_of_month": 0,
		}
		if "steps" in changes:
			changes["steps"] = frappe.as_json(changes["steps"])
		payload.update(changes)
		return update_macro(name, **payload)["data"]


# A rename, a reschedule and the enabled switch: three saves that leave the steps alone.
NOT_THE_STEPS = (
	{"macro_name": f"{PFX}-renamed"},
	{"schedule_enabled": 1, "schedule_frequency": "weekly", "schedule_weekday": "Monday"},
	{"enabled": 0},
)


class TestASaveThatLeavesTheStepsAlone(_SummarySaveBase):
	def _assert_untouched(self, name: str):
		before = self._summary(name)
		for changes in NOT_THE_STEPS:
			with self.subTest(changes=changes):
				saved = self._save(name, **changes)
				self.assertEqual(self._summary(name), before)
				self.assertIs(saved["summarize"], False)

	def test_keeps_a_summary_that_is_being_written(self):
		# The landing lookup needs "pending" AND the link: without either, the summary
		# in flight is dropped when its turn ends.
		name = self._macro()
		conv = self._pending(name)
		self._assert_untouched(name)
		self.assertEqual(self._summary(name)[1:], ("pending", conv))

	def test_keeps_the_old_summary_while_a_new_one_is_being_written(self):
		# Re-summarize on a macro that has a summary: the old text stays until the new lands.
		name = self._macro()
		self._pending(name, text="The summary before the re-summarize.")
		self._assert_untouched(name)

	def test_keeps_a_stored_summary(self):
		name = self._macro()
		self._set_summary(name, text="Analytics, then the top debtor.", status="ready", conversation="")
		self._assert_untouched(name)

	def test_starts_no_summary_for_one_that_failed(self):
		name = self._macro()
		self._set_summary(name, text="", status="failed", conversation="")
		self._assert_untouched(name)

	def test_starts_no_summary_for_a_macro_that_never_had_one(self):
		self._assert_untouched(self._macro())

	def test_the_summary_in_flight_still_lands_after_a_rename(self):
		name = self._macro()
		conv = self._pending(name)
		frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": 2,
				"role": "assistant",
				"content": (
					'```jarvis-macro-merge\n{"mergeable": true, "reason": "", '
					'"merged_prompt": "Analytics, then the top debtor."}\n```'
				),
			}
		).insert(ignore_permissions=True)
		self._save(name, macro_name=f"{PFX}-renamed-mid-summary")
		macros._apply_merge_after_turn(conv, errored=False)
		self.assertEqual(self._summary(name), ("Analytics, then the top debtor.", "ready", ""))

	def test_the_same_steps_spelled_differently_are_not_a_change(self):
		# What the form posts is not byte-for-byte what is stored (padding, a missing
		# key, a step the form keeps blank): compared after _parse_steps, as values.
		name = self._macro()
		self._set_summary(name, text="Analytics, then the top debtor.", status="ready", conversation="")
		before = self._summary(name)
		saved = self._save(
			name,
			steps=[
				{"label": " a ", "prompt": "Sales analytics for last quarter\n"},
				{"label": "", "prompt": ""},
				{"label": "b", "prompt": "Find the highest outstanding customer", "skills": None},
			],
		)
		self.assertEqual(self._summary(name), before)
		self.assertIs(saved["summarize"], False)

	def test_the_step_rows_are_left_as_they_are(self):
		name = self._macro()
		rows = frappe.get_all("Jarvis Macro Step", filters={"parent": name}, pluck="name", order_by="idx")
		self._save(name, macro_name=f"{PFX}-renamed-rows")
		self.assertEqual(
			frappe.get_all("Jarvis Macro Step", filters={"parent": name}, pluck="name", order_by="idx"),
			rows,
		)


class TestASaveThatChangesTheSteps(_SummarySaveBase):
	CHANGES = {
		"text": [TWO_STEPS[0], {"label": "b", "prompt": "Find the LOWEST outstanding customer"}],
		"label": [TWO_STEPS[0], {"label": "renamed", "prompt": TWO_STEPS[1]["prompt"]}],
		"reorder": [TWO_STEPS[1], TWO_STEPS[0]],
		"add": [*TWO_STEPS, {"label": "c", "prompt": "Email them a reminder"}],
		"thinking": [TWO_STEPS[0], {**TWO_STEPS[1], "thinking_override": "high"}],
		"model": [TWO_STEPS[0], {**TWO_STEPS[1], "model_override": "some-model"}],
	}

	def test_clears_the_summary_and_asks_for_a_new_one(self):
		for what, steps in self.CHANGES.items():
			with self.subTest(change=what):
				name = self._macro()
				self._set_summary(
					name, text="Analytics, then the top debtor.", status="ready", conversation=""
				)
				saved = self._save(name, steps=steps)
				self.assertEqual(self._summary(name)[:2], ("", ""))
				self.assertIs(saved["summarize"], True)

	def test_removing_a_step_clears_and_asks_while_two_are_left(self):
		name = self._macro([*TWO_STEPS, {"label": "c", "prompt": "Email them a reminder"}])
		self._set_summary(name, text="All three.", status="ready", conversation="")
		saved = self._save(name, steps=TWO_STEPS)
		self.assertEqual(self._summary(name)[:2], ("", ""))
		self.assertIs(saved["summarize"], True)

	def test_a_tagged_skill_is_part_of_the_step(self):
		skill = frappe.get_doc(
			{
				"doctype": "Jarvis Custom Skill",
				"skill_name": f"{PFX}-{frappe.generate_hash(length=6)}",
				"description": "a skill to tag",
				"instructions": "Do the thing.",
			}
		)
		skill.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Custom Skill", skill.name, force=True, ignore_permissions=True)
		)
		name = self._macro()
		self._set_summary(name, text="Analytics, then the top debtor.", status="ready", conversation="")
		saved = self._save(name, steps=[{**TWO_STEPS[0], "skills": [skill.name]}, TWO_STEPS[1]])
		self.assertEqual(self._summary(name)[:2], ("", ""))
		self.assertIs(saved["summarize"], True)
		# And the same tagged steps again are no change.
		self._set_summary(name, text="With the skill.", status="ready", conversation="")
		saved = self._save(name, description="only the description")
		self.assertEqual(self._summary(name)[:2], ("With the skill.", "ready"))
		self.assertIs(saved["summarize"], False)

	def test_discards_a_summary_that_was_being_written_for_the_old_steps(self):
		name = self._macro()
		self._pending(name)
		saved = self._save(name, steps=self.CHANGES["text"])
		self.assertEqual(self._summary(name)[:2], ("", ""))
		self.assertIs(saved["summarize"], True)

	def test_a_one_step_macro_is_never_summarized(self):
		name = self._macro()
		self._set_summary(name, text="Analytics, then the top debtor.", status="ready", conversation="")
		saved = self._save(name, steps=[TWO_STEPS[0]])
		self.assertEqual(self._summary(name)[:2], ("", ""))
		self.assertIs(saved["summarize"], False)
		saved = self._save(name, steps=[{"label": "a", "prompt": "Something else"}])
		self.assertIs(saved["summarize"], False)


class TestASummaryWrittenByHand(_SummarySaveBase):
	def test_is_saved_when_the_steps_are_unchanged(self):
		name = self._macro()
		self._set_summary(name, text="Analytics, then the top debtor.", status="ready", conversation="")
		saved = self._save(name, merged_prompt="  My own wording.  ")
		self.assertEqual(self._summary(name)[:2], ("My own wording.", "ready"))
		self.assertIs(saved["summarize"], False)

	def test_is_saved_when_the_steps_changed_too_and_no_summary_is_started(self):
		name = self._macro()
		self._set_summary(name, text="Analytics, then the top debtor.", status="ready", conversation="")
		saved = self._save(
			name, steps=TestASaveThatChangesTheSteps.CHANGES["text"], merged_prompt="My own wording."
		)
		self.assertEqual(self._summary(name)[:2], ("My own wording.", "ready"))
		self.assertIs(saved["summarize"], False)

	def test_replaces_one_that_is_being_written(self):
		# The owner's text wins over the summary in flight, which no longer lands.
		name = self._macro()
		conv = self._pending(name)
		saved = self._save(name, merged_prompt="My own wording.")
		self.assertEqual(self._summary(name)[:2], ("My own wording.", "ready"))
		self.assertIs(saved["summarize"], False)
		macros._apply_merge_after_turn(conv, errored=False)
		self.assertEqual(self._summary(name)[:2], ("My own wording.", "ready"))

	def test_emptied_with_unchanged_steps_stays_empty(self):
		name = self._macro()
		self._set_summary(name, text="Analytics, then the top debtor.", status="ready", conversation="")
		saved = self._save(name, merged_prompt="")
		self.assertEqual(self._summary(name)[:2], ("", ""))
		self.assertIs(saved["summarize"], False)

	def test_the_stored_text_sent_back_is_not_an_edit(self):
		# A form built before this change posts the summary it was shown on every save
		# that leaves the steps alone. That must not end a summary in flight, nor turn a
		# failed one into "ready".
		name = self._macro()
		self._pending(name, text="The summary before the re-summarize.")
		before = self._summary(name)
		saved = self._save(name, macro_name=f"{PFX}-old-form", merged_prompt=before[0])
		self.assertEqual(self._summary(name), before)
		self.assertIs(saved["summarize"], False)

		self._set_summary(name, text="", status="failed", conversation="")
		self._save(name, macro_name=f"{PFX}-old-form-2", merged_prompt="")
		self.assertEqual(self._summary(name)[:2], ("", "failed"))

	def test_the_stored_text_sent_back_with_changed_steps_is_cleared(self):
		name = self._macro()
		self._set_summary(name, text="Analytics, then the top debtor.", status="ready", conversation="")
		saved = self._save(
			name,
			steps=TestASaveThatChangesTheSteps.CHANGES["text"],
			merged_prompt="Analytics, then the top debtor.",
		)
		self.assertEqual(self._summary(name)[:2], ("", ""))
		self.assertIs(saved["summarize"], True)


class TestANewMacro(_SummarySaveBase):
	def test_of_two_steps_asks_for_a_summary(self):
		made = create_macro(f"{PFX}-new-two", steps=frappe.as_json(TWO_STEPS))["data"]
		self.assertIs(made["summarize"], True)

	def test_of_one_step_does_not(self):
		made = create_macro(f"{PFX}-new-one", steps=frappe.as_json(TWO_STEPS[:1]))["data"]
		self.assertIs(made["summarize"], False)


class TestSummarizeTwice(_SummarySaveBase):
	def test_the_second_call_returns_the_summary_already_being_written(self):
		name = self._macro()
		with patch("jarvis.chat.api._enqueue_turn", return_value=ENQUEUED) as enqueue:
			first = summarize_macro(name)
			second = summarize_macro(name)
		self.assertEqual(second, {"ok": True, "conversation": first["conversation"]})
		enqueue.assert_called_once()
		title = f"Merge: {frappe.db.get_value(MACRO, name, 'macro_name')}"
		self.assertEqual(frappe.db.count(CONV, {"title": title}), 1)
		self.assertEqual(self._summary(name)[1:], ("pending", first["conversation"]))

	def test_a_pending_mark_whose_chat_is_gone_does_not_block_a_new_summary(self):
		# Nothing will ever land for it; Re-summarize is the owner's way out.
		name = self._macro()
		self._set_summary(name, text="", status="pending", conversation="no-such-conversation")
		with patch("jarvis.chat.api._enqueue_turn", return_value=ENQUEUED) as enqueue:
			started = summarize_macro(name)
		enqueue.assert_called_once()
		self.assertEqual(self._summary(name)[1:], ("pending", started["conversation"]))

	def test_a_second_request_that_read_the_macro_before_the_first_committed(self):
		"""Two requests at once. The second took its snapshot before the first
		committed "pending"; it must still see it, and start nothing."""
		name = self._macro()
		other_conv = f"race-{frappe.generate_hash(length=10)}"

		def scenario():
			frappe.db.rollback()
			self._set_summary(name, text=None, status="", conversation="")
			with race_harness.snapshot_isolation_on(), race_harness.other_connection() as other:
				try:
					race_harness.open_read_view()
					self.assertFalse(frappe.db.get_value(MACRO, name, "merge_status"))
					# The first request, on its own connection: its chat and its mark.
					other.sql(
						"""INSERT INTO `tabJarvis Conversation`
						(name, creation, modified, owner, modified_by, title, status)
						VALUES (%s, NOW(), NOW(), %s, %s, %s, 'Archived')""",
						(other_conv, OWNER, OWNER, f"Merge: {PFX}-race"),
					)
					other.sql(
						"""UPDATE `tabJarvis Macro` SET merge_status = 'pending',
						merge_conversation = %s WHERE name = %s""",
						(other_conv, name),
					)
					other.commit()
					with patch("jarvis.chat.api._enqueue_turn", return_value=ENQUEUED) as enqueue:
						out = summarize_macro(name)
					return out, enqueue.call_count
				finally:
					frappe.db.rollback()
					other.sql("DELETE FROM `tabJarvis Conversation` WHERE name = %s", (other_conv,))
					other.commit()

		def check(result):
			out, dispatched = result
			self.assertEqual(out, {"ok": True, "conversation": other_conv})
			self.assertEqual(dispatched, 0)

		race_harness.assert_fix_closes_race(self, scenario, check)

	def test_a_second_request_waits_for_the_first_before_it_creates_anything(self):
		"""The macro's row is locked BEFORE the throwaway chat is made: a request that
		arrives while another is between its check and its commit waits there."""
		name = self._macro()
		title = f"Merge: {frappe.db.get_value(MACRO, name, 'macro_name')}"
		frappe.db.rollback()
		frappe.db.sql("SET SESSION innodb_lock_wait_timeout = 1")
		try:
			with race_harness.other_connection() as other:
				other.sql("SELECT name FROM `tabJarvis Macro` WHERE name = %s FOR UPDATE", (name,))
				with (
					patch("jarvis.chat.api._enqueue_turn", return_value=ENQUEUED) as enqueue,
					self.assertRaises(frappe.QueryTimeoutError),
				):
					summarize_macro(name)
				enqueue.assert_not_called()
				# Read on this connection, before its rollback: what the request had made.
				self.assertEqual(frappe.db.count(CONV, {"title": title}), 0)
		finally:
			frappe.db.rollback()
			frappe.db.sql("SET SESSION innodb_lock_wait_timeout = DEFAULT")


class TestSummarizePassesTheEntitlementGate(_SummarySaveBase):
	def test_a_refused_summary_creates_nothing(self):
		name = self._macro()
		title = f"Merge: {frappe.db.get_value(MACRO, name, 'macro_name')}"
		with (
			patch("jarvis.chat.policy.validate_can_send", return_value=(False, "usage_limit")),
			patch("jarvis.chat.api._enqueue_turn") as enqueue,
			self.assertRaises(frappe.ValidationError) as refused,
		):
			summarize_macro(name)
		enqueue.assert_not_called()
		self.assertIn("usage limit", str(refused.exception))
		self.assertEqual(frappe.db.count(CONV, {"title": title}), 0)
		self.assertFalse(self._summary(name)[1])
		self.assertFalse(self._summary(name)[2])

	def test_it_is_gated_as_one_attended_turn(self):
		# A person asked for it, so the scheduled-run budget is not what prices it.
		name = self._macro()
		with (
			patch.object(macros, "entitlement_block", return_value=None) as gate,
			patch("jarvis.chat.api._enqueue_turn", return_value=ENQUEUED),
		):
			summarize_macro(name)
		gate.assert_called_once_with(OWNER, steps=1, trigger="manual")

	def test_a_spent_scheduled_budget_does_not_refuse_it(self):
		name = self._macro()
		with (
			patch.object(macros, "_over_step_budget", return_value=True),
			patch("jarvis.chat.policy.validate_can_send", return_value=(True, None)),
			patch("jarvis.chat.api._enqueue_turn", return_value=ENQUEUED) as enqueue,
		):
			self.assertTrue(summarize_macro(name)["ok"])
		enqueue.assert_called_once()


class TestSummarizeWhoseDispatchRaises(_SummarySaveBase):
	def _raising(self, *, turn_went_out: bool):
		"""Stand-in for ``api._enqueue_turn`` that raises. Like the real one it has
		committed the step's message by then; with ``turn_went_out`` it also created
		the Turn row, which is what a dispatch that DID go out leaves behind."""

		def enqueue(conversation, prompt, **kw):
			seed = frappe.get_doc(
				{
					"doctype": MSG,
					"conversation": conversation,
					"seq": 1,
					"role": "user",
					"origin": kw["origin"],
					"content": prompt,
				}
			)
			seed.flags.ignore_permissions = True
			seed.flags.jarvis_server_write = True
			seed.insert()
			if turn_went_out:
				turn = frappe.get_doc(
					{
						"doctype": TURN,
						"run_id": frappe.generate_hash(length=16),
						"conversation": conversation,
						"relay_target_id": "t",
						"turn_class": "interactive",
						"state": "streaming",
						"seed_message": seed.name,
					}
				)
				turn.flags.ignore_permissions = True
				turn.insert()
			frappe.db.commit()
			raise RuntimeError("the gateway went away")

		return patch("jarvis.chat.api._enqueue_turn", side_effect=enqueue)

	def test_before_a_turn_went_out_leaves_no_pending_mark_and_no_chat(self):
		name = self._macro()
		title = f"Merge: {frappe.db.get_value(MACRO, name, 'macro_name')}"
		with self._raising(turn_went_out=False), self.assertRaises(frappe.ValidationError) as refused:
			summarize_macro(name)
		self.assertNotIn("gateway", str(refused.exception))
		frappe.db.rollback()  # what the request does after a raise
		self.assertEqual(self._summary(name)[1:], ("", ""))
		self.assertEqual(frappe.db.count(CONV, {"title": title}), 0)
		self.assertEqual(frappe.db.count(MSG, {"content": ["like", f"%{TWO_STEPS[0]['prompt']}%"]}), 0)

	def test_after_a_turn_went_out_the_mark_stays_and_the_result_lands(self):
		name = self._macro()
		with self._raising(turn_went_out=True):
			out = summarize_macro(name)
		frappe.db.rollback()
		conv = out["conversation"]
		self.assertTrue(out["ok"])
		self.assertEqual(self._summary(name)[1:], ("pending", conv))
		self.assertEqual(frappe.db.get_value(CONV, conv, "status"), "Archived")
		# The turn that went out ends: its outcome reaches the macro.
		macros._apply_merge_after_turn(conv, errored=True)
		self.assertEqual(self._summary(name)[1:], ("failed", ""))
		self.assertFalse(frappe.db.exists(CONV, conv))
