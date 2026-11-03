"""How a macro run reaches its owner once it ends, and where its chat is NOT shown.

* Every run that finished or failed, scheduled or started by hand, leaves a
  Notification Log linked to the run's chat. A run the engine stopped (a step waiting
  on a card) does too; one the owner stopped themselves does not. Before, only a
  scheduled failure and a one-time success were told, and a failure had no link.
* A run's chat is a run log: the chat list and its search leave it out (it opens
  from the Runs page), and a scheduled run no longer pushes a "conversation:new"
  for a chat the list never shows.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from jarvis.chat import api as chat_api
from jarvis.chat import macros
from jarvis.tests import test_macro_run_outcome as outcome

CONV = outcome.CONV
MACRO = outcome.MACRO
RUN = outcome.RUN
OWNER = outcome.OWNER


class TestTheOwnerIsToldHowARunEnded(outcome.MacroRunOutcomeBase):
	def _notes(self):
		return frappe.get_all(
			"Notification Log",
			filters={"for_user": OWNER},
			fields=["subject", "email_content", "link"],
			order_by="creation asc",
		)

	def _announce(self, status, error="", *, trigger="manual"):
		run, conv, macro = self._mk_run(trigger=trigger, tag=f"{status}-{trigger}")
		frappe.set_user("Administrator")
		with patch.object(macros, "_post_closing_message"), patch.object(macros, "_publish_done"):
			macros._announce(frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro), status, error)
		return conv, frappe.db.get_value(MACRO, macro, "macro_name")

	def test_a_run_started_by_hand_that_finished_is_told_with_a_link(self):
		conv, name = self._announce("completed")
		(note,) = self._notes()
		self.assertEqual(note.subject, f"Macro run finished: {name}")
		self.assertEqual(note.link, f"/jarvis/c/{conv}")
		self.assertIn("Open the chat", note.email_content)

	def test_a_recurring_scheduled_run_that_finished_is_told(self):
		conv, name = self._announce("completed", trigger="scheduled")
		(note,) = self._notes()
		self.assertEqual(note.subject, f"Macro run finished: {name}")
		self.assertEqual(note.link, f"/jarvis/c/{conv}")

	def test_a_finished_run_with_a_note_says_the_note(self):
		self._announce("completed", "Step 1 left a draft for you to apply in the chat.")
		(note,) = self._notes()
		self.assertIn("left a draft", note.email_content)

	def test_a_failed_run_is_told_with_its_reason_and_a_link(self):
		for trigger in ("manual", "scheduled"):
			with self.subTest(trigger=trigger):
				conv, name = self._announce("failed", "Step 2 failed: no such customer.", trigger=trigger)
				(note,) = self._notes()
				self.assertEqual(note.subject, f"Macro run failed: {name}")
				self.assertIn("no such customer", note.email_content)
				self.assertEqual(note.link, f"/jarvis/c/{conv}")
				self._purge()

	def test_a_run_the_engine_stopped_is_told(self):
		self._announce("stopped", "Stopped at step 1 of 2: a confirmation is waiting for you.")
		(note,) = self._notes()
		self.assertTrue(note.subject.startswith("Macro run stopped: "))

	def test_a_run_the_owner_stopped_is_not_told(self):
		self._announce("stopped", "")
		self.assertEqual(self._notes(), [])

	def test_a_run_whose_chat_is_gone_is_told_without_a_link(self):
		run, conv, macro = self._mk_run(tag="gone")
		frappe.set_user("Administrator")
		frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		with patch.object(macros, "_post_closing_message"), patch.object(macros, "_publish_done"):
			macros._announce(frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro), "completed", "")
		(note,) = self._notes()
		self.assertFalse(note.link)

	def test_a_run_that_ends_through_the_hook_is_told(self):
		run, conv, _ = self._mk_run(steps=1, at_step=1, tag="hook")
		step_turn, _, _ = self._turn(conv)
		macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		self.assertEqual(self._run(run).status, "completed")
		(note,) = self._notes()
		self.assertEqual(note.link, f"/jarvis/c/{conv}")


class TestTheMacroNameInASubjectIsEscaped(outcome.MacroRunOutcomeBase):
	"""The desk bell renders a Notification Log subject as HTML; the Macros list shows
	the body as text. So the name is escaped in every subject that holds it."""

	NAME = "<b>Q4</b>"

	def _subjects(self):
		return frappe.get_all("Notification Log", filters={"for_user": OWNER}, pluck="subject")

	def _assert_escaped(self):
		(subject,) = self._subjects()
		self.assertIn("&lt;b&gt;Q4&lt;/b&gt;", subject)
		self.assertNotIn("<b>", subject)

	def test_a_run_outcome(self):
		run, _, macro = self._mk_run(tag=self.NAME)
		frappe.set_user("Administrator")
		with patch.object(macros, "_post_closing_message"), patch.object(macros, "_publish_done"):
			macros._announce(frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro), "failed", "no")
		self._assert_escaped()

	def test_a_reaped_run(self):
		run, _, _ = self._mk_run(tag=self.NAME)
		frappe.set_user("Administrator")
		with patch.object(macros, "_publish_done"):
			macros._announce_reaped(run)
		self._assert_escaped()

	def test_a_scheduled_macro_that_did_not_run(self):
		from jarvis.chat import macro_scheduler

		frappe.set_user("Administrator")
		row = frappe._dict(owner=OWNER, name="x", macro_name=self.NAME)
		macro_scheduler._notify_owner(row, "reason")
		self._assert_escaped()


class TestARunsChatIsNotInTheChatList(outcome.MacroRunOutcomeBase):
	def _plain_chat(self, title):
		frappe.set_user(OWNER)
		conv = frappe.get_doc({"doctype": CONV, "title": title})
		conv.flags.ignore_permissions = True
		conv.insert()
		self._msg(conv.name, "user", content="hello")
		frappe.db.commit()
		return conv.name

	def test_the_list_and_its_search_leave_a_runs_chat_out(self):
		plain = self._plain_chat(f"{outcome.PFX} plain chat")
		_, run_conv, _ = self._mk_run(tag="listed")
		self._msg(run_conv, "user", content="step 1")
		frappe.db.commit()
		frappe.set_user(OWNER)
		listed = [r.name for r in chat_api.list_conversations(refresh=True)]
		self.assertIn(plain, listed)
		self.assertNotIn(run_conv, listed)
		found = [r.name for r in chat_api.search_conversations(search=outcome.PFX)["rows"]]
		self.assertIn(plain, found)
		self.assertNotIn(run_conv, found)
		self.assertEqual(chat_api.search_conversations(search=outcome.PFX)["total"], len(found))

	def test_a_runs_chat_still_opens_at_its_own_url(self):
		_, run_conv, _ = self._mk_run(tag="opens")
		self._msg(run_conv, "user", content="step 1")
		frappe.db.commit()
		frappe.set_user(OWNER)
		messages = chat_api.get_conversation(run_conv)["messages"]
		self.assertEqual([m["content"] for m in messages], ["step 1"])

	def test_a_scheduled_run_pushes_no_new_conversation(self):
		frappe.set_user(OWNER)
		macro = frappe.get_doc(
			{"doctype": MACRO, "macro_name": f"{outcome.PFX}-sched", "steps": [{"prompt": "step 1"}]}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		with (
			patch.object(macros, "entitlement_block", return_value=None),
			patch.object(macros, "_dispatch_step"),
			patch("jarvis.chat.macros.publish_to_user") as publish,
		):
			out = macros.run_macro(macro.name, trigger="scheduled")
		self.assertTrue(out["ok"])
		kinds = [c.args[1].get("kind") for c in publish.call_args_list]
		self.assertNotIn("conversation:new", kinds)

	def test_the_run_table_is_indexed_on_its_conversation(self):
		indexes = frappe.db.sql("SHOW INDEX FROM `tabJarvis Macro Run` WHERE Column_name = 'conversation'")
		self.assertTrue(indexes)
