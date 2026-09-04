"""Tests for jarvis.chat.feedback.submit_feedback.

Runs as a dedicated fixture user so cleanups stay scoped to disposable rows.
The admin forward is mocked (never hits a real admin); these assert the
server-side derivation, the ownership gate, and the best-effort swallow.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.api import create_conversation
from jarvis.chat.feedback import submit_feedback

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TEST_USER = "jarvis-feedback-test@example.com"

_UUID = "12345678-1234-1234-1234-123456789abc"
_SESSION_KEY = f"agent:main:dashboard:{_UUID}"


def _ensure_test_user(user: str = TEST_USER) -> None:
	if frappe.db.exists("User", user):
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": user,
			"first_name": "Feedback",
			"last_name": "Test",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	).insert(ignore_permissions=True)
	doc.add_roles("System Manager")
	frappe.db.commit()


def _delete_conv(name: str) -> None:
	for child in frappe.get_all(MSG, filters={"conversation": name}, pluck="name"):
		frappe.delete_doc(MSG, child, ignore_permissions=True, force=True)
	if frappe.db.exists(CONV, name):
		frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
	frappe.db.commit()


def _cleanup_user_conversations(user: str = TEST_USER) -> None:
	for name in frappe.get_all(CONV, filters={"owner": user}, pluck="name"):
		_delete_conv(name)


def _add_msg(conversation, role="assistant", model="gpt-5.5", duration=84000, seq=2):
	m = frappe.get_doc(
		{"doctype": MSG, "conversation": conversation, "seq": seq, "role": role, "content": "answer"}
	).insert(ignore_permissions=True)
	if role == "assistant":
		frappe.db.set_value(MSG, m.name, {"model": model, "reply_duration_ms": duration})
	frappe.db.commit()
	return m.name


class _FeedbackTestCase(FrappeTestCase):
	def setUp(self):
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv = create_conversation()
		frappe.db.set_value(CONV, self.conv, "session_key", _SESSION_KEY)
		self.msg = _add_msg(self.conv)

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _item(self, push):
		push.assert_called_once()
		return push.call_args.args[0]


class TestSubmitFeedback(_FeedbackTestCase):
	def test_up_forwards_derived_payload(self):
		with patch("jarvis.admin_client.push_chat_feedback") as push:
			res = submit_feedback(message_id=self.msg, rating="up")
		self.assertEqual(res, {"ok": True})
		item = self._item(push)
		self.assertEqual(item["rating"], "up")
		self.assertEqual(item["message_ref"], self.msg)
		self.assertEqual(item["conversation_ref"], self.conv)
		self.assertEqual(item["session_id"], _UUID)  # extracted from composite session_key
		self.assertEqual(item["model"], "gpt-5.5")
		self.assertEqual(item["reply_duration_ms"], 84000)
		self.assertEqual(item["user_ref"], TEST_USER)
		self.assertEqual(item["note"], "")

	def test_down_keeps_stripped_note(self):
		with patch("jarvis.admin_client.push_chat_feedback") as push:
			submit_feedback(message_id=self.msg, rating="down", note="  too slow  ")
		item = self._item(push)
		self.assertEqual(item["rating"], "down")
		self.assertEqual(item["note"], "too slow")

	def test_up_never_carries_a_note(self):
		with patch("jarvis.admin_client.push_chat_feedback") as push:
			submit_feedback(message_id=self.msg, rating="up", note="should be dropped")
		self.assertEqual(self._item(push)["note"], "")

	def test_rejects_bad_rating(self):
		with patch("jarvis.admin_client.push_chat_feedback") as push:
			with self.assertRaises(frappe.ValidationError):
				submit_feedback(message_id=self.msg, rating="meh")
		push.assert_not_called()

	def test_rejects_non_assistant_message(self):
		user_msg = _add_msg(self.conv, role="user", seq=1)
		with patch("jarvis.admin_client.push_chat_feedback") as push:
			with self.assertRaises(frappe.ValidationError):
				submit_feedback(message_id=user_msg, rating="up")
		push.assert_not_called()

	def test_rejects_missing_message(self):
		with self.assertRaises(frappe.DoesNotExistError):
			submit_feedback(message_id="does-not-exist", rating="up")

	def test_rejects_another_users_message(self):
		frappe.set_user("Administrator")
		other = create_conversation()
		other_msg = _add_msg(other)
		self.addCleanup(_delete_conv, other)
		frappe.set_user(TEST_USER)
		with patch("jarvis.admin_client.push_chat_feedback") as push:
			with self.assertRaises(frappe.PermissionError):
				submit_feedback(message_id=other_msg, rating="up")
		push.assert_not_called()

	def test_forward_failure_is_swallowed(self):
		with patch("jarvis.admin_client.push_chat_feedback", side_effect=RuntimeError("admin down")):
			res = submit_feedback(message_id=self.msg, rating="up")
		self.assertEqual(res, {"ok": True})  # best-effort: the tap never surfaces the error

	def test_missing_session_key_still_records(self):
		frappe.db.set_value(CONV, self.conv, "session_key", None)
		with patch("jarvis.admin_client.push_chat_feedback") as push:
			submit_feedback(message_id=self.msg, rating="up")
		self.assertEqual(self._item(push)["session_id"], "")  # link best-effort, rating still forwarded


if __name__ == "__main__":
	unittest.main()
