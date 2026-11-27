"""Payment Calendar S4b B6: `cache_shadow` (the serve function, the night oracle) and the L2 check of the residue rows.

L2 (plan section 5): for every scope window, `serve(stored rows of a generation, payload, window)` must equal the
reader's result for that window, row by row (`comparable`) and in its refusals (errata E5: the live reader wins).
The only exemption is the zero-money overlay rows of settled invoices that the open-set build never discovers (S6).
`TestResidueEquality` holds the three cases of one stored row for each row the reader shows. `TestResidueReading`
states what the reader does and which rows a generation stores for it.

These tests never commit: the fixtures are transactional and the view is replaced by a plain context.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_shadow
"""

import json
from datetime import date
from decimal import Decimal
from random import Random
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_months, get_first_day, get_last_day, getdate

from jarvis.payment_calendar import (
	banks,
	builtin_reader,
	cache_refresh,
	cache_shadow,
	cache_state,
	cache_store,
	read_log,
	snapshot,
)
from jarvis.payment_calendar import sources as today_sources
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.tests import test_payment_calendar_builtin_reader as reader_tests
from jarvis.tests.payment_calendar_fixtures import require_calendar_test_site

SIDES = ("Receivable", "Payable")
INVOICE = {"Receivable": "Sales Invoice", "Payable": "Purchase Invoice"}
DIGEST_KEY = "k" * 44


def stored_rows(build, side):
	"""The rows of a build in the form the store keeps them (ISO dates, Decimal text, JSON text)."""
	return [
		dict(zip(cache_store.ROW_FIELDS, encoded["values"], strict=True))
		for encoded in cache_store.encode_rows(side, build.rows)
	]


def reader_row(fields):
	"""A residue or open row as the reader shapes it, for the comparison with the reader's zero rows."""
	return cache_shadow._plain(cache_shadow._reader_row(cache_shadow.decode_row(fields)))


class WorkingCase(reader_tests.ReaderCase):
	"""Fixtures in a savepoint, the generation built through the real `_read_side`, the reader read directly."""

	def setUp(self):
		super().setUp()
		self.fx.fiscal_year()
		frappe.cache().hdel("fiscal_years", self.fx.company)
		for name in ("require_defaults_cache_hit", "check_source_contract"):
			patcher = patch.object(snapshot, name)
			patcher.start()
			self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.local.form_dict = frappe._dict()
		frappe.cache().hdel("fiscal_years", self.fx.company)
		super().tearDown()

	def build(self, side):
		self.sync()
		clock = cache_refresh.ViewClock(10_000, None, "t", False)
		with cache_refresh.refresh_caps(clock):
			return cache_refresh._read_side(self.fx.company, side, getdate(self.fx.today), set(), clock)

	def read(self, side, start, end):
		"""`(result, None)` or `(None, refusal code)` of the reader for a window."""
		try:
			return builtin_reader.read_obligations(self.fx.company, side, self.fx.today, start, end), None
		except CalendarSourceUnavailable as exc:
			return None, exc.code

	def serve(self, rows, build, start, end):
		try:
			return cache_shadow.serve(rows, build.payload, start, end), None
		except CalendarSourceUnavailable as exc:
			return None, exc.code

	def shown(self, result, build, side):
		known = cache_shadow.stored_vouchers(stored_rows(build, side))
		return cache_shadow.comparable(cache_shadow.exempt(result, known))

	def months(self):
		"""The legacy 24-month window and each month the scope accepts (`scope.normalize_scope`)."""
		first = get_first_day(add_months(self.fx.today, -12))
		windows = [("legacy", *cache_shadow.legacy_window(self.fx.today))]
		for index in range(24):
			start = get_first_day(add_months(first, index))
			windows.append((str(start)[:7], str(start), str(get_last_day(start))))
		return windows

	def day(self, offset):
		return str(add_days(self.fx.today, offset))

	def assert_serves_every_window(self, side, build=None):
		build = build or self.build(side)
		rows = stored_rows(build, side)
		seen = {"refused": 0, "served": 0}
		for label, start, end in self.months():
			with self.subTest(side=side, window=label):
				expected, expected_code = self.read(side, start, end)
				served, served_code = self.serve(rows, build, start, end)
				self.assertEqual(served_code, expected_code)
				if expected is None:
					seen["refused"] += 1
					continue
				seen["served"] += 1
				known = cache_shadow.stored_vouchers(rows)
				self.assertEqual(
					cache_shadow.comparable(served),
					cache_shadow.comparable(cache_shadow.exempt(expected, known)),
				)
		return seen

	def populate(self, side):
		"""Open and settled invoices of every kind the open set meets, on one party; returns name -> voucher."""
		names = {
			"plain": self.invoice(side, (100,)),
			"partly": self.invoice(side, (40, 60), paid=30),
			"credit": self.invoice(side, (50,), credit=20),
			"zero": self.invoice(side, (0.8, 1.2), paid=0.1),
		}
		self.pay("SecondPayment", names["zero"], -0.7, side)
		names["settled_in"] = self.settled(side, term=10, header=20)
		names["settled_out"] = self.settled(side, term=300, header=320)
		names["settled_no_terms"] = self.settled(side, schedule=False, header=15)
		names["settled_multi"] = self.invoice(side, (40, 60), paid=100)
		return names

	def second_party(self, side):
		"""A second party of the side; its invoices use the same ledger account."""
		doctype, name = ("Supplier", "Supplier2") if side == "Payable" else ("Customer", "Customer2")
		field = "supplier" if side == "Payable" else "customer"
		return self.fx.insert(
			doctype,
			name,
			**{f"{field}_name": self.fx.prefix + "2", f"{field}_type": "Company", "disabled": 0},
		)


