"""Concurrent attempts to send one decided question to the chat: two real
database connections, the real send_message transaction and the real claim
(only the worker dispatch and the admin readiness gate are stubbed).

Snapshot isolation is set per session (never globally): ON, OFF and the
server's default, recording which modes the server supports.
"""

from contextlib import contextmanager
from threading import Barrier, Event
from unittest.mock import patch

import frappe
from frappe import client
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import approval_reviewers, approvals_api, chat_asks
from jarvis.tests import test_chat_approval_continuation as continuation_fixture
from jarvis.tests._pending_action_helpers import as_user, ensure_user

AR = continuation_fixture.AR
MODES = {"on": 1, "off": 0, "default": None}


@contextmanager
def session_snapshot_isolation(mode):
	"""This connection only; restored afterwards."""
	if mode is None:
		yield
		return
	previous = frappe.db.sql("SELECT @@session.innodb_snapshot_isolation")[0][0]
	frappe.db.sql(f"SET SESSION innodb_snapshot_isolation={int(mode)}")
	try:
		yield
	finally:
		frappe.db.sql(f"SET SESSION innodb_snapshot_isolation={int(previous)}")


class TestChatApprovalContinuationRaces(continuation_fixture.ComposedChatReviewFixture, FrappeTestCase):
	def both_at(self, parties=2):
		"""Every attempt reads the row and builds its message before any sends."""
		barrier = Barrier(parties, timeout=20)
		real = approvals_api._decision_message

		def message(doc):
			barrier.wait()
			return real(doc)

		return patch.object(approvals_api, "_decision_message", side_effect=message)

	def retry_on_other_connection(self, mode):
		def call():
			with session_snapshot_isolation(mode):
				try:
					return approvals_api.continue_approval_chat(self.chat)
				except Exception as exc:  # reported, never hidden
					frappe.db.rollback()
					return {"raised": type(exc).__name__}

		return self.fx.other_request(self.owner, call)

	def test_two_simultaneous_requester_sends_post_one_message(self):
		"""N7, snapshot isolation ON / OFF / default."""
		supported = frappe.db.sql("SHOW VARIABLES LIKE 'innodb_snapshot_isolation'")
		self.assertTrue(supported, "the server must expose innodb_snapshot_isolation")
		for label, mode in MODES.items():
			with self.subTest(mode=label):
				self.chat = self.fx.question("Chat")
				conv = self.failed_by_reviewer()
				with (
					self.both_at(),
					continuation_fixture.real_send_without_worker() as (dispatch, _send),
					patch("frappe.publish_realtime"),
				):
					other = self.retry_on_other_connection(mode)
					with as_user(self.owner), session_snapshot_isolation(mode):
						try:
							mine = approvals_api.continue_approval_chat(self.chat)
							frappe.db.commit()
						except Exception as exc:
							frappe.db.rollback()
							mine = {"raised": type(exc).__name__}
					theirs = other.result(timeout=40)
				outcomes = sorted([mine, theirs], key=lambda r: r.get("already", 0))
				self.assertEqual(
					{
						"outcomes": [(r.get("ok"), r.get("continuation")) for r in outcomes],
						"messages": [r.origin for r in self.user_messages(conv)],
						"dispatch": dispatch.call_count,
						"state": frappe.get_doc(AR, self.chat).get("resume_state"),
					},
					{
						"outcomes": [(True, "done"), (True, "done")],
						"messages": ["board_answer"],
						"dispatch": 1,
						"state": "done",
					},
				)

	def test_reviewers_own_send_racing_a_requester_send_posts_one_message(self):
		"""N8: the reviewer's decide continuation and a requester send on a fresh 'due'."""
		self.tag()
		conv = self.conversation_of(self.chat)
		decided = Event()
		barrier = Barrier(2, timeout=20)
		real = approvals_api._decision_message

		def message(doc):
			if frappe.session.user == self.reviewer:
				decided.set()  # its decision (and 'due') is committed by now
			barrier.wait()
			return real(doc)

		with (
			patch.object(approvals_api, "_decision_message", side_effect=message),
			continuation_fixture.real_send_without_worker() as (dispatch, _send),
			patch("frappe.publish_realtime"),
		):
			other = self.fx.other_request(self.reviewer, lambda: approvals_api.decide(self.chat, "Yes"))
			self.assertTrue(decided.wait(timeout=20))
			with as_user(self.owner):
				mine = approvals_api.continue_approval_chat(self.chat)
				frappe.db.commit()
			reviewer = other.result(timeout=40)
		self.assertEqual(
			{
				"requester": (mine.get("ok"), mine.get("continuation")),
				"reviewer": (reviewer.get("resumed"), reviewer.get("continuation")),
				"messages": [r.origin for r in self.user_messages(conv)],
				"dispatch": dispatch.call_count,
			},
			{
				"requester": (True, "done"),
				"reviewer": (True, "done"),
				"messages": ["board_answer"],
				"dispatch": 1,
			},
		)

	def test_claim_and_answer_in_chat_wait_briefly_and_never_deadlock(self):
		"""N13, both orders: a send claims decided question A while the chat's
		answer-in-chat settles open question B, on two connections."""
		self.tag()
		conv = self.conversation_of(self.chat)
		other_question = self.fx.question("Chat")
		frappe.db.set_value(AR, other_question, "conversation", conv)
		frappe.db.commit()
		with as_user(self.reviewer), patch("frappe.publish_realtime"):
			approval_reviewers.decide_pending(self.chat, "Yes", True)  # A: decided, 'due'
		# Order 1: B's answer holds its locks; A's send claims meanwhile.
		frappe.db.sql(
			"SELECT name FROM `tabJarvis Approval Request` WHERE name=%s FOR UPDATE", other_question
		)
		with continuation_fixture.real_send_without_worker(), patch("frappe.publish_realtime"):
			sent = self.fx.other_request(
				self.owner, lambda: approvals_api.continue_approval_chat(self.chat)
			).result(timeout=40)
			with as_user(self.owner):
				chat_asks.resolve_on_user_message(conv)
		self.assertEqual(sent, {"ok": True, "continuation": "done"})
		self.assertEqual(frappe.db.get_value(AR, other_question, "status"), "Answered")
		# Order 2: a send in flight holds the conversation and A's row (its claim).
		# The answer's existing FOR UPDATE scan of the conversation's questions waits
		# for that send to commit (prepare and the legacy handler hold no row lock
		# when they call it), then settles B: a wait, never a deadlock.
		third = self.fx.question("Chat")
		frappe.db.set_value(AR, third, "conversation", conv)
		frappe.db.commit()
		frappe.db.sql("SELECT name FROM `tabJarvis Conversation` WHERE name=%s FOR UPDATE", conv)
		frappe.db.sql("SELECT name FROM `tabJarvis Approval Request` WHERE name=%s FOR UPDATE", self.chat)
		with patch("frappe.publish_realtime"):
			answer = self.fx.other_request(self.owner, lambda: chat_asks.resolve_on_user_message(conv))
			waiting = not answer.done()
			try:
				answer.result(timeout=1)
			except TimeoutError:
				pass
			still_waiting = not answer.done()
			frappe.db.rollback()  # the send commits / ends
			answer.result(timeout=40)
		self.assertEqual(
			(
				waiting and still_waiting,
				frappe.db.get_value(AR, third, "status"),
				frappe.get_doc(AR, self.chat).get("resume_state"),
				frappe.db.get_value(AR, self.chat, "status"),
			),
			(True, "Answered", "done", "Approved"),
		)

	def test_frozen_decision_rewrite_racing_a_send_is_refused_and_the_original_is_sent(self):
		"""N20: a requester-SM REST rewrite during a send in flight."""
		conv = self.failed_by_reviewer("Use supplier A")
		ensure_user(self.owner, ("Jarvis User", "System Manager"))
		reading = Event()
		release = Event()
		real = approvals_api._decision_message

		def message(doc):
			reading.set()
			self.assertTrue(release.wait(timeout=20))
			return real(doc)

		with (
			patch.object(approvals_api, "_decision_message", side_effect=message),
			continuation_fixture.real_send_without_worker(),
			patch("frappe.publish_realtime"),
		):
			other = self.fx.other_request(self.owner, lambda: approvals_api.continue_approval_chat(self.chat))
			self.assertTrue(reading.wait(timeout=20))
			with as_user(self.owner), self.assertRaises(frappe.PermissionError):
				client.set_value(AR, self.chat, "decision", "Use supplier B")
			frappe.db.rollback()
			release.set()
			sent = other.result(timeout=40)
		rows = self.user_messages(conv)
		self.assertEqual(
			(
				sent,
				len(rows),
				"Use supplier A" in rows[0].content,
				frappe.db.get_value(AR, self.chat, "decision"),
			),
			({"ok": True, "continuation": "done"}, 1, True, "Use supplier A"),
		)

	def test_a_refused_attempt_that_loses_to_an_accepted_send_reports_no_failure(self):
		"""F1 / N8: the reviewer's own attempt is refused (nothing inserted) while the
		requester's send on a second connection commits the message and 'done' before
		the refused attempt records its failure. Exactly one message; nobody is told it
		was not sent. OFF, server default and ON (per session)."""
		import threading

		for label, mode in (("off", 0), ("default", None), ("on", 1)):
			with self.subTest(mode=label):
				self.chat = self.fx.question("Chat")
				self.tag()
				conv = self.conversation_of(self.chat)
				main = threading.get_ident()
				real_resume = approvals_api._resume_conversation
				real_read = approval_reviewers.stored_resume_state
				other = {}

				def resume(*args, _real=real_resume, _main=main, **kwargs):
					if threading.get_ident() == _main:  # a known refusal before any insert
						return {"ok": False, "reason": "a reply is already in progress - hang on a moment"}
					return _real(*args, **kwargs)

				def read(name, _real=real_read, _main=main, _other=other, _mode=mode):
					state = _real(name)
					if threading.get_ident() == _main and "b" not in _other:

						def send():
							with session_snapshot_isolation(_mode):
								return approvals_api.continue_approval_chat(self.chat)

						_other["b"] = self.fx.other_request(self.owner, send).result(timeout=40)
					return state

				realtime = continuation_fixture.Realtime()
				with (
					patch.object(approvals_api, "_resume_conversation", side_effect=resume),
					patch.object(approval_reviewers, "stored_resume_state", side_effect=read),
					continuation_fixture.real_send_without_worker(),
					patch("frappe.publish_realtime", side_effect=realtime),
				):
					with as_user(self.reviewer), session_snapshot_isolation(mode):
						reviewer = approvals_api.decide(self.chat, "Yes")
						frappe.db.commit()
				outcome = (reviewer.get("resumed"), reviewer.get("continuation"))
				self.assertEqual(
					{
						"requester_send": other.get("b"),
						"messages": [r.origin for r in self.user_messages(conv)],
						"state": frappe.get_doc(AR, self.chat).get("resume_state"),
						"failure_events": realtime.payloads(self.owner, continuation_fixture.FAILED_KIND),
						"reviewer_told_not_sent": outcome[1] == "failed",
					},
					{
						"requester_send": {"ok": True, "continuation": "done"},
						"messages": ["board_answer"],
						"state": "done",
						"failure_events": [],
						"reviewer_told_not_sent": False,
					},
				)
				# OFF/default re-read the committed 'done'; ON may only know it as pending.
				self.assertIn(
					outcome, {(True, "done")} if mode != 1 else {(True, "done"), (False, "pending")}
				)
