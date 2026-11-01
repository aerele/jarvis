"""FEFO configuration must fail before it creates a run or saves a schedule."""

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_scheduler, agents_api
from jarvis.chat import fefo_expiry_compliance as fefo


class ReachedCapabilityGate(Exception):
	"""Stop a valid launch before it creates any test records."""


class TestFefoExpiryComplianceConfiguration(FrappeTestCase):
	def test_policy_validation_matches_the_bundle_contract(self):
		# The declaration is optional. An explicit blank value is valid, which keeps
		# the bench aligned with the evaluator instead of treating it as a third
		# required policy field.
		valid = {
			"fefo": {
				"reviewer": " Stock Controller ",
				"near_expiry_days": 90,
				"test_declaration": "",
				"record_scope": {"doctype": "Warehouse", "name": "Optional FEFO Scope"},
			}
		}
		self.assertEqual(fefo.configuration_issues(valid), {})

		cases = (
			({}, {"fefo_reviewer", "fefo_near_expiry_days"}),
			({"fefo": {}}, {"fefo_reviewer", "fefo_near_expiry_days"}),
			(
				{"fefo": {"reviewer": "   ", "near_expiry_days": 90}},
				{"fefo_reviewer"},
			),
			(
				{"fefo": {"reviewer": 90, "near_expiry_days": 90}},
				{"fefo_reviewer"},
			),
			(
				{"fefo": {"reviewer": "x" * 141, "near_expiry_days": 90}},
				{"fefo_reviewer"},
			),
			(
				{"fefo": {"reviewer": "Stock Controller", "near_expiry_days": True}},
				{"fefo_near_expiry_days"},
			),
			(
				{"fefo": {"reviewer": "Stock Controller", "near_expiry_days": 0}},
				{"fefo_near_expiry_days"},
			),
			(
				{"fefo": {"reviewer": "Stock Controller", "near_expiry_days": 3651}},
				{"fefo_near_expiry_days"},
			),
			(
				{"fefo": {"reviewer": "Stock Controller", "near_expiry_days": "90"}},
				{"fefo_near_expiry_days"},
			),
			(
				{
					"fefo": {
						"reviewer": "Stock Controller",
						"near_expiry_days": 90,
						"test_declaration": "x" * 501,
					}
				},
				{"fefo_test_declaration"},
			),
			(
				{
					"fefo": {
						"reviewer": "Stock Controller",
						"near_expiry_days": 90,
						"test_declaration": 90,
					}
				},
				{"fefo_test_declaration"},
			),
			(
				{
					"fefo": {
						"reviewer": "Stock Controller",
						"near_expiry_days": 90,
						"unsupported": True,
					}
				},
				{"config"},
			),
		)
		for config, expected in cases:
			with self.subTest(config=config):
				self.assertEqual(set(fefo.configuration_issues(config)), expected)

	def test_invalid_json_and_non_object_config_are_rejected(self):
		for config in ("{broken", "[]", None):
			with self.subTest(config=config):
				self.assertEqual(set(fefo.configuration_issues(config)), {"config"})

	def test_launch_preflight_refuses_invalid_policy_before_capability_for_both_paths(self):
		for trigger, initiating_human in (("scheduled", None), ("manual", "fefo-owner@example.invalid")):
			with self.subTest(trigger=trigger):
				registry_tools = MagicMock(side_effect=AssertionError("must not reach capability gate"))
				with (
					patch.object(frappe, "get_doc", return_value=self._listing()),
					patch("jarvis.chat.operator_review.backend", return_value=None),
					patch("jarvis.chat.agent_catalog.registry_tools_allow", registry_tools),
					patch(
						"jarvis.chat.agent_scheduler._resolve_initiating_human", return_value=initiating_human
					),
				):
					with self.assertRaises(frappe.ValidationError):
						agent_scheduler._launch_audit(
							self._installation({"fefo": {}}),
							trigger=trigger,
							initiating_human=initiating_human,
						)
				registry_tools.assert_not_called()

	def test_valid_policy_reaches_the_next_launch_guard(self):
		registry_tools = MagicMock(side_effect=ReachedCapabilityGate)
		with (
			patch.object(frappe, "get_doc", return_value=self._listing()),
			patch("jarvis.chat.operator_review.backend", return_value=None),
			patch("jarvis.chat.agent_catalog.registry_tools_allow", registry_tools),
		):
			with self.assertRaises(ReachedCapabilityGate):
				agent_scheduler._launch_audit(self._installation(self._valid_config()), trigger="scheduled")
		registry_tools.assert_called_once_with(fefo.AGENT)

	def test_schedule_enable_refuses_invalid_policy_before_save_as_run_as(self):
		doc = self._schedule_installation({"fefo": {}})
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

	def test_schedule_enable_accepts_valid_policy_as_run_as(self):
		doc = self._schedule_installation(self._valid_config())
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
			patch.object(agents_api, "compute_next_run", return_value="2026-11-02 09:00:00"),
			patch.object(agents_api, "log_activity"),
			patch.object(frappe.db, "commit"),
		):
			result = agents_api.set_schedule(doc.name, schedule_enabled=1, schedule_frequency="daily")
		self.assertEqual(run_as_contexts, [doc.run_as_user])
		doc.save.assert_called_once_with()
		self.assertEqual(result["data"]["next_run_at"], "2026-11-02 09:00:00")

	def test_schedule_preflight_leaves_other_agents_unchanged(self):
		doc = frappe._dict(agent="close-auditor", run_as_user="any@example.invalid", config="{}")
		with (
			patch.object(agents_api, "is_valid_unattended_owner") as valid_owner,
			patch.object(agents_api, "impersonate") as as_run_as,
		):
			agents_api._assert_fefo_schedule_configuration(doc)
		valid_owner.assert_not_called()
		as_run_as.assert_not_called()

	def test_get_agent_exposes_fefo_field_issues(self):
		installation = frappe._dict(
			name="FEFO-CONFIG-DETAIL",
			enabled=1,
			installed_version="0.4.1",
			installed_at=None,
			config=frappe.as_json({"fefo": {}}),
			sync_status="",
			synced_at=None,
			schedule_enabled=0,
			schedule_frequency="daily",
			schedule_weekday=None,
			schedule_day_of_month=None,
			schedule_time=None,
			next_run_at=None,
			last_run_at=None,
		)
		with (
			patch.object(frappe, "get_doc", return_value=self._listing()),
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe.db, "count", return_value=1),
			patch.object(frappe, "get_all", return_value=[installation]),
			patch.object(agents_api, "has_jarvis_admin_access", return_value=False),
			patch.object(agents_api, "_user_allowed_for_agent", return_value=True),
		):
			result = agents_api.get_agent.__wrapped__(fefo.AGENT)
		self.assertEqual(
			set(result["installation"]["configuration_issues"]),
			{"fefo_reviewer", "fefo_near_expiry_days"},
		)

	@staticmethod
	def _valid_config():
		return {
			"fefo": {
				"reviewer": "Stock Controller",
				"near_expiry_days": 90,
				"test_declaration": "Fake records only.",
			}
		}

	@staticmethod
	def _installation(config):
		return frappe._dict(
			name="FEFO-CONFIG-TEST",
			agent=fefo.AGENT,
			owner="fefo-owner@example.invalid",
			run_as_user="fefo-runas@example.invalid",
			reviewer="fefo-reviewer@example.invalid",
			config=frappe.as_json(config),
		)

	@staticmethod
	def _schedule_installation(config):
		doc = frappe._dict(
			name="FEFO-SCHEDULE-CONFIG-TEST",
			agent=fefo.AGENT,
			run_as_user="fefo-runas@example.invalid",
			config=frappe.as_json(config),
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
	def _listing():
		return frappe._dict(
			name=fefo.AGENT,
			agent_slug=fefo.AGENT,
			title="FEFO Expiry Compliance Auditor",
			description="",
			category="Inventory",
			nature="Auditor",
			version="0.4.1",
			publisher="Jarvis",
			status="Published",
			tools_required="[]",
			min_apps="[]",
			rule_pack="",
			default_schedule="{}",
			validated_for_fy="",
			doctypes_required="[]",
			config_keys=frappe.as_json(["fefo.reviewer", "fefo.near_expiry_days", "fefo.test_declaration"]),
			operator_visibility="available",
		)

	@staticmethod
	def _as_run_as(seen, user):
		seen.append(user)
		return nullcontext()
