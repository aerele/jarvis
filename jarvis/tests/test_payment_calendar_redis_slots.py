"""Payment Calendar S4b B2: `redis_slots`, the lease primitives that `routing` and the cache refresh share.

Every test uses keys of its own (a random suffix) and deletes them afterwards, so it never touches a key
another test, or the site, uses.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_redis_slots
"""

from unittest import TestCase
from unittest.mock import patch

import frappe
from rq.timeouts import JobTimeoutException

from jarvis.payment_calendar import redis_slots, routing


def redis(*command):
	return frappe.cache.execute_command(*command)


class SlotCase(TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		suffix = frappe.generate_hash(length=10)
		self.keys = [frappe.cache.make_key(f"jarvis:pcal:test:redis_slots:{suffix}:{n}") for n in range(3)]
		self.addCleanup(lambda: [redis("DEL", key) for key in self.keys])


class TestClaim(SlotCase):
	def test_the_first_owner_takes_the_key_and_a_second_owner_does_not(self):
		self.assertTrue(redis_slots.claim(self.keys[0], "a", 60))
		self.assertFalse(redis_slots.claim(self.keys[0], "b", 60))
		self.assertEqual(redis("GET", self.keys[0]), b"a")

	def test_the_key_expires_after_its_ttl(self):
		redis_slots.claim(self.keys[0], "a", 60)
		self.assertTrue(0 < redis("TTL", self.keys[0]) <= 60)

	def test_a_shared_key_is_a_plain_string_and_works_the_same(self):
		key = f"jarvis:pcal:test:redis_slots:shared:{frappe.generate_hash(length=10)}"
		self.assertIsInstance(frappe.cache.make_key(key, shared=True), str)
		self.addCleanup(redis, "DEL", key)
		self.assertTrue(redis_slots.claim(key, "a", 60))
		self.assertTrue(redis_slots.renew(key, "a", 120))
		redis_slots.release(key, "a")
		self.assertEqual(redis("EXISTS", key), 0)


class TestTakeSlot(SlotCase):
	def test_it_takes_the_first_free_key_and_none_when_all_are_held(self):
		self.assertEqual(redis_slots.take_slot("a", self.keys[:2], 60), self.keys[0])
		self.assertEqual(redis_slots.take_slot("b", self.keys[:2], 60), self.keys[1])
		self.assertIsNone(redis_slots.take_slot("c", self.keys[:2], 60))
		self.assertEqual(redis("GET", self.keys[0]), b"a")
		self.assertEqual(redis("GET", self.keys[1]), b"b")

	def test_it_skips_a_held_key(self):
		redis("SET", self.keys[0], "other", "EX", 60)
		self.assertEqual(redis_slots.take_slot("a", self.keys, 60), self.keys[1])

	def test_no_keys_is_no_slot(self):
		self.assertIsNone(redis_slots.take_slot("a", [], 60))


class TestRelease(SlotCase):
	def test_it_deletes_only_a_key_that_still_holds_the_token(self):
		redis_slots.claim(self.keys[0], "a", 60)
		redis_slots.release(self.keys[0], "b")
		self.assertEqual(redis("GET", self.keys[0]), b"a")
		redis_slots.release(self.keys[0], "a")
		self.assertEqual(redis("EXISTS", self.keys[0]), 0)

	def test_a_key_taken_again_after_expiry_is_not_deleted_by_the_old_owner(self):
		redis_slots.claim(self.keys[0], "old", 60)
		redis("DEL", self.keys[0])
		redis_slots.claim(self.keys[0], "new", 60)
		redis_slots.release(self.keys[0], "old")
		self.assertEqual(redis("GET", self.keys[0]), b"new")

	def test_a_redis_error_is_swallowed_and_a_job_deadline_is_not(self):
		with patch.object(frappe.cache, "eval", side_effect=RuntimeError("down")):
			redis_slots.release(self.keys[0], "a")
		with patch.object(frappe.cache, "eval", side_effect=JobTimeoutException()):
			with self.assertRaises(JobTimeoutException):
				redis_slots.release(self.keys[0], "a")


class TestRenew(SlotCase):
	def test_it_extends_the_ttl_of_an_owned_key(self):
		redis_slots.claim(self.keys[0], "a", 30)
		self.assertTrue(redis_slots.renew(self.keys[0], "a", 600))
		self.assertTrue(30 < redis("TTL", self.keys[0]) <= 600)

	def test_it_does_not_extend_a_key_of_another_owner_or_a_missing_key(self):
		redis_slots.claim(self.keys[0], "a", 30)
		self.assertFalse(redis_slots.renew(self.keys[0], "b", 600))
		self.assertTrue(redis("TTL", self.keys[0]) <= 30)
		self.assertFalse(redis_slots.renew(self.keys[1], "a", 600))
		self.assertEqual(redis("EXISTS", self.keys[1]), 0)

	def test_a_redis_error_is_not_a_confirmation_and_a_job_deadline_is_not_swallowed(self):
		with patch.object(frappe.cache, "eval", side_effect=RuntimeError("down")):
			self.assertFalse(redis_slots.renew(self.keys[0], "a", 60))
		with patch.object(frappe.cache, "eval", side_effect=JobTimeoutException()):
			with self.assertRaises(JobTimeoutException):
				redis_slots.renew(self.keys[0], "a", 60)


class TestRemember(SlotCase):
	def test_it_sets_the_value_with_a_ttl_and_overwrites(self):
		redis_slots.remember(self.keys[0], 60)
		self.assertEqual(redis("GET", self.keys[0]), b"1")
		redis_slots.remember(self.keys[0], 90, "token")
		self.assertEqual(redis("GET", self.keys[0]), b"token")
		self.assertTrue(60 < redis("TTL", self.keys[0]) <= 90)

	def test_a_redis_error_is_swallowed_and_a_job_deadline_is_not(self):
		with patch.object(frappe.cache, "execute_command", side_effect=RuntimeError("down")):
			redis_slots.remember(self.keys[0], 60)
		with patch.object(frappe.cache, "execute_command", side_effect=JobTimeoutException()):
			with self.assertRaises(JobTimeoutException):
				redis_slots.remember(self.keys[0], 60)


class TestRoutingUsesTheSharedPrimitives(SlotCase):
	"""`routing` has no Redis code of its own: its names are thin callers of `redis_slots`."""

	def test_the_routing_names_are_the_shared_ones(self):
		self.assertIs(routing._release, redis_slots.release)
		self.assertIs(routing._remember, redis_slots.remember)
		self.assertEqual(routing._RELEASE, redis_slots.RELEASE)

	def test_the_routing_claim_and_slot_take_use_the_site_slot_ttl(self):
		with patch.object(redis_slots, "claim", return_value=True) as claim:
			self.assertTrue(routing._claim(self.keys[0], "a"))
		claim.assert_called_once_with(self.keys[0], "a", routing.SLOT_TTL_S)
		with patch.object(redis_slots, "take_slot", return_value=self.keys[1]) as take:
			self.assertEqual(routing._take_slot("a"), self.keys[1])
		take.assert_called_once_with(
			"a", [routing.slot_key(n) for n in range(routing.READER_SLOTS)], routing.SLOT_TTL_S
		)

	def test_routing_holds_no_redis_script_of_its_own(self):
		import inspect

		self.assertNotIn("redis.call", inspect.getsource(routing))
		self.assertNotIn('"SET"', inspect.getsource(routing))
