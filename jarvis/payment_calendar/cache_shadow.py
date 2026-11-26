"""The cache shadow: what a generation would serve, and the night proof that it equals today's read.

Two parts.

`serve` and `serve_bank` are pure functions. They turn the stored rows and the payload of ONE generation into the
view result that the reader gives for a window (`serve`) or for a set of bank accounts (`serve_bank`). Nothing is served yet.
nothing; the serving release calls them, and `night_oracle` proves them every night.

What `serve` returns (the live reader's behaviour wins; the tests prove it against real fixtures):

- Every stored row that is not a residue row and not an excluded FX row (the open rows, whose content does not
  depend on the window), the excluded FX rows, and the result-level fields of the payload.
- Every stored RESIDUE row (a zero-money row) whose own `due_date` is inside the window: the reader's overlay adds a
  settled row for each term whose due date is inside the window, whatever else the invoice's dates are. A residue
  row therefore stands for ONE reader row (one term), with that term's own due date and amounts.
  `hide_in_window` plays no part: the reader shows such a row when its due date is in the window.
- A refusal. A residue row with `window_refusal` set and a date of `window_due_dates` (the dates that make the reader
  select the invoice: its schedule and header due dates) inside the window makes `serve` raise
  `settlement_schedule_mismatch`.

- `notes.invoice_level` counted again over the rows it returns, as the reader counts it over the rows of the window
  (residue rows with an `allocation_basis` included), not the build's note of the open window.
- `rows` in the reader's order (due date, voucher type, voucher number); `excluded_rows` in the stored order, which is
  the build's, which is the reader's engine order. `comparable` sorts both for the compare.

What it cannot give: the rows of settled invoices that the open-set build never discovers (no open header, no
foreign-currency or multi-account residue). The reader shows their overlay rows and may refuse a window for them.
They are left as today for now; `comparable` drops their zero-money rows from both sides, and
`night_oracle` records a refusal the generation did not know as `refusal_unseen`.

`night_oracle(company, side, anchor, rows, payload)` runs inside the build's `consistent_read()` view of a night
unit. It compares `serve` for the legacy 24-month window with today's read (guards lifted) row by row (`comparable`), or, on a
side with more ledger rows than `ORACLE_FULL_LEDGER_ROWS`, with ERPNext's report read for a sample of parties.
A sample record says how much it covered (`counts.parties` of `counts.parties_total`): a difference in a party
outside the sample is not seen that night (the sample changes every day with the anchor date). For
the bank it compares each stored balance with `banks.read_bank_balances`. The result holds counts, fixed words and
at most `MAX_DIGESTS` HMAC digests, never a name (the record lands in the RQ result). On a site without an
`encryption_key` a `diff` carries counts only (`digests` is empty). Nothing is written in the view: `persist` stores the result in
`Generation.shadow` after the view (the caller commits).
"""

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from random import Random
from time import monotonic

import frappe
from frappe.query_builder import Criterion
from frappe.query_builder.functions import Count
from frappe.utils import add_days, add_months, get_first_day, get_last_day, getdate

from jarvis.payment_calendar import (
	banks,
	builtin_reader,
	cache_state,
	cache_store,
	read_log,
	routing,
	shadow,
)
from jarvis.payment_calendar import sources as today_sources
from jarvis.payment_calendar.errors import TIMEOUTS, CalendarSourceUnavailable

# --- Numbers T0 must confirm. Each is a default until the measurement sets it. ---
# A side with more ledger lines than this is compared on a sample of parties (100,000 ledger rows).
ORACLE_FULL_LEDGER_ROWS = 100_000
# The sample: random parties (seed = the anchor date), the largest by line count, and every shared name.
SAMPLE_RANDOM = 20
SAMPLE_LARGEST = 5
# --- end of the T0-dependent numbers ---

MAX_DIGESTS = 20
# The most bank accounts the oracle reads one by one.
MAX_BANK_ORACLE_ACCOUNTS = 100
OUTCOMES = ("compared", "compared_sample", "open_equal_window_refusal", "diff", "not_compared")
WINDOW_REFUSAL = "settlement_schedule_mismatch"
_AMOUNTS = cache_store._AMOUNTS
_JSON = cache_store._JSON
_DATES = cache_store._DATES


