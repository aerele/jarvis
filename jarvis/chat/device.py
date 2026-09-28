"""Chat-device pairing + signing helpers.

Owns the customer-side half of agent's device-paired auth:
- Generates an Ed25519 keypair on first chat, persists it in Jarvis Settings.
- Calls admin's pair_chat_device endpoint to register the public side with the
  customer's agent container; persists the returned bearer token.
- Builds and signs the v3 device-auth payload agent verifies at every WS
  connect (mirrors agent src/gateway/device-auth.ts:36).

The keypair never leaves this bench. Admin only ever sees the public key.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass

import frappe
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jarvis import admin_client
from jarvis.exceptions import AgentUnreachableError

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatDeviceCredentials:
	device_id: str
	public_key: str  # base64url, no padding
	private_key: Ed25519PrivateKey
	device_token: str  # bearer for auth.deviceToken at connect ("" until paired, Mechanism A)
	# Gateway (shared) token from Jarvis Settings ``agent_token``, carried ONLY on
	# a Mechanism-A pairing that has no device token yet. The connect-first
	# bootstrap presents it as ``auth.token`` and signs the v3 payload's token
	# slot with it (spike invariant #2); steady state and legacy leave it "".
	bootstrap_token: str = ""

	@property
	def needs_bootstrap(self) -> bool:
		"""True for a Mechanism-A pairing with no device token yet: the connect
		must present the gateway token (``auth.token``) to create/await a pending
		pairing request and receive the device token on the approved reconnect.

		Steady state (``device_token`` set) and the legacy path (token forged
		synchronously by the CP) are both False -> today's steady-state connect,
		unchanged."""
		return not self.device_token and bool(self.bootstrap_token)


def _b64u(raw: bytes) -> str:
	return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
	pad = "=" * (-len(s) % 4)
	return base64.urlsafe_b64decode(s + pad)


def _derive_device_id(public_key_raw: bytes) -> str:
	"""Mirrors agent's deriveDeviceIdFromPublicKey (sha256 hex)."""
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


def _priv_b64u(priv: Ed25519PrivateKey) -> str:
	"""base64url-no-padding of an Ed25519 private key's raw bytes."""
	return _b64u(
		priv.private_bytes(
			encoding=serialization.Encoding.Raw,
			format=serialization.PrivateFormat.Raw,
			encryption_algorithm=serialization.NoEncryption(),
		)
	)


def _save_keypair(*, device_id: str, public_key_b64u: str, private_key_b64u: str) -> None:
	"""Persist ONLY the stable keypair (device_id + public + private), no token.

	Mechanism A separates keypair persistence from token acquisition: the keypair
	is written once, under the cold-start lock, and stays stable across every
	connect/approve/reconnect so the deviceId never moves (a moving deviceId is a
	pending the fleet-agent can never catch up to -> thrash). The device token is
	written LATER, out of the lock, by ``update_device_token`` once the gateway
	issues it on the approved connect."""
	from jarvis._password_utils import set_settings_password

	settings_doc = frappe.get_single("Jarvis Settings")
	settings_doc.db_set("chat_device_id", device_id)
	settings_doc.db_set("chat_device_public_key", public_key_b64u)
	set_settings_password(settings_doc, "chat_device_private_key", private_key_b64u)
	frappe.db.commit()


def _read_keypair() -> tuple[str, str, Ed25519PrivateKey] | None:
	"""Return the persisted STABLE keypair ``(device_id, public_key_b64u,
	private_key)``, or None when it is absent/corrupt (caller generates a fresh
	one). Deliberately does NOT require a device token: a Mechanism-A bench that
	is paired-pending (keypair present, no token yet) must be able to reuse its
	keypair without regenerating it."""
	s = frappe.get_single("Jarvis Settings")
	device_id = (s.chat_device_id or "").strip()
	public_key = (s.chat_device_public_key or "").strip()
	private_key_b64u = (s.get_password("chat_device_private_key", raise_exception=False) or "").strip()
	if not (device_id and public_key and private_key_b64u):
		return None
	try:
		priv = _load_private_key(private_key_b64u)
	except (ValueError, TypeError):
		return None
	return device_id, public_key, priv


def _read_gateway_token() -> str:
	"""The shared gateway token from Jarvis Settings ``agent_token`` (a Password
	field). This is the ``auth.token`` the Mechanism-A bootstrap connect presents
	(spike invariant #2). Empty string when unset -> the caller fails closed."""
	s = frappe.get_single("Jarvis Settings")
	return (s.get_password("agent_token", raise_exception=False) or "").strip()


