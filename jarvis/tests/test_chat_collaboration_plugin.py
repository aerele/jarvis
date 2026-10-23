"""Machine authentication must route collaborative tools to their live actor."""

import json
import uuid
from unittest.mock import patch

import frappe

from jarvis import api
from jarvis.chat import collaboration as c
from jarvis.chat.collaboration_callbacks import record_gateway_tool
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import OWNER, RECIPIENT


class TestCollaborationPlugin(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		frappe.db.set_value(
			c.BINDING, self.bound.name, {"runtime_session_id": "physical", "runtime_policy_hash": "verified"}
		)
		record_gateway_tool(self.turn.name, "qa-call", "jarvis__get_schema")
		frappe.db.commit()
		self.headers = {"X-Jarvis-Token": "qa-authenticated", "X-Jarvis-Tool-Call-Id": "qa-call"}
		for item in (
			patch("jarvis.api.validate_plugin_request", return_value=self.bound.session_key),
			patch("jarvis.api._get_header", side_effect=lambda name: self.headers.get(name, "")),
		):
			item.start()
			self.addCleanup(item.stop)

	def test_authenticated_plugin_runs_as_recipient_and_scrubs_model_result(self):
		def tool(*args, **kwargs):
			self.assertEqual(frappe.session.user, RECIPIENT)
			self.assertEqual(kwargs["conversation"], self.conversation)
			return {"ok": True, "data": {"api_key": "PRIVATE_SENTINEL", "total": 3}}

		with patch("jarvis.api._run_tool", side_effect=tool) as dispatch:
			result = api.call_tool("get_schema", {"doctype": "Sales Invoice"})
		self.assertTrue(result["ok"], result)
		self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))
		dispatch.assert_called_once()
		row = frappe.db.get_value(
			"Jarvis Chat Message",
			{"conversation": self.conversation, "role": "tool", "collaboration_binding": self.bound.name},
			["owner", "actor", "collaboration_binding", "collaboration_run_id"],
			as_dict=True,
		)
		self.assertEqual(
			(row.owner, row.actor, row.collaboration_binding, row.collaboration_run_id),
			(RECIPIENT, RECIPIENT, self.bound.name, self.turn.name),
		)

	def test_duplicate_callback_returns_the_first_receipt_without_reexecuting(self):
		with patch("jarvis.api._run_tool", return_value={"ok": True, "data": {"count": 3}}) as dispatch:
			first = api.call_tool("get_schema", {"doctype": "Sales Invoice"})
			second = api.call_tool("get_schema", {"doctype": "Sales Invoice"})
		self.assertTrue(first["ok"], first)
		self.assertEqual(second, first)
		dispatch.assert_called_once()

	def test_a_tool_commit_cannot_allow_a_duplicate_callback_to_reexecute(self):
		duplicates = []
		calls = 0

		def tool(*args, **kwargs):
			nonlocal calls
			calls += 1
			if calls == 1:
				frappe.db.commit()
				duplicates.append(api.call_tool("get_schema", {"doctype": "Sales Invoice"}))
			return {"ok": True, "data": {"count": 3}}

		with patch("jarvis.api._run_tool", side_effect=tool):
			result = api.call_tool("get_schema", {"doctype": "Sales Invoice"})
		self.assertTrue(result["ok"])
		self.assertEqual(calls, 1)
		self.assertEqual(duplicates[0]["error"]["code"], "ToolRunning")

	def test_reused_provider_call_id_in_a_new_binding_keeps_both_actors_receipts(self):
		with patch("jarvis.api._run_tool", return_value={"ok": True, "data": {"count": 3}}):
			self.assertTrue(api.call_tool("get_schema", {"doctype": "Sales Invoice"})["ok"])
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "state", "done")
		second, turn = self.running_turn(self.binding(uuid.uuid4().hex, actor=OWNER))
		frappe.db.set_value(
			c.BINDING, second.name, {"runtime_session_id": "physical-2", "runtime_policy_hash": "verified"}
		)
		record_gateway_tool(turn.name, "qa-call", "jarvis__get_schema")
		frappe.db.commit()
		with (
			patch("jarvis.api.validate_plugin_request", return_value=second.session_key),
			patch("jarvis.api._run_tool", return_value={"ok": True, "data": {"count": 5}}),
		):
			self.assertTrue(api.call_tool("get_schema", {"doctype": "Sales Invoice"})["ok"])
		rows = frappe.get_all(
			"Jarvis Chat Message",
			{"conversation": self.conversation, "role": "tool"},
			["actor", "collaboration_binding"],
		)
		self.assertEqual(
			{(row.actor, row.collaboration_binding) for row in rows},
			{(RECIPIENT, self.bound.name), (OWNER, second.name)},
		)

	def test_bootstrap_memory_refuses_before_observation_without_a_receipt(self):
		self.headers["X-Jarvis-Tool-Call-Id"] = "bootstrap-recall"
		before = frappe.db.count("Jarvis Chat Message", {"conversation": self.conversation})
		with patch("jarvis.api._run_tool") as dispatch:
			self.assertFalse(api.call_tool("recall", {"query": "private preferences"})["ok"])
			dispatch.assert_not_called()
		self.assertEqual(frappe.db.count("Jarvis Chat Message", {"conversation": self.conversation}), before)

	def test_missing_observed_call_id_never_dispatches(self):
		self.headers["X-Jarvis-Tool-Call-Id"] = ""
		with patch("jarvis.api._run_tool") as dispatch:
			self.assertFalse(api.call_tool("get_schema", {"doctype": "Sales Invoice"})["ok"])
			dispatch.assert_not_called()

	def test_revoked_binding_cannot_dispatch_an_observed_call(self):
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		with patch("jarvis.api._run_tool") as dispatch:
			self.assertFalse(api.call_tool("get_schema", {"doctype": "Sales Invoice"})["ok"])
			dispatch.assert_not_called()

	def test_claimed_late_receipt_stays_on_its_original_reply(self):
		from jarvis.chat.collaboration_bindings import insert_message, stamp
		from jarvis.chat.collaboration_turn import new_admission_context

		original = insert_message(self.bound, {"role": "assistant", "content": "original reply"})
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "assistant_message", original.name)
		frappe.db.commit()
		child_reply = []

		def complete_after_next_turn_started(*args, **kwargs):
			frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "state", "done")
			run = uuid.uuid4().hex
			with new_admission_context():
				seed = insert_message(self.bound, {"role": "user", "content": "new continuation"}, run_id=run)
				reply = insert_message(self.bound, {"role": "assistant", "content": "new reply"}, run_id=run)
				child_reply.append(reply.name)
				child = frappe.get_doc(
					{
						"doctype": "Jarvis Chat Turn",
						"run_id": run,
						"conversation": self.conversation,
						"relay_target_id": "default",
						"state": "dispatching",
						"seed_message": seed.name,
						"assistant_message": reply.name,
						"dispatching_at": frappe.utils.now_datetime(),
						"collaboration_send_started_at": frappe.utils.now_datetime(),
					}
				)
				stamp(child, self.bound)
				child.insert(ignore_permissions=True)
			frappe.db.commit()
			return {
				"ok": True,
				"data": {
					"file_url": "/private/files/qa-shared-report.pdf",
					"filename": "qa-shared-report.pdf",
				},
			}

		with patch("jarvis.api._run_tool", side_effect=complete_after_next_turn_started):
			result = api.call_tool("get_schema", {"doctype": "Sales Invoice"})
		self.assertTrue(result["ok"], result)
		receipt = frappe.db.get_value(
			"Jarvis Chat Message",
			{"conversation": self.conversation, "role": "tool", "collaboration_run_id": self.turn.name},
			"tool_call_id",
		)
		self.assertTrue(receipt)
		self.assertIn(
			receipt,
			json.loads(frappe.db.get_value("Jarvis Chat Message", original.name, "tool_call_ids") or "[]"),
		)
		self.assertNotIn(
			receipt,
			json.loads(frappe.db.get_value("Jarvis Chat Message", child_reply[0], "tool_call_ids") or "[]"),
		)
		self.assertTrue(
			json.loads(frappe.db.get_value("Jarvis Chat Message", original.name, "canvas") or "[]")
		)
		self.assertFalse(
			json.loads(frappe.db.get_value("Jarvis Chat Message", child_reply[0], "canvas") or "[]")
		)
