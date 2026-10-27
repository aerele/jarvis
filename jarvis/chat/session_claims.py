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
	digest = hashlib.sha256(str(session_key).lower().encode()).hexdigest()
	cache = frappe.cache()
	return cache.make_key(_PREFIX + digest)


def claim(session_key: str) -> None:
	try:
		frappe.cache().set(_cache_key(session_key), 1, ex=_TTL_SECONDS)
	except Exception:
		frappe.logger("jarvis.session_claims").warning("session claim failed", exc_info=True)


def is_claimed(session_key: str) -> bool:
	try:
		return frappe.cache().get(_cache_key(session_key)) is not None
	except Exception:
		frappe.logger("jarvis.session_claims").warning("session claim lookup failed", exc_info=True)
		return False
