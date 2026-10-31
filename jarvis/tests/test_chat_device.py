"""Tests for jarvis.chat.device - chat keypair + pairing + v3 signing.

Two surface areas to cover:
1. ensure_paired: generates a keypair if missing, calls admin to register the
   public side, persists everything atomically; reuses existing creds when
   present; surfaces admin failures as AgentUnreachableError without
   half-persisting a broken state.
2. build_payload_v3 / sign_payload: the byte-exact mirror of agent's
   device-auth.ts:36 - if agent rev-bumps the format, this is the test
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
		# Pairing fetches a missing callback key through a proof-backed sync: never
		# let these tests reach admin with real settings.
		key_fetch = patch("jarvis.onboarding.fetch_callback_key_if_missing")
		key_fetch.start()
		cls.addClassCleanup(key_fetch.stop)
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
	"""ensure_paired() forks on the CP's pairing ``mode`` (agent 9.3
	device-pairing fix). The cold-start path now calls the additive
	``request_chat_pairing`` (not the legacy ``pair_chat_device``): LEGACY mode
	reproduces today's synchronous-token behaviour, MECHANISM_A returns
	token-absent bootstrap creds (see TestEnsurePairedMechanismA). A fully
	populated (keypair + token) Settings row is still the steady-state hot path,
	reused with no CP call."""

	def setUp(self):
		_clear_settings()

	def test_legacy_mode_generates_keypair_and_persists(self):
		captured = {}

		def _fake_request(public_key, device_id, **kw):
			captured["public_key"] = public_key
			captured["device_id"] = device_id
			return {"mode": "legacy", "device_token": "tok-from-cp"}

		with patch("jarvis.chat.device.admin_client.request_chat_pairing", side_effect=_fake_request):
			creds = chat_device.ensure_paired()

		# Returned object is internally consistent + a steady-state (not bootstrap) pairing.
		self.assertEqual(creds.device_token, "tok-from-cp")
		self.assertFalse(creds.needs_bootstrap)
		self.assertEqual(creds.public_key, captured["public_key"])
		self.assertEqual(creds.device_id, captured["device_id"])
		# deviceId must match sha256(rawPublicKey) - same invariant agent enforces.
		raw = base64.urlsafe_b64decode(captured["public_key"] + "=" * (-len(captured["public_key"]) % 4))
		self.assertEqual(creds.device_id, hashlib.sha256(raw).hexdigest())
		# Persisted in Settings.
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.chat_device_id, creds.device_id)
		self.assertEqual(s.chat_device_public_key, creds.public_key)
		self.assertEqual(s.get_password("chat_device_token"), "tok-from-cp")
		self.assertTrue(s.get_password("chat_device_private_key"))

	def test_reuses_existing_creds_without_cp_call(self):
		# Seed Settings with a valid keypair + token (steady state).
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

		with patch("jarvis.chat.device.admin_client.request_chat_pairing") as mock_req:
			creds = chat_device.ensure_paired()
		self.assertFalse(mock_req.called)
		self.assertEqual(creds.device_token, "tok-existing")
		self.assertFalse(creds.needs_bootstrap)
		self.assertEqual(creds.device_id, hashlib.sha256(pub_raw).hexdigest())

	def test_cp_failure_raises_and_persists_no_token(self):
		"""A CP failure surfaces as AgentUnreachableError and NEVER persists a
		device TOKEN. The stable keypair MAY be persisted (that is the deliberate
		anti-thrash design - token-absent is a valid state), so the assertion is
		on the usable credential (the token), not on the keypair."""
		with patch("jarvis.chat.device.admin_client.request_chat_pairing", side_effect=RuntimeError("boom")):
			with self.assertRaises(AgentUnreachableError):
				chat_device.ensure_paired()
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.get_password("chat_device_token", raise_exception=False) or "")

	def test_legacy_empty_token_raises(self):
		with patch(
			"jarvis.chat.device.admin_client.request_chat_pairing",
			return_value={"mode": "legacy", "device_token": ""},
		):
			with self.assertRaises(AgentUnreachableError):
				chat_device.ensure_paired()
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.get_password("chat_device_token", raise_exception=False) or "")

	def test_unknown_mode_fails_closed(self):
		"""An unrecognised/blank mode must fail closed - never fabricate a
		credential (D-d)."""
		with patch(
			"jarvis.chat.device.admin_client.request_chat_pairing",
			return_value={"mode": "something-new"},
		):
			with self.assertRaises(AgentUnreachableError):
				chat_device.ensure_paired()
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.get_password("chat_device_token", raise_exception=False) or "")

	def test_concurrent_callers_share_one_pairing(self):
		"""Cold-start convoy collapse (now on the keypair-gen lock). A follower
		that enters the lock after the winner populated Settings reads the
		winner's creds and returns without any CP round-trip.

		Real concurrency on a single-threaded test runner is tricky to stage; we
		simulate the convoy by patching the lock context manager so the "second"
		caller pre-populates Settings before entering the lock body - the re-check
		inside the lock must short-circuit on those existing creds."""
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

		mock_req = patch("jarvis.chat.device.admin_client.request_chat_pairing").start()
		mock_lock = patch("jarvis._redis_lock.redis_lock", return_value=_FakeLockCtx()).start()
		try:
			creds = chat_device.ensure_paired()
		finally:
			patch.stopall()

		# Follower picked up the winner's creds; no CP request was made.
		self.assertFalse(mock_req.called)
		self.assertEqual(creds.device_token, "tok-winner")
		self.assertEqual(creds.device_id, first_device_id)
		self.assertTrue(mock_lock.called)

	def test_partial_state_regenerates_keypair(self):
		"""Only device_id set (a half-failed prior write): the keypair itself is
		absent, so a fresh one is generated and a legacy token acquired."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("chat_device_id", "abc")
		s.db_set("chat_device_public_key", "")  # incomplete
		s.db_set("chat_device_private_key", "")
		s.db_set("chat_device_token", "")
		frappe.db.commit()

		with patch(
			"jarvis.chat.device.admin_client.request_chat_pairing",
			return_value={"mode": "legacy", "device_token": "tok-repaired"},
		):
			creds = chat_device.ensure_paired()
		self.assertEqual(creds.device_token, "tok-repaired")
		self.assertNotEqual(creds.device_id, "abc")  # fresh keypair was generated


