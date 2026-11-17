"""All-account balances worker (B5, B11): one source view, chunked reads, contract checks, safe failures."""

import json
from threading import Thread
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days, nowdate

from jarvis.payment_calendar import balances, jobs, snapshot, store
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site

ACTOR = "Administrator"


class TestBalancesJob(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user(ACTOR)
		self.fx = CalendarFixtures()
		self.second = self.fx.account("Bank2", "Bank", "Asset")
		self.fx.fiscal_year()
		self.fx.gl(1000)
		self.fx.gl(500, account=self.second)
		frappe.db.commit()
		self.lookup = patch.object(jobs, "get_job", return_value=None).start()
		self.enqueue = patch.object(frappe, "enqueue", return_value=object()).start()

	def tearDown(self):
		patch.stopall()
		frappe.db.rollback()
		store.purge_actor(ACTOR)
		for doctype, name in reversed(self.fx.created):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		frappe.cache.hdel("fiscal_years", self.fx.company)
		frappe.clear_document_cache("Company", self.fx.company)

	def request(self):
		return jobs.request_account_balances(self.fx.company)["job_id"]

	def run_job(self):
		job_id = self.request()
		jobs.run_account_balances(actor=ACTOR, balances_id=job_id)
		return job_id

	def failed_code(self, job_id):
		status = store.load_status(ACTOR, job_id)
		self.assertEqual(status["state"], "failed", status)
		self.assertIsNone(store.load_result(ACTOR, job_id))
		return status["code"]

	def commit_independently(self, callback):
		site, sites = frappe.local.site, frappe.local.sites_path
		errors = []

		def writer():
			try:
				frappe.init(site=site, sites_path=sites)
				frappe.connect()
				frappe.set_user(ACTOR)
				callback()
				frappe.db.commit()
			except Exception as exc:
				errors.append(type(exc).__name__)
			finally:
				frappe.destroy()

		thread = Thread(target=writer)
		thread.start()
		thread.join(30)
		self.assertFalse(thread.is_alive())
		self.assertEqual(errors, [])

	def test_the_worker_stores_every_account_and_one_server_total(self):
		job_id = self.run_job()
		self.assertEqual(store.load_status(ACTOR, job_id)["state"], "complete")
		result = store.load_result(ACTOR, job_id)
		self.assertEqual(result["total"], "1500.00")
		self.assertEqual(
			{row["account"]: row["balance"] for row in result["accounts"]},
			{self.fx.bank: "1000.00", self.second: "500.00"},
		)
		# Names, types and bank labels are added at status-read time, never stored.
		self.assertTrue(all(set(row) == {"account", "balance"} for row in result["accounts"]))
		self.assertEqual((result["currency"], result["precision"]), ("INR", 2))
		self.assertTrue(result["generated_at"])

	def test_enqueue_uses_the_long_queue_and_the_rq_id_the_status_read_looks_up(self):
		job_id = self.request()
		status = store.load_status(ACTOR, job_id)
		kwargs = self.enqueue.call_args.kwargs
		self.assertEqual(self.enqueue.call_args.args[0], "jarvis.payment_calendar.jobs.run_account_balances")
		self.assertEqual((kwargs["queue"], kwargs["timeout"]), ("long", 300))
		self.assertEqual(kwargs["job_id"], "pcal:" + status["scope_hash"] + ":" + job_id)
		self.assertEqual(kwargs["balances_id"], job_id)
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(status["kind"], "balances")

	def test_all_reads_share_one_source_view_and_one_cache_scope(self):
		with (
			patch.object(balances, "consistent_read", wraps=snapshot.consistent_read) as view,
			patch.object(snapshot, "_source_caches", wraps=snapshot._source_caches) as caches,
		):
			self.run_job()
		self.assertEqual((view.call_count, caches.call_count), (1, 1))

	def test_reads_run_in_sequential_chunks_of_fifty_as_of_today(self):
		for index in range(51):
			self.fx.account(f"Cash{index}", "Cash", "Asset")
		frappe.db.commit()
		with patch.object(balances, "read_bank_balances", wraps=balances.read_bank_balances) as read:
			job_id = self.run_job()
		self.assertEqual([len(call.args[1]) for call in read.call_args_list], [50, 3])
		self.assertEqual({call.args[2] for call in read.call_args_list}, {nowdate()})
		self.assertEqual(store.load_result(ACTOR, job_id)["total"], "1500.00")

	def test_balances_are_read_at_the_scope_anchor_not_at_the_worker_clock(self):
		status = store.load_status(ACTOR, self.request())
		anchor = add_days(nowdate(), -1)
		seen = []

		def read(company, names, as_of):
			seen.append(as_of)
			return self.fake_read()(company, names, as_of)

		with patch.object(balances, "read_bank_balances", side_effect=read):
			result = balances.collect({**status["scope"], "anchor": anchor})
		self.assertEqual(seen, [anchor])
		# The result carries the day it was read for, so a later status read can refuse a stale one.
		self.assertEqual(result["anchor"], anchor)

	def test_a_catalog_without_accounts_totals_zero(self):
		for name in (self.fx.bank, self.second):
			frappe.db.set_value("Account", name, "disabled", 1)
		frappe.db.commit()
		job_id = self.run_job()
		result = store.load_result(ACTOR, job_id)
		self.assertEqual((result["accounts"], result["total"], result["currency"]), ([], "0.00", "INR"))

	def test_a_write_inside_the_job_fails_it_as_source_changed(self):
		def write(*args):
			frappe.db.set_value("Company", self.fx.company, "abbr", "CHANGED")

		with patch.object(balances, "read_bank_balances", side_effect=write):
			job_id = self.run_job()
		self.assertEqual(self.failed_code(job_id), "source_changed")
		self.assertNotEqual(frappe.db.get_value("Company", self.fx.company, "abbr"), "CHANGED")

	def fake_read(self, *, precision=2, touch_fiscal_cache=True):
		def read(company, names, as_of):
			if touch_fiscal_cache:
				frappe.cache.hget("fiscal_years", company)
			return {
				"total": "0.00",
				"accounts": [{"account": name, "balance": "0.00"} for name in names],
				"currency": "INR",
				"precision": precision,
			}

		return read

	def test_a_missing_fiscal_year_cache_hit_is_a_contract_failure(self):
		with patch.object(balances, "read_bank_balances", self.fake_read(touch_fiscal_cache=False)):
			job_id = self.run_job()
		self.assertEqual(self.failed_code(job_id), "source_contract_unavailable")

	def test_a_precision_that_differs_from_the_source_view_is_a_contract_failure(self):
		with patch.object(balances, "read_bank_balances", self.fake_read(precision=5)):
			job_id = self.run_job()
		self.assertEqual(self.failed_code(job_id), "source_contract_unavailable")

	def test_the_contract_checks_pass_for_an_honest_read(self):
		with patch.object(balances, "read_bank_balances", self.fake_read()):
			job_id = self.run_job()
		self.assertEqual(store.load_status(ACTOR, job_id)["state"], "complete")

	def test_a_validation_error_means_scope_changed_not_internal_error(self):
		with (
			patch.object(balances, "read_bank_balances", side_effect=frappe.ValidationError("PRIVATE")),
			patch.object(frappe, "log_error") as log,
		):
			job_id = self.run_job()
		self.assertEqual(self.failed_code(job_id), "scope_changed")
		log.assert_not_called()

	def test_a_forbidden_or_failed_chunk_fails_the_whole_job(self):
		for error, code in (
			(frappe.PermissionError("PRIVATE"), "forbidden"),
			(CalendarSourceUnavailable("invalid_source_amount"), "invalid_source_amount"),
			(RuntimeError("PRIVATE"), "internal_error"),
		):
			with self.subTest(code=code):
				with (
					patch.object(balances, "read_bank_balances", side_effect=error),
					patch.object(frappe, "log_error"),
				):
					job_id = self.run_job()
				self.assertEqual(self.failed_code(job_id), code)

	def test_an_account_revoked_during_the_read_is_caught_before_publish(self):
		real = balances.read_bank_balances

		def read(*args):
			result = real(*args)
			self.commit_independently(lambda: frappe.db.set_value("Account", self.second, "disabled", 1))
			return result

		with patch.object(balances, "read_bank_balances", side_effect=read):
			job_id = self.run_job()
		self.assertEqual(self.failed_code(job_id), "scope_changed")

	def test_the_job_logs_duration_and_account_count(self):
		logger = MagicMock()
		with patch.object(frappe, "logger", return_value=logger) as factory:
			self.run_job()
		factory.assert_any_call("payment_calendar")
		message = logger.info.call_args.args[0]
		self.assertIn("duration_ms=", message)
		self.assertIn("accounts=2", message)

	def test_an_unexpected_error_logs_the_kind_and_only_non_private_detail(self):
		for error, detail in ((KeyError("bank"), "bank"), (RuntimeError("PRIVATE_SUPPLIER"), None)):
			with self.subTest(error=type(error).__name__):
				with (
					patch.object(balances, "read_bank_balances", side_effect=error),
					patch.object(frappe, "log_error") as log,
				):
					self.run_job()
				body = json.loads(log.call_args.kwargs["message"])
				self.assertEqual(body["kind"], "balances")
				self.assertEqual(body["exception_class"], type(error).__name__)
				self.assertNotIn("PRIVATE_SUPPLIER", log.call_args.kwargs["message"])
				self.assertEqual(detail is None, "detail" not in body)
				if detail:
					self.assertIn(detail, body["detail"])
				store.purge_actor(ACTOR)

	def test_a_scope_changed_or_forbidden_failure_is_logged_with_its_class_only(self):
		for error, code in (
			(frappe.ValidationError("PRIVATE"), "scope_changed"),
			(frappe.PermissionError("PRIVATE"), "forbidden"),
		):
			with self.subTest(code=code):
				logger = MagicMock()
				with (
					patch.object(balances, "read_bank_balances", side_effect=error),
					patch.object(frappe, "log_error") as log,
					patch.object(frappe, "logger", return_value=logger),
				):
					self.run_job()
				log.assert_not_called()
				message = logger.warning.call_args.args[0]
				for part in ("balances", code, type(error).__name__, "stage=sources"):
					self.assertIn(part, message)
				self.assertNotIn("PRIVATE", message)
				store.purge_actor(ACTOR)

	def test_a_job_that_takes_over_150_seconds_logs_a_slow_job_warning(self):
		for took, slow in ((149_000, False), (151_000, True)):
			with self.subTest(took=took):
				logger = MagicMock()
				with (
					patch.object(jobs, "elapsed_ms", return_value=took),
					patch.object(frappe, "logger", return_value=logger),
				):
					self.run_job()
				warned = [c.args[0] for c in logger.warning.call_args_list if "slow" in c.args[0]]
				self.assertEqual(bool(warned), slow)
				if slow:
					self.assertIn("duration_ms=151000", warned[0])
				store.purge_actor(ACTOR)

	def test_a_snapshot_id_is_never_run_as_a_balances_job(self):
		scope = {"actor": ACTOR, "company": self.fx.company, "accounts": [], "anchor": nowdate()}
		sid, _ = store.admit(ACTOR, "e" * 64, scope, "fingerprint")
		with patch.object(balances, "read_bank_balances") as read:
			jobs.run_account_balances(actor=ACTOR, balances_id=sid)
		read.assert_not_called()
		self.assertEqual(store.load_status(ACTOR, sid)["state"], "queued")

	def test_the_job_cannot_borrow_the_queued_actors_privileges(self):
		job_id = self.request()
		with patch.object(balances, "read_bank_balances") as read:
			frappe.set_user("Guest")
			jobs.run_account_balances(actor=ACTOR, balances_id=job_id)
			read.assert_not_called()
		frappe.set_user(ACTOR)
		self.assertIsNone(store.load_result(ACTOR, job_id))
