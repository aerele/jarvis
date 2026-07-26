"""Mobile-app authentication + pairing helpers.

Onboarding (issue #224): the phone scans a QR shown in the Jarvis web app to
learn the *site connection details* (no typing a workspace URL), then signs in
with email + password once. That login establishes a short-lived cookie session
purely to call `get_mobile_token`, which returns a durable credential; from then
on the app authenticates every request (and the realtime socket) with
`Authorization: token <credential>` — stateless, no idle-timeout. The password
is never stored; on logout the phone re-signs-in (the site is remembered).

JF-016: that credential is now a PER-DEVICE token (`jmd:<id>:<secret>`, see
`jarvis/mobile/device_auth.py`), NOT the user's account-wide Frappe
api_key/api_secret. Each pairing mints its own `Jarvis Mobile Device` row with
only a HASH of the secret at rest, so a phone can be revoked on its own —
logout actually revokes something, a lost handset can be killed from any other
device, and none of it disturbs the user's other API integrations. This module
no longer reads or writes `User.api_key`/`User.api_secret` at all; installs that
already hold a global keypair keep working through ordinary Frappe token auth
(nothing here revokes it), and the app moves onto a device token at next login.
"""

import json
import socket
from base64 import b64encode
from io import BytesIO
from urllib.parse import urlparse

import frappe
from frappe.utils import cint

from jarvis.mobile import device_auth

# Bumped if the QR payload shape changes so old app builds can reject it cleanly.
PAIRING_PAYLOAD_VERSION = 1

# Pairing mints a durable credential, so it is throttled per user (a stolen
# cookie session should not be able to farm an unbounded fleet of tokens, and a
# looping client should not fill the device inventory). The window is per user
# rather than per IP so a whole office behind one NAT is not one budget.
PAIRING_LIMIT = 10
PAIRING_WINDOW_SECONDS = 15 * 60


def _require_system_user() -> str:
	"""Return the session user, or raise.

	PART 4 REVISED, TASK 41: a Website/portal user has no legitimate use for the
	Jarvis mobile endpoints (PART 1 TASK 6: portal users are a lower-trust
	population, and Frappe's own generate_keys is System-Manager gated), so a
	non-Desk user cannot self-mint or manage a durable credential here. Every
	endpoint in this module acts on the SESSION user only — never on a `user`
	argument."""
	from jarvis.permissions import is_system_user

	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.AuthenticationError

	if not is_system_user(user):
		frappe.throw(
			"The Jarvis mobile token is available to Jarvis app (Desk) users only.",
			frappe.PermissionError,
		)
	return user


def _pairing_cache_key(user: str):
	return frappe.cache.make_key(f"jarvis_mobile_pairing:{user}")


def _throttle_pairing(user: str) -> None:
	"""Reject a pairing storm. Mirrors frappe.rate_limiter's counter shape but
	keyed by user instead of IP."""
	key = _pairing_cache_key(user)
	if not frappe.cache.get(key):
		frappe.cache.setex(key, PAIRING_WINDOW_SECONDS, 0)
	if cint(frappe.cache.incrby(key, 1)) > PAIRING_LIMIT:
		frappe.throw(
			"Too many pairing attempts. Please try again in a few minutes.",
			frappe.RateLimitExceededError,
		)


@frappe.whitelist(methods=["POST"])
def get_mobile_token(device_label: str | None = None, platform: str | None = None) -> dict:
	"""Mint a NEW per-device credential for the logged-in user.

	Returns the plaintext secret exactly once — it is only ever stored hashed —
	so re-pairing a phone always produces a fresh token and never resurrects an
	old one.

	Back-compat with shipped app builds: the response still carries
	`api_key`/`api_secret`, because those builds join them into
	`Authorization: token <api_key>:<api_secret>`. `api_key` is
	`jmd:<token_id>` and `api_secret` is the device secret, so that join
	reproduces exactly the `jmd:<token_id>:<secret>` device token — an
	un-updated app upgrades onto a revocable credential with no client change.
	Newer builds should read `device_token` + `device` instead.

	Also returns `site` (the real Frappe site name) so the client targets the
	realtime namespace correctly when the workspace is reached via a bare IP.
	"""
	user = _require_system_user()
	_throttle_pairing(user)

	minted = device_auth.mint(user, device_label=device_label, platform=platform)
	return {
		"api_key": f"{device_auth.DEVICE_TOKEN_PREFIX}:{minted['token_id']}",
		"api_secret": minted["secret"],
		"device_token": minted["token"],
		"device": minted["token_id"],
		"device_label": minted["device_label"],
		"site": frappe.local.site,
	}


