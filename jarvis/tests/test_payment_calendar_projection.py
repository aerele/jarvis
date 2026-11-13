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
		"party_name": party,
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

	def test_payable_settlement_completeness_is_independent_of_cash_and_receivables(self):
		result = project_snapshot(
			source(term(0, original=50)), None, None, None, ANCHOR, ANCHOR, "2026-10-25"
		)
		self.assertFalse(result["forecast_available"])
		self.assertIs(result["payable_settlement_complete"], True)
		self.assertEqual(result["days"][ANCHOR]["settlement"], "Settled")

	def test_incomplete_and_missing_payables_do_not_certify_settlement(self):
		missing_reasons = source(term(0, original=50), complete=False)
		missing_reasons.pop("scope_reasons")
		for ap in [None, source(term(0, original=50), complete=False), missing_reasons]:
			with self.subTest(ap=ap):
				result = project_snapshot(ap, source(), "100", "20", ANCHOR, ANCHOR, "2026-10-25")
				self.assertIs(result["payable_settlement_complete"], False)

	def test_receivable_restrictions_do_not_make_complete_payables_unknown(self):
		result = self.project(source(term(10)), source(term(0, original=20), complete=False))
		self.assertFalse(result["forecast_available"])
		self.assertIs(result["payable_settlement_complete"], True)

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
			result["due_now"],
			{
				"scheduled": "5.00",
				"overdue": "30.00",
				"undated": "10.00",
				"immediate": "40.00",
				"total": "45.00",
			},
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


