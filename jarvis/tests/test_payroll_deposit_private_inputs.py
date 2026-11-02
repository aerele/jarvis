"""Regression tests for the private payroll Deposit-Status input contract."""

import json
import threading
import time
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.chat import payroll_deposit_snapshot as snapshot
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _delegate_capability, _private_output

COMPANY = "Private Deposit Co"
ACCOUNT_GROUP = "Payroll Liabilities - PD"
PF_ACCOUNT = "PF Liability - PD"
ESI_ACCOUNT = "ESI Liability - PD"
SCOPE = {
	"company": COMPANY,
	"fiscal_year": None,
	"from_date": "2024-04-01",
	"to_date": "2024-04-30",
	"report_date": "2024-05-31",
	"prior_fy_start": "2023-04-01",
	"prior_fy_end": "2024-03-31",
	"record_scope": {"doctype": "Account", "name": ACCOUNT_GROUP},
}
DEPOSIT = {
	"account_group": ACCOUNT_GROUP,
	"liability_accounts": {PF_ACCOUNT: "pf", ESI_ACCOUNT: "esi"},
	"vouchers": [{"doctype": "Journal Entry", "name": "JE-DEP-TEST", "liability_month": "2024-04"}],
	"settlement_as_of": "2024-05-31",
	"reviewer": "reviewer@example.test",
	"reviewer_capacity": True,
}


def _launch() -> dict:
	return snapshot.launch_config(
		frappe._dict(
			config=json.dumps(
				{
					"company": COMPANY,
					"from_date": SCOPE["from_date"],
					"to_date": SCOPE["to_date"],
					"deposit": DEPOSIT,
				}
			)
		)
	)


def _receipt(launch: dict, source_snapshot=None) -> dict:
	return {
		"contract": snapshot.RECEIPT_CONTRACT,
		"launch": {
			"scope": deepcopy(SCOPE),
			"scope_digest": snapshot.canonical_digest(SCOPE),
			"config_digest": launch["config_digest"],
			"scope_config_digest": launch["scope_config_digest"],
		},
		"source_snapshot": source_snapshot,
	}


def _run(launch=None, source_snapshot=None, dashboard=None) -> frappe._dict:
	launch = launch or _launch()
	return frappe._dict(
		{
			"name": "DEP-PRIVATE-RUN",
			"agent": snapshot.AGENT,
			"status": "running",
			"dashboard": dashboard,
			"session_key": "agent:agent-statutory-payroll-deposit-status:DEP-PRIVATE-RUN",
			"assessment_config_json": json.dumps(launch),
			"scope_json": json.dumps(SCOPE),
			"input_snapshot_json": json.dumps(_receipt(launch, source_snapshot)),
		}
	)


def _sources() -> dict:
	return {
		"accounts": [
			{
				"name": ACCOUNT_GROUP,
				"company": COMPANY,
				"parent_account": None,
				"is_group": 1,
				"disabled": 0,
				"account_currency": "INR",
			},
			{
				"name": PF_ACCOUNT,
				"company": COMPANY,
				"parent_account": ACCOUNT_GROUP,
				"is_group": 0,
				"disabled": 0,
				"account_currency": "INR",
			},
			{
				"name": ESI_ACCOUNT,
				"company": COMPANY,
				"parent_account": ACCOUNT_GROUP,
				"is_group": 0,
				"disabled": 0,
				"account_currency": "INR",
			},
		],
		"company": [{"name": COMPANY, "default_currency": "INR"}],
		"journal_entries": [
			{
				"name": "JE-DEP-TEST",
				"company": COMPANY,
				"posting_date": "2024-05-20",
				"docstatus": 1,
				"voucher_type": "Journal Entry",
				"is_opening": "No",
				"multi_currency": 0,
				"reversal_of": None,
				"reference_present": 1,
			}
		],
		"journal_lines": [
			{
				"name": "SECRET-PRIVATE-ROW",
				"parent": "JE-DEP-TEST",
				"parenttype": "Journal Entry",
				"account": PF_ACCOUNT,
				"account_currency": "INR",
				"debit": 1,
				"credit": 0,
			}
		],
		"journal_reversals": [],
		"payment_entries": [],
		"payment_deductions": [],
		"payment_taxes": [],
	}


def _captured_snapshot() -> tuple[dict, object]:
	launch = _launch()
	run = _run(launch)
	sources = _sources()
	counts = []
	for key in snapshot.SOURCE_ORDER:
		checkpoint = {"count": len(sources[key]), "modified": "2026-11-02 10:00:00"}
		counts.extend([checkpoint, deepcopy(checkpoint)])
	with (
		patch.object(snapshot, "_count_watermark", side_effect=counts),
		patch.object(snapshot, "_source_rows", side_effect=lambda key, _spec, _count: deepcopy(sources[key])),
		patch.object(frappe.db, "set_value") as set_value,
	):
		captured = snapshot.capture(run)
	return captured, set_value


def _ensure_run_as_user(email: str) -> str:
	"""Create the non-Administrator identity used by the source-query test."""
	if not frappe.db.exists("User", email):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Payroll Deposit Run As",
				"send_welcome_email": 0,
			}
		)
		user.flags.ignore_permissions = True
		user.insert(ignore_permissions=True)
	return email


def _v2_output(captured: dict, **overrides) -> dict:
	"""The exact seven-field compact handoff shape accepted from the v3 bundle."""
	value = {
		"findings": [
			{
				"token": "pf_esi_deposit_due_date",
				"ref_doctype": "Journal Entry",
				"ref_name": "JE-DEP-TEST",
			}
		],
		"coverage": {
			"pf_esi_deposit_due_date": {"state": "evaluated"},
			"s7q_interest_table": {"state": "evaluated"},
			"s14b_damages_table": {"state": "evaluated"},
		},
		"scope": deepcopy(SCOPE),
		"truncated": False,
		"rows_consumed": sum(row["data"]["rows"] for row in captured["source_receipts"]),
		"input_snapshot_id": captured["snapshot_id"],
		"input_snapshot_digest": captured["snapshot_digest"],
	}
	value.update(overrides)
	return value