# -------------------------------------------------------------------------------------------- stored rows


def decode_row(raw: Mapping | tuple | list) -> dict:
	"""A generation row, from a build tuple or a stored mapping, as plain values (dates as ISO text, JSON parsed).

	An empty text is None, as the store keeps it. Unknown fields are refused.
	"""
	fields = (
		dict(zip(cache_store.ROW_FIELDS, raw, strict=True)) if isinstance(raw, (tuple, list)) else dict(raw)
	)
	extra = set(fields) - set(cache_store.ROW_FIELDS)
	if extra - {"name", "generation", "seq", "row_id", "creation", "modified", "owner", "modified_by"}:
		raise ValueError("A cache row has an unknown field.")
	return {name: _decode_value(name, fields.get(name)) for name in cache_store.ROW_FIELDS}


def _decode_value(name: str, value):
	if name in cache_store._FLAGS:
		return int(value or 0)
	if value is None or value == "":
		return None
	if name in _DATES:
		return value.isoformat() if isinstance(value, (date, datetime)) else str(value)
	if name in _AMOUNTS:
		return cache_store.amount_text(value)
	if name in _JSON:
		return json.loads(value) if isinstance(value, str) else value
	return str(value)


def _reader_row(fields: dict) -> dict:
	"""A stored obligation or adjustment row as the reader's `_assemble` gives it (no id: ids are positions)."""
	row = {
		"voucher_type": fields["voucher_type"],
		"voucher_no": fields["voucher_no"],
		"party_type": fields["party_type"],
		"party": fields["party"],
		"party_name": fields["party_name"],
		"account": fields["account"],
		"posting_date": fields["posting_date"],
		"due_date": fields["due_date"],
		"term": fields["term"] or "",
		"original": fields["original"],
		"paid": fields["paid"],
		"credit": fields["credit"],
		"discount": fields["discount"],
		"remaining": fields["remaining"],
		"settlement": fields["settlement"],
		"kind": fields["kind"],
		"_permission": fields["permission"],
	}
	if fields["allocation_basis"]:
		row["allocation_basis"] = fields["allocation_basis"]
		row["allocation_reason"] = fields["allocation_reason"]
	return row


def _excluded_row(fields: dict) -> dict:
	return {
		"voucher_type": fields["voucher_type"],
		"voucher_no": fields["voucher_no"],
		"party_type": fields["party_type"],
		"party": fields["party"],
		"remaining": fields["remaining"],
		"kind": fields["kind"],
		"account_remaining": fields["account_remaining"],
		"_permission": fields["permission"],
	}


# ------------------------------------------------------------------------------------------------- serve


def legacy_window(anchor: date | str) -> tuple[str, str]:
	"""The 24-month window of `scope.normalize_scope` for a month-less scope, from `anchor` (a date)."""
	anchor = getdate(anchor)
	first = str(get_first_day(add_months(anchor, -12)))
	last = str(get_last_day(add_months(anchor, 11)))
	return first, str(add_days(last, 30))


def window_refused(decoded: Iterable[dict], window_start, window_end) -> bool:
	"""True when a decoded residue row would make the reader refuse a window: its flag and a due date inside it."""
	start, end = str(getdate(window_start)), str(getdate(window_end))
	return any(
		fields["residue"]
		and fields["window_refusal"]
		and any(start <= due <= end for due in fields["window_due_dates"] or ())
		for fields in decoded
	)


