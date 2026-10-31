"""Stale-key self-heal and Error Log hygiene for the key-sync paths.

A plugin request carrying a key this site does not hold queues ONE throttled,
proof-backed key sync. Every ``frappe.log_error`` the sharing-defense branch added
passes an explicit message: without one Frappe dumps each frame's locals, and these
frames hold the admin Authorization header and the connection payload.

``frappe.enqueue`` is always patched: nothing here may reach a real worker.
"""

import ast
import pathlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import _plugin_auth, onboarding
from jarvis._password_utils import clear_settings_password, set_settings_password
from jarvis._plugin_auth import PluginAuthError
from jarvis.tests._settings_snapshot import JarvisSettingsSnapshot
from jarvis.tests.test_api import _FakeRequest, sign_plugin_headers

SETTINGS = "Jarvis Settings"
CALLBACK = "ab" * 32
OWNER = "owner-token-heal"
SESSION = "agent:test:heal"
SECRET = "super-secret-bearer-value"


def _throttle_key() -> str:
	return onboarding._KEY_RESYNC_THROTTLE_PREFIX + frappe.local.site


def _clear_throttle():
	frappe.cache().delete(_throttle_key())


class TestStaleKeySelfHeal(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		settings = frappe.get_single(SETTINGS)
		set_settings_password(settings, "agent_token", OWNER, update_modified=False)
		set_settings_password(settings, "callback_key", CALLBACK, update_modified=False)
		_clear_throttle()
		self.addCleanup(_clear_throttle)
		audit = patch.object(_plugin_auth, "_audit_log")
		audit.start()
		self.addCleanup(audit.stop)

	def _validate(self, token, *, sign_with=None):
		headers = {"X-Jarvis-Token": token, "X-Jarvis-Session": SESSION}
		headers.update(sign_plugin_headers(sign_with or token, SESSION))
		with patch.object(frappe, "request", _FakeRequest(headers), create=True):
			return _plugin_auth.validate_plugin_request(b"")

	def _reject(self, token, **kw):
		with self.assertRaises(PluginAuthError) as ctx:
			self._validate(token, **kw)
		return ctx.exception

	def test_a_mismatched_key_is_rejected_and_queues_one_proof_backed_sync(self):
		with patch("frappe.enqueue") as enq:
			err = self._reject("a-key-this-site-never-stored")
		self.assertEqual(err.http_status, 401)
		enq.assert_called_once()
		self.assertEqual(enq.call_args.args[0], "jarvis.onboarding.run_key_resync")
		self.assertEqual(enq.call_args.kwargs["queue"], "short")
		self.assertTrue(enq.call_args.kwargs["deduplicate"])
		self.assertTrue(enq.call_args.kwargs["enqueue_after_commit"])
		self.assertIn(frappe.local.site, enq.call_args.kwargs["job_id"])

	def test_a_second_mismatch_inside_the_window_queues_nothing(self):
		with patch("frappe.enqueue") as enq:
			self._reject("stale-1")
			self._reject("stale-2")
			self._reject("stale-1")
		self.assertEqual(enq.call_count, 1)

	def test_the_window_is_ten_minutes(self):
		with patch("frappe.enqueue"):
			self._reject("stale-1")
		ttl = frappe.cache().ttl(_throttle_key())
		self.assertTrue(0 < ttl <= 600)
		self.assertEqual(onboarding.KEY_RESYNC_THROTTLE_S, 600)

	def test_a_new_window_queues_again(self):
		with patch("frappe.enqueue") as enq:
			self._reject("stale-1")
			_clear_throttle()
			self._reject("stale-1")
		self.assertEqual(enq.call_count, 2)

	def test_a_matching_key_never_queues(self):
		with patch("frappe.enqueue") as enq:
			self.assertEqual(self._validate(CALLBACK), SESSION)
			self.assertEqual(self._validate(OWNER), SESSION)
		enq.assert_not_called()

	def test_a_matching_key_with_a_bad_signature_never_queues(self):
		with patch("frappe.enqueue") as enq:
			err = self._reject(CALLBACK, sign_with=OWNER)
		self.assertEqual(err.http_status, 401)
		enq.assert_not_called()

	def test_no_stored_key_at_all_queues_nothing(self):
		settings = frappe.get_single(SETTINGS)
		clear_settings_password(settings, "agent_token")
		clear_settings_password(settings, "callback_key")
		with patch("frappe.enqueue") as enq:
			err = self._reject("anything")
		self.assertEqual(err.http_status, 503)
		enq.assert_not_called()

	def test_an_enqueue_failure_never_changes_the_reject(self):
		with patch("frappe.enqueue", side_effect=RuntimeError("queue down")):
			err = self._reject("stale-1")
		self.assertEqual(err.http_status, 401)

	def test_the_job_runs_one_proof_backed_sync_with_the_normal_timeout(self):
		with (
			patch("jarvis.onboarding._sync_connection") as sync,
			patch("frappe.enqueue") as enq,
		):
			onboarding.run_key_resync()
		sync.assert_called_once_with(prove=True)
		enq.assert_not_called()

	def test_the_job_runs_as_administrator(self):
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user("Guest")
		seen = []
		with patch(
			"jarvis.onboarding._sync_connection", side_effect=lambda **kw: seen.append(frappe.session.user)
		):
			onboarding.run_key_resync()
		self.assertEqual(seen, ["Administrator"])

	def test_the_job_logs_a_failure_without_frame_locals(self):
		with (
			patch("jarvis.onboarding._sync_connection", side_effect=RuntimeError("admin down")),
			patch.object(frappe, "log_error") as log_error,
		):
			onboarding.run_key_resync()
		kwargs = log_error.call_args.kwargs
		self.assertIn("RuntimeError: admin down", kwargs["message"])
		self.assertNotIn("Local variables", kwargs["message"])
		self.assertFalse(frappe.flags.get("jarvis_key_fetch"))

	def test_a_failed_job_shortens_the_throttle_to_two_minutes(self):
		with patch("frappe.enqueue"):
			self._reject("stale-1")
		self.assertTrue(120 < frappe.cache().ttl(_throttle_key()) <= 600)
		with (
			patch("jarvis.onboarding._sync_connection", side_effect=RuntimeError("admin down")),
			patch.object(frappe, "log_error"),
		):
			onboarding.run_key_resync()
		ttl = frappe.cache().ttl(_throttle_key())
		self.assertTrue(0 < ttl <= onboarding.KEY_RESYNC_FAILED_THROTTLE_S)
		self.assertEqual(onboarding.KEY_RESYNC_FAILED_THROTTLE_S, 120)

	def test_a_failed_job_never_lengthens_a_shorter_throttle(self):
		frappe.cache().set(_throttle_key(), b"1", ex=30)
		with (
			patch("jarvis.onboarding._sync_connection", side_effect=RuntimeError("admin down")),
			patch.object(frappe, "log_error"),
		):
			onboarding.run_key_resync()
		self.assertTrue(0 < frappe.cache().ttl(_throttle_key()) <= 30)

	def test_a_failed_job_without_a_throttle_key_creates_none(self):
		with (
			patch("jarvis.onboarding._sync_connection", side_effect=RuntimeError("admin down")),
			patch.object(frappe, "log_error"),
		):
			onboarding.run_key_resync()
		self.assertEqual(frappe.cache().ttl(_throttle_key()), -2)

	def test_a_successful_job_keeps_the_ten_minute_throttle(self):
		with patch("frappe.enqueue"):
			self._reject("stale-1")
		with patch("jarvis.onboarding._sync_connection"):
			onboarding.run_key_resync()
		self.assertTrue(120 < frappe.cache().ttl(_throttle_key()) <= 600)


class TestErrorLogCarriesNoSecrets(JarvisSettingsSnapshot, FrappeTestCase):
	def _boom(self, **kwargs):
		headers = {"Authorization": f"Bearer {SECRET}"}  # a frame local, like admin_client._do_post
		payload = {"callback_key": SECRET, "agent_token": SECRET}
		raise RuntimeError(f"admin refused ({len(headers)}, {len(payload)})")

	def test_callback_key_fetch_failure_logs_no_key_material(self):
		set_settings_password(frappe.get_single(SETTINGS), "jarvis_admin_api_key", "k", update_modified=False)
		set_settings_password(
			frappe.get_single(SETTINGS), "jarvis_admin_api_secret", "s", update_modified=False
		)
		with patch("jarvis.onboarding.admin_client.get_connection", side_effect=self._boom):
			onboarding._best_effort_key_sync()
		row = frappe.get_last_doc("Error Log", filters={"method": "callback key fetch failed"})
		self.assertIn("RuntimeError: admin refused", row.error)
		self.assertNotIn(SECRET, row.error)
		self.assertNotIn("Local variables", row.error)
		self.assertNotIn("Authorization", row.error)

	def test_every_log_error_this_branch_added_passes_a_message(self):
		"""A bare ``frappe.log_error(title=...)`` dumps frame locals. Guard the call sites
		the branch added or touched, by title."""
		root = pathlib.Path(frappe.get_app_path("jarvis"))
		titles = {
			"callback key fetch failed",
			"callback key resync failed",
			"callback key fetch: enqueue failed",
			"callback key resync: enqueue failed",
			"site_pin: pin failed",
			"site_pin: status read failed",
			"vouch_session: lookup failed",
			"site_identity: instance id unavailable",
		}
		files = ("api.py", "onboarding.py", "chat/site_pin.py", "chat/site_identity.py")
		seen = set()
		for rel in files:
			tree = ast.parse((root / rel).read_text())
			for node in ast.walk(tree):
				if not (
					isinstance(node, ast.Call)
					and isinstance(node.func, ast.Attribute)
					and node.func.attr == "log_error"
				):
					continue
				title = next(
					(
						kw.value.value
						for kw in node.keywords
						if kw.arg == "title" and isinstance(kw.value, ast.Constant)
					),
					None,
				)
				if title in titles:
					seen.add(title)
					self.assertIn("message", {kw.arg for kw in node.keywords}, f"{rel}: {title}")
		self.assertEqual(seen, titles)