class TestPayrollDepositPrivateSnapshot(TestCase):
	def test_server_source_plan_has_all_eight_fixed_sources(self):
		plan = snapshot.source_specs(SCOPE, _launch()["config"])
		self.assertEqual(set(plan), set(snapshot.SOURCE_ORDER))
		self.assertEqual(
			{key: spec["alias"] for key, spec in plan.items()},
			{
				"accounts": "a",
				"company": "c",
				"journal_entries": "j",
				"journal_lines": "l",
				"journal_reversals": "j",
				"payment_entries": "p",
				"payment_deductions": "d",
				"payment_taxes": "d",
			},
		)
		self.assertEqual(plan["payment_entries"]["from"], "Payment Entry")
		self.assertEqual(plan["payment_entries"]["select"][0], "p.name")
		self.assertEqual(plan["payment_entries"]["limit"], snapshot.SOURCE_CAP)

	def test_server_source_plan_compiles_before_the_private_read(self):
		from jarvis.tools import query as query_tool

		previous_user = frappe.session.user
		try:
			frappe.set_user("Administrator")
			for spec in snapshot.source_specs(SCOPE, _launch()["config"]).values():
				query_tool.compile_spec(spec)
		finally:
			frappe.set_user(previous_user)

	def test_private_server_query_keeps_a_complete_200_row_source_over_model_budget(self):
		"""The server source path must not inherit the model result-byte cap."""
		from jarvis.tools import query as query_tool

		rows = [{"name": f"COMP-{n}-{'x' * 220}", "default_currency": "INR"} for n in range(200)]

		class Query:
			def limit(self, value):
				self.limit_value = value
				return self

			def run(self, *, as_dict):
				if not as_dict:
					raise AssertionError("private query must request dict rows")
				return deepcopy(rows)

			def get_sql(self):
				return "select name, default_currency from tabCompany"

		query = Query()
		spec = {"from": "Company", "select": ["name", "default_currency"], "limit": 200}
		with (
			patch.object(query_tool, "compile_spec", return_value=query),
			patch.object(query_tool, "_masking_for_rows", return_value=([], {})),
			patch.object(query_tool.masking, "mask_rows"),
			patch("jarvis.tools._export.saved_queries.save", return_value="SAVED-QUERY"),
		):
			public = query_tool.query(spec, confirm_large=True)
			with _private_output.request(True):
				private = query_tool._query_private_server(spec, confirm_large=True)
				captured = snapshot._source_rows("company", spec, 200)

		self.assertTrue(public["partial"])
		self.assertEqual(len(public["rows"]), 200)
		self.assertNotIn("partial", private)
		self.assertNotIn("more_rows", private)
		self.assertEqual(private["rows"], rows)
		self.assertEqual(captured, rows)
		with self.assertRaises(InvalidArgumentError):
			query_tool._query_private_server(spec, confirm_large=True)

	def test_launch_config_does_not_follow_later_installation_mutation(self):
		launch = _launch()
		mutated = frappe._dict(
			config=json.dumps(
				{
					"company": "Other Company",
					"from_date": "2024-05-01",
					"to_date": "2024-05-31",
					"deposit": {**DEPOSIT, "account_group": "Other Account Group"},
				}
			)
		)
		self.assertNotEqual(snapshot.launch_config(mutated), launch)
		specs = snapshot.source_specs(SCOPE, launch["config"])
		self.assertIn(ACCOUNT_GROUP, snapshot.canonical_json(specs))
		self.assertNotIn("Other Account Group", snapshot.canonical_json(specs))

	def test_cross_company_or_account_scope_is_refused_before_source_plan(self):
		with self.assertRaises(InvalidArgumentError):
			snapshot.source_specs(
				{**SCOPE, "record_scope": {"doctype": "Account", "name": "Other Account Group"}},
				_launch()["config"],
			)
		rows = _sources()["accounts"]
		rows[0]["company"] = "Other Company"
		with self.assertRaises(InvalidArgumentError):
			snapshot._validate_account_rows(rows, SCOPE, _launch()["config"])

	def test_partial_source_and_source_drift_fail_closed(self):
		spec = snapshot.source_specs(SCOPE, _launch()["config"])["company"]
		with self.assertRaises(InvalidArgumentError):
			snapshot._source_rows("company", spec, snapshot.SOURCE_CAP + 1)
		with patch.object(
			snapshot.query_tool, "_query_private_server", return_value={"rows": [], "more_rows": True}
		):
			with self.assertRaises(InvalidArgumentError):
				snapshot._source_rows("company", spec, 0)

		run = _run()
		with (
			patch.object(
				snapshot,
				"_count_watermark",
				side_effect=[{"count": 0, "modified": "before"}, {"count": 0, "modified": "after"}],
			),
			patch.object(snapshot, "_source_rows", return_value=[]),
			self.assertRaises(InvalidArgumentError),
		):
			snapshot.capture(run)

	def test_missing_launch_configuration_fails_closed_before_any_source_read(self):
		run = _run()
		run.assessment_config_json = None
		with (
			patch.object(snapshot.query_tool, "_query_private_server") as query,
			self.assertRaises(InvalidArgumentError),
		):
			snapshot.capture(run)
		query.assert_not_called()

	def test_canonical_private_snapshot_keeps_sources_private_from_run_and_receipts(self):
		captured, set_value = _captured_snapshot()
		self.assertEqual(captured["contract"], snapshot.CONTRACT)
		canonical = snapshot.canonical_json(captured)
		decoded = json.loads(canonical)
		self.assertEqual(set(decoded["sources"]), set(snapshot.SOURCE_ORDER))
		self.assertEqual([row["source"] for row in decoded["source_receipts"]], list(snapshot.SOURCE_ORDER))
		self.assertIn("SECRET-PRIVATE-ROW", canonical)

		stored = set_value.call_args.args[3]
		self.assertNotIn("SECRET-PRIVATE-ROW", stored)
		self.assertNotIn('"sources"', stored)
		wire, receipt = _private_output.prepare(
			{"ok": True, "data": captured}, tool="get_payroll_deposit_inputs"
		)
		self.assertEqual(wire["result_mode"], _private_output.MODE)
		self.assertEqual(json.loads(wire["data"])["contract"], snapshot.CONTRACT)
		self.assertNotIn("SECRET-PRIVATE-ROW", json.dumps(receipt))
		self.assertNotIn("SECRET-PRIVATE-ROW", json.dumps(receipt["data"]))

	def test_v3_writeback_accepts_only_a_compact_server_projected_shape(self):
		captured, _ = _captured_snapshot()
		run = _run(source_snapshot=snapshot._metadata(captured))
		projected = snapshot.validate_writeback(run, **_v2_output(captured))
		self.assertEqual(projected["scope"], SCOPE)
		self.assertEqual(projected["findings"][0]["severity"], "warning")
		self.assertEqual(projected["findings"][0]["result_class"], "derived_candidate")
		self.assertNotIn("SECRET-PRIVATE-ROW", json.dumps(projected))
		self.assertRegex(projected["integrity_digest"], r"^[0-9a-f]{64}$")

	def test_server_digest_is_stable_and_rejects_delegate_digest(self):
		captured, _ = _captured_snapshot()
		run = _run(source_snapshot=snapshot._metadata(captured))
		v3 = _v2_output(captured)
		first = snapshot.validate_writeback(run, **v3)
		second = snapshot.validate_writeback(run, **deepcopy(v3))
		hex_sentinel = "73" * 32
		for supplied_digest in (None, hex_sentinel):
			with self.subTest(supplied_digest=supplied_digest), self.assertRaises(InvalidArgumentError):
				snapshot.validate_writeback(
					run,
					**_v2_output(captured, integrity_digest=supplied_digest),
				)
		changed = snapshot.validate_writeback(
			run,
			**_v2_output(captured, findings=[]),
		)
		coverage_changed = snapshot.validate_writeback(
			run,
			**_v2_output(
				captured,
				coverage={
					**_v2_output(captured)["coverage"],
					"s7q_interest_table": {
						"state": "not_evaluable",
						"reason_code": "external_evidence_absent",
					},
				},
				truncated=True,
			),
		)
		self.assertEqual(
			set(v3),
			{
				"findings",
				"coverage",
				"scope",
				"truncated",
				"rows_consumed",
				"input_snapshot_id",
				"input_snapshot_digest",
			},
		)
		self.assertEqual(first["integrity_digest"], second["integrity_digest"])
		self.assertNotEqual(first["integrity_digest"], changed["integrity_digest"])
		self.assertNotEqual(first["integrity_digest"], coverage_changed["integrity_digest"])
		self.assertNotIn(hex_sentinel, json.dumps(first))

	def test_v3_writeback_refuses_scope_snapshot_and_row_count_changes(self):
		captured, _ = _captured_snapshot()
		run = _run(source_snapshot=snapshot._metadata(captured))
		for changed in (
			{"scope": {**SCOPE, "company": "Other Company"}},
			{"input_snapshot_id": "other"},
			{"rows_consumed": 0},
			{"integrity_digest": "a" * 64},
			{
				"coverage": {
					**_v2_output(captured)["coverage"],
					"pf_esi_deposit_due_date": {"state": []},
				}
			},
		):
			with self.subTest(changed=changed), self.assertRaises(InvalidArgumentError):
				snapshot.validate_writeback(run, **_v2_output(captured, **changed))

	def test_v3_writeback_refuses_every_delegate_free_text_route(self):
		captured, _ = _captured_snapshot()
		run = _run(source_snapshot=snapshot._metadata(captured))
		sentinel = "PRIVATE-SOURCE-SENTINEL"
		bad_finding = _v2_output(captured)
		bad_finding["findings"][0]["note"] = sentinel
		bad_coverage = _v2_output(captured)
		bad_coverage["coverage"]["pf_esi_deposit_due_date"]["detail"] = sentinel
		for bad in (
			bad_finding,
			bad_coverage,
			_v2_output(captured, dashboard=sentinel),
			_v2_output(captured, dashboard=None),
			_v2_output(captured, report_sha256=sentinel),
			_v2_output(captured, report_sha256=None),
			_v2_output(captured, canvas_ref=sentinel),
			_v2_output(captured, canvas_ref=None),
		):
			with self.subTest(bad=bad), self.assertRaises(InvalidArgumentError):
				snapshot.validate_writeback(run, **bad)

	def test_v1_report_bound_receipts_fail_closed(self):
		captured, _ = _captured_snapshot()
		run = _run(source_snapshot=snapshot._metadata(captured))
		legacy = json.loads(run.input_snapshot_json)
		legacy["contract"] = "payroll_deposit_snapshot_receipt_v1"
		legacy["report"] = {"dashboard": "old", "report_sha256": "a" * 64}
		run.input_snapshot_json = json.dumps(legacy)
		with self.assertRaises(InvalidArgumentError):
			snapshot.validate_writeback(run, **_v2_output(captured))

	def test_deposit_dashboard_save_is_refused_before_any_html_persistence(self):
		from jarvis.tools import _agent_run_ctx
		from jarvis.tools import save_agent_dashboard as tool

		run_row = frappe._dict(
			name="DEP-PRIVATE-RUN",
			agent=snapshot.AGENT,
			status="running",
			installation="DEP-PRIVATE-INSTALL",
			dashboard=None,
		)
		_agent_run_ctx.set_session_key("private-dashboard-session")
		try:
			with (
				patch.object(frappe.db, "get_value", return_value=run_row),
				patch.object(frappe, "get_doc") as get_doc,
			):
				for kwargs in (
					{"html": "<html>PRIVATE-SOURCE-SENTINEL</html>"},
					{
						"html": "<html>safe</html>",
						"title": "PRIVATE-SOURCE-SENTINEL",
						"description": "PRIVATE-SOURCE-SENTINEL",
					},
				):
					with self.subTest(kwargs=kwargs), self.assertRaises(InvalidArgumentError):
						tool.save_agent_dashboard(**kwargs)
		finally:
			_agent_run_ctx.clear_session_key()
		get_doc.assert_not_called()


