"""Owner-approved non-cash FX exclusions must not hide or invent cash flows."""

from copy import deepcopy
from decimal import Decimal
from unittest import TestCase

from jarvis.payment_calendar.projection import project_snapshot
from jarvis.tests.payment_calendar_fixtures import require_calendar_test_site
from jarvis.tests.test_payment_calendar_projection import ANCHOR, source, term


class TestPaymentCalendarForecastFX(TestCase):
	def setUp(self):
		require_calendar_test_site()

	def inputs(self):
		ap = source(term(40), term(100, "2026-10-25", voucher="PI-2"))
		ar = source(term(20), term(200, "2026-10-26", voucher="SI-2"))
		ar["complete_for_company"] = False
		ar["scope_reasons"] = ["currency_residue_excluded"]
		ar["notes"] = {"currency_residue_excluded": {"count": 2, "amount": "0.03"}}
		ar["excluded_rows"] = [
			{
				"voucher_type": "Sales Invoice",
				"voucher_no": "FX-1",
				"party": "Customer",
				"kind": "settled_fx",
				"account_remaining": "0.00",
				"remaining": "0.04",
			},
			{
				"voucher_type": "Sales Invoice",
				"voucher_no": "FX-2",
				"party": "Customer",
				"kind": "settled_fx",
				"account_remaining": "0.00",
				"remaining": "-0.01",
			},
		]
		return ap, ar

	def project(self, ap, ar, cash="100", floor="90"):
		return project_snapshot(ap, ar, cash, floor, ANCHOR, "2026-10-23", "2026-10-26")

	def test_verified_fx_exclusions_allow_exact_orange_red_green_balances(self):
		result = self.project(*self.inputs())
		self.assertTrue(result["forecast_available"])
		self.assertEqual(result["scope_reasons"], [])
		self.assertEqual(
			[
				(result["days"][day]["projected_closing"], result["days"][day]["risk"])
				for day in ["2026-10-24", "2026-10-25", "2026-10-26"]
			],
			[("80.00", "orange"), ("-20.00", "red"), ("180.00", "green")],
		)
		self.assertIsNone(result["days"]["2026-10-23"]["risk"])
		self.assertEqual(result["source_notes"]["Receivable"]["currency_residue_excluded"]["amount"], "0.03")

	def test_payable_settlement_completeness_requires_the_same_exact_fx_proof(self):
		_, valid = self.inputs()
		for row in valid["excluded_rows"]:
			row["voucher_type"] = "Purchase Invoice"
		self.assertIs(self.project(valid, source())["payable_settlement_complete"], True)
		for field, value in [("kind", "adjustment"), ("account_remaining", "0.01")]:
			bad = deepcopy(valid)
			bad["excluded_rows"][0][field] = value
			self.assertIs(self.project(bad, source())["payable_settlement_complete"], False)
		for change in ("count", "duplicate", "scope"):
			bad = deepcopy(valid)
			if change == "count":
				bad["notes"]["currency_residue_excluded"]["count"] += 1
			elif change == "duplicate":
				bad["excluded_rows"][1]["voucher_no"] = bad["excluded_rows"][0]["voucher_no"]
			else:
				bad["scope_reasons"].append("filtered_obligations")
			self.assertIs(self.project(bad, source())["payable_settlement_complete"], False)

	def test_a_known_settled_fx_amount_is_never_new_cash_even_when_large(self):
		ap, ar = self.inputs()
		ar["excluded_rows"][0]["remaining"] = "10000.04"
		ar["notes"]["currency_residue_excluded"]["amount"] = "10000.03"
		result = self.project(ap, ar)
		self.assertTrue(result["forecast_available"])
		self.assertEqual(Decimal(result["days"][ANCHOR]["projected_closing"]), 80)

	def test_every_other_scope_failure_still_blocks_all_colours(self):
		for failure in [
			"restricted_obligations",
			"filtered_obligations",
			"owner_only_obligations",
			"masked_source_unsupported",
			"multibook_unsupported",
			"currency_reconciliation_unavailable",
			"custom_accounting_scope",
		]:
			with self.subTest(failure=failure):
				ap, ar = self.inputs()
				ar["scope_reasons"].append(failure)
				result = self.project(ap, ar)
				self.assertFalse(result["forecast_available"])
				self.assertIn(failure, result["scope_reasons"])
				self.assertTrue(all(day["risk"] is None for day in result["days"].values()))

	def test_missing_cash_or_minimum_still_blocks_colours(self):
		for cash, floor, reason in [(None, "90", "bank_unavailable"), ("100", None, "floor_unconfigured")]:
			with self.subTest(reason=reason):
				result = self.project(*self.inputs(), cash=cash, floor=floor)
				self.assertFalse(result["forecast_available"])
				self.assertIn(reason, result["scope_reasons"])

	def test_fx_rows_cannot_overlap_real_obligations_or_repeat_a_proof(self):
		for overlap in (True, False):
			ap, ar = self.inputs()
			if overlap:
				ar["excluded_rows"][0].update(
					{key: ar["rows"][0][key] for key in ("voucher_type", "voucher_no", "party")}
				)
			else:
				ar["excluded_rows"][1].update(
					{key: ar["excluded_rows"][0][key] for key in ("voucher_type", "voucher_no", "party")}
				)
			self.assertFalse(self.project(ap, ar)["forecast_available"])

	def test_settled_supplier_and_customer_fx_exclusions_do_not_change_cash(self):
		ap, ar = self.inputs()
		ap.update(
			{
				"complete_for_company": False,
				"scope_reasons": ["currency_residue_excluded"],
				"notes": {"currency_residue_excluded": {"count": 1, "amount": "-50.00"}},
				"excluded_rows": [
					{
						"voucher_type": "Purchase Invoice",
						"voucher_no": "AP-FX",
						"party": "Supplier",
						"kind": "settled_fx",
						"account_remaining": "0.00",
						"remaining": "-50.00",
					}
				],
			}
		)
		result = self.project(ap, ar)
		self.assertTrue(result["forecast_available"])
		self.assertEqual(result["days"][ANCHOR]["projected_closing"], "80.00")

	def test_unverified_malformed_or_not_settled_exclusions_do_not_waive_the_gate(self):
		_, valid = self.inputs()
		variants = []
		for field in ["notes", "excluded_rows"]:
			bad = deepcopy(valid)
			bad.pop(field)
			variants.append(bad)
		for key, value in [
			("kind", "adjustment"),
			("account_remaining", "0.01"),
			("account_remaining", None),
			("account_remaining", "0.001"),
			("voucher_type", "Journal Entry"),
			("remaining", "0.00"),
			("remaining", "NaN"),
		]:
			bad = deepcopy(valid)
			bad["excluded_rows"][0][key] = value
			variants.append(bad)
		for field, value in [("count", 3), ("count", True), ("amount", "0.04")]:
			bad = deepcopy(valid)
			bad["notes"]["currency_residue_excluded"][field] = value
			variants.append(bad)
		for index, ar in enumerate(variants):
			with self.subTest(variant=index):
				result = self.project(source(), ar)
				self.assertFalse(result["forecast_available"])
				self.assertIn("currency_residue_excluded", result["scope_reasons"])
