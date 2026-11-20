"""Seeded drift generator for the built-in reader: real ERPNext controllers, one rolled-back transaction.

`run_drift` builds a small company ledger through the operations in `payment_calendar_drift_ops` and calls
`on_drain` every few operations so the caller can compare the reader with today's read. Nothing here commits;
`write_guards` fails the run if anything tries. No party, voucher or company names leave this module.
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime

import frappe
from frappe.utils import add_days
from frappe.utils import data as frappe_data

from jarvis.tests import _erpnext_masters as masters
from jarvis.tests.payment_calendar_drift_ops import (
	ITEM,
	N_CUSTOMERS,
	N_SUPPLIERS,
	OPERATIONS,
	TEMPLATE_2X,
	TEMPLATE_3X,
	WARMUP_OPERATIONS,
	DriftOperations,
	Skip,
	customer_name,
	supplier_name,
)


@contextmanager
def frozen_clock(moment: str) -> Iterator[None]:
	"""The site's clock at `moment` (`YYYY-MM-DD HH:MM:SS`), for the code inside the block.

	Stdlib mock, not freezegun: CI does not install it (Frappe's own `freeze_time` needs it). `nowdate`,
	`today`, `now` and `nowtime` read `frappe.utils.data.now_datetime` at call time; every module that imported
	`now_datetime` by name holds its own reference, so each of those is swapped too and restored on exit,
	including a module first imported inside the block.
	The monotonic clocks stay real.
	"""
	fixed = datetime.strptime(moment, "%Y-%m-%d %H:%M:%S")
	original = frappe_data.now_datetime

	def fake():
		return fixed

	def swap(old, new):
		for module in list(sys.modules.values()):
			if getattr(module, "__dict__", {}).get("now_datetime") is old:
				module.now_datetime = new

	swap(original, fake)
	try:
		yield
	finally:
		# By identity, not from a list taken at entry: a module first imported inside the block
		# (a lazy import in a controller) took the fake, and must get the real function back too.
		swap(fake, original)


SAVEPOINT = "drift_op"
SUPPLIER_GROUP = "_PCALZ Supplier Group"
# After warm-up the seeded mix is too thin to reach these on every seed, so a fixed tail forces them once:
# `fold_return` returns an old open invoice and pays the rest (the return folds onto a closed original);
# `pe_full` pays old invoices in full for `refund` and `return_paid`; `pe_advance` makes an unallocated
# payment for `reconcile`. Order matters: `fold_return` first, it needs an old invoice that is still open.
FORCED_TAIL = (
	"fold_return",
	"pe_full",
	"pe_full",
	"refund",
	"return_paid",
	"pe_advance",
	"reconcile",
	"debit_note",
)
TAIL_LENGTH = len(FORCED_TAIL) + 3
FORCED_TRIES = 6
TEMPLATES = {
	TEMPLATE_2X: [("_PCALZ 15d 50", 50, 15), ("_PCALZ 45d 50", 50, 45)],
	TEMPLATE_3X: [("_PCALZ 0d 30", 30, 0), ("_PCALZ 30d 30", 30, 30), ("_PCALZ 60d 40", 40, 60)],
}


class GuardError(BaseException):
	"""A commit, a full rollback or a transaction statement was attempted inside the seed.

	A BaseException, so no controller `except Exception` can swallow it.
	"""


@contextmanager
def write_guards() -> Iterator[dict]:
	"""Fail on commit, full rollback and transaction SQL; count RQ enqueues instead of queueing them.

	A savepoint rollback is allowed. Everything is restored in `finally`. Yields the live counters.
	"""
	import frappe.utils.background_jobs as background_jobs

	db = frappe.db
	counters = {"commits": 0, "rollbacks": 0, "transaction_sql": 0, "enqueued": 0}
	original = {"commit": db.commit, "rollback": db.rollback, "sql": db.sql}
	original_queue, original_in_test = background_jobs.get_queue, frappe.flags.in_test

	def commit(*args, **kwargs):
		counters["commits"] += 1
		raise GuardError("commit inside the drift seed")

	def rollback(*args, save_point=None, **kwargs):
		if save_point:
			return original["rollback"](*args, save_point=save_point, **kwargs)
		counters["rollbacks"] += 1
		raise GuardError("full rollback inside the drift seed")

	def sql(query, *args, **kwargs):
		head = query.lstrip()[:40].lower() if isinstance(query, str) else ""
		if head.startswith(("commit", "start transaction", "begin", "set transaction")) or (
			head.startswith("rollback") and not head.startswith("rollback to savepoint")
		):
			counters["transaction_sql"] += 1
			raise GuardError("transaction statement inside the drift seed")
		return original["sql"](query, *args, **kwargs)

	class CountingQueue:
		count = 0

		def enqueue_call(self, *args, **kwargs):
			counters["enqueued"] += 1

	db.commit, db.rollback, db.sql = commit, rollback, sql
	background_jobs.get_queue = lambda *args, **kwargs: CountingQueue()
	frappe.flags.in_test = True
	try:
		yield counters
	finally:
		db.commit, db.rollback, db.sql = original["commit"], original["rollback"], original["sql"]
		background_jobs.get_queue = original_queue
		frappe.flags.in_test = original_in_test


class DriftGenerator(DriftOperations):
	def __init__(
		self,
		rng: random.Random,
		anchor: date,
		*,
		seed: int = 0,
		warmup: int = 15,
		forced_from: int | None = None,
	):
		self.rng, self.anchor, self.seed, self.warmup = rng, anchor, seed, warmup
		self.forced_from = forced_from
		self.counts: Counter = Counter()
		self.big_payment: str | None = None
		self.last_advance: tuple[str, str] | None = None
		self.forcing: str | None = None
		self.pending = list(FORCED_TAIL)
		self.tries = 0
		self.done = 0
		self.skips = self.failures = 0
		self.failure_kinds: Counter = Counter()

	def masters(self) -> None:
		"""Masters inside the caller's transaction. The company comes from the shared helper, created before."""
		masters.ensure_ledger_company()
		masters.ensure_item_group_and_uom()
		masters.ensure_selling_price_list()
		masters.ensure_fiscal_years(
			add_days(self.anchor, -600), add_days(self.anchor, -300), self.anchor, add_days(self.anchor, 25)
		)
		self._item_and_price_list()
		self._payment_terms()
		self._parties()

	def step(self) -> str | None:
		"""One weighted operation in a savepoint. Returns its kind, or None for a skip or a controller error.

		A controller error rolls back to the savepoint and is counted by kind in `failure_kinds`; the caller
		bounds it, so a broken operation cannot silently hollow out the run.
		"""
		kind = self._next_kind()
		forced = bool(self.pending) and self.pending[0] == kind and self._in_tail()
		self.forcing = kind if forced else None
		frappe.db.savepoint(SAVEPOINT)
		try:
			getattr(self, f"op_{kind}")()
		except Skip:
			frappe.db.rollback(save_point=SAVEPOINT)
			self.skips += 1
			return self._miss(forced)
		except GuardError:
			raise
		except Exception:
			frappe.db.rollback(save_point=SAVEPOINT)
			frappe.clear_cache()
			self.failures += 1
			self.failure_kinds[kind] += 1
			return self._miss(forced)
		finally:
			self.forcing = None
		self.done += 1
		if forced:
			self.pending.pop(0)
			self.tries = 0
		return kind

	def _miss(self, forced: bool) -> None:
		"""A forced operation that skipped or failed is tried again; after FORCED_TRIES it is dropped."""
		if forced:
			self.tries += 1
			if self.tries >= FORCED_TRIES:
				self.pending.pop(0)
				self.tries = 0

	def _in_tail(self) -> bool:
		return self.forced_from is not None and self.done >= self.forced_from

	def _next_kind(self) -> str:
		if self._in_tail() and self.pending:
			return self.pending[0]
		wanted = self.done >= self.warmup * 4 and not self.big_payment
		if wanted and self.rng.random() < 0.15:
			return "pe_big"
		weights = WARMUP_OPERATIONS if self.done < self.warmup else OPERATIONS
		return self.rng.choices(list(weights), weights=list(weights.values()))[0]

	def _item_and_price_list(self) -> None:
		if not frappe.db.exists("Item", ITEM):
			values = {
				"doctype": "Item",
				"item_code": ITEM,
				"item_group": masters.ITEM_GROUP,
				"stock_uom": masters.UOM,
				"is_stock_item": 0,
				"is_sales_item": 1,
				"is_purchase_item": 1,
			}
			if frappe.get_meta("Item").has_field("gst_hsn_code"):
				values["gst_hsn_code"] = "01011010"
			frappe.get_doc(values).insert(ignore_permissions=True)
		if not frappe.db.exists("Price List", "_PCALZ Buying"):
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": "_PCALZ Buying",
					"currency": "INR",
					"buying": 1,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)

	def _payment_terms(self) -> None:
		for template, terms in TEMPLATES.items():
			for term, portion, days in terms:
				if not frappe.db.exists("Payment Term", term):
					frappe.get_doc(
						{
							"doctype": "Payment Term",
							"payment_term_name": term,
							"invoice_portion": portion,
							"due_date_based_on": "Day(s) after invoice date",
							"credit_days": days,
						}
					).insert(ignore_permissions=True)
			if not frappe.db.exists("Payment Terms Template", template):
				rows = [
					{
						"payment_term": term,
						"invoice_portion": portion,
						"due_date_based_on": "Day(s) after invoice date",
						"credit_days": days,
					}
					for term, portion, days in terms
				]
				frappe.get_doc(
					{
						"doctype": "Payment Terms Template",
						"template_name": template,
						"allocate_payment_based_on_payment_terms": 1,
						"terms": rows,
					}
				).insert(ignore_permissions=True)

	def _parties(self) -> None:
		if not frappe.db.exists("Supplier Group", SUPPLIER_GROUP):
			# The site's own root group: a bare site (CI) may have none, and the group is then a root itself.
			root = frappe.db.get_value(
				"Supplier Group", {"is_group": 1, "parent_supplier_group": ["in", ["", None]]}, "name"
			)
			frappe.get_doc(
				{
					"doctype": "Supplier Group",
					"supplier_group_name": SUPPLIER_GROUP,
					"is_group": 0,
					"parent_supplier_group": root,
				}
			).insert(ignore_permissions=True)
		for index in range(N_CUSTOMERS):
			# ensure_customer names the customer after its customer_name (the default ERPNext naming).
			assert masters.ensure_customer(customer_name(index)) == customer_name(index)
		for index in range(N_SUPPLIERS):
			if not frappe.db.exists("Supplier", supplier_name(index)):
				frappe.get_doc(
					{
						"doctype": "Supplier",
						"supplier_name": supplier_name(index),
						"supplier_group": SUPPLIER_GROUP,
					}
				).insert(ignore_permissions=True, set_name=supplier_name(index))


