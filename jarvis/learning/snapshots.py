"""Monthly aggregate snapshots for Access-Log detectors (plan sections 4.4, 5.3, 6.1).

``Jarvis Pattern Snapshot`` holds one row per (detector_id, period YYYY-MM,
company) with an aggregate JSON payload. For the print-format detector the
payload is nested ``{party -> print_format -> {n, eff, days}}`` counts:
``n`` raw print events, ``eff`` the burst-collapsed effective count
(``stats.collapse_bursts`` over creation timestamps, so a one-session reprint
storm cannot satisfy n_min), ``days`` a ``{YYYY-MM-DD: count}`` map for the
spread gate. Snapshots make Access-Log history durable against the 30-day
Log Settings race and keep re-scans bounded to the un-ingested tail.

Fence contract (plan section 5.4): detectors never write. The detector's
postprocess READS snapshot rows plus the live log tail through the PatternDB
facade only; persistence happens here, engine-side, after the read fence
closes. :func:`ingest_print_log` is the one entry point the engine calls -
it streams ``tabAccess Log`` beyond the ``Jarvis Pattern Detector State``
watermark by indexed ``creation`` (``modified`` is NOT indexed on log
tables), resolves customers via bounded IN-list joins, merges the events
into monthly snapshot rows and advances the watermark. It never raises
(errors are logged and summarized) so the engine call stays a safe one-liner:

    from jarvis.learning import snapshots
    snapshots.ingest_print_log(run=run, paused=bool(paused_note))

placed in ``engine._execute`` after the mining loop's final FDR flush/commit
(the fence is closed between units, so any post-mining point is safe).

Budget contract (plan section 5.3): each chunk commits its OWN small
transaction (that chunk's upserts + the watermark move together), the
inter-chunk sleep runs with no transaction open, a paused mining run skips
the ingest entirely, and a scheduled run's closed analysis window halts it
between chunks. The watermark makes every skip/halt lossless - the next
run resumes the stream where this one stopped.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict

import frappe
from frappe.utils import now_datetime

SNAPSHOT = "Jarvis Pattern Snapshot"
DETECTOR_STATE = "Jarvis Pattern Detector State"
PRINT_FORMAT_DETECTOR_ID = "sell-customer-print-format"

# frappe/www/printview.py writes page=f"Print Format: {name}" on every
# printview render (the ONLY writer of method='Print' rows).
PRINT_PAGE_PREFIX = "Print Format: "

EPOCH_WATERMARK = "1900-01-01 00:00:00"
IN_LIST_CHUNK = 500  # bounded IN-list joins (plan section 5.3)
INGEST_MAX_ROWS = 50000  # per-call ceiling on streamed log rows
CHUNK_SLEEP_S = 0.2  # breathing room between log chunks (plan section 5.3)
PRINT_BURST_MAX_GAP_S = 120  # mirrors executor.BURST_MAX_GAP_S

_SNAPSHOT_ROWS_SQL = """
SELECT s.period AS period,
       s.company AS company,
       s.payload AS payload
FROM `tabJarvis Pattern Snapshot` s
WHERE s.detector_id = %(detector_id)s
LIMIT 10000
"""

_WATERMARK_SQL = """
SELECT ds.last_watermark AS wm
FROM `tabJarvis Pattern Detector State` ds
WHERE ds.name = %(detector_id)s
LIMIT 1
"""


def make_snapshot_key(detector_id, period, company) -> str:
	"""Deterministic unique key for the (detector_id, period, company) triple
	(hashed: company names can push a readable key past the 140-char Data
	limit). Mirrors the executor's pattern_key idiom."""
	raw = f"{detector_id}|{period}|{company or ''}"
	return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:40]


def _db_runner(query: str, params=None):
	"""Engine-side (unfenced) SQL runner; postprocess callers pass
	``patterndb.sql_select`` instead so their reads stay inside the fence."""
	return frappe.db.sql(query, params or {}, as_dict=True)


def _assert_fenced(doctype: str) -> None:
	"""Route every snapshot-side write through the engine's doctype allowlist
	(plan section 5.4). Lazy import: engine never imports this module at load
	time, so there is no cycle."""
	from jarvis.learning.engine import _fenced_write

	_fenced_write(doctype)


