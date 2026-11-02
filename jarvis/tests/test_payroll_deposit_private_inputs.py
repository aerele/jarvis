"""Regression tests for the private payroll Deposit-Status input contract."""

import json
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
	"""The exact compact handoff shape accepted from the v2 bundle."""
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
		"integrity_digest": "a" * 64,
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

	def test_v2_writeback_accepts_only_a_compact_server_projected_shape(self):
		captured, _ = _captured_snapshot()
		run = _run(source_snapshot=snapshot._metadata(captured))
		projected = snapshot.validate_writeback(run, **_v2_output(captured))
		self.assertEqual(projected["scope"], SCOPE)
		self.assertEqual(projected["findings"][0]["severity"], "warning")
		self.assertEqual(projected["findings"][0]["result_class"], "derived_candidate")
		self.assertNotIn("SECRET-PRIVATE-ROW", json.dumps(projected))

	def test_v2_writeback_refuses_scope_snapshot_and_row_count_changes(self):
		captured, _ = _captured_snapshot()
		run = _run(source_snapshot=snapshot._metadata(captured))
		for changed in (
			{"scope": {**SCOPE, "company": "Other Company"}},
			{"input_snapshot_id": "other"},
			{"rows_consumed": 0},
			{"integrity_digest": "A" * 64},
			{
				"coverage": {
					**_v2_output(captured)["coverage"],
					"pf_esi_deposit_due_date": {"state": []},
				}
			},
		):
			with self.subTest(changed=changed), self.assertRaises(InvalidArgumentError):
				snapshot.validate_writeback(run, **_v2_output(captured, **changed))

	def test_v2_writeback_refuses_every_delegate_free_text_route(self):
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
			_v2_output(captured, report_sha256=sentinel),
			_v2_output(captured, canvas_ref=sentinel),
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

	def test_valid_v2_writeback_forces_the_server_fallback_dashboard(self):
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
		finally:
			_agent_run_ctx.clear_session_key()

		self.assertEqual(result["dashboard"], "SERVER-FALLBACK-DASHBOARD")
		self.assertIsNone(persist.call_args.kwargs["dashboard"])
		self.assertIsNone(persist.call_args.kwargs["canvas_ref"])
		persisted = persist.call_args.args[2][0]
		self.assertEqual(persisted["severity"], "warning")
		self.assertNotIn("SECRET-PRIVATE-ROW", json.dumps(persist.call_args))


class TestPayrollDepositPrivateAuditRedaction(TestCase):
	def _cap(self, tools_allow):
		return {
			"run": "DEP-PRIVATE-RUN",
			"owner": "owner@example.test",
			"status": "running",
			"agent": snapshot.AGENT,
			"legacy": False,
			"tools_allow": tools_allow,
		}

	def _dispatch_with_receipt_capture(self, cap, tool, args, run_result=None):
		from jarvis import api

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
			patch.object(api, "_get_header", return_value=None),
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
				result = api._dispatch_from_session("owner@example.test", "private-session", tool, args)
			else:
				with (
					patch.object(api, "_run_tool", return_value=run_result),
					patch("jarvis.chat.connector_focus.refusal", return_value=None),
				):
					result = api._dispatch_from_session("owner@example.test", "private-session", tool, args)
		return result, receipts, logs

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
		args = {
			"findings": [{"note": sentinel}],
			"coverage": {"detail": sentinel},
			"dashboard": sentinel,
		}
		result, receipts, _logs = self._dispatch_with_receipt_capture(
			self._cap(["jarvis__record_agent_run"]),
			"record_agent_run",
			args,
			run_result={"ok": False, "error": {"code": "InvalidArgumentError", "message": "invalid"}},
		)
		self.assertFalse(result["ok"])
		self.assertEqual(len(receipts), 1)
		self.assertNotIn(sentinel, json.dumps(receipts))

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
