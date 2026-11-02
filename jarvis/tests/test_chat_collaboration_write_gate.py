"""Shared writes always ask their initiator, including normally light writes."""

import uuid
from unittest.mock import patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import RECIPIENT


class TestCollaborationWriteGate(CommittedCollaborationCase):
	def test_light_write_parks_for_actor_with_no_inherited_bypass(self):
		from jarvis.api import _run_tool
		from jarvis.chat.collaboration_callbacks import callback_scope, record_gateway_tool

		bound, turn = self.running_turn(self.binding(uuid.uuid4().hex))
		frappe.db.set_value(
			c.BINDING, bound.name, {"runtime_session_id": "physical", "runtime_policy_hash": "verified"}
		)
		frappe.db.set_value(
			c.CONVERSATION, self.conversation, {"auto_mode": 1, "request_autorun": 1, "skill_autorun": 1}
		)
		self.assertTrue(record_gateway_tool(turn.name, "observed-write", "jarvis__add_tag"))
		with callback_scope(bound.session_key, "add_tag", "observed-write", wait_s=0):
			with (
				patch("jarvis.api._dispatch_and_wrap") as dispatch,
				patch("jarvis.chat.pending_confirm.mint", return_value="HUMAN_ONLY_TOKEN") as mint,
				patch("jarvis.api.persist_pending_action"),
				patch("jarvis.chat.events.publish_to_user"),
				patch("jarvis.chat.pending_confirm.blocks_new_card", return_value=False),
			):
				result = _run_tool(
					"add_tag",
					{"doctype": "Note", "name": "example", "tag": "review"},
					conversation=self.conversation,
				)
				dispatch.assert_not_called()
				mint.assert_called_once()
				self.assertEqual(mint.call_args.kwargs["owner"], RECIPIENT)
				self.assertEqual(mint.call_args.kwargs["exec_user"], RECIPIENT)
				self.assertEqual(mint.call_args.kwargs["run_id"], turn.name)
				self.assertFalse(mint.call_args.kwargs.get("skill_docname"))
				self.assertNotIn("HUMAN_ONLY_TOKEN", frappe.as_json(result))
