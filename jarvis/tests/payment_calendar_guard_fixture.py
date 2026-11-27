"""The guard-size company for the manual timing run: raw bulk inserts, TEST SITE ONLY, never committed.

`GuardCompany(vouchers, ledger_rows).build()` fills one `CalendarFixtures` company with `vouchers` invoices and
about `ledger_rows` Payment Ledger Entry rows on EACH side (the size guards of today's read count per side). It
writes through `frappe.db.bulk_insert` inside the caller's savepoint; the caller rolls back. The mix is the
reader's worst case: every invoice has a term and a header due date inside the window (settled ones too), about
30 % are open, some have returns, one account per side is foreign (a fifth of its invoices keep a
company-currency residue), and the rest of the ledger rows are unreferenced Payment Entry and Journal Entry
lines.
"""

from __future__ import annotations

import random

import frappe
from frappe.utils import add_days, getdate, now

from jarvis.tests.payment_calendar_fixtures import CalendarFixtures

PARTIES = 50
OPEN_SHARE = 0.30
RETURN_SHARE = 0.03
FOREIGN_SHARE = 0.01
RESIDUE_EVERY = 5
RATE = 80
COMMON = ["creation", "modified", "owner", "modified_by", "docstatus"]
SIDE_KINDS = {
	# side -> (invoice doctype, party type, party field, account field)
	"Receivable": ("Sales Invoice", "Customer", "customer", "debit_to"),
	"Payable": ("Purchase Invoice", "Supplier", "supplier", "credit_to"),
}
INVOICE_FIELDS = [
	"name",
	"company",
	"posting_date",
	"due_date",
	"currency",
	"party_account_currency",
	"conversion_rate",
	"grand_total",
	"base_grand_total",
	"total_advance",
	"is_return",
	"outstanding_amount",
]
PLE_FIELDS = (
	"name company posting_date account_type account account_currency party_type party voucher_type "
	"voucher_no against_voucher_type against_voucher_no amount amount_in_account_currency delinked"
).split()
TERM_FIELDS = (
	"name parent parenttype parentfield idx due_date payment_amount base_payment_amount paid_amount "
	"base_paid_amount discounted_amount"
).split()


