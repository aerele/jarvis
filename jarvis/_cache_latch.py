"""An atomic once-only latch on a Redis key, without ``frappe.cache().set``.

``claim_once`` runs ``INCR`` and ``EXPIRE NX`` in one transaction and treats the first
increment as the claim. Of any number of concurrent callers exactly one sees 1, and the
key always carries a TTL (a counter left without one is given it by the next caller), so
a latch can never stay closed forever. Same primitive as the challenge limiter in
``jarvis.chat.pairing_proof``. The key is site-prefixed with ``make_key``, so it matches
a ``persistent_cache_keys`` prefix in hooks.py and ``delete_value(key)`` removes it.
"""

import frappe


def claim_once(key: str, ttl_s: int) -> bool:
	"""True for the first caller within ``ttl_s``; False for every later one. Raises on a
	cache error (callers decide whether that fails open or closed)."""
	cache = frappe.cache()
	redis_key = cache.make_key(key)
	pipe = cache.pipeline()
	pipe.incr(redis_key)
	pipe.expire(redis_key, ttl_s, nx=True)
	count, _ = pipe.execute()
	return int(count) == 1