def serve(rows: Iterable[Mapping | tuple | list], payload: Mapping, window_start, window_end) -> dict:
	"""The reader's result for a window of a Receivable or Payable generation. Pure.

	`rows` are the stored rows (mappings with the store's field names) or build tuples; `payload` is the generation
	payload (a mapping). Raises `CalendarSourceUnavailable("settlement_schedule_mismatch")` when `window_refused`.
	The rows carry no id, and they are in the reader's order (due date, voucher type, voucher number); two rows that
	tie on all three are in no fixed order, as the reader's ids are positions (compare with `comparable`).
	`excluded_rows` keep the stored order (the engine's), and `notes.invoice_level` is counted over the rows returned.
	"""
	decoded = [decode_row(raw) for raw in rows]
	if window_refused(decoded, window_start, window_end):
		raise CalendarSourceUnavailable(WINDOW_REFUSAL)
	start, end = str(getdate(window_start)), str(getdate(window_end))
	obligations = [
		_reader_row(fields)
		for fields in decoded
		if fields["kind"] not in (cache_store.KIND_EXCLUDED, cache_store.KIND_BANK)
		and (not fields["residue"] or (fields["due_date"] and start <= fields["due_date"] <= end))
	]
	obligations.sort(key=lambda row: (row["due_date"] or "", row["voucher_type"], row["voucher_no"]))
	excluded = [_excluded_row(fields) for fields in decoded if fields["kind"] == cache_store.KIND_EXCLUDED]
	return {
		"rows": obligations,
		"excluded_rows": excluded,
		"notes": invoice_level_note(payload["notes"], obligations),
		"canonical_total": payload["canonical_total"],
		"currency": payload["currency"],
		"precision": payload["precision"],
		"complete_for_company": payload["complete_for_company"],
		"scope_reasons": payload["scope_reasons"],
		"permission_columns": payload["permission_columns"],
	}


def serve_bank(rows: Iterable[Mapping | tuple | list], payload: Mapping, accounts: list[str]) -> dict:
	"""`banks.read_bank_balances` for `accounts`, from a Bank generation. Pure.

	Refuses `multibook_unsupported` when the company's Payment Ledger has a foreign finance book or a selected
	account has GL rows in one (today's `validate_finance_books`). An account that the generation does not hold
	is a refusal too (`source_contract_unavailable`: the generation is older than the account).
	"""
	balances = {}
	for raw in rows:
		fields = decode_row(raw)
		if fields["kind"] == cache_store.KIND_BANK:
			balances[fields["account"]] = fields["remaining"]
	if payload["ple_book_flag"] or any(name in payload["multibook_accounts"] for name in accounts):
		raise CalendarSourceUnavailable("multibook_unsupported")
	if any(name not in balances for name in accounts):
		raise CalendarSourceUnavailable()
	precision = payload["precision"]
	chosen = [{"account": name, "balance": balances[name]} for name in accounts]
	total = sum((Decimal(row["balance"]) for row in chosen), Decimal(0))
	return {
		"total": str(today_sources.money(total, precision)),
		"accounts": chosen,
		"currency": payload["currency"],
		"precision": precision,
	}


# ----------------------------------------------------------------------------------------------- compare


def _body(row: Mapping) -> str:
	return json.dumps({key: value for key, value in row.items() if key != "id"}, sort_keys=True, default=str)


def _plain(value):
	"""A value as JSON text would show it: dates as ISO text, amounts as text. The reader holds dates, a store text."""
	if isinstance(value, Mapping):
		return {key: _plain(item) for key, item in value.items()}
	if isinstance(value, (list, tuple)):
		return [_plain(item) for item in value]
	if isinstance(value, (date, datetime)):
		return value.isoformat()
	return str(value) if isinstance(value, Decimal) else value


def stored_vouchers(rows: Iterable[Mapping | tuple | list]) -> frozenset[str]:
	"""The vouchers a generation holds a row for (open, residue or excluded)."""
	return frozenset(decode_row(raw)["voucher_no"] for raw in rows)


def invoice_level_note(notes: Mapping, rows: Iterable[Mapping]) -> dict:
	"""`notes` with `invoice_level` counted over `rows`, as the reader counts it over the rows of its result."""
	counted = {key: value for key, value in notes.items() if key != "invoice_level"}
	level = sum(row.get("allocation_basis") == "invoice" for row in rows)
	if level:
		counted["invoice_level"] = {"count": level}
	return counted