class TestPaymentCalendarProjectionTemplateFields(TestCase):
	"""B2 day fields and B3 month totals that the #821 calendar template reads."""

	def setUp(self):
		require_calendar_test_site()

	def project(self, ap=None, ar=None, **kwargs):
		return project_snapshot(
			source() if ap is None else ap,
			source() if ar is None else ar,
			kwargs.pop("cash", "1000.00"),
			kwargs.pop("floor", "20.00"),
			ANCHOR,
			kwargs.pop("start", "2026-10-01"),
			kwargs.pop("end", "2026-12-05"),
			precision=2,
			**kwargs,
		)

	def day(self, result, date=ANCHOR):
		return result["days"][date]

	def test_row_counts_include_settled_zero_rows_and_are_zero_for_empty_days(self):
		ap = source(term(0, original=40, voucher="PI-S"), term(60, voucher="PI-2"))
		ar = source(term(5, voucher="SI-1"))
		result = self.project(ap, ar)
		self.assertEqual(self.day(result)["payable_rows"], 2)
		self.assertEqual(self.day(result)["receivable_rows"], 1)
		self.assertEqual(self.day(result, "2026-10-25")["payable_rows"], 0)

	def test_top_invoice_groups_terms_of_one_invoice_and_names_its_party(self):
		ap = source(
			term(30, voucher="PI-1", party="Supplier A"),
			term(25, voucher="PI-1", party="Supplier A"),
			term(40, voucher="PI-2", party="Supplier B"),
		)
		top = self.day(self.project(ap))["payable_top_invoice"]
		self.assertEqual(
			top,
			{
				"voucher_type": "Purchase Invoice",
				"voucher_no": "PI-1",
				"party_name": "Supplier A",
				"remaining": "55.00",
			},
		)

	def test_top_invoice_tie_goes_to_the_smallest_voucher_and_empty_days_have_none(self):
		ap = source(term(50, voucher="PI-9"), term(50, voucher="PI-2"), term(50, voucher="PI-5"))
		result = self.project(ap)
		self.assertEqual(self.day(result)["payable_top_invoice"]["voucher_no"], "PI-2")
		self.assertIsNone(self.day(result, "2026-10-25")["payable_top_invoice"])
		self.assertIsNone(self.day(result)["receivable_top_invoice"])

	def test_top_invoice_tie_on_one_voucher_goes_to_the_smallest_party_type_then_party(self):
		def named(party_type, party):
			return {**term(50, voucher="JE-1", party=party), "party_type": party_type}

		rows = [named("Supplier", "B"), named("Supplier", "A"), named("Employee", "Z")]
		for order in (rows, rows[::-1], [rows[1], rows[0], rows[2]]):
			top = self.day(self.project(source(*order)))["payable_top_invoice"]
			self.assertEqual(top["party_name"], "Z", [r["party"] for r in order])
		same_type = [named("Supplier", "B"), named("Supplier", "A")]
		for order in (same_type, same_type[::-1]):
			top = self.day(self.project(source(*order)))["payable_top_invoice"]
			self.assertEqual(top["party_name"], "A")

	def test_payable_concentration_needs_strictly_more_than_half_of_the_dues(self):
		cases = (
			(("50", "50"), False),
			(("50.01", "49.99"), True),
			(("49", "25", "26"), False),
			(("51", "49"), True),
		)
		for amounts, concentrated in cases:
			with self.subTest(amounts=amounts):
				ap = source(*(term(a, voucher=f"PI-{i}") for i, a in enumerate(amounts)))
				self.assertIs(self.day(self.project(ap))["payable_concentrated"], concentrated)

	def test_payable_concentration_percentage_is_one_decimal_rounded_down(self):
		ap = source(term(2, voucher="PI-1"), term(1, voucher="PI-2"))
		self.assertEqual(self.day(self.project(ap))["payable_concentration_pct"], "66.6")
		single = self.project(source(term(102236, voucher="PI-1")))
		self.assertEqual(self.day(single)["payable_concentration_pct"], "100.0")

	def test_zero_dues_have_no_star_and_no_percentage_even_with_a_settled_row(self):
		for ap in (source(), source(term(0, original=40))):
			with self.subTest(rows=len(ap["rows"])):
				day = self.day(self.project(ap))
				self.assertIs(day["payable_concentrated"], False)
				self.assertIsNone(day["payable_concentration_pct"])
				self.assertIsNone(day["payable_top_invoice"])

	def test_day_net_is_receivable_minus_payable_with_zero_for_a_quiet_day(self):
		ap = source(term(40, voucher="PI-1"), term(60, voucher="PI-2"))
		ar = source(term(30, voucher="SI-1", party="Customer A"))
		result = self.project(ap, ar)
		self.assertEqual(self.day(result)["net"], "-70.00")
		self.assertEqual(self.day(result, "2026-10-25")["net"], "0.00")
		receivable_only = self.project(None, source(term(25, voucher="SI-2")))
		self.assertEqual(self.day(receivable_only)["net"], "25.00")

	def test_day_net_is_null_when_either_side_is_unavailable(self):
		for ap, ar in ((None, source()), (source(), None), (None, None)):
			result = project_snapshot(ap, ar, "100", "20", ANCHOR, "2026-10-01", "2026-10-31", precision=2)
			for iso in (ANCHOR, "2026-10-25"):
				self.assertIn("net", self.day(result, iso))
				self.assertIsNone(self.day(result, iso)["net"], (ap is None, ar is None))

	def test_day_net_is_null_when_either_side_is_incomplete(self):
		cases = {
			"payables": (source(term(40), complete=False), source(term(30, voucher="SI-1"))),
			"receivables": (source(term(40)), source(term(30, voucher="SI-1"), complete=False)),
		}
		for name, (ap, ar) in cases.items():
			with self.subTest(name):
				result = self.project(ap, ar)
				self.assertIsNone(self.day(result)["net"])
				self.assertIsNone(self.day(result, "2026-10-25")["net"])

	def test_unavailable_payables_keep_a_boolean_star_flag_and_null_the_rest(self):
		result = project_snapshot(
			None, source(), "100", "20", ANCHOR, "2026-10-01", "2026-10-31", precision=2
		)
		day = self.day(result)
		self.assertIs(day["payable_concentrated"], False)
		for key in ("payable_rows", "payable_top_invoice", "payable_concentration_pct"):
			self.assertIsNone(day[key], key)

	def test_receivable_settlement_follows_the_payable_wording_and_survives_the_null_loop(self):
		cases = (
			(source(), "No dues"),
			(source(term(0, original=40)), "Settled"),
			(source(term(40)), "Unpaid"),
			(source(term(10, original=40)), "Partially paid"),
		)
		for ar, expected in cases:
			with self.subTest(expected=expected):
				self.assertEqual(self.day(self.project(ar=ar))["receivable_settlement"], expected)
		unavailable = project_snapshot(
			source(), None, "100", "20", ANCHOR, "2026-10-01", "2026-10-31", precision=2
		)
		self.assertEqual(self.day(unavailable)["receivable_settlement"], "Unavailable")
		self.assertIsNone(self.day(unavailable)["receivable_remaining"])

	def test_cumulative_sums_start_at_zero_on_the_anchor_day(self):
		ap = source(term(10, "2026-10-10"), term(7, ANCHOR), term(3, "2026-10-25"))
		ar = source(term(40, ANCHOR), term(5, "2026-10-26"))
		result = self.project(ap, ar)
		first = self.day(result)
		self.assertEqual((first["cumulative_receivable"], first["cumulative_payable"]), ("40.00", "7.00"))
		last = self.day(result, "2026-10-26")
		self.assertEqual((last["cumulative_receivable"], last["cumulative_payable"]), ("45.00", "10.00"))

	def test_cash_identity_holds_exactly_for_every_day_from_the_anchor(self):
		ap = source(
			term(10, "2026-10-10", voucher="PI-OLD"),
			term(7, voucher="PI-A"),
			term(3, "2026-10-25", voucher="PI-B"),
			term("0.10", "2026-11-02", voucher="PI-C"),
			term(4, None, voucher="PI-U"),
		)
		ar = source(
			term(40, "2026-10-12", voucher="SI-OLD"),
			term(5, ANCHOR, voucher="SI-A"),
			term("0.20", "2026-11-01"),
		)
		result = self.project(ap, ar, cash="123.45", start="2026-10-01", end="2026-11-30")
		due_now = result["due_now"]
		checked = 0
		for date, day in result["days"].items():
			if date < ANCHOR:
				continue
			expected = (
				Decimal(result["book_cash"])
				+ Decimal(day["cumulative_receivable"])
				- Decimal(day["cumulative_payable"])
				- Decimal(due_now["overdue"])
				- Decimal(due_now["undated"])
			)
			self.assertEqual(Decimal(day["projected_closing"]), expected, date)
			self.assertEqual(
				Decimal(day["margin"]), Decimal(day["projected_closing"]) - Decimal(result["floor"])
			)
			checked += 1
		self.assertEqual(checked, 38)

	def test_payable_dated_before_the_anchor_is_counted_only_through_due_now(self):
		result = self.project(source(term(10, "2026-10-10")))
		self.assertEqual(result["due_now"]["overdue"], "10.00")
		self.assertEqual(self.day(result)["cumulative_payable"], "0.00")
		self.assertEqual(self.day(result)["projected_closing"], "990.00")

	def test_cumulative_and_margin_are_null_before_the_anchor_and_when_the_forecast_is_unavailable(self):
		result = self.project(source(term(7)))
		before = self.day(result, "2026-10-23")
		for key in ("cumulative_receivable", "cumulative_payable", "margin"):
			self.assertIsNone(before[key], key)
		for missing in ({"cash": None}, {"floor": None}):
			with self.subTest(missing=missing):
				day = self.day(self.project(source(term(7)), **missing))
				for key in ("cumulative_receivable", "cumulative_payable", "margin"):
					self.assertIsNone(day[key], key)
		failed = project_snapshot(
			None, source(), "100", "20", ANCHOR, "2026-10-01", "2026-10-31", precision=2
		)
		for key in ("cumulative_receivable", "cumulative_payable", "margin"):
			self.assertIsNone(self.day(failed)[key], key)

	def test_due_now_immediate_is_overdue_plus_undated(self):
		result = self.project(source(term(30, "2026-09-01"), term(10, None), term(5)))
		self.assertEqual(result["due_now"]["immediate"], "40.00")
		self.assertEqual(result["due_now"]["total"], "45.00")
		unavailable = project_snapshot(None, source(), "100", "20", ANCHOR, "2026-10-01", "2026-10-31")
		self.assertIsNone(unavailable["due_now"]["immediate"])

	def test_month_totals_sum_positive_dated_remaining_inside_the_window(self):
		ap = source(
			term(30, "2026-11-03", voucher="PI-1"),
			term(20, "2026-11-04", voucher="PI-2"),
			term(0, "2026-11-05", original=9, voucher="PI-S"),
			term(-4, "2026-11-06", original=0, kind="adjustment", voucher="PI-N"),
			term(8, None, voucher="PI-U"),
			term(6, "2026-12-01", voucher="PI-D"),
		)
		ar = source(term(100, "2026-11-10", voucher="SI-1"))
		result = self.project(ap, ar, start="2026-11-01", end="2026-11-30", month="2026-11")
		self.assertEqual(result["month"], "2026-11")
		self.assertEqual(
			result["month_totals"],
			{
				"receivable": "100.00",
				"payable": "50.00",
				"net": "50.00",
				# Rows the window bucket lists: the settled-zero PI-S counts, the negative PI-N does not.
				"receivable_rows": 1,
				"payable_rows": 3,
				"complete": True,
			},
		)

	def test_month_totals_net_can_be_negative_or_zero(self):
		for pay, receive, net in ((30, 10, "-20.00"), (10, 10, "0.00")):
			with self.subTest(net=net):
				result = self.project(
					source(term(pay, "2026-11-03")),
					source(term(receive, "2026-11-04", voucher="SI-1")),
					start="2026-11-01",
					end="2026-11-30",
					month="2026-11",
				)
				self.assertEqual(result["month_totals"]["net"], net)

	def test_month_totals_are_null_unless_both_sides_are_complete(self):
		cases = {
			"payables_incomplete": (source(term(5, "2026-11-03"), complete=False), source()),
			"receivables_incomplete": (source(), source(term(5, "2026-11-03"), complete=False)),
			"payables_unavailable": (None, source()),
			"receivables_unavailable": (source(), None),
		}
		for name, (ap, ar) in cases.items():
			with self.subTest(name):
				result = project_snapshot(
					ap, ar, "100", "20", ANCHOR, "2026-11-01", "2026-11-30", precision=2, month="2026-11"
				)
				totals = result["month_totals"]
				self.assertIs(totals["complete"], False)
				self.assertEqual(
					{k: v for k, v in totals.items() if k != "complete"},
					dict.fromkeys(("receivable", "payable", "net", "receivable_rows", "payable_rows")),
				)

	def test_month_totals_stay_complete_without_a_bank_or_a_floor(self):
		result = self.project(
			source(term(5, "2026-11-03")),
			cash=None,
			floor=None,
			start="2026-11-01",
			end="2026-11-30",
			month="2026-11",
		)
		self.assertFalse(result["forecast_available"])
		self.assertEqual(result["month_totals"]["payable"], "5.00")
		self.assertIs(result["month_totals"]["complete"], True)

	def test_legacy_scope_without_a_month_has_null_month_totals_and_no_month_key(self):
		result = self.project(source(term(5)))
		self.assertIn("month_totals", result)
		self.assertIsNone(result["month_totals"])
		self.assertNotIn("month", result)

	def test_filtered_obligations_on_either_side_make_the_month_totals_incomplete(self):
		def filtered(*rows):
			return {**source(*rows, complete=False), "scope_reasons": ["filtered_obligations"]}

		for name, ap, ar in (
			("payables", filtered(term(5, "2026-11-03")), source()),
			("receivables", source(), filtered(term(5, "2026-11-03", voucher="SI-1"))),
		):
			with self.subTest(name):
				result = self.project(ap, ar, start="2026-11-01", end="2026-11-30", month="2026-11")
				self.assertIs(result["month_totals"]["complete"], False)
				self.assertIsNone(result["month_totals"]["net"])
				self.assertEqual(result["month"], "2026-11")

	def test_month_stays_the_month_string_and_upcoming_days_ignore_month_totals(self):
		ap = source(term(10, "2026-11-12"), term(20, "2026-12-01"))
		result = self.project(ap, start="2026-11-01", end="2026-11-30", month="2026-11")
		self.assertIsInstance(result["month"], str)
		self.assertEqual([r["date"] for r in upcoming_days(result, "2026-11-11")], ["2026-11-12"])
