"""Privacy canaries for the Custom App Learning private delegate path.

The test data is synthetic. It proves that source text, paths, and wiki bodies
remain on the authenticated delegate wire only. Durable chat receipts, realtime
events, structured audit rows, timelines, shared sessions, and ordinary desk
calls must not disclose or gain access to that material.
"""

from __future__ import annotations

import json
import time
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import agent_scheduler, collaboration_tools

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"

RAW_SOURCE = "PRIVATE_SOURCE_SENTINEL = 'do-not-persist'"
RAW_PATH = "fakeapp/private/source_file.py"
RAW_BODY = "PRIVATE_WIKI_BODY_SENTINEL"


class TestAppLearningPrivacyBoundary(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.session_key = f"private-app-learning:{frappe.generate_hash(length=16)}"
		self.conversation = frappe.get_doc(
			{
				"doctype": CONV,
				"title": "Synthetic Custom App Learning privacy test",
				"session_key": self.session_key,
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.delete(MSG, {"conversation": self.conversation.name})
		frappe.db.delete(CONV, self.conversation.name)
		frappe.db.commit()
		frappe.set_user("Administrator")

	def _assert_private_material_absent(self, *values) -> None:
		serialized = json.dumps(values, default=str, sort_keys=True)
		for marker in (RAW_SOURCE, RAW_PATH, RAW_BODY):
			self.assertNotIn(marker, serialized, msg=f"private material leaked: {marker}")

	def _private_args(self) -> dict:
		return {
			"app": "fakeapp",
			"path": RAW_PATH,
			"pages": [
				{
					"title": "Synthetic private page",
					"key": "private-page",
					"body_md": RAW_BODY,
					"source_paths": [RAW_PATH],
				}
			],
		}

	def _private_data(self) -> dict:
		return {
			"content": RAW_SOURCE,
			"path": RAW_PATH,
			"body_md": RAW_BODY,
			"files": [{"path": RAW_PATH, "content": RAW_SOURCE}],
			"slugs": [RAW_PATH],
			"size": 123,
			"redactions": 2,
			"applied": 1,
			"failed": 0,
			"rejected": 0,
			"idempotent": 0,
			"status": "completed",
			"pages_written": 1,
		}

	def test_receipt_projection_covers_private_aliases_and_error_shapes(self):
		"""Both registered spellings remove material on success and failure."""
		for bare in sorted(api._PRIVATE_APP_LEARNING_TOOLS):
			for tool in (bare, f"jarvis__{bare}"):
				with self.subTest(tool=tool):
					safe_args, safe_result = api._app_learning_receipt_payload(
						tool, self._private_args(), {"ok": True, "data": self._private_data()}
					)
					self._assert_private_material_absent(safe_args, safe_result)
					self.assertTrue(safe_args["private_app_learning"])
					self.assertTrue(safe_result["ok"])

					safe_args, safe_error = api._app_learning_receipt_payload(
						tool,
						self._private_args(),
						{"ok": False, "error": {"code": RAW_SOURCE, "message": RAW_PATH}},
					)
					self._assert_private_material_absent(safe_args, safe_error)
					self.assertEqual(safe_error["error"]["code"], "AppLearningToolError")

	def test_private_receipts_and_realtime_events_store_only_safe_metadata(self):
		"""The actual persistence path protects normal and error calls."""
		with mock.patch.object(api, "publish_realtime_tool_result") as publish:
			api._persist_and_publish_tool_call(
				session_key=self.session_key,
				tool="read_app_source",
				args=self._private_args(),
				result={"ok": True, "data": self._private_data()},
			)
			api._persist_and_publish_tool_call(
				session_key=self.session_key,
				tool="jarvis__record_app_wiki",
				args=self._private_args(),
				result={"ok": False, "error": {"code": RAW_SOURCE, "message": RAW_PATH}},
			)

		self.assertEqual(publish.call_count, 2)
		for call in publish.call_args_list:
			self._assert_private_material_absent(call.kwargs["args"], call.kwargs["result"])

		rows = frappe.get_all(
			MSG,
			filters={"conversation": self.conversation.name, "role": "tool"},
			fields=["tool_args", "tool_result"],
			order_by="creation asc",
		)
		self.assertEqual(len(rows), 2)
		for row in rows:
			self._assert_private_material_absent(row.tool_args, row.tool_result)

	def test_audit_and_timeline_never_retain_private_tool_material(self):
		"""Successful audit data and normal/error timeline rows are safe."""
		with mock.patch.object(api.audit, "record") as audit_record:
			api._record_tool_audit("jarvis__read_app_source", self._private_args(), self._private_data())
		self._assert_private_material_absent(audit_record.call_args.kwargs)
		self.assertTrue(audit_record.call_args.kwargs["args"]["private_app_learning"])

		with (
			mock.patch(
				"jarvis.chat.agent_run_steps.humanize_tool_call", return_value=("Safe", "")
			) as humanize,
			mock.patch("jarvis.chat.agent_run_steps.record_step") as record_step,
		):
			api._record_tool_step(
				{"name": "SYNTHETIC-RUN", "owner": "Administrator"},
				"jarvis__read_app_source",
				self._private_args(),
				self._private_data(),
				ok=True,
				started_ms=time.monotonic(),
			)
			api._record_tool_step(
				{"name": "SYNTHETIC-RUN", "owner": "Administrator"},
				"record_app_wiki",
				self._private_args(),
				None,
				ok=False,
				started_ms=time.monotonic(),
				error_message=f"failed while reading {RAW_PATH}: {RAW_SOURCE}",
			)

		for call in humanize.call_args_list:
			self._assert_private_material_absent(call.args)
		for call in record_step.call_args_list:
			self._assert_private_material_absent(call.args, call.kwargs)

	def test_capability_denial_persists_and_audits_only_safe_metadata(self):
		"""The pre-dispatch denial branch cannot bypass the private projection."""
		denial = {
			"message": f"denied {RAW_SOURCE}",
			"chat_message": f"denied {RAW_PATH}",
			"fatal": False,
		}
		with (
			mock.patch("jarvis.chat.collaboration.require_legacy_session"),
			mock.patch("jarvis.tools._delegate_capability.resolve", return_value={}),
			mock.patch("jarvis.tools._delegate_capability.tool_denial", return_value=denial),
			mock.patch.object(api, "_get_header", return_value=None),
			mock.patch.object(api.audit, "record") as audit_record,
			mock.patch.object(api, "publish_realtime_tool_result") as publish,
		):
			result = api._dispatch_from_session(
				"Administrator", self.session_key, "read_app_source", self._private_args()
			)

		self.assertFalse(result["ok"])
		self._assert_private_material_absent(audit_record.call_args.kwargs)
		self._assert_private_material_absent(
			publish.call_args.kwargs["args"], publish.call_args.kwargs["result"]
		)
		row = frappe.get_value(
			MSG,
			{"conversation": self.conversation.name, "role": "tool"},
			["tool_args", "tool_result"],
			as_dict=True,
		)
		self.assertIsNotNone(row)
		self._assert_private_material_absent(row.tool_args, row.tool_result)

	def test_private_tool_failure_and_outer_error_capture_are_projected(self):
		"""Known and unexpected private failures cannot enter the error buffers."""
		for exc in (
			api.InvalidArgumentError(f"invalid source {RAW_SOURCE} at {RAW_PATH}"),
			RuntimeError(f"unexpected source {RAW_SOURCE} at {RAW_PATH}"),
		):
			with self.subTest(exception=type(exc).__name__):
				with (
					mock.patch.object(api, "dispatch", side_effect=exc),
					mock.patch("jarvis.api_errors.record_tool_failure") as failure_sink,
				):
					result = api._dispatch_and_wrap("read_app_source", self._private_args(), False)
				self.assertFalse(result["ok"])
				self._assert_private_material_absent(
					result, failure_sink.call_args.args, failure_sink.call_args.kwargs
				)
				self.assertEqual(result["error"]["code"], "AppLearningToolError")

		raw_result = {"ok": False, "error": {"code": RAW_SOURCE, "message": RAW_PATH}}
		with (
			mock.patch.object(api, "_call_tool", return_value=raw_result),
			mock.patch("jarvis.api_errors.record_call_tool_failure") as outer_sink,
		):
			self.assertIs(api.call_tool("jarvis__read_app_source", self._private_args()), raw_result)
		self._assert_private_material_absent(outer_sink.call_args.args, outer_sink.call_args.kwargs)

	def test_scheduler_projects_a_legacy_fleet_reply_before_terminalizing(self):
		"""A pre-projection fleet host cannot put source into a Run error or relay path."""
		row = frappe._dict({"name": "SYNTHETIC-RUN", "agent": "custom-app-learning"})
		raw = {
			"status": "done",
			"finished_at": time.time(),
			"result": {
				"reply": RAW_SOURCE,
				"canvas_ref": [RAW_PATH],
				"gateway_status": "timeout",
				"summary": RAW_BODY,
			},
			"error": {"code": RAW_SOURCE, "message": RAW_PATH},
			"model_used": {
				"provider": "openai_compat",
				"model": f"{RAW_BODY} source text",
			},
		}
		# This is the exact admin relay shape that poll_dispatched_runs unwraps.
		relayed = agent_scheduler._polled_state({"ok": True, "data": raw})
		safe = agent_scheduler._safe_app_learning_fleet_state(row, relayed)
		self._assert_private_material_absent(safe)
		self.assertEqual(safe["result"]["gateway_status"], "aborted")
		self.assertEqual(safe["result"]["summary"], "aborted")
		self.assertIsNone(safe["model_used"])

		# The fallback retains only fleet's bounded model observability record.
		model_only = agent_scheduler._safe_app_learning_fleet_state(
			row,
			{
				"status": "running",
				"model_used": {"provider": "openai_compat", "model": "gpt-5.6-terra"},
			},
		)
		self.assertEqual(model_only["model_used"], {"provider": "openai_compat", "model": "gpt-5.6-terra"})
		self.assertIsNone(
			agent_scheduler._safe_app_learning_fleet_state(
				row,
				{
					"status": "running",
					"model_used": {
						"provider": "openai_compat",
						"model": "gpt-5.6-terra",
						"untrusted": RAW_SOURCE,
					},
				},
			)["model_used"]
		)

		with mock.patch.object(agent_scheduler, "_terminalize_stuck_run", return_value=True) as terminal:
			self.assertTrue(agent_scheduler._reconcile_polled_run(row, raw, frappe.utils.now_datetime()))
		self._assert_private_material_absent(terminal.call_args.args, terminal.call_args.kwargs)
		self.assertEqual(terminal.call_args.kwargs["error"], agent_scheduler._APP_LEARNING_ABORT_REPLY)

		failed_raw = {"status": "failed", "error": {"code": RAW_SOURCE, "message": RAW_PATH}}
		with mock.patch.object(agent_scheduler, "_terminalize_stuck_run", return_value=True) as terminal:
			self.assertTrue(
				agent_scheduler._reconcile_polled_run(row, failed_raw, frappe.utils.now_datetime())
			)
		self._assert_private_material_absent(terminal.call_args.args, terminal.call_args.kwargs)
		self.assertEqual(terminal.call_args.kwargs["error"], agent_scheduler._APP_LEARNING_FAILURE_MESSAGE)

	def test_shared_and_ordinary_calls_are_refused_before_source_access(self):
		"""Shared and ordinary calls cannot use this delegate-only source path."""
		self.assertTrue(
			all(collaboration_tools.tool_kind(tool) == "refused" for tool in api._PRIVATE_APP_LEARNING_TOOLS)
		)
		for tool in api._PRIVATE_APP_LEARNING_TOOLS:
			with self.subTest(shared_tool=tool):
				with (
					mock.patch.object(api, "_get_header", return_value="token"),
					mock.patch.object(api, "validate_plugin_request", return_value=self.session_key),
					mock.patch("jarvis.chat.site_pin.pin_if_unset"),
					mock.patch("jarvis.chat.collaboration.is_collaborative_session", return_value=True),
				):
					result = api._call_tool(f"jarvis__{tool}", self._private_args())
				self.assertFalse(result["ok"])
				self.assertEqual(result["error"]["code"], "PermissionError")

		with mock.patch.object(api, "_get_header", return_value=None):
			ordinary = api._call_tool("read_app_source", self._private_args())
		self.assertFalse(ordinary["ok"])
		self._assert_private_material_absent(ordinary)
