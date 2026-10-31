"""J4a: the site challenge endpoint, one-time nonces, and the proof the site sends
with every sensitive admin call.

Every class that touches Jarvis Settings uses ``JarvisSettingsSnapshot``. Every
admin_client network call is patched; settings_reset and the transport teardown are
never called against real settings.
"""

import contextlib
import re
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client, onboarding
from jarvis._password_utils import clear_settings_password, set_settings_password
from jarvis._plugin_auth import PluginAuthError
from jarvis.api import pairing_challenge
from jarvis.chat import device as chat_device
from jarvis.chat import pairing_proof, site_pin
from jarvis.exceptions import AdminAuthError, AdminSiteIsCopyError
from jarvis.tests._settings_snapshot import JarvisSettingsSnapshot
from jarvis.tests.test_api import _FakeRequest

SETTINGS = "Jarvis Settings"
HEX32 = re.compile(r"[0-9a-f]{32}")
CALLBACK = "cd" * 32


def _onboard():
	"""Fake admin credentials so sync_connection reaches its (patched) get_connection."""
	settings = frappe.get_single(SETTINGS)
	set_settings_password(settings, "jarvis_admin_api_key", "k", update_modified=False)
	set_settings_password(settings, "jarvis_admin_api_secret", "s", update_modified=False)


def _drop(nonce):
	frappe.cache().delete_value("jarvis:pairing_proof:" + nonce)


class TestNonces(FrappeTestCase):
	def test_issue_and_consume_once(self):
		self.assertTrue(pairing_proof.consume_nonce(pairing_proof.issue_nonce()))

	def test_nonce_is_single_use(self):
		nonce = pairing_proof.issue_nonce()
		self.assertTrue(pairing_proof.consume_nonce(nonce))
		self.assertFalse(pairing_proof.consume_nonce(nonce))

	def test_consume_rejects_malformed(self):
		for bad in ("../x", "", "a" * 31, "A" * 32, None):
			self.assertFalse(pairing_proof.consume_nonce(bad))

	def test_nonce_is_32_hex(self):
		nonce = pairing_proof.issue_nonce()
		self.addCleanup(_drop, nonce)
		self.assertRegex(nonce, HEX32)


class TestPairingChallenge(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_single_value(
			SETTINGS,
			{"paired_site": "", "pairing_blocked": 0, "pairing_blocked_reason": ""},
			update_modified=False,
		)

	@contextlib.contextmanager
	def _request(self, session=pairing_proof.PROOF_SESSION_KEY, verified=True):
		if verified:
			verify = patch("jarvis.api.validate_plugin_request", return_value=session)
		else:
			err = PluginAuthError(http_status=400, code="InvalidArgumentError", message="unsigned")
			verify = patch("jarvis.api.validate_plugin_request", side_effect=err)
		with patch.object(frappe, "request", _FakeRequest({}), create=True), verify:
			yield

	def test_challenge_requires_signature(self):
		nonce = pairing_proof.issue_nonce()
		with self._request(verified=False):
			res = pairing_challenge(nonce)
		self.assertFalse(res["ok"])
		self.assertEqual(frappe.local.response.http_status_code, 400)
		self.assertTrue(pairing_proof.consume_nonce(nonce))  # untouched

	def test_challenge_rejects_other_session_key(self):
		nonce = pairing_proof.issue_nonce()
		with self._request(session="agent:main:dashboard:x"):
			res = pairing_challenge(nonce)
		self.assertFalse(res["ok"])
		self.assertEqual(frappe.local.response.http_status_code, 400)
		self.assertTrue(pairing_proof.consume_nonce(nonce))

	def test_challenge_held_true_and_pins(self):
		nonce = pairing_proof.issue_nonce()
		with self._request():
			res = pairing_challenge(nonce)
		self.assertEqual(res, {"ok": True, "held": True})
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "paired_site"), frappe.local.site)

	def test_challenge_held_false_for_foreign_nonce(self):
		with self._request():
			res = pairing_challenge("ab" * 16)
		self.assertEqual(res, {"ok": True, "held": False})
		self.assertFalse(frappe.db.get_single_value(SETTINGS, "paired_site"))