# --------------------------------------------------------------------------- #
# upsert / read
# --------------------------------------------------------------------------- #
def upsert_monthly(detector_id: str, period: str, company: str | None, payload_dict: dict) -> str:
	"""Merge ``payload_dict`` additively into the (detector_id, period, company)
	snapshot row, creating it when missing. Engine-side only (asserted against
	the write fence). Returns the row name. Idempotence contract: callers feed
	each log row exactly once (the watermark guarantees it); merging the same
	batch twice would double-count, which is why the watermark advances in the
	same transaction as the upserts."""
	_assert_fenced(SNAPSHOT)
	key = make_snapshot_key(detector_id, period, company)
	name = frappe.db.get_value(SNAPSHOT, {"snapshot_key": key})
	if name:
		existing = _parse_payload(frappe.db.get_value(SNAPSHOT, name, "payload"))
		merged = merge_payloads(existing, payload_dict)
		frappe.db.set_value(
			SNAPSHOT,
			name,
			{
				"payload": frappe.as_json(merged),
				"rows_ingested": int(merged.get("rows_ingested") or 0),
			},
			update_modified=False,
		)
		return name
	doc = frappe.get_doc(
		{
			"doctype": SNAPSHOT,
			"detector_id": detector_id,
			"period": period,
			"company": company,
			"payload": frappe.as_json(payload_dict),
			"rows_ingested": int(payload_dict.get("rows_ingested") or 0),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def read_all(
	detector_id: str, company: str | None = None, runner=None, window_start: str | None = None
) -> dict:
	"""One merged payload across the detector's monthly snapshot rows.

	``company`` filters to one company (None = all); ``window_start``
	(YYYY-MM-DD) drops months before the detector window. ``runner`` defaults
	to the engine-side reader; fenced callers pass ``patterndb.sql_select``."""
	runner = runner or _db_runner
	rows = runner(_SNAPSHOT_ROWS_SQL, {"detector_id": detector_id}) or []
	min_period = (window_start or "")[:7]
	merged: dict = {}
	periods = []
	for r in rows:
		if company is not None and (r.get("company") or None) != company:
			continue
		period = r.get("period") or ""
		if min_period and period < min_period:
			continue
		merged = merge_payloads(merged, _parse_payload(r.get("payload")))
		periods.append(period)
	merged["periods"] = sorted(set(periods))
	return merged


def read_watermark(detector_id: str, runner=None) -> str:
	"""The detector's stream position (Detector State.last_watermark), or the
	epoch when the state row is missing/blank (full history scan)."""
	runner = runner or _db_runner
	rows = runner(_WATERMARK_SQL, {"detector_id": detector_id}) or []
	wm = rows[0].get("wm") if rows else None
	return str(wm) if wm else EPOCH_WATERMARK


def merge_payloads(base: dict | None, extra: dict | None) -> dict:
	"""Additive merge of two print-format payloads (counts summed, day maps
	summed, first/last markers widened). ``eff`` sums are approximate across
	batches: a burst spanning two ingest batches (or two per-chunk commits of
	one ingest call) counts once per batch - acceptable, it only ever
	over-counts by one per boundary."""
	out: dict = {"party_type": "Customer", "counts": {}, "rows_ingested": 0}
	for payload in (base, extra):
		if not payload:
			continue
		for party, formats in (payload.get("counts") or {}).items():
			for fmt, agg in (formats or {}).items():
				dst = out["counts"].setdefault(party, {}).setdefault(fmt, {"n": 0, "eff": 0, "days": {}})
				dst["n"] += int((agg or {}).get("n") or 0)
				dst["eff"] += int((agg or {}).get("eff") or 0)
				for day, count in ((agg or {}).get("days") or {}).items():
					dst["days"][day] = int(dst["days"].get(day) or 0) + int(count or 0)
		out["rows_ingested"] += int(payload.get("rows_ingested") or 0)
		for field, pick in (
			("first_day", min),
			("last_day", max),
			("first_creation", min),
			("last_creation", max),
		):
			value = payload.get(field)
			if value:
				out[field] = pick(out[field], value) if out.get(field) else value
	return out


def _parse_payload(raw) -> dict:
	if isinstance(raw, dict):
		return raw
	if not raw:
		return {}
	try:
		parsed = frappe.parse_json(raw)
	except Exception:
		return {}
	return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------- #
# print-log streaming + party resolution (shared: postprocess reads through
# the facade runner; ingest reads engine-side)
# --------------------------------------------------------------------------- #
def stream_print_events(runner, watermark, max_rows: int = INGEST_MAX_ROWS):
	"""Stream Print rows of ``tabAccess Log`` beyond ``watermark``, chunked by
	indexed ``creation`` (static statement; only the watermark parameter
	advances - plan section 5.3), resolving each chunk's customers as we go.
	Never sleeps: the caller's transaction (the postprocess read fence) stays
	open across the loop, so any pacing must happen outside it (the engine-side
	ingest paces itself between per-chunk commits instead).

	Returns ``(events, last_creation, rows_read)``. ``last_creation`` is the
	creation of the last RAW log row read (unresolvable rows advance it too,
	so they are never revisited). Ties at a chunk boundary are a non-issue in
	practice: ``creation`` is datetime(6)."""
	from jarvis.learning.detectors.selling import PRINT_LOG_CHUNK, PRINT_LOG_SQL

	events: list = []
	last = str(watermark or EPOCH_WATERMARK)
	rows_read = 0
	while rows_read < max_rows:
		rows = runner(PRINT_LOG_SQL, {"watermark": last}) or []
		if not rows:
			break
		rows_read += len(rows)
		events.extend(resolve_print_events(rows, runner))
		last = str(rows[-1].get("created"))
		if len(rows) < PRINT_LOG_CHUNK:
			break
	return events, last, rows_read


def resolve_print_events(rows, runner) -> list:
	"""Access Log rows -> customer print events. Parses the format name from
	``page`` and joins ``reference_document`` back to its selling doctype for
	the customer + company (bounded IN-lists, one static statement per
	doctype). Rows referencing other doctypes, drafts, or deleted documents
	are dropped."""
	from jarvis.learning.detectors.selling import PRINT_PARTY_SQL

	parsed = []
	names_by_doctype: dict = defaultdict(set)
	for r in rows or []:
		fmt = parse_print_format(r.get("page"))
		ref_doctype = r.get("ref_doctype")
		ref_name = r.get("ref_name")
		if not fmt or not ref_name or ref_doctype not in PRINT_PARTY_SQL:
			continue
		parsed.append((r, ref_doctype, ref_name, fmt))
		names_by_doctype[ref_doctype].add(ref_name)

	resolved: dict = {}
	for ref_doctype, names in names_by_doctype.items():
		sql = PRINT_PARTY_SQL[ref_doctype]
		ordered = sorted(names)
		for i in range(0, len(ordered), IN_LIST_CHUNK):
			chunk = ordered[i : i + IN_LIST_CHUNK]
			for hit in runner(sql, {"names": chunk}) or []:
				if hit.get("party"):
					resolved[(ref_doctype, hit.get("name"))] = hit

	events = []
	for r, ref_doctype, ref_name, fmt in parsed:
		hit = resolved.get((ref_doctype, ref_name))
		if not hit:
			continue
		created = str(r.get("created"))
		events.append(
			{
				"party_type": "Customer",
				"party": hit.get("party"),
				"print_format": fmt,
				"company": hit.get("company"),
				"created": created,
				"day": created[:10],
				"period": created[:7],
			}
		)
	return events


def parse_print_format(page) -> str | None:
	"""'Print Format: Tax Invoice' -> 'Tax Invoice' (None when not a
	print-format page)."""
	text = str(page or "").strip()
	if not text.startswith(PRINT_PAGE_PREFIX):
		return None
	return text[len(PRINT_PAGE_PREFIX) :].strip() or None


def aggregate_events(events) -> dict:
	"""Group resolved events into ``{(period, company): payload}`` monthly
	aggregates (the upsert grain). Burst collapse runs per (party, format)
	within the batch."""
	from jarvis.learning import stats

	grouped: dict = defaultdict(list)
	for e in events or []:
		grouped[(e["period"], e.get("company"))].append(e)

	out: dict = {}
	for key, evs in grouped.items():
		counts: dict = {}
		created_by_pair: dict = defaultdict(list)
		for e in evs:
			agg = counts.setdefault(e["party"], {}).setdefault(
				e["print_format"], {"n": 0, "eff": 0, "days": {}}
			)
			agg["n"] += 1
			agg["days"][e["day"]] = int(agg["days"].get(e["day"]) or 0) + 1
			created_by_pair[(e["party"], e["print_format"])].append(e["created"])
		for (party, fmt), created in created_by_pair.items():
			counts[party][fmt]["eff"] = stats.collapse_bursts(created, max_gap_s=PRINT_BURST_MAX_GAP_S)
		days = sorted(e["day"] for e in evs)
		created_all = sorted(e["created"] for e in evs)
		out[key] = {
			"party_type": "Customer",
			"counts": counts,
			"first_day": days[0],
			"last_day": days[-1],
			"first_creation": created_all[0],
			"last_creation": created_all[-1],
			"rows_ingested": len(evs),
		}
	return out


# --------------------------------------------------------------------------- #
# engine-side ingest (the documented one-liner target)
# --------------------------------------------------------------------------- #
def ingest_print_log(
	detector_id: str = PRINT_FORMAT_DETECTOR_ID,
	max_rows: int = INGEST_MAX_ROWS,
	run=None,
	paused: bool = False,
) -> dict:
	"""Fold the un-ingested Access Log Print tail into monthly snapshots and
	advance the detector watermark. Designed to be called by the engine AFTER
	the read fence closes (post-mining); never raises - errors are logged and
	reported in the returned summary so the engine call is a safe one-liner.

	Budget gates (plan section 5.3): ``paused`` (the engine's paused_note -
	time/row budget, window end, or disabled mid-run) skips the ingest
	entirely; ``run`` (the Jarvis Pattern Run doc) lets a SCHEDULED run halt
	between chunks once its analysis window closes (manual runs bypass the
	window, mirroring ``engine._pause_reason``). Each chunk commits its own
	small transaction - that chunk's upserts and the watermark move together -
	so a skip, halt, or crash never loses more than the in-flight chunk and
	the next run resumes the stream from the watermark, losslessly."""
	try:
		return _ingest_print_log(detector_id, max_rows, run=run, paused=paused)
	except Exception:
		frappe.log_error(
			title="jarvis pattern learning: print-log snapshot ingest failed",
			message=frappe.get_traceback(),
		)
		try:
			frappe.db.rollback()
		except Exception:
			pass
		return {"error": True, "rows": 0, "events": 0, "snapshots": 0}


def _ingest_print_log(detector_id: str, max_rows: int, run=None, paused: bool = False) -> dict:
	if not frappe.db.exists("DocType", "Access Log"):
		return {"skipped": "Access Log doctype not present", "rows": 0, "events": 0, "snapshots": 0}
	if paused:
		# Mining already exhausted a run budget; never launch an extra
		# Access-Log pass on top of it. The watermark defers the tail to the
		# next run.
		return {"skipped": "mining run paused", "rows": 0, "events": 0, "snapshots": 0}
	enabled = frappe.db.get_value(DETECTOR_STATE, detector_id, "enabled")
	if enabled is not None and not int(enabled):
		return {"skipped": "detector disabled", "rows": 0, "events": 0, "snapshots": 0}

	from jarvis.learning.detectors.selling import PRINT_LOG_CHUNK, PRINT_LOG_SQL

	watermark = read_watermark(detector_id)
	last = str(watermark or EPOCH_WATERMARK)
	rows_total = 0
	events_total = 0
	snapshot_names: set = set()
	halted = None
	while rows_total < max_rows:
		if _window_closed(run):
			# Out of window between chunks: stop. Every committed chunk is
			# durable; the watermark resumes the rest next run.
			halted = "window"
			break
		rows = _db_runner(PRINT_LOG_SQL, {"watermark": last}) or []
		if not rows:
			break
		events = resolve_print_events(rows, _db_runner)
		last = str(rows[-1].get("created"))
		rows_total += len(rows)
		events_total += len(events)
		# One SMALL transaction per chunk: the chunk's upserts and the
		# watermark move commit together (a crash re-reads at most one chunk;
		# the atomic watermark keeps the merge idempotent), and no transaction
		# stays open across the pacing sleep below.
		for (period, company), payload in aggregate_events(events).items():
			snapshot_names.add(upsert_monthly(detector_id, period, company, payload))
		_advance_watermark(detector_id, last, len(rows))
		frappe.db.commit()
		if len(rows) < PRINT_LOG_CHUNK:
			break
		if not frappe.flags.in_test:
			time.sleep(CHUNK_SLEEP_S)  # after commit: no open transaction
	summary = {
		"rows": rows_total,
		"events": events_total,
		"snapshots": len(snapshot_names),
		"watermark": last if rows_total else str(watermark),
	}
	if halted:
		summary["halted"] = halted
	return summary


def _window_closed(run) -> bool:
	"""True when a SCHEDULED mining run's analysis window has closed (manual
	runs bypass the window, mirroring ``engine._pause_reason``). ``run`` is
	the Jarvis Pattern Run doc the engine passes through; None (direct or
	test calls) never gates."""
	if run is None or getattr(run, "trigger", None) == "manual":
		return False
	try:
		from jarvis.learning.orchestrator import should_pause_for_window

		return bool(should_pause_for_window(run.window_start_used, run.window_end_used, now_datetime()))
	except Exception:
		return False


def _advance_watermark(detector_id: str, watermark: str, rows_read: int) -> None:
	"""Move the stream position forward (same transaction as the upserts, so a
	crash re-reads rather than skips). Creates the state row if the
	after_migrate seed has not run."""
	_assert_fenced(DETECTOR_STATE)
	if frappe.db.exists(DETECTOR_STATE, detector_id):
		current = frappe.db.get_value(DETECTOR_STATE, detector_id, "rows_scanned_total") or 0
		frappe.db.set_value(
			DETECTOR_STATE,
			detector_id,
			{
				"last_watermark": watermark,
				"rows_scanned_total": int(current) + int(rows_read),
			},
			update_modified=False,
		)
	else:
		frappe.get_doc(
			{
				"doctype": DETECTOR_STATE,
				"detector_id": detector_id,
				"enabled": 1,
				"last_watermark": watermark,
				"rows_scanned_total": int(rows_read),
			}
		).insert(ignore_permissions=True)