def run_drift(
	seed: int,
	ops: int,
	*,
	anchor: date,
	drain_every: tuple[int, int],
	on_drain: Callable[[int], None],
) -> dict:
	"""Run `ops` successful operations; call `on_drain(done)` every `drain_every` (low, high) of them.

	The caller owns the transaction and the guards. Returns counts only, no names.
	"""
	rng = random.Random(seed)
	generator = DriftGenerator(
		rng, anchor, seed=seed, warmup=max(10, ops // 4), forced_from=max(0, ops - TAIL_LENGTH)
	)
	generator.masters()
	kinds: Counter = Counter()
	attempts, drains, since = 0, 0, 0
	next_drain = rng.randint(*drain_every)
	while generator.done < ops and attempts < ops * 4:
		attempts += 1
		kind = generator.step()
		if kind is None:
			continue
		kinds[kind] += 1
		since += 1
		if since >= next_drain or generator.done == ops:
			on_drain(generator.done)
			drains, since, next_drain = drains + 1, 0, rng.randint(*drain_every)
	return {
		"ops": generator.done,
		"attempts": attempts,
		"skips": generator.skips,
		"failures": generator.failures,
		"failure_kinds": dict(generator.failure_kinds),
		"drains": drains,
		"kinds": dict(kinds),
	}