def exempt(result: Mapping, vouchers: frozenset[str]) -> dict:
	"""A reader result without the settled overlay rows that a generation cannot hold.

	A zero-money row of a voucher outside `vouchers` belongs to an invoice the open-set build never discovers.
	Those rows are dropped, and `notes.invoice_level` is counted again over the rows that stay (the reader's rule),
	so the note is comparable with `serve`'s.
	"""
	rows = [
		row for row in result["rows"] if Decimal(row["remaining"]) != 0 or row.get("voucher_no") in vouchers
	]
	return {**result, "rows": rows, "notes": invoice_level_note(result["notes"], rows)}


def comparable(result: Mapping, parties: frozenset[str] | None = None) -> dict:
	"""A reader or serve result in a fixed order, without ids, as plain values. Notes are compared as they are.

	With `parties`, rows and excluded rows of other parties are dropped and the total is the sum of the rows kept
	(the sample compare); the result-level fields are then left out.
	"""
	rows = [
		_plain({key: value for key, value in row.items() if key != "id"})
		for row in result["rows"]
		if parties is None or row["party"] in parties
	]
	rows.sort(key=_body)
	excluded = [
		_plain({key: value for key, value in row.items() if key != "id"})
		for row in result["excluded_rows"]
		if parties is None or row["party"] in parties
	]
	excluded.sort(key=_body)
	if parties is not None:
		total = sum((Decimal(row["remaining"]) for row in rows), Decimal(0))
		return {"rows": rows, "excluded_rows": excluded, "total": str(total)}
	return {
		"rows": rows,
		"excluded_rows": excluded,
		"notes": _plain(result["notes"]),
		"canonical_total": result["canonical_total"],
		"currency": result["currency"],
		"precision": result["precision"],
		"complete_for_company": result["complete_for_company"],
		"scope_reasons": _plain(result["scope_reasons"]),
		"permission_columns": _plain(result["permission_columns"]),
	}


def compare_open(expected: dict, actual: dict, digest=None) -> dict:
	"""`compared`, or `diff` with counts and at most `MAX_DIGESTS` digests of the rows that differ. Pure."""
	if expected == actual:
		return {"outcome": "compared", "counts": {"rows": len(actual["rows"])}}
	only_expected = Counter(_body(row) for row in expected["rows"]) - Counter(
		_body(row) for row in actual["rows"]
	)
	only_actual = Counter(_body(row) for row in actual["rows"]) - Counter(
		_body(row) for row in expected["rows"]
	)
	counts = {
		"rows": len(actual["rows"]),
		"only_today": sum(only_expected.values()),
		"only_cache": sum(only_actual.values()),
		"excluded_differ": int(expected["excluded_rows"] != actual["excluded_rows"]),
		"fields": sorted(
			key
			for key in {*expected, *actual} - {"rows", "excluded_rows"}
			if expected.get(key) != actual.get(key)
		),
	}
	keys = sorted({*only_expected, *only_actual})[:MAX_DIGESTS]
	return {"outcome": "diff", "counts": counts, "digests": [digest(key) for key in keys] if digest else []}


# ----------------------------------------------------------------------------------------------- oracle


def night_oracle(company: str, side: str, anchor: date, rows: list, payload: dict) -> dict | None:
	"""The proof of one night unit, or None when the cache is not active or the side is unknown.

	Called by `cache_refresh.build_generation` inside the view, after the build. It reads only (the view allows no
	write) and returns a name-free record: `outcome` (`compared`, `compared_sample`, `open_equal_window_refusal`,
	`diff`, `not_compared`), `reason`, `counts`, `digests`, `cache_build`, `oracle_ms`.
	"""
	if side not in cache_store.SIDES or not cache_state.cache_settings().active:
		return None
	started = monotonic()
	digest = read_log.ops_digest if frappe.conf.get("encryption_key") else None
	try:
		if side == "Bank":
			record = _bank_oracle(company, getdate(anchor), rows, payload, digest)
		else:
			record = _side_oracle(company, side, getdate(anchor), rows, payload, digest)
	except TIMEOUTS:
		raise
	except Exception as exc:
		record = {"outcome": "not_compared", "reason": f"error:{type(exc).__name__}"}
	return {
		**record,
		"cache_build": routing.cache_build()[:12],
		"oracle_ms": int((monotonic() - started) * 1000),
	}


