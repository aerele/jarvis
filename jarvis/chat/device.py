"""Chat-device pairing + signing helpers.

Owns the customer-side half of openclaw's device-paired auth:
- Generates an Ed25519 keypair on first chat, persists it in Jarvis Settings.
- Calls admin's pair_chat_device endpoint to register the public side with the
  customer's openclaw container; persists the returned bearer token.
- Builds and signs the v3 device-auth payload openclaw verifies at every WS
  connect (mirrors openclaw src/gateway/device-auth.ts:36).

The keypair never leaves this bench. Admin only ever sees the public key.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

import frappe
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jarvis import admin_client
from jarvis.exceptions import AgentUnreachableError


@dataclass(frozen=True)
class ChatDeviceCredentials:
	device_id: str
	public_key: str  # base64url, no padding
	private_key: Ed25519PrivateKey
	device_token: str  # bearer for auth.deviceToken at connect


def _b64u(raw: bytes) -> str:
	return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
	pad = "=" * (-len(s) % 4)
	return base64.urlsafe_b64decode(s + pad)


def _derive_device_id(public_key_raw: bytes) -> str:
	"""Mirrors openclaw's deriveDeviceIdFromPublicKey (sha256 hex)."""
	return hashlib.sha256(public_key_raw).hexdigest()


def _generate_keypair() -> tuple[Ed25519PrivateKey, bytes, str, str]:
	"""Returns (private_key, public_key_raw, public_key_b64u, device_id)."""
	priv = Ed25519PrivateKey.generate()
	pub_raw = priv.public_key().public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	return priv, pub_raw, _b64u(pub_raw), _derive_device_id(pub_raw)


def _load_private_key(b64u: str) -> Ed25519PrivateKey:
	return Ed25519PrivateKey.from_private_bytes(_b64u_decode(b64u))


def _save_credentials(
	*, settings_doc, private_key_b64u: str, public_key_b64u: str, device_id: str, device_token: str
) -> None:
	# chat_device_private_key / chat_device_token are Password fields; db_set
	# writes exactly what it's given straight into tabSingles with no
	# encryption (only Document.save()'s _save_passwords path encrypts a
	# Password field). set_settings_password encrypts into __Auth first and
	# db_sets only a mask, so the private key + bearer token never sit in
	# plaintext in the Single's row.
	from jarvis._password_utils import set_settings_password

	settings_doc.db_set("chat_device_id", device_id)
	settings_doc.db_set("chat_device_public_key", public_key_b64u)
	set_settings_password(settings_doc, "chat_device_private_key", private_key_b64u)
	set_settings_password(settings_doc, "chat_device_token", device_token)
	frappe.db.commit()


def _read_credentials() -> ChatDeviceCredentials | None:
	"""Load creds from Jarvis Settings, or None if any field is missing.

	Each piece must be present for the pairing to be usable; treating a
	partial state as 'not paired' so the next chat triggers a fresh
	pair_chat_device call and overwrites everything atomically."""
	s = frappe.get_single("Jarvis Settings")
	device_id = (s.chat_device_id or "").strip()
	public_key = (s.chat_device_public_key or "").strip()
	private_key_b64u = (s.get_password("chat_device_private_key", raise_exception=False) or "").strip()
	device_token = (s.get_password("chat_device_token", raise_exception=False) or "").strip()
	if not (device_id and public_key and private_key_b64u and device_token):
		return None
	try:
		priv = _load_private_key(private_key_b64u)
	except (ValueError, TypeError):
		# Corrupted private-key bytes - treat as unpaired so caller re-pairs.
		return None
	return ChatDeviceCredentials(
		device_id=device_id,
		public_key=public_key,
		private_key=priv,
		device_token=device_token,
	)


def ensure_paired() -> ChatDeviceCredentials:
	"""Return current chat device credentials, generating + registering them
	if missing. Idempotent: a fully-populated Settings row is reused as-is.

	Raises AgentUnreachableError if pairing fails (no creds to fall back
	to - the caller has no way to chat without them, so we surface the error
	cleanly instead of half-persisting an unusable state).

	Cold-start concurrency: send_message (web) and the RQ worker (background)
	both call ensure_paired before each turn. On a fresh bench (no
	credentials persisted yet) two concurrent callers both observe
	``_read_credentials() is None`` and both call ``_generate_and_pair``,
	each generating a different Ed25519 keypair and each round-tripping to
	admin.pair_chat_device. Last writer to Jarvis Settings wins; the other
	caller holds in-memory creds that admin's PairedDevice row doesn't know
	about. Cross-repo punch-list "Race: send_message + RQ worker both invoke
	ensure_paired() concurrently" from the 2026-06-16 review.

	Fix: serialize the generate+pair window under a Redis advisory lock.
	Double-checked: re-read inside the lock so the second arrival finds the
	winner's creds and skips the duplicate pair entirely. The lock has a
	60s TTL backstop so a crashed holder can't deadlock cold-start forever;
	on lock unavailability we fall through and pair anyway (better one
	duplicate keypair than a permanently broken chat).
	"""
	existing = _read_credentials()
	if existing is not None:
		return existing
	return _generate_and_pair_under_lock()


