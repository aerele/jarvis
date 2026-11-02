"""Every way a jarvis error reaches a user is captured and pushed to admin:
Error Log rows written without a traceback, jarvis API calls that answered
4xx, tools that returned {ok: false}, and turns that ended errored. Plus the
push guarantees: no cursor skips a row, and a batch admin refuses can't block
the ones after it."""

from __future__ import annotations

import json
import os
import tempfile
import types
from contextlib import ExitStack
from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase
from werkzeug.wrappers import Response

from jarvis import api_errors, error_push
from jarvis._redis_lock import redis_lock
from jarvis.exceptions import (
	AdminAmbiguousError,
	AdminAuthError,
	AdminContractError,
	AdminRejectedError,
	AdminUnreachableError,
	AdminValidationError,
)

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


def _ago(**kw):
	return frappe.utils.add_to_date(frappe.utils.now_datetime(), **{k: -v for k, v in kw.items()})


def _make_turn(run_id, *, state="errored", error="Relay connection closed", done_at=None, owner=USER):
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


def _frame(path: str, func: str, line: int, back=None):
	return types.SimpleNamespace(
		f_code=types.SimpleNamespace(co_filename=path, co_name=func), f_lineno=line, f_back=back
	)


def _exec_in(rel_path: str, func: str):
	"""A function whose code lives at ``<bench>/<rel_path>``, so the stack it makes
	names that file: it calls ``frappe.log_error(title, message)``."""
	src = f"def {func}(title):\n    return frappe.log_error(title=title, message='plain body')\n"
	scope = {"frappe": frappe}
	exec(compile(src, f"{frappe.utils.get_bench_path()}/{rel_path}", "exec"), scope)
	return scope[func]


def _error_log(method: str, error: str, *, at=None, name=None) -> str:
	"""An Error Log row written WITHOUT hooks (as ERPNext code with no jarvis frame on
	the stack would), at a chosen time. Returns its name; removed after the test."""
	doc = frappe.get_doc({"doctype": "Error Log", "method": method, "error": error})
	doc.set_new_name()
	if name:
		doc.name = name
	if at:
		doc.creation = doc.modified = at
	doc.db_insert()
	frappe.db.commit()
	return doc.name


