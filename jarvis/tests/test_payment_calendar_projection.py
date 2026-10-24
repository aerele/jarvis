"""Independent monetary examples for the deterministic daily projection."""

from copy import deepcopy
from decimal import Decimal
from unittest import TestCase

from jarvis.payment_calendar.projection import ProjectionError, project_snapshot, upcoming_days
from jarvis.tests.payment_calendar_fixtures import require_calendar_test_site

ANCHOR = "2026-10-24"


def term(remaining, due=ANCHOR, *, original=None, party="Supplier A", voucher="PI-1", kind="obligation"):
	return {
		"id": voucher + ":" + str(due),
		"remaining": str(remaining),
		"original": str(original if original is not None else max(Decimal(str(remaining)), Decimal(0))),
		"due_date": due,
		"party": party,
		"party_type": "Supplier",
		"voucher_type": "Purchase Invoice",
		"voucher_no": voucher,
		"kind": kind,
	}


def source(*rows, complete=True):
	return {
		"rows": list(rows),
		"canonical_total": str(sum((Decimal(r["remaining"]) for r in rows), Decimal(0))),
		"complete_for_company": complete,
		"scope_reasons": [] if complete else ["restricted_obligations"],
	}


class TestPaymentCalendarProjection(TestCase):
	def setUp(self):
		require_calendar_test_site()

	def project(self, ap=None, ar=None, **kwargs):
		return project_snapshot(
			source() if ap is None else ap,
			source() if ar is None else ar,
			kwargs.pop("cash", "100.00"),
			kwargs.pop("floor", "20.00"),
			ANCHOR,
			kwargs.pop("start", "2026-10-01"),
			kwargs.pop("end", "2026-12-05"),
			precision=2,
			**kwargs,
		)

	def test_same_party_id_in_two_doctypes_counts_as_two_payees(self):
		a = term(10, party="Shared identifier", voucher="PI-1")
		b = {
			**term(20, party="Shared identifier", voucher="JE-1"),
			"party_type": "Employee",
			"voucher_type": "Journal Entry",
		}
		data = self.project(source(a, b))
		self.assertEqual(data["days"][ANCHOR]["payable_parties_due"], 2)

	def test_unavailable_payable_source_cannot_mean_no_upcoming_payments(self):
		data = project_snapshot(None, source(), "100", "20", ANCHOR, "2026-10-01", "2026-12-05")
		with self.assertRaises(ProjectionError):
			upcoming_days(data, ANCHOR)

	def test_due_today_and_on_time_customer_collections(self):
		result = self.project(source(term(60)), source(term(10)))
		day = result["days"][ANCHOR]
		self.assertEqual(day["payable_remaining"], "60.00")
		self.assertEqual(day["receivable_remaining"], "10.00")
		self.assertEqual(day["projected_opening"], "100.00")
		self.assertEqual(day["projected_closing"], "50.00")
		self.assertEqual(day["risk"], "green")

	def test_overdue_and_undated_ap_are_charged_once_and_separate_from_the_today_cell(self):
		ap = source(term(30, "2026-09-01"), term(10, None), term(5), term(7, "2026-10-25"))
		result = self.project(ap)
		self.assertEqual(result["days"][ANCHOR]["payable_remaining"], "5.00")
		self.assertEqual(
			result["due_now"], {"scheduled": "5.00", "overdue": "30.00", "undated": "10.00", "total": "45.00"}
		)
		self.assertEqual(result["days"][ANCHOR]["projected_closing"], "55.00")
		self.assertEqual(result["days"]["2026-10-25"]["projected_closing"], "48.00")
		self.assertEqual(result["days"]["2026-10-26"]["projected_closing"], "48.00")

	def test_overdue_and_undated_ar_are_visible_but_never_assumed_received(self):
		result = self.project(ar=source(term(50, "2026-10-23"), term(25, None), term(10, "2026-10-25")))
		self.assertEqual(result["excluded_collections"]["overdue"], "50.00")
		self.assertEqual(result["excluded_collections"]["undated"], "25.00")
		self.assertEqual(result["days"][ANCHOR]["projected_closing"], "100.00")
		self.assertEqual(result["days"]["2026-10-25"]["projected_closing"], "110.00")

	def test_signed_adjustments_reconcile_without_forecast_refunds_or_new_collections(self):
		ap = source(term(30), term(-15, "2026-10-25", original=0, kind="adjustment"))
		ar = source(term(-40, "2026-10-25", original=0, kind="adjustment"))
		result = self.project(ap, ar)
		self.assertEqual(result["reconciliation"]["Payable"]["canonical_total"], "15.00")
		self.assertEqual(result["reconciliation"]["Payable"]["adjustments"], "-15.00")
		self.assertEqual(result["reconciliation"]["Receivable"]["adjustments"], "-40.00")
		self.assertEqual(result["days"]["2026-10-25"]["projected_closing"], "70.00")
		self.assertEqual(result["days"]["2026-10-25"]["settlement"], "No dues")

	def test_disjoint_buckets_reconcile_all_history_and_outside_horizon(self):
		ap = source(
			term(1, "2000-01-01"),
			term(2, None),
			term(3),
			term(4, "2027-01-01"),
			term(-5, kind="adjustment", original=0),
		)
		bucket = self.project(ap)["reconciliation"]["Payable"]
		self.assertEqual(
			bucket,
			{
				"canonical_total": "5.00",
				"current_future": "3.00",
				"overdue": "1.00",
				"undated": "2.00",
				"outside_horizon": "4.00",
				"adjustments": "-5.00",
			},
		)

	def test_paid_terms_stay_settled_with_zero_remaining_and_empty_days_are_no_dues(self):
		result = self.project(source(term(0, original=50)))
		self.assertEqual(result["days"][ANCHOR]["payable_remaining"], "0.00")
		self.assertEqual(result["days"][ANCHOR]["settlement"], "Settled")
		self.assertEqual(result["days"][ANCHOR]["payable_terms"], 1)
		self.assertEqual(result["days"]["2026-10-25"]["settlement"], "No dues")

	def test_mixed_day_counts_terms_invoices_and_parties_without_duplication(self):
		ap = source(term(0, original=40), term(60, original=60), term(10, party="Supplier B", voucher="PI-2"))
		day = self.project(ap)["days"][ANCHOR]
		self.assertEqual(day["settlement"], "Partially paid")
		self.assertEqual(day["payable_terms"], 3)
		self.assertEqual(day["payable_invoices"], 2)
		self.assertEqual(day["payable_parties"], 2)

	def test_risk_thresholds_including_exact_zero_and_floor_zero(self):
		for cash, floor, risk in (
			("-0.01", "20", "red"),
			("0", "20", "orange"),
			("19.99", "20", "orange"),
			("20", "20", "green"),
			("20.01", "20", "green"),
			("0", "0", "green"),
		):
			with self.subTest(cash=cash, floor=floor):
				self.assertEqual(self.project(cash=cash, floor=floor)["days"][ANCHOR]["risk"], risk)

	def test_carry_includes_intervening_days_even_when_display_starts_in_a_future_month(self):
		result = self.project(source(term(30, "2026-10-25")), start="2026-11-01")
		self.assertEqual(result["days"]["2026-11-01"]["projected_opening"], "70.00")
		self.assertEqual(result["days"]["2026-12-05"]["projected_closing"], "70.00")

	def test_decimal_precision_does_not_accumulate_binary_float_drift(self):
		result = self.project(source(term("0.10"), term("0.20", "2026-10-25")), cash="0.30", floor="0")
		self.assertEqual(result["days"]["2026-10-25"]["projected_closing"], "0.00")
		self.assertEqual(result["days"]["2026-10-25"]["risk"], "green")

	def test_historical_cells_show_current_residuals_without_historical_cash_colours(self):
		day = self.project(source(term(25, "2026-10-23")))["days"]["2026-10-23"]
		self.assertEqual(day["payable_remaining"], "25.00")
		self.assertIsNone(day["projected_closing"])
		self.assertIsNone(day["risk"])

	def test_incomplete_scope_retains_permitted_dues_without_cash_health(self):
		result = self.project(source(term(25), complete=False))
		self.assertEqual(result["days"][ANCHOR]["payable_remaining"], "25.00")
		self.assertFalse(result["forecast_available"])
		self.assertTrue(all(day["risk"] is None for day in result["days"].values()))

	def test_missing_cash_or_floor_never_becomes_a_zero_default(self):
		for missing in ({"cash": None}, {"floor": None}):
			with self.subTest(missing=missing):
				result = self.project(**missing)
				self.assertFalse(result["forecast_available"])
				self.assertIsNone(result["days"][ANCHOR]["risk"])

	def test_failed_source_is_unavailable_instead_of_no_dues(self):
		result = project_snapshot(None, source(), "100", "20", ANCHOR, ANCHOR, "2026-10-25", precision=2)
		self.assertIsNone(result["days"][ANCHOR]["payable_remaining"])
		self.assertEqual(result["days"][ANCHOR]["settlement"], "Unavailable")
		self.assertIsNone(result["days"][ANCHOR]["risk"])
		self.assertIsNone(result["days"][ANCHOR]["payable_terms"])
		self.assertIsNone(result["days"][ANCHOR]["payable_invoices"])

	def test_positive_accounting_adjustments_still_count_as_amounts_owed(self):
		result = self.project(source(term(10, original=0, kind="adjustment")))
		day = result["days"][ANCHOR]
		self.assertEqual(day["payable_remaining"], "10.00")
		self.assertEqual(day["payable_rows_due"], 1)
		self.assertEqual(day["projected_closing"], "90.00")
		self.assertEqual(result["reconciliation"]["Payable"]["current_future"], "10.00")

	def test_noncanonical_iso_week_dates_are_rejected(self):
		with self.assertRaises(ProjectionError):
			self.project(start="2026-W43-6")

	def test_source_values_must_already_match_the_currency_precision(self):
		with self.assertRaises(ProjectionError):
			self.project(cash="0.005")

	def test_upcoming_bounds_exclude_selected_include_day_thirty_and_label_historical_overdue(self):
		result = self.project(
			source(
				term(5),
				term(10, "2026-10-25"),
				term(20, "2026-11-23"),
				term(30, "2026-11-24"),
				term(0, "2026-10-26", original=10),
			)
		)
		self.assertEqual([r["date"] for r in upcoming_days(result, ANCHOR)], ["2026-10-25", "2026-11-23"])
		history = self.project(source(term(10, "2026-10-23")))
		self.assertTrue(upcoming_days(history, "2026-10-22")[0]["overdue"])

	def test_leap_day_rollover_and_no_event_days(self):
		result = project_snapshot(
			source(term(20, "2028-02-29")),
			source(),
			"100",
			"20",
			"2028-02-28",
			"2028-02-28",
			"2028-03-01",
			precision=2,
		)
		self.assertEqual(list(result["days"]), ["2028-02-28", "2028-02-29", "2028-03-01"])
		self.assertEqual(result["days"]["2028-03-01"]["projected_closing"], "80.00")

	def test_more_than_two_thousand_rows_are_complete(self):
		rows = [term("0.01", voucher=f"PI-{i}") for i in range(2501)]
		day = self.project(source(*rows))["days"][ANCHOR]
		self.assertEqual(day["payable_terms"], 2501)
		self.assertEqual(day["payable_remaining"], "25.01")
		self.assertEqual(day["projected_closing"], "74.99")

	def test_reconciliation_mismatch_and_nonfinite_amount_fail(self):
		bad = source(term(10))
		bad["canonical_total"] = "11.00"
		with self.assertRaises(ProjectionError):
			self.project(bad)
		for value in ("NaN", "Infinity", True):
			with self.subTest(value=value), self.assertRaises(ProjectionError):
				self.project(cash=value)

	def test_projection_does_not_mutate_source_rows(self):
		ap = source(term(25))
		before = deepcopy(ap)
		self.project(ap)
		self.assertEqual(ap, before)
