"""Tests for the Settings Activity / Usage tool-call reporting (issue #551).

Both panes reported ZERO tool calls for a chat whose transcript was rendering
ten tool cards, because both derived the count from a per-run map the SPA stamps
on the live ``run:end`` event. That map only ever holds the turns one browser
mount happened to watch, so a reload, a route change, or opening an older chat
read zero.

The fix moves the source of truth to the persisted ``role="tool"`` rows, which
is what these tests pin: rows go into the database, then the endpoints the panes
call must count them. Nothing is mocked and no live-run event is simulated, so a
regression back to a session-scoped source cannot pass. The fixture shape is the
one from the live report: three assistant turns calling 2, 3 and 5 tools.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.api import create_conversation, get_tool_activity, get_usage
from jarvis.tests.test_chat_api import (
	TEST_USER,
	_cleanup_user_conversations,
	_ensure_test_user,
)

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"

OTHER_USER = "jarvis-toolact-other@example.test"


def _ensure_other_user() -> None:
	"""A second chat user, so the ownership cases have someone else's
	conversation to be refused on. Idempotent."""
	if frappe.db.exists("User", OTHER_USER):
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": OTHER_USER,
			"first_name": "Jarvis",
			"last_name": "ToolActivity",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.add_roles("System Manager")
	frappe.db.commit()


class TestToolActivity(FrappeTestCase):
	def setUp(self):
		_ensure_test_user()
		_ensure_other_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		# get_usage reads token_budget_monthly off the Jarvis Settings Single.
		# Singles are document-cached, and a stale cached copy has flaked other
		# suites in this repo, so drop it before every case.
		frappe.clear_document_cache("Jarvis Settings")
		self.conversation = create_conversation()
		self.seq = 0

	def tearDown(self):
		# create_conversation COMMITS, so the FrappeTestCase rollback cannot undo
		# these rows. Both fixture users are cleaned explicitly, never Administrator
		# (that would wipe real chat history on a dev site).
		_cleanup_user_conversations()
		_cleanup_user_conversations(OTHER_USER)
		frappe.set_user(self._orig_user)

	# ---- fixture helpers ---------------------------------------------------

	def _row(self, role, conversation=None, **fields):
		"""Insert one chat message row in sequence order and return its name."""
		self.seq += 1
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conversation or self.conversation,
				"seq": self.seq,
				"role": role,
				**fields,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name

	def _turn(self, tools, conversation=None, **assistant_fields):
		"""One user ask, one assistant reply, then a tool receipt per name in
		``tools`` - the row shape a real turn leaves behind."""
		self._row("user", conversation=conversation, content="do the thing")
		self._row("assistant", conversation=conversation, content="done", **assistant_fields)
		for name in tools:
			self._row(
				"tool",
				conversation=conversation,
				tool_name=name,
				tool_status="completed",
				tool_result="{}",
			)

	def _live_shape(self):
		"""The exact transcript from the #551 report: 2, then 3, then 5 tools."""
		self._turn(["tool_search", "web_fetch"])
		self._turn(["tool_search", "tool_call", "get_schema"])
		self._turn(["tool_call", "get_list", "tool_call", "get_list", "get_list"])

	# ---- the primary bug ---------------------------------------------------

	def test_persisted_rows_report_a_non_zero_count(self):
		# The regression case. No run:end event, no live session state: only rows
		# in the database, exactly as a reloaded tab or an older chat sees them.
		self._live_shape()
		result = get_tool_activity(self.conversation)
		self.assertEqual(result["tool_calls"], 10)
		self.assertEqual([r["tools"] for r in result["runs"]], [5, 3, 2])

	def test_usage_reports_the_same_count_as_the_activity_pane(self):
		# The two panes disagreeing is half of what #551 reports, so the numbers
		# they read are asserted equal rather than each merely being non-zero.
		self._live_shape()
		self.assertEqual(
			get_usage(self.conversation)["chat_tool_calls"],
			get_tool_activity(self.conversation)["tool_calls"],
		)

	def test_names_are_reported_newest_turn_first(self):
		self._live_shape()
		runs = get_tool_activity(self.conversation)["runs"]
		self.assertEqual(runs[0]["names"], ["tool_call", "get_list", "tool_call", "get_list", "get_list"])
		self.assertEqual(runs[-1]["names"], ["tool_search", "web_fetch"])

	def test_plugin_prefix_is_stripped_from_names(self):
		# The agent runtime calls these as jarvis__<name>; the thread shows the
		# bare name, so the pane must too.
		self._turn(["jarvis__get_list"])
		runs = get_tool_activity(self.conversation)["runs"]
		self.assertEqual(runs[0]["names"], ["get_list"])

	# ---- grouping parity with the transcript accordion ---------------------

	def test_receipt_rows_are_not_counted(self):
		# A gated write renders inline in the thread as its own receipt chip, not
		# as an accordion entry. Counting it here would put two different tool
		# counts on one screen, which is the class of bug #551 is about.
		self._row("user", content="delete it")
		self._row("assistant", content="deleted")
		self._row("tool", tool_name="get_list", tool_status="completed")
		self._row("tool", tool_name="delete_doc", tool_status="completed", action_outcome="confirmed")
		self._row("tool", tool_name="delete_doc", action_outcome="discarded")
		result = get_tool_activity(self.conversation)
		self.assertEqual(result["tool_calls"], 1)
		self.assertEqual(result["runs"][0]["names"], ["get_list"])

	def test_a_hidden_row_does_not_split_a_turn(self):
		# The post-apply continuation prompt is a hidden user row. The SPA never
		# receives it, so treating it as a turn boundary here would report two
		# runs where the thread shows one.
		self._row("user", content="go")
		self._row("assistant", content="working")
		self._row("tool", tool_name="get_list", tool_status="completed")
		self._row("user", content="continue", hidden=1)
		self._row("tool", tool_name="get_doc", tool_status="completed")
		result = get_tool_activity(self.conversation)
		self.assertEqual(result["tool_calls"], 2)
		self.assertEqual(len(result["runs"]), 1)

	def test_turns_without_tools_are_omitted(self):
		self._turn([])
		self._turn(["get_list"])
		self._turn([])
		result = get_tool_activity(self.conversation)
		self.assertEqual(len(result["runs"]), 1)
		self.assertEqual(result["tool_calls"], 1)

	def test_empty_chat_reports_zero(self):
		result = get_tool_activity(self.conversation)
		self.assertEqual(result["tool_calls"], 0)
		self.assertEqual(result["runs"], [])
		self.assertEqual(get_usage(self.conversation)["chat_tool_calls"], 0)

	# ---- duration ----------------------------------------------------------

	def test_duration_comes_from_the_persisted_reply_span(self):
		self._turn(["get_list"], reply_duration_ms=26900)
		self.assertEqual(get_tool_activity(self.conversation)["runs"][0]["ms"], 26900)

	def test_an_implausible_persisted_duration_is_dropped(self):
		# A stamp past the 30 minute ceiling is nonsense, so it falls back to the
		# row's own span rather than rendering "7200.0s" next to a two second turn.
		self._turn(["get_list"], reply_duration_ms=7_200_000)
		ms = get_tool_activity(self.conversation)["runs"][0]["ms"]
		self.assertLess(ms, 1000)

	def test_an_unstamped_reply_falls_back_to_its_own_span(self):
		# reply_duration_ms is an Int column, so a legacy row reads 0, not NULL.
		# Treating 0 as a real duration would have made the fallback dead code.
		self._turn(["get_list"])
		self.assertLess(get_tool_activity(self.conversation)["runs"][0]["ms"], 1000)

	# ---- ownership ---------------------------------------------------------

	def test_another_users_conversation_is_refused(self):
		frappe.set_user(OTHER_USER)
		theirs = create_conversation()
		self._turn(["get_list"], conversation=theirs)
		frappe.set_user(TEST_USER)
		with self.assertRaises(frappe.PermissionError):
			get_tool_activity(theirs)

	def test_usage_never_counts_another_users_chat(self):
		# get_usage does not load the conversation doc, so its scoping is the
		# owned-conversation list. A foreign id must read 0, not that chat's real
		# count.
		frappe.set_user(OTHER_USER)
		theirs = create_conversation()
		self._turn(["get_list", "get_doc"], conversation=theirs)
		frappe.set_user(TEST_USER)
		self._turn(["get_list"])
		self.assertEqual(get_usage(theirs)["chat_tool_calls"], 0)
