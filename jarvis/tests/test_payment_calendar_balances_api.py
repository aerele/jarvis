"""All-account balances API (B5, AB7): gate, kinds, revalidation on every read, never a partial total."""

import json
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from jarvis.exceptions import PermissionDeniedError
from jarvis.payment_calendar import api, catalog, store
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site

NUMBER = "50100012343281"


class TestBalancesAPI(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_balances_api")
		self.fx = CalendarFixtures()
		self.second = self.fx.account("Bank2", "Bank", "Asset")
		self.user = self.fx.user()
		frappe.get_doc("User", self.user).add_roles("Jarvis User")
		frappe.set_user(self.user)
		self.lookup = patch("jarvis.payment_calendar.jobs.get_job", return_value=None).start()
		self.enqueue = patch.object(frappe, "enqueue", return_value=object()).start()

	def tearDown(self):
		patch.stopall()
		store.purge_actor(self.user)
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_balances_api")

	def request(self, company=None):
		return api.request_account_balances(company or self.fx.company)

	def job(self, accounts=None, *, digest="b" * 64, complete=True, total=None, balances=None, anchor=None):
		"""A balances job written straight into the store, as the worker leaves it."""
		accounts = accounts or [self.fx.bank, self.second]
		scope = {"actor": self.user, "company": self.fx.company, "accounts": sorted(accounts)}
		sid, _ = store.admit(self.user, digest, scope, "", kind="balances")
		if not complete:
			return sid
		rows = [{"account": name, "balance": (balances or {}).get(name, "100.00")} for name in accounts]
		store.mark_running(self.user, sid)
		store.publish(
			self.user,
			sid,
			{
				"generated_at": "2026-11-11T05:12:00+00:00",
				"anchor": anchor or nowdate(),
				"currency": "INR",
				"precision": 2,
				"accounts": rows,
				"total": total or f"{100 * len(rows)}.00",
			},
		)
		return sid

	def catalog_reads(self, read):
		"""The calls that list the account catalog (the label read lists Accounts by name instead)."""
		return [c for c in read.call_args_list if "account_type" in c.kwargs.get("filters", {})]

	def as_admin(self, callback):
		frappe.set_user("Administrator")
		try:
			return callback()
		finally:
			frappe.set_user(self.user)

	def test_request_returns_a_queued_job_with_the_permitted_catalog(self):
		result = self.request()
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["data"]["state"], "queued")
		status = store.load_status(self.user, result["data"]["job_id"])
		self.assertEqual(status["kind"], "balances")
		self.assertEqual(status["scope"]["accounts"], sorted([self.fx.bank, self.second]))
		self.assertEqual(status["scope"]["company"], self.fx.company)

	def test_a_user_without_the_payable_report_is_forbidden(self):
		self.as_admin(lambda: frappe.get_doc("User", self.user).remove_roles("Accounts Manager"))
		self.as_admin(lambda: frappe.get_doc("User", self.user).add_roles("HR Manager"))
		result = self.request()
		self.assertEqual((result["ok"], result["code"]), (False, "forbidden"))
		# Role changes enqueue framework jobs of their own; only the balances job matters here.
		self.assertFalse([c for c in self.enqueue.call_args_list if "run_account_balances" in c.args[0]])

	def test_the_gate_asks_for_the_payable_report_of_this_company(self):
		with patch.object(catalog, "require_report", side_effect=frappe.PermissionError) as gate:
			result = self.request()
		gate.assert_called_once_with(self.fx.company, "Payable")
		self.assertEqual(result["code"], "forbidden")
		self.enqueue.assert_not_called()

	def test_more_than_five_hundred_accounts_is_too_large(self):
		real = frappe.get_list

		def get_list(doctype, *args, **kwargs):
			if doctype == "Account":
				return [frappe._dict(name=f"Cash {i}", account_name=f"Cash {i}") for i in range(501)]
			return real(doctype, *args, **kwargs)

		with patch("frappe.get_list", side_effect=get_list):
			result = self.request()
		self.assertEqual((result["ok"], result["code"]), (False, "too_large"))
		self.enqueue.assert_not_called()

	def test_the_job_covers_only_accounts_the_user_may_total(self):
		real = catalog.validate_bank_accounts

		def validate(company, names):
			if self.second in names:
				raise PermissionDeniedError("PRIVATE")
			return real(company, names)

		with patch.object(catalog, "validate_bank_accounts", side_effect=validate):
			result = self.request()
		status = store.load_status(self.user, result["data"]["job_id"])
		self.assertEqual(status["scope"]["accounts"], [self.fx.bank])

	def test_an_identical_request_joins_and_a_different_one_is_busy(self):
		first = self.request()["data"]["job_id"]
		self.assertEqual(self.request()["data"]["job_id"], first)
		self.assertEqual(self.enqueue.call_count, 1)
		other = self.as_admin(CalendarFixtures)
		busy = self.request(other.company)
		self.assertEqual((busy["ok"], busy["code"], busy["state"]), (False, "worker_busy", "unavailable"))
		self.assertEqual(self.enqueue.call_count, 1)

	def test_a_balances_job_and_snapshot_requests_use_separate_slots(self):
		self.assertTrue(self.request()["ok"])
		months = [add_days(self.fx.today, offset)[:7] for offset in (0, 35, 70)]
		results = [api.request_snapshot(self.fx.company, [], month=month) for month in months]
		self.assertEqual([r["ok"] for r in results], [True, True, False], results)
		self.assertEqual(results[2]["code"], "worker_busy")

	def test_the_total_is_one_server_field_not_a_sum_of_the_rows(self):
		sid = self.job(total="999.00")
		data = api.get_account_balances_status(sid)["data"]
		self.assertEqual(data["total"], "999.00")
		self.assertEqual(
			set(data),
			{
				"job_id",
				"state",
				"code",
				"queued_at",
				"started_at",
				"completed_at",
				"expires_at",
				"generated_at",
				"currency",
				"precision",
				"accounts",
				"total",
			},
		)
		self.assertEqual((data["job_id"], data["state"], data["precision"]), (sid, "complete", 2))
		self.assertTrue(all("total" not in row for row in data["accounts"]))

	def test_names_types_and_bank_labels_are_added_at_read_time(self):
		self.as_admin(
			lambda: self.fx.insert(
				"Bank Account",
				"BA1",
				account_name="Operating",
				bank="HDFC Bank",
				account=self.fx.bank,
				company=self.fx.company,
				is_company_account=1,
				disabled=0,
				bank_account_no=NUMBER,
			)
		)
		sid = self.job()
		rows = {row["account"]: row for row in api.get_account_balances_status(sid)["data"]["accounts"]}
		self.assertEqual(
			rows[self.fx.bank],
			{
				"account": self.fx.bank,
				"account_name": self.fx.prefix + "Bank",
				"account_type": "Bank",
				"bank": "HDFC Bank",
				"reference": "•••• 3281",
				"balance": "100.00",
			},
		)
		self.assertEqual((rows[self.second]["bank"], rows[self.second]["reference"]), (None, None))
		self.assertNotIn("HDFC", json.dumps(store.load_result(self.user, sid)))

	def test_an_account_revoked_after_completion_discards_the_job_and_gives_no_total(self):
		sid = self.job()
		self.as_admin(lambda: frappe.db.set_value("Account", self.second, "disabled", 1))
		result = api.get_account_balances_status(sid)
		self.assertEqual(
			(result["ok"], result["code"], result["state"]), (False, "scope_changed", "scope_changed")
		)
		self.assertNotIn("data", result)
		self.assertIsNone(store.load_status(self.user, sid))
		self.assertIsNone(store.load_result(self.user, sid))

	def test_a_result_of_an_earlier_day_is_discarded_and_answers_scope_changed_without_a_tombstone(self):
		sid = self.job(anchor=add_days(nowdate(), -1))
		result = api.get_account_balances_status(sid)
		self.assertEqual(
			(result["ok"], result["code"], result["state"]), (False, "scope_changed", "scope_changed")
		)
		self.assertNotIn("data", result)
		self.assertIsNone(store.load_status(self.user, sid))
		self.assertIsNone(store.load_result(self.user, sid))
		self.assertEqual(frappe.cache.execute_command("EXISTS", store._key(self.user, sid, "discarded")), 0)
		self.assertEqual(api.get_account_balances_status(sid)["code"], "not_found_or_expired")

	def test_a_result_of_today_is_served_and_a_result_without_an_anchor_is_not(self):
		self.assertTrue(api.get_account_balances_status(self.job())["ok"])
		sid = self.job(digest="c" * 64)
		key = store._key(self.user, sid, "result")
		stored = json.loads(frappe.cache.execute_command("GET", key))
		del stored["anchor"]
		frappe.cache.execute_command("SET", key, json.dumps(stored), "EX", 300)
		self.assertEqual(api.get_account_balances_status(sid)["code"], "scope_changed")

	def test_a_catalog_source_error_on_a_completed_read_is_passed_on_and_keeps_the_job(self):
		for code in ("too_large", "source_contract_unavailable"):
			with self.subTest(code=code):
				sid = self.job(digest=("c" if code == "too_large" else "d") * 64)
				with patch.object(catalog, "require_report", side_effect=CalendarSourceUnavailable(code)):
					result = api.get_account_balances_status(sid)
				self.assertEqual(
					(result["ok"], result["state"], result["code"]), (False, "unavailable", code)
				)
				self.assertEqual(store.load_status(self.user, sid)["state"], "complete")
				self.assertIsNotNone(store.load_result(self.user, sid))

	def test_a_failed_request_gate_discards_a_completed_job(self):
		sid = self.job()
		with patch.object(catalog, "require_report", side_effect=frappe.PermissionError):
			result = api.get_account_balances_status(sid)
		self.assertEqual(result["code"], "scope_changed")
		self.assertIsNone(store.load_status(self.user, sid))

	def test_a_completed_read_validates_the_result_accounts_once_in_chunks_of_fifty(self):
		sid = self.job([f"Fake account {index}" for index in range(120)])
		calls = []
		with (
			patch.object(
				catalog, "validate_bank_accounts", side_effect=lambda company, names: calls.append(len(names))
			),
			patch("frappe.get_list", wraps=frappe.get_list) as read,
		):
			self.assertTrue(api.get_account_balances_status(sid)["ok"])
		# One pass over the result accounts: no second pass over the live catalog.
		self.assertEqual(calls, [50, 50, 20])
		self.assertEqual(self.catalog_reads(read), [])

	def test_a_queued_or_running_poll_runs_only_the_request_gate(self):
		sid = self.job(complete=False)
		with (
			patch.object(catalog, "validate_bank_accounts") as validate,
			patch("frappe.get_list", wraps=frappe.get_list) as read,
			patch.object(catalog, "require_report", wraps=catalog.require_report) as report,
		):
			for _ in range(2):
				self.assertEqual(api.get_account_balances_status(sid)["data"]["state"], "queued")
		validate.assert_not_called()
		self.assertEqual(self.catalog_reads(read), [])
		self.assertEqual(report.call_count, 2)

	def test_a_running_job_reruns_the_request_gate_without_being_discarded(self):
		sid = self.job(complete=False)
		with patch.object(catalog, "require_report", side_effect=frappe.PermissionError):
			result = api.get_account_balances_status(sid)
		self.assertEqual((result["ok"], result["code"]), (False, "forbidden"))
		self.assertEqual(store.load_status(self.user, sid)["state"], "queued")
		self.assertEqual(api.get_account_balances_status(sid)["data"]["state"], "queued")

	def test_a_snapshot_id_is_not_found_and_its_job_survives(self):
		snapshot_job = api.request_snapshot(self.fx.company, [])["data"]["snapshot_id"]
		# The balances gate would refuse this user; the kind check must answer before it runs.
		with patch.object(catalog, "require_report", side_effect=frappe.PermissionError):
			result = api.get_account_balances_status(snapshot_job)
		self.assertEqual((result["ok"], result["code"]), (False, "not_found_or_expired"))
		self.assertEqual(store.load_status(self.user, snapshot_job)["state"], "queued")

	def test_a_balances_id_is_not_found_for_snapshot_reads_and_its_job_survives(self):
		sid = self.job()
		for read in (api.get_snapshot_status, api.get_day_detail):
			with self.subTest(read=read.__name__):
				args = (sid, self.fx.today) if read is api.get_day_detail else (sid,)
				result = read(*args)
				self.assertEqual((result["ok"], result["code"]), (False, "not_found_or_expired"))
				self.assertEqual(store.load_status(self.user, sid)["state"], "complete")
				self.assertIsNotNone(store.load_result(self.user, sid))

	def test_a_job_lost_from_rq_is_reported(self):
		sid = self.request()["data"]["job_id"]
		key = store._key(self.user, sid, "status")
		row = store._read(key)
		row["queued_at"] = "2020-01-01T00:00:00+00:00"
		frappe.cache.execute_command("SET", key, store._json(row), "EX", 300)
		result = api.get_account_balances_status(sid)
		self.assertTrue(result["ok"], result)
		self.assertEqual((result["data"]["state"], result["data"]["code"]), ("failed", "job_lost"))

	def test_unknown_malformed_and_foreign_ids_look_identical(self):
		sid = self.job()
		frappe.set_user("Administrator")
		other = self.fx.user()
		frappe.get_doc("User", other).add_roles("Jarvis User")
		frappe.set_user(other)
		unknown = api.get_account_balances_status("0" * 32)
		self.assertEqual(unknown["code"], "not_found_or_expired")
		self.assertEqual(api.get_account_balances_status(sid), unknown)
		self.assertEqual(api.get_account_balances_status("not-an-id"), unknown)
		frappe.set_user(self.user)
		self.assertEqual(store.load_status(self.user, sid)["state"], "complete")
