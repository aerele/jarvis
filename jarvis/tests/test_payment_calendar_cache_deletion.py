"""Payment Calendar S4b B9: the Transaction Deletion Record guard (review-B7 MINOR-1, review-B9 M1).

ERPNext sets the status of a Transaction Deletion Record with `db_set`, and `db_set` runs `on_change`
(frappe/model/document.py `db_set`), so `lifecycle.on_deletion_record` purges on Queued, Running and Completed:
`TestMainPath` proves that with a real `db_set`. Two more rules close the edges:

* the refresh preflight refuses while a record is Queued or Running (`deletion_running`, no failure count);
* the tick, `cache.purge_after_deletion`, is a BACKSTOP: it purges a company once when a submitted Completed
  record is newer than its current generation (a lost purge job, a record completed while the hooks were not
  installed, a build that finished after the event). `TestPurgeAfterCompleted` inserts rows by SQL, so it
  proves the backstop only.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_deletion
"""

from datetime import timedelta
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import cache, cache_alerts, cache_refresh, cache_state, cache_store
from jarvis.tests import test_payment_calendar_cache_status as status_tests
from jarvis.tests import test_payment_calendar_cache_tick as tick_tests
from jarvis.tests.test_payment_calendar_cache_store import GENERATION, HEAD, _sql, site_offset_s

TDR = "Transaction Deletion Record"


class DeletionCase(status_tests.StableCase):
	NAME = "PCAL-B9-TDR-2"

	def setUp(self):
		super().setUp()
		self.addCleanup(self.drop)
		self.drop()

	def drop(self):
		frappe.db.rollback()
		frappe.db.delete(TDR, {"name": ["like", "PCAL-B9-TDR-%"]})
		frappe.db.commit()

	def insert(self, status, *, company=None, seconds_ago=0, name=None, docstatus=1):
		"""A record stamped in SITE time, as Frappe stamps it (the database clock is another zone)."""
		offset = site_offset_s()
		_sql(
			f"INSERT INTO `tab{TDR}` (name, creation, modified, modified_by, owner, docstatus, company, status) "
			"VALUES (%s, now(6) + interval %s second, now(6) + interval %s second - interval %s second, "
			"'Administrator', 'Administrator', %s, %s, %s)",
			(name or self.NAME, offset, offset, seconds_ago, docstatus, company or self.company, status),
		)
		frappe.db.commit()

	def insert_in_zone(self, offset: int, seconds_ago: int):
		"""A Completed record stamped by a site whose clock is `offset` seconds ahead of the database."""
		stamp = _sql("select now(6)")[0][0] + timedelta(seconds=offset - seconds_ago)
		_sql(
			f"INSERT INTO `tab{TDR}` (name, creation, modified, modified_by, owner, docstatus, company, status) "
			"VALUES (%s, %s, %s, 'Administrator', 'Administrator', 1, %s, 'Completed')",
			(self.NAME, stamp, stamp, self.company),
		)
		frappe.db.commit()

	def in_zone(self, offset: int):
		"""Make `frappe.utils.now_datetime` read as a site whose zone is `offset` seconds from the database's."""
		return patch(
			"frappe.utils.now_datetime",
			side_effect=lambda: _sql("select now(6)")[0][0] + timedelta(seconds=offset),
		)

	def epoch(self):
		return self.head().purge_epoch


class TestPreflightRefuses(DeletionCase):
	def test_a_queued_or_running_record_is_a_running_deletion(self):
		for status, expected in (
			("Queued", True),
			("Running", True),
			("Completed", False),
			("Failed", False),
		):
			with self.subTest(status=status):
				self.drop()
				self.insert(status)
				self.assertEqual(cache_refresh._tdr_running(), expected)

	def test_the_static_skip_names_it_deletion_running(self):
		self.insert("Running")
		with patch.object(cache_refresh, "_scheduler_inactive", return_value=False):
			self.assertEqual(cache_refresh._static_skip(self.company), "deletion_running")

	def test_a_completed_record_does_not_block_the_refresh(self):
		self.insert("Completed")
		with patch.object(cache_refresh, "_scheduler_inactive", return_value=False):
			self.assertIsNone(cache_refresh._static_skip(self.company))


class TestMainPath(DeletionCase):
	"""A real `db_set` on a real record reaches `lifecycle.on_deletion_record` (the doc event of hooks.py)."""

	def record(self, status="Running"):
		self.insert(status)
		return frappe.get_doc(TDR, self.NAME)

	def epoch(self):
		return _sql(f"select max(purge_epoch) from `tab{HEAD}` where company=%s", (self.company,))[0][0]

	def test_db_set_completed_purges_the_company_through_the_doc_event(self):
		self.make_head(age=600)
		doc = self.record()
		before = self.epoch()
		doc.db_set("status", "Completed")
		self.assertEqual(self.epoch(), before + 1)
		self.assertEqual({row[0] for row in _sql(f"select state from `tab{GENERATION}`")}, {"purged"})
		frappe.db.rollback()

	def test_db_set_running_purges_too(self):
		self.make_head(age=600)
		doc = self.record("Queued")
		before = self.epoch()
		doc.db_set("status", "Running")
		self.assertEqual(self.epoch(), before + 1)
		frappe.db.rollback()

	def test_db_set_failed_purges_nothing(self):
		self.make_head(age=600)
		doc = self.record()
		before = self.epoch()
		doc.db_set("status", "Failed")
		self.assertEqual(self.epoch(), before)
		frappe.db.rollback()

	def test_the_handler_is_called_with_the_company(self):
		doc = self.record()
		with patch.object(cache_store, "purge_company") as purge:
			doc.db_set("status", "Completed")
		purge.assert_called_once_with(self.company)
		frappe.db.rollback()


