"""Payment Calendar S4b B9 (AC-E3, review-B4 MINOR-6): one real refresh through a real RQ queue.

Nothing in the other cache tests runs these parts together: a real `frappe.enqueue` (pickled arguments, job id,
dedupe), a real RQ worker burst (`execute_job`, which opens its own connection and commits), the real reader in
a real `consistent_read()` view with `refresh_caps`, `publish`, `release_claim`, the night oracle and its
persistence. The worker runs IN this process (an RQ `SimpleWorker`), so the patches below reach it; the job
commits its rows through its own connection, so these tests commit and clean up what they make.

What is patched (never the cache code itself): the switch (not committed), the lane and night slot count (the
test queue is not the configured lane), the scheduler check, the pre-warm window, and the queue list (so that
the private test queue is accepted). The queue is private, so no real worker takes the job. The preflight's
query-hook check is NOT patched: ERPNext's own Company Restriction hook on Customer and Supplier passes it
(errata E6). Not covered here: the multi-iteration chain of AC-E3 (3 iterations, 2 skip-only, 1 hop) over real
RQ; the fake-clock tests cover its logic and the T0 bench rehearsal covers it for real.

Run ONLY on the test site, alone (the tests clear the three cache tables):
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_e2e
"""

import secrets
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.background_jobs import generate_qname, get_queues_timeout, get_redis_conn
from rq import Queue, SimpleWorker
from rq.job import Job

from jarvis.payment_calendar import cache, cache_refresh, cache_shadow, cache_state, read_log, routing
from jarvis.tests.test_payment_calendar_cache_store import GENERATION, HEAD, ROW

SIDE = "Receivable"
REAL_BENCH_SLOTS = cache_state.bench_slots


def buildable_company() -> str:
	"""The newest Company that has a Receivable account: the real reader needs one to build the side.

	The site is shared and its companies are made by other tests, so no name is fixed here.
	"""
	found = frappe.db.sql(
		"SELECT c.name FROM tabCompany c WHERE EXISTS (SELECT 1 FROM tabAccount a WHERE a.company=c.name "
		"AND a.account_type='Receivable') ORDER BY c.creation DESC LIMIT 1"
	)
	if not found:
		raise unittest.SkipTest("no Company with a Receivable account on this site")
	return found[0][0]


