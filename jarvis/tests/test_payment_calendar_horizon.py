"""The 30-day horizon of the stored result (B1) and the shared result builder."""

from datetime import date, timedelta
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from jarvis.payment_calendar import snapshot
from jarvis.payment_calendar.projection import ProjectionError
from jarvis.tests import payment_calendar_golden as golden
from jarvis.tests.payment_calendar_fixtures import require_calendar_test_site

ANCHOR = "2026-11-25"  # late in the month, so the horizon runs into December
START, END = "2026-11-01", "2026-11-30"
HORIZON_END = "2026-12-24"
ACCOUNT = "HDFC Current"


def row(remaining, due, voucher):
	return {
		"id": voucher,
		"remaining": str(remaining),
		"original": str(remaining),
		"due_date": due,
		"party": "Party",
		"party_name": "Party",
		"party_type": "Supplier",
		"voucher_type": "Purchase Invoice",
		"voucher_no": voucher,
		"kind": "obligation",
	}


def source(*rows):
	return {
		"rows": list(rows),
		"canonical_total": str(sum((Decimal(r["remaining"]) for r in rows), Decimal(0))),
		"complete_for_company": True,
		"scope_reasons": [],
	}


def scope(month="2026-11", start=START, end=END):
	return {"month": month, "anchor": ANCHOR, "window_start": start, "window_end": end}


def sources(payables=(), receivables=()):
	return {"Payable": source(*payables), "Receivable": source(*receivables)}


