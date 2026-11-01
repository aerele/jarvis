"""Focused checks for the server-owned Stock Variance evaluator."""

from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.chat import stock_variance_evaluation as evaluation
from jarvis.chat import stock_variance_native as native
from jarvis.chat import stock_variance_render as renderer
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _agent_run_ctx, record_agent_run

SCOPE = {
	"company": "Example Manufacturing",
	"record_scope": {"doctype": "Warehouse", "name": "Main Stores - EM"},
	"from_date": "2026-10-01",
	"to_date": "2026-10-31",
	"report_date": "2026-10-31",
	"execution_user": "stock-reader@example.invalid",
}
CONFIG = {"stock_variance": {"reviewer": "reviewer@example.invalid"}}


class TestStockVarianceSourcePlan(TestCase):
	def test_warehouse_metadata_plan_excludes_a_company_sibling(self):
		"""The selected Warehouse root is a source boundary, not a later filter."""
		query = native.query_specs(SCOPE, CONFIG)["warehouses"]
		subtree = next(where for where in query["where"] if where.get("op") == "exists")["value"]
		where = subtree["where"]

		self.assertIn(
			{"field": "whw.name", "op": "=", "value": {"$field": "r.name"}},
			where,
		)
		self.assertIn(
			{"field": "whroot.name", "op": "=", "value": SCOPE["record_scope"]["name"]},
			where,
		)
		self.assertIn(
			{"field": "whw.lft", "op": ">=", "value": {"$field": "whroot.lft"}},
			where,
		)
		self.assertIn(
			{"field": "whw.rgt", "op": "<=", "value": {"$field": "whroot.rgt"}},
			where,
		)

	def test_native_version_matches_the_server_contract_release(self):
		self.assertEqual(native.VERSION, "0.5.0")


class TestStockVarianceCaptureLimits(TestCase):
	def test_total_source_byte_cap_fails_closed(self):
		page = {"rows": [{"name": "WH-001", "value": "x" * 32}]}
		with (
			patch.object(evaluation, "_read_page", return_value=page),
			patch.object(native, "source_row_limit", return_value=100),
			patch.object(evaluation, "MAX_SOURCE_BYTES", 4096),
			patch.object(evaluation, "MAX_TOTAL_SOURCE_BYTES", 1),
		):
			with self.assertRaises(evaluation.CaptureFailure) as raised:
				evaluation._collect_source(
					"warehouses", {"where": [], "limit": 200}, 0.0, already_read_bytes=1
				)

		self.assertEqual(raised.exception.reason_code, "run_truncated_watermark")
		self.assertEqual(raised.exception.state, "truncated")

	def test_unsupported_database_fails_before_a_source_read(self):
		with patch.object(frappe.db, "db_type", "sqlite"):
			with self.assertRaises(evaluation.CaptureFailure) as raised:
				with evaluation._source_statement_timeout(2):
					pass

		self.assertEqual(raised.exception.reason_code, "unsupported_customisation")

	def test_postgres_uses_and_restores_a_statement_timeout(self):
		with (
			patch.object(frappe.db, "db_type", "postgres"),
			patch.object(frappe.db, "sql", return_value=[["30s"]]) as sql,
			patch.object(frappe.db, "set_execution_timeout") as set_timeout,
		):
			with evaluation._source_statement_timeout(2):
				pass

		set_timeout.assert_called_once_with(2)
		self.assertEqual(sql.call_args_list[0].args, ("SHOW statement_timeout",))
		self.assertEqual(sql.call_args_list[1].args, ("SET LOCAL statement_timeout = %s", ("30s",)))

	def test_postgres_timeout_leaves_recovery_to_the_source_savepoint(self):
		timed_out = TimeoutError("statement timeout")
		with (
			patch.object(frappe.db, "db_type", "postgres"),
			patch.object(frappe.db, "sql", return_value=[["30s"]]) as sql,
			patch.object(frappe.db, "set_execution_timeout") as set_timeout,
		):
			with self.assertRaisesRegex(TimeoutError, "statement timeout"):
				with evaluation._source_statement_timeout(2):
					raise timed_out

		set_timeout.assert_called_once_with(2)
		sql.assert_called_once_with("SHOW statement_timeout")


