"""A File Box file's open questions, shown in that file's own chat too (the Approval
Board shows the same items): ``filebox.open_waits``."""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import filebox
from jarvis.chat.api import get_conversation
from jarvis.tests._pending_action_helpers import OTHER, OWNER, PendingActionTestMixin, as_user

CONV = "Jarvis Conversation"
APPROVAL = "Jarvis Approval Request"


class TestOpenWaits(PendingActionTestMixin, FrappeTestCase):
	def _filebox_conv(self, owner: str = OWNER, file_box: int = 1) -> str:
		conv = self.make_conv(owner)
		frappe.db.set_value(CONV, conv, "file_box", file_box, update_modified=False)
		frappe.db.commit()
		self.addCleanup(frappe.db.delete, APPROVAL, {"conversation": conv})
		return conv

	def _ar(self, conv: str, **extra) -> str:
		doc = frappe.get_doc(
			{
				"doctype": APPROVAL,
				"title": "Which supplier?",
				"question": "Which supplier sent this bill?",
				"status": "Pending",
				"conversation": conv,
				"source": "File Box",
				"options": json.dumps(["Acme", "Globex"]),
				**extra,
			}
		)
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	def _waits(self, conv: str, user: str = OWNER) -> list:
		with as_user(user):
			return filebox.open_waits(conv)["items"]

	def test_a_question_and_a_held_record_are_listed_with_their_board_links(self):
		conv = self._filebox_conv()
		ar = self._ar(conv)
		held = self.park(conv, kind="file_box_held", summary="Create Supplier Acme", waiters=[conv])
		items = {i["src"]: i for i in self._waits(conv)}
		self.assertEqual(
			(items["ar"]["name"], items["ar"]["question"], items["ar"]["options"], items["ar"]["link"]),
			(ar, "Which supplier sent this bill?", ["Acme", "Globex"], f"/approvals/{ar}"),
		)
		self.assertEqual(
			(items["pa"]["name"], items["pa"]["title"], items["pa"]["link"]),
			(held, "Create Supplier Acme", f"/approvals?held={held}"),
		)

	def test_a_wiki_note_and_a_decided_question_are_not_listed(self):
		conv = self._filebox_conv()
		self._ar(conv, source="File Box Wiki")
		self._ar(conv, status="Approved", decision="Acme")
		self.assertEqual(self._waits(conv), [])

	def test_only_the_owner_sees_them_and_an_ordinary_chat_has_none(self):
		conv = self._filebox_conv()
		self._ar(conv)
		with self.assertRaises(frappe.PermissionError):
			self._waits(conv, user=OTHER)
		chat = self._filebox_conv(file_box=0)
		self._ar(chat)
		self.assertEqual(self._waits(chat), [])

	def test_the_chat_payload_says_it_is_a_file_box_chat(self):
		conv = self._filebox_conv()
		chat = self._filebox_conv(file_box=0)
		with as_user(OWNER):
			self.assertEqual(get_conversation(conv)["conversation"]["file_box"], 1)
			self.assertEqual(get_conversation(chat)["conversation"]["file_box"], 0)