class GuardCompany:
	"""Rows per table, built in memory for one company, inserted at the end of `build`."""

	def __init__(self, vouchers: int, ledger_rows: int, seed: int = 20261119):
		self.fx = CalendarFixtures()
		self.vouchers, self.ledger_rows = vouchers, ledger_rows
		self.rng = random.Random(seed)
		self.stamp = now()
		self.today = getdate(self.fx.today)
		# (doctype, fields) -> rows; one bulk insert per column set
		self.tables: dict[tuple, list[list]] = {}
		self.serial = 0
		self.counts = dict.fromkeys(("invoices", "open", "returns", "foreign", "residue", "unreferenced"), 0)

	def build(self) -> dict:
		for side in SIDE_KINDS:
			self._side(side)
		for (table, fields), rows in self.tables.items():
			frappe.db.bulk_insert(table, list(fields), rows, chunk_size=2000)
		self.counts["ledger_rows"] = frappe.db.count("Payment Ledger Entry", {"company": self.fx.company})
		return self.counts

	# ------------------------------------------------------------------ one side
	def _side(self, side: str) -> None:
		parties = self._parties(side)
		main = self.fx.receivable if side == "Receivable" else self.fx.payable
		foreign = self._foreign_account(side)
		used = 0
		for index in range(self.vouchers):
			is_foreign = self.rng.random() < FOREIGN_SHARE
			party = self.rng.choice(parties)
			used += self._invoice(side, index, party, foreign if is_foreign else main, is_foreign)
		self._unreferenced(side, parties, main, max(0, self.ledger_rows - used))

	def _invoice(self, side: str, index: int, party: str, account: str, is_foreign: bool) -> int:
		"""One invoice with its terms, payments and return; returns its ledger row count."""
		doctype, party_type, party_field, account_field = SIDE_KINDS[side]
		rng = self.rng
		name = f"{self.fx.prefix}{side[0]}I{index}"
		posted = add_days(self.today, -rng.randint(1, 330))
		rate = RATE if is_foreign else 1
		total = rng.randint(2, 600) * RATE if is_foreign else rng.randint(100, 50_000)
		is_open = not is_foreign and rng.random() < OPEN_SHARE
		credit = total // 10 if is_open and rng.random() < RETURN_SHARE / OPEN_SHARE else 0
		pays = self._payments(total, is_open, is_foreign)
		residue = is_foreign and self.counts["foreign"] % RESIDUE_EVERY == 0
		self.counts["invoices"] += 1
		self.counts["open"] += is_open
		self.counts["foreign"] += is_foreign
		self.counts["residue"] += residue
		currency = "USD" if is_foreign else "INR"
		self._add(
			doctype,
			[*INVOICE_FIELDS, party_field, account_field],
			[
				name,
				self.fx.company,
				posted,
				add_days(posted, 20),
				currency,
				currency,
				rate,
				total / rate,
				total,
				0,
				0,
				(total - sum(pays) - credit) / rate,
				party,
				account,
			],
			docstatus=1,
		)
		self._terms(doctype, name, posted, total, rng.randint(1, 3), rate)
		self._ple(side, party, account, posted, doctype, name, doctype, name, total, total / rate)
		for number, pay in enumerate(pays):
			self.serial += 1
			voucher, paid_on = (
				f"{self.fx.prefix}PE{self.serial}",
				min(add_days(posted, rng.randint(1, 60)), self.today),
			)
			self._voucher("Payment Entry", voucher, paid_on, party_type, party)
			short = residue and number == len(pays) - 1
			self._ple(
				side,
				party,
				account,
				paid_on,
				"Payment Entry",
				voucher,
				doctype,
				name,
				-pay + (0.15 if short else 0),
				-pay / rate,
			)
		if credit:
			self.counts["returns"] += 1
			self._return(side, doctype, party_field, name, party, account, credit)
		return 1 + len(pays) + bool(credit)

	def _payments(self, total: int, is_open: bool, is_foreign: bool) -> list[int]:
		"""Open: up to two partial payments (never the whole). Settled: in full, in one or two payments."""
		rng = self.rng
		if is_open:
			pays = [rng.randint(1, 4) * (total // 10) for _ in range(rng.randint(0, 2))]
			return pays
		if is_foreign or rng.random() < 0.6:
			return [total]
		return [total // 2, total - total // 2]

	def _return(self, side, doctype, party_field, original, party, account, credit) -> None:
		name = f"{original}R"
		self._add(
			doctype,
			[
				"name",
				"company",
				party_field,
				"posting_date",
				"is_return",
				"return_against",
				"update_outstanding_for_self",
			],
			[name, self.fx.company, party, self.today, 1, original, 0],
			docstatus=1,
		)
		self._ple(side, party, account, self.today, doctype, name, doctype, original, -credit, -credit)

	def _unreferenced(self, side: str, parties: list, account: str, budget: int) -> None:
		"""Payment Entry and Journal Entry lines against themselves: the reader's generic path reads them all."""
		party_type = SIDE_KINDS[side][1]
		for number in range(budget):
			self.serial += 1
			party = self.rng.choice(parties)
			posted = add_days(self.today, -self.rng.randint(0, 330))
			amount = self.rng.randint(10, 5_000) * (1 if number % 4 == 3 else -1)
			doctype = "Payment Entry" if number % 2 == 0 else "Journal Entry"
			voucher = f"{self.fx.prefix}U{self.serial}"
			self._voucher(doctype, voucher, posted, party_type, party)
			self._ple(side, party, account, posted, doctype, voucher, doctype, voucher, amount, amount)
			self.counts["unreferenced"] += 1

	# ------------------------------------------------------------------ masters
	def _parties(self, side: str) -> list[str]:
		party_type = SIDE_KINDS[side][1]
		names = []
		for number in range(PARTIES):
			name = self.fx.prefix + f"{side[0]}Party{number}"
			field = party_type.lower() + "_name"
			self.fx.insert(
				party_type,
				f"{side[0]}Party{number}",
				**{field: name, f"{party_type.lower()}_type": "Company", "disabled": 0},
			)
			names.append(name)
		return names

	def _foreign_account(self, side: str) -> str:
		return self.fx.insert(
			"Account",
			f"Foreign{side}",
			account_name=f"{self.fx.prefix}Foreign{side}",
			company=self.fx.company,
			account_type=side,
			account_currency="USD",
			is_group=0,
			disabled=0,
			report_type="Balance Sheet",
			root_type="Asset" if side == "Receivable" else "Liability",
			lft=1,
			rgt=2,
		)

	# ------------------------------------------------------------------ rows
	def _ple(
		self,
		side,
		party,
		account,
		posted,
		voucher_type,
		voucher_no,
		against_type,
		against_no,
		amount,
		in_account,
	):
		self.serial += 1
		currency = "USD" if in_account != amount else "INR"
		self._add(
			"Payment Ledger Entry",
			PLE_FIELDS,
			[
				f"{self.fx.prefix}L{self.serial}",
				self.fx.company,
				posted,
				side,
				account,
				currency,
				SIDE_KINDS[side][1],
				party,
				voucher_type,
				voucher_no,
				against_type,
				against_no,
				amount,
				in_account,
				0,
			],
		)

	def _terms(self, doctype, parent, posted, total, terms, rate) -> None:
		share = total // terms
		for number in range(terms):
			amount = total - share * (terms - 1) if number == terms - 1 else share
			values = [f"{parent}T{number}", parent, doctype, "payment_schedule", number + 1]
			values += [add_days(posted, 10 + number * 10), amount / rate, amount, 0, 0, 0]
			self._add("Payment Schedule", TERM_FIELDS, values)

	def _voucher(self, doctype, name, posted, party_type, party) -> None:
		"""The submitted header a raw ledger line needs (the fixtures insert the same minimal ones)."""
		meta = frappe.get_meta(doctype)
		values = {
			"company": self.fx.company,
			"posting_date": posted,
			"party_type": party_type,
			"party": party,
		}
		keep = {key: value for key, value in values.items() if meta.has_field(key)}
		self._add(doctype, ["name", *keep], [name, *keep.values()], docstatus=1)

	def _add(self, doctype, fields, values, docstatus=0) -> None:
		# `idx` is a column of every table; only the child rows set it themselves.
		extra = [] if "idx" in fields else ["idx"]
		rows = self.tables.setdefault((doctype, (*fields, *COMMON, *extra)), [])
		rows.append(
			[
				*values,
				self.stamp,
				self.stamp,
				"Administrator",
				"Administrator",
				docstatus,
				*([0] * len(extra)),
			]
		)