class TestPaymentCalendarHorizon(TestCase):
	def setUp(self):
		require_calendar_test_site()

	def build(self, data=None, *, cash="1000.00", floor="200.00", window=None, **kwargs):
		data = sources() if data is None else data
		bank = None if cash is None else {"total": cash, "accounts": [], "currency": "INR", "precision": 2}
		return snapshot.build_result(
			window or scope(),
			data,
			bank,
			None,
			{},
			currency="INR",
			floor={"safety_floor": floor, "company": "C", "currency": "INR"},
			precision=2,
			generated_at="2026-11-25T00:00:00+00:00",
			**kwargs,
		)

	def horizon(self, *args, **kwargs):
		return self.build(*args, **kwargs)["horizon"]

	def test_horizon_runs_30_days_from_the_anchor_into_the_next_month(self):
		data = sources(
			payables=[row(100, "2026-11-26", "PI-1"), row(300, "2026-12-03", "PI-2")],
			receivables=[row(50, "2026-12-01", "SI-1")],
		)
		result = self.build(data)
		horizon = result["horizon"]
		self.assertEqual((horizon["start"], horizon["end"]), (ANCHOR, HORIZON_END))
		self.assertEqual(len(horizon["days"]), 30)
		self.assertEqual(list(horizon["days"])[0], ANCHOR)
		self.assertEqual(list(horizon["days"])[-1], HORIZON_END)
		# The month projection stops at 30 November; the horizon carries on.
		self.assertNotIn("2026-12-01", result["projection"]["days"])
		self.assertEqual(horizon["days"]["2026-11-25"], {"projected_closing": "1000.00", "margin": "800.00"})
		self.assertEqual(horizon["days"]["2026-11-26"]["projected_closing"], "900.00")
		self.assertEqual(horizon["days"]["2026-12-01"], {"projected_closing": "950.00", "margin": "750.00"})
		self.assertEqual(horizon["days"]["2026-12-03"], {"projected_closing": "650.00", "margin": "450.00"})
		self.assertEqual(horizon["days"][HORIZON_END]["projected_closing"], "650.00")
		self.assertTrue(horizon["forecast_available"])
		self.assertEqual(horizon["scope_reasons"], [])

	def test_horizon_days_carry_only_closing_and_margin(self):
		for day in self.horizon(sources(payables=[row(100, "2026-12-03", "PI-1")]))["days"].values():
			self.assertEqual(set(day), {"projected_closing", "margin"})

	def test_horizon_closing_equals_month_closing_on_every_overlapping_day(self):
		data = sources(
			payables=[
				row(100, "2026-11-26", "PI-1"),
				row(300, "2026-12-03", "PI-2"),
				row(7, "2026-11-10", "PI-0"),
			],
			receivables=[row(50, "2026-11-29", "SI-1"), row(80, "2026-12-01", "SI-2")],
		)
		result = self.build(data)
		overlap = [d for d in result["projection"]["days"] if d >= ANCHOR]
		self.assertEqual(overlap, [f"2026-11-{n}" for n in range(25, 31)])
		for iso in overlap:
			month_day, horizon_day = result["projection"]["days"][iso], result["horizon"]["days"][iso]
			self.assertEqual(horizon_day["projected_closing"], month_day["projected_closing"], iso)
			self.assertEqual(horizon_day["margin"], month_day["margin"], iso)

	def test_a_december_snapshot_from_the_same_sources_has_the_same_horizon(self):
		data = sources(
			payables=[row(100, "2026-11-26", "PI-1"), row(300, "2026-12-03", "PI-2")],
			receivables=[row(50, "2026-12-01", "SI-1")],
		)
		november = self.build(data)
		december = self.build(data, window=scope("2026-12", "2026-12-01", "2026-12-31"))
		self.assertEqual(december["projection"]["start"], "2026-12-01")
		self.assertEqual(december["horizon"], november["horizon"])

	def test_lowest_is_the_minimum_closing_with_its_cushion_and_state(self):
		horizon = self.horizon(sources(payables=[row(100, "2026-11-26", "PI-1")]), floor="200.00")
		self.assertEqual(
			horizon["lowest"],
			{"date": "2026-11-26", "closing": "900.00", "cushion": "700.00", "state": "safe"},
		)
		self.assertIsNone(horizon["first_below_floor"])

	def test_a_tie_for_the_lowest_closing_goes_to_the_first_date(self):
		# Closing stays at 800 from 26 November to the end, so every later day ties.
		horizon = self.horizon(sources(payables=[row(200, "2026-11-26", "PI-1")]))
		self.assertEqual(horizon["lowest"]["date"], "2026-11-26")
		self.assertEqual(horizon["days"][HORIZON_END]["projected_closing"], "800.00")

	def test_state_boundaries_follow_ten_percent_of_the_floor(self):
		cases = (
			("below", "99.99"),  # cushion -0.01
			("tight", "100.00"),  # cushion 0
			("tight", "110.00"),  # cushion exactly 10 % of the floor
			("safe", "110.01"),
		)
		for state, closing in cases:
			with self.subTest(state=state, closing=closing):
				data = sources(payables=[row(Decimal("1000.00") - Decimal(closing), "2026-11-26", "PI-1")])
				lowest = self.horizon(data, floor="100.00")["lowest"]
				self.assertEqual((lowest["closing"], lowest["state"]), (closing, state))

	def test_a_zero_floor_with_a_zero_cushion_is_tight(self):
		lowest = self.horizon(sources(payables=[row(1000, "2026-11-26", "PI-1")]), floor="0.00")["lowest"]
		self.assertEqual((lowest["closing"], lowest["cushion"], lowest["state"]), ("0.00", "0.00", "tight"))

	def test_first_below_floor_is_the_first_day_with_a_closing_under_the_floor(self):
		data = sources(
			payables=[row(200, "2026-11-26", "PI-1"), row(300, "2026-12-03", "PI-2")],
			receivables=[row(500, "2026-12-10", "SI-1")],
		)
		self.assertEqual(self.horizon(data, floor="900.00")["first_below_floor"], "2026-11-26")
		self.assertEqual(self.horizon(data, floor="700.00")["first_below_floor"], "2026-12-03")
		self.assertIsNone(self.horizon(data, floor="500.00")["first_below_floor"])
		# A closing equal to the floor (500.00 here) is not below it.
		self.assertEqual(self.horizon(data, floor="500.01")["first_below_floor"], "2026-12-03")

	def test_the_anchor_day_itself_can_be_below_the_floor(self):
		horizon = self.horizon(sources(), cash="100.00", floor="200.00")
		self.assertEqual(horizon["first_below_floor"], ANCHOR)
		self.assertEqual(horizon["lowest"]["state"], "below")

	def test_without_cash_the_horizon_is_unavailable_and_has_no_lowest(self):
		horizon = self.horizon(sources(payables=[row(100, "2026-11-26", "PI-1")]), cash=None)
		self.assertIs(horizon["forecast_available"], False)
		self.assertIn("bank_unavailable", horizon["scope_reasons"])
		self.assertIsNone(horizon["lowest"])
		self.assertIsNone(horizon["first_below_floor"])
		self.assertEqual(len(horizon["days"]), 30)
		for day in horizon["days"].values():
			self.assertEqual(day, {"projected_closing": None, "margin": None})

	def test_an_unavailable_side_makes_the_horizon_unavailable(self):
		data = {"Payable": None, "Receivable": source()}
		horizon = self.horizon(data)
		self.assertIs(horizon["forecast_available"], False)
		self.assertIn("payables_unavailable", horizon["scope_reasons"])
		self.assertIsNone(horizon["lowest"])

	def test_the_horizon_key_is_always_present_and_null_when_not_computed(self):
		result = self.build(with_horizon=False)
		self.assertIn("horizon", result)
		self.assertIsNone(result["horizon"])
		self.assertEqual(result["schema"], snapshot.RESULT_SCHEMA)
		self.assertIsNotNone(self.build()["horizon"])

	def test_a_window_past_the_800_day_guard_is_refused_not_skipped(self):
		with self.assertRaises(ProjectionError) as caught:
			self.build(window=scope("2026-11", START, "2029-03-01"))
		self.assertEqual(caught.exception.code, "invalid_range")

	def test_the_horizon_range_is_inclusive_of_29_days_after_the_anchor(self):
		end = date.fromisoformat(ANCHOR) + timedelta(days=29)
		self.assertEqual(self.horizon()["end"], end.isoformat())

	def test_result_has_the_stored_shape(self):
		result = self.build()
		self.assertEqual(
			set(result),
			{
				"schema",
				"sources",
				"projection",
				"horizon",
				"currency",
				"floor",
				"bank",
				"bank_code",
				"unavailable",
				"generated_at",
			},
		)
		self.assertEqual(result["currency"], "INR")
		self.assertEqual(result["floor"]["safety_floor"], "200.00")

	def test_the_golden_dataset_uses_the_same_builder_as_collect(self):
		with patch.object(snapshot, "build_result", wraps=snapshot.build_result) as build:
			golden.build_result()
		build.assert_called_once()