class TestSideGenerations(WorkingCase):
	"""L2 on the open part, window by window, both sides."""

	def test_open_and_settled_invoices_of_every_kind_equal_the_reader_in_every_window(self):
		for side in SIDES:
			with self.subTest(side):
				self.populate(side)
				seen = self.assert_serves_every_window(side)
				self.assertEqual(seen["refused"], 0)
				self.assertGreater(seen["served"], 20)
				frappe.db.rollback(save_point="builtin_reader_case")
				frappe.db.savepoint("builtin_reader_case")
				self.fx = reader_tests.CalendarFixtures()
				self.fx.fiscal_year()
				frappe.cache().hdel("fiscal_years", self.fx.company)

	def test_a_plain_unpaid_invoice_is_served_with_the_reader_s_row(self):
		for side in SIDES:
			with self.subTest(side):
				invoice = self.invoice(side, (100,))
				build = self.build(side)
				start, end = self.day(0), self.day(40)
				served, _ = self.serve(stored_rows(build, side), build, start, end)
				expected, _ = self.read(side, start, end)
				self.assertEqual([row["voucher_no"] for row in served["rows"]], [invoice])
				self.assertEqual(self.shown(served, build, side), self.shown(expected, build, side))

	def test_a_partly_paid_invoice_serves_its_open_terms(self):
		for side in SIDES:
			with self.subTest(side):
				self.invoice(side, (40, 60), paid=30)
				build = self.build(side)
				served, _ = self.serve(stored_rows(build, side), build, self.day(0), self.day(40))
				self.assertEqual(sorted(Decimal(row["remaining"]) for row in served["rows"]), [10, 60])
				self.assertEqual(served["canonical_total"], "70.00")

	def test_multi_party_rows_keep_their_party(self):
		for side in SIDES:
			with self.subTest(side):
				other = self.second_party(side)
				mine = self.invoice(side, (100,))
				theirs = self.invoice(side, (70,), supplier=other)
				build = self.build(side)
				served, _ = self.serve(stored_rows(build, side), build, self.day(0), self.day(40))
				self.assertEqual({row["voucher_no"]: row["party"] for row in served["rows"]}[theirs], other)
				self.assert_serves_every_window(side, build)
				self.assertEqual({row["voucher_no"] for row in served["rows"]}, {mine, theirs})

	def test_the_excluded_fx_rows_and_the_notes_are_served(self):
		for side in SIDES:
			with self.subTest(side):
				invoice = self.foreign_invoice(side, gross=8000.15, paid=8000)
				build = self.build(side)
				served, _ = self.serve(stored_rows(build, side), build, self.day(0), self.day(0))
				expected, _ = self.read(side, self.day(0), self.day(0))
				self.assertEqual([row["voucher_no"] for row in served["excluded_rows"]], [invoice])
				self.assertEqual(served["notes"]["currency_residue_excluded"]["count"], 1)
				self.assertFalse(served["complete_for_company"])
				self.assertEqual(self.shown(served, build, side), self.shown(expected, build, side))
				self.assert_serves_every_window(side, build)
				frappe.db.rollback(save_point="builtin_reader_case")
				frappe.db.savepoint("builtin_reader_case")
				self.fx = reader_tests.CalendarFixtures()
				self.fx.fiscal_year()
				frappe.cache().hdel("fiscal_years", self.fx.company)

	def test_the_stored_form_and_the_build_form_serve_alike(self):
		self.invoice("Payable", (40, 60), paid=30)
		build = self.build("Payable")
		window = (self.day(0), self.day(40))
		self.assertEqual(
			cache_shadow.serve(build.rows, build.payload, *window),
			cache_shadow.serve(stored_rows(build, "Payable"), build.payload, *window),
		)


class TestResidueReading(WorkingCase):
	"""B4's reading of the residue rows, against what the reader does (the L2 verdict)."""

	side = "Payable"

	def zero_rows(self, result, voucher):
		return [
			row for row in result["rows"] if row["voucher_no"] == voucher and Decimal(row["remaining"]) == 0
		]

	def residue(self, build, voucher):
		return [
			row for row in stored_rows(build, self.side) if row["residue"] and row["voucher_no"] == voucher
		]

	def test_a_zero_money_term_row_equals_the_readers_overlay_row_and_shows_only_with_its_due_date(self):
		invoice = self.invoice(self.side, (0.8, 1.2), paid=0.1)
		self.pay("SecondPayment", invoice, -0.7, self.side)
		build = self.build(self.side)
		(stored,) = self.residue(build, invoice)
		self.assertEqual((stored["hide_in_window"], stored["window_refusal"]), (0, 0))
		with_due, _ = self.read(self.side, self.day(10), self.day(10))
		(seen,) = self.zero_rows(with_due, invoice)
		self.assertEqual(
			cache_shadow._plain({key: value for key, value in seen.items() if key != "id"}),
			reader_row(stored),
		)
		without_due, _ = self.read(self.side, self.day(-5), self.day(5))
		self.assertEqual(self.zero_rows(without_due, invoice), [])

	def test_the_flags_zero_do_not_mean_shown_in_every_window(self):
		invoice = self.invoice(self.side, (100,), paid=100)
		self.pinned_headers[("Purchase Invoice", invoice)] = 50
		build = self.build(self.side)
		(stored,) = self.residue(build, invoice)
		self.assertEqual((stored["hide_in_window"], stored["window_refusal"]), (0, 0))
		inside, _ = self.read(self.side, self.day(10), self.day(10))
		outside, _ = self.read(self.side, self.day(-5), self.day(5))
		self.assertEqual(len(self.zero_rows(inside, invoice)), 1)
		self.assertEqual(self.zero_rows(outside, invoice), [])

	def reader_zero_rows(self, voucher, start, end):
		"""The reader's zero-money rows of an invoice for a window, as the stored form compares them."""
		result, _ = self.read(self.side, start, end)
		return [
			cache_shadow._plain({key: value for key, value in row.items() if key != "id"})
			for row in self.zero_rows(result, voucher)
		]

	def test_a_settled_invoice_with_no_schedule_is_one_stored_row_served_in_the_window_of_its_due_date(self):
		"""The row is never hidden (`hide_in_window` is 0). It is served only in a window with its due date."""
		invoice = self.invoice(self.side, (100,), paid=100, schedules=False)
		self.pinned_headers[("Purchase Invoice", invoice)] = 50
		build = self.build(self.side)
		(stored,) = self.residue(build, invoice)
		due = stored["due_date"]
		self.assertEqual(due, str(frappe.db.get_value("Purchase Invoice", invoice, "due_date")))
		self.assertEqual((stored["hide_in_window"], stored["window_refusal"]), (0, 0))
		self.assertEqual(json.loads(stored["window_due_dates"]), [due])
		self.assertEqual(self.reader_zero_rows(invoice, due, due), [reader_row(stored)])
		self.assertEqual(self.reader_zero_rows(invoice, self.day(-5), self.day(5)), [])
		served, _ = self.serve(stored_rows(build, self.side), build, due, due)
		self.assertEqual(self.zero_rows(served, invoice), [cache_shadow._plain(reader_row(stored))])
		self.assert_serves_every_window(self.side, build)

	def test_a_multi_term_settled_candidate_is_one_stored_row_for_each_term_equal_to_the_readers_rows(self):
		invoice = self.invoice(self.side, (40, 60), paid=100)
		self.sync()
		self.pinned_headers[("Purchase Invoice", invoice)] = 50
		build = self.build(self.side)
		stored = sorted(self.residue(build, invoice), key=lambda row: row["due_date"])
		self.assertEqual(
			[(row["original"], row["paid"], row["remaining"], row["due_date"]) for row in stored],
			[("40.00", "40.00", "0.00", self.day(10)), ("60.00", "60.00", "0.00", self.day(20))],
		)
		self.assertEqual(
			[(row["hide_in_window"], row["window_refusal"]) for row in stored],
			[(0, 0), (0, 0)],
		)
		selected = {
			self.day(10),
			self.day(20),
			str(frappe.db.get_value("Purchase Invoice", invoice, "due_date")),
		}
		for row in stored:
			self.assertEqual(json.loads(row["window_due_dates"]), sorted(selected))
		shown = self.reader_zero_rows(invoice, *cache_shadow.legacy_window(self.fx.today))
		self.assertEqual(sorted(shown, key=lambda row: row["due_date"]), [reader_row(row) for row in stored])
		self.assertEqual(self.reader_zero_rows(invoice, self.day(10), self.day(10)), [reader_row(stored[0])])
		self.assertEqual(self.reader_zero_rows(invoice, self.day(20), self.day(20)), [reader_row(stored[1])])

	def test_the_paid_term_of_an_open_invoice_is_stored_and_served_like_the_readers_overlay_row(self):
		invoice = self.invoice(self.side, (40, 60), paid=40)
		build = self.build(self.side)
		(stored,) = self.residue(build, invoice)
		self.assertEqual(
			(stored["original"], stored["paid"], stored["remaining"], stored["due_date"]),
			("40.00", "40.00", "0.00", self.day(10)),
		)
		self.assertEqual((stored["hide_in_window"], stored["window_refusal"]), (0, 0))
		self.assertEqual(json.loads(stored["window_due_dates"]), [self.day(10)])
		self.assertEqual(self.reader_zero_rows(invoice, self.day(10), self.day(10)), [reader_row(stored)])
		served, _ = self.serve(stored_rows(build, self.side), build, self.day(10), self.day(10))
		self.assertEqual(self.zero_rows(served, invoice), [cache_shadow._plain(reader_row(stored))])
		open_rows = [row for row in stored_rows(build, self.side) if not row["residue"]]
		self.assertEqual([row["remaining"] for row in open_rows], ["60.00"])

	def test_a_settled_invoice_that_the_open_set_never_reads_is_not_in_the_generation(self):
		invoice = self.settled(self.side, term=10, header=20)
		build = self.build(self.side)
		self.assertEqual([row for row in stored_rows(build, self.side) if row["voucher_no"] == invoice], [])
		inside, _ = self.read(self.side, self.day(10), self.day(10))
		self.assertEqual(len(self.zero_rows(inside, invoice)), 1)

	def test_a_window_refusal_known_to_the_generation_is_the_readers_refusal_in_every_window(self):
		invoice = self.invoice(self.side, (80, 70), gross_total=100, paid=100)
		self.sync()
		self.pinned_headers[("Purchase Invoice", invoice)] = 50
		build = self.build(self.side)
		(stored,) = self.residue(build, invoice)
		self.assertEqual(stored["window_refusal"], 1)
		seen = self.assert_serves_every_window(self.side, build)
		self.assertGreater(seen["refused"], 0)
		self.assertGreater(seen["served"], 0)

	def test_a_window_refusal_for_an_invoice_the_generation_never_reads_is_not_predicted(self):
		self.invoice(self.side, (80, 70), gross_total=100, paid=100)
		build = self.build(self.side)
		legacy = cache_shadow.legacy_window(self.fx.today)
		expected, expected_code = self.read(self.side, *legacy)
		served, served_code = self.serve(stored_rows(build, self.side), build, *legacy)
		self.assertEqual(expected_code, "settlement_schedule_mismatch")
		self.assertIsNone(served_code)
		self.assertIsNotNone(served)