class TestPayrollDepositPrivateTransport(TestCase):
	def test_ordinary_and_shared_requests_are_refused_at_the_api_boundary(self):
		from jarvis import api

		frappe.set_user("Administrator")
		ordinary = api._call_tool("get_payroll_deposit_inputs", {}, result_mode=_private_output.MODE)
		self.assertFalse(ordinary["ok"])
		self.assertEqual(ordinary["error"]["code"], "PermissionError")

		def db_value(doctype, *_args, **_kwargs):
			if doctype == "User":
				return 1
			if doctype == "Jarvis Chat Session":
				return ""
			return None

		with (
			patch.object(
				api, "_get_header", side_effect=lambda name: "token" if name == "X-Jarvis-Token" else None
			),
			patch.object(api, "_request_body_bytes", return_value=b"{}"),
			patch.object(api, "validate_plugin_request", return_value="shared-private-session"),
			patch("jarvis.chat.site_pin.pin_if_unset"),
			patch("jarvis.chat.collaboration.is_collaborative_session", return_value=True),
			patch(
				"jarvis.chat.collaboration.resolve_binding", return_value=frappe._dict(actor="Administrator")
			),
			patch.object(frappe.db, "get_value", side_effect=db_value),
			patch.object(frappe.db, "get_single_value", return_value=""),
			patch("jarvis.chat.device.session_device_is_stale", return_value=False),
		):
			shared = api._call_tool("get_payroll_deposit_inputs", {}, result_mode=_private_output.MODE)
		self.assertFalse(shared["ok"])
		self.assertEqual(shared["error"]["code"], "PermissionError")

	def test_private_zero_argument_transport_is_the_only_source_path(self):
		with self.assertRaises(InvalidArgumentError):
			_private_output.require_source_transport("get_payroll_deposit_inputs")
		with self.assertRaises(InvalidArgumentError):
			_private_output.validate_args("get_payroll_deposit_inputs", {"company": COMPANY})
		cap = {
			"run": "DEP-PRIVATE-RUN",
			"status": "running",
			"agent": snapshot.AGENT,
			"legacy": False,
			"tools_allow": ["jarvis__get_payroll_deposit_inputs"],
		}
		self.assertTrue(_private_output.allowed(_private_output.MODE, "get_payroll_deposit_inputs", cap))
		self.assertFalse(_private_output.allowed(_private_output.MODE, "query", cap))
		self.assertFalse(
			_private_output.allowed(
				_private_output.MODE, "get_payroll_deposit_inputs", {**cap, "agent": "other-agent"}
			)
		)
		self.assertIsNotNone(_delegate_capability.tool_denial(None, "query", cap=cap))

	def test_wrong_agent_or_session_user_is_refused_before_capture_or_query(self):
		from jarvis.tools import get_payroll_deposit_inputs as tool

		wrong_agent = {
			"run": "DEP-PRIVATE-RUN",
			"status": "running",
			"agent": "other-agent",
			"legacy": False,
		}
		with (
			_private_output.request(True),
			patch.object(tool, "get_session_key", return_value="private-session"),
			patch.object(tool._delegate_capability, "resolve", return_value=wrong_agent),
			patch.object(tool.payroll_deposit_snapshot, "capture") as capture,
			self.assertRaises(InvalidArgumentError),
		):
			tool.get_payroll_deposit_inputs()
		capture.assert_not_called()

		wrong_status = {**wrong_agent, "agent": snapshot.AGENT, "status": "completed"}
		with (
			_private_output.request(True),
			patch.object(tool, "get_session_key", return_value="private-session"),
			patch.object(tool._delegate_capability, "resolve", return_value=wrong_status),
			patch.object(tool.payroll_deposit_snapshot, "capture") as capture,
			self.assertRaises(InvalidArgumentError),
		):
			tool.get_payroll_deposit_inputs()
		capture.assert_not_called()

		correct_cap = {**wrong_agent, "agent": snapshot.AGENT}
		correct_cap["tools_allow"] = ["jarvis__get_payroll_deposit_inputs"]
		with (
			_private_output.request(True),
			patch.object(tool, "get_session_key", return_value="private-session"),
			patch.object(tool._delegate_capability, "resolve", return_value=correct_cap),
			patch.object(frappe.db, "get_value", return_value="different-user@example.test"),
			patch.object(tool.payroll_deposit_snapshot, "capture") as capture,
			self.assertRaises(InvalidArgumentError),
		):
			tool.get_payroll_deposit_inputs()
		capture.assert_not_called()

	def test_direct_source_capture_requires_the_exact_snapshotted_tool(self):
		from jarvis.tools import get_payroll_deposit_inputs as tool

		cap = {
			"run": "DEP-PRIVATE-RUN",
			"status": "running",
			"agent": snapshot.AGENT,
			"run_as": "run-as@example.test",
			"legacy": False,
			"tools_allow": ["get_payroll_deposit_inputs"],
		}
		with (
			_private_output.request(True),
			patch.object(tool, "get_session_key", return_value="private-session"),
			patch.object(tool._delegate_capability, "resolve", return_value=cap),
			patch.object(tool.payroll_deposit_snapshot, "capture") as capture,
			self.assertRaises(InvalidArgumentError),
		):
			tool.get_payroll_deposit_inputs()
		capture.assert_not_called()

	def test_fixed_sources_query_as_the_non_administrator_run_as_user(self):
		"""The source capture must not inherit a scheduler Administrator context."""
		from jarvis.tools import get_payroll_deposit_inputs as tool

		run_as = _ensure_run_as_user("payroll-deposit-runas@example.test")
		launch = _launch()
		run = _run(launch)
		sources = _sources()
		plan = snapshot.source_specs(SCOPE, launch["config"])
		data_specs = {snapshot.canonical_json(spec): key for key, spec in plan.items()}
		count_specs = {
			snapshot.canonical_json(snapshot._aggregate_spec(spec)): key for key, spec in plan.items()
		}
		queried_as = []

		def query(spec, *, confirm_large=False):
			self.assertTrue(confirm_large)
			queried_as.append(frappe.session.user)
			encoded = snapshot.canonical_json(spec)
			if encoded in count_specs:
				key = count_specs[encoded]
				return {"rows": [{"count": len(sources[key]), "modified": "2026-11-02 10:00:00"}]}
			if encoded in data_specs:
				return {"rows": deepcopy(sources[data_specs[encoded]])}
			raise AssertionError(f"unexpected private source query: {encoded}")

		def db_value(doctype, *_args, **_kwargs):
			if doctype == "Jarvis Chat Session":
				return run_as
			if doctype == "Jarvis Agent Run":
				return run
			raise AssertionError(f"unexpected private source database read: {doctype}")

		cap = {
			"run": run.name,
			"status": "running",
			"agent": snapshot.AGENT,
			"run_as": run_as,
			"legacy": False,
			"tools_allow": ["jarvis__get_payroll_deposit_inputs"],
		}
		try:
			frappe.set_user(run_as)
			with (
				_private_output.request(True),
				patch.object(tool, "get_session_key", return_value=run.session_key),
				patch.object(tool._delegate_capability, "resolve", return_value=cap),
				patch.object(frappe.db, "get_value", side_effect=db_value),
				patch.object(frappe.db, "set_value"),
				patch.object(snapshot.query_tool, "_query_private_server", side_effect=query),
			):
				captured = tool.get_payroll_deposit_inputs()
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(captured["contract"], snapshot.CONTRACT)
		self.assertEqual(queried_as, [run_as] * (len(snapshot.SOURCE_ORDER) * 3))