def _jarvis_tb(func: str = "boom", exc: str = "ValueError: bad") -> str:
	return (
		"Traceback (most recent call last):\n"
		f'  File "apps/jarvis/jarvis/chat/api.py", line 1, in {func}\n{exc}'
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

	def test_the_direct_caller_of_log_error_is_named(self):
		package = api_errors._JARVIS_PACKAGE
		caller = _frame(f"{package}chat/pump.py", "make_mux", 40)
		log_error = _frame(f"{api_errors._frappe_package()}utils/error.py", "log_error", 9, caller)
		hook_caller = _frame(f"{api_errors._frappe_package()}model/document.py", "run_method", 3, log_error)
		self.assertEqual(
			api_errors._jarvis_caller(hook_caller),
			'  File "apps/jarvis/jarvis/chat/pump.py", line 40, in make_mux',
		)

	def test_jarvis_code_below_another_apps_log_error_call_names_nothing(self):
		# ERPNext calls log_error in the middle of a jarvis tool call: jarvis is on
		# the stack but is not the caller.
		jarvis = _frame(f"{api_errors._JARVIS_PACKAGE}api.py", "_dispatch_and_wrap", 5)
		erpnext = _frame("/bench/apps/erpnext/erpnext/stock/utils.py", "get_stock", 3, jarvis)
		log_error = _frame(f"{api_errors._frappe_package()}utils/error.py", "log_error", 9, erpnext)
		self.assertIsNone(
			api_errors._jarvis_caller(_frame("/x/frappe/model/document.py", "run", 1, log_error))
		)

	def test_no_log_error_frame_names_nothing(self):
		jarvis = _frame(f"{api_errors._JARVIS_PACKAGE}api.py", "f", 5)
		self.assertIsNone(api_errors._jarvis_caller(_frame("/bench/apps/frappe/x.py", "g", 1, jarvis)))

	def test_a_log_wrapper_is_replaced_by_its_caller(self):
		package = api_errors._JARVIS_PACKAGE
		site = _frame(f"{package}chat/pump.py", "make_mux", 40)
		wrapper = _frame(f"{package}chat/pump.py", "_log_failure", 7, site)
		log_error = _frame(f"{api_errors._frappe_package()}utils/error.py", "log_error", 9, wrapper)
		self.assertEqual(
			api_errors._jarvis_caller(_frame("/x/frappe/model/document.py", "run", 1, log_error)),
			'  File "apps/jarvis/jarvis/chat/pump.py", line 40, in make_mux',
		)

	def test_a_real_log_error_from_another_app_is_not_tagged(self):
		# The real call stack, hook included: a non-jarvis function calls
		# frappe.log_error while this (jarvis) test is further down the stack.
		doc = _exec_in("apps/erpnext/erpnext/stock/utils.py", "log_it")("erpnext stock thing")
		self.addCleanup(frappe.delete_doc, "Error Log", doc.name, force=True, ignore_permissions=True)
		self.assertNotIn("Logged from", doc.error)
		self.assertFalse(api_errors.is_jarvis_error(doc.method, doc.error))

	def test_a_real_log_wrapper_names_the_function_that_called_it(self):
		# The probe's file name must not sit in the app tree: it does not exist, and
		# coverage (source = apps/jarvis) would fail its report on it (NoSource).
		package = os.path.join(tempfile.gettempdir(), "zz_errcap_pkg") + os.sep  # never created
		src = (
			"def _log_it(title):\n    return frappe.log_error(title=title, message='plain')\n"
			"def real_site(title):\n    return _log_it(title)\n"
		)
		scope = {"frappe": frappe}
		exec(compile(src, f"{package}chat/zz_errcap_probe.py", "exec"), scope)
		with patch.object(api_errors, "_JARVIS_PACKAGE", package):
			doc = scope["real_site"]("errcap wrapper")
		self.addCleanup(frappe.delete_doc, "Error Log", doc.name, force=True, ignore_permissions=True)
		self.assertIn("in real_site", doc.error)
		self.assertNotIn("in _log_it", doc.error)

	def test_the_reporters_own_log_rows_are_tagged_and_so_stay_local(self):
		# error_push writes its "stalled" / "dropped" rows itself; the tag must name
		# its own file so is_jarvis_error keeps them out of the feed, and the hook
		# must not recurse.
		error_push._state_set("jarvis_error_push_log_stalled", "")
		self.addCleanup(error_push._state_set, "jarvis_error_push_log_stalled", "")
		self.addCleanup(
			frappe.db.delete, "Error Log", {"method": ("like", "jarvis errors: rollup push stalled%")}
		)
		saved = error_push._state_get(error_push._OK_KEY)
		self.addCleanup(error_push._state_set, error_push._OK_KEY, saved or "")
		frappe.db.delete("Error Log", {"method": ("like", "jarvis errors: rollup push stalled%")})
		error_push._state_set(error_push._OK_KEY, str(_ago(hours=2)))
		error_push._note_failure(AdminUnreachableError("down"))
		(row,) = frappe.get_all(
			"Error Log",
			filters={"method": ("like", "jarvis errors: rollup push stalled%")},
			fields=["method", "error"],
		)
		self.assertIn("apps/jarvis/jarvis/error_push.py", row.error)
		self.assertFalse(api_errors.is_jarvis_error(row.method, row.error))

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
		self._capture("/api/method/jarvis.chat.api.send_message", 429)  # rate limit as designed
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
		frappe.set_user("Guest")  # no valid token: nobody is known yet
		result = self._plugin_call(validate_plugin_request={"side_effect": rejected})
		self.assertFalse(result["ok"])
		(row,) = self.buffered()
		self.assertEqual((row.surface, row.error_code, row.error_class), ("call_tool", "400", "unsigned"))
		self.assertEqual(row.route, api_errors.UNAUTHENTICATED_ROUTE)
		self.assertIn("not signed", row.message)

	def test_an_unknown_session_is_an_error(self):
		# No Jarvis Chat Session row has this key, so it resolves to no user.
		frappe.set_user("Guest")
		result = self._plugin_call(validate_plugin_request={"return_value": "errcap-no-such-session"})
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		(row,) = self.buffered()
		self.assertEqual((row.surface, row.error_code), ("call_tool", "400"))
		self.assertEqual(row.route, api_errors.UNAUTHENTICATED_ROUTE)

	def test_an_unauthenticated_401_is_not_recorded(self):
		# Internet probes of an allow_guest endpoint, and plugin-auth rejections that
		# already write their own Error Log row: neither belongs in the tenant feed.
		from jarvis import api
		from jarvis._plugin_auth import PluginAuthError

		frappe.set_user("Guest")
		with patch.object(api, "_get_header", return_value=""):
			result = api.call_tool("get_list", {"doctype": "Customer"})  # no token header at all
		self.assertEqual(result["error"]["message"], "authentication required")
		self.assertEqual(frappe.local.response.http_status_code, 401)
		self.assertEqual(self.buffered(), [])

		bad_token = PluginAuthError(http_status=401, code="invalid_token", message="bad bearer")
		result = self._plugin_call(validate_plugin_request={"side_effect": bad_token})
		self.assertFalse(result["ok"])
		self.assertEqual(self.buffered(), [])

	def test_a_stale_pairing_401_is_an_error_of_the_chat_user(self):
		# The chat user is known by then, and the customer's chat is dead (jarvis #712).
		from jarvis import api

		frappe.set_user("Guest")
		with (
			patch("jarvis.chat.collaboration.is_collaborative_session", return_value=False),
			patch("jarvis.chat.collaboration.legacy_session_user", return_value=USER),
			patch("jarvis.chat.device.session_device_is_stale", return_value=True),
			patch.object(api, "_notify_stale_pairing"),
		):
			result = self._plugin_call(validate_plugin_request={"return_value": "errcap-stale-session"})
		self.assertEqual(result["error"]["code"], "AuthenticationError")
		(row,) = self.buffered()
		self.assertEqual((row.surface, row.error_code, row.user), ("call_tool", "401", USER))
		self.assertNotEqual(row.route, api_errors.UNAUTHENTICATED_ROUTE)

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
		(row,) = frappe.get_all(DT, fields=["surface", "count"])
		self.assertEqual((row.surface, row.count), ("tool", 1))

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

	def _turn(self, run_id, **kw):
		return _make_turn(run_id, **kw)

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
		_make_turn("errcap-copy-1")
		frappe.set_user(USER)
		api_errors.report_client_errors(
			[
				{
					"surface": "spa_chat",
					"error_class": "RunError",
					"message": "The run failed.",
					"run_id": "errcap-copy-1",
				},
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
class PushBase(CaptureBase):
	KEYS = (
		error_push._WATERMARK_KEY,
		error_push._TURN_WATERMARK_KEY,
		error_push._OK_KEY,
		error_push._ATTEMPT_KEY,
		error_push._FAILURES_KEY,
		error_push._CAUSE_KEY,
		error_push._BATCH_KEY,
		error_push._INFLIGHT_KEY,
		error_push._RETRY_AFTER_KEY,
		*(
			f"jarvis_error_push_log_{k}"
			for k in ("stalled", "dropped", "failed", "scan_error_log", "scan_failed_turns", "pruned_unsent")
		),
	)

	def setUp(self):
		super().setUp()
		saved = {k: error_push._state_get(k) for k in self.KEYS}
		self.addCleanup(self._restore, saved)
		for key in self.KEYS:
			error_push._state_set(key, "")
		for key in (error_push._WATERMARK_KEY, error_push._TURN_WATERMARK_KEY):
			error_push._state_set(key, str(frappe.utils.now_datetime()))
		frappe.db.commit()

	@staticmethod
	def _restore(saved):
		for key, value in saved.items():
			error_push._state_set(key, value or "")
		frappe.db.commit()

	def _push(self, impl):
		from jarvis import admin_client

		with patch.object(admin_client, "push_error_rollup", impl):
			error_push._do_push()


class TestPush(PushBase):
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

	def test_a_batch_admin_refuses_as_invalid_is_dropped_and_the_cursors_move(self):
		old = str(frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-30))
		for key in (error_push._WATERMARK_KEY, error_push._TURN_WATERMARK_KEY):
			error_push._state_set(key, old)
		self.addCleanup(frappe.db.delete, "Error Log", {"method": "errcap invalid"})
		_error_log("errcap invalid", _jarvis_tb("invalid"), at=_ago(minutes=10))
		_make_turn("errcap-inv-1", done_at=_ago(minutes=10))
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		refusal = AdminContractError("too many errors", code="InvalidErrorRollup")
		self._push(Mock(side_effect=refusal))  # a drop returns; it does not raise
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 0)
		for key in (error_push._WATERMARK_KEY, error_push._TURN_WATERMARK_KEY):
			self.assertGreater(
				frappe.utils.get_datetime(error_push._state_get(key).split("|")[0]),
				frappe.utils.get_datetime(old),
			)
		self.assertTrue(
			frappe.db.exists("Error Log", {"method": "jarvis errors: admin refused rollup batch"}),
			"a dropped batch is logged under its own title",
		)
		self.addCleanup(
			frappe.db.delete, "Error Log", {"method": "jarvis errors: admin refused rollup batch"}
		)

	def test_any_other_refusal_reverts_and_keeps_the_cursors(self):
		# A proxy 404/413 or a deploy-order ValidationError says nothing about the
		# batch: it is retried, not dropped.
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		before = {
			k: error_push._state_get(k) for k in (error_push._WATERMARK_KEY, error_push._TURN_WATERMARK_KEY)
		}
		with self.assertRaises(AdminValidationError):
			self._push(Mock(side_effect=AdminValidationError("nope", exc_type="ValidationError")))
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 1)
		self.assertEqual(before, {k: error_push._state_get(k) for k in before})

	def test_a_batch_admin_keeps_failing_is_dropped_after_twelve_cycles(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		failing = Mock(side_effect=AdminUnreachableError("admin returned a 500 error: boom", status_code=500))
		for _ in range(error_push.MAX_BATCH_FAILURES - 1):
			with self.assertRaises(AdminUnreachableError):
				self._push(failing)
			self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 1)
		self._push(failing)  # the last one drops it
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 0)
		self.assertEqual(frappe.db.count(DT, {"pushed": 1}), 1)
		self.addCleanup(
			frappe.db.delete, "Error Log", {"method": "jarvis errors: admin refused rollup batch"}
		)

	def test_outages_and_stopped_tenants_never_count_toward_a_drop(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		transport = AdminUnreachableError("admin is unreachable")
		transport.__cause__ = requests.ConnectionError("refused")
		stopped = AdminContractError("no running tenant", code="NoRunningTenant")
		for exc in (transport, stopped, AdminAuthError("401", status_code=401)):
			for _ in range(error_push.MAX_BATCH_FAILURES + 1):
				with self.assertRaises(type(exc)):
					self._push(Mock(side_effect=exc))
			self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 1, type(exc).__name__)

	def test_a_success_resets_the_failure_count(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		failing = Mock(side_effect=AdminUnreachableError("admin returned a 500 error", status_code=500))
		for _ in range(error_push.MAX_BATCH_FAILURES - 1):
			with self.assertRaises(AdminUnreachableError):
				self._push(failing)
		self._push(Mock())
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m2", user=USER)
		with self.assertRaises(AdminUnreachableError):
			self._push(failing)
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 1)
		self.assertEqual(error_push._state_get(error_push._FAILURES_KEY), "0")

	def test_a_gateway_answer_never_counts_toward_a_drop(self):
		frappe.db.delete("Error Log", {"method": "jarvis errors: admin refused rollup batch"})
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		for status in (502, 503, 504):
			gateway = AdminUnreachableError(
				f"admin returned non-JSON response (status {status})", status_code=status
			)
			for _ in range(error_push.MAX_BATCH_FAILURES + 1):
				with self.assertRaises(AdminUnreachableError):
					self._push(Mock(side_effect=gateway))
			self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 1, status)
		self.assertFalse(
			frappe.db.exists("Error Log", {"method": "jarvis errors: admin refused rollup batch"})
		)

	def test_a_read_timeout_never_counts_toward_a_drop(self):
		frappe.db.delete("Error Log", {"method": "jarvis errors: admin refused rollup batch"})
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		timeout = AdminAmbiguousError("admin did not answer in time")
		timeout.__cause__ = requests.ReadTimeout()
		for _ in range(error_push.MAX_BATCH_FAILURES + 1):
			with self.assertRaises(AdminAmbiguousError):
				self._push(Mock(side_effect=timeout))
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 1)
		self.assertFalse(
			frappe.db.exists("Error Log", {"method": "jarvis errors: admin refused rollup batch"})
		)

	def test_a_refusal_admin_will_not_take_back_counts_even_on_a_502(self):
		# Admin's fleet layer refuses with a 502 too: that is an answer, not a gateway fault.
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		refusal = AdminRejectedError("admin returned a 502 error: no", code="FleetConfigError")
		refusal.status_code = 502  # if a status is ever attached, the refusal still counts
		for _ in range(error_push.MAX_BATCH_FAILURES - 1):
			with self.assertRaises(AdminRejectedError):
				self._push(Mock(side_effect=refusal))
		self._push(Mock(side_effect=refusal))
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 0)
		self.addCleanup(
			frappe.db.delete, "Error Log", {"method": "jarvis errors: admin refused rollup batch"}
		)

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
		error_push._state_set(error_push._TURN_WATERMARK_KEY, "")
		with patch.object(
			api_errors, "collect_failed_turns", return_value={"rows": [], "watermark": None}
		) as c:
			self._push(Mock())
		since = frappe.utils.get_datetime(c.call_args[0][0])
		age = frappe.utils.now_datetime() - since
		self.assertAlmostEqual(age.total_seconds() / 3600, error_push.TURN_BACKFILL_HOURS, delta=0.1)


