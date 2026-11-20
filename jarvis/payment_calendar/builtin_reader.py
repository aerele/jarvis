"""Built-in reader: today's obligations without today's full-history ledger scan. MariaDB only.

The reader subclasses today's `CalendarReport` and replaces only the ledger fetch inside ERPNext's
`get_data`: it reads the ledger lines that can land on an open, orphan or generic voucher key through
indexed criteria, then runs ERPNext's own `init_voucher_balance` / `update_voucher_balance` and today's
`build_data` over them. Every figure comes from ledger lines through ERPNext's code; invoice headers
(`outstanding_amount`) are used for DISCOVERY only.

Two checks keep the smaller read honest, and either one ends in `ReaderFallback` (today's read then runs):

* the re-key probe: a read line whose target key is absent from the read lines would fall back to the
  paying voucher's own key, but in the full ledger the target key may exist. The probe looks the target
  up by the indexed `voucher_no` and reads its lines, to a bounded fixed point;
* the control total: one SQL aggregate with the report's own predicate proves that the lines NOT read sum
  to zero. It proves nothing about which keys they belong to; the closure above covers that.

Discovery finds the invoices whose lines are read: open ones (header), orphans, returns, and three sets that
make today's other rules exact. `due_in_window` is today's settled candidate rule (a term or the header due
inside the window, whatever the settlement date); `fx` is every invoice with a company-currency residual on a
foreign account; `multi` is every invoice with lines on two (account, party) keys over the full history, so the
inherited Counter refuses `multiple_invoice_accounts` exactly when today does.

Residual error boundary: `_reader_errors` wraps the reader's own steps (control, discovery, line and detail
reads, control-total check) and maps a programming error or a Decimal error there to a fallback. Other database
errors (lock wait 1205, deadlock 1213, 1969) are not mapped and propagate: the caller must classify them. `init_voucher_balance`, `update_voucher_balance` and `build_data` run on the
reader-built inputs OUTSIDE it, because an upstream error there must map to today's refusal. So a bug in the
SHAPE of a reader-built input (for example an `invoice_details` entry that is not a mapping) surfaces as
today's `CalendarSourceUnavailable`, not as a fallback; a MISSING `invoice_details` entry is read as `{}` by
upstream and raises nothing. Tests pin both.

Memory: the lines read are held in Python. Three caps (`GENERIC_LINE_CAP`, `TOTAL_LINE_CAP`,
`RANK_PARTY_CAP`) end a read that would hold too many in `ReaderFallback("too_many_lines")`; the header,
schedule and aggregate scans still grow with the history, and the database does that work.

No known difference from today's read: amounts, rows, notes, reasons, refusals, row ids and row order are
equal. A row id is the row's position in the whole result, which follows the order in which the report's
`init_voucher_balance` first meets each key, by the line's OWN voucher. The lines read could meet a key later
than the full ledger does (a voucher whose first line pays an invoice the read skipped), so the INIT pass
also sees the other lines of the vouchers whose key a read line creates (`_other_lines_of_read_vouchers`). They
feed init only: never the update pass, the control total or a row.

Nothing here writes, commits or rolls back, and nothing outside the tests imports this module (yet).
"""

import copy
import re
from collections import OrderedDict, namedtuple
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import cache
from time import perf_counter

import frappe
from frappe.query_builder import Criterion
from frappe.query_builder.functions import Count, Sum
from pypika.functions import Cast
from pypika.terms import Function, LiteralValue

from jarvis.payment_calendar import compatibility, sources
from jarvis.payment_calendar.books import validate_obligation_books
from jarvis.payment_calendar.compatibility import source_profile
from jarvis.payment_calendar.eligibility import reader_ineligibility
from jarvis.payment_calendar.errors import TIMEOUTS, CalendarSourceUnavailable, ReaderFallback

# Names per `IN` list, the same size as the other batched reads of the calendar.
CHUNK = 500
# Names per `IN` list of the invoice-detail reads: primary-key lookups, so a long list stays cheap.
DETAIL_CHUNK = 2000
# Strings per ranking statement; a group of strings that ranks together is never split.
RANK_CHUNK = 2000
COLLATION_NAME = re.compile(r"[A-Za-z0-9_]+")
# Case-insensitive Unicode and general collations: digits sort before letters, and the letters A-Z by
# alphabet with case ignored, which is what `str.casefold` gives on a name of digits and ASCII letters only.
LETTER_COLLATION = re.compile(r"utf8mb[34]_(general|unicode|unicode_520|uca1400_ai)_ci")
PLAIN_NAME = re.compile(r"[0-9A-Za-z]+")
# The report's ledger ORDER BY, which `sort_like_the_report` repeats (a test compares it with the report's
# own `ple_query`): posting date, party, then the line name (added by sources.CalendarReport).
REPORT_ORDER = ("posting_date", "party", "name")
# Memory ceilings, in ledger lines. Each line is a namedtuple of about 20 fields (roughly 2 KB with its
# strings and Decimals), so 150,000 lines are about 300 MB held at once next to the sort and the key maps.
# The generic (non-invoice) types are read whole, with no narrowing, so they get the tightest cap.
# The total is the number of lines the report's predicate sees for the side, the size of the history that
# the accumulation and the sort would walk if most of it were read.
GENERIC_LINE_CAP = 150_000
TOTAL_LINE_CAP = 400_000
# Distinct parties sent to the database to be ranked (one string each, in statements of RANK_CHUNK).
RANK_PARTY_CAP = 200_000
# The two composite indexes ERPNext adds to the ledger table: every line read by voucher goes through them.
LEDGER_TABLE = "tabPayment Ledger Entry"
LEDGER_INDEXES = ("against_voucher_no_against_voucher_type_index", "voucher_no_voucher_type_index")
# Probe rounds before the re-key fixed point is given up as unresolved.
MAX_REKEY_ROUNDS = 8
JOURNAL_ENTRY = "Journal Entry"

