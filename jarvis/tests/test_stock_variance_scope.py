"""Regression checks for the Stock Variance Warehouse-scope bridge."""

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_scheduler, agent_scope, agents_api, stock_variance_scope
from jarvis.jarvis.doctype.jarvis_agent_installation.jarvis_agent_installation import (
	JarvisAgentInstallation,
)


class _ReadableDocument:
	def __init__(self, **values):
		self.__dict__.update(values)

	def get(self, key):
		return getattr(self, key, None)

	def check_permission(self, ptype):
		self.last_permission = ptype

	def apply_fieldlevel_read_permissions(self):
		return None


class TestStockVarianceScope(FrappeTestCase):
	company = "Scope Example Ltd"
	root = "Scope Root - SEL"
	reviewer = "scope-reviewer@example.invalid"

	def config(self, **overrides):
		base = {
			"company": self.company,
			"record_scope": {"doctype": "Warehouse", "name": self.root},
			"stock_variance": {"reviewer": self.reviewer},
		}
		base.update(overrides)
		return base

	def readable_docs(self, *, is_group=1, company=None, company_is_group=0, disabled=0):
		company_doc = _ReadableDocument(name=self.company, is_group=company_is_group)
		warehouse = _ReadableDocument(
			name=self.root,
			company=company or self.company,
			is_group=is_group,
			disabled=disabled,
			lft=10,
			rgt=13 if is_group else 11,
		)
		return company_doc, warehouse

	def test_group_and_leaf_roots_are_valid_when_they_contain_a_readable_leaf(self):
		for is_group in (0, 1):
			with self.subTest(is_group=is_group):
				company_doc, warehouse = self.readable_docs(is_group=is_group)
				with (
					patch.object(frappe, "get_doc", side_effect=[company_doc, warehouse]),
					patch.object(frappe.db, "get_value", return_value=1),
					patch.object(frappe, "get_list", return_value=["Scope Leaf - SEL"]),
				):
					self.assertEqual(stock_variance_scope.configuration_issues(self.config()), {})

	def test_group_company_is_refused_before_the_native_evaluator_runs(self):
		company_doc, warehouse = self.readable_docs(company_is_group=1)
		with (
			patch.object(frappe, "get_doc", side_effect=[company_doc, warehouse]),
			patch.object(frappe.db, "get_value", return_value=1),
			patch.object(frappe, "get_list", return_value=["Scope Leaf - SEL"]),
		):
			issues = stock_variance_scope.configuration_issues(self.config())

		self.assertIn("company", issues)
		self.assertIn("record_scope", issues)

	def test_system_reviewer_is_refused(self):
		company_doc, warehouse = self.readable_docs()
		with (
			patch.object(frappe, "get_doc", side_effect=[company_doc, warehouse]),
			patch.object(frappe.db, "get_value", return_value=1),
			patch.object(frappe, "get_list", return_value=["Scope Leaf - SEL"]),
			patch.object(stock_variance_scope, "is_valid_unattended_owner", return_value=False),
		):
			issues = stock_variance_scope.configuration_issues(self.config())

		self.assertIn("stock_variance.reviewer", issues)

	def test_missing_or_wrong_scope_never_counts_as_a_valid_draft_value(self):
		cases = (
			({}, "record_scope"),
			({"record_scope": {"doctype": "Company", "name": self.company}}, "record_scope"),
			({"record_scope": {"doctype": "Warehouse", "name": " other "}}, "record_scope"),
			(
				{
					"record_scope": {
						"doctype": "Warehouse",
						"name": self.root,
						"company": self.company,
					}
				},
				"record_scope",
			),
			({"stock_variance": {"reviewer": ""}}, "stock_variance.reviewer"),
		)
		for override, key in cases:
			with self.subTest(override=override):
				config = self.config(**override)
				if not override:
					config.pop("record_scope")
				issues = stock_variance_scope.configuration_issues(config)
				self.assertIn(key, issues)

	def test_mismatched_disabled_or_empty_warehouse_tree_is_refused(self):
		cases = (
			("Other Ltd", ["Scope Leaf - SEL"], 0),
			(self.company, [], 0),
			(self.company, ["Scope Leaf - SEL"], 1),
		)
		for company, leaves, disabled in cases:
			with self.subTest(company=company, leaves=leaves, disabled=disabled):
				company_doc, warehouse = self.readable_docs(company=company, disabled=disabled)
				with (
					patch.object(frappe, "get_doc", side_effect=[company_doc, warehouse]),
					patch.object(frappe.db, "get_value", return_value=1),
					patch.object(frappe, "get_list", return_value=leaves),
				):
					self.assertIn("record_scope", stock_variance_scope.configuration_issues(self.config()))

	def test_declared_config_excludes_company_and_record_scope(self):
		config = self.config()
		listing = frappe._dict(config_keys='["stock_variance"]')
		installation = frappe._dict(config=json.dumps(config))

		self.assertEqual(
			agent_scheduler._explicit_config(listing, installation),
			{"stock_variance": {"reviewer": self.reviewer}},
		)

	def test_complete_configuration_guard_refuses_missing_warehouse_scope(self):
		config = self.config()
		config.pop("record_scope")
		with self.assertRaises(frappe.ValidationError):
			stock_variance_scope.require_configuration(config)

	def test_scope_json_and_prompt_keep_the_exact_validated_warehouse_root(self):
		config = self.config()
		validated_root = {"doctype": "Warehouse", "name": self.root}
		company_doc, warehouse = self.readable_docs()
		with (
			patch.object(frappe, "get_doc", side_effect=[company_doc, warehouse]),
			patch.object(frappe.db, "get_value", return_value=1),
			patch.object(frappe, "get_list", return_value=["Scope Leaf - SEL"]),
		):
			self.assertEqual(stock_variance_scope.require_configuration(config), config)

		base_scope = {
			"company": self.company,
			"fiscal_year": "2026-2027",
			"from_date": "2026-04-01",
			"to_date": "2027-03-31",
			"report_date": "2026-11-01",
			"prior_fy_start": "2025-04-01",
			"prior_fy_end": "2026-03-31",
			"execution_user": "warehouse-reader@example.invalid",
		}
		with (
			patch.object(agent_scope, "_resolve", return_value=base_scope),
			patch.object(frappe, "get_doc", return_value=warehouse),
		):
			resolved_scope = agent_scope.resolve_scope(frappe._dict(config=json.dumps(config)))
		self.assertEqual(resolved_scope["record_scope"], validated_root)

		installation = frappe._dict(
			name="SV-INSTALL-1", agent=stock_variance_scope.AGENT, config=json.dumps(config)
		)
		with (
			patch.object(agent_scope, "resolve_scope", return_value=resolved_scope),
			patch.object(frappe.db, "sql", return_value=[frappe._dict(n=0, m=None)]),
			patch.object(agent_scheduler, "_permission_profile", return_value="scope-profile"),
			patch.object(frappe.db, "set_value") as stamp,
		):
			stamped_scope = agent_scheduler._stamp_scope_and_watermark(
				"SV-RUN-1", installation, "warehouse-reader@example.invalid"
			)
		self.assertEqual(stamped_scope["record_scope"], validated_root)
		stamped_values = stamp.call_args.args[2]
		self.assertEqual(json.loads(stamped_values["scope_json"])["record_scope"], validated_root)

		listing = frappe._dict(agent_slug=stock_variance_scope.AGENT, config_keys='["stock_variance"]')
		with patch.object(
			agent_scheduler, "_explicit_config", return_value={"stock_variance": config["stock_variance"]}
		):
			prompt = agent_scheduler._audit_prompt(listing, installation, "manual", stamped_scope)
		marker = "ADDITIONAL SCOPE (copy these values unchanged): "
		scope_block = prompt.split(marker, 1)[1].split("\n\nEXPLICIT CONFIG", 1)[0]
		self.assertEqual(json.loads(scope_block), {"record_scope": validated_root})

	def test_launch_refuses_an_invalid_scope_before_creating_any_run_rows(self):
		installation = frappe._dict(
			agent=stock_variance_scope.AGENT,
			owner="scope-owner@example.invalid",
			run_as_user="warehouse-reader@example.invalid",
			reviewer="scope-reviewer@example.invalid",
			config="{}",
		)
		listing = frappe._dict(agent_slug=stock_variance_scope.AGENT)
		with (
			patch.object(frappe, "get_doc", return_value=listing),
			patch.object(
				stock_variance_scope,
				"require_configuration",
				side_effect=frappe.ValidationError("Warehouse scope is required."),
			) as require,
			patch.object(frappe.db, "savepoint") as savepoint,
		):
			with self.assertRaises(frappe.ValidationError):
				agent_scheduler._launch_audit(installation, "manual")

		require.assert_called_once_with("{}")
		savepoint.assert_not_called()

	def test_enable_checks_the_scope_as_the_configured_run_as_user(self):
		seen = []

		@contextmanager
		def as_run_as(user):
			seen.append(("enter", user))
			yield
			seen.append(("exit", user))

		installation = frappe._dict(
			agent=stock_variance_scope.AGENT,
			run_as_user="warehouse-reader@example.invalid",
			enabled=1,
			config=json.dumps(self.config()),
		)
		with (
			patch("jarvis._session.impersonate", as_run_as),
			patch.object(stock_variance_scope, "require_configuration") as require,
			patch.object(stock_variance_scope, "validate_saved_configuration") as validate_draft,
		):
			JarvisAgentInstallation._guard_stock_variance_scope(installation)

		require.assert_called_once_with(installation.config)
		validate_draft.assert_not_called()
		self.assertEqual(
			seen,
			[
				("enter", "warehouse-reader@example.invalid"),
				("exit", "warehouse-reader@example.invalid"),
			],
		)

	def test_run_as_remap_checks_an_existing_draft_scope_as_the_new_user(self):
		active_user = {"value": None}

		@contextmanager
		def as_run_as(user):
			active_user["value"] = user
			try:
				yield
			finally:
				active_user["value"] = None

		installation = frappe._dict(
			agent=stock_variance_scope.AGENT,
			run_as_user="new-warehouse-reader@example.invalid",
			enabled=0,
			config=json.dumps(self.config()),
		)
		installation.is_new = lambda: False
		installation.has_value_changed = lambda field: field == "run_as_user"

		def check_draft(config):
			self.assertEqual(active_user["value"], installation.run_as_user)
			self.assertEqual(config, installation.config)

		with (
			patch("jarvis._session.impersonate", as_run_as),
			patch.object(stock_variance_scope, "require_configuration") as require,
			patch.object(
				stock_variance_scope, "validate_saved_configuration", side_effect=check_draft
			) as validate_draft,
		):
			JarvisAgentInstallation._guard_stock_variance_scope(installation)

		require.assert_not_called()
		validate_draft.assert_called_once_with(installation.config)

	def test_unchanged_disabled_stale_scope_can_be_saved_for_recovery(self):
		installation = frappe._dict(
			agent=stock_variance_scope.AGENT,
			run_as_user="warehouse-reader@example.invalid",
			enabled=0,
			config=json.dumps(self.config()),
		)
		installation.is_new = lambda: False
		installation.has_value_changed = lambda field: False

		with (
			patch("jarvis._session.impersonate") as as_run_as,
			patch.object(stock_variance_scope, "validate_saved_configuration") as validate_draft,
		):
			JarvisAgentInstallation._guard_stock_variance_scope(installation)

		as_run_as.assert_not_called()
		validate_draft.assert_not_called()

	def test_disabled_config_change_still_checks_the_scope_as_the_run_as_user(self):
		installation = frappe._dict(
			agent=stock_variance_scope.AGENT,
			run_as_user="warehouse-reader@example.invalid",
			enabled=0,
			config=json.dumps(self.config()),
		)
		installation.is_new = lambda: False
		installation.has_value_changed = lambda field: field == "config"

		with patch.object(stock_variance_scope, "validate_saved_configuration") as validate_draft:
			JarvisAgentInstallation._guard_stock_variance_scope(installation)

		validate_draft.assert_called_once_with(installation.config)

	def test_scheduled_invalid_scope_is_skipped_without_an_enqueue_retry(self):
		row = frappe._dict(
			name="SV-INSTALL-1",
			owner="scope-owner@example.invalid",
			run_as_user="warehouse-reader@example.invalid",
			agent=stock_variance_scope.AGENT,
			schedule_frequency="daily",
			schedule_time="09:00:00",
			schedule_weekday=None,
			schedule_day_of_month=None,
			installable=1,
			config="{}",
			activation_state="shadow",
		)
		listing = frappe._dict(nature="Auditor", status="Published", operator_visibility="available")
		now = frappe.utils.now_datetime()

		with (
			patch.object(frappe.db, "get_value", return_value=listing),
			patch("jarvis.chat.agents_api._user_allowed_for_agent", return_value=True),
			patch.object(agent_scheduler, "_valid_owner", return_value=True),
			patch.object(agent_scheduler, "run_as_mapping_refusal", return_value=None),
			patch.object(agent_scheduler, "_over_run_budget", return_value=(False, "")),
			patch.object(agent_scheduler, "_is_app_learning", return_value=False),
			patch("jarvis.chat.agent_models.gate_run", return_value={"ok": True}),
			patch.object(
				stock_variance_scope,
				"configuration_issues",
				return_value={"company": "Select a readable company."},
			) as configuration_issues,
			patch.object(agent_scheduler, "_record_failed") as record_failed,
			patch.object(agent_scheduler, "_advance") as advance,
			patch.object(agent_scheduler, "_dispatch") as dispatch,
			patch.object(agent_scheduler, "_notify_owner") as notify_owner,
		):
			agent_scheduler._sweep_one(row, now, set())

		configuration_issues.assert_called_once_with("{}")
		record_failed.assert_called_once_with(
			row,
			"scheduled run skipped: complete the Stock Variance company, Warehouse scope and reviewer in Configure",
		)
		advance.assert_called_once_with(row, now)
		dispatch.assert_not_called()
		notify_owner.assert_not_called()

	def test_detail_issues_use_the_run_as_user_without_widening_the_response_shape(self):
		active_user = {"value": None}
		run_as_user = "detail-warehouse-reader@example.invalid"

		@contextmanager
		def as_run_as(user):
			active_user["value"] = user
			try:
				yield
			finally:
				active_user["value"] = None

		def calculate(config):
			self.assertEqual(active_user["value"], run_as_user)
			self.assertEqual(config, json.dumps(self.config()))
			return {"record_scope": "Warehouse is not readable by the run-as user."}

		with (
			patch.object(frappe.db, "get_value", return_value=run_as_user) as get_value,
			patch("jarvis._session.impersonate", as_run_as),
			patch.object(stock_variance_scope, "configuration_issues", side_effect=calculate),
		):
			issues = agents_api._stock_variance_issues_for_run_as("SV-INSTALL-1", json.dumps(self.config()))

		get_value.assert_called_once_with("Jarvis Agent Installation", "SV-INSTALL-1", "run_as_user")
		self.assertEqual(issues, {"record_scope": "Warehouse is not readable by the run-as user."})

	def test_detail_issues_refuse_a_blank_run_as_user_without_impersonating_the_viewer(self):
		with (
			patch.object(frappe.db, "get_value", return_value="") as get_value,
			patch("jarvis._session.impersonate") as as_run_as,
		):
			issues = agents_api._stock_variance_issues_for_run_as("SV-INSTALL-1", json.dumps(self.config()))

		get_value.assert_called_once_with("Jarvis Agent Installation", "SV-INSTALL-1", "run_as_user")
		as_run_as.assert_not_called()
		self.assertEqual(
			issues,
			{"run_as_user": "Set a run-as user before configuring the Warehouse scope."},
		)