# --------------------------------------------------------------------------- #
# F1: nothing a caller types picks how many rows are written
# --------------------------------------------------------------------------- #
class TestPushBackoff(PushBase):
	"""An outage is waited out with growing gaps; an answered failure is not."""

	START = "2026-10-22 10:00:00"

	def setUp(self):
		super().setUp()
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		frappe.db.commit()

	def _tick(self, minutes: int, outcome, seconds: int = 0) -> bool:
		"""Run the scheduler entry ``minutes`` after START; True when it called admin."""
		from jarvis import admin_client

		at = frappe.utils.get_datetime(frappe.utils.add_to_date(self.START, minutes=minutes, seconds=seconds))
		sender = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock()
		with (
			# Stdlib mock, not freezegun: CI does not install it. error_push reads the
			# clock through frappe.utils.now_datetime() only.
			patch("frappe.utils.now_datetime", return_value=at),
			patch.object(error_push, "_admin_configured", return_value=True),
			patch.object(admin_client, "push_error_rollup", sender),
		):
			error_push.push_error_rollup()
		return sender.called

	def test_an_outage_backs_off_and_a_success_resets_it(self):
		down = AdminUnreachableError("admin returned non-JSON response (status 503)", status_code=503)
		calls = [m for m in range(0, 241) if m % 5 == 0 and self._tick(m, down)]
		self.assertEqual(calls, [0, 5, 15, 35, 75, 135, 195])  # 5, 10, 20, 40, then 60 min apart
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 1)  # nothing was dropped
		self.assertTrue(error_push._state_get(error_push._RETRY_AFTER_KEY))
		self.assertFalse(self._tick(200, down), "a tick inside the wait does not call admin")
		self.assertTrue(self._tick(255, None), "once the wait is over a success goes through")
		self.assertEqual(error_push._state_get(error_push._RETRY_AFTER_KEY), "")
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m2", user=USER)
		frappe.db.commit()
		self.assertTrue(self._tick(260, None), "after a success the next tick calls admin again")

	def test_a_timeout_backs_off_too(self):
		timeout = AdminAmbiguousError("admin did not answer in time")
		timeout.__cause__ = requests.ReadTimeout()
		self.assertTrue(self._tick(0, timeout))
		self.assertFalse(self._tick(3, timeout))
		self.assertTrue(self._tick(5, timeout))

	def test_a_tick_that_runs_a_little_late_is_not_skipped_a_cycle(self):
		down = AdminUnreachableError("admin returned a 503 error", status_code=503)
		self.assertTrue(self._tick(0, down, seconds=30))  # the job ran 30 s late
		self.assertTrue(self._tick(5, down), "the next */5 tick still finds the wait over")

	def test_a_dropped_batch_starts_the_next_outage_at_the_first_wait(self):
		poison = AdminUnreachableError("admin returned a 500 error: boom", status_code=500)
		for n in range(error_push.MAX_BATCH_FAILURES):
			self.assertTrue(self._tick(n * 5, poison))  # the last one drops the batch
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 0)
		self.addCleanup(
			frappe.db.delete, "Error Log", {"method": "jarvis errors: admin refused rollup batch"}
		)
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m2", user=USER)
		frappe.db.commit()
		down = AdminUnreachableError("admin returned a 502 error", status_code=502)
		self.assertTrue(self._tick(60, down))
		self.assertTrue(self._tick(65, down), "the first wait is 5 minutes, not 60")

	def test_an_answered_failure_does_not_back_off(self):
		answered = AdminUnreachableError("admin returned a 500 error: boom", status_code=500)
		self.assertTrue(self._tick(0, answered))
		self.assertEqual(error_push._state_get(error_push._RETRY_AFTER_KEY), "")
		self.assertTrue(self._tick(5, answered))

	def test_an_answered_failure_clears_a_wait_that_was_set(self):
		error_push._state_set(error_push._RETRY_AFTER_KEY, str(frappe.utils.add_days(self.START, 1)))
		answered = AdminUnreachableError("admin returned a 500 error: boom", status_code=500)
		with self.assertRaises(AdminUnreachableError):
			self._push(Mock(side_effect=answered))
		self.assertEqual(error_push._state_get(error_push._RETRY_AFTER_KEY), "")

	def test_an_unreadable_wait_is_no_wait(self):
		error_push._state_set(error_push._RETRY_AFTER_KEY, "not a time")
		self.assertTrue(self._tick(0, None))

	def test_clearing_the_wait_pushes_at_once(self):
		down = AdminUnreachableError("admin returned a 502 error", status_code=502)
		self.assertTrue(self._tick(0, down))
		self.assertFalse(self._tick(1, None))
		error_push._state_set(error_push._RETRY_AFTER_KEY, "")
		self.assertTrue(self._tick(1, None))

	def test_a_failing_drop_decision_still_reverts_the_claim(self):
		answered = AdminUnreachableError("admin returned a 500 error: boom", status_code=500)
		with (
			patch.object(error_push, "_batch_id", side_effect=RuntimeError("db hiccup")),
			self.assertRaises(AdminUnreachableError),
		):
			self._push(Mock(side_effect=answered))
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 1)
		self.assertEqual(error_push._state_get(error_push._INFLIGHT_KEY), "")


