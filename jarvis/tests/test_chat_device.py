"""Tests for jarvis.chat.device - chat keypair + pairing + v3 signing.

Two surface areas to cover:
1. ensure_paired: generates a keypair if missing, calls admin to register the
   public side, persists everything atomically; reuses existing creds when
   present; surfaces admin failures as AgentUnreachableError without
   half-persisting a broken state.
2. build_payload_v3 / sign_payload: the byte-exact mirror of openclaw's
   device-auth.ts:36 - if openclaw rev-bumps the format, this is the test
   that catches it before chat goes live.
"""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import patch

import frappe
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import device as chat_device
from jarvis.exceptions import AgentUnreachableError


def _b64u(raw: bytes) -> str:
	return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


_SNAPSHOT_PASSWORD_FIELDS = (
	"chat_device_private_key",
	"chat_device_token",
)
_SNAPSHOT_PLAIN_FIELDS = (
	"chat_device_id",
	"chat_device_public_key",
)


class _SettingsSnapshotMixin:
	"""Save/restore the chat_device_* fields so the test suite leaves no
	residue on whichever site bench picked. Mirrors test_settings.py."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		s = frappe.get_single("Jarvis Settings")
		snap = {f: s.get(f) for f in _SNAPSHOT_PLAIN_FIELDS}
		for f in _SNAPSHOT_PASSWORD_FIELDS:
			snap[f] = s.get_password(f, raise_exception=False) or ""
		cls._chat_device_snapshot = snap

	@classmethod
	def tearDownClass(cls):
		try:
			s = frappe.get_single("Jarvis Settings")
			for f, v in cls._chat_device_snapshot.items():
				s.db_set(f, v)
			frappe.db.commit()
		finally:
			super().tearDownClass()


def _clear_settings():
	"""Wipe chat_device_* between tests so each one starts unpaired.

	The Password fields need their __Auth rows dropped too: the production
	write path stores the secret in __Auth (masked column), so a column-only
	db_set("") would let get_password resurrect a previous test's secret."""
	from frappe.utils.password import remove_encrypted_password

	s = frappe.get_single("Jarvis Settings")
	for f in (*_SNAPSHOT_PLAIN_FIELDS, *_SNAPSHOT_PASSWORD_FIELDS):
		s.db_set(f, "")
	for f in _SNAPSHOT_PASSWORD_FIELDS:
		remove_encrypted_password("Jarvis Settings", "Jarvis Settings", f)
	frappe.db.commit()