# side -> (invoice doctype, the invoice's ledger account field)
SIDES = {
	"Receivable": ("Sales Invoice", "debit_to"),
	"Payable": ("Purchase Invoice", "credit_to"),
}

_INVOICE_READS = {
	"name",
	"docstatus",
	"company",
	"posting_date",
	"due_date",
	"is_return",
	"return_against",
	"update_outstanding_for_self",
	"outstanding_amount",
}
# What today's InvoiceReconciler reads for every invoice of the read (reconciliation.py), repaired or not.
_RECONCILER_READS = {
	"currency",
	"party_account_currency",
	"conversion_rate",
	"grand_total",
	"rounded_total",
	"base_grand_total",
	"base_rounded_total",
	"total_advance",
	"write_off_amount",
}
# Every field the reader reads (itself or through the ERPNext methods and reconciler it runs) on the four
# voucher doctypes. eligibility.READER_FIELDS must cover it: a field masked for the user would be read unmasked.
READER_READS = {
	"Sales Invoice": _INVOICE_READS | _RECONCILER_READS | {"customer", "debit_to", "po_no", "sales_partner"},
	"Purchase Invoice": _INVOICE_READS
	| _RECONCILER_READS
	| {"supplier", "credit_to", "bill_no", "bill_date"},
	"Journal Entry": {
		"name",
		"docstatus",
		"company",
		"posting_date",
		"voucher_type",
		"due_date",
		"bill_no",
		"bill_date",
	},
	"Payment Entry": set(),
}


@contextmanager
def _reader_errors():
	"""A programming error or a Decimal error in reader-only code is a fallback, never a refusal like today's.

	`ArithmeticError` covers `decimal.InvalidOperation` (an amount that does not parse). A database error is
	not mapped: a lock wait or a deadlock says nothing about the reader, so it propagates to the caller.
	"""
	try:
		yield
	except (AttributeError, KeyError, TypeError, ArithmeticError) as exc:
		raise ReaderFallback("reader_error") from exc


MISSING_KEY = 1176  # MariaDB: Key '...' doesn't exist in table '...' (a FORCE INDEX hint on a missing index)
_INDEXES: dict[tuple, bool] = {}
_COLLATIONS: dict[tuple, str | None] = {}


def _index_exists(table: str, index: str) -> bool:
	"""Whether `table` has `index` in this database, asked once per process (information_schema.STATISTICS).

	An index hint is only a speed-up: it is added only when the index is there, so a site that lacks it
	(dropped, never created) reads the same rows through the optimizer's own choice.
	"""
	key = (frappe.local.site, table, index)
	if key not in _INDEXES:
		_INDEXES[key] = bool(
			frappe.db.sql(
				"SELECT 1 FROM information_schema.STATISTICS WHERE table_schema = DATABASE()"
				" AND table_name = %s AND index_name = %s LIMIT 1",
				(table, index),
			)
		)
	return _INDEXES[key]


@contextmanager
def _hint_errors():
	"""A statement that fails only because its index hint names a missing index is a fallback, never a refusal.

	Backstop of `_index_exists` (the answer is cached, the index may go while the process lives). The same
	statement without the hint is not retried here: the caller runs today's read on the fallback.
	"""
	try:
		yield
	except frappe.db.OperationalError as exc:
		if exc.args and exc.args[0] == MISSING_KEY:
			_INDEXES.clear()
			raise ReaderFallback("reader_error") from exc
		raise


def require_supported_source() -> None:
	"""`ReaderFallback` unless this is a major with audited pins whose pinned sources are all unchanged.

	Any failure to read the pinned sources (an import that raises something other than ImportError, a
	missing file) counts as drift: the pins are the reader's audit, so an unreadable source is unaudited.
	A job deadline passes through untouched. The drifted pin names are the fallback's `detail`.
	"""
	try:
		major = source_profile().major
	except CalendarSourceUnavailable as exc:
		raise ReaderFallback("unsupported_major") from exc
	if major != "16":
		raise ReaderFallback("unsupported_major")
	try:
		drift = compatibility.reader_pin_drift()
	except TIMEOUTS:
		raise
	except Exception as exc:
		raise ReaderFallback("pin_drift") from exc
	if drift:
		raise ReaderFallback("pin_drift", tuple(drift))


def require_ledger_indexes() -> None:
	"""`ReaderFallback` unless the two composite ledger indexes exist: without them the by-voucher reads scan."""
	for index in LEDGER_INDEXES:
		if not _index_exists(LEDGER_TABLE, index):
			raise ReaderFallback("reader_error")


@dataclass(frozen=True)
class LedgerControl:
	"""What the ledger holds for the report's predicate: the sum of all amounts and the lines per type pair."""

	total: Decimal
	# (voucher_type, against_voucher_type) -> line count; the against type may be None
	pairs: dict[tuple[str, str | None], int]


@dataclass(frozen=True)
class Discovery:
	"""Invoice names whose ledger lines must be read, by the reason they were found."""

	open: frozenset[str]
	due_in_window: frozenset[str]
	fx: frozenset[str]
	multi: frozenset[str]
	orphans: frozenset[str]
	# Added last, over the union of the others: returns and originals that fold onto each other.
	returns: frozenset[str]
	# Every against_voucher_type other than the side's invoice type (None for a NULL one)
	generic_types: tuple[str | None, ...]

	def invoices(self) -> frozenset[str]:
		return self.open | self.due_in_window | self.fx | self.multi | self.orphans | self.returns


