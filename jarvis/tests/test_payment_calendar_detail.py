"""Detail and bucket replies: sorting, settled, bucket totals tied to the KPIs (#821 B7, B8)."""

import json
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import detail
from jarvis.tests import payment_calendar_golden as golden
from jarvis.tests.payment_calendar_fixtures import require_calendar_test_site

MONTHS = ("2026-10", "2026-11", "2026-12")
PAYABLE_ONLY = ("today", "immediate", "now")
BIG = {"page": 1, "page_size": 100, "party_page": 1}


def D(value):
	return Decimal(value)


def make_row(row_id, due, remaining, party="Party", original=None, kind="obligation"):
	"""A source row in the shape the API stores; only the fields the detail code reads matter."""
	original = remaining if original is None else original
	row = golden._row("Payable", due, 0, party, "PINV-" + row_id, remaining, original)
	return {**row, "id": row_id, "kind": kind}


class DetailCase(TestCase):
	def setUp(self):
		require_calendar_test_site()

	def reply(self, month="2026-11", kind="day", **kwargs):
		scope, result = golden.build_result(month=month)
		status = {"scope": scope}
		if kind == "day":
			args = {"date": "2026-11-11", "side": "Payable", **kwargs}
			return detail.day_detail(status, result, **{**BIG, "sort": None, "order": None, **args})
		args = {"side": "Payable", **kwargs}
		return detail.bucket_detail(status, result, **{**BIG, "sort": None, "order": None, **args})

	def ids(self, rows):
		return [row["id"] for row in rows]


class TestSortRows(DetailCase):
	def rows(self):
		return [
			make_row("c", "2026-11-12", "300.00", "cedar"),
			make_row("u1", None, "900.00", "Zeta"),
			make_row("a", "2026-11-10", "100.00", "Banyan"),
			make_row("b", "2026-11-11", "200.00", "apex"),
			make_row("u2", None, "50.00", "Alpha"),
		]

	def test_default_is_remaining_descending_with_undated_last(self):
		self.assertEqual(self.ids(detail.sort_rows(self.rows(), None, None)), ["c", "b", "a", "u1", "u2"])

	def test_every_key_in_both_directions_puts_undated_rows_last(self):
		expected = {
			("remaining", "asc"): ["a", "b", "c", "u2", "u1"],
			("remaining", "desc"): ["c", "b", "a", "u1", "u2"],
			("due_date", "asc"): ["a", "b", "c", "u1", "u2"],
			("due_date", "desc"): ["c", "b", "a", "u1", "u2"],
			("party", "asc"): ["b", "a", "c", "u2", "u1"],
			("party", "desc"): ["c", "a", "b", "u1", "u2"],
		}
		for (sort, order), ids in expected.items():
			with self.subTest(sort=sort, order=order):
				self.assertEqual(self.ids(detail.sort_rows(self.rows(), sort, order)), ids)

	def test_remaining_compares_as_decimal_not_as_text(self):
		rows = [make_row("x", "2026-11-10", "9.00"), make_row("y", "2026-11-10", "10.00")]
		self.assertEqual(self.ids(detail.sort_rows(rows, "remaining", "asc")), ["x", "y"])

	def test_ties_fall_back_to_id_ascending_in_both_directions(self):
		rows = [make_row(i, "2026-11-10", "5.00", "Same") for i in ("m", "k", "z")]
		for sort in ("remaining", "due_date", "party"):
			for order in ("asc", "desc"):
				with self.subTest(sort=sort, order=order):
					self.assertEqual(self.ids(detail.sort_rows(rows, sort, order)), ["k", "m", "z"])

	def test_party_sort_ignores_case_but_never_rewrites_the_name(self):
		ordered = detail.sort_rows(self.rows(), "party", "asc")
		names = [row["party_name"] for row in ordered if row["due_date"]]
		self.assertEqual(names, ["apex", "Banyan", "cedar"])

	def test_invalid_sort_or_order_is_a_validation_error(self):
		bad = (("bogus", "asc"), ("remaining", "up"), ("", "asc"), ("remaining", ""), (5, "asc"))
		for sort, order in bad:
			with self.subTest(sort=sort, order=order), self.assertRaises(frappe.ValidationError):
				detail.sort_rows(self.rows(), sort, order)

	def test_omitted_sort_or_order_uses_the_defaults_independently(self):
		self.assertEqual(
			self.ids(detail.sort_rows(self.rows(), "due_date", None)), ["c", "b", "a", "u1", "u2"]
		)
		self.assertEqual(self.ids(detail.sort_rows(self.rows(), None, "asc")), ["a", "b", "c", "u2", "u1"])


