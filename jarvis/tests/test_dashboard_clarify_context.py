"""Interview-first on the Dashboards builder: the clarify arm of
``_prepend_doc_context``'s dashboards branch.

Owner-reported (2026-07-27): the builder started drawing off the very first
message instead of agreeing what to build. The frontend sends the same
``{"page": "dashboards"}`` context on every turn and has no notion of a turn
index, so "is this the first build?" is derived SERVER-SIDE here - from the same
``Jarvis Chat Message.canvas`` field ``persist_canvases`` stamps and
``get_conversation`` hands the UI. No new context key, no API surface change.

Run: bench --site patterntest.localhost run-tests --module
     jarvis.tests.test_dashboard_clarify_context
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.turn_handler import _dashboard_has_canvas, _prepend_doc_context

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
CLARIFY_MARK = "NOTHING has been drawn on this canvas yet"


class _DashboardContextTestCase(FrappeTestCase):
	def setUp(self):
		self._convs: list[str] = []

	def tearDown(self):
		for name in self._convs:
			frappe.delete_doc(CONV, name, force=True, ignore_permissions=True, delete_permanently=True)
		frappe.db.commit()

	def _conversation(self) -> str:
		doc = frappe.get_doc({"doctype": CONV, "title": "dash clarify test"}).insert(ignore_permissions=True)
		self._convs.append(doc.name)
		return doc.name

	def _message(self, conversation: str, seq: int, role: str, canvas=None) -> str:
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conversation,
				"seq": seq,
				"role": role,
				"content": "x",
				"canvas": frappe.as_json(canvas) if canvas else None,
			}
		).insert(ignore_permissions=True)
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


class TestDashboardsClarifyArm(_DashboardContextTestCase):
	def test_first_turn_asks_instead_of_building(self):
		conv = self._conversation()
		out = _prepend_doc_context("sales dashboard please", {"page": "dashboards"}, conv)
		self.assertIn(CLARIFY_MARK, out)
		self.assertIn("```jarvis-ask", out)
		self.assertIn("do NOT build on this pass", out)
		# the 2-4 decisions that shape the build
		self.assertIn("the data scope", out)
		self.assertIn("the breakdown that matters", out)
		# ...and the escape hatches, so it never becomes an interrogation
		self.assertIn("Skip any decision their request already", out)
		self.assertIn("Never ask twice", out)
		# the standing build contract is still in the block
		self.assertIn("Jarvis Dashboards builder page", out)
		self.assertTrue(out.endswith("\n\nsales dashboard please"))

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

	def test_an_explicit_theme_is_not_re_asked(self):
		conv = self._conversation()
		out = _prepend_doc_context("sales dashboard", {"page": "dashboards", "theme": "claude"}, conv)
		questions = out[out.index(CLARIFY_MARK) :]
		self.assertNotIn("which theme it should use", questions)
		no_theme = _prepend_doc_context("sales dashboard", {"page": "dashboards"}, conv)
		self.assertIn("which theme it should use", no_theme[no_theme.index(CLARIFY_MARK) :])

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
