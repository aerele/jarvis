"""Tests for jarvis.oauth.cron.poll_oauth_refresh_status.

Sprint-3 (2026-06-16 review): hourly reconciliation between the bench's
cached OAuth state and what the container actually holds. Pins the
state machine:

  oauth + present  -> no-op (don't clobber a successful save's status)
  oauth + absent   -> flip to oauth_expired, clear cached email
  oauth + unreachable -> skip (ambiguous - don't false-flip)
  oauth + admin auth -> log + write 'failed: auth: ...' (distinct from expired)
  api_key mode     -> no-op (path doesn't exist for api-key tenants)
  never connected  -> no-op (nothing to invalidate)
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.oauth.cron import poll_oauth_refresh_status


class _PollTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		s = frappe.get_single("Jarvis Settings")
		cls._snap = {
			"llm_auth_mode": s.get("llm_auth_mode"),
			"llm_oauth_account_email": s.get("llm_oauth_account_email"),
			"last_sync_status": s.get("last_sync_status"),
			"last_sync_at": s.get("last_sync_at"),
		}

	@classmethod
	def tearDownClass(cls):
		s = frappe.get_single("Jarvis Settings")
		for k, v in cls._snap.items():
			s.db_set(k, v)
		frappe.db.commit()
		super().tearDownClass()

	def _set(self, **kw):
		s = frappe.get_single("Jarvis Settings")
		for k, v in kw.items():
			s.db_set(k, v, update_modified=False)
		frappe.db.commit()


class TestOauthRefreshPoll(_PollTestCase):
	def setUp(self):
		self._set(
			llm_auth_mode="oauth",
			llm_oauth_account_email="connected@example.com",
			last_sync_status="ok (restart via admin)",
		)

	def test_skips_when_mode_is_api_key(self):
		"""API-key tenants don't have a refresh path; cron must no-op."""
		self._set(llm_auth_mode="api_key")
		with patch("jarvis.admin_client.post_llm_auth_status") as admin:
			poll_oauth_refresh_status()
		admin.assert_not_called()

	def test_skips_when_never_connected(self):
		"""Empty llm_oauth_account_email -> nothing to invalidate."""
		self._set(llm_oauth_account_email="")
		with patch("jarvis.admin_client.post_llm_auth_status") as admin:
			poll_oauth_refresh_status()
		admin.assert_not_called()

	def test_no_change_when_profile_present(self):
		"""Container reports profile present -> leave last_sync_status
		alone (don't clobber the legitimate save's 'ok' marker)."""
		with patch(
			"jarvis.admin_client.post_llm_auth_status", return_value={"data": {"auth_profile_present": True}}
		):
			poll_oauth_refresh_status()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.last_sync_status, "ok (restart via admin)")
		self.assertEqual(s.llm_oauth_account_email, "connected@example.com")

	def test_flips_to_expired_when_profile_absent(self):
		"""The core fix: container reports profile absent -> flip status
		to oauth_expired AND clear the cached account email so the UI
		can render 'reconnect'."""
		with patch(
			"jarvis.admin_client.post_llm_auth_status", return_value={"data": {"auth_profile_present": False}}
		):
			poll_oauth_refresh_status()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.last_sync_status, "oauth_expired")
		self.assertEqual(s.llm_oauth_account_email, "")
		self.assertIsNotNone(s.last_sync_at)

	def test_admin_unreachable_does_not_false_flip(self):
		"""AdminUnreachable is ambiguous (could be admin down OR container
		lost profile). Must NOT flip status - the next hour retries."""
		from jarvis.exceptions import AdminUnreachableError

		with patch(
			"jarvis.admin_client.post_llm_auth_status",
			side_effect=AdminUnreachableError("connection refused"),
		):
			poll_oauth_refresh_status()
		s = frappe.get_single("Jarvis Settings")
		# Unchanged.
		self.assertEqual(s.last_sync_status, "ok (restart via admin)")
		self.assertEqual(s.llm_oauth_account_email, "connected@example.com")

	def test_admin_auth_error_writes_distinct_status(self):
		"""AdminAuthError is a different failure mode from oauth_expired
		(bench credentials wrong, not container state). Surface it
		separately so the operator can act on the right thing."""
		from jarvis.exceptions import AdminAuthError

		with patch("jarvis.admin_client.post_llm_auth_status", side_effect=AdminAuthError("bad bench creds")):
			poll_oauth_refresh_status()
		s = frappe.get_single("Jarvis Settings")
		self.assertIn("failed", s.last_sync_status or "")
		self.assertIn("auth", s.last_sync_status or "")
		# Email NOT cleared - this isn't an oauth_expired flip.
		self.assertEqual(s.llm_oauth_account_email, "connected@example.com")

	def test_admin_rate_limited_skips_without_flip(self):
		"""Rate-limit is transient; skip + retry next tick."""
		from jarvis.exceptions import AdminRateLimitedError

		with patch(
			"jarvis.admin_client.post_llm_auth_status",
			side_effect=AdminRateLimitedError(retry_after_seconds=600),
		):
			poll_oauth_refresh_status()
		s = frappe.get_single("Jarvis Settings")
		# Unchanged.
		self.assertEqual(s.last_sync_status, "ok (restart via admin)")
		self.assertEqual(s.llm_oauth_account_email, "connected@example.com")

	def test_admin_validation_error_skips_without_flip_and_does_not_raise(self):
		"""409 NoRunningTenant (container mid-restart, lapsed subscription)
		arrives as AdminValidationError. It is ambiguous for the same reason
		AdminUnreachableError is - a stopped container hasn't lost its profile
		- so it must not flip. It also must not escape: uncaught, it raised out
		of the hourly scheduled job."""
		from jarvis.exceptions import AdminValidationError

		with patch(
			"jarvis.admin_client.post_llm_auth_status", side_effect=AdminValidationError("NoRunningTenant")
		):
			poll_oauth_refresh_status()  # must not raise
		s = frappe.get_single("Jarvis Settings")
		# Unchanged.
		self.assertEqual(s.last_sync_status, "ok (restart via admin)")
		self.assertEqual(s.llm_oauth_account_email, "connected@example.com")
