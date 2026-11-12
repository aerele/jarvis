"""Day and bucket detail replies: row filters, sorting, paging, totals.

The API and the golden dataset both build their replies as `render(page(...))`, so they cannot drift
apart. A `Page` holds the stored rows of one reply, so the API can check them before `render` makes
them public.

A request is validated into a `Query` before any row is loaded. Stored side rows are in default order
and `index_side` (run at publish) lists, per day and per bucket, the row positions, party summary and total,
so a read slices a list instead of filtering and sorting thousands of rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal

import frappe

from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.projection import listed_row

SIDES = ("Payable", "Receivable")
SORT_KEYS = ("remaining", "due_date", "party")
ORDERS = ("asc", "desc")
DEFAULT_SORT, DEFAULT_ORDER = "remaining", "desc"
PAYABLE_ONLY_BUCKETS = ("today", "immediate", "now")
RECONCILIATION_BUCKETS = ("overdue", "undated", "adjustments", "outside_horizon")
BUCKETS = (*RECONCILIATION_BUCKETS, *PAYABLE_ONLY_BUCKETS, "window")


@dataclass(frozen=True)
class Page:
	"""One reply before it is public: stored rows (with their private proofs) and the reply's numbers."""

	rows: list
	paging: dict
	party_slice: list | None
	party_paging: dict | None
	total_remaining: str
	generated_at: str
	precision: int

	@property
	def party_keys(self) -> set:
		"""(party_type, party) of every party the reply lists; empty without the party summary."""
		return {(party["party_type"], party["party"]) for party in self.party_slice or []}


@dataclass(frozen=True)
class Query:
	"""A validated day or bucket request. `field` names its entry in the stored index."""

	kind: str
	key: str
	side: str
	page: int
	page_size: int
	party_page: int
	sort: str
	order: str
	parties: bool

	@property
	def field(self) -> str:
		return f"{self.kind}:{self.key}"

	@property
	def default_order(self) -> bool:
		return (self.sort, self.order) == (DEFAULT_SORT, DEFAULT_ORDER)


def day_detail(status, result, **request):
	"""One due date, one side; settled-zero terms are listed with their day."""
	return render(day_page(status, result, **request))


def bucket_detail(status, result, **request):
	"""A reconciliation, due-now or window bucket of one side."""
	return render(bucket_page(status, result, **request))


def render(page):
	"""The public reply of a page: only here do the rows lose their private keys."""
	party_total = None if page.party_paging is None else page.party_paging["total"]
	party_number = None if page.party_paging is None else page.party_paging["page"]
	reply = {
		**page.paging,
		"rows": [public_row(row, page.precision) for row in page.rows],
		"parties": [] if page.party_slice is None else page.party_slice,
		"party_total": party_total,
		"party_page": party_number,
		"total_remaining": page.total_remaining,
		"generated_at": page.generated_at,
	}
	return {key: reply[key] for key in _REPLY_KEYS}


def day_query(status, *, date, side, page, page_size, party_page, sort, order, parties=True):
	"""Validate a day request; nothing is read."""
	sort, order = parse_sort(sort, order)
	selected = check_date(date, status["scope"])
	return _query("day", selected, side, (page, page_size, party_page), (sort, order), parties)


def bucket_query(status, *, bucket, side, page, page_size, party_page, sort, order, parties=True):
	"""Validate a bucket request; nothing is read."""
	sort, order = parse_sort(sort, order)
	if bucket not in BUCKETS:
		raise frappe.ValidationError("Choose a reconciliation bucket.")
	if bucket in PAYABLE_ONLY_BUCKETS and side != "Payable":
		raise frappe.ValidationError("This bucket lists payables only.")
	return _query("bucket", bucket, side, (page, page_size, party_page), (sort, order), parties)


def day_page(status, result, **request):
	return page_of(day_query(status, **request), result)


def bucket_page(status, result, **request):
	return page_of(bucket_query(status, **request), result)


def page_of(query, result):
	"""The page of a validated query from the stored side rows and its index entry (see `index_entry`)."""
	source = _source(result, query.side)
	entry = source["index"].get(query.field) or _empty_entry(query, result)
	invoices = _invoices(query, source["rows"], entry["rows"])
	party_slice = party_paging = None
	if query.parties:
		slice_ = page_rows(entry["parties"], query.party_page, query.page_size)
		party_slice = slice_["rows"]
		party_paging = {"total": len(entry["parties"]), "page": slice_["page"]}
	return Page(
		rows=invoices.pop("rows"),
		paging=invoices,
		party_slice=party_slice,
		party_paging=party_paging,
		total_remaining=entry["total"],
		generated_at=result["generated_at"],
		precision=result["projection"]["precision"],
	)