def _attempt(read, *args):
	"""`(result, None)` or `(None, CalendarSourceUnavailable)`; any other error, and a job deadline, propagate."""
	try:
		return read(*args), None
	except CalendarSourceUnavailable as exc:
		return None, exc


def _today_lifted(company, side, anchor, start, end):
	with shadow.lifted_guards():
		return today_sources.read_obligations(company, side, anchor.isoformat(), start, end)


def _side_oracle(company, side, anchor, rows, payload, digest) -> dict:
	start, end = legacy_window(anchor)
	if today_sources._ledger_rows_exceed(company, side, anchor, ORACLE_FULL_LEDGER_ROWS):
		return _sample_oracle(company, side, anchor, rows, payload, start, end, digest)
	today, today_error = _attempt(_today_lifted, company, side, anchor, start, end)
	served, served_error = _attempt(serve, rows, payload, start, end)
	known = stored_vouchers(rows)
	if today_error is None and served_error is None:
		return {**compare_open(comparable(exempt(today, known)), comparable(served), digest), "mode": "full"}
	if today_error is not None and today_error.code == WINDOW_REFUSAL:
		return _window_refusal(company, side, anchor, rows, payload, served_error, digest)
	return {
		"outcome": "diff",
		"mode": "full",
		"counts": {
			"refusals": [getattr(today_error, "code", None), getattr(served_error, "code", None)],
		},
		"digests": [],
	}


def _window_refusal(company, side, anchor, rows, payload, served_error, digest) -> dict:
	"""Today refuses the legacy window for a settled candidate: the open part is compared with today's open window.

	`counts.refusal_unseen` is 1 when the generation did not know the refusal (a settled invoice that the open-set
	build never reads); a later release cannot predict such a refusal from the generation.
	"""
	today, today_error = _attempt(
		_today_lifted, company, side, anchor, cache_store.OPEN_SET_WINDOW, cache_store.OPEN_SET_WINDOW
	)
	if today_error is not None:
		return {"outcome": "not_compared", "reason": f"today_refused:{today_error.code}", "mode": "full"}
	served, open_error = _attempt(
		serve, rows, payload, cache_store.OPEN_SET_WINDOW, cache_store.OPEN_SET_WINDOW
	)
	if open_error is not None:
		return {"outcome": "not_compared", "reason": f"served_refused:{open_error.code}", "mode": "full"}
	known = stored_vouchers(rows)
	record = compare_open(comparable(exempt(today, known)), comparable(served), digest)
	if record["outcome"] == "diff":
		return {**record, "mode": "full"}
	counts = {**record["counts"], "refusal_unseen": int(served_error is None)}
	return {"outcome": "open_equal_window_refusal", "mode": "full", "counts": counts}


# ------------------------------------------------------------------------------------------ the sample


def sample_parties(company: str, side: str, anchor: date) -> list[str]:
	"""The parties of the sample (see `_sample`)."""
	return _sample(company, side, anchor)[0]


def _sample(company: str, side: str, anchor: date) -> tuple[list[str], int]:
	"""The sample, and the number of parties in the ledger (the sample's coverage).

	The sample is drawn from the LEDGER (not from the generation): the largest, every shared name, and random
	parties.
	One aggregate over the side's ledger predicate gives each party's line count and how many party types use the
	name. The random part is seeded with the anchor date, so a re-run picks the same parties.
	"""
	ple = frappe.qb.DocType("Payment Ledger Entry")
	accounts = frappe.get_all("Account", filters={"account_type": side, "company": company}, pluck="name")
	counted = (
		frappe.qb.from_(ple)
		.select(ple.party, Count("*"), Count(ple.party_type).distinct())
		.where(
			Criterion.all(builtin_reader._ledger_predicate(ple, company, side, accounts))
			& (ple.posting_date <= anchor)
		)
		.groupby(ple.party)
		.run()
	)
	lines = {party: count for party, count, _types in counted if party}
	shared = {party for party, _count, types in counted if party and types > 1}
	largest = sorted(lines, key=lambda party: (-lines[party], party))[:SAMPLE_LARGEST]
	rest = sorted(set(lines) - set(largest) - shared)
	chosen = Random(anchor.isoformat()).sample(rest, min(SAMPLE_RANDOM, len(rest)))
	return sorted({*largest, *shared, *chosen}), len(lines)


