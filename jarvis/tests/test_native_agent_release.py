"""The reviewed native replacement cannot restore historical agent authority."""

import copy
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_catalog, agent_closure, ticket_triage
from jarvis.exceptions import InvalidArgumentError


class TestNativeAgentRelease(FrappeTestCase):
	def setUp(self):
		self.agent = ticket_triage.AGENT
		self.release = agent_closure.native_release_spec(self.agent)
		self.assertIsNotNone(self.release)
		self.run = frappe._dict(
			agent=self.agent,
			bundle_version="0.2.0",
			trigger="manual",
			status="running",
			session_key="native-ticket-test",
			capability_contract="snapshot",
			tools_allow_json=json.dumps(list(self.release.tools)),
			capability_writes_json="[]",
			assessment_config_json=json.dumps({"policy_version": "ticket-triage-v1"}),
		)

	def test_exact_native_snapshot_allows_only_two_typed_calls(self):
		cap = {"agent": self.agent, "run": "NATIVE-TICKET", "legacy": False}
		with patch.object(frappe, "get_doc", return_value=self.run):
			for tool in (ticket_triage.SOURCE_TOOL, ticket_triage.STAGE_TOOL):
				self.assertIsNone(agent_closure.delegate_tool_denial(cap, tool))
			for tool in ("query", "get_doc", "create_doc", "update_doc", "record_agent_run", "exec"):
				self.assertTrue(agent_closure.delegate_tool_denial(cap, tool)["fatal"])
		ticket_triage._require_active_run(self.run)

	def test_old_expanded_or_malformed_snapshot_cannot_reach_native_source(self):
		changes = [
			{"bundle_version": "0.1.1"},
			{"trigger": "scheduled"},
			{"capability_contract": "legacy"},
			{"tools_allow_json": json.dumps([*self.release.tools, "jarvis__get_doc"])},
			{"tools_allow_json": json.dumps([self.release.tools[0]])},
			{"tools_allow_json": json.dumps([*self.release.tools, self.release.tools[0]])},
			{"tools_allow_json": "{}"},
			{"tools_allow_json": "[{}]"},
			{"capability_writes_json": '[{"doctype":"HD Ticket"}]'},
			{"assessment_config_json": '{"policy_version":"old"}'},
			{"assessment_config_json": "[]"},
		]
		for change in changes:
			with self.subTest(change=change):
				run = frappe._dict({**self.run, **change})
				cap = {"agent": self.agent, "run": "OLD-TICKET", "legacy": False}
				with patch.object(frappe, "get_doc", return_value=run):
					self.assertTrue(
						agent_closure.delegate_tool_denial(cap, ticket_triage.SOURCE_TOOL)["fatal"]
					)
				with self.assertRaises(InvalidArgumentError):
					ticket_triage._require_active_run(run)

	def test_legacy_capability_and_generic_session_remain_blocked_for_native_version(self):
		cap = {"agent": self.agent, "run": "NATIVE-TICKET", "legacy": True}
		with patch.object(frappe, "get_doc", return_value=self.run):
			self.assertTrue(agent_closure.delegate_tool_denial(cap, ticket_triage.SOURCE_TOOL)["fatal"])
		for prefix in agent_closure.closed_agent_spec(self.agent).session_prefixes:
			with self.assertRaises(frappe.PermissionError):
				agent_closure.assert_session_dispatch_allowed(prefix + "native-or-old")
		self.assertIsNotNone(agent_closure.direct_write_refusal(self.agent, "update", "HD Ticket"))

	def test_registry_drift_or_missing_code_pin_closes_release(self):
		for change in (
			{"version": "0.1.1"},
			{"status": "Coming Soon"},
			{"tools_allow": [*self.release.tools, "jarvis__update_doc"]},
			{"writes": [{"doctype": "HD Ticket"}]},
			{"default_schedule": {"schedule_enabled": 1}},
		):
			registry = copy.deepcopy(agent_catalog._load_registry())
			entry = next(a for a in registry["agents"] if a["agent_slug"] == self.agent)
			entry.update(change)
			with patch.object(agent_catalog, "_load_registry", return_value=registry):
				self.assertTrue(agent_closure.is_closed_agent(self.agent))
				self.assertEqual(agent_catalog.registry_tools_allow(self.agent), [])
				with self.assertRaises(frappe.PermissionError):
					agent_closure.assert_new_install_allowed(self.agent)
		with patch.dict(agent_closure._NATIVE_RELEASES, {}, clear=True):
			self.assertTrue(agent_closure.is_closed_agent(self.agent))

	def test_native_install_version_and_manual_only_rules_apply_before_launch(self):
		current = frappe._dict(agent=self.agent, installed_version="0.2.0", enabled=0, schedule_enabled=0)
		self.assertFalse(agent_closure.is_closed_installation(current))
		agent_closure.validate_installation(current)
		agent_closure.assert_launch_allowed(self.agent, current)
		old = frappe._dict({**current, "installed_version": "0.1.1"})
		self.assertTrue(agent_closure.is_closed_installation(old))
		with self.assertRaises(frappe.PermissionError):
			agent_closure.assert_enabled(self.agent, 1, old)
		with self.assertRaises(frappe.PermissionError):
			agent_closure.assert_launch_allowed(self.agent, old)
		for doc in (old, current):
			with self.assertRaises(frappe.PermissionError):
				agent_closure.assert_scheduled(doc.agent, 1)
		with self.assertRaises(frappe.PermissionError):
			agent_closure.validate_installation(frappe._dict({**current, "enabled": 1, "config": "{}"}))
