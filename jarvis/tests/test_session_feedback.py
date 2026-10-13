"""Tests for the once-per-session feedback popup's backend: the maintained
``Jarvis Conversation.turn_count`` counter and the two whitelisted endpoints.

Runs as a dedicated fixture user so cleanups stay scoped to disposable rows,
and mirrors ``test_chat_feedback.py``'s fixture shape rather than a
rollback-based one: ``settlement._bump_turn_count`` commits its own tiny
transaction by design, so an ``addCleanup(frappe.db.rollback)`` would be
hollow. A reaction is stored in the feedback outbox; its delivery job is patched
out (the fixtures commit, which would otherwise queue a real job), and the admin
push is mocked wherever it appears - these never hit a real admin.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.api import create_conversation
from jarvis.chat.feedback import (
	SESSION_FEEDBACK_TURN_THRESHOLD,
	session_feedback_status,
	submit_session_feedback,
)
from jarvis.chat.feedback_outbox import outbox_name
from jarvis.chat.settlement import _bump_turn_count

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
OUTBOX = "Jarvis Feedback Outbox"
TEST_USER = "jarvis-session-feedback-test@example.com"

_PUSH = "jarvis.admin_client.push_session_feedback"


def _ensure_test_user(user: str = TEST_USER) -> None:
	if frappe.db.exists("User", user):
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": user,
			"first_name": "Session",
			"last_name": "Feedback",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	).insert(ignore_permissions=True)
	doc.add_roles("System Manager")
	frappe.db.commit()


def _delete_conv(name: str) -> None:
	for turn in frappe.get_all(TURN, filters={"conversation": name}, pluck="name"):
		frappe.delete_doc(TURN, turn, ignore_permissions=True, force=True)
	for child in frappe.get_all(MSG, filters={"conversation": name}, pluck="name"):
		frappe.delete_doc(MSG, child, ignore_permissions=True, force=True)
	if frappe.db.exists(CONV, name):
		frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
	frappe.db.commit()


def _cleanup_user_conversations(user: str = TEST_USER) -> None:
	for name in frappe.get_all(CONV, filters={"owner": user}, pluck="name"):
		_delete_conv(name)
	frappe.db.delete(OUTBOX, {"user": user})
	frappe.db.commit()


class _SessionFeedbackTestCase(FrappeTestCase):
	def setUp(self):
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		enqueue = patch("jarvis.chat.feedback_outbox._enqueue_delivery")
		enqueue.start()
		self.addCleanup(enqueue.stop)
		_cleanup_user_conversations()
		self.conv = create_conversation()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _turn_count(self, conversation=None):
		return frappe.db.get_value(CONV, conversation or self.conv, "turn_count")

	def _set_turns(self, n, conversation=None):
		frappe.db.set_value(CONV, conversation or self.conv, "turn_count", n, update_modified=False)

	def _mk_turn(self, run_id, *, hidden=0, conversation=None):
		"""A settled turn's row plus the seed user message the bump inspects."""
		conv = conversation or self.conv
		seed = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				# Unique per conversation, the way api._next_seq allocates it.
				"seq": frappe.db.count(MSG, {"conversation": conv}) + 1,
				"role": "user",
				"content": "hi",
				"streaming": 0,
				"hidden": hidden,
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": run_id,
				"conversation": conv,
				"relay_target_id": "default",
				"turn_class": "interactive",
				"state": "finalizing",
				"seed_message": seed.name,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return run_id


