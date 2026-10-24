"""Real Redis isolation, admission and late-publication tests; no ledger writes."""

import json
import threading
from copy import deepcopy
from hashlib import sha256
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from jarvis.payment_calendar import store
from jarvis.tests.payment_calendar_fixtures import require_calendar_test_site


class TestPaymentCalendarStore(TestCase):
	def setUp(self):
		require_calendar_test_site()
		self.actor = f"calendar-store-{uuid4().hex}@example.com"
		self.other = f"calendar-store-{uuid4().hex}@example.com"
		self.scope = {
			"actor": self.actor,
			"company": "Calendar fixture",
			"accounts": [],
			"anchor": "2026-10-24",
		}
		self.digest = sha256(uuid4().bytes).hexdigest()
		self.payload = {"summary": {"book_cash": "100.00"}, "rows": {"Payable": [], "Receivable": []}}
		self.unrelated = frappe.cache.make_key("calendar-test-unrelated:" + uuid4().hex)
		frappe.cache.set(self.unrelated, b"keep")

	def tearDown(self):
		store.purge_actor(self.actor)
		store.purge_actor(self.other)
		frappe.cache.delete(self.unrelated)

	def admit(self, actor=None, digest=None):
		actor = actor or self.actor
		return store.admit(
			actor, digest or self.digest, {**self.scope, "actor": actor}, "permission-fingerprint"
		)

	def complete(self, actor=None, digest=None):
		actor = actor or self.actor
		sid, created = self.admit(actor, digest)
		self.assertTrue(created)
		self.assertTrue(store.mark_running(actor, sid))
		self.assertTrue(store.publish(actor, sid, self.payload))
		return sid

	def test_repeated_admission_joins_one_active_scope(self):
		sid, first = self.admit()
		second, created = self.admit()
		self.assertTrue(first)
		self.assertFalse(created)
		self.assertEqual(second, sid)
		self.assertEqual(store.load_status(self.actor, sid)["state"], "queued")

	def test_lua_state_changes_preserve_an_empty_account_array(self):
		sid = self.complete()
		self.assertEqual(store.load_status(self.actor, sid)["scope"]["accounts"], [])

	def test_watchdog_cache_clear_preserves_state_and_purge_bookkeeping(self):
		sid = self.complete()
		frappe.clear_cache()
		self.assertEqual(store.load_result(self.actor, sid), self.payload)
		self.assertTrue(frappe.cache.execute_command("EXISTS", store._index(store._owner(self.actor))))
		store.purge_actor(self.actor)
		self.assertIsNone(store.load_result(self.actor, sid))

	def test_two_active_jobs_per_actor_are_bounded_but_identical_requests_join(self):
		first, _ = self.admit()
		self.admit(digest=sha256(b"second").hexdigest())
		with self.assertRaises(store.ActorBusy):
			self.admit(digest=sha256(b"third").hexdigest())
		self.assertEqual(self.admit()[0], first)

	def test_admission_prunes_expired_completed_index_members(self):
		sid = self.complete()
		frappe.cache.delete(store._key(self.actor, sid, "status"), store._key(self.actor, sid, "result"))
		self.admit(digest=sha256(b"new").hexdigest())
		members = frappe.cache.execute_command("SMEMBERS", store._index(store._owner(self.actor)))
		self.assertFalse(any(sid.encode() in member for member in members))

	def test_concurrent_admission_has_one_winner(self):
		site, sites_path = frappe.local.site, frappe.local.sites_path
		barrier = threading.Barrier(8)
		results, failures = [], []

		def contender():
			try:
				frappe.init(site=site, sites_path=sites_path)
				barrier.wait(10)
				results.append(
					store.admit(self.actor, self.digest, deepcopy(self.scope), "permission-fingerprint")
				)
			except Exception as exc:
				failures.append(type(exc).__name__)
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=contender) for _ in range(8)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(15)
		self.assertFalse(any(t.is_alive() for t in threads))
		self.assertEqual(failures, [])
		self.assertEqual(len({sid for sid, _ in results}), 1)
		self.assertEqual(sum(created for _, created in results), 1)

	def test_cross_user_and_unknown_ids_have_identical_missing_results(self):
		sid = self.complete()
		self.assertIsNone(store.load_status(self.other, sid))
		self.assertIsNone(store.load_result(self.other, sid))
		self.assertIsNone(store.load_result(self.actor, uuid4().hex))

	def test_complete_result_is_bounded_to_five_minutes(self):
		sid = self.complete()
		self.assertEqual(store.load_result(self.actor, sid), self.payload)
		for kind in ("status", "result"):
			self.assertGreater(frappe.cache.ttl(store._key(self.actor, sid, kind)), 0)
			self.assertLessEqual(frappe.cache.ttl(store._key(self.actor, sid, kind)), 300)

	def test_late_publish_after_purge_cannot_resurrect_financial_data(self):
		sid, _ = self.admit()
		self.assertTrue(store.mark_running(self.actor, sid))
		store.purge_actor(self.actor)
		self.assertFalse(store.publish(self.actor, sid, self.payload))
		self.assertIsNone(store.load_status(self.actor, sid))
		self.assertIsNone(store.load_result(self.actor, sid))

	def test_queued_expiry_cannot_be_recreated_by_worker_start(self):
		sid, _ = self.admit()
		frappe.cache.expire(store._key(self.actor, sid, "status"), 0)
		self.assertFalse(store.mark_running(self.actor, sid))
		self.assertFalse(store.publish(self.actor, sid, self.payload))
		self.assertIsNone(store.load_result(self.actor, sid))

	def test_purge_releases_admission_even_if_the_status_was_evicted(self):
		old, _ = self.admit()
		frappe.cache.expire(store._key(self.actor, old, "status"), 0)
		store.purge_actor(self.actor)
		new, created = self.admit()
		self.assertTrue(created)
		self.assertNotEqual(old, new)

	def test_same_database_site_alias_cannot_deliver_another_site_snapshot(self):
		sid = self.complete()
		with patch.object(frappe.local, "site", "calendar-other-site"):
			self.assertIsNone(store.load_status(self.actor, sid))
			self.assertIsNone(store.load_result(self.actor, sid))

	def test_a_completed_refresh_gets_a_new_id(self):
		before = self.complete()
		after, created = self.admit()
		self.assertTrue(created)
		self.assertNotEqual(after, before)
		self.assertIsNone(store.load_result(self.actor, after))

	def test_old_run_cannot_overwrite_or_fail_a_new_run(self):
		old = self.complete()
		new, _ = self.admit()
		self.assertFalse(store.publish(self.actor, old, {"summary": {"book_cash": "999.00"}}))
		self.assertFalse(store.fail(self.actor, old, "timeout"))
		self.assertEqual(store.load_status(self.actor, new)["state"], "queued")
		self.assertEqual(store.load_result(self.actor, old), self.payload)

	def test_purge_removes_only_feature_data_for_that_actor(self):
		mine = self.complete()
		other = self.complete(self.other, sha256(uuid4().bytes).hexdigest())
		store.purge_actor(self.actor)
		self.assertIsNone(store.load_result(self.actor, mine))
		self.assertEqual(store.load_result(self.other, other), self.payload)
		self.assertEqual(frappe.cache.get(self.unrelated), b"keep")

	def test_workspace_purge_preserves_unrelated_cache(self):
		sid = self.complete()
		store.purge_site()
		self.assertIsNone(store.load_result(self.actor, sid))
		self.assertEqual(frappe.cache.get(self.unrelated), b"keep")

	def test_unknown_failure_text_is_never_stored_as_a_cause(self):
		for cause in ("PRIVATE_PARTY_AND_AMOUNT_123", {"text": "PRIVATE_PARTY_AND_AMOUNT_123"}, None):
			with self.subTest(cause=type(cause).__name__):
				sid, _ = self.admit()
				self.assertTrue(store.fail(self.actor, sid, cause))
				status = store.load_status(self.actor, sid)
				self.assertEqual(status["state"], "failed")
				self.assertEqual(status["code"], "internal_error")
				self.assertNotIn("PRIVATE_PARTY", json.dumps(status))

	def test_actor_mismatch_is_refused_before_admission(self):
		with self.assertRaises(ValueError):
			store.admit(self.other, self.digest, self.scope, "permission-fingerprint")

	def test_result_eviction_never_becomes_an_empty_financial_result(self):
		sid = self.complete()
		frappe.cache.expire(store._key(self.actor, sid, "result"), 0)
		self.assertIsNone(store.load_result(self.actor, sid))