class TestPurgeAcrossZones(DeletionCase):
	"""The record's `modified` is site time and the generation's `as_of` is database time: one clock for both."""

	# The generation is 600 s old. A record completed 300 s ago is newer; one completed 900 s ago is older.
	def purged(self, offset: int, seconds_ago: int) -> bool:
		self.make_head(age=600)
		before = self.epoch()
		self.insert_in_zone(offset, seconds_ago)
		with self.in_zone(offset):
			cache.purge_after_deletion()
		return self.epoch() == before + 1

	def test_a_site_ahead_of_the_database_keeps_the_generation_for_an_older_record(self):
		self.assertFalse(self.purged(5 * 3600 + 1800, 900))

	def test_a_site_ahead_of_the_database_purges_for_a_newer_record(self):
		self.assertTrue(self.purged(5 * 3600 + 1800, 300))

	def test_a_site_behind_the_database_purges_for_a_newer_record(self):
		self.assertTrue(self.purged(-3 * 3600, 300))

	def test_a_site_behind_the_database_keeps_the_generation_for_an_older_record(self):
		self.assertFalse(self.purged(-3 * 3600, 900))

	def test_one_clock_for_site_and_database_still_works(self):
		self.assertTrue(self.purged(0, 300))


class TestPurgeAfterCompleted(DeletionCase):
	def test_a_record_completed_after_the_build_purges_the_company_once(self):
		self.make_head(age=600)
		before = self.epoch()
		self.insert("Completed")
		cache.purge_after_deletion()
		head = self.head()
		self.assertEqual((head.purge_epoch, head.current_generation), (before + 1, None))
		states = {row[0] for row in _sql(f"select state from `tab{GENERATION}`")}
		self.assertEqual(states, {"purged"})
		cache.purge_after_deletion()
		self.assertEqual(self.epoch(), before + 1)

	def test_the_purge_job_is_queued_after_the_commit(self):
		self.make_head(age=600)
		self.insert("Completed")
		self.enqueue.reset_mock()
		cache.purge_after_deletion()
		self.assertEqual(self.enqueue.call_args.args, (cache_store.PURGE_METHOD,))

	def test_a_record_older_than_the_generation_changes_nothing(self):
		self.make_head(age=60)
		before = self.epoch()
		self.insert("Completed", seconds_ago=600)
		cache.purge_after_deletion()
		self.assertEqual(self.epoch(), before)
		self.assertIsNotNone(self.head().current_generation)

	def test_a_queued_or_running_record_is_left_to_the_doc_event(self):
		self.make_head(age=600)
		before = self.epoch()
		for status in ("Queued", "Running", "Failed", "Cancelled"):
			self.drop()
			self.insert(status)
			cache.purge_after_deletion()
		self.assertEqual(self.epoch(), before)

	def test_a_draft_or_cancelled_record_never_purges(self):
		self.make_head(age=600)
		before = self.epoch()
		for docstatus in (0, 2):
			with self.subTest(docstatus=docstatus):
				self.drop()
				self.insert("Completed", docstatus=docstatus)
				cache.purge_after_deletion()
				self.assertEqual(self.epoch(), before)
		self.assertIsNotNone(self.head().current_generation)

	def test_a_record_of_another_company_changes_nothing(self):
		self.make_head(age=600)
		before = self.epoch()
		self.insert("Completed", company="PCAL-B9-OTHER")
		cache.purge_after_deletion()
		self.assertEqual(self.epoch(), before)

	def test_a_company_with_no_current_generation_is_not_bumped_again(self):
		self.make_head(age=600)
		_sql(f"update `tab{HEAD}` set current_generation=NULL")
		frappe.db.commit()
		before = self.epoch()
		self.insert("Completed")
		cache.purge_after_deletion()
		self.assertEqual(self.epoch(), before)

	def test_a_database_error_rolls_back_and_propagates(self):
		self.make_head(age=600)
		self.insert("Completed")

		def bump_then_fail(company):
			cache_store.bump_epoch(company)
			raise frappe.QueryDeadlockError

		with (
			patch.object(cache_store, "purge_company", side_effect=bump_then_fail),
			patch.object(frappe.db, "rollback", wraps=frappe.db.rollback) as rolled,
			self.assertRaises(frappe.QueryDeadlockError),
		):
			cache.purge_after_deletion()
		rolled.assert_called()


class TestTickCallsIt(DeletionCase):
	def setUp(self):
		super().setUp()
		self.start(patch.object(cache_alerts, "alerts"))

	def test_the_tick_purges_after_a_completed_record(self):
		self.make_head(age=600)
		before = self.epoch()
		self.insert("Completed")
		cache.tick()
		self.assertEqual(self.epoch(), before + 1)

	def test_an_error_in_it_does_not_stop_the_enqueues_of_the_tick(self):
		self.make_head(age=3000)
		with patch.object(cache, "purge_after_deletion", side_effect=ValueError(self.company)):
			cache.tick()
		self.assertTrue(self.enqueue.called)

	def test_with_the_switch_off_the_tick_does_not_look_at_records(self):
		self.settings = cache_state.CacheSettings("off", (), False)
		with patch.object(cache, "purge_after_deletion") as purge:
			cache.tick()
		purge.assert_not_called()
