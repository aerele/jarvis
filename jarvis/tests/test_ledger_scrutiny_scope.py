"""Unit checks for Ledger Scrutiny's fail-closed company-period boundary."""

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from jarvis.chat import agent_runs, agent_scheduler
from jarvis.chat.agent_scope import as_of_date
from jarvis.chat.ledger_scrutiny_scope import (
	AGENT,
	bind_writeback_scope,
	requires_bound_scope,
	scoped_visibility,
	validate_bound_scope,
	validate_ledger_report_scope,
)
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError


class TestLedgerScrutinyScope(TestCase):
	def _scope(self):
		return {
			"company": "Test Co",
			"fiscal_year": "2026-2027",
			"from_date": "2026-04-01",
			"to_date": "2027-03-31",
			"report_date": "2026-10-31",
			"prior_fy_start": "2025-04-01",
			"prior_fy_end": "2026-03-31",
			"scoped_visibility": False,
		}

	def test_only_ledger_scrutiny_requires_a_bound_scope(self):
		self.assertTrue(requires_bound_scope(AGENT))
		self.assertFalse(requires_bound_scope("close-auditor"))

	def test_valid_scope_accepts_company_select_as_the_run_as_user(self):
		with (
			patch("jarvis.chat.ledger_scrutiny_scope.frappe.db.exists", return_value=True) as exists,
			patch("jarvis.chat.ledger_scrutiny_scope.assert_company_permitted") as permitted,
			patch("jarvis.chat.ledger_scrutiny_scope.frappe.has_permission", side_effect=(False, True)),
		):
			validate_bound_scope(
				{
					"company": "Test Co",
					"fiscal_year": "2026-2027",
					"from_date": "2026-04-01",
					"to_date": "2027-03-31",
					"report_date": "2026-10-31",
				}
			)
		exists.assert_called_once_with("Company", "Test Co")
		permitted.assert_called_once_with("Test Co")

	def test_valid_scope_refuses_an_unreadable_company(self):
		with (
			patch("jarvis.chat.ledger_scrutiny_scope.frappe.db.exists", return_value=True),
			patch("jarvis.chat.ledger_scrutiny_scope.assert_company_permitted"),
			patch("jarvis.chat.ledger_scrutiny_scope.frappe.has_permission", return_value=False),
			self.assertRaises(PermissionDeniedError),
		):
			validate_bound_scope(
				{
					"company": "Test Co",
					"fiscal_year": "2026-2027",
					"from_date": "2026-04-01",
					"to_date": "2027-03-31",
					"report_date": "2026-10-31",
				}
			)

	def test_missing_or_bad_boundary_is_refused_before_company_read(self):
		for scope in (
			None,
			{},
			{
				"company": "Test Co",
				"from_date": "2026-04-01",
				"to_date": "2026-04-30",
				"report_date": "2026-04-30",
			},
			{"company": "Test Co", "from_date": "bad", "to_date": "2026-04-30", "report_date": "2026-04-30"},
			{
				"company": "Test Co",
				"from_date": "2026-05-01",
				"to_date": "2026-04-30",
				"report_date": "2026-04-30",
			},
			{
				"company": "Test Co",
				"from_date": "2026-05-01",
				"to_date": "2026-05-31",
				"report_date": "2026-04-30",
			},
		):
			with self.subTest(scope=scope), self.assertRaises(InvalidArgumentError):
				validate_bound_scope(scope)

	def test_launch_visibility_comes_from_the_run_as_permission_profile(self):
		with patch("frappe.permissions.get_user_permissions", return_value={"Cost Center": ["North"]}):
			self.assertTrue(scoped_visibility("auditor@example.com"))
		with patch("frappe.permissions.get_user_permissions", return_value={"Cost Center": []}):
			self.assertFalse(scoped_visibility("auditor@example.com"))
		with patch(
			"frappe.permissions.get_user_permissions", side_effect=RuntimeError("profile unavailable")
		):
			self.assertTrue(scoped_visibility("auditor@example.com"))

	def test_writeback_uses_the_server_visibility_snapshot_when_profile_is_missing(self):
		run = frappe._dict(
			agent=AGENT,
			permission_profile=None,
			scope_json='{"scoped_visibility":true}',
		)
		self.assertTrue(agent_runs._scoped_visibility(run, frappe._dict(scoped_visibility=0)))

	def test_prompt_preserves_the_server_owned_visibility_flag_in_scope(self):
		scope = {
			"company": "Test Co",
			"fiscal_year": "2026-2027",
			"from_date": "2026-04-01",
			"to_date": "2027-03-31",
			"report_date": "2026-10-31",
			"prior_fy_start": "2025-04-01",
			"prior_fy_end": "2026-03-31",
			"scoped_visibility": True,
		}
		with patch("jarvis.chat.agent_catalog.registry_tools_allow", return_value=[]):
			message = agent_scheduler._audit_prompt({"agent_slug": AGENT}, MagicMock(), "manual", scope, {})
		self.assertIn('scoped_visibility="1"', message)
		self.assertIn("preserve this value in scope.json", message)

		scope["scoped_visibility"] = False
		with patch("jarvis.chat.agent_catalog.registry_tools_allow", return_value=[]):
			message = agent_scheduler._audit_prompt({"agent_slug": AGENT}, MagicMock(), "manual", scope, {})
		self.assertIn('scoped_visibility="0"', message)

	def test_declared_controls_reach_the_immutable_launch_snapshot(self):
		listing = {
			"config_keys": '["materiality", "suspense_accounts", "subledger_owned_accounts", '
			'"subledger_account_owner", "chart_standard", "suspense_confirmed"]'
		}
		inst = frappe._dict(
			config=(
				'{"materiality":10000,"suspense_accounts":["1910 - Suspense"],'
				'"subledger_owned_accounts":[],"subledger_account_owner":{},'
				'"chart_standard":true,"suspense_confirmed":true,"not_declared":"ignore"}'
			)
		)
		snapshot = agent_scheduler._explicit_config(listing, inst)
		self.assertEqual(
			snapshot,
			{
				"materiality": 10000,
				"suspense_accounts": ["1910 - Suspense"],
				"subledger_owned_accounts": [],
				"subledger_account_owner": {},
				"chart_standard": True,
				"suspense_confirmed": True,
			},
		)
		with patch("jarvis.chat.agent_catalog.registry_tools_allow", return_value=[]):
			message = agent_scheduler._audit_prompt(
				{"agent_slug": AGENT}, MagicMock(), "manual", {}, snapshot
			)
		self.assertIn('"suspense_accounts": ["1910 - Suspense"]', message)
		self.assertNotIn("not_declared", message)

	def test_report_date_caps_the_generic_watermark_window(self):
		self.assertEqual(
			as_of_date({"to_date": "2027-03-31", "report_date": "2026-10-31"}),
			"2026-10-31",
		)
		self.assertEqual(
			as_of_date({"to_date": "2026-04-30", "report_date": "2026-10-31"}),
			"2026-04-30",
		)
		self.assertEqual(as_of_date({"to_date": "2026-04-30"}), "2026-04-30")
		self.assertIsNone(as_of_date({"to_date": "bad", "report_date": "2026-10-31"}))

	def test_writeback_scope_must_match_the_server_launch_snapshot(self):
		scope = self._scope()
		self.assertEqual(bind_writeback_scope(scope, dict(scope)), scope)
		for changed in (
			dict(scope, company="Other Co"),
			dict(scope, report_date="2026-11-01"),
			dict(scope, scoped_visibility=True),
			{k: v for k, v in scope.items() if k != "prior_fy_end"},
		):
			with self.subTest(changed=changed), self.assertRaises(InvalidArgumentError):
				bind_writeback_scope(scope, changed)

	def test_writeback_persister_enforces_the_server_launch_snapshot(self):
		scope = self._scope()
		run = MagicMock()
		run.get.side_effect = lambda key: (
			'{"company":"Test Co","fiscal_year":"2026-2027","from_date":"2026-04-01",'
			'"to_date":"2027-03-31","report_date":"2026-10-31",'
			'"prior_fy_start":"2025-04-01","prior_fy_end":"2026-03-31",'
			'"scoped_visibility":false}'
			if key == "scope_json"
			else None
		)
		inst = MagicMock(agent=AGENT, owner="auditor@example.com")
		inst.get.return_value = "live"
		with (
			patch("jarvis.chat.agent_runs._listing_token_sets", return_value=(set(), set())),
			patch("jarvis.chat.agent_runs.frappe.db.set_value") as set_value,
			patch("jarvis.chat.agent_runs.frappe.get_doc") as create_doc,
			self.assertRaises(InvalidArgumentError),
		):
			agent_runs.record_delegate_run(
				run,
				inst,
				[{"token": "would-write", "ref_doctype": "Company", "ref_name": "Test Co"}],
				scope=dict(scope, company="Other Co"),
				dashboard="would-write-dashboard",
			)
		set_value.assert_not_called()
		create_doc.assert_not_called()

	def test_native_reports_cannot_leave_the_server_launch_scope(self):
		scope = self._scope()
		validate_ledger_report_scope(
			scope,
			"General Ledger",
			{
				"company": "Test Co",
				"from_date": "2026-04-01",
				"to_date": "2026-10-31",
				"show_opening_entries": 1,
			},
		)
		validate_ledger_report_scope(
			scope,
			"General Ledger",
			{
				"company": "Test Co",
				"from_date": "2025-04-01",
				"to_date": "2026-03-31",
				"show_opening_entries": 1,
			},
		)
		validate_ledger_report_scope(
			scope,
			"Trial Balance",
			{
				"company": "Test Co",
				"fiscal_year": "2026-2027",
				"from_date": "2026-04-01",
				"to_date": "2026-10-31",
				"with_period_closing_entry_for_current_period": 1,
			},
		)
		for report, filters in (
			(
				"General Ledger",
				{
					"company": "Other Co",
					"from_date": "2026-04-01",
					"to_date": "2026-10-31",
					"show_opening_entries": 1,
				},
			),
			(
				"General Ledger",
				{
					"company": "Test Co",
					"from_date": "2026-04-01",
					"to_date": "2027-03-31",
					"show_opening_entries": 1,
				},
			),
			(
				"General Ledger",
				{
					"company": "Test Co",
					"from_date": "2026-04-01",
					"to_date": "2026-10-31",
					"show_opening_entries": 1,
					"account": "1100 - Cash",
				},
			),
			(
				"Trial Balance",
				{
					"company": "Test Co",
					"fiscal_year": "2025-2026",
					"from_date": "2026-04-01",
					"to_date": "2026-10-31",
					"with_period_closing_entry_for_current_period": 1,
				},
			),
			(
				"Trial Balance",
				{
					"company": "Test Co",
					"fiscal_year": "2026-2027",
					"from_date": "2026-04-01",
					"to_date": "2026-10-31",
					"with_period_closing_entry_for_current_period": 1,
					"cost_center": "North",
				},
			),
		):
			with self.subTest(report=report, filters=filters), self.assertRaises(PermissionDeniedError):
				validate_ledger_report_scope(scope, report, filters)
