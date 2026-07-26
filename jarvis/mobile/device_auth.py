"""Per-device, revocable mobile credentials (JF-016).

Before this module the phone was handed the user's account-wide Frappe
``api_key``/``api_secret``: not device-scoped, so logging out revoked nothing,
revoking it broke every other API integration the user owned, and there was no
expiry and no device inventory. Pairing now mints a credential that belongs to
ONE phone.

Token wire format
-----------------
``jmd:<token_id>:<secret>`` presented as ``Authorization: token jmd:<id>:<sec>``.

Three colon-separated segments is not cosmetic — it is the integration point.
``frappe.auth.validate_auth_via_api_keys`` does ``api_key, api_secret =
auth_token.split(":")``, which raises ``ValueError`` on a three-segment token
and is swallowed by that function's ``except (AttributeError, TypeError,
ValueError): pass``. So core's api-key path cleanly declines a device token
(instead of raising ``AuthenticationError`` for an unknown key, which a
two-segment format would have done BEFORE any app hook could run) and
``validate_auth_via_hooks`` then reaches ``authenticate_device_token`` below.
The prefix also makes a device token unmistakable in a log or a bug report.

Storage
-------
Only ``HMAC-SHA256(key=token_id, msg=secret)`` is persisted, on the ``Jarvis
Mobile Device`` row. The token id is the per-row salt; HMAC rather than
``sha256(token_id + ":" + secret)`` because plain concatenation is ambiguous
(``("a", "b:c")`` and ``("a:b", "c")`` would hash identically). The secret is
192 bits from ``secrets.token_hex``, so one hash round is the right primitive:
there is no low-entropy input to grind, and a KDF would burn real CPU on every
authenticated request. Comparison is constant-time.

Realtime
--------
Frappe's socket.io middleware forwards the client's ``Authorization`` header to
``/api/method/frappe.realtime.get_user_info`` — an ordinary HTTP request — so
the hook below authenticates the realtime handshake with no extra work.
``frappe.set_user`` leaves ``session.data.user_type`` empty and core already
falls back to a cached ``User.user_type`` read for exactly that case.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import frappe

DEVICE_DOCTYPE = "Jarvis Mobile Device"

#: Wire prefix; also the reason core's api-key parser declines the token.
DEVICE_TOKEN_PREFIX = "jmd"

TOKEN_ID_LENGTH = 24  # 96 bits — an identifier, not a secret
SECRET_LENGTH = 48  # 192 bits from secrets.token_hex

#: ``last_used`` is telemetry for the device inventory, not an audit clock: one
#: write per authenticated request would be a hot-path disaster, so it is
#: stamped at most once per window per device.
LAST_USED_THROTTLE_SECONDS = 300

#: Ceiling on live devices per user. Re-pairing the same phone mints a new row
#: (the old secret is unrecoverable), so without a cap a user who reinstalls
#: repeatedly accumulates live credentials forever.
MAX_DEVICES_PER_USER = 20

#: Frappe rolls the transaction back at the end of a read-only request, so a
#: ``last_used`` stamp taken during auth on one of these needs its own commit.
_READ_ONLY_METHODS = ("GET", "HEAD", "OPTIONS")

_AUDIT_LOGGER = "jarvis.mobile_device_audit"


# --------------------------------------------------------------------------- #
# Token format
# --------------------------------------------------------------------------- #
def _hash_secret(token_id: str, secret: str) -> str:
	"""Keyed SHA-256 of the secret, salted with the (unique) token id.

	HMAC, not ``sha256(token_id + ":" + secret)``: concatenation is ambiguous —
	``("a", "b:c")`` and ``("a:b", "c")`` would collide."""
	return hmac.new(token_id.encode(), secret.encode(), hashlib.sha256).hexdigest()


def format_token(token_id: str, secret: str) -> str:
	return f"{DEVICE_TOKEN_PREFIX}:{token_id}:{secret}"


def parse_token(raw: str | None) -> tuple[str, str] | None:
	"""Split a device token into ``(token_id, secret)``; ``None`` if the string
	is not one (an ordinary Frappe ``key:secret`` pair returns ``None``, which
	is what keeps the two credential kinds from ever crossing paths)."""
	if not raw:
		return None
	parts = raw.strip().split(":")
	if len(parts) != 3 or parts[0] != DEVICE_TOKEN_PREFIX:
		return None
	token_id, secret = parts[1], parts[2]
	if not token_id or not secret:
		return None
	return token_id, secret


def is_device_token(raw: str | None) -> bool:
	return parse_token(raw) is not None


def current_device_token_id() -> str | None:
	"""Token id that authenticated THIS request, if a device token did."""
	return frappe.local.flags.get("jarvis_mobile_device")


# --------------------------------------------------------------------------- #
# Mint
# --------------------------------------------------------------------------- #
def mint(user: str, device_label: str | None = None, platform: str | None = None) -> dict:
	"""Create a NEW credential for one device and return the plaintext ONCE.

	Every pairing gets its own row + its own secret — nothing is ever reused,
	so two phones can never hold the same credential and revoking one cannot
	affect the other."""
	token_id = frappe.generate_hash(length=TOKEN_ID_LENGTH)
	secret = frappe.generate_hash(length=SECRET_LENGTH)
	doc = frappe.get_doc(
		{
			"doctype": DEVICE_DOCTYPE,
			"user": user,
			"device_label": device_label,
			"platform": platform,
			"token_id": token_id,
			"secret_hash": _hash_secret(token_id, secret),
			"enabled": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	_prune(user, keep_token_id=token_id)
	_audit("mint", user=user, token_id=token_id, device=doc.device_label, platform=doc.platform)
	return {
		"name": doc.name,
		"token_id": token_id,
		"secret": secret,
		"token": format_token(token_id, secret),
		"device_label": doc.device_label,
		"platform": doc.platform,
	}


def _prune(user: str, keep_token_id: str) -> None:
	"""Revoke the oldest live devices past ``MAX_DEVICES_PER_USER``."""
	rows = frappe.get_all(
		DEVICE_DOCTYPE,
		filters={"user": user, "enabled": 1},
		fields=["name", "token_id"],
		order_by="creation desc, name desc",
		limit_page_length=0,
	)
	for row in rows[MAX_DEVICES_PER_USER:]:
		if row.token_id == keep_token_id:
			continue
		_disable(row.name, "device limit reached")
		_audit("prune", user=user, token_id=row.token_id)


# --------------------------------------------------------------------------- #
# Authenticate
# --------------------------------------------------------------------------- #
def resolve(raw: str | None) -> dict | None:
	"""Resolve a device token to ``{"user", "token_id", "name"}``.

	Returns ``None`` — never a partially-trusted result — for anything that is
	not a live token belonging to an enabled user. Fails CLOSED."""
	parsed = parse_token(raw)
	if not parsed:
		return None
	token_id, secret = parsed
	row = frappe.db.get_value(
		DEVICE_DOCTYPE,
		{"token_id": token_id},
		["name", "user", "secret_hash", "enabled", "last_used"],
		as_dict=True,
	)
	# Hash unconditionally so an unknown token id and a wrong secret cost the
	# same, then compare in constant time.
	presented = _hash_secret(token_id, secret)
	if not row or not hmac.compare_digest(row.secret_hash or "", presented):
		return None
	if not row.enabled:
		return None
	if not row.user or not frappe.db.get_value("User", row.user, "enabled"):
		return None
	_touch(row.name, row.last_used)
	return {"user": row.user, "token_id": token_id, "name": row.name}


def authenticate_device_token() -> None:
	"""``auth_hooks`` entry — authenticate ``Authorization: token jmd:<id>:<s>``.

	Runs after core's api-key path declined the token (see the module
	docstring). On failure it deliberately does nothing: the session stays
	Guest and ``frappe.auth.validate_auth`` turns a presented-but-unusable
	Authorization header into a 401 by itself."""
	if not getattr(frappe.local, "request", None):
		return
	header = frappe.get_request_header("Authorization", "") or ""
	parts = header.split(" ", 1)
	if len(parts) != 2 or parts[0].lower() != "token":
		return
	if not is_device_token(parts[1]):
		return

	# An already-established cookie session wins, exactly as core's api-key
	# path does (it only sets the user when the resumed session is Guest).
	login_manager = getattr(frappe.local, "login_manager", None)
	if login_manager is not None and login_manager.user not in ("", "Guest"):
		return

	device = resolve(parts[1])
	if not device:
		return

	# frappe.set_user() resets form_dict; core's api-key path restores it and so
	# must we, or every argument of the request is lost.
	form_dict = frappe.local.form_dict
	frappe.set_user(device["user"])
	frappe.local.form_dict = form_dict
	frappe.local.flags.jarvis_mobile_device = device["token_id"]


def _touch(name: str, last_used) -> None:
	"""Best-effort throttled ``last_used`` stamp. Never raises into auth."""
	now = frappe.utils.now_datetime()
	if last_used:
		try:
			if (now - frappe.utils.get_datetime(last_used)).total_seconds() < LAST_USED_THROTTLE_SECONDS:
				return
		except Exception:
			pass
	try:
		frappe.db.set_value(DEVICE_DOCTYPE, name, "last_used", now, update_modified=False)
		request = getattr(frappe.local, "request", None)
		if request is not None and request.method in _READ_ONLY_METHODS:
			# Auth runs before the handler, so nothing else is pending yet and
			# this commit is contained to the one column above.
			frappe.db.commit()
	except Exception:
		frappe.logger("jarvis.mobile").debug(f"last_used stamp failed for {name}", exc_info=True)


# --------------------------------------------------------------------------- #
# Inventory + revocation
# --------------------------------------------------------------------------- #
def list_devices(user: str) -> list[dict]:
	"""The user's device inventory. Never exposes ``secret_hash``."""
	rows = frappe.get_all(
		DEVICE_DOCTYPE,
		filters={"user": user},
		fields=[
			"token_id",
			"device_label",
			"platform",
			"enabled",
			"last_used",
			"creation",
			"revoked_at",
		],
		order_by="creation desc",
		limit_page_length=0,
	)
	current = current_device_token_id()
	for row in rows:
		row["current"] = 1 if current and row["token_id"] == current else 0
	return rows