class TestPayrollDepositWritebackClaim(TestCase):
	def test_record_agent_run_claims_one_terminal_dashboard_across_connections(self):
		"""The real Deposit tool path admits one terminal writer across connections.

		The first worker pauses only after ``record_agent_run`` has acquired its
		``FOR UPDATE`` claim. Both workers then use the real v3 validator and the
		real fallback-dashboard persister. The loser must return the idempotent
		terminal response without creating a second report.
		"""
		from jarvis.chat import agent_runs
		from jarvis.tools import _agent_run_ctx
		from jarvis.tools import record_agent_run as tool

		captured, _ = _captured_snapshot()
		launch = _launch()
		run_as = _ensure_run_as_user("payroll-deposit-race@example.test")
		installation_name = None
		run_name = None
		created: list[str] = []
		try:
			installation = frappe.get_doc(
				{
					"doctype": "Jarvis Agent Installation",
					"agent": snapshot.AGENT,
					"run_as_user": run_as,
					"reviewer": run_as,
					"activation_state": "live",
				}
			)
			installation.owner = "Administrator"
			# The fixture needs a real persisted installation, but it deliberately
			# does not grant the fake run-as user every payroll DocType. The tool
			# path below carries no finding reference, so this avoids testing the
			# unrelated installation-permission preflight here.
			installation.flags.ignore_links = True
			installation.db_insert()
			installation_name = installation.name
			session_key = f"agent:{snapshot.AGENT}:deposit-race-{frappe.generate_hash(length=12)}"
			run = frappe.get_doc(
				{
					"doctype": "Jarvis Agent Run",
					"agent": snapshot.AGENT,
					"installation": installation_name,
					"trigger": "manual",
					"status": "running",
					"started_at": frappe.utils.now(),
					"session_key": session_key,
					"preparation_mode": "shadow",
					"assessment_config_json": json.dumps(launch),
					"scope_json": json.dumps(SCOPE),
					"input_snapshot_json": json.dumps(_receipt(launch, snapshot._metadata(captured))),
				}
			)
			run.owner = "Administrator"
			run.insert(ignore_permissions=True)
			run_name = run.name
			frappe.db.commit()

			site, sites_path = frappe.local.site, frappe.local.sites_path
			locked, release = threading.Event(), threading.Event()
			thread_state = threading.local()
			results, errors = [], []
			real_validate = snapshot.validate_writeback
			real_persist = agent_runs.persist_agent_dashboard

			def held_validate(*args, **kwargs):
				if getattr(thread_state, "is_first", False):
					locked.set()
					if not release.wait(10):
						raise TimeoutError("test did not release the claimed Deposit run")
				return real_validate(*args, **kwargs)

			def observed_persist(*args, **kwargs):
				name = real_persist(*args, **kwargs)
				created.append(name)
				return name

			def writeback(first: bool):
				try:
					frappe.init(site, sites_path=sites_path)
					frappe.connect()
					frappe.set_user(run_as)
					thread_state.is_first = first
					_agent_run_ctx.set_session_key(session_key)
					results.append(tool.record_agent_run(**_v2_output(captured, findings=[])))
					frappe.db.commit()
				except Exception as exc:
					errors.append(repr(exc))
				finally:
					_agent_run_ctx.clear_session_key()
					try:
						frappe.destroy()
					except Exception:
						pass

			with (
				patch.object(snapshot, "validate_writeback", side_effect=held_validate),
				patch.object(agent_runs, "persist_agent_dashboard", side_effect=observed_persist),
			):
				first = threading.Thread(target=writeback, args=(True,))
				second = threading.Thread(target=writeback, args=(False,))
				first.start()
				self.assertTrue(locked.wait(10), errors)
				second.start()
				time.sleep(0.1)
				release.set()
				first.join(15)
				second.join(15)

			self.assertFalse(first.is_alive())
			self.assertFalse(second.is_alive())
			self.assertEqual(errors, [])
			self.assertEqual(len([r for r in results if not r.get("idempotent")]), 1)
			self.assertEqual(len([r for r in results if r.get("idempotent")]), 1)
			final = frappe.db.get_value("Jarvis Agent Run", run_name, ["status", "dashboard"], as_dict=True)
			self.assertEqual(final.status, "completed")
			self.assertEqual(len(created), 1)
			self.assertEqual(final.dashboard, created[0])
			self.assertEqual(frappe.db.count("Jarvis Dashboard", {"name": final.dashboard}), 1)
		finally:
			# The test owns these fake rows. Raw cleanup is deliberate: it models the
			# trusted database retention path and avoids using an external permission
			# bypass on the immutable report controller.
			if run_name:
				frappe.db.delete("Jarvis Agent Run Step", {"run": run_name})
				frappe.db.delete("Jarvis Agent Activity", {"run": run_name})
				frappe.db.delete("Jarvis Agent Run", {"name": run_name})
			for name in created:
				frappe.db.delete("Jarvis Dashboard", {"name": name})
			if installation_name:
				frappe.db.delete("Jarvis Agent Installation", {"name": installation_name})
			frappe.db.commit()

	def test_real_db_row_lock_allows_one_deposit_finalizer_and_dashboard(self):
		"""Two MariaDB connections serialize one Deposit terminal writeback.

		This is intentionally a real connection test, not a mocked ``for_update``
		assertion.  The first worker holds the Run lock while the second starts;
		the second can only observe the terminal row and cannot create another
		fallback dashboard.
		"""
		run = frappe.get_doc(
			{
				"doctype": "Jarvis Agent Run",
				"agent": snapshot.AGENT,
				"status": "running",
				"owner": "Administrator",
			}
		).insert(ignore_permissions=True, ignore_links=True)
		frappe.db.commit()
		site, sites_path, run_name = frappe.local.site, frappe.local.sites_path, run.name
		locked, release = threading.Event(), threading.Event()
		outcomes, errors, created = [], [], []

		def finalizer(first: bool):
			try:
				frappe.init(site, sites_path=sites_path)
				frappe.connect()
				row = frappe.db.get_value(
					"Jarvis Agent Run", run_name, ["status"], as_dict=True, for_update=True
				)
				if row.status != "running":
					outcomes.append(f"idempotent:{row.status}")
					frappe.db.commit()
					return
				if first:
					locked.set()
					release.wait(5)
				# This represents the fallback creation after record_agent_run has
				# claimed the Run. Only the lock winner reaches this block.
				dashboard = frappe.get_doc(
					{
						"doctype": "Jarvis Dashboard",
						"dashboard_title": f"deposit-lock-{run_name}",
						"scope": "User",
						"html": "<h1>deposit lock test</h1>",
					}
				).insert(ignore_permissions=True)
				created.append(dashboard.name)
				frappe.db.set_value(
					"Jarvis Agent Run",
					run_name,
					{"status": "completed", "dashboard": dashboard.name},
					update_modified=False,
				)
				outcomes.append("winner")
				frappe.db.commit()
			except Exception as exc:
				errors.append(repr(exc))
			finally:
				try:
					frappe.destroy()
				except Exception:
					pass

		first = threading.Thread(target=finalizer, args=(True,))
		second = threading.Thread(target=finalizer, args=(False,))
		try:
			first.start()
			self.assertTrue(locked.wait(5), errors)
			second.start()
			time.sleep(0.1)
			release.set()
			first.join(10)
			second.join(10)
			self.assertFalse(first.is_alive())
			self.assertFalse(second.is_alive())
			self.assertEqual(errors, [])
			self.assertEqual(outcomes.count("winner"), 1)
			self.assertEqual(outcomes.count("idempotent:completed"), 1)
			final = frappe.db.get_value("Jarvis Agent Run", run_name, ["status", "dashboard"], as_dict=True)
			self.assertEqual(final.status, "completed")
			self.assertEqual(final.dashboard, created[0])
			self.assertEqual(frappe.db.count("Jarvis Dashboard", {"name": final.dashboard}), 1)
		finally:
			# The report is linked only after the winner commits. Remove the trusted
			# Run link first, then clean the isolated test artifact.
			frappe.db.delete("Jarvis Agent Run", {"name": run_name})
			for name in created:
				frappe.db.delete("Jarvis Dashboard", {"name": name})
			frappe.db.commit()

	def test_finalized_claim_is_idempotent_under_the_run_row_lock(self):
		"""A second writer that waited on the claim lock must not persist again."""
		from jarvis.tools import _agent_run_ctx
		from jarvis.tools import record_agent_run as tool

		row = frappe._dict(
			name="DEP-PRIVATE-RUN",
			status="completed",
			installation="DEP-PRIVATE-INSTALL",
			agent=snapshot.AGENT,
		)
		_agent_run_ctx.set_session_key("private-writeback-session")
		try:
			with patch.object(frappe.db, "get_value", return_value=row) as get_value:
				result = tool.record_agent_run(findings=[{"note": "PRIVATE-SOURCE-SENTINEL"}])
				with self.assertRaises(InvalidArgumentError):
					tool.record_agent_run(integrity_digest="73" * 32)
		finally:
			_agent_run_ctx.clear_session_key()
		self.assertTrue(result["idempotent"])
		self.assertEqual(result["status"], "completed")
		self.assertTrue(get_value.call_args.kwargs["for_update"])

	def test_malformed_deposit_writeback_stops_before_persistence(self):
		from jarvis.tools import _agent_run_ctx
		from jarvis.tools import record_agent_run as tool

		captured, _ = _captured_snapshot()
		run_doc = _run(source_snapshot=snapshot._metadata(captured))
		run_row = frappe._dict(
			name=run_doc.name,
			status="running",
			installation="DEP-PRIVATE-INSTALL",
			agent=snapshot.AGENT,
		)
		listing = frappe._dict(
			rule_tokens=json.dumps(sorted(snapshot._WRITEBACK_TOKENS)), doctypes_required="[]"
		)

		def db_value(doctype, *_args, **_kwargs):
			if doctype == "Jarvis Agent Run":
				return run_row
			if doctype == "Jarvis Agent Listing":
				return listing
			raise AssertionError(f"unexpected read: {doctype}")

		inst = frappe._dict(agent=snapshot.AGENT, activation_state="shadow")
		_agent_run_ctx.set_session_key("private-writeback-session")
		try:
			with (
				patch.object(frappe.db, "get_value", side_effect=db_value) as get_value,
				patch.object(
					frappe,
					"get_doc",
					side_effect=lambda doctype, _name: inst
					if doctype == "Jarvis Agent Installation"
					else run_doc,
				),
				patch("jarvis.chat.agent_runs.record_delegate_run") as persist,
				self.assertRaises(InvalidArgumentError),
			):
				tool.record_agent_run(
					**_v2_output(
						captured,
						findings=[
							{
								"token": "pf_esi_deposit_due_date",
								"ref_doctype": "Journal Entry",
								"ref_name": "JE-DEP-TEST",
								"note": "PRIVATE-SOURCE-SENTINEL",
							}
						],
					)
				)
		finally:
			_agent_run_ctx.clear_session_key()
		self.assertTrue(get_value.call_args_list[0].kwargs["for_update"])
		persist.assert_not_called()

	def test_valid_v3_writeback_forces_the_server_fallback_dashboard(self):
		from jarvis.tools import _agent_run_ctx
		from jarvis.tools import record_agent_run as tool

		captured, _ = _captured_snapshot()
		run_doc = _run(source_snapshot=snapshot._metadata(captured))
		run_doc.owner = "owner@example.test"
		run_row = frappe._dict(
			name=run_doc.name,
			status="running",
			installation="DEP-PRIVATE-INSTALL",
			agent=snapshot.AGENT,
		)
		listing = frappe._dict(
			rule_tokens=json.dumps(sorted(snapshot._WRITEBACK_TOKENS)), doctypes_required="[]"
		)
		final = frappe._dict(
			name=run_doc.name,
			status="completed",
			findings_count=1,
			blocker_count=0,
			coverage_note="",
			dashboard="SERVER-FALLBACK-DASHBOARD",
			owner=run_doc.owner,
		)

		def db_value(doctype, *_args, **_kwargs):
			if doctype == "Jarvis Agent Run":
				return run_row
			if doctype == "Jarvis Agent Listing":
				return listing
			raise AssertionError(f"unexpected read: {doctype}")

		inst = frappe._dict(agent=snapshot.AGENT, activation_state="shadow")
		_agent_run_ctx.set_session_key("private-writeback-session")
		try:
			with (
				patch.object(frappe.db, "get_value", side_effect=db_value),
				patch.object(frappe.db, "exists", return_value=True),
				patch.object(frappe, "has_permission", return_value=True),
				patch.object(
					frappe,
					"get_doc",
					side_effect=lambda doctype, _name: inst
					if doctype == "Jarvis Agent Installation"
					else run_doc,
				),
				patch("jarvis.chat.operator_review.backend", return_value=None),
				patch("jarvis.chat.agent_runs.record_delegate_run", return_value=final) as persist,
				patch("jarvis.chat.agent_run_steps.record_step"),
			):
				result = tool.record_agent_run(**_v2_output(captured))
				for field in ("integrity_digest", "dashboard", "report_sha256", "canvas_ref"):
					with self.subTest(field=field), self.assertRaises(InvalidArgumentError):
						tool.record_agent_run(**_v2_output(captured, **{field: None}))
		finally:
			_agent_run_ctx.clear_session_key()

		self.assertEqual(result["dashboard"], "SERVER-FALLBACK-DASHBOARD")
		self.assertIsNone(persist.call_args.kwargs["dashboard"])
		self.assertIsNone(persist.call_args.kwargs["canvas_ref"])
		self.assertRegex(persist.call_args.kwargs["integrity_digest"], r"^[0-9a-f]{64}$")
		persisted = persist.call_args.args[2][0]
		self.assertEqual(persisted["severity"], "warning")
		self.assertNotIn("SECRET-PRIVATE-ROW", json.dumps(persist.call_args))
		self.assertEqual(persist.call_count, 1)


