"""admin-v2#630: a failed apply attempt that is still pending a retry says so.

``last_sync_status`` keeps its exact format (``_handover_attempt`` parses
" (attempt N)" with fullmatch, so a suffix would reset the attempt count); the
failure rides on ``last_sync_attempt_error``. It is set where an attempt dies on
AdminUnreachableError and cleared by every other status write.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client
from jarvis.admin_client import AdminUnreachableError
from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
	_ATTEMPT_ERROR_FIELD,
	_ATTEMPT_ERROR_UNREACHABLE,
	_PENDING_APPLYING_STATUS,
	_PENDING_HANDOVER_STATUS,
	_enqueued_sync_via_admin_pool,
	_handover_via_admin,
	_stamp_pool_pending,
	_write_settings_fields,
	reconcile_pending_llm_sync,
)
from jarvis.onboarding import _sync_status_payload
from jarvis.tests.test_lone_direct_handover import _handover_ready_settings
from jarvis.tests.test_settings_on_update import _reset_settings
from jarvis.tests.test_unified_llm_config import _add_model_row, _RT3SettingsTestCase

_JS = "jarvis.jarvis.doctype.jarvis_settings.jarvis_settings"


class TestWriteSettingsFieldsClearsTheError(FrappeTestCase):
	def test_a_status_write_clears_the_error(self):
		settings = frappe._dict({_ATTEMPT_ERROR_FIELD: _ATTEMPT_ERROR_UNREACHABLE})
		with patch("frappe.db.set_single_value") as mock_set:
			_write_settings_fields(settings, {"last_sync_status": "ok (restart via admin)"})
		self.assertEqual(settings[_ATTEMPT_ERROR_FIELD], "")
		self.assertEqual(mock_set.call_args.args[1][_ATTEMPT_ERROR_FIELD], "")

	def test_an_explicit_error_is_kept(self):
		settings = frappe._dict()
		with patch("frappe.db.set_single_value"):
			_write_settings_fields(
				settings,
				{"last_sync_status": _PENDING_APPLYING_STATUS, _ATTEMPT_ERROR_FIELD: "boom"},
			)
		self.assertEqual(settings[_ATTEMPT_ERROR_FIELD], "boom")

	def test_a_write_without_a_status_leaves_it_alone(self):
		settings = frappe._dict({_ATTEMPT_ERROR_FIELD: "boom"})
		with patch("frappe.db.set_single_value"):
			_write_settings_fields(settings, {"llm_pool_synced_at": "2026-10-01 00:00:00"})
		self.assertEqual(settings[_ATTEMPT_ERROR_FIELD], "boom")

	def test_a_new_sync_request_clears_it(self):
		settings = frappe._dict({_ATTEMPT_ERROR_FIELD: "boom"})
		with patch("frappe.db.set_single_value"):
			_stamp_pool_pending(settings)
		self.assertEqual(settings[_ATTEMPT_ERROR_FIELD], "")


class TestHandoverRecordsTheFailedAttempt(FrappeTestCase):
	def _run(self, post):
		settings = _handover_ready_settings(
			last_sync_status=f"{_PENDING_HANDOVER_STATUS} (attempt 2)",
			**{_ATTEMPT_ERROR_FIELD: "stale"},
		)
		with (
			patch.object(admin_client, "post_subscription_handover", **post),
			patch("frappe.db.set_single_value"),
			patch(f"{_JS}._commit_terminal_sync_status"),
		):
			_handover_via_admin(settings)
		return settings

	def test_unreachable_keeps_the_status_format_and_sets_the_error(self):
		settings = self._run({"side_effect": AdminUnreachableError("timeout")})
		self.assertEqual(settings.last_sync_status, f"{_PENDING_HANDOVER_STATUS} (attempt 2)")
		self.assertEqual(settings.get(_ATTEMPT_ERROR_FIELD), _ATTEMPT_ERROR_UNREACHABLE)

	def test_a_later_success_clears_it(self):
		settings = self._run({"return_value": {"result": "ok", "status": "applied"}})
		self.assertTrue(settings.last_sync_status.startswith("ok"))
		self.assertEqual(settings.get(_ATTEMPT_ERROR_FIELD), "")


class TestReconcileSkipsTheAgeGateOnAFailedAttempt(FrappeTestCase):
	def _reconcile(self, **overrides):
		settings = _handover_ready_settings(
			last_sync_status=f"{_PENDING_HANDOVER_STATUS} (attempt 1)",
			last_sync_requested_at=frappe.utils.now(),
			**overrides,
		)
		settings.get_password = MagicMock(return_value="test-admin-key")
		settings._enqueue_handover = MagicMock()
		settings._enqueue_pool_sync = MagicMock()
		with patch("frappe.get_single", return_value=settings):
			reconcile_pending_llm_sync()
		return settings

	def test_a_fresh_request_without_an_error_still_waits(self):
		settings = self._reconcile()
		settings._enqueue_handover.assert_not_called()

	def test_a_fresh_request_with_an_error_is_redriven_on_the_next_tick(self):
		settings = self._reconcile(**{_ATTEMPT_ERROR_FIELD: _ATTEMPT_ERROR_UNREACHABLE})
		settings._enqueue_handover.assert_called_once_with(attempt=2)

	def test_the_attempt_cap_still_holds_with_an_error(self):
		settings = _handover_ready_settings(
			last_sync_status=f"{_PENDING_HANDOVER_STATUS} (attempt 3)",
			last_sync_requested_at=frappe.utils.now(),
			**{_ATTEMPT_ERROR_FIELD: _ATTEMPT_ERROR_UNREACHABLE},
		)
		settings.get_password = MagicMock(return_value="test-admin-key")
		settings._enqueue_handover = MagicMock()
		settings._enqueue_pool_sync = MagicMock()
		with patch("frappe.get_single", return_value=settings), patch("frappe.log_error"):
			reconcile_pending_llm_sync()
		settings._enqueue_handover.assert_not_called()
		settings._enqueue_pool_sync.assert_called_once()


class TestPoolSyncRecordsTheFailedAttempt(_RT3SettingsTestCase):
	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("llm_pool_synced_at", None, update_modified=False)
		settings.db_set("last_sync_status", "", update_modified=False)
		frappe.db.commit()
		settings = frappe.get_single("Jarvis Settings")
		for i, model in enumerate(("gpt-4o", "gpt-3.5-turbo")):
			_add_model_row(
				settings,
				provider="openai_compat",
				model=model,
				tier="strong",
				order=i,
				api_key=f"sk-attempt-{i}",
				base_url="https://api.openai.com",
			)
		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()

	def test_unreachable_pending_sets_the_error_and_ok_clears_it(self):
		with (
			patch("jarvis.admin_client.post_update_llm_pool", side_effect=AdminUnreachableError("timeout")),
			patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Configuring"}),
		):
			_enqueued_sync_via_admin_pool()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.last_sync_status, _PENDING_APPLYING_STATUS)
		self.assertEqual(settings.get(_ATTEMPT_ERROR_FIELD), _ATTEMPT_ERROR_UNREACHABLE)

		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			_enqueued_sync_via_admin_pool()
		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue((settings.last_sync_status or "").startswith("ok"))
		self.assertEqual(settings.get(_ATTEMPT_ERROR_FIELD) or "", "")

	def test_resync_clears_it(self):
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import request_resync

		frappe.db.set_single_value("Jarvis Settings", _ATTEMPT_ERROR_FIELD, _ATTEMPT_ERROR_UNREACHABLE)
		with patch("jarvis.admin_client.post_update_llm_pool", side_effect=AdminUnreachableError("t")):
			settings = frappe.get_single("Jarvis Settings")
			with patch(f"{_JS}._switch_or_enqueue"):
				request_resync(settings)
		self.assertEqual(frappe.get_single("Jarvis Settings").get(_ATTEMPT_ERROR_FIELD) or "", "")


class TestSyncStatusPayload(FrappeTestCase):
	def test_the_error_shows_only_while_pending(self):
		s = frappe._dict({_ATTEMPT_ERROR_FIELD: _ATTEMPT_ERROR_UNREACHABLE})
		self.assertEqual(
			_sync_status_payload(s, _PENDING_APPLYING_STATUS)["attempt_error"], _ATTEMPT_ERROR_UNREACHABLE
		)
		self.assertEqual(_sync_status_payload(s, "ok (restart via admin)")["attempt_error"], "")
