"""Seeded drift test: the built-in reader against today's read while real controllers change the ledger.

One fixed seed and a frozen date make the run repeatable. It runs in one rolled-back transaction on top of the
shared test company, with guards that fail on any commit. Only the wall time is printed, never asserted.

Why seed 2 and a forced tail, not the plan's seed 7: the seeded mix is thin at 60 operations. Seed 7 never
reaches `pe_multi` (all 12 drain-sides still equal), and with seed 2 alone `reconcile`, `return_paid` and
`debit_note` were never attempted, so the reader's re-key probe and return closure were not exercised. The last
`TAIL_LENGTH` operations are therefore a fixed tail (`FORCED_TAIL` in `payment_calendar_drift`: a late return
that folds onto an old open invoice and payment of the rest, two payments in full of old invoices, a late return
of an old paid invoice with its refund, a return of a paid invoice, an unallocated advance and its
reconciliation, a debit note), each retried a few times if it skips. Seed 2 plus the tail reaches every kind in
`REQUIRED_KINDS` with 0 diffs and at most 2 rolled-back controller errors; `MAX_FAILURES` bounds them.

Known limit (checked by mutation): with the re-key probe or the return closure disabled this seed still passes.
The tail does reach the shapes (the folded return's original is added by the closure, `returns` = 1 in the
reader stats), but the result is the same either way: ERPNext only folds a return that fits the original's
outstanding, so a folded original that is not discovered nets to zero and has no row. A refund cannot fold (a
return of a paid invoice keeps its own outstanding). A generic line that targets a key whose own lines are
unread (a payment against a journal entry whose lines are against a closed invoice) cannot be made through
the controllers. Both are covered by the raw-row unit tests in `test_payment_calendar_builtin_reader`.
"""

import random
import time
from collections import Counter
from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_months, get_first_day, get_last_day

from jarvis.payment_calendar import builtin_reader
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.shadow import today_unguarded
from jarvis.tests import _erpnext_masters as masters
from jarvis.tests import payment_calendar_drift as drift_module
from jarvis.tests.payment_calendar_differential import READER_TALLY, compare_reader_with_today
from jarvis.tests.payment_calendar_drift import GuardError, frozen_clock, run_drift, write_guards
from jarvis.tests.payment_calendar_fixtures import require_calendar_test_site
from jarvis.tests.payment_calendar_majors import GATE_NO_V15_READER_PINS, v16_only

