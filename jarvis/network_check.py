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
- It never explains a refusal. Every cause (bad shape, stale clock, no secret, rate limit)
  returns the same ``{"ok": False}`` with HTTP 200, so the endpoint is not an oracle.
"""

import hashlib
import hmac
import re
import time

import frappe
from frappe.utils.password import get_decrypted_password

_LABEL = b"jarvis-connection-check-v1"
_CID_RE = re.compile(r"[0-9a-f]{32}")
_NONCE_RE = re.compile(r"[0-9a-f]{64}")
_PAST_S = 30
_AHEAD_S = 300

RATE_LIMIT = 60
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


def _rate_limited() -> bool:
	"""60 calls a minute per source IP. The counter is created with its TTL in one atomic
	step (SET NX EX), so a crash between creating and expiring it cannot leave a counter
	that blocks an address forever. Fails open on cache trouble, like
	``jarvis._plugin_auth._enforce_rate_limit``: the endpoint proves nothing secret, and a
	Redis blip must not fail a customer's connection test."""
	try:
		ip = getattr(frappe.local, "request_ip", None) or "unknown"
		key = f"jarvis:netcheck_rl:{frappe.local.site}:{ip}"
		cache = frappe.cache()
		cache.set(key, 0, ex=RATE_WINDOW_S, nx=True)
		return cache.incr(key) > RATE_LIMIT
	except Exception:
		return False


def _api_secret() -> str:
	secret = get_decrypted_password(
		"Jarvis Settings", "Jarvis Settings", "jarvis_admin_api_secret", raise_exception=False
	)
	return (secret or "").strip()


def _well_formed(v, cid, nonce, exp) -> bool:
	# ``type(x) is int`` on purpose: True is an int in Python, and a numeric string is not
	# the integer the contract specifies.
	if type(v) is not int or v != 1 or type(exp) is not int:
		return False
	if not (isinstance(cid, str) and _CID_RE.fullmatch(cid)):
		return False
	return isinstance(nonce, str) and bool(_NONCE_RE.fullmatch(nonce))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def answer(v=None, cid=None, nonce=None, exp=None):
	if _rate_limited():
		return _refuse()
	if not _well_formed(v, cid, nonce, exp):
		return _refuse()
	now = _now()
	if exp < now - _PAST_S or exp > now + _AHEAD_S:
		return _refuse()
	secret = _api_secret()
	if not secret:
		return _refuse()
	site = frappe.local.site
	return {"v": 1, "site": site, "mac": compute_mac(secret, cid, nonce, exp, site)}
