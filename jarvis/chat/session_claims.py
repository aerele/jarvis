"""Claims on the session keys this site created.

The gateway hands back a key at session creation, before any run or tool call writes
a row for it. Claiming the key here lets ``vouch_session`` answer "this site started
that session" from an exact-key record, never from the key's shape. Keys are
lowercased first because the gateway canonicalizes them. A cache error never raises:
claim is then a no-op and ``is_claimed`` is False.
"""

import hashlib

import frappe

_PREFIX = "jarvis:session_claim:"
_TTL_SECONDS = 7 * 24 * 3600


def _cache_key(session_key: str) -> str:
	"""The UNPREFIXED key: ``set_value`` and ``exists`` each apply ``make_key`` themselves."""
	digest = hashlib.sha256(str(session_key).lower().encode()).hexdigest()
	return _PREFIX + digest


def claim(session_key: str) -> None:
	try:
		frappe.cache().set_value(_cache_key(session_key), 1, expires_in_sec=_TTL_SECONDS)
	except Exception:
		frappe.logger("jarvis.session_claims").warning("session claim failed", exc_info=True)


def is_claimed(session_key: str) -> bool:
	try:
		return bool(frappe.cache().exists(_cache_key(session_key)))
	except Exception:
		frappe.logger("jarvis.session_claims").warning("session claim lookup failed", exc_info=True)
		return False


def is_claimed_strict(session_key: str) -> bool:
	"""``is_claimed`` for a caller that must not fail open: a cache error RAISES
	instead of reading as "not claimed", so ``vouch_session`` can answer 503.

	Uses the raw ``ttl`` (-2 = no such key): the cache wrapper's ``exists`` swallows a
	connection error and would answer False."""
	cache = frappe.cache()
	return cache.ttl(cache.make_key(_cache_key(session_key))) != -2