class TestStockVarianceResponseContract(TestCase):
	def test_summary_has_exact_safe_plugin_shape(self):
		stored = {
			"status": "completed",
			"result_state": "evaluated_clean",
			"dashboard": "Stock Review & Main",
			"findings_count": -1,
			"blocker_count": "3",
			"coverage_note": None,
		}
		with patch.object(frappe.db, "get_value", return_value=stored):
			result = evaluation._summary({"run": "SV-RUN-1"}, {"execution_user": None})

		self.assertEqual(
			set(result),
			{
				"run",
				"status",
				"result_state",
				"findings_count",
				"blocker_count",
				"coverage_note",
				"dashboard",
				"dashboard_url",
				"execution_user",
			},
		)
		self.assertEqual(result["findings_count"], 0)
		self.assertEqual(result["blocker_count"], 3)
		self.assertEqual(result["dashboard_url"], "/dashboards/Stock%20Review%20%26%20Main")
		for key in ("run", "status", "result_state", "coverage_note", "execution_user"):
			self.assertIsInstance(result[key], str)
		for key in ("findings_count", "blocker_count"):
			self.assertIsInstance(result[key], int)
			self.assertGreaterEqual(result[key], 0)
		for key in ("dashboard", "dashboard_url"):
			self.assertTrue(result[key] is None or isinstance(result[key], str))

	def test_server_writeback_marker_is_request_scoped(self):
		self.assertFalse(evaluation.is_server_writeback())
		with evaluation.server_writeback():
			self.assertTrue(evaluation.is_server_writeback())
		self.assertFalse(evaluation.is_server_writeback())

	def test_model_cannot_call_the_direct_writeback(self):
		_agent_run_ctx.set_session_key("agent:agent-stock-variance-auditor:SV-RUN-1")
		self.addCleanup(_agent_run_ctx.clear_session_key)
		run = frappe._dict(
			name="SV-RUN-1",
			status="running",
			agent="stock-variance-auditor",
			installation="SV-INSTALL-1",
		)
		with patch.object(frappe.db, "get_value", return_value=run):
			with self.assertRaisesRegex(InvalidArgumentError, "server-owned"):
				record_agent_run.record_agent_run()


