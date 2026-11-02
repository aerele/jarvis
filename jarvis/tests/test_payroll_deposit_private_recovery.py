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
from jarvis._plugin_auth import _RECOVERY_RATE_PER_MINUTE
from jarvis._session import impersonate
from jarvis.chat import payroll_deposit_recovery

AGENT = "statutory-payroll-deposit-status"
TOKEN = "deposit-private-recovery-test-token"
SENTINEL = "SECRET-PRIVATE-ROW"


class _FakeRequest:
	def __init__(self, headers: dict[str, str], body: bytes):
		self.headers = headers
		self._body = body

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


class TestPayrollDepositPrivateRecovery(FrappeTestCase):
	"""Use real signed requests and database locks with isolated fake rows."""

	def setUp(self):
		super().setUp()
		self._run_names: list[str] = []
		self._installation_names: list[str] = []
		self._installations_by_user: dict[str, str] = {}
		self._session_keys: list[str] = []
		settings = frappe.get_single("Jarvis Settings")
		self._original_token = settings.get_password("agent_token", raise_exception=False) or ""
		self._original_device = settings.chat_device_id or ""
		settings.db_set("agent_token", TOKEN)
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
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("agent_token", self._original_token)
		settings.db_set("chat_device_id", self._original_device)
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
	def _request(self, record, *, body: bytes = b"{}", headers: dict[str, str] | None = None):
		request_headers = _signed_headers(TOKEN, record.session_key, body)
		request_headers.update(headers or {})
		with patch.object(frappe, "request", _FakeRequest(request_headers, body), create=True):
			yield

	def _callback(self, record, *, body: bytes = b"{}", headers: dict[str, str] | None = None) -> dict:
		with self._request(record, body=body, headers=headers):
			return api.terminalize_unusable_payroll_deposit_private_source()

	def _status(self, record) -> frappe._dict:
		frappe.db.commit()
		return frappe.db.get_value(
			"Jarvis Agent Run", record.run, ["status", "error", "dashboard"], as_dict=True
		)

	def test_signed_empty_callback_fails_only_its_active_run_with_a_fixed_ack(self):
		result = self._callback(
			self.record,
			headers={
				"X-Jarvis-Tool-Call-Id": SENTINEL,
				"X-Jarvis-Tool": SENTINEL,
			},
		)

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
			result = self._callback(
				self.record,
				headers={"X-Jarvis-Tool-Call-Id": SENTINEL, "X-Jarvis-Tool": SENTINEL},
			)
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
					self.assertEqual(self._callback(record), {"ok": True})
					self.assertEqual(self._status(record).status, "running")
		finally:
			frappe.db.set_value("User", disabled_user, "enabled", 1)
			frappe.db.commit()

	def test_stale_device_callback_is_inert(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("chat_device_id", "recovery-current-device")
		frappe.db.set_value(
			"Jarvis Chat Session",
			{"session_key": self.record.session_key},
			"chat_device_id",
			"recovery-old-device",
		)
		frappe.db.commit()

		self.assertEqual(self._callback(self.record), {"ok": True})
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
		result = self._callback(self.record, body=body)

		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		self.assertNotIn(SENTINEL, json.dumps(result))
		self.assertNotIn(self.record.run, json.dumps(result))
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
