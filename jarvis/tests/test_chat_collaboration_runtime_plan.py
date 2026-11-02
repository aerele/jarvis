"""Use the actor agent's fresh default, never an owner's conversation pin."""

from unittest.mock import patch

import frappe

from jarvis.tests._collaboration_helpers import CollaborationCase
from jarvis.tests.test_chat_collaboration_runtime import Gateway


class PlanningGateway(Gateway):
	capacity = 8192

	def _request(self, method, params, **kwargs):
		if method == "models.list":
			return {
				"payload": {
					"models": [{"provider": "provider", "id": self.model, "contextWindow": self.capacity}]
				}
			}
		return super()._request(method, params, **kwargs)


class TestCollaborationRuntimePlan(CollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound = self.binding()
		self.gateway = PlanningGateway()
		config = patch.dict(
			frappe.conf,
			{"chat_collaboration_model_budgets": {"provider/actor-model": {"runtime_reserve": 1024}}},
		)
		config.start()
		self.addCleanup(config.stop)

	def resolve(self):
		from jarvis.chat.collaboration_runtime import resolve_runtime

		return resolve_runtime(self.gateway, self.bound, skills_root="/workspace/skills")

	def test_fresh_agent_default_is_pinned_and_verified_without_a_model_send(self):
		plan = self.resolve()
		self.assertEqual(plan.policy.model_ref, "provider/actor-model")
		self.assertEqual(plan.context_tokens, 8192)
		self.assertEqual(plan.runtime_reserve, 1024)
		self.assertEqual(self.gateway.creates, 1)
		self.assertFalse(self.gateway.sends)
		self.assertNotIn("model", self.gateway.calls[0][1])

	def test_missing_capacity_and_no_attestation_fails_closed(self):
		self.gateway.capacity = None
		with self.assertRaises(frappe.ValidationError):
			self.resolve()
		self.assertFalse(self.gateway.sends)

	def test_unknown_runtime_instruction_reserve_fails_closed(self):
		with patch.dict(frappe.conf, {"chat_collaboration_model_budgets": {}}):
			with self.assertRaises(frappe.ValidationError):
				self.resolve()

	def test_a_resolved_model_change_does_not_rebind_an_existing_logical_session(self):
		self.resolve()
		self.gateway.model = "different-model"
		with self.assertRaises(frappe.PermissionError):
			self.resolve()
		self.assertEqual(self.gateway.creates, 1)
