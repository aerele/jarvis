"""Real permission-checked delivery; projections never bypass all-row visibility."""

import json
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar import access, api, detail, store
from jarvis.payment_calendar.compatibility import source_profile
from jarvis.payment_calendar.projection import project_snapshot
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.settings import get_settings, update_safety_floor
from jarvis.payment_calendar.sources import read_obligations
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarAPI(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_api")
		self.fx = CalendarFixtures()
		self.invoice = self.fx.invoice((40, 60))
		self.fx.invoice((90,), side="Receivable")
		update_safety_floor(self.fx.company, "20", 0)
		self.user = self.fx.user()
		frappe.get_doc("User", self.user).add_roles("Jarvis User")
		frappe.set_user(self.user)
		self.scope = normalize_scope(self.fx.company, [])
		proof = access.authorize_scope(self.scope)
		sources = {
			side: read_obligations(
				self.fx.company, side, self.fx.today, self.scope["window_start"], self.scope["window_end"]
			)
			for side in ("Payable", "Receivable")
		}
		self.result = {
			"schema": 2,
			"sources": sources,
			"projection": project_snapshot(
				sources["Payable"],
				sources["Receivable"],
				None,
				"20",
				self.fx.today,
				self.scope["window_start"],
				self.scope["window_end"],
			),
			"horizon": None,
			"bank": None,
			"bank_code": None,
			"floor": get_settings(self.fx.company),
			"currency": "INR",
			"generated_at": "2026-10-24T10:00:00+00:00",
			"unavailable": {},
		}
		self.sid, _ = store.admit(self.user, "b" * 64, self.scope, proof["fingerprint"])
		store.mark_running(self.user, self.sid)
		store.publish_snapshot(self.user, self.sid, store.split_result(self.result, self.scope))

	def tearDown(self):
		store.purge_actor(self.user)
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_api")
		frappe.clear_cache(doctype="Purchase Invoice")

	def test_month_requests_keep_the_server_anchor_and_narrow_only_the_window(self):
		month = add_days(self.fx.today, 35)[:7]
		with patch(
			"jarvis.payment_calendar.api.jobs.request_snapshot", return_value={"state": "queued"}
		) as request:
			for requested, expected in ((month, month), ("current", self.fx.today[:7]), (None, None)):
				with self.subTest(month=requested):
					result = api.request_snapshot(self.fx.company, [], month=requested)
					self.assertTrue(result["ok"], result)
					scope = request.call_args.args[0]
					self.assertEqual(scope["anchor"], self.fx.today)
					self.assertEqual(scope["actor"], self.user)
					self.assertEqual(scope.get("month"), expected)
					if expected:
						self.assertEqual(scope["window_start"], expected + "-01")
						self.assertEqual(scope["window_end"][:7], expected)
					else:
						self.assertEqual(scope["window_start"], scope["selectable_start"])

	def test_bad_or_out_of_range_month_never_dispatches_a_job(self):
		with patch("jarvis.payment_calendar.api.jobs.request_snapshot") as request:
			for month in ("1900-01", "9999-12", "2026-13", "2026-1", {}, True, ""):
				with self.subTest(month=month):
					result = api.request_snapshot(self.fx.company, [], month=month)
					self.assertFalse(result["ok"], result)
					self.assertEqual(result["code"], "invalid_request")
			request.assert_not_called()

	def test_status_delivers_aggregates_without_private_permission_proof_or_source_rows(self):
		api._load(self.sid)
		result = api.get_snapshot_status(self.sid)
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["data"]["currency"], "INR")
		self.assertIs(result["data"]["projection"]["payable_settlement_complete"], True)
		self.assertNotIn("_permission", json.dumps(result))
		self.assertNotIn("permission_columns", json.dumps(result))
		# The top invoice of a day is the one deliberate invoice number in status (B2).
		quoted = json.dumps(self.invoice)
		tops = [
			day[key]
			for day in result["data"]["projection"]["days"].values()
			for key in ("payable_top_invoice", "receivable_top_invoice")
			if day[key]
		]
		named = [top for top in tops if top["voucher_no"] == self.invoice]
		self.assertTrue(named)
		self.assertEqual(json.dumps(result).count(quoted), len(named))
		for top in named:
			top["voucher_no"] = None
		self.assertNotIn(self.invoice, json.dumps(result))

	def test_detail_and_upcoming_use_the_same_snapshot_without_running_sources(self):
		selected = add_days(self.fx.today, 10)
		with patch("jarvis.payment_calendar.snapshot.read_obligations") as source:
			detail = api.get_day_detail(self.sid, selected, "Payable", 1, 1)
			upcoming = api.get_upcoming(self.sid, self.fx.today)
			source.assert_not_called()
		self.assertTrue(detail["ok"], detail)
		self.assertEqual(detail["data"]["rows"][0]["remaining"], "40.00")
		self.assertEqual(detail["data"]["parties"][0]["remaining"], "40.00")
		self.assertNotIn("_permission", json.dumps(detail))
		self.assertEqual([r["remaining"] for r in upcoming["data"]["rows"]], ["40.00", "60.00"])

	def test_cross_user_snapshot_is_indistinguishable_from_expiry(self):
		frappe.set_user("Administrator")
		other = self.fx.user()
		frappe.get_doc("User", other).add_roles("Jarvis User")
		frappe.set_user(other)
		self.assertEqual(api.get_snapshot_status(self.sid), api.get_snapshot_status("0" * 32))

	def test_floor_change_blocks_all_old_aggregate_and_detail_delivery(self):
		frappe.set_user("Administrator")
		update_safety_floor(self.fx.company, "30", 1)
		frappe.set_user(self.user)
		result = api.get_snapshot_status(self.sid)
		self.assertFalse(result["ok"])
		self.assertEqual(result["code"], "scope_changed")
		self.assertNotIn("data", result)
		self.assertIsNone(store.load_result(self.user, self.sid))

	def test_sales_person_restrictions_block_previously_cached_receivable_delivery(self):
		frappe.set_user("Administrator")
		person = self.fx.insert(
			"Sales Person",
			"SalesPerson",
			sales_person_name="Scoped calendar sales person",
			is_group=0,
			lft=1,
			rgt=2,
		)
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": self.user,
				"allow": "Sales Person",
				"for_value": person,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.set_user(self.user)
		for response in (
			api.get_snapshot_status(self.sid),
			api.get_day_detail(self.sid, self.fx.today, "Receivable"),
		):
			self.assertFalse(response["ok"], response)
			self.assertNotIn("data", response)
		if source_profile().major == "15":
			# A current fingerprint cannot legitimize a cached AR source produced
			# before the v15 limitation was enforced: delivery checks it again.
			proof = access.authorize_scope(self.scope)
			sid, _ = store.admit(self.user, "d" * 64, self.scope, proof["fingerprint"])
			store.mark_running(self.user, sid)
			store.publish_snapshot(self.user, sid, store.split_result(self.result, self.scope))
			for response in (
				api.get_snapshot_status(sid),
				api.get_day_detail(sid, self.fx.today, "Receivable"),
			):
				self.assertFalse(response["ok"], response)
				self.assertEqual(response["code"], "restricted_obligations")
				self.assertNotIn("data", response)

	def test_floor_identity_uses_the_locked_row_even_if_a_plain_read_would_be_stale(self):
		with patch.object(api, "get_settings", return_value={"configuration_id": "old-deleted-row"}):
			result = api.configure_floor(self.fx.company, "99", 1, "old-deleted-row")
		self.assertFalse(result["ok"])
		self.assertEqual(result["code"], "settings_changed")
		self.assertEqual(get_settings(self.fx.company)["safety_floor"], "20.00")

	def test_a_failed_floor_request_rolls_back_its_partial_write(self):
		current = get_settings(self.fx.company)

		def fail(*args):
			frappe.db.set_value(
				"Jarvis Payment Calendar Settings", current["configuration_id"], "safety_floor", 99
			)
			raise RuntimeError("PRIVATE_FINANCIAL_EXCEPTION")

		with patch.object(api, "update_safety_floor", side_effect=fail), patch.object(frappe, "log_error"):
			result = api.configure_floor(self.fx.company, "99", 1, current["configuration_id"])
		self.assertFalse(result["ok"])
		self.assertEqual(get_settings(self.fx.company)["safety_floor"], "20.00")

	def test_live_invoice_query_change_blocks_aggregates_even_with_the_same_hook_identity(self):
		from jarvis.tests import test_payment_calendar_party_permissions as party_tests

		# Introduce a stateful hook before binding its identity, then change its
		# result without changing any policy metadata or the callback's path.
		original = frappe.get_hooks

		def hooks(key=None, *args, **kwargs):
			if key == "permission_query_conditions":
				value = original(key, *args, **kwargs).copy()
				value["Supplier"] = [
					"jarvis.tests.test_payment_calendar_party_permissions.supplier_query_condition"
				]
				return value
			return original(key, *args, **kwargs)

		with patch.object(frappe, "get_hooks", side_effect=hooks):
			party_tests._selected_supplier = self.fx.supplier
			proof = access.authorize_scope(self.scope)
			sid, _ = store.admit(self.user, "c" * 64, self.scope, proof["fingerprint"])
			store.mark_running(self.user, sid)
			store.publish_snapshot(self.user, sid, store.split_result(self.result, self.scope))
			party_tests._selected_supplier = "NO_SUCH_SUPPLIER"
			result = api.get_snapshot_status(sid)
			self.assertFalse(result["ok"], result)
			self.assertNotIn("data", result)
		party_tests._selected_supplier = None

	def test_invalid_dates_pages_and_sides_return_safe_envelopes(self):
		for date, side, page, size in (
			("2026-02-30", "Payable", 1, 25),
			(self.fx.today, "Unknown", 1, 25),
			(self.fx.today, "Payable", -1, 25),
			(self.fx.today, "Payable", 1, 101),
		):
			with self.subTest(date=date, side=side, page=page, size=size):
				result = api.get_day_detail(self.sid, date, side, page, size)
				self.assertFalse(result["ok"])
				self.assertNotIn("data", result)

	def test_the_party_page_is_validated_even_when_the_party_summary_is_off(self):
		for parties in (True, False):
			for party_page in (0, -1, "x", 100001, True):
				with self.subTest(parties=parties, party_page=party_page):
					for result in (
						api.get_day_detail(
							self.sid, self.fx.today, "Payable", 1, 25, party_page, None, None, parties
						),
						api.get_bucket_detail(
							self.sid, "overdue", "Payable", 1, 25, party_page, None, None, parties
						),
					):
						self.assertEqual((result["ok"], result["code"]), (False, "invalid_request"))

	def test_invalid_sort_or_order_returns_the_invalid_envelope(self):
		for sort, order in (("bogus", "asc"), ("remaining", "up"), ("", "desc"), ("party", "")):
			with self.subTest(sort=sort, order=order):
				for result in (
					api.get_day_detail(self.sid, self.fx.today, "Payable", 1, 25, 1, sort, order),
					api.get_bucket_detail(self.sid, "overdue", "Payable", 1, 25, 1, sort, order),
				):
					self.assertEqual(
						(result["ok"], result["state"], result["code"]), (False, "invalid", "invalid_request")
					)
					self.assertNotIn("data", result)

	def test_detail_rows_are_sorted_carry_settled_and_no_source_link(self):
		selected = add_days(self.fx.today, 10)
		# The window bucket spans both terms (40 and 60), so the order is visible.
		for order, expected in (("asc", ["40.00", "60.00"]), ("desc", ["60.00", "40.00"])):
			window = api.get_bucket_detail(self.sid, "window", "Payable", 1, 25, 1, "remaining", order)
			self.assertEqual([r["remaining"] for r in window["data"]["rows"]], expected)
		row = api.get_day_detail(self.sid, selected, "Payable")["data"]["rows"][0]
		self.assertNotIn("source_link", row)
		self.assertEqual(row["settled"], "0.00")
		self.assertEqual(api.get_day_detail(self.sid, selected)["data"]["total_remaining"], "40.00")

	def test_payable_only_buckets_answer_payables_and_refuse_receivables(self):
		for bucket in ("today", "immediate", "now"):
			ok = api.get_bucket_detail(self.sid, bucket)
			self.assertTrue(ok["ok"], ok)
			self.assertIn("total_remaining", ok["data"])
			refused = api.get_bucket_detail(self.sid, bucket, "Receivable")
			self.assertEqual((refused["ok"], refused["state"]), (False, "invalid"))

	def test_window_bucket_total_is_the_month_total_and_its_rows_are_the_tab_count(self):
		window = api.get_bucket_detail(self.sid, "window", "Payable")
		self.assertTrue(window["ok"], window)
		totals = self.result["projection"]["month_totals"]
		self.assertIsNone(totals)  # this fixture scope has no month: the window total is unknown
		self.assertIsNone(window["data"]["total_remaining"])

	def test_error_envelopes_never_echo_exception_text_or_server_messages(self):
		with (
			patch.object(api, "_load", side_effect=ValueError("PRIVATE_SUPPLIER_AMOUNT")),
			patch.object(frappe, "log_error") as log,
		):
			frappe.local.message_log = ["PRIVATE_SUPPLIER_AMOUNT"]
			result = api.get_snapshot_status(self.sid)
			self.assertNotIn("PRIVATE_SUPPLIER_AMOUNT", json.dumps(result))
			self.assertFalse(frappe.local.message_log)
			self.assertNotIn("PRIVATE_SUPPLIER_AMOUNT", str(log.call_args))

	def test_failed_payable_source_cannot_be_reported_as_an_empty_upcoming_list(self):
		proof = access.authorize_scope(self.scope)
		result = {
			**self.result,
			"sources": {**self.result["sources"], "Payable": None},
			"unavailable": {"Payable": "too_large"},
		}
		result["projection"] = project_snapshot(
			None,
			result["sources"]["Receivable"],
			None,
			"20",
			self.fx.today,
			self.scope["window_start"],
			self.scope["window_end"],
		)
		sid, _ = store.admit(self.user, "f" * 64, self.scope, proof["fingerprint"])
		store.mark_running(self.user, sid)
		store.publish_snapshot(self.user, sid, store.split_result(result, self.scope))
		response = api.get_upcoming(sid, self.fx.today)
		self.assertFalse(response["ok"], response)
		self.assertEqual(response["state"], "unavailable")
		self.assertEqual(response["code"], "too_large")
		self.assertNotIn("data", response)

	def test_active_job_cap_refusal_uses_the_failure_envelope(self):
		with patch.object(api.jobs, "request_snapshot", side_effect=store.ActorBusy):
			response = api.request_snapshot(self.fx.company, [])
		self.assertFalse(response["ok"])
		self.assertEqual(response["code"], "worker_busy")
		self.assertEqual(response["state"], "unavailable")

	def test_forbidden_receivables_are_not_reported_as_a_retryable_outage(self):
		result = {
			**self.result,
			"sources": {**self.result["sources"], "Receivable": None},
			"unavailable": {"Receivable": "forbidden"},
		}
		with patch.object(api, "_complete", return_value=(store.load_status(self.user, self.sid), result)):
			response = api.get_day_detail(self.sid, self.fx.today, "Receivable")
		self.assertFalse(response["ok"])
		self.assertEqual(response["state"], "forbidden")
		self.assertFalse(response["retryable"])

	def publish(self, result, digest):
		proof = access.authorize_scope(self.scope)
		sid, _ = store.admit(self.user, digest * 64, self.scope, proof["fingerprint"])
		store.mark_running(self.user, sid)
		store.publish_snapshot(self.user, sid, store.split_result(result, self.scope))
		return sid

	def test_status_response_comes_from_the_module_level_snapshot_status_body(self):
		with patch.object(
			api, "snapshot_status_body", return_value={"state": "complete", "marker": 1}
		) as build:
			result = api.get_snapshot_status(self.sid)
		self.assertEqual(result, {"ok": True, "data": {"state": "complete", "marker": 1}})
		self.assertEqual(build.call_args.args[1]["currency"], "INR")

	def test_a_bucket_read_takes_its_rows_from_the_stored_index_not_from_a_filter(self):
		with patch.object(detail, "bucket_filter", side_effect=AssertionError("filtered")):
			result = api.get_bucket_detail(self.sid, "window")
		self.assertTrue(result["ok"], result)
		self.assertTrue(result["data"]["rows"])

	def test_schema_two_result_is_delivered_with_month_totals(self):
		sid = self.publish({**self.result, "schema": 2, "horizon": None}, "2")
		result = api.get_snapshot_status(sid)
		self.assertTrue(result["ok"], result)
		self.assertIn("month_totals", result["data"]["projection"])
		self.assertIsNone(result["data"]["horizon"])

	def test_schema_two_horizon_is_delivered_unchanged(self):
		horizon = {"start": "2026-10-24", "forecast_available": False, "lowest": None}
		sid = self.publish({**self.result, "schema": 2, "horizon": horizon}, "4")
		result = api.get_snapshot_status(sid)
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["data"]["horizon"], horizon)

	def internal_errors(self, *errors):
		"""How many Error Log rows these failures of `get_snapshot_status` write."""
		with patch.object(frappe, "log_error") as log:
			for error in errors:
				with patch.object(api, "_load", side_effect=error):
					self.assertEqual(api.get_snapshot_status(self.sid)["code"], "internal_error")
		return log.call_count

	def test_a_repeated_internal_error_writes_one_error_log_row_per_operation_and_class(self):
		self.assertEqual(self.internal_errors(*[ValueError("a")] * 5), 1)

	def test_another_exception_class_or_operation_gets_its_own_row(self):
		self.assertEqual(self.internal_errors(ValueError("a"), RuntimeError("b"), ValueError("c")), 2)
		with (
			patch.object(frappe, "log_error") as log,
			patch.object(api.detail, "check_side", side_effect=ValueError("d")),
		):
			api.get_day_detail(self.sid, self.fx.today, "Payable")
		self.assertEqual(log.call_count, 1)

	def test_the_throttle_never_stops_the_first_row_when_the_cache_is_down(self):
		with patch.object(frappe.cache, "execute_command", side_effect=ConnectionError("redis down")):
			self.assertEqual(self.internal_errors(ValueError("a"), ValueError("b")), 2)

	def test_other_internal_errors_log_no_exception_text(self):
		with (
			patch.object(api, "_load", side_effect=ValueError("PRIVATE_SUPPLIER_AMOUNT")),
			patch.object(frappe, "log_error") as log,
		):
			api.get_snapshot_status(self.sid)
		self.assertNotIn("detail", json.loads(log.call_args.kwargs["message"]))
