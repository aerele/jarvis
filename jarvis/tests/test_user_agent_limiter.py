"""The bench-side allowlist for user agent sessions in ``jarvis.api.call_tool``."""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import frappe

from jarvis import api
from jarvis.chat import user_agents
from jarvis.tests._user_agent_helpers import DOCTYPE, OTHER, OWNER, UserAgentCase
from jarvis.tests.test_api import _FakeRequest


class TestUserAgentLimiter(UserAgentCase):
	def setUp(self):
		super().setUp()
		self.agent = self.make_agent(tools_json='["get_list"]')
		self.key = user_agents.mint_session(self.agent.agent_id, OWNER)
		frappe.local.jarvis_user_agent_rows = None

	@contextlib.contextmanager
	def plugin(self, *, key=None, user=OWNER):
		key = key or self.key
		headers = {"X-Jarvis-Token": "t", "X-Jarvis-Session": key}
		with (
			patch.object(frappe, "request", _FakeRequest(headers), create=True),
			patch("jarvis.api.validate_plugin_request", return_value=key),
			patch("jarvis.chat.site_pin.pin_if_unset"),
			patch("jarvis.chat.collaboration.is_collaborative_session", return_value=False),
			patch("jarvis.chat.collaboration.legacy_session_user", return_value=user),
			patch("jarvis.api._persist_and_publish_tool_call"),
			patch("frappe.enqueue"),
			patch("jarvis.api.dispatch", return_value=[{"name": "T-1"}]) as dispatch,
		):
			frappe.local.jarvis_user_agent_rows = None
			yield dispatch

	def call(self, tool="get_list", **kw):
		with self.plugin(**kw) as dispatch:
			result = api.call_tool(tool=tool, args={"doctype": "ToDo"})
		return result, dispatch

	def assertRefused(self, result, dispatch, needle):
		self.assertFalse(result["ok"], msg=result)
		self.assertEqual(result["error"]["code"], "PermissionError")
		self.assertIn(needle, result["error"]["message"])
		self.assertEqual(frappe.local.response.http_status_code, 403)
		dispatch.assert_not_called()

	def test_a_picked_tool_runs(self):
		result, dispatch = self.call("get_list")
		self.assertTrue(result["ok"], msg=result)
		dispatch.assert_called_once()

	def test_the_prefixed_spelling_of_a_picked_tool_runs(self):
		self.assertIsNone(user_agents.tool_refusal(self.key, OWNER, "jarvis__get_list"))

	def test_a_tool_the_owner_did_not_pick_is_refused(self):
		result, dispatch = self.call("get_doc")
		self.assertRefused(result, dispatch, "not allowed to use the get_doc tool")

	def test_a_session_resolving_to_another_user_is_refused(self):
		result, dispatch = self.call("get_list", user=OTHER)
		self.assertRefused(result, dispatch, "belongs to someone else")

	def test_a_disabled_agent_is_refused(self):
		frappe.db.set_value(DOCTYPE, self.agent.name, "enabled", 0)
		result, dispatch = self.call("get_list")
		self.assertRefused(result, dispatch, "switched off")

	def test_a_deleted_agent_is_refused(self):
		frappe.db.delete(DOCTYPE, self.agent.name)
		result, dispatch = self.call("get_list")
		self.assertRefused(result, dispatch, "was deleted")

	def test_the_flag_off_refuses(self):
		self.flag(False)
		result, dispatch = self.call("get_list")
		self.assertRefused(result, dispatch, "turned off")

	def test_a_malformed_user_agent_key_is_refused(self):
		self.assertIn(
			"not valid", user_agents.tool_refusal("agent:agent-u-XYZ:dashboard:x", OWNER, "get_list")
		)

	def test_normal_chat_and_marketplace_delegates_are_untouched_without_a_query(self):
		with patch.object(frappe.db, "get_value", side_effect=AssertionError("queried")):
			for key in (
				"agent:main:dashboard:jarvis-chat-a-1",
				"agent:role-sales:dashboard:jarvis-chat-a-1",
				"agent:agent-ap-review:run-1",
				"agent:main:subagent:dashboard:collab:" + "a" * 32,
				"",
				None,
			):
				with self.subTest(key=key):
					self.assertIsNone(user_agents.tool_refusal(key, OWNER, "delete_doc"))

	def test_the_agent_row_is_read_once_per_request(self):
		frappe.local.jarvis_user_agent_rows = None
		with patch.object(frappe.db, "get_value", wraps=frappe.db.get_value) as get_value:
			user_agents.tool_refusal(self.key, OWNER, "get_list")
			user_agents.tool_refusal(self.key, OWNER, "get_doc")
		reads = [c for c in get_value.call_args_list if c.args and c.args[0] == DOCTYPE]
		self.assertEqual(len(reads), 1)