class TestHostileInput(CaptureBase):
	def setUp(self):
		super().setUp()
		frappe.local.response = frappe._dict(http_status_code=400)
		frappe.set_user("Guest")
		frappe.local.jarvis_call_tool_user = None
		frappe.local.jarvis_tool_failure_recorded = False

	def test_rejected_calls_for_any_tool_name_share_one_row(self):
		rejected = {"ok": False, "error": {"code": "unsigned", "message": "request is not signed"}}
		for tool in ("a1", "b2", "../../etc", "x" * 500):
			api_errors.record_call_tool_failure(tool, rejected)
		(row,) = frappe.get_all(DT, fields=["route", "count", "user", "error_class"])
		self.assertEqual((row.route, row.count, row.user), ("unauthenticated", 4, "Guest"))
		self.assertEqual(row.error_class, "unsigned")

	def test_an_unknown_tool_is_one_row_on_the_tool_lane(self):
		for tool in ("made_up_1", "made_up_2"):
			api_errors.record_tool_failure(
				tool, {"code": "ToolNotFoundError", "message": f"no such tool: {tool}"}
			)
		(row,) = frappe.get_all(DT, fields=["route", "error_class", "count", "message"])
		self.assertEqual((row.route, row.error_class, row.count), ("unknown_tool", "unknown_tool", 2))

	def test_an_unknown_tool_is_one_row_for_a_known_caller_too(self):
		frappe.local.jarvis_call_tool_user = USER
		for tool in ("made_up_1", "made_up_2"):
			api_errors.record_call_tool_failure(
				tool, {"ok": False, "error": {"code": "PermissionError", "message": "no"}}
			)
		(row,) = frappe.get_all(DT, fields=["route", "count", "user"])
		self.assertEqual((row.route, row.count, row.user), ("unknown_tool", 2, USER))

	def test_an_unknown_endpoint_is_one_row(self):
		for path in ("/api/method/jarvis.nope.one", "/api/method/jarvis.nope.two"):
			TestFailedRequests._capture(self, path, 404, exc_type="DoesNotExistError")
		(row,) = frappe.get_all(DT, fields=["route", "count"])
		self.assertEqual((row.route, row.count), ("unknown_method", 2))

	def test_the_rate_bucket_is_per_surface(self):
		with patch.object(api_errors, "_over_report_rate_limit", return_value=False) as limited:
			api_errors.record_server_error("tool", error_code="e", error_class="c", message="m", user=USER)
			api_errors.record_server_error(
				"call_tool", error_code="e", error_class="c", message="m", user=USER
			)
		self.assertEqual(
			[c.args[1] for c in limited.call_args_list], ["server_error.tool", "server_error.call_tool"]
		)

	def test_a_burst_is_capped_per_minute(self):
		frappe.flags.in_test = False
		self.addCleanup(setattr, frappe.flags, "in_test", True)
		stamp = 1_700_000_000 + 60 * int(frappe.generate_hash(length=6), 16)  # a minute no one used
		with (
			patch.object(api_errors, "time", types.SimpleNamespace(time=lambda: stamp)),
			patch.object(api_errors, "SERVER_RATE_PER_MIN", 3),
		):
			for i in range(8):
				api_errors.record_server_error(
					"api",
					error_code="e",
					error_class="c",
					message=f"distinct failure {chr(97 + i)}",
					user=USER,
				)
		self.assertEqual(len(self.buffered()), 3)