class TestWrappersSendProof(FrappeTestCase):
	def _body(self, fn, *args, **kwargs):
		with patch.object(admin_client, "_post", return_value={}) as post:
			fn(*args, **kwargs)
		return post.call_args.kwargs["body"]

	def test_wrappers_send_proof_nonce(self):
		calls = (
			(admin_client.request_chat_pairing, ("pk", "did")),
			(admin_client.pair_chat_device, ("pk", "did")),
			(admin_client.post_rotate_agent_token, ("t" * 32,)),
			(admin_client.unpair_chat_devices, ()),
		)
		for fn, args in calls:
			self.assertNotIn("proof_nonce", self._body(fn, *args), fn.__name__)
			self.assertEqual(self._body(fn, *args, proof_nonce="n" * 32)["proof_nonce"], "n" * 32)

	def test_get_connection_sends_proof_nonce_only_when_set(self):
		with (
			patch.object(admin_client, "_post", return_value={}) as post,
			patch("jarvis.chat.runtime_profile.ingest_current"),
		):
			admin_client.get_connection()
			self.assertNotIn("proof_nonce", post.call_args.kwargs["body"])
			admin_client.get_connection(proof_nonce="n" * 32)
			self.assertEqual(post.call_args.kwargs["body"]["proof_nonce"], "n" * 32)