class TestSettled(DetailCase):
	def settled(self, remaining, original, kind="obligation"):
		row = make_row("s", "2026-11-10", remaining, original=original, kind=kind)
		return detail.public_row(row, 2)["settled"]

	def test_settled_is_original_minus_remaining_at_the_same_precision(self):
		self.assertEqual(self.settled("18500.00", "25000.00"), "6500.00")
		self.assertEqual(self.settled("0.00", "12500.00"), "12500.00")
		self.assertEqual(self.settled("100.00", "100.00"), "0.00")

	def test_settled_is_null_for_adjustments_non_positive_originals_and_overpaid_rows(self):
		self.assertIsNone(self.settled("10.00", "25.00", kind="adjustment"))
		self.assertIsNone(self.settled("0.00", "0.00"))
		self.assertIsNone(self.settled("5.00", "-3.00"))
		self.assertIsNone(self.settled("30.00", "25.00"))

	def test_settled_is_null_for_a_negative_remaining_even_on_an_obligation(self):
		self.assertIsNone(self.settled("-5.00", "20.00"))
		self.assertEqual(self.settled("0.00", "20.00"), "20.00")

	def test_public_rows_carry_no_source_link_and_no_private_keys(self):
		for row in self.reply()["rows"]:
			self.assertNotIn("source_link", row)
			self.assertFalse([key for key in row if key.startswith("_")], row["id"])
			self.assertIn("settled", row)


class TestDetailReplies(DetailCase):
	def test_day_detail_defaults_to_remaining_descending(self):
		rows = self.reply()["rows"]
		self.assertEqual([r["remaining"] for r in rows], ["18500.00", "14280.00", "9268.00"])

	def test_day_detail_sorts_before_paging(self):
		first = self.reply(sort="party", order="asc", page_size=1)
		self.assertEqual(first["rows"][0]["party_name"], "Atlas Components")
		self.assertEqual(first["total"], 3)
		self.assertTrue(first["has_more"])

	def test_party_names_come_back_exactly_as_stored(self):
		names = {row["party_name"] for row in self.reply(sort="party", order="desc")["rows"]}
		self.assertEqual(names, {"Cedar Trading Co.", "Atlas Components", "Meridian Packaging"})

	def test_only_the_rows_of_the_page_are_made_public(self):
		with patch.object(detail, "public_row", wraps=detail.public_row) as public:
			reply = self.reply(page_size=2)
		self.assertGreater(reply["total"], 2)
		self.assertEqual(public.call_count, len(reply["rows"]))

	def test_day_total_remaining_sums_all_rows_not_just_the_page(self):
		reply = self.reply(page_size=1)
		self.assertEqual(len(reply["rows"]), 1)
		self.assertEqual(reply["total_remaining"], "42048.00")
		receivable = self.reply(side="Receivable")
		self.assertEqual(receivable["total_remaining"], "86250.00")

	def test_total_remaining_is_a_two_decimal_string_even_for_no_rows(self):
		reply = self.reply(date="2026-11-30", side="Payable")
		self.assertEqual(reply["total_remaining"], "4732.00")
		self.assertEqual(self.reply(date="2026-11-24")["total_remaining"], "0.00")

	def test_invalid_sort_is_refused_before_any_row_is_read(self):
		with patch.object(detail, "_source") as read:
			with self.assertRaises(frappe.ValidationError):
				self.reply(sort="bogus")
			with self.assertRaises(frappe.ValidationError):
				self.reply(kind="bucket", bucket="today", order="sideways")
		read.assert_not_called()

	def test_new_replies_keep_the_existing_envelope_keys(self):
		reply = self.reply()
		keys = ("rows", "total", "page", "page_size", "has_more", "parties", "party_total", "generated_at")
		for key in keys:
			self.assertIn(key, reply)
		json.dumps(reply)