class TestEnsurePaired(_SettingsSnapshotMixin, FrappeTestCase):
	def setUp(self):
		_clear_settings()

	def test_generates_keypair_calls_admin_and_persists(self):
		captured = {}

		def _fake_pair(public_key, device_id):
			captured["public_key"] = public_key
			captured["device_id"] = device_id
			return {"device_token": "tok-from-admin"}

		with patch("jarvis.chat.device.admin_client.pair_chat_device", side_effect=_fake_pair):
			creds = chat_device.ensure_paired()

		# Returned object is internally consistent.
		self.assertEqual(creds.device_token, "tok-from-admin")
		self.assertEqual(creds.public_key, captured["public_key"])
		self.assertEqual(creds.device_id, captured["device_id"])
		# deviceId must match sha256(rawPublicKey) - same invariant openclaw enforces.
		raw = base64.urlsafe_b64decode(captured["public_key"] + "=" * (-len(captured["public_key"]) % 4))
		self.assertEqual(creds.device_id, hashlib.sha256(raw).hexdigest())
		# Persisted in Settings.
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.chat_device_id, creds.device_id)
		self.assertEqual(s.chat_device_public_key, creds.public_key)
		self.assertEqual(s.get_password("chat_device_token"), "tok-from-admin")
		self.assertTrue(s.get_password("chat_device_private_key"))

	def test_reuses_existing_creds_without_admin_call(self):
		# Seed Settings with a valid keypair + token.
		priv = Ed25519PrivateKey.generate()
		pub_raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
		priv_raw = priv.private_bytes(
			serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
		)
		s = frappe.get_single("Jarvis Settings")
		s.db_set("chat_device_id", hashlib.sha256(pub_raw).hexdigest())
		s.db_set("chat_device_public_key", _b64u(pub_raw))
		s.db_set("chat_device_private_key", _b64u(priv_raw))
		s.db_set("chat_device_token", "tok-existing")
		frappe.db.commit()

		with patch("jarvis.chat.device.admin_client.pair_chat_device") as mock_pair:
			creds = chat_device.ensure_paired()
		self.assertFalse(mock_pair.called)
		self.assertEqual(creds.device_token, "tok-existing")
		self.assertEqual(creds.device_id, hashlib.sha256(pub_raw).hexdigest())

	def test_admin_failure_raises_and_does_not_persist(self):
		with patch("jarvis.chat.device.admin_client.pair_chat_device", side_effect=RuntimeError("boom")):
			with self.assertRaises(AgentUnreachableError):
				chat_device.ensure_paired()
		# Nothing persisted on failure.
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.chat_device_id)
		self.assertFalse(s.get_password("chat_device_private_key", raise_exception=False))

	def test_empty_device_token_raises_unreachable(self):
		with patch("jarvis.chat.device.admin_client.pair_chat_device", return_value={"device_token": ""}):
			with self.assertRaises(AgentUnreachableError):
				chat_device.ensure_paired()

	def test_concurrent_callers_share_one_admin_pair_call(self):
		"""Cold-start convoy collapse. Cross-repo punch-list "Race:
		send_message + RQ worker both invoke ensure_paired() concurrently".

		Before the fix: a fresh bench with no chat_device_* fields and
		two concurrent callers (web request + RQ worker) BOTH observed
		``_read_credentials() is None`` and BOTH called
		``_generate_and_pair`` - last writer to Jarvis Settings wins;
		the other caller holds in-memory creds that don't match what
		admin saw.

		After the fix: a Redis lock collapses the convoy. The first
		caller pairs; the second waits on the lock, re-reads inside it,
		and returns the winner's creds without a second admin call.

		Real concurrency on a single-threaded test runner is tricky to
		stage. We simulate the convoy by patching the lock context
		manager so the "second" caller pre-populates Settings before
		entering the lock body - the re-check inside the lock must
		short-circuit on those existing creds.
		"""
		# First caller paints credentials into Settings as if it had won
		# the lock race.
		first_priv = Ed25519PrivateKey.generate()
		first_pub_raw = first_priv.public_key().public_bytes(
			serialization.Encoding.Raw,
			serialization.PublicFormat.Raw,
		)
		first_device_id = hashlib.sha256(first_pub_raw).hexdigest()

		def _pre_populate_settings_inside_lock():
			s = frappe.get_single("Jarvis Settings")
			s.db_set("chat_device_id", first_device_id)
			s.db_set("chat_device_public_key", _b64u(first_pub_raw))
			s.db_set(
				"chat_device_private_key",
				_b64u(
					first_priv.private_bytes(
						serialization.Encoding.Raw,
						serialization.PrivateFormat.Raw,
						serialization.NoEncryption(),
					)
				),
			)
			s.db_set("chat_device_token", "tok-winner")
			frappe.db.commit()

		class _FakeLockCtx:
			def __enter__(_self):
				_pre_populate_settings_inside_lock()
				return True

			def __exit__(_self, *a):
				return False

		mock_pair = patch("jarvis.chat.device.admin_client.pair_chat_device").start()
		mock_lock = patch("jarvis._redis_lock.redis_lock", return_value=_FakeLockCtx()).start()
		try:
			creds = chat_device.ensure_paired()
		finally:
			patch.stopall()

		# Second caller picked up the winner's creds; no admin pair call
		# was made.
		self.assertFalse(mock_pair.called)
		self.assertEqual(creds.device_token, "tok-winner")
		self.assertEqual(creds.device_id, first_device_id)
		self.assertTrue(mock_lock.called)

	def test_partial_state_triggers_repair(self):
		"""If only some fields are set, treat the whole pairing as missing
		so the next call re-pairs atomically - protects against half-failed
		writes from a previous deploy/migration."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("chat_device_id", "abc")
		s.db_set("chat_device_public_key", "")  # incomplete
		s.db_set("chat_device_private_key", "")
		s.db_set("chat_device_token", "")
		frappe.db.commit()

		with patch(
			"jarvis.chat.device.admin_client.pair_chat_device", return_value={"device_token": "tok-repaired"}
		):
			creds = chat_device.ensure_paired()
		self.assertEqual(creds.device_token, "tok-repaired")
		self.assertNotEqual(creds.device_id, "abc")  # fresh keypair was generated


class TestRotateChatDevice(_SettingsSnapshotMixin, FrappeTestCase):
	def setUp(self):
		_clear_settings()

	def test_rotate_generates_fresh_keypair_even_when_pairing_exists(self):
		# Seed Settings with valid pre-rotation creds.
		priv = Ed25519PrivateKey.generate()
		pub_raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
		old_device_id = hashlib.sha256(pub_raw).hexdigest()
		s = frappe.get_single("Jarvis Settings")
		s.db_set("chat_device_id", old_device_id)
		s.db_set("chat_device_public_key", _b64u(pub_raw))
		s.db_set(
			"chat_device_private_key",
			_b64u(
				priv.private_bytes(
					serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
				)
			),
		)
		s.db_set("chat_device_token", "tok-old")
		frappe.db.commit()

		with patch(
			"jarvis.chat.device.admin_client.pair_chat_device", return_value={"device_token": "tok-new"}
		):
			out = chat_device.rotate_chat_device()

		# Wire-shape check + new device_id is fresh + token rotated.
		self.assertTrue(out["ok"])
		self.assertNotEqual(out["data"]["device_id"], old_device_id)
		s2 = frappe.get_single("Jarvis Settings")
		self.assertEqual(s2.chat_device_id, out["data"]["device_id"])
		self.assertEqual(s2.get_password("chat_device_token"), "tok-new")

	def test_rotate_preserves_old_creds_on_admin_failure(self):
		# Seed old creds.
		priv = Ed25519PrivateKey.generate()
		pub_raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
		old_device_id = hashlib.sha256(pub_raw).hexdigest()
		s = frappe.get_single("Jarvis Settings")
		s.db_set("chat_device_id", old_device_id)
		s.db_set("chat_device_public_key", _b64u(pub_raw))
		s.db_set("chat_device_token", "tok-old")
		frappe.db.commit()

		with patch(
			"jarvis.chat.device.admin_client.pair_chat_device", side_effect=RuntimeError("admin down")
		):
			with self.assertRaises(AgentUnreachableError):
				chat_device.rotate_chat_device()

		# Old creds intact.
		s2 = frappe.get_single("Jarvis Settings")
		self.assertEqual(s2.chat_device_id, old_device_id)
		self.assertEqual(s2.get_password("chat_device_token"), "tok-old")


class TestUpdateDeviceToken(_SettingsSnapshotMixin, FrappeTestCase):
	"""update_device_token persists a gateway-REISSUED device token, but
	only for the pairing Settings still holds - a concurrent re-pair by
	another worker must never be clobbered by the old device's rotation."""

	def setUp(self):
		_clear_settings()

	def test_persists_for_current_pairing(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("chat_device_id", "dev-1")
		s.db_set("chat_device_token", "tok-old")
		frappe.db.commit()

		self.assertTrue(
			chat_device.update_device_token("tok-rotated", device_id="dev-1"),
		)
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			s.get_password("chat_device_token", raise_exception=False),
			"tok-rotated",
		)

	def test_lock_unavailable_skips_persist(self):
		"""Redis lock unavailable -> return False without touching Settings;
		the check-then-write must never run unserialized against a
		concurrent re-pair (it could mix the new device's identity with
		the old device's rotated token)."""
		from contextlib import contextmanager

		s = frappe.get_single("Jarvis Settings")
		s.db_set("chat_device_id", "dev-1")
		s.db_set("chat_device_token", "tok-old")
		frappe.db.commit()

		@contextmanager
		def _unavailable_lock(*a, **kw):
			yield False

		with patch("jarvis._redis_lock.redis_lock", _unavailable_lock):
			self.assertFalse(
				chat_device.update_device_token("tok-rotated", device_id="dev-1"),
			)
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			s.get_password("chat_device_token", raise_exception=False),
			"tok-old",
		)

	def test_refuses_when_pairing_moved_on(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("chat_device_id", "dev-2-fresh")
		s.db_set("chat_device_token", "tok-fresh")
		frappe.db.commit()

		self.assertFalse(
			chat_device.update_device_token("tok-rotated", device_id="dev-1-old"),
		)
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			s.get_password("chat_device_token", raise_exception=False),
			"tok-fresh",
		)

	def test_falsy_token_returns_false_without_persist(self):
		"""A falsy reissued token must return False and never touch
		Settings: set_settings_password no-ops on falsy values, so
		proceeding would have claimed True while persisting nothing -
		violating the 'Returns True when persisted' contract."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("chat_device_id", "dev-1")
		s.db_set("chat_device_token", "tok-old")
		frappe.db.commit()

		with patch("jarvis._password_utils.set_settings_password") as mock_set:
			self.assertFalse(
				chat_device.update_device_token("", device_id="dev-1"),
			)
		mock_set.assert_not_called()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			s.get_password("chat_device_token", raise_exception=False),
			"tok-old",
			"stored token must be untouched by a falsy reissue",
		)


class TestSigning(FrappeTestCase):
	def test_build_payload_v3_format(self):
		out = chat_device.build_payload_v3(
			device_id="DID",
			client_id="gateway-client",
			client_mode="backend",
			role="operator",
			scopes=["operator.write", "operator.admin"],
			signed_at_ms=12345,
			device_token="TOK",
			nonce="NONCE",
			platform="Linux",
			device_family="",
		)
		# Mirror of openclaw's buildDeviceAuthPayloadV3 (device-auth.ts:36).
		# Platform is normalized to ASCII lowercase ("linux"); device_family
		# stays empty.
		expected = (
			"v3|DID|gateway-client|backend|operator|operator.write,operator.admin|12345|TOK|NONCE|linux|"
		)
		self.assertEqual(out, expected)

	def test_sign_payload_verifies_with_public_key(self):
		"""Round-trip: sign with private, verify with the matching public key
		using the same Ed25519 raw scheme openclaw uses."""
		priv = Ed25519PrivateKey.generate()
		pub_raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
		payload = "v3|x|y|z|operator||0||n||"
		sig_b64u = chat_device.sign_payload(priv, payload)
		# Decode and verify.
		sig = base64.urlsafe_b64decode(sig_b64u + "=" * (-len(sig_b64u) % 4))
		pub = Ed25519PublicKey.from_public_bytes(pub_raw)
		pub.verify(sig, payload.encode("utf-8"))  # raises if invalid

	def test_metadata_normalization_lowercases_ascii_only(self):
		out = chat_device.build_payload_v3(
			device_id="x",
			client_id="c",
			client_mode="m",
			role="r",
			scopes=["s"],
			signed_at_ms=0,
			device_token="",
			nonce="n",
			platform="DarwinARM64",
			device_family="iPhone15",
		)
		# Trailing fields after the nonce: |<platform>|<device_family>
		self.assertTrue(out.endswith("|darwinarm64|iphone15"))