class TestEstablishPairing(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		# Start unpaired; the snapshot restores the real rows afterwards.
		settings = frappe.get_single(SETTINGS)
		frappe.db.set_single_value(
			SETTINGS, {"chat_device_id": "", "chat_device_public_key": ""}, update_modified=False
		)
		for field in ("chat_device_private_key", "chat_device_token"):
			clear_settings_password(settings, field)

	def test_establish_pairing_sends_proof_nonce(self):
		seen = {}

		def _fake(public_key, device_id, **kw):
			seen.update(kw)
			return {"mode": "legacy", "device_token": "tok"}

		with (
			patch("jarvis.chat.device.admin_client.request_chat_pairing", side_effect=_fake),
			patch("jarvis.onboarding.fetch_callback_key_if_missing"),
		):
			chat_device._establish_pairing()
		self.assertRegex(seen["proof_nonce"], HEX32)

	def test_mechanism_a_returns_bootstrap_codes_untouched(self):
		resp = {"mode": "mechanism_a", "accepted": True, "bootstrap_codes": ["c1", "c2"]}
		set_settings_password(frappe.get_single(SETTINGS), "agent_token", "gw-token", update_modified=False)
		with (
			patch("jarvis.chat.device.admin_client.request_chat_pairing", return_value=resp),
			patch("jarvis.onboarding.fetch_callback_key_if_missing"),
		):
			creds = chat_device._establish_pairing()
		self.assertEqual(creds.bootstrap_codes, ("c1", "c2"))

	def test_pairing_response_stores_callback_key(self):
		"""A callback_key riding a pairing response is ignored; the key arrives only
		through the proof-backed sync (write_connection's guarded path)."""
		_onboard()
		clear_settings_password(frappe.get_single(SETTINGS), "callback_key")
		resp = {"mode": "legacy", "device_token": "tok", "callback_key": "ee" * 32}
		conn = {"agent_url": "ws://x.invalid", "callback_key": CALLBACK}
		with (
			patch("jarvis.chat.device.admin_client.request_chat_pairing", return_value=resp),
			patch("jarvis.onboarding.admin_client.get_connection", return_value=conn) as get_conn,
		):
			chat_device._establish_pairing()
		self.assertRegex(get_conn.call_args.kwargs["proof_nonce"], HEX32)
		stored = frappe.get_single(SETTINGS).get_password("callback_key", raise_exception=False)
		self.assertEqual(stored, CALLBACK)


class TestSyncConnection(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		_onboard()

	def _sync(self, conn):
		with (
			patch("jarvis.onboarding.require_jarvis_admin"),
			patch("jarvis.onboarding.admin_client.get_connection", return_value=conn) as get_conn,
		):
			onboarding.sync_connection()
		return get_conn

	def test_sync_connection_sends_proof_nonce(self):
		get_conn = self._sync({"tenant_status": "running"})
		self.assertRegex(get_conn.call_args.kwargs["proof_nonce"], HEX32)

	def test_sync_connection_keeps_token_when_payload_omits_it(self):
		set_settings_password(frappe.get_single(SETTINGS), "agent_token", "keep-me", update_modified=False)
		self._sync({"agent_url": "ws://x.invalid"})
		stored = frappe.get_single(SETTINGS).get_password("agent_token", raise_exception=False)
		self.assertEqual(stored, "keep-me")

	def test_retired_without_key_runs_proof_backed_sync(self):
		clear_settings_password(frappe.get_single(SETTINGS), "callback_key")
		calls = []

		def _conn(**kw):
			calls.append(kw)
			return {"agent_url": "ws://x.invalid", "callback_key": CALLBACK, "owner_key_retired": True}

		with (
			patch("jarvis.onboarding.admin_client.get_connection", side_effect=_conn),
		):
			onboarding.write_connection({"owner_key_retired": True})
		self.assertEqual(len(calls), 1)
		self.assertRegex(calls[0]["proof_nonce"], HEX32)
		self.assertEqual(
			frappe.get_single(SETTINGS).get_password("callback_key", raise_exception=False), CALLBACK
		)

	def test_retired_fetch_does_not_loop_when_no_key_returned(self):
		clear_settings_password(frappe.get_single(SETTINGS), "callback_key")
		with (
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				return_value={"agent_url": "ws://x.invalid", "owner_key_retired": True},
			) as get_conn,
		):
			onboarding.write_connection({"owner_key_retired": True})
		self.assertEqual(get_conn.call_count, 1)


class TestRotateAndUnpair(JarvisSettingsSnapshot, FrappeTestCase):
	def test_rotate_agent_token_sends_proof_nonce(self):
		from jarvis.api import rotate_agent_token

		with patch.object(admin_client, "post_rotate_agent_token", return_value={}) as post:
			rotate_agent_token()
		self.assertRegex(post.call_args.kwargs["proof_nonce"], HEX32)


class TestReverify(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_single_value(
			SETTINGS,
			{"paired_site": "other.site", "pairing_blocked": 1, "pairing_blocked_reason": "copy"},
			update_modified=False,
		)

	def test_reverify_unblocks_when_proved(self):
		with patch("jarvis.admin_client.verify_site", return_value={"proved": True}) as vs:
			out = site_pin.reverify()
		self.assertRegex(vs.call_args.args[0], HEX32)
		self.assertFalse(out["blocked"])
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "paired_site"), frappe.local.site)

	def test_reverify_keeps_block_when_not_proved(self):
		err = AdminAuthError("no", status_code=403, code="SiteProofFailed")
		with patch("jarvis.admin_client.verify_site", side_effect=err):
			out = site_pin.reverify()
		self.assertTrue(out["blocked"])
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "paired_site"), "other.site")

	def test_reverify_requires_system_manager(self):
		with (
			patch("frappe.only_for", side_effect=frappe.PermissionError),
			patch("jarvis.admin_client.verify_site") as vs,
		):
			with self.assertRaises(frappe.PermissionError):
				site_pin.reverify()
		vs.assert_not_called()

	def test_verify_site_call_bypasses_copy_gate(self):
		fake = MagicMock()
		with (
			patch("frappe.get_single", return_value=fake),
			patch.object(admin_client, "_admin_url", return_value="https://admin.invalid"),
			patch.object(admin_client, "_admin_access_token", return_value="bearer"),
			patch.object(admin_client, "_do_post", return_value={"proved": True}) as do_post,
		):
			self.assertEqual(admin_client.verify_site("n" * 32), {"proved": True})
			self.assertTrue(do_post.called)
			do_post.reset_mock()
			with self.assertRaises(AdminSiteIsCopyError):
				admin_client._post("/x", {})
			do_post.assert_not_called()