class TestPayrollDepositPrivateAuditRedaction(TestCase):
	def _cap(self, tools_allow, *, status="running"):
		return {
			"run": "DEP-PRIVATE-RUN",
			"owner": "owner@example.test",
			"status": status,
			"agent": snapshot.AGENT,
			"legacy": False,
			"tools_allow": tools_allow,
		}

	def _v3_args(self, token="safe-token"):
		return {
			"findings": [{"token": token}],
			"coverage": {},
			"scope": {},
			"truncated": False,
			"rows_consumed": 0,
			"input_snapshot_id": "snapshot",
			"input_snapshot_digest": "digest",
		}

	def _dispatch_with_receipt_capture(
		self, cap, tool, args, run_result=None, tool_call_id=None, result_mode=None
	):
		from jarvis import api

		frappe.local.jarvis_private_delegate_metadata = False
		receipts, logs = [], []

		class Logger:
			def info(self, value):
				logs.append(value)

			def error(self, value, *args, **kwargs):
				logs.append(value)

		def insert(_conversation, fields, **_kwargs):
			receipts.append(deepcopy(fields))
			return "TOOL-MESSAGE"

		patches = (
			patch("jarvis.chat.collaboration.require_legacy_session"),
			patch.object(api, "impersonate"),
			patch.object(_delegate_capability, "resolve", return_value=cap),
			patch.object(
				api,
				"_get_header",
				side_effect=lambda name: tool_call_id if name == "X-Jarvis-Tool-Call-Id" else None,
			),
			patch.object(frappe.db, "get_value", return_value="owner@example.test"),
			patch.object(frappe.db, "commit"),
			patch.object(api, "_locked_insert_chat_message", side_effect=insert),
			patch.object(api, "publish_realtime_tool_result"),
			patch.object(api, "_maybe_attach_artifact"),
			patch("jarvis.chat.collaboration_turn.current_turn", return_value=None),
			patch.object(frappe, "logger", return_value=Logger()),
		)
		with (
			patches[0],
			patches[1],
			patches[2],
			patches[3],
			patches[4],
			patches[5],
			patches[6],
			patches[7],
			patches[8],
			patches[9],
			patches[10],
		):
			if run_result is None:
				result = api._dispatch_from_session(
					"owner@example.test", "private-session", tool, args, result_mode=result_mode
				)
			else:
				with (
					patch.object(api, "_run_tool", return_value=run_result),
					patch("jarvis.chat.connector_focus.refusal", return_value=None),
				):
					result = api._dispatch_from_session(
						"owner@example.test", "private-session", tool, args, result_mode=result_mode
					)
		return result, receipts, logs

	def _dispatch_with_full_egress_capture(self, cap, tool, args, tool_call_id):
		"""Exercise receipt, realtime, and audit egress for one private denial."""
		from jarvis import api

		frappe.local.jarvis_private_delegate_metadata = False
		receipts, events, audit_entries = [], [], []

		class Logger:
			def info(self, value, *_args, **_kwargs):
				audit_entries.append(value)

			def warning(self, value, *_args, **_kwargs):
				audit_entries.append(value)

			def error(self, value, *_args, **_kwargs):
				audit_entries.append(value)

		def insert(_conversation, fields, **_kwargs):
			receipts.append(deepcopy(fields))
			return "TOOL-MESSAGE"

		with (
			patch("jarvis.chat.collaboration.require_legacy_session"),
			patch.object(api, "impersonate"),
			patch.object(_delegate_capability, "resolve", return_value=cap),
			patch.object(
				api,
				"_get_header",
				side_effect=lambda name: tool_call_id if name == "X-Jarvis-Tool-Call-Id" else None,
			),
			patch.object(frappe.db, "get_value", return_value="owner@example.test"),
			patch.object(frappe.db, "commit"),
			patch.object(api, "_locked_insert_chat_message", side_effect=insert),
			patch.object(
				api,
				"publish_realtime_tool_result",
				side_effect=lambda **event: events.append(deepcopy(event)),
			),
			patch.object(api, "_maybe_attach_artifact"),
			patch("jarvis.chat.collaboration_turn.current_turn", return_value=None),
			# The audit timestamp is outside this egress-boundary assertion.
			patch.object(frappe.utils, "now", return_value="2026-11-02 00:00:00"),
			patch.object(frappe, "logger", return_value=Logger()),
		):
			result = api._dispatch_from_session("owner@example.test", "private-session", tool, args)
		return result, receipts, events, audit_entries

	def test_source_receipts_preserve_deposit_redaction_and_terminal_boundary(self):
		"""A source-tool denial must not turn a Deposit egress into a source receipt."""
		from jarvis import api
		from jarvis.chat import source_receipts
		from jarvis.tools import query_file

		self.assertIn("query_file", source_receipts.SOURCE_TOOLS)
		self.assertTrue(callable(query_file.query_file))
		sentinel = "PRIVATE-QUERY-FILE-SOURCE"
		call_id = "PRIVATE-QUERY-FILE-CALL-ID"
		args = {"file_url": sentinel, "sql": f"SELECT '{sentinel}'"}

		result, receipts, events, audit_entries = self._dispatch_with_full_egress_capture(
			self._cap(["jarvis__record_agent_run"]), "query_file", args, call_id
		)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], api.CapabilityDeniedError.__name__)
		self.assertEqual(len(receipts), 1)
		self.assertEqual(len(events), 1)
		self.assertEqual(len(audit_entries), 1)
		stored_result = json.loads(receipts[0]["tool_result"])
		self.assertFalse(source_receipts.is_source_receipt(stored_result))
		self.assertEqual(receipts[0]["tool_name"], _private_output.PRIVATE_DELEGATE_TOOL_LABEL)
		self.assertEqual(json.loads(receipts[0]["tool_args"]), {"private_delegate_args_redacted": True})
		self.assertIsNone(receipts[0]["tool_call_id"])
		self.assertFalse(source_receipts.is_source_receipt(events[0]["result"]))
		self.assertEqual(events[0]["tool_name"], _private_output.PRIVATE_DELEGATE_TOOL_LABEL)
		self.assertEqual(events[0]["args"], {"private_delegate_args_redacted": True})
		self.assertIsNone(events[0]["tool_call_id"])
		audit_entry = json.loads(audit_entries[0])
		self.assertEqual(audit_entry["tool"], _private_output.PRIVATE_DELEGATE_TOOL_LABEL)
		self.assertEqual(audit_entry["args"], {"private_delegate_args_redacted": True})
		for surface in (result, receipts, events, audit_entries):
			self.assertNotIn(sentinel, json.dumps(surface, default=str))
			self.assertNotIn(call_id, json.dumps(surface, default=str))

		terminal, terminal_receipts, terminal_events, terminal_audit = self._dispatch_with_full_egress_capture(
			self._cap(["jarvis__record_agent_run"], status="failed"), "query_file", args, call_id
		)
		self.assertFalse(terminal["ok"])
		self.assertEqual(terminal["error"]["code"], api.RunHaltedError.__name__)
		self.assertEqual(terminal_receipts, [])
		self.assertEqual(terminal_events, [])
		self.assertEqual(terminal_audit, [])
		self.assertNotIn(sentinel, json.dumps(terminal))
		self.assertNotIn(call_id, json.dumps(terminal))

	def test_caller_tool_and_call_id_are_redacted_on_allowed_and_denied_paths(self):
		from jarvis import api

		sentinel_tool = "PRIVATE-TOOL-SENTINEL"
		sentinel_id = "PRIVATE-CALL-ID-SENTINEL"
		for label, allowed, tool, args, run_result, hidden_tool in (
			(
				"allowed",
				["jarvis__record_agent_run"],
				"record_agent_run",
				self._v3_args(sentinel_tool),
				{"ok": True, "data": {"safe": True}},
				sentinel_tool,
			),
			("denied", ["jarvis__record_agent_run"], sentinel_tool, {}, None, sentinel_tool),
			(
				"unregistered",
				["jarvis__record_agent_run"],
				"private_tool_sentinel",
				{},
				None,
				"private_tool_sentinel",
			),
			(
				"malformed",
				["jarvis__record_agent_run"],
				"record_agent_run",
				self._v3_args(),
				{"ok": False, "error": {"code": "InvalidArgumentError", "message": sentinel_tool}},
				sentinel_tool,
			),
		):
			with self.subTest(label=label):
				result, receipts, logs = self._dispatch_with_receipt_capture(
					self._cap(allowed),
					tool,
					args,
					run_result=run_result,
					tool_call_id=sentinel_id,
				)
				self.assertNotIn(hidden_tool, json.dumps(result))
				self.assertNotIn(sentinel_id, json.dumps(result))
				self.assertNotIn(hidden_tool, json.dumps(receipts))
				self.assertNotIn(sentinel_id, json.dumps(receipts))
				self.assertNotIn(hidden_tool, "\n".join(str(row) for row in logs))
				self.assertNotIn(sentinel_id, "\n".join(str(row) for row in logs))
				self.assertEqual(receipts[0]["tool_name"], _private_output.PRIVATE_DELEGATE_TOOL_LABEL)
				self.assertIsNone(receipts[0]["tool_call_id"])

	def test_unbounded_deposit_call_id_is_refused_without_raw_persistence(self):
		too_long = "x" * 129
		result, receipts, logs = self._dispatch_with_receipt_capture(
			self._cap(["jarvis__record_agent_run"]),
			"record_agent_run",
			self._v3_args(),
			run_result={"ok": True, "data": {"safe": True}},
			tool_call_id=too_long,
		)
		self.assertFalse(result["ok"])
		self.assertNotIn(too_long, json.dumps(result))
		self.assertNotIn(too_long, json.dumps(receipts))
		self.assertNotIn(too_long, "\n".join(str(row) for row in logs))
		self.assertEqual(receipts[0]["tool_name"], _private_output.PRIVATE_DELEGATE_TOOL_LABEL)
		self.assertIsNone(receipts[0]["tool_call_id"])

	def test_terminal_deposit_dispatch_stops_before_metadata_or_argument_egress(self):
		from jarvis import api

		sentinel = "PRIVATE-TERMINAL-SOURCE"
		call_id = "PRIVATE-TERMINAL-CALL-ID"
		for tool, args, result_mode in (
			(
				"get_payroll_deposit_inputs",
				{"source": sentinel},
				_private_output.MODE,
			),
			("record_agent_run", {"findings": [{"token": sentinel}]}, None),
		):
			with (
				self.subTest(tool=tool),
				patch.object(api, "_parse_args") as parse_args,
				patch.object(api.audit, "record") as audit_record,
				patch.object(api, "_record_tool_step") as step,
			):
				result, receipts, logs = self._dispatch_with_receipt_capture(
					self._cap(
						["jarvis__get_payroll_deposit_inputs", "jarvis__record_agent_run"],
						status="failed",
					),
					tool,
					args,
					tool_call_id=call_id,
					result_mode=result_mode,
				)
			self.assertFalse(result["ok"])
			self.assertEqual(result["error"]["code"], api.RunHaltedError.__name__)
			parse_args.assert_not_called()
			audit_record.assert_not_called()
			step.assert_not_called()
			self.assertEqual(receipts, [])
			for surface in (result, receipts, logs):
				self.assertNotIn(sentinel, json.dumps(surface, default=str))
				self.assertNotIn(call_id, json.dumps(surface, default=str))

	def test_stale_deposit_metadata_is_redacted_before_the_receipt(self):
		from jarvis import api

		cap = self._cap(["jarvis__record_agent_run"])
		sentinel_tool = "PRIVATE-STALE-TOOL"
		sentinel_id = "PRIVATE-STALE-CALL-ID"
		frappe.cache().delete_value("jarvis:stale_pairing_notified:private-stale-session")
		receipts, events = [], []

		def get_value(doctype, name=None, fieldname=None, *_args, **_kwargs):
			if doctype == "User":
				return 1
			if doctype == "Jarvis Chat Session":
				return "old"
			if doctype == "Jarvis Conversation":
				return "STALE-CONVERSATION" if isinstance(name, dict) else "owner@example.test"
			return None

		def insert(_conversation, fields, **_kwargs):
			receipts.append(deepcopy(fields))
			return "STALE-TOOL-MESSAGE"

		with (
			patch.object(
				api,
				"_get_header",
				side_effect=lambda name: {
					"X-Jarvis-Token": "token",
					"X-Jarvis-Tool-Call-Id": sentinel_id,
				}.get(name),
			),
			patch.object(api, "_request_body_bytes", return_value=b"{}"),
			patch.object(api, "validate_plugin_request", return_value="private-stale-session"),
			patch("jarvis.chat.site_pin.pin_if_unset"),
			patch("jarvis.chat.collaboration.is_collaborative_session", return_value=False),
			patch("jarvis.chat.collaboration.legacy_session_user", return_value="owner@example.test"),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(frappe.db, "get_single_value", return_value="new"),
			patch("jarvis.chat.device.session_device_is_stale", return_value=True),
			patch.object(_delegate_capability, "resolve", return_value=cap),
			patch.object(api, "impersonate"),
			patch.object(frappe.db, "commit"),
			patch.object(api, "_locked_insert_chat_message", side_effect=insert),
			patch.object(
				api, "publish_realtime_tool_result", side_effect=lambda **event: events.append(event)
			),
			patch.object(api, "_maybe_attach_artifact"),
			patch("jarvis.chat.collaboration_turn.current_turn", return_value=None),
			patch("jarvis.api_errors.record_server_error") as error_capture,
		):
			result = api.call_tool(sentinel_tool, {})
		self.assertNotIn(sentinel_tool, json.dumps(result))
		self.assertNotIn(sentinel_id, json.dumps(result))
		self.assertEqual(len(receipts), 1)
		self.assertEqual(len(events), 1)
		for surface in (receipts, events, error_capture.call_args_list):
			self.assertNotIn(sentinel_tool, json.dumps(surface, default=str))
			self.assertNotIn(sentinel_id, json.dumps(surface, default=str))
		self.assertEqual(receipts[0]["tool_name"], _private_output.PRIVATE_DELEGATE_TOOL_LABEL)
		self.assertIsNone(receipts[0]["tool_call_id"])
		self.assertEqual(events[0]["tool_name"], _private_output.PRIVATE_DELEGATE_TOOL_LABEL)
		self.assertIsNone(events[0]["tool_call_id"])
		self.assertEqual(error_capture.call_args.kwargs["route"], _private_output.PRIVATE_DELEGATE_TOOL_LABEL)

	def test_terminal_stale_deposit_call_has_no_notice_or_raw_metadata_egress(self):
		from jarvis import api

		cap = self._cap(["jarvis__record_agent_run"], status="failed")
		sentinel_tool = "PRIVATE-TERMINAL-STALE-TOOL"
		sentinel_id = "PRIVATE-TERMINAL-STALE-CALL-ID"
		sentinel_arg = "PRIVATE-TERMINAL-STALE-SOURCE"

		def get_value(doctype, *_args, **_kwargs):
			if doctype == "User":
				return 1
			if doctype == "Jarvis Chat Session":
				return "old"
			return None

		with (
			patch.object(
				api,
				"_get_header",
				side_effect=lambda name: {
					"X-Jarvis-Token": "token",
					"X-Jarvis-Tool-Call-Id": sentinel_id,
				}.get(name),
			),
			patch.object(api, "_request_body_bytes", return_value=b"{}"),
			patch.object(api, "validate_plugin_request", return_value="private-terminal-stale-session"),
			patch("jarvis.chat.site_pin.pin_if_unset"),
			patch("jarvis.chat.collaboration.is_collaborative_session", return_value=False),
			patch("jarvis.chat.collaboration.legacy_session_user", return_value="owner@example.test"),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(frappe.db, "get_single_value", return_value="new"),
			patch("jarvis.chat.device.session_device_is_stale", return_value=True),
			patch.object(_delegate_capability, "resolve", return_value=cap),
			patch.object(api, "impersonate"),
			patch.object(api, "_persist_and_publish_tool_call") as receipt,
			patch.object(api.audit, "record") as audit_record,
			patch.object(api, "_record_tool_step") as step,
			patch.object(api, "publish_realtime_tool_result") as realtime,
			patch("jarvis.api_errors.record_server_error") as error_capture,
		):
			result = api.call_tool(sentinel_tool, {"source": sentinel_arg})

		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], api.RunHaltedError.__name__)
		receipt.assert_not_called()
		audit_record.assert_not_called()
		step.assert_not_called()
		realtime.assert_not_called()
		for surface in (result, error_capture.call_args_list):
			serialized = json.dumps(surface, default=str)
			self.assertNotIn(sentinel_tool, serialized)
			self.assertNotIn(sentinel_id, serialized)
			self.assertNotIn(sentinel_arg, serialized)
		self.assertEqual(error_capture.call_args.kwargs["route"], _private_output.PRIVATE_DELEGATE_TOOL_LABEL)

	def test_denied_dashboard_arguments_never_reach_audit_or_transcript(self):
		sentinel = "PRIVATE-SOURCE-SENTINEL"
		result, receipts, logs = self._dispatch_with_receipt_capture(
			self._cap(["jarvis__record_agent_run"]),
			"save_agent_dashboard",
			{"html": f"<html>{sentinel}</html>"},
		)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "CapabilityDeniedError")
		self.assertEqual(len(receipts), 1)
		self.assertNotIn(sentinel, json.dumps(receipts))
		self.assertNotIn(sentinel, "\n".join(str(row) for row in logs))
		self.assertIn("private_delegate_args_redacted", receipts[0]["tool_args"])

	def test_malformed_writeback_arguments_never_reach_receipt_or_step(self):
		from jarvis import api
		from jarvis.chat import agent_run_steps

		sentinel = "PRIVATE-SOURCE-SENTINEL"
		hex_sentinel = "73" * 32
		args = {
			"findings": [{"note": sentinel}],
			"coverage": {"detail": sentinel},
			"dashboard": sentinel,
			"integrity_digest": hex_sentinel,
		}
		result, receipts, logs = self._dispatch_with_receipt_capture(
			self._cap(["jarvis__record_agent_run"]),
			"record_agent_run",
			args,
			run_result={"ok": False, "error": {"code": "InvalidArgumentError", "message": "invalid"}},
		)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		self.assertEqual(receipts, [])
		self.assertNotIn(sentinel, json.dumps(receipts))
		self.assertNotIn(hex_sentinel, json.dumps(receipts))
		self.assertNotIn(hex_sentinel, "\n".join(str(row) for row in logs))
		self.assertNotIn(hex_sentinel, json.dumps(result))

		cap = self._cap(["jarvis__record_agent_run"])
		token = _private_output.begin_delegate_redaction(cap)
		try:
			with (
				patch.object(
					agent_run_steps, "humanize_tool_call", return_value=("label", "detail")
				) as humanize,
				patch.object(agent_run_steps, "record_step"),
			):
				api._record_tool_step(
					{"name": "DEP-PRIVATE-RUN", "owner": "owner@example.test"},
					"save_agent_dashboard",
					{"html": sentinel},
					None,
					ok=False,
					started_ms=0,
					error_message="invalid",
				)
		finally:
			_private_output.end_delegate_redaction(token)
		self.assertNotIn(sentinel, json.dumps(humanize.call_args.args[1]))
		self.assertEqual(humanize.call_args.args[1], {"private_delegate_args_redacted": True})

	def test_active_deposit_writeback_rejects_every_non_v3_original_key_before_dispatch(self):
		from jarvis import api

		sentinel = "PRIVATE-V3-ORIGINAL-KEY"
		for label, mutate in (
			("dashboard_null", lambda value: value.update({"dashboard": None})),
			("report_null", lambda value: value.update({"report_sha256": None})),
			("canvas_null", lambda value: value.update({"canvas_ref": None})),
			("integrity_null", lambda value: value.update({"integrity_digest": None})),
			("unknown", lambda value: value.update({"unexpected": sentinel})),
			("legacy_user", lambda value: value.update({"_user": sentinel})),
			("legacy_session", lambda value: value.update({"_session": sentinel})),
			("legacy_session_key", lambda value: value.update({"_session_key": sentinel})),
			("missing", lambda value: value.pop("rows_consumed")),
		):
			args = self._v3_args(sentinel)
			mutate(args)
			with (
				self.subTest(label=label),
				patch.object(api, "_parse_args") as parse_args,
				patch.object(api, "_run_tool") as run_tool,
				patch.object(api.audit, "record") as audit_record,
				patch.object(api, "_record_tool_step") as step,
			):
				result, receipts, logs = self._dispatch_with_receipt_capture(
					self._cap(["jarvis__record_agent_run"]),
					"record_agent_run",
					json.dumps(args),
				)
			self.assertFalse(result["ok"])
			self.assertEqual(result["error"]["code"], "InvalidArgumentError")
			parse_args.assert_not_called()
			run_tool.assert_not_called()
			audit_record.assert_not_called()
			step.assert_not_called()
			self.assertEqual(receipts, [])
			for surface in (result, receipts, logs):
				self.assertNotIn(sentinel, json.dumps(surface, default=str))