FROZEN = "2025-06-16 10:00:00"
ANCHOR = date(2025, 6, 16)
SEED = 2
OPS = 60
# The cadence scales with OPS: at least five drains, so at least ten drain-sides.
DRAIN_EVERY = (OPS // 7, OPS // 5)
MIN_DRAIN_SIDES = 10
# The reader's re-key probe needs `reconcile`; its return closure needs `credit_note`, `debit_note`, `return_paid`.
REQUIRED_KINDS = {
	"pe_multi",
	"unreconcile",
	"cancel_multiref",
	"cancel_amend",
	"credit_note",
	"debit_note",
	"return_paid",
	"refund",
	"fold_return",
	"reconcile",
}
# A controller error rolls its operation back and is counted. More than this means an operation is broken.
MAX_FAILURES = 4
SIDES = ("Receivable", "Payable")


def today_with_lifted_guards(*args):
	return today_unguarded(*args)


def window(anchor: date) -> tuple[str, str]:
	first = get_first_day(add_months(anchor, -12))
	last = get_last_day(add_months(anchor, 11))
	return str(first), str(add_days(last, 30))


def drain_sides(company: str, anchor: date) -> list[str]:
	"""Compare the reader with today's read for both sides: "compared", or "refusal" when both refused alike.

	No header sync: the controllers keep headers. A reader fallback or any difference raises (the comparator
	declares no fallback), so only an agreed outcome is returned.
	"""
	start, end = window(anchor)
	outcomes = []
	for side in SIDES:
		compare = compare_reader_with_today(
			builtin_reader.read_obligations,
			today_with_lifted_guards,
			sync=None,
			expected_fallback=None,
		)
		refusals = READER_TALLY["refusals"]
		try:
			compare(company, side, str(anchor), start, end)
		except CalendarSourceUnavailable:
			pass
		outcomes.append("refusal" if READER_TALLY["refusals"] > refusals else "compared")
	return outcomes


def probe(company: str) -> dict:
	"""Committed-state evidence; equal before and after the seed means nothing leaked."""
	counts = {
		doctype: frappe.db.count(doctype, {"company": company})
		for doctype in ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry")
	}
	counts["Payment Ledger Entry"] = frappe.db.count("Payment Ledger Entry", {"company": company})
	counts["series"] = [
		list(row) for row in frappe.db.sql("select name, current from tabSeries order by name")
	]
	return counts


@v16_only(GATE_NO_V15_READER_PINS)
class TestReaderDrift(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		require_calendar_test_site()
		frappe.set_user("Administrator")
		masters.clear_cache_after_rollback(cls)
		# Created here, outside the seed transaction: the seed's rollback must not drop the company.
		cls.company = masters.ensure_ledger_company()

	def test_seeded_drift_has_no_diffs(self):
		outcomes: list = []
		before = probe(self.company)
		frappe.db.savepoint("drift_seed")
		started = time.monotonic()  # measured outside the frozen clock
		try:
			with frozen_clock(FROZEN), write_guards() as guards:
				self.assertTrue(frappe.flags.in_test)
				summary = run_drift(
					SEED,
					OPS,
					anchor=ANCHOR,
					drain_every=DRAIN_EVERY,
					on_drain=lambda done: outcomes.extend(drain_sides(self.company, ANCHOR)),
				)
		finally:
			frappe.db.rollback(save_point="drift_seed")
			frappe.clear_cache()
		elapsed = time.monotonic() - started
		print(f"\nreader drift: seed={SEED} {summary} sides={Counter(outcomes)} {elapsed:.1f}s")

		self.assertEqual(summary["ops"], OPS)
		self.assertLessEqual(summary["failures"], MAX_FAILURES, summary["failure_kinds"])
		self.assertGreaterEqual(summary["drains"] * len(SIDES), MIN_DRAIN_SIDES)
		self.assertGreaterEqual(Counter(outcomes)["compared"], MIN_DRAIN_SIDES)
		# No fallback (the control total passed every time) and no refusal (every drain-side was compared).
		self.assertEqual(set(outcomes), {"compared"})
		# The generator still reaches the operations that re-key and re-link ledger lines.
		self.assertLessEqual(REQUIRED_KINDS, set(summary["kinds"]))
		for counter in ("commits", "rollbacks", "transaction_sql"):
			self.assertEqual(guards[counter], 0, counter)
		self.assertEqual(probe(self.company), before)

	def test_drift_runs_without_a_header_sync(self):
		seen, real = [], compare_reader_with_today

		def spy(reader, today, **kwargs):
			seen.append(kwargs)
			return real(reader, today, **kwargs)

		with patch(f"{__name__}.compare_reader_with_today", spy):
			drain_sides(self.company, ANCHOR)
		self.assertEqual(len(seen), len(SIDES))
		for kwargs in seen:
			self.assertIsNone(kwargs["sync"])
			self.assertIsNone(kwargs["expected_fallback"])

	def test_the_supplier_group_is_created_on_a_site_with_no_root_group(self):
		"""A bare site (CI) has no 'All Supplier Groups': the generator's group is then a root itself."""
		frappe.db.savepoint("pcal_no_root")
		self.addCleanup(frappe.db.rollback, save_point="pcal_no_root")
		generator = drift_module.DriftGenerator(random.Random(1), ANCHOR, seed=1, warmup=10, forced_from=0)
		group = "_PCALZ No Root Group"
		real = frappe.db.get_value

		def no_root(doctype, filters=None, *args, **kwargs):
			if doctype == "Supplier Group" and isinstance(filters, dict) and "is_group" in filters:
				return None  # the root lookup only
			return real(doctype, filters, *args, **kwargs)

		with (
			patch.object(drift_module, "SUPPLIER_GROUP", group),
			patch.object(frappe.db, "get_value", side_effect=no_root),
		):
			generator._parties()
		self.assertTrue(real("Supplier Group", group, "name"))

	def test_the_frozen_clock_is_restored_in_a_module_first_imported_inside_it(self):
		import importlib
		import sys
		import types

		from frappe.utils import data as frappe_data

		name = "jarvis_pcal_clock_probe"
		real_now_datetime = frappe_data.now_datetime
		with frozen_clock(FROZEN):
			module = types.ModuleType(name)
			module.now_datetime = frappe_data.now_datetime  # a lazy `from frappe.utils import now_datetime`
			sys.modules[name] = module
			self.assertEqual(module.now_datetime().year, 2025)
			self.assertEqual(frappe_data.nowdate(), "2025-06-16")
		try:
			# By identity, not by date: a run on the frozen date itself must pass too.
			self.assertIs(module.now_datetime, real_now_datetime)
			self.assertIs(frappe_data.now_datetime, real_now_datetime)
			# Read the real date on both sides of nowdate to allow a midnight change.
			before_date = real_now_datetime().date().isoformat()
			restored_date = frappe_data.nowdate()
			after_date = real_now_datetime().date().isoformat()
			self.assertIn(restored_date, {before_date, after_date})
		finally:
			sys.modules.pop(name, None)
		importlib.invalidate_caches()

	def test_the_write_guard_fails_on_commit_and_is_restored(self):
		original = frappe.db.commit
		with write_guards() as guards:
			with self.assertRaises(GuardError):
				frappe.db.commit()
			with self.assertRaises(GuardError):
				frappe.db.sql("COMMIT")
			with self.assertRaises(GuardError):
				frappe.db.rollback()
			frappe.db.savepoint("guard_probe")
			frappe.db.rollback(save_point="guard_probe")
		self.assertEqual(guards["commits"], 1)
		self.assertEqual(guards["transaction_sql"], 1)
		self.assertEqual(guards["rollbacks"], 1)
		self.assertEqual(frappe.db.commit, original)
