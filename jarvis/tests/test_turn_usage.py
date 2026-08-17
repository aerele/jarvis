"""Tests for per-turn usage capture (usage-dashboard Part A, task U1).

Hermetic like test_usage_per_model.py: disposable fixture users/sessions
created in setUp, and because record_turn_usage COMMITS, every Jarvis User
Settings + Jarvis Chat Session + Jarvis Turn Usage row (plus, for the
tool_calls tests, the Jarvis Conversation / Jarvis Chat Turn / Jarvis Chat
Message rows) owned or referencing a fixture user is deleted in tearDown - a
transaction rollback cannot undo a commit.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import usage

USETT = "Jarvis User Settings"
SESSION = "Jarvis Chat Session"
TURN_USAGE = "Jarvis Turn Usage"
CONV = "Jarvis Conversation"
CHAT_TURN = "Jarvis Chat Turn"
MSG = "Jarvis Chat Message"

USER_A = "jarvis-turnusage-a@example.test"
_ALL_USERS = (USER_A,)


def _ensure_user(email: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Jarvis",
				"last_name": "TurnUsageTest",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)


def _make_session(session_key: str, user: str, *, profile_agent_id: str = "", profile_tier: str = "") -> None:
	frappe.get_doc(
		{
			"doctype": SESSION,
			"session_key": session_key,
			"user": user,
			"profile_agent_id": profile_agent_id,
			"profile_tier": profile_tier,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def _cleanup() -> None:
	for name in frappe.get_all(TURN_USAGE, filters={"user": ["in", list(_ALL_USERS)]}, pluck="name"):
		frappe.delete_doc(TURN_USAGE, name, ignore_permissions=True, force=True)
	for name in frappe.get_all(CHAT_TURN, filters={"relay_target_id": ["like", "test-turnusage-%"]}, pluck="name"):
		frappe.delete_doc(CHAT_TURN, name, ignore_permissions=True, force=True)
	for name in frappe.get_all(CONV, filters={"title": ["like", "turnusage-fixture%"]}, pluck="name"):
		for msg in frappe.get_all(MSG, filters={"conversation": name}, pluck="name"):
			frappe.delete_doc(MSG, msg, ignore_permissions=True, force=True)
		frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
	for email in _ALL_USERS:
		for name in frappe.get_all(USETT, filters={"user": email}, pluck="name"):
			frappe.delete_doc(USETT, name, ignore_permissions=True, force=True)
		for name in frappe.get_all(SESSION, filters={"user": email}, pluck="name"):
			frappe.delete_doc(SESSION, name, ignore_permissions=True, force=True)


class TestTurnUsage(FrappeTestCase):
	def setUp(self):
		self._orig_user = frappe.session.user
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_cleanup()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_cleanup()
		frappe.db.commit()
		frappe.set_user(self._orig_user)

	def _row(self, **kw):
		base = {
			"totalTokensFresh": True,
			"inputTokens": 0,
			"outputTokens": 0,
			"totalTokens": 0,
			"model": "gpt-5.5",
			"modelProvider": "openai",
		}
		base.update(kw)
		return base

	# -- (a) RECORDED path writes one row with profile fields + in/out ------- #
	def test_recorded_row_carries_profile_and_tokens(self):
		_make_session("agent:tu-rec", USER_A, profile_agent_id="role-hr", profile_tier="lite")
		outcome = usage.record_turn_usage(
			"agent:tu-rec", self._row(inputTokens=10, outputTokens=5, totalTokens=100)
		)
		self.assertEqual(outcome, usage.USAGE_RECORDED)
		rows = frappe.get_all(
			TURN_USAGE,
			filters={"session_key": "agent:tu-rec"},
			fields=[
				"user",
				"profile_agent_id",
				"profile_tier",
				"model",
				"tokens_in",
				"tokens_out",
				"cache_read",
				"cache_write",
				"cache_reported",
			],
		)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row.user, USER_A)
		self.assertEqual(row.profile_agent_id, "role-hr")
		self.assertEqual(row.profile_tier, "lite")
		self.assertEqual(row.model, "gpt-5.5")
		self.assertEqual(row.tokens_in, 10)
		self.assertEqual(row.tokens_out, 5)
		# Live-checked 2026-08-17: the gateway build in use reports no cache
		# token fields on sessions.list rows, so these are always the honest
		# "not reported" state, never a fabricated zero.
		self.assertEqual(row.cache_read, 0)
		self.assertEqual(row.cache_write, 0)
		self.assertEqual(row.cache_reported, 0)

	# -- (b) VALID_ZERO path still writes a row (attribution, zero tokens) -- #
	def test_valid_zero_row_writes_attribution(self):
		_make_session("agent:tu-zero", USER_A, profile_agent_id="", profile_tier="full")
		outcome = usage.record_turn_usage("agent:tu-zero", self._row(inputTokens=0, outputTokens=0))
		self.assertEqual(outcome, usage.USAGE_VALID_ZERO)
		rows = frappe.get_all(
			TURN_USAGE,
			filters={"session_key": "agent:tu-zero"},
			fields=["user", "tokens_in", "tokens_out"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].user, USER_A)
		self.assertEqual(rows[0].tokens_in, 0)
		self.assertEqual(rows[0].tokens_out, 0)

	# -- (c) RETRY path writes nothing ---------------------------------------- #
	def test_retry_path_writes_nothing(self):
		_make_session("agent:tu-retry", USER_A)
		outcome = usage.record_turn_usage(
			"agent:tu-retry", self._row(totalTokensFresh=False, inputTokens=10, outputTokens=5)
		)
		self.assertEqual(outcome, usage.USAGE_RETRY)
		self.assertFalse(frappe.db.exists(TURN_USAGE, {"session_key": "agent:tu-retry"}))

	def test_retry_no_session_mapping_writes_nothing(self):
		outcome = usage.record_turn_usage(
			"agent:tu-unmapped", self._row(inputTokens=5, outputTokens=5)
		)
		self.assertEqual(outcome, usage.USAGE_RETRY)
		self.assertFalse(frappe.db.exists(TURN_USAGE, {"session_key": "agent:tu-unmapped"}))

	# -- (d) a raising turn-usage write must not break the accrual ---------- #
	def test_row_write_failure_does_not_change_outcome(self):
		_make_session("agent:tu-fail", USER_A)
		title = "jarvis usage: turn usage row write failed"
		before = frappe.db.count("Error Log", {"method": title})
		with patch("jarvis.chat.usage._insert_turn_usage_row", side_effect=RuntimeError("boom")):
			outcome = usage.record_turn_usage(
				"agent:tu-fail", self._row(inputTokens=7, outputTokens=3, totalTokens=50)
			)
		self.assertEqual(outcome, usage.USAGE_RECORDED)
		# The aggregate accrual still landed even though the per-turn row write failed.
		s = frappe.db.get_value(USETT, {"user": USER_A}, "month_tokens")
		self.assertEqual(s, 10)
		after = frappe.db.count("Error Log", {"method": title})
		self.assertGreater(after, before)
		self.assertFalse(frappe.db.exists(TURN_USAGE, {"session_key": "agent:tu-fail"}))

	# -- (e) tool_calls counts tool-role messages seq-bounded to the turn --- #
	def _make_turn_with_tools(self, user: str) -> str:
		"""Conversation: [tool(before), user(seed), tool, tool, assistant, tool(after)].
		Returns the Jarvis Chat Turn's run_id. Only the two tool rows strictly
		between seed_message and assistant_message (by seq) must count."""
		conv = frappe.get_doc(
			{"doctype": CONV, "title": "turnusage-fixture", "status": "Active", "owner": user}
		)
		conv.insert(ignore_permissions=True)

		def _msg(seq: int, role: str, **kw) -> str:
			doc = frappe.get_doc(
				{"doctype": MSG, "conversation": conv.name, "seq": seq, "role": role, "content": "", **kw}
			)
			doc.insert(ignore_permissions=True)
			return doc.name

		_msg(1, "tool", tool_name="before_turn")
		seed_name = _msg(2, "user", content="do the thing")
		_msg(3, "tool", tool_name="in_turn_1")
		_msg(4, "tool", tool_name="in_turn_2")
		asst_name = _msg(5, "assistant", content="done")
		_msg(6, "tool", tool_name="after_turn")

		run_id = f"test-turnusage-{frappe.generate_hash(length=10)}"
		frappe.get_doc(
			{
				"doctype": CHAT_TURN,
				"run_id": run_id,
				"conversation": conv.name,
				"relay_target_id": run_id,
				"seed_message": seed_name,
				"assistant_message": asst_name,
				"state": "done",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return run_id

	def test_tool_calls_counts_only_rows_within_the_turn(self):
		_make_session("agent:tu-tools", USER_A)
		run_id = self._make_turn_with_tools(USER_A)
		outcome = usage.record_turn_usage(
			"agent:tu-tools",
			self._row(inputTokens=4, outputTokens=2, totalTokens=60),
			run_id=run_id,
		)
		self.assertEqual(outcome, usage.USAGE_RECORDED)
		tool_calls = frappe.db.get_value(TURN_USAGE, {"session_key": "agent:tu-tools"}, "tool_calls")
		self.assertEqual(tool_calls, 2)

	def test_tool_calls_zero_when_no_run_id(self):
		_make_session("agent:tu-norun", USER_A)
		usage.record_turn_usage("agent:tu-norun", self._row(inputTokens=1, outputTokens=1, totalTokens=10))
		tool_calls = frappe.db.get_value(TURN_USAGE, {"session_key": "agent:tu-norun"}, "tool_calls")
		self.assertEqual(tool_calls, 0)