def index_side(rows, result, side, scope):
	"""The stored index of one side: `{"day:<iso>" | "bucket:<name>": entry}`.

	`rows` must be in default order, so each entry's positions are ascending and a default-sort read needs no
	sort. Every bucket of the side gets an entry, so no bucket read depends on a missing field.
	"""
	days = {}
	for position, row in enumerate(rows):
		if row["due_date"] is not None and _listed(row):
			days.setdefault(row["due_date"], []).append(position)
	index = {
		f"day:{due}": _entry(rows, positions, _total([rows[i] for i in positions], result))
		for due, positions in days.items()
	}
	for bucket in BUCKETS:
		if bucket in PAYABLE_ONLY_BUCKETS and side != "Payable":
			continue
		belongs = bucket_filter(bucket, scope)
		positions = [i for i, row in enumerate(rows) if belongs(row)]
		total = _bucket_total(bucket, [rows[i] for i in positions], result, side)
		index[f"bucket:{bucket}"] = _entry(rows, positions, total)
	return index


def bucket_filter(bucket, scope):
	"""Predicate for the rows of one bucket."""
	if bucket in PAYABLE_ONLY_BUCKETS:
		return _due_now_filter(bucket, scope["anchor"])
	if bucket == "window":
		return lambda row: (
			row["due_date"] is not None
			and scope["window_start"] <= row["due_date"] <= scope["window_end"]
			and _listed(row)
		)
	return lambda row: _reconciliation_bucket(row, scope) == bucket


def _reconciliation_bucket(row, scope):
	"""The reconciliation bucket of one row; None for a settled-zero row."""
	amount, due = Decimal(row["remaining"]), row["due_date"]
	if amount < 0:
		return "adjustments"
	if amount == 0:
		return None
	if due is None:
		return "undated"
	if due < scope["anchor"]:
		return "overdue"
	if due > scope["window_end"]:
		return "outside_horizon"
	return "current_future"


def parse_sort(sort, order):
	"""(sort, order) with defaults for omitted values; anything else is invalid."""
	sort = DEFAULT_SORT if sort is None else sort
	order = DEFAULT_ORDER if order is None else order
	if sort not in SORT_KEYS or order not in ORDERS:
		raise frappe.ValidationError("Choose a sort key and order.")
	return sort, order


def sort_rows(rows, sort=None, order=None):
	"""Dated rows first, undated rows last in both directions; ties go to `id` ascending."""
	sort, order = parse_sort(sort, order)
	key = _SORT_KEY[sort]
	by_id = sorted(rows, key=lambda row: row["id"])
	dated = [row for row in by_id if row["due_date"] is not None]
	undated = [row for row in by_id if row["due_date"] is None]
	# sorted() keeps equal keys in id order, also with reverse=True.
	return [row for group in (dated, undated) for row in sorted(group, key=key, reverse=order == "desc")]


def public_row(row, precision):
	"""A row without private keys, with settled = original - remaining where that is meaningful."""
	row = {key: value for key, value in row.items() if not key.startswith("_")}
	row["settled"] = _settled(row, precision)
	return row


def page_rows(rows, page, size):
	page, size = _page(page, size)
	offset = (page - 1) * size
	return {
		"rows": rows[offset : offset + size],
		"total": len(rows),
		"page": page,
		"page_size": size,
		"has_more": offset + size < len(rows),
	}


def check_date(value, scope):
	"""The ISO date, if it is a real date inside the snapshot window."""
	try:
		if not isinstance(value, str) or len(value) != 10 or Date.fromisoformat(value).isoformat() != value:
			raise ValueError
	except ValueError as exc:
		raise frappe.ValidationError("Choose a calendar date.") from exc
	if not scope["window_start"] <= value <= scope["window_end"]:
		raise frappe.ValidationError("Choose a date in the snapshot window.")
	return value


def check_side(result, side):
	"""Raise for an unknown, forbidden or unavailable side; needs no rows, only `unavailable`."""
	if side not in SIDES:
		raise frappe.ValidationError("Choose payables or receivables.")
	unavailable = result.get("unavailable", {})
	# A result that carries its sources also reports a side without a source.
	if side in unavailable or ("sources" in result and result["sources"].get(side) is None):
		code = unavailable.get(side, "source_contract_unavailable")
		if code == "forbidden":
			raise frappe.PermissionError("Report access is unavailable.")
		raise CalendarSourceUnavailable(code)