class TestResidueEquality(WorkingCase):
	"""Errata E5: the serve from a generation equals the live reader for every row the reader shows."""

	side = "Payable"

	def test_a_paid_term_of_a_part_paid_invoice_is_served_as_a_settled_row(self):
		"""(a) The reader shows the paid term (outstanding 0) in a window with its due date. The generation stores
		one residue row for it, with the term's own due date, amounts and permission."""
		self.invoice(self.side, (40, 60), paid=40)
		self.assert_serves_every_window(self.side)

	def test_a_settled_invoice_with_a_schedule_is_served_as_one_row_for_each_term(self):
		"""(b) A header-open, ledger-settled invoice: the reader shows one row for each term. The generation stores
		one residue row for each term, with the term's due date."""
		invoice = self.invoice(self.side, (40, 60), paid=100)
		self.sync()
		self.pinned_headers[("Purchase Invoice", invoice)] = 50
		self.assert_serves_every_window(self.side)

	def test_a_settled_invoice_with_no_schedule_is_served_in_the_window_of_its_due_date(self):
		"""(c) The reader shows the single row of the invoice when its header due date is in the window. The
		generation stores that row with the same due date and permission, and never hides it."""
		invoice = self.invoice(self.side, (100,), paid=100, schedules=False)
		self.sync()
		self.pinned_headers[("Purchase Invoice", invoice)] = 50
		self.assert_serves_every_window(self.side)

	def test_a_zero_money_term_row_already_equals_the_reader_in_every_window(self):
		invoice = self.invoice(self.side, (0.8, 1.2), paid=0.1)
		self.pay("SecondPayment", invoice, -0.7, self.side)
		self.assert_serves_every_window(self.side)