def _generate_and_pair_under_lock() -> ChatDeviceCredentials:
	"""Convoy-collapse helper for the cold-start race. Acquires the
	chat_device_initial_pair lock with a bounded wait; inside the lock,
	re-reads to see if the winner already paired; if so, returns their
	creds; if not, runs the actual pair flow."""
	from jarvis._redis_lock import redis_lock

	with redis_lock(
		"chat_device_initial_pair",
		timeout_s=60,
		blocking_timeout_s=30.0,
	) as _acquired:  # deliberately unchecked - see below
		# Re-check inside the lock window. The winner of a contended cold-
		# start has already populated Jarvis Settings; followers read those
		# creds and return without a second pair_chat_device round-trip.
		existing = _read_credentials()
		if existing is not None:
			return existing
		# Lock-unavailable + no creds: a Redis outage during cold-start
		# would otherwise block the bench's chat path indefinitely.
		# Falling through accepts at-worst-one-duplicate-pair; that's a
		# strictly better failure mode than chat-permanently-broken.
		return _generate_and_pair()


def _generate_and_pair() -> ChatDeviceCredentials:
	"""Generate a fresh Ed25519 keypair, register it with admin (which
	relays to the customer's openclaw container as a PairedDevice
	record), and persist the resulting credentials.

	Shared between ensure_paired (cold-start path) and
	rotate_chat_device (operator-triggered rotation path). The two
	previously share-and-fork happened inline; pulling it out lets
	rotation reuse the validation + error surface without
	duplicating the keypair generation logic.
	"""
	priv, _pub_raw, pub_b64u, device_id = _generate_keypair()
	priv_b64u = _b64u(
		priv.private_bytes(
			encoding=serialization.Encoding.Raw,
			format=serialization.PrivateFormat.Raw,
			encryption_algorithm=serialization.NoEncryption(),
		)
	)

	try:
		resp = admin_client.pair_chat_device(public_key=pub_b64u, device_id=device_id)
	except Exception as e:
		raise AgentUnreachableError(f"chat device pairing failed: {e}") from e

	device_token = (resp or {}).get("device_token") or ""
	if not device_token:
		raise AgentUnreachableError("admin pair_chat_device returned no device_token")

	settings = frappe.get_single("Jarvis Settings")
	_save_credentials(
		settings_doc=settings,
		private_key_b64u=priv_b64u,
		public_key_b64u=pub_b64u,
		device_id=device_id,
		device_token=device_token,
	)
	return ChatDeviceCredentials(
		device_id=device_id,
		public_key=pub_b64u,
		private_key=priv,
		device_token=device_token,
	)


@frappe.whitelist(methods=["POST"])
def rotate_chat_device() -> dict:
	"""Force a fresh Ed25519 keypair + re-pair, overwriting any existing
	chat-device credentials in Jarvis Settings.

	System Manager only. Operators run this:
	  - After a suspected leak of the private key (the Password field
	    is encrypted at rest but operators with site DB access could
	    read __Auth).
	  - As routine hygiene on a schedule (annual rotation).
	  - To recover from a corrupted PairedDevice record on the
	    container side - admin's pair leg invalidates the previous
	    PairedDevice for this device_id.

	Atomicity: a new keypair is generated and admin-paired BEFORE the
	old credentials are overwritten. If admin's pair_chat_device
	fails (network, validation, rate-limit), the old credentials stay
	intact so chat keeps working on the previous pairing. The new
	credentials only land in Jarvis Settings on a successful
	round-trip.

	Returns ``{"ok": true, "data": {"device_id": "<new>"}}`` on
	success; raises (translated to the {ok: false, error: {...}}
	envelope at the @frappe.whitelist boundary) on any failure.

	Punch-list item from the 2026-06-16 review: chat-device Ed25519
	private key had no rotation surface.

	Gated on ``require_jarvis_admin`` (PART 4 REVISED, TASK 45): the chat DEVICE
	keypair is tenant infrastructure, distinct from the gateway ``agent_token``
	(which STAYS SM-only via ``api.rotate_agent_token``).
	"""
	from jarvis.permissions import require_jarvis_admin

	require_jarvis_admin()
	creds = _generate_and_pair()
	frappe.logger().info(
		"chat_device.rotate: new device_id=%s",
		creds.device_id,
	)
	return {"ok": True, "data": {"device_id": creds.device_id}}


