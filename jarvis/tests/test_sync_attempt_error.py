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
	JarvisSettings,
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


class TestReconcileKeepsTheRetrySpacing(FrappeTestCase):
	"""Each handover attempt restarts the customer's container, so a recorded
	failure must NOT shorten the 600 s spacing between attempts."""

	def test_a_fresh_request_waits_even_with_an_error_set(self):
		settings = _handover_ready_settings(
			last_sync_status=f"{_PENDING_HANDOVER_STATUS} (attempt 1)",
			last_sync_requested_at=frappe.utils.now(),
			**{_ATTEMPT_ERROR_FIELD: _ATTEMPT_ERROR_UNREACHABLE},
		)
		settings.get_password = MagicMock(return_value="test-admin-key")
		settings._enqueue_handover = MagicMock()
		settings._enqueue_pool_sync = MagicMock()
		with patch("frappe.get_single", return_value=settings):
			reconcile_pending_llm_sync()
		settings._enqueue_handover.assert_not_called()
		settings._enqueue_pool_sync.assert_not_called()


class TestBypassingWritersClearTheError(FrappeTestCase):
	def test_the_disconnect_field_set_clears_it(self):
		from jarvis.onboarding import _DISCONNECTED_LLM_FIELDS

		self.assertEqual(_DISCONNECTED_LLM_FIELDS.get(_ATTEMPT_ERROR_FIELD), "")

	def test_the_workspace_reset_blanks_it(self):
		from jarvis import settings_reset

		self.assertIn(_ATTEMPT_ERROR_FIELD, settings_reset.FULL.blank)


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


class TestSingleModelSyncRecordsTheFailedAttempt(FrappeTestCase):
	"""The single-model (direct) path must mirror the pool path: an accepted
	"applying" push is NOT a failure, only AdminUnreachableError is."""

	def _run(self, post, *, converged=False):
		settings = frappe._dict(
			{
				"llm_auth_mode": "api_key",
				"llm_provider": "openai",
				"llm_model": "gpt-4o",
				"llm_base_url": "",
				"last_sync_status": _PENDING_APPLYING_STATUS,
				_ATTEMPT_ERROR_FIELD: "stale",
			}
		)
		settings._resolve_llm_secret_for_push = MagicMock(return_value="sk-test")
		settings._resync_custom_skills_after_restart = MagicMock()
		settings._resync_learned_skills_after_restart = MagicMock()
		with (
			patch.object(admin_client, "post_update_llm_creds", **post),
			patch("jarvis.installed_apps_sync.record_synced_snapshot"),
			patch(f"{_JS}._converge_via_admin", return_value=converged),
			patch(f"{_JS}._refresh_db_snapshot"),
			patch(f"{_JS}._commit_terminal_sync_status"),
			patch("frappe.db.set_single_value"),
		):
			JarvisSettings._sync_via_admin(settings, "restart")
		return settings

	def test_accepted_applying_leaves_no_attempt_error(self):
		settings = self._run({"return_value": {"status": "applying"}})
		self.assertEqual(settings.last_sync_status, _PENDING_APPLYING_STATUS)
		self.assertEqual(settings.get(_ATTEMPT_ERROR_FIELD), "")
		self.assertEqual(_sync_status_payload(settings, settings.last_sync_status)["attempt_error"], "")

	def test_unreachable_sets_the_error_and_the_page_is_failed(self):
		settings = self._run({"side_effect": AdminUnreachableError("timeout")})
		self.assertEqual(settings.last_sync_status, _PENDING_APPLYING_STATUS)
		self.assertEqual(settings.get(_ATTEMPT_ERROR_FIELD), _ATTEMPT_ERROR_UNREACHABLE)
		payload = _sync_status_payload(settings, settings.last_sync_status)
		self.assertTrue(payload["pending"])
		self.assertEqual(payload["attempt_error"], _ATTEMPT_ERROR_UNREACHABLE)

	def test_a_later_success_clears_it(self):
		settings = self._run({"return_value": {"status": "applied", "action": "restart"}})
		self.assertTrue(settings.last_sync_status.startswith("ok"))
		self.assertEqual(settings.get(_ATTEMPT_ERROR_FIELD), "")


class TestSyncStatusPayload(FrappeTestCase):
	def test_the_error_shows_only_while_pending(self):
		s = frappe._dict({_ATTEMPT_ERROR_FIELD: _ATTEMPT_ERROR_UNREACHABLE})
		self.assertEqual(
			_sync_status_payload(s, _PENDING_APPLYING_STATUS)["attempt_error"], _ATTEMPT_ERROR_UNREACHABLE
		)
		self.assertEqual(_sync_status_payload(s, "ok (restart via admin)")["attempt_error"], "")