def _due_now_filter(bucket, anchor):
	"""Payable rows with positive remaining that make up one due-now KPI."""
	due_rules = {
		"today": lambda due: due == anchor,
		"immediate": lambda due: due is None or due < anchor,
		"now": lambda due: due is None or due <= anchor,
	}
	rule = due_rules[bucket]
	return lambda row: Decimal(row["remaining"]) > 0 and rule(row["due_date"])


def _listed(row):
	return listed_row(Decimal(row["remaining"]), Decimal(row["original"]), row["kind"])


def _settled(row, precision):
	remaining, original = Decimal(row["remaining"]), Decimal(row["original"])
	if row["kind"] == "adjustment" or original <= 0 or not 0 <= remaining <= original:
		return None
	return str((original - remaining).quantize(Decimal(1).scaleb(-precision)))


def _bucket_total(bucket, rows, result, side):
	"""The KPI the bucket must equal: reconciliation buckets are signed, the others positive-only."""
	if bucket == "window":
		return _window_total(result, side)
	return _total(rows, result, signed=bucket in RECONCILIATION_BUCKETS)


def _total(rows, result, signed=False):
	amounts = [Decimal(row["remaining"]) for row in rows]
	total = sum(amounts if signed else [a for a in amounts if a > 0], Decimal(0))
	return str(total.quantize(Decimal(1).scaleb(-result["projection"]["precision"])))


def _window_total(result, side):
	"""The month total of the side; null while the totals are incomplete or missing."""
	totals = result["projection"].get("month_totals")
	return totals[side.lower()] if totals else None


def _query(kind, key, side, numbers, ordering, parties):
	page, page_size, party_page = numbers
	number, size = _page(page, page_size)
	# The party page is validated even when the summary is off: an invalid request never half-passes.
	party_number = _page(party_page, page_size)[0]
	return Query(kind, key, side, number, size, party_number, *ordering, parties)


def _source(result, side):
	"""The stored side source; an unavailable side raises its own error code."""
	check_side(result, side)
	return result["sources"][side]


def _entry(rows, positions, total):
	return {"rows": positions, "parties": _party_rows([rows[i] for i in positions]), "total": total}


def _empty_entry(query, result):
	"""No stored entry means no row: a day without rows, never a different total than the old path gave."""
	if query.kind == "day":
		total = _total([], result)
	else:
		total = _bucket_total(query.key, [], result, query.side)
	return {"rows": [], "parties": [], "total": total}


def _invoices(query, rows, positions):
	"""One page of the entry's rows; only a non-default sort orders (just the selection)."""
	if query.default_order:
		paged = page_rows(positions, query.page, query.page_size)
		paged["rows"] = [rows[i] for i in paged["rows"]]
		return paged
	selection = [rows[i] for i in positions]
	return page_rows(sort_rows(selection, query.sort, query.order), query.page, query.page_size)


def _party_rows(rows):
	parties = {}
	for row in rows:
		key = (row["party_type"], row["party"])
		party = parties.setdefault(
			key,
			{
				"party_type": key[0],
				"party": key[1],
				"party_name": row["party_name"],
				"remaining": Decimal(0),
				"terms": 0,
				"invoices": set(),
			},
		)
		party["remaining"] += Decimal(row["remaining"])
		party["terms"] += 1
		party["invoices"].add((row["voucher_type"], row["voucher_no"]))
	party_rows = [
		{**party, "remaining": str(party["remaining"]), "invoices": len(party["invoices"])}
		for party in parties.values()
	]
	party_rows.sort(key=lambda r: (-Decimal(r["remaining"]), r["party_type"], r["party"]))
	return party_rows


def _page(value, size):
	value, size = _page_number(value), _page_number(size)
	if not 1 <= value <= 100000 or not 1 <= size <= 100:
		raise frappe.ValidationError("Choose a page size up to 100.")
	return value, size


def _page_number(number):
	if (
		isinstance(number, bool)
		or not isinstance(number, (int, str))
		or not str(number).isascii()
		or not str(number).isdigit()
	):
		raise frappe.ValidationError("Choose a page.")
	return int(number)


_REPLY_KEYS = (
	"rows",
	"total",
	"page",
	"page_size",
	"has_more",
	"parties",
	"party_total",
	"party_page",
	"total_remaining",
	"generated_at",
)
_SORT_KEY = {
	"remaining": lambda row: Decimal(row["remaining"]),
	"due_date": lambda row: row["due_date"] or "",
	"party": lambda row: (row.get("party_name") or row["party"]).casefold(),
}