class TestPrune(CaptureBase):
	def _rows(self, n, *, pushed, days_old):
		at = frappe.utils.add_days(frappe.utils.now_datetime(), -days_old)
		for i in range(n):
			frappe.get_doc(
				{
					"doctype": DT,
					"surface": "tool",
					"fingerprint": f"errcap-prune-{pushed}-{i}",
					"user": USER,
					"count": 1,
					"first_seen": at,
					"last_seen": at,
					"pushed": pushed,
					"pushed_at": at if pushed else None,
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()

	def test_the_prune_works_through_more_than_one_batch(self):
		self._rows(5, pushed=1, days_old=error_push.PUSHED_RETENTION_DAYS + 2)
		with patch.object(error_push, "PRUNE_BATCH", 2):
			self.assertEqual(error_push.prune_pushed_client_errors(), 5)
		self.assertEqual(frappe.db.count(DT), 0)

	def test_the_buffer_ceiling_is_enforced_in_batches(self):
		self._rows(7, pushed=0, days_old=0)
		self.addCleanup(frappe.db.delete, "Error Log", {"method": "jarvis errors: unsent errors pruned"})
		with (
			patch.object(error_push, "PRUNE_BATCH", 2),
			patch.object(error_push, "MAX_BUFFER_ROWS", 3),
			patch.object(error_push, "_log_throttled") as logged,
		):
			self.assertEqual(error_push.prune_pushed_client_errors(), 4)
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 3)
		self.assertEqual(logged.call_args.args[2], "jarvis errors: unsent errors pruned")

	def test_deleting_only_sent_rows_logs_nothing(self):
		self._rows(2, pushed=1, days_old=error_push.PUSHED_RETENTION_DAYS + 2)
		with patch.object(error_push, "_log_throttled") as logged:
			error_push.prune_pushed_client_errors()
		logged.assert_not_called()


# --------------------------------------------------------------------------- #
# F2: the claim, the fold and the call do not fight over a row
# --------------------------------------------------------------------------- #
class TestFold(CaptureBase):
	def _fold(self, message="boom"):
		api_errors._fold(
			surface="tool",
			error_code="e",
			error_class="t",
			message=message,
			stack="",
			route="r",
			user=USER,
			lock_wait_s=0,
		)

	def test_repeats_are_one_row_with_a_count(self):
		for _ in range(3):
			self._fold()
		(row,) = frappe.get_all(DT, fields=["count"])
		self.assertEqual(row.count, 3)

	def test_a_row_the_push_has_claimed_is_never_written_back_to_unpushed(self):
		self._fold()
		names, rows = error_push._collect_ui_errors()  # the claim, uncommitted
		self._fold()  # the same error again, mid-flight
		claimed = frappe.db.get_value(DT, names[0], ["pushed", "count"], as_dict=True)
		self.assertEqual((claimed.pushed, claimed.count), (1, 1))
		(fresh,) = frappe.get_all(DT, filters={"pushed": 0}, fields=["count"])
		self.assertEqual(fresh.count, 1)

	def test_a_row_claimed_between_the_lookup_and_the_update_is_not_incremented(self):
		# The race the pushed = 0 guard exists for: the lookup finds the row, the
		# push claims it, and only then does the UPDATE run.
		self._fold()
		names, _rows = error_push._collect_ui_errors()
		real = frappe.db.get_value

		def stale_lookup(doctype, filters=None, *args, **kwargs):
			if doctype == DT and isinstance(filters, dict) and "fingerprint" in filters:
				return names[0]
			return real(doctype, filters, *args, **kwargs)

		with patch.object(frappe.db, "get_value", stale_lookup):
			self._fold()
		claimed = real(DT, names[0], ["pushed", "count"], as_dict=True)
		self.assertEqual((claimed.pushed, claimed.count), (1, 1))
		(fresh,) = frappe.get_all(DT, filters={"pushed": 0}, fields=["count"])
		self.assertEqual(fresh.count, 1)

	def test_a_failed_fold_leaves_the_rest_of_the_transaction_alone(self):
		frappe.db.set_default("errcap_keep", "1")
		self.addCleanup(frappe.db.set_default, "errcap_keep", "")

		def poison(**kw):
			frappe.db.set_default("errcap_poison", "1")
			raise RuntimeError("insert failed")

		with patch.object(api_errors, "_fold_row", side_effect=poison), self.assertRaises(RuntimeError):
			self._fold()
		self.assertEqual(frappe.db.get_default("errcap_keep"), "1")
		self.assertFalse(frappe.db.get_default("errcap_poison"))


class TestPushOrdering(PushBase):
	def _entry(self, **patches):
		"""The real */5 entry point with admin configured."""
		with ExitStack() as stack:
			stack.enter_context(patch.object(error_push, "_admin_configured", return_value=True))
			stack.enter_context(patch.object(error_push, "_log_push_failure_throttled"))
			for name, kw in patches.items():
				stack.enter_context(patch.object(error_push, name, **kw))
			error_push.push_error_rollup()

	def _buffered_row(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		frappe.db.commit()

	def _assert_unclaimed(self):
		frappe.db.rollback()  # whatever a failure handler committed is what stays
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 1, "the claim was committed")

	def test_a_failure_in_the_claim_read_does_not_commit_the_claim(self):
		self._buffered_row()
		real = error_push._collect_ui_errors

		def claim_then_fail():
			real()  # the UPDATE has run
			raise RuntimeError("read failed")

		self._entry(_collect_ui_errors={"side_effect": claim_then_fail})
		self._assert_unclaimed()

	def test_a_failure_shaping_the_batch_does_not_commit_the_claim(self):
		self._buffered_row()
		self._entry(_wire={"side_effect": RuntimeError("bad row")})
		self._assert_unclaimed()

	def test_a_failure_writing_state_after_the_claim_does_not_commit_the_claim(self):
		self._buffered_row()
		real = error_push._state_set

		def fail_on_inflight(key, value):
			if key == error_push._INFLIGHT_KEY and value:
				raise RuntimeError("state write failed")
			return real(key, value)

		self._entry(_state_set={"side_effect": fail_on_inflight})
		self._assert_unclaimed()

	def test_the_batch_id_holds_when_the_turn_cursor_was_never_stored(self):
		self._buffered_row()
		error_push._state_set(error_push._TURN_WATERMARK_KEY, "")
		failing = Mock(side_effect=AdminUnreachableError("admin returned a 500 error", status_code=500))
		with patch.object(error_push, "_batch_id", wraps=error_push._batch_id) as batch_id:
			for _ in range(2):
				with self.assertRaises(AdminUnreachableError):
					self._push(failing)
		self.assertEqual(batch_id.call_args_list[0], batch_id.call_args_list[1])
		self.assertTrue(error_push._state_get(error_push._TURN_WATERMARK_KEY))
		self.assertTrue(error_push._state_get(error_push._BATCH_KEY).endswith(":2"))

	def test_the_push_lock_is_per_site(self):
		self.assertIn(frappe.local.site, error_push._push_lock_name())

	def test_the_claim_is_committed_before_the_call_and_the_call_is_short(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		events = []
		real_commit = frappe.db.commit

		def commit(*a, **kw):
			events.append("commit")
			return real_commit(*a, **kw)

		def push(errors, **kw):
			events.append("http")
			self.assertEqual(kw.get("timeout_s"), error_push.PUSH_TIMEOUT_S)
			self.assertEqual(error_push.PUSH_TIMEOUT_S, 30)
			# the names are recorded for a crash recovery
			self.assertTrue(error_push._state_get(error_push._INFLIGHT_KEY))

		with patch.object(frappe.db, "commit", commit):
			self._push(push)
		self.assertLess(events.index("commit"), events.index("http"))
		self.assertFalse(error_push._state_get(error_push._INFLIGHT_KEY))

	def test_a_crash_between_the_claim_and_the_call_is_recovered(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		with self.assertRaises(KeyboardInterrupt):
			self._push(Mock(side_effect=KeyboardInterrupt()))
		self.assertEqual(frappe.db.count(DT, {"pushed": 1}), 1)  # stranded by the "crash"
		self._push(Mock())  # next run gives it back, then sends it
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 0)

	def test_a_failure_after_the_claim_is_not_committed_as_sent(self):
		# F3: the claim is read and shaped BEFORE it is committed; a failure there
		# leaves the rows unclaimed and the call never made.
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		frappe.db.commit()  # the buffer row is committed work; only the claim is at risk
		push = Mock()
		with (
			patch.object(error_push, "_wire", side_effect=RuntimeError("bad row")),
			self.assertRaises(RuntimeError),
		):
			self._push(push)
		frappe.db.rollback()
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 1)
		push.assert_not_called()

	def test_a_failing_collector_claims_nothing(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		with (
			patch.object(api_errors, "collect_failed_turns", side_effect=RuntimeError("scan failed")),
			self.assertRaises(RuntimeError),
		):
			self._push(Mock())
		self.assertEqual(frappe.db.count(DT, {"pushed": 0}), 1)

	def test_the_push_lock_keeps_two_pushes_apart(self):
		with (
			patch.object(error_push, "_admin_configured", return_value=True),
			patch.object(error_push, "_do_push") as do_push,
			redis_lock(error_push._push_lock_name(), timeout_s=30) as held,
		):
			self.assertTrue(held)
			error_push.push_error_rollup()
		do_push.assert_not_called()

	def test_the_kill_switch_stops_the_push(self):
		with (
			patch.object(error_push, "_admin_configured", return_value=True),
			patch.object(error_push, "_do_push") as do_push,
			patch.dict(frappe.conf, {"jarvis_error_capture_off": 1}),
		):
			error_push.push_error_rollup()
		do_push.assert_not_called()

	def test_a_buffer_row_keeps_its_user_even_administrator(self):
		api_errors.record_server_error(
			"tool", error_code="e", error_class="t", message="m", user="Administrator"
		)
		push = Mock()
		self._push(push)
		self.assertEqual(push.call_args[0][0][0]["user_ref"], "Administrator")

	def test_the_batch_never_exceeds_what_admin_accepts(self):
		now = frappe.utils.now_datetime()
		fields = [
			"name",
			"creation",
			"modified",
			"owner",
			"modified_by",
			"docstatus",
			"idx",
			"surface",
			"fingerprint",
			"user",
			"count",
			"first_seen",
			"last_seen",
			"pushed",
		]
		frappe.db.bulk_insert(
			DT,
			fields,
			[
				[f"errcap-b{i}", now, now, USER, USER, 0, 0, "tool", f"fp{i}", USER, 1, now, now, 0]
				for i in range(250)
			],
		)
		old = str(_ago(minutes=30))
		error_push._state_set(error_push._WATERMARK_KEY, old)
		el_fields = [
			"name",
			"creation",
			"modified",
			"owner",
			"modified_by",
			"docstatus",
			"idx",
			"method",
			"error",
		]
		at = _ago(minutes=10)
		self.addCleanup(frappe.db.delete, "Error Log", {"method": ("like", "errcap-cap-%")})
		frappe.db.bulk_insert(
			"Error Log",
			el_fields,
			[
				[f"errcap-el{i}", at, at, USER, USER, 0, 0, f"errcap-cap-{i}", _jarvis_tb(f"fn{i}")]
				for i in range(250)
			],
		)
		frappe.db.commit()
		push = Mock()
		self._push(push)
		batch = push.call_args[0][0]
		self.assertLessEqual(len(batch), error_push.ADMIN_BATCH_LIMIT)
		self.assertEqual(sum(1 for e in batch if e["surface"] == "tool"), error_push.UI_CAP)
		self.assertEqual(sum(1 for e in batch if e["surface"] == "server"), api_errors.MAX_GROUPS)


class TestNoCacheFlush(PushBase):
	"""Pipeline state is written every cycle; frappe.db.set_default would flush the
	whole site cache each time."""

	def setUp(self):
		super().setUp()
		frappe.db.delete("DefaultValue", {"parent": "__default", "defkey": ("like", "jarvis_error_push%")})
		frappe.db.commit()

	def _flushes(self, run):
		with (
			patch.object(frappe, "clear_cache") as full,
			patch.object(frappe.cache_manager, "clear_defaults_cache") as narrow,
		):
			run()
		return full.call_count, narrow

	def _entry(self, push):
		from jarvis import admin_client

		with (
			patch.object(error_push, "_admin_configured", return_value=True),
			patch.object(admin_client, "push_error_rollup", push),
		):
			error_push.push_error_rollup()

	def test_a_cycle_with_something_to_send_flushes_nothing(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		full, narrow = self._flushes(lambda: self._entry(Mock()))
		self.assertEqual(full, 0)
		narrow.assert_called_with("__default")
		self.assertTrue(error_push._state_get(error_push._OK_KEY))

	def test_a_cycle_with_nothing_to_send_flushes_nothing(self):
		full, _ = self._flushes(lambda: self._entry(Mock()))
		self.assertEqual(full, 0)

	def test_a_cycle_with_admin_down_flushes_nothing(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		full, narrow = self._flushes(
			lambda: self._entry(Mock(side_effect=AdminUnreachableError("admin is unreachable")))
		)
		self.assertEqual(full, 0)
		narrow.assert_called_with("__default")
		self.assertEqual(error_push._state_get(error_push._FAILURES_KEY), "1")

	def test_a_value_that_did_not_change_writes_nothing(self):
		error_push._state_set(error_push._CAUSE_KEY, "same")
		full, narrow = self._flushes(lambda: error_push._state_set(error_push._CAUSE_KEY, "same"))
		self.assertEqual(full, 0)
		narrow.assert_not_called()


class TestScanLag(PushBase):
	def test_rows_newer_than_the_lag_are_neither_sent_nor_passed(self):
		before = {
			k: error_push._state_get(k) for k in (error_push._WATERMARK_KEY, error_push._TURN_WATERMARK_KEY)
		}
		self.addCleanup(frappe.db.delete, "Error Log", {"method": "errcap fresh"})
		_error_log("errcap fresh", _jarvis_tb("fresh"))
		_make_turn("errcap-fresh-1")
		push = Mock()
		self._push(push)
		push.assert_not_called()
		self.assertEqual(before, {k: error_push._state_get(k) for k in before})


class TestCursors(PushBase):
	def test_a_cursor_never_moves_back(self):
		key = error_push._WATERMARK_KEY
		error_push._state_set(key, "2026-10-26 10:00:00|zz")
		error_push._advance_watermark(key, None, "2026-10-26 09:00:00|aa")
		self.assertEqual(error_push._state_get(key), "2026-10-26 10:00:00|zz")
		error_push._advance_watermark(key, None, "2026-10-26 10:00:00|zzz")
		self.assertEqual(error_push._state_get(key), "2026-10-26 10:00:00|zzz")

	def test_a_bare_legacy_cursor_means_strictly_after(self):
		at = _ago(days=4)
		self.addCleanup(frappe.db.delete, "Error Log", {"method": "errcap legacy"})
		_error_log("errcap legacy", _jarvis_tb("legacy"), at=at)
		upto = str(frappe.utils.add_to_date(at, minutes=5))
		self.assertEqual(api_errors.collect_error_log(str(at), upto=upto)["rows"], [])
		before = str(frappe.utils.add_to_date(at, seconds=-1))
		self.assertEqual(len(api_errors.collect_error_log(before, upto=upto)["rows"]), 1)


class TestPipelineHealth(PushBase):
	def setUp(self):
		super().setUp()
		for key in ("stalled", "dropped", "scan_error_log", "failed"):
			error_push._state_set(f"jarvis_error_push_log_{key}", "")
		self.addCleanup(frappe.db.delete, "Error Log", {"method": ("like", "jarvis errors: %")})

	def stalled_rows(self):
		return frappe.get_all(
			"Error Log", filters={"method": ("like", "jarvis errors: rollup push stalled%")}, pluck="method"
		)

	def test_a_failure_is_recorded_in_the_defaults(self):
		error_push._note_failure(AdminUnreachableError("down"))
		error_push._note_failure(AdminUnreachableError("down"))
		self.assertEqual(error_push._state_get(error_push._FAILURES_KEY), "2")
		self.assertEqual(error_push._state_get(error_push._CAUSE_KEY), "AdminUnreachableError")
		self.assertEqual(self.stalled_rows(), [])  # not stale yet

	def test_a_stale_pipeline_logs_a_stalled_row_once_per_six_hours(self):
		error_push._state_set(error_push._OK_KEY, str(_ago(hours=3, minutes=5)))
		error_push._note_failure(AdminUnreachableError("down"))
		error_push._note_failure(AdminUnreachableError("down"))
		self.assertEqual(
			self.stalled_rows(), ["jarvis errors: rollup push stalled 3h (AdminUnreachableError)"]
		)

	def test_the_stalled_row_is_never_forwarded(self):
		error_push._state_set(error_push._OK_KEY, str(_ago(hours=2)))
		since = str(_ago(seconds=1))
		error_push._note_failure(AdminUnreachableError("down"))
		frappe.db.commit()
		self.assertEqual(api_errors.collect_error_log(since)["rows"], [])

	def test_the_entry_point_records_a_transient_failure_silently(self):
		with (
			patch.object(error_push, "_admin_configured", return_value=True),
			patch.object(error_push, "_do_push", side_effect=AdminUnreachableError("down")),
			patch.object(error_push, "_log_push_failure_throttled") as logged,
		):
			error_push.push_error_rollup()
		logged.assert_not_called()
		self.assertEqual(error_push._state_get(error_push._FAILURES_KEY), "1")

	def test_a_success_records_when_it_happened(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		self._push(Mock())
		self.assertTrue(error_push._state_get(error_push._OK_KEY))
		self.assertTrue(error_push._state_get(error_push._ATTEMPT_KEY))
		self.assertEqual(error_push._state_get(error_push._FAILURES_KEY), "0")

	def test_a_dropped_batch_says_how_big_and_why(self):
		api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		refusal = AdminContractError("too many", code="InvalidErrorRollup")
		with patch.object(frappe, "logger") as logger:
			self._push(Mock(side_effect=refusal))
		warned = " ".join(str(c) for c in logger.return_value.warning.call_args_list)
		self.assertIn("dropped 1 errors", warned)
		self.assertIn("InvalidErrorRollup", warned)
		(title,) = frappe.get_all(
			"Error Log", filters={"method": "jarvis errors: admin refused rollup batch"}, pluck="error"
		)
		self.assertIn("dropped 1 errors", title)

	def test_a_scan_that_cannot_read_rows_says_so(self):
		since = str(_ago(seconds=1))
		self.addCleanup(frappe.db.delete, "Error Log", {"method": "errcap unreadable"})
		_error_log("errcap unreadable", _jarvis_tb("unreadable"))
		with patch.object(api_errors, "_error_log_row", side_effect=ValueError("bad row")):
			result = api_errors.collect_error_log(since)
		self.assertEqual((result["failed"], result["matched"]), (1, 0))
		self.assertIn("ValueError: bad row", result["first_error"])
		error_push._log_scan_problems(error_log=result)
		self.assertTrue(frappe.db.exists("Error Log", {"method": "jarvis errors: could not read some rows"}))

	def test_scan_counts_are_returned(self):
		since = str(_ago(seconds=1))
		self.addCleanup(frappe.db.delete, "Error Log", {"method": ("like", "errcap count%")})
		_error_log("errcap count 1", _jarvis_tb("count1"))
		_error_log("errcap count 2", 'File "apps/erpnext/x.py", line 1, in f\nKeyError: x')
		result = api_errors.collect_error_log(since)
		self.assertGreaterEqual(result["scanned"], 2)
		self.assertEqual((result["matched"], result["failed"]), (1, 0))

	def test_a_capture_hook_that_fails_leaves_a_warning(self):
		with (
			patch.object(api_errors, "_fold", side_effect=RuntimeError("db gone")),
			patch.object(api_errors, "_capture_warning") as warned,
		):
			api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
		warned.assert_called_once()


class TestKillSwitch(CaptureBase):
	def test_every_lane_stands_down(self):
		with patch.dict(frappe.conf, {"jarvis_error_capture_off": 1}):
			api_errors.record_server_error("tool", error_code="e", error_class="t", message="m", user=USER)
			TestFailedRequests._capture(self, "/api/method/jarvis.chat.api.send_message", 417)
			doc = frappe._dict(error="plain")
			api_errors.tag_error_log_origin(doc)
			self.assertEqual(doc.error, "plain")
			self.assertEqual(api_errors.collect_error_log(None)["rows"], [])
			self.assertEqual(api_errors.collect_failed_turns(None)["rows"], [])
			frappe.set_user(USER)
			answer = api_errors.report_client_errors([{"surface": "spa", "message": "ui boom"}])
			frappe.set_user("Administrator")
			self.assertEqual(answer, {"ok": False, "accepted": 0, "throttled": True})
		self.assertEqual(self.buffered(), [])


# --------------------------------------------------------------------------- #
# Error Log rows: what is forwarded
# --------------------------------------------------------------------------- #
class TestTaggedRows(CaptureBase):
	def _tagged(self, title, message="plain"):
		doc = frappe.log_error(title=title, message=message)
		self.addCleanup(frappe.delete_doc, "Error Log", doc.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		return doc

	def test_only_the_title_and_the_origin_are_forwarded(self):
		since = str(frappe.utils.now_datetime())
		self._tagged("Jarvis: model reply failed", "the customer said: transfer all stock to Acme Traders")
		(row,) = [
			r
			for r in api_errors.collect_error_log(since)["rows"]
			if r["route"] == "Jarvis: model reply failed"
		]
		self.assertEqual(row["message"], "Jarvis: model reply failed")
		self.assertTrue(
			row["stack"].startswith('Logged from:\n  File "apps/jarvis/jarvis/tests/test_error_capture.py"')
		)
		self.assertNotIn("Acme", json.dumps(row))
		self.assertNotIn("transfer", json.dumps(row))

	def test_two_titles_from_one_function_are_two_errors(self):
		since = str(frappe.utils.now_datetime())
		self._tagged("errcap first title")
		self._tagged("errcap second title")
		rows = {
			r["route"]: r
			for r in api_errors.collect_error_log(since)["rows"]
			if r["route"].startswith("errcap")
		}
		self.assertEqual(len(rows), 2)
		self.assertNotEqual(*(r["fingerprint"] for r in rows.values()))

	def test_digits_in_a_title_do_not_split_it(self):
		since = str(frappe.utils.now_datetime())
		self._tagged("errcap retry 3 of 5")
		self._tagged("errcap retry 4 of 5")
		(row,) = [
			r for r in api_errors.collect_error_log(since)["rows"] if r["route"].startswith("errcap retry")
		]
		self.assertEqual(row["count"], 2)

	def test_a_message_that_only_says_logged_from_is_not_a_tag(self):
		since = str(frappe.utils.now_datetime())
		self.addCleanup(frappe.db.delete, "Error Log", {"method": "jarvis.errcap.forged"})
		_error_log("jarvis.errcap.forged", 'secret body\n\nLogged from:\n  File "x", line 1, in f')
		(row,) = [
			r for r in api_errors.collect_error_log(since)["rows"] if r["route"] == "jarvis.errcap.forged"
		]
		self.assertNotEqual(row["stack"], "")

	def test_a_traceback_with_a_tag_leads_with_the_tag(self):
		since = str(frappe.utils.now_datetime())
		doc = f'{_jarvis_tb("boom")}\n\nLogged from:\n  File "apps/jarvis/jarvis/chat/pump.py", line 4, in make_mux\n'
		self.addCleanup(frappe.db.delete, "Error Log", {"method": "errcap tb"})
		_error_log("errcap tb", doc)
		(row,) = [r for r in api_errors.collect_error_log(since)["rows"] if r["route"] == "errcap tb"]
		self.assertTrue(row["stack"].startswith("Logged from:"))

	def test_a_huge_body_is_scrubbed_only_in_part(self):
		self.assertLessEqual(
			len(api_errors._scrub_error_text("word " * 100_000)), api_errors._SCRUB_MAX_CHARS
		)


class TestErrorLogScanEdges(CaptureBase):
	# Rows are placed three days back, in a window nothing else writes to.
	def setUp(self):
		super().setUp()
		self.base = _ago(days=3)
		self.since = str(frappe.utils.add_to_date(self.base, minutes=-1))
		self.upto = str(frappe.utils.add_to_date(self.base, minutes=5))

	def at(self, seconds):
		return frappe.utils.add_to_date(self.base, seconds=seconds)

	def test_two_distinct_jarvis_rows_are_read_one_group_per_cycle(self):
		self.addCleanup(frappe.db.delete, "Error Log", {"method": ("like", "errcap grp%")})
		_error_log("errcap grp 1", _jarvis_tb("one"), at=self.at(1))
		_error_log("errcap grp 2", _jarvis_tb("two"), at=self.at(2))
		first = api_errors.collect_error_log(self.since, max_groups=1, upto=self.upto)
		self.assertEqual([r["route"] for r in first["rows"]], ["errcap grp 1"])
		second = api_errors.collect_error_log(first["watermark"], max_groups=1, upto=self.upto)
		self.assertEqual([r["route"] for r in second["rows"]], ["errcap grp 2"])

	def test_the_cursor_passes_a_row_that_is_not_jarvis(self):
		self.addCleanup(frappe.db.delete, "Error Log", {"method": "errcap erpnext"})
		name = _error_log(
			"errcap erpnext", 'File "apps/erpnext/erpnext/x.py", line 1, in f\nKeyError: x', at=self.at(1)
		)
		result = api_errors.collect_error_log(self.since, upto=self.upto)
		self.assertEqual(result["rows"], [])
		self.assertEqual(result["watermark"], f"{self.at(1)}|{name}")

	def test_the_reporters_neighbours_stay_local(self):
		for rel in ("admin_client.py", "chat/usage_push.py"):
			tb = f'File "apps/jarvis/jarvis/{rel}", line 1, in f\nValueError: x'
			self.assertFalse(api_errors.is_jarvis_error("admin_client: network error", tb), rel)


# --------------------------------------------------------------------------- #
# call_tool attribution, tool envelopes, failed-request edges
# --------------------------------------------------------------------------- #
class TestAttributionAndEnvelopes(CaptureBase):
	DISABLED = "errcap-disabled@example.com"

	def setUp(self):
		super().setUp()
		frappe.local.response = frappe._dict()
		if not frappe.db.exists("User", self.DISABLED):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": self.DISABLED,
					"first_name": "errcap off",
					"send_welcome_email": 0,
					"enabled": 0,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		self.key = f"errcap-session-{frappe.generate_hash(length=8)}"
		frappe.get_doc(
			{"doctype": "Jarvis Chat Session", "session_key": self.key, "user": self.DISABLED}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(frappe.db.delete, "Jarvis Chat Session", {"session_key": self.key})

	def test_a_rejection_after_the_session_resolves_is_the_chat_users(self):
		result = TestCallToolFailures._plugin_call(self, validate_plugin_request={"return_value": self.key})
		self.assertEqual(result["error"]["code"], "PermissionError")
		(row,) = self.buffered()
		self.assertEqual(
			(row.surface, row.error_code, row.user, row.route),
			("call_tool", "403", self.DISABLED, "get_list"),
		)

	def test_a_connector_that_failed_inside_its_envelope_is_recorded(self):
		from jarvis import api

		inner = {"ok": False, "error": {"code": "transport_error", "message": "connector timed out"}}
		frappe.set_user(USER)
		with patch.object(api, "dispatch", return_value=inner):
			envelope = api._dispatch_and_wrap("call_connector", {}, False)
		frappe.set_user("Administrator")
		self.assertTrue(envelope["ok"])
		(row,) = self.buffered()
		self.assertEqual(
			(row.surface, row.route, row.error_code), ("tool", "call_connector", "transport_error")
		)
		self.assertTrue(frappe.local.jarvis_tool_failure_recorded)

	def test_a_connector_that_worked_records_nothing(self):
		from jarvis import api

		with patch.object(api, "dispatch", return_value={"ok": True, "data": []}):
			api._dispatch_and_wrap("call_connector", {}, False)
		self.assertEqual(self.buffered(), [])


class TestFailedRequestEdges(CaptureBase):
	def test_a_499_is_buffered(self):
		TestFailedRequests._capture(self, "/api/method/jarvis.chat.api.send_message", 499)
		(row,) = self.buffered()
		self.assertEqual(row.error_code, "499")

	def test_a_rate_limit_answering_as_designed_is_not_an_error(self):
		TestFailedRequests._capture(self, "/api/method/jarvis.chat.api.send_message", 429)
		self.assertEqual(self.buffered(), [])

	def test_what_the_failed_request_left_behind_is_discarded(self):
		frappe.db.set_default("errcap_pending", "1")
		self.addCleanup(frappe.db.set_default, "errcap_pending", "")
		TestFailedRequests._capture(self, "/api/method/jarvis.chat.api.send_message", 417)
		self.assertFalse(frappe.db.get_default("errcap_pending"))
		self.assertEqual(len(self.buffered()), 1)


class TestBrowserRunError(CaptureBase):
	def _report(self, **extra):
		frappe.set_user(USER)
		api_errors.report_client_errors(
			[{"surface": "spa_chat", "error_class": "RunError", "message": "The run failed.", **extra}]
		)
		frappe.set_user("Administrator")

	def test_a_run_error_for_an_errored_turn_is_left_to_the_turn_lane(self):
		_make_turn("errcap-run-1")
		self._report(run_id="errcap-run-1")
		self.assertEqual(self.buffered(), [])

	def test_a_run_error_with_nothing_behind_it_is_kept(self):
		self._report()
		self.assertEqual(len(self.buffered()), 1)

	def test_a_recovered_run_error_is_kept(self):
		self._report(run_id="recovered")
		self.assertEqual(len(self.buffered()), 1)

	def test_a_run_error_for_a_cancelled_turn_is_kept(self):
		_make_turn("errcap-run-2", state="cancelled", error="")
		self._report(run_id="errcap-run-2")
		self.assertEqual(len(self.buffered()), 1)
