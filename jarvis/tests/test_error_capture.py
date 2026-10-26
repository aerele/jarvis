"""Every way a jarvis error reaches a user is captured and pushed to admin:
Error Log rows written without a traceback, jarvis API calls that answered
4xx, tools that returned {ok: false}, and turns that ended errored. Plus the
push guarantees: no cursor skips a row, and a batch admin refuses can't block
the ones after it."""

from __future__ import annotations

import types
from contextlib import ExitStack
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from werkzeug.wrappers import Response

from jarvis import api_errors, error_push
from jarvis.exceptions import AdminContractError, AdminValidationError

DT = api_errors.DT
USER = "errcap-user@example.com"
TURN = api_errors.TURN_DT


class CaptureBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("User", USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": USER,
					"first_name": "errcap",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.db.delete(DT)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete(DT)
		frappe.db.delete(TURN, {"name": ("like", "errcap-%")})
		frappe.db.commit()
		super().tearDown()

	def buffered(self) -> list:
		return frappe.get_all(DT, fields=["surface", "error_code", "error_class", "message", "route", "user"])


def _frame(path: str, func: str, line: int, back=None):
	return types.SimpleNamespace(
		f_code=types.SimpleNamespace(co_filename=path, co_name=func), f_lineno=line, f_back=back
	)


# --------------------------------------------------------------------------- #
# Error Log rows written with frappe.log_error(title, message)
# --------------------------------------------------------------------------- #
class TestErrorLogOrigin(CaptureBase):
	def test_a_plain_message_from_jarvis_names_its_caller(self):
		since = str(frappe.utils.now_datetime())
		doc = frappe.log_error(title="Jarvis: admin auth failed", message="status=401")
		self.addCleanup(frappe.delete_doc, "Error Log", doc.name, force=True, ignore_permissions=True)
		self.assertIn('File "apps/jarvis/jarvis/tests/test_error_capture.py"', doc.error)
		self.assertTrue(api_errors.is_jarvis_error(doc.method, doc.error))
		frappe.db.commit()
		rows = api_errors.collect_error_log(since)["rows"]
		self.assertTrue(any(r["route"] == "Jarvis: admin auth failed" for r in rows))

	def test_the_nearest_jarvis_frame_is_named(self):
		package = api_errors.os.path.dirname(api_errors.os.path.abspath(api_errors.__file__))
		outer = _frame(f"{package}/chat/pump.py", "make_mux", 40)
		inner = _frame("/bench/apps/frappe/frappe/utils/error.py", "log_error", 9, outer)
		self.assertEqual(
			api_errors._jarvis_caller(inner), '  File "apps/jarvis/jarvis/chat/pump.py", line 40, in make_mux'
		)

	def test_no_jarvis_frame_on_the_stack_names_nothing(self):
		erpnext = _frame("/bench/apps/erpnext/erpnext/stock/utils.py", "get_stock", 3)
		self.assertIsNone(api_errors._jarvis_caller(_frame("/bench/apps/frappe/x.py", "f", 1, erpnext)))

	def test_a_row_that_already_names_jarvis_is_left_alone(self):
		tb = 'Traceback:\n  File "apps/jarvis/jarvis/chat/api.py", line 3, in send\nValueError: x'
		doc = frappe._dict(error=tb)
		api_errors.tag_error_log_origin(doc)
		self.assertEqual(doc.error, tb)


# --------------------------------------------------------------------------- #
# jarvis API calls that answered 4xx
# --------------------------------------------------------------------------- #
class TestFailedRequests(CaptureBase):
	def _capture(self, path, status, *, exc_type="ValidationError", messages=None, user=USER, body=None):
		frappe.set_user(user)
		frappe.local.response = frappe._dict(exc_type=exc_type)
		frappe.local.message_log = messages if messages is not None else [{"message": "Customer is disabled"}]
		response = Response(body or "{}", status=status, mimetype="application/json")
		try:
			api_errors.capture_failed_request(response=response, request=types.SimpleNamespace(path=path))
		finally:
			frappe.local.message_log = []
			frappe.set_user("Administrator")

	def test_a_thrown_validation_error_is_buffered(self):
		self._capture("/api/method/jarvis.chat.api.send_message", 417)
		(row,) = self.buffered()
		self.assertEqual(row.surface, "api")
		self.assertEqual((row.error_code, row.error_class), ("417", "ValidationError"))
		self.assertEqual(row.route, "jarvis.chat.api.send_message")
		self.assertEqual(row.user, USER)
		self.assertIn("is disabled", row.message)

	def test_the_same_failure_on_two_endpoints_stays_two_rows(self):
		self._capture("/api/method/jarvis.chat.api.get_conversation", 404, exc_type="DoesNotExistError")
		self._capture("/api/method/jarvis.chat.api.rename_conversation", 404, exc_type="DoesNotExistError")
		self._capture("/api/method/jarvis.chat.api.rename_conversation", 404, exc_type="DoesNotExistError")
		rows = frappe.get_all(DT, fields=["route", "count"], order_by="route")
		self.assertEqual(
			[(r.route, r.count) for r in rows],
			[("jarvis.chat.api.get_conversation", 1), ("jarvis.chat.api.rename_conversation", 2)],
		)

	def test_the_v2_api_path_counts_too(self):
		self._capture("/api/v2/method/jarvis.chat.api.send_message", 403, exc_type="PermissionError")
		self.assertEqual([r.route for r in self.buffered()], ["jarvis.chat.api.send_message"])

	def test_a_tool_envelope_body_gives_the_class_and_message(self):
		body = '{"message": {"ok": false, "error": {"code": "unsigned", "message": "request not signed"}}}'
		self._capture("/api/method/jarvis.api.confirm_tool", 400, exc_type=None, messages=[], body=body)
		(row,) = self.buffered()
		self.assertEqual(row.error_class, "unsigned")
		self.assertIn("request not signed", row.message)

	def test_what_is_not_a_jarvis_failure_is_skipped(self):
		self._capture("/api/method/jarvis.chat.api.send_message", 200)
		self._capture("/api/method/jarvis.chat.api.send_message", 401)  # signed out
		self._capture("/api/method/jarvis.chat.api.send_message", 500)  # Error Log lane
		self._capture("/api/method/erpnext.stock.get_stock", 417)
		self._capture("/api/method/jarvis.api_errors.report_client_errors", 417)
		self._capture("/api/method/jarvis.api.call_tool", 400)  # records its own
		self._capture("/api/method/jarvis.chat.api.send_message", 417, user="Guest")
		self.assertEqual(self.buffered(), [])

	def test_never_raises(self):
		with patch.object(api_errors, "_request_failure", side_effect=RuntimeError("boom")):
			self._capture("/api/method/jarvis.chat.api.send_message", 417)


# --------------------------------------------------------------------------- #
# tools that returned {ok: false}
# --------------------------------------------------------------------------- #
class TestToolFailures(CaptureBase):
	def test_a_failed_tool_is_buffered(self):
		from jarvis import api

		frappe.set_user(USER)
		with patch.object(api, "dispatch", side_effect=frappe.ValidationError("Customer is disabled")):
			envelope = api._dispatch_and_wrap("get_list", {"doctype": "Customer"}, False)
		frappe.set_user("Administrator")
		self.assertFalse(envelope["ok"])
		(row,) = self.buffered()
		self.assertEqual((row.surface, row.error_class, row.route), ("tool", "get_list", "get_list"))
		self.assertEqual(row.error_code, envelope["error"]["code"])
		self.assertEqual(row.user, USER)

	def test_a_write_guard_refusal_is_not_a_failure(self):
		api_errors.record_tool_failure("set_value", {"code": "sensitive_refused", "message": "refused"})
		self.assertEqual(self.buffered(), [])


# --------------------------------------------------------------------------- #
# call_tool: rejected calls and refusals before the tool runs
# --------------------------------------------------------------------------- #
class TestCallToolFailures(CaptureBase):
	def setUp(self):
		super().setUp()
		frappe.local.response = frappe._dict()

	def _plugin_call(self, **patches):
		"""Drive the real call_tool down the container's (X-Jarvis-Token) path."""
		from jarvis import api

		with ExitStack() as stack:
			stack.enter_context(
				patch.object(api, "_get_header", side_effect=lambda h: "tok" if h == "X-Jarvis-Token" else "")
			)
			stack.enter_context(patch.object(api, "_request_body_bytes", return_value=b"{}"))
			for name, kw in patches.items():
				stack.enter_context(patch.object(api, name, **kw))
			return api.call_tool("get_list", {"doctype": "Customer"})

	def test_a_rejected_container_call_is_an_error(self):
		from jarvis._plugin_auth import PluginAuthError

		rejected = PluginAuthError(http_status=400, code="unsigned", message="request is not signed")
		result = self._plugin_call(validate_plugin_request={"side_effect": rejected})
		self.assertFalse(result["ok"])
		(row,) = self.buffered()
		self.assertEqual((row.surface, row.error_code, row.error_class), ("call_tool", "400", "unsigned"))
		self.assertEqual(row.route, "get_list")
		self.assertIn("not signed", row.message)

	def test_an_unknown_session_is_an_error(self):
		# No Jarvis Chat Session row has this key, so it resolves to no user.
		result = self._plugin_call(validate_plugin_request={"return_value": "errcap-no-such-session"})
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		(row,) = self.buffered()
		self.assertEqual((row.surface, row.error_code), ("call_tool", "400"))

	def test_a_refusal_before_the_tool_runs_is_a_tool_failure_of_the_chat_user(self):
		from jarvis import api

		def refuse(tool, args):
			frappe.local.jarvis_call_tool_user = USER
			return api._error("InvalidArgumentError", "args must be an object")

		with patch.object(api, "_call_tool", side_effect=refuse):
			api.call_tool("get_list", "not json")
		(row,) = self.buffered()
		self.assertEqual((row.surface, row.error_class, row.user), ("tool", "get_list", USER))

	def test_a_tool_failure_is_counted_once(self):
		from jarvis import api

		def failed_tool(tool, args):
			with patch.object(api, "dispatch", side_effect=frappe.ValidationError("Customer is disabled")):
				return api._dispatch_and_wrap(tool, {"doctype": "Customer"}, False)

		with patch.object(api, "_call_tool", side_effect=failed_tool):
			api.call_tool("get_list", {"doctype": "Customer"})
		self.assertEqual(len(self.buffered()), 1)

	def test_a_successful_call_records_nothing(self):
		from jarvis import api

		with patch.object(api, "_call_tool", return_value={"ok": True, "data": []}):
			api.call_tool("get_list", {})
		self.assertEqual(self.buffered(), [])


# --------------------------------------------------------------------------- #
# turns that ended errored
# --------------------------------------------------------------------------- #
class TestFailedTurns(CaptureBase):
	def setUp(self):
		super().setUp()
		frappe.db.delete(TURN, {"name": ("like", "errcap-%")})
		frappe.db.commit()

	def _turn(self, run_id, *, state="errored", error="Relay connection closed", done_at=None, owner=USER):
		frappe.set_user(owner)
		doc = frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": run_id,
				"conversation": "errcap-conv",
				"relay_target_id": "t",
				"turn_class": "interactive",
				"state": state,
				"seed_message": "m",
				"error": error,
				"done_at": done_at or frappe.utils.now_datetime(),
			}
		)
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)
		frappe.set_user("Administrator")
		return doc

	def test_errored_turns_are_folded_per_error_and_user(self):
		since = str(frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-1))
		self._turn("errcap-1")
		self._turn("errcap-2")
		self._turn("errcap-3", state="done", error="")
		result = api_errors.collect_failed_turns(since)
		(row,) = result["rows"]
		self.assertEqual(row["count"], 2)
		self.assertEqual((row["kind"], row["surface"], row["severity"]), ("exception", "turn", "error"))
		self.assertEqual(row["user_ref"], USER)
		self.assertEqual(row["run_id"], "errcap-2")
		# The cursor moved past both, so the next cycle reads neither again.
		self.assertEqual(api_errors.collect_failed_turns(result["watermark"])["rows"], [])

	def test_a_turn_inside_the_lag_waits_for_the_next_cycle(self):
		since = str(frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-1))
		self._turn("errcap-4")
		upto = str(frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-60))
		result = api_errors.collect_failed_turns(since, upto=upto)
		self.assertEqual(result["rows"], [])
		self.assertEqual(result["watermark"], since)

	def test_turns_sharing_a_timestamp_are_all_read(self):
		at = frappe.utils.now_datetime()
		since = str(frappe.utils.add_to_date(at, seconds=-1))
		self._turn("errcap-5", done_at=at, error="first failure")
		self._turn("errcap-6", done_at=at, error="second failure")
		first = api_errors.collect_failed_turns(since, max_groups=1)
		self.assertEqual([r["run_id"] for r in first["rows"]], ["errcap-5"])
		second = api_errors.collect_failed_turns(first["watermark"], max_groups=1)
		self.assertEqual([r["run_id"] for r in second["rows"]], ["errcap-6"])

	def test_the_browser_copy_of_a_failed_turn_is_not_counted_twice(self):
		frappe.set_user(USER)
		api_errors.report_client_errors(
			[
				{"surface": "spa_chat", "error_class": "RunError", "message": "The run failed."},
				{"surface": "turn", "error_class": "TypeError", "message": "x is undefined"},
			]
		)
		frappe.set_user("Administrator")
		self.assertEqual([r.surface for r in self.buffered()], ["ui"])