class TestTurnCounter(_SessionFeedbackTestCase):
	def test_turn_count_increments_once_per_bump(self):
		for i in range(3):
			_bump_turn_count(self.conv, self._mk_turn(f"sfrun{i}"))
		self.assertEqual(self._turn_count(), 3)

	def test_turn_count_never_counts_messages(self):
		"""The counter is MAINTAINED, not derived: three bumps against ONE turn
		reach 3 while the conversation still holds a single message. A live
		COUNT(*) over the message table could only ever report 1 here, so this
		fails the moment the implementation starts scanning."""
		run = self._mk_turn("sfrunA")
		for _ in range(3):
			_bump_turn_count(self.conv, run)
		self.assertEqual(frappe.db.count(MSG, {"conversation": self.conv}), 1)
		self.assertEqual(self._turn_count(), 3)

	def test_file_box_conversation_is_not_counted(self):
		# Server-set path: the controller gates a generic save that ENABLES
		# file_box, exactly as the real File Box drop path bypasses it.
		frappe.db.set_value(CONV, self.conv, "file_box", 1, update_modified=False)
		_bump_turn_count(self.conv, self._mk_turn("sfrunB"))
		self.assertEqual(self._turn_count(), 0)

	def test_agent_initiated_conversation_is_not_counted(self):
		# A macro / app-learning / scheduled-audit / proactive run log: the user
		# never chose to start it, so its turns are not engagement.
		frappe.db.set_value(CONV, self.conv, "agent_initiated", 1, update_modified=False)
		_bump_turn_count(self.conv, self._mk_turn("sfrunC"))
		self.assertEqual(self._turn_count(), 0)

	def test_hidden_continuation_turn_is_not_counted(self):
		"""Every human Apply/Confirm click dispatches a HIDDEN continuation turn
		through this same path. Counting it would make one user action worth two
		turns and fire the popup at half the intended depth."""
		_bump_turn_count(self.conv, self._mk_turn("sfrunD", hidden=1))
		self.assertEqual(self._turn_count(), 0)

	def test_visible_and_hidden_turns_mixed(self):
		_bump_turn_count(self.conv, self._mk_turn("sfrunE"))
		_bump_turn_count(self.conv, self._mk_turn("sfrunF", hidden=1))
		_bump_turn_count(self.conv, self._mk_turn("sfrunG"))
		self.assertEqual(self._turn_count(), 2, "only the two user-visible turns count")

	def test_bump_is_a_no_op_on_a_missing_conversation(self):
		# A settled turn whose conversation was deleted must not cost the turn its
		# terminal publish. The UPDATE simply matches 0 rows (it does not raise);
		# the raising branch is covered by the stubbed-frappe harness.
		_bump_turn_count("does-not-exist", "sfrun-missing")

	def test_bump_never_raises_even_when_the_error_log_itself_fails(self):
		"""A DB outage mid-bump: the UPDATE raises, the rollback raises, and the
		Error Log INSERT (over the same dead connection) raises too. "NEVER
		raises" has to hold through all three, or the turn loses its run:end
		publish and finalize enqueue (settlement calls the bump right before
		them). rollback/commit are stubbed so the real ones cannot discard this
		test's own transaction state."""
		with (
			patch.object(frappe.db, "sql", side_effect=RuntimeError("db gone")),
			patch.object(frappe.db, "rollback", side_effect=RuntimeError("still gone")),
			patch.object(frappe.db, "commit"),
			patch("frappe.log_error", side_effect=RuntimeError("cannot insert Error Log")) as log,
		):
			_bump_turn_count(self.conv, "sfrun-outage")  # must not raise
		log.assert_called_once()
		self.assertEqual(self._turn_count(), 0, "nothing was counted during the outage")


class TestSessionFeedbackStatus(_SessionFeedbackTestCase):
	def test_not_due_below_threshold(self):
		self._set_turns(SESSION_FEEDBACK_TURN_THRESHOLD - 1)
		self.assertFalse(session_feedback_status(self.conv)["due"])

	def test_due_at_threshold(self):
		self._set_turns(SESSION_FEEDBACK_TURN_THRESHOLD)
		self.assertTrue(session_feedback_status(self.conv)["due"])

	def test_still_due_above_threshold(self):
		self._set_turns(SESSION_FEEDBACK_TURN_THRESHOLD + 5)
		self.assertTrue(session_feedback_status(self.conv)["due"])

	def test_file_box_conversation_never_due(self):
		frappe.db.set_value(CONV, self.conv, "file_box", 1, update_modified=False)
		self._set_turns(999)
		self.assertFalse(session_feedback_status(self.conv)["due"])

	def test_agent_initiated_conversation_never_due(self):
		# Defence in depth: the counter never advances for these anyway, but an
		# older row (created before this field existed) must not fire the popup
		# on what is really an automated run log.
		frappe.db.set_value(CONV, self.conv, "agent_initiated", 1, update_modified=False)
		self._set_turns(999)
		self.assertFalse(session_feedback_status(self.conv)["due"])

	def test_rejects_another_users_conversation(self):
		frappe.set_user("Administrator")
		other = create_conversation()
		frappe.set_user(TEST_USER)
		try:
			with self.assertRaises(frappe.PermissionError):
				session_feedback_status(other)
		finally:
			frappe.set_user("Administrator")
			_delete_conv(other)
			frappe.set_user(TEST_USER)


