"""Month boundaries and financial carry use the real permission/report path."""

from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_months, get_first_day, get_last_day, nowdate

from jarvis.payment_calendar import api, scope, snapshot
from jarvis.payment_calendar.access import ScopeChanged, authorize_scope
from jarvis.payment_calendar.projection import project_snapshot, upcoming_days
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarMonth(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_month")
		self.fx = CalendarFixtures()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_month")

	def test_month_scope_preserves_server_anchor_and_global_navigation_bounds(self):
		for offset in (-12, -1, 0, 1, 11):
			month = add_months(nowdate(), offset)[:7]
			value = scope.normalize_scope(self.fx.company, [], month=month)
			self.assertEqual(value["anchor"], nowdate())
			self.assertEqual(value["month"], month)
			self.assertEqual(value["window_start"], month + "-01")
			self.assertEqual(value["window_end"], str(get_last_day(month + "-01")))
			self.assertEqual(value["selectable_start"], str(get_first_day(add_months(nowdate(), -12))))
			authorize_scope(value)

	def test_month_input_cannot_widen_the_global_window_or_supply_an_accounting_date(self):
		for month in (
			"",
			"2026-00",
			"2026-13",
			"2026-1",
			"2026-10-01",
			True,
			{},
			add_months(nowdate(), -13)[:7],
			add_months(nowdate(), 12)[:7],
		):
			with self.subTest(month=month), self.assertRaises(frappe.ValidationError):
				scope.normalize_scope(self.fx.company, [], month=month)

	def test_authorization_refuses_a_forged_month_window(self):
		value = scope.normalize_scope(self.fx.company, [], month=nowdate()[:7])
		value["window_end"] = add_days(value["window_end"], 1)
		with self.assertRaises(ScopeChanged):
			authorize_scope(value)

	def test_public_request_binds_server_current_or_explicit_month_and_supports_legacy_clients(self):
		with patch.object(api.jobs, "request_snapshot", return_value={"state": "queued"}) as request:
			self.assertTrue(api.request_snapshot(self.fx.company, [])["ok"])
			self.assertNotIn("month", request.call_args.args[0])
			self.assertTrue(api.request_snapshot(self.fx.company, [], month="current")["ok"])
			self.assertEqual(request.call_args.args[0]["month"], nowdate()[:7])
			month = add_months(nowdate(), 1)[:7]
			self.assertTrue(api.request_snapshot(self.fx.company, [], month=month)["ok"])
			self.assertEqual(request.call_args.args[0]["month"], month)

	def test_projection_of_a_historical_month_does_not_invent_cash_history(self):
		month = add_months(nowdate(), -1)[:7]
		empty = {"rows": [], "canonical_total": "0", "complete_for_company": True}
		result = project_snapshot(
			empty, empty, "100", "20", nowdate(), month + "-01", str(get_last_day(month + "-01"))
		)
		self.assertTrue(result["days"])
		self.assertTrue(
			all(day["risk"] is None and day["projected_closing"] is None for day in result["days"].values())
		)

	def test_monthly_upcoming_stops_at_month_end(self):
		from jarvis.tests.test_payment_calendar_projection import source, term

		result = project_snapshot(
			source(term(10, "2026-10-31"), term(20, "2026-11-01")),
			source(),
			"100",
			"0",
			"2026-10-28",
			"2026-10-01",
			"2026-10-31",
		)
		result["month"] = "2026-10"
		self.assertEqual([r["date"] for r in upcoming_days(result, "2026-10-28")], ["2026-10-31"])
		self.assertEqual(upcoming_days(result, "2026-10-31"), [])

	def test_month_collection_retains_overdue_outstanding_and_limits_settled_terms(self):
		current = scope.normalize_scope(self.fx.company, [], month=nowdate()[:7])
		# Raw fixtures are committed only in private snapshot tests; collect() owns
		# its transaction, so use the canonical reader in this savepoint test.
		from jarvis.payment_calendar.sources import read_obligations

		old = self.fx.invoice((30,), paid=30)
		frappe.db.set_value(
			"Payment Schedule", {"parent": old}, "due_date", add_months(current["window_start"], -1)
		)
		overdue = self.fx.invoice((25,))
		frappe.db.set_value(
			"Payment Schedule", {"parent": overdue}, "due_date", add_months(current["window_start"], -1)
		)
		rows = read_obligations(
			self.fx.company, "Payable", nowdate(), current["window_start"], current["window_end"]
		)["rows"]
		self.assertNotIn(old, [r["voucher_no"] for r in rows])
		self.assertEqual(sum(Decimal(r["remaining"]) for r in rows if r["voucher_no"] == overdue), 25)


from jarvis.tests import test_payment_calendar_snapshot as snapshot_fixtures


class TestPaymentCalendarMonthSnapshot(TestCase):
	setUp = snapshot_fixtures.TestPaymentCalendarSnapshot.setUp
	tearDown = snapshot_fixtures.TestPaymentCalendarSnapshot.tearDown

	def test_month_projection_matches_full_snapshot_for_each_selected_day_and_carry(self):
		from jarvis.payment_calendar.settings import update_safety_floor

		update_safety_floor(self.fx.company, "20", 0)
		frappe.db.set_value("Payment Schedule", {"parent": self.ap}, "due_date", add_days(nowdate(), 1))
		frappe.db.commit()
		legacy = scope.normalize_scope(self.fx.company, [self.fx.bank])
		full = snapshot.collect(legacy, authorize_scope(legacy)["fingerprint"])
		for offset in (-1, 0, 1):
			month = add_months(nowdate(), offset)[:7]
			monthly = scope.normalize_scope(self.fx.company, [self.fx.bank], month=month)
			value = snapshot.collect(monthly, authorize_scope(monthly)["fingerprint"])
			days = value["projection"]["days"]
			self.assertEqual(len(days), get_last_day(month + "-01").day)
			self.assertTrue(all(date.startswith(month + "-") for date in days))
			for date, day in days.items():
				self.assertEqual(day, full["projection"]["days"][date])
			self.assertEqual(value["projection"]["due_now"], full["projection"]["due_now"])

	def test_month_scope_is_published_and_other_month_detail_is_refused(self):
		from jarvis.payment_calendar import store

		month = nowdate()[:7]
		monthly = scope.normalize_scope(self.fx.company, [], month=month)
		proof = authorize_scope(monthly)
		value = snapshot.collect(monthly, proof["fingerprint"])
		store.purge_actor("Administrator")
		sid, _ = store.admit("Administrator", "c" * 64, monthly, proof["fingerprint"])
		try:
			self.assertTrue(store.mark_running("Administrator", sid))
			self.assertTrue(store.publish("Administrator", sid, value))
			result = api.get_snapshot_status(sid)
			self.assertTrue(result["ok"])
			self.assertEqual(result["data"]["scope"]["month"], month)
			self.assertEqual(len(result["data"]["projection"]["days"]), get_last_day(nowdate()).day)
			self.assertTrue(api.get_day_detail(sid, nowdate())["ok"])
			self.assertFalse(api.get_day_detail(sid, add_months(month + "-01", 1))["ok"])
		finally:
			store.purge_actor("Administrator")
