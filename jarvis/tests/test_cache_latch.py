"""The once-only latch used by the foreign-session throttles and the key-resync throttle."""

import threading

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis._cache_latch import claim_once

KEY = "jarvis:test_cache_latch:k1"


def _redis_key() -> bytes:
	return frappe.cache().make_key(KEY)


class TestClaimOnce(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.cache().delete(_redis_key())
		self.addCleanup(frappe.cache().delete, _redis_key())

	def test_only_the_first_call_claims(self):
		self.assertTrue(claim_once(KEY, 60))
		self.assertFalse(claim_once(KEY, 60))
		self.assertFalse(claim_once(KEY, 60))

	def test_the_key_expires_and_is_site_prefixed(self):
		claim_once(KEY, 60)
		self.assertTrue(0 < frappe.cache().ttl(_redis_key()) <= 60)
		self.assertTrue(_redis_key().decode().endswith("|" + KEY))

	def test_delete_value_releases_the_latch(self):
		claim_once(KEY, 60)
		frappe.cache().delete_value(KEY)
		self.assertTrue(claim_once(KEY, 60))

	def test_a_key_without_a_ttl_gets_re_armed(self):
		frappe.cache().incr(_redis_key())  # a leftover key that would otherwise block forever
		self.assertEqual(frappe.cache().ttl(_redis_key()), -1)
		self.assertFalse(claim_once(KEY, 60))
		self.assertTrue(0 < frappe.cache().ttl(_redis_key()) <= 60)

	def test_a_re_armed_key_keeps_its_remaining_ttl(self):
		claim_once(KEY, 60)
		frappe.cache().expire(_redis_key(), 30)
		claim_once(KEY, 60)
		self.assertTrue(0 < frappe.cache().ttl(_redis_key()) <= 30)

	def test_the_primitive_has_one_winner_under_a_race(self):
		"""Real redis: threads race the same transaction ``claim_once`` runs (``frappe.local``
		is per thread, so the key and connection are prepared up front); exactly one sees 1."""
		cache = frappe.cache()
		key = _redis_key()
		results = []
		barrier = threading.Barrier(8)

		def worker():
			barrier.wait()
			pipe = cache.pipeline()
			pipe.incr(key)
			pipe.ttl(key)
			results.append(int(pipe.execute()[0]) == 1)

		threads = [threading.Thread(target=worker) for _ in range(8)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()
		self.assertEqual(results.count(True), 1)
