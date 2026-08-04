"""Tests for per-conversation thinking effort (Phase 2).

Covers the directive-leading invariant that holds even when
_prepend_doc_context prefixes a [Viewing: ...] line (spec section 7.3,
verification item 10.3).
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_session_pool
from jarvis.chat.api import create_conversation, send_message
from jarvis.chat.worker import run_agent_turn
from jarvis.tests.test_chat_api import (
	TEST_USER,
	_cleanup_user_conversations,
	_ensure_test_user,
)


class TestThinkingDirectiveLeading(FrappeTestCase):
	"""The /think directive must be the FIRST bytes sent to agent
	even when _prepend_doc_context prepends a [Viewing: ...] line for
	floating-widget auto-context (spec 7.3, verification 10.3).

	These tests call run_agent_turn with a mocked agent session and
	capture the message argument actually passed to stream_agent_turn.

	test_directive_leads_with_doc_context is the RED-before / GREEN-after
	test: before the relocation fix in handle_chat_send it failed because the
	/think prefix was applied before _prepend_doc_context (so [Viewing:]
	displaced the directive); after the fix it passes.
	"""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		conv = create_conversation()
		with patch("frappe.enqueue"):
			result = send_message(
				conv,
				"what is the status?",
				thinking_override="high",
			)
		# send_message no longer creates the agent session on the web
		# request (2026-07 latency plan, Phase 1.1 — the worker creates it on
		# its pooled connection). These tests assert message COMPOSITION on an
		# existing session, so seed the key directly, same as
		# test_chat_worker._make_conversation_with_user_message.
		frappe.db.set_value("Jarvis Conversation", conv, "session_key", "agent:fake")
		frappe.db.commit()
		self.conv = conv
		self.user_msg = result["message_id"]

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _capture_message_sent(self, context=None):
		"""Run one agent turn and return (message, thinking) passed to chat_send.

		The turn goes out via ``chat_send`` (relay flow); the thinking level rides
		the structured ``thinking`` kwarg, not the message body. Uses
		call_args_list[0] (the FIRST call) to capture the user turn; a second
		call, if any, is the auto-title prompt and must be ignored.
		"""
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = iter(
			[
				{"kind": "lifecycle", "phase": "start"},
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		with patch(
			"jarvis.chat.agent_session_pool.AgentSession.connect",
			return_value=fake_sess,
		):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(
					self.conv,
					self.user_msg,
					run_id="r1",
					context=context,
				)
		first_call = fake_sess.chat_send.call_args_list[0]
		pos = first_call.args
		kw = first_call.kwargs
		msg = pos[1] if len(pos) >= 2 else kw.get("message")
		return msg, kw.get("thinking")

	def test_directive_leads_with_doc_context(self):
		"""Thinking rides chat_send's structured kwarg even with [Viewing: ...]
		doc-context prepended — never displaced, never inlined in the body."""
		msg, thinking = self._capture_message_sent(
			context={"doctype": "Sales Order", "name": "SO-001"},
		)
		self.assertIsNotNone(msg)
		self.assertEqual(thinking, "high")
		self.assertNotIn("/think", msg)
		self.assertIn("[Viewing: Sales Order SO-001", msg)
		self.assertIn("[Context:", msg)
		self.assertIn("what is the status?", msg)

	def test_context_text_preserved(self):
		"""[Viewing: ...] and [Context: ...] text is not mutated."""
		msg, _thinking = self._capture_message_sent(
			context={"doctype": "Purchase Invoice", "name": "PINV-0001"},
		)
		self.assertIn(
			"[Viewing: Purchase Invoice PINV-0001; resolve 'this'/'here' against it]",
			msg,
		)
		self.assertIn("[Context:", msg)

	def test_directive_leads_without_doc_context(self):
		"""Baseline: thinking kwarg set without doc-context (no regression)."""
		msg, thinking = self._capture_message_sent(context=None)
		self.assertEqual(thinking, "high")
		self.assertNotIn("/think", msg)
