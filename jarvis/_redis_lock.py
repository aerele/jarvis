"""Redis-backed advisory mutex used by Sprint-2 concurrency fixes.

Two call sites today:

  jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin
      Two close saves enqueue two workers; without the lock both call
      admin -> fleet -> agent in parallel and either clobber each
      other's last_sync_status writes or fire two redundant container
      restarts. The lock serializes one-at-a-time per bench.

  jarvis.chat.agent_client.AgentSession._attempt_connect
      After a tenant re-provision, N RQ workers concurrently observe
      "stale pairing" rejections and all race to clear_credentials() +
      re-pair. Each ensure_paired() generates a different Ed25519 keypair;
      Jarvis Settings's chat_device_* fields are last-write-wins, so all
      but one worker end up with creds that don't match what admin
      stored. The lock makes the clear+re-pair sequence one-at-a-time;
      the late arrivals re-read fresh creds and reuse the winner's pair.

Both call sites tolerate "lock held elsewhere" via a bounded wait + check
of the now-current state (so they're not strict mutual-exclusion, more
"convoy collapse"). For the small number of expected contenders (<=N
chat workers, <=2 settings savers) Redis lock latency is negligible.

frappe.cache() returns a RedisWrapper that subclasses redis.Redis, so
the standard redis-py Lock primitive is available - it handles the
token-tracked release Lua script so a slow holder won't accidentally
delete a lock that has since been re-acquired by another worker.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import frappe

LOCK_PREFIX = "jarvis:lock:"


@contextlib.contextmanager
def redis_lock(name: str, *, timeout_s: int = 60, blocking_timeout_s: float = 0.0) -> Iterator[bool]:
	"""Acquire a Redis advisory lock named ``name``.

	Args:
		timeout_s: TTL on the underlying Redis key. A crashed holder
			automatically releases after this many seconds, so the lock
			can never permanently deadlock the system.
		blocking_timeout_s: Time the caller is willing to wait to
			acquire. 0 == try-once (non-blocking).

	Yields:
		True if the lock was acquired (caller is the holder), False
		otherwise. Callers SHOULD branch on the yielded value and may
		choose to no-op (coalesce-and-move-on) or re-poll fresh state.

	Never raises on contention - that's a normal flow. Real Redis
	errors (connection refused etc.) propagate.
	"""
	cache = frappe.cache()
	key = LOCK_PREFIX + name
	lock = cache.lock(key, timeout=timeout_s, blocking_timeout=blocking_timeout_s or None)
	acquired = False
	try:
		acquired = bool(lock.acquire(blocking=bool(blocking_timeout_s)))
		yield acquired
	finally:
		if acquired:
			try:
				lock.release()
			except Exception:
				# The TTL is the backstop - if release fails (e.g. lock
				# already expired and re-acquired by someone else)
				# don't surface the noise to the caller.
				pass