class TestServePure(TestCase):
	"""`serve`, `serve_bank`, `comparable`, `compare_open` on hand-made rows: no database."""

	PAYLOAD = {
		"notes": {},
		"canonical_total": "70.00",
		"currency": "INR",
		"precision": 2,
		"complete_for_company": True,
		"scope_reasons": [],
		"permission_columns": [],
	}

	def row(self, **fields):
		base = {
			"kind": "obligation",
			"voucher_type": "Purchase Invoice",
			"voucher_no": "PINV-1",
			"party_type": "Supplier",
			"party": "SUP-1",
			"party_name": "Supplier One",
			"account": "Creditors",
			"posting_date": "2026-11-01",
			"due_date": "2026-12-01",
			"term": "",
			"original": "100.00",
			"paid": "30.00",
			"credit": "0.00",
			"discount": "0.00",
			"remaining": "70.00",
			"settlement": "Partially paid",
			"permission": {"party": "SUP-1"},
		}
		base.update(fields)
		return base

	def residue(self, **fields):
		return self.row(
			remaining="0.00",
			settlement="Settled",
			residue=1,
			window_due_dates=json.dumps(["2026-12-01", "2026-12-20"]),
			**fields,
		)

	def test_a_stored_row_becomes_the_readers_row(self):
		served = cache_shadow.serve([self.row()], self.PAYLOAD, "2026-12-01", "2026-12-31")
		self.assertEqual(
			served["rows"],
			[
				{
					"voucher_type": "Purchase Invoice",
					"voucher_no": "PINV-1",
					"party_type": "Supplier",
					"party": "SUP-1",
					"party_name": "Supplier One",
					"account": "Creditors",
					"posting_date": "2026-11-01",
					"due_date": "2026-12-01",
					"term": "",
					"original": "100.00",
					"paid": "30.00",
					"credit": "0.00",
					"discount": "0.00",
					"remaining": "70.00",
					"settlement": "Partially paid",
					"kind": "obligation",
					"_permission": {"party": "SUP-1"},
				}
			],
		)
		self.assertEqual(served["canonical_total"], "70.00")

	def test_an_allocation_basis_row_keeps_its_two_keys_and_a_row_without_one_has_neither(self):
		based = self.row(allocation_basis="invoice", allocation_reason="foreign_rounding", paid=None)
		(row,) = cache_shadow.serve([based], self.PAYLOAD, "2026-12-01", "2026-12-31")["rows"]
		self.assertEqual((row["allocation_basis"], row["allocation_reason"]), ("invoice", "foreign_rounding"))
		self.assertIsNone(row["paid"])
		(plain,) = cache_shadow.serve([self.row()], self.PAYLOAD, "2026-12-01", "2026-12-31")["rows"]
		self.assertNotIn("allocation_basis", plain)

	def test_the_rows_come_in_the_readers_order(self):
		later = self.row(voucher_no="B", due_date="2026-12-09")
		earlier = self.row(voucher_no="A", due_date="2026-12-09")
		first = self.row(voucher_no="Z", due_date="2026-12-01")
		served = cache_shadow.serve([later, earlier, first], self.PAYLOAD, "2026-12-01", "2026-12-31")
		self.assertEqual([row["voucher_no"] for row in served["rows"]], ["Z", "A", "B"])

	def test_a_residue_row_is_served_with_its_own_due_date_and_no_other(self):
		residue = self.residue(voucher_no="PINV-2", due_date="2026-12-20", original="60.00")

		def served(start, end):
			rows = cache_shadow.serve([self.row(), residue], self.PAYLOAD, start, end)["rows"]
			return [row["voucher_no"] for row in rows]

		self.assertEqual(served("2026-12-15", "2026-12-31"), ["PINV-1", "PINV-2"])
		self.assertEqual(served("2026-11-01", "2026-12-10"), ["PINV-1"])
		self.assertEqual(served("2027-01-01", "2027-01-31"), ["PINV-1"])

	def test_a_residue_row_is_served_when_the_window_ends_or_starts_on_its_due_date(self):
		residue = self.residue(due_date="2026-12-20")
		for start, end in (
			("2026-12-20", "2026-12-20"),
			("2026-12-01", "2026-12-20"),
			("2026-12-20", "2027-01-31"),
		):
			self.assertEqual(len(cache_shadow.serve([residue], self.PAYLOAD, start, end)["rows"]), 1)

	def test_a_residue_row_with_no_due_date_is_never_served(self):
		residue = self.residue(due_date=None)
		self.assertEqual(cache_shadow.serve([residue], self.PAYLOAD, "1900-01-01", "2999-12-31")["rows"], [])

	def test_hide_in_window_does_not_hide_a_residue_row(self):
		residue = self.residue(due_date="2026-12-20", hide_in_window=1)
		self.assertEqual(
			len(cache_shadow.serve([residue], self.PAYLOAD, "2026-12-01", "2026-12-31")["rows"]), 1
		)

	def test_excluded_fx_rows_are_served_apart(self):
		excluded = {
			"kind": "settled_fx",
			"voucher_type": "Purchase Invoice",
			"voucher_no": "PINV-2",
			"party_type": "Supplier",
			"party": "SUP-1",
			"remaining": "0.15",
			"account_remaining": "0.00",
			"permission": {"party": "SUP-1"},
		}
		served = cache_shadow.serve([self.row(), excluded], self.PAYLOAD, "2026-12-01", "2026-12-31")
		self.assertEqual([row["voucher_no"] for row in served["rows"]], ["PINV-1"])
		self.assertEqual(served["excluded_rows"][0]["account_remaining"], "0.00")

	def test_a_refusal_flag_refuses_a_window_with_one_of_its_due_dates_only(self):
		flagged = self.residue(window_refusal=1)
		for start, end, refused in (
			("2026-12-01", "2026-12-01", True),
			("2026-12-20", "2026-12-20", True),
			("2026-11-01", "2026-11-30", False),
			("2026-12-02", "2026-12-19", False),
			("2026-12-21", "2027-01-31", False),
		):
			with self.subTest(start=start, end=end):
				if refused:
					with self.assertRaises(CalendarSourceUnavailable) as raised:
						cache_shadow.serve([self.row(), flagged], self.PAYLOAD, start, end)
					self.assertEqual(raised.exception.code, "settlement_schedule_mismatch")
				else:
					cache_shadow.serve([self.row(), flagged], self.PAYLOAD, start, end)

	def test_a_residue_row_without_the_flag_never_refuses(self):
		cache_shadow.serve([self.residue()], self.PAYLOAD, "2026-12-01", "2026-12-31")

	def test_a_row_with_an_unknown_field_is_refused(self):
		with self.assertRaises(ValueError):
			cache_shadow.decode_row({**self.row(), "surprise": 1})

	def test_a_float_amount_is_refused(self):
		with self.assertRaises(TypeError):
			cache_shadow.decode_row(self.row(remaining=70.0))

	def test_exempt_drops_only_the_zero_rows_of_unknown_vouchers_and_counts_the_note_again(self):
		based = self.row(allocation_basis="invoice", allocation_reason="x", remaining="5.00")
		kept_zero = self.residue(voucher_no="PINV-9")
		unknown_zero = self.residue(voucher_no="PINV-8", allocation_basis="invoice", allocation_reason="y")
		result = {
			**self.PAYLOAD,
			"rows": [
				{"id": "a", **cache_shadow._reader_row(cache_shadow.decode_row(based))},
				{"id": "b", **cache_shadow._reader_row(cache_shadow.decode_row(kept_zero))},
				{"id": "c", **cache_shadow._reader_row(cache_shadow.decode_row(unknown_zero))},
			],
			"excluded_rows": [],
			"notes": {"invoice_level": {"count": 2}},
		}
		part = cache_shadow.exempt(result, frozenset({"PINV-1", "PINV-9"}))
		self.assertEqual([row["voucher_no"] for row in part["rows"]], ["PINV-1", "PINV-9"])
		self.assertEqual(part["notes"], {"invoice_level": {"count": 1}})

	def test_comparable_drops_ids_sorts_and_keeps_the_notes_as_they_are(self):
		result = {
			**self.PAYLOAD,
			"rows": [
				{"id": "a", "party": "S", "remaining": "2.00", "voucher_no": "B"},
				{"id": "b", "party": "S", "remaining": "1.00", "voucher_no": "A"},
			],
			"excluded_rows": [],
			"notes": {"invoice_level": {"count": 7}},
		}
		part = cache_shadow.comparable(result)
		self.assertEqual([row["voucher_no"] for row in part["rows"]], ["A", "B"])
		self.assertNotIn("id", part["rows"][0])
		self.assertEqual(part["notes"], {"invoice_level": {"count": 7}})

	def test_comparable_reads_dates_and_text_alike(self):
		with_date = {"party": "S", "remaining": "1.00", "_permission": {"posting_date": date(2026, 1, 2)}}
		with_text = {"party": "S", "remaining": "1.00", "_permission": {"posting_date": "2026-01-02"}}
		result = {**self.PAYLOAD, "excluded_rows": [], "notes": {}}
		self.assertEqual(
			cache_shadow.comparable({**result, "rows": [with_date]}),
			cache_shadow.comparable({**result, "rows": [with_text]}),
		)

	def test_a_sample_comparable_keeps_the_sample_parties_and_sums_the_rows_kept(self):
		rows = [
			{"party": "A", "remaining": "10.00", "x": 1},
			{"party": "B", "remaining": "5.00", "x": 2},
			{"party": "A", "remaining": "0.00", "x": 3},
		]
		part = cache_shadow.comparable(
			{"rows": rows, "excluded_rows": [{"party": "B", "remaining": "1"}]}, frozenset({"A"})
		)
		self.assertEqual(sorted(row["x"] for row in part["rows"]), [1, 3])
		self.assertEqual((part["total"], part["excluded_rows"]), ("10.00", []))

	def test_the_invoice_level_note_is_counted_over_the_rows_served_residue_rows_included(self):
		based = self.row(allocation_basis="invoice", allocation_reason="foreign_rounding", paid=None)
		residue = self.residue(
			voucher_no="PINV-2",
			due_date="2026-12-20",
			allocation_basis="invoice",
			allocation_reason="foreign_rounding",
		)
		plain = self.row(voucher_no="PINV-3")
		payload = {**self.PAYLOAD, "notes": {"invoice_level": {"count": 1}, "other": 5}}
		inside = cache_shadow.serve([based, residue, plain], payload, "2026-12-15", "2026-12-31")
		self.assertEqual(inside["notes"], {"invoice_level": {"count": 2}, "other": 5})
		outside = cache_shadow.serve([based, residue, plain], payload, "2026-11-01", "2026-12-10")
		self.assertEqual(outside["notes"], {"invoice_level": {"count": 1}, "other": 5})
		none = cache_shadow.serve([plain], payload, "2026-12-15", "2026-12-31")
		self.assertEqual(none["notes"], {"other": 5})

	def test_the_excluded_rows_keep_the_stored_order(self):
		def excluded(number):
			return {
				"kind": "settled_fx",
				"voucher_type": "Purchase Invoice",
				"voucher_no": number,
				"party_type": "Supplier",
				"party": "SUP-1",
				"remaining": "0.15",
				"account_remaining": "0.00",
				"permission": {"party": "SUP-1"},
			}

		rows = [excluded("PINV-9"), excluded("PINV-1"), excluded("PINV-5")]
		served = cache_shadow.serve(rows, self.PAYLOAD, "2026-12-01", "2026-12-31")
		self.assertEqual(
			[row["voucher_no"] for row in served["excluded_rows"]], ["PINV-9", "PINV-1", "PINV-5"]
		)

	def test_a_refusal_of_the_open_window_serve_is_not_compared_by_class(self):
		flagged = cache_store.row_tuple(
			{
				"kind": "obligation",
				"voucher_type": "Purchase Invoice",
				"voucher_no": "PINV-1",
				"residue": 1,
				"remaining": Decimal(0),
				"window_refusal": 1,
				"window_due_dates": ["1900-01-01"],
			}
		)
		today = {**self.PAYLOAD, "rows": [], "excluded_rows": []}
		with patch.object(cache_shadow, "_today_lifted", return_value=today):
			record = cache_shadow._window_refusal(
				"C", "Payable", date(2026, 1, 1), [flagged], self.PAYLOAD, None, None
			)
		self.assertEqual(record["outcome"], "not_compared")
		self.assertEqual(record["reason"], "served_refused:settlement_schedule_mismatch")

	def test_a_difference_is_counted_and_digested_without_a_name(self):
		left = {"rows": [{"voucher_no": "A"}, {"voucher_no": "B"}], "excluded_rows": [], "x": 1}
		right = {"rows": [{"voucher_no": "A"}, {"voucher_no": "C"}], "excluded_rows": [], "x": 2}
		record = cache_shadow.compare_open(left, right, digest=lambda text: "h" + str(len(text)))
		self.assertEqual(record["outcome"], "diff")
		self.assertEqual(
			(record["counts"]["only_today"], record["counts"]["only_cache"], record["counts"]["fields"]),
			(1, 1, ["x"]),
		)
		self.assertEqual(len(record["digests"]), 2)
		self.assertNotIn("voucher_no", json.dumps(record["digests"]))
		self.assertEqual(cache_shadow.compare_open(left, left)["outcome"], "compared")

	def test_digests_are_capped(self):
		left = {"rows": [{"n": index} for index in range(50)], "excluded_rows": []}
		right = {"rows": [], "excluded_rows": []}
		record = cache_shadow.compare_open(left, right, digest=str)
		self.assertEqual(len(record["digests"]), cache_shadow.MAX_DIGESTS)

	def test_the_legacy_window_runs_twelve_months_back_to_thirty_days_after_the_eleventh_month_ahead(self):
		self.assertEqual(cache_shadow.legacy_window("2026-11-24"), ("2025-11-01", "2027-11-30"))
		self.assertEqual(cache_shadow.legacy_window(date(2024, 2, 29)), ("2023-02-01", "2025-03-02"))


