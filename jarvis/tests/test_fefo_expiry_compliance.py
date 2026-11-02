"""Fail-closed boundaries for the FEFO expiry-compliance replacement."""

import hashlib
import json
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import (
	agent_runs,
	agent_scheduler,
	agent_scope,
	agents_api,
	canvas,
	operator_review,
	turn_handler,
)
from jarvis.chat import fefo_expiry_compliance as fefo
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import query, record_agent_run, run_report
from jarvis.tools import save_agent_dashboard as save_dashboard_tool

SCOPE = {
	"company": "FEFO Scope Company",
	"from_date": "2026-10-01",
	"to_date": "2026-10-31",
	"report_date": "2026-10-31",
	"fefo_scope_contract": "fefo-expiry-compliance-v0.4.2",
	"record_scope": {"doctype": "Warehouse", "name": "FEFO Leaf"},
}
PROOF = {
	"schema": "fefo-native-visibility-proof-v1",
	"complete": True,
	"policy_sha256": "a" * 64,
}
POLICY = {
	"fefo": {
		"reviewer": "Expiry Reviewer",
		"near_expiry_days": 90,
		"test_declaration": "Fake data only.",
	}
}


def _predicate(field, op, value):
	return {"field": field, "op": op, "value": value}


def _ledger_where(*, item_reference=None, voucher_type=None, cutoff="2026-10-31"):
	where = [
		_predicate("s.company", "=", SCOPE["company"]),
		_predicate("s.posting_date", "<=", cutoff),
		_predicate("s.docstatus", "<", 2),
		_predicate("s.is_cancelled", "=", 0),
		_predicate("s.warehouse", "=", SCOPE["record_scope"]["name"]),
	]
	if item_reference:
		where.append(_predicate("s.item_code", "=", {"$field": item_reference}))
	if voucher_type:
		where.append(_predicate("s.voucher_type", "=", voucher_type))
	return where


def _direct_ledger(*, cutoff="2026-10-31"):
	return {
		"from": "Stock Ledger Entry",
		"alias": "r",
		"select": ["r.name"],
		"where": [
			_predicate("r.company", "=", SCOPE["company"]),
			_predicate("r.posting_date", "<=", cutoff),
			_predicate("r.docstatus", "<", 2),
			_predicate("r.is_cancelled", "=", 0),
			_predicate("r.warehouse", "=", SCOPE["record_scope"]["name"]),
		],
		"limit": 20,
	}


def _item_request(*, op="exists", correlated=True, cutoff="2026-10-31"):
	where = _ledger_where(cutoff=cutoff)
	if correlated:
		where.append(_predicate("s.item_code", "=", {"$field": "r.name"}))
	return {
		"from": "Item",
		"alias": "r",
		"select": ["r.name"],
		"where": [{"op": op, "value": {"from": "Stock Ledger Entry", "alias": "s", "where": where}}],
		"limit": 20,
	}


def _voucher_request(*, split=False):
	where = _ledger_where(voucher_type="Stock Entry")
	where.append(_predicate("s.voucher_no", "=", {"$field": "r.name"}))
	if split:
		where = _ledger_where()
		return {
			"from": "Stock Entry",
			"alias": "r",
			"select": ["r.name"],
			"where": [
				{
					"op": "exists",
					"value": {
						"from": "Stock Ledger Entry",
						"alias": "s",
						"where": [*where, _predicate("s.voucher_no", "=", {"$field": "r.name"})],
					},
				},
				{
					"op": "exists",
					"value": {
						"from": "Stock Ledger Entry",
						"alias": "s",
						"where": [*where, _predicate("s.voucher_type", "=", "Stock Entry")],
					},
				},
			],
			"limit": 20,
		}
	return {
		"from": "Stock Entry",
		"alias": "r",
		"select": ["r.name"],
		"where": [{"op": "exists", "value": {"from": "Stock Ledger Entry", "alias": "s", "where": where}}],
		"limit": 20,
	}


