"""Guest endpoint the Jarvis admin's connection test calls (design spec 6.3, contract 3).

A fleet host, not a browser, POSTs here through the customer's PUBLIC address to prove that
the bench behind that address holds the customer's admin API secret. The bench answers
with an HMAC over the single-use challenge, keyed by a value derived from the secret, so
the secret itself never travels.

What this endpoint deliberately does NOT do:
- It does not check ``network_mode``: the answer leaks nothing, and a bench that
  reconnected without the private setup must still answer.
- It keeps no nonce store and writes nothing. Admin enforces single use, so a replayed
  answer is useless.
- It never explains a refusal. Every cause the handler sees (bad shape, stale clock, no
  secret, rate limit) returns the same ``{"ok": False}`` with HTTP 200, so the endpoint is
  not an oracle. Framework-level errors (invalid JSON, a GET) are answered by Frappe itself.
"""

import hashlib
import hmac
import re
import time
from typing import Any

import frappe
from frappe.utils.password import get_decrypted_password

_LABEL = b"jarvis-connection-check-v1"
_CID_RE = re.compile(r"[0-9a-f]{32}")
_NONCE_RE = re.compile(r"[0-9a-f]{64}")
_PAST_S = 30
_AHEAD_S = 300

# A flood ceiling, not a per-caller quota: behind a gateway every caller (the fleet host and
# any attacker) shares the gateway's IP, so a low limit would let junk block the real probe.
RATE_LIMIT = 600
RATE_WINDOW_S = 60


def compute_mac(api_secret: str, cid: str, nonce: str, exp: int, site: str) -> str:
	"""The contract's MAC, verbatim."""
	key = hmac.new(api_secret.encode(), _LABEL, hashlib.sha256).digest()
	canonical = f"v1|{cid}|{nonce}|{exp}|{site}"
	return hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()


def _now() -> float:
	return time.time()


def _refuse() -> dict:
	return {"ok": False}


def _rate_limit_key() -> str:
	"""Keyed by a digest of the caller IP: X-Forwarded-For is attacker-controlled, so the
	raw value must not size the key."""
	ip = getattr(frappe.local, "request_ip", None) or "unknown"
	digest = hashlib.sha256(str(ip).encode()).hexdigest()[:32]
	return f"jarvis:netcheck_rl:{frappe.local.site}:{digest}"


def _rate_limited() -> bool:
	"""600 well-formed calls a minute per source IP. Same idiom as ``jarvis.mobile.auth._throttle_pairing``:
	``incr`` and ``ttl`` in one pipeline, and a counter found without a TTL (evicted and
	recreated by INCR, or left by a crash) is re-armed, so an address can never be blocked
	forever. Fails open on cache trouble, like ``jarvis._plugin_auth._enforce_rate_limit``:
	the endpoint proves nothing secret, and a Redis blip must not fail a connection test."""
	try:
		key = _rate_limit_key()
		cache = frappe.cache()
		pipe = cache.pipeline()
		pipe.incr(key)
		pipe.ttl(key)
		attempts, ttl = pipe.execute()
		if ttl < 0:
			cache.expire(key, RATE_WINDOW_S)
		return int(attempts) > RATE_LIMIT
	except Exception:
		return False


def _api_secret() -> str:
	secret = get_decrypted_password(
		"Jarvis Settings", "Jarvis Settings", "jarvis_admin_api_secret", raise_exception=False
	)
	return (secret or "").strip()


def _well_formed(v: Any, cid: Any, nonce: Any, exp: Any) -> bool:
	# ``type(x) is int`` on purpose: True is an int in Python, and a numeric string is not
	# the integer the contract specifies.
	if type(v) is not int or v != 1 or type(exp) is not int:
		return False
	if not (isinstance(cid, str) and _CID_RE.fullmatch(cid)):
		return False
	return isinstance(nonce, str) and bool(_NONCE_RE.fullmatch(nonce))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def answer(v: Any = None, cid: Any = None, nonce: Any = None, exp: Any = None):
	# ``Any`` on purpose: Frappe's strict-bench check demands annotations, and any narrower
	# type would coerce input before the shape check below. Refusals stay byte-identical.
	# Shape first, so junk never spends the budget; the limiter still runs before any secret read.
	if not _well_formed(v, cid, nonce, exp):
		return _refuse()
	if _rate_limited():
		return _refuse()
	now = _now()
	if exp < now - _PAST_S or exp > now + _AHEAD_S:
		return _refuse()
	secret = _api_secret()
	if not secret:
		return _refuse()
	site = frappe.local.site
	return {"v": 1, "site": site, "mac": compute_mac(secret, cid, nonce, exp, site)}