@frappe.whitelist()
def list_mobile_devices() -> list[dict]:
	"""The session user's paired-device inventory (no secrets, own rows only)."""
	user = _require_system_user()
	return device_auth.list_devices(user)


@frappe.whitelist(methods=["POST"])
def revoke_mobile_device(device: str) -> dict:
	"""Revoke ONE paired device by its token id.

	`device` must belong to the session user; anything else raises
	PermissionError, so this cannot revoke — or probe for — another account's
	devices. Revoking the device that is making this very call is the normal
	logout path: the next request with that token is a 401."""
	user = _require_system_user()
	revoked = device_auth.revoke(user, device)
	return {"revoked": 1 if revoked else 0, "device": device}


@frappe.whitelist(methods=["POST"])
def revoke_all_mobile_devices(keep_current: int = 0) -> dict:
	"""Revoke every paired device of the session user ("sign out everywhere").

	`keep_current=1` spares the device making the call — only meaningful when
	the call itself is authenticated by a device token."""
	user = _require_system_user()
	keep = device_auth.current_device_token_id() if cint(keep_current) else None
	return {"revoked": device_auth.revoke_all(user, except_token_id=keep)}


def _lan_ip() -> str | None:
	"""Best-effort primary LAN IP of this host (opens a UDP socket, sends nothing)."""
	try:
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		try:
			s.connect(("8.8.8.8", 80))
			return s.getsockname()[0]
		finally:
			s.close()
	except Exception:
		return None


def _pairing_payload() -> dict:
	"""Non-secret site connection details the phone needs to reach this site."""
	dev = bool(frappe.conf.get("developer_mode"))
	# In dev/self-host the realtime server listens on its own port; in production
	# it rides the site origin, so no port is advertised.
	port = frappe.conf.get("socketio_port") if dev else None

	site = frappe.utils.get_url()
	# Dev foolproofing: if the web was opened at localhost/.localhost/127.0.0.1,
	# the phone can't reach that host — swap in the laptop's LAN IP so the scanned
	# URL is reachable. Dev-only; production hostnames are never touched.
	if dev:
		parsed = urlparse(site)
		host = parsed.hostname or ""
		if host in ("127.0.0.1", "localhost") or host.endswith(".localhost"):
			lan = _lan_ip()
			if lan:
				suffix = f":{parsed.port}" if parsed.port else ""
				site = f"{parsed.scheme}://{lan}{suffix}"

	return {
		"v": PAIRING_PAYLOAD_VERSION,
		"site": site,
		"name": frappe.local.site,
		"port": port,
		# The web user's login id, so the phone can prefill the email field
		# (they still type their password). Not a secret.
		"email": frappe.session.user,
	}


@frappe.whitelist()
def get_pairing_qr() -> dict:
	"""Return an SVG QR (base64) encoding the site connection details, plus the
	raw payload. Shown in the Jarvis web app for the phone to scan during
	onboarding. Contains NO secret — only where to reach the site."""
	if not frappe.session.user or frappe.session.user == "Guest":
		raise frappe.AuthenticationError

	from pyqrcode import create as qrcreate

	payload = _pairing_payload()
	data = json.dumps(payload, separators=(",", ":"))

	qr = qrcreate(data, error="M")
	stream = BytesIO()
	try:
		qr.svg(stream, scale=5, quiet_zone=2, background="#ffffff", module_color="#111111")
		svg_b64 = b64encode(stream.getvalue()).decode()
	finally:
		stream.close()

	return {"svg": svg_b64, "payload": payload}
