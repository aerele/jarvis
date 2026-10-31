"""J4a: the site challenge endpoint (authenticated by the nonce alone), one-time
nonces, and the proof the site sends with the sensitive admin calls.

Every class that touches Jarvis Settings uses ``JarvisSettingsSnapshot``. Every
admin_client network call is patched; settings_reset and the transport teardown are
never called against real settings.
"""

import contextlib
import hashlib
import re
import threading
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client, onboarding
from jarvis._password_utils import clear_settings_password, set_settings_password
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

	def test_issued_nonce_expires(self):
		nonce = pairing_proof.issue_nonce()
		self.addCleanup(_drop, nonce)
		ttl = frappe.cache().ttl(pairing_proof._redis_key(nonce))
		self.assertTrue(0 < ttl <= pairing_proof._TTL_S)

	def test_consume_is_one_atomic_delete(self):
		"""Atomicity comes from redis, not from this code: each nonce is its own key and the
		consume is a single DEL whose reply (keys removed) says who won. Redis runs commands
		one at a time, so of N racing consumes exactly one sees 1. No read precedes it."""
		cache = MagicMock()
		cache.make_key.return_value = b"k"
		cache.delete.side_effect = [1, 0]
		with patch("frappe.cache", return_value=cache):
			first = pairing_proof.consume_nonce("ab" * 16)
			second = pairing_proof.consume_nonce("ab" * 16)
		self.assertEqual((first, second), (True, False))
		self.assertEqual(cache.delete.call_count, 2)
		cache.get.assert_not_called()
		cache.get_value.assert_not_called()

	def test_concurrent_consumes_of_one_nonce_have_one_winner(self):
		"""Real redis: threads race one nonce; exactly one gets True."""
		nonce = pairing_proof.issue_nonce()
		self.addCleanup(_drop, nonce)
		results = []
		barrier = threading.Barrier(8)
		cache = frappe.cache()
		key = pairing_proof._redis_key(nonce)

		def worker():
			barrier.wait()
			results.append(cache.delete(key) == 1)

		threads = [threading.Thread(target=worker) for _ in range(8)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()
		self.assertEqual(results.count(True), 1)


def _reset_challenge_limits():
	# The limiter's counters are plain (unprefixed) redis keys, like network_check's.
	cache = frappe.cache()
	for key in list(cache.scan_iter(match=pairing_proof._RL_PREFIX + "*")):
		cache.delete(key)


class TestPairingChallenge(JarvisSettingsSnapshot, FrappeTestCase):
	"""The endpoint authenticates by the nonce alone: no plugin headers, no signature."""

	def setUp(self):
		super().setUp()
		frappe.db.set_single_value(
			SETTINGS,
			{"paired_site": "", "pairing_blocked": 0, "pairing_blocked_reason": ""},
			update_modified=False,
		)
		_reset_challenge_limits()
		self.addCleanup(_reset_challenge_limits)

	@contextlib.contextmanager
	def _request(self, headers=None, ip="203.0.113.7"):
		prior = getattr(frappe.local, "request_ip", None)
		frappe.local.request_ip = ip
		try:
			with (
				patch.object(frappe, "request", _FakeRequest(headers or {}), create=True),
				patch("jarvis.api.validate_plugin_request") as verify,
			):
				yield verify
		finally:
			frappe.local.request_ip = prior

	def test_unsigned_call_with_a_held_nonce_passes(self):
		nonce = pairing_proof.issue_nonce()
		with self._request() as verify:
			res = pairing_challenge(nonce)
		self.assertEqual(res, {"ok": True, "held": True})
		verify.assert_not_called()

	def test_stale_plugin_headers_are_ignored(self):
		"""A container still sending a key the site no longer holds must not break the proof."""
		nonce = pairing_proof.issue_nonce()
		headers = {
			"X-Jarvis-Token": "a-key-this-site-never-stored",
			"X-Jarvis-Session": "pairing-challenge",
			"X-Jarvis-Signature": "00",
			"X-Jarvis-Nonce": "n" * 16,
			"X-Jarvis-Timestamp": "1",
		}
		with self._request(headers) as verify:
			res = pairing_challenge(nonce)
		self.assertEqual(res, {"ok": True, "held": True})
		verify.assert_not_called()

	def test_malformed_nonce_is_400_and_touches_no_nonce(self):
		nonce = pairing_proof.issue_nonce()
		for bad in ("", "../x", "a" * 31, "a" * 33, "A" * 32, "g" * 32, None):
			with self._request():
				res = pairing_challenge(bad)
			self.assertFalse(res["ok"], bad)
			self.assertEqual(frappe.local.response.http_status_code, 400, bad)
		self.assertTrue(pairing_proof.consume_nonce(nonce))  # untouched

	def test_held_true_and_pins(self):
		nonce = pairing_proof.issue_nonce()
		with self._request():
			res = pairing_challenge(nonce)
		self.assertEqual(res, {"ok": True, "held": True})
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "paired_site"), frappe.local.site)

	def test_unknown_nonce_is_held_false_and_does_not_pin(self):
		with self._request():
			res = pairing_challenge("ab" * 16)
		self.assertEqual(res, {"ok": True, "held": False})
		self.assertFalse(frappe.db.get_single_value(SETTINGS, "paired_site"))

	def test_second_use_is_held_false(self):
		nonce = pairing_proof.issue_nonce()
		with self._request():
			first = pairing_challenge(nonce)
			second = pairing_challenge(nonce)
		self.assertTrue(first["held"])
		self.assertEqual(second, {"ok": True, "held": False})

	def test_per_ip_rate_limit_is_429_and_never_logs_errors(self):
		limit = pairing_proof.CHALLENGE_PER_IP_LIMIT
		rows_before = frappe.db.count("Error Log")
		with patch.object(frappe, "log_error") as log_error:
			with self._request(ip="198.51.100.9"):
				for _ in range(limit):
					self.assertTrue(pairing_challenge("ab" * 16)["ok"])
				res = pairing_challenge("ab" * 16)
			self.assertFalse(res["ok"])
			self.assertEqual(frappe.local.response.http_status_code, 429)
			# Another address is not affected by this one's bucket.
			with self._request(ip="198.51.100.10"):
				self.assertTrue(pairing_challenge("ab" * 16)["ok"])
		log_error.assert_not_called()
		self.assertEqual(frappe.db.count("Error Log"), rows_before)

	def test_failed_nonces_count_toward_the_limit(self):
		"""Malformed nonces spend the budget too, and a limited call leaves a valid nonce untouched."""
		nonce = pairing_proof.issue_nonce()
		with self._request(ip="198.51.100.20"):
			for _ in range(pairing_proof.CHALLENGE_PER_IP_LIMIT):
				pairing_challenge("not-a-nonce")
			res = pairing_challenge(nonce)
		self.assertFalse(res["ok"])
		self.assertEqual(frappe.local.response.http_status_code, 429)
		self.assertTrue(pairing_proof.consume_nonce(nonce))

	def test_global_bucket_limits_across_addresses(self):
		with patch.object(pairing_proof, "CHALLENGE_GLOBAL_LIMIT", 3):
			for i in range(3):
				with self._request(ip=f"192.0.2.{i + 1}"):
					self.assertTrue(pairing_challenge("ab" * 16)["ok"])
			with self._request(ip="192.0.2.99"):
				res = pairing_challenge("ab" * 16)
		self.assertFalse(res["ok"])
		self.assertEqual(frappe.local.response.http_status_code, 429)

	def test_cache_trouble_fails_open(self):
		with patch("frappe.cache", side_effect=RuntimeError("redis down")):
			self.assertFalse(pairing_proof.challenge_rate_limited())

	def test_rate_limit_counter_rearms_a_missing_ttl(self):
		with self._request(ip="198.51.100.30"):
			pairing_challenge("ab" * 16)
		digest = hashlib.sha256(b"198.51.100.30").hexdigest()[:32]
		key = f"{pairing_proof._RL_PREFIX}{frappe.local.site}:ip:{digest}"
		cache = frappe.cache()
		cache.persist(key)
		self.assertEqual(cache.ttl(key), -1)
		with self._request(ip="198.51.100.30"):
			pairing_challenge("ab" * 16)
		self.assertGreater(cache.ttl(key), 0)


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
			patch("jarvis.onboarding.resync_callback_key_after_pairing"),
		):
			chat_device._establish_pairing()
		self.assertRegex(seen["proof_nonce"], HEX32)

	def test_mechanism_a_returns_bootstrap_codes_untouched(self):
		resp = {"mode": "mechanism_a", "accepted": True, "bootstrap_codes": ["c1", "c2"]}
		set_settings_password(frappe.get_single(SETTINGS), "agent_token", "gw-token", update_modified=False)
		with (
			patch("jarvis.chat.device.admin_client.request_chat_pairing", return_value=resp),
			patch("jarvis.onboarding.resync_callback_key_after_pairing"),
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


class TestResyncAfterPairing(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		_onboard()

	def test_rotated_key_replaces_stored_one_after_pairing(self):
		set_settings_password(frappe.get_single(SETTINGS), "callback_key", CALLBACK)
		rotated = "ee" * 32
		resp = {"mode": "legacy", "device_token": "tok"}
		conn = {"agent_url": "ws://x.invalid", "callback_key": rotated}
		with (
			patch("jarvis.chat.device.admin_client.request_chat_pairing", return_value=resp),
			patch("jarvis.onboarding.admin_client.get_connection", return_value=conn) as get_conn,
		):
			chat_device._establish_pairing()
		get_conn.assert_called_once()
		self.assertRegex(get_conn.call_args.kwargs["proof_nonce"], HEX32)
		stored = frappe.get_single(SETTINGS).get_password("callback_key", raise_exception=False)
		self.assertEqual(stored, rotated)

	def test_failed_resync_never_fails_pairing(self):
		resp = {"mode": "legacy", "device_token": "tok"}
		with (
			patch("jarvis.chat.device.admin_client.request_chat_pairing", return_value=resp),
			patch("jarvis.onboarding.admin_client.get_connection", side_effect=RuntimeError("admin down")),
			patch("jarvis.onboarding.frappe.log_error"),
		):
			creds = chat_device._establish_pairing()
		self.assertEqual(creds.device_token, "tok")

	def test_resync_runs_after_rotate_pairing(self):
		priv_pub = {"device_token": "tok-new"}
		with (
			patch("jarvis.chat.device.admin_client.pair_chat_device", return_value=priv_pub),
			patch("jarvis.chat.device._save_credentials"),
			patch("jarvis.onboarding.resync_callback_key_after_pairing") as resync,
		):
			chat_device._generate_and_pair()
		resync.assert_called_once_with()


class TestSyncConnection(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		_onboard()

	def _sync(self, conn, **kwargs):
		with (
			patch("jarvis.onboarding.require_jarvis_admin"),
			patch("jarvis.onboarding.admin_client.get_connection", return_value=conn) as get_conn,
		):
			onboarding.sync_connection(**kwargs)
		return get_conn

	def test_wizard_poll_sends_no_proof(self):
		"""The poll calls sync_connection with no arguments, every 2 s: it must not spend
		admin's shared proof budget."""
		get_conn = self._sync({"tenant_status": "running"})
		self.assertNotIn("proof_nonce", get_conn.call_args.kwargs)

	def test_sync_button_sends_a_proof(self):
		get_conn = self._sync({"tenant_status": "running"}, prove=1)
		self.assertRegex(get_conn.call_args.kwargs["proof_nonce"], HEX32)

	def test_scheduler_sync_sends_a_proof(self):
		with (
			patch("jarvis.onboarding.require_jarvis_admin"),
			patch("jarvis.onboarding.admin_client.get_connection", return_value={}) as get_conn,
		):
			onboarding.scheduled_sync_connection()
		self.assertRegex(get_conn.call_args.kwargs["proof_nonce"], HEX32)

	def test_daily_hook_runs_the_proving_sync(self):
		from jarvis import hooks

		self.assertIn("jarvis.onboarding.scheduled_sync_connection", hooks.scheduler_events["daily"])
		self.assertNotIn("jarvis.onboarding.sync_connection", hooks.scheduler_events["daily"])
		with self.assertRaises(frappe.PermissionError):
			frappe.is_whitelisted(onboarding.scheduled_sync_connection)

	def test_sync_connection_keeps_token_when_payload_omits_it(self):
		set_settings_password(frappe.get_single(SETTINGS), "agent_token", "keep-me", update_modified=False)
		self._sync({"agent_url": "ws://x.invalid"})
		stored = frappe.get_single(SETTINGS).get_password("agent_token", raise_exception=False)
		self.assertEqual(stored, "keep-me")

	def test_retired_without_key_queues_the_fetch_after_commit(self):
		"""write_connection holds the Jarvis Settings row lock: the admin round trip must
		not run inside it."""
		clear_settings_password(frappe.get_single(SETTINGS), "callback_key")
		with (
			patch("frappe.enqueue") as enq,
			patch("jarvis.onboarding.admin_client.get_connection") as get_conn,
		):
			onboarding.write_connection({"owner_key_retired": True})
		get_conn.assert_not_called()
		enq.assert_called_once()
		self.assertEqual(enq.call_args.args[0], "jarvis.onboarding.fetch_callback_key_if_missing")
		self.assertTrue(enq.call_args.kwargs["enqueue_after_commit"])
		self.assertTrue(enq.call_args.kwargs["deduplicate"])
		self.assertTrue(enq.call_args.kwargs["job_id"])

	def test_retired_with_a_key_queues_nothing(self):
		set_settings_password(frappe.get_single(SETTINGS), "callback_key", CALLBACK)
		with patch("frappe.enqueue") as enq:
			onboarding.write_connection({"owner_key_retired": True})
		enq.assert_not_called()

	def test_the_queued_fetch_runs_a_proof_backed_sync(self):
		clear_settings_password(frappe.get_single(SETTINGS), "callback_key")
		calls = []

		def _conn(**kw):
			calls.append(kw)
			return {"agent_url": "ws://x.invalid", "callback_key": CALLBACK, "owner_key_retired": True}

		with (
			patch("frappe.enqueue") as enq,
			patch("jarvis.onboarding.admin_client.get_connection", side_effect=_conn),
		):
			onboarding.fetch_callback_key_if_missing()
		self.assertEqual(len(calls), 1)
		self.assertRegex(calls[0]["proof_nonce"], HEX32)
		enq.assert_not_called()
		self.assertEqual(
			frappe.get_single(SETTINGS).get_password("callback_key", raise_exception=False), CALLBACK
		)

	def test_the_queued_fetch_does_not_loop_when_no_key_returned(self):
		clear_settings_password(frappe.get_single(SETTINGS), "callback_key")
		with (
			patch("frappe.enqueue") as enq,
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				return_value={"agent_url": "ws://x.invalid", "owner_key_retired": True},
			) as get_conn,
		):
			onboarding.fetch_callback_key_if_missing()
		self.assertEqual(get_conn.call_count, 1)
		enq.assert_not_called()


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