def revoke(user: str, token_id: str, reason: str = "revoked by user") -> bool:
	"""Revoke ONE of ``user``'s devices. Returns False if already revoked.

	A row belonging to somebody else is indistinguishable from a row that does
	not exist — both raise PermissionError, so this can never be used to probe
	another account's device inventory."""
	row = (
		frappe.db.get_value(
			DEVICE_DOCTYPE, {"token_id": token_id}, ["name", "user", "enabled"], as_dict=True
		)
		if token_id
		else None
	)
	if not row or row.user != user:
		frappe.throw("Unknown device.", frappe.PermissionError)
	if not row.enabled:
		return False
	_disable(row.name, reason)
	_audit("revoke", user=user, token_id=token_id, reason=reason)
	return True


def revoke_all(user: str, except_token_id: str | None = None, reason: str = "revoked by user") -> int:
	"""Revoke every live device of ``user`` (optionally sparing one)."""
	rows = frappe.get_all(
		DEVICE_DOCTYPE,
		filters={"user": user, "enabled": 1},
		fields=["name", "token_id"],
		limit_page_length=0,
	)
	count = 0
	for row in rows:
		if except_token_id and row.token_id == except_token_id:
			continue
		_disable(row.name, reason)
		_audit("revoke", user=user, token_id=row.token_id, reason=reason)
		count += 1
	return count


def _disable(name: str, reason: str) -> None:
	doc = frappe.get_doc(DEVICE_DOCTYPE, name)
	doc.enabled = 0
	doc.revoked_at = frappe.utils.now_datetime()
	doc.revoked_by = frappe.session.user
	doc.revoked_reason = (reason or "")[:140]
	doc.save(ignore_permissions=True)


def _audit(action: str, **fields) -> None:
	"""Structured audit line. Mirrors jarvis/audit.py: a logger rather than a
	doc insert, so it is transaction-safe and can never raise into the caller.
	The durable, queryable trail is the row itself (revoked_at / revoked_by /
	revoked_reason plus the doctype's Version history)."""
	try:
		entry = {
			"ts": frappe.utils.now(),
			"action": action,
			"actor": getattr(frappe.session, "user", None),
			**fields,
		}
		frappe.logger(_AUDIT_LOGGER).info(json.dumps(entry, default=str))
	except Exception:
		pass
