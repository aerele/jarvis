"""An atomic once-only latch on a Redis key, without ``frappe.cache().set``.

``claim_once`` runs ``INCR`` and ``TTL`` in one pipeline and treats the first increment
as the claim. Of any number of concurrent callers exactly one sees 1. A key found without
a TTL (a crash between the INCR and the EXPIRE, or a leftover) is given one, so a latch can
never stay closed forever. Same idiom as the challenge limiter in
``jarvis.chat.pairing_proof`` and ``jarvis.network_check``; it works on any Redis version
(no ``EXPIRE NX``). The key is site-prefixed with ``make_key``, so it matches a
``persistent_cache_keys`` prefix in hooks.py and ``delete_value(key)`` removes it.
"""

import frappe


def claim_once(key: str, ttl_s: int) -> bool:
	"""True for the first caller within ``ttl_s``; False for every later one. Raises on a
	cache error (callers decide whether that fails open or closed)."""
	cache = frappe.cache()
	redis_key = cache.make_key(key)
	pipe = cache.pipeline()
	pipe.incr(redis_key)
	pipe.ttl(redis_key)
	count, ttl = pipe.execute()
	if ttl < 0:
		cache.expire(redis_key, ttl_s)
	return int(count) == 1
