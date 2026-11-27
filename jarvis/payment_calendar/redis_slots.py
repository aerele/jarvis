"""Redis leases and slots shared by the built-in reader's router and the cache refresh.

Every key here is a lease: `SET NX EX` with the owner's token, deleted or renewed only while it still
holds that token (one Lua script, so the check and the write are one step). A lease that expires is gone;
a job killed without a trace (OOM, SIGKILL) therefore never blocks the key for longer than its TTL.

The module knows no key name and no TTL: the caller passes both. `routing` keeps its per-site keys
(`jarvis:pcal:reader:*`); the cache refresh keeps bench-wide keys (`make_key(shared=True)`).

Redis is not the database. These keys admit work and nothing more: a lost key may let two jobs start,
and the cache's own durable claim then fences them.
"""

import frappe

from jarvis.payment_calendar.errors import TIMEOUTS

# Delete a key only while it holds the caller's token (the key may have expired and been taken again).
RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
# Extend a key's life only while it holds the caller's token.
RENEW = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"


def claim(key, token: str, ttl: int) -> bool:
	"""`SET NX EX` with the owner's token; False when another owner holds the key."""
	return bool(frappe.cache.execute_command("SET", key, token, "NX", "EX", ttl))


def take_slot(token: str, keys, ttl: int):
	"""The first key of `keys` that is free, now held by `token`; None when all are held."""
	for key in keys:
		if claim(key, token, ttl):
			return key
	return None


def release(key, token: str) -> None:
	"""Delete a key only while it still holds our token. Best effort; a job deadline propagates."""
	try:
		frappe.cache.eval(RELEASE, 1, key, token)
	except TIMEOUTS:
		raise
	except Exception:
		pass


def renew(key, token: str, ttl: int) -> bool:
	"""Give a key `ttl` more seconds, only while it still holds our token.

	True only when Redis confirmed it. A false answer, or a Redis error, means the lease is not known to be
	ours; the caller decides what that costs. A job deadline propagates.
	"""
	try:
		return bool(frappe.cache.eval(RENEW, 1, key, token, ttl))
	except TIMEOUTS:
		raise
	except Exception:
		return False


def remember(key, ttl: int, value: str = "1") -> None:
	"""Set a key for `ttl` seconds with one atomic SET (a memo). Best effort; a job deadline propagates."""
	try:
		frappe.cache.execute_command("SET", key, value, "EX", ttl)
	except TIMEOUTS:
		raise
	except Exception:
		pass
