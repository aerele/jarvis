"""Fail-closed boundaries for the FEFO expiry-compliance replacement."""

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_runs, agent_scheduler, agent_scope, agents_api, operator_review
from jarvis.chat import fefo_expiry_compliance as fefo
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import query, record_agent_run, run_report

SCOPE = {
	"company": "FEFO Scope Company",
	"from_date": "2026-10-01",
	"to_date": "2026-10-31",
	"report_date": "2026-10-31",
	"fefo_scope_contract": "fefo-expiry-compliance-v0.4.1",
	"record_scope": {"doctype": "Warehouse", "name": "FEFO Leaf"},
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
		):
			self.assertEqual(
				agent_scope.fefo_stamped_scope_as_user(SCOPE, "fefo@example.invalid"),
				agent_scope.fefo_output_scope(SCOPE),
			)
		boundary.assert_called_once_with(SCOPE["record_scope"], SCOPE["company"])

		for bad in (
			{**SCOPE, "from_date": "2026-11-01"},
			{**SCOPE, "to_date": "2026-10-32"},
			{**SCOPE, "report_date": "2026-10-31T00:00:00"},
		):
			with self.subTest(bad=bad), patch.object(agent_scope, "_fefo_warehouse_boundary") as boundary:
				self.assertIsNone(agent_scope.fefo_stamped_scope_as_user(bad, "fefo@example.invalid"))
				boundary.assert_not_called()

	def test_legacy_scope_cannot_become_a_current_stamped_scope(self):
		legacy = {key: value for key, value in SCOPE.items() if key != "fefo_scope_contract"}
		self.assertEqual(agent_scope.fefo_output_scope(legacy), agent_scope.fefo_output_scope(SCOPE))
		self.assertIsNone(agent_scope.fefo_stamped_scope(legacy))
		self.assertEqual(agent_scope.fefo_stamped_scope(SCOPE), agent_scope.fefo_output_scope(SCOPE))

	def test_launch_scope_stamps_and_delivers_the_server_owned_contract_marker(self):
		installation = frappe._dict(agent=fefo.AGENT, config=frappe.as_json(POLICY))
		resolved = {
			"company": SCOPE["company"],
			"from_date": SCOPE["from_date"],
			"to_date": SCOPE["to_date"],
			"report_date": SCOPE["report_date"],
		}
		with patch.object(agent_scope, "_resolve", return_value=resolved):
			stamped = agent_scope.resolve_scope(installation, allow_record_scope=True)
		self.assertEqual(stamped["fefo_scope_contract"], SCOPE["fefo_scope_contract"])
		prompt = agent_scheduler._audit_prompt(
			frappe._dict(agent_slug=fefo.AGENT),
			installation,
			"manual",
			stamped,
		)
		self.assertIn('fefo_scope_contract="fefo-expiry-compliance-v0.4.1"', prompt)

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
		cleanup.assert_called_once_with(run_doc, inst)

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
		cleanup.assert_called_once_with(run_doc, inst)

	def test_legacy_valid_looking_scope_cannot_auto_resolve_a_new_fefo_run(self):
		class ReachedRunFinalization(Exception):
			pass

		current_scope = {key: value for key, value in SCOPE.items() if key != "record_scope"}
		legacy_scope = {key: value for key, value in current_scope.items() if key != "fefo_scope_contract"}
		run_doc = frappe._dict(name="FEFO-CURRENT", agent=fefo.AGENT, wm_row_count=None)
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