class TestSubmitSessionFeedback(_SessionFeedbackTestCase):
	def setUp(self):
		super().setUp()
		self._set_turns(SESSION_FEEDBACK_TURN_THRESHOLD)

	def _stored(self, conversation=None):
		name = outbox_name("Session", conversation or self.conv, TEST_USER)
		return frappe.db.get_value(OUTBOX, name, "payload")

	def _item(self, push):
		"""The stored answer. The request itself never calls admin."""
		push.assert_not_called()
		payload = self._stored()
		self.assertTrue(payload, "the reaction was not stored")
		return json.loads(payload)

	def _reopen(self):
		"""Clear the claim so the next submit is a first submit again - the popup
		is genuinely one-shot per conversation, so a loop over chips has to."""
		frappe.db.set_value(CONV, self.conv, "session_feedback_asked_at", None, update_modified=False)

	def test_chip_stores_derived_payload(self):
		with patch(_PUSH) as push:
			res = submit_session_feedback(self.conv, "Good")
		self.assertEqual(res, {"ok": True, "recorded": True, "delivered": False})
		item = self._item(push)
		self.assertEqual(item["kind"], "Session")
		self.assertEqual(item["session_ref"], self.conv)
		self.assertEqual(item["chip_value"], "Good")
		self.assertEqual(item["user_ref"], TEST_USER)
		self.assertEqual(item["note"], "")

	def test_answering_stamps_asked_and_the_popup_never_fires_again(self):
		with patch(_PUSH):
			submit_session_feedback(self.conv, "Good")
		self.assertTrue(frappe.db.get_value(CONV, self.conv, "session_feedback_asked_at"))
		self.assertFalse(session_feedback_status(self.conv)["due"])

	def test_skip_stamps_asked_without_recording_or_forwarding(self):
		with patch(_PUSH) as push:
			res = submit_session_feedback(self.conv)
		self.assertEqual(res, {"ok": True, "recorded": False})
		push.assert_not_called()
		self.assertIsNone(self._stored(), "a Skip has no response to store")
		self.assertTrue(frappe.db.get_value(CONV, self.conv, "session_feedback_asked_at"))
		self.assertFalse(session_feedback_status(self.conv)["due"])

	def test_okay_or_worse_keeps_the_stripped_note(self):
		# "Okay" counts as low - the popup reveals the note field at "Okay or
		# worse", not only for the bottom two chips.
		for chip in ("Okay", "Not great", "Frustrating"):
			with self.subTest(chip=chip):
				self._reopen()
				with patch(_PUSH) as push:
					submit_session_feedback(self.conv, chip, note="  it lost the thread  ")
				self.assertEqual(self._item(push)["note"], "it lost the thread")

	def test_positive_chip_never_carries_a_note(self):
		for chip in ("Great", "Good"):
			with self.subTest(chip=chip):
				self._reopen()
				with patch(_PUSH) as push:
					submit_session_feedback(self.conv, chip, note="should be dropped")
				self.assertEqual(self._item(push)["note"], "")

	def test_note_is_bounded(self):
		with patch(_PUSH) as push:
			submit_session_feedback(self.conv, "Frustrating", note="x" * 5000)
		self.assertEqual(len(self._item(push)["note"]), 1000)

	def test_rejects_invalid_chip_value_without_burning_the_popup(self):
		with patch(_PUSH) as push:
			with self.assertRaises(frappe.ValidationError):
				submit_session_feedback(self.conv, "not-a-real-chip")
		push.assert_not_called()
		self.assertIsNone(self._stored())
		# A broken client must not consume the user's one chance to answer.
		self.assertFalse(frappe.db.get_value(CONV, self.conv, "session_feedback_asked_at"))
		self.assertTrue(session_feedback_status(self.conv)["due"])

	def test_a_second_submit_records_nothing(self):
		"""Two tabs, or a double-click that beats the dialog's disable: the claim
		is a compare-and-set, so only the first call stores an answer."""
		with patch(_PUSH) as push:
			first = submit_session_feedback(self.conv, "Great")
			second = submit_session_feedback(self.conv, "Frustrating", note="second tab")
		self.assertEqual(first, {"ok": True, "recorded": True, "delivered": False})
		self.assertEqual(second, {"ok": True, "recorded": False})
		self.assertEqual(self._item(push)["chip_value"], "Great", "the first answer is the one kept")

	def test_a_second_skip_records_nothing(self):
		with patch(_PUSH) as push:
			submit_session_feedback(self.conv)
			second = submit_session_feedback(self.conv)
		self.assertEqual(second, {"ok": True, "recorded": False})
		push.assert_not_called()
		self.assertIsNone(self._stored())

	def test_a_chip_after_a_skip_is_not_recorded(self):
		# Skip is final for the conversation - a later chip must not sneak in.
		with patch(_PUSH) as push:
			submit_session_feedback(self.conv)
			late = submit_session_feedback(self.conv, "Great")
		self.assertEqual(late, {"ok": True, "recorded": False})
		push.assert_not_called()
		self.assertIsNone(self._stored())

	def test_the_request_never_calls_admin(self):
		with patch(_PUSH, side_effect=RuntimeError("admin down")) as push:
			res = submit_session_feedback(self.conv, "Great")
		self.assertEqual(res, {"ok": True, "recorded": True, "delivered": False})
		self.assertEqual(self._item(push)["chip_value"], "Great")

	def test_rejects_another_users_conversation(self):
		frappe.set_user("Administrator")
		other = create_conversation()
		frappe.set_user(TEST_USER)
		try:
			with patch(_PUSH) as push:
				with self.assertRaises(frappe.PermissionError):
					submit_session_feedback(other, "Good")
			push.assert_not_called()
			self.assertIsNone(self._stored(other))
			self.assertFalse(frappe.db.get_value(CONV, other, "session_feedback_asked_at"))
		finally:
			frappe.set_user("Administrator")
			_delete_conv(other)
			frappe.set_user(TEST_USER)

	def test_is_post_only(self):
		"""It WRITES (a compare-and-set claim plus the stored answer, committed
		with the request) and queues delivery for after that commit, exactly like
		its pulse siblings (see
		test_pulse_feedback.py::test_both_endpoints_are_post_only) - reached over
		GET, Frappe would not enforce CSRF on it."""
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[submit_session_feedback], ["POST"])