def has_paired_token(settings_doc=None) -> bool:
	"""Cheap UX gate for the turn path: True iff a chat device token is already
	persisted (steady state). When False the next connect must (re)pair, so the
	turn renders a "Setting up your assistant…" state before it blocks on the
	connect. Accepts an already-loaded Settings doc to avoid a second fetch."""
	s = settings_doc or frappe.get_single("Jarvis Settings")
	return bool((s.get_password("chat_device_token", raise_exception=False) or "").strip())


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
	"""Return current chat device credentials, establishing them if missing.
	Idempotent: a fully-populated Settings row (keypair + device token) is the
	steady-state hot path and is reused as-is with NO round-trip.

	Raises AgentUnreachableError if pairing fails (no creds to fall back to - the
	caller has no way to chat without them, so we surface the error cleanly
	instead of half-persisting an unusable state).

	When there is no usable device token yet, the actual work is delegated to
	``_establish_pairing`` - which keeps the keypair stable and forks on the CP's
	pairing ``mode`` (legacy synchronous forge vs Mechanism-A connect-first)."""
	existing = _read_credentials()
	if existing is not None:
		return existing
	return _establish_pairing()


def _establish_pairing() -> ChatDeviceCredentials:
	"""Establish a pairing when no usable device token is persisted.

	Two-stage, and the split is load-bearing (plan-check gap #10):

	  1. Ensure a STABLE keypair (generate + persist only if absent). This is the
	     ONLY step serialized under the ``chat_device_initial_pair`` Redis lock:
	     two cold-start callers must not each mint a different keypair (two
	     deviceIds -> the fleet-agent can never converge on one pending -> thrash).
	  2. Fork on the CP's pairing ``mode``, OUTSIDE the lock:
	       - ``legacy``       -> the CP forged a device token synchronously
	                             (today's behaviour): persist + present it.
	       - ``mechanism_a``  -> NO token yet; return bootstrap creds so the
	                             connect-first flow in ``agent_client`` obtains the
	                             token on the gateway-approved reconnect.

	Keeping the CP call + the connect/approve/token cascade OUT of the lock is
	safe precisely BECAUSE the keypair is now stable: concurrent callers act on
	the SAME deviceId (idempotent), rather than the pre-fix race where each minted
	a fresh keypair and flapped the container's pairing state.
	"""
	device_id, pub_b64u, priv = _ensure_keypair_under_lock()

	# A peer may have finished pairing (adopted + persisted a token) while we ran
	# keypair-gen; reuse it rather than re-requesting.
	existing = _read_credentials()
	if existing is not None:
		return existing

	try:
		resp = admin_client.request_chat_pairing(public_key=pub_b64u, device_id=device_id) or {}
	except Exception as e:
		raise AgentUnreachableError(f"chat device pairing request failed: {e}") from e

	mode = (resp.get("mode") or "").strip()
	if mode == "legacy":
		return _pair_legacy(resp, device_id=device_id, pub_b64u=pub_b64u, priv=priv)
	if mode == "mechanism_a":
		return _pair_mechanism_a(device_id=device_id, pub_b64u=pub_b64u, priv=priv)
	# Unknown/blank mode: fail closed (D-d) - never fabricate a credential.
	raise AgentUnreachableError(f"request_chat_pairing returned unknown mode {mode!r}")


def _ensure_keypair_under_lock() -> tuple[str, str, Ed25519PrivateKey]:
	"""Return a STABLE ``(device_id, public_key_b64u, private_key)``, generating +
	persisting a fresh keypair only when none exists yet.

	Keypair generation is the ONLY thing under the ``chat_device_initial_pair``
	lock (plan-check gap #10): the token cascade runs outside it. Double-checked
	inside the lock so a contended cold-start mints exactly one keypair - the
	winner persists, followers read it back. Lock unavailable + no keypair: fall
	through and generate anyway (a Redis outage must not wedge chat forever;
	at-worst-one-duplicate-keypair beats permanently-broken, and the stable-write
	below means a duplicate is quickly reconciled by the next read)."""
	existing = _read_keypair()
	if existing is not None:
		return existing

	from jarvis._redis_lock import redis_lock

	with redis_lock(
		"chat_device_initial_pair",
		timeout_s=60,
		blocking_timeout_s=30.0,
	) as _acquired:  # unchecked: see the fall-through note above
		existing = _read_keypair()
		if existing is not None:
			return existing
		priv, _pub_raw, pub_b64u, device_id = _generate_keypair()
		_save_keypair(device_id=device_id, public_key_b64u=pub_b64u, private_key_b64u=_priv_b64u(priv))
		return device_id, pub_b64u, priv


