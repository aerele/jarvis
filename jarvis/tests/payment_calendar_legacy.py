"""Test-only copy of the rev-4 day and bucket reply path (never imported by application code).

It filters and sorts the side rows of the ORIGINAL assembled result, in source order, on every call.

What is independent of `detail`: the sort (its own key table and tie rule, so the stored order and the index
cannot change both sides of the comparison) and the day selection (`due_date` equality plus `listed_row`).
What is shared on purpose, so this oracle does NOT prove it: `detail.bucket_filter` (bucket membership),
`detail._party_rows` (party summary), `detail._total` and `detail._bucket_total` (totals),
`detail.page_rows` (paging) and `Page` / `render` (reply shape). Those are pinned by the frozen literal
replies of `test_payment_calendar_frozen_reply.py`, computed from the base commit's own code.
"""

from decimal import Decimal

from jarvis.payment_calendar import detail
from jarvis.payment_calendar.projection import listed_row

SORT_KEYS = {
	"remaining": lambda row: Decimal(row["remaining"]),
	"due_date": lambda row: row["due_date"] or "",
	"party": lambda row: (row.get("party_name") or row["party"]).casefold(),
}


def _listed(row):
	return listed_row(Decimal(row["remaining"]), Decimal(row["original"]), row["kind"])


def _ordered(rows, sort, order):
	"""Dated rows first, undated last; equal keys go to `id` ascending in both directions."""
	key = SORT_KEYS[sort or "remaining"]
	reverse = (order or "desc") == "desc"
	by_id = sorted(rows, key=lambda row: row["id"])
	dated = [row for row in by_id if row["due_date"] is not None]
	undated = [row for row in by_id if row["due_date"] is None]
	return sorted(dated, key=key, reverse=reverse) + sorted(undated, key=key, reverse=reverse)


def _selection(kind, key, status, result, side):
	rows = result["sources"][side]["rows"]
	if kind == "day":
		return [row for row in rows if row["due_date"] == key and _listed(row)]
	belongs = detail.bucket_filter(key, status["scope"])
	return [row for row in rows if belongs(row)]


def _total(kind, key, rows, result, side):
	if kind == "day":
		return detail._total(rows, result)
	return detail._bucket_total(key, rows, result, side)


def legacy_page(status, result, *, kind, key, side, page, page_size, party_page, sort, order, parties=True):
	"""The public reply the rev-4 path gave for one day (`kind="day"`) or bucket (`kind="bucket"`)."""
	rows = _selection(kind, key, status, result, side)
	party_slice = party_paging = None
	if parties:
		summary = detail._party_rows(rows)
		slice_ = detail.page_rows(summary, party_page, page_size)
		party_slice, party_paging = slice_["rows"], {"total": len(summary), "page": slice_["page"]}
	invoices = detail.page_rows(_ordered(rows, sort, order), page, page_size)
	page_ = detail.Page(
		rows=invoices.pop("rows"),
		paging=invoices,
		party_slice=party_slice,
		party_paging=party_paging,
		total_remaining=_total(kind, key, rows, result, side),
		generated_at=result["generated_at"],
		precision=result["projection"]["precision"],
	)
	return detail.render(page_)
