"""A confirmed card's continuation message never answers the chat's open question."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import chat_asks

AR = "Jarvis Approval Request"
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"


class TestContinuationKeepsQuestionOpen(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.conv = (
			frappe.get_doc({"doctype": CONV, "title": "continuation-ask"})
			.insert(ignore_permissions=True)
			.name
		)
		self.ask = (
			frappe.get_doc(
				{
					"doctype": AR,
					"title": "continuation-ask",
					"source": "Chat",
					"conversation": self.conv,
					"question": "Offer the discount?",
					"options": '["Yes", "No"]',
					"status": "Pending",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		frappe.db.commit()
		self.addCleanup(self._cleanup)
		publish = patch("jarvis.chat.chat_asks.publish_to_user")
		publish.start()
		self.addCleanup(publish.stop)

	def _cleanup(self):
		frappe.db.delete(MSG, {"conversation": self.conv})
		frappe.db.delete(AR, {"name": self.ask})
		frappe.db.delete(CONV, {"name": self.conv})
		frappe.db.commit()

	def _message(self, origin, seq):
		# ``origin`` is server-stamped (a client cannot set it), so write the row directly.
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": self.conv,
				"role": "user",
				"seq": seq,
				"origin": origin,
				"content": "x",
			}
		)
		doc.db_insert()
		return doc.name

	def test_continuation_leaves_the_question_pending(self):
		chat_asks.resolve_on_user_message(self.conv, seed_message=self._message("continuation", 1))
		self.assertEqual(frappe.db.get_value(AR, self.ask, "status"), "Pending")

	def test_a_human_reply_still_answers_it(self):
		chat_asks.resolve_on_user_message(self.conv, seed_message=self._message("human", 2))
		self.assertEqual(frappe.db.get_value(AR, self.ask, "status"), "Answered")