class TestStockVarianceServerRunner(TestCase):
	def setUp(self):
		self.session_key = "agent:agent-stock-variance-auditor:SV-RUN-1"
		_agent_run_ctx.set_session_key(self.session_key)
		self.addCleanup(_agent_run_ctx.clear_session_key)
		self.scope = dict(SCOPE)
		self.run_row = frappe._dict(
			name="SV-RUN-1",
			status="running",
			agent="stock-variance-auditor",
			installation="SV-INSTALL-1",
			scope_json=frappe.as_json(self.scope),
			assessment_config_json=frappe.as_json(CONFIG),
		)
		self.run_doc = frappe._dict(name="SV-RUN-1")
		self.installation = frappe._dict(name="SV-INSTALL-1", activation_state="live")

	def test_runner_uses_only_the_stamped_scope_and_configuration(self):
		sheet = {"integrity_digest": "digest", "run_state": "evaluated_clean"}
		capture_data = {"raw_source": "must stay in the app"}
		capture_evidence = {
			"source_digest": "source-digest",
			"source_counts": {"counts": 1},
			"source_bytes": 100,
			"elapsed_ms": 1,
		}
		summary = {
			"run": "SV-RUN-1",
			"status": "completed",
			"result_state": "evaluated_clean",
			"findings_count": 0,
			"blocker_count": 0,
			"coverage_note": "",
			"dashboard": "DASH-1",
			"dashboard_url": "/dashboards/DASH-1",
			"execution_user": SCOPE["execution_user"],
		}
		with (
			patch.object(frappe.db, "get_value", return_value=self.run_row) as get_value,
			patch.object(frappe, "get_doc", side_effect=[self.run_doc, self.installation]),
			patch.object(native, "policy") as policy,
			patch.object(evaluation, "capture", return_value=(capture_data, capture_evidence)) as capture,
			patch.object(native, "evaluate_native", return_value=sheet) as evaluate,
			patch.object(renderer, "writeback_payload", return_value={"findings": []}),
			patch.object(renderer, "render", return_value="<html></html>"),
			patch.object(evaluation, "_persist", return_value=summary) as persist,
		):
			result = evaluation.run_stock_variance_evaluation()

		self.assertEqual(result, summary)
		self.assertTrue(get_value.call_args_list[0].kwargs["for_update"])
		policy.assert_called_once_with(self.scope, CONFIG)
		capture.assert_called_once()
		self.assertEqual(capture.call_args.args[:2], (self.scope, CONFIG))
		evaluate.assert_called_once_with(self.scope, CONFIG, capture_data)
		evidence = persist.call_args.args[4]
		self.assertNotIn("raw_source", evidence)
		self.assertEqual(evidence["source_digest"], "source-digest")

	def test_capture_failure_records_typed_non_evaluable_output(self):
		failure = evaluation.CaptureFailure("source unavailable", reason_code="permission_slice")
		summary = {
			"run": "SV-RUN-1",
			"status": "partial",
			"result_state": "not_evaluable",
			"findings_count": 0,
			"blocker_count": 0,
			"coverage_note": "",
			"dashboard": None,
			"dashboard_url": None,
			"execution_user": SCOPE["execution_user"],
		}
		with (
			patch.object(frappe.db, "get_value", return_value=self.run_row),
			patch.object(frappe, "get_doc", side_effect=[self.run_doc, self.installation]),
			patch.object(native, "policy"),
			patch.object(evaluation, "capture", side_effect=failure),
			patch.object(evaluation, "_persist", return_value=summary) as persist,
		):
			result = evaluation.run_stock_variance_evaluation()

		self.assertEqual(result, summary)
		payload = persist.call_args.args[3]
		self.assertEqual(payload["findings"], [])
		self.assertEqual(payload["coverage"][native.TOKEN]["state"], "not_evaluable")
		self.assertEqual(payload["coverage"][native.TOKEN]["reason_code"], "permission_slice")
		self.assertNotIn("html", persist.call_args.kwargs)

	def test_postgres_timeout_rolls_back_before_typed_failure_writeback(self):
		timed_out = TimeoutError("statement timeout")
		summary = {
			"run": "SV-RUN-1",
			"status": "partial",
			"result_state": "not_evaluable",
			"findings_count": 0,
			"blocker_count": 0,
			"coverage_note": "",
			"dashboard": None,
			"dashboard_url": None,
			"execution_user": SCOPE["execution_user"],
		}
		events = []

		def timed_capture(_scope, _config, *, started):
			return evaluation._read_page({"from": "Warehouse", "select": ["name"]}, started)

		def persisted(*_args, **_kwargs):
			events.append("persist")
			return summary

		with (
			patch.object(frappe.db, "get_value", return_value=self.run_row),
			patch.object(frappe, "get_doc", side_effect=[self.run_doc, self.installation]),
			patch.object(native, "policy"),
			patch.object(evaluation, "capture", side_effect=timed_capture),
			patch.object(frappe.db, "db_type", "postgres"),
			patch.object(frappe.db, "savepoint", side_effect=lambda _name: events.append("savepoint")),
			patch.object(
				frappe.db,
				"rollback",
				side_effect=lambda **kwargs: events.append(("rollback", kwargs.get("save_point"))),
			) as rollback,
			patch.object(frappe.db, "release_savepoint") as release_savepoint,
			patch.object(frappe.db, "sql", return_value=[["30s"]]) as sql,
			patch.object(frappe.db, "set_execution_timeout"),
			patch.object(frappe.db, "is_statement_timeout", return_value=True),
			patch("jarvis.tools.query.query", side_effect=timed_out),
			patch.object(evaluation, "_persist", side_effect=persisted) as persist,
		):
			result = evaluation.run_stock_variance_evaluation()

		self.assertEqual(result, summary)
		rollback.assert_called_once()
		self.assertEqual(events[0], "savepoint")
		self.assertEqual(events[1][0], "rollback")
		self.assertEqual(events[2], "persist")
		release_savepoint.assert_not_called()
		sql.assert_called_once_with("SHOW statement_timeout")
		payload = persist.call_args.args[3]
		self.assertEqual(payload["coverage"][native.TOKEN]["state"], "truncated")
		self.assertEqual(payload["coverage"][native.TOKEN]["reason_code"], "run_truncated_watermark")

	def test_source_savepoint_release_failure_rolls_back_before_typed_failure_writeback(self):
		summary = {
			"run": "SV-RUN-1",
			"status": "partial",
			"result_state": "not_evaluable",
			"findings_count": 0,
			"blocker_count": 0,
			"coverage_note": "",
			"dashboard": None,
			"dashboard_url": None,
			"execution_user": SCOPE["execution_user"],
		}
		events = []

		def release(_name):
			events.append("release")
			raise RuntimeError("release failed")

		def persisted(*_args, **_kwargs):
			events.append("persist")
			return summary

		with (
			patch.object(frappe.db, "get_value", return_value=self.run_row),
			patch.object(frappe, "get_doc", side_effect=[self.run_doc, self.installation]),
			patch.object(native, "policy"),
			patch.object(
				evaluation,
				"capture",
				return_value=({}, {"source_digest": "source", "source_counts": {}, "source_bytes": 0}),
			),
			patch.object(frappe.db, "savepoint", side_effect=lambda _name: events.append("savepoint")),
			patch.object(
				frappe.db,
				"release_savepoint",
				side_effect=release,
			),
			patch.object(
				frappe.db,
				"rollback",
				side_effect=lambda **kwargs: events.append(("rollback", kwargs.get("save_point"))),
			) as rollback,
			patch.object(evaluation, "_persist", side_effect=persisted) as persist,
		):
			result = evaluation.run_stock_variance_evaluation()

		self.assertEqual(result, summary)
		rollback.assert_called_once()
		self.assertEqual(events[0], "savepoint")
		self.assertEqual(events[1], "release")
		self.assertEqual(events[2][0], "rollback")
		self.assertEqual(events[3], "persist")
		payload = persist.call_args.args[3]
		self.assertEqual(payload["coverage"][native.TOKEN]["state"], "not_evaluable")
		self.assertEqual(payload["coverage"][native.TOKEN]["reason_code"], "record_coverage_insufficient")

	def test_retry_after_execution_claim_reads_the_terminal_result_without_recapture(self):
		terminal = frappe._dict(self.run_row)
		terminal.status = "completed"
		summary = {
			"run": "SV-RUN-1",
			"status": "completed",
			"result_state": "evaluated_clean",
			"findings_count": 0,
			"blocker_count": 0,
			"coverage_note": "",
			"dashboard": "DASH-1",
			"dashboard_url": "/dashboards/DASH-1",
			"execution_user": SCOPE["execution_user"],
		}
		with (
			patch.object(frappe.db, "get_value", return_value=terminal) as get_value,
			patch.object(evaluation, "capture") as capture,
			patch.object(evaluation, "_summary", return_value=summary),
		):
			result = evaluation.run_stock_variance_evaluation()

		self.assertEqual(result, summary)
		self.assertTrue(get_value.call_args.kwargs["for_update"])
		capture.assert_not_called()

	def test_runner_dashboard_is_rehomed_to_the_visibility_owner(self):
		from jarvis.chat import agent_runs

		self.installation.activation_state = "shadow"
		with (
			patch.object(evaluation, "_store_evidence"),
			patch.object(agent_runs, "_visibility_owner", return_value="reviewer@example.invalid"),
			patch.object(agent_runs, "persist_agent_dashboard", return_value="DASH-1"),
			patch("jarvis.tools.save_agent_dashboard._shadow_gate_html", return_value="<safe></safe>"),
			patch.object(frappe.db, "get_value", return_value="run-as@example.invalid"),
			patch.object(frappe.db, "set_value") as set_value,
			patch.object(record_agent_run, "record_agent_run", return_value={"run": "SV-RUN-1"}),
			patch.object(evaluation, "_summary", return_value={"run": "SV-RUN-1"}),
		):
			evaluation._persist(
				self.run_doc,
				self.installation,
				self.scope,
				{"findings": []},
				{"source_bytes": 0},
				html="<html></html>",
			)

		set_value.assert_called_once_with(
			"Jarvis Dashboard",
			"DASH-1",
			{"owner": "reviewer@example.invalid", "target_user": "reviewer@example.invalid"},
			update_modified=False,
		)