# --------------------------------------------------------------------------- #
# Error Log scan: paging and grouping
# --------------------------------------------------------------------------- #
class TestErrorLogScan(CaptureBase):
	def _log(self, title):
		doc = frappe.log_error(title=title, message="plain")
		self.addCleanup(frappe.delete_doc, "Error Log", doc.name, force=True, ignore_permissions=True)

	def test_reads_past_one_page_and_folds_repeats(self):
		since = str(frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-1))
		for _ in range(5):
			self._log("errcap repeat")
		frappe.db.commit()
		with patch.object(api_errors, "ERROR_LOG_SCAN_LIMIT", 2):
			rows = api_errors.collect_error_log(since)["rows"]
		(row,) = [r for r in rows if r["route"] == "errcap repeat"]
		self.assertEqual(row["count"], 5)

	def test_a_full_batch_leaves_the_rest_for_the_next_cycle(self):
		since = str(frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-1))
		self._log("errcap first")
		with patch.object(api_errors, "_traceback_fingerprint", side_effect=["a", "b"]):
			first = api_errors.collect_error_log(since, max_groups=1)
		self.assertEqual([r["route"] for r in first["rows"]], ["errcap first"])
		self._log("errcap second")
		second = api_errors.collect_error_log(first["watermark"])
		self.assertEqual([r["route"] for r in second["rows"]], ["errcap second"])


