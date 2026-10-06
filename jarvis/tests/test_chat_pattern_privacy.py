"""A learned pattern mined from someone's chats stays hidden from reviewers until
its owner answers its question (#18): the board, the by-name endpoints, Desk /
REST reads, the nightly surfacing pass and the clean-up patch.

Nothing is committed: rows are inserted in the test transaction and roll back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import learned_api
from jarvis.learning import chat_pattern_privacy as privacy
from jarvis.learning import engine

JLP = privacy.JLP
QUESTION = privacy.QUESTION
REVIEWER = "chat-privacy-reviewer@example.com"
OWNER = "chat-privacy-owner@example.com"


def _first_option(doctype: str, fieldname: str) -> str:
	options = frappe.get_meta(doctype).get_field(fieldname).options or ""
	return next((o for o in options.split("\n") if o.strip()), "")


class TestChatPatternPrivacy(FrappeTestCase):
	def setUp(self):
		for email, roles in ((REVIEWER, ["Jarvis Skill Reviewer"]), (OWNER, [])):
			if not frappe.db.exists("User", email):
				user = frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0]})
				user.insert(ignore_permissions=True)
				if roles:
					user.add_roles(*roles)
		self.chat_row = self._pattern("chat-privacy-chat", privacy.CHAT_DETECTOR, surfaced=1)
		self.other_row = self._pattern("chat-privacy-other", "some-other-detector", surfaced=1)
		self.question = frappe.get_doc(
			{
				"doctype": QUESTION,
				"user": OWNER,
				"question": "Do you group invoices by region?",
				"origin": _first_option(QUESTION, "origin"),
				"source_pattern": self.chat_row,
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _pattern(self, key: str, detector: str, surfaced: int) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": JLP,
					"detector_id": detector,
					"pattern_key": f"{key}-{frappe.generate_hash(length=6)}",
					"domain": _first_option(JLP, "domain"),
					"pattern_statement": f"{key} statement",
					"skill_draft": f"- {key} draft",
					"status": "Proposed",
					"surfaced": surfaced,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _answer(self):
		frappe.db.set_value(QUESTION, self.question.name, "status", "Answered")

	def _board_names(self) -> set:
		frappe.set_user(REVIEWER)
		try:
			rows = learned_api.list_learned_patterns_page(
				surfaced="all", search="chat-privacy", page_length=50
			)["rows"]
		finally:
			frappe.set_user("Administrator")
		return {r["name"] for r in rows}

	def test_the_board_hides_an_unanswered_chat_pattern_until_its_owner_answers(self):
		names = self._board_names()
		self.assertNotIn(self.chat_row, names)
		self.assertIn(self.other_row, names)
		self._answer()
		self.assertIn(self.chat_row, self._board_names())

	def test_by_name_endpoints_treat_it_as_missing(self):
		frappe.set_user(REVIEWER)
		with self.assertRaises(frappe.DoesNotExistError):
			learned_api.get_learned_pattern(self.chat_row)
		frappe.set_user("Administrator")
		self._answer()
		frappe.set_user(REVIEWER)
		self.assertEqual(learned_api.get_learned_pattern(self.chat_row)["name"], self.chat_row)

	def test_desk_and_rest_reads_hide_it(self):
		frappe.set_user(REVIEWER)
		self.assertNotIn(self.chat_row, frappe.get_list(JLP, pluck="name", limit_page_length=0))
		self.assertIn(self.other_row, frappe.get_list(JLP, pluck="name", limit_page_length=0))
		self.assertFalse(frappe.has_permission(JLP, "read", doc=self.chat_row))
		frappe.set_user("Administrator")
		self._answer()
		frappe.set_user(REVIEWER)
		self.assertTrue(frappe.has_permission(JLP, "read", doc=self.chat_row))

	def test_answering_surfaces_it(self):
		frappe.db.set_value(JLP, self.chat_row, "surfaced", 0)
		self._answer()
		privacy.release_for_question(self.question.name)
		self.assertEqual(frappe.db.get_value(JLP, self.chat_row, "surfaced"), 1)

	def test_the_nightly_pass_never_surfaces_a_chat_pattern(self):
		frappe.db.set_value(JLP, self.chat_row, "surfaced", 0)
		frappe.db.set_value(JLP, self.other_row, "surfaced", 0)
		with (
			patch.object(engine, "_settings_value", return_value=10_000),
			patch.object(engine, "_fenced_write"),
			patch.object(frappe.db, "commit"),
		):
			engine._promote_surfaced("run")
		self.assertEqual(frappe.db.get_value(JLP, self.chat_row, "surfaced"), 0)
		self.assertEqual(frappe.db.get_value(JLP, self.other_row, "surfaced"), 1)

	def test_the_patch_takes_back_surfaced_unanswered_chat_patterns_only(self):
		from jarvis.patches.v2_26_hide_unanswered_chat_patterns import execute

		answered = self._pattern("chat-privacy-answered", privacy.CHAT_DETECTOR, surfaced=1)
		frappe.get_doc(
			{
				"doctype": QUESTION,
				"user": OWNER,
				"question": "Answered one?",
				"origin": _first_option(QUESTION, "origin"),
				"source_pattern": answered,
				"status": "Answered",
			}
		).insert(ignore_permissions=True)
		execute()
		self.assertEqual(frappe.db.get_value(JLP, self.chat_row, "surfaced"), 0)
		self.assertEqual(frappe.db.get_value(JLP, answered, "surfaced"), 1)
		self.assertEqual(frappe.db.get_value(JLP, self.other_row, "surfaced"), 1)