class TestRealRqRefresh(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for doctype in (HEAD, GENERATION, ROW):
			frappe.reload_doc("jarvis", "doctype", frappe.scrub(doctype))
		cls.company = buildable_company()
		cls.site = frappe.local.site

	def setUp(self):
		frappe.set_user("Administrator")
		self.digest = read_log.company_digest(self.company)
		self.queue_name = f"jarvis_pcal_e2e_{secrets.token_hex(4)}"
		self.connection = get_redis_conn()
		self.queue = Queue(generate_qname(self.queue_name), connection=self.connection)
		self.settings = cache_state.CacheSettings("shadow", (self.company,), False)
		self.addCleanup(self.clean)
		self.clean()
		queues = {**get_queues_timeout(), self.queue_name: 1800}
		for patcher in (
			patch("frappe.utils.background_jobs.get_queues_timeout", return_value=queues),
			patch.object(cache_state, "cache_settings", side_effect=lambda **_: self.settings),
			patch.object(cache_state, "bench_slots", return_value=1),
			patch.object(cache_state, "lane_live", return_value=True),
			patch.object(cache_state, "LANE_QUEUE", self.queue_name),
			patch.object(cache_refresh, "_scheduler_inactive", return_value=False),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		cache.reset_lane_memo()

	real_offenders = staticmethod(cache_refresh.query_hook_offenders)

	def clean(self):
		self.reconnect()
		frappe.db.rollback()
		for doctype in (ROW, GENERATION, HEAD):
			frappe.db.sql(f"delete from `tab{doctype}`")
		frappe.db.commit()
		frappe.cache.execute_command(
			"DEL", frappe.cache.make_key(f"jarvis:pcal:cache:night:{self.digest}:{SIDE}:{routing.today()}")
		)
		self.queue.empty()

	def reconnect(self):
		"""`execute_job` destroys the local context when it ends; a test that goes on needs a new one."""
		if not getattr(frappe.local, "site", None):
			frappe.init(self.site, force=True)
			frappe.connect()
		frappe.set_user("Administrator")

	def burst(self):
		"""Run every job of the private queue to its end, in this process."""
		SimpleWorker([self.queue], connection=self.connection).work(burst=True)
		self.reconnect()

	def head_and_generation(self):
		frappe.db.rollback()
		head = frappe.db.sql(
			f"select current_generation, claim_token, hop_seq, last_failure, last_duration_ms from `tab{HEAD}`",
			as_dict=True,
		)
		generation = frappe.db.sql(
			f"select name, state, outcome, row_count, shadow, failure_class from `tab{GENERATION}` order by name",
			as_dict=True,
		)
		return head, generation

	def test_a_day_refresh_goes_through_the_queue_and_publishes_a_current_generation(self):
		self.assertEqual(cache.enqueue_side(self.company, SIDE, opened=True), "queued")
		self.assertEqual(self.queue.count, 1)
		self.burst()
		self.assertEqual(self.queue.count, 0)
		job = Job.fetch(self._job_id(), connection=self.connection)
		self.assertEqual(job.get_status(), "finished", job.exc_info)
		self.assertEqual(job.return_value().outcome, "swapped")
		heads, generations = self.head_and_generation()
		self.assertEqual(len(heads), 1)
		self.assertEqual([g.state for g in generations], ["current"])
		self.assertEqual(heads[0].current_generation, generations[0].name)
		self.assertIsNone(heads[0].claim_token)
		self.assertEqual(heads[0].hop_seq, 1)
		self.assertIsNone(heads[0].last_failure)
		self.assertIsNone(generations[0].shadow)

	def test_a_night_unit_goes_through_the_queue_and_stores_the_oracle_record(self):
		real = frappe.enqueue

		def to_private_queue(*args, **kwargs):
			return real(*args, **{**kwargs, "queue": self.queue_name})

		with (
			patch.object(cache, "in_prewarm_window", return_value=True),
			patch.object(frappe, "enqueue", side_effect=to_private_queue),
		):
			self.assertEqual(cache.enqueue_side(self.company, SIDE, night=True), "queued")
			self.burst()
		job = Job.fetch(self._job_id(), connection=self.connection)
		self.assertEqual(job.get_status(), "finished", job.exc_info)
		self.assertEqual(job.return_value().outcome, "swapped")
		_, generations = self.head_and_generation()
		self.assertEqual([g.state for g in generations], ["current"])
		stored = frappe.parse_json(generations[0].shadow)
		self.assertIn(stored["outcome"], cache_shadow.OUTCOMES)
		self.assertEqual(stored["cache_build"], routing.cache_build()[:12])
		self.assertNotIn(self.company, generations[0].shadow)
		self.assertEqual(job.return_value().oracle["outcome"], stored["outcome"])

	def test_a_bench_with_one_long_worker_and_no_lane_builds_a_night_generation(self):
		"""E8-1: no lane, one worker on `long`: the real slot rule gives the night unit its one slot, the unit goes
		to the `long` queue (the private queue stands in for it, so that no real worker takes the job) and
		the one worker swaps a generation. By day the same bench queues nothing."""
		real = frappe.enqueue
		queues = []

		def to_private_queue(*args, **kwargs):
			queues.append(kwargs["queue"])
			return real(*args, **{**kwargs, "queue": self.queue_name})

		suffix = secrets.token_hex(5)
		slot_prefix = f"jarvis:pcal:test:cache_e2e:{suffix}:slot:"
		self.addCleanup(lambda: frappe.cache.execute_command("DEL", cache_state.slot_key(0)))
		with (
			patch.object(cache_state, "SLOT_PREFIX", slot_prefix),
			patch.object(cache_state, "bench_slots", REAL_BENCH_SLOTS),
			patch.object(cache_state, "lane_workers", return_value=0),
			patch.object(cache_state, "long_workers", return_value=1),
			patch.object(cache_state, "lane_live", return_value=False),
			patch.object(frappe, "enqueue", side_effect=to_private_queue),
		):
			self.assertEqual(cache.enqueue_side(self.company, SIDE, opened=True), "busy")
			self.assertEqual(self.queue.count, 0)
			with patch.object(cache, "in_prewarm_window", return_value=True):
				self.assertEqual(cache.enqueue_side(self.company, SIDE, night=True), "queued")
				self.burst()
			self.assertFalse(frappe.cache.execute_command("EXISTS", cache_state.slot_key(0)))
		self.assertEqual(queues, ["long"])
		job = Job.fetch(self._job_id(), connection=self.connection)
		self.assertEqual(job.get_status(), "finished", job.exc_info)
		self.assertEqual(job.return_value().outcome, "swapped")
		_, generations = self.head_and_generation()
		self.assertEqual([g.state for g in generations], ["current"])

	def test_a_second_enqueue_of_the_same_hop_is_dropped_while_it_waits(self):
		self.assertEqual(cache.enqueue_side(self.company, SIDE), "queued")
		self.assertEqual(cache.enqueue_side(self.company, SIDE), "busy")
		self.assertEqual(self.queue.count, 1)

	def test_the_job_arguments_hold_the_digest_and_no_company_name(self):
		cache.enqueue_side(self.company, SIDE)
		job = self.queue.jobs[0]
		self.assertNotIn(self.company, repr(job.kwargs) + job.id)
		self.assertIn(self.digest, job.id)

	def test_the_preflight_accepts_the_erpnext_company_restriction_hooks(self):
		"""ERPNext 16.55 puts `company_restriction.get_permission_query_conditions` on Customer and Supplier. It is
		audited (`eligibility.AUDITED_HOOKS`), so the preflight does not refuse it (errata E6)."""
		self.assertEqual(self.real_offenders(["Customer", "Supplier"]), [])

	def _job_id(self):
		return f"{self.site}||pcal-cache|{self.digest}|{SIDE}|0"
