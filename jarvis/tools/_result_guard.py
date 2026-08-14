import json
from typing import Any

# Model-facing char budget for a single agent tool result, enforced on the
# openclaw-agent path (jarvis.api._dispatch_from_session) - NOT in get_list/query,
# which the dashboard builder/renderer also call. Compact + ensure_ascii=False so
# it matches what openclaw feeds the model and does not over-count non-Latin
# scripts. ~9K tokens, with headroom for openclaw's ~1.3x tool_call envelope.
MAX_RESULT_CHARS = 35_000

# Top-level list-valued keys we know how to truncate: query -> "rows",
# run_report -> "result". A bare list (get_list) is handled directly.
_ROW_KEYS = ("rows", "result")
# Metadata keys the guard owns in a truncation envelope; a colliding data key of
# the same name must never overwrite the guard's authoritative value.
_META_KEYS = ("_truncated", "shown", "total", "note")
_SAFETY = 200  # pad for note-digit / separator variance below the hard cap

# Routing help (model-facing): the full-data escapes are report_pdf for a saved
# report (runs server-side, returns a file, bypasses this cap) and
# export_document / export_excel for record exports on already-narrowed data.
# There is deliberately no "request specific rows" advice - no get_list offset
# exists - so the guidance is narrow / aggregate / report_pdf.
_NOTE = (
	"Result truncated to fit the context window: showing the first {shown} of "
	"{total} rows. PARTIAL - do not treat as complete; any file, summary, or count "
	"built from these rows is partial too. For the full answer: narrow the filter, "
	"aggregate with query, export narrowed records with export_document / "
	"export_excel, or use report_pdf for a saved report (server-side, no cap)."
)
_NOTE_NONE = (
	"Result too large: even one row exceeds the size budget. Request fewer fields "
	"(avoid fields=['*']), narrow the filter, aggregate with query, or use "
	"report_pdf for a saved report (server-side, returns the full data as a file, "
	"no cap)."
)
_NOTE_NONROW = (
	"Result too large: the non-row content (e.g. the SQL text or columns) alone "
	"exceeds the size budget, so ALL rows were dropped. PARTIAL, showing zero rows. "
	"Narrow the query so the surrounding content is smaller, or use report_pdf for "
	"a saved report (server-side, returns the full data as a file, no cap)."
)


def _size(obj) -> int:
	return len(json.dumps(obj, default=str, ensure_ascii=False, separators=(",", ":")))


def _find_list(data) -> tuple[str | None, str | None, list | None]:
	if isinstance(data, list):
		return "bare", None, data
	if isinstance(data, dict):
		for k in _ROW_KEYS:
			if isinstance(data.get(k), list):
				return "nested", k, data[k]
	return None, None, None


def _fit(rows: list, budget: int) -> list:
	"""Largest prefix of ``rows`` whose serialized size is <= ``budget`` (may be []).

	``_size(rows[:k])`` is monotonic non-decreasing in ``k``, so binary-search the
	largest fitting ``k``. This keeps a small leading row that fits even when later
	rows are huge (an average-based estimate would wrongly drop it)."""
	if not rows or budget <= 0:
		return []
	lo, hi = 0, len(rows)
	while lo < hi:
		mid = (lo + hi + 1) // 2
		if _size(rows[:mid]) <= budget:
			lo = mid
		else:
			hi = mid - 1
	return rows[:lo]


def _envelope(data, kind: str, key: str | None, kept: list, n: int, note: str) -> dict:
	"""Build the ``_truncated`` PARTIAL envelope. Metadata (``_truncated``/``shown``/
	``total``/``note``) is placed BEFORE the row key so it survives the chat SPA's
	4000-char pretty-print slice even when the surviving/non-row content is large."""
	if kind == "bare":
		return {"_truncated": True, "shown": len(kept), "total": n, "note": note, "rows": kept}
	out: dict = {"_truncated": True, "shown": len(kept), "total": n, "note": note}
	# Skip the row key AND the four metadata keys: a tool result carrying its own
	# top-level "total"/"note"/"shown"/"_truncated" must not clobber the guard's
	# authoritative values (metadata is emitted first for the debug view, but must
	# always win over a colliding data key).
	for k, v in data.items():
		if k != key and k not in _META_KEYS:
			out[k] = v
	out[key] = kept
	return out


def enforce_result_budget(data, tool: str) -> tuple[Any, dict | None]:
	"""Cap a tool result's model-facing serialized size on the openclaw-agent path.

	Returns ``(data, event)``. ``event is None`` means the result is within budget
	(or an empty/bare edge) and ``data`` is returned unchanged. Otherwise ``event``
	is a dict whose ``kind`` names the outcome for telemetry:

	  * ``"truncated"``       - a list-shaped result was cut to the rows that fit;
	                            ``data`` is replaced by a PARTIAL envelope.
	  * ``"uncapped"``        - no truncatable top-level row list; passed through
	                            unchanged (bounding a document is out of scope).
	  * ``"uncapped_nonrow"`` - a row list exists but the surrounding non-row
	                            content (e.g. a huge SQL string) alone exceeds the
	                            budget, so ALL rows are dropped to bound the blast
	                            radius; ``data`` is NOT returned unchanged.
	  * ``"measure_failed"``  - the result could not be serialized/measured; passed
	                            through unchanged (distinct from an in-budget None).

	Never raises: a measurement failure yields a ``measure_failed`` event and the
	original ``data``."""
	try:
		total = _size(data)
	except Exception:
		# Could not even measure the result: do not truncate blind, but surface it
		# as its own signal (distinct from an in-budget "small" result -> None).
		return data, {"kind": "measure_failed", "original_chars": None, "shown": None, "total": None}
	if total <= MAX_RESULT_CHARS:
		return data, None

	kind, key, rows = _find_list(data)
	if rows is None:
		return data, {"kind": "uncapped", "original_chars": total, "shown": None, "total": None}

	n = len(rows)
	overhead = _size(_envelope(data, kind, key, [], n, _NOTE_NONE))
	row_budget = MAX_RESULT_CHARS - overhead - _SAFETY
	if row_budget <= 0:
		# Non-row bulk (e.g. a huge query `sql`) is itself over budget: truncating
		# rows can't get under the cap. Bound the blast radius by dropping ALL rows
		# (an honest zero-row PARTIAL) rather than shipping the whole result.
		out = _envelope(data, kind, key, [], n, _NOTE_NONROW)
		return out, {"kind": "uncapped_nonrow", "original_chars": total, "shown": 0, "total": n}

	kept = _fit(rows, row_budget)
	note = _NOTE.format(shown=len(kept), total=n) if kept else _NOTE_NONE
	out = _envelope(data, kind, key, kept, n, note)
	return out, {"kind": "truncated", "original_chars": total, "shown": len(kept), "total": n}
