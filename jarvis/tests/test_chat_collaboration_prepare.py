"""Prepare a real queued row without owner sessions, history or credentials."""

import json
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import prepare
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests._share_helpers import RECIPIENT
from jarvis.tests.test_chat_collaboration_runtime_plan import PlanningGateway


def setUpModule():
	install_synthetic_runtime_profile()


class TestCollaborationPrepare(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		frappe.db.set_value(
			"Jarvis Chat Turn",
			self.turn.name,
			{"state": "queued", "reserved": 1, "collaboration_send_started_at": None},
		)
		frappe.db.set_value(
			c.CONVERSATION,
			self.conversation,
			{
				"session_key": "OWNER_PRIVATE_SESSION",
				"model_override": "OWNER_PRIVATE_MODEL",
				"thinking_override": "high",
			},
		)
		frappe.db.commit()
		self.gateway = PlanningGateway()

		@contextmanager
		def checkout(*args, **kwargs):
			yield self.gateway

		for item in (
			patch("jarvis.chat.agent_session_pool.checkout", checkout),
			patch("jarvis.chat.pump.ensure_pump"),
			patch("jarvis.chat.pump.lpush_wake"),
			patch("jarvis.chat.policy.validate_can_send", return_value=(True, None)),
			patch.dict(
				frappe.conf,
				{"chat_collaboration_model_budgets": {"provider/actor-model": {"runtime_reserve": 1024}}},
			),
		):
			item.start()
			self.addCleanup(item.stop)

	def test_prepared_payload_and_placeholder_use_the_recipient_binding(self):
		result = prepare.run_prepare(self.turn.name)
		self.assertTrue(result.get("ready"), result)
		turn = frappe.get_doc("Jarvis Chat Turn", self.turn.name)
		payload = json.loads(turn.dispatch_payload)
		self.assertEqual(turn.state, "ready")
		self.assertEqual(payload["session_key"], self.bound.session_key)
		self.assertEqual(payload["chat_user"], RECIPIENT)
		self.assertEqual(payload["collaboration_runtime"]["model_ref"], "provider/actor-model")
		self.assertEqual(payload["collaboration_runtime"]["skills_root"], "/workspace/skills")
		self.assertNotIn("OWNER_PRIVATE_SESSION", turn.dispatch_payload)
		self.assertNotIn("OWNER_PRIVATE_MODEL", turn.dispatch_payload)
		self.assertIsNone(payload["thinking"])
		row = frappe.get_doc("Jarvis Chat Message", turn.assistant_message)
		self.assertEqual(
			(row.actor, row.owner, row.collaboration_binding), (RECIPIENT, RECIPIENT, self.bound.name)
		)
		self.assertIsNone(turn.collaboration_send_started_at)
		self.assertFalse(self.gateway.sends)

	def test_missing_attested_skills_root_refuses_before_gateway_requests(self):
		with patch.dict(frappe.conf, {"chat_collaboration_skills_root": None}):
			result = prepare.run_prepare(self.turn.name)
		self.assertFalse(result["ok"])
		self.assertEqual(self.gateway.calls, [])
		self.assertFalse(self.gateway.sends)

	def test_transient_prepare_failure_keeps_the_unsent_request_retryable(self):
		with patch(
			"jarvis.chat.collaboration_workers.resolve_runtime",
			side_effect=AgentUnreachableError("temporary", code="ack-timeout"),
		):
			result = prepare.run_prepare(self.turn.name)
		self.assertFalse(result["ok"])
		row = frappe.get_doc("Jarvis Chat Turn", self.turn.name)
		self.assertFalse(row.collaboration_send_started_at)
		self.assertTrue(row.collaboration_retry_allowed)
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Active")

	def test_lagging_previous_usage_keeps_the_unsent_child_retryable(self):
		from jarvis.chat import usage
		from jarvis.chat.collaboration_bindings import stamp

		parent = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Turn",
				"run_id": uuid.uuid4().hex,
				"conversation": self.conversation,
				"relay_target_id": "default",
				"state": "done",
				"seed_message": self.turn.seed_message,
				"terminal_kind": "relay:final",
				"collaboration_send_started_at": frappe.utils.now_datetime(),
			}
		)
		stamp(parent, self.bound)
		parent.insert(ignore_permissions=True)
		frappe.db.commit()
		with patch("jarvis.chat.collaboration_usage.capture", return_value=usage.USAGE_RETRY):
			result = prepare.run_prepare(self.turn.name)
		self.assertFalse(result["ok"])
		row = frappe.get_doc("Jarvis Chat Turn", self.turn.name)
		self.assertFalse(row.collaboration_send_started_at)
		self.assertTrue(row.collaboration_retry_allowed)
		self.assertFalse(self.gateway.sends)

	def test_revoked_queued_request_never_contacts_gateway_and_releases_credit(self):
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		result = prepare.run_prepare(self.turn.name)
		self.assertFalse(result["ok"])
		self.assertEqual(self.gateway.calls, [])
		turn = frappe.get_doc("Jarvis Chat Turn", self.turn.name)
		self.assertIn(turn.state, ("errored", "cancelled"))
		self.assertFalse(turn.reserved)

	def test_unknown_complete_context_budget_refuses_without_model_execution(self):
		with patch.dict(frappe.conf, {"chat_collaboration_model_budgets": {}}):
			result = prepare.run_prepare(self.turn.name)
		self.assertFalse(result["ok"])
		self.assertFalse(self.gateway.sends)
		self.assertEqual(frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "state"), "errored")

	def test_smaller_model_omits_whole_old_records_and_keeps_current_request(self):
		frappe.db.set_value("Jarvis Chat Message", self.message, "content", "OLD_WHOLE_RECORD:" + "x" * 12000)
		request = frappe.db.get_value("Jarvis Chat Message", self.turn.seed_message, "content")
		frappe.db.commit()
		result = prepare.run_prepare(self.turn.name)
		self.assertTrue(result.get("ready"), result)
		payload = json.loads(frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "dispatch_payload"))
		self.assertIn(request, payload["message"])
		self.assertIn("[Older content omitted]", payload["message"])
		self.assertNotIn("OLD_WHOLE_RECORD:", payload["message"])
		self.assertLessEqual(len(payload["message"].encode()), self.gateway.capacity - 1024)
		self.assertFalse(self.gateway.sends)
