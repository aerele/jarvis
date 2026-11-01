"""Payroll Pre-Run must derive its boundary from one run-as-readable entry."""

import json
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_scheduler, agent_scope, agents_api, payroll_prerun_scope

AGENT = "payroll-prerun-auditor"
LISTING = "Jarvis Agent Listing"
RUN = "Jarvis Agent Run"
CONV = "Jarvis Conversation"


class _PayrollEntry:
	def __init__(self, values, *, redacted=None):
		self.values = dict(values)
		self.redacted = redacted or {}
		self.checked = []

	def get(self, key, default=None):
		return self.values.get(key, default)

	def check_permission(self, ptype):
		self.checked.append(ptype)

	def apply_fieldlevel_read_permissions(self):
		self.values.update(self.redacted)


def _entry(**overrides):
	values = {
		"name": "PAY-TEST-2026-10",
		"company": "Payroll Test Co",
		"start_date": "2026-10-01",
		"end_date": "2026-10-31",
		"docstatus": 0,
		"salary_slips_submitted": 0,
		"salary_slip_based_on_timesheet": 0,
	}
	values.update(overrides)
	return _PayrollEntry(values)


class TestPayrollPreRunScope(unittest.TestCase):
	def test_server_derives_entry_scope_and_caps_report_date(self):
		entry = _entry(end_date="2026-10-15")
		config = {
			"payroll_entry": "PAY-TEST-2026-10",
			"reviewer_of_record": "Priya (payroll owner)",
		}
		base = {
			"company": "Payroll Test Co",
			"fiscal_year": None,
			"from_date": "2026-10-01",
			"to_date": "2026-10-15",
			"prior_fy_start": "2025-04-01",
			"prior_fy_end": "2026-03-31",
			"report_date": "2099-01-01",
		}
		with (
			patch.object(frappe, "get_doc", return_value=entry),
			patch("jarvis.chat.agent_scope._resolve", return_value=base),
			patch.object(frappe.utils, "today", return_value="2026-10-31"),
		):
			scope = payroll_prerun_scope.scope_from_config(config)

		self.assertEqual(entry.checked, ["read", "read"])
		self.assertEqual(scope["company"], "Payroll Test Co")
		self.assertEqual(scope["from_date"], "2026-10-01")
		self.assertEqual(scope["to_date"], "2026-10-15")
		self.assertEqual(scope["report_date"], "2026-10-15")
		self.assertEqual(scope["record_scope"], {"doctype": "Payroll Entry", "name": "PAY-TEST-2026-10"})
		self.assertEqual(scope["scope_kind"], "single_batch")

	def test_config_rejects_generic_period_and_raw_record_boundary(self):
		config = {
			"payroll_entry": "PAY-TEST-2026-10",
			"reviewer_of_record": "payroll-reviewer@example.invalid",
			"company": "Other Co",
			"from_date": "2020-01-01",
			"to_date": "2099-12-31",
			"record_scope": {"doctype": "Payroll Entry", "name": "OTHER-PAYROLL"},
		}
		with (
			patch.object(frappe, "get_doc", return_value=_entry()),
		):
			issues = payroll_prerun_scope.configuration_issues(config)

		self.assertEqual(set(issues), {"config"})
		with self.assertRaises(frappe.ValidationError):
			payroll_prerun_scope.require_configuration(config)

	def test_field_level_redaction_refuses_the_entry(self):
		with (
			patch.object(
				frappe, "get_doc", return_value=_PayrollEntry(_entry().values, redacted={"company": None})
			),
		):
			issues = payroll_prerun_scope.configuration_issues(
				{
					"payroll_entry": "PAY-TEST-2026-10",
					"reviewer_of_record": "payroll-reviewer@example.invalid",
				}
			)
		self.assertEqual(set(issues), {"payroll_entry"})

	def test_agent_scope_routes_payroll_to_the_server_owned_resolver(self):
		inst = frappe._dict(
			agent=AGENT,
			config=json.dumps(
				{
					"payroll_entry": "PAY-TEST-2026-10",
					"reviewer_of_record": "payroll-reviewer@example.invalid",
					"company": "attacker-controlled-company",
				}
			),
		)
		expected = {
			"company": "Payroll Test Co",
			"record_scope": {"doctype": "Payroll Entry", "name": "PAY-TEST-2026-10"},
		}
		with patch.object(payroll_prerun_scope, "scope_from_config", return_value=expected) as resolve:
			self.assertEqual(agent_scope.resolve_scope(inst), expected)
		resolve.assert_called_once_with(inst.config)

	def test_payroll_scope_fault_never_degrades_to_unscoped(self):
		inst = frappe._dict(agent=AGENT, config="{}", scoped_visibility=0)
		with (
			patch.object(agent_scope, "resolve_scope", side_effect=frappe.ValidationError("bad entry")),
			patch.object(agent_scheduler, "_permission_profile", return_value=None),
			patch("jarvis.chat.operator_review.backend", return_value=None),
			patch.object(frappe, "log_error"),
		):
			with self.assertRaises(frappe.ValidationError):
				agent_scheduler._stamp_scope_and_watermark("unused", inst, "run-as@example.invalid")

	def test_engagement_snapshot_delivers_only_declared_payroll_values(self):
		listing = frappe._dict(config_keys=["payroll_entry", "reviewer_of_record"])
		inst = frappe._dict(
			config=json.dumps(
				{
					"payroll_entry": "PAY-TEST-2026-10",
					"reviewer_of_record": "Priya (payroll owner)",
					"company": "must-not-reach-delegate",
					"record_scope": {"doctype": "Payroll Entry", "name": "other-entry"},
				}
			)
		)
		self.assertEqual(
			agent_scheduler._explicit_config(listing, inst),
			{
				"payroll_entry": "PAY-TEST-2026-10",
				"reviewer_of_record": "Priya (payroll owner)",
			},
		)


