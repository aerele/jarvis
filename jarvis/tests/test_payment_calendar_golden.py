"""Golden Payment Calendar scenarios: the contract between the API and the #821 frontend.

Regenerate the committed JSON with `JARVIS_UPDATE_GOLDEN=1`, then
`bench --site <test site> run-tests --module jarvis.tests.test_payment_calendar_golden`.
Without the variable this module only compares and never writes.
"""

import json
import os
from decimal import Decimal
from pathlib import Path
from unittest import TestCase

from jarvis.tests import payment_calendar_golden as golden
from jarvis.tests.payment_calendar_fixtures import require_calendar_test_site

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "frontend" / "tests" / "payment-calendar" / "golden"

# Every golden file; each one has a builder in `payment_calendar_golden.SCENARIO_BUILDERS`.
SCENARIOS = {
	"default",
	"floor_140000",
	"floor_147614",
	"floor_150000",
	"floor_200000",
	"accounts_chip_removed",
	"accounts_none",
	"accounts_50",
	"month_net_negative",
	"month_net_zero",
	"grouped_invoice_terms",
	"star_exactly_50pct",
	"month_oct",
	"month_dec",
	# Detail and bucket replies of the default scenario (B7, B8): request + reply as the API sends them.
	"default_day_payable",
	"default_day_receivable",
	"default_bucket_immediate",
	"default_bucket_now",
	"default_bucket_today",
	"default_bucket_window_payable",
	"default_bucket_window_receivable",
	"default_bucket_window_payable_party_asc",
	# The "Show all accounts" status reply for the whole 60-account catalog (B5).
	"balances_complete",
}


def D(value):
	return Decimal(value)


