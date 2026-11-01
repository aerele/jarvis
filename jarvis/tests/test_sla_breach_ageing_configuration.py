"""SLA-breach auditor configuration must name one readable Helpdesk team."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_scheduler
from jarvis.chat import sla_breach_ageing as sla


class ReachedCapabilityGate(Exception):
	"""Stop a launch after its configuration preflight without creating records."""


class TestSlaBreachAgeingConfiguration(FrappeTestCase):
	def test_missing_and_malformed_record_scope_are_rejected_without_lookup(self):
		invalid = (
			None,
			"{broken",
			"[]",
			{},
			{"record_scope": None},
			{"record_scope": {"doctype": "HD Team"}},
			{"record_scope": {"doctype": "HD Team", "name": ""}},
			{"record_scope": {"doctype": "HD Team", "name": " Support East "}},
			{"record_scope": {"doctype": "Team", "name": "Support East"}},
			{"record_scope": {"doctype": "HD Team", "name": "Support East", "extra": 1}},
		)
		for config in invalid:
			with self.subTest(config=config), patch.object(frappe, "get_doc") as get_doc:
				self.assertEqual(set(sla.configuration_issues(config)), {"sla_team"})
				get_doc.assert_not_called()

	def test_unreadable_team_is_rejected(self):
		team = MagicMock()
		team.check_permission.side_effect = frappe.PermissionError("no read")
		with patch.object(frappe, "get_doc", return_value=team):
			self.assertEqual(
				set(
					sla.configuration_issues({"record_scope": {"doctype": "HD Team", "name": "Support East"}})
				),
				{"sla_team"},
			)
		team.check_permission.assert_called_once_with("read")

	def test_valid_selected_team_has_no_issue_and_checks_read_permission(self):
		team = MagicMock()
		config = {"record_scope": {"doctype": "HD Team", "name": "Support East"}}
		with patch.object(frappe, "get_doc", return_value=team) as get_doc:
			self.assertEqual(sla.configuration_issues(config), {})
		get_doc.assert_called_once_with("HD Team", "Support East")
		team.check_permission.assert_called_once_with("read")

	def test_launch_preflight_refuses_missing_malformed_or_unreadable_team_before_capability(self):
		for config, team in (
			({}, None),
			({"record_scope": {"doctype": "HD Team", "name": "Support East", "extra": 1}}, None),
			({"record_scope": {"doctype": "HD Team", "name": "Support East"}}, self._unreadable_team()),
		):
			with self.subTest(config=config):
				registry_tools = MagicMock(side_effect=AssertionError("must not reach capability gate"))
				with (
					patch.object(frappe, "get_doc", side_effect=self._documents(team)),
					patch("jarvis.chat.agent_catalog.registry_tools_allow", registry_tools),
				):
					with self.assertRaises(frappe.ValidationError):
						agent_scheduler._launch_audit(self._installation(config), trigger="scheduled")
				registry_tools.assert_not_called()

	def test_valid_selected_team_launch_reaches_the_next_guard(self):
		team = MagicMock()
		config = {"record_scope": {"doctype": "HD Team", "name": "Support East"}}
		with (
			patch.object(frappe, "get_doc", side_effect=self._documents(team)),
			patch(
				"jarvis.chat.agent_catalog.registry_tools_allow",
				side_effect=ReachedCapabilityGate,
			) as registry_tools,
		):
			with self.assertRaises(ReachedCapabilityGate):
				agent_scheduler._launch_audit(self._installation(config), trigger="scheduled")
		registry_tools.assert_called_once_with(sla.AGENT)
		team.check_permission.assert_called_once_with("read")

	@staticmethod
	def _installation(config):
		return frappe._dict(
			name="SLA-CONFIG-TEST",
			agent=sla.AGENT,
			owner="sla-owner@example.invalid",
			run_as_user="sla-runas@example.invalid",
			reviewer="sla-reviewer@example.invalid",
			config=frappe.as_json(config),
		)

	@staticmethod
	def _unreadable_team():
		team = MagicMock()
		team.check_permission.side_effect = frappe.PermissionError("no read")
		return team

	@staticmethod
	def _documents(team):
		listing = frappe._dict(agent_slug=sla.AGENT, status="Published")

		def get_doc(doctype, name=None, *args, **kwargs):
			if doctype == "Jarvis Agent Listing" and name == sla.AGENT:
				return listing
			if doctype == "HD Team" and name == "Support East" and team is not None:
				return team
			raise AssertionError(f"unexpected document lookup: {doctype} {name}")

		return get_doc