class TestBucketTotalsEqualKpis(DetailCase):
	def test_today_is_positive_payables_due_on_the_anchor_on_every_month_snapshot(self):
		for month in MONTHS:
			with self.subTest(month=month):
				scope, result = golden.build_result(month=month)
				reply = self.reply(month=month, kind="bucket", bucket="today")
				due_now = result["projection"]["due_now"]
				self.assertEqual(reply["total_remaining"], due_now["scheduled"])
				self.assertEqual({r["due_date"] for r in reply["rows"]}, {"2026-11-11"})
				self.assertTrue(all(D(r["remaining"]) > 0 for r in reply["rows"]))
				self.assertEqual(
					self.ids(reply["rows"]),
					self.ids(self.reply(kind="bucket", bucket="today")["rows"]),
				)

	def test_today_leaves_out_settled_zero_rows_that_day_detail_lists(self):
		scope, result = golden.build_result()
		rows = [make_row("z", "2026-11-11", "0.00", original="500.00")]
		result["sources"]["Payable"]["rows"] += rows
		golden.reindex(scope, result)
		reply = detail.bucket_detail(
			{"scope": scope}, result, bucket="today", side="Payable", sort=None, order=None, **BIG
		)
		self.assertNotIn("z", self.ids(reply["rows"]))
		day = detail.day_detail(
			{"scope": scope}, result, date="2026-11-11", side="Payable", sort=None, order=None, **BIG
		)
		self.assertIn("z", self.ids(day["rows"]))

	def test_immediate_is_overdue_plus_undated_and_equals_the_kpi(self):
		for month in MONTHS:
			with self.subTest(month=month):
				_, result = golden.build_result(month=month)
				reply = self.reply(month=month, kind="bucket", bucket="immediate")
				self.assertEqual(reply["total_remaining"], result["projection"]["due_now"]["immediate"])
				self.assertEqual(reply["total_remaining"], "27500.00")
				for row in reply["rows"]:
					self.assertTrue(row["due_date"] is None or row["due_date"] < "2026-11-11")

	def test_now_is_today_plus_immediate_and_equals_the_kpi(self):
		for month in MONTHS:
			with self.subTest(month=month):
				_, result = golden.build_result(month=month)
				reply = self.reply(month=month, kind="bucket", bucket="now")
				self.assertEqual(reply["total_remaining"], result["projection"]["due_now"]["total"])
				self.assertEqual(reply["total_remaining"], "69548.00")
				today = self.reply(month=month, kind="bucket", bucket="today")["rows"]
				immediate = self.reply(month=month, kind="bucket", bucket="immediate")["rows"]
				self.assertEqual(
					sorted(self.ids(reply["rows"])), sorted(self.ids(today) + self.ids(immediate))
				)

	def test_payable_only_buckets_refuse_receivables_on_every_snapshot(self):
		for month in MONTHS:
			for bucket in PAYABLE_ONLY:
				with self.subTest(month=month, bucket=bucket), self.assertRaises(frappe.ValidationError):
					self.reply(month=month, kind="bucket", bucket=bucket, side="Receivable")

	def test_window_total_equals_month_totals_for_both_sides(self):
		for month in MONTHS:
			for side in ("Payable", "Receivable"):
				with self.subTest(month=month, side=side):
					_, result = golden.build_result(month=month)
					totals = result["projection"]["month_totals"]
					reply = self.reply(month=month, kind="bucket", bucket="window", side=side)
					self.assertEqual(reply["total_remaining"], totals[side.lower()])

	def test_window_lists_settled_zero_rows_and_its_length_is_the_tab_count(self):
		for month in ("2026-11", "2026-12"):
			for side in ("Payable", "Receivable"):
				with self.subTest(month=month, side=side):
					_, result = golden.build_result(month=month)
					totals = result["projection"]["month_totals"]
					rows = self.reply(month=month, kind="bucket", bucket="window", side=side)["rows"]
					self.assertEqual(len(rows), totals[side.lower() + "_rows"])
		nov = self.reply(kind="bucket", bucket="window")
		self.assertTrue([r for r in nov["rows"] if D(r["remaining"]) == 0])
		self.assertEqual(nov["total"], len(nov["rows"]))

	def test_payable_only_buckets_refuse_receivables_before_any_source_read(self):
		for code in ("forbidden", "too_large"):
			scope, result = golden.build_result()
			result["sources"]["Receivable"] = None
			result["unavailable"] = {"Receivable": code}
			for bucket in PAYABLE_ONLY:
				with self.subTest(code=code, bucket=bucket), patch.object(detail, "_source") as read:
					with self.assertRaises(frappe.ValidationError):
						detail.bucket_detail(
							{"scope": scope},
							result,
							bucket=bucket,
							side="Receivable",
							sort=None,
							order=None,
							**BIG,
						)
					read.assert_not_called()

	def test_reconciliation_bucket_totals_are_signed_and_equal_the_reconciliation(self):
		for month in MONTHS:
			_, result = golden.build_result(month=month)
			for side in ("Payable", "Receivable"):
				reconciliation = result["projection"]["reconciliation"][side]
				for bucket in ("overdue", "undated", "adjustments", "outside_horizon"):
					with self.subTest(month=month, side=side, bucket=bucket):
						reply = self.reply(month=month, kind="bucket", bucket=bucket, side=side)
						self.assertEqual(reply["total_remaining"], reconciliation[bucket])

	def test_adjustments_bucket_lists_the_negative_rows_with_a_negative_total(self):
		payable = self.reply(kind="bucket", bucket="adjustments")
		self.assertEqual(payable["total_remaining"], "-3217.00")
		self.assertEqual(self.ids(payable["rows"]), ["adjustment-payable-0"])
		self.assertIsNone(payable["rows"][0]["settled"])
		receivable = self.reply(kind="bucket", bucket="adjustments", side="Receivable")
		self.assertEqual(receivable["total_remaining"], "-1830.00")

	def test_window_leaves_out_negative_rows_and_lists_positive_adjustment_kind_rows(self):
		scope, result = golden.build_result()
		rows = result["sources"]["Payable"]["rows"]
		rows.append(make_row("pos_adj", "2026-11-20", "100.00", original="0.00", kind="adjustment"))
		golden.reindex(scope, result)
		reply = detail.bucket_detail(
			{"scope": scope}, result, bucket="window", side="Payable", sort=None, order=None, **BIG
		)
		listed = self.ids(reply["rows"])
		self.assertNotIn("adjustment-payable-0", listed)  # dated in the window, but negative
		self.assertIn("pos_adj", listed)
		self.assertTrue(all(D(r["remaining"]) >= 0 for r in reply["rows"]))
		self.assertEqual(reply["total_remaining"], result["projection"]["month_totals"]["payable"])

	def test_window_rows_stay_inside_the_snapshot_window(self):
		rows = self.reply(month="2026-12", kind="bucket", bucket="window", side="Receivable")["rows"]
		self.assertTrue(rows)
		self.assertTrue(all("2026-12-01" <= r["due_date"] <= "2026-12-31" for r in rows))

	def test_window_total_is_null_when_the_month_totals_are_incomplete(self):
		scope, result = golden.build_result()
		result["sources"]["Receivable"]["complete_for_company"] = False
		result["projection"]["month_totals"] = {
			**dict.fromkeys(("receivable", "payable", "net", "receivable_rows", "payable_rows")),
			"complete": False,
		}
		golden.reindex(scope, result)
		reply = detail.bucket_detail(
			{"scope": scope}, result, bucket="window", side="Payable", sort=None, order=None, **BIG
		)
		self.assertIsNone(reply["total_remaining"])
		self.assertTrue(reply["rows"])

	def test_window_total_is_null_without_month_totals(self):
		scope, result = golden.build_result()
		result["projection"]["month_totals"] = None
		golden.reindex(scope, result)
		reply = detail.bucket_detail(
			{"scope": scope}, result, bucket="window", side="Payable", sort=None, order=None, **BIG
		)
		self.assertIsNone(reply["total_remaining"])

	def test_existing_buckets_still_answer_with_a_total(self):
		_, result = golden.build_result()
		for bucket, key in (("overdue", "overdue"), ("undated", "undated")):
			reply = self.reply(kind="bucket", bucket=bucket)
			self.assertEqual(reply["total_remaining"], result["projection"]["reconciliation"]["Payable"][key])
		receivable = self.reply(kind="bucket", bucket="overdue", side="Receivable")
		self.assertTrue(receivable["rows"])
		self.assertEqual(
			receivable["total_remaining"], result["projection"]["reconciliation"]["Receivable"]["overdue"]
		)

	def test_unknown_bucket_is_a_validation_error(self):
		with self.assertRaises(frappe.ValidationError):
			self.reply(kind="bucket", bucket="nope")

	def test_bucket_replies_sort_with_undated_last(self):
		rows = self.reply(kind="bucket", bucket="now", sort="due_date", order="desc")["rows"]
		dates = [r["due_date"] for r in rows]
		self.assertEqual(dates[-2:], [None, None])
		self.assertEqual(dates[:-2], sorted(dates[:-2], reverse=True))