class TestPaymentCalendarGolden(TestCase):
	def setUp(self):
		require_calendar_test_site()

	def test_every_scenario_has_a_builder(self):
		self.assertEqual(set(golden.SCENARIO_BUILDERS), SCENARIOS)

	def test_committed_files_match_regenerated_output(self):
		for name in golden.SCENARIO_BUILDERS:
			rendered = golden.render(name)
			path = GOLDEN_DIR / f"{name}.json"
			if os.environ.get("JARVIS_UPDATE_GOLDEN") == "1":
				GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
				path.write_text(rendered, encoding="utf-8")
			self.assertTrue(path.exists(), f"missing golden file {path.name}; regenerate it")
			self.assertEqual(path.read_text(encoding="utf-8"), rendered, name)

	def test_no_committed_file_lacks_a_scenario(self):
		committed = {path.stem for path in GOLDEN_DIR.glob("*.json")}
		self.assertLessEqual(committed, set(SCENARIOS))

	def test_regeneration_is_byte_identical(self):
		for name in golden.SCENARIO_BUILDERS:
			self.assertEqual(golden.render(name), golden.render(name), name)

	def test_output_is_normalised_json_with_sorted_keys(self):
		text = golden.render("default")
		data = json.loads(text)
		self.assertEqual(text, json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
		self.assertEqual(data["status"]["snapshot_id"], golden.SNAPSHOT_ID)
		self.assertEqual(data["status"]["generated_at"], golden.PLACEHOLDER_TIME)
		self.assertEqual(data["status"]["expires_at"], golden.PLACEHOLDER_TIME)

	def status(self, name="default"):
		return json.loads(golden.render(name))["status"]

	def test_default_cash_floor_and_due_now_match_the_template(self):
		status = self.status()
		projection = status["projection"]
		self.assertEqual(status["scope"]["anchor"], "2026-11-11")
		self.assertEqual(status["scope"]["company"], "Northstar Manufacturing Pvt Ltd")
		self.assertEqual(status["scope"]["month"], "2026-11")
		self.assertEqual((projection["book_cash"], projection["floor"]), ("246850.00", "50000.00"))
		self.assertEqual(
			projection["due_now"],
			{
				"scheduled": "42048.00",
				"overdue": "22500.00",
				"undated": "5000.00",
				"immediate": "27500.00",
				"total": "69548.00",
			},
		)
		self.assertIs(projection["forecast_available"], True)

	def test_default_month_net_is_the_template_surplus(self):
		self.assertEqual(
			self.status()["projection"]["month_totals"],
			{
				"receivable": "930600.00",
				"payable": "600898.00",
				"net": "329702.00",
				# Rows the window bucket lists: the five settled-zero payable terms count.
				"receivable_rows": 59,
				"payable_rows": 54,
				"complete": True,
			},
		)

	def test_default_cash_identity_holds_exactly_on_every_day_from_the_anchor(self):
		projection = self.status()["projection"]
		due_now = projection["due_now"]
		days = [d for date, d in projection["days"].items() if date >= "2026-11-11"]
		self.assertEqual(len(days), 20)
		for day in days:
			expected = (
				D(projection["book_cash"])
				+ D(day["cumulative_receivable"])
				- D(day["cumulative_payable"])
				- D(due_now["overdue"])
				- D(due_now["undated"])
			)
			self.assertEqual(D(day["projected_closing"]), expected, day["date"])
			self.assertEqual(D(day["margin"]), D(day["projected_closing"]) - D(projection["floor"]))

	def test_default_cumulative_sums_start_at_zero_on_the_anchor_and_skip_earlier_payables(self):
		days = self.status()["projection"]["days"]
		self.assertEqual(days["2026-11-11"]["cumulative_payable"], "42048.00")
		self.assertEqual(days["2026-11-11"]["cumulative_receivable"], "86250.00")
		self.assertEqual(days["2026-11-11"]["projected_closing"], "263552.00")
		for date in ("2026-11-04", "2026-11-10"):
			self.assertIsNone(days[date]["cumulative_payable"], date)
			self.assertIsNone(days[date]["projected_closing"], date)
		# Payables dated 2-10 Nov (22,500 in total) reach cash only through due_now.overdue.
		self.assertEqual(days["2026-11-04"]["payable_remaining"], "4500.00")

	def test_default_day_net_is_receivable_minus_payable(self):
		days = self.status()["projection"]["days"]
		self.assertEqual(days["2026-11-11"]["net"], "44202.00")
		self.assertEqual(days["2026-11-13"]["net"], "-40457.00")
		for day in days.values():
			self.assertEqual(
				D(day["net"]), D(day["receivable_remaining"]) - D(day["payable_remaining"]), day["date"]
			)

	def test_default_lowest_day_is_23_november_with_a_97614_cushion(self):
		days = self.status()["projection"]["days"]
		self.assertEqual(days["2026-11-23"]["projected_closing"], "147614.00")
		self.assertEqual(days["2026-11-23"]["margin"], "97614.00")
		closings = [D(d["projected_closing"]) for date, d in days.items() if date >= "2026-11-11"]
		self.assertEqual(min(closings), D("147614.00"))
		self.assertEqual(closings.index(min(closings)), 12)

	def test_default_star_is_on_19_november_and_exactly_half_gets_none(self):
		days = self.status()["projection"]["days"]
		# A single-invoice day is 100 % concentrated, so the template rule starts it too.
		starred = {date for date, day in days.items() if day["payable_concentrated"]}
		self.assertIn("2026-11-19", starred)
		self.assertNotIn("2026-11-17", starred)
		self.assertNotIn("2026-11-24", starred)
		self.assertTrue(all(days[date]["payable_concentration_pct"] for date in starred))
		day = days["2026-11-19"]
		self.assertEqual(day["payable_remaining"], "102236.00")
		self.assertEqual(day["payable_concentration_pct"], "100.0")
		self.assertEqual(day["payable_top_invoice"]["voucher_no"], "PINV-2026-0207")
		self.assertEqual(day["payable_top_invoice"]["party_name"], "Oakridge Industries")
		half = days["2026-11-17"]
		self.assertEqual(half["payable_concentration_pct"], "50.0")
		self.assertEqual(half["payable_top_invoice"]["voucher_no"], "PINV-2026-0173")

	def test_default_day_fields_for_settled_empty_and_receivable_days(self):
		days = self.status()["projection"]["days"]
		settled = days["2026-11-24"]
		self.assertEqual((settled["settlement"], settled["payable_rows"]), ("Settled", 1))
		self.assertIsNone(settled["payable_top_invoice"])
		self.assertEqual(days["2026-11-30"]["receivable_settlement"], "Unpaid")
		self.assertEqual(days["2026-11-12"]["receivable_remaining"], "19500.00")
		self.assertEqual(days["2026-11-13"]["receivable_settlement"], "No dues")

	def horizon(self, name):
		return self.status(name)["horizon"]

	def test_default_horizon_lowest_is_23_november_with_a_safe_97614_cushion(self):
		horizon = self.horizon("default")
		self.assertEqual((horizon["start"], horizon["end"]), ("2026-11-11", "2026-12-10"))
		self.assertEqual(len(horizon["days"]), 30)
		self.assertEqual(
			horizon["lowest"],
			{"date": "2026-11-23", "closing": "147614.00", "cushion": "97614.00", "state": "safe"},
		)
		self.assertIsNone(horizon["first_below_floor"])
		self.assertIs(horizon["forecast_available"], True)

	def test_horizon_agrees_with_the_month_projection_on_every_november_day(self):
		status = self.status()
		for iso, day in status["horizon"]["days"].items():
			if iso <= "2026-11-30":
				month_day = status["projection"]["days"][iso]
				self.assertEqual(day["projected_closing"], month_day["projected_closing"], iso)
				self.assertEqual(day["margin"], month_day["margin"], iso)
		self.assertIsNotNone(status["horizon"]["days"]["2026-12-10"]["projected_closing"])

	def test_floor_scenarios_give_each_lowest_state_from_the_same_closing(self):
		cases = (
			("floor_140000", "tight", "7614.00"),  # amber: inside 10 % of the floor
			("floor_147614", "tight", "0.00"),
			("floor_150000", "below", "-2386.00"),
			("floor_200000", "below", "-52386.00"),
		)
		for name, state, cushion in cases:
			with self.subTest(name):
				lowest = self.horizon(name)["lowest"]
				self.assertEqual(
					(lowest["date"], lowest["closing"], lowest["cushion"], lowest["state"]),
					("2026-11-23", "147614.00", cushion, state),
				)
				self.assertEqual(self.status(name)["projection"]["floor"], name[-6:] + ".00")

	def test_floor_150000_first_below_floor_is_the_first_closing_under_it(self):
		horizon = self.horizon("floor_150000")
		days = horizon["days"]
		first = horizon["first_below_floor"]
		self.assertIsNotNone(first)
		self.assertLess(D(days[first]["projected_closing"]), D("150000"))
		earlier = [d for iso, d in days.items() if iso < first]
		self.assertTrue(all(D(d["projected_closing"]) >= D("150000") for d in earlier))

	def test_floor_200000_first_below_floor_is_19_november(self):
		self.assertEqual(self.horizon("floor_200000")["first_below_floor"], "2026-11-19")

	def test_default_and_tight_floors_never_go_below(self):
		for name in ("default", "floor_140000", "floor_147614"):
			self.assertIsNone(self.horizon(name)["first_below_floor"], name)

	def test_without_accounts_the_horizon_is_unavailable(self):
		status = self.status("accounts_none")
		horizon = status["horizon"]
		self.assertIs(horizon["forecast_available"], False)
		self.assertIn("bank_unavailable", horizon["scope_reasons"])
		self.assertIsNone(horizon["lowest"])
		self.assertIsNone(horizon["first_below_floor"])
		self.assertIsNone(status["projection"]["book_cash"])

	def reply(self, name):
		return json.loads(golden.render(name))

	def test_day_detail_golden_lists_the_template_invoices_with_settled(self):
		payable = self.reply("default_day_payable")
		self.assertEqual(
			payable["request"],
			{
				"kind": "day",
				"date": "2026-11-11",
				"side": "Payable",
				"page": 1,
				"page_size": 100,
				"sort": None,
				"order": None,
			},
		)
		rows = payable["data"]["rows"]
		vouchers = [r["voucher_no"] for r in rows]
		self.assertEqual(vouchers, ["PINV-2026-0142", "PINV-2026-0148", "PINV-2026-0151"])
		self.assertEqual([r["settled"] for r in rows], ["6500.00", "0.00", "0.00"])
		self.assertEqual(payable["data"]["total_remaining"], "42048.00")
		self.assertEqual(self.reply("default_day_receivable")["data"]["total_remaining"], "86250.00")
		for name in ("default_day_payable", "default_day_receivable"):
			for row in self.reply(name)["data"]["rows"]:
				self.assertNotIn("source_link", row)

	def test_bucket_goldens_equal_their_kpis(self):
		due_now = self.status()["projection"]["due_now"]
		self.assertEqual(self.reply("default_bucket_today")["data"]["total_remaining"], due_now["scheduled"])
		self.assertEqual(
			self.reply("default_bucket_immediate")["data"]["total_remaining"], due_now["immediate"]
		)
		self.assertEqual(self.reply("default_bucket_now")["data"]["total_remaining"], due_now["total"])
		totals = self.status()["projection"]["month_totals"]
		for side in ("payable", "receivable"):
			window = self.reply(f"default_bucket_window_{side}")["data"]
			self.assertEqual(window["total_remaining"], totals[side])
			self.assertEqual(window["total"], totals[f"{side}_rows"])
			self.assertEqual(len(window["rows"]), totals[f"{side}_rows"])

	def test_sorted_variant_orders_the_window_by_party_with_undated_rows_absent(self):
		golden_reply = self.reply("default_bucket_window_payable_party_asc")
		self.assertEqual(golden_reply["request"]["sort"], "party")
		self.assertEqual(golden_reply["request"]["order"], "asc")
		names = [r["party_name"].casefold() for r in golden_reply["data"]["rows"]]
		self.assertEqual(names, sorted(names))

	def test_month_snapshots_golden_carry_the_same_today_and_now_kpis(self):
		for name in ("month_oct", "month_dec"):
			with self.subTest(name):
				status = self.status(name)
				self.assertEqual(status["projection"]["due_now"]["total"], "69548.00")
				self.assertEqual(status["projection"]["due_now"]["scheduled"], "42048.00")
		self.assertEqual(self.status("month_oct")["projection"]["month_totals"]["net"], "0.00")
		self.assertEqual(self.status("month_dec")["projection"]["month_totals"]["net"], "-5000.00")

	def test_default_bank_block_names_each_account_with_bank_type_and_masked_reference(self):
		bank = self.status()["bank"]
		self.assertEqual((bank["total"], bank["currency"], bank["precision"]), ("246850.00", "INR", 2))
		self.assertEqual(
			bank["accounts"],
			[
				{
					"account": "HDFC Current",
					"account_name": "HDFC Current",
					"account_type": "Bank",
					"bank": "HDFC Bank",
					"reference": "\u2022\u2022\u2022\u2022 3281",
					"balance": "221850.00",
				},
				# Cash accounts have no Bank Account: the labels are null, as D6 gives them.
				{
					"account": "Petty Cash",
					"account_name": "Petty Cash",
					"account_type": "Cash",
					"bank": None,
					"reference": None,
					"balance": "25000.00",
				},
			],
		)

	def test_accounts_none_has_no_bank_block(self):
		self.assertIsNone(self.status("accounts_none")["bank"])

	def test_chip_removed_keeps_only_hdfc_current(self):
		status = self.status("accounts_chip_removed")
		self.assertEqual(status["scope"]["accounts"], ["HDFC Current"])
		self.assertEqual([a["account"] for a in status["bank"]["accounts"]], ["HDFC Current"])
		self.assertEqual(status["bank"]["total"], "221850.00")
		self.assertEqual(status["projection"]["book_cash"], "221850.00")
		self.assertEqual(status["projection"]["days"]["2026-11-11"]["projected_closing"], "238552.00")
		self.assertIs(status["horizon"]["forecast_available"], True)

	def test_fifty_accounts_come_from_the_sixty_account_catalog(self):
		self.assertEqual(len(golden.CATALOG), 60)
		status = self.status("accounts_50")
		accounts = status["bank"]["accounts"]
		self.assertEqual(len(accounts), 50)
		self.assertEqual(sorted(status["scope"]["accounts"]), sorted(a["account"] for a in accounts))
		self.assertEqual(D(status["bank"]["total"]), sum((D(a["balance"]) for a in accounts), D(0)))
		self.assertEqual(status["projection"]["book_cash"], status["bank"]["total"])
		self.assertEqual({a["account_type"] for a in accounts}, {"Bank", "Cash"})
		self.assertTrue(all(a["account_name"] for a in accounts))
		hdfc = accounts[0]
		self.assertEqual((hdfc["account_name"], hdfc["bank"]), ("HDFC Current", "HDFC Bank"))
		self.assertEqual(hdfc["reference"], "\u2022\u2022\u2022\u2022 3281")
		banks = {a["bank"] for a in accounts if a["bank"]}
		self.assertGreaterEqual(len(banks), 4)

	def test_fifty_accounts_lowest_closing_has_its_own_compact_label(self):
		self.assert_lowest_closing_label_is_unique("accounts_50")
		days = self.status("accounts_50")["horizon"]["days"]
		lowest = min(D(day["projected_closing"]) for day in days.values())
		self.assertEqual(days["2026-11-23"]["projected_closing"], str(lowest))

	def test_adjustment_rows_have_nothing_paid(self):
		for side in ("Payable", "Receivable"):
			for row in golden.ADJUSTMENT_ROWS[side]:
				built = golden._adjustment_row(side, 0, *row)
				self.assertEqual((built["original"], built["paid"]), ("0.00", "0.00"))

	def test_balances_golden_is_the_flat_status_reply_for_the_whole_catalog(self):
		golden_reply = self.reply("balances_complete")
		self.assertEqual(set(golden_reply), {"scenario", "request", "data"})
		self.assertEqual(
			golden_reply["request"],
			{"kind": "balances", "company": golden.COMPANY, "job_id": golden.SNAPSHOT_ID},
		)
		data = golden_reply["data"]
		self.assertEqual(
			(data["job_id"], data["state"], data["code"]), (golden.SNAPSHOT_ID, "complete", None)
		)
		self.assertEqual((data["currency"], data["precision"]), ("INR", 2))
		self.assertEqual(data["generated_at"], golden.PLACEHOLDER_TIME)
		self.assertEqual(data["expires_at"], golden.PLACEHOLDER_TIME)
		accounts = data["accounts"]
		self.assertEqual([row["account"] for row in accounts], [name for name, *_ in golden.CATALOG])
		self.assertEqual(D(data["total"]), sum((D(row["balance"]) for row in accounts), D(0)))
		self.assertNotIn("bank", data)
		self.assertNotIn("scope", data)
		hdfc, cash = accounts[0], accounts[1]
		self.assertEqual((hdfc["bank"], hdfc["reference"]), ("HDFC Bank", "\u2022\u2022\u2022\u2022 3281"))
		self.assertEqual((cash["account_type"], cash["bank"], cash["reference"]), ("Cash", None, None))

	def day(self, name, date="2026-11-25"):
		return self.status(name)["projection"]["days"][date]

	def assert_lowest_closing_label_is_unique(self, name):
		# Quiet days repeat a closing by design; a different amount must not share the lowest day's label.
		closings = [D(d["projected_closing"]) for d in self.status(name)["horizon"]["days"].values()]
		lowest = min(closings)
		label = round(lowest / 100000, 1)
		self.assertNotIn(label, [round(v / 100000, 1) for v in closings if v != lowest], name)

	def test_month_net_negative_is_a_deficit_from_one_named_payable(self):
		status = self.status("month_net_negative")
		totals = status["projection"]["month_totals"]
		self.assertEqual(
			(totals["receivable"], totals["payable"], totals["net"]),
			("930600.00", "1000898.00", "-70298.00"),
		)
		self.assertEqual((totals["payable_rows"], totals["complete"]), (55, True))
		self.assertEqual(
			self.day("month_net_negative")["payable_top_invoice"]["voucher_no"], "PINV-2026-0301"
		)
		self.assertEqual(self.day("month_net_negative")["payable_remaining"], "400000.00")
		self.assert_lowest_closing_label_is_unique("month_net_negative")

	def test_month_net_zero_is_exactly_zero_not_a_rounding_artefact(self):
		totals = self.status("month_net_zero")["projection"]["month_totals"]
		self.assertEqual(
			(totals["receivable"], totals["payable"], totals["net"]), ("930600.00", "930600.00", "0.00")
		)
		self.assertEqual((totals["payable_rows"], totals["complete"]), (55, True))
		self.assertEqual(self.day("month_net_zero")["payable_top_invoice"]["voucher_no"], "PINV-2026-0302")
		self.assert_lowest_closing_label_is_unique("month_net_zero")

	def test_grouped_terms_of_one_invoice_decide_the_star_by_their_sum(self):
		starred = self.day("grouped_invoice_terms")
		# Each term is 30 % of the day; the invoice they form is 60 %, so the star appears.
		self.assertEqual(
			(starred["payable_remaining"], starred["payable_terms"], starred["payable_invoices"]),
			("100000.00", 3, 2),
		)
		self.assertEqual(
			starred["payable_top_invoice"],
			{
				"voucher_type": "Purchase Invoice",
				"voucher_no": "PINV-2026-0311",
				"party_name": "Atlas Components",
				"remaining": "60000.00",
			},
		)
		self.assertEqual(
			(starred["payable_concentration_pct"], starred["payable_concentrated"]), ("60.0", True)
		)
		term_rows = [
			r
			for r in self.reply("grouped_invoice_terms")["day_detail"]["data"]["rows"]
			if r["voucher_no"] == "PINV-2026-0311"
		]
		self.assertEqual([r["remaining"] for r in term_rows], ["30000.00", "30000.00"])
		self.assertEqual([r["term"] for r in term_rows], ["Term 1 of 2", "Term 2 of 2"])
		self.assert_lowest_closing_label_is_unique("grouped_invoice_terms")

	def test_grouped_terms_that_stay_below_half_give_no_star(self):
		plain = self.day("grouped_invoice_terms", "2026-11-26")
		self.assertEqual(
			(plain["payable_remaining"], plain["payable_terms"], plain["payable_invoices"]),
			("105000.00", 5, 4),
		)
		self.assertEqual(plain["payable_top_invoice"]["voucher_no"], "PINV-2026-0321")
		self.assertEqual(plain["payable_top_invoice"]["remaining"], "42000.00")
		self.assertEqual((plain["payable_concentration_pct"], plain["payable_concentrated"]), ("40.0", False))

	def test_grouped_scenario_carries_the_day_detail_that_lists_the_terms(self):
		reply = self.reply("grouped_invoice_terms")
		self.assertEqual(set(reply), {"scenario", "status", "day_detail"})
		self.assertEqual(reply["day_detail"]["request"]["date"], "2026-11-25")
		self.assertEqual(reply["day_detail"]["data"]["total_remaining"], "100000.00")

	def test_exactly_half_gets_no_star_and_a_one_decimal_share(self):
		day = self.day("star_exactly_50pct")
		self.assertEqual(day["payable_remaining"], "100000.00")
		self.assertEqual(day["payable_top_invoice"]["voucher_no"], "PINV-2026-0331")
		self.assertEqual(day["payable_top_invoice"]["remaining"], "50000.00")
		self.assertEqual((day["payable_concentration_pct"], day["payable_concentrated"]), ("50.0", False))
		self.assert_lowest_closing_label_is_unique("star_exactly_50pct")