def line_class(fields: tuple[str, ...]):
	"""A cheap row type for the ledger lines: attribute access like `frappe._dict`, a field never selected is None.

	ERPNext's accumulation reads `ple.remarks` although the report query only selects it with `show_remarks`:
	`frappe._dict` answers None for it, so the row type does too. A namedtuple is built by one C call per row
	(a `frappe._dict` costs a dict per row), which matters at 100k lines.
	"""
	base = namedtuple("Line", fields)

	class Line(base):
		__slots__ = ()

		def __getattr__(self, name):
			if name.startswith("__"):
				raise AttributeError(name)
			return None

	return Line


def _chunks(names, size=None):
	size = size or CHUNK
	names = list(names)
	for start in range(0, len(names), size):
		yield names[start : start + size]


def _ledger_predicate(ple, company, account_type, accounts):
	"""The report's own ledger predicate, minus the posting date (prepare_conditions, add_common_filters).

	`party_type != 'Employee'` is the report's own criterion on Receivable only, with SQL `!=` semantics:
	a NULL party type is excluded exactly as the report excludes it.
	"""
	criteria = [ple.company == company, ple.account.isin(accounts), ple.delinked == 0]
	if account_type == "Receivable":
		criteria.append(ple.party_type != "Employee")
	return criteria


def ledger_control(company: str, account_type: str, anchor, accounts: list[str]) -> LedgerControl:
	"""One aggregate with the report's predicate; no ledger row reaches Python.

	The sum is read as text and parsed as Decimal: frappe's database layer turns a DECIMAL result into a
	float, which cannot hold a 21-digit amount exactly.
	"""
	ple = frappe.qb.DocType("Payment Ledger Entry")
	# MariaDB's CAST has CHAR; Postgres needs VARCHAR (CHAR alone would cut the text to one character).
	text = Cast(Sum(ple.amount), "CHAR" if frappe.db.db_type == "mariadb" else "VARCHAR")
	rows = (
		frappe.qb.from_(ple)
		.select(ple.voucher_type, ple.against_voucher_type, text, Count("*"))
		.where(
			Criterion.all(_ledger_predicate(ple, company, account_type, accounts))
			& (ple.posting_date <= anchor)
		)
		.groupby(ple.voucher_type, ple.against_voucher_type)
		.run()
	)
	total, pairs = Decimal(0), {}
	for voucher_type, against_type, amount, count in rows:
		total += Decimal(amount or 0)
		pairs[(voucher_type, against_type)] = count
	return LedgerControl(total, pairs)


def control_total_matches(balances: Decimal, ledger: Decimal, precision: int) -> bool:
	"""True when two sums differ by less than half a unit of the last place of the currency precision."""
	return abs(balances - ledger) < Decimal(5).scaleb(-(precision + 1))


def column_collations(columns: tuple[str, ...]) -> dict[str, str | None]:
	"""The collation of each named column of the ledger table; None where the database has none (Postgres).

	Read from the catalog, so that the ranks below follow the collation the report's own `ORDER BY` uses.
	Asked once per process and column, like the index check: a collation changed under a live worker is
	picked up when the worker restarts, and a stale one can only reorder row ids, never change an amount.
	"""
	if frappe.db.db_type != "mariadb":
		return dict.fromkeys(columns)
	site = frappe.local.site
	missing = tuple(column for column in columns if (site, LEDGER_TABLE, column) not in _COLLATIONS)
	if missing:
		found = dict(
			frappe.db.sql(
				"SELECT COLUMN_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
				"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME IN %s",
				(LEDGER_TABLE, missing),
			)
		)
		# Only a catalog value that is safe to put in statement text is kept.
		for column in missing:
			_COLLATIONS[(site, LEDGER_TABLE, column)] = _safe_collation(found.get(column))
	return {column: _COLLATIONS[(site, LEDGER_TABLE, column)] for column in columns}


def _safe_collation(collation: str | None) -> str | None:
	"""The name goes into the statement text (a collation cannot be a parameter): a plain name only."""
	if collation and not COLLATION_NAME.fullmatch(collation):
		raise ReaderFallback("reader_error")
	return collation


def collation_ranks(groups: list[list[str]], collation: str | None) -> list[dict[str, int]]:
	"""Per group, each string's rank under the database's own collation (equal strings share a rank).

	One statement per run of groups (a group is never split, so its ranks stay comparable). The database
	ranks the strings, not Python: a casefold does not order accents or punctuation as the collation does,
	and the report's ledger query is ordered by the collation. `frappe.db.sql` and not `frappe.qb`: pypika
	has no derived table of literals with a window function, and every value is a bound parameter.
	"""
	collation = _safe_collation(collation)
	ranks, start = [], 0
	while start < len(groups):
		end, size = start, 0
		while end < len(groups) and (end == start or size + len(groups[end]) <= RANK_CHUNK):
			size += len(groups[end])
			end += 1
		values, parts = [], []
		for index, strings in enumerate(groups[start:end]):
			for string in strings:
				parts.append("SELECT %s AS g, %s AS s" if not parts else "SELECT %s, %s")
				values += [index, string]
		order = f"s COLLATE {collation}" if collation else "s"
		rows = frappe.db.sql(
			f"SELECT g, s, DENSE_RANK() OVER (PARTITION BY g ORDER BY {order}) FROM ("
			+ " UNION ALL ".join(parts)
			+ ") AS v",
			values,
		)
		found = [{} for _ in range(start, end)]
		for index, string, rank in rows:
			found[index][string] = rank
		ranks += found
		start = end
	return ranks


def plain_ranks(names: list[str], collation: str | None) -> dict[str, int] | None:
	"""Each name's rank by `str.casefold`, or None when that is not the database's order for these names.

	The line names of the ledger are hashes of digits and lowercase letters, and sending them all to the
	database to be ranked costs about as much as reading them. For names of digits and ASCII letters only,
	under a case-insensitive Unicode or general collation, the casefold order is the collation's order
	(a test checks both against the database). Any other name or collation is ranked by the database.
	"""
	if not collation or not LETTER_COLLATION.fullmatch(collation):
		return None
	if not all(PLAIN_NAME.fullmatch(name) for name in names):
		return None
	order = {key: rank for rank, key in enumerate(sorted({name.casefold() for name in names}))}
	return {name: order[name.casefold()] for name in names}