def _today_sample(company, side, anchor, start, end, parties: list[str]) -> dict:
	"""ERPNext's report read for `parties` only, through today's engine, guards lifted."""
	report, window_start, window_end, anchor_date, values = today_sources._require_inputs(
		company, side, anchor.isoformat(), start, end
	)
	filters = {**today_sources._engine_filters(company, anchor_date), "party": list(parties)}
	with shadow.lifted_guards():
		engine = today_sources._run_engine(
			today_sources._report_class(), filters, side, window_start, window_end
		)
	return today_sources._assemble(engine, report, side, window_start, window_end, values)


def _sample_oracle(company, side, anchor, rows, payload, start, end, digest) -> dict:
	parties, parties_total = _sample(company, side, anchor)
	today, error = _attempt(_today_sample, company, side, anchor, start, end, parties)
	if error is not None:
		return {"outcome": "not_compared", "reason": f"sample_refused:{error.code}", "mode": "sample"}
	chosen, known = frozenset(parties), stored_vouchers(rows)
	served, served_error = _attempt(serve, rows, payload, start, end)
	if served_error is not None:
		return {
			"outcome": "not_compared",
			"reason": f"sample_served_refused:{served_error.code}",
			"mode": "sample",
		}
	record = compare_open(comparable(exempt(today, known), chosen), comparable(served, chosen), digest)
	counts = {**record["counts"], "parties": len(parties), "parties_total": parties_total}
	outcome = "compared_sample" if record["outcome"] == "compared" else "diff"
	return {**record, "outcome": outcome, "mode": "sample", "counts": counts}


# ------------------------------------------------------------------------------------------------- bank


def _bank_oracle(company, anchor, rows, payload, digest) -> dict:
	"""Each stored balance against `banks.read_bank_balances` for that account; flagged accounts must refuse."""
	stored = {}
	for raw in rows:
		fields = decode_row(raw)
		if fields["kind"] == cache_store.KIND_BANK:
			stored[fields["account"]] = fields["remaining"]
	if len(stored) > MAX_BANK_ORACLE_ACCOUNTS:
		return {"outcome": "not_compared", "reason": "too_many_accounts"}
	different = []
	for name in sorted(stored):
		today, today_error = _attempt(banks.read_bank_balances, company, [name], anchor.isoformat())
		served, served_error = _attempt(serve_bank, rows, payload, [name])
		if today_error is not None or served_error is not None:
			agree = (
				today_error is not None and served_error is not None and today_error.code == served_error.code
			)
		else:
			agree = today["accounts"] == served["accounts"] and today["total"] == served["total"]
		if not agree:
			different.append(name)
	counts = {"accounts": len(stored), "differ": len(different)}
	if not different:
		return {"outcome": "compared", "mode": "bank", "counts": counts}
	keys = different[:MAX_DIGESTS]
	return {
		"outcome": "diff",
		"mode": "bank",
		"counts": counts,
		"digests": [digest(name) for name in keys] if digest else [],
	}


# ------------------------------------------------------------------------------------------- persistence


def persist(generation: int, record: dict | None) -> bool:
	"""Store a night record in `Generation.shadow` (name-free JSON); True when the generation row exists.

	The caller commits, outside the view. A generation that is gone (purged) stores nothing: the call logs the class
	`persist_generation_gone` (no name) and returns False. No record stores nothing and returns False.
	"""
	if not record:
		return False
	text = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
	frappe.db.sql(
		f"UPDATE `tab{cache_store.GENERATION}` SET shadow=%s WHERE name=%s",
		(text, generation),
	)
	if frappe.db.sql(f"SELECT 1 FROM `tab{cache_store.GENERATION}` WHERE name=%s", (generation,)):
		return True
	frappe.logger("payment_calendar").info("cache_shadow persist_generation_gone")
	return False