class TestPayrollPreRunLaunchBoundary(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from jarvis.chat import agent_catalog

		frappe.set_user("Administrator")
		agent_catalog.sync_agent_listings()
		cls.user = "payroll-pre-run-launch@example.invalid"
		if not frappe.db.exists("User", cls.user):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": cls.user,
					"first_name": "Payroll launch test",
					"send_welcome_email": 0,
					"enabled": 1,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value("User", cls.user, "enabled", 1, update_modified=False)
		cls.other_user = "payroll-pre-run-other@example.invalid"
		if not frappe.db.exists("User", cls.other_user):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": cls.other_user,
					"first_name": "Other payroll launch test",
					"send_welcome_email": 0,
					"enabled": 1,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value("User", cls.other_user, "enabled", 1, update_modified=False)
		frappe.db.commit()

	def test_missing_entry_stops_before_run_or_remote_dispatch(self):
		inst = frappe._dict(
			agent=AGENT,
			owner=self.user,
			run_as_user=self.user,
			reviewer=self.user,
			config="{}",
		)
		runs_before = frappe.db.count(RUN, {"agent": AGENT})
		conversations_before = frappe.db.count(CONV)
		with patch("jarvis.admin_client.post_agent_run") as dispatch:
			frappe.set_user(self.user)
			try:
				with self.assertRaises(frappe.ValidationError):
					agent_scheduler._launch_audit(inst, trigger="manual")
			finally:
				frappe.set_user("Administrator")
		dispatch.assert_not_called()
		self.assertEqual(frappe.db.count(RUN, {"agent": AGENT}), runs_before)
		self.assertEqual(frappe.db.count(CONV), conversations_before)

	def test_direct_launch_as_another_user_stops_before_entry_read_or_dispatch(self):
		"""A direct/internal caller cannot validate a Payroll Entry under its own access."""
		inst = frappe._dict(
			agent=AGENT,
			owner=self.user,
			run_as_user=self.other_user,
			reviewer=self.user,
			config="{}",
		)
		runs_before = frappe.db.count(RUN, {"agent": AGENT})
		conversations_before = frappe.db.count(CONV)
		with (
			patch("jarvis.admin_client.post_agent_run") as dispatch,
			patch("jarvis.chat.payroll_prerun_scope._validate_entry") as validate_entry,
		):
			frappe.set_user(self.user)
			try:
				with self.assertRaises(frappe.PermissionError):
					agent_scheduler._launch_audit(inst, trigger="manual")
			finally:
				frappe.set_user("Administrator")
		dispatch.assert_not_called()
		validate_entry.assert_not_called()
		self.assertEqual(frappe.db.count(RUN, {"agent": AGENT}), runs_before)
		self.assertEqual(frappe.db.count(CONV), conversations_before)

	def test_installation_validation_checks_the_entry_as_run_as_user(self):
		"""The controller does not validate a selected entry with the editor's access."""
		from jarvis.jarvis.doctype.jarvis_agent_installation.jarvis_agent_installation import (
			JarvisAgentInstallation,
		)

		inst = frappe._dict(
			agent=AGENT,
			run_as_user=self.other_user,
			config="{}",
			enabled=1,
		)
		seen = []
		with patch(
			"jarvis.chat.payroll_prerun_scope.require_configuration",
			side_effect=lambda _raw: seen.append(frappe.session.user),
		):
			frappe.set_user(self.user)
			try:
				JarvisAgentInstallation._guard_payroll_prerun_scope(inst)
				self.assertEqual(frappe.session.user, self.user)
			finally:
				frappe.set_user("Administrator")
		self.assertEqual(seen, [self.other_user])

	def test_configuration_issue_lookup_checks_the_entry_as_run_as_user(self):
		"""The Configure page must not say an entry is ready based on the editor's access."""
		original_get_value = frappe.db.get_value

		def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			if (
				doctype == "Jarvis Agent Installation"
				and filters == "PAYROLL-ISSUES"
				and fieldname == "run_as_user"
			):
				return self.other_user
			return original_get_value(doctype, filters, fieldname, *args, **kwargs)

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch(
				"jarvis.chat.payroll_prerun_scope.configuration_issues",
				side_effect=lambda _raw: {"checked_as": frappe.session.user},
			),
		):
			frappe.set_user(self.user)
			try:
				issues = agents_api._payroll_prerun_issues_for_run_as("PAYROLL-ISSUES", "{}")
				self.assertEqual(frappe.session.user, self.user)
			finally:
				frappe.set_user("Administrator")
		self.assertEqual(issues, {"checked_as": self.other_user})
