"""Unit tests for ``jarvis.connectors.limits`` - the circuit breaker and
concurrency cap. Plain ``unittest``; the state machines run against the
in-memory fake store with a controllable clock, no bench or Redis.
"""

from __future__ import annotations

import unittest

from jarvis.connectors import limits


class _Clock:
	def __init__(self, t=1000.0):
		self.t = t

	def __call__(self):
		return self.t


class TestCircuitBreaker(unittest.TestCase):
	def _breaker(self, clock):
		return limits.CircuitBreaker(
			limits.InMemoryStore(), "conn1", threshold=3, window_s=60, open_s=30, now=clock
		)

	def test_opens_after_threshold_failures(self):
		clock = _Clock()
		cb = self._breaker(clock)
		self.assertTrue(cb.allow())
		cb.record_failure()
		cb.record_failure()
		self.assertTrue(cb.allow(), "still closed below threshold")
		cb.record_failure()  # third failure trips it
		self.assertFalse(cb.allow(), "open after threshold")

	def test_recovers_after_open_window(self):
		clock = _Clock()
		cb = self._breaker(clock)
		for _ in range(3):
			cb.record_failure()
		self.assertFalse(cb.allow())
		clock.t += 31  # past open_s=30
		self.assertTrue(cb.allow(), "closes again after the open window elapses")

	def test_success_clears_failure_count(self):
		clock = _Clock()
		cb = self._breaker(clock)
		cb.record_failure()
		cb.record_failure()
		cb.record_success()
		cb.record_failure()
		cb.record_failure()
		self.assertTrue(cb.allow(), "count was reset by the success, so 2 more do not trip")

	def test_corrupt_open_marker_fails_closed_not_open(self):
		store = limits.InMemoryStore()
		store.set("cb:open:conn1", "not-a-number", 30)
		cb = limits.CircuitBreaker(store, "conn1", now=_Clock())
		self.assertTrue(cb.allow(), "an unparsable marker must not wedge the connector open")


class TestConcurrencyCap(unittest.TestCase):
	def test_allows_up_to_limit_and_releases(self):
		cap = limits.ConcurrencyCap(limits.InMemoryStore(), "conn1", limit=2, ttl_s=40)
		with cap.slot():
			with cap.slot():
				with self.assertRaises(limits.AtCapacityError):
					with cap.slot():
						pass
			# one released -> a new slot is available again
			with cap.slot():
				pass

	def test_reject_undoes_its_own_increment(self):
		store = limits.InMemoryStore()
		cap = limits.ConcurrencyCap(store, "conn1", limit=1, ttl_s=40)
		with cap.slot():
			with self.assertRaises(limits.AtCapacityError):
				with cap.slot():
					pass
		# back to zero after all context managers exit (no leaked slot)
		self.assertEqual(store.get("cc:conn1"), "0")

	def test_negative_underflow_deletes_key(self):
		store = limits.InMemoryStore()
		cap = limits.ConcurrencyCap(store, "conn1", limit=2, ttl_s=40)
		# Simulate the TTL having fired mid-call: counter gone while a slot is held.
		with cap.slot():
			store.delete("cc:conn1")
		# release drove it to -1 then deleted it; key is clean, not stuck negative
		self.assertIsNone(store.get("cc:conn1"))


if __name__ == "__main__":
	unittest.main()