class TestPagePath(DetailCase):
	"""The API and the goldens share one path: render(page(...))."""

	def page(self, kind="day", **kwargs):
		scope, result = golden.build_result(month="2026-11")
		args = {**BIG, "sort": None, "order": None, "side": "Payable", **kwargs}
		if kind == "day":
			return detail.day_page({"scope": scope}, result, date="2026-11-11", **args), scope, result, args
		return detail.bucket_page({"scope": scope}, result, bucket="window", **args), scope, result, args

	def test_a_reply_is_the_rendered_page_for_a_day_and_a_bucket(self):
		for kind, reply in (("day", detail.day_detail), ("bucket", detail.bucket_detail)):
			with self.subTest(kind=kind):
				page, scope, result, args = self.page(kind)
				key = {"date": "2026-11-11"} if kind == "day" else {"bucket": "window"}
				self.assertEqual(detail.render(page), reply({"scope": scope}, result, **key, **args))

	def test_a_page_holds_the_stored_rows_and_only_the_render_makes_them_public(self):
		page, _, result, _ = self.page()
		self.assertTrue(page.rows)
		self.assertTrue(all("_permission" in row for row in page.rows))
		stored = {row["id"]: row for row in result["sources"]["Payable"]["rows"]}
		self.assertTrue(all(stored[row["id"]] is row for row in page.rows))
		for row in detail.render(page)["rows"]:
			self.assertFalse([key for key in row if key.startswith("_")])

	def test_party_keys_are_the_pairs_of_the_party_slice(self):
		page, _, _, _ = self.page(page_size=1)
		reply = detail.render(page)
		self.assertEqual(page.party_keys, {(p["party_type"], p["party"]) for p in reply["parties"]})
		self.assertEqual(len(page.party_keys), 1)

	def test_without_parties_the_summary_is_empty_and_has_no_keys(self):
		for kind in ("day", "bucket"):
			with self.subTest(kind=kind):
				scope, result = golden.build_result(month="2026-11")
				args = {**BIG, "sort": None, "order": None, "side": "Payable", "parties": False}
				make = (
					(lambda: detail.day_page({"scope": scope}, result, date="2026-11-11", **args))
					if kind == "day"
					else (lambda: detail.bucket_page({"scope": scope}, result, bucket="window", **args))
				)
				with patch.object(detail, "_party_rows") as summary:
					page = make()
				summary.assert_not_called()
				self.assertEqual(page.party_keys, set())
				reply = detail.render(page)
				self.assertEqual((reply["parties"], reply["party_total"]), ([], None))
				self.assertTrue(reply["rows"])

	def test_the_side_check_names_a_forbidden_or_unavailable_side_without_any_row(self):
		status_part = {"unavailable": {"Receivable": "forbidden", "Payable": "too_large"}}
		with self.assertRaises(frappe.PermissionError):
			detail.check_side(status_part, "Receivable")
		with self.assertRaises(detail.CalendarSourceUnavailable):
			detail.check_side(status_part, "Payable")
		with self.assertRaises(frappe.ValidationError):
			detail.check_side(status_part, "Other")
		self.assertIsNone(detail.check_side({"unavailable": {}}, "Payable"))
