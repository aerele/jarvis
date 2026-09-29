"""Tests for jarvis.chat.api.get_conversation's live-turn-steps reload contract (T2).

Root cause A (plan 2026-09-28-live-turn-steps.md): get_conversation returns the
RAW streaming mirror for a still-streaming assistant row (Jarvis Chat
Message.content keeps step text by design), so a mid-turn reload showed step
text glued into the reply. For a streaming=1 row this module's target finds
the row's run (one frappe.qb query, no get_value loop), reads the pump's
cached step text and returns content = remove_steps(content, steps),
live_steps, and the run identity (run_id/last_event_seq/pump_epoch) so a
fresh tab can seed its own realtime fencing. A settled row is untouched.

This module never calls frappe.db.commit(): every row it creates lives only in
the test's own transaction and rolls back with it - see test_chat_api.py's
_ensure_test_user/_cleanup_user_conversations for the COMMITTING version this
module deliberately does not copy (per the live-turn-steps plan's T2 brief).
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.api import get_conversation

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
TEST_USER = "jarvis-live-steps-test@example.com"


def _ensure_test_user(user: str = TEST_USER) -> None:
	"""Create the fixture user if missing, without committing - see module
	docstring. Idempotent: a prior (committing) test run may have already
	created and persisted this user, in which case this is a no-op read."""
	if frappe.db.exists("User", user):
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": user,
			"first_name": "Jarvis",
			"last_name": "LiveSteps",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.add_roles("System Manager")


class TestLiveTurnSteps(FrappeTestCase):
	def setUp(self):
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig_user)

	def _conversation(self) -> str:
		conv = frappe.get_doc({"doctype": CONV, "title": "Live steps test"})
		conv.insert(ignore_permissions=True)
		return conv.name

	def _message(self, conversation: str, seq: int, role: str, **kw) -> str:
		row = {"doctype": MSG, "conversation": conversation, "seq": seq, "role": role}
		row.update(kw)
		msg = frappe.get_doc(row)
		msg.insert(ignore_permissions=True)
		return msg.name

	def _turn(self, run_id: str, conversation: str, seed_message: str, assistant_message: str, **kw) -> None:
		row = {
			"doctype": TURN,
			"run_id": run_id,
			"conversation": conversation,
			"relay_target_id": "livesteps_target",
			"turn_class": "interactive",
			"state": "streaming",
			"seed_message": seed_message,
			"assistant_message": assistant_message,
			"enqueued_at": frappe.utils.now(),
		}
		row.update(kw)
		frappe.get_doc(row).insert(ignore_permissions=True)

	def test_streaming_row_gets_content_stripped_and_run_identity(self):
		conv = self._conversation()
		seed = self._message(conv, 1, "user", content="What invoices are overdue?")
		step = "I'll check the overdue invoices."
		answer = "Three invoices are currently overdue."
		assistant = self._message(conv, 2, "assistant", content=f"{step}\n\n{answer}", streaming=1)
		self._turn("run-livesteps-1", conv, seed, assistant, last_event_seq=5, pump_epoch=2)

		with patch("jarvis.chat.pump._read_run_steps", return_value=[step]) as mocked:
			result = get_conversation(conv)
		mocked.assert_called_once_with("run-livesteps-1")

		rows = {m["name"]: m for m in result["messages"]}
		live = rows[assistant]
		self.assertEqual(live["content"], answer)
		self.assertEqual(live["live_steps"], [step])
		self.assertEqual(live["run_id"], "run-livesteps-1")
		self.assertEqual(live["last_event_seq"], 5)
		self.assertEqual(live["pump_epoch"], 2)
		self.assertEqual(live["turn_state"], "streaming")
		# The seed (user) row is not streaming and carries no run identity.
		self.assertNotIn("run_id", rows[seed])

	def test_an_ended_run_whose_row_has_not_settled_reports_its_state(self):
		# Review E1: the reply row can still read streaming=1 a moment after the
		# run ended; the SPA reads turn_state to treat its text as the answer.
		conv = self._conversation()
		seed = self._message(conv, 1, "user", content="How many customers?")
		assistant = self._message(conv, 2, "assistant", content="There are 3 customers.", streaming=1)
		self._turn(
			"run-livesteps-3", conv, seed, assistant, state="finalizing", last_event_seq=4, pump_epoch=1
		)

		with patch("jarvis.chat.pump._read_run_steps", return_value=[]):
			result = get_conversation(conv)

		live = {m["name"]: m for m in result["messages"]}[assistant]
		self.assertEqual(live["turn_state"], "finalizing")
		self.assertEqual(live["content"], "There are 3 customers.")

	def test_a_reload_strips_a_grown_preamble_whole_through_the_real_cache(self):
		# Review [2]: the pump cache appends each growing resend; the reload must
		# collapse them or it cuts the prefix and leaves the tail in the reply.
		from jarvis.chat.pump import _run_steps_key

		partial = "I checked the ledger"  # a streaming prefix of full
		full = "I checked the ledger and found stale entries."
		answer = "The ledger has 4 stale entries dated before April, all from the old import."
		conv = self._conversation()
		seed = self._message(conv, 1, "user", content="Check the ledger")
		assistant = self._message(conv, 2, "assistant", content=f"{full}\n\n{answer}", streaming=1)
		self._turn("run-livesteps-4", conv, seed, assistant, last_event_seq=3, pump_epoch=1)
		key = _run_steps_key("run-livesteps-4")
		frappe.cache().set_value(key, [partial, full], expires_in_sec=60)
		try:
			result = get_conversation(conv)
		finally:
			frappe.cache().delete_value(key)

		live = {m["name"]: m for m in result["messages"]}[assistant]
		self.assertEqual(live["content"], answer)
		self.assertEqual(live["live_steps"], [full])

	def test_a_step_not_found_in_content_is_skipped_not_stripped(self):
		# remove_steps/strip_steps both skip a cached step that is not an exact
		# substring of the text they are given (the direct/harness-mode shape,
		# since pump.on_step now caches text even when raw is None - T2).
		conv = self._conversation()
		seed = self._message(conv, 1, "user", content="Hi")
		answer = "Found it."
		assistant = self._message(conv, 2, "assistant", content=answer, streaming=1)
		self._turn("run-livesteps-2", conv, seed, assistant, last_event_seq=1, pump_epoch=1)

		with patch("jarvis.chat.pump._read_run_steps", return_value=["Looking that up"]):
			result = get_conversation(conv)

		rows = {m["name"]: m for m in result["messages"]}
		live = rows[assistant]
		self.assertEqual(live["content"], answer)  # unchanged: the step was never in it
		self.assertEqual(live["live_steps"], ["Looking that up"])  # still shown live

	def test_settled_row_is_untouched(self):
		conv = self._conversation()
		self._message(conv, 1, "user", content="Hi")
		assistant = self._message(conv, 2, "assistant", content="Hello there.", streaming=0)

		with patch("jarvis.chat.pump._read_run_steps") as mocked:
			result = get_conversation(conv)
		mocked.assert_not_called()

		rows = {m["name"]: m for m in result["messages"]}
		settled = rows[assistant]
		self.assertEqual(settled["content"], "Hello there.")
		self.assertNotIn("live_steps", settled)
		self.assertNotIn("run_id", settled)
		self.assertNotIn("last_event_seq", settled)
		self.assertNotIn("pump_epoch", settled)
		self.assertNotIn("turn_state", settled)

	def test_no_streaming_rows_never_queries_turns(self):
		conv = self._conversation()
		self._message(conv, 1, "user", content="Hi")
		self._message(conv, 2, "assistant", content="Hello.", streaming=0)

		with patch("jarvis.chat.pump._read_run_steps") as mocked:
			get_conversation(conv)
		mocked.assert_not_called()