def _pair_legacy(
	resp: dict, *, device_id: str, pub_b64u: str, priv: Ed25519PrivateKey
) -> ChatDeviceCredentials:
	"""Legacy / 6.8 tenant: the CP forged a device token synchronously (today's
	behaviour, routed via ``request_chat_pairing`` mode=legacy). Persist + present
	it. A missing token fails closed - never half-persist an unusable state."""
	device_token = ((resp or {}).get("device_token") or "").strip()
	if not device_token:
		raise AgentUnreachableError("request_chat_pairing (legacy mode) returned no device_token")
	settings = frappe.get_single("Jarvis Settings")
	_save_credentials(
		settings_doc=settings,
		private_key_b64u=_priv_b64u(priv),
		public_key_b64u=pub_b64u,
		device_id=device_id,
		device_token=device_token,
	)
	_logger.info("chat pairing: legacy mode device_id=%s", device_id)
	return ChatDeviceCredentials(
		device_id=device_id,
		public_key=pub_b64u,
		private_key=priv,
		device_token=device_token,
	)


def _pair_mechanism_a(*, device_id: str, pub_b64u: str, priv: Ed25519PrivateKey) -> ChatDeviceCredentials:
	"""Mechanism-A tenant (9.x): there is NO token yet, and that is a VALID state.
	The CP has told the fleet-agent to poll + approve this deviceId; the gateway
	issues the device token on the approved connect. Return bootstrap creds
	carrying the gateway ``agent_token`` so ``agent_client`` can connect-first
	(present ``auth.token``), drive NOT_PAIRED -> approved, and adopt the reissued
	token. The stable keypair is already persisted, so re-requesting per turn
	never regenerates the deviceId (no thrash).

	Fail closed (D-d) if the gateway token is unset: without it the bootstrap
	connect can neither be presented nor signed."""
	bootstrap_token = _read_gateway_token()
	if not bootstrap_token:
		raise AgentUnreachableError("mechanism_a pairing needs the gateway agent_token, but none is set")
	_logger.info("chat pairing: mechanism_a connect-first device_id=%s", device_id)
	return ChatDeviceCredentials(
		device_id=device_id,
		public_key=pub_b64u,
		private_key=priv,
		device_token="",
		bootstrap_token=bootstrap_token,
	)


def _generate_and_pair() -> ChatDeviceCredentials:
	"""Generate a fresh Ed25519 keypair, register it with admin (which
	relays to the customer's agent container as a PairedDevice
	record), and persist the resulting credentials.

	Used ONLY by ``rotate_chat_device`` now (the operator-triggered
	force-repair): it still routes through the legacy synchronous forge
	(``admin_client.pair_chat_device``), which the CP keeps intact per the
	additive-endpoint decision. The cold-start / first-turn pairing path moved to
	``_establish_pairing`` (mode fork). Operator rotation under Mechanism A -
	regenerate keypair, then connect-first to re-pair - is a named follow-on
	(same family as the deferred ``unpair_devices`` fix); on a Mechanism-A tenant
	the CP's legacy forge is the broken path, so this stays legacy-only until then.
	"""
	priv, _pub_raw, pub_b64u, device_id = _generate_keypair()
	priv_b64u = _priv_b64u(priv)

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
	"""Wipe persisted chat-device creds. Called when agent rejects an
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


def session_device_is_stale(row_device_id: str, current_device_id: str) -> bool:
	"""True iff a chat session's snapshotted ``chat_device_id`` no longer
	matches the bench's CURRENT pairing.

	Shared by two call sites so they can never drift:
	  - ``jarvis.api.call_tool`` (the security guard): rejects a plugin
	    call over a stale session with 401, bounding a leaked-session-key
	    replay to the window before the next re-pair. That guard must
	    NEVER auto-heal itself here - doing so would let a leaked session
	    key survive a re-pair, defeating the whole point of the check.
	  - ``jarvis.chat.turn_handler.handle_chat_send`` (the recovery): at
	    the START of a new turn, on the browser-authenticated path (not
	    the plugin's replayable session key), a stale binding means the
	    conversation's existing agent session was minted under a device
	    pairing that no longer exists. Treating it like "no session yet"
	    mints a FRESH one under the current pairing so the conversation
	    keeps working without a customer support ticket. The old,
	    stale-bound session row is left alone and stays dead - the
	    security property above is untouched.

	Both blank values pass through as "not stale" (pre-migration row /
	pre-pairing bench), matching the historical backwards-compat rule in
	``call_tool``.
	"""
	row = (row_device_id or "").strip()
	current = (current_device_id or "").strip()
	if not row or not current:
		return False
	return row != current


def update_device_token(new_token: str, *, device_id: str) -> bool:
	"""Persist a gateway-REISSUED device token for the current pairing.

	agent's hello-ok can carry a rotated ``auth.deviceToken`` (the
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
	"""Mirrors agent's normalizeDeviceMetadataForAuth: trim + ASCII lowercase
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
	"""Byte-for-byte mirror of agent's buildDeviceAuthPayloadV3.

	If agent rev-bumps the payload format we discover it as a
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
