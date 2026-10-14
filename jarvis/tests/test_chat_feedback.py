"""Tests for jarvis.chat.feedback.submit_feedback.

Runs as a dedicated fixture user so cleanups stay scoped to disposable rows.
The request only stores the rating in the feedback outbox; delivery to admin is
a queued job (patched out here, so no real job reaches the site's queue) and is
covered by test_feedback_outbox. These assert the server-side derivation, the
ownership gate, and that the request never waits on admin.
"""

import json
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.api import create_conversation
from jarvis.chat.feedback import submit_feedback

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
OUTBOX = "Jarvis Feedback Outbox"
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
	_cleanup_outbox(user)


def _cleanup_outbox(user: str = TEST_USER) -> None:
	frappe.db.delete(OUTBOX, {"user": user})
	frappe.db.commit()


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
		# The fixtures commit, which would run the after-commit enqueue for real:
		# a delivery job on this site's queue, pushing test data to whatever
		# admin the site points at.
		enqueue = patch("jarvis.chat.feedback_outbox._enqueue_delivery")
		self.enqueue = enqueue.start()
		self.addCleanup(enqueue.stop)
		_cleanup_user_conversations()
		self.conv = create_conversation()
		frappe.db.set_value(CONV, self.conv, "session_key", _SESSION_KEY)
		self.msg = _add_msg(self.conv)

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _rows(self, ref=None):
		return frappe.get_all(
			OUTBOX,
			filters={"kind": "Reply", "ref": ref or self.msg},
			fields=["name", "status", "payload", "revision", "attempts"],
		)

	def _item(self, ref=None):
		rows = self._rows(ref)
		self.assertEqual(len(rows), 1, "exactly one stored row per rated reply")
		return json.loads(rows[0].payload)


class TestSubmitFeedback(_FeedbackTestCase):
	def test_up_stores_derived_payload(self):
		res = submit_feedback(message_id=self.msg, rating="up")
		self.assertEqual(res, {"ok": True, "delivered": False})
		item = self._item()
		self.assertEqual(item["rating"], "up")
		self.assertEqual(item["message_ref"], self.msg)
		self.assertEqual(item["conversation_ref"], self.conv)
		self.assertEqual(item["session_id"], _UUID)  # extracted from composite session_key
		self.assertEqual(item["model"], "gpt-5.5")
		self.assertEqual(item["reply_duration_ms"], 84000)
		self.assertEqual(item["user_ref"], TEST_USER)
		self.assertEqual(item["note"], "")
		self.enqueue.assert_called_once_with(self._rows()[0].name, 1)

	def test_down_keeps_stripped_note(self):
		submit_feedback(message_id=self.msg, rating="down", note="  too slow  ")
		item = self._item()
		self.assertEqual(item["rating"], "down")
		self.assertEqual(item["note"], "too slow")

	def test_up_never_carries_a_note(self):
		submit_feedback(message_id=self.msg, rating="up", note="should be dropped")
		self.assertEqual(self._item()["note"], "")

	def test_rejects_bad_rating(self):
		with self.assertRaises(frappe.ValidationError):
			submit_feedback(message_id=self.msg, rating="meh")
		self.assertEqual(self._rows(), [])

	def test_rejects_non_assistant_message(self):
		user_msg = _add_msg(self.conv, role="user", seq=1)
		with self.assertRaises(frappe.ValidationError):
			submit_feedback(message_id=user_msg, rating="up")
		self.assertEqual(self._rows(user_msg), [])

	def test_rejects_missing_message(self):
		with self.assertRaises(frappe.DoesNotExistError):
			submit_feedback(message_id="does-not-exist", rating="up")

	def test_rejects_another_users_message(self):
		frappe.set_user("Administrator")
		other = create_conversation()
		other_msg = _add_msg(other)
		self.addCleanup(_delete_conv, other)
		frappe.set_user(TEST_USER)
		with self.assertRaises(frappe.PermissionError):
			submit_feedback(message_id=other_msg, rating="up")
		self.assertEqual(self._rows(other_msg), [])

	def test_the_request_never_calls_admin(self):
		# The tap must not wait on admin, or fail with it.
		with patch("jarvis.admin_client.push_chat_feedback", side_effect=RuntimeError("admin down")) as push:
			res = submit_feedback(message_id=self.msg, rating="up")
		self.assertEqual(res, {"ok": True, "delivered": False})
		push.assert_not_called()

	def test_missing_session_key_still_records(self):
		frappe.db.set_value(CONV, self.conv, "session_key", None)
		submit_feedback(message_id=self.msg, rating="up")
		self.assertEqual(self._item()["session_id"], "")  # link best-effort, rating still stored

	def test_is_post_only(self):
		"""It writes, and the delivery is queued for after the commit: reached over
		GET the rating would be rolled back and never sent."""
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[submit_feedback], ["POST"])


if __name__ == "__main__":
	unittest.main()