def sort_like_the_report(lines: list) -> list:
	"""`lines` in the order of the report's own ledger query (`REPORT_ORDER`): posting date, party, line name.

	The reads come back unordered (see `_unordered_query`) and are ordered here, once. Party and name are
	ordered by the database's collation (see `collation_ranks`, `plain_ranks`); only strings that can tie
	are ranked: all distinct parties (they decide the order) and the names of lines that share a date and a
	party rank. A NULL party sorts first, as in MariaDB.
	"""
	if len(lines) < 2:
		return list(lines)
	collations = column_collations(("party", "name"))
	parties = sorted({line.party for line in lines if line.party is not None})
	if len(parties) > RANK_PARTY_CAP:
		raise ReaderFallback("too_many_lines")
	ranked = collation_ranks([parties], collations["party"])[0] if len(parties) > 1 else {}
	# MariaDB puts NULL first, Postgres last.
	null_rank = -1 if frappe.db.db_type == "mariadb" else len(parties)
	ranked[None] = null_rank
	# One (date, party rank) per line, computed once: the tie groups and the final sort both use it.
	slots = [(line.posting_date, ranked.get(line.party, 0)) for line in lines]
	ties = {}
	for slot, line in zip(slots, lines, strict=True):
		ties.setdefault(slot, []).append(line.name)
	name_rank, database_groups = {}, []
	for names in ties.values():
		if len(names) > 1:
			plain = plain_ranks(names, collations["name"])
			if plain is None:
				database_groups.append(names)
			else:
				name_rank.update(plain)
	if database_groups:
		for group in collation_ranks(database_groups, collations["name"]):
			name_rank.update(group)
	# The position is the last key: lines that tie on all three keep the order they came in (a stable sort).
	order = sorted(
		(slot[0], slot[1], name_rank.get(line.name, 0), position)
		for position, (slot, line) in enumerate(zip(slots, lines, strict=True))
	)
	return [lines[position] for (_, _, _, position) in order]