class TestLegacyWindow(TestCase):
	def test_it_is_the_window_of_a_scope_without_a_month(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("shadow_window")
		try:
			fx = reader_tests.CalendarFixtures()
			scope = normalize_scope(fx.company, [], check_accounts=False)
			self.assertEqual(
				cache_shadow.legacy_window(scope["anchor"]), (scope["window_start"], scope["window_end"])
			)
		finally:
			frappe.db.rollback(save_point="shadow_window")


class TestBankServe(WorkingCase):
	def bank(self):
		self.sync()
		return cache_refresh._read_bank(self.fx.company, getdate(self.fx.today), set())

	def today(self, accounts):
		return banks.read_bank_balances(self.fx.company, accounts, self.fx.today)

	def test_the_served_balances_equal_the_bank_read(self):
		second = self.fx.account("Bank2", "Bank", "Asset")
		self.fx.gl(500)
		self.fx.gl(-125, account=second)
		build = self.bank()
		for accounts in ([self.fx.bank], [second], sorted([self.fx.bank, second])):
			with self.subTest(accounts=len(accounts)):
				served = cache_shadow.serve_bank(build.rows, build.payload, accounts)
				self.assertEqual(served, self.today(accounts))

	def test_an_account_in_a_foreign_book_refuses_like_today(self):
		other = self.fx.account("Bank3", "Bank", "Asset")
		self.fx.gl(100)
		self.fx.gl(10, account=other, finance_book="Other Book")
		build = self.bank()
		cache_shadow.serve_bank(build.rows, build.payload, [self.fx.bank])
		with self.assertRaises(CalendarSourceUnavailable) as served:
			cache_shadow.serve_bank(build.rows, build.payload, [other])
		with self.assertRaises(CalendarSourceUnavailable) as today:
			self.today([other])
		self.assertEqual((served.exception.code, today.exception.code), ("multibook_unsupported",) * 2)

	def test_the_company_ple_book_flag_refuses_every_account(self):
		self.fx.gl(100)
		self.fx.ple("BookedPLE", "Purchase Invoice", "X", 10, finance_book="Other Book")
		build = self.bank()
		with self.assertRaises(CalendarSourceUnavailable) as served:
			cache_shadow.serve_bank(build.rows, build.payload, [self.fx.bank])
		with self.assertRaises(CalendarSourceUnavailable) as today:
			self.today([self.fx.bank])
		self.assertEqual((served.exception.code, today.exception.code), ("multibook_unsupported",) * 2)

	def test_an_account_the_generation_does_not_hold_is_refused(self):
		self.fx.gl(100)
		build = self.bank()
		with self.assertRaises(CalendarSourceUnavailable):
			cache_shadow.serve_bank(build.rows, build.payload, [self.fx.bank, "No such account"])

	def test_a_zero_precision_total_follows_the_payload_precision(self):
		self.fx.gl(10)
		build = self.bank()
		payload = {**build.payload, "precision": 0}
		self.assertEqual(cache_shadow.serve_bank(build.rows, payload, [self.fx.bank])["total"], "10")


class OracleCase(WorkingCase):
	def setUp(self):
		super().setUp()
		for patcher in (
			patch.object(cache_state, "cache_settings", return_value=SimpleNamespace(active=True)),
			patch.dict(frappe.conf, {"encryption_key": DIGEST_KEY}),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def oracle(self, side, build=None, rows=None):
		build = build or self.build(side)
		return cache_shadow.night_oracle(
			self.fx.company,
			side,
			getdate(self.fx.today),
			build.rows if rows is None else rows,
			build.payload,
		)


class TestNightOracle(OracleCase):
	def test_equal_generations_are_compared_on_both_sides(self):
		for side in SIDES:
			with self.subTest(side):
				self.invoice(side, (100,))
				self.invoice(side, (40, 60), paid=30)
				self.settled(side)
				record = self.oracle(side)
				self.assertEqual((record["outcome"], record["mode"]), ("compared", "full"))
				self.assertEqual(record["counts"]["rows"], 3)

	def test_a_missing_row_is_a_diff_with_a_digest_and_no_name(self):
		invoice = self.invoice("Payable", (100,))
		self.invoice("Payable", (40, 60), paid=30)
		build = self.build("Payable")
		rows = [row for row in build.rows if row[cache_store.ROW_FIELDS.index("voucher_no")] != invoice]
		record = self.oracle("Payable", build, rows)
		self.assertEqual(record["outcome"], "diff")
		self.assertEqual(record["counts"]["only_today"], 1)
		self.assertEqual(len(record["digests"]), 1)
		self.assertNotIn(invoice, json.dumps(record))
		self.assertNotIn(self.fx.company, json.dumps(record))

	def test_a_diff_without_an_encryption_key_has_counts_and_no_digests_and_no_name(self):
		invoice = self.invoice("Payable", (100,))
		self.invoice("Payable", (40, 60), paid=30)
		build = self.build("Payable")
		rows = [row for row in build.rows if row[cache_store.ROW_FIELDS.index("voucher_no")] != invoice]
		with patch.dict(frappe.conf, {"encryption_key": None}):
			record = self.oracle("Payable", build, rows)
		self.assertEqual(record["outcome"], "diff")
		self.assertEqual(record["counts"]["only_today"], 1)
		self.assertEqual(record["digests"], [])
		self.assertNotIn(invoice, json.dumps(record))
		self.assertNotIn(self.fx.company, json.dumps(record))

	def test_a_changed_amount_is_a_diff(self):
		self.invoice("Receivable", (100,))
		build = self.build("Receivable")
		at = cache_store.ROW_FIELDS.index("remaining")
		changed = [(*row[:at], Decimal("99.00"), *row[at + 1 :]) for row in build.rows]
		self.assertEqual(self.oracle("Receivable", build, changed)["outcome"], "diff")

	def test_an_extra_row_is_a_diff(self):
		self.invoice("Payable", (100,))
		build = self.build("Payable")
		record = self.oracle("Payable", build, [*build.rows, build.rows[0]])
		self.assertEqual((record["outcome"], record["counts"]["only_cache"]), ("diff", 1))

	def test_a_refusal_the_generation_knows_is_an_open_equal_window_refusal(self):
		invoice = self.invoice("Payable", (80, 70), gross_total=100, paid=100)
		self.invoice("Payable", (50,))
		self.sync()
		self.pinned_headers[("Purchase Invoice", invoice)] = 50
		record = self.oracle("Payable")
		self.assertEqual(record["outcome"], "open_equal_window_refusal")
		self.assertEqual(record["counts"]["refusal_unseen"], 0)

	def test_a_refusal_the_generation_cannot_know_is_recorded_as_unseen(self):
		self.invoice("Payable", (80, 70), gross_total=100, paid=100)
		self.invoice("Payable", (50,))
		record = self.oracle("Payable")
		self.assertEqual(record["outcome"], "open_equal_window_refusal")
		self.assertEqual(record["counts"]["refusal_unseen"], 1)

	def test_a_refusal_with_a_different_open_set_is_a_diff(self):
		self.invoice("Payable", (80, 70), gross_total=100, paid=100)
		self.invoice("Payable", (50,))
		record = self.oracle("Payable", rows=[])
		self.assertEqual(record["outcome"], "diff")

	def test_a_refusal_other_than_the_window_one_is_a_diff_of_refusals(self):
		self.invoice("Payable", (50,))
		build = self.build("Payable")
		with patch.object(
			today_sources, "read_obligations", side_effect=CalendarSourceUnavailable("too_large")
		):
			record = self.oracle("Payable", build)
		self.assertEqual(record["outcome"], "diff")
		self.assertEqual(record["counts"]["refusals"], ["too_large", None])

	def test_a_flag_the_reader_does_not_confirm_is_a_diff(self):
		self.invoice("Payable", (50,))
		build = self.build("Payable")
		flagged = cache_store.row_tuple(
			{
				"kind": "obligation",
				"voucher_type": "Purchase Invoice",
				"voucher_no": "X",
				"residue": 1,
				"remaining": Decimal(0),
				"window_refusal": 1,
				"window_due_dates": [self.day(10)],
			}
		)
		record = self.oracle("Payable", build, [*build.rows, flagged])
		self.assertEqual(record["outcome"], "diff")
		self.assertEqual(record["counts"]["refusals"], [None, "settlement_schedule_mismatch"])

	def test_the_record_carries_the_build_and_the_time_and_no_name(self):
		self.invoice("Payable", (50,))
		record = self.oracle("Payable")
		self.assertEqual(len(record["cache_build"]), 12)
		self.assertIsInstance(record["oracle_ms"], int)
		text = json.dumps(record)
		self.assertNotIn(self.fx.company, text)
		self.assertNotIn(self.fx.supplier, text)

	def test_it_is_not_applicable_when_the_cache_is_off_or_the_side_is_unknown(self):
		self.invoice("Payable", (50,))
		build = self.build("Payable")
		with patch.object(cache_state, "cache_settings", return_value=SimpleNamespace(active=False)):
			self.assertIsNone(self.oracle("Payable", build))
		self.assertIsNone(
			cache_shadow.night_oracle(self.fx.company, "Sideways", getdate(self.fx.today), [], {})
		)

	def test_an_unexpected_error_is_a_not_compared_record_by_class(self):
		self.invoice("Payable", (50,))
		build = self.build("Payable")
		with patch.object(today_sources, "read_obligations", side_effect=KeyError("Acme Ltd")):
			record = self.oracle("Payable", build)
		self.assertEqual(record["outcome"], "not_compared")
		self.assertEqual(record["reason"], "error:KeyError")
		self.assertNotIn("Acme", json.dumps(record))

	def test_a_job_deadline_is_not_swallowed(self):
		from jarvis.payment_calendar.errors import TIMEOUTS

		self.invoice("Payable", (50,))
		build = self.build("Payable")
		with patch.object(today_sources, "read_obligations", side_effect=TIMEOUTS[0]()):
			with self.assertRaises(TIMEOUTS):
				self.oracle("Payable", build)

	def test_the_oracle_writes_nothing(self):
		self.invoice("Payable", (50,))
		build = self.build("Payable")
		with patch.object(frappe.db, "commit") as commit, patch.object(frappe.db, "rollback") as rollback:
			self.oracle("Payable", build)
		commit.assert_not_called()
		rollback.assert_not_called()


class TestSampleOracle(OracleCase):
	def setUp(self):
		super().setUp()
		patcher = patch.object(cache_shadow, "ORACLE_FULL_LEDGER_ROWS", 0)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_a_side_above_the_row_limit_is_compared_on_a_sample(self):
		for side in SIDES:
			with self.subTest(side):
				self.invoice(side, (100,))
				self.invoice(side, (70,), supplier=self.second_party(side), paid=20)
				record = self.oracle(side)
				self.assertEqual((record["outcome"], record["mode"]), ("compared_sample", "sample"))
				self.assertEqual((record["counts"]["parties"], record["counts"]["parties_total"]), (2, 2))
				frappe.db.rollback(save_point="builtin_reader_case")
				frappe.db.savepoint("builtin_reader_case")
				self.fx = reader_tests.CalendarFixtures()
				self.fx.fiscal_year()
				frappe.cache().hdel("fiscal_years", self.fx.company)

	def test_a_whole_party_missing_from_the_generation_is_a_diff_when_the_ledger_sample_has_it(self):
		other = self.second_party("Payable")
		self.invoice("Payable", (100,))
		gone = self.invoice("Payable", (70,), supplier=other)
		build = self.build("Payable")
		at = cache_store.ROW_FIELDS.index("party")
		rows = [row for row in build.rows if row[at] != other]
		record = self.oracle("Payable", build, rows)
		self.assertEqual((record["outcome"], record["mode"]), ("diff", "sample"))
		self.assertEqual(record["counts"]["only_today"], 1)
		self.assertNotIn(gone, json.dumps(record))

	def test_the_sample_read_of_every_party_equals_todays_full_read(self):
		other = self.second_party("Payable")
		self.invoice("Payable", (100,))
		self.invoice("Payable", (40, 60), supplier=other, paid=30)
		self.sync()
		anchor, (start, end) = getdate(self.fx.today), cache_shadow.legacy_window(self.fx.today)
		parties = cache_shadow.sample_parties(self.fx.company, "Payable", anchor)
		self.assertEqual(sorted(parties), sorted([self.fx.supplier, other]))
		sample = cache_shadow._today_sample(self.fx.company, "Payable", anchor, start, end, parties)
		full = cache_shadow._today_lifted(self.fx.company, "Payable", anchor, start, end)
		self.assertEqual(
			cache_shadow.comparable(sample, frozenset(parties)),
			cache_shadow.comparable(cache_shadow.exempt(full, frozenset()), frozenset(parties)),
		)

	def test_a_sample_of_one_party_reads_only_that_party(self):
		other = self.second_party("Payable")
		mine = self.invoice("Payable", (100,))
		self.invoice("Payable", (70,), supplier=other)
		self.sync()
		anchor, (start, end) = getdate(self.fx.today), cache_shadow.legacy_window(self.fx.today)
		sample = cache_shadow._today_sample(
			self.fx.company, "Payable", anchor, start, end, [self.fx.supplier]
		)
		self.assertEqual([row["voucher_no"] for row in sample["rows"]], [mine])

	def test_the_largest_the_shared_and_the_seeded_random_parties_are_chosen(self):
		anchor = getdate(self.fx.today)
		parties = [self.second_party("Payable")]
		self.invoice("Payable", (10,))
		for index in range(3):
			self.invoice("Payable", (10,), supplier=parties[0], paid=index)
		with (
			patch.object(cache_shadow, "SAMPLE_LARGEST", 1),
			patch.object(cache_shadow, "SAMPLE_RANDOM", 0),
		):
			self.assertEqual(cache_shadow.sample_parties(self.fx.company, "Payable", anchor), parties)

	def test_the_random_choice_is_seeded_with_the_anchor_date_and_bounded(self):
		anchor = getdate(self.fx.today)
		parties = [self.fx.supplier]
		self.invoice("Payable", (10,))
		for index in range(8):
			party = self.fx.insert(
				"Supplier",
				f"Extra{index}",
				supplier_name=f"{self.fx.prefix}{index}",
				supplier_type="Company",
				disabled=0,
			)
			parties.append(party)
			self.invoice("Payable", (10,), supplier=party)
		with (
			patch.object(cache_shadow, "SAMPLE_LARGEST", 0),
			patch.object(cache_shadow, "SAMPLE_RANDOM", 3),
		):
			chosen = cache_shadow.sample_parties(self.fx.company, "Payable", anchor)
		expected = sorted(Random(anchor.isoformat()).sample(sorted(parties), 3))
		self.assertEqual(chosen, expected)

	def test_a_name_shared_by_two_party_types_is_always_in_the_sample(self):
		anchor = getdate(self.fx.today)
		self.invoice("Payable", (10,))
		self.fx.ple(
			"Shared", "Journal Entry", "X", 5, "Payable", party=self.fx.supplier, account=self.fx.payable
		)
		frappe.db.set_value("Payment Ledger Entry", self.fx.prefix + "Shared", "party_type", "Employee")
		other = self.second_party("Payable")
		self.invoice("Payable", (10,), supplier=other)
		with (
			patch.object(cache_shadow, "SAMPLE_LARGEST", 0),
			patch.object(cache_shadow, "SAMPLE_RANDOM", 0),
		):
			self.assertEqual(
				cache_shadow.sample_parties(self.fx.company, "Payable", anchor), [self.fx.supplier]
			)

	def test_a_refusal_of_the_sample_read_is_not_compared(self):
		self.invoice("Payable", (50,))
		with patch.object(cache_shadow, "_today_sample", side_effect=CalendarSourceUnavailable("too_large")):
			record = self.oracle("Payable")
		self.assertEqual((record["outcome"], record["reason"]), ("not_compared", "sample_refused:too_large"))


class TestSampleCoverage(OracleCase):
	def setUp(self):
		super().setUp()
		patcher = patch.object(cache_shadow, "ORACLE_FULL_LEDGER_ROWS", 0)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_the_record_says_how_many_parties_the_sample_covers(self):
		self.invoice("Payable", (100,))
		self.invoice("Payable", (70,), supplier=self.second_party("Payable"))
		with (
			patch.object(cache_shadow, "SAMPLE_LARGEST", 1),
			patch.object(cache_shadow, "SAMPLE_RANDOM", 0),
		):
			record = self.oracle("Payable")
		self.assertEqual(record["counts"]["parties"], 1)
		self.assertEqual(record["counts"]["parties_total"], 2)

	def test_a_difference_in_a_party_outside_the_sample_is_not_seen_and_the_record_says_so(self):
		other = self.second_party("Payable")
		self.invoice("Payable", (100, 100), paid=30)
		self.invoice("Payable", (70,), supplier=other)
		build = self.build("Payable")
		at = cache_store.ROW_FIELDS.index("party")
		rows = [row for row in build.rows if row[at] != other]
		with (
			patch.object(cache_shadow, "SAMPLE_LARGEST", 1),
			patch.object(cache_shadow, "SAMPLE_RANDOM", 0),
		):
			record = self.oracle("Payable", build, rows)
		self.assertEqual(record["outcome"], "compared_sample")
		self.assertLess(record["counts"]["parties"], record["counts"]["parties_total"])


class TestBankOracle(OracleCase):
	def bank(self):
		self.sync()
		return cache_refresh._read_bank(self.fx.company, getdate(self.fx.today), set())

	def test_stored_balances_equal_the_bank_read(self):
		self.fx.gl(500)
		self.fx.gl(-125, account=self.fx.account("Bank2", "Bank", "Asset"))
		record = self.oracle("Bank", self.bank())
		self.assertEqual((record["outcome"], record["mode"]), ("compared", "bank"))
		self.assertEqual(record["counts"], {"accounts": 2, "differ": 0})

	def test_a_wrong_stored_balance_is_a_diff_with_a_digest_not_a_name(self):
		self.fx.gl(500)
		build = self.bank()
		at = cache_store.ROW_FIELDS.index("remaining")
		wrong = [(*row[:at], Decimal("501.00"), *row[at + 1 :]) for row in build.rows]
		record = self.oracle("Bank", build, wrong)
		self.assertEqual((record["outcome"], record["counts"]), ("diff", {"accounts": 1, "differ": 1}))
		self.assertEqual(len(record["digests"]), 1)
		self.assertNotIn(self.fx.bank, json.dumps(record))

	def test_a_flagged_account_that_the_generation_serves_anyway_is_a_diff(self):
		other = self.fx.account("Bank3", "Bank", "Asset")
		self.fx.gl(100)
		self.fx.gl(10, account=other, finance_book="Other Book")
		build = self.bank()
		forgot = {**build.payload, "multibook_accounts": []}
		record = cache_shadow.night_oracle(
			self.fx.company, "Bank", getdate(self.fx.today), build.rows, forgot
		)
		self.assertEqual((record["outcome"], record["counts"]["differ"]), ("diff", 1))

	def test_flagged_accounts_that_both_refuse_agree(self):
		other = self.fx.account("Bank3", "Bank", "Asset")
		self.fx.gl(100)
		self.fx.gl(10, account=other, finance_book="Other Book")
		record = self.oracle("Bank", self.bank())
		self.assertEqual((record["outcome"], record["counts"]["differ"]), ("compared", 0))

	def test_too_many_accounts_are_not_compared(self):
		self.fx.gl(1)
		build = self.bank()
		with patch.object(cache_shadow, "MAX_BANK_ORACLE_ACCOUNTS", 0):
			record = self.oracle("Bank", build)
		self.assertEqual((record["outcome"], record["reason"]), ("not_compared", "too_many_accounts"))


class TestPersist(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("shadow_persist")
		self.addCleanup(lambda: frappe.db.rollback(save_point="shadow_persist"))
		self.generation = frappe.db.get_next_sequence_val(cache_store.GENERATION)
		frappe.db.sql(
			f"INSERT INTO `tab{cache_store.GENERATION}` (name, company, side, state, creation, modified, owner, "
			"modified_by, docstatus, idx) VALUES (%s, %s, 'Payable', 'building', NOW(6), NOW(6), 'Administrator', "
			"'Administrator', 0, 0)",
			(self.generation, "PCAL-B6-persist"),
		)

	def stored(self):
		return frappe.db.sql(
			f"SELECT shadow FROM `tab{cache_store.GENERATION}` WHERE name=%s", (self.generation,)
		)[0][0]

	def test_a_record_is_stored_as_json_in_the_generation(self):
		record = {"outcome": "compared", "counts": {"rows": 3}, "mode": "full"}
		self.assertTrue(cache_shadow.persist(self.generation, record))
		self.assertEqual(json.loads(self.stored()), record)

	def test_no_record_changes_nothing(self):
		self.assertFalse(cache_shadow.persist(self.generation, None))
		self.assertIsNone(self.stored())

	def test_a_generation_that_is_gone_stores_nothing_returns_false_and_logs_a_class_only(self):
		gone = self.generation + 1000000
		with patch.object(frappe, "logger") as logger:
			self.assertFalse(cache_shadow.persist(gone, {"outcome": "diff"}))
		line = logger.return_value.info.call_args.args[0]
		self.assertEqual(line, "cache_shadow persist_generation_gone")
		self.assertNotIn(str(gone), line)

	def test_the_same_record_stored_twice_still_reports_the_row(self):
		record = {"outcome": "compared"}
		self.assertTrue(cache_shadow.persist(self.generation, record))
		self.assertTrue(cache_shadow.persist(self.generation, record))

	def test_the_store_does_not_commit(self):
		with patch.object(frappe.db, "commit") as commit:
			cache_shadow.persist(self.generation, {"outcome": "diff"})
		commit.assert_not_called()


class TestWiring(TestCase):
	def test_the_refresh_imports_the_shadow_module_itself_and_has_no_lookup_by_name(self):
		self.assertIs(cache_refresh.cache_shadow, cache_shadow)
		for name in ("_night_oracle", "_shadow_module", "_ORACLE", "importlib"):
			self.assertFalse(hasattr(cache_refresh, name), name)

	def test_the_outcomes_are_the_plan_s_words(self):
		self.assertEqual(
			set(cache_shadow.OUTCOMES),
			{"compared", "compared_sample", "open_equal_window_refusal", "diff", "not_compared"},
		)

	def test_the_module_reads_no_cache_row_table(self):
		from pathlib import Path

		text = Path(cache_shadow.__file__).read_text()
		self.assertNotIn(cache_store.ROW, text)
		self.assertNotIn("`tab{cache_store.ROW}`", text)