class TestEnsurePairedMechanismA(_SettingsSnapshotMixin, FrappeTestCase):
	"""Mechanism-A (agent 9.x) pairing: ensure_paired returns token-absent
	bootstrap creds, the keypair stays STABLE across pending turns (no deviceId
	thrash - plan-check gap #3), and it fails closed without a gateway token."""

	def setUp(self):
		_clear_settings()
		# The gateway token the bootstrap connect presents (auth.token). Not
		# covered by _SettingsSnapshotMixin, so snapshot + restore it ourselves.
		s = frappe.get_single("Jarvis Settings")
		self._agent_token_snap = s.get_password("agent_token", raise_exception=False) or ""
		self._set_agent_token("gw-secret")

	def tearDown(self):
		self._set_agent_token(self._agent_token_snap)

	def _set_agent_token(self, value):
		from frappe.utils.password import remove_encrypted_password

		from jarvis._password_utils import set_settings_password

		s = frappe.get_single("Jarvis Settings")
		if value:
			set_settings_password(s, "agent_token", value)
		else:
			s.db_set("agent_token", "")
			remove_encrypted_password("Jarvis Settings", "Jarvis Settings", "agent_token")
		frappe.db.commit()

	def test_returns_bootstrap_creds_with_no_token(self):
		with patch(
			"jarvis.chat.device.admin_client.request_chat_pairing",
			return_value={"mode": "mechanism_a", "accepted": True},
		):
			creds = chat_device.ensure_paired()
		# Token-absent is a VALID state; the gateway token rides bootstrap_token.
		self.assertEqual(creds.device_token, "")
		self.assertEqual(creds.bootstrap_token, "gw-secret")
		self.assertTrue(creds.needs_bootstrap)
		raw = base64.urlsafe_b64decode(creds.public_key + "=" * (-len(creds.public_key) % 4))
		self.assertEqual(creds.device_id, hashlib.sha256(raw).hexdigest())
		# Keypair persisted; token NOT persisted.
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.chat_device_id, creds.device_id)
		self.assertTrue(s.get_password("chat_device_private_key"))
		self.assertFalse(s.get_password("chat_device_token", raise_exception=False) or "")

	def test_without_gateway_token_fails_closed(self):
		self._set_agent_token("")
		with patch(
			"jarvis.chat.device.admin_client.request_chat_pairing",
			return_value={"mode": "mechanism_a", "accepted": True},
		):
			with self.assertRaises(AgentUnreachableError):
				chat_device.ensure_paired()
		# Fail-closed: no device token fabricated.
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.get_password("chat_device_token", raise_exception=False) or "")

	def test_keypair_is_stable_across_pending_turns(self):
		"""THE anti-thrash guarantee (plan-check gap #3): while pairing is pending
		(no token yet), every ensure_paired returns the SAME deviceId. A keypair
		regenerated each turn would be a moving target the fleet-agent can never
		approve."""
		with patch(
			"jarvis.chat.device.admin_client.request_chat_pairing",
			return_value={"mode": "mechanism_a", "accepted": True},
		):
			first = chat_device.ensure_paired()
			second = chat_device.ensure_paired()
		self.assertEqual(first.device_id, second.device_id)
		self.assertEqual(first.public_key, second.public_key)

	def test_token_adoption_switches_to_steady_state_same_keypair(self):
		"""Once the gateway-issued token is persisted (adopted), ensure_paired
		returns STEADY-state creds (needs_bootstrap False) on the SAME keypair -
		the pending->paired transition never changes the deviceId."""
		with patch(
			"jarvis.chat.device.admin_client.request_chat_pairing",
			return_value={"mode": "mechanism_a", "accepted": True},
		):
			pending = chat_device.ensure_paired()
		# Gateway approves + issues a token; the bench adopts it.
		self.assertTrue(chat_device.update_device_token("tok-issued", device_id=pending.device_id))
		with patch("jarvis.chat.device.admin_client.request_chat_pairing") as mock_req:
			steady = chat_device.ensure_paired()
		self.assertFalse(mock_req.called)
		self.assertFalse(steady.needs_bootstrap)
		self.assertEqual(steady.device_token, "tok-issued")
		self.assertEqual(steady.device_id, pending.device_id)