@cache
def _builtin_class():
	class BuiltinReport(sources._report_class()):
		# A caller's dict that receives the stats of one read, whatever its outcome (see `read_obligations`).
		stats_sink = None
		# Row type per selected field list (see `line_class`).
		_line_classes: dict = None
		_read_query = None
		# The lines of the init pass (the read lines and the other lines of their vouchers); see `_narrow_lines`.
		_init_lines = None

		def voucher_limit(self):
			"""The reader never refuses `too_large`; its own line caps end in a `too_many_lines` fallback."""
			return None

		@contextmanager
		def _timed(self, name):
			started = perf_counter()
			try:
				yield
			finally:
				timings = self.reader_stats["timings_ms"]
				timings[name] = round(timings.get(name, 0) + (perf_counter() - started) * 1000, 1)

		def get_data(self):
			"""The read, with its stats published to the caller's dict whatever the outcome.

			The read fills a fresh dict that is then merged into the caller's, so keys the caller put in its
			own dict are kept (only the three stats keys are replaced).
			"""
			self.reader_stats = {"counts": {}, "timings_ms": {}, "control": {}}
			self._line_classes = {}
			self._read_query = None
			self._init_lines = None
			try:
				self._read_data()
			except ReaderFallback as exc:
				note_fallback(self.reader_stats, exc)
				raise
			finally:
				if self.stats_sink is not None:
					self.stats_sink.update(self.reader_stats)

		def _read_data(self):
			"""ERPNext's `get_data` sequence with the full-history ledger fetch replaced (pinned upstream)."""
			# Once per read: the pin check hashes about 25 sources, and eligibility is told its result.
			with self._timed("pins"):
				require_supported_source()
			with self._timed("eligibility"):
				reasons = reader_ineligibility(
					self.filters.company, self.account_type, self.columns, audited_sources_ok=True
				)
			if reasons:
				raise ReaderFallback("ineligible", tuple(reasons))
			self.get_sales_invoices_or_customers_based_on_sales_person()
			if self.sales_person_records is not None:
				raise ReaderFallback("sales_person_scope")
			self.get_invoices_based_on_sales_partner()
			self.get_future_payments()
			if not self.filters.party_type or self.filters.party_type in ["Customer", "Supplier"]:
				self.get_return_entries()
			self.get_exchange_rate_revaluations()
			self.prepare_ple_query()
			self.data = []
			self.voucher_balance = OrderedDict()
			with _reader_errors():
				self.invoice_type, self.account_field = SIDES[self.account_type]
				self.side_accounts = self._side_accounts()
				if frappe.db.db_type == "mariadb":
					require_ledger_indexes()
				with self._timed("control"):
					control = ledger_control(
						self.filters.company, self.account_type, self.filters.report_date, self.side_accounts
					)
				self._check_line_caps(control)
				with self._timed("discovery"):
					discovery = self._discover(control)
				with self._timed("lines"):
					lines = self._narrow_lines(discovery)
				with self._timed("details"):
					self.invoice_details = self._narrow_invoice_details(lines)
			# ERPNext's own accumulation, as fetch_ple_in_buffered_cursor runs it: two passes.
			with self._timed("accumulate"):
				# Init sees the lines of the read vouchers the read skipped too, so keys are first met in
				# today's order (row ids); update sees only the lines read.
				for ple in lines if self._init_lines is None else self._init_lines:
					self.init_voucher_balance(ple)
				for ple in lines:
					self.update_voucher_balance(ple)
			with _reader_errors():
				self._check_control_total(control)
			self.build_delivery_note_map()
			with self._timed("build"):
				self.build_data()

		def _check_line_caps(self, control: LedgerControl) -> None:
			"""`too_many_lines` when the ledger is too big to hold in memory: the generic types are read whole."""
			generic = sum(
				count for (_, against), count in control.pairs.items() if against != self.invoice_type
			)
			total = sum(control.pairs.values())
			self.reader_stats["counts"].update(ledger_lines=total, generic_lines=generic)
			if generic > GENERIC_LINE_CAP or total > TOTAL_LINE_CAP:
				raise ReaderFallback("too_many_lines")

		def _side_accounts(self):
			"""The report's own account list for the side (add_common_filters)."""
			accounts = [
				d.name
				for d in frappe.get_all(
					"Account", filters={"account_type": self.account_type, "company": self.filters.company}
				)
			]
			if not accounts:
				# Without accounts the report would not filter by account at all: not a read to narrow.
				raise ReaderFallback("reader_error")
			return accounts

		# ------------------------------------------------------------------ discovery
		def _discover(self, control: LedgerControl) -> Discovery:
			"""Which invoices' ledger lines to read. Header fields find them; no figure comes from a header."""
			doctype, anchor = self.invoice_type, self.filters.report_date
			invoice = frappe.qb.DocType(doctype)
			ple = self.ple
			open_names = (
				frappe.qb.from_(invoice)
				.select(invoice.name)
				.where(
					(invoice.company == self.filters.company)
					& (invoice.docstatus == 1)
					& (invoice.posting_date <= anchor)
					& invoice[self.account_field].isin(self.side_accounts)
					& (invoice.outstanding_amount != 0)
				)
			)
			# The header counts lines dated after the anchor, the report does not: a header can be 0 while
			# the invoice is open as of the anchor. Lines after the anchor point at those invoices.
			future_names = (
				frappe.qb.from_(ple)
				.select(ple.against_voucher_no)
				.distinct()
				.where(
					Criterion.all(
						_ledger_predicate(ple, self.filters.company, self.account_type, self.side_accounts)
					)
					& (ple.posting_date > anchor)
					& (ple.against_voucher_type == doctype)
				)
			)
			# No report row exists for these invoices, so a payment's lines against them fall back to the
			# payment's own row.
			orphan_names = (
				frappe.qb.from_(invoice)
				.select(invoice.name)
				.where(
					invoice[self.account_field].isin(self.side_accounts)
					& (
						(invoice.docstatus == 2)
						| ((invoice.docstatus == 1) & (invoice.posting_date > anchor))
					)
				)
			)
			with self._timed("open"):
				opened = frozenset(name for (name,) in open_names.run())
			with self._timed("future"):
				opened |= frozenset(name for (name,) in future_names.run() if name)
			with self._timed("orphans"):
				orphans = frozenset(name for (name,) in orphan_names.run())
			with self._timed("due_window"):
				due_in_window = self._due_in_window()
			with self._timed("fx"):
				fx = self._fx_residues()
			with self._timed("multi"):
				multi = self._multi_account()
			base = opened | orphans | due_in_window | fx | multi
			discovery = Discovery(
				open=opened,
				due_in_window=due_in_window,
				fx=fx,
				multi=multi,
				orphans=orphans,
				returns=frozenset(),
				generic_types=tuple(
					sorted(
						{against for (_, against) in control.pairs if against != doctype},
						key=lambda against: (against is not None, against or ""),
					)
				),
			)
			# Last, over the final union of every other set.
			closed = self._close_returns(discovery.invoices())
			discovery = replace(discovery, returns=frozenset(closed - base))
			counts = self.reader_stats["counts"]
			counts.update(
				open=len(discovery.open),
				due_in_window=len(discovery.due_in_window),
				fx=len(discovery.fx),
				multi=len(discovery.multi),
				orphans=len(discovery.orphans),
				returns=len(discovery.returns),
				generic_types=len(discovery.generic_types),
			)
			return discovery

		def _due_in_window(self) -> frozenset[str]:
			"""Invoices with a payment term, or a header due date, inside the window: today's settled candidates.

			`_settled_in_window` selects a closed invoice by exactly these two tests, whatever its settlement
			date, so every candidate is found by construction (both window ends inclusive, as BETWEEN). The
			same company, submitted and posted-by-the-anchor filters as the invoice details the rule reads
			(`get_invoice_details`); no account filter and no `is_return` filter, which the rule does not have
			either: an invoice whose header account differs from its ledger account is still found.
			Two statements, terms and header dates: the snapshot read allows only statements that start with
			SELECT, which a UNION of parenthesised selects does not.
			"""
			doctype, anchor = self.invoice_type, self.filters.report_date
			start, end = self.window_start, self.window_end
			invoice, schedule = frappe.qb.DocType(doctype), frappe.qb.DocType("Payment Schedule")
			scope = (
				(invoice.company == self.filters.company)
				& (invoice.docstatus == 1)
				& (invoice.posting_date <= anchor)
			)
			by_term = (
				frappe.qb.from_(invoice)
				.join(schedule)
				.on(schedule.parent == invoice.name)
				.select(invoice.name)
				.where(scope & (schedule.parenttype == doctype) & schedule.due_date.between(start, end))
			)
			by_header = (
				frappe.qb.from_(invoice)
				.select(invoice.name)
				.where(scope & invoice.due_date.between(start, end))
			)
			return frozenset(name for query in (by_term, by_header) for (name,) in query.run())

		def _fx_residues(self) -> frozenset[str]:
			"""Invoices with a non-zero residual, in company or in account currency, on a foreign side account.

			A superset of today's excluded FX keys (`InvoiceReconciler.excluded_fx` needs a non-zero company
			residual on a foreign account) and of the keys behind today's `currency_reconciliation_unavailable`
			(a non-zero company OR account-currency residual): their lines are read, and today's `build_data`
			decides. The account-currency sum matters: a key can net to 0 in company currency while 50 of 100
			USD is unpaid, and the invoice header (which the discovery cannot trust) may say 0. Grouped by
			the whole key (invoice, account, party), so offsetting keys of one invoice cannot hide a residual.
			A native account is not searched: its `amount_in_account_currency` equals `amount` on every line
			ERPNext writes, so the company-currency checks (header, control total) already cover it.

			The group key is the line's own `against_voucher_no`, not the key ERPNext folds it onto: a line
			against a return (one that does not update its own outstanding) is summed into the ORIGINAL's key
			by `get_voucher_balance`. That is still a superset: a non-zero folded sum has a non-zero group
			among the lines it adds up (the original's own group or the return's), so at least one of the two
			names is found here, and `_close_returns` (and, behind it, the re-key probe) reads the other.
			A residue that exists only through a return is covered by a test (and cancelling ones by another).
			"""
			doctype, anchor = self.invoice_type, self.filters.report_date
			currency = frappe.get_cached_value("Company", self.filters.company, "default_currency")
			account = frappe.qb.DocType("Account")
			foreign = [
				name
				for (name,) in frappe.qb.from_(account)
				.select(account.name)
				.where(
					account.name.isin(self.side_accounts)
					& ((account.account_currency != currency) | account.account_currency.isnull())
				)
				.run()
			]
			if not foreign:
				return frozenset()
			ple = self.ple
			names = (
				frappe.qb.from_(ple)
				.select(ple.against_voucher_no)
				.where(
					Criterion.all(_ledger_predicate(ple, self.filters.company, self.account_type, foreign))
					& (ple.posting_date <= anchor)
					& (ple.against_voucher_type == doctype)
				)
				.groupby(ple.against_voucher_no, ple.account, ple.party)
				.having((Sum(ple.amount) != 0) | (Sum(ple.amount_in_account_currency) != 0))
			)
			return frozenset(name for (name,) in names.run() if name)

		def _multi_account(self) -> frozenset[str]:
			"""Invoices with ledger lines on more than one (account, party) key, over the full history.

			The one aggregate per side that keeps today's `multiple_invoice_accounts` refusal exact: a closed
			invoice with offsetting lines on two accounts is refused today, so its lines are read here and
			the inherited Counter refuses at today's point in `build_data`. A superset (every invoice with
			lines on two keys, own lines or not).

			The WHERE is the report's own ledger predicate (`_ledger_predicate`, shared with the control total
			and the other reads) plus the date and the invoice type. `COUNT(DISTINCT account, party)` has no
			query-builder form, so it is a literal. It skips a row whose party is NULL, while the report keys
			such a line on (account, NULL), so the NULL-party accounts are counted by a second distinct count.

			`FORCE INDEX (account)`: left to itself the optimizer intersects the `against_voucher_type` and
			`company` indexes, which hold the same value on most rows; at guard size the range over the side's
			own accounts is up to 3 times faster. It is also the set the report's own ledger query reads, so
			it does not grow with the other companies or accounts of the site. A lookup by the one type this
			statement reads is faster still on a one-company ledger (0.3 s) but reads the invoice-type lines
			of every company. The index is the one ERPNext declares on the ledger's `account` field.
			"""
			if frappe.db.db_type != "mariadb":
				raise ReaderFallback("reader_error")
			ple = self.ple
			distinct_keys = Function("COUNT", LiteralValue("DISTINCT `account`, `party`"))
			null_party_accounts = Function(
				"COUNT", LiteralValue("DISTINCT CASE WHEN `party` IS NULL THEN `account` END")
			)
			query = (
				frappe.qb.from_(ple)
				.select(ple.against_voucher_no)
				.where(
					Criterion.all(
						_ledger_predicate(ple, self.filters.company, self.account_type, self.side_accounts)
					)
					& (ple.posting_date <= self.filters.report_date)
					& (ple.against_voucher_type == self.invoice_type)
				)
				.groupby(ple.against_voucher_no)
				.having(distinct_keys + null_party_accounts > 1)
			)
			if _index_exists(LEDGER_TABLE, "account"):
				query = query.force_index("account")
			with _hint_errors():
				rows = query.run()
			return frozenset(name for (name,) in rows if name)

		def _close_returns(self, names):
			"""`names` plus every return and original that folds onto a name already in it, in both directions.

			ERPNext remaps a line against a return (one that does not update its own outstanding) onto the
			original invoice's key, so the two are read together or not at all: a return in the set needs its
			original, and an original in the set needs its returns.
			"""
			folds = {ret: original for ret, original in self.return_entries.items() if original}
			closed = set(names)
			changed = True
			while changed:
				changed = False
				for ret, original in folds.items():
					if (ret in closed) != (original in closed):
						closed.update((ret, original))
						changed = True
			return closed

		# ------------------------------------------------------------------ ledger lines
		def _read(self, criterion):
			"""The report's own ledger query plus exactly one more criterion."""
			with self._timed("read"):
				query = self._unordered_query().where(criterion)
				fields = tuple(term.alias or term.name for term in query._selects)
				row_type = self._line_classes.setdefault(fields, line_class(fields))
				return [row_type._make(row) for row in query.run()]

		def _unordered_query(self):
			"""The report's ledger query without its ORDER BY.

			The lines of all reads are merged and sorted once in `sort_like_the_report`, which is a total order
			on (date, party, name) under the database collation, so each statement's own sort is wasted work
			(half the time of the large type reads). Everything else is the report's query.
			"""
			if self._read_query is None:
				query = copy.copy(self.ple_query)
				query._orderbys = []
				self._read_query = query
			return self._read_query

		def _narrow_lines(self, discovery: Discovery) -> list:
			"""Every ledger line that lands on a key the report would have under the full ledger.

			Lines against the invoices, every line against another type, then the re-key probe to a fixed
			point. Deduplicated by line name; merged in the report's order.
			"""
			doctype, ple = self.invoice_type, self.ple
			lines, invoices, read_invoices = {}, set(discovery.invoices()), set()

			def add(rows):
				fresh = [row for row in rows if row.name not in lines]
				lines.update((row.name, row) for row in fresh)
				return fresh

			def read_against(names):
				read_invoices.update(names)
				for chunk in _chunks(sorted(names)):
					add(
						self._read((ple.against_voucher_type == doctype) & ple.against_voucher_no.isin(chunk))
					)

			read_against(set(invoices))
			for against in discovery.generic_types:
				add(
					self._read(
						ple.against_voucher_type.isnull()
						if against is None
						else ple.against_voucher_type == against
					)
				)
			probed, rounds = set(), 0
			while True:
				pending = self._close_returns(invoices) - read_invoices
				if pending:
					invoices |= pending
					read_against(pending)
				targets = self._unresolved_targets(lines.values()) - probed
				if not pending and not targets:
					break
				if targets:
					rounds += 1
					if rounds > MAX_REKEY_ROUNDS:
						raise ReaderFallback("rekey_unresolved")
					probed |= targets
					found = self._read_keyed(targets, add)
					invoices |= {
						name
						for row in found
						for voucher_type, name in (
							(row.voucher_type, row.voucher_no),
							(row.against_voucher_type, row.against_voucher_no),
						)
						if voucher_type == doctype
					}
			ordered = sort_like_the_report(list(lines.values()))
			init_only = self._other_lines_of_read_vouchers(ordered)
			if len(ordered) + len(init_only) > TOTAL_LINE_CAP:
				raise ReaderFallback("too_many_lines")
			self._init_lines = sort_like_the_report(ordered + init_only) if init_only else ordered
			counts = self.reader_stats["counts"]
			counts.update(
				lines=len(ordered),
				init_only_lines=len(init_only),
				rekey_probed=len(probed),
				rekey_rounds=rounds,
			)
			# Exact sum of the lines read, for the control check (frappe turns amounts into floats).
			self._lines_total = sum((Decimal(str(row.amount or 0)) for row in ordered), Decimal(0))
			return ordered

		def _other_lines_of_read_vouchers(self, lines: list) -> list:
			"""The ledger lines the read did not take, of the vouchers whose key a read line creates.

			Row ids follow the order in which the report's `init_voucher_balance` first meets each key, and
			it keys a line by its OWN voucher. A voucher whose first line pays an invoice the read skipped
			(closed, outside the window) would otherwise be met first at a later line and move among the keys
			that tie. These lines feed the INIT pass only (`_init_lines`, used by `_read_data`): never the update pass, the control
			total or any row, so no amount can change. Read by `voucher_no`, as the re-key probe does, and
			only lines of a key that already has a read line (the key exists in the read as well).
			"""
			read = {line.name for line in lines}
			owned = {(line.account, line.voucher_type, line.voucher_no, line.party) for line in lines}
			by_type = {}
			for _, voucher_type, voucher_no, _ in owned:
				by_type.setdefault(voucher_type, set()).add(voucher_no)
			ple, other = self.ple, {}
			for voucher_type, names in sorted(by_type.items()):
				for chunk in _chunks(sorted(names)):
					for row in self._read((ple.voucher_type == voucher_type) & ple.voucher_no.isin(chunk)):
						key = (row.account, row.voucher_type, row.voucher_no, row.party)
						# `IN` matches letter case loosely; the key match is exact.
						if row.name not in read and key in owned:
							other[row.name] = row
			return list(other.values())

		def _read_keyed(self, targets, add):
			"""Read the lines of every key in `targets` (account, voucher_type, voucher_no, party) by `voucher_no`.

			Only lines whose own key is a target are added: the key then exists in the read lines, as it does
			in the full ledger.
			"""
			found, ple = [], self.ple
			for chunk in _chunks(sorted({voucher_no for (_, _, voucher_no, _) in targets})):
				rows = self._read(ple.voucher_no.isin(chunk))
				# `IN` matches letter case loosely; the key match is exact.
				found += add(
					[
						row
						for row in rows
						if (row.account, row.voucher_type, row.voucher_no, row.party) in targets
					]
				)
			return found

		def _target_key(self, line):
			"""The key `get_voucher_balance` tries first for a line (return remap included)."""
			key = (line.account, line.against_voucher_type, line.against_voucher_no, line.party)
			if line.against_voucher_type in ("Sales Invoice", "Purchase Invoice"):
				original = self.return_entries.get(line.against_voucher_no)
				if original:
					key = (line.account, line.against_voucher_type, original, line.party)
			return key

		def _creates_row(self, line):
			"""Types for which `get_voucher_balance` builds the missing key row instead of falling back.

			The last two arms are dead under the fixed filters of `sources._engine_filters`
			(`for_revaluation_journals` is 0, `handle_employee_advances` is unset); they mirror upstream so a
			filter change cannot make the probe skip a key.
			"""
			against = line.against_voucher_type
			return (
				against == "Payroll Entry"
				or (against == "Employee Advance" and self.filters.handle_employee_advances)
				or (against == "Exchange Rate Revaluation" and self.filters.for_revaluation_journals)
			)

		def _unresolved_targets(self, lines):
			"""Keys a read line aims at that no read line owns: the line would fall back to its own key.

			In the full ledger such a key may exist. Each one is returned for the probe to look up.
			"""
			lines = list(lines)
			owned = {(line.account, line.voucher_type, line.voucher_no, line.party) for line in lines}
			unresolved = set()
			for line in lines:
				key = self._target_key(line)
				# A NULL against number keys to the line itself: no voucher owns it, and `voucher_no` is
				# never NULL, so there is nothing to look up (and None cannot be sorted with names).
				if key[2] is not None and key not in owned and not self._creates_row(line):
					unresolved.add(key)
			return unresolved

		def _narrow_invoice_details(self, lines) -> frappe._dict:
			"""`get_invoice_details`, restricted to the invoices and journals the lines name.

			A plain query instead of `get_list`: equal for a user the eligibility gate lets through (no match
			conditions, no masked field), and it skips the permission and field-parsing work of a framework
			read per chunk. Same fields, same filters and the same `setdefault` order as upstream (pinned).
			"""
			invoice_doctype, anchor = self.invoice_type, self.filters.report_date
			names = {invoice_doctype: set(), JOURNAL_ENTRY: set()}
			for line in lines:
				if line.voucher_type in names:
					names[line.voucher_type].add(line.voucher_no)
				if line.against_voucher_type in names:
					names[line.against_voucher_type].add(line.against_voucher_no)
			fields = {
				"Sales Invoice": ["name", "due_date", "po_no", "sales_partner", "is_return"],
				"Purchase Invoice": ["name", "due_date", "bill_no", "bill_date", "is_return"],
				JOURNAL_ENTRY: ["name", "due_date", "bill_no", "bill_date"],
			}
			details = frappe._dict()
			for doctype in (invoice_doctype, JOURNAL_ENTRY):
				table = frappe.qb.DocType(doctype)
				query = frappe.qb.from_(table)
				if frappe.db.db_type == "mariadb" and _index_exists(f"tab{doctype}", "PRIMARY"):
					# With a long `name IN` list next to the posting-date filter the optimizer reads the
					# journal table through the posting-date index (4.5 s for 38k names against 0.8 s).
					query = query.force_index("PRIMARY")
				query = query.select(*(table[field] for field in fields[doctype]))
				if doctype == JOURNAL_ENTRY:
					# Invoices booked via journal entries count only with a bill number. CHAR_LENGTH is the
					# exact test of a non-empty string: `<> ''` ignores trailing spaces (PAD SPACE).
					query = query.where(Function("CHAR_LENGTH", table.bill_no) > 0)
				for chunk in _chunks(sorted(names[doctype]), DETAIL_CHUNK):
					with _hint_errors():
						rows = query.where(
							(table.posting_date <= anchor)
							& (table.company == self.filters.company)
							& (table.docstatus == 1)
							& table.name.isin(chunk)
						).run(as_dict=True)
					for row in rows:
						details.setdefault(row.name, row)
			return details

		def _check_control_total(self, control: LedgerControl) -> None:
			"""Every ledger line the report would read is in a key or provably sums to zero."""
			balances = sum(
				(
					Decimal(str(row.invoiced)) - Decimal(str(row.paid)) - Decimal(str(row.credit_note))
					for row in self.voucher_balance.values()
				),
				Decimal(0),
			)
			precision = self.currency_precision
			passed = control_total_matches(balances, control.total, precision) and control_total_matches(
				self._lines_total, control.total, precision
			)
			self.reader_stats["control"] = {
				"ledger": str(control.total),
				"balances": str(balances),
				"lines": str(self._lines_total),
				"passed": passed,
			}
			if not passed:
				raise ReaderFallback("control_total")

	return BuiltinReport


