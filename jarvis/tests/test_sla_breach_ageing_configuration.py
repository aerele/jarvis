"""SLA-breach auditor configuration must name one readable Helpdesk team."""

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_scheduler, agents_api
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

	def test_schedule_enable_refuses_invalid_or_malformed_scope_before_save_as_run_as(self):
		"""A schedule must not defer an invalid SLA boundary to its first cron tick."""
		for config in ({}, "{broken"):
			with self.subTest(config=config):
				doc = self._schedule_installation(config)
				run_as_contexts = []
				with (
					patch.object(frappe, "get_doc", return_value=doc),
					patch("jarvis.chat.agent_installability.assert_installable"),
					patch.object(agents_api, "is_valid_unattended_owner", return_value=True),
					patch.object(
						agents_api,
						"impersonate",
						side_effect=lambda user: self._as_run_as(run_as_contexts, user),
					),
				):
					with self.assertRaises(frappe.ValidationError):
						agents_api.set_schedule(doc.name, schedule_enabled=1)
				self.assertEqual(run_as_contexts, [doc.run_as_user])
				doc.save.assert_not_called()

	def test_schedule_enable_refuses_an_unreadable_team_before_save_as_run_as(self):
		doc = self._schedule_installation({"record_scope": {"doctype": "HD Team", "name": "Support East"}})
		team = self._unreadable_team()
		run_as_contexts = []
		with (
			patch.object(frappe, "get_doc", side_effect=self._schedule_documents(doc, team)),
			patch("jarvis.chat.agent_installability.assert_installable"),
			patch.object(agents_api, "is_valid_unattended_owner", return_value=True),
			patch.object(
				agents_api,
				"impersonate",
				side_effect=lambda user: self._as_run_as(run_as_contexts, user),
			),
		):
			with self.assertRaises(frappe.ValidationError):
				agents_api.set_schedule(doc.name, schedule_enabled=1)
		self.assertEqual(run_as_contexts, [doc.run_as_user])
		team.check_permission.assert_called_once_with("read")
		doc.save.assert_not_called()

	def test_schedule_enable_accepts_a_readable_team_as_run_as(self):
		doc = self._schedule_installation({"record_scope": {"doctype": "HD Team", "name": "Support East"}})
		team = MagicMock()
		run_as_contexts = []
		with (
			patch.object(frappe, "get_doc", side_effect=self._schedule_documents(doc, team)),
			patch("jarvis.chat.agent_installability.assert_installable"),
			patch.object(agents_api, "is_valid_unattended_owner", return_value=True),
			patch.object(
				agents_api,
				"impersonate",
				side_effect=lambda user: self._as_run_as(run_as_contexts, user),
			),
			patch.object(agents_api, "compute_next_run", return_value="2026-11-02 09:00:00"),
			patch.object(agents_api, "log_activity"),
			patch.object(frappe.db, "commit"),
		):
			result = agents_api.set_schedule(doc.name, schedule_enabled=1, schedule_frequency="daily")
		self.assertEqual(run_as_contexts, [doc.run_as_user])
		team.check_permission.assert_called_once_with("read")
		self.assertEqual(doc.schedule_enabled, 1)
		doc.save.assert_called_once_with()
		self.assertEqual(result["data"]["next_run_at"], "2026-11-02 09:00:00")

	def test_schedule_preflight_leaves_other_agents_unchanged(self):
		doc = frappe._dict(agent="close-auditor", run_as_user="any@example.invalid", config="{}")
		with (
			patch.object(agents_api, "is_valid_unattended_owner") as valid_owner,
			patch.object(agents_api, "impersonate") as as_run_as,
		):
			agents_api._assert_sla_schedule_configuration(doc)
		valid_owner.assert_not_called()
		as_run_as.assert_not_called()

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
	def _schedule_installation(config):
		doc = frappe._dict(
			name="SLA-SCHEDULE-CONFIG-TEST",
			agent=sla.AGENT,
			run_as_user="sla-runas@example.invalid",
			config=frappe.as_json(config) if isinstance(config, dict) else config,
			schedule_enabled=0,
			schedule_frequency="daily",
			schedule_time=None,
			schedule_weekday=None,
			schedule_day_of_month=None,
			next_run_at=None,
		)
		doc.check_permission = MagicMock()
		doc.save = MagicMock()
		return doc

	@staticmethod
	def _as_run_as(seen, user):
		seen.append(user)
		return nullcontext()

	@staticmethod
	def _schedule_documents(installation, team):
		def get_doc(doctype, name=None, *args, **kwargs):
			if doctype == "Jarvis Agent Installation" and name == installation.name:
				return installation
			if doctype == "HD Team" and name == "Support East":
				return team
			raise AssertionError(f"unexpected document lookup: {doctype} {name}")

		return get_doc

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
