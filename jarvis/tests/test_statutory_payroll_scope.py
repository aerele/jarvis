"""Regression tests for the server-bound Statutory Payroll batch scope."""

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_scheduler, agent_scope, agents_api, derived_config, statutory_payroll_scope
from jarvis.jarvis.doctype.jarvis_agent_installation.jarvis_agent_installation import (
	JarvisAgentInstallation,
)


class _ReadableDocument:
	def __init__(self, *, hidden=(), **values):
		self._hidden = set(hidden)
		self.__dict__.update(values)

	def get(self, key):
		return getattr(self, key, None)

	def check_permission(self, ptype):
		self.last_permission = ptype

	def apply_fieldlevel_read_permissions(self):
		for key in self._hidden:
			setattr(self, key, None)


class TestStatutoryPayrollScope(FrappeTestCase):
	entry_name = "PAY-FAKE-2026-10"
	company = "Fake Payroll Company"
	run_as_user = "payroll-reader@example.invalid"

	def setUp(self):
		super().setUp()
		company_scope = patch("jarvis.tools._company_scope.assert_company_permitted")
		company_scope.start()
		self.addCleanup(company_scope.stop)

	def config(self, **overrides):
		value = {"payroll_entry": self.entry_name}
		value.update(overrides)
		return value

	def docs(
		self,
		*,
		company=None,
		start_date="2026-10-01",
		end_date="2026-10-31",
		docstatus=1,
		hidden=(),
		company_hidden=(),
		company_is_group=0,
	):
		entry = _ReadableDocument(
			name=self.entry_name,
			company=company or self.company,
			start_date=start_date,
			end_date=end_date,
			docstatus=docstatus,
			hidden=hidden,
		)
		company_doc = _ReadableDocument(
			name=company or self.company, is_group=company_is_group, hidden=company_hidden
		)
		return entry, company_doc

	def test_scope_comes_only_from_the_readable_payroll_entry(self):
		entry, company = self.docs()
		# These values imitate a forged old generic-scope configuration. The
		# Payroll Entry remains the sole source of scope values.
		config = self.config(
			company="Forged Company",
			from_date="2024-01-01",
			to_date="2024-12-31",
			record_scope={"doctype": "Payroll Entry", "name": "OTHER"},
		)
		with (
			patch.object(frappe, "get_doc", side_effect=[entry, company]),
			patch.object(frappe.utils, "today", return_value="2026-11-01"),
		):
			scope = statutory_payroll_scope.resolve_scope(json.dumps(config))

		self.assertEqual(
			scope,
			{
				"company": self.company,
				"fiscal_year": None,
				"from_date": "2026-10-01",
				"to_date": "2026-10-31",
				"prior_period_from": "2026-09-01",
				"prior_period_to": "2026-09-30",
				"report_date": "2026-11-01",
				"record_scope": {"doctype": "Payroll Entry", "name": self.entry_name},
			},
		)

	def test_missing_malformed_unreadable_cancelled_future_or_nonmonthly_entry_is_refused(self):
		cases = (
			({}, "payroll_entry", None),
			({"payroll_entry": " other "}, "payroll_entry", None),
			([], "config", None),
			(self.config(), "payroll_entry", {"hidden": ("company",)}),
			(self.config(), "payroll_entry", {"docstatus": 0}),
			(self.config(), "payroll_entry", {"start_date": None}),
			(self.config(), "payroll_entry", {"end_date": ""}),
			(self.config(), "payroll_entry", {"end_date": "0000-00-00"}),
			(self.config(), "payroll_entry", {"end_date": False}),
			(self.config(), "payroll_entry", {"end_date": "2026-11-30"}),
			(self.config(), "payroll_entry", {"start_date": "2026-10-02"}),
		)
		for raw, field, values in cases:
			with self.subTest(raw=raw, values=values):
				if values is None:
					issues = statutory_payroll_scope.configuration_issues(raw)
				else:
					entry, company = self.docs(**values)
					with (
						patch.object(frappe, "get_doc", side_effect=[entry, company]),
						patch.object(frappe.utils, "today", return_value="2026-11-01"),
					):
						issues = statutory_payroll_scope.configuration_issues(raw)
				self.assertIn(field, issues)

	def test_foreign_payroll_entry_or_company_scope_is_refused(self):
		from jarvis.exceptions import PermissionDeniedError

		with patch.object(frappe, "get_doc", side_effect=frappe.PermissionError):
			issues = statutory_payroll_scope.configuration_issues(self.config())
		self.assertIn("payroll_entry", issues)

		entry, company = self.docs()
		with (
			patch.object(frappe, "get_doc", side_effect=[entry, company]),
			patch(
				"jarvis.tools._company_scope.assert_company_permitted",
				side_effect=PermissionDeniedError("foreign Company"),
			),
			patch.object(frappe.utils, "today", return_value="2026-11-01"),
		):
			issues = statutory_payroll_scope.configuration_issues(self.config())
		self.assertIn("payroll_entry", issues)

	def test_unreadable_company_or_group_company_is_refused(self):
		for company_hidden, company_is_group in (((), 1), (("is_group",), 0)):
			with self.subTest(company_hidden=company_hidden, company_is_group=company_is_group):
				entry, company = self.docs(company_hidden=company_hidden, company_is_group=company_is_group)
				with (
					patch.object(frappe, "get_doc", side_effect=[entry, company]),
					patch.object(frappe.utils, "today", return_value="2026-11-01"),
				):
					issues = statutory_payroll_scope.configuration_issues(self.config())
				self.assertIn("payroll_entry", issues)

	def test_agent_scope_uses_the_statutory_resolver_before_generic_config(self):
		installation = frappe._dict(
			agent=statutory_payroll_scope.AGENT,
			config=json.dumps(self.config(company="Forged Company")),
		)
		expected = {"record_scope": {"doctype": "Payroll Entry", "name": self.entry_name}}
		with (
			patch.object(statutory_payroll_scope, "resolve_scope", return_value=expected) as resolve,
			patch.object(agent_scope, "_resolve") as generic,
		):
			self.assertEqual(agent_scope.resolve_scope(installation), expected)
		resolve.assert_called_once_with(installation.config)
		generic.assert_not_called()

	def test_enabled_save_checks_the_entry_as_run_as_user(self):
		seen = []

		@contextmanager
		def as_run_as(user):
			seen.append(("enter", user))
			yield
			seen.append(("exit", user))

		installation = frappe._dict(
			agent=statutory_payroll_scope.AGENT,
			run_as_user=self.run_as_user,
			enabled=1,
			config=json.dumps(self.config()),
		)
		installation.is_new = lambda: False
		installation.has_value_changed = lambda field: False
		with (
			patch("jarvis._session.impersonate", as_run_as),
			patch.object(statutory_payroll_scope, "require_configuration") as require,
			patch.object(statutory_payroll_scope, "validate_saved_configuration") as draft,
		):
			JarvisAgentInstallation._guard_statutory_payroll_scope(installation)

		require.assert_called_once_with(installation.config)
		draft.assert_not_called()
		self.assertEqual(seen, [("enter", self.run_as_user), ("exit", self.run_as_user)])

	def test_launch_refuses_before_creating_rows_when_scope_is_invalid(self):
		active_user = {"value": None}

		@contextmanager
		def as_run_as(user):
			active_user["value"] = user
			try:
				yield
			finally:
				active_user["value"] = None

		def refuse_as_run_as(config):
			self.assertEqual(active_user["value"], self.run_as_user)
			raise frappe.ValidationError("Payroll Entry is required.")

		installation = frappe._dict(
			name="STAT-INSTALL-1",
			agent=statutory_payroll_scope.AGENT,
			owner="owner@example.invalid",
			run_as_user=self.run_as_user,
			reviewer="reviewer@example.invalid",
			config="{}",
		)
		listing = frappe._dict(agent_slug=statutory_payroll_scope.AGENT, config_keys=[])
		with (
			patch.object(frappe, "get_doc", return_value=listing),
			patch("jarvis.chat.operator_review.backend", return_value=None),
			patch.object(
				statutory_payroll_scope,
				"require_configuration",
				side_effect=refuse_as_run_as,
			) as require,
			patch("jarvis._session.impersonate", as_run_as),
			patch.object(frappe.db, "savepoint") as savepoint,
		):
			with self.assertRaises(frappe.ValidationError):
				agent_scheduler._launch_audit(installation, "manual")

		require.assert_called_once_with("{}")
		savepoint.assert_not_called()

	def test_scope_stamp_resolves_statutory_scope_as_run_as_user(self):
		active_user = {"value": None}

		@contextmanager
		def as_run_as(user):
			active_user["value"] = user
			try:
				yield
			finally:
				active_user["value"] = None

		expected = {
			"company": self.company,
			"fiscal_year": None,
			"from_date": "2026-10-01",
			"to_date": "2026-10-31",
			"prior_period_from": "2026-09-01",
			"prior_period_to": "2026-09-30",
			"report_date": "2026-11-01",
			"record_scope": {"doctype": "Payroll Entry", "name": self.entry_name},
		}
		installation = frappe._dict(agent=statutory_payroll_scope.AGENT, config=json.dumps(self.config()))

		def resolve(_installation):
			self.assertEqual(active_user["value"], self.run_as_user)
			return expected

		with (
			patch("jarvis._session.impersonate", as_run_as),
			patch("jarvis.chat.operator_review.backend", return_value=None),
			patch.object(agent_scope, "resolve_scope", side_effect=resolve),
			patch.object(agent_scheduler, "_permission_profile", return_value="{}"),
			patch.object(frappe.db, "sql", return_value=[frappe._dict(n=0, m=None)]),
			patch.object(frappe.db, "set_value") as set_value,
		):
			self.assertEqual(
				agent_scheduler._stamp_scope_and_watermark("RUN-1", installation, self.run_as_user),
				expected,
			)

		set_value.assert_called_once()

	def test_scope_stamp_refuses_a_permission_change_without_generic_fallback(self):
		@contextmanager
		def as_run_as(_user):
			yield

		installation = frappe._dict(agent=statutory_payroll_scope.AGENT, config=json.dumps(self.config()))
		with (
			patch("jarvis._session.impersonate", as_run_as),
			patch("jarvis.chat.operator_review.backend", return_value=None),
			patch.object(agent_scope, "resolve_scope", side_effect=frappe.PermissionError),
			patch.object(frappe, "log_error") as log_error,
			patch.object(frappe.db, "set_value") as set_value,
		):
			with self.assertRaises(frappe.PermissionError):
				agent_scheduler._stamp_scope_and_watermark("RUN-1", installation, self.run_as_user)

		log_error.assert_called_once()
		set_value.assert_not_called()

	def test_writeback_scope_must_match_the_server_scope(self):
		from jarvis.exceptions import InvalidArgumentError
		from jarvis.tools import record_agent_run as writeback

		server_scope = {
			"company": self.company,
			"fiscal_year": None,
			"from_date": "2026-10-01",
			"to_date": "2026-10-31",
			"prior_period_from": "2026-09-01",
			"prior_period_to": "2026-09-30",
			"report_date": "2026-11-01",
			"record_scope": {"doctype": "Payroll Entry", "name": self.entry_name},
		}
		returned = {**server_scope, "payroll_entry": "FORGED"}
		self.assertEqual(
			statutory_payroll_scope.canonical_writeback_scope(server_scope, returned), server_scope
		)
		run = frappe._dict(agent=statutory_payroll_scope.AGENT, scope_json=json.dumps(server_scope))
		self.assertEqual(writeback._canonical_scope_for_writeback(run, returned), server_scope)
		for key, value in (
			("company", "Foreign Company"),
			("from_date", "2026-09-01"),
			("to_date", "2026-09-30"),
			("prior_period_from", "2026-08-01"),
			("record_scope", {"doctype": "Payroll Entry", "name": "FORGED"}),
		):
			with self.subTest(key=key):
				changed = {**server_scope, key: value}
				with self.assertRaises(statutory_payroll_scope.ConfigurationError):
					statutory_payroll_scope.canonical_writeback_scope(server_scope, changed)
				with self.assertRaises(InvalidArgumentError):
					writeback._canonical_scope_for_writeback(run, changed)

	def test_snapshot_and_prompt_exclude_raw_scope_but_include_server_scope(self):
		statutory = {"reviewer": "reviewer@example.invalid", "mapping_confirmed": True}
		listing = frappe._dict(
			agent_slug=statutory_payroll_scope.AGENT,
			config_keys='["payroll_entry", "statutory", "statutory_schema"]',
		)
		installation = frappe._dict(
			agent=statutory_payroll_scope.AGENT,
			config=json.dumps(
				self.config(
					statutory=statutory,
					company="Forged Company",
					from_date="2020-01-01",
					record_scope={"doctype": "Payroll Entry", "name": "FORGED"},
				)
			),
		)
		derived = {"statutory_schema": {"india_fields_present": {"pan_number": False}}}
		with patch.object(derived_config, "derive", return_value=derived) as derive:
			snapshot = agent_scheduler._explicit_config(listing, installation)

		self.assertEqual(snapshot, {"statutory": statutory, **derived})
		self.assertEqual(derive.call_args.args[0], {"statutory", "statutory_schema"})
		server_scope = {
			"company": self.company,
			"fiscal_year": None,
			"from_date": "2026-10-01",
			"to_date": "2026-10-31",
			"report_date": "2026-11-01",
			"prior_fy_start": None,
			"prior_fy_end": None,
			"prior_period_from": "2026-09-01",
			"prior_period_to": "2026-09-30",
			"record_scope": {"doctype": "Payroll Entry", "name": self.entry_name},
		}
		with patch(
			"jarvis.chat.agent_catalog.registry_tools_allow",
			return_value=["jarvis__get_engagement_config"],
		):
			prompt = agent_scheduler._audit_prompt(
				listing, installation, "manual", server_scope, engagement_config=snapshot
			)
		self.assertIn('"record_scope": {"doctype": "Payroll Entry", "name": "PAY-FAKE-2026-10"}', prompt)
		self.assertIn('"prior_period_from": "2026-09-01"', prompt)
		self.assertIn('"fiscal_year": null', prompt)
		self.assertNotIn('fiscal_year="None"', prompt)
		self.assertNotIn("Forged Company", prompt)
		self.assertNotIn("FORGED", prompt)
		self.assertNotIn('"payroll_entry"', prompt)
		for value, expected in (
			(True, "true"),
			(False, "false"),
			("false", None),
			("true", None),
			(0, None),
			(1, None),
			([], None),
			({}, None),
			(None, None),
		):
			with self.subTest(scoped_visibility=value):
				scope = {**server_scope, "scoped_visibility": value}
				with patch(
					"jarvis.chat.agent_catalog.registry_tools_allow",
					return_value=["jarvis__get_engagement_config"],
				):
					prompt = agent_scheduler._audit_prompt(
						listing, installation, "manual", scope, engagement_config=snapshot
					)
				if expected is None:
					self.assertNotIn('"scoped_visibility"', prompt)
				else:
					self.assertIn(f'"scoped_visibility": {expected}', prompt)

	def test_vendored_statutory_catalog_uses_only_native_read_tools(self):
		path = frappe.get_app_path("jarvis", "agents", "registry.json")
		with open(path, encoding="utf-8") as registry_file:
			entry = next(
				row
				for row in json.load(registry_file)["agents"]
				if row["agent_slug"] == statutory_payroll_scope.AGENT
			)
		self.assertEqual(entry["version"], "0.4.3")
		self.assertEqual(entry["tools_required"], ["jarvis__query", "jarvis__get_doc"])
		self.assertNotIn("jarvis__get_list", entry["tools_allow"])
		self.assertNotIn("jarvis__run_report", entry["tools_allow"])

	def test_scheduled_invalid_scope_is_skipped_before_dispatch(self):
		row = frappe._dict(
			name="STAT-INSTALL-1",
			owner="owner@example.invalid",
			run_as_user=self.run_as_user,
			agent=statutory_payroll_scope.AGENT,
			schedule_frequency="monthly",
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
				statutory_payroll_scope,
				"configuration_issues",
				return_value={"payroll_entry": "Select a readable submitted monthly Payroll Entry."},
			) as issues,
			patch.object(agent_scheduler, "_record_failed") as failed,
			patch.object(agent_scheduler, "_advance") as advance,
			patch.object(agent_scheduler, "_dispatch") as dispatch,
		):
			agent_scheduler._sweep_one(row, now, set())

		issues.assert_called_once_with("{}")
		failed.assert_called_once_with(
			row,
			"scheduled run skipped: select a readable submitted monthly Payroll Entry in Configure",
		)
		advance.assert_called_once_with(row, now)
		dispatch.assert_not_called()

	def test_detail_issues_run_as_the_execution_user(self):
		active_user = {"value": None}

		@contextmanager
		def as_run_as(user):
			active_user["value"] = user
			try:
				yield
			finally:
				active_user["value"] = None

		def check(config):
			self.assertEqual(active_user["value"], self.run_as_user)
			self.assertEqual(config, "{}")
			return {"payroll_entry": "Select a readable submitted monthly Payroll Entry."}

		with (
			patch.object(frappe.db, "get_value", return_value=self.run_as_user),
			patch("jarvis.chat.agents_api.impersonate", as_run_as),
			patch.object(statutory_payroll_scope, "configuration_issues", side_effect=check),
		):
			issues = agents_api._statutory_payroll_issues_for_run_as("STAT-INSTALL-1", "{}")
		self.assertEqual(issues, {"payroll_entry": "Select a readable submitted monthly Payroll Entry."})
