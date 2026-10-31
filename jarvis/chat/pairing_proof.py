"""One-time nonces for the site proof.

Admin hands secrets and pairing material only to the site that proves it holds a
nonce this site issued: fleet runs the plugin CLI in the tenant container, which
POSTs ``jarvis.api.pairing_challenge`` to the bound URL. A copy keeps its own
cache, so the real site answers ``held=false`` for a nonce the copy issued.
"""

import contextlib
import hashlib
import re
import secrets

import frappe
import redis

_PREFIX = "jarvis:pairing_proof:"
_TTL_S = 600
_NONCE_RE = re.compile(r"[0-9a-f]{32}")

# The challenge endpoint is a guest endpoint (the nonce is its only credential), so it
# carries its own flood limits. Two buckets: per source IP, and one for the whole site
# (behind a gateway many callers share an IP, and a rotating-IP flood must still stop).
# Every request counts, valid nonce or not. Legitimate use is one call per admin proof.
CHALLENGE_PER_IP_LIMIT = 30
CHALLENGE_GLOBAL_LIMIT = 120
CHALLENGE_WINDOW_S = 60
_RL_PREFIX = "jarvis:pairing_challenge_rl:"


def is_well_formed(nonce) -> bool:
	return isinstance(nonce, str) and bool(_NONCE_RE.fullmatch(nonce))


def challenge_rate_limited() -> bool:
	"""Count this request against the per-IP and the per-site buckets; True when either
	is over. Same idiom as ``jarvis.network_check._rate_limited``: ``incr`` and ``ttl`` in
	one pipeline, a counter without a TTL is re-armed so nobody is blocked forever, the
	IP is digested because X-Forwarded-For is attacker-controlled, and cache trouble
	fails OPEN (a Redis blip must not break the proof; the nonce is still single-use).
	Writes nothing to the Error Log: it runs on every request."""
	ip = getattr(frappe.local, "request_ip", None) or "unknown"
	digest = hashlib.sha256(str(ip).encode()).hexdigest()[:32]
	site = frappe.local.site
	buckets = (
		(f"{_RL_PREFIX}{site}:ip:{digest}", CHALLENGE_PER_IP_LIMIT),
		(f"{_RL_PREFIX}{site}:all", CHALLENGE_GLOBAL_LIMIT),
	)
	limited = False
	try:
		cache = frappe.cache()
		for key, limit in buckets:
			pipe = cache.pipeline()
			pipe.incr(key)
			pipe.ttl(key)
			attempts, ttl = pipe.execute()
			if ttl < 0:
				cache.expire(key, CHALLENGE_WINDOW_S)
			limited = limited or int(attempts) > limit
	except Exception:
		return False
	return limited


def _redis_key(nonce: str) -> bytes:
	return frappe.cache().make_key(_PREFIX + nonce)


def issue_nonce() -> str:
	nonce = secrets.token_hex(16)
	with contextlib.suppress(redis.exceptions.ConnectionError):
		frappe.cache().set(_redis_key(nonce), b"1", ex=_TTL_S)
	return nonce


def consume_nonce(nonce: str) -> bool:
	"""True once per issued nonce; False for unknown, expired or malformed.

	Atomic: each nonce is its own redis key and the consume is one DEL, whose reply is
	the number of keys it removed. Redis runs commands one at a time, so of two
	concurrent consumes of the same nonce exactly one sees 1 and the other 0. A
	get-then-delete would let both pass."""
	if not is_well_formed(nonce):
		return False
	try:
		return frappe.cache().delete(_redis_key(nonce)) == 1
	except redis.exceptions.ConnectionError:
		return False
