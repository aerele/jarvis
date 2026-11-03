"""A server turn identity alone cannot grant a plugin execution capability."""

from unittest.mock import patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import events
from jarvis.tests._collaboration_helpers import CollaborationCase
from jarvis.tests._share_helpers import RECIPIENT


class TestCollaborationDispatch(CollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn()
		frappe.db.set_value(
			c.BINDING, self.bound.name, {"runtime_session_id": "physical", "runtime_policy_hash": "verified"}
		)

	def record(self, tool="get_schema"):
		from jarvis.chat.collaboration_callbacks import record_gateway_tool

		self.assertTrue(record_gateway_tool(self.turn.name, "observed-tool", "jarvis__" + tool))

	def test_tool_boundary_keeps_correlation_only_for_shared_parser(self):
		payload = {
			"stream": "tool",
			"data": {
				"phase": "start",
				"name": "jarvis__get_schema",
				"toolCallId": "observed-tool",
				"args": {"private": "do not retain"},
			},
		}
		self.assertEqual(events.parse_event(payload), {"kind": "tool_boundary"})
		self.assertEqual(
			events.parse_event(payload, collaboration=True),
			{"kind": "tool_boundary", "tool_name": "jarvis__get_schema", "tool_call_id": "observed-tool"},
		)

	def test_identity_only_scope_cannot_call_tools(self):
		from jarvis.api import _run_tool
		from jarvis.chat.collaboration_turn import turn_context

		with turn_context(self.bound, self.turn.name):
			self.assertFalse(
				_run_tool("get_schema", {"doctype": "Sales Invoice"}, conversation=self.conversation)["ok"]
			)

	def test_observed_callback_runs_as_recipient_and_restores_identity(self):
		from jarvis.api import _run_tool
		from jarvis.chat.collaboration_callbacks import callback_scope

		self.record()
		before = frappe.session.user

		def execute(*args, **kwargs):
			self.assertEqual(frappe.session.user, RECIPIENT)
			return {"ok": True, "data": {"fields": []}}

		with callback_scope(self.bound.session_key, "get_schema", "observed-tool", wait_s=0):
			with patch("jarvis.api._dispatch_and_wrap", side_effect=execute):
				self.assertTrue(
					_run_tool("get_schema", {"doctype": "Sales Invoice"}, conversation=self.conversation)[
						"ok"
					]
				)
		self.assertEqual(frappe.session.user, before)

	def test_callback_permit_cannot_be_spent_on_another_tool(self):
		from jarvis.api import _run_tool
		from jarvis.chat.collaboration_callbacks import callback_scope

		self.record()
		with callback_scope(self.bound.session_key, "get_schema", "observed-tool", wait_s=0):
			with patch("jarvis.api._dispatch_and_wrap") as execute:
				self.assertFalse(
					_run_tool(
						"delete_doc", {"doctype": "Note", "name": "foreign"}, conversation=self.conversation
					)["ok"]
				)
				execute.assert_not_called()

	def test_generic_memory_read_is_refused_before_dispatch(self):
		from jarvis.api import _run_tool
		from jarvis.chat.collaboration_callbacks import callback_scope

		self.record("get_doc")
		with callback_scope(self.bound.session_key, "get_doc", "observed-tool", wait_s=0):
			with patch("jarvis.api._dispatch_and_wrap") as execute:
				result = _run_tool(
					"get_doc",
					{"doctype": "Jarvis User Memory", "name": RECIPIENT},
					conversation=self.conversation,
				)
				self.assertFalse(result["ok"])
				execute.assert_not_called()
