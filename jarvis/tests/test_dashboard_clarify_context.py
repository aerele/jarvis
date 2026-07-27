"""Interview-first on the Dashboards builder: the clarify arm of
``_prepend_doc_context``'s dashboards branch.

Owner-reported (2026-07-27): the builder started drawing off the very first
message instead of agreeing what to build. The frontend sends the same
``{"page": "dashboards"}`` context on every turn and has no notion of a turn
index, so "is this the first build?" is derived SERVER-SIDE here - from the same
``Jarvis Chat Message.canvas`` field ``persist_canvases`` stamps and
``get_conversation`` hands the UI, plus "have I already asked?" so the interview
ends on the answer rather than on a canvas that may never persist.

Run: bench --site patterntest.localhost run-tests --module
     jarvis.tests.test_dashboard_clarify_context
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.turn_handler import (
	_dashboard_has_asked,
	_dashboard_has_canvas,
	_prepend_doc_context,
)

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
CLARIFY_MARK = "NOTHING has been drawn on this canvas yet"
BUILD_NOW_MARK = "You have ALREADY asked your clarifying questions"
ASK_BLOCK = 'Which records?\n\n```jarvis-ask\n[{"q":"Which records?","type":"text"}]\n```'


class _DashboardContextTestCase(FrappeTestCase):
	def setUp(self):
		self._convs: list[str] = []
		self._msgs: list[str] = []

	def tearDown(self):
		# Children first: `conversation` is a Link, so a conversation with live
		# messages cannot be deleted - and a half-deletion would leave message
		# rows pointing at a conversation that no longer exists. NO commit: the
		# FrappeTestCase transaction is what isolates this from the site.
		for name in self._msgs:
			frappe.delete_doc(MSG, name, force=True, ignore_permissions=True, delete_permanently=True)
		for name in self._convs:
			frappe.delete_doc(CONV, name, force=True, ignore_permissions=True, delete_permanently=True)

	def _conversation(self) -> str:
		doc = frappe.get_doc({"doctype": CONV, "title": "dash clarify test"}).insert(ignore_permissions=True)
		self._convs.append(doc.name)
		return doc.name

	def _message(self, conversation: str, seq: int, role: str, canvas=None, content="x") -> str:
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conversation,
				"seq": seq,
				"role": role,
				"content": content,
				"canvas": frappe.as_json(canvas) if canvas else None,
			}
		).insert(ignore_permissions=True)
		self._msgs.append(doc.name)
		return doc.name


class TestDashboardHasCanvas(_DashboardContextTestCase):
	def test_no_conversation_is_not_a_canvas(self):
		self.assertFalse(_dashboard_has_canvas(""))
		self.assertFalse(_dashboard_has_canvas(None))

	def test_messages_without_canvas_do_not_count(self):
		conv = self._conversation()
		self._message(conv, 1, "user")
		self._message(conv, 2, "assistant")
		self.assertFalse(_dashboard_has_canvas(conv))

	def test_one_stamped_canvas_flips_it(self):
		conv = self._conversation()
		self._message(conv, 1, "user")
		self._message(
			conv,
			2,
			"assistant",
			canvas=[{"name": "documents/a/index.html", "title": "T", "type": "html"}],
		)
		self.assertTrue(_dashboard_has_canvas(conv))

	def test_a_generated_image_is_not_a_dashboard(self):
		"""``generated_media`` stamps the same field with type:"image" items, and
		the builder shares its conversation with main chat ("Open in chat") - a
		picture must not suppress the interview."""
		conv = self._conversation()
		self._message(
			conv,
			1,
			"assistant",
			canvas=[{"name": "/files/x.png", "title": "Generated image", "type": "image"}],
		)
		self.assertFalse(_dashboard_has_canvas(conv))
		# ...but an html item alongside an image still counts
		self._message(
			conv,
			2,
			"assistant",
			canvas=[
				{"name": "/files/x.png", "type": "image"},
				{"name": "documents/a/index.html", "type": "html"},
			],
		)
		self.assertTrue(_dashboard_has_canvas(conv))

	def test_another_conversations_canvas_does_not_leak(self):
		mine = self._conversation()
		theirs = self._conversation()
		self._message(
			theirs,
			1,
			"assistant",
			canvas=[{"name": "documents/a/index.html", "type": "html"}],
		)
		self.assertFalse(_dashboard_has_canvas(mine))
		self.assertTrue(_dashboard_has_canvas(theirs))


class TestDashboardHasAsked(_DashboardContextTestCase):
	def test_no_conversation_has_not_asked(self):
		self.assertFalse(_dashboard_has_asked(""))
		self.assertFalse(_dashboard_has_asked(None))

	def test_plain_turns_are_not_an_ask(self):
		conv = self._conversation()
		self._message(conv, 1, "user", content="sales dashboard please")
		self._message(conv, 2, "assistant", content="Here is what I found.")
		self.assertFalse(_dashboard_has_asked(conv))

	def test_an_assistant_ask_block_flips_it(self):
		conv = self._conversation()
		self._message(conv, 1, "assistant", content=ASK_BLOCK)
		self.assertTrue(_dashboard_has_asked(conv))

	def test_a_user_quoting_the_fence_is_not_an_ask(self):
		"""Only the ASSISTANT asking counts - a user pasting the fence must not
		switch the builder into "you already asked"."""
		conv = self._conversation()
		self._message(conv, 1, "user", content=ASK_BLOCK)
		self.assertFalse(_dashboard_has_asked(conv))

	def test_another_conversations_ask_does_not_leak(self):
		mine = self._conversation()
		theirs = self._conversation()
		self._message(theirs, 1, "assistant", content=ASK_BLOCK)
		self.assertFalse(_dashboard_has_asked(mine))
		self.assertTrue(_dashboard_has_asked(theirs))


class TestDashboardsClarifyArm(_DashboardContextTestCase):
	def test_first_turn_asks_instead_of_building(self):
		conv = self._conversation()
		out = _prepend_doc_context("sales dashboard please", {"page": "dashboards"}, conv)
		self.assertIn(CLARIFY_MARK, out)
		self.assertIn("```jarvis-ask", out)
		self.assertIn("do NOT build", out)
		# the 2-3 decisions that shape the build
		self.assertIn("the data scope", out)
		self.assertIn("the breakdown that matters", out)
		# ...and the escape hatches, so it never becomes an interrogation
		self.assertIn("Skip any decision their request already", out)
		self.assertIn("Never ask twice", out)
		# the standing build contract is still in the block
		self.assertIn("Jarvis Dashboards builder page", out)
		self.assertTrue(out.endswith("\n\nsales dashboard please"))

	def test_a_message_that_is_not_a_build_request_gets_a_normal_answer(self):
		"""The arm cannot read the message, so it says so: "hi" or "what data do
		you have?" typed into the builder composer must not be interrogated."""
		conv = self._conversation()
		out = _prepend_doc_context("hi", {"page": "dashboards"}, conv)
		self.assertIn("If this message is not a request to build a dashboard", out)
		self.assertIn("just answer it normally - no jarvis-ask block and no build", out)

	def test_the_turn_carrying_the_answers_builds_instead_of_asking_again(self):
		"""Turn 2: the ask went out, the user answered, and no canvas exists yet
		(the build has not run). The old canvas-only gate re-ordered the interview
		here - the exact loop the owner complained about, one round later."""
		conv = self._conversation()
		self._message(conv, 1, "user", content="sales dashboard please")
		self._message(conv, 2, "assistant", content=ASK_BLOCK)
		self._message(conv, 3, "user", content="Here are my answers: ...")
		out = _prepend_doc_context("Here are my answers: ...", {"page": "dashboards"}, conv)
		self.assertIn(BUILD_NOW_MARK, out)
		self.assertNotIn(CLARIFY_MARK, out)
		self.assertNotIn("do NOT build", out)
		self.assertIn("build the dashboard now", out)

	def test_a_failed_publish_does_not_re_open_the_interview(self):
		"""``persist_canvases`` returns early without stamping ``canvas`` when the
		gateway fetch or the File save fails for every artifact. The model built,
		the artifact never landed - the next turn must still build, not re-ask."""
		conv = self._conversation()
		self._message(conv, 1, "assistant", content=ASK_BLOCK)
		self._message(conv, 2, "user", content="static, by month")
		self._message(conv, 3, "assistant", content="Here is the dashboard.")  # publish failed
		out = _prepend_doc_context("it is blank?", {"page": "dashboards"}, conv)
		self.assertIn(BUILD_NOW_MARK, out)
		self.assertNotIn("```jarvis-ask", out)

	def test_once_a_canvas_exists_the_wording_is_the_iterate_one(self):
		conv = self._conversation()
		self._message(
			conv,
			1,
			"assistant",
			canvas=[{"name": "documents/a/index.html", "type": "html"}],
		)
		out = _prepend_doc_context("make the bars horizontal", {"page": "dashboards"}, conv)
		self.assertNotIn(CLARIFY_MARK, out)
		self.assertNotIn(BUILD_NOW_MARK, out)
		self.assertNotIn("```jarvis-ask", out)
		self.assertIn("create or iterate on a dashboard/report", out)

	def test_revising_a_saved_dashboard_is_never_a_first_build(self):
		"""The document already exists; asking what to build would be absurd."""
		conv = self._conversation()
		out = _prepend_doc_context(
			"add a total row",
			{"page": "dashboards", "doctype": "Jarvis Dashboard", "name": "JD-0001"},
			conv,
		)
		self.assertNotIn(CLARIFY_MARK, out)
		self.assertIn("They are revising the saved dashboard JD-0001", out)

	def test_an_explicit_data_mode_is_not_re_asked(self):
		conv = self._conversation()
		asked = _prepend_doc_context("sales dashboard", {"page": "dashboards"}, conv)
		self.assertIn("STATIC one-time report", asked)
		settled = _prepend_doc_context("sales dashboard", {"page": "dashboards", "data_mode": "live"}, conv)
		self.assertIn(CLARIFY_MARK, settled)
		# the toggle already answered it, so it is not one of the questions
		questions = settled[settled.index(CLARIFY_MARK) :]
		self.assertNotIn("or a LIVE data-connected dashboard", questions)
		self.assertIn("the data scope", questions)

	def test_the_theme_is_never_one_of_the_questions(self):
		"""The picker always carries a theme (it defaults to Jarvis), so there is
		no unset state to ask about - the interview stays 2-3 decisions."""
		conv = self._conversation()
		for context in ({"page": "dashboards"}, {"page": "dashboards", "theme": "claude"}):
			out = _prepend_doc_context("sales dashboard", context, conv)
			self.assertIn(CLARIFY_MARK, out)
			self.assertNotIn("which theme it should use", out[out.index(CLARIFY_MARK) :])

	def test_no_conversation_id_still_clarifies_and_never_raises(self):
		"""Callers that pass no conversation (older worker payloads, tests) get
		the first-build arm rather than an exception."""
		out = _prepend_doc_context("sales dashboard", {"page": "dashboards"})
		self.assertIn(CLARIFY_MARK, out)

	def test_other_context_branches_are_untouched(self):
		self.assertEqual(_prepend_doc_context("hello", None), "hello")
		self.assertEqual(_prepend_doc_context("hello", {}), "hello")
		triggers = _prepend_doc_context("make a trigger", {"page": "triggers"})
		self.assertIn("Jarvis Triggers page", triggers)
		self.assertNotIn(CLARIFY_MARK, triggers)