# --------------------------------------------------------------------------- #
# Push: lanes, refusals
# --------------------------------------------------------------------------- #
class TestPush(CaptureBase):
	def setUp(self):
		super().setUp()
		for key in (error_push._WATERMARK_KEY, error_push._TURN_WATERMARK_KEY):
			frappe.db.set_default(key, str(frappe.utils.now_datetime()))
		frappe.db.commit()

	def _push(self, impl):
		from jarvis import admin_client

		with patch.object(admin_client, "push_error_rollup", impl):
			error_push._do_push()

	def test_server_rows_go_as_exceptions_at_their_severity(self):
		api_errors.record_server_error(
			"tool", error_code="e", error_class="get_list", message="boom", user=USER
		)
		frappe.set_user(USER)
		api_errors.report_client_errors([{"surface": "spa", "error_code": "x", "message": "ui boom"}])
		frappe.set_user("Administrator")
		push = Mock()
		self._push(push)
		sent = {e["surface"]: (e["kind"], e["severity"]) for e in push.call_args[0][0]}
		self.assertEqual(sent, {"tool": ("exception", "warning"), "spa": ("ui", "error")})

	def test_a_refused_batch_is_dropped_not_resent_forever(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		with self.assertRaises(AdminValidationError):
			self._push(Mock(side_effect=AdminValidationError("too many errors", exc_type="ValidationError")))
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 0)

	def test_a_stopped_tenant_keeps_the_batch_for_later(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		refusal = AdminContractError("customer has no running tenant", code="NoRunningTenant")
		with self.assertRaises(AdminValidationError):
			self._push(Mock(side_effect=refusal))
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 1)
		# and the scheduler entry stays quiet about it
		with (
			patch.object(error_push, "_admin_configured", return_value=True),
			patch.object(error_push, "_do_push", side_effect=refusal),
			patch.object(error_push, "_log_push_failure_throttled") as logged,
		):
			error_push.push_error_rollup()
		logged.assert_not_called()

	def test_the_first_run_of_the_turn_lane_starts_a_day_back(self):
		frappe.db.set_default(error_push._TURN_WATERMARK_KEY, "")
		with patch.object(
			api_errors, "collect_failed_turns", return_value={"rows": [], "watermark": None}
		) as c:
			self._push(Mock())
		since = frappe.utils.get_datetime(c.call_args[0][0])
		age = frappe.utils.now_datetime() - since
		self.assertAlmostEqual(age.total_seconds() / 3600, error_push.TURN_BACKFILL_HOURS, delta=0.1)
