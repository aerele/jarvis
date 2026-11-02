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
DASHBOARD = "PRIVATE-DEPOSIT-DASHBOARD"
REPORT_HTML = "<!doctype html><html><body>Private payroll deposit report</body></html>"


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


def _receipt(launch: dict, source_snapshot=None, report=None) -> dict:
	return {
		"contract": snapshot.RECEIPT_CONTRACT,
		"launch": {
			"scope": deepcopy(SCOPE),
			"scope_digest": snapshot.canonical_digest(SCOPE),
			"config_digest": launch["config_digest"],
			"scope_config_digest": launch["scope_config_digest"],
		},
		"source_snapshot": source_snapshot,
		"report": report,
	}


def _run(launch=None, source_snapshot=None, report=None, dashboard=None) -> frappe._dict:
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
			"input_snapshot_json": json.dumps(_receipt(launch, source_snapshot, report)),
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


def _report_proof(dashboard=DASHBOARD, html=REPORT_HTML) -> dict:
	return {"dashboard": dashboard, "report_sha256": snapshot.report_sha256(html)}


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
		with patch.object(snapshot.query_tool, "query", return_value={"rows": [], "more_rows": True}):
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
			patch.object(snapshot.query_tool, "query") as query,
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

	def test_writeback_refuses_changed_scope_and_bad_snapshot_proof(self):
		captured, _ = _captured_snapshot()
		launch = _launch()
		proof = _report_proof()
		run = _run(launch, snapshot._metadata(captured), proof, DASHBOARD)
		with self.assertRaises(InvalidArgumentError):
			snapshot.validate_writeback(
				run,
				{**SCOPE, "company": "Other Company"},
				captured["snapshot_id"],
				captured["snapshot_digest"],
				DASHBOARD,
				proof["report_sha256"],
			)
		with self.assertRaises(InvalidArgumentError):
			snapshot.validate_writeback(
				run, SCOPE, "other", captured["snapshot_digest"], DASHBOARD, proof["report_sha256"]
			)
		self.assertEqual(
			snapshot.validate_writeback(
				run,
				SCOPE,
				captured["snapshot_id"],
				captured["snapshot_digest"],
				DASHBOARD,
				proof["report_sha256"],
			),
			SCOPE,
		)

	def test_dashboard_proof_refuses_missing_fake_or_changed_report(self):
		captured, _ = _captured_snapshot()
		launch = _launch()
		proof = _report_proof()
		missing = _run(launch, snapshot._metadata(captured), dashboard=DASHBOARD)
		with self.assertRaises(InvalidArgumentError):
			snapshot.validate_writeback(
				missing,
				SCOPE,
				captured["snapshot_id"],
				captured["snapshot_digest"],
				DASHBOARD,
				proof["report_sha256"],
			)

		stamped = _run(launch, snapshot._metadata(captured), proof, DASHBOARD)
		with self.assertRaises(InvalidArgumentError):
			snapshot.validate_writeback(
				stamped,
				SCOPE,
				captured["snapshot_id"],
				captured["snapshot_digest"],
				"FAKE-DASHBOARD",
				proof["report_sha256"],
			)
		with self.assertRaises(InvalidArgumentError):
			snapshot.validate_writeback(
				stamped,
				SCOPE,
				captured["snapshot_id"],
				captured["snapshot_digest"],
				DASHBOARD,
				"0" * 64,
			)

	def test_report_stamp_keeps_dashboard_html_out_of_the_run_receipt(self):
		captured, _ = _captured_snapshot()
		launch = _launch()
		run = _run(launch, snapshot._metadata(captured), dashboard=DASHBOARD)
		proof = _report_proof()
		with (
			patch.object(frappe.db, "get_value", return_value=run),
			patch.object(frappe.db, "set_value") as set_value,
		):
			snapshot.stamp_report(run.name, DASHBOARD, proof["report_sha256"])
		stored = set_value.call_args.args[3]
		self.assertEqual(json.loads(stored)["report"], proof)
		self.assertNotIn(REPORT_HTML, stored)

	def test_dashboard_save_hashes_original_html_before_shadow_transform(self):
		from jarvis.tools import _agent_run_ctx
		from jarvis.tools import save_agent_dashboard as tool

		original = "<html><body>Original private deposit report</body></html>"
		transformed = "<html><body>Server shadow preview</body></html>"
		run_row = frappe._dict(
			name="DEP-PRIVATE-RUN",
			agent=snapshot.AGENT,
			status="running",
			installation="DEP-PRIVATE-INSTALL",
			dashboard=None,
		)
		run_doc = frappe._dict(name=run_row.name)
		inst = frappe._dict(activation_state="shadow")

		def db_value(doctype, _filters_or_name, fieldname, **_kwargs):
			if doctype == "Jarvis Agent Run":
				return run_row
			if doctype == "Jarvis Dashboard" and fieldname == "owner":
				return "reviewer@example.test"
			if doctype == "Jarvis Dashboard" and fieldname == "dashboard_title":
				return "Private Deposit Report"
			raise AssertionError(f"unexpected dashboard database read: {doctype}/{fieldname}")

		_agent_run_ctx.set_session_key("private-dashboard-session")
		try:
			with (
				patch.object(frappe.db, "get_value", side_effect=db_value),
				patch.object(
					frappe,
					"get_doc",
					side_effect=lambda doctype, _name: inst
					if doctype == "Jarvis Agent Installation"
					else run_doc,
				),
				patch("jarvis.chat.agent_runs._visibility_owner", return_value="reviewer@example.test"),
				patch("jarvis.chat.agent_runs.persist_agent_dashboard", return_value=DASHBOARD) as persist,
				patch.object(tool, "_shadow_gate_html", return_value=transformed),
				patch.object(snapshot, "stamp_report") as stamp_report,
			):
				saved = tool.save_agent_dashboard(html=original, title="Private Deposit Report")
		finally:
			_agent_run_ctx.clear_session_key()

		self.assertEqual(saved["dashboard"], DASHBOARD)
		self.assertEqual(persist.call_args.args[2], transformed)
		self.assertEqual(
			stamp_report.call_args.args,
			(run_doc.name, DASHBOARD, snapshot.report_sha256(original)),
		)
		self.assertNotEqual(snapshot.report_sha256(original), snapshot.report_sha256(transformed))


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
		}
		try:
			frappe.set_user(run_as)
			with (
				_private_output.request(True),
				patch.object(tool, "get_session_key", return_value=run.session_key),
				patch.object(tool._delegate_capability, "resolve", return_value=cap),
				patch.object(frappe.db, "get_value", side_effect=db_value),
				patch.object(frappe.db, "set_value"),
				patch.object(snapshot.query_tool, "query", side_effect=query),
			):
				captured = tool.get_payroll_deposit_inputs()
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(captured["contract"], snapshot.CONTRACT)
		self.assertEqual(queried_as, [run_as] * (len(snapshot.SOURCE_ORDER) * 3))
