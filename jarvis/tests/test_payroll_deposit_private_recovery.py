"""Runtime regressions for the neutral Deposit private-source recovery callback."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis._password_utils import set_settings_password
from jarvis._plugin_auth import _RECOVERY_RATE_PER_MINUTE
from jarvis._session import impersonate
from jarvis.chat import payroll_deposit_recovery
from jarvis.tests._settings_snapshot import JarvisSettingsSnapshot

AGENT = "statutory-payroll-deposit-status"
TOKEN = "deposit-private-recovery-test-token"
SENTINEL = "SECRET-PRIVATE-ROW"


class _FakeRequest:
	def __init__(
		self,
		headers: dict[str, str],
		body: bytes,
		*,
		content_type: str = "application/json",
		args: dict | None = None,
		form: dict | None = None,
	):
		self.headers = headers
		self._body = body
		self.content_type = content_type
		self.mimetype = content_type.split(";", 1)[0].strip().lower()
		self.args = args or {}
		self.form = form or {}

	def get_data(self, cache: bool = True) -> bytes:
		return self._body


def _signed_headers(token: str, session_key: str, body: bytes) -> dict[str, str]:
	nonce = secrets.token_hex(16)
	timestamp = str(int(time.time()))
	canonical = "|".join([session_key, hashlib.sha256(body).hexdigest(), nonce, timestamp]).encode()
	return {
		"X-Jarvis-Token": token,
		"X-Jarvis-Session": session_key,
		"X-Jarvis-Signature": hmac.new(token.encode(), canonical, hashlib.sha256).hexdigest(),
		"X-Jarvis-Nonce": nonce,
		"X-Jarvis-Timestamp": timestamp,
	}


def _ensure_user(email: str) -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Deposit Recovery",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
	return email


class TestPayrollDepositPrivateRecovery(JarvisSettingsSnapshot, FrappeTestCase):
	"""Use real signed requests and database locks with isolated fake rows."""

	def setUp(self):
		super().setUp()
		self._run_names: list[str] = []
		self._installation_names: list[str] = []
		self._installations_by_user: dict[str, str] = {}
		self._session_keys: list[str] = []
		settings = frappe.get_single("Jarvis Settings")
		# Plugin authentication reads Password values straight from __Auth.  Keep
		# the token there, and let JarvisSettingsSnapshot restore both that row and
		# the Settings singleton after a test that commits its callback outcome.
		set_settings_password(settings, "agent_token", TOKEN)
		self.user = _ensure_user("payroll-deposit-recovery@example.test")
		self.record = self._new_record()

	def tearDown(self):
		# These rows are fake test data. Direct deletes are the trusted retention
		# path and avoid using the immutable dashboard lifecycle as a test bypass.
		for session_key in self._session_keys:
			frappe.db.delete("Jarvis Chat Session", {"session_key": session_key})
		for run_name in self._run_names:
			frappe.db.delete("Jarvis Agent Run Step", {"run": run_name})
			frappe.db.delete("Jarvis Agent Activity", {"run": run_name})
			frappe.db.delete("Jarvis Agent Run", {"name": run_name})
		for installation_name in self._installation_names:
			frappe.db.delete("Jarvis Agent Installation", {"name": installation_name})
		for session_key in self._session_keys:
			for prefix in ("jarvis:plugin_rl:", "jarvis:plugin_recovery_rl:"):
				try:
					frappe.cache().delete(prefix + session_key)
				except Exception:
					pass
		frappe.db.commit()
		super().tearDown()

	def _new_record(
		self,
		*,
		user: str | None = None,
		tools: list[str] | None = None,
		contract: str = "snapshot",
		session_key: str | None = None,
	) -> frappe._dict:
		user = user or self.user
		installation_name = self._installations_by_user.get(user)
		if not installation_name:
			installation = frappe.get_doc(
				{
					"doctype": "Jarvis Agent Installation",
					"agent": AGENT,
					"run_as_user": user,
					"reviewer": user,
					"activation_state": "live",
					"enabled": 1,
				}
			)
			installation.flags.ignore_links = True
			with impersonate(user):
				installation.db_insert()
			installation_name = installation.name
			self._installations_by_user[user] = installation_name
			self._installation_names.append(installation_name)
		session_key = session_key or f"agent:agent-payroll-deposit:{frappe.generate_hash(length=12)}"
		run = frappe.get_doc(
			{
				"doctype": "Jarvis Agent Run",
				"agent": AGENT,
				"installation": installation_name,
				"trigger": "manual",
				"status": "running",
				"started_at": frappe.utils.now(),
				"session_key": session_key,
				"preparation_mode": "shadow",
				"capability_contract": contract,
				"tools_allow_json": json.dumps(
					tools
					if tools is not None
					else ["jarvis__get_payroll_deposit_inputs", "jarvis__record_agent_run"]
				),
				"capability_nature": "auditor",
				"capability_writes_json": "[]",
			}
		)
		run.owner = user
		run.insert(ignore_permissions=True, ignore_links=True)
		self._run_names.append(run.name)
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Session",
				"session_key": session_key,
				"user": user,
				"chat_device_id": "",
			}
		).insert(ignore_permissions=True)
		self._session_keys.append(session_key)
		frappe.db.commit()
		return frappe._dict(run=run.name, session_key=session_key, installation=installation_name, user=user)

	@contextmanager
	def _request(
		self,
		record,
		*,
		body: bytes = b"{}",
		headers: dict[str, str] | None = None,
		content_type: str = "application/json",
	):
		request_headers = _signed_headers(TOKEN, record.session_key, body)
		request_headers.update(headers or {})
		with patch.object(
			frappe,
			"request",
			_FakeRequest(request_headers, body, content_type=content_type),
			create=True,
		):
			yield

	def _callback(
		self,
		record,
		*,
		body: bytes = b"{}",
		headers: dict[str, str] | None = None,
		content_type: str = "application/json",
	) -> dict:
		with self._request(record, body=body, headers=headers, content_type=content_type):
			return api.terminalize_unusable_payroll_deposit_private_source()

	def _handler_callback(
		self, *, body: bytes, content_type: str, query_string=None, headers: dict[str, str] | None = None
	) -> tuple[int, dict]:
		"""Drive the recovery method through Frappe's RPC handler and serializer."""
		from frappe.app import make_form_dict
		from frappe.handler import execute_cmd
		from frappe.utils.response import build_response
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		method = "jarvis.api.terminalize_unusable_payroll_deposit_private_source"
		saved = (
			getattr(frappe.local, "response", None),
			getattr(frappe.local, "form_dict", None),
			getattr(frappe.local, "request", None),
			getattr(frappe.local, "message_log", None),
		)
		try:
			request = Request(
				EnvironBuilder(
					path=f"/api/method/{method}",
					method="POST",
					data=body,
					content_type=content_type,
					query_string=query_string,
					headers=headers,
				).get_environ()
			)
			frappe.local.request = request
			frappe.local.response = frappe._dict({"type": "json"})
			frappe.local.message_log = []
			make_form_dict(request)
			frappe.local.form_dict["cmd"] = method
			data = execute_cmd(method)
			frappe.local.response["message"] = data
			response = build_response("json")
			return response.status_code, json.loads(response.get_data(as_text=True))
		finally:
			frappe.local.response, frappe.local.form_dict, frappe.local.request, frappe.local.message_log = (
				saved
			)

	def _status(self, record) -> frappe._dict:
		frappe.db.commit()
		return frappe.db.get_value(
			"Jarvis Agent Run", record.run, ["status", "error", "dashboard"], as_dict=True
		)

	def test_signed_empty_callback_fails_only_its_active_run_with_a_fixed_ack(self):
		result = self._callback(self.record)

		self.assertEqual(result, {"ok": True})
		final = self._status(self.record)
		self.assertEqual(final.status, "failed")
		self.assertEqual(final.error, payroll_deposit_recovery.PRIVATE_SOURCE_UNAVAILABLE)
		self.assertIsNone(final.dashboard)
		self.assertFalse(frappe.db.exists("Jarvis Chat Session", {"session_key": self.record.session_key}))
		activity = frappe.db.get_value(
			"Jarvis Agent Activity", {"run": self.record.run}, ["detail", "agent", "run"], as_dict=True
		)
		self.assertEqual(activity.detail, payroll_deposit_recovery.PRIVATE_SOURCE_UNAVAILABLE)
		for surface in (result, final, activity):
			self.assertNotIn(SENTINEL, json.dumps(surface, default=str))

	def test_callback_has_no_tool_receipt_audit_realtime_or_error_egress(self):
		with (
			patch.object(api, "_persist_and_publish_tool_call") as receipt,
			patch.object(api.audit, "record") as audit_record,
			patch.object(api, "publish_realtime_tool_result") as realtime,
			patch.object(frappe, "log_error") as error_log,
		):
			result = self._callback(self.record)
		self.assertEqual(result, {"ok": True})
		receipt.assert_not_called()
		audit_record.assert_not_called()
		realtime.assert_not_called()
		error_log.assert_not_called()

	def test_callback_cannot_affect_a_different_run_or_session(self):
		other = self._new_record()
		result = self._callback(self.record)
		self.assertEqual(result, {"ok": True})
		self.assertEqual(self._status(self.record).status, "failed")
		self.assertEqual(self._status(other).status, "running")

		foreign_key = f"agent:agent-payroll-deposit:foreign-{frappe.generate_hash(length=12)}"
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Session",
				"session_key": foreign_key,
				"user": self.user,
				"chat_device_id": "",
			}
		).insert(ignore_permissions=True)
		self._session_keys.append(foreign_key)
		foreign = frappe._dict(session_key=foreign_key)
		self.assertEqual(self._callback(foreign), {"ok": True})
		self.assertEqual(self._status(other).status, "running")

	def test_mismatched_capability_run_cannot_terminalize_any_run(self):
		from jarvis.tools import _delegate_capability

		other = self._new_record()
		forged_cap = {
			"run": other.run,
			"status": "running",
			"agent": AGENT,
			"run_as": self.user,
			"legacy": False,
			"tools_allow": ["jarvis__get_payroll_deposit_inputs"],
		}
		with patch.object(_delegate_capability, "resolve", return_value=forged_cap):
			self.assertFalse(
				payroll_deposit_recovery.recover_private_input(self.record.session_key, self.user)
			)
		self.assertEqual(self._status(self.record).status, "running")
		self.assertEqual(self._status(other).status, "running")

	def test_capability_and_identity_matrix_refuses_without_disclosing_state(self):
		no_source = self._new_record(tools=["jarvis__record_agent_run"])
		legacy = self._new_record(contract="legacy")
		disabled_user = _ensure_user("payroll-deposit-recovery-disabled@example.test")
		disabled = self._new_record(user=disabled_user)
		frappe.db.set_value("User", disabled_user, "enabled", 0)
		frappe.db.commit()
		try:
			for record in (no_source, legacy, disabled):
				with self.subTest(record=record.run):
					result = self._callback(record)
					if record is disabled:
						# The callback cannot use a disabled identity to terminalize an
						# otherwise eligible running source Run. It must not acknowledge
						# success and let the plugin discard its private input.
						self.assertFalse(result["ok"])
						self.assertEqual(result["error"]["code"], "ServiceUnavailable")
						self.assertNotIn(record.run, json.dumps(result))
					else:
						self.assertEqual(result, {"ok": True})
					self.assertEqual(self._status(record).status, "running")
		finally:
			frappe.db.set_value("User", disabled_user, "enabled", 1)
			frappe.db.commit()

	def test_stale_device_callback_does_not_acknowledge_an_unresolved_run(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("chat_device_id", "recovery-current-device")
		frappe.db.set_value(
			"Jarvis Chat Session",
			{"session_key": self.record.session_key},
			"chat_device_id",
			"recovery-old-device",
		)
		frappe.db.commit()

		result = self._callback(self.record)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "ServiceUnavailable")
		self.assertNotIn(self.record.run, json.dumps(result))
		self.assertEqual(self._status(self.record).status, "running")

	def test_raw_body_is_exact_and_never_echoes_caller_values(self):
		body = json.dumps(
			{
				"reason": SENTINEL,
				"run": self.record.run,
				"tool": SENTINEL,
				"tool_call_id": SENTINEL,
			}
		).encode()
		with patch.object(api, "validate_private_input_recovery_request") as authenticate:
			result = self._callback(self.record, body=body)

		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		authenticate.assert_not_called()
		self.assertNotIn(SENTINEL, json.dumps(result))
		self.assertNotIn(self.record.run, json.dumps(result))
		self.assertEqual(self._status(self.record).status, "running")

	def test_tool_dispatch_headers_are_rejected_before_auth_or_terminalization(self):
		headers = _signed_headers(TOKEN, self.record.session_key, b"{}")
		headers.update({"X-Jarvis-Tool": SENTINEL, "X-Jarvis-Tool-Call-Id": SENTINEL})
		with (
			patch.object(api, "validate_private_input_recovery_request") as authenticate,
			patch("jarvis.chat.payroll_deposit_recovery.recover_private_input") as terminalize,
			patch.object(frappe, "log_error") as error_log,
		):
			status, payload = self._handler_callback(
				body=b"{}", content_type="application/json", headers=headers
			)

		result = payload["message"]
		self.assertEqual(status, 400)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		self.assertNotIn(SENTINEL, json.dumps(payload))
		authenticate.assert_not_called()
		terminalize.assert_not_called()
		error_log.assert_not_called()
		self.assertEqual(self._status(self.record).status, "running")

	def test_json_content_type_with_charset_is_accepted(self):
		result = self._callback(self.record, content_type="application/json; charset=utf-8")

		self.assertEqual(result, {"ok": True})
		self.assertEqual(self._status(self.record).status, "failed")

	def test_non_neutral_request_shapes_are_refused_by_the_frappe_handler_before_auth(self):
		for label, body, content_type, query_string in (
			("text", b"{}", "text/plain", None),
			("form", f"reason={SENTINEL}".encode(), "application/x-www-form-urlencoded", None),
			("query", b"{}", "application/json", {"reason": SENTINEL}),
			(
				"duplicate_cmd",
				b"{}",
				"application/json",
				[
					("cmd", "jarvis.api.terminalize_unusable_payroll_deposit_private_source"),
					("cmd", SENTINEL),
				],
			),
		):
			with (
				self.subTest(label=label),
				patch.object(api, "validate_private_input_recovery_request") as authenticate,
				patch("jarvis.chat.payroll_deposit_recovery.recover_private_input") as terminalize,
				patch.object(frappe, "log_error") as error_log,
			):
				status, payload = self._handler_callback(
					body=body, content_type=content_type, query_string=query_string
				)
				message = payload["message"]
				self.assertEqual(status, 400)
				self.assertFalse(message["ok"])
				self.assertEqual(message["error"]["code"], "InvalidArgumentError")
				self.assertNotIn(SENTINEL, json.dumps(payload))
				authenticate.assert_not_called()
				terminalize.assert_not_called()
				error_log.assert_not_called()
				self.assertEqual(self._status(self.record).status, "running")

	def test_three_signed_malformed_callbacks_do_not_spend_the_recovery_budget(self):
		body = b"{}"
		with patch.object(frappe, "log_error") as error_log:
			results = [
				self._callback(self.record, content_type="text/plain"),
				self._callback(self.record, body=f'{{"reason":"{SENTINEL}"}}'.encode()),
			]
			headers = _signed_headers(TOKEN, self.record.session_key, body)
			with patch.object(
				frappe,
				"request",
				_FakeRequest(headers, body, args={"reason": SENTINEL}),
				create=True,
			):
				results.append(api.terminalize_unusable_payroll_deposit_private_source())

		for result in results:
			self.assertFalse(result["ok"])
			self.assertEqual(result["error"]["code"], "InvalidArgumentError")
			self.assertNotIn(SENTINEL, json.dumps(result))
		self.assertEqual(self._status(self.record).status, "running")
		error_log.assert_not_called()

		self.assertEqual(self._callback(self.record), {"ok": True})
		self.assertEqual(self._status(self.record).status, "failed")

	def test_unexpected_recovery_failure_returns_a_fixed_non_success(self):
		with (
			patch(
				"jarvis.chat.payroll_deposit_recovery.recover_private_input",
				side_effect=RuntimeError(SENTINEL),
			),
			patch.object(frappe.db, "rollback", wraps=frappe.db.rollback) as rollback,
			patch.object(frappe, "logger") as logger,
		):
			status, payload = self._handler_callback(
				body=b"{}",
				content_type="application/json",
				headers=_signed_headers(TOKEN, self.record.session_key, b"{}"),
			)
			result = payload["message"]

		self.assertEqual(status, 503)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "ServiceUnavailable")
		self.assertEqual(result["error"]["message"], "Private payroll input recovery is unavailable.")
		rollback.assert_called_once()
		self.assertNotIn(SENTINEL, json.dumps(result))
		logger.return_value.error.assert_called_once_with("private payroll input recovery failed")
		self.assertNotIn(SENTINEL, json.dumps(logger.return_value.error.call_args, default=str))
		self.assertEqual(self._status(self.record).status, "running")

	def test_unresolved_active_run_never_gets_a_success_acknowledgement(self):
		"""A false recovery outcome must not let the plugin discard source data."""
		with patch.object(payroll_deposit_recovery, "_locked_run_matches", return_value=False):
			status, payload = self._handler_callback(
				body=b"{}",
				content_type="application/json",
				headers=_signed_headers(TOKEN, self.record.session_key, b"{}"),
			)

		result = payload["message"]
		self.assertEqual(status, 503)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "ServiceUnavailable")
		self.assertEqual(result["error"]["message"], "Private payroll input recovery is unavailable.")
		self.assertNotIn(self.record.run, json.dumps(result))
		self.assertNotIn(SENTINEL, json.dumps(result))
		self.assertEqual(self._status(self.record).status, "running")

	def test_normal_rate_limit_does_not_spend_or_block_recovery_budget(self):
		cache = frappe.cache()
		cache.set(f"jarvis:plugin_rl:{self.record.session_key}", 60, ex=60)
		result = self._callback(self.record)

		self.assertEqual(result, {"ok": True})
		self.assertEqual(self._status(self.record).status, "failed")

	def test_recovery_budget_rejects_without_terminalizing(self):
		frappe.cache().set(
			f"jarvis:plugin_recovery_rl:{self.record.session_key}", _RECOVERY_RATE_PER_MINUTE, ex=60
		)
		result = self._callback(self.record)

		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "RateLimitExceededError")
		self.assertEqual(self._status(self.record).status, "running")

	def test_terminal_runs_are_idempotent_no_ops(self):
		for status in ("completed", "partial", "stopped"):
			record = self.record if status == "completed" else self._new_record()
			frappe.db.set_value("Jarvis Agent Run", record.run, "status", status)
			frappe.db.commit()
			with self.subTest(status=status):
				self.assertEqual(self._callback(record), {"ok": True})
				self.assertEqual(self._status(record).status, status)

	def test_signed_nonce_replay_is_rejected_before_another_terminal_action(self):
		body = b"{}"
		headers = _signed_headers(TOKEN, self.record.session_key, body)
		with patch.object(frappe, "request", _FakeRequest(headers, body), create=True):
			self.assertEqual(api.terminalize_unusable_payroll_deposit_private_source(), {"ok": True})
		with patch.object(frappe, "request", _FakeRequest(headers, body), create=True):
			replay = api.terminalize_unusable_payroll_deposit_private_source()
		self.assertFalse(replay["ok"])
		self.assertEqual(replay["error"]["code"], "AuthenticationError")
		self.assertEqual(self._status(self.record).status, "failed")

	def test_callback_and_callback_race_change_one_run_once(self):
		from jarvis.chat import agent_scheduler

		self._run_race(
			first=lambda: payroll_deposit_recovery.recover_private_input(self.record.session_key, self.user),
			second=lambda: payroll_deposit_recovery.recover_private_input(self.record.session_key, self.user),
			patch_target=agent_scheduler,
		)
		final = self._status(self.record)
		self.assertEqual(final.status, "failed")
		self.assertEqual(final.error, payroll_deposit_recovery.PRIVATE_SOURCE_UNAVAILABLE)
		self.assertEqual(frappe.db.count("Jarvis Agent Activity", {"run": self.record.run}), 1)
		self.assertEqual(frappe.db.count("Jarvis Dashboard", {"agent_run": self.record.run}), 0)

	def test_callback_waits_for_source_capture_then_terminalizes(self):
		from jarvis.chat import payroll_deposit_snapshot
		from jarvis.tools import _agent_run_ctx, _private_output
		from jarvis.tools import get_payroll_deposit_inputs as source_tool

		capturing, release = threading.Event(), threading.Event()
		results, errors = [], []
		site, sites_path = frappe.local.site, frappe.local.sites_path

		def held_capture(_run):
			capturing.set()
			if not release.wait(10):
				raise TimeoutError("source capture was not released")
			return {"private": SENTINEL}

		def capture():
			try:
				frappe.init(site, sites_path=sites_path)
				frappe.connect()
				frappe.set_user(self.user)
				_agent_run_ctx.set_session_key(self.record.session_key)
				with _private_output.request(True):
					results.append(source_tool.get_payroll_deposit_inputs())
				frappe.db.commit()
			except Exception as exc:
				errors.append(repr(exc))
			finally:
				_agent_run_ctx.clear_session_key()
				try:
					frappe.destroy()
				except Exception:
					pass

		with patch.object(payroll_deposit_snapshot, "capture", side_effect=held_capture):
			capture_thread = threading.Thread(target=capture)
			callback_thread = self._thread(
				lambda: payroll_deposit_recovery.recover_private_input(self.record.session_key, self.user),
				errors,
			)
			capture_thread.start()
			self.assertTrue(capturing.wait(10), errors)
			callback_thread.start()
			time.sleep(0.1)
			release.set()
			capture_thread.join(15)
			callback_thread.join(15)

		self.assertFalse(capture_thread.is_alive())
		self.assertFalse(callback_thread.is_alive())
		self.assertEqual(errors, [])
		self.assertEqual(results, [{"private": SENTINEL}])
		self.assertEqual(self._status(self.record).status, "failed")
		self.assertNotIn(SENTINEL, json.dumps(self._status(self.record), default=str))

	def test_callback_wins_over_late_writeback_under_the_same_run_lock(self):
		from jarvis.chat import agent_scheduler
		from jarvis.tools import _agent_run_ctx
		from jarvis.tools import record_agent_run as writeback_tool

		locked, release = threading.Event(), threading.Event()
		thread_state = threading.local()
		results, errors = [], []
		real_fail = agent_scheduler.fail_locked_run

		def held_fail(*args, **kwargs):
			if getattr(thread_state, "first", False):
				locked.set()
				if not release.wait(10):
					raise TimeoutError("callback lock was not released")
			return real_fail(*args, **kwargs)

		def callback():
			thread_state.first = True
			return payroll_deposit_recovery.recover_private_input(self.record.session_key, self.user)

		def writeback():
			_agent_run_ctx.set_session_key(self.record.session_key)
			try:
				return writeback_tool.record_agent_run()
			finally:
				_agent_run_ctx.clear_session_key()

		with patch.object(agent_scheduler, "fail_locked_run", side_effect=held_fail):
			callback_thread = self._thread(callback, errors, results)
			writeback_thread = self._thread(writeback, errors, results)
			callback_thread.start()
			self.assertTrue(locked.wait(10), errors)
			writeback_thread.start()
			time.sleep(0.1)
			release.set()
			callback_thread.join(15)
			writeback_thread.join(15)

		self.assertFalse(callback_thread.is_alive())
		self.assertFalse(writeback_thread.is_alive())
		self.assertEqual(errors, [])
		self.assertIn(True, results)
		self.assertIn(
			{
				"run": self.record.run,
				"status": "failed",
				"idempotent": True,
				"note": "run already finalized; writeback ignored",
			},
			results,
		)
		final = self._status(self.record)
		self.assertEqual(final.status, "failed")
		self.assertIsNone(final.dashboard)

	def test_terminal_commit_gap_rejects_source_and_writeback_before_any_egress(self):
		"""A bearer that survives status commit cannot dispatch one last tool."""
		from jarvis.chat import agent_runs
		from jarvis.tools import _private_output

		status_committed, release = threading.Event(), threading.Event()
		results, errors = [], []
		call_id = "PRIVATE-TERMINAL-GAP-CALL-ID"
		sentinel = "PRIVATE-TERMINAL-GAP-SOURCE"
		real_teardown = agent_runs.teardown_run_session

		def held_teardown(session_key):
			status_committed.set()
			if not release.wait(10):
				raise TimeoutError("terminal session teardown was not released")
			return real_teardown(session_key)

		with patch.object(agent_runs, "teardown_run_session", side_effect=held_teardown):
			terminal_thread = self._thread(
				lambda: payroll_deposit_recovery.recover_private_input(self.record.session_key, self.user),
				errors,
				results,
			)
			terminal_thread.start()
			try:
				self.assertTrue(status_committed.wait(10), errors)
				# Release this connection's old read view before it observes the
				# committed terminal state from the other real DB connection.
				frappe.db.commit()
				self.assertEqual(frappe.db.get_value("Jarvis Agent Run", self.record.run, "status"), "failed")

				with (
					patch("jarvis.chat.site_pin.pin_if_unset"),
					patch.object(api, "_persist_and_publish_tool_call") as receipt,
					patch.object(api.audit, "record") as audit_record,
					patch.object(api, "_record_tool_step") as step,
					patch.object(api, "publish_realtime_tool_result") as realtime,
					patch.object(
						api,
						"_run_tool",
						side_effect=AssertionError("terminal Deposit tool reached execution"),
					),
					patch("jarvis.api_errors.record_server_error") as error_capture,
					patch.object(frappe, "log_error") as error_log,
				):
					with self._request(self.record, headers={"X-Jarvis-Tool-Call-Id": call_id}):
						source_result = api.call_tool(
							"get_payroll_deposit_inputs",
							{"source": sentinel},
							result_mode=_private_output.MODE,
						)
					with self._request(self.record, headers={"X-Jarvis-Tool-Call-Id": call_id}):
						writeback_result = api.call_tool(
							"record_agent_run", {"findings": [{"token": sentinel}]}
						)

				for result in (source_result, writeback_result):
					self.assertFalse(result["ok"])
					self.assertEqual(result["error"]["code"], api.RunHaltedError.__name__)
				receipt.assert_not_called()
				audit_record.assert_not_called()
				step.assert_not_called()
				realtime.assert_not_called()
				error_log.assert_not_called()
				for surface in (
					source_result,
					writeback_result,
					error_capture.call_args_list,
				):
					serialized = json.dumps(surface, default=str)
					for hidden in (
						sentinel,
						call_id,
						"get_payroll_deposit_inputs",
						"record_agent_run",
					):
						self.assertNotIn(hidden, serialized)
				self.assertEqual(
					error_capture.call_args.kwargs["route"],
					_private_output.PRIVATE_DELEGATE_TOOL_LABEL,
				)
			finally:
				release.set()
				terminal_thread.join(15)

		self.assertFalse(terminal_thread.is_alive())
		self.assertEqual(errors, [])
		self.assertEqual(results, [True])
		final = self._status(self.record)
		self.assertEqual(final.status, "failed")
		self.assertEqual(final.error, payroll_deposit_recovery.PRIVATE_SOURCE_UNAVAILABLE)
		self.assertIsNone(final.dashboard)
		self.assertFalse(frappe.db.exists("Jarvis Chat Session", {"session_key": self.record.session_key}))
		self.assertEqual(frappe.db.count("Jarvis Dashboard", {"agent_run": self.record.run}), 0)
		self.assertEqual(frappe.db.count("Jarvis Agent Finding", {"run": self.record.run}), 0)
		self.assertEqual(frappe.db.count("Jarvis Agent Finding", {"last_seen_run": self.record.run}), 0)
		self.assertEqual(frappe.db.count("Jarvis Agent Run Step", {"run": self.record.run}), 0)
		self.assertNotIn(sentinel, json.dumps(final, default=str))

	def test_active_deposit_v3_extra_keys_create_no_durable_output(self):
		from jarvis.tools import _private_output

		sentinel = "PRIVATE-V3-EXTRA-KEY"
		call_id = "PRIVATE-V3-EXTRA-CALL-ID"
		args = {
			"findings": [{"token": sentinel}],
			"coverage": {},
			"scope": {},
			"truncated": False,
			"rows_consumed": 0,
			"input_snapshot_id": "snapshot",
			"input_snapshot_digest": "digest",
			"dashboard": None,
			"_session": sentinel,
		}
		with (
			patch("jarvis.chat.site_pin.pin_if_unset"),
			patch.object(api, "_persist_and_publish_tool_call") as receipt,
			patch.object(api.audit, "record") as audit_record,
			patch.object(api, "_record_tool_step") as step,
			patch.object(api, "publish_realtime_tool_result") as realtime,
			patch.object(api, "_run_tool") as run_tool,
			patch("jarvis.api_errors.record_server_error") as error_capture,
		):
			with self._request(self.record, headers={"X-Jarvis-Tool-Call-Id": call_id}):
				result = api.call_tool("record_agent_run", json.dumps(args))

		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		receipt.assert_not_called()
		audit_record.assert_not_called()
		step.assert_not_called()
		realtime.assert_not_called()
		run_tool.assert_not_called()
		for surface in (result, error_capture.call_args_list):
			serialized = json.dumps(surface, default=str)
			self.assertNotIn(sentinel, serialized)
			self.assertNotIn(call_id, serialized)
		self.assertEqual(error_capture.call_args.kwargs["route"], _private_output.PRIVATE_DELEGATE_TOOL_LABEL)
		final = self._status(self.record)
		self.assertEqual(final.status, "running")
		self.assertIsNone(final.dashboard)
		self.assertEqual(frappe.db.count("Jarvis Dashboard", {"agent_run": self.record.run}), 0)
		self.assertEqual(frappe.db.count("Jarvis Agent Finding", {"run": self.record.run}), 0)
		self.assertEqual(frappe.db.count("Jarvis Agent Run Step", {"run": self.record.run}), 0)

	def _thread(self, target, errors: list, results: list | None = None) -> threading.Thread:
		site, sites_path = frappe.local.site, frappe.local.sites_path

		def runner():
			try:
				frappe.init(site, sites_path=sites_path)
				frappe.connect()
				frappe.set_user("Administrator")
				result = target()
				if results is not None:
					results.append(result)
				frappe.db.commit()
			except Exception as exc:
				errors.append(repr(exc))
			finally:
				try:
					frappe.destroy()
				except Exception:
					pass

		return threading.Thread(target=runner)

	def _run_race(self, *, first, second, patch_target):
		locked, release = threading.Event(), threading.Event()
		thread_state = threading.local()
		results, errors = [], []
		real_fail = patch_target.fail_locked_run

		def held_fail(*args, **kwargs):
			if getattr(thread_state, "first", False):
				locked.set()
				if not release.wait(10):
					raise TimeoutError("recovery lock was not released")
			return real_fail(*args, **kwargs)

		def first_target():
			thread_state.first = True
			return first()

		with patch.object(patch_target, "fail_locked_run", side_effect=held_fail):
			first_thread = self._thread(first_target, errors, results)
			second_thread = self._thread(second, errors, results)
			first_thread.start()
			self.assertTrue(locked.wait(10), errors)
			second_thread.start()
			time.sleep(0.1)
			release.set()
			first_thread.join(15)
			second_thread.join(15)

		self.assertFalse(first_thread.is_alive())
		self.assertFalse(second_thread.is_alive())
		self.assertEqual(errors, [])
		self.assertEqual(sorted(results), [False, True])
