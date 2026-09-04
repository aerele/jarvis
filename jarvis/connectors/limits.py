"""Circuit breaker + concurrency cap for connector calls.

Both protect OTHER tenants (and the shared gunicorn pool) from one connector's
bad day: a hung or flapping endpoint must not tie up workers or make every chat
turn wait out the full 20s timeout. Both are backed by an injectable ``store``
so the state machine is unit-tested against an in-memory fake with a controllable
clock, and driven in production by a thin Redis adapter over ``frappe.cache()``
(built in ``broker.py`` so this module stays frappe-free).

STORE CONTRACT (all keys are plain strings; the adapter owns site-prefixing):

  * ``incr(key) -> int``            atomic increment, returns the new value
  * ``decr(key) -> int``            atomic decrement, returns the new value
  * ``expire(key, ttl_s)``          set a TTL (seconds)
  * ``get(key) -> str | None``      read (str), or None if absent
  * ``set(key, value, ttl_s)``      write with a TTL
  * ``delete(key)``                 remove

Redis ``incr``/``decr``/``expire`` are atomic, so the counters are race-free
under concurrent workers without a mutex; every counter carries a TTL so a
crashed worker (or a lost ``finally``) can never wedge the breaker open or the
cap full forever.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager


class AtCapacityError(Exception):
	"""Raised by :meth:`ConcurrencyCap.slot` when the per-connector in-flight
	count is already at the limit. The broker turns this into a fast-fail tool
	error, never a wait."""


class CircuitBreaker:
	"""N transport failures within ``window_s`` trip the breaker OPEN for
	``open_s`` seconds; while open, :meth:`allow` returns False and the broker
	fast-fails without touching the network. A success clears the failure count.

	Only TRANSPORT-class failures should be recorded (connect/timeout/5xx/
	malformed). SSRF blocks, gate denials, argument errors, auth 401/403, and an
	in-band ``isError: true`` tool result are NOT breaker failures - they say
	nothing about the endpoint's health and must not open the circuit."""

	def __init__(
		self,
		store,
		key: str,
		*,
		threshold: int = 5,
		window_s: int = 60,
		open_s: int = 30,
		now: Callable[[], float] = time.time,
	):
		self._store = store
		self._fail_key = f"cb:fail:{key}"
		self._open_key = f"cb:open:{key}"
		self._threshold = threshold
		self._window_s = window_s
		self._open_s = open_s
		# Wall-clock (time.time), not monotonic: the open marker is an absolute
		# instant shared across processes via Redis.
		self._now = now

	def allow(self) -> bool:
		open_until = self._store.get(self._open_key)
		if open_until is None:
			return True
		try:
			return self._now() >= float(open_until)
		except (TypeError, ValueError):
			# Corrupt marker - fail OPEN would wedge the connector; treat as closed
			# and let the next failure re-arm it cleanly.
			self._store.delete(self._open_key)
			return True

	def record_success(self) -> None:
		self._store.delete(self._fail_key)

	def record_failure(self) -> None:
		n = self._store.incr(self._fail_key)
		if n == 1:
			self._store.expire(self._fail_key, self._window_s)
		if n >= self._threshold:
			self._store.set(self._open_key, str(self._now() + self._open_s), self._open_s)
			self._store.delete(self._fail_key)


class ConcurrencyCap:
	"""At most ``limit`` calls in flight for one (tenant, connector) at a time.
	The counter self-expires (TTL a little longer than a call's total timeout) so
	a lost decrement cannot leak a slot permanently. A decrement that drops below
	zero (the TTL fired mid-call and the key was recreated by a later caller)
	deletes the key so the count re-bases at zero instead of drifting negative."""

	def __init__(self, store, key: str, *, limit: int = 4, ttl_s: int = 40):
		self._store = store
		self._key = f"cc:{key}"
		self._limit = limit
		self._ttl_s = ttl_s

	@contextmanager
	def slot(self):
		n = self._store.incr(self._key)
		if n == 1:
			self._store.expire(self._key, self._ttl_s)
		if n > self._limit:
			# Undo our own increment before rejecting.
			self._release()
			raise AtCapacityError(f"Connector is at its concurrency limit ({self._limit}).")
		try:
			yield
		finally:
			self._release()

	def _release(self) -> None:
		try:
			left = self._store.decr(self._key)
			if left is not None and left < 0:
				self._store.delete(self._key)
		except Exception:
			# The TTL is the backstop - never let a release failure escape.
			pass


class InMemoryStore:
	"""A single-process, non-atomic store for unit tests. NOT for production (no
	cross-worker visibility, no real atomicity) - it just satisfies the store
	contract so the state machines above can be driven deterministically."""

	def __init__(self):
		self._d: dict[str, str] = {}

	def incr(self, key: str) -> int:
		v = int(self._d.get(key, "0")) + 1
		self._d[key] = str(v)
		return v

	def decr(self, key: str) -> int:
		v = int(self._d.get(key, "0")) - 1
		self._d[key] = str(v)
		return v

	def expire(self, key: str, ttl_s: int) -> None:
		# TTL is a no-op in the fake; tests drive expiry by deleting explicitly.
		pass

	def get(self, key: str):
		return self._d.get(key)

	def set(self, key: str, value, ttl_s: int) -> None:
		self._d[key] = str(value)

	def delete(self, key: str) -> None:
		self._d.pop(key, None)
