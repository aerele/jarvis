"""Exercise private source output through the real bound delegate dispatcher."""

import hashlib
import importlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.tests.test_platform_capability_contract import _mk_listing, _mk_run, _mk_user, _snapshot
from jarvis.tools import _agent_run_ctx, _private_output, registry

CANARY = "PRIVATE-SOURCE-CANARY"


class TestPrivateAuditOutput(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.slug = "private-output-" + frappe.generate_hash(length=10)
		self.key = f"agent:agent-{self.slug}:fake-test"
		self.actor = _mk_user(self.slug + "@example.invalid", roles=("System Manager", "Jarvis User"))
		_mk_listing(self.slug, "Auditor", [])
		self.run = _mk_run(
			self.slug,
			self.actor,
			self.key,
			_snapshot(
				[
					"jarvis__query",
					"jarvis__run_report",
					"jarvis__get_cycle_count_inputs",
					"jarvis__get_purchase_match_inputs",
					"jarvis__get_receivables_review_inputs",
					"jarvis__get_ticket_triage_inputs",
				],
				"auditor",
				[],
			),
		)
		self.conv = frappe.get_doc(
			{"doctype": "Jarvis Conversation", "title": "Private output test", "session_key": self.key}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{"doctype": "Jarvis Chat Session", "session_key": self.key, "user": self.actor}
		).insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- receipts use a separate transaction

	def tearDown(self):
		frappe.set_user("Administrator")
		_agent_run_ctx.clear_session_key()
		for dt in ("Jarvis Agent Run Step", "Jarvis Agent Activity"):
			field = "run" if dt == "Jarvis Agent Run Step" else "agent"
			if frappe.db.exists("DocType", dt):
				frappe.db.delete(dt, {field: self.run if field == "run" else self.slug})
		frappe.db.delete("Jarvis Chat Message", {"conversation": self.conv.name})
		frappe.delete_doc("Jarvis Conversation", self.conv.name, force=True, ignore_permissions=True)
		frappe.db.delete("Jarvis Chat Session", {"session_key": self.key})
		for dt in ("Jarvis Agent Run", "Jarvis Agent Installation"):
			for name in frappe.get_all(dt, {"agent": self.slug}, pluck="name"):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.delete_doc("Jarvis Agent Listing", self.slug, force=True, ignore_permissions=True)
		frappe.delete_doc("User", self.actor, force=True, ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- remove committed test fixtures

	def _dispatch(self, payload, *, mode=_private_output.MODE, tool="query", key=None):
		if tool == "query":
			args = {"spec": {"from": "ToDo", "select": ["name"], "limit": 1000}}
		elif tool in _private_output.PRIVATE_SOURCE_TOOLS:
			args = {}
		else:
			args = {"report_name": "Fake report"}
		with (
			patch.dict(registry._TOOLS, {tool: lambda **kwargs: payload}),
			patch("jarvis.api._get_header", return_value=None),
		):
			return api._dispatch_from_session(self.actor, key or self.key, tool, args, result_mode=mode)

	@staticmethod
	def _source_capture_module(tool):
		module = importlib.import_module(f"jarvis.tools.{tool}")
		# Cycle Count captures from its review module. AP and AR expose the
		# same capture seam directly from their tool wrapper.
		return getattr(module, "ticket_triage", getattr(module, "cycle_count_review", module))

	def test_full_private_query_and_report_leave_only_safe_receipts(self):
		for tool, row_key in (("query", "rows"), ("run_report", "result")):
			payload = {row_key: [{"name": f"FAKE-{i}", "value": CANARY * 10} for i in range(1000)]}
			with patch("jarvis.api.publish_realtime_tool_result") as published:
				result = self._dispatch(payload, tool=tool)
			self.assertTrue(result["ok"], result)
			self.assertEqual(result["result_mode"], _private_output.MODE)
			self.assertEqual(result["data"], payload)
			self.assertNotIn(CANARY, str(published.call_args))
			self.assertTrue(published.called)
		messages = frappe.get_all("Jarvis Chat Message", {"conversation": self.conv.name}, ["tool_result"])
		self.assertEqual(len(messages), 2)
		for message in messages:
			self.assertNotIn(CANARY, message.tool_result)
			summary = json.loads(message.tool_result)["data"]
			self.assertTrue(summary["private_output"])
			self.assertEqual(summary["count"], 1000)
			self.assertEqual(len(summary["sha256"]), 64)
		steps = frappe.get_all("Jarvis Agent Run Step", {"run": self.run}, ["label", "detail"])
		self.assertEqual(len(steps), 2)
		self.assertNotIn(CANARY, str(steps))
		self.assertTrue(all(s.detail == "1000 rows" for s in steps))

	def test_private_review_input_leaves_only_its_safe_receipt(self):
		payload = {
			"rows": [
				{
					"source_digest": "a" * 64,
					"scope": {"company": "Fake Co"},
					"datasets": {"invoices": [{"bill_no": CANARY, "amount": 0.0}]},
				}
			]
		}
		for tool in sorted(_private_output.PRIVATE_SOURCE_TOOLS):
			with self.subTest(tool=tool), patch("jarvis.api.publish_realtime_tool_result") as published:
				result = self._dispatch(payload, tool=tool)
			self.assertTrue(result["ok"], result)
			self.assertEqual(result["result_mode"], _private_output.MODE)
			self.assertIsInstance(result["data"], str)
			self.assertIn('"amount":0.0', result["data"])
			self.assertEqual(json.loads(result["data"]), payload)
			self.assertTrue(result["private_output"]["private_output"])
			self.assertEqual(result["private_output"]["count"], 1)
			self.assertEqual(result["private_output"]["bytes"], len(result["data"].encode()))
			self.assertEqual(
				result["private_output"]["sha256"],
				hashlib.sha256(result["data"].encode()).hexdigest(),
			)
			self.assertNotIn(CANARY, str(published.call_args))
			message = frappe.get_last_doc("Jarvis Chat Message", {"conversation": self.conv.name})
			self.assertNotIn(CANARY, message.tool_result)
			self.assertEqual(json.loads(message.tool_result)["data"], result["private_output"])
			step = frappe.get_last_doc("Jarvis Agent Run Step", {"run": self.run})
			self.assertNotIn(CANARY, step.detail)

	def test_ticket_triage_private_marker_never_reaches_receipt_realtime_step_or_error(self):
		"""Exercise the real dispatcher, not only the private-output formatter."""
		marker = "TT-RAW-MARKER-DO-NOT-PERSIST"
		payload = {
			"rows": [
				{
					"source_digest": "a" * 64,
					"capture_id": "TT-private-capture",
					"tickets": [
						{
							"name": "TT-0001",
							"subject": marker,
							"description": marker,
							"raised_by": marker,
						}
					],
				}
			]
		}
		with patch("jarvis.api.publish_realtime_tool_result") as published:
			result = self._dispatch(payload, tool="get_ticket_triage_inputs")
		self.assertTrue(result["ok"], result)
		self.assertIn(marker, result["data"])
		self.assertNotIn(marker, str(published.call_args))
		for message in frappe.get_all(
			"Jarvis Chat Message", {"conversation": self.conv.name}, ["tool_result"]
		):
			self.assertNotIn(marker, message.tool_result)
		for step in frappe.get_all("Jarvis Agent Run Step", {"run": self.run}, ["detail"]):
			self.assertNotIn(marker, step.detail)

		# A malformed private source is a real persisted error receipt.  It must
		# not echo the rejected source row into the chat, realtime, step, or error
		# envelope.
		bad_payload = {"rows": [{"source_digest": "not-a-digest", "subject": marker}]}
		with patch("jarvis.api.publish_realtime_tool_result") as published:
			result = self._dispatch(bad_payload, tool="get_ticket_triage_inputs")
		self.assertFalse(result["ok"], result)
		self.assertNotIn(marker, str(result))
		self.assertNotIn(marker, str(published.call_args))
		for message in frappe.get_all(
			"Jarvis Chat Message", {"conversation": self.conv.name}, ["tool_result"]
		):
			self.assertNotIn(marker, message.tool_result)
		for step in frappe.get_all("Jarvis Agent Run Step", {"run": self.run}, ["detail"]):
			self.assertNotIn(marker, step.detail)

	def test_private_review_input_uses_sorted_canonical_source_bytes(self):
		payload = {"rows": [{"z_key": "last", "source_digest": "a" * 64, "a_key": "first"}]}
		expected = json.dumps(
			payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
		).encode()
		for tool in sorted(_private_output.PRIVATE_SOURCE_TOOLS):
			with self.subTest(tool=tool):
				result = self._dispatch(payload, tool=tool)
				self.assertEqual(result["data"].encode(), expected)
				self.assertEqual(result["private_output"]["bytes"], len(expected))
				self.assertEqual(result["private_output"]["sha256"], hashlib.sha256(expected).hexdigest())

	def test_private_review_input_rejects_an_unsealed_or_non_singleton_envelope(self):
		for tool in sorted(_private_output.PRIVATE_SOURCE_TOOLS):
			for payload in (
				{"rows": []},
				{"rows": [{"source_digest": "a" * 64}, {"source_digest": "b" * 64}]},
				{"rows": [{"source_digest": "not-a-digest", "secret": CANARY}]},
				{"rows": [{"source_digest": "a" * 64}], "result": []},
			):
				with self.subTest(tool=tool, payload=payload):
					result = self._dispatch(payload, tool=tool)
					self.assertFalse(result["ok"], result)
					self.assertNotIn("data", result)
					self.assertNotIn(CANARY, str(result))

	def test_private_review_handlers_seal_the_actual_captured_snapshot(self):
		snapshot = {
			"source_digest": "a" * 64,
			"scope": {"company": "Fake Co"},
			"datasets": {"invoices": [{"bill_no": CANARY, "amount": 0.0}]},
		}
		# Ticket Triage uses its own session-bound capture and typed dispatcher;
		# its real-record dispatch tests live in test_ticket_triage.
		for tool in sorted(_private_output.PRIVATE_SOURCE_TOOLS - {"get_ticket_triage_inputs"}):
			module = self._source_capture_module(tool)
			with (
				self.subTest(tool=tool),
				patch.object(module, "get_inputs", return_value=snapshot) as source,
				patch("jarvis.api.publish_realtime_tool_result") as published,
			):
				result = api._dispatch_from_session(
					self.actor, self.key, tool, {}, result_mode=_private_output.MODE
				)
			source.assert_called_once()
			self.assertTrue(result["ok"], result)
			self.assertEqual(json.loads(result["data"]), {"rows": [snapshot]})
			self.assertEqual(result["private_output"]["count"], 1)
			self.assertEqual(result["private_output"]["bytes"], len(result["data"].encode()))
			self.assertNotIn(CANARY, str(published.call_args))
			message = frappe.get_last_doc("Jarvis Chat Message", {"conversation": self.conv.name})
			self.assertNotIn(CANARY, message.tool_result)
			step = frappe.get_last_doc("Jarvis Agent Run Step", {"run": self.run})
			self.assertNotIn(CANARY, step.detail)

	def test_private_review_handlers_refuse_malformed_captured_snapshot_without_leak(self):
		for tool in sorted(_private_output.PRIVATE_SOURCE_TOOLS - {"get_ticket_triage_inputs"}):
			module = self._source_capture_module(tool)
			for snapshot in (None, {}, {"source_digest": "not-a-digest", "secret": CANARY}):
				with (
					self.subTest(tool=tool, snapshot=snapshot),
					patch.object(module, "get_inputs", return_value=snapshot) as source,
					patch("jarvis.api.publish_realtime_tool_result") as published,
				):
					result = api._dispatch_from_session(
						self.actor, self.key, tool, {}, result_mode=_private_output.MODE
					)
				source.assert_called_once()
				self.assertFalse(result["ok"], result)
				self.assertNotIn("data", result)
				self.assertNotIn(CANARY, str(result))
				self.assertNotIn(CANARY, str(published.call_args))

	def test_review_input_requires_private_mode_before_tool_dispatch(self):
		for tool in sorted(_private_output.PRIVATE_SOURCE_TOOLS):
			for mode in (None, "ordinary-output"):
				with self.subTest(tool=tool, mode=mode), patch("jarvis.api._run_tool") as execute:
					result = api._dispatch_from_session(self.actor, self.key, tool, {}, result_mode=mode)
				self.assertFalse(result["ok"], result)
				self.assertNotIn("data", result)
				execute.assert_not_called()

	def test_private_review_input_rejects_model_selected_arguments_before_dispatch(self):
		for tool in sorted(_private_output.PRIVATE_SOURCE_TOOLS):
			with self.subTest(tool=tool), patch("jarvis.api._run_tool") as execute:
				result = api._dispatch_from_session(
					self.actor,
					self.key,
					tool,
					{"company": "model-selected"},
					result_mode=_private_output.MODE,
				)
				self.assertFalse(result["ok"], result)
				execute.assert_not_called()

	def test_private_query_keeps_the_requested_page_when_more_rows_exist(self):
		payload = {"rows": [{"name": "FAKE", "value": CANARY}], "more_rows": True}
		with patch("jarvis.api.publish_realtime_tool_result"):
			result = self._dispatch(payload)
		self.assertTrue(result["ok"], result)
		self.assertTrue(result["data"]["more_rows"])
		self.assertEqual(result["result_mode"], _private_output.MODE)
		message = frappe.get_value("Jarvis Chat Message", {"conversation": self.conv.name}, "tool_result")
		receipt = json.loads(message)
		self.assertEqual(receipt["data"]["count"], 1)
		self.assertEqual(len(receipt["data"]["sha256"]), 64)
		self.assertNotIn("more_rows", receipt["data"])
		self.assertNotIn(CANARY, message)

	def test_ordinary_agent_output_is_still_guarded(self):
		payload = {"rows": [{"name": f"FAKE-{i}", "value": "x" * 200} for i in range(1000)]}
		result = self._dispatch(payload, mode=None)
		self.assertTrue(result["data"]["_truncated"])
		self.assertLess(len(result["data"]["rows"]), 1000)

	def test_private_query_without_an_explicit_limit_does_not_execute(self):
		with patch("jarvis.api._get_header", return_value=None), patch("jarvis.api._run_tool") as execute:
			result = api._dispatch_from_session(
				self.actor,
				self.key,
				"query",
				{"spec": {"from": "DocType", "select": ["name"]}},
				result_mode=_private_output.MODE,
			)
		self.assertFalse(result["ok"])
		execute.assert_not_called()

	def test_native_large_private_query_has_no_false_partial_note_or_export_cache(self):
		args = {
			"spec": {
				"from": "DocType",
				"alias": "d",
				"select": ["d.name", "d.module", "d.autoname"],
				"limit": 1000,
			},
			"confirm_large": True,
		}
		with (
			patch("jarvis.api._get_header", return_value=None),
			patch("jarvis.tools._export.saved_queries.save") as saved,
		):
			result = api._dispatch_from_session(
				self.actor, self.key, "query", args, result_mode=_private_output.MODE
			)
		self.assertTrue(result["ok"], result)
		self.assertGreater(len(json.dumps(result["data"])), 35000)
		self.assertNotIn("partial", result["data"])
		self.assertNotIn("result_id", result["data"])
		saved.assert_not_called()
		self.assertFalse(_private_output.is_private())

	def test_private_output_caps_and_truncation_refuse_without_source_data(self):
		for payload in (
			{"rows": [{"name": "FAKE"}] * 20001},
			{"rows": [{"name": "FAKE", "value": "界" * (_private_output.MAX_BYTES // 3 + 1)}]},
			{"result": [{"value": CANARY}], "_truncated": True},
			{"prepared_report": True, "status": "generating"},
		):
			result = self._dispatch(payload)
			self.assertFalse(result["ok"])
			self.assertNotIn("data", result)
			self.assertNotIn(CANARY, str(result))

	def test_private_report_completion_markers_refuse_without_source_data(self):
		for extra in ({"more_rows": True}, {"partial": True}):
			with self.subTest(extra=extra):
				result = self._dispatch({"result": [{"value": CANARY}], **extra}, tool="run_report")
				self.assertFalse(result["ok"])
				self.assertNotIn("data", result)
				self.assertNotIn(CANARY, str(result))

	def test_private_post_tool_refusal_has_a_safe_failure_audit(self):
		payload = {"rows": [{"value": CANARY}], "_truncated": True}
		with (
			patch("jarvis.api.audit.record") as recorded,
			patch("jarvis.api.publish_realtime_tool_result") as published,
		):
			result = self._dispatch(payload)
		self.assertFalse(result["ok"])
		self.assertNotIn("data", result)
		self.assertNotIn(CANARY, str(result))
		recorded.assert_called_once()
		kwargs = recorded.call_args.kwargs
		self.assertEqual(kwargs["tool"], "query")
		self.assertFalse(kwargs["ok"])
		self.assertEqual(kwargs["error_code"], "InvalidArgumentError")
		self.assertEqual(kwargs["error_message"], "The private source is truncated; coverage is incomplete.")
		self.assertNotIn("result", kwargs)
		self.assertNotIn(CANARY, str(recorded.call_args))
		steps = frappe.get_all("Jarvis Agent Run Step", {"run": self.run}, ["detail"])
		self.assertEqual(len(steps), 1)
		self.assertIn("private source is truncated", steps[0].detail)
		messages = frappe.get_all("Jarvis Chat Message", {"conversation": self.conv.name}, ["tool_result"])
		self.assertEqual(len(messages), 1)
		self.assertNotIn(CANARY, messages[0].tool_result)
		self.assertNotIn(CANARY, str(published.call_args))

	def test_nonfinite_private_values_are_refused_without_echoing_source(self):
		for value in (float("nan"), float("inf"), -float("inf")):
			with self.subTest(value=value):
				result = self._dispatch({"rows": [{"value": value, "secret": CANARY}]})
				self.assertFalse(result["ok"])
				self.assertNotIn("data", result)
				self.assertNotIn(CANARY, str(result))

	def test_waiting_or_prepared_report_with_old_rows_is_not_a_complete_private_source(self):
		for extra in ({"prepared_report": True}, {"status": "generating"}):
			with self.subTest(extra=extra):
				result = self._dispatch({"result": [{"value": CANARY}], **extra}, tool="run_report")
				self.assertFalse(result["ok"])
				self.assertNotIn("data", result)
				self.assertNotIn(CANARY, str(result))

	def test_non_delegate_unknown_mode_and_stopped_run_do_not_execute(self):
		for mode, key in ((_private_output.MODE, "agent:main:fake"), ("unknown", self.key)):
			with patch("jarvis.api._run_tool") as execute:
				self.assertFalse(self._dispatch({}, mode=mode, key=key)["ok"])
				execute.assert_not_called()
		frappe.db.set_value("Jarvis Agent Run", self.run, "status", "stopped")
		with patch("jarvis.api._run_tool") as execute:
			self.assertFalse(self._dispatch({})["ok"])
			execute.assert_not_called()

	def test_contract_denied_tool_and_legacy_run_do_not_execute(self):
		with patch("jarvis.api._run_tool") as execute:
			self.assertFalse(self._dispatch({}, tool="create_doc")["ok"])
			execute.assert_not_called()
		frappe.db.set_value("Jarvis Agent Run", self.run, "capability_contract", "legacy")
		with patch("jarvis.api._run_tool") as execute:
			self.assertFalse(self._dispatch({})["ok"])
			execute.assert_not_called()

	def test_standard_external_path_refuses_private_mode_before_dispatch(self):
		with patch("jarvis.api._get_header", return_value=None), patch("jarvis.api._run_tool") as execute:
			self.assertFalse(api.call_tool("query", {}, result_mode=_private_output.MODE)["ok"])
			execute.assert_not_called()

	def test_standard_external_source_entry_guard_refuses_before_dispatch(self):
		self.assertEqual(
			_private_output.PRIVATE_SOURCE_TOOLS,
			frozenset(
				{
					"get_cycle_count_inputs",
					"get_purchase_match_inputs",
					"get_receivables_review_inputs",
					"get_ticket_triage_inputs",
				}
			),
		)
		for tool in sorted(_private_output.PRIVATE_SOURCE_TOOLS):
			module = self._source_capture_module(tool)
			for mode in (None, _private_output.MODE):
				with (
					self.subTest(tool=tool, mode=mode),
					patch.object(
						module,
						"capture_private_inputs" if tool == "get_ticket_triage_inputs" else "get_inputs",
					) as source,
					patch("jarvis.api._get_header", return_value=None),
					patch("jarvis.api._dispatch_current_user") as dispatch,
					patch("jarvis.api._run_tool") as run_tool,
				):
					result = api.call_tool(tool, {}, result_mode=mode)
				source.assert_not_called()
				dispatch.assert_not_called()
				run_tool.assert_not_called()
				self.assertFalse(result["ok"], result)
				self.assertNotIn("data", result)

	def test_plugin_endpoint_requires_the_bound_read_contract(self):
		headers = {"X-Jarvis-Token": "fake-test-token"}
		payload = {"rows": [{"name": "FAKE", "value": CANARY}]}
		with (
			patch("jarvis.api._get_header", side_effect=headers.get),
			patch("jarvis.api.validate_plugin_request", return_value=self.key),
			patch.dict(registry._TOOLS, {"query": lambda **kwargs: payload}),
		):
			result = api.call_tool("query", {"spec": {"limit": 1000}}, result_mode=_private_output.MODE)
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["data"], payload)
		self.assertEqual(result["result_mode"], _private_output.MODE)
		for tool, mode in (("create_doc", _private_output.MODE), ("query", "unknown")):
			with (
				patch("jarvis.api._get_header", side_effect=headers.get),
				patch("jarvis.api.validate_plugin_request", return_value=self.key),
				patch("jarvis.api._run_tool") as execute,
			):
				self.assertFalse(api.call_tool(tool, {}, result_mode=mode)["ok"])
				execute.assert_not_called()

	def test_plugin_source_calls_refuse_ordinary_and_shared_paths_before_dispatch(self):
		headers = {"X-Jarvis-Token": "fake-test-token"}
		for tool in sorted(_private_output.PRIVATE_SOURCE_TOOLS):
			for mode in (None, "ordinary-output"):
				with (
					self.subTest(tool=tool, mode=mode),
					patch("jarvis.api._get_header", side_effect=headers.get),
					patch("jarvis.api.validate_plugin_request", return_value=self.key),
					patch("jarvis.api._dispatch_from_session") as execute,
				):
					result = api.call_tool(tool, {}, result_mode=mode)
				self.assertFalse(result["ok"], result)
				execute.assert_not_called()
			for mode in (None, _private_output.MODE):
				with (
					self.subTest(tool=tool, shared_mode=mode),
					patch("jarvis.api._get_header", side_effect=headers.get),
					patch("jarvis.api.validate_plugin_request", return_value=self.key),
					patch("jarvis.chat.collaboration.is_collaborative_session", return_value=True),
					patch(
						"jarvis.chat.collaboration.resolve_binding",
						return_value=SimpleNamespace(actor=self.actor),
					),
					patch("jarvis.chat.collaboration_dispatch.dispatch") as shared_dispatch,
					patch("jarvis.api._dispatch_from_session") as direct_dispatch,
				):
					result = api.call_tool(tool, {}, result_mode=mode)
				self.assertFalse(result["ok"], result)
				shared_dispatch.assert_not_called()
				direct_dispatch.assert_not_called()

	def test_external_output_without_private_mode_remains_uncapped(self):
		payload = {"rows": [{"name": "FAKE", "value": "x" * 200}] * 1000}
		with (
			patch("jarvis.api._get_header", return_value=None),
			patch.dict(registry._TOOLS, {"query": lambda **kwargs: payload}),
		):
			result = api.call_tool("query", {"spec": {}})
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["data"], payload)

	def test_shared_plugin_session_refuses_private_output_without_dispatch(self):
		with (
			patch("jarvis.api._get_header", side_effect={"X-Jarvis-Token": "fake-token"}.get),
			patch("jarvis.api.validate_plugin_request", return_value=self.key),
			patch("jarvis.chat.collaboration.is_collaborative_session", return_value=True),
			patch(
				"jarvis.chat.collaboration.resolve_binding", return_value=SimpleNamespace(actor=self.actor)
			),
			patch("jarvis.chat.collaboration_dispatch.dispatch") as execute,
			patch("jarvis.api._run_tool") as run_tool,
		):
			result = api.call_tool("query", {}, result_mode=_private_output.MODE)
		self.assertFalse(result["ok"])
		execute.assert_not_called()
		run_tool.assert_not_called()

	def test_native_query_handler_and_its_permission_gate_are_preserved(self):
		args = {"spec": {"from": "DocType", "alias": "d", "select": ["d.name"], "limit": 1}}
		with patch("jarvis.api._get_header", return_value=None):
			result = api._dispatch_from_session(
				"Administrator", self.key, "query", args, result_mode=_private_output.MODE
			)
		self.assertTrue(result["ok"], result)
		self.assertEqual(len(result["data"]["rows"]), 1)
		# A real Frappe identity without DocType read access receives the same
		# coded refusal; private transport cannot grant a role or field permission.
		with patch("jarvis.api._get_header", return_value=None):
			result = api._dispatch_from_session(
				"Guest", self.key, "query", args, result_mode=_private_output.MODE
			)
		self.assertFalse(result["ok"], result)
