"""The Macros screens' server half: what a save keeps, what a delete removes, and
what the chat is told about the message that closes a run.

Small fixes from the 2026-10-01 macros review:

* a step with a label and no prompt was dropped by the save without a word;
* deleting a macro removed its run history one ``delete_doc`` at a time;
* the chat could not tell the engine's closing message from a reply, so it took the
  Retry control off the failed step's reply (``get_conversation`` did not say).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from jarvis.chat import api as chat_api
from jarvis.chat import macros, macros_api
from jarvis.tests import test_macro_run_outcome as outcome
from jarvis.tests._pending_action_helpers import ensure_user

CONV = outcome.CONV
MACRO = outcome.MACRO
RUN = outcome.RUN
OWNER = outcome.OWNER


class TestAStepIsNeverDroppedSilently(outcome.MacroRunOutcomeBase):
	def test_a_labelled_step_with_no_prompt_is_refused_by_name(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			macros_api._parse_steps([{"prompt": "first"}, {"label": "Post the entries", "prompt": "  "}])
		self.assertIn("Step 2", str(cm.exception))
		self.assertIn("Post the entries", str(cm.exception))

	def test_a_wholly_empty_step_is_still_dropped(self):
		# The form always carries one blank card to type into; it is not a step.
		rows = macros_api._parse_steps(
			[{"prompt": "first"}, {"label": " ", "prompt": ""}, {}, {"prompt": "second"}]
		)
		self.assertEqual([r["prompt"] for r in rows], ["first", "second"])

	def test_the_json_string_form_is_refused_the_same_way(self):
		with self.assertRaises(frappe.ValidationError):
			macros_api._parse_steps(frappe.as_json([{"label": "Only a label", "prompt": ""}]))

	def test_a_refused_save_leaves_the_macro_as_it_was(self):
		_, _, macro = self._mk_run(steps=2)
		frappe.set_user(OWNER)
		with self.assertRaises(frappe.ValidationError):
			macros_api.update_macro(
				macro, steps=frappe.as_json([{"prompt": "step 1"}, {"label": "Lost", "prompt": ""}])
			)
		frappe.db.rollback()
		self.assertEqual(
			frappe.get_all("Jarvis Macro Step", filters={"parent": macro}, pluck="prompt", order_by="idx"),
			["step 1", "step 2"],
		)


class TestDeletingAMacroTakesItsRunHistory(outcome.MacroRunOutcomeBase):
	def _more_runs(self, macro, n):
		frappe.set_user(OWNER)
		for _ in range(n):
			run = frappe.get_doc(
				{"doctype": RUN, "macro": macro, "status": "completed", "total_steps": 1, "trigger": "manual"}
			)
			run.flags.ignore_permissions = True
			run.insert()
		frappe.db.commit()

	def test_the_runs_go_in_one_statement_not_one_delete_per_row(self):
		_, conv, doomed = self._mk_run(tag="doomed")
		kept_run, _, kept = self._mk_run(tag="kept")
		self._more_runs(doomed, 4)
		frappe.set_user(OWNER)
		with patch("frappe.delete_doc", wraps=frappe.delete_doc) as delete_doc:
			self.assertEqual(macros_api.delete_macro(doomed), {"ok": True})
		self.assertEqual([c.args[0] for c in delete_doc.call_args_list], [MACRO])
		self.assertFalse(frappe.db.exists(MACRO, doomed))
		self.assertEqual(frappe.db.count(RUN, {"macro": doomed}), 0)
		# Only this macro's history: another macro's run, and the run's conversation,
		# are not execution history of the deleted macro.
		self.assertTrue(frappe.db.exists(RUN, kept_run))
		self.assertTrue(frappe.db.exists(MACRO, kept))
		self.assertTrue(frappe.db.exists(CONV, conv))

	def test_someone_elses_macro_keeps_its_runs(self):
		# The owner gate comes before the run rows are touched.
		run, _, macro = self._mk_run()
		frappe.set_user("Administrator")
		frappe.set_user(ensure_user("macro-api-intruder@example.com"))
		with self.assertRaises(frappe.PermissionError):
			macros_api.delete_macro(macro)
		frappe.set_user("Administrator")
		self.assertTrue(frappe.db.exists(RUN, run))
		self.assertTrue(frappe.db.exists(MACRO, macro))


class TestTheChatIsToldWhichMessageClosesARun(outcome.MacroRunOutcomeBase):
	def test_the_closing_message_carries_its_run_and_a_reply_does_not(self):
		# The desktop chat offers Retry on the last message only. The closing message
		# comes after the failed reply, so the chat has to be able to tell the two apart.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn, _, reply = self._turn(conv, state="errored", error="The model is overloaded.")
		macros.advance_after_turn(conv, errored=True, run_id=step_turn)
		frappe.set_user(OWNER)
		messages = chat_api.get_conversation(conv)["messages"]
		closing = messages[-1]
		self.assertEqual((closing["ref_doctype"], closing["ref_name"]), (RUN, run))
		self.assertTrue(closing["content"].startswith("✗ Macro failed."))
		failed = next(m for m in messages if m["name"] == reply)
		self.assertFalse(failed.get("ref_doctype"))