class TestNeedsBootstrap(FrappeTestCase):
	"""The connect-mode discriminator: needs_bootstrap is True ONLY for a
	Mechanism-A pairing with no device token yet (token absent + gateway token
	present)."""

	def _creds(self, *, device_token, bootstrap_token):
		priv = Ed25519PrivateKey.generate()
		return chat_device.ChatDeviceCredentials(
			device_id="d",
			public_key="p",
			private_key=priv,
			device_token=device_token,
			bootstrap_token=bootstrap_token,
		)

	def test_true_only_when_token_absent_and_bootstrap_present(self):
		self.assertTrue(self._creds(device_token="", bootstrap_token="gw").needs_bootstrap)

	def test_steady_state_is_not_bootstrap(self):
		self.assertFalse(self._creds(device_token="tok", bootstrap_token="gw").needs_bootstrap)
		self.assertFalse(self._creds(device_token="tok", bootstrap_token="").needs_bootstrap)

	def test_no_gateway_token_is_not_bootstrap(self):
		self.assertFalse(self._creds(device_token="", bootstrap_token="").needs_bootstrap)


class TestHasPairedToken(_SettingsSnapshotMixin, FrappeTestCase):
	"""The cheap UX gate the turn path reads to decide whether to render the
	'Setting up your assistant…' state before it connects."""

	def setUp(self):
		_clear_settings()

	def test_true_when_token_present(self):
		from jarvis._password_utils import set_settings_password

		s = frappe.get_single("Jarvis Settings")
		set_settings_password(s, "chat_device_token", "tok")
		frappe.db.commit()
		self.assertTrue(chat_device.has_paired_token())

	def test_false_when_token_absent(self):
		self.assertFalse(chat_device.has_paired_token())


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
		# Mirror of agent's buildDeviceAuthPayloadV3 (device-auth.ts:36).
		# Platform is normalized to ASCII lowercase ("linux"); device_family
		# stays empty.
		expected = (
			"v3|DID|gateway-client|backend|operator|operator.write,operator.admin|12345|TOK|NONCE|linux|"
		)
		self.assertEqual(out, expected)

	def test_sign_payload_verifies_with_public_key(self):
		"""Round-trip: sign with private, verify with the matching public key
		using the same Ed25519 raw scheme agent uses."""
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


class TestSessionDeviceIsStale(FrappeTestCase):
	"""jarvis #712: the single source of truth both jarvis.api.call_tool
	(the security guard, must never auto-heal) and turn_handler.
	handle_chat_send (the recovery, safe to auto-heal) read to decide
	whether a session's snapshotted device binding is stale."""

	def test_mismatched_ids_are_stale(self):
		self.assertTrue(chat_device.session_device_is_stale("old-device", "new-device"))

	def test_matching_ids_are_not_stale(self):
		self.assertFalse(chat_device.session_device_is_stale("same-device", "same-device"))

	def test_blank_row_device_is_backwards_compat_not_stale(self):
		"""Pre-migration row (no chat_device_id column populated yet) must
		keep dispatching - see the C2 backwards-compat note in api.py."""
		self.assertFalse(chat_device.session_device_is_stale("", "current-device"))

	def test_blank_current_device_is_not_stale(self):
		"""A bench that has never paired has no current device to compare
		against - never reject on that alone."""
		self.assertFalse(chat_device.session_device_is_stale("old-device", ""))

	def test_both_blank_is_not_stale(self):
		self.assertFalse(chat_device.session_device_is_stale("", ""))

	def test_whitespace_is_stripped_before_comparing(self):
		self.assertFalse(chat_device.session_device_is_stale("  dev-1  ", "dev-1"))