def clear_credentials() -> None:
	"""Wipe persisted chat-device creds. Called when openclaw rejects an
	existing pairing (token revoked, device not paired, etc.) so the next
	chat attempt regenerates.

	chat_device_private_key / chat_device_token are Password fields:
	db_set(field, "") only blanks the masked placeholder in tabSingles - the
	__Auth row still holds the prior secret, so get_password() would keep
	returning the just-revoked key/token after a "clear" (same footgun
	documented on jarvis/dev.py's _PASSWORD_FIELDS). clear_settings_password
	drops the __Auth row too so the fields actually read as cleared."""
	from jarvis._password_utils import clear_settings_password

	settings = frappe.get_single("Jarvis Settings")
	settings.db_set("chat_device_id", "")
	settings.db_set("chat_device_public_key", "")
	clear_settings_password(settings, "chat_device_private_key")
	clear_settings_password(settings, "chat_device_token")
	frappe.db.commit()


def update_device_token(new_token: str, *, device_id: str) -> bool:
	"""Persist a gateway-REISSUED device token for the current pairing.

	openclaw's hello-ok can carry a rotated ``auth.deviceToken`` (the
	gateway replaces the stored token whenever the existing entry no
	longer lines up with the connect's scopes/issuer). The rotation is
	already durable on the gateway side by the time the client sees
	hello-ok, so a bench that keeps signing with the pair-time token
	fails every FOLLOWING connect with "device token mismatch".

	Guarded on ``device_id``: if another worker re-paired while this
	connect was in flight, Jarvis Settings now holds a DIFFERENT
	device's credentials, and the rotated token of the old device must
	not overwrite them. The check-then-write runs under the same
	``chat_device_pair_repair`` lock the repair path wipes under, so a
	re-pair can't land BETWEEN our device_id read and our token write
	(which would mix the new device's identity with the old device's
	token). Lock unavailable -> skip the persist (return False): the
	stale-pairing self-heal recovers the next connect, which beats
	risking the interleave. Returns True when persisted."""
	from jarvis._password_utils import set_settings_password
	from jarvis._redis_lock import redis_lock

	# A falsy token is never persisted: set_settings_password no-ops on a
	# falsy value, so proceeding would return True without writing anything
	# - violating the "Returns True when persisted" contract above. Reject
	# up front, before taking the repair lock.
	if not new_token:
		return False

	with redis_lock(
		"chat_device_pair_repair",
		timeout_s=30,
		blocking_timeout_s=5.0,
	) as acquired:
		if not acquired:
			return False
		settings = frappe.get_single("Jarvis Settings")
		if (settings.chat_device_id or "").strip() != device_id:
			return False
		# chat_device_token is a Password field - db_set would write the
		# rotated bearer straight into tabSingles as plaintext; encrypt it
		# into __Auth first (see _password_utils module docstring).
		set_settings_password(settings, "chat_device_token", new_token)
		frappe.db.commit()
		return True


def _normalize_metadata(value: str) -> str:
	"""Mirrors openclaw's normalizeDeviceMetadataForAuth: trim + ASCII lowercase
	(only [A-Z] → [a-z]; Unicode left alone, matching the deterministic
	cross-runtime normalization the gateway uses)."""
	trimmed = (value or "").strip()
	return "".join(c.lower() if "A" <= c <= "Z" else c for c in trimmed)


def build_payload_v3(
	*,
	device_id: str,
	client_id: str,
	client_mode: str,
	role: str,
	scopes: list[str],
	signed_at_ms: int,
	device_token: str,
	nonce: str,
	platform: str = "linux",
	device_family: str = "",
) -> str:
	"""Byte-for-byte mirror of openclaw's buildDeviceAuthPayloadV3.

	If openclaw rev-bumps the payload format we discover it as a
	'device-signature' rejection on connect - the only fragile spot in
	the whole transport rewrite, hence the explicit comment + the
	corresponding test in tests/test_chat_device.py."""
	return "|".join(
		[
			"v3",
			device_id,
			client_id,
			client_mode,
			role,
			",".join(scopes),
			str(signed_at_ms),
			device_token or "",
			nonce,
			_normalize_metadata(platform),
			_normalize_metadata(device_family),
		]
	)


def sign_payload(private_key: Ed25519PrivateKey, payload: str) -> str:
	"""Ed25519-sign UTF-8 payload bytes; return base64url-no-padding signature."""
	sig = private_key.sign(payload.encode("utf-8"))
	return _b64u(sig)
