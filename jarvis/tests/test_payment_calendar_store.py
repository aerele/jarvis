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
from jarvis.payment_calendar.errors import SnapshotExpired
from jarvis.tests.payment_calendar_fixtures import require_calendar_test_site
from jarvis.tests.payment_calendar_results import SCOPE, schema2_result


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
		# A balances job stores `payload` as one key; a snapshot stores `parts` and reads back as `assembled`.
		self.payload = {"summary": {"book_cash": "100.00"}, "rows": {"Payable": [], "Receivable": []}}
		self.parts = store.split_result(schema2_result(), SCOPE)
		self.assembled = store.assemble_result(self.parts)
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
		self.assertTrue(store.publish_snapshot(actor, sid, self.parts))
		return sid

	def stored_keys(self, actor, sid):
		"""Every key a result of this id could be stored under: the single result key, each part and each hash."""
		return [
			store._key(actor, sid, "result"),
			*(store._part_key(actor, sid, p) for p in store.PARTS),
			*(store._key(actor, sid, name) for name in store.HASHES),
		]

	def assert_nothing_stored(self, actor, sid):
		for key in self.stored_keys(actor, sid):
			self.assertFalse(frappe.cache.execute_command("EXISTS", key), key)

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
		self.assertEqual(store.load_result(self.actor, sid), self.assembled)
		self.assertTrue(frappe.cache.execute_command("EXISTS", store._index(store._owner(self.actor))))
		store.purge_actor(self.actor)
		self.assertIsNone(store.load_result(self.actor, sid))
		self.assert_nothing_stored(self.actor, sid)

	def test_two_active_jobs_per_actor_are_bounded_but_identical_requests_join(self):
		first, _ = self.admit()
		self.admit(digest=sha256(b"second").hexdigest())
		with self.assertRaises(store.ActorBusy):
			self.admit(digest=sha256(b"third").hexdigest())
		self.assertEqual(self.admit()[0], first)

	def balances(self, digest=None, actor=None):
		actor = actor or self.actor
		return store.admit(
			actor,
			digest or sha256(uuid4().bytes).hexdigest(),
			{**self.scope, "actor": actor},
			"permission-fingerprint",
			kind="balances",
		)

	def test_status_rows_carry_their_kind(self):
		snapshot_id, _ = self.admit()
		balances_id, _ = self.balances()
		self.assertEqual(store.load_status(self.actor, snapshot_id)["kind"], "snapshot")
		self.assertEqual(store.load_status(self.actor, balances_id)["kind"], "balances")

	def test_unknown_kind_is_refused_before_admission(self):
		with self.assertRaises(ValueError):
			store.admit(self.actor, self.digest, self.scope, "permission-fingerprint", kind="other")

	def test_a_running_balances_job_never_takes_a_snapshot_slot(self):
		balances_id, _ = self.balances()
		self.assertTrue(store.mark_running(self.actor, balances_id))
		self.admit()
		self.admit(digest=sha256(b"second").hexdigest())
		with self.assertRaises(store.ActorBusy):
			self.admit(digest=sha256(b"third").hexdigest())

	def test_two_active_snapshots_never_block_a_balances_job(self):
		self.admit()
		self.admit(digest=sha256(b"second").hexdigest())
		sid, created = self.balances()
		self.assertTrue(created)
		self.assertTrue(sid)

	def test_a_second_different_balances_job_is_busy_but_an_identical_one_joins(self):
		digest = sha256(b"balances").hexdigest()
		first, _ = self.balances(digest)
		with self.assertRaises(store.ActorBusy):
			self.balances()
		again, created = self.balances(digest)
		self.assertEqual((again, created), (first, False))

	def test_a_completed_balances_job_frees_its_slot(self):
		sid, _ = self.balances()
		self.assertTrue(store.mark_running(self.actor, sid))
		self.assertTrue(store.publish(self.actor, sid, self.payload))
		self.assertTrue(self.balances()[1])

	def test_a_failed_balances_job_frees_its_slot(self):
		sid, _ = self.balances()
		self.assertTrue(store.fail(self.actor, sid, "timeout"))
		self.assertTrue(self.balances()[1])

	def test_a_row_without_a_kind_counts_as_a_snapshot(self):
		first, _ = self.admit()
		second, _ = self.admit(digest=sha256(b"second").hexdigest())
		for sid in (first, second):
			key = store._key(self.actor, sid, "status")
			row = store._read(key)
			del row["kind"]
			frappe.cache.execute_command("SET", key, store._json(row), "EX", 300)
		with self.assertRaises(store.ActorBusy):
			self.admit(digest=sha256(b"third").hexdigest())
		self.assertTrue(self.balances()[1])

	def test_purge_removes_a_balances_job_its_result_and_its_active_lock(self):
		digest = sha256(b"balances-purge").hexdigest()
		sid, _ = self.balances(digest)
		self.assertTrue(store.mark_running(self.actor, sid))
		running_lock = store._active(digest)
		self.assertTrue(frappe.cache.execute_command("EXISTS", running_lock))
		store.purge_actor(self.actor)
		self.assertFalse(frappe.cache.execute_command("EXISTS", running_lock))
		self.assertIsNone(store.load_status(self.actor, sid))
		done, _ = self.balances(digest)
		self.assertTrue(store.mark_running(self.actor, done))
		self.assertTrue(store.publish(self.actor, done, self.payload))
		store.purge_actor(self.actor)
		self.assertIsNone(store.load_status(self.actor, done))
		self.assertIsNone(store.load_result(self.actor, done))
		self.assertFalse(store.was_discarded(self.actor, done))

	def test_discard_removes_a_balances_job_and_its_result(self):
		sid, _ = self.balances()
		store.mark_running(self.actor, sid)
		store.publish(self.actor, sid, self.payload)
		store.discard(self.actor, sid)
		self.assertIsNone(store.load_status(self.actor, sid))
		self.assertIsNone(store.load_result(self.actor, sid))
		self.assertFalse(store.was_discarded(self.actor, sid))

	def test_admission_prunes_expired_completed_index_members(self):
		sid = self.complete()
		frappe.cache.delete(store._key(self.actor, sid, "status"), *self.stored_keys(self.actor, sid))
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
		for actor, name in ((self.other, sid), (self.actor, uuid4().hex)):
			with self.assertRaises(SnapshotExpired):
				store.load_part(actor, name, "status")

	def test_complete_result_is_bounded_to_five_minutes(self):
		sid = self.complete()
		self.assertEqual(store.load_result(self.actor, sid), self.assembled)
		keys = [store._key(self.actor, sid, "status"), *self.stored_keys(self.actor, sid)[1:]]
		for key in keys:
			self.assertGreater(frappe.cache.ttl(key), 0)
			self.assertLessEqual(frappe.cache.ttl(key), 300)

	def test_late_publish_after_purge_cannot_resurrect_financial_data(self):
		sid, _ = self.admit()
		self.assertTrue(store.mark_running(self.actor, sid))
		store.purge_actor(self.actor)
		self.assertFalse(store.publish_snapshot(self.actor, sid, self.parts))
		self.assertIsNone(store.load_status(self.actor, sid))
		self.assertIsNone(store.load_result(self.actor, sid))
		self.assert_nothing_stored(self.actor, sid)

	def test_queued_expiry_cannot_be_recreated_by_worker_start(self):
		sid, _ = self.admit()
		frappe.cache.expire(store._key(self.actor, sid, "status"), 0)
		self.assertFalse(store.mark_running(self.actor, sid))
		self.assertFalse(store.publish_snapshot(self.actor, sid, self.parts))
		self.assertIsNone(store.load_result(self.actor, sid))
		self.assert_nothing_stored(self.actor, sid)

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
			for part in store.PARTS:
				with self.assertRaises(SnapshotExpired):
					store.load_part(self.actor, sid, part)

	def test_same_actor_and_snapshot_id_are_isolated_between_database_namespaces(self):
		namespace = {"db_name": "calendar-other-" + uuid4().hex}
		other_parts = store.split_result(schema2_result(currency="USD"), SCOPE)
		other_assembled = store.assemble_result(other_parts)
		with patch.object(store, "token_hex", return_value=uuid4().hex):
			sid, _ = self.admit()
			key = store._key(self.actor, sid, "status")
			try:
				with patch.dict(frappe.local.conf, namespace):
					self.assertNotEqual(store._key(self.actor, sid, "status"), key)
					self.assertIsNone(store.load_status(self.actor, sid))
					self.assertIsNone(store.load_result(self.actor, sid))
					other_sid, created = self.admit()
					self.assertTrue(created)
					self.assertEqual(other_sid, sid)
					self.assertTrue(store.mark_running(self.actor, sid))
					self.assertTrue(store.publish_snapshot(self.actor, sid, other_parts))
				self.assertEqual(store.load_status(self.actor, sid)["state"], "queued")
				self.assertTrue(store.mark_running(self.actor, sid))
				self.assertTrue(store.publish_snapshot(self.actor, sid, self.parts))
				with patch.dict(frappe.local.conf, namespace):
					self.assertEqual(store.load_result(self.actor, sid), other_assembled)
					store.purge_actor(self.actor)
					self.assertIsNone(store.load_result(self.actor, sid))
					self.assert_nothing_stored(self.actor, sid)
				self.assertEqual(store.load_result(self.actor, sid), self.assembled)
				with patch.dict(frappe.local.conf, namespace):
					self.complete()
					store.purge_site()
					self.assertIsNone(store.load_result(self.actor, sid))
					self.assert_nothing_stored(self.actor, sid)
				self.assertEqual(store.load_result(self.actor, sid), self.assembled)
			finally:
				with patch.dict(frappe.local.conf, namespace):
					store.purge_actor(self.actor)

	def test_a_completed_refresh_gets_a_new_id(self):
		before = self.complete()
		after, created = self.admit()
		self.assertTrue(created)
		self.assertNotEqual(after, before)
		self.assertIsNone(store.load_result(self.actor, after))
		self.assert_nothing_stored(self.actor, after)

	def test_old_run_cannot_overwrite_or_fail_a_new_run(self):
		old = self.complete()
		new, _ = self.admit()
		overwrite = store.split_result(schema2_result(currency="USD"), SCOPE)
		self.assertFalse(store.publish_snapshot(self.actor, old, overwrite))
		self.assertFalse(store.fail(self.actor, old, "timeout"))
		self.assertEqual(store.load_status(self.actor, new)["state"], "queued")
		self.assertEqual(store.load_result(self.actor, old), self.assembled)
		self.assert_nothing_stored(self.actor, new)

	def test_purge_removes_only_feature_data_for_that_actor(self):
		mine = self.complete()
		other = self.complete(self.other, sha256(uuid4().bytes).hexdigest())
		store.purge_actor(self.actor)
		self.assertIsNone(store.load_result(self.actor, mine))
		self.assert_nothing_stored(self.actor, mine)
		self.assertEqual(store.load_result(self.other, other), self.assembled)
		for part in store.PARTS:
			self.assertEqual(store.load_part(self.other, other, part), self.parts[part])
		self.assertEqual(frappe.cache.get(self.unrelated), b"keep")

	def test_workspace_purge_preserves_unrelated_cache(self):
		sid = self.complete()
		store.purge_site()
		self.assertIsNone(store.load_result(self.actor, sid))
		self.assert_nothing_stored(self.actor, sid)
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
		for part in ("status", "payable"):
			with self.subTest(evicted=part):
				sid = self.complete(digest=sha256(part.encode() + uuid4().bytes).hexdigest())
				frappe.cache.expire(store._part_key(self.actor, sid, part), 0)
				self.assertIsNone(store.load_result(self.actor, sid))
				with self.assertRaises(SnapshotExpired):
					store.load_part(self.actor, sid, part)

	def test_public_status_names_the_id_by_the_callers_key(self):
		sid, _ = self.admit()
		status = store.load_status(self.actor, sid)
		times = ("queued_at", "started_at", "completed_at", "expires_at")
		snapshot_view = store.public_status(status)
		self.assertEqual(snapshot_view["snapshot_id"], sid)
		self.assertEqual(set(snapshot_view), {"snapshot_id", "state", "code", *times})
		job_view = store.public_status(status, "job_id")
		self.assertEqual(job_view["job_id"], sid)
		self.assertEqual(set(job_view), {"job_id", "state", "code", *times})
		self.assertEqual(job_view["state"], "queued")