class TestFefoExpiryCompliance(FrappeTestCase):
	def _scope_guard(self):
		return (
			patch.object(agent_scope, "active_fefo_scope", return_value=SCOPE),
			patch("jarvis.tools._doctype_name.canonical_doctype", side_effect=lambda value: value),
		)

	def test_configuration_accepts_only_policy_and_optional_top_level_scope(self):
		fefo.validate_configuration_edit(POLICY)
		fefo.validate_configuration_edit({**POLICY, "record_scope": SCOPE["record_scope"]})
		for bad in (
			{**POLICY, "company": SCOPE["company"]},
			{**POLICY, "from_date": SCOPE["from_date"]},
			{"fefo": {**POLICY["fefo"], "record_scope": SCOPE["record_scope"]}},
			{**POLICY, "record_scope": None},
		):
			with self.subTest(bad=bad):
				with self.assertRaises(ValueError):
					fefo.validate_configuration_edit(bad)
				self.assertIn("config", fefo.configuration_issues(bad))

	def test_configuration_refuses_every_noncanonical_present_record_scope(self):
		for scope in (
			None,
			False,
			{},
			{"doctype": "Warehouse"},
			{"doctype": "Company", "name": "FEFO Scope Company"},
			{"doctype": "Warehouse", "name": " FEFO Leaf"},
			{"doctype": "Warehouse", "name": "FEFO Leaf", "all": True},
		):
			with self.subTest(scope=scope):
				with self.assertRaises(ValueError):
					fefo.validate_configuration_edit({**POLICY, "record_scope": scope})

	def test_leaf_boundary_rejects_missing_group_disabled_unreadable_or_foreign_warehouse(self):
		valid = frappe._dict(company=SCOPE["company"], is_group=0, disabled=0)
		with (
			patch.object(frappe.db, "get_value", return_value=valid),
			patch.object(frappe, "has_permission", return_value=True),
		):
			self.assertEqual(
				agent_scope._fefo_warehouse_boundary(SCOPE["record_scope"], SCOPE["company"]),
				SCOPE["record_scope"],
			)

		cases = (
			(None, True, True),
			(frappe._dict(company=SCOPE["company"], is_group=1, disabled=0), True, True),
			(frappe._dict(company=SCOPE["company"], is_group=0, disabled=1), True, True),
			(frappe._dict(company="Other Company", is_group=0, disabled=0), True, True),
			(valid, False, True),
			(valid, True, False),
		)
		for warehouse, warehouse_readable, company_readable in cases:
			with self.subTest(
				warehouse=warehouse,
				warehouse_readable=warehouse_readable,
				company_readable=company_readable,
			):
				with (
					patch.object(frappe.db, "get_value", return_value=warehouse),
					patch.object(
						frappe,
						"has_permission",
						side_effect=(warehouse_readable, company_readable),
					),
					self.assertRaises(InvalidArgumentError),
				):
					agent_scope._fefo_warehouse_boundary(SCOPE["record_scope"], SCOPE["company"])

	def test_absent_scope_stamps_the_intentional_companywide_mode_without_a_warehouse_probe(self):
		installation = frappe._dict(agent=fefo.AGENT, config=frappe.as_json(POLICY))
		resolved = {
			"company": SCOPE["company"],
			"from_date": SCOPE["from_date"],
			"to_date": SCOPE["to_date"],
			"report_date": SCOPE["report_date"],
		}
		with (
			patch.object(agent_scope, "_resolve", return_value=resolved),
			patch.object(agent_scope, "_fefo_warehouse_boundary") as boundary,
			patch.object(agent_scope, "fefo_visibility_proof", return_value=PROOF),
		):
			stamped = agent_scope.resolve_scope(installation, allow_record_scope=True)
		boundary.assert_not_called()
		self.assertEqual(stamped["fefo_scope_contract"], SCOPE["fefo_scope_contract"])
		self.assertNotIn("record_scope", stamped)

	def test_stamped_scope_revalidates_canonical_dates_before_leaf_access(self):
		with (
			patch("jarvis._session.impersonate", return_value=nullcontext()),
			patch.object(
				agent_scope, "_fefo_warehouse_boundary", return_value=SCOPE["record_scope"]
			) as boundary,
			patch.object(agent_scope, "fefo_visibility_proof", return_value=PROOF),
		):
			self.assertEqual(
				agent_scope.fefo_stamped_scope_as_user(SCOPE, "fefo@example.invalid", PROOF),
				agent_scope.fefo_output_scope(SCOPE),
			)
		boundary.assert_called_once_with(SCOPE["record_scope"], SCOPE["company"])

		for bad in (
			{**SCOPE, "from_date": "2026-11-01"},
			{**SCOPE, "to_date": "2026-10-32"},
			{**SCOPE, "report_date": "2026-10-31T00:00:00"},
		):
			with self.subTest(bad=bad), patch.object(agent_scope, "_fefo_warehouse_boundary") as boundary:
				self.assertIsNone(agent_scope.fefo_stamped_scope_as_user(bad, "fefo@example.invalid", PROOF))
				boundary.assert_not_called()

	def test_legacy_scope_cannot_become_a_current_stamped_scope(self):
		legacy = {key: value for key, value in SCOPE.items() if key != "fefo_scope_contract"}
		legacy_v041 = {**SCOPE, "fefo_scope_contract": "fefo-expiry-compliance-v0.4.1"}
		self.assertEqual(agent_scope.fefo_output_scope(legacy), agent_scope.fefo_output_scope(SCOPE))
		self.assertIsNone(agent_scope.fefo_stamped_scope(legacy))
		self.assertIsNone(agent_scope.fefo_stamped_scope(legacy_v041))
		self.assertIsNone(agent_scope.fefo_stamped_scope_as_user(legacy_v041, "fefo@example.invalid", PROOF))
		self.assertEqual(agent_scope.fefo_stamped_scope(SCOPE), agent_scope.fefo_output_scope(SCOPE))

	def test_launch_scope_stamps_and_delivers_the_server_owned_contract_marker(self):
		installation = frappe._dict(agent=fefo.AGENT, config=frappe.as_json(POLICY))
		resolved = {
			"company": SCOPE["company"],
			"from_date": SCOPE["from_date"],
			"to_date": SCOPE["to_date"],
			"report_date": SCOPE["report_date"],
		}
		with (
			patch.object(agent_scope, "_resolve", return_value=resolved),
			patch.object(agent_scope, "fefo_visibility_proof", return_value=PROOF),
		):
			stamped = agent_scope.resolve_scope(installation, allow_record_scope=True)
		self.assertEqual(stamped["fefo_scope_contract"], SCOPE["fefo_scope_contract"])
		prompt = agent_scheduler._audit_prompt(
			frappe._dict(agent_slug=fefo.AGENT),
			installation,
			"manual",
			stamped,
		)
		self.assertIn('fefo_scope_contract="fefo-expiry-compliance-v0.4.2"', prompt)

	def test_live_fefo_scope_json_cannot_replace_or_remove_the_stamped_marker(self):
		"""An ORM write cannot weaken the server-owned FEFO run boundary."""
		original = frappe.as_json(SCOPE)
		run = frappe.get_doc(
			{
				"doctype": "Jarvis Agent Run",
				"agent": fefo.AGENT,
				"trigger": "manual",
				"status": "running",
				"started_at": frappe.utils.now(),
				"scope_json": original,
				"fefo_visibility_proof_json": frappe.as_json(PROOF),
			}
		)
		run.insert(ignore_permissions=True, ignore_links=True)
		self.addCleanup(frappe.db.delete, "Jarvis Agent Run", {"name": run.name})

		for replacement in (
			frappe.as_json({**SCOPE, "record_scope": {"doctype": "Warehouse", "name": "Other Leaf"}}),
			frappe.as_json({key: value for key, value in SCOPE.items() if key != "fefo_scope_contract"}),
		):
			with self.subTest(replacement=replacement):
				doc = frappe.get_doc("Jarvis Agent Run", run.name)
				doc.scope_json = replacement
				with self.assertRaises(frappe.PermissionError):
					doc.save(ignore_permissions=True)
				self.assertEqual(frappe.db.get_value("Jarvis Agent Run", run.name, "scope_json"), original)

		for replacement in (frappe.as_json({**PROOF, "policy_sha256": "b" * 64}), None):
			with self.subTest(proof_replacement=replacement):
				doc = frappe.get_doc("Jarvis Agent Run", run.name)
				doc.fefo_visibility_proof_json = replacement
				with self.assertRaises(frappe.PermissionError):
					doc.save(ignore_permissions=True)
				self.assertEqual(
					frappe.db.get_value("Jarvis Agent Run", run.name, "fefo_visibility_proof_json"),
					frappe.as_json(PROOF),
				)

	def test_config_api_refuses_unknown_top_level_key_before_save(self):
		original = frappe.as_json(POLICY)
		installation = frappe._dict(name="FEFO-CONFIG", agent=fefo.AGENT, config=original)
		installation.check_permission = MagicMock()
		installation.save = MagicMock()
		with (
			patch.object(frappe, "get_doc", return_value=installation),
			patch.object(agents_api, "registry_supports_record_scope", return_value=True),
			patch.object(agents_api, "log_activity") as activity,
			patch.object(frappe.db, "commit") as commit,
		):
			with self.assertRaises(frappe.ValidationError):
				agents_api.set_config(installation.name, frappe.as_json({**POLICY, "company": "Other"}))
		self.assertEqual(installation.config, original)
		installation.save.assert_not_called()
		activity.assert_not_called()
		commit.assert_not_called()

	def test_enabled_schedule_refuses_invalid_policy_before_save(self):
		installation = frappe._dict(
			name="FEFO-SCHEDULE",
			agent=fefo.AGENT,
			run_as_user="fefo-run-as@example.invalid",
			config=frappe.as_json({"fefo": {}}),
			schedule_enabled=0,
			schedule_frequency="daily",
			schedule_time=None,
			schedule_weekday=None,
			schedule_day_of_month=None,
			next_run_at=None,
		)
		installation.check_permission = MagicMock()
		installation.save = MagicMock()
		with (
			patch.object(frappe, "get_doc", return_value=installation),
			patch.object(fefo, "is_valid_unattended_owner", return_value=True),
			patch.object(fefo, "impersonate", return_value=nullcontext()),
			patch.object(agents_api, "log_activity") as activity,
			patch.object(frappe.db, "commit") as commit,
		):
			with self.assertRaises(frappe.ValidationError):
				agents_api.set_schedule(installation.name, schedule_enabled=1)
		installation.save.assert_not_called()
		activity.assert_not_called()
		commit.assert_not_called()

	def test_manual_launch_refuses_a_bad_leaf_before_the_savepoint(self):
		installation = frappe._dict(
			name="FEFO-MANUAL-SCOPE",
			agent=fefo.AGENT,
			owner="fefo-owner@example.invalid",
			run_as_user="fefo-run-as@example.invalid",
			reviewer="Expiry Reviewer",
			config=frappe.as_json({**POLICY, "record_scope": SCOPE["record_scope"]}),
		)
		listing = frappe._dict(
			agent_slug=fefo.AGENT,
			status="Published",
			title="FEFO test",
			config_keys=frappe.as_json(["fefo.reviewer"]),
		)
		tools = MagicMock(side_effect=AssertionError("must not inspect tools before the leaf preflight"))
		with (
			patch.object(frappe, "get_doc", return_value=listing),
			patch.object(operator_review, "backend", return_value=None),
			patch.object(agent_scheduler, "_resolve_initiating_human", return_value=installation.owner),
			patch("jarvis.chat.agent_catalog.registry_supports_record_scope", return_value=True),
			patch.object(
				agent_scope,
				"resolve_scope",
				side_effect=InvalidArgumentError("FEFO Warehouse scope is stale"),
			),
			patch.object(frappe, "log_error"),
			patch("jarvis.chat.agent_catalog.registry_tools_allow", tools),
			patch.object(frappe.db, "savepoint") as savepoint,
		):
			with self.assertRaisesRegex(InvalidArgumentError, "Warehouse scope is stale"):
				agent_scheduler._launch_audit(installation, trigger="manual")
		tools.assert_not_called()
		savepoint.assert_not_called()

	def test_due_schedule_refuses_stale_leaf_before_claim_or_run_state(self):
		"""A direct stale schedule edit must remain safe to repair or disable."""
		row = frappe._dict(
			name="FEFO-DUE-SCHEDULE",
			owner="fefo-owner@example.invalid",
			run_as_user="fefo-run-as@example.invalid",
			agent=fefo.AGENT,
			schedule_frequency="daily",
			schedule_time="00:00",
			installable=1,
			config=frappe.as_json({**POLICY, "record_scope": SCOPE["record_scope"]}),
			activation_state="live",
		)
		listing = frappe._dict(nature="Auditor", status="Published", operator_visibility="available")
		with (
			patch.object(frappe.db, "get_value", return_value=listing),
			patch("jarvis.chat.agent_catalog.registry_supports_record_scope", return_value=True),
			patch.object(agent_scheduler, "_valid_owner", return_value=True),
			patch.object(agent_scheduler, "_is_app_learning", return_value=False),
			patch("jarvis.chat.agents_api._user_allowed_for_agent", return_value=True),
			patch.object(agent_scheduler, "run_as_mapping_refusal", return_value=None),
			patch.object(
				fefo,
				"assert_schedule_configuration",
				side_effect=InvalidArgumentError("FEFO Warehouse scope is stale"),
			) as preflight,
			patch.object(agent_scheduler, "_record_failed") as record_failed,
			patch.object(agent_scheduler, "_advance") as advance,
			patch.object(agent_scheduler, "_claim_slot") as claim,
			patch.object(agent_scheduler, "_dispatch") as dispatch,
			patch.object(frappe, "logger"),
		):
			agent_scheduler._sweep_one(row, object(), set())
		preflight.assert_called_once_with(row)
		record_failed.assert_not_called()
		advance.assert_not_called()
		claim.assert_not_called()
		dispatch.assert_not_called()

	def test_scope_projection_rejects_incoherent_dates_and_mode_changes(self):
		self.assertEqual(
			agent_scope.fefo_output_scope(SCOPE),
			{key: value for key, value in SCOPE.items() if key != "fefo_scope_contract"},
		)
		for bad in (
			{**SCOPE, "from_date": "2026-11-01"},
			{**SCOPE, "report_date": "not-a-date"},
			{key: value for key, value in SCOPE.items() if key != "record_scope"},
		):
			with self.subTest(bad=bad):
				self.assertIsNone(record_agent_run._bound_fefo_scope(SCOPE, bad))

	def test_relation_aware_native_fence_accepts_only_correlated_source(self):
		active, canonical = self._scope_guard()
		with active, canonical:
			agent_scope.assert_active_fefo_query_scope(_direct_ledger())
			agent_scope.assert_active_fefo_query_scope(_item_request())
			for forged in (
				_item_request(correlated=False),
				_item_request(op="not exists"),
				_direct_ledger(cutoff="2026-11-01"),
				_item_request(cutoff="2026-11-01"),
			):
				with self.subTest(forged=forged):
					with self.assertRaises(InvalidArgumentError):
						agent_scope.assert_active_fefo_query_scope(forged)

	def test_voucher_source_requires_one_correlated_ledger_exists(self):
		active, canonical = self._scope_guard()
		with active, canonical:
			agent_scope.assert_active_fefo_query_scope(_voucher_request())
			with self.assertRaises(InvalidArgumentError):
				agent_scope.assert_active_fefo_query_scope(_voucher_request(split=True))

	def test_closing_header_requires_a_leaf_link_and_direct_balance_is_refused(self):
		closing = {
			"from": "Stock Closing Entry",
			"alias": "r",
			"select": ["r.name"],
			"where": [
				_predicate("r.company", "=", SCOPE["company"]),
				_predicate("r.docstatus", "=", 1),
				_predicate("r.to_date", "<=", "2026-10-31"),
				{
					"op": "exists",
					"value": {
						"from": "Stock Closing Balance",
						"alias": "b",
						"where": [
							_predicate("b.stock_closing_entry", "=", {"$field": "r.name"}),
							_predicate("b.company", "=", SCOPE["company"]),
							_predicate("b.warehouse", "=", SCOPE["record_scope"]["name"]),
						],
					},
				},
			],
			"limit": 20,
		}
		active, canonical = self._scope_guard()
		with active, canonical:
			agent_scope.assert_active_fefo_query_scope(closing)
			forged = {**closing, "where": closing["where"][:-1]}
			with self.assertRaises(InvalidArgumentError):
				agent_scope.assert_active_fefo_query_scope(forged)
			with self.assertRaises(InvalidArgumentError):
				agent_scope.assert_active_fefo_query_scope(
					{"from": "Stock Closing Balance", "alias": "r", "where": [], "select": ["r.name"]}
				)

	def test_leaf_fence_refuses_a_companywide_repost_source(self):
		repost = {
			"from": "Repost Item Valuation",
			"alias": "r",
			"select": ["r.name"],
			"where": [
				_predicate("r.company", "=", SCOPE["company"]),
				_predicate("r.warehouse", "=", SCOPE["record_scope"]["name"]),
			],
			"limit": 20,
		}
		active, canonical = self._scope_guard()
		with active, canonical:
			agent_scope.assert_active_fefo_query_scope(repost)
			with self.assertRaises(InvalidArgumentError):
				agent_scope.assert_active_fefo_query_scope({**repost, "where": repost["where"][:1]})

	def test_forged_query_and_report_stop_before_the_source_tool(self):
		active, canonical = self._scope_guard()
		with active, canonical, patch.object(query, "_compile") as compile_query:
			with self.assertRaises(InvalidArgumentError):
				query.query(_item_request(correlated=False))
		compile_query.assert_not_called()

		with (
			patch.object(agent_scope, "active_fefo_scope", return_value=SCOPE),
			patch.object(run_report.frappe.db, "exists") as report_exists,
			patch.object(run_report, "frappe_run_report") as execute_report,
		):
			with self.assertRaises(InvalidArgumentError):
				run_report.run_report(
					"Batch-Wise Balance History",
					{
						"company": SCOPE["company"],
						"from_date": SCOPE["from_date"],
						"to_date": SCOPE["to_date"],
						"warehouse": "Other Warehouse",
					},
				)
		report_exists.assert_not_called()
		execute_report.assert_not_called()

	def test_missing_or_stale_visibility_proof_stops_query_and_report_before_source_access(self):
		run = frappe._dict(
			agent=fefo.AGENT, scope_json=frappe.as_json(SCOPE), fefo_visibility_proof_json=None
		)

		def get_value(doctype, *_args, **_kwargs):
			if doctype == "Jarvis Agent Run":
				return run
			if doctype == "Jarvis Chat Session":
				return "fefo@example.invalid"
			self.fail(f"unexpected source lookup: {doctype}")

		with (
			patch("jarvis.tools._agent_run_ctx.get_session_key", return_value="agent:fefo:FEFO-RUN"),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(query, "_compile") as compile_query,
			self.assertRaises(InvalidArgumentError),
		):
			query.query(_direct_ledger())
		compile_query.assert_not_called()

		with (
			patch("jarvis.tools._agent_run_ctx.get_session_key", return_value="agent:fefo:FEFO-RUN"),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(run_report.frappe.db, "exists") as report_exists,
			patch.object(run_report, "frappe_run_report") as execute_report,
			self.assertRaises(InvalidArgumentError),
		):
			run_report.run_report(
				"Batch-Wise Balance History",
				{
					"company": SCOPE["company"],
					"from_date": SCOPE["from_date"],
					"to_date": SCOPE["to_date"],
					"warehouse": SCOPE["record_scope"]["name"],
				},
			)
		report_exists.assert_not_called()
		execute_report.assert_not_called()

	def test_public_writeback_refuses_a_stale_leaf_before_the_direct_persistence_seam(self):
		tokens = ["fef-life-3f72", "fef-near-8c30", "fef-past-2d48", "fef-pick-6a91", "fef-total-9b51"]
		run_row = frappe._dict(
			name="FEFO-RUN",
			status="running",
			installation="FEFO-INSTALLATION",
			agent=fefo.AGENT,
		)
		run_doc = frappe._dict(name="FEFO-RUN", agent=fefo.AGENT, scope_json=frappe.as_json(SCOPE))
		final_run = frappe._dict(
			name="FEFO-RUN",
			status="partial",
			findings_count=0,
			blocker_count=0,
			coverage_note="scope did not match server-stamped FEFO boundary",
			owner="fefo@example.invalid",
		)

		def get_value(doctype, *_args, **_kwargs):
			if doctype == record_agent_run.RUN:
				return run_row
			if doctype == record_agent_run.LISTING:
				return frappe._dict(rule_tokens=frappe.as_json(tokens), doctypes_required="[]")
			self.fail(f"unexpected get_value for {doctype}")

		with (
			patch("jarvis.tools._agent_run_ctx.get_session_key", return_value="agent:fefo:FEFO-RUN"),
			patch.object(record_agent_run.frappe.db, "get_value", side_effect=get_value),
			patch.object(
				record_agent_run.frappe,
				"get_doc",
				side_effect=(frappe._dict(name="FEFO-INSTALLATION"), run_doc),
			),
			patch.object(operator_review, "backend", return_value=None),
			patch.object(agent_runs, "_fefo_run_as_user", return_value="fefo@example.invalid"),
			patch.object(agent_scope, "fefo_stamped_scope_as_user", return_value=None),
			patch("jarvis.chat.agent_runs.record_delegate_run", return_value=final_run) as land,
			patch("jarvis.chat.agent_run_steps.record_step"),
			patch.object(record_agent_run.frappe, "logger"),
		):
			result = record_agent_run.record_agent_run(
				findings=[],
				coverage={token: {"state": "evaluated"} for token in tokens},
				scope=SCOPE,
				integrity_digest="unsafe-digest",
				canvas_ref="unsafe-canvas",
				dashboard="unsafe-dashboard",
				rows_consumed=100,
			)

		self.assertEqual(result["status"], "partial")
		_args, kwargs = land.call_args
		self.assertEqual(kwargs["scope"], {})
		self.assertEqual(set(kwargs["coverage"]), set(tokens))
		self.assertTrue(all(row["state"] == "not_evaluable" for row in kwargs["coverage"].values()))
		self.assertIsNone(kwargs["integrity_digest"])
		self.assertIsNone(kwargs["canvas_ref"])
		self.assertIsNone(kwargs["dashboard"])
		self.assertIsNone(kwargs["rows_consumed"])

	def test_direct_writeback_stops_at_scope_cleanup_before_finding_persistence(self):
		class ScopeCleanupReached(Exception):
			pass

		run_doc = frappe._dict(name="FEFO-RUN", agent=fefo.AGENT)
		inst = frappe._dict(agent=fefo.AGENT, owner="fefo@example.invalid", reviewer="fefo@example.invalid")
		with (
			patch.object(agent_runs, "_listing_token_sets", return_value=({"fef-life-3f72"}, set())),
			patch.object(agent_runs.frappe.db, "get_value", return_value=frappe.as_json(SCOPE)),
			patch.object(
				agent_runs, "clear_untrusted_fefo_artifacts", side_effect=ScopeCleanupReached
			) as cleanup,
			self.assertRaises(ScopeCleanupReached),
		):
			agent_runs.record_delegate_run(
				run_doc,
				inst,
				[{"token": "fef-life-3f72", "ref_doctype": "Warehouse", "ref_name": "Other"}],
				coverage={"fef-life-3f72": "evaluated"},
				scope={**SCOPE, "record_scope": {"doctype": "Warehouse", "name": "Other"}},
			)
		cleanup.assert_called_once_with(run_doc, inst, proof_must_be_invalid=True)

	def test_direct_writeback_with_a_missing_run_as_identity_cleans_up(self):
		class ScopeCleanupReached(Exception):
			pass

		run_doc = frappe._dict(name="FEFO-RUN", agent=fefo.AGENT)
		inst = frappe._dict(agent=fefo.AGENT, owner="fefo@example.invalid", reviewer="fefo@example.invalid")
		with (
			patch.object(agent_runs, "_listing_token_sets", return_value=({"fef-life-3f72"}, set())),
			patch.object(agent_runs.frappe.db, "get_value", return_value=frappe.as_json(SCOPE)),
			patch.object(
				agent_runs,
				"_fefo_run_as_user",
				side_effect=frappe.ValidationError("FEFO run-as identity is unavailable"),
			),
			patch.object(
				agent_runs, "clear_untrusted_fefo_artifacts", side_effect=ScopeCleanupReached
			) as cleanup,
			self.assertRaises(ScopeCleanupReached),
		):
			agent_runs.record_delegate_run(
				run_doc,
				inst,
				[{"token": "fef-life-3f72", "ref_doctype": "Warehouse", "ref_name": "FEFO Leaf"}],
				coverage={"fef-life-3f72": "evaluated"},
				scope=agent_scope.fefo_output_scope(SCOPE),
			)
		cleanup.assert_called_once_with(run_doc, inst, proof_must_be_invalid=True)

	def test_legacy_valid_looking_scope_cannot_auto_resolve_a_new_fefo_run(self):
		class ReachedRunFinalization(Exception):
			pass

		current_scope = {key: value for key, value in SCOPE.items() if key != "record_scope"}
		legacy_scope = {key: value for key, value in current_scope.items() if key != "fefo_scope_contract"}
		run_doc = frappe._dict(
			name="FEFO-CURRENT",
			agent=fefo.AGENT,
			wm_row_count=None,
			fefo_visibility_proof_json=frappe.as_json(PROOF),
		)
		inst = frappe._dict(
			agent=fefo.AGENT,
			owner="fefo@example.invalid",
			reviewer="fefo@example.invalid",
			activation_state="live",
		)
		resolution_attempts = []

		def get_value(doctype, *_args, **_kwargs):
			if doctype == agent_runs.RUN:
				return frappe.as_json(current_scope)
			if doctype == agent_runs.SESSION:
				return "fefo@example.invalid"
			self.fail(f"unexpected get_value for {doctype}")

		def get_all(doctype, **_kwargs):
			if doctype == agent_runs.FINDING:
				return [
					frappe._dict(
						name="OLD-FEFO-FINDING",
						fingerprint="old-fingerprint",
						company=current_scope["company"],
						last_seen_run="LEGACY-FEFO-RUN",
					)
				]
			if doctype == agent_runs.RUN:
				return [frappe._dict(name="LEGACY-FEFO-RUN", scope_json=frappe.as_json(legacy_scope))]
			self.fail(f"unexpected get_all for {doctype}")

		def set_value(doctype, name, values, **_kwargs):
			if doctype == agent_runs.FINDING and name == "OLD-FEFO-FINDING":
				resolution_attempts.append(values)
				raise AssertionError("legacy FEFO finding must not auto-resolve")
			raise ReachedRunFinalization

		with (
			patch.object(agent_runs, "_listing_token_sets", return_value=({"fef-life-3f72"}, set())),
			patch.object(agent_runs, "_watermark_drift", return_value=False),
			patch.object(agent_runs, "_scoped_visibility", return_value=False),
			patch.object(agent_runs, "_rowcount_shortfall", return_value=False),
			patch.object(agent_runs, "_fefo_run_as_user", return_value="fefo@example.invalid"),
			patch.object(
				agent_scope,
				"fefo_stamped_scope_as_user",
				return_value=agent_scope.fefo_output_scope(current_scope),
			),
			patch.object(agent_runs.frappe.db, "get_value", side_effect=get_value),
			patch.object(agent_runs.frappe, "get_all", side_effect=get_all),
			patch.object(agent_runs.frappe.db, "set_value", side_effect=set_value),
			self.assertRaises(ReachedRunFinalization),
		):
			agent_runs.record_delegate_run(
				run_doc,
				inst,
				[],
				coverage={"fef-life-3f72": "evaluated"},
				scope=agent_scope.fefo_output_scope(current_scope),
			)
		self.assertEqual(resolution_attempts, [])

	def test_visibility_proof_allows_only_no_or_exact_company_user_permission(self):
		"""A Company-only grant is complete; every other User Permission is a slice."""
		safe = frappe._dict(
			allow="Company",
			for_value=SCOPE["company"],
			apply_to_all_doctypes=1,
			applicable_for="",
			hide_descendants=0,
			is_default=0,
		)
		effective = {"Company": [frappe._dict(doc=SCOPE["company"], applicable_for="", hide_descendants=0)]}
		with (
			patch.object(frappe, "get_all", return_value=[safe]),
			patch("frappe.permissions.get_user_permissions", return_value=effective),
			patch.object(agent_scope, "_fefo_company_permission_links_are_safe", return_value=[]),
		):
			self.assertEqual(
				agent_scope._fefo_company_only_user_permissions("fefo@example.invalid", SCOPE["company"]),
				[
					{
						"allow": "Company",
						"for_value": SCOPE["company"],
						"apply_to_all_doctypes": 1,
						"applicable_for": "",
						"hide_descendants": 0,
						"is_default": 0,
					}
				],
			)
		with (
			patch.object(frappe, "get_all", return_value=[]),
			patch("frappe.permissions.get_user_permissions", return_value={}),
		):
			self.assertEqual(
				agent_scope._fefo_company_only_user_permissions("fefo@example.invalid", SCOPE["company"]), []
			)

		for rows, permissions in (
			([{**safe, "allow": "Warehouse"}], effective),
			([{**safe, "for_value": "Other Company"}], effective),
			([{**safe, "applicable_for": "Stock Ledger Entry"}], effective),
			([safe, safe], effective),
			([safe], {"Warehouse": [frappe._dict(doc="FEFO Leaf")]}),
		):
			with self.subTest(rows=rows, permissions=permissions):
				with (
					patch.object(frappe, "get_all", return_value=rows),
					patch("frappe.permissions.get_user_permissions", return_value=permissions),
					self.assertRaises(InvalidArgumentError),
				):
					agent_scope._fefo_company_only_user_permissions("fefo@example.invalid", SCOPE["company"])

	def test_visibility_refresh_removes_stale_system_settings_before_policy_hashing(self):
		"""Strict User Permission policy must not come from a prior worker cache."""
		had_settings = hasattr(frappe.local, "system_settings")
		previous_settings = getattr(frappe.local, "system_settings", None)
		frappe.local.system_settings = frappe._dict(apply_strict_user_permissions=0)
		try:
			with (
				patch.object(
					frappe.db,
					"get_value",
					return_value=frappe._dict(enabled=1, user_type="System User"),
				),
				patch.object(frappe.cache, "hdel"),
				patch.object(frappe.cache, "delete_value") as delete_value,
				patch.object(frappe, "clear_document_cache") as clear_document_cache,
			):
				agent_scope._fefo_refresh_access("fefo@example.invalid")
			self.assertFalse(hasattr(frappe.local, "system_settings"))
			clear_document_cache.assert_any_call("Jarvis Settings", "Jarvis Settings")
			delete_value.assert_any_call(
				"masked_fields::Serial and Batch Entry::Serial and Batch Bundle::fefo@example.invalid"
			)
		finally:
			if had_settings:
				frappe.local.system_settings = previous_settings
			elif hasattr(frappe.local, "system_settings"):
				del frappe.local.system_settings

	def test_exact_company_user_permission_refuses_an_unreviewed_source_company_link(self):
		"""A custom Link -> Company makes the otherwise narrow exception partial."""
		safe = frappe._dict(
			allow="Company",
			for_value=SCOPE["company"],
			apply_to_all_doctypes=1,
			applicable_for="",
			hide_descendants=0,
			is_default=0,
		)
		effective = {"Company": [frappe._dict(doc=SCOPE["company"], applicable_for="", hide_descendants=0)]}

		class ItemMeta:
			def __init__(self, links):
				self.links = links

			def get_link_fields(self):
				return self.links

		custom_company_link = frappe._dict(
			fieldname="custom_fefo_company",
			fieldtype="Link",
			options="Company",
			ignore_user_permissions=0,
		)
		with (
			patch.object(agent_scope, "_FEFO_VISIBILITY_SURFACE", (("Item", None),)),
			patch.object(frappe, "get_meta", return_value=ItemMeta([custom_company_link])),
			patch.object(frappe, "get_all", return_value=[safe]),
			patch("frappe.permissions.get_user_permissions", return_value=effective),
			self.assertRaises(InvalidArgumentError),
		):
			agent_scope._fefo_company_only_user_permissions("fefo@example.invalid", SCOPE["company"])

		with (
			patch.object(agent_scope, "_FEFO_VISIBILITY_SURFACE", (("Item", None),)),
			patch.object(frappe, "get_meta", return_value=ItemMeta([])),
			patch.object(frappe, "get_all", return_value=[safe]),
			patch("frappe.permissions.get_user_permissions", return_value=effective),
		):
			self.assertEqual(
				agent_scope._fefo_company_only_user_permissions("fefo@example.invalid", SCOPE["company"]),
				[
					{
						"allow": "Company",
						"for_value": SCOPE["company"],
						"apply_to_all_doctypes": 1,
						"applicable_for": "",
						"hide_descendants": 0,
						"is_default": 0,
					}
				],
			)

	def test_native_report_field_guard_refuses_restricted_masked_or_unknown_fields(self):
		"""Raw report reads must never bypass a source field restriction."""

		class Meta:
			def __init__(self, fields, valid_fields):
				self.fields = fields
				self.valid_fields = valid_fields

			def get_valid_columns(self):
				return self.valid_fields

			def get_field(self, fieldname):
				return self.fields.get(fieldname)

		public = Meta({"description": frappe._dict(permlevel=0, mask="")}, ["description"])
		restricted = Meta({"description": frappe._dict(permlevel=1, mask="")}, ["description"])
		masked = Meta({"description": frappe._dict(permlevel=0, mask="####")}, ["description"])
		unknown = Meta({}, ["description"])
		with (
			patch.object(agent_scope, "_FEFO_NATIVE_REPORT_FIELDS", {"Item": frozenset({"description"})}),
			patch.object(frappe, "get_meta", return_value=public),
		):
			self.assertEqual(
				agent_scope._fefo_native_report_fields(),
				[{"doctype": "Item", "fields": ["description"]}],
			)
		for meta in (restricted, masked, unknown):
			with self.subTest(meta=meta):
				with (
					patch.object(
						agent_scope, "_FEFO_NATIVE_REPORT_FIELDS", {"Item": frozenset({"description"})}
					),
					patch.object(frappe, "get_meta", return_value=meta),
					self.assertRaises(InvalidArgumentError),
				):
					agent_scope._fefo_native_report_fields()

		# Stock Closing Balance uses select("*").  A non-public custom column is
		# therefore unsafe even when the canonical FEFO code does not name it.
		closing = Meta(
			{
				"safe": frappe._dict(permlevel=0, mask=""),
				"custom_restricted": frappe._dict(permlevel=1, mask=""),
			},
			["safe", "custom_restricted"],
		)
		with (
			patch.object(agent_scope, "_FEFO_NATIVE_REPORT_FIELDS", {"Stock Closing Balance": None}),
			patch.object(frappe, "get_meta", return_value=closing),
			self.assertRaises(InvalidArgumentError),
		):
			agent_scope._fefo_native_report_fields()

	def test_native_report_field_guard_covers_stock_closing_helpers_and_dimensions(self):
		"""The raw report guard includes helper reads and dynamic SLE dimensions."""
		self.assertTrue(
			{
				"name",
				"item_code",
				"warehouse",
				"batch_no",
				"posting_date",
				"posting_time",
				"posting_datetime",
				"actual_qty",
				"qty_after_transaction",
				"stock_value_difference",
				"stock_value",
				"valuation_rate",
				"stock_uom",
				"docstatus",
				"is_cancelled",
				"has_batch_no",
				"serial_and_batch_bundle",
				"voucher_no",
				"company",
			}.issubset(agent_scope._FEFO_NATIVE_REPORT_FIELDS["Stock Ledger Entry"])
		)
		self.assertTrue(
			{"name", "item_name", "description", "stock_uom", "item_group", "has_serial_no"}.issubset(
				agent_scope._FEFO_NATIVE_REPORT_FIELDS["Item"]
			)
		)

		class Meta:
			def __init__(self, field):
				self.field = field

			def get_valid_columns(self):
				return ["custom_inventory_dimension"]

			def get_field(self, fieldname):
				return self.field if fieldname == "custom_inventory_dimension" else None

		public = Meta(frappe._dict(permlevel=0, mask=""))
		restricted = Meta(frappe._dict(permlevel=1, mask=""))
		with (
			patch.object(agent_scope, "_FEFO_NATIVE_REPORT_FIELDS", {"Stock Ledger Entry": frozenset()}),
			patch.object(
				agent_scope, "_fefo_inventory_dimension_targets", return_value=["custom_inventory_dimension"]
			),
			patch.object(frappe, "get_meta", return_value=public),
		):
			self.assertEqual(
				agent_scope._fefo_native_report_fields(),
				[{"doctype": "Stock Ledger Entry", "fields": ["custom_inventory_dimension"]}],
			)
		with (
			patch.object(agent_scope, "_FEFO_NATIVE_REPORT_FIELDS", {"Stock Ledger Entry": frozenset()}),
			patch.object(
				agent_scope, "_fefo_inventory_dimension_targets", return_value=["custom_inventory_dimension"]
			),
			patch.object(frappe, "get_meta", return_value=restricted),
			self.assertRaises(InvalidArgumentError),
		):
			agent_scope._fefo_native_report_fields()

		with patch.object(
			frappe,
			"get_all",
			return_value=[frappe._dict(target_fieldname="custom_inventory_dimension")],
		):
			self.assertEqual(agent_scope._fefo_inventory_dimension_targets(), ["custom_inventory_dimension"])
		with (
			patch.object(frappe, "get_all", return_value=[frappe._dict(target_fieldname=" bad")]),
			self.assertRaises(InvalidArgumentError),
		):
			agent_scope._fefo_inventory_dimension_targets()

	def test_visibility_proof_refuses_owner_hooks_and_permission_scripts(self):
		full = {"read": 1, "select": 1, "has_if_owner_enabled": False, "if_owner": {}}
		select_only = {"read": 0, "select": 1, "has_if_owner_enabled": False, "if_owner": {}}
		owner_only = {"read": 1, "select": 0, "has_if_owner_enabled": True, "if_owner": {"read": 1}}
		with patch("frappe.permissions.get_role_permissions", return_value=full):
			self.assertEqual(
				agent_scope._fefo_role_access(object(), "fefo@example.invalid"),
				{"read": True, "select": True},
			)
		with (
			patch("frappe.permissions.get_role_permissions", return_value=select_only),
			self.assertRaises(InvalidArgumentError),
		):
			agent_scope._fefo_role_access(object(), "fefo@example.invalid")
		with (
			patch("frappe.permissions.get_role_permissions", return_value=owner_only),
			self.assertRaises(InvalidArgumentError),
		):
			agent_scope._fefo_role_access(object(), "fefo@example.invalid")

		def no_hooks(name, default=None):
			return {} if name in {"permission_query_conditions", "has_permission"} else default

		with (
			patch.object(frappe, "get_hooks", side_effect=no_hooks),
			patch(
				"frappe.core.doctype.server_script.server_script_utils.get_server_script_map",
				return_value={"permission_query": {}},
			),
		):
			agent_scope._fefo_assert_no_record_slices()

		for hooks, scripts in (
			(
				{"permission_query_conditions": {"Stock Ledger Entry": ["app.slice"]}},
				{"permission_query": {}},
			),
			({"has_permission": {"*": ["app.slice"]}}, {"permission_query": {}}),
			(
				{"permission_query_conditions": {}, "has_permission": {}},
				{"permission_query": {"Batch": "Slice"}},
			),
		):

			def selected(name, default=None, hooks=hooks):
				return hooks.get(name, default)

			with self.subTest(hooks=hooks, scripts=scripts):
				with (
					patch.object(frappe, "get_hooks", side_effect=selected),
					patch(
						"frappe.core.doctype.server_script.server_script_utils.get_server_script_map",
						return_value=scripts,
					),
					self.assertRaises(InvalidArgumentError),
				):
					agent_scope._fefo_assert_no_record_slices()

	def test_visibility_proof_refuses_company_select_without_read(self):
		"""A Company picker grant cannot substitute for source-plan read access."""
		companywide_scope = {key: value for key, value in SCOPE.items() if key != "record_scope"}
		with (
			patch.object(agent_scope, "_fefo_refresh_access"),
			patch.object(agent_scope, "_fefo_role_access", return_value={"read": True, "select": True}),
			patch.object(frappe, "get_meta", return_value=MagicMock()),
			patch.object(frappe, "has_permission", side_effect=(False, True)) as has_permission,
			self.assertRaises(InvalidArgumentError),
		):
			agent_scope.fefo_visibility_proof(companywide_scope)
		has_permission.assert_called_once_with(
			"Company",
			"read",
			doc=SCOPE["company"],
			user=frappe.session.user,
			print_logs=False,
		)

	def test_canonical_query_plan_matches_the_full_signed_v042_bundle_contract(self):
		"""The full app plan matches bundle ``query_specs`` in both scope modes."""
		leaf = agent_scope._fefo_canonical_query_specs(SCOPE)
		companywide_scope = {key: value for key, value in SCOPE.items() if key != "record_scope"}
		companywide = agent_scope._fefo_canonical_query_specs(companywide_scope)

		# These SHA-256 pins are the canonical JSON outputs of
		# fefo_native_inputs.query_specs(scope, POLICY) in bundle v0.4.2.  They
		# cover select, where, joins, order, and nested EXISTS fields, not a few
		# selected examples.  A bundle-plan change must update this app proof in
		# the same review.
		def digest(plan):
			return hashlib.sha256(
				json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
			).hexdigest()

		self.assertEqual(
			digest(companywide), "156d950a2a5ba2ca3140233852d6c21cf697b0288d61df288f965bec703a78e0"
		)
		self.assertEqual(digest(leaf), "79fc3de17428de3be519c6b64f8223d8ea41405428238955d183b25f33f11e47")
		self.assertNotIn("dimensions", leaf)
		self.assertIn("dimensions", companywide)
		self.assertIn(
			"Stock Closing Balance",
			{row[0] for row in agent_scope._fefo_query_field_references(leaf["closings"])},
		)
		self.assertNotIn(
			"Stock Closing Balance",
			{row[0] for row in agent_scope._fefo_query_field_references(companywide["closings"])},
		)
		fields = {
			(doctype, field)
			for spec in leaf.values()
			for doctype, field, _parent in agent_scope._fefo_query_field_references(spec)
		}
		self.assertTrue(
			{
				("Company", "default_currency"),
				("Batch", "expiry_date"),
				("Stock Ledger Entry", "voucher_type"),
				("Serial and Batch Bundle", "type_of_transaction"),
				("Stock Closing Entry", "from_date"),
			}.issubset(fields)
		)
		self.assertIn(
			("Serial and Batch Entry", "batch_no", "Serial and Batch Bundle"),
			agent_scope._fefo_query_field_references(leaf["bundle_entries"]),
		)

	def test_canonical_query_access_uses_the_query_compiler_before_a_source_read(self):
		"""A complete proof must pass the same compiler as jarvis__query."""
		spec = {"from": "Item", "alias": "r", "select": ["r.name"], "where": [], "limit": 1}
		with (
			patch.object(agent_scope, "_fefo_canonical_query_specs", return_value={"items": spec}),
			patch.object(query, "_compile", return_value=(MagicMock(), {})) as compile_query,
			patch.object(
				agent_scope,
				"_fefo_assert_canonical_query_fields_unmasked",
				return_value=[{"doctype": "Item", "field": "name", "parent": ""}],
			),
		):
			self.assertEqual(
				agent_scope._fefo_canonical_query_access(SCOPE),
				[{"source": "items", "fields": [{"doctype": "Item", "field": "name", "parent": ""}]}],
			)
		compile_query.assert_called_once_with(spec)

	def test_canonical_query_mask_guard_refuses_a_masked_child_field(self):
		"""A masked source field cannot supply a full-coverage FEFO input."""
		meta = MagicMock()
		with (
			patch.object(agent_scope.frappe, "session", frappe._dict(user="fefo@example.invalid")),
			patch("jarvis.tools._export.masking.mask_field_value", object()),
			patch.object(
				agent_scope,
				"_fefo_query_field_references",
				return_value=[("Serial and Batch Entry", "batch_no", "Serial and Batch Bundle")],
			),
			patch.object(frappe, "get_meta", return_value=meta),
			patch(
				"jarvis.compat.masked_fields_of", return_value=[frappe._dict(fieldname="batch_no")]
			) as masked,
			self.assertRaises(InvalidArgumentError),
		):
			agent_scope._fefo_assert_canonical_query_fields_unmasked({})
		masked.assert_called_once_with(meta, "Serial and Batch Bundle")

	def test_query_allowlist_requires_every_outer_join_and_exists_doctype(self):
		"""A configured allowlist must admit the exact active canonical plan."""
		all_doctypes = "\n".join(doctype for doctype, _parent in agent_scope._FEFO_VISIBILITY_SURFACE)
		with patch.object(frappe.db, "get_single_value", return_value=all_doctypes):
			proof = agent_scope._fefo_query_allowlist(SCOPE)
		self.assertEqual(proof["mode"], "configured")
		self.assertIn("Stock Closing Balance", proof["required"])
		self.assertNotIn("Inventory Dimension", proof["required"])
		with (
			patch.object(
				frappe.db, "get_single_value", return_value=all_doctypes.replace("Stock Closing Balance", "")
			),
			self.assertRaises(InvalidArgumentError),
		):
			agent_scope._fefo_query_allowlist(SCOPE)

	def test_native_report_access_matches_the_run_report_permission_gate(self):
		"""The proof verifies the exact report and Stock Ledger Entry report access."""
		report = frappe._dict(
			name="Batch-Wise Balance History",
			ref_doctype="Stock Ledger Entry",
			report_type="Script Report",
			is_standard="Yes",
			module="Stock",
			disabled=0,
			modified="2026-11-02 10:00:00",
		)
		with (
			patch("frappe.desk.query_report.get_report_doc", return_value=report),
			patch.object(frappe, "has_permission", return_value=True) as has_permission,
		):
			self.assertEqual(
				agent_scope._fefo_native_report_access(),
				{
					"name": "Batch-Wise Balance History",
					"ref_doctype": "Stock Ledger Entry",
					"report_type": "Script Report",
					"is_standard": "Yes",
					"module": "Stock",
					"modified": "2026-11-02 10:00:00",
				},
			)
		has_permission.assert_called_once_with("Stock Ledger Entry", "report", print_logs=False)
		for bad_report, report_permission in (
			({**report, "disabled": 1}, True),
			({**report, "report_type": "Query Report"}, True),
			({**report, "is_standard": "No"}, True),
			({**report, "module": "Accounts"}, True),
			(report, False),
		):
			with self.subTest(bad_report=bad_report, report_permission=report_permission):
				with (
					patch("frappe.desk.query_report.get_report_doc", return_value=frappe._dict(bad_report)),
					patch.object(frappe, "has_permission", return_value=report_permission),
					self.assertRaises(InvalidArgumentError),
				):
					agent_scope._fefo_native_report_access()
		with (
			patch("frappe.desk.query_report.get_report_doc", side_effect=frappe.PermissionError),
			self.assertRaises(InvalidArgumentError),
		):
			agent_scope._fefo_native_report_access()

	def test_visibility_proof_uses_canonical_parent_paths_without_child_false_positive(self):
		serial_entry = frappe._dict(istable=1)
		bundle = MagicMock()
		bundle.get.return_value = [
			frappe._dict(
				fieldname="entries", fieldtype="Table", options="Serial and Batch Entry", permlevel=0
			)
		]
		bundle.get_permlevel_access.return_value = [0]
		with patch.object(frappe, "has_permission", return_value=True) as permission:
			self.assertEqual(
				agent_scope._fefo_parent_path(
					serial_entry,
					bundle,
					"Serial and Batch Entry",
					"Serial and Batch Bundle",
					"fefo@example.invalid",
				),
				"table:entries",
			)
		permission.assert_called_once_with(
			"Serial and Batch Entry",
			"read",
			user="fefo@example.invalid",
			parent_doctype="Serial and Batch Bundle",
			print_logs=False,
		)

		balance = frappe._dict(
			istable=0,
			fields=[
				frappe._dict(fieldname="stock_closing_entry", fieldtype="Link", options="Stock Closing Entry")
			],
		)
		self.assertEqual(
			agent_scope._fefo_parent_path(
				balance,
				MagicMock(),
				"Stock Closing Balance",
				"Stock Closing Entry",
				"fefo@example.invalid",
			),
			"link:stock_closing_entry",
		)

	def test_visibility_proof_builds_for_normal_full_access_and_rejects_bad_proof(self):
		with (
			patch.object(agent_scope, "_fefo_refresh_access"),
			patch.object(agent_scope, "_fefo_role_access", return_value={"read": True, "select": True}),
			patch.object(frappe, "get_meta", return_value=MagicMock()),
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(agent_scope, "_fefo_warehouse_boundary", return_value=SCOPE["record_scope"]),
			patch.object(agent_scope, "_fefo_company_only_user_permissions", return_value=[]),
			patch.object(agent_scope, "_fefo_assert_no_record_slices"),
			patch.object(agent_scope, "_fefo_source_permissions", return_value=[]),
			patch.object(agent_scope, "_fefo_query_allowlist", return_value={"mode": "unset"}),
			patch.object(agent_scope, "_fefo_canonical_query_access", return_value=[]),
			patch.object(
				agent_scope, "_fefo_native_report_access", return_value={"name": "Batch-Wise Balance History"}
			),
			patch.object(agent_scope, "_fefo_native_report_fields", return_value=[]),
			patch.object(frappe, "get_roles", return_value=["Stock User"]),
			patch.object(frappe, "get_system_settings", return_value=1),
		):
			proof = agent_scope.fefo_visibility_proof(SCOPE)
		self.assertEqual(proof["schema"], PROOF["schema"])
		self.assertTrue(proof["complete"])
		self.assertRegex(proof["policy_sha256"], r"^[0-9a-f]{64}$")
		self.assertIsNone(agent_scope._fefo_visibility_proof_value(None))
		self.assertIsNone(agent_scope._fefo_visibility_proof_value("not-json"))
		self.assertIsNone(agent_scope._fefo_visibility_proof_value({**PROOF, "extra": True}))

		with (
			patch("jarvis._session.impersonate", return_value=nullcontext()),
			patch.object(agent_scope, "_fefo_warehouse_boundary", return_value=SCOPE["record_scope"]),
			patch.object(agent_scope, "fefo_visibility_proof", return_value=PROOF),
		):
			self.assertIsNone(agent_scope.fefo_stamped_scope_as_user(SCOPE, "fefo@example.invalid", None))
			self.assertIsNone(
				agent_scope.fefo_stamped_scope_as_user(SCOPE, "fefo@example.invalid", {"bad": True})
			)
			self.assertIsNone(
				agent_scope.fefo_stamped_scope_as_user(
					SCOPE,
					"fefo@example.invalid",
					{**PROOF, "policy_sha256": "b" * 64},
				)
			)

	def test_visibility_proof_digest_rechecks_the_report_and_query_allowlist(self):
		"""An executable-source policy drift invalidates the opaque run proof."""
		allowlist = {"mode": "configured", "required": ["Item"], "sha256": "a" * 64}
		report = {"name": "Batch-Wise Balance History", "modified": "2026-11-02 10:00:00"}
		with (
			patch.object(agent_scope, "_fefo_refresh_access"),
			patch.object(agent_scope, "_fefo_role_access", return_value={"read": True, "select": True}),
			patch.object(frappe, "get_meta", return_value=MagicMock()),
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(agent_scope, "_fefo_warehouse_boundary", return_value=SCOPE["record_scope"]),
			patch.object(agent_scope, "_fefo_company_only_user_permissions", return_value=[]),
			patch.object(agent_scope, "_fefo_assert_no_record_slices"),
			patch.object(agent_scope, "_fefo_source_permissions", return_value=[]),
			patch.object(agent_scope, "_fefo_query_allowlist", return_value=allowlist) as current_allowlist,
			patch.object(agent_scope, "_fefo_canonical_query_access", return_value=[]),
			patch.object(agent_scope, "_fefo_native_report_access", return_value=report) as current_report,
			patch.object(agent_scope, "_fefo_native_report_fields", return_value=[]),
			patch.object(frappe, "get_roles", return_value=["Stock User"]),
			patch.object(frappe, "get_system_settings", return_value=1),
		):
			first = agent_scope.fefo_visibility_proof(SCOPE)
			current_allowlist.return_value = {**allowlist, "sha256": "b" * 64}
			second = agent_scope.fefo_visibility_proof(SCOPE)
			current_report.return_value = {**report, "modified": "2026-11-02 10:01:00"}
			third = agent_scope.fefo_visibility_proof(SCOPE)
		self.assertNotEqual(first, second)
		self.assertNotEqual(second, third)

	def test_stamp_recomputes_the_server_only_proof_without_putting_it_in_scope(self):
		stamped = {key: value for key, value in SCOPE.items()}
		values = {}
		with (
			patch.object(operator_review, "backend", return_value=None),
			patch.object(agent_scope, "fefo_visibility_proof", return_value=PROOF) as proof,
			patch.object(agent_scheduler, "_permission_profile", return_value="{}"),
			patch.object(frappe.db, "sql", return_value=[frappe._dict(n=0, m=None)]),
			patch.object(
				frappe.db, "set_value", side_effect=lambda _dt, _name, data, **_kw: values.update(data)
			),
		):
			result = agent_scheduler._stamp_scope_and_watermark(
				"FEFO-RUN",
				frappe._dict(agent=fefo.AGENT, config=frappe.as_json(POLICY)),
				"fefo@example.invalid",
				resolved_scope=stamped,
				record_scope_supported=True,
			)
		self.assertEqual(result, stamped)
		proof.assert_called_once_with(stamped)
		self.assertEqual(frappe.parse_json(values["fefo_visibility_proof_json"]), PROOF)
		self.assertNotIn("fefo_visibility_proof", frappe.parse_json(values["scope_json"]))

	def test_visibility_failure_blocks_dashboard_canvas_and_later_media_before_persistence(self):
		run_row = frappe._dict(
			name="FEFO-RUN",
			status="running",
			installation="FEFO-INSTALLATION",
			dashboard=None,
		)
		with (
			patch("jarvis.tools._save_guards.require_session_key", return_value="agent:fefo:FEFO-RUN"),
			patch.object(save_dashboard_tool.frappe.db, "get_value", return_value=run_row),
			patch.object(
				save_dashboard_tool.frappe,
				"get_doc",
				side_effect=(
					frappe._dict(name="FEFO-INSTALLATION"),
					frappe._dict(name="FEFO-RUN", agent=fefo.AGENT),
				),
			),
			patch.object(
				agent_scope,
				"assert_fefo_run_artifact_scope",
				side_effect=InvalidArgumentError("proof unavailable"),
			),
			patch.object(agent_runs, "persist_agent_dashboard") as persist_dashboard,
			self.assertRaises(InvalidArgumentError),
		):
			save_dashboard_tool.save_agent_dashboard("<html><body>unsafe</body></html>")
		persist_dashboard.assert_not_called()

		with (
			patch.object(
				agent_scope,
				"assert_fefo_message_artifact_scope",
				side_effect=InvalidArgumentError("proof unavailable"),
			),
			patch.object(canvas, "fetch_canvas") as fetch_canvas,
			self.assertRaises(InvalidArgumentError),
		):
			canvas.persist_canvases("FEFO-MSG", "canvas/unsafe.html", "ws://127.0.0.1:1", "token")
		fetch_canvas.assert_not_called()

		with (
			patch.object(
				agent_scope,
				"assert_fefo_message_artifact_scope",
				side_effect=InvalidArgumentError("proof unavailable"),
			),
			patch.object(canvas, "persist_canvases") as persist_canvas,
			patch.object(frappe, "log_error"),
		):
			turn_handler.persist_rich_outputs(
				"FEFO-MSG",
				"FEFO-CONVERSATION",
				"fefo@example.invalid",
				"FEFO-RUN",
				0,
				media_rels=["/tmp/x.png"],
			)
		persist_canvas.assert_not_called()

	def test_invalid_proof_cleanup_deletes_only_proven_private_canvas_files(self):
		run_doc = frappe._dict(
			name="FEFO-RUN",
			agent=fefo.AGENT,
			dashboard="FEFO-DASHBOARD",
			conversation="FEFO-CONVERSATION",
		)
		message = frappe._dict(
			name="FEFO-MSG",
			owner="fefo@example.invalid",
			canvas=frappe.as_json([{"file_url": "/private/files/fefo.png"}]),
		)
		file_doc = frappe._dict(
			name="FEFO-FILE",
			file_url="/private/files/fefo.png",
			is_private=1,
			attached_to_doctype="Jarvis Chat Message",
			attached_to_name="FEFO-MSG",
			owner="fefo@example.invalid",
		)
		dashboard = frappe._dict(
			name="FEFO-DASHBOARD",
			source_conversation="FEFO-CONVERSATION",
			owner="fefo@example.invalid",
			target_user="fefo@example.invalid",
		)
		with (
			patch.object(agent_scope, "fefo_run_visibility_is_current", return_value=False),
			patch.object(agent_runs, "_fefo_dedicated_conversation", return_value="FEFO-CONVERSATION"),
			patch.object(agent_runs, "_fefo_canvas_messages", return_value=[message]),
			patch.object(agent_runs.frappe.db, "get_value", return_value=dashboard),
			patch.object(agent_runs.frappe, "get_all", return_value=[file_doc]),
			patch.object(agent_runs.frappe, "delete_doc") as delete_doc,
			patch.object(agent_runs.frappe.db, "set_value") as set_value,
		):
			agent_runs.clear_untrusted_fefo_artifacts(
				run_doc,
				frappe._dict(owner="fefo@example.invalid", reviewer="fefo@example.invalid"),
				proof_must_be_invalid=True,
			)
		self.assertEqual(
			delete_doc.call_args_list,
			[
				((agent_runs.DASHBOARD, "FEFO-DASHBOARD"), {"ignore_permissions": True, "force": True}),
				(("File", "FEFO-FILE"), {"ignore_permissions": True, "force": True}),
			],
		)
		self.assertIn(
			(("Jarvis Chat Message", "FEFO-MSG", "canvas", None), {"update_modified": False}),
			set_value.call_args_list,
		)

	def test_cleanup_refuses_current_proof_but_terminalizes_when_one_file_is_unproven(self):
		run_doc = frappe._dict(name="FEFO-RUN", agent=fefo.AGENT, conversation="FEFO-CONVERSATION")
		with (
			patch.object(agent_scope, "fefo_run_visibility_is_current", return_value=True),
			patch.object(agent_runs.frappe, "delete_doc") as delete_doc,
			self.assertRaises(frappe.ValidationError),
		):
			agent_runs.clear_untrusted_fefo_artifacts(
				run_doc,
				frappe._dict(owner="fefo@example.invalid", reviewer="fefo@example.invalid"),
				proof_must_be_invalid=True,
			)
		delete_doc.assert_not_called()

		unsafe = frappe._dict(
			name="UNSAFE-FILE",
			file_url="/private/files/fefo.png",
			is_private=0,
			attached_to_doctype="Jarvis Chat Message",
			attached_to_name="FEFO-MSG",
			owner="fefo@example.invalid",
		)
		safe = frappe._dict(
			name="SAFE-FILE",
			file_url="/private/files/safe.png",
			is_private=1,
			attached_to_doctype="Jarvis Chat Message",
			attached_to_name="FEFO-MSG",
			owner="fefo@example.invalid",
		)
		dashboard = frappe._dict(
			name="FEFO-DASHBOARD",
			source_conversation="FEFO-CONVERSATION",
			owner="fefo@example.invalid",
			target_user="fefo@example.invalid",
		)
		rejected = frappe._dict(
			name="FEFO-RUN",
			agent=fefo.AGENT,
			conversation="FEFO-CONVERSATION",
			dashboard="FEFO-DASHBOARD",
			assessment_output_json="unsafe",
			canvas_ref="unsafe",
			integrity_digest="unsafe",
		)
		with (
			patch.object(agent_scope, "fefo_run_visibility_is_current", return_value=False),
			patch.object(agent_runs, "_fefo_dedicated_conversation", return_value="FEFO-CONVERSATION"),
			patch.object(
				agent_runs,
				"_fefo_canvas_messages",
				return_value=[
					frappe._dict(
						name="FEFO-MSG",
						owner="fefo@example.invalid",
						canvas=frappe.as_json(
							[
								{"file_url": "/private/files/fefo.png"},
								{"file_url": "/private/files/safe.png"},
							]
						),
					)
				],
			),
			patch.object(agent_runs.frappe.db, "get_value", return_value=dashboard),
			patch.object(agent_runs.frappe, "get_all", return_value=[unsafe, safe]),
			patch.object(agent_runs.frappe, "delete_doc") as delete_doc,
			patch.object(agent_runs.frappe.db, "set_value") as set_value,
			patch.object(agent_runs.frappe, "log_error") as log_error,
		):
			agent_runs.clear_untrusted_fefo_artifacts(
				rejected,
				frappe._dict(owner="fefo@example.invalid", reviewer="fefo@example.invalid"),
				proof_must_be_invalid=True,
			)
		self.assertEqual(
			delete_doc.call_args_list,
			[
				((agent_runs.DASHBOARD, "FEFO-DASHBOARD"), {"ignore_permissions": True, "force": True}),
				(("File", "SAFE-FILE"), {"ignore_permissions": True, "force": True}),
			],
		)
		self.assertTrue(log_error.called)
		self.assertIn(
			(("Jarvis Chat Message", "FEFO-MSG", "canvas", None), {"update_modified": False}),
			set_value.call_args_list,
		)
		self.assertIn(
			(
				(
					agent_runs.RUN,
					"FEFO-RUN",
					{
						"assessment_output_json": None,
						"canvas_ref": None,
						"integrity_digest": None,
						"dashboard": None,
					},
				),
				{"update_modified": False},
			),
			set_value.call_args_list,
		)
		self.assertIsNone(rejected.dashboard)
		self.assertIsNone(rejected.assessment_output_json)