def _engine_factory(stats: dict | None):
	"""The engine class, or a callable building it with the caller's stats dict; `_run_engine` calls either."""
	engine_class = _builtin_class()
	if stats is None:
		return engine_class

	def build(filters, window_start, window_end):
		engine = engine_class(filters, window_start, window_end)
		engine.stats_sink = stats
		return engine

	return build


def read_obligations(
	company: str,
	account_type: str,
	as_of: str,
	window_start: str,
	window_end: str,
	*,
	stats: dict | None = None,
) -> dict:
	"""Today's `sources.read_obligations` result without the full-history scan, or `ReaderFallback`.

	Same inputs, same gates in the same order, same refusals (`CalendarSourceUnavailable` with today's codes),
	same result assembly. No size probe and no ledger count guard: the reader's own caps end in a
	`too_many_lines` fallback. `ReaderFallback` is not a refusal; the caller then runs today's read.

	Error mapping: an AttributeError, KeyError or TypeError raised in ERPNext's or today's shared code is the
	refusal today's read gives (`CalendarSourceUnavailable`); one raised in code only the reader runs (and
	a Decimal error there) is `ReaderFallback("reader_error")` (inside `get_data`), so a reader bug never
	looks like a refusal. Other database errors (lock wait, deadlock) propagate unmapped: the caller
	must classify them.

	`stats`, when given, is filled with timings (ms), counts and control totals, no names, also when the read
	falls back after the control total was measured. A fallback sets `stats["fallback"]` to its reason (and
	`stats["fallback_detail"]` to its fixed detail strings), whether it came before or after the engine ran.
	"""
	try:
		report, start, end, anchor, company_values = sources._require_inputs(
			company, account_type, as_of, window_start, window_end
		)
		validate_obligation_books(company, account_type, anchor)
		engine = sources._run_engine(
			_engine_factory(stats), sources._engine_filters(company, anchor), account_type, start, end
		)
		return sources._assemble(engine, report, account_type, start, end, company_values)
	except ReaderFallback as exc:
		if stats is not None:
			note_fallback(stats, exc)
		raise
	except (AttributeError, KeyError, TypeError) as exc:
		raise CalendarSourceUnavailable() from exc


def note_fallback(stats: dict, fallback: ReaderFallback) -> None:
	"""Record why a read fell back: the reason and its fixed detail strings, never a data value."""
	stats["fallback"] = fallback.reason
	stats["fallback_detail"] = list(fallback.detail)
